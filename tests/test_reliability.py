import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest
import tempfile
from types import SimpleNamespace

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('reliability', ROOT / 'tests/reliability.py')
checks = importlib.util.module_from_spec(spec); spec.loader.exec_module(checks)
spec = importlib.util.spec_from_file_location('apt_mirror', ROOT / 'tests/apt_mirror.py')
mirror = importlib.util.module_from_spec(spec); spec.loader.exec_module(mirror)
spec = importlib.util.spec_from_file_location('reliability_setup', ROOT / 'tests/reliability_setup.py')
setup = importlib.util.module_from_spec(spec); spec.loader.exec_module(setup)
spec = importlib.util.spec_from_file_location('verify_compiler', ROOT / 'tests/verify-compiler.py')
compiler = importlib.util.module_from_spec(spec); spec.loader.exec_module(compiler)


class ReliabilityTests(unittest.TestCase):
    def test_resume_accepts_structured_fault_case_lists(self):
        cases=[{'case':'builder-memory-limit','result':'FAILURE'}]
        fake=SimpleNamespace(args=SimpleNamespace(resume=True),admin=SimpleNamespace(crumb='old'),
                             clients={},guard=lambda:None,
                             report={'phases':{'faults-restart':{'status':'PASS','details':cases}}})
        result=checks.Suite.phase(fake,'faults-restart',lambda:self.fail('已通过的故障阶段不应重复执行'))
        self.assertEqual(result,cases)
        self.assertIsNone(fake.admin.crumb)

    def test_pull_policy_compares_digests_across_registry_aliases(self):
        first='registry-a/sdk@sha256:'+'1'*64
        alias='registry-b/compat@sha256:'+'1'*64
        updated='registry-a/sdk@sha256:'+'2'*64
        self.assertEqual(compiler.image_digest(first),compiler.image_digest(alias))
        self.assertNotEqual(compiler.image_digest(first),compiler.image_digest(updated))
        self.assertEqual(compiler.image_digest('docker://sha256:'+'1'*64),compiler.image_digest(first))
        for invalid in ('registry-a/sdk:latest','sha256:1234'):
            with self.assertRaises(ValueError):compiler.image_digest(invalid)

    def test_preparation_isolates_git_and_never_inherits_production_credentials(self):
        base=yaml.safe_load((ROOT/'config/site.intranet.yaml').read_text())
        base.update(jenkinsUrl='http://192.168.10.10:32080/',caSecret='production-ca',
                    compilerImagePullSecrets=['production-registry'],proxy='http://production-proxy:8080')
        result=setup.verification_config(base,32180)
        self.assertEqual(result['jenkinsUrl'],'http://192.168.10.10:32180/')
        self.assertEqual(checks.validate_site(result),'jenkins-reliability-verify')
        self.assertEqual(result['xcn']['gitUrl'],'http://xran-demo:8000/xcn.git')
        self.assertEqual(result['xcn']['gitCredentialsId'],'')
        self.assertEqual(result['registryCredentialsId'],'')
        self.assertEqual(result['compilerImagePullSecrets'],[])
        self.assertNotIn('caSecret',result)
        self.assertEqual(result['proxy'],'')
        self.assertEqual(base['gitCredentialsId'],'intranet-gerrit-ssh')
        self.assertEqual(base['xcn']['gitCredentialsId'],'intranet-gerrit-ssh')

    def test_production_or_mismatched_sites_are_rejected_before_network_access(self):
        cfg = yaml.safe_load((ROOT / 'config/site.yaml').read_text())
        with self.assertRaisesRegex(RuntimeError, '独立验收实例'): checks.validate_site(cfg)
        cfg.update(namespace='jenkins-reliability-verify', verificationInstance=True, nodePort=32180,
                   jenkinsUrl='http://127.0.0.1:32180/')
        self.assertEqual(checks.validate_site(cfg), cfg['namespace'])
        for change in ({'namespace':'jenkins'}, {'verificationInstance':False}, {'nodePort':32080},
                       {'registry':'192.168.10.35'}, {'gitCredentialsId':'production-key'}):
            with self.subTest(change=change), self.assertRaises(RuntimeError): checks.validate_site(dict(cfg, **change))

    def test_incomplete_stability_evidence_cannot_pass(self):
        valid = dict(elapsedSeconds=1800, completed=30, kinds={'service':10,'image':10,'xcn':10},
                     maximumLeases=6, maximumRunningPods=6, maximumWaiting=2, unexpectedFailures=0)
        checks.validate_soak(valid, 30)
        for change in ({'elapsedSeconds':1799}, {'completed':29}, {'kinds':{'service':20,'image':10}},
                       {'maximumLeases':5}, {'maximumLeases':7}, {'maximumRunningPods':5},
                       {'maximumWaiting':1}, {'unexpectedFailures':1}):
            with self.subTest(change=change), self.assertRaises(RuntimeError): checks.validate_soak(dict(valid, **change),30)

    def test_resumed_soak_requires_original_samples_and_requested_duration(self):
        with tempfile.TemporaryDirectory() as name:
            p=Path(name)/'samples.jsonl'
            start=dt.datetime.now(dt.timezone.utc)
            p.write_text('\n'.join(json.dumps({'at':(start+dt.timedelta(seconds=s)).isoformat(),
                         'leases':list(range(6)),'running':list(range(8)),
                         'pods':[{'phase':'Running'} for _ in range(6)]}) for s in (0,1800))+'\n')
            summary=dict(elapsedSeconds=1800,completed=30,kinds={'service':10,'image':10,'xcn':10},
                         maximumLeases=6,maximumRunningPods=6,maximumWaiting=2,unexpectedFailures=0,
                         samples=str(p),samplesSha256=checks.digest(p),
                         artifacts=[{'kind':kind} for kind in ('service','image','xcn') for _ in range(10)])
            checks.validate_soak_evidence(summary,30)
            with self.assertRaisesRegex(RuntimeError,'摘要与原始采样'):
                checks.validate_soak_evidence(dict(summary,maximumWaiting=3),30)
            with self.assertRaisesRegex(RuntimeError,'持续时间不足'): checks.validate_soak_evidence(summary,31)
            p.write_text('tampered\n')
            with self.assertRaisesRegex(RuntimeError,'采样丢失或校验'): checks.validate_soak_evidence(summary,30)

    def test_resource_comparison_accepts_canonical_kubernetes_units(self):
        left={'builder':{'limits':{'cpu':'1000m','memory':'1024Mi','ephemeral-storage':'2048Mi'}}}
        right={'builder':{'limits':{'cpu':'1','memory':'1Gi','ephemeral-storage':'2Gi'}}}
        self.assertTrue(checks.equal_resources(left,right))
        right['builder']['limits']['memory']='2Gi'
        self.assertFalse(checks.equal_resources(left,right))
        with self.assertRaises(RuntimeError): checks.quantity('invalid')

    def test_test_mirror_cannot_upload_proxy_or_forward_arbitrary_paths(self):
        for path in ('/ubuntu/dists/jammy/InRelease', '/ubuntu/dists/jammy-updates/main/binary-amd64/by-hash/SHA256/abcdef',
                     '/ubuntu/pool/main/z/zlib/zlib1g_1.2.11-1_amd64.deb',
                     '/ubuntu/pool/main/g/gcc/g%2b%2b_4%3a11.2.0%7e22.04_amd64.deb'):
            self.assertTrue(mirror.upstream_path(path).startswith('https://archive.ubuntu.com/ubuntu/'))
        for path in ('https://example.com/source', '/ubuntu/dists/jammy/InRelease?source=secret', '/etc/passwd',
                     '/ubuntu/../secret', '/ubuntu/dists/jammy/%2e%2e/secret', '/ubuntu/pool/main/source.tar.gz'):
            with self.subTest(path=path), self.assertRaises(ValueError): mirror.upstream_path(path)
        self.assertFalse(hasattr(mirror.Mirror, 'do_POST'))
        self.assertFalse(hasattr(mirror.Mirror, 'do_CONNECT'))

    def test_offline_storage_manifest_matches_fixed_image_inputs(self):
        env=dict(line.split('=',1) for line in (ROOT/'config/versions.env').read_text().splitlines() if line and not line.startswith('#'))
        documents=list(yaml.safe_load_all((ROOT/'vendor/local-path-storage.v0.0.37.yaml').read_text()))
        deployment=next(x for x in documents if x['kind']=='Deployment')
        helper=next(x for x in documents if x['kind']=='ConfigMap')['data']['helperPod.yaml']
        helper=yaml.safe_load(helper)['spec']['containers'][0]['image']
        self.assertEqual(deployment['spec']['template']['spec']['containers'][0]['image'], 'docker.io/'+env['LOCAL_PATH_IMAGE'])
        self.assertEqual(helper, 'docker.io/library/'+env['STORAGE_HELPER_IMAGE'])

    def test_mirror_rejects_corrupted_by_hash_index(self):
        with tempfile.TemporaryDirectory() as name:
            p=Path(name)/'index';p.write_bytes(b'valid index')
            url='https://archive.ubuntu.com/ubuntu/dists/jammy/main/binary-amd64/by-hash/SHA256/'+hashlib.sha256(p.read_bytes()).hexdigest()
            self.assertTrue(mirror.valid_index(p,url))
            p.write_bytes(b'stale proxy response')
            self.assertFalse(mirror.valid_index(p,url))


if __name__=='__main__': unittest.main()
