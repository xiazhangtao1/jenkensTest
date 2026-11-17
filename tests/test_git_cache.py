"""Real Git smart-HTTP traffic, isolated object stores and cross-process locks."""
from concurrent.futures import ThreadPoolExecutor
import contextlib
import fcntl
import gzip
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / 'runtime/git_checkout.py'


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], text=True, stderr=subprocess.DEVNULL).strip()


class GitCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.temporary.name)
        cls.fixture = cls.directory / 'fixture'
        cls.fixture.mkdir()
        git(cls.fixture, 'init', '-b', 'main')
        git(cls.fixture, 'config', 'user.name', 'Cache fixture')
        git(cls.fixture, 'config', 'user.email', 'cache@example.invalid')
        git(cls.fixture, 'config', 'commit.gpgsign', 'false')
        (cls.fixture / 'payload').write_bytes(os.urandom(256 * 1024))
        git(cls.fixture, 'add', '.')
        git(cls.fixture, 'commit', '-m', 'first')
        cls.old = git(cls.fixture, 'rev-parse', 'HEAD')
        git(cls.fixture, 'tag', 'release-v1')
        git(cls.fixture, 'tag', '-a', 'annotated-v1', '-m', 'annotated')
        git(cls.fixture, 'checkout', '-b', 'feature/test')
        (cls.fixture / 'version').write_text('feature')
        git(cls.fixture, 'add', '.')
        git(cls.fixture, 'commit', '-m', 'feature')
        cls.feature = git(cls.fixture, 'rev-parse', 'HEAD')
        git(cls.fixture, 'checkout', 'main')
        (cls.fixture / 'version').write_text('main')
        git(cls.fixture, 'add', '.')
        git(cls.fixture, 'commit', '-m', 'main')
        cls.head = git(cls.fixture, 'rev-parse', 'HEAD')
        git(cls.fixture, 'update-ref', 'refs/changes/01/1/1', cls.feature)
        cls.traffic = []
        cls.traffic_lock = threading.Lock()
        cls.blocks = {}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def handle_git(self):
                parsed = urlsplit(self.path)
                block = cls.blocks.get(parsed.path.split('/')[1])
                if block:
                    block[0].set(); block[1].wait(20)
                content = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                if self.headers.get('Content-Encoding') == 'gzip':
                    content = gzip.decompress(content)
                env = dict(os.environ, GIT_PROJECT_ROOT=str(cls.directory), GIT_HTTP_EXPORT_ALL='1',
                           PATH_INFO=parsed.path, QUERY_STRING=parsed.query,
                           REQUEST_METHOD=self.command, CONTENT_TYPE=self.headers.get('Content-Type', ''),
                           CONTENT_LENGTH=str(len(content)), HTTP_GIT_PROTOCOL=self.headers.get('Git-Protocol', ''))
                result = subprocess.run(['git', 'http-backend'], input=content, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, env=env, check=True)
                header, _, body = result.stdout.partition(b'\r\n\r\n')
                headers = [line.split(':', 1) for line in header.decode().splitlines() if ':' in line]
                status = next((int(value.strip().split()[0]) for name, value in headers if name.lower() == 'status'), 200)
                self.send_response(status)
                for name, value in headers:
                    if name.lower() != 'status':
                        self.send_header(name, value.strip())
                self.end_headers()
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    self.wfile.write(body)
                if parsed.path.endswith('/git-upload-pack'):
                    with cls.traffic_lock:
                        cls.traffic.append(len(body))

            do_GET = handle_git
            do_POST = handle_git

        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join()
        cls.temporary.cleanup()

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=self.directory))
        self.remote = self.directory / (self.root.name + '.git')
        subprocess.run(['git', 'clone', '--mirror', '--no-hardlinks', str(self.fixture), str(self.remote)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.url = f'http://127.0.0.1:{self.server.server_port}/{self.remote.name}'
        self.caches = self.root / 'cache'; self.caches.mkdir()
        for project in ('x-ran-cloud', 'xcn', 'xtue-cloud'):
            (self.caches / project).mkdir(mode=0o2775)

    def environment(self, project='x-ran-cloud', kind='branch', ref='main', **extra):
        return dict(os.environ, XRAN_GIT_URL=self.url, XRAN_REF_TYPE=kind, XRAN_REF=ref,
                    XRAN_PROJECT_ID=project, XRAN_GIT_CACHE_ENABLED='true',
                    XRAN_GIT_CACHE_ROOT=str(self.caches / project),
                    XRAN_GIT_MINIMUM_FREE_GIB='0', XRAN_GIT_MINIMUM_FREE_PERCENT='0',
                    XRAN_GIT_SPACE_REQUIREMENTS='{}', NO_PROXY='localhost,127.0.0.1',
                    no_proxy='localhost,127.0.0.1', **extra)

    def checkout(self, name='build', project='x-ran-cloud', kind='branch', ref='main', enabled=True):
        work = self.root / name; work.mkdir(exist_ok=True)
        env = self.environment(project, kind, ref)
        env['XRAN_GIT_CACHE_ENABLED'] = str(enabled).lower()
        result = subprocess.run(['python3', str(DRIVER)], cwd=work, env=env,
                                capture_output=True, text=True, timeout=40)
        if result.returncode:
            error = subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)
            error.add_note(result.stdout + '\n' + result.stderr) if hasattr(error, 'add_note') else None
            raise error
        return work, json.loads((work / 'checkout.json').read_text())

    def cache(self, metadata, project='x-ran-cloud'):
        return self.caches / project / (metadata['gitCache']['key'] + '.git')

    def test_cold_hot_incremental_transfer_and_independent_history(self):
        begin = len(self.traffic)
        first, cold = self.checkout()
        cold_bytes = sum(self.traffic[begin:])
        begin = len(self.traffic)
        second, warm = self.checkout('warm')
        warm_bytes = sum(self.traffic[begin:])
        self.assertFalse(cold['gitCache']['hit']); self.assertTrue(warm['gitCache']['hit'])
        self.assertEqual(warm['commit'], self.head)
        self.assertGreater(cold_bytes, 256 * 1024)
        self.assertLess(warm_bytes, cold_bytes // 10)
        (first/'source/version').write_text('new commit')
        git(first/'source','-c','user.name=Fixture','-c','user.email=fixture@example.invalid',
            '-c','commit.gpgsign=false','commit','-am','new remote commit')
        updated = git(first/'source','rev-parse','HEAD')
        git(first/'source','push',str(self.remote),'HEAD:refs/heads/main')
        begin = len(self.traffic)
        delta_work, delta = self.checkout('delta')
        delta_bytes = sum(self.traffic[begin:])
        self.assertEqual(delta['commit'],updated)
        self.assertTrue(delta['gitCache']['hit'])
        self.assertLess(delta_bytes,cold_bytes//10)
        cache = self.cache(warm)
        self.assertEqual(cache.stat().st_mode & 0o7777, 0o2775)
        self.assertFalse((second / 'source/.git/objects/info/alternates').exists())
        refs = git(cache, 'for-each-ref', '--format=%(refname)').splitlines()
        self.assertNotIn('refs/changes/01/1/1', refs)
        common = next(p for p in (cache / 'objects').rglob('*') if p.is_file() and (second/'source/.git/objects'/p.relative_to(cache/'objects')).is_file())
        self.assertNotEqual(common.stat().st_ino, (second/'source/.git/objects'/common.relative_to(cache/'objects')).stat().st_ino)
        shutil.rmtree(cache)
        git(second / 'source', 'fsck', '--full')
        self.assertEqual(git(second / 'source', 'rev-parse', 'annotated-v1^{commit}'), self.old)
        self.assertEqual(git(second / 'source', 'remote', 'get-url', 'origin'), self.url)
        print(json.dumps({'coldBytes': cold_bytes, 'warmBytes': warm_bytes, 'deltaBytes': delta_bytes, 'timings': warm['gitCache']['timings']}))

    def test_branch_tags_sha_force_updates_and_prune(self):
        for kind, ref, sha in [('branch','feature/test',self.feature),('tag','release-v1',self.old),
                              ('tag','annotated-v1',self.old),('commit',self.old,self.old),
                              ('commit',self.feature[:9],self.feature)]:
            with self.subTest(kind=kind, ref=ref):
                work, metadata = self.checkout(kind=kind, ref=ref)
                self.assertEqual(metadata['commit'], sha)
        git(self.remote, 'update-ref', 'refs/heads/main', self.old)
        git(self.remote, 'update-ref', 'refs/tags/release-v1', self.head)
        work, meta = self.checkout()
        self.assertEqual(meta['commit'], self.old)
        self.assertEqual(self.checkout(kind='tag', ref='release-v1')[1]['commit'], self.head)
        git(self.remote, 'update-ref', '-d', 'refs/tags/release-v1')
        with self.assertRaises(subprocess.CalledProcessError):
            self.checkout(kind='tag', ref='release-v1')
        self.assertFalse((work / 'checkout.json').exists())

    def test_review_sha_is_copied_by_local_transport_without_review_refs(self):
        tree = git(self.remote, 'rev-parse', self.head + '^{tree}')
        sha = git(self.remote, '-c', 'user.name=Fixture', '-c', 'user.email=f@example.invalid',
                  'commit-tree', tree, '-p', self.head, '-m', 'review only')
        git(self.remote, 'update-ref', 'refs/changes/02/2/1', sha)
        work = self.root / 'review'; work.mkdir()
        # Exercise the same local transport selected for a foreign-owned cache.
        script = ("import importlib.util;from pathlib import Path;"
                  f"s=importlib.util.spec_from_file_location('checkout_driver',{str(DRIVER)!r});"
                  "d=importlib.util.module_from_spec(s);s.loader.exec_module(d);"
                  "d.os.geteuid=lambda:999999;d.checkout(Path.cwd())")
        result = subprocess.run(['python3', '-c', script], cwd=work,
                                env=self.environment(kind='commit', ref=sha), capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        metadata = json.loads((work / 'checkout.json').read_text())
        self.assertEqual(metadata['commit'], sha)
        self.assertNotIn('refs/changes/', git(self.cache(metadata), 'for-each-ref', '--format=%(refname)'))
        shutil.rmtree(self.cache(metadata))
        self.assertEqual(git(work / 'source', 'rev-parse', 'HEAD'), sha)
        git(work / 'source', '-c', 'safe.directory=' + str(work / 'source'), 'fsck', '--full')

    def test_interrupted_git_locks_are_recovered_under_project_lock(self):
        _, metadata = self.checkout()
        cache = self.cache(metadata)
        locks = [cache / name for name in ('config.lock', 'packed-refs.lock', 'FETCH_HEAD.lock', 'refs/heads/main.lock')]
        for path in locks:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('interrupted writer')
        _, recovered = self.checkout()
        self.assertTrue(recovered['gitCache']['hit'])
        self.assertEqual(recovered['commit'], self.head)
        self.assertTrue(all(not path.exists() for path in locks))

    def test_corrupt_blob_is_refetched_once_before_compilation(self):
        _, metadata = self.checkout()
        cache = self.cache(metadata)
        sha = git(cache, 'rev-parse', self.head + ':payload')
        blob = cache / 'objects' / sha[:2] / sha[2:]
        blob.chmod(0o664); blob.write_bytes(b'corrupt cached blob')
        work, recovered = self.checkout()
        self.assertFalse(recovered['gitCache']['hit'])
        self.assertTrue(recovered['gitCache']['checkoutRetried'])
        self.assertEqual(recovered['commit'], self.head)
        self.assertEqual((work / 'source/payload').read_bytes(), (self.fixture / 'payload').read_bytes())
        git(work / 'source', 'fsck', '--full')

    @unittest.skipUnless(os.geteuid() == 0, '需要在编译镜像的 root 测试容器内验证真实 UID 切换')
    def test_actual_root_and_uid1000_can_create_and_reuse_each_others_cache(self):
        self.directory.chmod(0o755)
        self.root.chmod(0o2775); os.chown(self.root, 1000, 1000)
        for path in [self.caches, *self.caches.iterdir()]:
            os.chown(path, 1000, 1000); path.chmod(0o2775)
        root_work, metadata = self.checkout('root-first')
        cache = self.cache(metadata)
        self.assertEqual(cache.stat().st_uid, 0)
        work = self.root / 'uid1000'; work.mkdir(); os.chown(work, 1000, 1000)
        def normal():
            result = subprocess.run(['python3', str(DRIVER)], cwd=work, env=self.environment(),
                capture_output=True, text=True, timeout=40, user=1000, group=1000, extra_groups=[1000])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads((work / 'checkout.json').read_text())
        self.assertTrue(normal()['gitCache']['hit'])
        git(work / 'source', '-c', 'safe.directory=' + str(work / 'source'), 'fsck', '--full')
        shutil.rmtree(cache)
        self.assertFalse(normal()['gitCache']['hit'])
        self.assertEqual(cache.stat().st_uid, 1000)
        self.assertTrue(self.checkout('root-again')[1]['gitCache']['hit'])

    def test_six_concurrent_builds_and_project_isolation(self):
        projects = ('x-ran-cloud', 'xcn', 'xtue-cloud') * 2
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda item:self.checkout('build-'+str(item[0]),item[1]), enumerate(projects)))
        self.assertEqual(sum(not meta['gitCache']['hit'] for _, meta in results), 3)
        self.assertTrue(all(meta['commit']==self.head for _,meta in results))
        self.assertEqual(len({meta['gitCache']['key'] for _,meta in results}),3)
        for work, _ in results:
            (work / 'source/version').write_text(work.name)
        for work, _ in results:
            self.assertEqual((work / 'source/version').read_text(),work.name)

    def test_remote_failure_never_builds_stale_main_and_disabled_mode_works(self):
        work, meta = self.checkout()
        moved = self.remote.with_suffix('.unavailable')
        self.remote.rename(moved)
        try:
            with self.assertRaises(subprocess.CalledProcessError): self.checkout()
            self.assertFalse((work/'checkout.json').exists())
        finally: moved.rename(self.remote)
        work, direct = self.checkout(enabled=False)
        self.assertFalse(direct['gitCache']['enabled'])
        self.assertEqual(direct['commit'],self.head)

    def test_corrupt_cache_rebuilt_and_waiting_checkout_can_be_cancelled(self):
        work, meta = self.checkout()
        cache = self.cache(meta)
        (cache/'HEAD').write_text('broken\n')
        work, rebuilt = self.checkout()
        self.assertFalse(rebuilt['gitCache']['hit'])
        self.assertEqual(rebuilt['commit'],self.head)
        lock = self.caches/'x-ran-cloud/.lock'
        with lock.open('r+') as held:
            fcntl.flock(held,fcntl.LOCK_EX)
            process = subprocess.Popen(['python3',str(DRIVER)],cwd=work,env=self.environment(),
                                       stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            time.sleep(0.3)
            self.assertIsNone(process.poll())
            process.terminate(); process.communicate(timeout=10)
            self.assertNotEqual(process.returncode,0)
        self.assertEqual(self.checkout()[1]['commit'],self.head)

    def test_ambiguous_short_commit_is_rejected(self):
        tree=git(self.remote,'rev-parse',self.head+'^{tree}')
        seen={};pair=None
        for number in range(200000):
            content=f'tree {tree}\nauthor Fixture <f@example.invalid> 1 +0000\ncommitter Fixture <f@example.invalid> 1 +0000\n\ncollision {number}\n'.encode()
            sha=hashlib.sha1(b'commit '+str(len(content)).encode()+b'\0'+content).hexdigest()
            prefix=sha[:7]
            if prefix in seen:
                pair=(seen[prefix],content);break
            seen[prefix]=content
        self.assertIsNotNone(pair)
        for number,content in enumerate(pair):
            sha=subprocess.check_output(['git','-C',str(self.remote),'hash-object','-t','commit','-w','--stdin'],input=content).decode().strip()
            git(self.remote,'update-ref',f'refs/heads/collision-{number}',sha)
        with self.assertRaises(subprocess.CalledProcessError):self.checkout(kind='commit',ref=prefix)
        self.assertFalse((self.root/'build/checkout.json').exists())

    def test_relative_submodule_uses_real_origin_after_local_copy(self):
        module=self.root/'module';module.mkdir();git(module,'init','-b','main')
        (module/'module.txt').write_text('module data');git(module,'add','.')
        git(module,'-c','user.name=Fixture','-c','user.email=f@example.invalid','-c','commit.gpgsign=false','commit','-m','module')
        module_sha=git(module,'rev-parse','HEAD')
        bare=self.directory/(self.root.name+'-module.git')
        subprocess.run(['git','clone','--bare',str(module),str(bare)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        work=self.root/'edit'
        subprocess.run(['git','clone',str(self.remote),str(work)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        (work/'.gitmodules').write_text(f'[submodule "deps/module"]\n path = deps/module\n url = ../{bare.name}\n')
        git(work,'add','.gitmodules');git(work,'update-index','--add','--cacheinfo',f'160000,{module_sha},deps/module')
        git(work,'-c','user.name=Fixture','-c','user.email=f@example.invalid','-c','commit.gpgsign=false','commit','-m','submodule')
        git(work,'push',str(self.remote),'HEAD:refs/heads/main')
        for name in ('first','warm'):
            target,meta=self.checkout(name)
            self.assertEqual((target/'source/deps/module/module.txt').read_text(),'module data')
            self.assertEqual(git(target/'source/deps/module','rev-parse','HEAD'),module_sha)

    def test_cancel_active_fetch_releases_writer_lock_and_disk_guard_is_explicit(self):
        work=self.root/'active';work.mkdir()
        entered,release=threading.Event(),threading.Event()
        self.blocks[self.remote.name]=(entered,release)
        process=subprocess.Popen(['python3',str(DRIVER)],cwd=work,env=self.environment(),stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            self.assertTrue(entered.wait(10))
            process.terminate();process.communicate(timeout=10)
            self.assertNotEqual(process.returncode,0)
            with (self.caches/'x-ran-cloud/.lock').open('r+') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        finally:
            release.set();self.blocks.pop(self.remote.name,None)
            if process.poll() is None:process.kill();process.communicate(timeout=5)
        self.assertEqual(self.checkout()[1]['commit'],self.head)
        env=self.environment();env['XRAN_GIT_MINIMUM_FREE_GIB']='1048576'
        failed=subprocess.run(['python3',str(DRIVER)],cwd=work,env=env,capture_output=True,text=True,timeout=20)
        self.assertNotEqual(failed.returncode,0)
        self.assertIn('磁盘空间不足',failed.stderr)
        self.assertFalse((work/'checkout.json').exists())


if __name__ == '__main__': unittest.main()
