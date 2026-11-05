import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_runner', ROOT / 'runtime/run-build.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        runner.ROOT = self.root
        runner.SOURCE = self.root / 'source'
        runner.PUBLISH = self.root / 'publish'
        (runner.SOURCE / 'helm').mkdir(parents=True)
        self.env = patch.dict(os.environ, XRAN_KIND='open5gs', XRAN_TARGET='release', XRAN_CHART_VERSION='26.11.6',
                              XRAN_IMAGE_TAG='26.11.6', XRAN_REGISTRY='192.168.10.35',
                              XRAN_OFFLINE='false', XRAN_BUILD_METADATA=json.dumps({'result': 'SUCCESS'}))
        self.env.start()
        self.addCleanup(self.env.stop)

    def archive(self, name, members):
        with tarfile.open(runner.SOURCE / 'helm' / name, 'w:gz' if name.endswith('.tgz') else 'w') as tar:
            for key, value in members.items():
                data = value.encode()
                info = tarfile.TarInfo(key); info.size = len(data)
                tar.addfile(info, io.BytesIO(data))

    def fixtures(self, chart_version='26.11.6'):
        for name in ['runtime', 'webui', 'monitor']:
            ref = f'192.168.10.35/xcn/xcn-{name}:26.11.6'
            self.archive(f'xcn-{name}.tar', {'manifest.json': json.dumps([{'Config': 'config.json',
                'RepoTags': [ref], 'Layers': ['layer.tar']}]), 'config.json': '{}', 'layer.tar': 'layer'})
        self.archive('xcn-26.11.6.tgz', {'xcn/Chart.yaml': f'name: xcn\nversion: {chart_version}\nappVersion: 26.11.6\n',
            'xcn/values.yaml': 'images:\n  runtime: {tag: 26.11.6}\n  webui: {tag: 26.11.6}\nmonitor: {image: {tag: 26.11.6}}\n'})

    def test_four_outputs_and_push_warning(self):
        self.fixtures()
        self.assertEqual(runner.collect(), 0)
        meta = json.loads((runner.PUBLISH / 'metadata.json').read_text())
        self.assertEqual(len(meta['files']), 4)
        self.assertEqual(len(meta['images']), 3)
        self.assertTrue(meta['warnings'])
        self.assertFalse(meta['offline'])

    def test_missing_output_and_wrong_chart_version_fail(self):
        for wrong_version in [False, True]:
            with self.subTest(wrong_version=wrong_version):
                self.fixtures('0.1.0' if wrong_version else '26.11.6')
                if not wrong_version: (runner.SOURCE / 'helm/xcn-webui.tar').unlink()
                self.assertEqual(runner.collect(), 1)
                self.assertEqual(json.loads((runner.PUBLISH / 'metadata.json').read_text())['result'], 'FAILURE')

    def test_corrupt_image_and_symlink_fail(self):
        self.fixtures()
        (runner.SOURCE / 'helm/xcn-runtime.tar').write_bytes(b'broken')
        monitor = runner.SOURCE / 'helm/xcn-monitor.tar'
        monitor.unlink(); monitor.symlink_to(runner.SOURCE / 'helm/xcn-webui.tar')
        self.assertEqual(runner.collect(), 1)
        self.assertFalse((runner.PUBLISH / 'xcn-runtime.tar').exists())
        self.assertFalse((runner.PUBLISH / 'xcn-monitor.tar').exists())

    def test_original_script_receives_offline_and_preserves_versions(self):
        script = runner.SOURCE / 'helm/build-release.sh'
        script.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > arguments\nprintf "192.168.10.35/xcn/xcn-runtime:26.11.6: build OK, push FAILED (local image retained)\\n"\n')
        script.chmod(0o755)
        for offline in ['true', 'false']:
            os.environ['XRAN_OFFLINE'] = offline
            with patch.object(runner, 'ensure_docker'):
                runner.build()
            self.assertEqual((runner.SOURCE / 'helm/arguments').read_text().splitlines(),
                             (['--offline'] if offline == 'true' else []) + ['26.11.6', '26.11.6'])
            self.assertEqual(json.loads((self.root / 'release-push.json').read_text())['192.168.10.35/xcn/xcn-runtime:26.11.6'], 'failed')

    def test_missing_main_entry_is_explicit(self):
        with self.assertRaisesRegex(RuntimeError, '缺少 helm/build-release.sh'):
            runner.build()
