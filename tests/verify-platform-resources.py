#!/usr/bin/env python3
"""真实 Pod 资源与入场配置、产物元数据的一致性验收。"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import time

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from ci import kube_env
from jenkins_api import JenkinsAPI


def quantity(value):
    match = re.fullmatch(r'(\d+)(m|Ki|Mi|Gi)?', str(value))
    assert match, value
    return int(match[1]) * {'m': .001, 'Ki': 1024, 'Mi': 1024**2, 'Gi': 1024**3, None: 1}[match[2]]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', required=True)
    parser.add_argument('--kind', required=True, choices=['service', 'image', 'xcn'])
    parser.add_argument('--number', type=int)
    parser.add_argument('--capture-only', action='store_true')
    args = parser.parse_args()
    cfg = yaml.safe_load(Path(args.site).read_text())
    directory = ROOT / '.state' / cfg['namespace']
    users = json.loads((directory / 'users.json').read_text())
    job = 'xcn' if args.kind == 'xcn' else 'x-ran-cloud'
    api = JenkinsAPI(cfg['jenkinsUrl'], 'builder1', users['BUILDER1_PASSWORD'], job=job)
    admin = JenkinsAPI(cfg['jenkinsUrl'], 'admin', users['ADMIN_PASSWORD'], job=job)
    if not args.number:
        assert args.kind != 'xcn', '完整 xcn 发布请使用 verify-xcn.py；本脚本通过 --number 观察该构建'
    params = dict(BUILD_KIND=args.kind, REF_TYPE='branch', GIT_REF='main', SERVICE_TARGET='serviceA',
                  IMAGE_TARGET='imageA', IMAGE_TAG='platform-' + str(time.time_ns()),
                  COMPILER_IMAGE='192.168.10.35/xnet/xran-compile:latest', COMPILER_IMAGE_PULL_POLICY='Never')
    number = args.number or api.wait_number(api.trigger(params))
    print(f'观察 {job} #{number}', flush=True)
    deadline, pod = time.time() + 150, None
    while time.time() < deadline:
        raw = subprocess.check_output(['kubectl', '-n', cfg['namespace'], 'get', 'pods',
            '-l', f'xran-job={job},xran-build=b{number}', '-o', 'json'], env=kube_env(), text=True)
        items = json.loads(raw)['items']
        if items:
            pod = next((p for p in items if not p['metadata'].get('deletionTimestamp')), items[0])
            break
        result = api.get(api.job_path + str(number) + '/api/json')
        assert result['building'], api.console(number)
        time.sleep(.5)
    assert pod, '未观察到构建 Pod'
    state = json.loads(admin.script("println(new File(jenkins.model.Jenkins.get().rootDir,'xran-platform/state.json').text);return null"))
    snapshot = state['leases'][job + '#' + str(number)]
    assert str(snapshot['settingsVersion']) == pod['metadata']['annotations']['xran-ci/settings-version']
    assert snapshot['resourceProfile'] == args.kind
    resources = {c['name']: c['resources'] for c in pod['spec']['containers']}
    assert resources.keys() == snapshot['resourceAllocation'].keys()
    for name, selected in snapshot['resourceAllocation'].items():
        for key in ['requests', 'limits']:
            for resource, amount in selected[key].items():
                assert quantity(resources[name][key][resource]) == quantity(amount), (name, resource, resources[name], selected)
    totals = {key: sum(quantity(c['limits'][key]) for c in resources.values()) for key in ['cpu', 'memory', 'ephemeral-storage']}
    selected = snapshot['config']
    assert totals == {'cpu': selected['buildCpuLimit'], 'memory': selected['buildMemoryGiB'] * 1024**3,
                      'ephemeral-storage': selected['temporaryStorageGiB'] * 1024**3}, totals
    volume = next(v for v in pod['spec']['volumes'] if v['name'] == 'workspace-volume')
    assert quantity(volume['emptyDir']['sizeLimit']) == totals['ephemeral-storage']
    assert pod['spec']['automountServiceAccountToken'] is False
    report = dict(job=job, number=number, settingsVersion=snapshot['settingsVersion'], resourceProfile=args.kind,
                  admittedAt=snapshot['admittedAt'], pod=pod['metadata']['name'], resources=resources, totals=totals,
                  selected=selected, currentSettingsVersion=state['version'], status='PASS')
    path = directory / ('platform-resources-' + args.kind + '.json')
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print('PASS: 实际 Pod 资源与持久化入场快照一致', json.dumps(totals), flush=True)
    if not args.capture_only:
        result = api.wait(number, timeout=14400 if args.kind == 'xcn' else 600)
        assert result['result'] == 'SUCCESS', api.console(number)
        metadata = api.get(api.job_path + str(number) + '/artifact/publish/metadata.json')
        for key in ['settingsVersion', 'resourceProfile', 'resourceAllocation', 'admittedAt']:
            assert metadata[key] == snapshot[key], key
        assert metadata['builderImage'] == params['COMPILER_IMAGE'] and metadata['builderImagePullPolicy'] == 'Never'
        report['metadata'] = {key: metadata[key] for key in ['commit', 'settingsVersion', 'resourceProfile', 'admittedAt', 'builderImage', 'builderImagePullPolicy']}
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print('PASS: 构建成功、元数据与 Pod 分配一致，显式编译环境优先', flush=True)


if __name__ == '__main__':
    main()
