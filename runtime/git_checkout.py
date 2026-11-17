#!/usr/bin/env python3
"""Trusted Git checkout; the same file is shipped in images and by ConfigMap."""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time


class DiskPressure(RuntimeError):
    pass


def atomic_json(path, value):
    fd, temporary = tempfile.mkstemp(prefix='.state-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(value, output, ensure_ascii=False)
            output.write('\n')
        os.chmod(temporary, 0o664)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def directory_bytes(path):
    return sum(p.stat().st_size for p in path.rglob('*') if p.is_file() and not p.is_symlink()) if path.exists() else 0


def safe_directory(path):
    if path.is_symlink() or path.resolve() != path.absolute():
        raise ValueError('Git 缓存目录不能包含符号链接: ' + str(path))


class Git:
    def __init__(self, guard_paths=(), lock_fd=None):
        self.guard_paths = guard_paths
        self.lock_fd = lock_fd
        self.requirements = json.loads(os.environ.get('XRAN_GIT_SPACE_REQUIREMENTS', '{}'))

    def guard(self):
        for path in self.guard_paths:
            usage = shutil.disk_usage(path)
            required = max(int(os.environ.get('XRAN_GIT_MINIMUM_FREE_GIB', '0')) * 1073741824,
                           int(usage.total * float(os.environ.get('XRAN_GIT_MINIMUM_FREE_PERCENT', '0')) / 100),
                           int(self.requirements.get(str(os.stat(path).st_dev), 0)))
            if usage.free < required:
                raise DiskPressure(f'Git 所在磁盘空间不足: {path}; available={usage.free}, required={required}')

    def __call__(self, *args, cwd=None, capture=False, checked=True):
        self.guard()
        command = ['git', '-c', 'gc.auto=0', '-c', 'maintenance.auto=false',
                   '-c', 'pack.threads=2', '-c', 'pack.windowMemory=64m']
        if cwd:
            command += ['-c', 'safe.directory=' + str(Path(cwd).resolve())]
        command += list(map(str, args))
        # Inherit the flock descriptor: an orphaned Git writer must keep the lock.
        with tempfile.TemporaryFile(mode='w+') as output:
            process = subprocess.Popen(command, cwd=cwd, stdout=output if capture else None,
                                       start_new_session=True,
                                       pass_fds=() if self.lock_fd is None else (self.lock_fd,))
            try:
                next_guard = 0
                while process.poll() is None:
                    if time.monotonic() >= next_guard:
                        self.guard()
                        next_guard = time.monotonic() + 1
                    time.sleep(0.02)
            except BaseException:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                raise
            if checked and process.returncode:
                raise subprocess.CalledProcessError(process.returncode, command)
            output.seek(0)
            return output.read().strip() if capture else process.returncode


def validate(kind, ref, url):
    if not ref or ref.startswith('-') or any(c in ref for c in ('\0', '\n', '\r')):
        raise ValueError('非法 Git 引用')
    if not url.startswith(('https://', 'http://', 'ssh://', 'git@')) or any(c in url for c in ('\0', '\n', '\r')):
        raise ValueError('Git URL 仅支持 http(s)/ssh')
    if kind == 'commit':
        if not re.fullmatch(r'[0-9a-fA-F]{7,40}', ref):
            raise ValueError('commit 必须是 7–40 位十六进制 SHA')
    elif kind in ('branch', 'tag'):
        Git()('check-ref-format', ('refs/heads/' if kind == 'branch' else 'refs/tags/') + ref)
    else:
        raise ValueError('未知引用类型')


def resolve(git, directory, kind, ref, bare=False):
    if kind == 'branch':
        revision = ('refs/heads/' if bare else 'refs/remotes/origin/') + ref
    elif kind == 'tag':
        revision = 'refs/tags/' + ref
    else:
        revision = ref
        if len(ref) == 40 and git('cat-file', '-e', ref + '^{commit}', cwd=directory, checked=False):
            git('fetch', '--no-recurse-submodules', 'origin', ref, cwd=directory)
    return git('rev-parse', '--verify', '--end-of-options', revision + '^{commit}', cwd=directory, capture=True)


def update(git, directory, url):
    if not (directory / 'HEAD').exists():
        git('init', '--bare', '-b', 'main', directory)
        git('remote', 'add', 'origin', url, cwd=directory)
    # Use only heads/tags, never --mirror (which includes Gerrit review refs).
    git('config', 'remote.origin.url', url, cwd=directory)
    git('config', '--replace-all', 'remote.origin.fetch', '+refs/heads/*:refs/heads/*', cwd=directory)
    git('config', '--add', 'remote.origin.fetch', '+refs/tags/*:refs/tags/*', cwd=directory)
    git('fetch', '--atomic', '--no-recurse-submodules', '--prune', 'origin', cwd=directory)


def clean_interrupted_locks(directory):
    # The project flock excludes every cache writer, including orphaned Git
    # children. Git's own lock files can still survive SIGKILL or a node reboot.
    locks = [directory / name for name in ('HEAD.lock', 'config.lock', 'FETCH_HEAD.lock',
                                           'packed-refs.lock', 'shallow.lock', 'ORIG_HEAD.lock')]
    locks.extend((directory / 'refs').rglob('*.lock'))
    for path in locks:
        safe_directory(path.parent)
        if path.is_file() or path.is_symlink():
            path.unlink()
            print('清理中断遗留的 Git 锁: ' + str(path.relative_to(directory)), flush=True)


def copy_repository(git, cache, source, url, sha):
    # Modern Git rejects local optimization across owners. Use the local Git
    # transport in that case; both paths create a self-contained object store.
    # The upload-pack child sanitizes the parent's command-line configuration;
    # pass the one trusted cache path explicitly to that child as well.
    upload_pack = '--upload-pack=' + shlex.join(['git', '-c', 'safe.directory=' + str(cache),
        '-c', 'pack.threads=2', '-c', 'pack.windowMemory=64m', 'upload-pack'])
    options = ['--no-local', upload_pack] if cache.stat().st_uid != os.geteuid() else ['--no-hardlinks']
    git('-c', 'safe.directory=' + str(cache), 'clone', *options, '--no-checkout', '--', cache, source)
    # An explicit SHA may have been fetched from a Gerrit review ref without
    # being reachable from heads/tags. The local transport omits such objects.
    if git('cat-file', '-e', sha + '^{commit}', cwd=source, checked=False):
        git('-c', 'safe.directory=' + str(cache), 'fetch', upload_pack, '--no-tags', '--no-recurse-submodules',
            str(cache), sha, cwd=source)
    git('remote', 'set-url', 'origin', url, cwd=source)
    if (source / '.git/objects/info/alternates').exists():
        raise RuntimeError('工作副本仍依赖 Git 缓存，停止构建')


def cached_checkout(root, source, kind, ref, url, timings, verify_objects=False):
    project = os.environ.get('XRAN_PROJECT_ID', 'x-ran-cloud')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,61}', project):
        raise ValueError('非法缓存项目标识')
    directory = Path(os.environ.get('XRAN_GIT_CACHE_ROOT', '/var/xran-git-cache')).absolute()
    # The Pod mounts just this project's PVC subdirectory at directory.
    safe_directory(directory)
    if not directory.is_dir():
        raise RuntimeError('Git 缓存 PVC 未挂载，请更新部署或在管理员设置中关闭 Git 缓存')
    identity = hashlib.sha256((project + '\0' + url + '\0' + os.environ.get('XRAN_GIT_CREDENTIALS_ID', '')).encode()).hexdigest()
    cache = directory / (identity + '.git')
    state_path = directory / (identity + '.json')
    lock_path = directory / '.lock'
    old_umask = os.umask(0o002)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o664)
    except BaseException:
        os.umask(old_umask)
        raise
    try:
        start = time.monotonic()
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.2)
        timings['waitMillis'] = round((time.monotonic() - start) * 1000)
        git = Git((directory, root), fd)
        safe_directory(cache)
        if state_path.is_symlink():
            raise ValueError('非法 Git 缓存状态文件')
        for leftover in directory.glob(identity + '.tmp-*'):
            safe_directory(leftover)
            shutil.rmtree(leftover)
        state = {'project': project, 'key': identity, 'repository': url, 'status': 'updating',
                 'lastUsedAt': time.time(), 'bytes': directory_bytes(cache), 'lastError': ''}
        if state_path.exists():
            with contextlib.suppress(ValueError, OSError):
                state['lastUpdatedAt'] = json.loads(state_path.read_text()).get('lastUpdatedAt')
        atomic_json(state_path, state)
        hit = cache.is_dir()
        try:
            start = time.monotonic()
            if hit:
                # Authentication/network failures must not be turned into a stale-cache success.
                try:
                    clean_interrupted_locks(cache)
                    if git('rev-parse', '--is-bare-repository', cwd=cache, capture=True) != 'true':
                        raise subprocess.CalledProcessError(1, ['git', 'rev-parse'])
                    if verify_objects and git('fsck', '--full', '--no-dangling', cwd=cache, checked=False):
                        raise subprocess.CalledProcessError(1, ['git', 'fsck'])
                except subprocess.CalledProcessError:
                    print('Git 缓存损坏，清理后重建一次', flush=True)
                    shutil.rmtree(cache)
                    hit = False
            for attempt in range(2):
                try:
                    if hit:
                        update(git, cache, url)
                    else:
                        staging = Path(tempfile.mkdtemp(prefix=identity + '.tmp-', dir=directory))
                        try:
                            # mkdtemp starts at 0700 regardless of umask. Both
                            # root image jobs and UID1000 jobs must traverse it.
                            staging.chmod(0o2775)
                            update(git, staging, url)
                            staging.rename(cache)
                        finally:
                            if staging.exists():
                                shutil.rmtree(staging)
                    timings['fetchMillis'] = round((time.monotonic() - start) * 1000)
                    sha = resolve(git, cache, kind, ref, bare=True)
                    state.update(lastUpdatedAt=time.time(), commit=sha)
                    copied = time.monotonic()
                    copy_repository(git, cache, source, url, sha)
                    timings['copyMillis'] = round((time.monotonic() - copied) * 1000)
                    break
                except subprocess.CalledProcessError:
                    # A full fsck is expensive: use it only to distinguish local
                    # corruption from network/authentication/invalid-ref errors.
                    if not hit or attempt or git('fsck', '--full', '--no-dangling', cwd=cache, checked=False) == 0:
                        raise
                    print('Git 缓存对象损坏，重建一次', flush=True)
                    shutil.rmtree(cache)
                    if source.exists():
                        shutil.rmtree(source)
                    hit = False
            state['status'] = 'ready'
            print('Git 缓存: ' + ('命中，增量更新' if hit else '首次建立/重建') + '; commit=' + sha, flush=True)
            return sha, {'enabled': True, 'hit': hit, 'key': identity}
        except BaseException as error:
            state.update(status='error', lastError=str(error))
            raise
        finally:
            state.update(bytes=directory_bytes(cache), lastUsedAt=time.time())
            atomic_json(state_path, state)
    finally:
        os.close(fd)
        os.umask(old_umask)


