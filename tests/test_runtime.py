import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import yaml

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

    def test_checkout_command_exits_successfully(self):
        subprocess.run([sys.executable,str(REPO/'runtime/run-build.py'),'checkout'],cwd=self.work,check=True)
        self.assertEqual(json.loads((self.work/'checkout.json').read_text())['commit'],self.head)

    def test_compile_service_and_big_package(self):
        for target, count in [('serviceA', 1), ('services_all', 1)]:
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


class RuntimeSnapshotTests(unittest.TestCase):
    def test_configmap_revision_is_pinned_for_the_whole_build(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); mount = root/'mount'; version = mount/'..v1'
            version.mkdir(parents=True)
            names = ('run-build.py','projects.py','git_checkout.py')
            scripts = {name:(REPO/'runtime'/name).read_text() for name in names}
            for name,content in scripts.items(): (version/name).write_text(content)
            (mount/'..data').symlink_to('..v1')
            for name in names: (mount/name).symlink_to('..data/'+name)
            digest = hashlib.sha256(json.dumps(scripts,sort_keys=True).encode()).hexdigest()
            environment = dict(os.environ, XRAN_RUNTIME_SCRIPTS_SHA256=digest)
            subprocess.run([sys.executable,str(mount/'run-build.py'),'snapshot'],cwd=root,env=environment,check=True)
            # Kubelet switches the mount to a different revision during compilation.
            next_version = mount/'..v2';shutil.copytree(version,next_version)
            (next_version/'run-build.py').write_text('raise RuntimeError("new deployment")\n')
            (mount/'..data-next').symlink_to('..v2');(mount/'..data-next').replace(mount/'..data')
            for name,content in scripts.items():self.assertEqual((root/'.xran-platform'/name).read_text(),content)
            result = subprocess.check_output([sys.executable,str(root/'.xran-platform/run-build.py'),'capabilities'],cwd=root,text=True)
            self.assertEqual(json.loads(result)['xranServiceArtifacts'],1)

    def test_mismatched_runtime_is_rejected_before_working_copy_creation(self):
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run([sys.executable,str(REPO/'runtime/run-build.py'),'snapshot'],cwd=folder,
                env=dict(os.environ,XRAN_RUNTIME_SCRIPTS_SHA256='wrong'),capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('平台运行脚本校验失败',result.stderr)
            self.assertFalse((Path(folder)/'.xran-platform').exists())


class IntranetServiceTests(unittest.TestCase):
    def test_every_individual_service_dispatches_and_collects_its_exact_name(self):
        targets = yaml.safe_load((REPO / 'config/site.intranet.yaml').read_text())['services']
        for target in targets[1:]:
            with self.subTest(target=target), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, publish = root / 'source', root / 'publish'
                services = source / 'services'
                services.mkdir(parents=True)
                script = services / 'build.sh'
                # Small valid packages exercise the adapter contract, not private business code.
                script.write_text('''#!/bin/sh
set -eu
test "$1" = -c && test "$2" = -m
printf '%s' "$3" > dispatched-target
mkdir -p ../JDBG/Release/services
tar -czf "../JDBG/Release/services/$3.tar.gz" dispatched-target
''')
                script.chmod(0o755)
                (root / 'checkout.json').write_text(json.dumps({'commit': 'a' * 40}))
                with patch.object(runner, 'ROOT', root), patch.object(runner, 'SOURCE', source), \
                        patch.object(runner, 'PUBLISH', publish), patch.dict(os.environ,
                        XRAN_KIND='service', XRAN_TARGET=target, XRAN_SERVICE_SERIAL='true',
                        XRAN_BUILD_METADATA='{"result":"SUCCESS"}'):
                    runner.build()
                    self.assertEqual((services / 'dispatched-target').read_text(), target)
                    self.assertEqual(runner.collect(), 0)
                    metadata = json.loads((publish / 'metadata.json').read_text())
                    self.assertEqual(metadata['result'], 'SUCCESS')
                    self.assertEqual([f['name'] for f in metadata['files']], [target + '.tar.gz'])
                    self.assertEqual(metadata['files'][0]['sha256'],
                                     hashlib.sha256((publish / (target + '.tar.gz')).read_bytes()).hexdigest())


class ServiceArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name, value in [('ROOT', self.root), ('SOURCE', self.root / 'source'),
                            ('PUBLISH', self.root / 'publish')]:
            context = patch.object(runner, name, value)
            context.start()
            self.addCleanup(context.stop)
        self.directory = runner.SOURCE / 'JDBG/Release/services'
        self.directory.mkdir(parents=True)
        self.services = runner.SOURCE / 'services'
        self.services.mkdir()
        (self.services / 'build.sh').write_text('#!/bin/sh\nexit 0\n')
        (self.services / 'build.sh').chmod(0o755)
        (self.root / 'checkout.json').write_text(json.dumps({'commit': 'b' * 40}))
        context = patch.dict(os.environ, XRAN_KIND='service', XRAN_TARGET='DemoA',
                             XRAN_PROJECT_SPEC='{}', XRAN_BUILD_METADATA='{"result":"SUCCESS"}')
        context.start()
        self.addCleanup(context.stop)

    def archive(self, name, empty=False):
        path = self.directory / name
        with tarfile.open(path, 'w:gz') as archive:
            if not empty:
                member = tarfile.TarInfo('app'); member.size = 3
                archive.addfile(member, io.BytesIO(b'app'))
        return path

    def metadata(self):
        return json.loads((runner.PUBLISH / 'metadata.json').read_text())

    def test_both_targets_accept_arbitrary_archive_names_and_checksummable_unicode(self):
        for target in ('DemoA', 'services_all'):
            for name in ('custom-26.11-release.tar.gz', '服务 输出.tar.gz', 'release\\one\nname\r.tar.gz'):
                with self.subTest(target=target, name=name):
                    os.environ['XRAN_TARGET'] = target
                    archive = self.archive(name)
                    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
                    # Former name restrictions must also be ignored in old recipe snapshots.
                    os.environ['XRAN_PROJECT_SPEC'] = json.dumps({'builds': {'service': {
                        'artifact': '{target}.tar.gz', 'bundlePattern': 'L2L3_*.tar.gz'}}})
                    self.assertEqual(runner.collect(), 0)
                    meta = self.metadata()
                    self.assertEqual(meta['files'], [{'name': name, 'size': (runner.PUBLISH / name).stat().st_size,
                                                     'sha256': digest}])
                    subprocess.run(['sha256sum', '--check', 'SHA256SUMS'], cwd=runner.PUBLISH,
                                   check=True, stdout=subprocess.DEVNULL)

    def test_old_tracked_packages_are_removed_even_when_script_produces_nothing(self):
        stale = self.archive('old-release.tar.gz')
        runner.build()
        self.assertFalse(stale.exists())
        self.assertEqual(runner.collect(), 1)
        self.assertEqual(self.metadata()['files'], [])

    def test_compilation_replaces_old_packages_and_ignores_other_suffixes(self):
        self.archive('stale.tar.gz')
        (self.directory / 'notes.txt').write_text('keep')
        (self.services / 'build.sh').write_text('''#!/bin/sh
set -eu
mkdir -p ../JDBG/Release/services
tar -czf ../JDBG/Release/services/brand-new.tar.gz build.sh
''')
        runner.build()
        self.assertEqual(runner.collect(), 0)
        self.assertEqual([item['name'] for item in self.metadata()['files']], ['brand-new.tar.gz'])
        self.assertEqual((self.directory / 'notes.txt').read_text(), 'keep')

    def test_multiple_archives_fail_instead_of_choosing_one(self):
        self.archive('one.tar.gz'); self.archive('two.tar.gz')
        self.assertEqual(runner.collect(), 1)
        self.assertTrue(any('实际归集 2 个' in error for error in self.metadata()['collectionErrors']))

    def test_empty_or_corrupt_archives_fail(self):
        for empty in (True, False):
            with self.subTest(empty=empty):
                path = self.archive('custom.tar.gz', empty=empty)
                if not empty: path.write_bytes(path.read_bytes()[:32])
                self.assertEqual(runner.collect(), 1)
                self.assertEqual(self.metadata()['files'], [])

    def test_bad_gzip_trailer_is_rejected_after_valid_tar_members(self):
        for truncated in (False, True):
            with self.subTest(truncated=truncated):
                path = self.archive('otherwise-valid.tar.gz')
                content = bytearray(path.read_bytes())
                if truncated: content = content[:-4]
                else: content[-8] ^= 1
                path.write_bytes(content)
                self.assertEqual(runner.collect(), 1)
                self.assertEqual(self.metadata()['files'], [])

    def test_dangling_symlink_is_rejected_and_cleanup_never_deletes_link_target(self):
        private = self.root / 'private'; private.write_text('private')
        link = self.directory / 'different.tar.gz'; link.symlink_to(private)
        self.assertEqual(runner.collect(), 1)
        runner.build()
        self.assertFalse(link.is_symlink())
        self.assertEqual(private.read_text(), 'private')
        link.symlink_to(self.root / 'missing')
        self.assertEqual(runner.collect(), 1)
        self.assertTrue(any('非法产物路径' in error for error in self.metadata()['collectionErrors']))


class ServiceCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name, value in [('ROOT', self.root), ('SOURCE', self.root / 'source')]:
            context = patch.object(runner, name, value)
            context.start()
            self.addCleanup(context.stop)
        self.services = runner.SOURCE / 'services'
        self.services.mkdir(parents=True)
        self.env = patch.dict(os.environ, XRAN_KIND='service', XRAN_TARGET='serviceA', XRAN_SERVICE_SERIAL='true')
        self.env.start()
        self.addCleanup(self.env.stop)

    def fixture(self):
        # Directory creation and dependency generation lack an ordering dependency.
        content = '''ifeq ($(CUR_PROJECT),test)
all: output dep-a dep-b
output:
\t@sleep 0.4
\t@mkdir -p objRelease
dep-a:
\t@printf 'a\\n' > objRelease/a.dep
dep-b:
\t@printf 'b\\n' > objRelease/b.dep
else
all:
\t+$(MAKE) -j32 CUR_PROJECT=test
endif
'''
        (self.services / 'makefile').write_text(content)
        script = self.services / 'build.sh'
        script.write_text('#!/bin/bash\nset -eu\nread -r dpdk\nprintf "%s" "$dpdk" > dpdk-input\nmake -j32\n')
        script.chmod(0o755)
        return content.encode()

    def test_serial_fixes_recursive_make_race_and_answers_dpdk_prompt(self):
        self.fixture()
        original = (self.services / 'makefile').read_bytes()
        baseline = subprocess.run(['make', '-j32'], cwd=self.services, capture_output=True, text=True)
        self.assertNotEqual(baseline.returncode, 0)
        self.assertIn('cannot create', baseline.stderr)
        shutil.rmtree(self.services / 'objRelease', ignore_errors=True)
        runner.build()
        self.assertEqual((self.services / 'dpdk-input').read_text(), '/opt/dpdk-22.03')
        self.assertEqual((self.services / 'objRelease/a.dep').read_text(), 'a\n')
        modified = (self.services / 'makefile').read_bytes()
        self.assertTrue(modified.endswith(original))
        runner.prepare_xran()
        self.assertEqual((self.services / 'makefile').read_bytes(), modified)

    def test_disabled_and_invalid_setting(self):
        original = self.fixture()
        os.environ['XRAN_SERVICE_SERIAL'] = 'false'
        runner.prepare_xran()
        self.assertEqual((self.services / 'makefile').read_bytes(), original)
        os.environ['XRAN_SERVICE_SERIAL'] = 'invalid'
        with self.assertRaisesRegex(ValueError, '必须为 true 或 false'):
            runner.prepare_xran()

    def test_image_build_uses_the_same_preparation(self):
        self.fixture()
        directory = runner.SOURCE / 'platform/k8s/images-build'
        directory.mkdir(parents=True)
        script = directory / 'images-build.sh'
        script.write_text('#!/bin/bash\nset -eu\ncd ../../../services\n./build.sh -c -m serviceA\n')
        script.chmod(0o755)
        os.environ.update(XRAN_KIND='image', XRAN_TARGET='imageA', XRAN_IMAGE_TAG='test')
        with patch.object(runner, 'ensure_docker'):
            runner.build()
        self.assertTrue((self.services / 'objRelease/b.dep').is_file())
        self.assertEqual((self.services / 'dpdk-input').read_text(), '/opt/dpdk-22.03')

    def test_removed_target_and_script_failure_are_reported(self):
        os.environ['XRAN_TARGET'] = 'all'
        with self.assertRaisesRegex(ValueError, 'all 已取消'):
            runner.build()
        os.environ['XRAN_TARGET'] = 'serviceA'
        script = self.services / 'build.sh'
        script.write_text('#!/bin/sh\nexit 17\n')
        script.chmod(0o755)
        with self.assertRaises(subprocess.CalledProcessError) as failed:
            runner.build()
        self.assertEqual(failed.exception.returncode, 17)

    def test_makefile_symlink_is_not_modified(self):
        outside = self.root / 'outside'
        outside.write_bytes(b'original')
        (self.services / 'makefile').symlink_to(outside)
        with self.assertRaisesRegex(ValueError, '必须位于本次检出'):
            runner.prepare_xran()
        self.assertEqual(outside.read_bytes(), b'original')


if __name__ == '__main__':
    unittest.main()
