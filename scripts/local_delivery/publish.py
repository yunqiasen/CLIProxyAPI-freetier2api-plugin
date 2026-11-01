"""Commit-scoped publication, durable intent, and verified recovery."""
from pathlib import Path
import os
import re
import shutil
import subprocess
import time
import uuid

from .state import digest, load, locked, save

BRANCH = 'freetier2api_fork'
INTERRUPTED = {'switching', 'rolling_back', 'recovery_failed'}


class Publisher:
    def __init__(self, repo, state_dir, runtime, builder, max_reloads=8):
        self.repo = Path(repo).resolve()
        self.root = Path(state_dir).resolve()
        self.runtime = runtime
        self.builder = builder
        self.max_reloads = max_reloads
        self.state_file = self.root / 'state.json'
        # A single runtime-wide lock also serializes callers using another state directory.
        self.lock = self.runtime.plugin_dir / '.local-delivery.lock'

    def status(self):
        return load(self.state_file)

    def write(self, state, phase, **values):
        state.update(values, phase=phase, updated_at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
        save(self.state_file, state)
        return state

    def eligible(self):
        def git(*args):
            return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True).strip()
        if git('branch', '--show-current') != BRANCH:
            return None
        if git('status', '--porcelain', '--untracked-files=all'):
            return None
        return git('rev-parse', 'HEAD')

    def backup(self, snapshot):
        source = Path(snapshot['path'])
        if source.is_symlink() or not source.is_file():
            raise RuntimeError('The loaded plugin is not a regular local file')
        checksum = digest(source)
        target = self.root / 'backups' / checksum / source.name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not target.exists():
            temporary = target.with_name('.' + target.name + '.' + uuid.uuid4().hex)
            try:
                with source.open('rb') as src, temporary.open('xb') as dst:
                    os.chmod(temporary, 0o600)
                    shutil.copyfileobj(src, dst)
                    dst.flush()
                    os.fsync(dst.fileno())
                if digest(temporary) != checksum:
                    raise RuntimeError('Plugin changed while backing it up')
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        if digest(target) != checksum:
            raise RuntimeError('Plugin backup checksum mismatch')
        # A previous interrupted directory sync may have left the file present;
        # revalidate durability on every attempt, including existing backups.
        with target.open('rb') as stream:
            os.fsync(stream.fileno())
        for parent in (target.parent, target.parent.parent, self.root):
            directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return {'version': snapshot['version'], 'artifact': str(target), 'sha256': checksum,
                'filename': source.name, 'metadata_version': snapshot.get('metadata_version', snapshot['version']),
                'console_sha256': snapshot.get('console_sha256')}

    def place(self, manifest):
        source = Path(manifest['artifact'])
        version = manifest['version']
        if not re.fullmatch(r'[0-9][0-9A-Za-z.+-]*', version) or '-v' in version:
            raise RuntimeError('Invalid plugin version')
        name = manifest.get('filename', f'freetier2api-v{version}.so')
        if name != f'freetier2api-v{version}.so':
            raise RuntimeError('Plugin filename/version mismatch')
        final = self.runtime.plugin_dir / name
        if digest(source) != manifest['sha256']:
            raise RuntimeError('Artifact checksum mismatch')
        if final.exists() or final.is_symlink():
            if final.is_symlink() or digest(final) != manifest['sha256']:
                raise RuntimeError('Installed version already exists with different bytes')
            return final
        temporary = final.with_name('.' + final.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with source.open('rb') as src, temporary.open('xb') as dst:
                shutil.copyfileobj(src, dst)
                dst.flush()
                os.fsync(dst.fileno())
            temporary.chmod(0o644)
            # A hard link atomically publishes without replacing another publisher's file.
            os.link(temporary, final)
            directory = os.open(final.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
        return final

    def verify_release(self, release, baseline):
        observed = self.runtime.verify(release['version'], baseline, release.get('console_sha256'),
                                       release.get('metadata_version', release['version']))
        if observed.get('artifact_sha256') != release['sha256']:
            raise RuntimeError('Loaded plugin artifact checksum mismatch')
        return observed

    def recover(self, state):
        previous = state.get('previous')
        baseline = state.get('baseline')
        if not previous or not baseline:
            raise RuntimeError('No durable previous version is available')
        candidate = state.get('candidate', {})
        current = self.runtime.snapshot(control_only=True,
                                        known=(previous, candidate, state.get('recovery')))
        recorded_recovery = state.get('recovery') or {}
        allowed = {previous['version'], candidate.get('version'), recorded_recovery.get('version')}
        selectors = allowed | {baseline.get('selector')}
        if (current['version'] not in allowed or current.get('selector') not in selectors
                or current['identity'] != baseline['identity']
                or current.get('config') != baseline.get('config')):
            self.write(state, 'recovery_failed', error='An external runtime change needs inspection before recovery')
            raise RuntimeError('An external runtime change prevents automatic recovery')
        self.write(state, 'rolling_back')
        try:
            if current['version'] == recorded_recovery.get('version'):
                recovery = recorded_recovery
            elif current['version'] == previous['version'] and not state.get('selection_attempted'):
                # No candidate selection happened, so the old tasks were never quiesced.
                recovery = previous
            else:
                # Go libraries are NODELETE and have singleton host callbacks. A fresh
                # inode/path is required, even when restoring identical previous bytes.
                count = current.get('mapped_images', state.get('reload_count', 0))
                if count >= self.max_reloads:
                    raise RuntimeError('Native reload budget has no remaining rollback slot')
                generation = previous.get('metadata_version', previous['version']) + '-rollback.' + uuid.uuid4().hex[:12]
                recovery = {**previous, 'version': generation,
                            'filename': f'freetier2api-v{generation}.so'}
                self.write(state, 'rolling_back', recovery=recovery, reload_count=count + 1)
            self.place(recovery)
            self.runtime.select(recovery['version'])
            observed = self.verify_release(recovery, baseline)
            self.write(state, 'rolled_back', observed_version=observed['version'], recovery=recovery,
                       failed_commit=state.get('target_commit'))
        except BaseException:
            self.write(state, 'recovery_failed', error='Rollback verification failed; inspect local logs before resuming')
            raise
        return state

    def rollback(self):
        with locked(self.lock):
            state = self.status()
            if state.get('phase') in {'building', 'build_failed', 'superseded', 'paused_budget'}:
                return {**state, 'skipped': 'No new runtime was selected by the last attempt'}
            return self.recover(state)

    def publish(self, retry=False):
        with locked(self.lock):
            state = self.status()
            if state.get('phase') in INTERRUPTED:
                # Recovery never silently continues to another candidate.
                return self.recover(state)
            commit = self.eligible()
            if not commit:
                return {**state, 'skipped': 'Expected a clean freetier2api_fork checkout'}
            if state.get('failed_commit') == commit and not retry:
                return {**state, 'skipped': 'Failed commit is latched; inspect then use publish --retry'}
            if state.get('phase') == 'active' and state.get('active', {}).get('commit') == commit:
                current = self.runtime.snapshot()
                self.verify_release(state['active'], current)
                # Later user setting changes and normal CPA restarts are not a new
                # plugin publication. Use current state for any future rollback.
                if any(current.get(k) != state['baseline'].get(k)
                       for k in ('identity', 'config', 'credentials', 'models')):
                    self.write(state, 'active', baseline=current, runtime_identity=current['identity'],
                               reload_count=current.get('mapped_images', 0))
                return state
            baseline = self.runtime.snapshot()
            identity = baseline['identity']
            count = baseline.get('mapped_images', state.get('reload_count', 0) if state.get('runtime_identity') == identity else 0)
            if count + 2 > self.max_reloads:
                return self.write(state, 'paused_budget', error='Native reload budget reached; explicit CPA maintenance required', runtime_identity=identity, reload_count=count)
            output = self.root / 'builds' / (commit[:12] + '-' + uuid.uuid4().hex[:8])
            self.write(state, 'building', target_commit=commit, error=None)
            try:
                manifest = self.builder.build(self.repo, commit, output)
            except BaseException:
                self.write(state, 'build_failed', failed_commit=commit, error='Build failed; see the private build log')
                raise
            if self.eligible() != commit:
                return self.write(state, 'superseded', error=None)
            # Inspect again after the build; refuse to overwrite an external operator's switch.
            latest = self.runtime.snapshot()
            if any(latest.get(k) != baseline.get(k) for k in ('identity', 'version', 'selector', 'config', 'credentials', 'models', 'console_sha256', 'artifact_sha256')):
                return self.write(state, 'superseded', error='Runtime changed during build; no version was selected')
            previous = self.backup(baseline)
            self.write(state, 'switching', previous=previous, baseline=baseline,
                       candidate=manifest, recovery=None, selection_attempted=False,
                       runtime_identity=identity, reload_count=count + 1,
                       failed_commit=commit)
            try:
                # Pin the current binary before the candidate can be discovered.
                self.runtime.select(previous['version'])
                self.verify_release(previous, baseline)
                self.place(manifest)
                if self.eligible() != commit:
                    # Keep the old version pinned and exclude an unused candidate from discovery.
                    candidate_path = self.runtime.plugin_dir / f"freetier2api-v{manifest['version']}.so"
                    if candidate_path.is_file() and digest(candidate_path) == manifest['sha256']:
                        candidate_path.unlink()
                    return self.write(state, 'superseded', error='New source detected before switch')
                # The old plugin may have been quiesced even if replacement fails.
                self.write(state, 'switching', selection_attempted=True)
                self.runtime.select(manifest['version'])
                observed = self.verify_release(manifest, baseline)
                self.write(state, 'active', active=manifest, observed_version=observed['version'], failed_commit=None, error=None)
            except BaseException:
                self.recover(state)
                raise
            return state
