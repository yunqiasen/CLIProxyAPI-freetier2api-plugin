"""Public command-line interface; credentials stay at the runtime boundary."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from .build import DockerBuilder
from .publish import Publisher
from .runtime import Runtime
from .state import load, save
from .service import cancellation_signals, failure_message, install, watch, watcher_status

DEFAULT_REPO = Path(__file__).resolve().parents[2]


class ConfigError(ValueError):
    pass


def read_config(path):
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise ConfigError('配置尚未初始化；先运行 init --builder-image 镜像摘要。')
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ConfigError('配置文件需要当前用户所有、权限 0600。')
    try:
        value = json.loads(path.read_text())
    except (ValueError, OSError):
        raise ConfigError('配置文件格式错误。') from None
    return validate_config(value)


def validate_config(value):
    if not isinstance(value, dict):
        raise ConfigError('配置需要 JSON 对象。')
    allowed = {'repo', 'state_dir', 'cpa_root', 'url', 'container', 'builder_image',
               'platform', 'interval', 'timeout', 'max_reloads'}
    if value.keys() - allowed:
        raise ConfigError('配置含未知字段；凭据由 CPA 读取，请勿写入此配置。')
    cfg = {'repo': str(DEFAULT_REPO), 'url': 'http://127.0.0.1:8317',
           'container': 'cli-proxy-api', 'platform': 'linux/amd64', 'interval': 5,
           'timeout': 60, 'max_reloads': 8, **value}
    try:
        for key in ('repo', 'state_dir', 'cpa_root', 'url', 'container', 'builder_image', 'platform'):
            if key in cfg and not isinstance(cfg[key], str):
                raise ValueError()
        cfg['repo'] = str(Path(cfg['repo']).resolve(strict=True))
        cfg['state_dir'] = str(Path(cfg.get('state_dir', Path(cfg['repo']) / '.local/delivery')).resolve())
        cfg['cpa_root'] = str(Path(cfg.get('cpa_root', Path(cfg['repo']).parent / 'CLIProxyAPI')).resolve(strict=True))
        Path(cfg['state_dir']).relative_to(Path(cfg['repo']) / '.local')
        if not Path(cfg['repo']).is_dir() or not Path(cfg['cpa_root']).is_dir():
            raise ValueError()
        top = subprocess.check_output(['git', '-C', cfg['repo'], 'rev-parse', '--show-toplevel'],
                                      text=True, stderr=subprocess.DEVNULL).strip()
        if Path(top).resolve() != Path(cfg['repo']):
            raise ValueError()
        if not cfg.get('builder_image') or cfg['platform'] != 'linux/amd64':
            raise ValueError()
        DockerBuilder(cfg['builder_image'], cfg['platform'])
        Runtime(cfg['url'], cfg['cpa_root'], cfg['container'], cfg['timeout'])
        for key, low, high in (('interval', 0.1, 3600), ('timeout', 0.1, 600), ('max_reloads', 2, 8)):
            number = cfg[key]
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not low <= number <= high:
                raise ValueError()
        if not isinstance(cfg['max_reloads'], int):
            raise ValueError()
    except (ValueError, TypeError, OSError, RuntimeError, subprocess.SubprocessError):
        raise ConfigError('配置校验失败：检查本地目录、镜像完整 sha256 摘要和数值范围。') from None
    return cfg


def initialize(path, args):
    path = Path(path).absolute()
    value = read_config(path) if path.exists() else {}
    for key in ('repo', 'cpa_root', 'builder_image', 'url'):
        option = getattr(args, key, None)
        if option is not None:
            value[key] = str(option)
    cfg = validate_config(value)
    try:
        path.resolve().relative_to(Path(cfg['repo']) / '.local')
    except ValueError:
        raise ConfigError('配置文件应放在源码目录的 .local 内。') from None
    if path.is_symlink() or path.parent.is_symlink():
        raise ConfigError('配置路径应使用普通文件和目录。')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.stat().st_uid != os.getuid():
        raise ConfigError('配置目录需要当前用户所有。')
    path.parent.chmod(0o700)
    if not path.exists() or load(path) != cfg:
        save(path, cfg)
    return cfg


def private_state(cfg):
    base = Path(cfg['repo']) / '.local'
    target = Path(cfg['state_dir'])
    path = base
    for part in (None, *target.relative_to(base).parts):
        if part is not None:
            path = path / part
        if path.is_symlink():
            raise ConfigError('本地状态目录应使用普通目录。')
        path.mkdir(mode=0o700, exist_ok=True)
        if path.stat().st_uid != os.getuid():
            raise ConfigError('本地状态目录需要当前用户所有。')
        path.chmod(0o700)


def source_status(repo):
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args], text=True,
                                       stderr=subprocess.DEVNULL).strip()
    branch = git('branch', '--show-current')
    commit = git('rev-parse', 'HEAD')
    clean = not git('status', '--porcelain', '--untracked-files=all')
    return {'branch': branch, 'commit': commit, 'clean': clean,
            'eligible': clean and branch == 'freetier2api_fork'}


def publication_running(cfg):
    lock = Path(cfg['cpa_root']) / 'logs/plugins/linux/amd64/.local-delivery.lock'
    try:
        with lock.open('rb') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
    except FileNotFoundError:
        pass
    return False


def status(cfg):
    root = Path(cfg['state_dir'])
    value = {'phase': 'idle', **load(root / 'state.json'), 'configured': True,
             'repo': cfg['repo'], 'state_dir': str(root), 'paused': (root / 'paused').exists(),
             'source': source_status(cfg['repo']), 'watcher': watcher_status(root),
             'max_reloads': cfg['max_reloads'], 'publishing': publication_running(cfg)}
    if value['phase'] == 'building' and not value['publishing']:
        value.update(phase='build_interrupted', persisted_phase='building',
                     error='上次构建已中断；检查私有构建日志和残留构建容器后运行 publish --retry。')
    return value


def emit(value, as_json=False):
    if as_json:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True), flush=True)
        return
    labels = {'idle': '尚未发布', 'active': '已发布', 'building': '构建中', 'build_failed': '构建失败',
              'build_interrupted': '上次构建已中断', 'switching': '切换中', 'rolled_back': '已回滚', 'rolling_back': '回滚中',
              'recovery_failed': '回滚待检查', 'paused_budget': '载入预算已用完',
              'superseded': '已被新状态取代', 'configured': '配置就绪', 'service_installed': '服务已安装',
              'ineligible': '跳过未提交或分支不符的源码', 'latched': '当前提交已处理或失败锁定',
              'skipped': '跳过本次发布', 'waiting': '等待源码稳定或预算复查',
              'failed': '交付失败', 'paused': '暂停中', 'busy': '另一发布正在执行',
              'published': '发布操作结束', 'source_error': '本地状态读取失败', 'recovery_required': '等待手动恢复'}
    phase = value.get('event', value.get('phase', 'idle'))
    print('状态：' + labels.get(phase, phase) + ('；已暂停' if value.get('paused') else ''), flush=True)
    active = value.get('active', {})
    if active.get('version'):
        print('版本：' + value.get('observed_version', active['version']) + '（最近验证）', flush=True)
        if value.get('phase') == 'active':
            print('提交：' + active.get('commit', '')[:12], flush=True)
        else:
            print('最近发布提交：' + active.get('commit', '')[:12] + '（以当前状态为准）', flush=True)
    if value.get('source'):
        source = value['source']
        print('源码：' + (source['branch'] or '游离 HEAD') + ' ' + source['commit'][:12] +
              (' 干净' if source['clean'] else ' 有未提交改动'), flush=True)
    if 'watcher' in value:
        print('监听：' + ('运行中' if value['watcher'].get('running') else '停止'), flush=True)
    if 'reload_count' in value:
        print(f"原生载入预算：{value['reload_count']}/{value.get('max_reloads', 8)}", flush=True)
    if value.get('unit'):
        print('服务文件：' + value['unit'], flush=True)
    if value.get('skipped'):
        print('跳过：' + value['skipped'], flush=True)
    error = value.get('error') or value.get('watcher', {}).get('error')
    if error:
        print('说明：' + error, flush=True)


def parser():
    result = argparse.ArgumentParser(description='本地 FreeTier 插件交付；只读取已提交的干净 freetier2api_fork。')
    result.add_argument('--config', type=Path, default=DEFAULT_REPO / '.local/config.json', help='私有 JSON 配置路径')
    commands = result.add_subparsers(dest='command', required=True)
    for name, help_text in [('status', '查看源码与持久化交付状态；不访问运行服务'),
                            ('publish', '手动发布；失败后使用 --retry 重试'),
                            ('watch', '轮询新提交；失败提交锁定，Ctrl-C 停止'),
                            ('pause', '暂停后续发布；正在进行的发布会先完成'),
                            ('resume', '恢复后续发布；保留失败提交锁定'),
                            ('rollback', '验证并恢复上一版本；暂停期间也可执行'),
                            ('init', '创建私有配置；仅接受完整镜像摘要，不接收凭据'),
                            ('install-service', '安装用户服务；仅 --enable 或 --start 启用')]:
        command = commands.add_parser(name, help=help_text)
        command.add_argument('--json', action='store_true', help='输出 JSON')
        if name == 'publish':
            command.add_argument('--retry', action='store_true', help='显式重试当前失败提交')
        if name in ('init', 'install-service'):
            command.add_argument('--builder-image', help='已拉取的 bookworm 镜像完整 sha256 摘要')
            command.add_argument('--repo', type=Path, help='插件源码目录；默认脚本所属仓库')
            command.add_argument('--cpa-root', type=Path, help='CPA 源码目录；默认源码同级 CLIProxyAPI')
            command.add_argument('--url', help='CPA 本地 HTTP 地址；默认 http://127.0.0.1:8317')
        if name == 'install-service':
            command.add_argument('--enable', action='store_true', help='启用用户服务，下次用户会话启动')
            command.add_argument('--start', action='store_true', help='立即启用并启动用户服务')
    result.epilog = '用法：init --builder-image 镜像@sha256:摘要 → publish → install-service --start。无自动提交、推送或模型生成。'
    return result


def main(argv=None):
    with cancellation_signals() as cancel:
        result = run(argv, cancel)
        return 130 if cancel.cancelled else result


def run(argv, cancel):
    args = parser().parse_args(argv)
    try:
        if args.command in ('init', 'install-service'):
            cfg = initialize(args.config, args)
            if args.command == 'install-service':
                emit(install(DEFAULT_REPO / 'scripts/dev-plugin', args.config, enable=args.enable, start=args.start), args.json)
            else:
                emit({**status(cfg), 'phase': 'configured'}, args.json)
            return 0
        cfg = read_config(args.config)
        root = Path(cfg['state_dir'])
        if args.command != 'status':
            private_state(cfg)
        if args.command == 'pause':
            save(root / 'paused', {'paused': True})
        elif args.command == 'resume':
            (root / 'paused').unlink(missing_ok=True)
            save(root / 'resume.json', {'sequence': uuid.uuid4().hex})
        elif args.command in ('publish', 'rollback', 'watch'):
            if args.command == 'publish' and (root / 'paused').exists():
                emit({**status(cfg), 'skipped': '已暂停后续发布。'}, args.json)
                return 0
            publisher = Publisher(cfg['repo'], root,
                                  Runtime(cfg['url'], cfg['cpa_root'], cfg['container'], cfg['timeout']),
                                  DockerBuilder(cfg['builder_image'], cfg['platform']), cfg['max_reloads'])
            if args.command == 'watch':
                return watch(cfg, publisher, source_status, lambda value: emit(value, args.json), cancel)
            result = publisher.publish(retry=args.retry) if args.command == 'publish' else publisher.rollback()
            if args.command == 'publish' and result.get('phase') == 'active':
                save(root / 'manual-success.json', {'commit': result['active']['commit'], 'sequence': uuid.uuid4().hex})
            emit({**status(cfg), **result}, args.json)
            return 0
        emit(status(cfg), args.json)
        return 0
    except KeyboardInterrupt:
        return 130
    except ConfigError as error:
        print(str(error), file=sys.stderr)
        return 2
    except RuntimeError as error:
        print(failure_message(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, subprocess.SubprocessError):
        print('操作失败；检查本地配置与私有状态文件。', file=sys.stderr)
        return 1
