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
import tempfile
import time
sys.path.insert(0, str(Path(__file__).resolve().parent))
from projects import relative
from git_checkout import checkout as checkout_source

ROOT = Path.cwd()
SOURCE = ROOT / 'source'
PUBLISH = ROOT / 'publish'


def project_spec():
    return json.loads(os.environ.get('XRAN_PROJECT_SPEC', '{}'))


def project_recipe(kind):
    return project_spec().get('builds', {}).get(kind, {})


def inside_source(name):
    path = SOURCE / relative(name)
    if not path.resolve().is_relative_to(SOURCE.resolve()):
        raise ValueError('项目路径越界: ' + name)
    return path


def recipe_command(recipe, **values):
    return [part.format(**values) for part in recipe['command']]


def command_recipe():
    return project_spec()['targets'][os.environ['XRAN_TARGET']]


def run(args, cwd=None, capture=False, input_text=None, env=None):
    return subprocess.run(args, cwd=cwd, check=True, text=True,
                          input=input_text,
                          stdout=subprocess.PIPE if capture else None, **({'env': env} if env is not None else {})).stdout


def checkout():
    return checkout_source(ROOT, SOURCE, PUBLISH)


def ensure_docker(archive):
    for _ in range(120):
        if subprocess.run(['docker', 'info'], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError('独立 Docker daemon 启动超时')
    run(['docker', 'load', '-i', archive])


def release_build():
    spec = project_recipe('xcn')
    directory = inside_source(spec.get('workdir', 'helm'))
    script = directory / spec.get('command', ['./build-release.sh'])[0]
    if not script.is_file():
        raise RuntimeError('当前 Git 引用缺少 helm/build-release.sh，请选择具备发布入口的分支/tag/commit')
    chart, app = os.environ['XRAN_CHART_VERSION'], os.environ['XRAN_IMAGE_TAG']
    archive = '/opt/xran/xcn-bases/images.tar'
    if not Path(archive).is_file():
        archive = '/opt/xran/open5gs-bases/images.tar'  # compatibility with earlier Pod templates
    ensure_docker(archive)
    # The upstream script keeps old outputs on failure; remove only this run's
    # expected files in the isolated checkout to prevent stale publication.
    for name in spec.get('images', ['xcn-runtime.tar', 'xcn-webui.tar', 'xcn-monitor.tar']) + [spec.get('chart', 'xcn-{chartVersion}.tgz').format(chartVersion=chart)]:
        (directory / relative(name)).unlink(missing_ok=True)
    command = spec.get('command', ['./build-release.sh']) + (['--offline'] if os.environ['XRAN_OFFLINE'] == 'true' else []) + [chart, app]
    pushes = {}
    with (ROOT / 'release-build.log').open('w') as log:
        process = subprocess.Popen(command, cwd=directory, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True)
        for line in process.stdout:
            log.write(line)
            print(line, end='', flush=True)
            match = re.match(r'^(\S+): build OK, push (OK|FAILED)', line)
            if match:
                pushes[match[1]] = 'success' if match[2] == 'OK' else 'failed'
        status = process.wait()
    (ROOT / 'release-push.json').write_text(json.dumps(pushes))
    if status:
        raise subprocess.CalledProcessError(status, command)


def capabilities():
    print(json.dumps({'xranServiceCompatibility': 1, 'xranImageArchiveFallback': 1, 'projectRecipes': 1}))


def prepare_xran():
    value = os.environ.get('XRAN_SERVICE_SERIAL', 'true')
    if value not in ('true', 'false'):
        raise ValueError('XRAN_SERVICE_SERIAL 必须为 true 或 false')
    makefile = inside_source(project_recipe('service').get('workdir', 'services')) / 'makefile'
    if value == 'true' and makefile.exists():
        if makefile.is_symlink() or not makefile.resolve().is_relative_to(SOURCE.resolve()):
            raise ValueError('服务 Makefile 必须位于本次检出的源码目录内')
        header = b'# X-RAN-CLOUD CI: serialize service compilation\n.NOTPARALLEL:\n'
        content = makefile.read_bytes()
        if not content.startswith(header):
            makefile.write_bytes(header + content)
        print('服务编译：串行；已适配 services/makefile', flush=True)
    else:
        print('服务编译：' + ('遵循原脚本并行设置' if value == 'false' else
              '未发现 services/makefile，执行原构建入口'), flush=True)


def build():
    target = os.environ['XRAN_TARGET']
    kind = os.environ['XRAN_KIND']
    if project_spec().get('adapter') == 'command':
        spec = command_recipe()
        for name in spec['artifacts']:
            inside_source(name).unlink(missing_ok=True)
        environment = {**os.environ, **project_spec().get('env', {}), **spec.get('env', {})}
        run(recipe_command(spec, target=target), inside_source(spec['workdir']), input_text='', env=environment)
        return
    if kind == 'open5gs':  # previous compiler adapter protocol
        kind = 'xcn'
    if kind == 'service':
        if target == 'all':
            raise ValueError('服务目标 all 已取消，请选择单个服务或 services_all')
        prepare_xran()
        spec = project_recipe('service')
        run(recipe_command(spec, target=target) if spec else ['./build.sh', '-c', '-m', target],
            inside_source(spec.get('workdir', 'services')), input_text='/opt/dpdk-22.03\n')
    elif kind == 'image':
        prepare_xran()
        ensure_docker('/opt/xran/base-images.tar')
        image_archive_path().unlink(missing_ok=True)
        spec = project_recipe('image')
        run(recipe_command(spec, target=target, imageTag=os.environ['XRAN_IMAGE_TAG']) if spec else
            ['./images-build.sh', target, os.environ['XRAN_IMAGE_TAG']],
            inside_source(spec.get('workdir', 'platform/k8s/images-build')), input_text='/opt/dpdk-22.03\n')
    elif kind == 'xcn':
        release_build()
    else:
        raise ValueError('未知构建类型')


def read_small(archive, name):
    item = archive.getmember(name)
    if not item.isfile() or item.size > 8 * 1024 * 1024:
        raise ValueError('非法归档元数据: ' + name)
    return archive.extractfile(item).read()


def image_archive_path():
    target = os.environ['XRAN_TARGET']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', target) or target in ('.', '..'):
        raise ValueError('非法镜像目标')
    directory = inside_source(project_recipe('image').get('artifactDirectory', 'platform/k8s/images-build'))
    if not directory.resolve().is_relative_to(SOURCE.resolve()):
        raise ValueError('镜像产物目录必须位于本次检出的源码内')
    return directory / (target + '.tar')


def validate_image_archive(path, reference, expected_id=None):
    if path.is_symlink() or not path.is_file():
        raise ValueError('非法镜像产物路径: ' + path.name)
    with tarfile.open(path, 'r:*') as archive:
        for member in archive:
            if member.isfile():
                with archive.extractfile(member) as data:
                    while data.read(1024 * 1024): pass
        manifest = json.loads(read_small(archive, 'manifest.json'))
        if not isinstance(manifest, list) or len(manifest) != 1 or not isinstance(manifest[0], dict):
            raise ValueError('镜像包必须包含唯一 Docker 镜像')
        image = manifest[0]
        if reference not in (image.get('RepoTags') or []):
            raise ValueError('Docker 镜像引用不匹配: ' + reference)
        layers = image.get('Layers')
        if not isinstance(layers, list) or not layers or any(not archive.getmember(layer).isfile() for layer in layers):
            raise ValueError('缺少 Docker 镜像层')
        config = read_small(archive, image['Config'])
        if not isinstance(json.loads(config), dict):
            raise ValueError('非法 Docker 镜像配置')
        image_id = 'sha256:' + hashlib.sha256(config).hexdigest()
        if expected_id and image_id != expected_id:
            raise ValueError('镜像包 ID 与本次 DinD 中的目标镜像不一致')
        return image_id


def collect_image(meta):
    path = image_archive_path()
    reference = os.environ['XRAN_IMAGE_REFERENCE']
    meta['imageReference'] = reference
    meta['pushStatus'] = 'success' if meta.get('result') == 'SUCCESS' else 'failed-or-not-reached'
    if not path.exists() and not path.is_symlink() and meta.get('result') == 'ABORTED':
        return None
    probe = subprocess.run(['docker', 'image', 'inspect', reference], capture_output=True, text=True)
    info = json.loads(probe.stdout)[0] if probe.returncode == 0 else None
    if path.exists() or path.is_symlink():
        image_id = validate_image_archive(path, reference, info['Id'] if info else None)
        origin = 'script'
    else:
        if not info:
            raise ValueError('缺少镜像包，且本次 DinD 中找不到镜像 ' + reference +
                             '；请核对 registry、imageRepositories 和 tag')
        if not path.parent.is_dir():
            raise ValueError('缺少镜像产物目录: ' + str(path.parent))
        print('缺少镜像包，自动从本次 DinD 导出: ' + reference, flush=True)
        descriptor, name = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=path.parent)
        os.close(descriptor)
        temporary = Path(name)
        try:
            run(['docker', 'save', '--output', str(temporary), reference])
            image_id = validate_image_archive(temporary, reference, info['Id'])
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        origin = 'docker-save'
    meta.update(imageId=image_id, imageArchiveSource=origin, repoDigests=info.get('RepoDigests', []) if info else [])
    return path


