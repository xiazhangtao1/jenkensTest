import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('image_runner', ROOT / 'runtime/run-build.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class ImageArtifactTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for key, value in [('ROOT', self.root), ('SOURCE', self.root / 'source'), ('PUBLISH', self.root / 'publish')]:
            p = patch.object(runner, key, value); p.start(); self.addCleanup(p.stop)
        self.directory = runner.SOURCE / 'platform/k8s/images-build'
        self.directory.mkdir(parents=True)
        self.path = self.directory / 'imageA.tar'
        self.ref = 'registry.local/xnet/imagea:test'
        self.config = b'{"architecture":"amd64","os":"linux"}'
        self.image_id = 'sha256:' + hashlib.sha256(self.config).hexdigest()
        env = patch.dict(os.environ, XRAN_KIND='image', XRAN_TARGET='imageA', XRAN_IMAGE_TAG='test',
            XRAN_IMAGE_REFERENCE=self.ref, XRAN_BUILD_METADATA='{"result":"SUCCESS"}')
        env.start(); self.addCleanup(env.stop)
        self.present, self.save_failure = True, False
        self.calls = []
        execute = patch.object(runner.subprocess, 'run', side_effect=self.docker)
        execute.start(); self.addCleanup(execute.stop)

    def archive(self, path, reference=None, missing_layer=False):
        members = {'config.json': self.config, 'manifest.json': json.dumps([{'Config':'config.json',
            'RepoTags':[reference or self.ref], 'Layers':['layer.tar']}]).encode()}
        if not missing_layer: members['layer.tar'] = b'layer'
        with tarfile.open(path, 'w') as archive:
            for name, data in members.items():
                member = tarfile.TarInfo(name); member.size = len(data)
                archive.addfile(member, io.BytesIO(data))

    def docker(self, command, **kwargs):
        self.calls.append(command)
        if command[:3] == ['docker','image','inspect']:
            self.assertEqual(command[3], self.ref)
            return SimpleNamespace(returncode=0 if self.present else 1,
                stdout=json.dumps([{'Id':self.image_id,'RepoDigests':[]}]) if self.present else '')
        self.assertEqual(command[:3], ['docker','save','--output'])
        self.assertEqual(command[-1], self.ref)
        self.archive(Path(command[3]))
        if self.save_failure: raise subprocess.CalledProcessError(1, command)
        return SimpleNamespace(returncode=0, stdout='')

    def metadata(self):
        return json.loads((runner.PUBLISH / 'metadata.json').read_text())

    def test_missing_package_is_exported_and_hashed(self):
        self.assertEqual(runner.collect(), 0)
        meta = self.metadata()
        self.assertEqual(meta['imageArchiveSource'], 'docker-save')
        self.assertEqual(meta['imageId'], self.image_id)
        output = runner.PUBLISH / 'imageA.tar'
        self.assertEqual(meta['files'][0]['sha256'], hashlib.sha256(output.read_bytes()).hexdigest())
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.directory.glob('*.tmp')), [])

    def test_intranet_image_names_dispatch_and_support_both_archive_paths(self):
        for target in ['usp-core', 'xran-simuphy']:
            self.path = self.directory / (target + '.tar')
            self.ref = '192.168.10.35/xnet/' + target + ':test'
            os.environ.update(XRAN_TARGET=target, XRAN_IMAGE_REFERENCE=self.ref)
            with patch.object(runner, 'ensure_docker'), patch.object(runner, 'run') as command:
                runner.build()
                command.assert_called_once_with(['./images-build.sh', target, 'test'],
                    self.directory, input_text='/opt/dpdk-22.03\n')
            for origin in ['script', 'docker-save']:
                with self.subTest(target=target, origin=origin):
                    if origin == 'script':
                        self.archive(self.path)
                    self.calls.clear()
                    self.assertEqual(runner.collect(), 0)
                    meta = self.metadata()
                    self.assertEqual(meta['imageArchiveSource'], origin)
                    self.assertEqual(meta['imageReference'], self.ref)
                    self.assertEqual(meta['imageId'], self.image_id)
                    self.assertEqual([f['name'] for f in meta['files']], [target + '.tar'])
                    artifact = runner.PUBLISH / (target + '.tar')
                    self.assertEqual(meta['files'][0]['sha256'], hashlib.sha256(artifact.read_bytes()).hexdigest())
                    artifact.unlink()

    def test_script_package_is_used_without_save_even_if_image_was_removed(self):
        self.archive(self.path); self.present = False
        self.assertEqual(runner.collect(), 0)
        self.assertEqual(self.metadata()['imageArchiveSource'], 'script')
        self.assertEqual(len(self.calls), 1)

    def test_missing_reference_never_guesses_or_pulls(self):
        self.present = False
        self.assertEqual(runner.collect(), 1)
        self.assertIn(self.ref, self.metadata()['collectionErrors'][0])
        self.assertEqual(len(self.calls), 1)
        self.assertFalse((runner.PUBLISH / 'imageA.tar').exists())

    def test_bad_packages_are_not_replaced(self):
        for variant in ('corrupt', 'wrong-tag', 'missing-layer', 'wrong-id', 'symlink'):
            with self.subTest(variant=variant):
                self.path.unlink(missing_ok=True)
                self.calls.clear()
                if variant == 'corrupt': self.path.write_bytes(b'broken')
                elif variant == 'symlink': self.path.symlink_to(self.root/'absent')
                else:
                    self.archive(self.path, self.ref+'-other' if variant == 'wrong-tag' else None,
                                 missing_layer=variant == 'missing-layer')
                    if variant == 'wrong-id': self.image_id = 'sha256:'+'0'*64
                self.assertEqual(runner.collect(), 1)
                self.assertEqual(len(self.calls), 1)
                self.assertFalse((runner.PUBLISH / 'imageA.tar').exists())
                self.image_id = 'sha256:' + hashlib.sha256(self.config).hexdigest()

    def test_save_failure_cleans_partial_output(self):
        self.save_failure = True
        self.assertEqual(runner.collect(), 1)
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_failed_script_retains_valid_image_but_failure_result(self):
        os.environ['XRAN_BUILD_METADATA'] = '{"result":"FAILURE"}'
        self.assertEqual(runner.collect(), 0)
        self.assertEqual(self.metadata()['result'], 'FAILURE')
        self.assertEqual(self.metadata()['imageArchiveSource'], 'docker-save')
        self.assertNotEqual(self.metadata()['pushStatus'], 'success')

    def test_cancelled_build_does_not_start_export(self):
        os.environ['XRAN_BUILD_METADATA'] = '{"result":"ABORTED"}'
        self.assertEqual(runner.collect(), 0)
        self.assertEqual(self.calls, [])

    def test_build_removes_only_target_archive_before_script(self):
        self.path.write_bytes(b'old')
        other = self.directory / 'imageB.tar'; other.write_bytes(b'keep')
        with patch.object(runner, 'ensure_docker'), patch.object(runner, 'run') as command:
            command.side_effect = lambda *args, **kwargs: self.assertFalse(self.path.exists())
            runner.build()
        self.assertEqual(other.read_bytes(), b'keep')


if __name__ == '__main__':
    unittest.main()
