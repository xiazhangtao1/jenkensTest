#!/usr/bin/env python3
"""Create a disposable demo Git image and a fresh, explicitly marked Jenkins."""
import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import ci

NAMESPACE = 'jenkins-reliability-verify'
DEMO_IMAGE = 'xran-ci/reliability-demo:1'
LEGACY_IMAGE = 'xran-ci/reliability-legacy:1'


def run(command, **kwargs):
    return subprocess.run(list(map(str, command)), check=True, **kwargs)


def git(folder, *arguments):
    return subprocess.check_output(['git', '-C', str(folder), *arguments], text=True).strip()


def verification_config(base, node_port):
    config=copy.deepcopy(base)
    host=urlsplit(config['jenkinsUrl']).hostname
    config.update(namespace=NAMESPACE, release='jenkins', nodePort=node_port,
                  jenkinsUrl=f'http://{host}:{node_port}/', storageClass='jenkins-reliability-local-path',
                  pvcSize='50Gi', verificationInstance=True, offline=True, demo=True,
                  compilerImagePullPolicy='Always',compilerImagePullSecrets=[],proxy='',
                  gitUrl='http://xran-demo:8000/X-RAN-CLOUD.git',gitCredentialsId='',
                  registry='xran-registry:5000',registryCredentialsId='',registryInsecure=True,
                  services=['serviceA','serviceB','serviceC'],images=['imageA','imageB','imageC'],
                  imageRepositories={name:'xran-demo/'+name.lower() for name in ('imageA','imageB','imageC')})
    config.pop('caSecret',None)
    config['xcn'].update(gitUrl='http://xran-demo:8000/xcn.git',gitCredentialsId='',offline=False,
                         registryCredentialsId='')
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', required=True)
    parser.add_argument('--base-site', default=str(ROOT/'config/site.yaml'),
                        help='现有站点配置；仅继承配置，验收 namespace/PVC/端口会独立创建')
    parser.add_argument('--source', default=str(ROOT.parent / 'X-RAN-CLOUD'))
    parser.add_argument('--xcn-source', default=ci.default_xcn_source())
    parser.add_argument('--node-port', type=int, default=32180)
    parser.add_argument('--compat-image', default='192.168.10.35/xnet/xran-compile:26.11.08-compat-r2')
    parser.add_argument('--sdk-image', default='192.168.10.35/xnet/xran-compile:26.11.08-sdk-r2')
    args = parser.parse_args()
    site = Path(args.site).resolve()
    if site.exists():
        raise RuntimeError('准备命令只创建新站点，拒绝覆盖已有文件：' + str(site))
    found = ci.kubectl('get', 'namespace', NAMESPACE, '--ignore-not-found', '-o', 'name', capture=True)
    if found:
        raise RuntimeError('验收 namespace 已存在，拒绝覆盖：' + NAMESPACE)
    services = json.loads(ci.kubectl('get', 'services', '-A', '-o', 'json', capture=True))
    if any(p.get('nodePort') == args.node_port for s in services['items'] for p in s['spec'].get('ports', [])):
        raise RuntimeError('验收 NodePort 已占用')
    folder = ROOT / '.state' / NAMESPACE
    folder.mkdir(parents=True, exist_ok=True)
    context = folder / 'fixture-context'
    context.mkdir()
    pins = {}
    for name, source in [('X-RAN-CLOUD', args.source), ('xcn', args.xcn_source)]:
        mirror = context / (name + '.git')
        run(['git', 'clone', '--mirror', '--no-hardlinks', source, mirror])
        pins[name] = git(mirror, 'rev-parse', 'HEAD')
    # An orphan fixture branch leaves every real business ref and source tree unchanged.
    work = folder / 'fixture-work'
    work.mkdir()
    run(['git', 'init', '-q', work])
    run(['git', '-C', work, 'checkout', '-qb', 'reliability-soak'])
    chart = work / 'helm/xcn'
    (chart / 'templates').mkdir(parents=True)
    (chart / 'Chart.yaml').write_text('apiVersion: v2\nname: xcn\nversion: 1.0.0\nappVersion: "1.0.0"\n')
    (chart / 'values.yaml').write_text('{}\n')
    (chart / 'templates/test.yaml').write_text('apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: reliability-fixture\ndata:\n  version: {{ .Chart.AppVersion | quote }}\n')
    script = work / 'helm/build-release.sh'
    shutil.copy2(ROOT / 'tests/fixtures/xcn-release.sh', script)
    script.chmod(0o755)
    run(['git', '-C', work, 'add', '.'])
    run(['git', '-C', work, '-c', 'user.name=Reliability fixture', '-c', 'user.email=fixture@example.invalid',
         'commit', '-qm', 'Isolated Docker and Helm reliability workload'])
    run(['git', '-C', work, 'push', str(context / 'xcn.git'), 'reliability-soak'])
    pins['soak'] = git(work, 'rev-parse', 'HEAD')
    for name in ('X-RAN-CLOUD', 'xcn'):
        run(['git', '-C', context / (name + '.git'), 'update-server-info'])
    (context / 'Dockerfile').write_text('FROM ' + args.compat_image + '\nCOPY X-RAN-CLOUD.git /opt/demo/X-RAN-CLOUD.git\nCOPY xcn.git /opt/demo/xcn.git\nCMD ["python3", "/opt/xran/demo-server.py"]\n')
    env = dict(os.environ, DOCKER_BUILDKIT='0')
    run(['docker', 'build', '--network', 'none', '--pull=false', '-t', DEMO_IMAGE, context], env=env)
    legacy = folder / 'legacy-context'; legacy.mkdir()
    (legacy / 'run-build.py').write_text((ROOT / 'runtime/run-build.py').read_text().replace(
        "'xranImageArchiveFallback': 1", "'xranImageArchiveFallback': 0"))
    (legacy / 'Dockerfile').write_text('FROM ' + args.compat_image + '\nCOPY run-build.py /opt/xran/run-build.py\n')
    run(['docker', 'build', '--network', 'none', '--pull=false', '-t', LEGACY_IMAGE, legacy], env=env)
    config = verification_config(yaml.safe_load(Path(args.base_site).read_text()), args.node_port)
    site.parent.mkdir(parents=True, exist_ok=True)
    site.write_text(yaml.safe_dump(config, sort_keys=False))
    original = ci.versions
    versions = dict(original(), DEMO_IMAGE=DEMO_IMAGE)
    ci.versions = lambda: versions
    archive = folder / 'k8s-images.tar'
    run(['docker', 'save', '-o', archive, DEMO_IMAGE, LEGACY_IMAGE, versions['CONTROLLER_IMAGE'],
         args.compat_image,args.sdk_image])
    (folder / 'tools').mkdir(exist_ok=True)
    cache = ci.CACHE
    try:
        ci.CACHE = folder
        ci.import_images(SimpleNamespace(site=str(site), namespace=None, offline=True,
                                        importer_image=versions['AGENT_IMAGE']))
    finally:
        ci.CACHE = cache
    archive.unlink()
    ci.deploy(SimpleNamespace(site=str(site), namespace=None, offline=True))
    ci.kubectl('label', 'namespace', NAMESPACE, 'xran-ci/verification=true')
    pins.update(demoImage=DEMO_IMAGE, legacyImage=LEGACY_IMAGE)
    (folder / 'fixtures.json').write_text(json.dumps(pins, indent=2) + '\n')
    print('隔离验收站点准备完成：' + str(site), flush=True)


if __name__ == '__main__':
    main()
