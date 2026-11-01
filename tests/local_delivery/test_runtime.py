import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from local_delivery.runtime import Runtime


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runtime = Runtime('http://127.0.0.1:8317', self.root, 'fixture', timeout=0.1)
        self.runtime.identity = lambda: 'same-container:process'
        self.runtime.plugin_dir = self.root / 'logs/plugins/linux/amd64'
        self.runtime.plugin_dir.mkdir(parents=True)
        (self.runtime.plugin_dir / 'freetier2api-v0.1.23.so').write_bytes(b'original')
        st = (self.runtime.plugin_dir / 'freetier2api-v0.1.23.so').stat()
        self.runtime.native_images = {(os.major(st.st_dev), os.minor(st.st_dev), st.st_ino)}
        self.calls = []
        self.cfg = {'access': {'api-keys': ['PRIVATE']}, 'plugins': {
            'enabled': True, 'configs': {'freetier2api': {'enabled': True, 'store': {'version': '0.1.23'}, 'setting': 'keep'}}}}
        self.version = 'v0.1.23'
        self.runtime.request = self.request

    def request(self, path, method='GET', value=None, api=False, raw=False):
        self.calls.append((method, path, value))
        if path.endswith('/config'):
            return json.loads(json.dumps(self.cfg))
        if path.endswith('/plugins'):
            return {'plugins': [{'id': 'freetier2api', 'registered': True, 'effective_enabled': True,
                                 'path': '/CLIProxyAPI/logs/plugins/linux/amd64/freetier2api-v0.1.23.so',
                                 'metadata': {'version': self.version}}]}
        if path.endswith('/credentials'):
            return {'files': [{'name': 'sample.json', 'type': 'freetier', 'note': 'keep', 'status': 'mutable'}]}
        if path == '/v1/models':
            return {'data': [{'id': 'alias'}]}
        if path.endswith('/console'):
            return b'<html>fixture</html>'
        if method == 'PUT':
            self.cfg['plugins']['configs']['freetier2api']['store']['version'] = value
            return {'status': 'ok'}
        raise AssertionError(path)

    def test_inventory_is_read_only_and_uses_host_models(self):
        snapshot = self.runtime.snapshot()
        self.assertEqual(snapshot['version'], '0.1.23')
        self.assertEqual(snapshot['path'], str(self.runtime.plugin_dir / 'freetier2api-v0.1.23.so'))
        self.assertNotIn('PRIVATE', json.dumps(snapshot))
        self.assertTrue(all(method == 'GET' for method, _, _ in self.calls))
        self.assertIn(('GET', '/v1/models', None), self.calls)
        self.assertFalse(any(path.endswith('/freetier2api/models') for _, path, _ in self.calls))

    def test_select_only_updates_correct_leaf(self):
        self.runtime.select('0.1.23-fork.abcdef')
        self.assertEqual(self.calls[-1], ('PUT', '/v8/management/config/plugins/configs/freetier2api/store/version', '0.1.23-fork.abcdef'))
        self.assertEqual(self.cfg['plugins']['configs']['freetier2api']['setting'], 'keep')

    def test_verification_rejects_metadata_mismatch_and_configuration_loss(self):
        baseline = self.runtime.snapshot()
        self.assertEqual(self.runtime.verify('0.1.23', baseline)['version'], '0.1.23')
        self.cfg['plugins']['configs']['freetier2api']['setting'] = 'lost'
        with self.assertRaisesRegex(RuntimeError, 'verification'):
            self.runtime.verify('0.1.23', baseline)

    def test_unmapped_discovered_file_is_not_reported_as_loaded(self):
        self.runtime.native_images = set()
        with self.assertRaisesRegex(RuntimeError, 'mapped'):
            self.runtime.snapshot()

    def test_snapshot_records_artifact_checksum(self):
        snapshot = self.runtime.snapshot()
        self.assertEqual(snapshot['artifact_sha256'], hashlib.sha256(b'original').hexdigest())

if __name__ == '__main__':
    unittest.main()
