import hashlib
import importlib.util
import json
import os
import stat
from unittest.mock import patch
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from local_delivery.publish import Publisher


class Runtime:
    def __init__(self, root):
        self.plugin_dir = root / 'installed'
        self.plugin_dir.mkdir()
        self.path = self.plugin_dir / 'freetier2api-v0.1.23.so'
        self.path.write_bytes(b'old-plugin')
        self.version = '0.1.23'
        self.identity = 'container:process'
        self.puts = []
        self.accept = True

    def snapshot(self, **_options):
        return {'identity': self.identity, 'version': self.version,
                'path': str(self.path), 'selector': self.version,
                'credentials': ['unchanged'], 'models': ['alias'], 'config': 'same',
                'console_sha256': 'original',
                'artifact_sha256': hashlib.sha256(self.path.read_bytes()).hexdigest()}

    def select(self, version):
        self.puts.append(version)
        if self.accept or version == '0.1.23' or '-rollback.' in version:
            self.version = version
            self.path = self.plugin_dir / f'freetier2api-v{version}.so'

    def verify(self, version, baseline, console_sha256=None, metadata_version=None):
        if self.version != version:
            raise RuntimeError('registered version mismatch')
        return self.snapshot()


class Builder:
    def __init__(self):
        self.fail = False
        self.after = None

    def build(self, repo, commit, output):
        if self.fail:
            raise RuntimeError('test build failure')
        output.mkdir(parents=True)
        version = '0.1.23-fork.' + commit[:12]
        artifact = output / f'freetier2api-v{version}.so'
        artifact.write_bytes(b'new-plugin')
        result = {'artifact': str(artifact), 'version': version, 'commit': commit,
                  'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest()}
        if self.after:
            self.after()
        return result


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-q', '-b', 'freetier2api_fork')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        (self.repo / 'source').write_text('first')
        self.git('add', 'source')
        self.git('commit', '-qm', 'first')
        self.runtime = Runtime(self.root)
        self.builder = Builder()
        self.publisher = Publisher(self.repo, self.root / 'state', self.runtime, self.builder)

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True).strip()

    def test_publishes_clean_fork_commit_and_keeps_original_backup(self):
        result = self.publisher.publish()
        self.assertEqual(result['phase'], 'active')
        self.assertEqual(result['active']['commit'], self.git('rev-parse', 'HEAD'))
        self.assertEqual(self.runtime.version, result['active']['version'])
        self.assertEqual(Path(result['previous']['artifact']).read_bytes(), b'old-plugin')
        self.assertEqual(self.publisher.publish()['phase'], 'active')
        self.assertEqual(len(self.runtime.puts), 2, 'second publish must be a no-op')

    def test_dirty_and_main_never_build_or_select(self):
        self.builder.fail = True
        (self.repo / 'source').write_text('unfinished')
        self.assertIn('skipped', self.publisher.publish())
        self.git('checkout', '--', 'source')
        self.git('switch', '-qc', 'main')
        self.assertIn('skipped', self.publisher.publish())
        self.assertEqual(self.runtime.puts, [])

    def test_build_failure_keeps_runtime_and_latches_same_commit(self):
        self.builder.fail = True
        with self.assertRaisesRegex(RuntimeError, 'test build'):
            self.publisher.publish()
        self.assertEqual(self.runtime.puts, [])
        self.assertEqual(self.publisher.status()['phase'], 'build_failed')
        self.assertIn('skipped', self.publisher.publish())

    def test_newer_commit_during_build_is_not_published(self):
        def advance():
            (self.repo / 'source').write_text('newest')
            self.git('add', 'source')
            self.git('commit', '-qm', 'newer')
        self.builder.after = advance
        self.assertEqual(self.publisher.publish()['phase'], 'superseded')
        self.assertEqual(self.runtime.puts, [])

    def test_failed_selection_rolls_back_and_latches_candidate(self):
        self.runtime.accept = False
        with self.assertRaisesRegex(RuntimeError, 'version mismatch'):
            self.publisher.publish()
        self.assertTrue(self.runtime.version.startswith('0.1.23-rollback.'))
        self.assertEqual(self.publisher.status()['phase'], 'rolled_back')
        self.assertIn('skipped', self.publisher.publish())

    def test_interrupted_switch_recovers_before_any_new_build(self):
        self.publisher.publish()
        state = self.publisher.status()
        self.publisher.write(state, 'switching')
        self.builder.fail = True
        self.assertEqual(self.publisher.publish()['phase'], 'rolled_back')
        self.assertTrue(self.runtime.version.startswith('0.1.23-rollback.'))

    def test_rollback_is_verified_and_budget_never_restarts_host(self):
        self.publisher.publish()
        self.assertEqual(self.publisher.rollback()['phase'], 'rolled_back')
        self.assertTrue(self.runtime.version.startswith('0.1.23-rollback.'))
        self.publisher.max_reloads = 1
        state = self.publisher.status()
        state['failed_commit'] = None
        self.publisher.write(state, 'rolled_back')
        self.assertEqual(self.publisher.publish()['phase'], 'paused_budget')
        self.assertEqual(self.runtime.identity, 'container:process')

    def test_lock_contention_prevents_parallel_publication(self):
        from local_delivery.state import locked
        with locked(self.publisher.lock):
            with self.assertRaisesRegex(RuntimeError, 'Another plugin publication'):
                self.publisher.publish()
        self.assertEqual(self.runtime.puts, [])

    def test_recovery_never_reverts_new_external_runtime(self):
        self.publisher.publish()
        state = self.publisher.status()
        self.publisher.write(state, 'switching')
        self.runtime.version = '9.0.0'
        self.runtime.path = self.runtime.plugin_dir / 'freetier2api-v9.0.0.so'
        self.runtime.path.write_bytes(b'operator change')
        before = list(self.runtime.puts)
        with self.assertRaisesRegex(RuntimeError, 'external'):
            self.publisher.publish()
        self.assertEqual(self.runtime.puts, before)


    def test_rollback_after_build_failure_never_reverts_older_success(self):
        self.publisher.publish()
        active = self.runtime.version
        (self.repo / 'source').write_text('second')
        self.git('add', 'source')
        self.git('commit', '-qm', 'second')
        self.builder.fail = True
        with self.assertRaises(RuntimeError):
            self.publisher.publish()
        self.assertEqual(self.publisher.rollback()['observed_version'], active)
        self.assertEqual(self.runtime.version, active)

    def test_recovery_is_idempotent_after_recovered_generation_already_active(self):
        self.publisher.publish()
        result = self.publisher.rollback()
        version = self.runtime.version
        self.publisher.write(result, 'rolling_back')
        self.publisher.publish()
        self.assertEqual(self.runtime.version, version)

    def test_publishers_for_same_runtime_share_lock_across_state_directories(self):
        from local_delivery.state import locked
        other = Publisher(self.repo, self.root / 'other-state', self.runtime, self.builder)
        with locked(self.publisher.lock):
            with self.assertRaisesRegex(RuntimeError, 'Another plugin publication'):
                other.publish()
        self.assertEqual(self.runtime.puts, [])

    def test_idle_active_commit_adopts_host_restart_without_republishing(self):
        self.publisher.publish()
        self.runtime.identity = 'new-process'
        count = len(self.runtime.puts)
        result = self.publisher.publish()
        self.assertEqual(result['runtime_identity'], 'new-process')
        self.assertEqual(len(self.runtime.puts), count)

    def test_backup_sync_failure_prevents_any_runtime_selection(self):
        real_sync = os.fsync
        def fault(fd):
            if '/backups/' in os.readlink('/proc/self/fd/' + str(fd)):
                raise OSError('fixture backup sync failure')
            return real_sync(fd)
        with patch('os.fsync', side_effect=fault):
            with self.assertRaisesRegex(OSError, 'backup sync'):
                self.publisher.publish()
        self.assertEqual(self.runtime.puts, [])
        self.assertEqual(self.runtime.version, '0.1.23')

    def test_failed_backup_directory_sync_is_retried_before_any_selection(self):
        real_sync = os.fsync
        def fault(fd):
            path = os.readlink('/proc/self/fd/' + str(fd))
            if '/backups' in path and stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError('fixture directory sync failure')
            return real_sync(fd)
        with patch('os.fsync', side_effect=fault):
            for _ in range(2):
                with self.assertRaisesRegex(OSError, 'directory sync'):
                    self.publisher.publish(retry=True)
        self.assertEqual(self.runtime.puts, [])

if __name__ == '__main__':
    unittest.main()
