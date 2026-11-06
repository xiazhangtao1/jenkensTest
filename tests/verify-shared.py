#!/usr/bin/env python3
"""仅对专用验收 Jenkins 验证共享槽位、标签锁、Pod 选择及跨项目清理。"""
import argparse
import base64
import json
from pathlib import Path
import subprocess
import sys
import time

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from ci import kube_env
from jenkins_api import JenkinsAPI


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', required=True)
    args = parser.parse_args()
    cfg = yaml.safe_load(Path(args.site).read_text())
    assert cfg.get('verificationInstance') is True and cfg['namespace'] != 'jenkins', '需要独立验收实例'
    state = ROOT / '.state' / cfg['namespace']
    users = json.loads((state / 'users.json').read_text())
    clients = {job: JenkinsAPI(cfg['jenkinsUrl'], 'admin', users['ADMIN_PASSWORD'], job=job)
               for job in ['x-ran-cloud', 'open5gs']}
    admin = clients['x-ran-cloud']
    report = {'namespace': cfg['namespace'], 'checks': []}
    report_path = state / 'shared-verification.json'
    originals, started, pods = {}, [], []

    def record(name, details):
        report['checks'].append(dict(name=name, status='PASS', details=details))
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print('PASS:', name, flush=True)

    def script(text):
        encoded = base64.b64encode(text.encode()).decode()
        return admin.script("new GroovyShell(jenkins.model.Jenkins.get().pluginManager.uberClassLoader,new Binding([out:out])).evaluate(new String('" + encoded + "'.decodeBase64(),'UTF-8'));return null")

    def submit(job, **params):
        api = clients[job]
        number = api.wait_number(api.trigger(params))
        started.append((job, number))
        return job, number

    def submit_many(requests):
        # Submit the whole burst before waiting out Jenkins' quiet period.
        queued = [(job, clients[job].trigger(params)) for job, params in requests]
        builds = []
        for job, queue in queued:
            pair = (job, clients[job].wait_number(queue))
            started.append(pair)
            builds.append(pair)
        return builds

    def held():
        return json.loads(admin.script("println(groovy.json.JsonOutput.toJson(org.jenkins.plugins.lockableresources.LockableResourcesManager.get().resources.findAll{it.labels?.tokenize()?.contains('xran-build-slot') && it.isLocked()}.collect{it.name}));return null"))

    def observe(builds):
        maximum, blocked = 0, False
        deadline = time.time() + 240
        while time.time() < deadline:
            count = len(held())
            maximum = max(maximum, count)
            active = [pair for pair in builds if clients[pair[0]].get(clients[pair[0]].job_path + f'{pair[1]}/api/json')['building']]
            blocked = blocked or len(active) > count > 0
            if not active: break
            time.sleep(1)
        else:
            raise TimeoutError('共享槽位验收超时')
        for job, number in builds:
            assert clients[job].wait(number)['result'] == 'SUCCESS', clients[job].console(number)
        return maximum, blocked

    try:
        for job, api in clients.items():
            originals[job] = json.loads(admin.script("println(groovy.json.JsonOutput.toJson(jenkins.model.Jenkins.get().getItem('" + job + "').definition.script));return null"))
        raw = subprocess.check_output(['kubectl', '-n', cfg['namespace'], 'get', 'configmap', 'xran-config', '-o', 'json'], text=True, env=kube_env())
        templates = {kind: json.loads(raw)['data'][f'pod-{kind}.json'] for kind in ['service', 'image', 'open5gs']}
        template_json = base64.b64encode(json.dumps(templates).encode()).decode()
        support = (ROOT / 'jenkins/storage.groovy').read_text() + '\n' + (ROOT / 'jenkins/compiler.groovy').read_text()
        source = support + "\ndef templates=XranStorage.parseJson(new String('" + template_json + "'.decodeBase64(),'UTF-8'))\n" + (ROOT / 'tests/compiler-template.groovy').read_text()
        record('三类 Pod 模板及镜像参数校验', json.loads(script(source)))

        for job, suffix in [('x-ran-cloud', 'xran'), ('open5gs', 'open5gs')]:
            name = 'shared-pod-check-' + suffix
            manifest = {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': name, 'namespace': cfg['namespace'],
                        'labels': {'xran-job': job, 'xran-build': 'b987654'}},
                        'spec': {'restartPolicy': 'Never', 'automountServiceAccountToken': False,
                                 'containers': [{'name': 'builder', 'image': 'busybox:1.37.0', 'imagePullPolicy': 'Never',
                                                 'command': ['sleep', '180'], 'resources': {'requests': {'cpu': '10m', 'memory': '16Mi'}, 'limits': {'cpu': '50m', 'memory': '32Mi'}}}]}}
            subprocess.run(['kubectl', 'apply', '-f', '-'], input=json.dumps(manifest), text=True, check=True, env=kube_env(), stdout=subprocess.DEVNULL)
            pods.append(name)
        subprocess.run(['kubectl', '-n', cfg['namespace'], 'wait', '--for=condition=Ready', '--timeout=90s', *['pod/' + p for p in pods]], check=True, env=kube_env(), stdout=subprocess.DEVNULL)
        selection = script(support + "\ndef ns='" + cfg['namespace'] + "';def a=XranCompiler.podInfo(ns,'987654','x-ran-cloud');def b=XranCompiler.podInfo(ns,'987654','open5gs');assert a.pod=='shared-pod-check-xran' && b.pod=='shared-pod-check-open5gs';println(groovy.json.JsonOutput.toJson([xran:a.pod,open5gs:b.pod]));return null")
        record('相同构建号按项目分别选择 Pod', json.loads(selection))
        subprocess.run(['kubectl', '-n', cfg['namespace'], 'delete', 'pod', *pods, '--wait=false'], check=True, env=kube_env(), stdout=subprocess.DEVNULL)
        pods.clear()

        # Only the body after the real slot acquisition is short-circuited; the
        # production parameter validation and both lock levels execute unchanged.
        marker = "def pod = XranCompiler.template("
        for job, api in clients.items():
            assert marker in originals[job]
            api.set_pipeline(originals[job].replace(marker, "echo('shared-verification-slot-held'); sleep(time: 20, unit: 'SECONDS'); return\n" + marker, 1))
        stamp = str(int(time.time()))
        batch = submit_many([('x-ran-cloud' if i % 2 == 0 else 'open5gs',
                              dict(COMPILER_IMAGE_PULL_POLICY='Never', CHART_VERSION='1.0.0',
                                   IMAGE_TAG=f'control-{stamp}-{i}', APP_VERSION=f'control-{stamp}-{i}')) for i in range(8)])
        maximum, blocked = observe(batch)
        assert maximum == cfg['concurrency'] == 6 and blocked, (maximum, blocked)
        record('两项目合计六个槽位、其余排队', {'builds': batch, 'maximumSlots': maximum, 'queued': blocked})
        same = submit_many([('open5gs', dict(CHART_VERSION=f'1.0.{i}', APP_VERSION='shared-' + stamp)) for i in range(2)])
        maximum, blocked = observe(same)
        assert maximum == 1 and blocked, (maximum, blocked)
        record('open5gs 相同 APP_VERSION 等待标签锁且不占额外槽位', {'builds': same, 'maximumSlots': maximum})

        # The following histories and files exist only on this dedicated PVC.
        for api in clients.values(): api.set_pipeline("echo('isolated storage fixture')")
        fixtures = []
        for job in ['x-ran-cloud', 'open5gs', 'x-ran-cloud', 'open5gs']:
            pair = submit(job); clients[job].wait(pair[1]); fixtures.append(pair)
        for job, number in fixtures:
            admin.script("def r=jenkins.model.Jenkins.get().getItem('" + job + "').getBuildByNumber(" + str(number) + ");def f=new File(r.getArtifactsDir(),'fixture.bin');f.parentFile.mkdirs();f.bytes=new byte[1024];return null")
        clients['open5gs'].set_pipeline("echo('running storage fixture'); sleep(time: 120, unit: 'SECONDS')")
        active = submit('open5gs')
        admin.script("def r=jenkins.model.Jenkins.get().getItem('open5gs').getBuildByNumber(" + str(active[1]) + ");def f=new File(r.getArtifactsDir(),'running.bin');f.parentFile.mkdirs();f.bytes=new byte[1024];return null")
        storage_output = script((ROOT / 'jenkins/storage.groovy').read_text() + '\n' + (ROOT / 'tests/storage-integration.groovy').read_text())
        (state / 'storage-verification.log').write_text(storage_output)
        result = json.loads(storage_output)
        assert result['oldestDeleted'].startswith('x-ran-cloud#') and result['secondDeleted'].startswith('open5gs#')
        assert result['runningProtected'] and result['audit'] and result['rejectedWhenInsufficient']
        record('跨项目最早回收、运行/保留保护、提示审计及空间不足拒绝', result)
        report['status'] = 'PASS'
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    finally:
        for job, number in started:
            api = clients[job]
            if api.get(api.job_path + f'{number}/api/json')['building']:
                api.post(api.job_path + f'{number}/stop')
                api.wait(number, timeout=120)
        for job, original in originals.items(): clients[job].set_pipeline(original)
        if pods:
            subprocess.run(['kubectl', '-n', cfg['namespace'], 'delete', 'pod', *pods, '--ignore-not-found', '--wait=false'], env=kube_env(), check=False)


if __name__ == '__main__':
    main()
