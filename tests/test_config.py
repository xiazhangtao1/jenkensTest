import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

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
        for kind in ['service', 'image', 'open5gs']:
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
            if kind == 'open5gs':
                self.assertEqual(pod['spec']['initContainers'][0]['image'], cfg['open5gsBasesImage'])
                self.assertLess(cfg['open5gs']['innerBuildMemoryGiB'], cfg['dindMemoryGiB'])

    def test_offline_profiles_keep_selected_compiler_policy(self):
        cfg = ci.config(SimpleNamespace(site=REPO / 'config/site.yaml', namespace='jenkins-offline-verify', offline=True))
        for kind in ['service', 'image', 'open5gs']:
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
        for kind in ['service', 'image', 'open5gs']:
            pod = ci.pod_template(cfg, kind)
            builder = next(c for c in pod['spec']['containers'] if c['name'] == 'builder')
            self.assertEqual(builder['image'], 'registry.local:5000/team/sdk:26.11.05')
            self.assertEqual(builder['imagePullPolicy'], 'IfNotPresent')
            self.assertEqual(pod['spec']['imagePullSecrets'], [{'name': 'compile-registry'}])

    def test_defaults(self):
        cfg = ci.config(SimpleNamespace(site=REPO / 'config/site.yaml', namespace=None, offline=False))
        self.assertEqual(cfg['builderImage'], '192.168.10.35/xnet/xran-compile:latest')
        self.assertEqual(cfg['compilerImagePullPolicy'], 'Always')

    def test_small_temporary_storage_has_valid_requests_and_limits(self):
        cfg = ci.config(SimpleNamespace(site=REPO / 'config/site.yaml', namespace=None, offline=False))
        for size in [1, 2, 6, 20]:
            cfg['temporaryStorageGiB'] = size
            for kind in ['service', 'image', 'open5gs']:
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
