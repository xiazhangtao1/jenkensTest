#!/usr/bin/env python3
"""独立 Jenkins：两套编译镜像、串行开关、废弃目标、并发及重启持久化。"""
import argparse
import base64
import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
import urllib.error

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from ci import kube_env
from verify import Verification


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', required=True)
    parser.add_argument('--compat-image', required=True)
    parser.add_argument('--sdk-image', required=True)
    parser.add_argument('--pull-policy', default='Always', choices=['Always', 'IfNotPresent', 'Never'])
    parser.add_argument('--source', default=str(ROOT.parent / 'X-RAN-CLOUD'))
    parser.add_argument('--skip-functional', action='store_true', help='接续同实例已经通过的兼容版全套功能验收')
    args = parser.parse_args()
    cfg = yaml.safe_load(Path(args.site).read_text())
    assert cfg.get('verificationInstance') is True and cfg['namespace'] != 'jenkins', '需要独立验收实例'
    report = ROOT / '.state' / cfg['namespace'] / 'build-compatibility.json'
    v = Verification(SimpleNamespace(site=args.site, source=args.source, report=str(report),
        compiler_image=args.compat_image, compiler_image_pull_policy=args.pull_policy, services_only=False))
    if args.skip_functional:
        previous = json.loads(report.read_text())
        assert previous['namespace'] == cfg['namespace']
        assert any(check['name'] == 'push 失败仍能下载并运行有效镜像 tar' and check['status'] == 'PASS'
                   for check in previous['checks']), '须先完成兼容版功能验收'
        assert previous['builds'][0]['parameters']['COMPILER_IMAGE'] == args.compat_image
        v.report = previous
        for key in ('status', 'error', 'finishedAt'):
            v.report.pop(key, None)
    original = None

    def state():
        return json.loads(v.admin.script('println(new File(jenkins.model.Jenkins.get().rootDir,'
            "'xran-platform/state.json').text);return null"))

    def save_serial(value):
        previous = state()
        candidate = dict(previous['settings'], revision=previous['version'], xranServiceSerial=value)
        encoded = base64.b64encode(json.dumps(candidate).encode()).decode()
        v.admin.script("def c=io.xran.jenkins.BuildPlatformConfiguration.get();"
            "c.configure((org.kohsuke.stapler.StaplerRequest2)null,net.sf.json.JSONObject.fromObject("
            "new String('" + encoded + "'.decodeBase64(),'UTF-8')));return null")
        saved = state()
        assert saved['settings']['xranServiceSerial'] is value
        assert saved['version'] == saved['appliedVersion']
        return saved

    try:
        v.ready()
        v.maintenance_access()
        initial = state()
        assert initial['settings']['xranServiceSerial'] is True
        assert initial['settings']['serviceMemoryGiB'] == 8
        assert initial['settings']['concurrency'] == 6
        with v.admin.request('configure') as response:
            assert 'X-RAN-CLOUD 服务编译串行执行' in response.read().decode()
        definitions = v.admin.get('job/x-ran-cloud/api/json?tree=property[parameterDefinitions[name,choices]]')
        service = next(p for prop in definitions['property'] for p in prop.get('parameterDefinitions', [])
                       if p['name'] == 'SERVICE_TARGET')
        assert 'all' not in service['choices'] and 'services_all' in service['choices']
        v.record('管理员开关及默认资源；页面仅提供单服务和 services_all', service)

        # All existing branch/tag/SHA and image save/push/download checks use the compatibility image.
        if not args.skip_functional:
            v.functional()
        for kind, target in [('service', 'serviceA'), ('service', 'services_all'), ('image', 'imageA')]:
            check_name = 'SDK 镜像构建 ' + kind + '/' + target
            if args.skip_functional and any(check['name'] == check_name and check['status'] == 'PASS'
                    and any(build['number'] == check['details']['build']
                            and build['parameters']['COMPILER_IMAGE'] == args.sdk_image for build in v.report['builds'])
                    for check in v.report['checks']):
                continue
            number = v.submit(COMPILER_IMAGE=args.sdk_image, BUILD_KIND=kind,
                              SERVICE_TARGET=target if kind == 'service' else 'serviceA',
                              IMAGE_TARGET=target if kind == 'image' else 'imageA',
                              IMAGE_TAG='sdk-formal-' + str(time.time_ns()))
            v.completed(number)
            meta = v.artifacts(number, v.head, 'builder1', compiler_image=args.sdk_image)
            assert meta['builderImage'] == args.sdk_image and meta['xranServiceSerial'] is True
            v.record(check_name, {'build': number, 'imageId': meta['builderImageId']})

        # Bypass the choice control to prove the trusted Pipeline also rejects the removed target.
        number = int(v.admin.script("def job=jenkins.model.Jenkins.get().getItem('x-ran-cloud');"
            "def f=job.scheduleBuild2(0,new hudson.model.ParametersAction("
            "new hudson.model.StringParameterValue('SERVICE_TARGET','all')));"
            "println(f.waitForStart().number);return null").strip())
        v.completed(number, 'FAILURE')
        assert 'all 已取消' in v.admin.console(number)
        v.record('绕过页面提交 all 仍明确拒绝', {'build': number})

        save_serial(False)
        number = v.submit()
        v.completed(number)
        meta = v.artifacts(number, v.head, 'builder1')
        assert meta['xranServiceSerial'] is False
        assert '遵循原脚本并行设置' in v.admin.console(number)
        v.record('关闭开关真实传入 false 并记录到构建快照', {'build': number})
        save_serial(True)

        original = json.loads(v.admin.script("println(groovy.json.JsonOutput.toJson("
            "jenkins.model.Jenkins.get().getItem('x-ran-cloud').definition.script));return null"))
        v.set_script(original.replace('def sourceType =', 'cfg.demoBuildDelaySeconds = 12\ndef sourceType =', 1))
        first = v.submit(COMPILER_IMAGE=args.compat_image)
        second = v.submit('builder2', COMPILER_IMAGE=args.sdk_image, SERVICE_TARGET='serviceB')
        maximum, overlap, _ = v.observe([first, second], parallel=[first, second])
        assert overlap and maximum >= 2
        ids = [v.artifacts(first, v.head, 'builder1')['builderImageId'],
               v.artifacts(second, v.head, 'builder2', compiler_image=args.sdk_image)['builderImageId']]
        assert ids[0] != ids[1]
        v.set_script(original)
        original = None
        v.record('两种编译环境同时构建；单任务串行不影响任务并发',
                 {'builds': [first, second], 'maximumPods': maximum, 'imageIds': ids})

        saved = save_serial(False)
        subprocess.run(['kubectl', '-n', cfg['namespace'], 'rollout', 'restart', 'statefulset/jenkins'],
                       env=kube_env(), check=True)
        subprocess.run(['kubectl', '-n', cfg['namespace'], 'rollout', 'status', 'statefulset/jenkins',
                        '--timeout=300s'], env=kube_env(), check=True)
        for client in [v.admin, *v.clients.values()]:
            client.crumb = None
        # Service endpoints/kube-proxy can lag behind the Pod readiness transition.
        deadline = time.time() + 240
        while True:
            try:
                restored = state()
                break
            except (urllib.error.URLError, TimeoutError):
                if time.time() >= deadline:
                    raise
                v.admin.crumb = None
                time.sleep(2)
        assert restored['settings']['xranServiceSerial'] is False
        assert restored['version'] == saved['version']
        assert v.admin.get(f'job/x-ran-cloud/{first}/api/json')['result'] == 'SUCCESS'
        save_serial(True)
        v.record('Controller 重启保留关闭状态、配置版本与构建历史', {'version': saved['version']})
        v.report['status'] = 'PASS'
    except Exception as error:
        v.report.update(status='FAIL', error=str(error))
        raise
    finally:
        if original is not None:
            v.set_script(original)
        v.report['finishedAt'] = time.strftime('%FT%T%z')
        v.save()


if __name__ == '__main__':
    main()
