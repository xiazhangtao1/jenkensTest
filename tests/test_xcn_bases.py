import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('xcn_base_ci', ROOT / 'scripts/ci.py')
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


class XcnBaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name)
        self.archive = self.cache / 'xcn-base-images.tar'
        self.archive.write_bytes(b'previous Ubuntu and intranet VPP')
        self.cfg = {'xcn': {'ubuntuImage': 'intranet/ubuntu:22.04', 'vppImage': 'intranet/vpp:26.06'},
                    'xcnBasesImage': 'xran-ci/xcn-bases:2'}
        self.calls = []

    def record_run(self, command, **kwargs):
        self.calls.append([str(x) for x in command])
        if command[:2] == ['docker', 'save']:
            Path(command[3]).write_bytes(b'new Ubuntu and same VPP')

    def test_replace_loads_existing_vpp_before_retagging_and_saves_both(self):
        compiler = Mock()
        with patch.object(ci, 'CACHE', self.cache), patch.object(ci, 'compiler_module', return_value=compiler), \
             patch.object(ci, 'run', side_effect=self.record_run):
            ci.prepare_xcn_bases(self.cfg, True, 'sha256:pinned-ubuntu')
        self.assertEqual(self.calls[0], ['docker', 'load', '-i', str(self.archive)])
        self.assertEqual(self.calls[1], ['docker', 'tag', 'sha256:pinned-ubuntu', 'intranet/ubuntu:22.04'])
        self.assertEqual(self.calls[2][-2:], ['intranet/ubuntu:22.04', 'intranet/vpp:26.06'])
        self.assertEqual(self.archive.read_bytes(), b'new Ubuntu and same VPP')
        self.assertEqual(compiler.ensure_image.call_count, 2)
        self.assertEqual(list(self.cache.iterdir()), [self.archive])

    def test_failed_save_leaves_existing_archive_and_removes_temporary_files(self):
        def fail(command, **kwargs):
            if command[:2] == ['docker', 'save']:
                Path(command[3]).write_bytes(b'incomplete')
                raise subprocess.CalledProcessError(1, command)
        with patch.object(ci, 'CACHE', self.cache), patch.object(ci, 'compiler_module', return_value=Mock()), \
             patch.object(ci, 'run', side_effect=fail):
            with self.assertRaises(subprocess.CalledProcessError):
                ci.prepare_xcn_bases(self.cfg, True, 'sha256:pinned-ubuntu')
        self.assertEqual(self.archive.read_bytes(), b'previous Ubuntu and intranet VPP')
        self.assertEqual(list(self.cache.iterdir()), [self.archive])

    def test_shared_ubuntu_and_vpp_references_cannot_be_retagged(self):
        for target in [ci.versions()['UBUNTU_IMAGE'], self.cfg['xcn']['vppImage']]:
            self.cfg['xcn']['ubuntuImage'] = target
            with patch.object(ci, 'CACHE', self.cache), patch.object(ci, 'compiler_module'), patch.object(ci, 'run') as run:
                with self.assertRaisesRegex(ValueError, '独立镜像引用'):
                    ci.prepare_xcn_bases(self.cfg, True, 'sha256:pinned-ubuntu')
                self.assertFalse(any(call.args[0][:2] == ['docker', 'tag'] for call in run.call_args_list))

    def test_preflight_is_a_disposable_simulation_using_only_selected_source(self):
        args = SimpleNamespace(apt_sources='config/sources.intranet.list', offline=True)
        with patch.object(ci, 'run') as run:
            ci.check_xcn_apt(args, 'sha256:pinned-ubuntu')
        command = run.call_args.args[0]
        self.assertEqual(command[:5], ['docker', 'run', '--rm', '--network', 'host'])
        self.assertIn(f'{ROOT}/config/sources.intranet.list:/etc/apt/sources.list:ro', command)
        script = command[command.index('bash') + 2]
        self.assertIn('--simulate', script)
        self.assertIn('APT::Update::Error-Mode=any', script)
        for option in ['Dir::Etc::sourceparts=-', 'Acquire::http::Proxy=DIRECT', 'Acquire::https::Proxy=DIRECT']:
            self.assertIn(option, command)
        self.assertNotIn('-e', command)

    def test_bad_image_or_failed_preflight_cannot_replace_archive_or_build_carrier(self):
        args = SimpleNamespace(xcn_ubuntu_archive=str(self.archive), offline=True)
        lock = json.loads((ROOT / 'config/xcn-ubuntu.lock.json').read_text())
        for good_image in [False, True]:
            compiler = Mock()
            compiler.inspect.return_value = {'Id': lock['imageId'] if good_image else 'sha256:unexpected',
                                             'Os': 'linux', 'Architecture': 'amd64'}
            with patch.object(ci, 'config', return_value=self.cfg), patch.object(ci, 'compiler_module', return_value=compiler), \
                 patch.object(ci, 'run') as run, patch.object(ci, 'prepare_xcn_bases') as prepare, \
                 patch.object(ci, 'check_xcn_apt', side_effect=RuntimeError('preflight failed')):
                with self.assertRaises((ValueError, RuntimeError)):
                    ci.xcn_bases(args)
                prepare.assert_not_called()
                self.assertTrue(all(call.args[0][:2] == ['docker', 'load'] for call in run.call_args_list))

    def test_carrier_build_has_no_compiler_controller_or_network_build(self):
        args = SimpleNamespace(xcn_ubuntu_archive=None, offline=True)
        compiler = Mock()
        with patch.object(ci, 'config', return_value=self.cfg), patch.object(ci, 'compiler_module', return_value=compiler), \
             patch.object(ci, 'CACHE', self.cache), patch.object(ci, 'prepare_xcn_bases') as prepare, \
             patch.object(ci, 'run') as run:
            ci.xcn_bases(args)
        prepare.assert_called_once_with(self.cfg, True, None)
        command = run.call_args.args[0]
        self.assertEqual(command[:5], ['docker', 'build', '--network', 'none', '--pull=false'])
        self.assertEqual(command[-2], 'xran-ci/xcn-bases:2')
        self.assertEqual(command[-1].parent, self.cache)
        self.assertEqual(command[-1] / 'Dockerfile', command[command.index('-f') + 1])
        self.assertEqual(list(self.cache.iterdir()), [self.archive])
        compiler.build.assert_not_called()


if __name__ == '__main__':
    unittest.main()
