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
        for kind in ['service', 'image']:
            pod = ci.pod_template(cfg, kind)
            limits = [c['resources']['limits'] for c in pod['spec']['containers']]
            self.assertEqual(sum(memory(x['ephemeral-storage']) for x in limits), 20 * 1024)
            self.assertEqual(sum(memory(x['memory']) for x in limits), 8 * 1024)
            self.assertEqual(sum(int(x['cpu'].removesuffix('m')) for x in limits), 4000)
            self.assertFalse(pod['spec']['automountServiceAccountToken'])
            privileged = [c['name'] for c in pod['spec']['containers'] if c.get('securityContext', {}).get('privileged')]
            self.assertEqual(privileged, ['dind'] if kind == 'image' else [])

    def test_offline_profiles_never_pull(self):
        cfg = ci.config(SimpleNamespace(site=REPO / 'config/site.yaml', namespace='jenkins-offline-verify', offline=True))
        for kind in ['service', 'image']:
            self.assertTrue(all(c['imagePullPolicy'] == 'Never' for c in ci.pod_template(cfg, kind)['spec']['containers']))


if __name__ == '__main__':
    unittest.main()
