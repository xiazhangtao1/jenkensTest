#!/usr/bin/env python3
"""对独立验收实例执行真实构建、下载、并发和清理验证。"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from ci import kube_env
from jenkins_api import JenkinsAPI


class Verification:
    def __init__(self, args):
        self.args = args
        self.cfg = yaml.safe_load(Path(args.site).read_text())
        self.state = ROOT / '.state' / self.cfg['namespace']
        users = json.loads((self.state / 'users.json').read_text())
        self.admin = JenkinsAPI(self.cfg['jenkinsUrl'], 'admin', users['ADMIN_PASSWORD'])
        self.clients = {name: JenkinsAPI(self.cfg['jenkinsUrl'], name, users[name.upper() + '_PASSWORD'])
                        for name in ['builder1', 'builder2']}
        self.report = {'namespace': self.cfg['namespace'], 'startedAt': time.strftime('%FT%T%z'), 'checks': [], 'builds': []}
        self.report_path = Path(args.report) if args.report else self.state / 'verification.json'
        self.downloads = self.state / 'downloads'
        self.downloads.mkdir(exist_ok=True)
        self.head = subprocess.check_output(['git', '-C', args.source, 'rev-parse', 'main'], text=True).strip()
        self.old = subprocess.check_output(['git', '-C', args.source, 'rev-parse', 'demo-v1.0.0^{commit}'], text=True).strip()
        self.base = {'BUILD_KIND': 'service', 'REF_TYPE': 'branch', 'GIT_REF': 'main',
                     'SERVICE_TARGET': 'serviceA', 'IMAGE_TARGET': 'imageA', 'IMAGE_TAG': 'latest'}

    def save(self):
        self.report_path.write_text(json.dumps(self.report, ensure_ascii=False, indent=2) + '\n')

    def record(self, name, details=None):
        self.report['checks'].append({'name': name, 'status': 'PASS', 'details': details})
        self.save()
        print('PASS:', name, flush=True)

    def ready(self):
        deadline = time.time() + 240
        while time.time() < deadline:
            try:
                self.admin.get('api/json')
                break
            except (urllib.error.URLError, TimeoutError):
                time.sleep(2)
        else:
            raise TimeoutError('Jenkins 尚未就绪')
        plugins = self.admin.get('pluginManager/api/json?tree=plugins[shortName,active,enabled]')['plugins']
        bad = [p['shortName'] for p in plugins if not p['active'] or not p['enabled']]
        assert not bad, bad
        self.record('全部插件启用', {'count': len(plugins)})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            opener.open(self.cfg['jenkinsUrl'].rstrip('/') + '/api/json', timeout=20)
            raise AssertionError('匿名用户可访问 API')
        except urllib.error.HTTPError as e:
            assert e.code in (401, 403)
        try:
            self.clients['builder1'].script('println("forbidden")')
            raise AssertionError('普通账号可执行脚本控制台')
        except urllib.error.HTTPError as e:
            assert e.code == 403
        self.record('匿名访问及普通账号管理权限受限')

    def maintenance_access(self):
        job = 'job/storage-maintenance/'
        details = self.admin.get(job + 'api/json?tree=name,lastBuild[number]')
        assert details['name'] == 'storage-maintenance' and details['lastBuild']
        last = details['lastBuild']['number']
        with self.admin.request(job + 'configure') as response:
            assert response.status == 200
        results = {}
        for name, client in self.clients.items():
            visible = [item['name'] for item in client.get('api/json?tree=jobs[name]')['jobs']]
            assert 'x-ran-cloud' in visible and 'storage-maintenance' not in visible, visible
            with client.request('') as response:
                assert b'href="job/storage-maintenance/' not in response.read()
            client.get('job/x-ran-cloud/api/json')
            denied = {}
            for path, form in [('', None), ('api/json', None), ('config.xml', None),
                               (f'{last}/consoleText', None), ('build', {})]:
                try:
                    with client.request(job + path, form):
                        raise AssertionError(f'{name} 可访问维护任务 {path}')
                except urllib.error.HTTPError as error:
                    assert error.code in (403, 404), (path, error.code)
                    denied[path or 'job'] = error.code
            results[name] = {'visibleJobs': visible, 'deniedEndpoints': denied}
        self.record('维护任务仅管理员可见；普通账号首页、API、直链、日志及手动触发受限', results)

    def submit(self, user='builder1', **changes):
        params = dict(self.base, **changes)
        client = self.clients[user]
        number = client.wait_number(client.trigger(params))
        self.report['builds'].append({'number': number, 'user': user, 'parameters': params})
        self.save()
        return number

    def completed(self, number, expected='SUCCESS'):
        build = self.admin.wait(number)
        if build['result'] != expected:
            console = self.admin.console(number)
            (self.state / f'build-{number}.log').write_text(console)
            raise AssertionError(f'#{number}: {build["result"]}, expected {expected}\n{console[-7000:]}')
        return build

    def artifacts(self, number, sha, user, expected_push='success', expected_result='SUCCESS'):
        target = self.downloads / str(number)
        target.mkdir(exist_ok=True)
        prefix = f'job/x-ran-cloud/{number}/artifact/publish/'
        client = self.clients[user]
        client.download(prefix + 'metadata.json', target / 'metadata.json')
        client.download(prefix + 'SHA256SUMS', target / 'SHA256SUMS')
        meta = json.loads((target / 'metadata.json').read_text())
        assert meta['commit'] == sha and meta['userId'] == user
        assert meta['result'] == expected_result
        assert meta['startedAt'] and meta['finishedAt'] and meta['durationMillis'] >= 0
        for item in meta['files']:
            path = target / item['name']
            client.download(prefix + item['name'], path)
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
            assert digest.hexdigest() == item['sha256'] and path.stat().st_size == item['size']
            if meta['kind'] == 'service':
                with tarfile.open(path) as archive:
                    for member in archive:
                        if member.name.endswith('/build.json'):
                            assert json.load(archive.extractfile(member))['commit'] == sha
            else:
                subprocess.run(['docker', 'load', '-i', path], check=True, stdout=subprocess.DEVNULL)
                result = subprocess.check_output(['docker', 'run', '--rm', '--network', 'none', meta['imageReference']], text=True)
                assert sha in result and meta['pushStatus'] == expected_push and meta.get('imageId')
        subprocess.run(['sha256sum', '-c', 'SHA256SUMS'], cwd=target, check=True, capture_output=True)
        return meta

    def functional(self):
        cases = [('serviceA 分支', {}, self.head),
            ('serviceB 演示分支', {'SERVICE_TARGET': 'serviceB', 'GIT_REF': 'demo/feature'}, self.old),
            ('serviceC tag', {'SERVICE_TARGET': 'serviceC', 'REF_TYPE': 'tag', 'GIT_REF': 'demo-v1.0.0'}, self.old),
            ('带注释 tag', {'REF_TYPE': 'tag', 'GIT_REF': 'demo-v1.0.0-annotated'}, self.old),
            ('all 多包', {'SERVICE_TARGET': 'all'}, self.head),
            ('services_all 历史完整 SHA', {'SERVICE_TARGET': 'services_all', 'REF_TYPE': 'commit', 'GIT_REF': self.old}, self.old),
            ('唯一短 SHA', {'REF_TYPE': 'commit', 'GIT_REF': self.head[:9]}, self.head)]
        if not self.args.services_only:
            cases += [(image + ' build/save/push/load', {'BUILD_KIND': 'image', 'IMAGE_TARGET': image,
                'IMAGE_TAG': 'verify-' + str(int(time.time()))}, self.head) for image in ['imageA', 'imageB', 'imageC']]
        for i, (name, changes, sha) in enumerate(cases):
            user = 'builder1' if i % 2 == 0 else 'builder2'
            number = self.submit(user, **changes)
            self.completed(number)
            meta = self.artifacts(number, sha, user)
            assert len(meta['files']) == (3 if changes.get('SERVICE_TARGET') == 'all' else 1)
            self.record(name, {'build': number, 'commit': sha})
        for name, changes in [('不存在的 Git 引用明确失败', {'GIT_REF': 'missing-ci-reference'}),
                              ('非法标签明确失败', {'BUILD_KIND': 'image', 'IMAGE_TAG': 'bad;touch /tmp/injected'})]:
            number = self.submit(**changes)
            self.completed(number, 'FAILURE')
            self.record(name, {'build': number})
        if not self.args.services_only:
            original = json.loads(self.admin.script("println(groovy.json.JsonOutput.toJson(jenkins.model.Jenkins.get().getItem('x-ran-cloud').definition.script));return null"))
            try:
                self.set_script(original.replace('def sourceType =', "cfg.registry = '127.0.0.1:1'\ndef sourceType =", 1))
                number = self.submit(BUILD_KIND='image', IMAGE_TAG='push-failure-ci')
                self.completed(number, 'FAILURE')
                self.artifacts(number, self.head, 'builder1', expected_push='failed-or-not-reached', expected_result='FAILURE')
                self.record('push 失败仍能下载并运行有效镜像 tar', {'build': number})
            finally:
                self.set_script(original)

    def set_script(self, text):
        self.admin.set_pipeline(text)

    def pods(self):
        items = json.loads(subprocess.check_output(['kubectl', '-n', self.cfg['namespace'], 'get', 'pods',
            '-l', 'xran-ci=build', '-o', 'json'], text=True, env=kube_env()))['items']
        return [p for p in items if not p['metadata'].get('deletionTimestamp') and p['status'].get('phase') not in ['Succeeded', 'Failed']]

    def observe(self, numbers, limit=6, exclusive=None, parallel=None):
        maximum, overlapped, exclusive_max = 0, False, 0
        deadline = time.time() + 600
        while time.time() < deadline:
            pods = self.pods()
            ids = {p['metadata']['labels'].get('xran-build') for p in pods}
            maximum = max(maximum, len(pods))
            assert len(pods) <= limit
            if exclusive:
                count = len(ids & {'b' + str(n) for n in exclusive})
                exclusive_max = max(exclusive_max, count)
                assert count <= 1
            if parallel and {'b' + str(n) for n in parallel}.issubset(ids):
                overlapped = True
            if all(not self.admin.get(f'job/x-ran-cloud/{n}/api/json')['building'] for n in numbers):
                break
            time.sleep(1)
        else:
            raise TimeoutError('并发测试超时')
        for n in numbers:
            self.completed(n)
        return maximum, overlapped, exclusive_max

    def concurrency(self):
        original = json.loads(self.admin.script("println(groovy.json.JsonOutput.toJson(jenkins.model.Jenkins.get().getItem('x-ran-cloud').definition.script));return null"))
        try:
            self.set_script(original.replace('def sourceType =', 'cfg.demoBuildDelaySeconds = 25\ndef sourceType =', 1))
            numbers = [self.submit('builder1' if i % 2 == 0 else 'builder2', SERVICE_TARGET=['serviceA', 'serviceB', 'serviceC'][i % 3]) for i in range(7)]
            maximum, _, _ = self.observe(numbers)
            assert maximum == 6, maximum
            self.record('七任务提交，最多六个并发', {'builds': numbers, 'maximumPods': maximum})
            if not self.args.services_only:
                self.concurrent_images()
            first = self.submit(BUILD_KIND='image', IMAGE_TAG='same-tag-ci')
            second = self.submit('builder2', BUILD_KIND='image', IMAGE_TAG='same-tag-ci', REF_TYPE='tag', GIT_REF='demo-v1.0.0')
            other = self.submit(SERVICE_TARGET='serviceB')
            _, parallel, exclusive = self.observe([first, second, other], exclusive=[first, second], parallel=[first, other])
            assert exclusive == 1 and parallel
            self.artifacts(second, self.old, 'builder2')
            self.record('同标签串行，其他目标并行', {'builds': [first, second, other]})
            number = self.submit()
            deadline = time.time() + 120
            while time.time() < deadline and not any(p['metadata']['labels'].get('xran-build') == 'b' + str(number) for p in self.pods()):
                time.sleep(1)
            self.admin.post(f'job/x-ran-cloud/{number}/stop')
            self.completed(number, 'ABORTED')
            self.record('取消任务', {'build': number})
        finally:
            self.set_script(original)

    def concurrent_images(self):
        stamp = str(int(time.time()))
        numbers = [self.submit('builder1' if i % 2 == 0 else 'builder2', BUILD_KIND='image',
            IMAGE_TARGET=self.cfg['images'][i % len(self.cfg['images'])],
            IMAGE_TAG='parallel-ci-' + stamp + '-' + str(i)) for i in range(6)]
        maximum, _, _ = self.observe(numbers)
        assert maximum == 6, maximum
        self.record('六个不同标签镜像并发构建', {'builds': numbers, 'maximumPods': maximum})

    def storage(self):
        result = self.admin.script((ROOT / 'jenkins/storage.groovy').read_text() + '\n' +
                                   (ROOT / 'tests/storage-integration.groovy').read_text())
        self.record('磁盘压力、最早回收、保留保护、审计和空间不足拒绝', json.loads(result.strip()))

    def run(self):
        try:
            self.ready()
            self.maintenance_access()
            if not self.args.permissions_only:
                self.functional()
                if not self.args.quick:
                    self.concurrency()
                    self.storage()
            self.report['status'] = 'PASS'
        except Exception as error:
            self.report.update(status='FAIL', error=str(error))
            raise
        finally:
            self.report['finishedAt'] = time.strftime('%FT%T%z')
            self.save()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', default=str(ROOT / 'config/site.yaml'))
    parser.add_argument('--source', default='/home/xiazhangtao/code/X-RAN-CLOUD')
    parser.add_argument('--quick', action='store_true')
    parser.add_argument('--services-only', action='store_true')
    parser.add_argument('--permissions-only', action='store_true', help='只检查权限，不提交构建或执行清理测试')
    parser.add_argument('--report', help='指定报告路径，避免覆盖已有完整验收报告')
    Verification(parser.parse_args()).run()
