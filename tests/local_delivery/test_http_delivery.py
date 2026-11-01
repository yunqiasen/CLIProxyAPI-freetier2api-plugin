"""Public publisher + real HTTP management transport, never an upstream model."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from local_delivery.publish import Publisher
from local_delivery.runtime import Runtime


class HttpDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-q', '-b', 'freetier2api_fork')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        (self.repo / 'source').write_text('v1')
        self.git('add', '.')
        self.git('commit', '-qm', 'initial')
        self.installed = self.root / 'cpa/logs/plugins/linux/amd64'
        self.installed.mkdir(parents=True)
        (self.installed / 'freetier2api-v0.1.23.so').write_bytes(b'previous')
        self.generation = '0.1.23'
        self.metadata = 'v0.1.23'
        self.selector = '0.1.23'
        self.accept = True
        self.calls = []
        self.owner_note = 'keep this credential label'
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                fixture.calls.append(('GET', self.path))
                if self.path == '/v8/management/config':
                    data = {'access': {'api-keys': ['NOT-A-REAL-KEY']}, 'plugins': {'enabled': True,
                        'configs': {'freetier2api': {'enabled': True, 'note': 'keep',
                                                   'store': {'version': fixture.selector}}}}}
                elif self.path == '/v8/management/plugins':
                    data = {'plugins': [{'id': 'freetier2api', 'registered': True, 'effective_enabled': True,
                        'path': '/CLIProxyAPI/logs/plugins/linux/amd64/freetier2api-v' + fixture.selector + '.so',
                        'metadata': {'version': fixture.metadata}}]}
                elif self.path == '/v8/management/credentials':
                    data = {'files': [{'name': 'credential.json', 'type': 'freetier', 'disabled': False,
                                        'note': fixture.owner_note}]}
                elif self.path == '/v1/models':
                    data = {'data': [{'id': 'custom-alias'}]}
                elif self.path == '/v0/resource/plugins/freetier2api/console':
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'<html>console</html>')
                    return
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())

            def do_PUT(self):
                fixture.calls.append(('PUT', self.path))
                if self.path != '/v8/management/config/plugins/configs/freetier2api/store/version':
                    self.send_error(404)
                    return
                value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                fixture.selector = value
                if fixture.accept or value == '0.1.23' or '-rollback.' in value:
                    fixture.generation = value
                    fixture.metadata = value.split('-rollback.')[0]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{}')

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.runtime = Runtime('http://127.0.0.1:' + str(server.server_port), self.root / 'cpa', timeout=.15)
        self.runtime.key = 'FIXTURE-MANAGEMENT'
        def identity():
            st = (self.installed / f'freetier2api-v{self.generation}.so').stat()
            self.runtime.native_images = {(os.major(st.st_dev), os.minor(st.st_dev), st.st_ino)}
            self.runtime.mapped_images = 1
            return 'fixture:pid:start'
        self.runtime.identity = identity

        class Builder:
            def build(_self, repo, commit, output):
                output.mkdir(parents=True)
                version = '0.1.23-fork.' + commit[:12] + '-b.' + uuid.uuid4().hex[:12]
                artifact = output / f'freetier2api-v{version}.so'
                artifact.write_bytes(b'candidate')
                return {'commit': commit, 'version': version, 'artifact': str(artifact),
                        'sha256': hashlib.sha256(b'candidate').hexdigest(),
                        'console_sha256': hashlib.sha256(b'<html>console</html>').hexdigest()}
        self.publisher = Publisher(self.repo, self.root / 'state', self.runtime, Builder())

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True).strip()

    def test_http_publication_rollback_and_read_only_acceptance(self):
        result = self.publisher.publish()
        self.assertEqual(result['phase'], 'active')
        self.assertEqual(self.generation, result['active']['version'])
        result = self.publisher.rollback()
        self.assertEqual(result['phase'], 'rolled_back')
        self.assertTrue(self.generation.startswith('0.1.23-rollback.'))
        self.assertEqual(self.owner_note, 'keep this credential label')
        self.assertTrue(all(method == 'GET' or path.endswith('/store/version') for method, path in self.calls))
        self.assertFalse(any(path.endswith('/freetier2api/models') or '/completions' in path for _, path in self.calls))

    def test_http_200_without_new_registered_metadata_is_failure_and_recovers(self):
        self.accept = False
        with self.assertRaisesRegex(RuntimeError, 'verification'):
            self.publisher.publish()
        self.assertEqual(self.selector, '0.1.23')
        self.assertEqual(self.generation, '0.1.23')
        self.assertEqual(self.publisher.status()['phase'], 'rolled_back')
        puts = sum(method == 'PUT' for method, _ in self.calls)
        self.assertIn('skipped', self.publisher.publish())
        self.assertEqual(sum(method == 'PUT' for method, _ in self.calls), puts)

    def test_redirect_does_not_forward_management_credentials(self):
        # The installed opener deliberately contains a rejecting redirect policy.
        from local_delivery.runtime import NoRedirect
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, '', {}, 'http://localhost:1/'))


if __name__ == '__main__':
    unittest.main()
