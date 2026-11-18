import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('mkl_compiler', ROOT / 'scripts/compile-image.py')
compiler = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compiler)


class MklCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'config').mkdir()
        self.data = b'locked Intel CPU SDK component'
        self.item = {'file': 'intel-component.deb', 'bytes': len(self.data),
                     'sha256': hashlib.sha256(self.data).hexdigest(),
                     'url': 'https://apt.repos.intel.com/oneapi/pool/main/intel-component.deb'}
        self.directory = self.root / '.cache/compile-tools/mkl'
        self.directory.mkdir(parents=True)
        self.lock = self.root / 'config/mkl.lock.json'
        self.write_lock()
        for name, value in [('ROOT', self.root), ('CACHE', self.root / '.cache')]:
            p = patch.object(compiler, name, value)
            p.start(); self.addCleanup(p.stop)

    def write_lock(self):
        self.lock.write_text(json.dumps({'version': '2022.1.0', 'packages': [self.item]}))

    def test_verified_cache_works_in_both_modes_without_network(self):
        (self.directory / self.item['file']).write_bytes(self.data)
        with patch.object(compiler.urllib.request, 'urlopen', side_effect=AssertionError('network')):
            for offline in (True, False):
                compiler.prepare_mkl(offline)
        self.assertIn(self.item['sha256'], (self.directory / 'SHA256SUMS').read_text())

    def test_missing_or_corrupt_cache_fails_offline_without_modification(self):
        target = self.directory / self.item['file']
        with patch.object(compiler.urllib.request, 'urlopen', side_effect=AssertionError('network')):
            for value in (None, b'corrupt'):
                if value is not None:
                    target.write_bytes(value)
                with self.assertRaisesRegex(RuntimeError, '离线依赖缺失或校验失败'):
                    compiler.prepare_mkl(True)
                self.assertEqual(target.read_bytes() if target.exists() else None, value)

    def test_failed_download_does_not_replace_existing_file_or_leave_partial(self):
        target = self.directory / self.item['file']
        target.write_bytes(b'previous cache')
        with patch.object(compiler.urllib.request, 'urlopen', return_value=io.BytesIO(b'wrong payload')):
            with self.assertRaisesRegex(RuntimeError, 'SHA256 不符'):
                compiler.prepare_mkl(False)
        self.assertEqual(target.read_bytes(), b'previous cache')
        self.assertEqual(list(self.directory.iterdir()), [target])

    def test_verified_download_is_published_with_checksum(self):
        with patch.object(compiler.urllib.request, 'urlopen', return_value=io.BytesIO(self.data)):
            compiler.prepare_mkl(False)
        self.assertEqual((self.directory / self.item['file']).read_bytes(), self.data)
        self.assertEqual((self.directory / 'SHA256SUMS').read_text(), self.item['sha256'] + '  ' + self.item['file'] + '\n')

    def test_lock_cannot_write_outside_cache(self):
        self.item['file'] = '../outside.deb'; self.write_lock()
        with self.assertRaisesRegex(ValueError, '非法 MKL'):
            compiler.prepare_mkl(True)


if __name__ == '__main__':
    unittest.main()
