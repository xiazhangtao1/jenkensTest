#!/usr/bin/env python3
"""Ubuntu 22.04: prepare / images / render / import-images / deploy / bundle / backup / restore."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile

import yaml

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / '.cache'


def run(args, **kwargs):
    print('+ ' + ' '.join(str(x) for x in args), flush=True)
    return subprocess.run([str(x) for x in args], check=True, **kwargs)


def output(args, **kwargs):
    return subprocess.check_output([str(x) for x in args], text=True, **kwargs).strip()


def kube_env():
    # kube-apiserver/Service 不经过外网代理，包括操作者现有 clashctl 环境。
    env = os.environ.copy()
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        env.pop(name, None)
    return env


def kubectl(*args, capture=False):
    command = ['kubectl', '--request-timeout=30s', *args]
    return output(command, env=kube_env()) if capture else run(command, env=kube_env())


def versions():
    return dict(line.split('=', 1) for line in (ROOT / 'config/versions.env').read_text().splitlines()
                if line and not line.startswith('#'))


def config(args):
    cfg = yaml.safe_load(Path(args.site).read_text())
    if args.namespace:
        cfg['namespace'] = args.namespace
    if args.offline:
        cfg['offline'] = True
    assert 1 <= int(cfg['concurrency']) <= 32
    assert 1 <= int(cfg['temporaryStorageGiB']) <= 1024
    assert 0 <= float(cfg['minimumFreePercent']) < 80
    v = versions()
    cfg.update(builderImage=v['BUILDER_IMAGE'], controllerImage=v['CONTROLLER_IMAGE'],
               agentImage=v['AGENT_IMAGE'], dindImage=v['DIND_IMAGE'],
               demoImage=v['DEMO_IMAGE'], registryImage=v['REGISTRY_IMAGE'])
    image_lock = ROOT / 'config/images.lock.json'
    cfg['imageIds'] = json.loads(image_lock.read_text()) if image_lock.exists() else {}
    return cfg


def state_dir(cfg):
    path = ROOT / '.state' / cfg['namespace']
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def hash_file(path):
    sha = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            sha.update(chunk)
    return sha.hexdigest()


def prepare(args):
    v = versions()
    for name in ('images', 'plugins', 'charts', 'tools', 'apt-archives', 'plugin-manager-cache'):
        (CACHE / name).mkdir(parents=True, exist_ok=True)
    for ref in [v[x] for x in ['JENKINS_BASE', 'AGENT_IMAGE', 'DOCKER_CLI_IMAGE', 'DIND_IMAGE', 'REGISTRY_IMAGE', 'UBUNTU_IMAGE', 'LOCAL_PATH_IMAGE', 'STORAGE_HELPER_IMAGE']]:
        exists = subprocess.run(['docker', 'image', 'inspect', ref], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL).returncode == 0
        if not exists:
            archive = CACHE / 'images' / (ref.replace('/', '_').replace(':', '_') + '.tar')
            # 此通道使用操作者已有的代理，且逐层校验官方 Docker Hub 内容。
            run([sys.executable, ROOT / 'scripts/fetch-image.py', ref, archive])
            run(['docker', 'load', '-i', archive])
    chart = CACHE / 'charts' / f'jenkins-{v["JENKINS_CHART_VERSION"]}.tgz'
    if not chart.exists():
        run(['helm', 'pull', f'https://github.com/jenkinsci/helm-charts/releases/download/jenkins-{v["JENKINS_CHART_VERSION"]}/{chart.name}',
             '--destination', chart.parent])
    lock = ROOT / 'config/plugins.lock'
    pinned = lock.read_text().splitlines() if lock.exists() else []
    pinned_names = {line.split(':', 1)[0] for line in pinned}
    requested = [line.strip() for line in (ROOT / 'config/plugins.txt').read_text().splitlines()
                 if line.strip() and not line.startswith('#')]
    added = [line for line in requested if line.split(':', 1)[0] not in pinned_names]
    plugins = CACHE / 'plugins-requested.txt'
    plugins.write_text('\n'.join(pinned + added) + '\n')
    if added or not (CACHE / 'plugins.complete').exists():
        proxy = os.environ.get('HTTPS_PROXY', os.environ.get('https_proxy', ''))
        java_opts = '-Duser.home=/tmp -Dsun.net.client.defaultConnectTimeout=15000 -Dsun.net.client.defaultReadTimeout=30000'
        if proxy:
            from urllib.parse import urlsplit
            url = urlsplit(proxy)
            java_opts += f' -Dhttps.proxyHost={url.hostname} -Dhttps.proxyPort={url.port or 80} -Dhttp.proxyHost={url.hostname} -Dhttp.proxyPort={url.port or 80}'
        opts = ['-e', 'JAVA_OPTS=' + java_opts]
        with (CACHE / 'plugins-resolved.log').open('w') as log:
            run(['docker', 'run', '--rm', '--network', 'host', '--user', f'{os.getuid()}:{os.getgid()}',
                 *opts, '-v', f'{CACHE / "plugins"}:/usr/share/jenkins/ref/plugins',
                 '-v', f'{CACHE / "plugin-manager-cache"}:/tmp/.cache',
                 '-v', f'{plugins}:/tmp/plugins.txt:ro', '--entrypoint', 'jenkins-plugin-cli',
                 v['JENKINS_BASE'], '--plugin-file', '/tmp/plugins.txt', '--no-download', '--list',
                 '--latest=false' if lock.exists() else '--latest=true'], stdout=log, stderr=log)
        run([sys.executable, ROOT / 'scripts/cache-plugins.py'])
    lock_plugins()
    for binary in ('helm', 'kubectl'):
        if Path(shutil.which(binary)).resolve() != (CACHE / 'tools' / binary).resolve():
            shutil.copy2(shutil.which(binary), CACHE / 'tools' / binary)
    if shutil.which('ctr'):
        shutil.copy2(shutil.which('ctr'), CACHE / 'tools/ctr')
    print('准备完成；执行 images 构建自定义镜像。')


def lock_plugins():
    if not (CACHE / 'plugins.complete').exists():
        raise RuntimeError('插件下载尚未全部完成；执行 prepare 并检查返回状态')
    plugins = []
    for path in sorted((CACHE / 'plugins').glob('*.jpi')):
        with zipfile.ZipFile(path) as archive:
            text = archive.read('META-INF/MANIFEST.MF').decode().replace('\r\n ', '').replace('\r\n', '\n')
            fields = dict(line.split(': ', 1) for line in text.splitlines() if ': ' in line)
        plugins.append(f'{fields["Short-Name"]}:{fields["Plugin-Version"]}')
    if not plugins:
        raise RuntimeError('插件缓存为空')
    requested = {line.strip().split(':', 1)[0] for line in (ROOT / 'config/plugins.txt').read_text().splitlines()
                 if line.strip() and not line.startswith('#')}
    missing = requested - {line.split(':', 1)[0] for line in plugins}
    if missing:
        raise RuntimeError('缺少请求的插件，请先执行 prepare：' + ', '.join(sorted(missing)))
    (ROOT / 'config/plugins.lock').write_text('\n'.join(sorted(plugins)) + '\n')


def images(args):
    v = versions()
    base_images = [line.strip() for line in (ROOT / 'config/base-images.txt').read_text().splitlines()
                   if line.strip() and not line.startswith('#')]
    run(['docker', 'save', '-o', CACHE / 'base-images.tar', *base_images])
    proxy_args = []
    for name in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'no_proxy'):
        if os.environ.get(name):
            proxy_args += ['--build-arg', name]
    build_env = os.environ.copy()
    build_env['DOCKER_BUILDKIT'] = '0'
    build = ['docker', 'build', '--network', args.build_network, '--pull=false']
    run([*build, *proxy_args, '--build-arg', f'APT_SOURCES={args.apt_sources}',
         '--build-arg', f'DOCKER_CLI_IMAGE={v["DOCKER_CLI_IMAGE"]}',
         '-f', ROOT / 'docker/builder.Dockerfile', '-t', v['BUILDER_IMAGE'], ROOT], env=build_env)
    lock_plugins()
    run([*build, '--build-arg', f'JENKINS_BASE={v["JENKINS_BASE"]}',
         '-f', ROOT / 'docker/controller.Dockerfile', '-t', v['CONTROLLER_IMAGE'], ROOT], env=build_env)
    mirror = CACHE / 'demo.git'
    if mirror.exists():
        shutil.rmtree(mirror)
    run(['git', 'clone', '--mirror', '--no-hardlinks', args.source, mirror])
    run(['git', '-C', mirror, 'update-server-info'])
    run([*build, '--build-arg', f'BUILDER_IMAGE={v["BUILDER_IMAGE"]}',
         '-f', ROOT / 'docker/demo.Dockerfile', '-t', v['DEMO_IMAGE'], ROOT], env=build_env)
    refs = runtime_images(config(args))
    records = {ref: json.loads(output(['docker', 'image', 'inspect', ref]))[0]['Id'] for ref in refs}
    (ROOT / 'config/images.lock.json').write_text(json.dumps(records, indent=2) + '\n')
    run(['docker', 'save', '-o', CACHE / 'k8s-images.tar', *refs])


def runtime_images(cfg):
    refs = [cfg[x] for x in ['controllerImage', 'builderImage', 'agentImage', 'dindImage']]
    if cfg.get('demo'):
        refs += [cfg['demoImage'], cfg['registryImage']]
    refs += [versions()[key] for key in ['LOCAL_PATH_IMAGE', 'STORAGE_HELPER_IMAGE']]
    return list(dict.fromkeys(refs))


def pod_template(cfg, kind):
    maximum = int(cfg['temporaryStorageGiB']) * 1024
    cpu = int(cfg.get('buildCpuLimit', 4)) * 1000
    memory = int(cfg.get('buildMemoryGiB', 8)) * 1024
    if cpu < 2000 or memory < 2048:
        raise ValueError('包含 Agent/DinD 的总预算至少 2 CPU / 2Gi 内存')
    if maximum < 1024:
        raise ValueError('临时空间应至少 1Gi')
    shared = {'name': 'workspace-volume', 'mountPath': '/home/jenkins/agent'}
    proxy = cfg.get('proxy', '')
    bypass = cfg.get('noProxy', '') + ',xran-demo,xran-registry,jenkins,.svc,.cluster.local'
    env = [{'name': n, 'value': proxy} for n in ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy']]
    env += [{'name': n, 'value': bypass} for n in ['NO_PROXY', 'no_proxy']]
    jnlp = {'name': 'jnlp', 'image': cfg['agentImage'], 'workingDir': '/home/jenkins/agent',
        'resources': {'requests': {'cpu': '100m', 'memory': '256Mi', 'ephemeral-storage': '64Mi'},
                      'limits': {'cpu': '250m', 'memory': '512Mi', 'ephemeral-storage': '256Mi'}},
        'volumeMounts': [shared]}
    image = kind == 'image'
    builder = {'name': 'builder', 'image': cfg['builderImage'], 'workingDir': '/home/jenkins/agent',
        'command': ['sleep', 'infinity'], 'env': env,
        'resources': {'requests': {'cpu': '500m', 'memory': f'{min(1024, memory - (1536 if image else 512))}Mi',
            'ephemeral-storage': f'{min(1024, maximum // 2 if image else maximum - 256)}Mi'},
            'limits': {'cpu': f'{cpu - (1500 if image else 250)}m', 'memory': f'{memory - (1536 if image else 512)}Mi',
                       'ephemeral-storage': f'{maximum // 2 if image else maximum - 256}Mi'}},
        'securityContext': {'runAsUser': 1000, 'runAsGroup': 1000, 'allowPrivilegeEscalation': False},
        'volumeMounts': [dict(shared), {'name': 'build-trust', 'mountPath': '/home/jenkins/.ssh', 'readOnly': True}]}
    volumes = [{'name': 'workspace-volume', 'emptyDir': {'sizeLimit': f'{cfg["temporaryStorageGiB"]}Gi'}},
               {'name': 'build-trust', 'secret': {'secretName': 'xran-build-trust', 'optional': True}}]
    containers = [jnlp, builder]
    if image:
        builder['volumeMounts'].append({'name': 'docker-run', 'mountPath': '/var/run/xran'})
        arguments = ['dockerd', '--host=unix:///var/run/xran/docker.sock', '--group=1000', '--storage-driver=overlay2']
        if cfg.get('registryInsecure'):
            arguments += ['--insecure-registry=' + cfg['registry']]
        dind = {'name': 'dind', 'image': cfg['dindImage'], 'command': ['dockerd-entrypoint.sh'], 'args': arguments,
            'env': [{'name': 'DOCKER_TLS_CERTDIR', 'value': ''}],
            'securityContext': {'privileged': True, 'runAsUser': 0, 'runAsGroup': 0},
            'resources': {'requests': {'cpu': '250m', 'memory': '256Mi',
                'ephemeral-storage': f'{min(1024, maximum - maximum // 2 - 256)}Mi'},
                'limits': {'cpu': '1250m', 'memory': '1Gi', 'ephemeral-storage': f'{maximum - maximum // 2 - 256}Mi'}},
            'volumeMounts': [{'name': 'docker-run', 'mountPath': '/var/run/xran'},
                             {'name': 'docker-data', 'mountPath': '/var/lib/docker'}]}
        containers.append(dind)
        volumes += [{'name': 'docker-run', 'emptyDir': {}},
                    {'name': 'docker-data', 'emptyDir': {'sizeLimit': f'{cfg["temporaryStorageGiB"]}Gi'}}]
    if cfg.get('caSecret'):
        volumes.append({'name': 'registry-ca', 'secret': {'secretName': cfg['caSecret']}})
        builder['volumeMounts'].append({'name': 'registry-ca', 'mountPath': '/var/xran/ca', 'readOnly': True})
        builder['env'] += [{'name': key, 'value': '/var/xran/ca/ca.crt'} for key in ['GIT_SSL_CAINFO', 'CURL_CA_BUNDLE']]
        if image:
            dind['volumeMounts'].append({'name': 'registry-ca', 'mountPath': '/etc/docker/certs.d/' + cfg['registry'], 'readOnly': True})
    for container in containers:
        container['imagePullPolicy'] = 'Never' if cfg.get('offline') else 'IfNotPresent'
    return {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'labels': {'xran-ci': 'build', 'xran-build': 'bXRAN_BUILD_NUMBER'}},
        'spec': {'automountServiceAccountToken': False,
            'securityContext': {'runAsUser': 1000, 'runAsGroup': 1000, 'fsGroup': 1000},
            'activeDeadlineSeconds': int(cfg['buildTimeoutMinutes']) * 60,
            'containers': containers, 'volumes': volumes}}


def render(args):
    cfg = config(args)
    directory = state_dir(cfg)
    namespace = cfg['namespace']
    manifests = [{'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': namespace,
        'labels': {'pod-security.kubernetes.io/enforce': 'privileged'}}},
        {'apiVersion': 'storage.k8s.io/v1', 'kind': 'StorageClass',
         'metadata': {'name': cfg['storageClass']}, 'provisioner': 'rancher.io/local-path',
         'reclaimPolicy': 'Retain', 'volumeBindingMode': 'WaitForFirstConsumer'}]
    runtime = {'site.json': json.dumps(cfg, ensure_ascii=False, indent=2),
               'pod-service.yaml': yaml.safe_dump(pod_template(cfg, 'service')),
               'pod-image.yaml': yaml.safe_dump(pod_template(cfg, 'image'))}
    manifests.append({'apiVersion': 'v1', 'kind': 'ConfigMap',
        'metadata': {'name': 'xran-config', 'namespace': namespace}, 'data': runtime})
    if cfg.get('offline'):
        manifests.append({'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy',
            'metadata': {'name': 'xran-offline-egress', 'namespace': namespace},
            'spec': {'podSelector': {}, 'policyTypes': ['Egress'], 'egress': [
                {'to': [{'podSelector': {}}]},
                {'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'kube-system'}}}],
                 'ports': [{'protocol': 'UDP', 'port': 53}, {'protocol': 'TCP', 'port': 53}]},
                {'to': [{'ipBlock': {'cidr': subnet}} for subnet in ['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16']]}
            ]}})
    if cfg.get('demo'):
        manifests.extend(demo_manifests(cfg))
    (directory / 'resources.yaml').write_text(yaml.safe_dump_all(manifests, sort_keys=False))
    realm = {'local': {'allowsSignup': False, 'users': [
        {'id': name, 'name': display, 'password': '${' + password + '}'} for name, display, password in [
            ('admin', 'CI Administrator', 'ADMIN_PASSWORD'), ('builder1', 'Builder One', 'BUILDER1_PASSWORD'),
            ('builder2', 'Builder Two', 'BUILDER2_PASSWORD')]]}}
    authorization = {'projectMatrix': {'entries': [
        {'user': {'name': 'admin', 'permissions': ['Overall/Administer']}},
        {'group': {'name': 'authenticated', 'permissions': ['Overall/Read', 'Job/Read', 'Job/Build', 'Job/Cancel', 'View/Read']}}
    ]}}
    values = {'fullnameOverride': 'jenkins', 'controller': {
        'image': {'registry': 'docker.io', 'repository': cfg['controllerImage'].split(':')[0],
                  'tag': cfg['controllerImage'].rsplit(':', 1)[1], 'pullPolicy': 'Never' if cfg.get('offline') else 'IfNotPresent'},
        'numExecutors': 0, 'installPlugins': False, 'additionalPlugins': [],
        'podAnnotations': {'xran-ci/image-id': cfg['imageIds'].get(cfg['controllerImage'], 'unlocked')},
        'sidecars': {'configAutoReload': {'enabled': False}}, 'initializeOnce': False,
        'testEnabled': False, 'agentListenerEnabled': False,
        'serviceType': 'NodePort', 'nodePort': int(cfg['nodePort']), 'jenkinsUrl': cfg['jenkinsUrl'],
        'resources': {'requests': {'cpu': '500m', 'memory': '1Gi'}, 'limits': {'cpu': '2', 'memory': '4Gi'}},
        'javaOpts': '-Xms512m -Xmx2g -Djenkins.install.runSetupWizard=false -Duser.timezone=Asia/Shanghai',
        'admin': {'createSecret': False, 'existingSecret': 'xran-users', 'userKey': 'ADMIN_USER', 'passwordKey': 'ADMIN_PASSWORD'},
        'containerEnvFrom': [{'secretRef': {'name': 'xran-users'}}],
        'containerEnv': [{'name': 'PLUGINS_FORCE_UPGRADE', 'value': 'true'}],
        'JCasC': {'defaultConfig': True, 'securityRealm': yaml.safe_dump(realm),
            'authorizationStrategy': yaml.safe_dump(authorization), 'configScripts': {'xran': yaml.safe_dump({
                'jenkins': {'systemMessage': 'X-RAN-CLOUD CI：历史包持续保存；磁盘压力时回收最早未保留产物。清理明细见 storage-maintenance。'},
                'appearance': {'simpleTheme': {'elements': [{'jsUrl': {
                    'url': cfg['jenkinsUrl'].rstrip('/') + '/userContent/xran-parameters.js?v=' +
                           hash_file(ROOT / 'jenkins/parameters.js')[:12]}}]}}})}}},
        'persistence': {'enabled': True, 'size': cfg['pvcSize'], 'storageClass': cfg['storageClass'],
            'annotations': {'helm.sh/resource-policy': 'keep'},
            'volumes': [{'name': 'xran-config', 'configMap': {'name': 'xran-config'}}],
            'mounts': [{'name': 'xran-config', 'mountPath': '/var/xran', 'readOnly': True}]},
        'agent': {'websocket': True, 'containerCap': int(cfg['concurrency']),
            'image': {'repository': cfg['agentImage'].split(':')[0], 'tag': cfg['agentImage'].rsplit(':', 1)[1]},
            'alwaysPullImage': False}, 'rbac': {'create': True}}
    (directory / 'values.yaml').write_text(yaml.safe_dump(values, sort_keys=False))
    print(directory)
    return cfg


def demo_manifests(cfg):
    ns = cfg['namespace']
    result = []
    for name, image, port, volume in [('xran-demo', cfg['demoImage'], 8000, False),
                                      ('xran-registry', cfg['registryImage'], 5000, True)]:
        if volume:
            result.append({'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim',
                'metadata': {'name': name, 'namespace': ns, 'annotations': {'helm.sh/resource-policy': 'keep'}},
                'spec': {'accessModes': ['ReadWriteOnce'], 'storageClassName': cfg['storageClass'],
                         'resources': {'requests': {'storage': '20Gi'}}}})
        container = {'name': name, 'image': image, 'imagePullPolicy': 'Never' if cfg.get('offline') else 'IfNotPresent',
            'ports': [{'containerPort': port}], 'resources': {'requests': {'cpu': '50m', 'memory': '64Mi'},
                'limits': {'cpu': '500m', 'memory': '512Mi'}},
            'readinessProbe': {'httpGet': {'path': '/v2/' if volume else '/X-RAN-CLOUD.git/HEAD', 'port': port}, 'initialDelaySeconds': 2}}
        spec = {'automountServiceAccountToken': False, 'containers': [container]}
        if volume:
            container['volumeMounts'] = [{'name': 'data', 'mountPath': '/var/lib/registry'}]
            container['env'] = [{'name': 'REGISTRY_STORAGE_DELETE_ENABLED', 'value': 'true'}]
            spec['volumes'] = [{'name': 'data', 'persistentVolumeClaim': {'claimName': name}}]
        result += [{'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': {'name': name, 'namespace': ns},
            'spec': {'replicas': 1, 'strategy': {'type': 'Recreate'}, 'selector': {'matchLabels': {'app': name}},
                     'template': {'metadata': {'labels': {'app': name},
                         'annotations': {'xran-ci/image-id': cfg['imageIds'].get(image, 'unlocked')}}, 'spec': spec}}},
            {'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'name': name, 'namespace': ns},
             'spec': {'selector': {'app': name}, 'ports': [{'port': port, 'targetPort': port}]}}]
    return result


def create_namespace(cfg):
    manifest = {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': cfg['namespace'],
        'labels': {'pod-security.kubernetes.io/enforce': 'privileged'}}}
    subprocess.run(['kubectl', 'apply', '-f', '-'], input=yaml.safe_dump(manifest), text=True, check=True, env=kube_env())


def import_images(args):
    cfg = config(args)
    archive = CACHE / 'k8s-images.tar'
    if not archive.exists():
        raise RuntimeError('先执行 images 或从离线包导入 k8s-images.tar')
    # 操作者可以使用 sudo，但本项目不要求将密码交给脚本。
    direct = subprocess.run(['sudo', '-n', 'ctr', '-n', 'k8s.io', 'images', 'ls', '-q'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if direct.returncode == 0:
        run(['sudo', '-n', 'ctr', '-n', 'k8s.io', 'images', 'import', archive])
        return
    create_namespace(cfg)
    if not (CACHE / 'tools/ctr').exists():
        shutil.copy2('/usr/bin/ctr', CACHE / 'tools/ctr')
    pods = json.loads(kubectl('get', 'pods', '-A', '-o', 'json', capture=True))
    images = {c['image'] for p in pods['items'] if p['status'].get('phase') == 'Running' for c in p['spec']['containers']}
    bootstrap = args.importer_image or next((image for image in sorted(images) if 'xcn-runtime' in image), None)
    if not bootstrap:
        raise RuntimeError('无 sudo 权限；请用 --importer-image 指定节点已缓存、含 glibc 的工具镜像，或由管理员执行 sudo ctr 导入。')
    nodes = json.loads(kubectl('get', 'nodes', '-o', 'json', capture=True))['items']
    if len(nodes) != 1:
        raise RuntimeError('导入 Pod 模式仅用于本机单节点验证；多节点请将归档复制到每个构建节点执行 sudo ctr 导入。')
    name = 'xran-image-import'
    pod = {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': name, 'namespace': cfg['namespace']},
        'spec': {'nodeName': nodes[0]['metadata']['name'], 'restartPolicy': 'Never', 'automountServiceAccountToken': False,
            'containers': [{'name': 'import', 'image': bootstrap, 'imagePullPolicy': 'Never',
                'command': ['/import/tools/ctr', '--address', '/runtime/containerd.sock', '-n', 'k8s.io', 'images', 'import', '/import/k8s-images.tar'],
                'securityContext': {'runAsUser': 0, 'privileged': True},
                'volumeMounts': [{'name': 'archive', 'mountPath': '/import', 'readOnly': True},
                                {'name': 'runtime', 'mountPath': '/runtime/containerd.sock'}]}],
            'volumes': [{'name': 'archive', 'hostPath': {'path': str(CACHE), 'type': 'Directory'}},
                        {'name': 'runtime', 'hostPath': {'path': '/run/containerd/containerd.sock', 'type': 'Socket'}}]}}
    kubectl('-n', cfg['namespace'], 'delete', 'pod', name, '--ignore-not-found', '--wait=true')
    subprocess.run(['kubectl', 'apply', '-f', '-'], input=yaml.safe_dump(pod), text=True, check=True, env=kube_env())
    deadline = time.time() + 600
    try:
        while time.time() < deadline:
            current = json.loads(kubectl('-n', cfg['namespace'], 'get', 'pod', name, '-o', 'json', capture=True))
            phase = current['status'].get('phase')
            if phase == 'Succeeded':
                print(kubectl('-n', cfg['namespace'], 'logs', name, capture=True))
                return
            if phase == 'Failed':
                raise RuntimeError(kubectl('-n', cfg['namespace'], 'logs', name, capture=True))
            time.sleep(3)
        raise RuntimeError('镜像导入超时；检查 importer Pod 日志')
    finally:
        kubectl('-n', cfg['namespace'], 'delete', 'pod', name, '--ignore-not-found', '--wait=false')


def users_secret(cfg):
    directory = state_dir(cfg)
    path = directory / 'users.json'
    try:
        existing = json.loads(kubectl('-n', cfg['namespace'], 'get', 'secret', 'xran-users', '-o', 'json', capture=True))
        users = {key: base64.b64decode(value).decode() for key, value in existing['data'].items()}
    except subprocess.CalledProcessError:
        users = json.loads(path.read_text()) if path.exists() else {
            'ADMIN_USER': 'admin', **{key: secrets.token_urlsafe(24) for key in ['ADMIN_PASSWORD', 'BUILDER1_PASSWORD', 'BUILDER2_PASSWORD']}}
        manifest = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'xran-users', 'namespace': cfg['namespace']},
                    'type': 'Opaque', 'stringData': users}
        subprocess.run(['kubectl', 'apply', '-f', '-'], input=yaml.safe_dump(manifest), text=True, check=True, env=kube_env())
    path.write_text(json.dumps(users, indent=2) + '\n')
    path.chmod(0o600)


def deploy(args):
    cfg = render(args)
    create_namespace(cfg)
    users_secret(cfg)
    directory = state_dir(cfg)
    kubectl('apply', '-f', directory / 'resources.yaml')
    chart = CACHE / 'charts' / f'jenkins-{versions()["JENKINS_CHART_VERSION"]}.tgz'
    run(['helm', 'upgrade', '--install', cfg['release'], chart, '-n', cfg['namespace'],
         '-f', directory / 'values.yaml', '--timeout', '12m'], env=kube_env())
    # 某些 CPU webhook 直接设置 nodeName，绕过 scheduler 的 WFFC 选节点阶段。
    # 只为本次 Jenkins PVC 补齐其实际 Pod 节点，保持 PV 亲和性一致。
    deadline = time.time() + 120
    while time.time() < deadline:
        pvc = json.loads(kubectl('-n', cfg['namespace'], 'get', 'pvc', 'jenkins', '-o', 'json', capture=True))
        if pvc['status'].get('phase') == 'Bound':
            break
        raw = kubectl('-n', cfg['namespace'], 'get', 'pod', 'jenkins-0', '--ignore-not-found', '-o', 'json', capture=True)
        if raw:
            pod = json.loads(raw)
            node = pod['spec'].get('nodeName')
            chosen = pvc['metadata'].get('annotations', {}).get('volume.kubernetes.io/selected-node')
            if node and not chosen:
                kubectl('-n', cfg['namespace'], 'annotate', 'pvc', 'jenkins', 'volume.kubernetes.io/selected-node=' + node)
        time.sleep(2)
    kubectl('-n', cfg['namespace'], 'rollout', 'status', 'statefulset/jenkins', '--timeout=600s')
    if cfg.get('demo'):
        for name in ['xran-demo', 'xran-registry']:
            kubectl('-n', cfg['namespace'], 'rollout', 'status', 'deployment/' + name, '--timeout=120s')
    print(f'Jenkins: {cfg["jenkinsUrl"]}\n账号密码保存在 {directory / "users.json"} (0600)。')


def bundle(args):
    cfg = config(args)
    v = versions()
    target = ROOT / 'dist/offline' / ('xran-jenkins-' + time.strftime('%Y%m%d-%H%M%S'))
    target.mkdir(parents=True)
    for name in ['config', 'docker', 'jenkins', 'runtime', 'scripts', 'tests', 'docs', 'vendor']:
        if (ROOT / name).exists():
            shutil.copytree(ROOT / name, target / name, ignore=shutil.ignore_patterns('__pycache__'))
    for name in ['README.md', 'Makefile', '.dockerignore', '.gitignore']:
        if (ROOT / name).exists():
            shutil.copy2(ROOT / name, target / name)
    for name in ['charts', 'plugins', 'tools']:
        shutil.copytree(CACHE / name, target / '.cache' / name)
    shutil.copy2(CACHE / 'base-images.tar', target / '.cache/base-images.tar')
    shutil.copy2(CACHE / 'plugins.complete', target / '.cache/plugins.complete')
    shutil.copytree(CACHE / 'apt-archives', target / '.cache/apt-archives', ignore=shutil.ignore_patterns('lock', 'partial'))
    shutil.copy2(CACHE / 'k8s-images.tar', target / '.cache/k8s-images.tar')
    shutil.copytree(CACHE / 'demo.git', target / '.cache/demo.git')
    bases = [v[key] for key in ['JENKINS_BASE', 'DOCKER_CLI_IMAGE', 'UBUNTU_IMAGE']]
    bases.extend(line.strip() for line in (ROOT / 'config/base-images.txt').read_text().splitlines()
                 if line.strip() and not line.startswith('#'))
    run(['docker', 'save', '-o', target / '.cache/build-base-images.tar', *dict.fromkeys(bases)])
    repos = target / 'repos'
    repos.mkdir()
    run(['git', '-C', args.source, 'bundle', 'create', repos / 'X-RAN-CLOUD.bundle', '--all'])
    run(['git', '-C', ROOT, 'bundle', 'create', repos / 'jenkensTest.bundle', '--all'])
    manifest = {'createdAt': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'platform': 'linux/amd64',
                'versions': v, 'runtimeImages': runtime_images(cfg), 'files': {}}
    for path in sorted(target.rglob('*')):
        if path.is_file():
            manifest['files'][str(path.relative_to(target))] = {'bytes': path.stat().st_size, 'sha256': hash_file(path)}
    (target / 'MANIFEST.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + '\n')
    (target / 'SHA256SUMS').write_text(''.join(f'{value["sha256"]}  {name}\n' for name, value in manifest['files'].items()) +
                                      f'{hash_file(target / "MANIFEST.json")}  MANIFEST.json\n')
    print('离线目录（不含运行凭据）：' + str(target))


def backup(args):
    cfg = config(args)
    target = Path(args.archive or ROOT / 'dist' / ('jenkins-home-' + time.strftime('%Y%m%d-%H%M%S') + '.tar.gz'))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch(mode=0o600, exist_ok=True)
    target.chmod(0o600)
    from jenkins_api import JenkinsAPI
    users = json.loads((state_dir(cfg) / 'users.json').read_text())
    api = JenkinsAPI(cfg['jenkinsUrl'], 'admin', users['ADMIN_PASSWORD'])
    api.post('quietDown')
    active = api.get('api/json?tree=jobs[name,builds[number,building]]')
    if any(b['building'] for job in active['jobs'] for b in job.get('builds', [])):
        api.post('cancelQuietDown')
        raise RuntimeError('仍有构建运行或等待锁，请完成/取消后再备份；已恢复接收新任务。')
    # 备份时停 Controller，避免 secrets/jobs XML/构建归档处于不同时间点。
    kubectl('-n', cfg['namespace'], 'scale', 'statefulset/jenkins', '--replicas=0')
    wait_controller_stopped(cfg)
    pod = maintenance_pod(cfg)
    subprocess.run(['kubectl', 'apply', '-f', '-'], input=yaml.safe_dump(pod), text=True, check=True, env=kube_env())
    try:
        kubectl('-n', cfg['namespace'], 'wait', '--for=condition=Ready', 'pod/xran-home-maintenance', '--timeout=120s')
        with target.open('wb') as stream:
            subprocess.run(['kubectl', '-n', cfg['namespace'], 'exec', 'xran-home-maintenance', '--',
                'tar', '-C', '/home-data', '-czf', '-', '.'], stdout=stream, check=True, env=kube_env())
        target.chmod(0o600)
        print('备份包含凭据和 secrets，请放到其他物理磁盘：' + str(target))
    finally:
        kubectl('-n', cfg['namespace'], 'delete', 'pod/xran-home-maintenance', '--ignore-not-found', '--wait=true')
        kubectl('-n', cfg['namespace'], 'scale', 'statefulset/jenkins', '--replicas=1')


def maintenance_pod(cfg, own_volume=False):
    pod = {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': 'xran-home-maintenance', 'namespace': cfg['namespace']},
        'spec': {'restartPolicy': 'Never', 'automountServiceAccountToken': False,
            'securityContext': {'runAsUser': 1000, 'runAsGroup': 1000, 'fsGroup': 1000},
            'containers': [{'name': 'tools', 'image': cfg['builderImage'],
                'imagePullPolicy': 'Never' if cfg.get('offline') else 'IfNotPresent',
                'command': ['sleep', 'infinity'], 'volumeMounts': [{'name': 'home', 'mountPath': '/home-data'}]}],
            'volumes': [{'name': 'home', 'persistentVolumeClaim': {'claimName': 'jenkins'}}]}}
    if own_volume:
        # local-path 卷根目录通常为 root 所有；仅初始化本次恢复卷的根目录。
        pod['spec']['initContainers'] = [{'name': 'volume-owner', 'image': cfg['builderImage'],
            'imagePullPolicy': 'Never' if cfg.get('offline') else 'IfNotPresent',
            'command': ['chown', '1000:1000', '/home-data'],
            'securityContext': {'runAsUser': 0, 'runAsGroup': 0, 'allowPrivilegeEscalation': False,
                'capabilities': {'drop': ['ALL'], 'add': ['CHOWN']}},
            'volumeMounts': [{'name': 'home', 'mountPath': '/home-data'}]}]
    return pod


def restore(args):
    if not args.archive:
        raise ValueError('restore 必须指定 --archive')
    cfg = config(args)
    kubectl('-n', cfg['namespace'], 'scale', 'statefulset/jenkins', '--replicas=0')
    wait_controller_stopped(cfg)
    subprocess.run(['kubectl', 'apply', '-f', '-'], input=yaml.safe_dump(maintenance_pod(cfg, own_volume=True)), text=True, check=True, env=kube_env())
    restored = False
    try:
        kubectl('-n', cfg['namespace'], 'wait', '--for=condition=Ready', 'pod/xran-home-maintenance', '--timeout=120s')
        # 仅允许恢复到空 PVC，防止混入旧任务和旧凭据。
        kubectl('-n', cfg['namespace'], 'exec', 'xran-home-maintenance', '--', 'sh', '-c',
                'test -z "$(find /home-data -mindepth 1 -maxdepth 1 -not -name lost+found -print -quit)"')
        with Path(args.archive).open('rb') as stream:
            subprocess.run(['kubectl', '-n', cfg['namespace'], 'exec', '-i', 'xran-home-maintenance', '--',
                'tar', '-C', '/home-data', '-xzf', '-', '--no-same-owner', '--no-overwrite-dir'],
                stdin=stream, check=True, env=kube_env())
        restored = True
    finally:
        kubectl('-n', cfg['namespace'], 'delete', 'pod/xran-home-maintenance', '--ignore-not-found', '--wait=true')
        if restored:
            kubectl('-n', cfg['namespace'], 'scale', 'statefulset/jenkins', '--replicas=1')
        else:
            print('恢复失败，Controller 保持 0 副本；请检查日志并使用新的空 PVC 重试。', file=sys.stderr)


def wait_controller_stopped(cfg):
    deadline = time.time() + 180
    while time.time() < deadline:
        current = kubectl('-n', cfg['namespace'], 'get', 'pod', 'jenkins-0', '--ignore-not-found', '-o', 'name', capture=True)
        if not current:
            return
        time.sleep(2)
    raise TimeoutError('Controller 停止超时')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'images', 'render', 'import-images', 'deploy', 'bundle', 'backup', 'restore'])
    parser.add_argument('--site', default=str(ROOT / 'config/site.yaml'))
    parser.add_argument('--source', default='/home/xiazhangtao/code/X-RAN-CLOUD')
    parser.add_argument('--namespace')
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--apt-sources', default='config/sources.online.list')
    parser.add_argument('--build-network', default='host', choices=['host', 'none', 'default'])
    parser.add_argument('--archive')
    parser.add_argument('--importer-image')
    arguments = parser.parse_args()
    os.chdir(ROOT)
    (ROOT / '.cache').mkdir(exist_ok=True)
    globals()[arguments.command.replace('-', '_')](arguments)
