#!/usr/bin/env python3
"""Agent 侧适配器：Git 精确检出、运行原有脚本、校验和归集产物。"""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import time

ROOT = Path.cwd()
SOURCE = ROOT / 'source'
PUBLISH = ROOT / 'publish'


def run(args, cwd=None, capture=False):
    return subprocess.run(args, cwd=cwd, check=True, text=True,
                          stdout=subprocess.PIPE if capture else None).stdout


def checkout():
    kind, ref = os.environ['XRAN_REF_TYPE'], os.environ['XRAN_REF']
    url = os.environ['XRAN_GIT_URL']
    if not ref or ref.startswith('-') or '\x00' in ref or '\n' in ref:
        raise ValueError('非法 Git 引用')
    # 禁止 local helper / 自定义协议；仓库地址仅由管理员配置。
    if not (url.startswith(('https://', 'http://', 'ssh://', 'git@'))):
        raise ValueError('Git URL 仅支持 http(s)/ssh')
    if SOURCE.exists():
        shutil.rmtree(SOURCE)
    PUBLISH.mkdir(exist_ok=True)
    for old in PUBLISH.iterdir():
        if old.is_dir():
            shutil.rmtree(old)
        else:
            old.unlink()
    run(['git', 'init', str(SOURCE)])
    run(['git', 'remote', 'add', 'origin', url], SOURCE)
    # 完整获取远端分支和 tag，支持历史 commit 及唯一短 SHA。
    run(['git', 'fetch', '--no-recurse-submodules', '--prune', 'origin',
         '+refs/heads/*:refs/remotes/origin/*', '+refs/tags/*:refs/tags/*'], SOURCE)
    if kind in ('branch', 'tag'):
        run(['git', 'check-ref-format', ('refs/heads/' if kind == 'branch' else 'refs/tags/') + ref])
        revision = ('refs/remotes/origin/' if kind == 'branch' else 'refs/tags/') + ref
    elif kind == 'commit':
        if not re.fullmatch(r'[0-9a-fA-F]{7,40}', ref):
            raise ValueError('commit 必须是 7–40 位十六进制 SHA')
        if len(ref) == 40:
            probe = subprocess.run(['git', 'cat-file', '-e', ref + '^{commit}'], cwd=SOURCE)
            if probe.returncode:
                run(['git', 'fetch', 'origin', ref], SOURCE)
        revision = ref
    else:
        raise ValueError('未知引用类型')
    sha = run(['git', 'rev-parse', '--verify', '--end-of-options', revision + '^{commit}'],
              SOURCE, True).strip()
    run(['git', '-c', 'advice.detachedHead=false', 'checkout', '--detach', sha], SOURCE)
    # 历史真实项目可含 submodule；地址和依赖需可在内网访问。
    run(['git', 'submodule', 'update', '--init', '--recursive'], SOURCE)
    (ROOT / 'checkout.json').write_text(json.dumps({'commit': sha, 'ref': ref,
        'refType': kind, 'repository': url}, ensure_ascii=False))


def build():
    target = os.environ['XRAN_TARGET']
    kind = os.environ['XRAN_KIND']
    if kind == 'service':
        run(['./build.sh', '-c', '-m', target], SOURCE / 'services')
    elif kind == 'image':
        for _ in range(120):
            if subprocess.run(['docker', 'info'], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError('独立 Docker daemon 启动超时')
        run(['docker', 'load', '-i', '/opt/xran/base-images.tar'])
        run(['./images-build.sh', target, os.environ['XRAN_IMAGE_TAG']],
            SOURCE / 'platform/k8s/images-build')
    else:
        raise ValueError('未知构建类型')


def collect():
    """即使 push 失败，也只发布通过 tar 完整性检查的已有文件。"""
    PUBLISH.mkdir(exist_ok=True)
    meta = json.loads((ROOT / 'checkout.json').read_text()) if (ROOT / 'checkout.json').exists() else {}
    meta.update(json.loads(os.environ['XRAN_BUILD_METADATA']))
    kind, target = os.environ['XRAN_KIND'], os.environ['XRAN_TARGET']
    directory = SOURCE / ('JDBG/Release/services' if kind == 'service' else 'platform/k8s/images-build')
    files = []
    if directory.exists():
        files = sorted(directory.glob('*.tar.gz')) if kind == 'service' else [directory / (target + '.tar')]
    collected = []
    errors = []
    for path in files:
        if not path.is_file():
            continue
        if path.is_symlink() or not re.fullmatch(r'[A-Za-z0-9_.-]+', path.name):
            errors.append('非法产物路径: ' + path.name)
            continue
        try:
            with tarfile.open(path, 'r:*') as archive:
                # 不提取内容，逐个读取验证压缩流完整性。
                for entry in archive:
                    if entry.isfile():
                        with archive.extractfile(entry) as data:
                            while data.read(1024 * 1024):
                                pass
        except (tarfile.TarError, EOFError, OSError) as error:
            errors.append(f'{path.name}: {error}')
            continue
        out = PUBLISH / path.name
        shutil.move(str(path), out)
        sha = hashlib.sha256()
        with out.open('rb') as data:
            for chunk in iter(lambda: data.read(1024 * 1024), b''):
                sha.update(chunk)
        collected.append({'name': out.name, 'size': out.stat().st_size, 'sha256': sha.hexdigest()})
    if kind == 'service' and meta.get('result') == 'SUCCESS':
        expected = (set(json.loads(os.environ['XRAN_SERVICES'])) if target == 'all'
                    else {target} if target != 'services_all' else None)
        actual = {x['name'].removesuffix('.tar.gz') for x in collected}
        if expected is not None and expected != actual:
            errors.append('服务产物集合不符合目标')
        if target == 'services_all' and (len(collected) != 1 or not re.fullmatch(
                r'L2L3_\d{10}_' + meta['commit'][:9] + r'\.tar\.gz', collected[0]['name'])):
            errors.append('services_all 产物名称或数量不符合约定')
    if kind == 'image':
        reference = os.environ['XRAN_IMAGE_REFERENCE']
        meta['imageReference'] = reference
        meta['pushStatus'] = 'success' if meta.get('result') == 'SUCCESS' else 'failed-or-not-reached'
        probe = subprocess.run(['docker', 'image', 'inspect', reference],
                               capture_output=True, text=True)
        if probe.returncode == 0:
            info = json.loads(probe.stdout)[0]
            meta['imageId'] = info['Id']
            meta['repoDigests'] = info.get('RepoDigests', [])
    if meta.get('result') == 'SUCCESS' and not collected:
        errors.append('构建没有产生有效产物')
    if errors:
        meta['result'] = 'FAILURE'
    meta['files'] = collected
    meta['collectionErrors'] = errors
    meta['finishedAt'] = dt.datetime.now(dt.timezone.utc).isoformat()
    (PUBLISH / 'metadata.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2) + '\n')
    (PUBLISH / 'SHA256SUMS').write_text(''.join(f'{x["sha256"]}  {x["name"]}\n' for x in collected))
    (ROOT / 'collection.json').write_text(json.dumps({'bytes': sum(p.stat().st_size for p in PUBLISH.iterdir()),
        'errors': errors, 'files': len(collected)}))
    if errors:
        print('\n'.join(errors), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    commands = {'checkout': checkout, 'build': build, 'collect': collect}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        raise SystemExit('usage: run-build.py checkout|build|collect')
    raise SystemExit(commands[sys.argv[1]]() or 0)
