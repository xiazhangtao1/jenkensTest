import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import subprocess

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('compile_image', ROOT / 'scripts/compile-image.py')
compiler = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compiler)


class CompilerTests(unittest.TestCase):
    def test_reference_options(self):
        args = compiler.parser().parse_args([])
        self.assertEqual(compiler.image_reference(args.image_name, args.image_tag),
                         '192.168.10.35/xnet/xran-compile:latest')
        self.assertEqual(compiler.image_reference('registry.local:5000/team/sdk', '26.11.05'),
                         'registry.local:5000/team/sdk:26.11.05')
        self.assertEqual(compiler.image_reference('example/sdk', 'feature_A-2'), 'example/sdk:feature_A-2')
        for name, tag in [('sdk:old', 'latest'), ('sdk', ''), ('sdk', 'bad;touch'),
                          ('sdk\nFROM evil', 'tag'), ('-sdk', 'tag'), ('-repo/sdk', 'tag'), ('UPPER/sdk', 'latest')]:
            with self.subTest(name=name, tag=tag), self.assertRaises(ValueError):
                compiler.image_reference(name, tag)

    def test_split_sources_restore_and_detect_corruption(self):
        original_root, original_cache = compiler.ROOT, compiler.CACHE
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            vendor = root / 'vendor/compile'
            vendor.mkdir(parents=True)
            first, second = vendor / 'part0', vendor / 'part1'
            first.write_bytes(b'first'); second.write_bytes(b'second')
            whole = root / 'whole'; whole.write_bytes(b'firstsecond')
            record = {'archive': 'source.tar.gz', 'sha256': compiler.sha256(whole),
                      'parts': [{'path': p.name, 'sha256': compiler.sha256(p)} for p in (first, second)]}
            (vendor / 'manifest.json').write_text(json.dumps({'sources': [record]}))
            try:
                compiler.ROOT, compiler.CACHE = root, root / '.cache'
                compiler.assemble_sources()
                restored = root / '.cache/compile-sources/source.tar.gz'
                self.assertEqual(restored.read_bytes(), b'firstsecond')
                restored.unlink(); second.write_bytes(b'broken')
                with self.assertRaisesRegex(RuntimeError, '分片缺失或校验失败'):
                    compiler.assemble_sources()
                self.assertFalse(restored.exists())
            finally:
                compiler.ROOT, compiler.CACHE = original_root, original_cache

    def test_make_rejects_invalid_tag_without_shell_expansion(self):
        with tempfile.TemporaryDirectory() as folder:
            sentinel = Path(folder) / 'unexpected'
            result = subprocess.run(['make', 'compile-image', 'IMAGE_TAG=bad;touch ' + str(sentinel)],
                                    cwd=ROOT, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('非法镜像名称或 tag', result.stdout + result.stderr)
            self.assertFalse(sentinel.exists())

    def test_offline_apt_cache_accepts_internal_source_without_network(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / 'config'; config.mkdir()
            (config / 'apt-packages.txt').write_text('gcc\n')
            (config / 'compile-apt-packages.txt').write_text('meson\n')
            (config / 'sources.intranet.list').write_text('deb http://192.168.10.36/ubuntu_22_04/ jammy main\n')
            cache = root / '.cache/compile-apt'; cache.mkdir(parents=True)
            deb = cache / 'gcc.deb'; deb.write_bytes(b'cached-deb')
            (cache / 'manifest.json').write_text(json.dumps({'inputs': {'baseImageId': 'base-id',
                'sourcesSha256': 'prepared-with-online-sources', 'packages': ['gcc', 'meson']},
                'debs': {deb.name: compiler.sha256(deb)}}))
            args = SimpleNamespace(offline=True, apt_sources='config/sources.intranet.list')
            with patch.object(compiler, 'ROOT', root), patch.object(compiler, 'CACHE', root / '.cache'), \
                 patch.object(compiler, 'inspect', return_value={'Id': 'base-id'}), \
                 patch.object(compiler, 'run', side_effect=AssertionError('offline network invocation')):
                compiler.prepare_apt(args, 'ubuntu:22.04')
                deb.write_bytes(b'corrupt')
                with self.assertRaisesRegex(RuntimeError, '完整 APT 缓存缺失'):
                    compiler.prepare_apt(args, 'ubuntu:22.04')


if __name__ == '__main__':
    unittest.main()
