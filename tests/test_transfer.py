import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('transfer', ROOT / 'scripts/transfer.py')
transfer = importlib.util.module_from_spec(spec); spec.loader.exec_module(transfer)
spec = importlib.util.spec_from_file_location('ci_transfer', ROOT / 'scripts/ci.py')
ci = importlib.util.module_from_spec(spec); spec.loader.exec_module(ci)


class TransferTests(unittest.TestCase):
    def test_selected_archive_loads_only_upstream_images_and_rejects_wrong_id(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); source, target = root/'all.tar', root/'upstream.tar'
            config = b'{"architecture":"amd64","os":"linux"}'
            image_id = 'sha256:' + transfer.hashlib.sha256(config).hexdigest()
            manifests = [{'Config':'config.json','RepoTags':['upstream:test','custom/compiler:latest'],'Layers':['layer.tar']}]
            with tarfile.open(source,'w') as archive:
                for name,data in {'config.json':config,'layer.tar':b'layer','manifest.json':json.dumps(manifests).encode()}.items():
                    item=tarfile.TarInfo(name);item.size=len(data);archive.addfile(item,io.BytesIO(data))
            transfer.select_images(source,target,{'upstream:test':image_id})
            with tarfile.open(target) as archive:
                self.assertEqual(json.load(archive.extractfile('manifest.json'))[0]['RepoTags'],['upstream:test'])
                self.assertEqual(archive.extractfile('layer.tar').read(),b'layer')
            with self.assertRaisesRegex(RuntimeError,'锁文件不符'):
                transfer.select_images(source,root/'bad.tar',{'upstream:test':'sha256:wrong'})
            self.assertFalse((root/'bad.tar').exists())

    def test_manifest_detects_changed_files_and_directory_escape(self):
        with tempfile.TemporaryDirectory() as name, patch.object(transfer,'ROOT',Path(name)):
            root=Path(name);(root/'.cache').mkdir();p=root/'.cache/sdk';p.write_bytes(b'cached')
            files=transfer.files_under(['.cache/sdk'])
            manifest=root/'manifest.json'
            manifest.write_text(json.dumps({'format':1,'kind':'offline-dependencies',
                'components':[{'package':'compiler.tar.gz','files':files}]}))
            self.assertEqual(transfer.check(manifest,root),[])
            p.write_bytes(b'broken')
            self.assertEqual(transfer.check(manifest,root),['compiler.tar.gz'])
            data=json.loads(manifest.read_text());data['components'][0]['files']={'../escape':{}}
            manifest.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError,'越界'):transfer.check(manifest,root)

    def test_deterministic_dependency_archive_preserves_executable_mode(self):
        with tempfile.TemporaryDirectory() as name, patch.object(transfer,'ROOT',Path(name)):
            root=Path(name);p=root/'helm';p.write_bytes(b'tool');p.chmod(0o755)
            files=transfer.files_under(['helm'])
            transfer.write_tar(root/'a.tar.gz',files);transfer.write_tar(root/'b.tar.gz',files)
            self.assertEqual(transfer.sha(root/'a.tar.gz'),transfer.sha(root/'b.tar.gz'))
            with tarfile.open(root/'a.tar.gz') as archive:self.assertEqual(archive.getmember('helm').mode,0o755)

    def test_code_full_and_incremental_never_include_sdk_archives_or_local_config(self):
        with tempfile.TemporaryDirectory() as name, patch.object(transfer,'ROOT',Path(name)):
            root=Path(name);(root/'docs').mkdir();(root/'docs/transfer.md').write_text('instructions')
            (root/'vendor/compile').mkdir(parents=True);(root/'vendor/compile/sdk.part').write_bytes(b'large')
            (root/'app.py').write_text('old');(root/'.gitignore').write_text('site.local.yaml\n')
            (root/'config').mkdir();(root/'config/site.local.yaml').write_text('private-old')
            def git(*args):return subprocess.check_output(['git',*args],cwd=root,text=True).strip()
            git('init','-q');git('add','.')
            git('add','-f','config/site.local.yaml')
            def commit():git('-c','user.name=test','-c','user.email=test@example.invalid','commit','-qm','test')
            commit();base=git('rev-parse','HEAD')
            (root/'app.py').write_text('new');(root/'config/site.local.yaml').write_text('private-new')
            git('add','.');commit()
            (root/'site.local.yaml').write_text('private')
            transfer.code('')
            folder=root/'dist/transfer/02-code'
            with tarfile.open(folder/'code.tar.gz') as archive:
                self.assertNotIn('vendor/compile/sdk.part',archive.getnames())
                self.assertNotIn('site.local.yaml',archive.getnames())
                self.assertNotIn('config/site.local.yaml',archive.getnames())
                self.assertEqual(archive.extractfile('app.py').read(),b'new')
            transfer.code(base)
            self.assertFalse((folder/'code.tar.gz').exists())
            self.assertIn('app.py',(folder/'code.patch').read_text())
            self.assertNotIn('sdk.part',(folder/'code.patch').read_text())
            self.assertNotIn('private-',(folder/'code.patch').read_text())

    def test_production_images_do_not_clone_business_repositories_or_build_demo(self):
        cfg={'demo':False,'builderImage':'example/sdk:latest','controllerImage':'example/controller:1'}
        versions=dict(ci.versions(), CONTROLLER_IMAGE='example/controller:1')
        with tempfile.TemporaryDirectory() as name, patch.object(ci,'ROOT',Path(name)), \
             patch.object(ci,'CACHE',Path(name)/'.cache'),patch.object(ci,'config',return_value=cfg), \
             patch.object(ci,'versions',return_value=versions), \
             patch.object(ci,'compiler_module') as compiler,patch.object(ci,'lock_plugins'), \
             patch.object(ci,'prepare_xcn_bases'),patch.object(ci,'runtime_images',return_value=[]),patch.object(ci,'run') as run:
            (Path(name)/'config').mkdir()
            ci.images(SimpleNamespace(jobs=4,apt_sources='internal',offline=True,build_network='none',
                dockerfile='docker/builder-compat.Dockerfile'))
            self.assertTrue(compiler.return_value.build.called)
            commands=[list(map(str,c.args[0])) for c in run.call_args_list]
            self.assertFalse(any(c[0]=='git' for c in commands))
            self.assertFalse(any('docker/demo.Dockerfile' in x for c in commands for x in c))


if __name__=='__main__':unittest.main()
