import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DindStartupTests(unittest.TestCase):
    """Execute the shell script against a temporary cgroup filesystem.

    These cover hierarchy/argument handling, not kernel cgroup enforcement.
    """

    def launch(self, folder, membership, parent=None, controllers='cpu memory pids', blocked=False):
        root = Path(folder)
        hierarchy = root / 'cgroup'
        hierarchy.mkdir()
        membership_file = root / 'membership'
        membership_file.write_text(membership)
        if parent is not None:
            (hierarchy / 'cgroup.controllers').write_text(controllers + '\n')
            directory = hierarchy / parent.lstrip('/')
            directory.mkdir(parents=True, exist_ok=True)
            (directory / 'cgroup.controllers').write_text(controllers + '\n')
            (directory / 'cgroup.procs').write_text('123\n')
            if blocked:
                (directory / 'cgroup.subtree_control').mkdir()
            else:
                (directory / 'cgroup.subtree_control').write_text('')
        script = (ROOT / 'runtime/dind-start.sh').read_text()
        script = script.replace('/sys/fs/cgroup', str(hierarchy))
        script = script.replace('/proc/self/cgroup', str(membership_file))
        entrypoint = root / 'dockerd-entrypoint.sh'
        entrypoint.write_text('#!/usr/bin/env python3\nimport json,os,sys\n'
                              'open(os.environ["ARGS_FILE"],"w").write(json.dumps(sys.argv[1:]))\n')
        entrypoint.chmod(0o755)
        env = dict(os.environ, PATH=str(root) + ':' + os.environ['PATH'], ARGS_FILE=str(root / 'args.json'))
        return subprocess.run(['sh', '-s', '--', '--insecure-registry=registry:5000'], input=script,
                              text=True, capture_output=True, env=env, timeout=5)

    def test_hybrid_v1_uses_memory_membership_even_when_unified_entry_is_first(self):
        with tempfile.TemporaryDirectory() as folder:
            result = self.launch(folder, '0::/unified\n7:memory:/kubepods/pod-a/dind\n')
            self.assertEqual(result.returncode, 0, result.stderr)
            args = json.loads((Path(folder) / 'args.json').read_text())
            self.assertIn('--cgroup-parent=/kubepods/pod-a/dind', args)
            self.assertIn('--insecure-registry=registry:5000', args)
            self.assertNotIn('--exec-opt=native.cgroupdriver=cgroupfs', args)

    def test_v2_host_and_private_namespaces_keep_builds_under_dind_limit(self):
        for parent in ('/kubepods.slice/pod-a/dind.scope', '/'):
            with self.subTest(parent=parent), tempfile.TemporaryDirectory() as folder:
                result = self.launch(folder, f'0::{parent}\n', parent=parent)
                self.assertEqual(result.returncode, 0, result.stderr)
                args = json.loads((Path(folder) / 'args.json').read_text())
                self.assertIn(f'--cgroup-parent={parent.rstrip("/")}/xran-builds', args)
                self.assertIn('--exec-opt=native.cgroupdriver=cgroupfs', args)
                self.assertIn('--insecure-registry=registry:5000', args)
                directory = Path(folder) / 'cgroup' / parent.lstrip('/')
                self.assertEqual((directory / 'xran-daemon/cgroup.procs').read_text(), '123\n')
                self.assertEqual(set((directory / 'cgroup.subtree_control').read_text().split()),
                                 {'+cpu', '+memory', '+pids'})

    def test_missing_memory_delegation_stops_before_daemon_start(self):
        with tempfile.TemporaryDirectory() as folder:
            result = self.launch(folder, '0::/\n', parent='/', controllers='cpu pids')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('not delegated: memory', result.stderr)
            self.assertFalse((Path(folder) / 'args.json').exists())

    def test_failed_delegation_has_bounded_retries_and_does_not_start_daemon(self):
        with tempfile.TemporaryDirectory() as folder:
            result = self.launch(folder, '0::/\n', parent='/', blocked=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Cannot delegate DinD cgroup v2 controllers', result.stderr)
            self.assertFalse((Path(folder) / 'args.json').exists())

    def test_missing_memory_membership_or_escaping_path_stops_before_daemon(self):
        for membership in ('0::/hybrid\n', '7:memory:/../../outside\n', '7:memory:relative\n'):
            with self.subTest(membership=membership), tempfile.TemporaryDirectory() as folder:
                result = self.launch(folder, membership)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((Path(folder) / 'args.json').exists())


if __name__ == '__main__':
    unittest.main()
