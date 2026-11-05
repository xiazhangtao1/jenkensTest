#!/usr/bin/env python3
"""通过当前 HTTP(S)_PROXY 下载 Docker Hub 镜像，校验 blob 后生成 docker-load 归档。

避免为一次下载修改共用 Docker daemon 的代理配置。只支持本项目使用的 linux/amd64。
"""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request

ACCEPT = ', '.join(['application/vnd.oci.image.index.v1+json',
    'application/vnd.docker.distribution.manifest.list.v2+json',
    'application/vnd.oci.image.manifest.v1+json',
    'application/vnd.docker.distribution.manifest.v2+json'])


def download_image(reference, destination):
    name = reference.removeprefix('docker.io/')
    repository, tag = name.rsplit(':', 1)
    if '/' not in repository:
        repository = 'library/' + repository
    query = urllib.parse.urlencode({'service': 'registry.docker.io',
        'scope': f'repository:{repository}:pull'})
    curl = ['curl', '--fail', '--location', '--silent', '--show-error', '--retry', '6',
            '--retry-all-errors', '--connect-timeout', '12', '--max-time', '240', '--retry-max-time', '300']
    token = json.loads(subprocess.check_output(curl + ['https://auth.docker.io/token?' + query]))['token']

    def request(path, output=None):
        command = curl + ['-H', 'Authorization: Bearer ' + token, '-H', 'Accept: ' + ACCEPT]
        if output is not None:
            subprocess.run(command + ['--output', str(output),
                f'https://registry-1.docker.io/v2/{repository}/{path}'], check=True)
            return None
        return subprocess.check_output(command + [f'https://registry-1.docker.io/v2/{repository}/{path}'])

    def manifest(value):
        data = request('manifests/' + value)
        if value.startswith('sha256:') and hashlib.sha256(data).hexdigest() != value[7:]:
            raise ValueError('manifest digest mismatch')
        return json.loads(data)

    selected = manifest(tag)
    if 'manifests' in selected:
        platforms = [x for x in selected['manifests']
                     if x.get('platform', {}).get('os') == 'linux'
                     and x.get('platform', {}).get('architecture') == 'amd64']
        if len(platforms) != 1:
            raise ValueError('expected exactly one linux/amd64 manifest')
        selected = manifest(platforms[0]['digest'])
    cache = destination.parent / 'blobs'
    cache.mkdir(parents=True, exist_ok=True)

    def blob(descriptor):
        digest = descriptor['digest'].removeprefix('sha256:')
        if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('unsupported digest')
        target = cache / digest
        if target.exists():
            with target.open('rb') as source:
                sha = hashlib.sha256()
                for chunk in iter(lambda: source.read(1024 * 1024), b''):
                    sha.update(chunk)
                if sha.hexdigest() == digest:
                    return target
        part = target.with_suffix('.part')
        for attempt in range(4):
            try:
                sha = hashlib.sha256()
                request('blobs/' + descriptor['digest'], part)
                with part.open('rb') as source:
                    while chunk := source.read(1024 * 1024):
                        sha.update(chunk)
                if sha.hexdigest() != digest or part.stat().st_size != descriptor['size']:
                    raise ValueError('blob size/digest mismatch')
                part.replace(target)
                print(f'{reference}: verified {digest[:12]} ({descriptor["size"]} bytes)', flush=True)
                return target
            except Exception:
                part.unlink(missing_ok=True)
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)

    config = blob(selected['config'])
    layers = [blob(layer) for layer in selected['layers']]
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Docker archive needs uncompressed layer.tar; use temporary files to bound RAM.
    with tempfile.TemporaryDirectory(dir=destination.parent) as tmp:
        root = Path(tmp)
        config_name = selected['config']['digest'][7:] + '.json'
        shutil.copyfile(config, root / config_name)
        layer_names = []
        for descriptor, compressed in zip(selected['layers'], layers):
            layer_name = descriptor['digest'][7:] + '/layer.tar'
            path = root / layer_name
            path.parent.mkdir()
            with compressed.open('rb') as source:
                magic = source.read(2)
            reader = gzip.open(compressed, 'rb') if magic == b'\x1f\x8b' else compressed.open('rb')
            with reader, path.open('wb') as output:
                shutil.copyfileobj(reader, output, 1024 * 1024)
            layer_names.append(layer_name)
        (root / 'manifest.json').write_text(json.dumps([{
            'Config': config_name, 'RepoTags': [reference], 'Layers': layer_names}]))
        temporary = destination.with_suffix('.tmp')
        with tarfile.open(temporary, 'w') as archive:
            archive.add(root / 'manifest.json', arcname='manifest.json')
            archive.add(root / config_name, arcname=config_name)
            for layer in layer_names:
                archive.add(root / layer, arcname=layer)
        temporary.replace(destination)
    print(str(destination), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image')
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    download_image(args.image, args.output)