def collect_release():
    import yaml
    PUBLISH.mkdir(exist_ok=True)
    meta = json.loads((ROOT / 'checkout.json').read_text()) if (ROOT / 'checkout.json').exists() else {}
    meta.update(json.loads(os.environ['XRAN_BUILD_METADATA']))
    chart, app = os.environ['XRAN_CHART_VERSION'], os.environ['XRAN_IMAGE_TAG']
    registry = os.environ['XRAN_REGISTRY']
    pushes = json.loads((ROOT / 'release-push.json').read_text()) if (ROOT / 'release-push.json').exists() else {}
    collected, errors, images = [], [], []
    spec = project_recipe('xcn')
    directory = inside_source(spec.get('workdir', 'helm'))
    names = spec.get('images', ['xcn-runtime.tar', 'xcn-webui.tar', 'xcn-monitor.tar']) + [spec.get('chart', 'xcn-{chartVersion}.tgz').format(chartVersion=chart)]
    for name in names:
        path = directory / relative(name)
        if not path.exists():
            if meta.get('result') == 'SUCCESS': errors.append('缺少产物: ' + name)
            continue
        try:
            if path.is_symlink() or not path.is_file(): raise ValueError('非法产物路径')
            with tarfile.open(path, 'r:*') as archive:
                for member in archive:
                    if member.isfile():
                        with archive.extractfile(member) as data:
                            while data.read(1024 * 1024): pass
                if name.endswith('.tgz'):
                    info = yaml.safe_load(read_small(archive, 'xcn/Chart.yaml'))
                    values = yaml.safe_load(read_small(archive, 'xcn/values.yaml'))
                    if (info.get('name'), info.get('version'), info.get('appVersion')) != ('xcn', chart, app):
                        raise ValueError('Chart 名称或版本不匹配')
                    tags = [values['images'][key]['tag'] for key in ['runtime', 'webui']] + [values['monitor']['image']['tag']]
                    if tags != [app] * 3: raise ValueError('Chart 默认镜像标签不匹配')
                else:
                    reference = f'{registry}/xcn/{name[:-4]}:{app}'
                    manifest = json.loads(read_small(archive, 'manifest.json'))
                    if len(manifest) != 1 or reference not in (manifest[0].get('RepoTags') or []):
                        raise ValueError('Docker 镜像引用不匹配')
                    layers = manifest[0].get('Layers')
                    if not isinstance(layers, list) or not layers or any(not archive.getmember(layer).isfile() for layer in layers):
                        raise ValueError('缺少 Docker 镜像层')
                    image_id = 'sha256:' + hashlib.sha256(read_small(archive, manifest[0]['Config'])).hexdigest()
                    images.append({'name': name[:-4], 'reference': reference, 'imageId': image_id,
                                   'pushStatus': pushes.get(reference, 'unknown')})
            out = PUBLISH / name
            shutil.move(str(path), out)
            digest = hashlib.sha256()
            with out.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''): digest.update(block)
            collected.append({'name': name, 'size': out.stat().st_size, 'sha256': digest.hexdigest()})
        except (ValueError, KeyError, TypeError, tarfile.TarError, EOFError, OSError, yaml.YAMLError) as error:
            errors.append(name + ': ' + str(error))
    if errors: meta['result'] = 'FAILURE'
    warnings = ['镜像推送未全部成功；有效本地包仍可下载'] if any(i['pushStatus'] != 'success' for i in images) else []
    meta.update(files=collected, images=images, chartVersion=chart, appVersion=app,
                offline=os.environ['XRAN_OFFLINE'] == 'true', collectionErrors=errors, warnings=warnings,
                finishedAt=dt.datetime.now(dt.timezone.utc).isoformat())
    (PUBLISH / 'metadata.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2) + '\n')
    (PUBLISH / 'SHA256SUMS').write_text(''.join(f'{x["sha256"]}  {x["name"]}\n' for x in collected))
    (ROOT / 'collection.json').write_text(json.dumps({'bytes': sum(p.stat().st_size for p in PUBLISH.iterdir()),
        'errors': errors, 'files': len(collected), 'warnings': warnings}))
    if errors: print('\n'.join(errors), file=sys.stderr)
    return 1 if errors else 0


def collect():
    """即使 push 失败，也只发布通过 tar 完整性检查的已有文件。"""
    if os.environ['XRAN_KIND'] in ('xcn', 'open5gs'):
        return collect_release()
    PUBLISH.mkdir(exist_ok=True)
    meta = json.loads((ROOT / 'checkout.json').read_text()) if (ROOT / 'checkout.json').exists() else {}
    meta.update(json.loads(os.environ['XRAN_BUILD_METADATA']))
    kind, target = os.environ['XRAN_KIND'], os.environ['XRAN_TARGET']
    generic = project_spec().get('adapter') == 'command'
    spec = project_recipe(kind)
    directory = inside_source(spec.get('artifactDirectory', 'JDBG/Release/services' if kind == 'service' else 'platform/k8s/images-build'))
    files = ([inside_source(name) for name in command_recipe()['artifacts']] if generic else
        sorted(directory.glob(spec.get('bundlePattern', 'L2L3_*.tar.gz') if target == 'services_all' else spec.get('artifact', '{target}.tar.gz').format(target=target)))
        if kind == 'service' and directory.exists() else [])
    collected = []
    errors = []
    if kind == 'image':
        try:
            candidate = collect_image(meta)
            if candidate is not None: files = [candidate]
        except (ValueError, KeyError, TypeError, IndexError, tarfile.TarError, EOFError, OSError,
                subprocess.CalledProcessError) as error:
            errors.append('镜像产物: ' + str(error))
    for path in files:
        if not path.is_file():
            if generic and meta.get('result') == 'SUCCESS': errors.append('缺少预期产物: ' + path.name)
            continue
        if path.is_symlink() or not re.fullmatch(r'[A-Za-z0-9_.-]+', path.name):
            errors.append('非法产物路径: ' + path.name)
            continue
        try:
            if kind == 'service':
                with tarfile.open(path, 'r:*') as archive:
                    # 不提取内容，逐个读取验证压缩流完整性。
                    content_files = 0
                    for entry in archive:
                        if entry.isfile():
                            content_files += 1
                            with archive.extractfile(entry) as data:
                                while data.read(1024 * 1024): pass
                    if generic and not content_files:
                        raise tarfile.ReadError('服务包不能为空')
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
    if generic and meta.get('result') == 'SUCCESS' and len(collected) != len(command_recipe()['artifacts']):
        errors.append('服务产物数量不符合配置')
    if kind == 'service' and not generic and meta.get('result') == 'SUCCESS':
        if target == 'all':
            errors.append('服务目标 all 已取消，请选择单个服务或 services_all')
        expected = {spec.get('artifact', '{target}.tar.gz').format(target=target).removesuffix('.tar.gz')} if target != 'services_all' else None
        actual = {x['name'].removesuffix('.tar.gz') for x in collected}
        if expected is not None and expected != actual:
            errors.append('服务产物集合不符合目标')
        pattern = spec.get('bundleNameRegex', r'L2L3_\d{10}_{commit9}\.tar\.gz').replace('{commit9}', meta.get('commit', '')[:9])
        if target == 'services_all' and (len(collected) != 1 or not re.fullmatch(pattern, collected[0]['name'])):
            errors.append('services_all 产物名称或数量不符合约定')
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
    commands = {'checkout': checkout, 'build': build, 'collect': collect, 'capabilities': capabilities}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        raise SystemExit('usage: run-build.py checkout|build|collect|capabilities')
    raise SystemExit(commands[sys.argv[1]]() or 0)
