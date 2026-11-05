import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('runner', REPO / 'runtime/run-build.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class RuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        fixture = Path(os.environ.get('XRAN_SOURCE', str(REPO.parent / 'X-RAN-CLOUD')))
        if not fixture.exists():
            fixture = REPO / '.cache/demo.git'
        cls.bare = cls.root / 'X-RAN-CLOUD.git'
        subprocess.run(['git', 'clone', '--mirror', '--no-hardlinks', str(fixture), str(cls.bare)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(['git', '-C', str(cls.bare), 'update-server-info'], check=True)
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(cls.root)))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}/X-RAN-CLOUD.git'
        cls.head = subprocess.check_output(['git', '-C', str(cls.bare), 'rev-parse', 'main'], text=True).strip()
        cls.old = subprocess.check_output(['git', '-C', str(cls.bare), 'rev-parse', 'demo-v1.0.0^{commit}'], text=True).strip()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(dir=self.root))
        runner.ROOT = self.work
        runner.SOURCE = self.work / 'source'
        runner.PUBLISH = self.work / 'publish'
        self.env = patch.dict(os.environ, {'XRAN_GIT_URL': self.url, 'XRAN_REF_TYPE': 'branch', 'XRAN_REF': 'main',
            'XRAN_KIND': 'service', 'XRAN_TARGET': 'serviceA', 'XRAN_IMAGE_TAG': 'latest',
            'XRAN_SERVICES': json.dumps(['serviceA', 'serviceB', 'serviceC']),
            'XRAN_BUILD_METADATA': json.dumps({'result': 'SUCCESS'}), 'NO_PROXY': '127.0.0.1,localhost',
            'no_proxy': '127.0.0.1,localhost'})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_branch_tag_and_unique_sha(self):
        for kind, reference, expected in [('branch', 'main', self.head), ('tag', 'demo-v1.0.0', self.old),
                ('commit', self.old, self.old), ('commit', self.old[:9], self.old)]:
            with self.subTest(kind=kind, ref=reference):
                os.environ.update(XRAN_REF_TYPE=kind, XRAN_REF=reference)
                runner.checkout()
                self.assertEqual(json.loads((self.work / 'checkout.json').read_text())['commit'], expected)

    def test_malicious_or_invalid_ref_is_not_shell(self):
        marker = self.work / 'injected'
        for kind, reference in [('branch', '--upload-pack=touch'), ('commit', 'xyz1234'),
                ('branch', 'main;touch ' + str(marker)), ('branch', 'missing')]:
            os.environ.update(XRAN_REF_TYPE=kind, XRAN_REF=reference)
            with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                runner.checkout()
            self.assertFalse(marker.exists())

    def test_compile_all_and_big_package(self):
        for target, count in [('serviceA', 1), ('all', 3), ('services_all', 1)]:
            with self.subTest(target=target):
                runner.checkout()
                os.environ['XRAN_TARGET'] = target
                runner.build()
                self.assertEqual(runner.collect(), 0)
                metadata = json.loads((runner.PUBLISH / 'metadata.json').read_text())
                self.assertEqual(len(metadata['files']), count)
                for item in metadata['files']:
                    path = runner.PUBLISH / item['name']
                    with tarfile.open(path) as archive:
                        members = [m for m in archive if m.name.endswith('/build.json')]
                        for member in members:
                            self.assertEqual(json.load(archive.extractfile(member))['commit'], self.head)

    def test_truncated_archive_fails_collection(self):
        runner.checkout()
        runner.build()
        artifact = runner.SOURCE / 'JDBG/Release/services/serviceA.tar.gz'
        artifact.write_bytes(artifact.read_bytes()[:32])
        self.assertEqual(runner.collect(), 1)
        self.assertEqual(json.loads((runner.PUBLISH / 'metadata.json').read_text())['result'], 'FAILURE')

    def test_symlink_archive_is_rejected(self):
        runner.checkout()
        directory = runner.SOURCE / 'JDBG/Release/services'
        directory.mkdir(parents=True)
        secret = self.work / 'private'
        secret.write_text('not a build artifact')
        (directory / 'serviceA.tar.gz').symlink_to(secret)
        self.assertEqual(runner.collect(), 1)
        self.assertFalse((runner.PUBLISH / 'serviceA.tar.gz').exists())


if __name__ == '__main__':
    unittest.main()