def checkout(root, source=None, publish=None):
    root = Path(root).absolute()
    source, publish = Path(source or root / 'source'), Path(publish or root / 'publish')
    kind, ref, url = (os.environ[name] for name in ('XRAN_REF_TYPE', 'XRAN_REF', 'XRAN_GIT_URL'))
    validate(kind, ref, url)
    if source.exists():
        shutil.rmtree(source)
    publish.mkdir(exist_ok=True)
    for old in publish.iterdir():
        if old.is_dir() and not old.is_symlink():
            shutil.rmtree(old)
        else:
            old.unlink()
    timings = {}
    (root / 'checkout.json').unlink(missing_ok=True)
    enabled = os.environ.get('XRAN_GIT_CACHE_ENABLED', 'false').lower() == 'true'
    for attempt in range(2 if enabled else 1):
        if enabled:
            sha, cache = cached_checkout(root, source, kind, ref, url, timings, verify_objects=bool(attempt))
        else:
            git = Git((root,))
            git('init', source)
            git('remote', 'add', 'origin', url, cwd=source)
            start = time.monotonic()
            git('fetch', '--atomic', '--no-recurse-submodules', '--prune', 'origin',
                '+refs/heads/*:refs/remotes/origin/*', '+refs/tags/*:refs/tags/*', cwd=source)
            timings['fetchMillis'] = round((time.monotonic() - start) * 1000)
            sha, cache = resolve(git, source, kind, ref), {'enabled': False, 'hit': False}
        git = Git((root,))
        start = time.monotonic()
        try:
            git('-c', 'advice.detachedHead=false', 'checkout', '--detach', sha, cwd=source)
            timings['checkoutMillis'] = round((time.monotonic() - start) * 1000)
            if attempt: cache['checkoutRetried'] = True
            break
        except subprocess.CalledProcessError:
            if not enabled or attempt: raise
            # Blob corruption may first surface while expanding the independent
            # worktree. Recheck under the project lock, rebuild only a bad cache,
            # and retry once. Normal checkouts never pay for a full fsck.
            print('工作副本检出失败，复查缓存对象后重试一次', flush=True)
            shutil.rmtree(source)
    start = time.monotonic()
    git('submodule', 'update', '--init', '--recursive', cwd=source)
    timings['submoduleMillis'] = round((time.monotonic() - start) * 1000)
    cache['timings'] = timings
    metadata = {'commit': sha, 'ref': ref, 'refType': kind, 'repository': url, 'gitCache': cache}
    atomic_json(root / 'checkout.json', metadata)
    print('Git 检出统计: ' + json.dumps(cache, ensure_ascii=False), flush=True)
    return metadata


if __name__ == '__main__':
    def cancelled(signum, frame):
        raise SystemExit('Git 检出已取消')
    signal.signal(signal.SIGTERM, cancelled)
    checkout(Path.cwd())
