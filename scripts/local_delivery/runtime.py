"""Local Docker/CPA boundary; maintenance never invokes generation endpoints."""
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from .state import digest

SELECTOR = '/v8/management/config/plugins/configs/freetier2api/store/version'


def normalize(version):
    return str(version or '').lstrip('vV')


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Runtime:
    def __init__(self, url, cpa_root, container='cli-proxy-api', timeout=60):
        origin = urllib.parse.urlsplit(url)
        if origin.scheme != 'http' or origin.hostname not in ('127.0.0.1', 'localhost') or origin.path not in ('', '/') or origin.username or origin.password or origin.query or origin.fragment:
            raise ValueError('Local delivery requires a loopback HTTP origin without credentials or path')
        self.url = url.rstrip('/')
        self.root = Path(cpa_root).resolve()
        self.container = container
        self.timeout = timeout
        self.plugin_dir = self.root / 'logs/plugins/linux/amd64'
        self.key = None
        self.api_key = None
        self.mounts = []
        self.mapped_images = 0
        self.native_images = set()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def docker(self, *args):
        try:
            return subprocess.check_output(['docker', *args], text=True, stderr=subprocess.PIPE, timeout=15)
        except (subprocess.SubprocessError, OSError):
            raise RuntimeError('Local Docker inspection failed') from None

    def identity(self):
        context = json.loads(self.docker('context', 'inspect'))[0]
        endpoint = os.environ.get('DOCKER_HOST') or context['Endpoints']['docker']['Host']
        if not endpoint.startswith('unix://'):
            raise RuntimeError('The Docker daemon must be local')
        container = json.loads(self.docker('inspect', self.container))[0]
        if not container['State']['Running'] or container['HostConfig']['NetworkMode'] != 'host':
            raise RuntimeError('Expected the running local CPA with host networking')
        self.mounts = container['Mounts']
        logs = [m for m in self.mounts if m['Destination'] == '/CLIProxyAPI/logs']
        source = [m for m in self.mounts if m['Destination'] == '/workspace']
        if len(logs) != 1 or Path(logs[0]['Source']).resolve() != self.root / 'logs' or not logs[0].get('RW'):
            raise RuntimeError('CPA plugin mount does not match the configured local workspace')
        if len(source) != 1 or Path(source[0]['Source']).resolve() != self.root:
            raise RuntimeError('CPA source mount does not match the configured workspace')
        env = dict(item.split('=', 1) for item in container['Config']['Env'] if '=' in item)
        self.key = env.get('MANAGEMENT_PASSWORD')
        if not self.key:
            raise RuntimeError('CPA has no local management credential; configure a private credential source')
        rows = self.docker('top', self.container, '-eo', 'pid,comm').splitlines()[1:]
        pids = [line.split()[0] for line in rows if line.split()[-1] == 'CLIProxyAPI']
        if len(pids) != 1:
            raise RuntimeError('CPA child process identity is ambiguous')
        # Read within the container's PID namespace; no privileged host /proc read.
        script = """import json,os
rows=[]
for name in os.listdir('/proc'):
 if name.isdigit():
  try:
   if open('/proc/'+name+'/comm').read().strip()=='CLIProxyAPI':
    text=open('/proc/'+name+'/stat').read(); start=text[text.rfind(')')+2:].split()[19]
    maps=open('/proc/'+name+'/maps').read().splitlines()
    images={tuple(x.split()[3:5]) for x in maps if '/freetier2api-v' in x}
    rows.append([name,start,list(images)])
  except OSError: pass
print(json.dumps(rows))"""
        identities = json.loads(self.docker('exec', self.container, 'python3', '-c', script))
        if len(identities) != 1:
            raise RuntimeError('CPA native image identity is ambiguous')
        process, start, images = identities[0]
        self.native_images = {(int(dev.split(':')[0], 16), int(dev.split(':')[1], 16), int(inode))
                              for dev, inode in images}
        self.mapped_images = len(self.native_images)
        return container['Id'] + ':' + process + ':' + start

    def request(self, path, method='GET', value=None, api=False, raw=False):
        key = self.api_key if api else self.key
        if not key:
            self.identity()
            key = self.key
        data = json.dumps(value).encode() if method != 'GET' else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
        try:
            with self.opener.open(req, timeout=8) as response:
                body = response.read(16 * 1024 * 1024 + 1)
                if len(body) > 16 * 1024 * 1024:
                    raise RuntimeError('Management response exceeded size limit')
                return body if raw else json.loads(body)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f'CPA maintenance {method} {path}: HTTP {exc.code}') from None
        except (OSError, ValueError):
            raise RuntimeError(f'CPA maintenance {method} {path}: response not verified') from None

    def snapshot(self, *, control_only=False, known=()):
        identity = self.identity()
        cfg = self.request('/v8/management/config')
        plugin_cfg = cfg.get('plugins', {}).get('configs', {}).get('freetier2api', {})
        matches = [p for p in self.request('/v8/management/plugins')['plugins'] if p['id'] == 'freetier2api']
        if len(matches) != 1 or (not control_only and
                (not matches[0].get('registered') or not matches[0].get('effective_enabled'))):
            raise RuntimeError('FreeTier plugin is not enabled and registered')
        plugin = matches[0]
        metadata_version = normalize((plugin.get('metadata') or {}).get('version'))
        missing_registration = control_only and not plugin.get('registered')
        active_path = plugin.get('active_path', '')
        if not missing_registration and not active_path:
            raise RuntimeError('CPA does not expose registered active_path; update the host first')
        filename = PurePosixPath(active_path if not missing_registration else plugin.get('path', '')).name
        match = re.fullmatch(r'freetier2api-v([0-9][0-9A-Za-z.+-]*)\.so', filename)
        if not match:
            raise RuntimeError('Unexpected registered plugin path')
        generation = match.group(1)
        if not missing_registration and generation != metadata_version and not generation.startswith(metadata_version + '-rollback.'):
            raise RuntimeError('Registered plugin path and embedded version disagree')
        candidates = [generation]
        # With no registered instance, recovery may inspect known mapped files,
        # but acceptance always uses the host's actual registered active_path.
        if missing_registration:
            candidates.extend(release['version'] for release in known if release)
        path = None
        for version in dict.fromkeys(candidates):
            if not re.fullmatch(r'[0-9][0-9A-Za-z.+-]*', version):
                continue
            candidate_path = self.plugin_dir / f'freetier2api-v{version}.so'
            if candidate_path.is_symlink() or not candidate_path.is_file():
                continue
            st = candidate_path.stat()
            if (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino) in self.native_images:
                path = candidate_path
                break
        if path is None:
            raise RuntimeError('Registered plugin file is missing or not mapped by this CPA process')
        selector = normalize(plugin_cfg.get('store', {}).get('version', ''))
        stable_config = copy.deepcopy(cfg)
        stable_store = stable_config['plugins']['configs']['freetier2api'].get('store', {})
        stable_store.pop('version', None)
        if not stable_store:
            stable_config['plugins']['configs']['freetier2api'].pop('store', None)
        result = {'identity': identity, 'version': version, 'metadata_version': metadata_version,
                  'mapped_images': self.mapped_images, 'path': str(path), 'selector': selector,
                  'artifact_sha256': digest(path), 'config': fingerprint(stable_config)}
        if control_only:
            # Recovery must not depend on a broken candidate's console or models.
            return result
        keys = cfg.get('access', {}).get('api-keys', [])
        if not keys or not isinstance(keys[0], str):
            raise RuntimeError('An existing CPA API key is needed for the local model inventory')
        self.api_key = keys[0]
        credential_fields = ('name', 'type', 'disabled', 'display_name', 'note', 'auth_index')
        credentials = sorted([{k: item.get(k) for k in credential_fields}
                              for item in self.request('/v8/management/credentials')['files']], key=lambda item: item['name'])
        models = sorted(item['id'] for item in self.request('/v1/models', api=True)['data'])
        console = self.request('/v0/resource/plugins/freetier2api/console', raw=True)
        result.update(credentials=fingerprint(credentials), models=fingerprint(models),
                      console_sha256=hashlib.sha256(console).hexdigest())
        return result

    def select(self, version):
        if not re.fullmatch(r'[0-9][0-9A-Za-z.+-]*', version):
            raise ValueError('Invalid version selector')
        self.request(SELECTOR, 'PUT', version)

    def verify(self, version, baseline, console_sha256=None, metadata_version=None):
        metadata_version = metadata_version or version
        end = time.monotonic() + self.timeout
        while True:
            try:
                current = self.snapshot()
                stable = all(current[k] == baseline[k] for k in ('identity', 'config', 'credentials', 'models'))
                if stable and current['version'] == version and current['metadata_version'] == metadata_version and current['selector'] == version and (console_sha256 is None or current['console_sha256'] == console_sha256):
                    return current
            except RuntimeError:
                pass
            if time.monotonic() >= end:
                raise RuntimeError('Plugin version/state verification did not converge')
            time.sleep(min(1, max(0, end - time.monotonic())))
