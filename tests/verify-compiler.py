#!/usr/bin/env python3
"""Real Jenkins SDK selection tests against an operator-provided demo registry."""
import argparse
import base64
import json
from pathlib import Path
import re
import subprocess
import time
from types import SimpleNamespace

from verify import Verification, ROOT
from ci import kube_env


def image_digest(value):
    match=re.search(r'(?:^|@|://)(sha256:[a-f0-9]{64})$',value)
    if not match:raise ValueError('Pod 镜像 ID 缺少有效 SHA256 摘要')
    return match[1]


def run(args):
    v = Verification(SimpleNamespace(site=args.site, source=args.source, report=args.report,
        compiler_image=None, compiler_image_pull_policy=None))
    v.base.update(COMPILER_IMAGE=args.compiler_image, COMPILER_IMAGE_PULL_POLICY='Always')
    original = None
    stamp = str(int(time.time()))

    def observe(numbers):
        snapshots, overlap = {}, False
        deadline = time.time() + 600
        while time.time() < deadline:
            pods = v.pods()
            active = set()
            for pod in pods:
                number = int(pod['metadata']['labels']['xran-build'][1:])
                if number not in numbers:
                    continue
                active.add(number)
                builder = next(c for c in pod['spec']['containers'] if c['name'] == 'builder')
                status = next((c for c in pod['status'].get('containerStatuses', []) if c['name'] == 'builder'), {})
                snapshots[number] = {'pod': pod['metadata']['name'], 'image': builder['image'],
                    'policy': builder['imagePullPolicy'], 'imageId': status.get('imageID'),
                    'waiting': status.get('state', {}).get('waiting'), 'resources': builder['resources']}
            overlap = overlap or set(numbers).issubset(active)
            if all(not v.admin.get(f'job/x-ran-cloud/{n}/api/json')['building'] for n in numbers):
                break
            time.sleep(1)
        else:
            raise TimeoutError('编译镜像验证构建超时')
        return snapshots, overlap

    def success(name, image, policy, kind='service', user='builder1', target='serviceA'):
        number = v.submit(user, COMPILER_IMAGE=image, COMPILER_IMAGE_PULL_POLICY=policy,
                          BUILD_KIND=kind, SERVICE_TARGET=target, IMAGE_TAG='sdk-' + stamp)
        snapshots, _ = observe([number])
        v.completed(number)
        meta = v.artifacts(number, v.head, user)
        snapshot = snapshots[number]
        assert meta['builderImage'] == snapshot['image'] == image
        assert meta['builderImagePullPolicy'] == snapshot['policy'] == policy
        assert meta['builderImageId'] == snapshot['imageId']
        v.record(name, {'build': number, 'metadataImageId': meta['builderImageId'], 'pod': snapshot})
        return meta

    try:
        v.ready()
        v.maintenance_access()
        definitions = v.admin.get('job/x-ran-cloud/api/json?tree=property[parameterDefinitions[name,defaultParameterValue[value],choices]]')
        parameters = {p['name']: p for prop in definitions['property'] for p in prop.get('parameterDefinitions', [])}
        assert parameters['COMPILER_IMAGE']['defaultParameterValue']['value'] == args.cached_compiler_image
        assert parameters['COMPILER_IMAGE_PULL_POLICY']['defaultParameterValue']['value'] == 'Always'
        assert parameters['COMPILER_IMAGE_PULL_POLICY']['choices'] == ['Always', 'IfNotPresent', 'Never']
        assert len(parameters) == 8
        v.record('服务与镜像任务公共编译参数及默认值', parameters)

        # Rendered templates live in a ConfigMap; obtain their exact live content.
        raw = subprocess.check_output(['kubectl', '-n', v.cfg['namespace'], 'get', 'configmap', 'xran-config', '-o', 'json'],
                                      text=True, env=kube_env())
        templates = {kind: json.loads(raw)['data'][f'pod-{kind}.json'] for kind in ['service', 'image']}
        source = (ROOT / 'jenkins/storage.groovy').read_text() + '\n' + (ROOT / 'jenkins/compiler.groovy').read_text()
        template_json = base64.b64encode(json.dumps(templates).encode()).decode()
        source += "\ndef templates=XranStorage.parseJson(new String('" + template_json + "'.decodeBase64(),'UTF-8'))\n"
        source += (ROOT / 'tests/compiler-template.groovy').read_text()
        encoded = base64.b64encode(source.encode()).decode()
        result = v.admin.script("def text=new String('" + encoded + "'.decodeBase64(),'UTF-8'); new GroovyShell(jenkins.model.Jenkins.get().pluginManager.uberClassLoader,new Binding([out:out])).evaluate(text); return null")
        v.record('真实插件环境的模板隔离与非法输入检查', json.loads(result))

        baseline = success('Always 服务包及实际 Pod 镜像 ID', args.compiler_image, 'Always')
        success('IfNotPresent 使用已导入内网地址镜像', args.cached_compiler_image, 'IfNotPresent', target='serviceB')
        success('Never 使用已导入内网地址镜像', args.cached_compiler_image, 'Never', target='serviceC')
        second = success('指定另一个 SDK 版本构建业务镜像并下载运行', args.second_compiler_image, 'Always', kind='image', user='builder2')
        assert image_digest(second['builderImageId']) != image_digest(baseline['builderImageId'])

        original = json.loads(v.admin.script("println(groovy.json.JsonOutput.toJson(jenkins.model.Jenkins.get().getItem('x-ran-cloud').definition.script));return null"))
        v.set_script(original.replace('def sourceType =', 'cfg.demoBuildDelaySeconds = 8\ndef sourceType =', 1))
        first = v.submit(COMPILER_IMAGE=args.compiler_image, SERVICE_TARGET='serviceA')
        other = v.submit('builder2', COMPILER_IMAGE=args.second_compiler_image, BUILD_KIND='image',
                         IMAGE_TARGET='imageB', IMAGE_TAG='sdk-parallel-' + stamp)
        snapshots, overlap = observe([first, other])
        assert overlap, snapshots
        for number, image, user in [(first, args.compiler_image, 'builder1'), (other, args.second_compiler_image, 'builder2')]:
            v.completed(number)
            meta = v.artifacts(number, v.head, user)
            assert meta['builderImage'] == snapshots[number]['image'] == image
            assert meta['builderImageId'] == snapshots[number]['imageId']
        v.record('不同编译镜像并发运行且互不影响', {'builds': [first, other], 'pods': snapshots})
        v.set_script(original)

        subprocess.run(['docker', 'tag', args.second_compiler_image, args.compiler_image], check=True)
        subprocess.run(['docker', 'push', args.compiler_image], check=True)
        cached = success('同 tag 已更新时 IfNotPresent 仍使用节点缓存', args.compiler_image, 'IfNotPresent')
        assert image_digest(cached['builderImageId']) == image_digest(baseline['builderImageId'])
        refreshed = success('同 tag 已更新时 Always 使用新摘要', args.compiler_image, 'Always')
        assert image_digest(refreshed['builderImageId']) == image_digest(second['builderImageId'])
        assert image_digest(refreshed['builderImageId']) != image_digest(baseline['builderImageId'])

        for name, changes, expected in [
            ('非法编译镜像引用明确失败', {'COMPILER_IMAGE': 'ubuntu:latest\nprivileged: true'}, '非法编译镜像地址'),
            ('不兼容的编译镜像明确失败', {'COMPILER_IMAGE': args.incompatible_image}, '编译镜像缺少工具')]:
            number = v.submit(**changes)
            observe([number])
            v.completed(number, 'FAILURE')
            assert expected in v.admin.console(number)
            v.record(name, {'build': number})

        v.set_script(original.replace('cfg.putAll(snapshot.config)', 'cfg.putAll(snapshot.config)\n        cfg.agentStartupTimeoutMinutes = 1', 1))
        for policy, expected in [('Never', 'ErrImageNeverPull'), ('Always', 'pull')]:
            number = v.submit(COMPILER_IMAGE=args.compiler_image.rsplit(':', 1)[0] + ':missing-' + stamp,
                              COMPILER_IMAGE_PULL_POLICY=policy)
            observe([number])
            build = v.admin.wait(number)
            assert build['result'] in ('FAILURE', 'ABORTED'), build['result']
            console = v.admin.console(number)
            assert expected.lower() in console.lower(), console[-3000:]
            assert build['duration'] < 180000, build['duration']
            v.record('缺少镜像时 ' + policy + ' 有界失败并给出诊断', {'build': number, 'durationMillis': build['duration'], 'result': build['result']})
        v.report['status'] = 'PASS'
    except Exception as error:
        v.report.update(status='FAIL', error=str(error))
        raise
    finally:
        if original:
            v.set_script(original)
        # Restore the demo latest tag to the SDK built from this repository.
        subprocess.run(['docker', 'tag', args.cached_compiler_image, args.compiler_image], check=True)
        subprocess.run(['docker', 'push', args.compiler_image], check=True)
        v.report['finishedAt'] = time.strftime('%FT%T%z')
        v.save()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', default=str(ROOT / 'config/site.yaml'))
    parser.add_argument('--source', default='/home/xiazhangtao/code/X-RAN-CLOUD')
    parser.add_argument('--report', default=str(ROOT / '.state/jenkins/compiler-verification.json'))
    parser.add_argument('--compiler-image', default='localhost:15050/xnet/xran-compile:latest')
    parser.add_argument('--second-compiler-image', default='localhost:15050/xnet/xran-compile:verification-variant')
    parser.add_argument('--cached-compiler-image', default='192.168.10.35/xnet/xran-compile:latest')
    parser.add_argument('--incompatible-image', default='localhost:15050/xnet/pull-probe:jammy')
    run(parser.parse_args())
