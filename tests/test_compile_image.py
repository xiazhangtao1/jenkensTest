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
        self.assertEqual(args.dockerfile, 'docker/builder-compat.Dockerfile')
        self.assertEqual(compiler.image_reference('registry.local:5000/team/sdk', '26.11.05'),
                         'registry.local:5000/team/sdk:26.11.05')
        self.assertEqual(compiler.image_reference('example/sdk', 'feature_A-2'), 'example/sdk:feature_A-2')
        for name, tag in [('sdk:old', 'latest'), ('sdk', ''), ('sdk', 'bad;touch'),
                          ('sdk\nFROM evil', 'tag'), ('-sdk', 'tag'), ('-repo/sdk', 'tag'), ('UPPER/sdk', 'latest')]:
            with self.subTest(name=name, tag=tag), self.assertRaises(ValueError):
                compiler.image_reference(name, tag)

    def test_dockerfile_paths_and_build_arguments(self):
        with tempfile.TemporaryDirectory(prefix='compiler path ') as folder:
            root = Path(folder)
            recipe = root / "Dockerfile custom ' name"
            recipe.write_text('FROM ubuntu:22.04\n')
            cache = root / '.cache'
            cache.mkdir()
            (cache / 'base-images.tar').touch()
            with patch.object(compiler, 'ROOT', root), patch.object(compiler, 'CACHE', cache):
                self.assertEqual(compiler.resolve_dockerfile(recipe.name), recipe)
                self.assertEqual(compiler.resolve_dockerfile(str(recipe)), recipe)
                for invalid in ('missing.Dockerfile', str(root)):
                    with self.assertRaisesRegex(ValueError, 'Dockerfile 不存在'):
                        compiler.resolve_dockerfile(invalid)
                args = SimpleNamespace(image_name='example/sdk', image_tag='test', jobs=4,
                    dockerfile=recipe.name, offline=True, apt_sources='config/sources.intranet.list', no_export=True)
                with patch.object(compiler, 'versions', return_value={'UBUNTU_IMAGE': 'ubuntu:22.04',
                        'DOCKER_CLI_IMAGE': 'docker:test'}), \
                     patch.object(compiler, 'assemble_sources'), patch.object(compiler, 'ensure_image'), \
                     patch.object(compiler, 'prepare_apt'), patch.object(compiler, 'prepare_helm'), \
                     patch.object(compiler, 'run') as execute:
                    (root / 'config').mkdir()
                    (root / 'config/base-images.txt').write_text('')
                    compiler.build(args)
                command = execute.call_args_list[0].args[0]
                self.assertEqual(command[command.index('-f') + 1], recipe)
                self.assertEqual(command[-1], root)
                self.assertEqual(command[command.index('--network') + 1], 'none')

    def test_make_dockerfile_path_is_not_executed_by_shell(self):
        with tempfile.TemporaryDirectory() as folder:
            sentinel = Path(folder) / 'unexpected'
            result = subprocess.run(['make', 'compile-image',
                'DOCKERFILE=missing file;touch ' + str(sentinel)], cwd=ROOT, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Dockerfile 不存在', result.stdout + result.stderr)
            self.assertFalse(sentinel.exists())

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

    def test_verified_apt_cache_survives_mirror_change_in_both_modes(self):
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
                original_manifest = (cache / 'manifest.json').read_bytes()
                for offline in (True, False):
                    args.offline = offline
                    compiler.prepare_apt(args, 'ubuntu:22.04')
                    self.assertEqual((cache / 'manifest.json').read_bytes(), original_manifest)
                args.offline = True
                deb.write_bytes(b'corrupt')
                with self.assertRaisesRegex(RuntimeError, '完整 APT 缓存缺失'):
                    compiler.prepare_apt(args, 'ubuntu:22.04')

    def test_apt_cache_input_changes_are_not_reused_offline(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / 'config'; config.mkdir()
            (config / 'apt-packages.txt').write_text('gcc\n')
            (config / 'compile-apt-packages.txt').write_text('meson\n')
            (config / 'sources.list').write_text('deb http://192.168.10.36/ubuntu_22_04/ jammy main\n')
            cache = root / '.cache/compile-apt'; cache.mkdir(parents=True)
            deb = cache / 'gcc.deb'; deb.write_bytes(b'verified-deb')
            lock = {'inputs': {'baseImageId': 'base-id', 'sourcesSha256': 'other-mirror',
                              'packages': ['gcc', 'meson']}, 'debs': {deb.name: compiler.sha256(deb)}}
            args = SimpleNamespace(offline=True, apt_sources='config/sources.list')
            with patch.object(compiler, 'ROOT', root), patch.object(compiler, 'CACHE', root / '.cache'), \
                 patch.object(compiler, 'inspect', return_value={'Id': 'base-id'}), \
                 patch.object(compiler, 'run', side_effect=AssertionError('offline network invocation')):
                for changes in [{'baseImageId': 'different-base'}, {'packages': ['gcc']}]:
                    changed = dict(lock, inputs=dict(lock['inputs'], **changes))
                    (cache / 'manifest.json').write_text(json.dumps(changed))
                    with self.subTest(changes=changes), self.assertRaisesRegex(RuntimeError, '完整 APT 缓存缺失'):
                        compiler.prepare_apt(args, 'ubuntu:22.04')

    def test_failed_apt_preparation_preserves_previous_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / 'config'; config.mkdir()
            (config / 'packages.txt').write_text('libzstd-dev pkgconf python3-yaml\n')
            (config / 'sources.list').write_text('deb http://192.168.10.36/ubuntu_22_04/ jammy main\n')
            cache = root / '.cache/compile-extra-apt'; cache.mkdir(parents=True)
            deb = cache / 'pkgconf.deb'; deb.write_bytes(b'verified-deb')
            (cache / 'manifest.json').write_text(json.dumps({'inputs': {'baseImageId': 'base-id',
                'sourcesSha256': 'old-mirror', 'packages': ['pkgconf', 'python3-yaml']},
                'debs': {deb.name: compiler.sha256(deb)}}))
            before = {p.name: p.read_bytes() for p in cache.iterdir()}
            args = SimpleNamespace(offline=False, apt_sources='config/sources.list', prepare_network='host')
            with patch.object(compiler, 'ROOT', root), patch.object(compiler, 'CACHE', root / '.cache'), \
                 patch.object(compiler, 'inspect', return_value={'Id': 'base-id'}), \
                 patch.object(compiler, 'run', side_effect=subprocess.CalledProcessError(100, ['apt-get'])) as execute:
                with self.assertRaises(subprocess.CalledProcessError):
                    compiler.prepare_apt(args, 'ubuntu:22.04', 'compile-extra-apt', ['config/packages.txt'])
                self.assertEqual(execute.call_count, 1)  # A new package still requires preparation.
                self.assertEqual({p.name: p.read_bytes() for p in cache.iterdir()}, before)


if __name__ == '__main__':
    unittest.main()
