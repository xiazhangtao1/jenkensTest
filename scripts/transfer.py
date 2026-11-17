#!/usr/bin/env python3
"""Separate offline dependencies, code updates and optional generated images."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def files_under(names):
    result = {}
    for name in names:
        path = ROOT / name
        if not path.exists():
            raise RuntimeError('缺少已准备的依赖：' + name)
        paths = path.rglob('*') if path.is_dir() else [path]
        for item in sorted(paths):
            if 'partial' in item.parts or item.name == 'lock' or not item.is_file():
                continue
            if item.is_symlink():
                raise RuntimeError('依赖不能是符号链接：' + str(item))
            result[item.relative_to(ROOT).as_posix()] = {
                'sha256': sha(item), 'bytes': item.stat().st_size,
                'mode': item.stat().st_mode & 0o777}
    return dict(sorted(result.items()))


def write_tar(path, files, extra=None):
    temporary = path.with_name(path.name + '.tmp')
    try:
        with temporary.open('wb') as output, gzip.GzipFile(filename='', fileobj=output, mode='wb', mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w', format=tarfile.PAX_FORMAT) as archive:
                for name, record in sorted(files.items()):
                    source = ROOT / name
                    item = tarfile.TarInfo(name)
                    item.size, item.mode = record['bytes'], record['mode']
                    with source.open('rb') as stream:
                        archive.addfile(item, stream)
                if extra:
                    import io
                    for name, data in extra.items():
                        item = tarfile.TarInfo(name); item.size = len(data); item.mode = 0o644
                        archive.addfile(item, io.BytesIO(data))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def select_images(source, destination, expected):
    """Copy only selected Docker images, sharing their layers inside the archive."""
    if destination.exists():
        with tarfile.open(destination) as archive:
            manifest = json.load(archive.extractfile('manifest.json'))
            found = {tag: 'sha256:' + hashlib.sha256(archive.extractfile(item['Config']).read()).hexdigest()
                     for item in manifest for tag in item['RepoTags']}
            if found == expected:
                return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + '.tmp')
    try:
        with tarfile.open(source) as incoming:
            manifest, payload, found = [], set(), {}
            for original in json.load(incoming.extractfile('manifest.json')):
                tags = sorted(set(original.get('RepoTags') or []) & expected.keys())
                if not tags:
                    continue
                item = dict(original, RepoTags=tags)
                image_id = 'sha256:' + hashlib.sha256(incoming.extractfile(item['Config']).read()).hexdigest()
                for tag in tags:
                    if expected[tag] != image_id:
                        raise RuntimeError('镜像与锁文件不符：' + tag)
                    found[tag] = image_id
                manifest.append(item)
                payload.update([item['Config'], *item['Layers']])
            if found != expected:
                raise RuntimeError('缺少外部运行镜像：' + ', '.join(sorted(expected.keys() - found.keys())))
            with tarfile.open(temporary, 'w') as outgoing:
                for name in sorted(payload):
                    member = incoming.getmember(name)
                    if not member.isfile() or Path(name).is_absolute() or '..' in Path(name).parts:
                        raise RuntimeError('非法镜像归档成员：' + name)
                    with incoming.extractfile(member) as stream:
                        outgoing.addfile(member, stream)
                import io
                data = json.dumps(manifest).encode()
                member = tarfile.TarInfo('manifest.json'); member.size = len(data)
                outgoing.addfile(member, io.BytesIO(data))
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def transfer_root():
    directory = ROOT / 'dist/transfer'
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / 'docs/transfer.md', directory / 'README.md')
    return directory


def export_runtime_images(destination, expected):
    """Export cached upstream images without building or pulling any custom image."""
    refs = list(expected)
    inspected = json.loads(subprocess.check_output(['docker', 'image', 'inspect', *refs], text=True))
    if {ref: image['Id'] for ref, image in zip(refs, inspected)} != expected:
        raise RuntimeError('本机外部镜像与锁文件不符，请先准备固定版本的镜像')
    descriptor, name = tempfile.mkstemp(prefix='upstream-', suffix='.tar', dir=destination.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        subprocess.run(['docker', 'save', '--output', str(temporary), *refs], check=True)
        select_images(temporary, destination, expected)
    finally:
        temporary.unlink(missing_ok=True)


def dependencies():
    output = transfer_root() / '01-dependencies'; output.mkdir(exist_ok=True)
    versions = dict(line.split('=', 1) for line in (ROOT / 'config/versions.env').read_text().splitlines()
                    if line and not line.startswith('#'))
    lock = json.loads((ROOT / 'config/images.lock.json').read_text())
    refs = [versions[key] for key in ('AGENT_IMAGE', 'DIND_IMAGE', 'LOCAL_PATH_IMAGE', 'STORAGE_HELPER_IMAGE')]
    if versions['REGISTRY_IMAGE'] in lock:
        refs.append(versions['REGISTRY_IMAGE'])
    source, destination = ROOT / '.cache/k8s-images.tar', ROOT / '.cache/runtime-base-images.tar'
    expected = {ref: lock[ref] for ref in refs}
    if not source.exists() and not destination.exists():
        export_runtime_images(destination, expected)
    else:
        select_images(source, destination, expected)
    groups = [
        ('compiler', 'compiler-deps.tar.gz', ['vendor/compile', '.cache/compile-apt', '.cache/compile-extra-apt',
            '.cache/compile-compat-apt', '.cache/compile-tools', '.cache/base-images.tar']),
        ('jenkins', 'jenkins-deps.tar.gz', ['.cache/plugins', '.cache/plugins.complete', '.cache/platform-m2', '.cache/charts', '.cache/tools']),
        ('build-images', 'build-base-images.tar', ['.cache/build-base-images.tar']),
        ('runtime-images', 'runtime-base-images.tar', ['.cache/runtime-base-images.tar']),
        ('xcn-images', 'xcn-base-images.tar', ['.cache/xcn-base-images.tar'])]
    previous = json.loads((output / 'MANIFEST.json').read_text()) if (output / 'MANIFEST.json').exists() else {}
    old = {item['name']: item for item in previous.get('components', [])}
    components = []
    for name, filename, sources in groups:
        files = files_under(sources)
        identity = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
        target = output / filename
        cached = old.get(name, {})
        if cached.get('id') == identity and target.exists() and sha(target) == cached.get('sha256'):
            print('未变化，复用：' + filename, flush=True)
        else:
            if filename.endswith('.tar.gz'):
                write_tar(target, files)
            else:
                # A real file, not a symlink; copying this delivery directory is self-contained.
                target.unlink(missing_ok=True)
                os.link(ROOT / sources[0], target)
            print('已生成：' + filename, flush=True)
        record = {'name': name, 'package': filename, 'id': identity, 'sha256': sha(target),
                  'bytes': target.stat().st_size, 'files': files}
        if name.endswith('images'):
            with tarfile.open(target) as archive:
                record['images'] = {tag: 'sha256:' + hashlib.sha256(archive.extractfile(item['Config']).read()).hexdigest()
                                    for item in json.load(archive.extractfile('manifest.json')) for tag in item['RepoTags']}
        components.append(record)
    data = {'format': 1, 'kind': 'offline-dependencies', 'components': components}
    (output / 'MANIFEST.json').write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    (output / 'SHA256SUMS').write_text(''.join(item['sha256'] + '  ' + item['package'] + '\n' for item in components)
        + sha(output / 'MANIFEST.json') + '  MANIFEST.json\n')
    print('依赖已单独打包；不包含编译镜像、Controller 成品、业务 Git 库或账号私钥。', flush=True)


def code(base):
    output = transfer_root() / '02-code'; output.mkdir(exist_ok=True)
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    subprocess.run(['git', 'diff', '--exit-code', 'HEAD', '--'], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    record = {'commit': head, 'excludedDependencies': 'vendor/compile', 'containsGitHistory': False}
    manifest = output.parent / '01-dependencies/MANIFEST.json'
    if manifest.exists():
        shutil.copy2(manifest, output / 'DEPENDENCIES.json')
        record['dependenciesManifestSha256'] = sha(manifest)
    else:
        (output / 'DEPENDENCIES.json').unlink(missing_ok=True)
    if base:
        base = subprocess.check_output(['git', 'rev-parse', '--verify', '--end-of-options', base + '^{commit}'], cwd=ROOT, text=True).strip()
        record['baseCommit'] = base
        package = output / 'code.patch'
        package.write_bytes(subprocess.check_output(['git', 'diff', '--binary', '--no-renames', base, head,
            '--', '.', ':(exclude)vendor/compile', ':(exclude)config/site.local.yaml', ':(exclude)TRANSFER_SOURCE.json'], cwd=ROOT))
        (output / 'code.tar.gz').unlink(missing_ok=True)
    else:
        names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')[:-1]
        names = [name for name in names if not name.startswith('vendor/compile/')
                 and name not in ('config/site.local.yaml', 'TRANSFER_SOURCE.json')]
        package = output / 'code.tar.gz'
        write_tar(package, files_under(names), {'TRANSFER_SOURCE.json': json.dumps(record, indent=2).encode() + b'\n'})
        (output / 'code.patch').unlink(missing_ok=True)
    record.update(package=package.name, bytes=package.stat().st_size, sha256=sha(package))
    (output / 'CODE.json').write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
    sums = record['sha256'] + '  ' + package.name + '\n' + sha(output / 'CODE.json') + '  CODE.json\n'
    if manifest.exists():
        sums += record['dependenciesManifestSha256'] + '  DEPENDENCIES.json\n'
    (output / 'SHA256SUMS').write_text(sums)
    print('仅代码：' + str(package) + '；大文件依赖单独带入。')


def check(manifest, workspace, docker=False):
    data = json.loads(Path(manifest).read_text())
    if data.get('format') != 1 or data.get('kind') != 'offline-dependencies':
        raise ValueError('不是分类依赖清单')
    workspace = Path(workspace).resolve()
    missing = []
    for component in data['components']:
        matches = True
        for name, expected in component['files'].items():
            path = workspace / name
            if not path.resolve().is_relative_to(workspace):
                raise ValueError('清单路径越界')
            if not path.is_file() or path.stat().st_size != expected['bytes'] or sha(path) != expected['sha256']:
                matches = False
                break
        if not matches and docker and component.get('images'):
            probe = subprocess.run(['docker', 'image', 'inspect', *component['images']], capture_output=True, text=True)
            if probe.returncode == 0:
                found = {tag: image['Id'] for image in json.loads(probe.stdout) for tag in image.get('RepoTags', [])}
                matches = all(found.get(tag) == digest for tag, digest in component['images'].items())
                if matches:
                    print('镜像在本机 Docker 中已有，可自行导出：' + component['package'])
        print(('已有，无需拷贝：' if matches else '缺少或变化，需要拷贝：') + component['package'])
        if not matches:
            missing.append(component['package'])
    return missing


def dependency_files(data):
    if data.get('format') != 1 or data.get('kind') != 'offline-dependencies':
        raise ValueError('不是分类依赖清单')
    files = {}
    for component in data['components']:
        for name, record in component['files'].items():
            dependency_path(ROOT, name)
            if name in files and files[name] != record:
                raise ValueError('依赖清单包含冲突文件')
            files[name] = record
    return files


def dependency_path(root, name):
    if not isinstance(name, str) or not name.startswith(('.cache/', 'vendor/compile/')) or '\\' in name:
        raise ValueError('差量仅允许依赖目录')
    p = Path(name)
    root = Path(root).resolve()
    if p.is_absolute() or '..' in p.parts or not (root / p).resolve().is_relative_to(root):
        raise ValueError('依赖路径越界')
    current = root
    for part in p.parts:
        current /= part
        if current.is_symlink(): raise ValueError('依赖路径不能包含符号链接')
    return root / p


def verify_dependency(path, record):
    if not path.is_file() or path.stat().st_size != record['bytes'] or sha(path) != record['sha256']:
        raise RuntimeError('依赖缺失或校验失败: ' + str(path))


def delta(base_manifest, target_manifest):
    base = json.loads(Path(base_manifest).read_text())
    target = json.loads(Path(target_manifest).read_text())
    old, new = dependency_files(base), dependency_files(target)
    changed = {n: r for n, r in new.items() if old.get(n) != r}
    for name, record in changed.items(): verify_dependency(dependency_path(ROOT, name), record)
    folder = transfer_root() / '01-dependencies/updates'
    folder.mkdir(parents=True, exist_ok=True)
    description = {'format': 1, 'kind': 'offline-dependency-delta', 'base': base, 'target': target,
                   'changed': changed, 'removed': sorted(old.keys() - new.keys())}
    path = folder / 'dependencies-delta.tar.gz'
    write_tar(path, changed, {'DELTA.json': json.dumps(description, ensure_ascii=False).encode()})
    (folder / 'TARGET_MANIFEST.json').write_text(json.dumps(target, ensure_ascii=False, indent=2) + '\n')
    (folder / 'SHA256SUMS').write_text(sha(path) + '  ' + path.name + '\n' + sha(folder / 'TARGET_MANIFEST.json') + '  TARGET_MANIFEST.json\n')
    print(f'差量包: {path}; 新增/变化 {len(changed)} 个文件，移除 {len(description["removed"])} 个旧依赖文件')
    return path


def apply_delta(package, workspace):
    root = Path(workspace).resolve()
    with tarfile.open(package) as archive:
        members = archive.getmembers()
        if len({m.name for m in members}) != len(members): raise ValueError('重复差量归档成员')
        item = archive.getmember('DELTA.json')
        if not item.isfile() or item.size > 32 * 1024 * 1024: raise ValueError('非法差量清单')
        data = json.load(archive.extractfile(item))
        if data.get('format') != 1 or data.get('kind') != 'offline-dependency-delta': raise ValueError('差量格式错误')
        old, new = dependency_files(data['base']), dependency_files(data['target'])
        expected = {n:r for n,r in new.items() if old.get(n) != r}
        if data['changed'] != expected or data['removed'] != sorted(old.keys() - new.keys()): raise ValueError('差量规则与清单不符')
        if set(archive.getnames()) != set(expected) | {'DELTA.json'}: raise ValueError('差量包成员不符')
        # Validate the entire baseline before touching anything, including removed files.
        for name, record in old.items(): verify_dependency(dependency_path(root, name), record)
        for name in new.keys() - old.keys():
            if dependency_path(root, name).exists(): raise RuntimeError('新增依赖路径已存在，请核对基线: ' + name)
        with tempfile.TemporaryDirectory(prefix='dependency-delta-') as staging:
            staged = Path(staging)
            for name, record in expected.items():
                member = archive.getmember(name)
                if not member.isfile() or member.size != record['bytes']: raise ValueError('非法依赖成员: ' + name)
                path = staged / name; path.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as incoming, path.open('wb') as outgoing: shutil.copyfileobj(incoming, outgoing)
                verify_dependency(path, record)
            for name, record in expected.items():
                path = dependency_path(root, name); path.parent.mkdir(parents=True, exist_ok=True)
                descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + '.delta-', dir=path.parent)
                temporary = Path(temporary_name)
                try:
                    with os.fdopen(descriptor, 'wb') as output, (staged / name).open('rb') as incoming:
                        shutil.copyfileobj(incoming, output)
                    temporary.chmod(record.get('mode', 0o644)); temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
            for name in data['removed']: dependency_path(root, name).unlink()
    for name, record in new.items(): verify_dependency(dependency_path(root, name), record)
    manifest = root / 'DEPENDENCIES_APPLIED.json'
    manifest.write_text(json.dumps(data['target'], ensure_ascii=False, indent=2) + '\n')
    print('差量应用并校验通过；下次使用 DEPENDENCIES_APPLIED.json 作为内网基线。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('deps')
    c = commands.add_parser('code'); c.add_argument('--base-commit', default='')
    c = commands.add_parser('check'); c.add_argument('--manifest', required=True); c.add_argument('--workspace', default=str(ROOT)); c.add_argument('--docker', action='store_true')
    commands.add_parser('git')
    c = commands.add_parser('delta'); c.add_argument('--base-manifest', required=True); c.add_argument('--manifest', default=str(ROOT / 'dist/transfer/01-dependencies/MANIFEST.json'))
    c = commands.add_parser('apply-delta'); c.add_argument('--package', required=True); c.add_argument('--workspace', default=str(ROOT))
    args = parser.parse_args()
    if args.command == 'delta':
        delta(args.base_manifest, args.manifest)
    elif args.command == 'apply-delta':
        apply_delta(args.package, args.workspace)
    elif args.command == 'deps':
        dependencies()
    elif args.command == 'code':
        code(args.base_commit)
    elif args.command == 'check':
        missing = check(args.manifest, args.workspace, args.docker)
        print('本次需要的依赖包：' + (', '.join(missing) if missing else '无'))
    elif args.command == 'git':
        output = transfer_root() / '02-code/optional-git'; output.mkdir(parents=True, exist_ok=True)
        target = output / 'jenkensTest.bundle'
        subprocess.run(['git', 'bundle', 'create', str(target), '--all'], cwd=ROOT, check=True)
        (output / 'SHA256SUMS').write_text(sha(target) + '  ' + target.name + '\n')
        print('可选完整 Git 历史（只需首次迁移）：' + str(target))


if __name__ == '__main__':
    main()
