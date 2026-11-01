"""Opt-in watcher control and user-service installation (no implicit startup)."""
import contextlib
import copy
import os
from pathlib import Path
import signal
import time

from .publish import INTERRUPTED
from .state import load, locked, save


class Cancellation:
    def __init__(self):
        self.cancelled = False

    def request(self, _signum, _frame):
        if not self.cancelled:
            self.cancelled = True
            # subprocess.run and DockerBuilder both unwind and clean their child.
            raise KeyboardInterrupt()


@contextlib.contextmanager
def cancellation_signals():
    cancel = Cancellation()
    previous = {signum: signal.signal(signum, cancel.request)
                for signum in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield cancel
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def watcher_status(root):
    state = load(Path(root) / 'watcher.json')
    pid = state.get('pid')
    if state.get('running') and isinstance(pid, int):
        try:
            os.kill(pid, 0)
        except OSError:
            state['running'] = False
    else:
        state['running'] = False
    return state


def failure_message(error):
    text = str(error)
    if text == 'Another plugin publication is running':
        return '另一发布正在执行；稍后重试。'
    if 'Docker' in text or text.startswith('docker '):
        return '本地 Docker 检查或执行失败；确认 Docker 与 CPA 容器状态，查看私有构建日志。'
    if text.startswith('build ') or text.startswith('Build '):
        return '构建失败；查看 .local/delivery/builds 内的私有 build.log，处理后运行 publish --retry。'
    if text.startswith('CPA maintenance'):
        return 'CPA 本地管理请求失败；确认容器、端口和容器内凭据，检查后运行 publish --retry。'
    if 'runtime change' in text or 'verification' in text or 'checksum' in text:
        return '运行版本或状态校验失败；先查看 status，检查当前插件后手动 rollback 或 publish --retry。'
    return '本次交付失败；查看 status 与私有构建日志，检查后手动 publish --retry。'


def watch(cfg, publisher, inspect_source, emit, cancel):
    root = Path(cfg['state_dir'])
    with locked(root / 'watch.lock'):
        state = load(root / 'watcher.json')
        state.update(running=True, pid=os.getpid())
        persisted = None

        def persist():
            nonlocal persisted
            if state != persisted:
                save(root / 'watcher.json', state)
                persisted = copy.deepcopy(state)

        persist()
        last_event = None
        manual_success = state.get('manual_success', {})
        next_verification = 0
        next_attempt = 0
        retry_commit = None
        resume_sequence = load(root / 'resume.json')

        def event(kind, **values):
            nonlocal last_event
            payload = {'event': kind, **values}
            state.update(last_event=kind)
            persist()
            if payload != last_event:
                emit(payload)
                last_event = payload

        def clear_failure():
            for key in ('failed_commit', 'error', 'failure_phase', 'recovery_required'):
                state.pop(key, None)

        def attempt(commit, phase, verification=False):
            nonlocal next_attempt, retry_commit
            state.update(last_attempted_commit=commit)
            persist()
            try:
                result = publisher.publish(retry=phase in ('superseded', 'paused_budget'))
            except Exception as error:
                if cancel.cancelled:
                    return
                if str(error) == 'Another plugin publication is running':
                    state.pop('last_attempted_commit', None)
                    event('busy', phase=phase, commit=commit)
                else:
                    state.update(failed_commit=commit, failure_phase=phase,
                                 recovery_required=phase in INTERRUPTED,
                                 error=failure_message(error))
                    event('failed', phase=publisher.status().get('phase', 'idle'), commit=commit, error=state['error'])
            else:
                clear_failure()
                if result.get('failed_commit'):
                    state['failed_commit'] = result['failed_commit']
                result_phase = result.get('phase', 'idle')
                if result_phase in ('superseded', 'paused_budget') or result.get('skipped') and not result.get('failed_commit'):
                    state.pop('last_attempted_commit', None)
                    retry_commit = commit
                    delay = 60 if result_phase == 'paused_budget' or result.get('error') else max(1, cfg['interval'])
                    next_attempt = time.monotonic() + delay
                values = {'phase': result_phase, 'commit': commit}
                if result.get('skipped'):
                    values['skipped'] = result['skipped']
                event('skipped' if result.get('skipped') else result_phase, **values)

        try:
            while not cancel.cancelled:
                try:
                    source = inspect_source(cfg['repo'])
                    commit = source['commit']
                    journal = publisher.status()
                    phase = journal.get('phase', 'idle')
                    resumed = load(root / 'resume.json')
                    if resumed != resume_sequence:
                        resume_sequence = resumed
                        next_attempt = 0
                    control = load(root / 'manual-success.json')
                    if control != manual_success:
                        manual_success = control
                        state['manual_success'] = control
                        if control.get('commit') == commit:
                            clear_failure()
                            next_verification = 0
                    if (root / 'paused').exists():
                        event('paused', paused=True, phase=phase)
                    elif phase in INTERRUPTED:
                        if state.get('recovery_required') or phase == 'recovery_failed':
                            event('recovery_required', phase=phase, commit=commit)
                        else:
                            attempt(commit, phase)
                    elif not source['eligible']:
                        event('ineligible', phase=phase, commit=commit, branch=source['branch'], clean=source['clean'])
                    elif phase == 'active' and journal.get('active', {}).get('commit') == commit:
                        # A successful explicit retry after a failed build clears the watcher latch.
                        if state.get('failure_phase') != 'active':
                            clear_failure()
                        if state.get('failed_commit') == commit:
                            event('latched', phase=phase, commit=commit)
                        elif time.monotonic() >= next_verification:
                            next_verification = time.monotonic() + 60
                            attempt(commit, phase, verification=True)
                        else:
                            event('active', phase=phase, commit=commit)
                    elif phase in ('superseded', 'paused_budget'):
                        if commit == retry_commit and time.monotonic() < next_attempt:
                            event('waiting', phase=phase, commit=commit)
                        else:
                            attempt(commit, phase)
                    elif state.get('last_attempted_commit') == commit or journal.get('failed_commit') == commit:
                        event('latched', phase=phase, commit=commit)
                    elif commit == retry_commit and time.monotonic() < next_attempt:
                        event('waiting', phase=phase, commit=commit)
                    else:
                        attempt(commit, phase)
                except KeyboardInterrupt:
                    cancel.cancelled = True
                    break
                except Exception:
                    if cancel.cancelled:
                        break
                    state['error'] = '读取本地源码或状态失败。'
                    event('source_error', error=state['error'])
                if not cancel.cancelled:
                    time.sleep(cfg['interval'])
        except KeyboardInterrupt:
            cancel.cancelled = True
        finally:
            state.update(running=False, pid=None, last_event='stopped')
            persist()
        return 130 if cancel.cancelled else 0


def install(script, config, *, enable=False, start=False):
    """Write one owner-only user unit; starting it always needs an explicit flag."""
    import subprocess
    import sys
    import tempfile

    def quoted(value):
        text = str(value)
        if any(ord(character) < 32 or ord(character) == 127 for character in text):
            raise ValueError('Invalid service path')
        return '"' + text.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'

    unit_name = 'freetier2api-local-delivery.service'
    unit_dir = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'systemd/user'
    unit_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    unit = unit_dir / unit_name
    if unit.is_symlink():
        raise ValueError('Expected a regular user service file')
    command = ' '.join(quoted(value) for value in (Path(sys.executable).resolve(), Path(script).resolve(),
                                                  '--config', Path(config).resolve(), 'watch'))
    content = f'''[Unit]
Description=FreeTier local committed-plugin delivery

[Service]
Type=simple
ExecStart={command}
UMask=0077
NoNewPrivileges=true
Restart=on-failure
RestartSec=10s
SuccessExitStatus=130
KillMode=mixed
TimeoutStopSec=120s
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
'''
    if not unit.exists() or unit.read_text() != content:
        fd, name = tempfile.mkstemp(prefix='.' + unit_name, dir=unit_dir)
        try:
            with os.fdopen(fd, 'w') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, unit)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    unit.chmod(0o600)
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True, capture_output=True, timeout=30)
    if start:
        subprocess.run(['systemctl', '--user', 'enable', '--now', unit_name], check=True, capture_output=True, timeout=30)
    elif enable:
        subprocess.run(['systemctl', '--user', 'enable', unit_name], check=True, capture_output=True, timeout=30)
    return {'phase': 'service_installed', 'unit': str(unit), 'enabled_requested': enable or start, 'start_requested': start}
