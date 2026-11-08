#!/usr/bin/env python3
"""Build/export the Ubuntu 22.04 SDK independently of Jenkins preparation."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / '.cache'
DEFAULT_NAME = '192.168.10.35/xnet/xran-compile'
DEFAULT_DOCKERFILE = 'docker/builder-compat.Dockerfile'
TAG = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}')
HOST = r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*'
NAME = re.compile(r'(?:' + HOST + r'(?::[0-9]+)?/)?[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*')


def image_reference(name, tag):
    if not NAME.fullmatch(name) or not TAG.fullmatch(tag):
        raise ValueError('非法镜像名称或 tag；名称不含 tag，仓库地址可以带端口')
    return name + ':' + tag


def resolve_dockerfile(value):
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    if not path.is_file():
        raise ValueError('Dockerfile 不存在或不是普通文件：' + str(path))
    return path


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def run(command, **kwargs):
    print('+ ' + ' '.join(map(str, command)), flush=True)
    return subprocess.run(list(map(str, command)), check=True, **kwargs)


def inspect(ref):
    return json.loads(subprocess.check_output(['docker', 'image', 'inspect', ref], text=True))[0]


def versions():
    return dict(line.split('=', 1) for line in (ROOT / 'config/versions.env').read_text().splitlines()
                if line and not line.startswith('#'))


def ensure_image(ref, offline):
    if subprocess.run(['docker', 'image', 'inspect', ref], stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL).returncode:
        if offline:
            raise RuntimeError('离线构建缺少基础镜像，请先 docker load：' + ref)
        archive = CACHE / 'images' / (ref.replace('/', '_').replace(':', '_') + '.tar')
        archive.parent.mkdir(parents=True, exist_ok=True)
        if not archive.exists():
            run([sys.executable, ROOT / 'scripts/fetch-image.py', ref, archive])
        run(['docker', 'load', '-i', archive])


def assemble_sources():
    vendor = ROOT / 'vendor/compile'
    manifest = json.loads((vendor / 'manifest.json').read_text())
    target = CACHE / 'compile-sources'
    target.mkdir(parents=True, exist_ok=True)
    for item in manifest['sources']:
        for part in item['parts']:
            path = vendor / part['path']
            if not path.is_file() or sha256(path) != part['sha256']:
                raise RuntimeError('源码分片缺失或校验失败：' + str(path))
        archive = target / item['archive']
        if archive.exists() and sha256(archive) == item['sha256']:
            continue
        temporary = archive.with_suffix(archive.suffix + '.tmp')
        try:
            with temporary.open('wb') as output:
                for part in item['parts']:
                    path = vendor / part['path']
                    with path.open('rb') as stream:
                        for block in iter(lambda: stream.read(1024 * 1024), b''):
                            output.write(block)
            if sha256(temporary) != item['sha256']:
                raise RuntimeError('源码归档校验失败：' + item['archive'])
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
    (target / 'SHA256SUMS').write_text(''.join(f"{s['sha256']}  {s['archive']}\n" for s in manifest['sources']))


def prepare_apt(args, base, cache_name='compile-apt', package_files=None):
    cache = CACHE / cache_name
    cache.mkdir(parents=True, exist_ok=True)
    source = (ROOT / args.apt_sources).resolve()
    package_files = package_files or ['config/apt-packages.txt', 'config/compile-apt-packages.txt']
    packages = sorted({package for name in package_files for package in (ROOT / name).read_text().split()})
    fingerprint = {'baseImageId': inspect(base)['Id'], 'sourcesSha256': sha256(source), 'packages': packages}
    lock_path = cache / 'manifest.json'
    if lock_path.exists():
        lock = json.loads(lock_path.read_text())
        cached_inputs = lock.get('inputs', {})
        same_packages = all(cached_inputs.get(key) == fingerprint[key] for key in ('baseImageId', 'packages'))
        matching_inputs = same_packages and (args.offline or cached_inputs.get('sourcesSha256') == fingerprint['sourcesSha256'])
        if matching_inputs and all((cache / name).exists() and sha256(cache / name) == digest
                                                   for name, digest in lock.get('debs', {}).items()) and lock.get('debs'):
            return
    if args.offline:
        raise RuntimeError('完整 APT 缓存缺失或与基础镜像/源/软件清单不符：' + cache_name +
                           '；请在联网环境先执行同配置的 compile-image')
    # Download the complete dependency closure from a fresh instance of the pinned base.
    # A temporary directory avoids mixing multiple revisions of a deb on subsequent preparations.
    staging = CACHE / (cache_name + '-staging')
    staging.mkdir(exist_ok=True)
    proxy = []
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'NO_PROXY', 'no_proxy'):
        if name in os.environ:
            proxy += ['-e', name]
    run(['docker', 'run', '--rm', '--network', args.prepare_network, *proxy,
         '-v', f'{source}:/etc/apt/sources.list:ro', '-v', f'{staging}:/var/cache/apt/archives',
         '-e', 'DEBIAN_FRONTEND=noninteractive', base, 'bash', '-euc',
         'rm -f /etc/apt/apt.conf.d/docker-clean; '
         'apt-get -o Acquire::Retries=3 -o Acquire::http::Pipeline-Depth=0 update; '
         'apt-get -o Acquire::Retries=3 -o Acquire::http::Pipeline-Depth=0 '
         '--download-only -y --no-install-recommends install "$@"', 'apt-cache', *packages])
    # Old cache revisions are replaced only after successful preparation.
    for path in cache.glob('*.deb'):
        path.unlink()
    for path in staging.glob('*.deb'):
        path.replace(cache / path.name)
    debs = {path.name: sha256(path) for path in sorted(cache.glob('*.deb'))}
    if not debs:
        raise RuntimeError('APT 未下载任何依赖')
    lock_path.write_text(json.dumps({'inputs': fingerprint, 'debs': debs}, indent=2) + '\n')


def prepare_helm(offline):
    lock = json.loads((ROOT / 'config/helm.lock.json').read_text())
    directory = CACHE / 'compile-tools'
    directory.mkdir(exist_ok=True)
    binary = directory / 'helm'
    if binary.exists() and sha256(binary) == lock['binarySha256']:
        return
    if offline:
        raise RuntimeError('离线缓存缺少已锁定的 Helm 二进制')
    archive = directory / 'helm.tar.gz'
    with urllib.request.urlopen(lock['url'], timeout=120) as response, archive.open('wb') as output:
        while block := response.read(1024 * 1024):
            output.write(block)
    with tarfile.open(archive) as source:
        data = source.extractfile('linux-amd64/helm').read()
    if hashlib.sha256(data).hexdigest() != lock['binarySha256']:
        raise RuntimeError('Helm 二进制校验失败')
    binary.write_bytes(data)
    binary.chmod(0o755)


def build(args):
    ref = image_reference(args.image_name, args.image_tag)
    dockerfile = resolve_dockerfile(getattr(args, 'dockerfile', DEFAULT_DOCKERFILE))
    if not 1 <= args.jobs <= 64:
        raise ValueError('BUILD_JOBS 必须为 1..64')
    v = versions()
    CACHE.mkdir(exist_ok=True)
    assemble_sources()
    for base in [v['UBUNTU_IMAGE'], v['DOCKER_CLI_IMAGE']]:
        ensure_image(base, args.offline)
    bases = [line.strip() for line in (ROOT / 'config/base-images.txt').read_text().splitlines()
             if line.strip() and not line.startswith('#')]
    for base in bases:
        ensure_image(base, args.offline)
    if not (CACHE / 'base-images.tar').exists():
        run(['docker', 'save', '-o', CACHE / 'base-images.tar', *bases])
    prepare_apt(args, v['UBUNTU_IMAGE'])
    prepare_apt(args, v['UBUNTU_IMAGE'], 'compile-extra-apt', ['config/compiler-extra-apt-packages.txt'])
    # Prepare both recipes' small extra caches so the same offline kit can build either.
    prepare_apt(args, v['UBUNTU_IMAGE'], 'compile-compat-apt', ['config/compiler-compat-apt-packages.txt'])
    prepare_helm(args.offline)
    env = os.environ.copy()
    env['DOCKER_BUILDKIT'] = '0'
    # Compilation never needs a network: all archives and debs have already been verified.
    run(['docker', 'build', '--network', 'none', '--pull=false',
         '--build-arg', f'APT_SOURCES={args.apt_sources}', '--build-arg', f'BUILD_JOBS={args.jobs}',
         '--build-arg', f'DOCKER_CLI_IMAGE={v["DOCKER_CLI_IMAGE"]}',
         '-f', dockerfile, '-t', ref, ROOT], env=env)
    run(['docker', 'run', '--rm', '--network', 'none', ref, 'bash', '/opt/xran/verify-compile.sh'])
    if not args.no_export:
        export(ref, dockerfile)
    return ref


def export(ref, dockerfile):
    directory = ROOT / 'dist/transfer/03-generated-images/compiler' / re.sub(r'[^A-Za-z0-9_.-]', '_', ref)
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / 'image.tar'
    run(['docker', 'save', '-o', archive, ref])
    details = inspect(ref)
    probe = '''import json, subprocess
commands = {'protoc': ['protoc', '--version'], 'protocC': ['protoc-c', '--version'],
            'protobufCLibrary': ['pkg-config', '--modversion', 'libprotobuf-c'],
            'pkgconf': ['pkgconf', '--version']}
print(json.dumps({name: subprocess.check_output(command, text=True).strip()
                  for name, command in commands.items()}))'''
    dependency_versions = json.loads(subprocess.check_output(
        ['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'python3', ref, '-c', probe], text=True))
    dockerfile = Path(dockerfile)
    metadata = {'image': ref, 'imageId': details['Id'], 'createdAt': details['Created'],
                'dockerfile': str(dockerfile.relative_to(ROOT)) if dockerfile.is_relative_to(ROOT) else str(dockerfile),
                'dockerfileSha256': sha256(dockerfile), 'dependencyVersions': dependency_versions,
                'sourceManifestSha256': sha256(ROOT / 'vendor/compile/manifest.json'),
                'aptManifestSha256': sha256(CACHE / 'compile-apt/manifest.json'),
                'extraAptManifestSha256': sha256(CACHE / 'compile-extra-apt/manifest.json'),
                'compatAptManifestSha256': sha256(CACHE / 'compile-compat-apt/manifest.json'),
                'bytes': archive.stat().st_size, 'sha256': sha256(archive)}
    (directory / 'manifest.json').write_text(json.dumps(metadata, indent=2) + '\n')
    (directory / 'SHA256SUMS').write_text(f"{metadata['sha256']}  image.tar\n{sha256(directory / 'manifest.json')}  manifest.json\n")
    print('编译镜像已导出：' + str(directory), flush=True)


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('command', choices=['build', 'push', 'assemble'], nargs='?', default='build')
    cli.add_argument('--image-name', default=DEFAULT_NAME)
    cli.add_argument('--image-tag', default='latest')
    cli.add_argument('--dockerfile', default=DEFAULT_DOCKERFILE,
                     help='编译镜像 Dockerfile 路径，构建上下文固定为项目根目录')
    cli.add_argument('--jobs', type=int, default=4)
    cli.add_argument('--apt-sources', default='config/sources.online.list')
    cli.add_argument('--offline', action='store_true')
    cli.add_argument('--prepare-network', default='host', choices=['host', 'default'])
    cli.add_argument('--no-export', action='store_true')
    return cli


if __name__ == '__main__':
    args = parser().parse_args()
    os.chdir(ROOT)
    try:
        if args.command == 'build':
            build(args)
        elif args.command == 'push':
            run(['docker', 'push', image_reference(args.image_name, args.image_tag)])
        else:
            assemble_sources()
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
