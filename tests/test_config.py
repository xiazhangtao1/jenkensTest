import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
import tempfile
from unittest.mock import patch

import yaml

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ci', REPO / 'scripts/ci.py')
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


def memory(value):
    if value.endswith('Gi'):
        return int(value[:-2]) * 1024
    return int(value.removesuffix('Mi'))


class ConfigurationTests(unittest.TestCase):
    def test_both_pod_profiles_enforce_total_resources(self):
        cfg = ci.config(SimpleNamespace(site=REPO / 'config/site.yaml', namespace=None, offline=False))
        self.assertEqual(cfg['concurrency'], 6)
        for kind in ['service', 'image', 'xcn']:
            pod = ci.pod_template(cfg, kind)
            limits = [c['resources']['limits'] for c in pod['spec']['containers']]
            self.assertEqual(sum(memory(x['ephemeral-storage']) for x in limits), 20 * 1024)
            self.assertEqual(sum(memory(x['memory']) for x in limits), 8 * 1024)
            self.assertEqual(sum(int(x['cpu'].removesuffix('m')) for x in limits), 4000)
            self.assertFalse(pod['spec']['automountServiceAccountToken'])
            privileged = [c['name'] for c in pod['spec']['containers'] if c.get('securityContext', {}).get('privileged')]
            self.assertEqual(privileged, ['dind'] if kind != 'service' else [])
            if kind != 'service':
                dind = next(c for c in pod['spec']['containers'] if c['name'] == 'dind')
                self.assertEqual(memory(dind['resources']['limits']['memory']), 4096)
                self.assertEqual(dind['command'], ['sh', '/etc/xran/dind-start.sh'])
            if kind == 'xcn':
                init = pod['spec']['initContainers'][0]
                self.assertEqual(init['image'], cfg['xcnBasesImage'])
                bases = next(v for v in pod['spec']['volumes'] if v['name'] == 'xcn-bases')
                # File cache during archive copy must fit without increasing the Pod budget.
                self.assertGreaterEqual(memory(init['resources']['limits']['memory']), memory(bases['emptyDir']['sizeLimit']))
                self.assertLessEqual(memory(init['resources']['limits']['memory']), sum(memory(x['memory']) for x in limits))
                self.assertLess(cfg['xcn']['innerBuildMemoryGiB'], cfg['dindMemoryGiB'])

    def test_offline_profiles_keep_selected_compiler_policy(self):
        cfg = ci.config(SimpleNamespace(site=REPO / 'config/site.yaml', namespace='jenkins-offline-verify', offline=True))
        for kind in ['service', 'image', 'xcn']:
            containers = ci.pod_template(cfg, kind)['spec']['containers']
            self.assertTrue(all(c['imagePullPolicy'] == ('Always' if c['name'] == 'builder' else 'Never')
                                for c in containers))
            cfg['compilerImagePullPolicy'] = 'Never'
            self.assertTrue(all(c['imagePullPolicy'] == 'Never' for c in ci.pod_template(cfg, kind)['spec']['containers']))
            cfg['compilerImagePullPolicy'] = 'Always'

    def test_compiler_image_override_and_secrets(self):
        args = SimpleNamespace(site=REPO / 'config/site.yaml', namespace=None, offline=False,
                               image_name='registry.local:5000/team/sdk', image_tag='26.11.05')
        cfg = ci.config(args)
        cfg['compilerImagePullPolicy'] = 'IfNotPresent'
        cfg['compilerImagePullSecrets'] = ['compile-registry']
        for kind in ['service', 'image', 'xcn']:
            pod = ci.pod_template(cfg, kind)
            builder = next(c for c in pod['spec']['containers'] if c['name'] == 'builder')
            self.assertEqual(builder['image'], 'registry.local:5000/team/sdk:26.11.05')
            self.assertEqual(builder['imagePullPolicy'], 'IfNotPresent')
            self.assertEqual(pod['spec']['imagePullSecrets'], [{'name': 'compile-registry'}])

    def test_defaults(self):
        cfg = ci.config(SimpleNamespace(site=REPO / 'config/site.yaml', namespace=None, offline=False))
        self.assertEqual(cfg['builderImage'], '192.168.10.35/xnet/xran-compile:latest')
        self.assertEqual(cfg['compilerImagePullPolicy'], 'Always')
        self.assertIs(cfg['xcn']['offline'], True)

    def test_previous_site_configuration_keeps_project_values(self):
        source = yaml.safe_load((REPO / 'config/site.yaml').read_text())
        legacy = source.pop('xcn')
        legacy.update(gitUrl='ssh://tester@192.168.10.82:29418/ICT_CN', offline=False)
        source['open5gs'] = legacy
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'site.yaml'
            path.write_text(yaml.safe_dump(source))
            cfg = ci.config(SimpleNamespace(site=path, namespace=None, offline=True))
            self.assertNotIn('open5gs', cfg)
            self.assertEqual(cfg['xcn'], legacy)
            pod = ci.pod_template(cfg, 'xcn')
            builder = next(c for c in pod['spec']['containers'] if c['name'] == 'builder')
            mounts = {v['mountPath'] for v in builder['volumeMounts']}
            self.assertIn('/opt/xran/xcn-bases', mounts)
            self.assertIn('/opt/xran/open5gs-bases', mounts)

    def test_xcn_mode_is_a_site_setting(self):
        source = yaml.safe_load((REPO / 'config/site.yaml').read_text())
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory) / 'site.yaml'
            args = SimpleNamespace(site=site, namespace=None, offline=False)
            for value in [None, True, False, 'false', 0]:
                with self.subTest(value=value):
                    if value is None:
                        source['xcn'].pop('offline', None)
                    else:
                        source['xcn']['offline'] = value
                    site.write_text(yaml.safe_dump(source))
                    if isinstance(value, bool) or value is None:
                        cfg = ci.config(args)
                        self.assertIs(cfg['xcn']['offline'], True if value is None else value)
                    else:
                        with self.assertRaisesRegex(ValueError, 'xcn.offline'):
                            ci.config(args)

            hashes = []
            for value in [True, False]:
                source['xcn']['offline'] = value
                site.write_text(yaml.safe_dump(source))
                with patch.object(ci, 'state_dir', return_value=Path(directory)):
                    ci.render(args)
                values = yaml.safe_load((Path(directory) / 'values.yaml').read_text())
                hashes.append(values['controller']['podAnnotations']['xran-ci/site-sha256'])
            self.assertNotEqual(*hashes)  # A mode change must roll out with the new ConfigMap.

    def test_small_temporary_storage_has_valid_requests_and_limits(self):
        cfg = ci.config(SimpleNamespace(site=REPO / 'config/site.yaml', namespace=None, offline=False))
        for size in [1, 2, 6, 20]:
            cfg['temporaryStorageGiB'] = size
            for kind in ['service', 'image', 'xcn']:
                with self.subTest(size=size, kind=kind):
                    containers = ci.pod_template(cfg, kind)['spec']['containers']
                    self.assertEqual(sum(memory(c['resources']['limits']['ephemeral-storage']) for c in containers), size * 1024)
                    for container in containers:
                        requests, limits = (container['resources'][key] for key in ['requests', 'limits'])
                        for resource in ['memory', 'ephemeral-storage']:
                            self.assertGreater(memory(limits[resource]), 0)
                            self.assertLessEqual(memory(requests[resource]), memory(limits[resource]))


if __name__ == '__main__':
    unittest.main()
