"""Exercise the shipped executable against temporary Git and OS/API fixtures."""
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import threading
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/dev-plugin'
IMAGE = 'golang@sha256:' + 'a' * 64


class CLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.make_writable)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'plugin'
        self.repo.mkdir()
        self.cpa = self.root / 'CLIProxyAPI'
        self.cpa.mkdir()
        self.config = self.repo / '.local/config.json'
        self.state = self.repo / '.local/delivery'
        self.git('init', '-q', '-b', 'freetier2api_fork')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('config', 'user.name', 'Fixture')
        (self.repo / '.gitignore').write_text('.local/\n')
        (self.repo / 'source').write_text('first\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'fixture')
        self.env = dict(os.environ)
        self.env['PYTHONDONTWRITEBYTECODE'] = '1'
        self.env['HOME'] = str(self.root / 'home')
        self.env['XDG_CONFIG_HOME'] = str(self.root / 'home/.config')
        self.env.pop('DOCKER_HOST', None)
        self.env.pop('DOCKER_CONTEXT', None)

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True).strip()

    def configure(self, **values):
        cfg = {'repo': str(self.repo), 'cpa_root': str(self.cpa), 'builder_image': IMAGE,
               'interval': 0.1, **values}
        self.config.parent.mkdir(mode=0o700, exist_ok=True)
        self.config.write_text(json.dumps(cfg))
        self.config.chmod(0o600)

    def cli(self, *args):
        return subprocess.run([str(SCRIPT), '--config', str(self.config), *args],
                              cwd=self.root, env=self.env, text=True, capture_output=True, timeout=10)

    def test_status_reads_local_state_without_contacting_runtime(self):
        self.configure()
        result = self.cli('status', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(result.stdout)
        self.assertTrue(status['configured'])
        self.assertEqual(status['phase'], 'idle')
        self.assertTrue(status['source']['eligible'])
        self.assertEqual(status['source']['commit'], self.git('rev-parse', 'HEAD'))
        self.assertEqual(status['state_dir'], str(self.state))
        self.assertFalse(status['paused'])
        self.assertFalse(self.state.exists(), 'reading status must not create delivery state')


    def test_dirty_and_main_are_skipped_without_running_docker(self):
        self.configure()
        (self.repo / 'source').write_text('unfinished')
        result = self.cli('publish', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('skipped', json.loads(result.stdout))
        self.git('checkout', '--', 'source')
        self.git('switch', '-qc', 'main')
        result = self.cli('publish', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('skipped', json.loads(result.stdout))
        self.assertFalse((self.state / 'state.json').exists())

    def test_pause_blocks_manual_publish_and_resume_clears_flag(self):
        self.configure()
        result = self.cli('pause', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['paused'])
        self.assertEqual((self.state / 'paused').stat().st_mode & 0o777, 0o600)
        result = self.cli('publish', '--retry', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['paused'])
        self.assertIn('skipped', json.loads(result.stdout))
        result = self.cli('resume', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)['paused'])
        self.assertFalse(json.loads(self.cli('status', '--json').stdout)['paused'])


    def fake_command(self, name, code):
        directory = self.root / 'bin'
        directory.mkdir(exist_ok=True)
        script = directory / name
        script.write_text('#!/usr/bin/env python3\n' + code)
        script.chmod(0o755)
        self.env['PATH'] = str(directory) + os.pathsep + os.environ['PATH']
        return script

    def start_watch(self):
        process = subprocess.Popen([str(SCRIPT), '--config', str(self.config), 'watch', '--json'],
                                   cwd=self.root, env=self.env, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def cleanup():
            if process.poll() is None:
                process.terminate()
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
        self.addCleanup(cleanup)
        return process

    def await_status(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        latest = None
        while time.monotonic() < deadline:
            result = self.cli('status', '--json')
            if result.returncode == 0:
                latest = json.loads(result.stdout)
                if predicate(latest):
                    return latest
            time.sleep(0.05)
        self.fail(f'expected status not observed: {latest}')

    def test_locked_publication_exits_without_touching_runtime(self):
        self.configure()
        plugin_dir = self.cpa / 'logs/plugins/linux/amd64'
        plugin_dir.mkdir(parents=True)
        with (plugin_dir / '.local-delivery.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.cli('publish', '--json')
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.state / 'state.json').exists())

    def test_watch_latches_runtime_failure_across_restart_and_new_commit_retries(self):
        self.configure()
        counter = self.root / 'attempts'
        self.fake_command('docker', 'from pathlib import Path\n'
                          f"with Path({str(counter)!r}).open('a') as f: f.write('attempt\\n')\n"
                          "raise SystemExit(19)\n")
        commit = self.git('rev-parse', 'HEAD')
        watcher = self.start_watch()
        status = self.await_status(lambda s: s.get('watcher', {}).get('failed_commit') == commit)
        self.assertTrue(status['watcher']['running'])
        time.sleep(0.35)
        self.assertEqual(len(counter.read_text().splitlines()), 1)
        watcher.terminate()
        stdout, stderr = watcher.communicate(timeout=5)
        self.assertEqual(watcher.returncode, 130, stderr)
        self.assertLessEqual(len(stdout.splitlines()), 4, stdout)
        watcher = self.start_watch()
        time.sleep(0.35)
        self.assertEqual(len(counter.read_text().splitlines()), 1)
        (self.repo / 'source').write_text('next commit')
        self.git('add', 'source')
        self.git('commit', '-qm', 'next')
        next_commit = self.git('rev-parse', 'HEAD')
        self.await_status(lambda s: s.get('watcher', {}).get('failed_commit') == next_commit)
        self.assertEqual(len(counter.read_text().splitlines()), 2)
        self.assertEqual(self.git('rev-parse', 'HEAD'), next_commit)
        watcher.terminate()
        watcher.communicate(timeout=5)


    def test_init_and_install_are_private_idempotent_and_start_only_on_opt_in(self):
        calls = self.root / 'systemctl.jsonl'
        self.fake_command('systemctl', 'import json,sys\nfrom pathlib import Path\n'
                          f"with Path({str(calls)!r}).open('a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n")
        args = ['init', '--repo', str(self.repo), '--cpa-root', str(self.cpa), '--builder-image', IMAGE, '--json']
        result = self.cli(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(calls.exists(), 'init must not interact with systemd')
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.config.parent.stat().st_mode & 0o777, 0o700)
        result = self.cli('install-service', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        unit = Path(json.loads(result.stdout)['unit'])
        self.assertEqual(unit.stat().st_mode & 0o777, 0o600)
        self.assertIn(str(self.config), unit.read_text())
        self.assertIn('UMask=0077', unit.read_text())
        before = unit.stat().st_mtime_ns, self.config.stat().st_mtime_ns
        result = self.cli('install-service', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(before, (unit.stat().st_mtime_ns, self.config.stat().st_mtime_ns))
        commands = [json.loads(line) for line in calls.read_text().splitlines()]
        self.assertEqual(commands, [['--user', 'daemon-reload'], ['--user', 'daemon-reload']])
        result = self.cli('install-service', '--start', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = [json.loads(line) for line in calls.read_text().splitlines()]
        self.assertEqual(commands[-1], ['--user', 'enable', '--now', 'freetier2api-local-delivery.service'])

    def test_service_rejects_invalid_or_secret_configuration_without_installing(self):
        calls = self.root / 'systemctl_called'
        self.fake_command('systemctl', f'from pathlib import Path\nPath({str(calls)!r}).touch()\n')
        self.configure(builder_image='golang:latest')
        result = self.cli('install-service', '--start', '--json')
        self.assertEqual(result.returncode, 2)
        self.assertFalse(calls.exists())
        self.configure(management_key='PRIVATE_TEST_CREDENTIAL')
        result = self.cli('install-service', '--start', '--json')
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('PRIVATE_TEST_CREDENTIAL', result.stdout + result.stderr)
        self.assertFalse(calls.exists())
        self.config.unlink()
        result = self.cli('install-service', '--start', '--json')
        self.assertEqual(result.returncode, 2)
        self.assertFalse(calls.exists())
        self.assertFalse((self.root / 'home/.config/systemd/user').exists())


    def make_writable(self):
        for directory, _, _ in os.walk(self.tmp.name):
            Path(directory).chmod(0o700)

    def runtime_fixture(self, behavior='fail'):
        '''A loopback CPA protocol endpoint plus external Docker command fixture.'''
        self.runtime_calls = []
        self.plugin_dir = self.cpa / 'logs/plugins/linux/amd64'
        self.plugin_dir.mkdir(parents=True)
        (self.plugin_dir / 'freetier2api-v0.1.23.so').write_bytes(b'original-native-fixture')
        runtime_state = self.root / 'runtime.json'
        runtime_state.write_text(json.dumps({'version': '0.1.23', 'loaded': ['0.1.23']}))
        self.build_counter = self.root / 'builds'
        self.cleaned = self.root / 'cleaned'
        self.docker_pid = self.root / 'docker_pid'
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                outer.runtime_calls.append(('GET', self.path))
                version = json.loads(runtime_state.read_text())['version']
                values = {
                    '/v8/management/config': {'access': {'api-keys': ['PRIVATE_API_FIXTURE']},
                        'plugins': {'enabled': True, 'configs': {'freetier2api': {'enabled': True,
                                   'store': {'version': version}, 'keep': 'untouched'}}}},
                    '/v8/management/plugins': {'plugins': [{'id': 'freetier2api', 'registered': True,
                        'effective_enabled': True, 'metadata': {'version': version.split('-rollback.')[0]},
                        'path': '/CLIProxyAPI/logs/plugins/linux/amd64/freetier2api-v' + version + '.so',
                        'active_path': '/CLIProxyAPI/logs/plugins/linux/amd64/freetier2api-v' + version + '.so'}]},
                    '/v8/management/credentials': {'files': [{'name': 'fixture', 'type': 'freetier',
                                                             'disabled': False, 'note': 'untouched'}]},
                    '/v1/models': {'data': [{'id': 'fixture-model'}]},
                    '/v0/resource/plugins/freetier2api/console': b'<html>fixture</html>',
                }
                if self.path not in values:
                    self.send_response(404)
                    self.end_headers()
                    return
                body = values[self.path]
                body = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_PUT(self):
                outer.runtime_calls.append(('PUT', self.path))
                if self.path != '/v8/management/config/plugins/configs/freetier2api/store/version':
                    self.send_response(404)
                    self.end_headers()
                    return
                version = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                data = json.loads(runtime_state.read_text())
                data['version'] = version
                if version not in data['loaded']:
                    data['loaded'].append(version)
                runtime_state.write_text(json.dumps(data))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{}')

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.configure(url=f'http://127.0.0.1:{server.server_port}', timeout=0.1)
        app = self.repo / 'internal/app/app.go'
        app.parent.mkdir(parents=True)
        app.write_text('package app\nconst DefaultVersion = "0.1.23"\n')
        console = self.repo / 'internal/admin/console.html'
        console.parent.mkdir(parents=True)
        console.write_bytes(b'<html>fixture</html>')
        self.git('add', '.')
        self.git('commit', '-qm', 'build fixture')
        code = f'''import json,os,struct,sys,time
from pathlib import Path
root = Path({str(self.root)!r})
cpa = Path({str(self.cpa)!r})
plugin_dir = Path({str(self.plugin_dir)!r})
args = sys.argv[1:]
if args[:2] == ['context', 'inspect']:
    print(json.dumps([{{'Endpoints': {{'docker': {{'Host': 'unix:///fixture'}}}}}}]))
elif args[0] == 'inspect':
    print(json.dumps([{{'Id': json.loads((root/'runtime.json').read_text()).get('identity', 'fixture-container'), 'State': {{'Running': True}},
        'HostConfig': {{'NetworkMode': 'host'}}, 'Config': {{'Env': ['MANAGEMENT_PASSWORD=PRIVATE_MANAGEMENT_FIXTURE']}},
        'Mounts': [{{'Destination': '/CLIProxyAPI/logs', 'Source': str(cpa/'logs'), 'RW': True}},
                   {{'Destination': '/workspace', 'Source': str(cpa), 'RW': True}}]}}]))
elif args[0] == 'top':
    print('PID COMMAND\\n123 CLIProxyAPI')
elif args[0] == 'exec':
    state = json.loads((root/'runtime.json').read_text())
    images = []
    for version in state['loaded']:
        st = (plugin_dir / ('freetier2api-v'+version+'.so')).stat()
        images.append([f'{{os.major(st.st_dev):x}}:{{os.minor(st.st_dev):x}}', str(st.st_ino)])
    print(json.dumps([['123', '456', images]]))
elif args[:2] == ['image', 'inspect']:
    print(json.dumps({{'Os': 'linux', 'Architecture': 'amd64'}}))
elif args[0] == 'run':
    with (root/'builds').open('a') as f: f.write('build\\n')
    (root/'docker_pid').write_text(str(os.getpid()))
    behavior = {behavior!r}
    if behavior == 'sleep':
        time.sleep(60)
    if behavior == 'gate' and len((root/'builds').read_text().splitlines()) == 1:
        while not (root/'continue').exists(): time.sleep(0.03)
    if behavior not in ('success', 'gate'):
        raise SystemExit(42)
    out = Path(next(value[:-5] for value in args if value.endswith(':/out')))
    header = bytearray(64)
    header[:7] = b'\\x7fELF\\x02\\x01\\x01'
    struct.pack_into('<HH', header, 16, 3, 62)
    (out/args[-1]).write_bytes(header)
    (out/'symbols.txt').write_text('\\n'.join(f'{{i}}: 0001 1 FUNC GLOBAL DEFAULT 12 {{symbol}}' for i,symbol in enumerate(['cliproxy_plugin_init','cliproxyPluginCall','cliproxyPluginFree','cliproxyPluginShutdown'])))
elif args[:2] == ['rm', '-f']:
    (root/'cleaned').touch()
else:
    raise SystemExit(99)
'''
        self.fake_command('docker', code)

    def test_watch_build_failure_is_visible_and_retry_is_explicit(self):
        self.runtime_fixture()
        watcher = self.start_watch()
        status = self.await_status(lambda value: value['phase'] == 'build_failed')
        self.assertEqual(status['failed_commit'], self.git('rev-parse', 'HEAD'))
        time.sleep(0.35)
        self.assertEqual(len(self.build_counter.read_text().splitlines()), 1)
        watcher.terminate()
        output, error = watcher.communicate(timeout=5)
        self.assertNotIn('PRIVATE_', output + error)
        result = self.cli('publish', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('skipped', json.loads(result.stdout))
        self.assertEqual(len(self.build_counter.read_text().splitlines()), 1)
        result = self.cli('publish', '--retry', '--json')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(self.build_counter.read_text().splitlines()), 2)
        self.assertTrue(all(method == 'GET' for method, _ in self.runtime_calls))

    def test_interrupting_watch_cancels_running_docker_and_releases_lock(self):
        self.runtime_fixture('sleep')
        watcher = self.start_watch()
        self.await_status(lambda value: value['phase'] == 'building' and self.docker_pid.exists())
        child_pid = int(self.docker_pid.read_text())
        watcher.terminate()
        output, error = watcher.communicate(timeout=5)
        self.assertEqual(watcher.returncode, 130, error)
        self.assertTrue(self.cleaned.exists(), 'builder cancellation must remove its named container')
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)
        status = json.loads(self.cli('status', '--json').stdout)
        self.assertFalse(status['watcher']['running'])
        self.assertEqual(status['phase'], 'build_failed')
        self.assertTrue(all(method == 'GET' for method, _ in self.runtime_calls))
        self.assertNotIn('PRIVATE_', output + error)

    def test_publish_and_paused_rollback_verify_actual_runtime_seam(self):
        self.runtime_fixture('success')
        result = self.cli('publish', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        published = json.loads(result.stdout)
        self.assertEqual(published['phase'], 'active')
        self.assertEqual(published['active']['commit'], self.git('rev-parse', 'HEAD'))
        self.assertEqual(self.cli('pause').returncode, 0)
        result = self.cli('rollback', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        rolled_back = json.loads(result.stdout)
        self.assertEqual(rolled_back['phase'], 'rolled_back')
        self.assertTrue(rolled_back['paused'])
        self.assertTrue(rolled_back['observed_version'].startswith('0.1.23'))
        self.assertNotIn('PRIVATE_', result.stdout + result.stderr)
        allowed_put = '/v8/management/config/plugins/configs/freetier2api/store/version'
        self.assertTrue(all(method == 'GET' or path == allowed_put for method, path in self.runtime_calls))


    def test_status_explains_version_source_watcher_and_reload_budget(self):
        self.runtime_fixture('success')
        result = self.cli('publish', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        released = json.loads(result.stdout)
        result = self.cli('status')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(released['active']['version'], result.stdout)
        self.assertIn(released['active']['commit'][:12], result.stdout)
        self.assertIn('freetier2api_fork', result.stdout)
        self.assertIn('干净', result.stdout)
        self.assertIn('停止', result.stdout)
        self.assertIn('/8', result.stdout)

    def test_configuration_limits_native_reload_budget_to_eight(self):
        self.configure(max_reloads=9)
        result = self.cli('status', '--json')
        self.assertEqual(result.returncode, 2)
        self.assertIn('配置', result.stderr)

    def test_failed_recovery_latches_instead_of_retrying_every_poll(self):
        self.configure()
        self.state.mkdir(parents=True)
        commit = self.git('rev-parse', 'HEAD')
        (self.state / 'state.json').write_text(json.dumps({'phase': 'switching', 'target_commit': commit,
            'previous': {'version': '0.1.23'}, 'baseline': {'identity': 'fixture'}}))
        counter = self.root / 'recovery_attempts'
        self.fake_command('docker', 'from pathlib import Path\n'
                          f"with Path({str(counter)!r}).open('a') as f: f.write('attempt\\n')\n"
                          "raise SystemExit(19)\n")
        watcher = self.start_watch()
        self.await_status(lambda s: s.get('watcher', {}).get('failed_commit') == commit)
        time.sleep(0.35)
        self.assertEqual(len(counter.read_text().splitlines()), 1)
        watcher.terminate()
        watcher.communicate(timeout=5)
        watcher = self.start_watch()
        time.sleep(0.35)
        self.assertEqual(len(counter.read_text().splitlines()), 1)
        watcher.terminate()
        watcher.communicate(timeout=5)


    def test_watcher_respects_pause_and_delivers_each_new_clean_commit_once(self):
        self.runtime_fixture('success')
        self.assertEqual(self.cli('pause').returncode, 0)
        watcher = self.start_watch()
        self.await_status(lambda value: value.get('watcher', {}).get('last_event') == 'paused')
        time.sleep(0.15)
        self.assertFalse(self.build_counter.exists())
        self.assertEqual(self.runtime_calls, [])
        self.assertEqual(self.cli('resume').returncode, 0)
        self.await_status(lambda value: value['phase'] == 'active')
        self.assertEqual(self.cli('pause').returncode, 0)
        (self.repo / 'source').write_text('next clean commit')
        self.git('add', 'source')
        self.git('commit', '-qm', 'next')
        commit = self.git('rev-parse', 'HEAD')
        time.sleep(0.2)
        self.assertEqual(len(self.build_counter.read_text().splitlines()), 1)
        self.assertEqual(self.cli('resume').returncode, 0)
        self.await_status(lambda value: value.get('active', {}).get('commit') == commit)
        time.sleep(0.2)
        self.assertEqual(len(self.build_counter.read_text().splitlines()), 2)
        watcher.terminate()
        watcher.communicate(timeout=5)

    def test_active_watcher_verifies_host_restart_without_another_build(self):
        self.runtime_fixture('success')
        result = self.cli('publish', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        runtime_file = self.root / 'runtime.json'
        runtime = json.loads(runtime_file.read_text())
        runtime['identity'] = 'restarted-container'
        runtime_file.write_text(json.dumps(runtime))
        self.runtime_calls.clear()
        watcher = self.start_watch()
        self.await_status(lambda value: value.get('runtime_identity', '').startswith('restarted-container:'))
        self.assertEqual(len(self.build_counter.read_text().splitlines()), 1)
        self.assertTrue(all(method == 'GET' for method, _ in self.runtime_calls))
        watcher.terminate()
        watcher.communicate(timeout=5)


    def test_mutating_commands_restrict_existing_private_state_permissions(self):
        self.configure()
        self.config.parent.chmod(0o755)
        self.state.mkdir(mode=0o755)
        result = self.cli('pause', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.config.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)

    def test_runtime_failure_has_actionable_sanitized_reason(self):
        self.configure()
        self.fake_command('docker', "import sys\nprint('PRIVATE_TEST_CREDENTIAL', file=sys.stderr)\nraise SystemExit(17)\n")
        result = self.cli('publish')
        self.assertEqual(result.returncode, 1)
        self.assertIn('Docker', result.stderr)
        self.assertNotIn('PRIVATE_TEST_CREDENTIAL', result.stderr + result.stdout)


    def test_superseded_dirty_build_retries_when_same_head_becomes_clean(self):
        self.runtime_fixture('gate')
        watcher = self.start_watch()
        self.await_status(lambda value: value['phase'] == 'building' and self.build_counter.exists())
        (self.repo / 'source').write_text('unfinished while building')
        (self.root / 'continue').touch()
        self.await_status(lambda value: value['phase'] == 'superseded')
        self.git('checkout', '--', 'source')
        self.await_status(lambda value: value['phase'] == 'active')
        self.assertEqual(len(self.build_counter.read_text().splitlines()), 2)
        watcher.terminate()
        stdout, _ = watcher.communicate(timeout=5)
        events = [json.loads(line) for line in stdout.splitlines()]
        self.assertTrue(any(event['event'] == 'superseded' for event in events))
        self.assertFalse(any(event['event'] == 'published' and event.get('phase') != 'active' for event in events))

    def test_budget_pause_rechecks_same_commit_after_explicit_resume(self):
        self.runtime_fixture('success')
        runtime_file = self.root / 'runtime.json'
        runtime = json.loads(runtime_file.read_text())
        for patch in range(24, 30):
            version = f'0.1.{patch}'
            (self.plugin_dir / f'freetier2api-v{version}.so').write_bytes(b'old mapped fixture')
            runtime['loaded'].append(version)
        runtime_file.write_text(json.dumps(runtime))
        watcher = self.start_watch()
        self.await_status(lambda value: value['phase'] == 'paused_budget')
        self.assertFalse(self.build_counter.exists())
        self.assertEqual(self.cli('pause').returncode, 0)
        self.await_status(lambda value: value.get('watcher', {}).get('last_event') == 'paused')
        runtime['loaded'] = ['0.1.23']
        runtime['identity'] = 'restarted-container'
        runtime_file.write_text(json.dumps(runtime))
        self.assertEqual(self.cli('resume').returncode, 0)
        self.await_status(lambda value: value['phase'] == 'active')
        self.assertEqual(len(self.build_counter.read_text().splitlines()), 1)
        watcher.terminate()
        watcher.communicate(timeout=5)


    def test_status_reports_abandoned_build_as_interrupted_not_running(self):
        self.configure()
        self.state.mkdir(parents=True)
        (self.state / 'state.json').write_text(json.dumps({'phase': 'building',
            'target_commit': self.git('rev-parse', 'HEAD')}))
        result = self.cli('status', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value['phase'], 'build_interrupted')
        self.assertEqual(value['persisted_phase'], 'building')
        self.assertIn('publish --retry', value['error'])
        self.assertFalse(value['publishing'])


    def test_repeated_busy_events_do_not_persist_a_false_commit_latch_after_kill(self):
        self.configure()
        plugin_dir = self.cpa / 'logs/plugins/linux/amd64'
        plugin_dir.mkdir(parents=True)
        counter = self.root / 'after_busy_attempts'
        self.fake_command('docker', 'from pathlib import Path\n'
                          f"with Path({str(counter)!r}).open('a') as f: f.write('attempt\\n')\n"
                          "raise SystemExit(19)\n")
        with (plugin_dir / '.local-delivery.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            watcher = self.start_watch()
            self.await_status(lambda value: value.get('watcher', {}).get('last_event') == 'busy')
            time.sleep(0.35)
            watcher.kill()
            stdout, _ = watcher.communicate(timeout=5)
            self.assertEqual(sum(json.loads(line)['event'] == 'busy' for line in stdout.splitlines()), 1)
        watcher = self.start_watch()
        commit = self.git('rev-parse', 'HEAD')
        self.await_status(lambda value: value.get('watcher', {}).get('failed_commit') == commit)
        self.assertEqual(len(counter.read_text().splitlines()), 1)
        watcher.terminate()
        watcher.communicate(timeout=5)


if __name__ == '__main__':
    unittest.main()
