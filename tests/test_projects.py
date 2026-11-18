import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('project_runner', ROOT / 'runtime/run-build.py')
runner = importlib.util.module_from_spec(spec); spec.loader.exec_module(runner)
from projects import load_registry
import yaml


class ProjectTests(unittest.TestCase):
    def test_registry_legacy_connections_overrides_and_new_project(self):
        site = yaml.safe_load((ROOT/'config/site.intranet.yaml').read_text())
        projects = load_registry(site, ROOT/'config/projects.yaml')
        self.assertEqual(projects['xcn']['displayName'], 'X-CN-CLOUD')
        self.assertEqual(projects['xtue-cloud']['displayName'], 'X-TUE-CLOUD')
        self.assertEqual(projects['x-ran-cloud']['gitUrl'], site['gitUrl'])
        self.assertEqual(projects['xcn']['gitUrl'], site['xcn']['gitUrl'])
        self.assertEqual(list(projects['xtue-cloud']['targets']), ['nrUE','tuePhy'])
        site['projects'] = {'xtue-cloud': {'gitUrl':'ssh://different@host/repo','gitCredentialsId':'chosen'}}
        projects = load_registry(site, ROOT/'config/projects.yaml')
        self.assertEqual(projects['xtue-cloud']['gitCredentialsId'],'chosen')
        with tempfile.TemporaryDirectory() as folder:
            config=yaml.safe_load((ROOT/'config/projects.yaml').read_text())
            config['projects']['demo-c']=dict(config['projects']['xtue-cloud'], displayName='DemoC')
            path=Path(folder)/'projects.yaml';path.write_text(yaml.safe_dump(config))
            self.assertIn('demo-c',load_registry(site,path))
            config['projects']['demo-c']['targets']['nrUE']['workdir']='../outside'
            path.write_text(yaml.safe_dump(config))
            with self.assertRaises(ValueError): load_registry(site,path)

    def test_configured_commands_and_artifacts_both_targets_and_failures(self):
        site=yaml.safe_load((ROOT/'config/site.intranet.yaml').read_text())
        project=load_registry(site,ROOT/'config/projects.yaml')['xtue-cloud']
        project['env'] = {'XTUE_TEST_ENV': 'project', 'XTUE_SHARED_ENV': 'shared'}
        project['targets']['nrUE']['env'] = {'XTUE_TEST_ENV': 'target'}
        with tempfile.TemporaryDirectory() as folder:
            work=Path(folder);source=work/'source';(source/'cmake_targets').mkdir(parents=True)
            script=source/'cmake_targets/build_oai'
            script.write_text('''#!/usr/bin/env python3
import io,tarfile,sys,os
assert sys.argv[1]=='-k'
assert os.environ['XTUE_SHARED_ENV']=='shared'
assert os.environ['XTUE_TEST_ENV']==('target' if sys.argv[2]=='--nrUE' else 'project')
name={'--nrUE':'tue.tar.gz','--tuePhy':'tue_simuphy.tar.gz'}[sys.argv[2]]
with tarfile.open(name,'w:gz') as archive:
 data=b'fixture only'; member=tarfile.TarInfo('app.txt');member.size=len(data);archive.addfile(member,io.BytesIO(data))
''');script.chmod(0o755)
            (work/'checkout.json').write_text(json.dumps({'commit':'a'*40}))
            with patch.object(runner,'ROOT',work),patch.object(runner,'SOURCE',source),patch.object(runner,'PUBLISH',work/'publish'),patch.dict(os.environ,
                 XRAN_PROJECT_SPEC=json.dumps(project),XRAN_KIND='service',XRAN_BUILD_METADATA=json.dumps({'result':'SUCCESS'})):
                for target,name in [('nrUE','tue.tar.gz'),('tuePhy','tue_simuphy.tar.gz')]:
                    os.environ['XRAN_TARGET']=target
                    runner.build(); self.assertEqual(runner.collect(),0)
                    self.assertTrue((work/'publish'/name).is_file())
                    meta=json.loads((work/'publish/metadata.json').read_text())
                    self.assertEqual([x['name'] for x in meta['files']],[name])
                # A successful script that produces no output must not publish an old archive.
                os.environ['XRAN_TARGET']='nrUE'
                output=source/'cmake_targets/tue.tar.gz';output.write_bytes(b'old archive')
                script.write_text('#!/bin/sh\nexit 0\n');runner.build()
                self.assertFalse(output.exists());self.assertEqual(runner.collect(),1)
                output.write_bytes(b'not a tar');self.assertEqual(runner.collect(),1)
                output.unlink();output.symlink_to(work/'checkout.json')
                with self.assertRaises(ValueError):runner.collect()
                output.unlink();script.write_text('#!/bin/sh\nexit 3\n')
                with self.assertRaises(subprocess.CalledProcessError):runner.build()
                project['targets']['nrUE']['workdir']='../../outside'
                os.environ['XRAN_PROJECT_SPEC']=json.dumps(project)
                with self.assertRaises(ValueError):runner.build()
