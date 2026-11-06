#!/usr/bin/env python3
"""真实 open5gs 发布验收；可用 --number 接续已有构建，避免重复长时间编译。"""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import time
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from jenkins_api import JenkinsAPI


def run(command, **kwargs):
    return subprocess.check_output(command, text=True, **kwargs).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', default='config/site.yaml')
    parser.add_argument('--source', default=str(ROOT.parent / 'open5gs'))
    parser.add_argument('--number', type=int)
    parser.add_argument('--ref-type', choices=['branch', 'tag', 'commit'], default='branch')
    parser.add_argument('--ref', default='open5gs-basev277-develop')
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--chart-version', default='')
    parser.add_argument('--app-version', default='')
    parser.add_argument('--compiler-image', default='192.168.10.35/xnet/xran-compile:latest')
    parser.add_argument('--pull-policy', choices=['Always', 'IfNotPresent', 'Never'], default='Never')
    parser.add_argument('--timeout', type=int, default=14400)
    parser.add_argument('--values', help='Helm 渲染验收 values；默认使用所选提交的 helm/tests/site-values.yaml')
    args = parser.parse_args()
    cfg = yaml.safe_load(Path(args.site).read_text())
    state = ROOT / '.state' / cfg['namespace']
    users = json.loads((state / 'users.json').read_text())
    api = JenkinsAPI(cfg['jenkinsUrl'], 'builder1', users['BUILDER1_PASSWORD'], job='open5gs')
    params = dict(COMPILER_IMAGE=args.compiler_image, COMPILER_IMAGE_PULL_POLICY=args.pull_policy,
                  REF_TYPE=args.ref_type, GIT_REF=args.ref, OFFLINE=str(args.offline).lower(),
                  CHART_VERSION=args.chart_version, APP_VERSION=args.app_version)
    number = args.number or api.wait_number(api.trigger(params))
    target = state / 'downloads' / 'open5gs' / str(number)
    target.mkdir(parents=True, exist_ok=True)
    report = {'number': number, 'url': api.url + api.job_path + str(number) + '/', 'checks': []}
    report_path = state / 'open5gs-release.json'

    def record(name, details=None):
        report['checks'].append({'name': name, 'status': 'PASS', 'details': details})
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print('PASS:', name, flush=True)

    print(f'等待 open5gs #{number}，超时 {args.timeout} 秒', flush=True)
    build = api.wait(number, timeout=args.timeout)
    console = api.console(number)
    (target / 'console.log').write_text(console)
    assert build['result'] == 'SUCCESS', console[-10000:]
    prefix = api.job_path + f'{number}/artifact/publish/'
    for name in ['metadata.json', 'SHA256SUMS']:
        api.download(prefix + name, target / name)
    meta = json.loads((target / 'metadata.json').read_text())
    reference = ('refs/heads/' if args.ref_type == 'branch' else 'refs/tags/') + args.ref
    if args.ref_type == 'commit': reference = args.ref
    expected_sha = run(['git', '-C', args.source, 'rev-parse', reference + '^{commit}'])
    assert meta['commit'] == expected_sha, (meta['commit'], expected_sha)
    assert meta['userId'] == 'builder1' and meta['project'] == 'open5gs'
    assert meta['result'] == 'SUCCESS' and meta['durationMillis'] > 0 and meta['finishedAt']
    assert meta['builderImage'] == args.compiler_image and meta['builderImageId']
    assert meta['builderImagePullPolicy'] == args.pull_policy and meta['offline'] == args.offline
    today = dt.datetime.fromisoformat(meta['startedAt']).astimezone(ZoneInfo('Asia/Shanghai'))
    date_version = f'{today.year % 100:02d}.{today.month}.{today.day}'
    assert meta['chartVersion'] == (args.chart_version or date_version)
    assert meta['appVersion'] == (args.app_version or date_version)
    actual_params = {p['name']: p['value'] for action in build['actions'] for p in action.get('parameters', [])}
    assert actual_params['CHART_VERSION'] == meta['chartVersion']
    assert actual_params['APP_VERSION'] == meta['appVersion']
    record('精确提交、构建人、时间、日期版本及编译镜像', meta)
    expected = {'xcn-runtime.tar', 'xcn-webui.tar', 'xcn-monitor.tar', f'xcn-{meta["chartVersion"]}.tgz'}
    assert {item['name'] for item in meta['files']} == expected
    for item in meta['files']:
        path = target / item['name']
        api.download(prefix + item['name'], path)
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''): digest.update(block)
        assert digest.hexdigest() == item['sha256'] and path.stat().st_size == item['size']
    run(['sha256sum', '-c', 'SHA256SUMS'], cwd=target)
    record('普通账号下载四个产物及 SHA256 完整性', {'files': sorted(expected)})
    helm = str(ROOT / '.cache/tools/helm')
    chart = target / f'xcn-{meta["chartVersion"]}.tgz'
    # This chart deliberately rejects unset site addresses. Use its committed
    # documentation-only fixture for rendering; never install this test release.
    values = target / 'render-values.yaml'
    values.write_text(Path(args.values).read_text() if args.values else run(
        ['git', '-C', args.source, 'show', expected_sha + ':helm/tests/site-values.yaml']))
    lint = run([helm, 'lint', str(chart), '-f', str(values)])
    rendered = run([helm, 'template', 'verify', str(chart), '-f', str(values)])
    docs = [doc for doc in yaml.safe_load_all(rendered) if doc]
    assert docs and all(doc.get('kind') for doc in docs)
    with tarfile.open(chart) as archive:
        info = yaml.safe_load(archive.extractfile('xcn/Chart.yaml'))
        assert info['version'] == meta['chartVersion'] and info['appVersion'] == meta['appVersion']
    record('Helm lint 与模板渲染', {'objects': len(docs), 'lint': lint})
    commands = {
        'xcn-runtime': ['sh', '-ec', 'xcn-amfd -v; test -r /usr/local/lib/libmemif.so'],
        'xcn-webui': ['sh', '-ec', 'node --version; test -s .next/BUILD_ID'],
        'xcn-monitor': ['sh', '-ec', 'python3 -c \'import ssl,sqlite3,ctypes,bz2,lzma,readline,aiohttp,defusedxml; ctypes.CDLL("/opt/xcn-monitor/libxcn-trace.so")\'; tshark --version'],
    }
    assert len(meta['images']) == 3
    for image in meta['images']:
        run(['docker', 'load', '-i', str(target / (image['name'] + '.tar'))])
        assert run(['docker', 'image', 'inspect', '--format', '{{.Id}}', image['reference']]) == image['imageId']
        output = run(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', commands[image['name']][0],
                      image['reference'], *commands[image['name']][1:]], stderr=subprocess.STDOUT)
        record(image['name'] + ' 导入与断网运行检查', {'imageId': image['imageId'], 'output': output})
    statuses = {image['reference']: image['pushStatus'] for image in meta['images']}
    if any(status != 'success' for status in statuses.values()):
        assert meta['warnings'] and '镜像推送未全部成功' in build['description']
    record('推送状态如实记录；非致命 push 失败仍保留有效产物', statuses)
    report['status'] = 'PASS'
    report['finishedAt'] = time.strftime('%FT%T%z')
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    main()
