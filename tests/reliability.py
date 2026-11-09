#!/usr/bin/env python3
"""Version-bound regression and sustained real-Pod checks on an isolated Jenkins."""
import argparse
import base64
import copy
from collections import Counter
from contextlib import contextmanager
import datetime as dt
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.parse import urlsplit
import urllib.error
import urllib.request

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from ci import kube_env, versions
from jenkins_api import JenkinsAPI


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def source_fingerprint():
    # Reports, credentials and generated files cannot make their own evidence stale.
    paths = [ROOT / 'Makefile']
    for folder in ('jenkins', 'runtime', 'scripts', 'docker', 'config', 'plugins/xran-build-platform/src'):
        paths += [p for p in (ROOT / folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts
                  and p.name != 'site.local.yaml']
    paths += [ROOT / 'plugins/xran-build-platform/pom.xml', ROOT / 'vendor/local-path-storage.v0.0.37.yaml']
    h = hashlib.sha256()
    for p in sorted(set(paths)):
        h.update(str(p.relative_to(ROOT)).encode() + b'\0' + bytes.fromhex(digest(p)))
    return h.hexdigest()


def validate_site(cfg):
    namespace = cfg.get('namespace', '')
    require(cfg.get('verificationInstance') is True and namespace == 'jenkins-reliability-verify',
            '仅允许明确标记的 jenkins-reliability-verify 独立验收实例')
    url = urlsplit(cfg.get('jenkinsUrl', ''))
    require(url.scheme in ('http', 'https') and url.hostname and url.port == cfg.get('nodePort'),
            'Jenkins URL 必须与验收 NodePort 对应')
    require(cfg.get('demo') is True and cfg.get('gitCredentialsId', '') == '',
            '本套验收只使用独立 demo Git，不使用生产 Git 凭据')
    require(cfg.get('registry') == 'xran-registry:5000', '必须使用同 namespace 的测试 Registry')
    return namespace


def validate_soak(summary, minutes):
    require(summary['elapsedSeconds'] >= minutes * 60, '稳定性持续时间不足')
    require(summary['completed'] >= 30 and all(summary['kinds'].get(k, 0) >= 10 for k in ('service', 'image', 'xcn')),
            '稳定性任务不足：至少 30 个，三类各至少 10 个')
    require(summary['maximumLeases'] == 6 and summary['maximumRunningPods'] >= 6 and summary['maximumWaiting'] >= 2,
            '未实际观察到六并发及至少两个等待任务')
    require(summary['unexpectedFailures'] == 0, '稳定性存在非预期构建失败')


def validate_soak_evidence(summary, minutes):
    validate_soak(summary, minutes)
    samples = ROOT / summary['samples']
    require(samples.is_file() and digest(samples) == summary['samplesSha256'], '稳定性原始采样丢失或校验值变化')
    values = [json.loads(line) for line in samples.read_text().splitlines()]
    require(values and (dt.datetime.fromisoformat(values[-1]['at']) - dt.datetime.fromisoformat(values[0]['at'])).total_seconds()
            >= minutes * 60, '原始采样未覆盖要求的持续时间')
    require(len(summary['artifacts']) == summary['completed'] and
            dict(Counter(item['kind'] for item in summary['artifacts'])) == summary['kinds'], '稳定性产物证据与汇总不符')
    for key, actual in {
            'maximumLeases':max(len(v['leases']) for v in values),
            'maximumWaiting':max(len(v['running'])-len(v['leases']) for v in values),
            'maximumRunningPods':max(sum(p['phase']=='Running' for p in v['pods']) for v in values)}.items():
        require(summary[key] == actual, '稳定性并发摘要与原始采样不符：'+key)


def quantity(value):
    match = re.fullmatch(r'(\d+(?:\.\d+)?)([KMGTPE]i|[numkKMGTPE])?', str(value))
    require(match is not None, '无法比较 Kubernetes 资源数量：' + str(value))
    suffix = match[2] or ''
    factors = {'': 1, 'n': Decimal('1e-9'), 'u': Decimal('1e-6'), 'm': Decimal('.001'), 'k': 1000, 'K': 1000}
    for power, prefix in enumerate('MGTPE', 2): factors[prefix] = 1000 ** power
    for power, prefix in enumerate('KMGTPE', 1): factors[prefix + 'i'] = 1024 ** power
    return Decimal(match[1]) * factors[suffix]


def equal_resources(left, right):
    return left.keys() == right.keys() and all(
        left[name].keys() == right[name].keys() and all(
            left[name][mode].keys() == right[name][mode].keys() and all(
                quantity(value) == quantity(right[name][mode][resource]) for resource, value in left[name][mode].items())
            for mode in left[name]) for name in left)


class Suite:
    def __init__(self, args):
        self.args = args
        self.cfg = yaml.safe_load(Path(args.site).read_text())
        self.namespace = validate_site(self.cfg)
        self.directory = ROOT / '.state' / self.namespace
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'reliability-report.json'
        self.fingerprint = source_fingerprint()
        self.report = {'status': 'RUNNING', 'startedAt': self.now(), 'namespace': self.namespace,
                       'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                       'sourceFingerprint': self.fingerprint, 'phases': {},
                       'limitations': ['未访问真实内网 Gerrit/APT/Harbor', '未重启宿主机',
                                       '30 分钟稳定性不替代长期运行验证']}
        if args.resume and self.path.exists():
            previous = json.loads(self.path.read_text())
            require(previous['sourceFingerprint'] == self.fingerprint, '代码输入已变化，拒绝复用旧验收结果')
            self.report = previous
            self.report['status'] = 'RUNNING'
            self.report.pop('error', None)
        users = json.loads((self.directory / 'users.json').read_text())
        self.admin = JenkinsAPI(self.cfg['jenkinsUrl'], 'admin', users['ADMIN_PASSWORD'])
        self.clients = {(job, user): JenkinsAPI(self.cfg['jenkinsUrl'], user, users[user.upper() + '_PASSWORD'], job=job)
                        for job in ('x-ran-cloud', 'xcn') for user in ('builder1', 'builder2')}
        self.fixtures = json.loads((self.directory / 'fixtures.json').read_text())
        prior = self.report['phases'].get('isolation', {}).get('details') or {}
        self.pvc = prior.get('pvcUid') if args.resume else None
        if args.resume and prior:
            require(prior['fixtures'] == self.fixtures, '验收 Git 或测试镜像输入变化，拒绝复用旧结果')
        self.save()

    @staticmethod
    def now():
        return dt.datetime.now(dt.timezone.utc).isoformat()

    def save(self):
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.report, ensure_ascii=False, indent=2) + '\n')
        temporary.replace(self.path)

    def kube(self, *arguments, data=None):
        command = ['kubectl', '--request-timeout=30s', *map(str, arguments)]
        return subprocess.check_output(command, input=json.dumps(data) if data is not None else None,
                                       text=True, env=kube_env())

    def groovy(self, text):
        return json.loads(self.admin.script('println(groovy.json.JsonOutput.toJson({ ->\n' + text + '\n}()));return null'))

    def guard(self):
        cm = json.loads(self.kube('-n', self.namespace, 'get', 'configmap/xran-config', '-o', 'json'))
        live = json.loads(cm['data']['site.json'])
        require(live.get('namespace') == self.namespace and live.get('verificationInstance') is True,
                '实际 Jenkins 配置并非独立验收实例')
        svc = json.loads(self.kube('-n', self.namespace, 'get', 'service/jenkins', '-o', 'json'))
        require(any(p.get('nodePort') == self.cfg['nodePort'] for p in svc['spec']['ports']), '实际服务端口不匹配')
        remote = self.groovy("return new groovy.json.JsonSlurperClassic().parseText(new File('/var/xran/site.json').text)")
        require(remote['namespace'] == self.namespace and remote.get('verificationInstance') is True,
                'Jenkins URL 指向了其他实例，拒绝执行')
        pod=json.loads(self.kube('-n',self.namespace,'get','pod/jenkins-0','-o','json'))
        actual=next(c['image'] for c in pod['spec']['containers'] if c['name']=='jenkins').removeprefix('docker.io/')
        require(actual==versions()['CONTROLLER_IMAGE'],'验收 Controller 不是当前发布版本')
        actual_id=next(s['imageID'] for s in pod['status']['containerStatuses'] if s['name']=='jenkins')
        require(actual_id==remote['imageIds'][remote['controllerImage']], '实际 Controller 镜像 ID 与发布锁文件不符')
        require(self.groovy('return jenkins.model.Jenkins.get().isNoUsageStatistics() == true'),
                '离线 Jenkins 未关闭浏览器使用统计请求')
        pvc = json.loads(self.kube('-n', self.namespace, 'get', 'pvc/jenkins', '-o', 'json'))
        if self.pvc:
            require(self.pvc == pvc['metadata']['uid'], '验收 PVC 身份意外变化')
        self.pvc = pvc['metadata']['uid']
        return {'pvcUid': self.pvc, 'controllerImage': remote['controllerImage'],
                'controllerRuntimeImageId':actual_id,
                'controllerLockedImageId':remote['imageIds'][remote['controllerImage']],
                'fixtures': self.fixtures, 'sourceFingerprint': self.fingerprint}

    def state(self):
        return self.groovy("return new groovy.json.JsonSlurperClassic().parseText(new File(jenkins.model.Jenkins.get().rootDir,'xran-platform/state.json').text)")

    def settings(self, **changes):
        state = self.state()
        candidate = dict(state['settings'], revision=state['version'], **changes)
        encoded = base64.b64encode(json.dumps(candidate).encode()).decode()
        self.admin.script("io.xran.jenkins.BuildPlatformConfiguration.get().configure((org.kohsuke.stapler.StaplerRequest2)null,"
                          "net.sf.json.JSONObject.fromObject(new String('" + encoded + "'.decodeBase64(),'UTF-8')));return null")
        saved = self.state()
        require(saved['version'] == saved['appliedVersion'] and not saved.get('applyError'), '平台设置应用失败')
        return saved

    def phase(self, name, action):
        # Child regressions deliberately restart Jenkins; old CSRF crumbs expire.
        self.admin.crumb = None
        for api in self.clients.values(): api.crumb = None
        self.guard()
        old = self.report['phases'].get(name, {})
        python_inputs = hashlib.sha256(b''.join(bytes.fromhex(digest(p)) for p in sorted((ROOT/'tests').glob('*.py')))).hexdigest()
        reusable = name not in ('isolation', 'final-idle', 'primary-unchanged') and (
            name != 'python' or old.get('testInputsSha256') == python_inputs)
        if self.args.resume and old.get('status') == 'PASS' and reusable:
            details = old.get('details') or {}
            if name == 'soak':
                validate_soak_evidence(details, self.args.soak_minutes)
            if isinstance(details,dict) and details.get('log'):
                path = ROOT / details['log']
                require(path.is_file() and digest(path) == details['sha256'], '旧阶段日志丢失或校验值变化：' + name)
            print('复用同一版本已完成阶段：' + name, flush=True)
            return old.get('details')
        item = {'status': 'RUNNING', 'startedAt': self.now()}
        if name == 'python': item['testInputsSha256'] = python_inputs
        self.report['phases'][name] = item; self.save()
        print('开始：' + name, flush=True)
        try:
            item['details'] = action()
            item['status'] = 'PASS'
        except BaseException as error:
            item.update(status='FAIL', error=str(error)); raise
        finally:
            item['finishedAt'] = self.now(); self.save()
        print('PASS：' + name, flush=True)
        return item['details']

    def command(self, name, command, env=None, timeout=14400):
        log = self.directory / (name + '.log')
        started = time.time()
        with log.open('w') as output:
            completed = subprocess.run(list(map(str, command)), cwd=ROOT, env=env,
                                       stdout=output, stderr=subprocess.STDOUT, timeout=timeout)
        require(completed.returncode == 0, name + ' 失败，日志：' + str(log))
        return {'log': str(log.relative_to(ROOT)), 'sha256': digest(log), 'seconds': round(time.time() - started, 2)}

    def child(self, script, *arguments):
        return self.command(Path(script).stem, [sys.executable, ROOT / 'tests' / script, '--site', self.args.site, *arguments])

    def pipeline(self, job, script=None):
        require(job in ('x-ran-cloud', 'xcn'), '非法验收任务')
        if script is None:
            return self.groovy("return jenkins.model.Jenkins.get().getItem('" + job + "').definition.script")
        api = copy.copy(self.admin)
        api.job = job; api.job_path = 'job/' + job + '/'
        api.set_pipeline(script)

    def real_xcn(self):
        require(self.args.xcn_number or self.args.apt_mirror, '真实 xcn 回归需要 --apt-mirror 指定软件源或 --xcn-number 接续本轮构建')
        number = self.args.xcn_number
        if not number:
            parsed = urlsplit(self.args.apt_mirror)
            require(parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.query and not parsed.fragment
                    and re.fullmatch(r'[A-Za-z0-9:/._~-]+', self.args.apt_mirror), '非法验收 APT 镜像地址')
            original = self.pipeline('xcn')
            injection = "'APT_MIRROR=" + self.args.apt_mirror + "', 'PUSH_TIMEOUT=10', 'TZ=Asia/Shanghai',"
            modified = original.replace("'TZ=Asia/Shanghai',", injection, 1)
            require(modified != original, '无法设置本轮业务软件源')
            try:
                self.pipeline('xcn', modified)
                api = self.clients[('xcn', 'builder1')]
                number = api.wait_number(api.trigger(dict(REF_TYPE='commit', GIT_REF=self.fixtures['xcn'],
                    CHART_VERSION='1.9.0', APP_VERSION='reliability-real-' + str(time.time_ns()),
                    COMPILER_IMAGE=self.args.compat_image, COMPILER_IMAGE_PULL_POLICY='Never')))
            finally:
                self.pipeline('xcn', original)
        build = self.clients[('xcn', 'builder1')].get(f'job/xcn/{number}/api/json')
        parameters = {p['name']: p['value'] for action in build['actions'] for p in action.get('parameters', [])}
        require(parameters['GIT_REF'] == self.fixtures['xcn'] and parameters['COMPILER_IMAGE'] == self.args.compat_image,
                '接续构建的源码/镜像不是本轮固定输入')
        require(time.time() - build['timestamp'] / 1000 < 8 * 3600, '拒绝复用八小时以前的业务构建')
        details = self.child('verify-xcn.py', '--source', self.args.xcn_source, '--number', str(number),
                            '--ref-type', 'commit', '--ref', self.fixtures['xcn'], '--compiler-image', self.args.compat_image,
                            '--pull-policy', 'Never', '--chart-version', parameters['CHART_VERSION'],
                            '--app-version', parameters['APP_VERSION'])
        return dict(details, number=number, commit=self.fixtures['xcn'],
                    businessReportSha256=digest(self.directory / 'xcn-release.json'))

    def python_tests(self):
        result = self.command('python-tests', [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_*.py', '-v'])
        text = (ROOT / result['log']).read_text()
        match = re.search(r'Ran (\d+) tests?', text)
        require(match and re.search(r'^OK$', text, re.M), 'Python 测试未提供完整通过结果')
        return dict(result, tests=int(match[1]))

    def plugin_tests(self):
        with tempfile.TemporaryDirectory(prefix='xran-plugin-verify-') as name:
            workspace = Path(name)
            for folder in ('scripts', 'config', 'jenkins', 'plugins/xran-build-platform'):
                shutil.copytree(ROOT / folder, workspace / folder,
                                ignore=shutil.ignore_patterns('__pycache__', 'target', 'site.local.yaml'))
            cache = workspace / '.cache'; cache.mkdir()
            for folder in ('plugins', 'platform-m2'):
                (cache / folder).symlink_to(ROOT / '.cache' / folder, target_is_directory=True)
            result = self.command('plugin-tests', [sys.executable, workspace / 'scripts/platform-plugin.py', '--offline'])
            reports = list((workspace / 'plugins/xran-build-platform/target/surefire-reports').glob('TEST-*.xml'))
            import xml.etree.ElementTree as ET
            counts = Counter(); skipped = []
            for p in reports:
                node = ET.parse(p).getroot()
                for key in ('tests', 'errors', 'failures', 'skipped'): counts[key] += int(node.get(key, 0))
                for case in node.findall('testcase'):
                    if case.find('skipped') is not None:
                        skipped.append({'class': case.get('classname'), 'name': case.get('name'),
                                        'reason': case.find('skipped').get('message', '')})
            require(counts['tests'] > 0 and counts['errors'] == counts['failures'] == 0, '插件测试报告不完整或有失败')
            manifest = json.loads((cache / 'local-plugins/manifest.json').read_text())
            require(manifest['sourceSha256'] == json.loads((ROOT / 'config/platform-plugin.lock.json').read_text())['sourceSha256'],
                    '测试插件与发布插件源文件不匹配')
            return dict(result, counts=dict(counts), skipped=skipped, sourceSha256=manifest['sourceSha256'])

    def compiler_tests(self):
        result = {}
        for label, ref in [('compat', self.args.compat_image), ('sdk', self.args.sdk_image)]:
            image = json.loads(subprocess.check_output(['docker', 'image', 'inspect', ref], text=True))[0]
            result[label] = dict(self.command('compiler-' + label, ['docker', 'run', '--rm', '--network', 'none', ref,
                                                                 'bash', '/opt/xran/verify-compile.sh']), image=ref, imageId=image['Id'])
        return result

    def history(self, api=None):
        client = api or self.admin
        text = """
def j=jenkins.model.Jenkins.get()
def hash={ File f -> f.exists()?java.security.MessageDigest.getInstance('SHA-256').digest(f.bytes).encodeHex().toString():null }
def file=new File(j.rootDir,'xran-platform/state.json')
def platform=new groovy.json.JsonSlurperClassic().parseText(file.text)
def histories=['x-ran-cloud','xcn'].collectEntries{ name ->
 def job=j.getItem(name)
 [(name):job.builds.collect{ r -> [number:r.number,result:r.result?.toString(),keep:r.keepLog,
    metadata:hash(new File(r.rootDir,'xran-metadata.json')),
    artifacts:r.artifacts.collect{[name:it.relativePath,size:it.length]}] }]
}
return [settings:platform.settings,audit:platform.audit,version:platform.version,histories:histories,
 users:hudson.model.User.getAll().collect{it.id}.sort()]
"""
        return json.loads(client.script('println(groovy.json.JsonOutput.toJson({ ->\n' + text + '\n}()));return null'))

    def primary_snapshot(self):
        cfg=yaml.safe_load((ROOT/'config/site.yaml').read_text())
        namespace=cfg['namespace']
        require(namespace!=self.namespace,'主站点配置不能指向验收 namespace')
        objects=json.loads(self.kube('-n',namespace,'get','pods,pvc','--ignore-not-found','-o','json'))['items']
        selected=[{'kind':p['kind'],'name':p['metadata']['name'],'uid':p['metadata']['uid']} for p in objects
                  if p['metadata']['name'] in ('jenkins','jenkins-0')]
        result={'namespace':namespace,'objects':sorted(selected,key=lambda x:x['name'])}
        if selected:
            cm=json.loads(self.kube('-n',namespace,'get','configmap/xran-config','-o','json'))
            cfg=json.loads(cm['data']['site.json'])
            require(cfg['namespace']==namespace,'主实例 ConfigMap 身份不符')
            users=ROOT/'.state'/namespace/'users.json'
            require(users.is_file(),'缺少主实例只读核对凭据，无法完成未受影响检查')
            user=json.loads(users.read_text())
            api=JenkinsAPI(cfg['jenkinsUrl'],'admin',user['ADMIN_PASSWORD'])
            identity=json.loads(api.script("println(new File('/var/xran/site.json').text);return null"))
            require(identity['namespace']==namespace,'主站点观察地址与实际身份不符')
            history=self.history(api)
            result['historyAndSettingsSha256']=hashlib.sha256(json.dumps(history,sort_keys=True).encode()).hexdigest()
            result['businessHistories']={name:len(builds) for name,builds in history['histories'].items()}
        return result

    def primary_unchanged(self):
        current=self.primary_snapshot()
        require(current==self.report['protectedBefore'],'主 Jenkins 的 Pod、PVC、历史或网页设置发生变化')
        return current

    def wait_api(self, api=None, timeout=240):
        client = api or self.admin
        client.crumb = None
        deadline = time.monotonic() + timeout
        stable=0
        while time.monotonic() < deadline:
            try:
                client.get('api/json')
                ready=json.loads(client.script("println(groovy.json.JsonOutput.toJson("
                    "jenkins.model.Jenkins.get().getInitLevel()==hudson.init.InitMilestone.COMPLETED));return null"))
                stable=stable+1 if ready else 0
                if stable>=3:return
            except (OSError, urllib.error.URLError):
                stable=0;client.crumb=None
            time.sleep(2)
        raise TimeoutError('Controller 未在规定时间恢复')

    def faults(self):
        self.idle()
        baseline = self.state()['settings']
        original = self.pipeline('x-ran-cloud')
        api = self.clients[('x-ran-cloud', 'builder1')]
        started = []; evidence = []

        def submit(**changes):
            parameters = dict(COMPILER_IMAGE=self.args.compat_image, COMPILER_IMAGE_PULL_POLICY='Never',
                              REF_TYPE='branch', GIT_REF='main', BUILD_KIND='service', SERVICE_TARGET='serviceA')
            parameters.update(changes)
            number = api.wait_number(api.trigger(parameters)); started.append(number)
            return number

        def failed(number):
            result = api.wait(number, timeout=240)
            require(result['result'] in ('FAILURE', 'ABORTED'), '故障任务被错误标记成功')
            self.idle()
            return result['result']

        try:
            self.settings(agentStartupTimeoutMinutes=1)
            number = submit(COMPILER_IMAGE='xran-ci/reliability-nonexistent:missing')
            result = failed(number)
            require('ErrImageNeverPull' in api.console(number), '未捕获预期镜像拉取错误')
            evidence.append({'case':'missing-compiler','number':number,'result':result})
            self.settings(agentStartupTimeoutMinutes=baseline['agentStartupTimeoutMinutes'])
            hold = original.replace("stage('编译与打包') {", "stage('编译与打包') {\n echo('reliability-agent-held'); sleep(time:120,unit:'SECONDS')\n", 1)
            self.pipeline('x-ran-cloud', hold)
            number = submit()
            deadline = time.monotonic() + 150
            while time.monotonic() < deadline:
                if 'reliability-agent-held' in api.console(number): break
                time.sleep(2)
            else: raise TimeoutError('故障测试未进入构建容器')
            pods = json.loads(self.kube('-n', self.namespace, 'get', 'pods', '-l',
                         f'xran-job=x-ran-cloud,xran-build=b{number}', '-o', 'json'))['items']
            require(len(pods) == 1, '无法唯一确定待删除的测试 Pod')
            self.kube('-n', self.namespace, 'delete', 'pod', pods[0]['metadata']['name'], '--wait=false')
            evidence.append({'case':'agent-pod-deleted','number':number,'result':failed(number)})
            # OOM only a 96 MiB test container, never exhaust the host's memory.
            oom = original.replace('def yaml = groovy.json.JsonOutput.toJson(pod)',
                "pod.spec.containers.find{it.name=='builder'}.resources.limits.memory='96Mi'\n"
                "pod.spec.containers.find{it.name=='builder'}.resources.requests.memory='64Mi'\n"
                'def yaml = groovy.json.JsonOutput.toJson(pod)', 1)
            oom = oom.replace("stage('编译与打包') {", """stage('编译与打包') {
 sh '''set +e
 python3 -c 'x=bytearray(256*1024*1024)'
 status=$?
 echo "RELIABILITY_OOM_EXIT=$status"
 if [ -r /sys/fs/cgroup/memory.events ]; then cat /sys/fs/cgroup/memory.events; fi
 if [ -r /sys/fs/cgroup/memory/memory.oom_control ]; then cat /sys/fs/cgroup/memory/memory.oom_control; fi
 exit "$status"
 '''
""", 1)
            self.pipeline('x-ran-cloud', oom)
            number = submit(); result = failed(number)
            console = api.console(number)
            require('RELIABILITY_OOM_EXIT=137' in console, '未产生预期的容器内存限制退出码')
            evidence.append({'case':'builder-memory-limit','number':number,'result':result,'testLimit':'96Mi','exitCode':137})
            self.pipeline('x-ran-cloud', original)
            number = submit()
            require(api.wait(number,timeout=240)['result'] == 'SUCCESS', '故障后正常构建未恢复')
            evidence.append({'case':'normal-build-after-faults','number':number,'result':'SUCCESS'})
            self.idle()
            self.settings(serviceMemoryGiB=9)
            before = self.history()
            self.kube('-n',self.namespace,'rollout','restart','statefulset/jenkins')
            self.kube('-n',self.namespace,'rollout','status','statefulset/jenkins','--timeout=240s')
            self.wait_api()
            after = self.history()
            require(before == after, '重启后账号、网页设置或已有历史不一致')
            require(self.state()['settings']['serviceMemoryGiB'] == 9, '网页保存的 9GiB 内存被初始值覆盖')
            evidence.append({'case':'web-memory-and-history-after-restart','serviceMemoryGiB':9,
                             'historySha256':hashlib.sha256(json.dumps(after,sort_keys=True).encode()).hexdigest()})
            return evidence
        finally:
            self.wait_api()
            for number in started:
                if api.get(api.job_path+str(number)+'/api/json')['building']:
                    api.crumb=None; api.post(api.job_path+str(number)+'/stop'); api.wait(number,timeout=180)
            self.pipeline('x-ran-cloud',original)
            self.settings(**baseline)

    def backup_restore(self):
        import ci
        self.idle()
        before = self.history()
        backup = self.directory / 'reliability-home.tar.gz'
        arguments = argparse.Namespace(site=self.args.site,namespace=None,offline=True,archive=str(backup),
                    image_name=self.args.compat_image.rsplit(':',1)[0],image_tag=self.args.compat_image.rsplit(':',1)[1])
        log = self.directory/'backup-restore.log'
        restore_ns='jenkins-reliability-restore'
        require(not self.kube('get','namespace',restore_ns,'--ignore-not-found','-o','name').strip(),
                '恢复目标必须是全新 namespace，拒绝覆盖')
        services=json.loads(self.kube('get','services','-A','-o','json'))['items']
        require(not any(port.get('nodePort')==32182 for service in services for port in service['spec'].get('ports',[])),
                '备份恢复验收端口 32182 已被占用')
        self.guard()
        ci.backup(arguments)
        archive_sha=digest(backup)
        self.kube('-n',self.namespace,'rollout','status','statefulset/jenkins','--timeout=240s')
        self.wait_api()
        require(self.history()==before,'备份后原验收实例数据发生变化')
        cfg=dict(self.cfg,namespace=restore_ns,nodePort=32182,jenkinsUrl=self.cfg['jenkinsUrl'].replace(
            ':'+str(self.cfg['nodePort'])+'/',':32182/'),storageClass='jenkins-reliability-restore',demo=False)
        cfg['gitUrl']='http://xran-demo.'+self.namespace+'.svc.cluster.local:8000/X-RAN-CLOUD.git'
        cfg['xcn']=dict(cfg['xcn'],gitUrl='http://xran-demo.'+self.namespace+'.svc.cluster.local:8000/xcn.git')
        cfg['registry']='xran-registry.'+self.namespace+'.svc.cluster.local:5000'
        directory=ROOT/'.state'/restore_ns;directory.mkdir(exist_ok=True)
        shutil.copy2(self.directory/'users.json',directory/'users.json')
        (directory/'users.json').chmod(0o600)
        site=directory/'site.yaml';site.write_text(yaml.safe_dump(cfg,sort_keys=False))
        arguments.site=str(site)
        ci.render(arguments);ci.create_namespace(cfg);ci.users_secret(cfg)
        self.kube('apply','-f',directory/'resources.yaml')
        with log.open('w') as stream:
            subprocess.run(['helm','upgrade','--install','jenkins',str(ROOT/'.cache/charts'/('jenkins-'+versions()['JENKINS_CHART_VERSION']+'.tgz')),
                            '-n',restore_ns,'-f',str(directory/'values.yaml'),'--set','controller.replicas=0','--timeout','5m'],
                           env=kube_env(),check=True,stdout=stream,stderr=subprocess.STDOUT)
        node=json.loads(self.kube('-n',self.namespace,'get','pod/jenkins-0','-o','json'))['spec']['nodeName']
        self.kube('-n',restore_ns,'annotate','pvc/jenkins','volume.kubernetes.io/selected-node='+node,'--overwrite')
        self.kube('-n',restore_ns,'wait','--for=jsonpath={.status.phase}=Bound','pvc/jenkins','--timeout=120s')
        ci.restore(arguments)
        self.kube('-n',restore_ns,'rollout','status','statefulset/jenkins','--timeout=240s')
        users=json.loads((directory/'users.json').read_text())
        api=JenkinsAPI(cfg['jenkinsUrl'],'admin',users['ADMIN_PASSWORD'])
        self.wait_api(api)
        require(self.history(api)==before,'新 PVC 恢复后账号、设置、历史及产物清单不一致')
        builder=JenkinsAPI(cfg['jenkinsUrl'],'builder1',users['BUILDER1_PASSWORD'])
        number=builder.wait_number(builder.trigger(dict(COMPILER_IMAGE=self.args.compat_image,COMPILER_IMAGE_PULL_POLICY='Never',
                   REF_TYPE='commit',GIT_REF=self.fixtures['X-RAN-CLOUD'],BUILD_KIND='service',SERVICE_TARGET='serviceA')))
        require(builder.wait(number,timeout=240)['result']=='SUCCESS','恢复后实际服务构建失败')
        metadata=builder.get(builder.job_path+str(number)+'/artifact/publish/metadata.json')
        require(metadata['commit']==self.fixtures['X-RAN-CLOUD'] and metadata['userId']=='builder1','恢复后构建元数据不一致')
        with builder.request(builder.job_path+str(number)+'/artifact/publish/'+metadata['files'][0]['name']) as stream:
            content=stream.read()
        require(hashlib.sha256(content).hexdigest()==metadata['files'][0]['sha256'],'恢复后产物下载校验失败')
        self.kube('-n',restore_ns,'scale','statefulset/jenkins','--replicas=0')
        backup.unlink()
        return {'archiveSha256':archive_sha,'restoredNamespace':restore_ns,'exactHistoryRestored':True,
                'restoredServiceBuild':number,'settingsVersion':before['version'],'backupDeletedAfterValidation':True}

    @contextmanager
    def registry(self):
        port = self.args.registry_port
        log = (self.directory / 'registry-forward.log').open('w')
        process = subprocess.Popen(['kubectl', '-n', self.namespace, 'port-forward', '--address=127.0.0.1',
                                    'service/xran-registry', f'{port}:5000'], env=kube_env(), stdout=log, stderr=log)
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for _ in range(30):
                require(process.poll() is None, '测试 Registry 转发失败，检查端口占用')
                try:
                    with opener.open(f'http://127.0.0.1:{port}/v2/', timeout=2): break
                except OSError: time.sleep(1)
            else: raise TimeoutError('测试 Registry 不可达')
            refs = {}
            for name, source in [('compat', self.args.compat_image), ('sdk', self.args.sdk_image),
                                 ('legacy', self.fixtures['legacyImage']), ('incompatible', versions()['UBUNTU_IMAGE'])]:
                target = f'127.0.0.1:{port}/reliability/{name}:1'
                self.command('registry-publish-' + name, ['docker', 'tag', source, target])
                self.command('registry-push-' + name, ['docker', 'push', target])
                refs[name] = target
            yield refs
        finally:
            process.terminate()
            try: process.wait(timeout=10)
            except subprocess.TimeoutExpired: process.kill(); process.wait()
            log.close()

    def compiler_selection(self, refs):
        baseline = self.state()['settings']
        try:
            self.settings(compilerImage=self.args.compat_image, compilerImagePullPolicy='Always')
            return self.child('verify-compiler.py', '--source', self.args.source,
                              '--report', self.directory / 'compiler-selection.json',
                              '--compiler-image', refs['compat'], '--second-compiler-image', refs['sdk'],
                              '--cached-compiler-image', self.args.compat_image,
                              '--incompatible-image', refs['incompatible'])
        finally:
            self.settings(**baseline)

    def short_pipeline(self, original, job, delay):
        injection='cfg.demoBuildDelaySeconds='+str(delay)+'\n'
        if job=='xcn':injection+="if(isXcn) cfg.registry='xran-registry:5000'\n"
        modified=original.replace('def sourceType =',injection+'def sourceType =',1)
        require(modified!=original,'无法注入短负载等待')
        if job=='xcn':
            marker='def yaml = groovy.json.JsonOutput.toJson(pod)'
            require(marker in modified,'无法配置测试 DinD Registry')
            modified=modified.replace(marker,
                "pod.spec.containers.find{it.name=='dind'}.args=['--insecure-registry=xran-registry:5000']\n"+marker,1)
        return modified

    def resource_profiles(self):
        self.idle()
        baseline=self.state()['settings']
        originals={job:self.pipeline(job) for job in ('x-ran-cloud','xcn')}
        profiles={'service':(5,9,8),'image':(6,10,12),'xcn':(4,8,20)}
        changes={kind+suffix:value for kind,values in profiles.items()
                 for suffix,value in zip(('Cpu','MemoryGiB','TemporaryStorageGiB'),values)}
        active=[];observed={};proof={}
        try:
            version=self.settings(**changes)['version']
            for job,text in originals.items():
                self.pipeline(job,self.short_pipeline(text,job,8))
            for kind in profiles:
                job='xcn' if kind=='xcn' else 'x-ran-cloud';api=self.clients[(job,'builder1')]
                tag='resources-'+str(time.time_ns())
                parameters=dict(COMPILER_IMAGE=self.args.compat_image,COMPILER_IMAGE_PULL_POLICY='Never',
                                REF_TYPE='branch',GIT_REF='reliability-soak' if kind=='xcn' else 'main',
                                BUILD_KIND='image' if kind=='image' else 'service',SERVICE_TARGET='serviceA',
                                IMAGE_TARGET='imageA',IMAGE_TAG=tag,CHART_VERSION='1.0.0',APP_VERSION=tag)
                number=api.wait_number(api.trigger(parameters))
                active.append(dict(kind=kind,job=job,user='builder1',number=number,tag=tag,chart='1.0.0'))
            deadline=time.monotonic()+300
            while time.monotonic()<deadline:
                value=self.sample()
                for item in active:
                    key=item['job']+'#'+str(item['number'])
                    if key in observed:continue
                    for pod in value['pods']:
                        labels=pod['metadata']['labels']
                        if labels.get('xran-job')!=item['job'] or labels.get('xran-build')!='b'+str(item['number']):continue
                        status=next((s for s in pod['status'].get('containerStatuses',[]) if s['name']=='builder'),{})
                        if not status.get('imageID'):continue
                        allocation={c['name']:c['resources'] for c in pod['spec']['containers']}
                        selected=next(s for k,s in self.state()['leases'].items() if k==key)
                        require(selected['settingsVersion']==version and equal_resources(allocation,selected['resourceAllocation']),
                                '实际 Pod 未使用已保存的资源配置')
                        require(pod['metadata']['annotations']['xran-ci/settings-version']==str(version),'Pod 配置版本标注错误')
                        totals={r:sum(quantity(c['limits'][r]) for c in allocation.values()) for r in ('cpu','memory','ephemeral-storage')}
                        cpu,memory,temp=profiles[item['kind']]
                        require(totals=={'cpu':cpu,'memory':memory*1024**3,'ephemeral-storage':temp*1024**3},'实际 Pod 总资源不符')
                        workspace=next(v for v in pod['spec']['volumes'] if v['name']=='workspace-volume')
                        require(quantity(workspace['emptyDir']['sizeLimit'])==totals['ephemeral-storage'],'工作区上限不符')
                        require(pod['spec']['automountServiceAccountToken'] is False,'构建 Pod 意外自动挂载 API token')
                        observed[key]={'imageId':status['imageID'],'allocation':allocation}
                        proof[item['kind']]={'pod':pod['metadata']['name'],'cpu':cpu,'memoryGiB':memory,
                                             'temporaryStorageGiB':temp,'settingsVersion':version}
                        if item['kind']=='xcn':
                            init=next(c for c in pod['spec']['initContainers'] if c['name']=='xcn-bases')
                            bases=next(v for v in pod['spec']['volumes'] if v['name']=='xcn-bases')
                            init_memory=quantity(init['resources']['limits']['memory'])
                            require(quantity(bases['emptyDir']['sizeLimit'])<=init_memory<=totals['memory'],
                                    '基础镜像复制缓存预算不足或增加了 Pod 总内存预算')
                            proof[item['kind']]['baseInitResources']=init['resources']
                if all(not self.clients[(i['job'],i['user'])].get(
                        'job/'+i['job']+'/'+str(i['number'])+'/api/json')['building'] for i in active):break
                time.sleep(1)
            else:raise TimeoutError('实际资源配置验收构建超时')
            for item in active:
                api=self.clients[(item['job'],item['user'])]
                if api.get(api.job_path+str(item['number'])+'/api/json')['result']!='SUCCESS':
                    (self.directory/(item['job']+'-resources-failure.log')).write_text(api.console(item['number']))
                    raise RuntimeError('实际资源验收构建失败：'+item['job']+'#'+str(item['number']))
                proof[item['kind']]['artifactValidation']=self.validate_artifacts(item,observed.get(item['job']+'#'+str(item['number'])))
                require(proof[item['kind']]['artifactValidation']['settingsVersion']==version,'产物配置版本与入场分配不符')
            self.idle()
            return proof
        finally:
            for item in active:
                api=self.clients[(item['job'],item['user'])]
                if api.get(api.job_path+str(item['number'])+'/api/json')['building']:
                    api.post(api.job_path+str(item['number'])+'/stop');api.wait(item['number'],timeout=180)
            for job,text in originals.items():self.pipeline(job,text)
            self.settings(**baseline)

    def idle(self, timeout=180):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = self.sample()
            if not value['leases'] and not value['running'] and not value['pods'] and not value['locked'] and not value['reservations']:
                return value
            time.sleep(2)
        raise TimeoutError('构建、临时 Pod 或持久化槽位未释放：' + json.dumps(value, ensure_ascii=False))

    def sample(self):
        value = self.groovy("""
def j=jenkins.model.Jenkins.get()
def state=new groovy.json.JsonSlurperClassic().parseText(new File(j.rootDir,'xran-platform/state.json').text)
def file=new File(j.rootDir,'xran-storage/reservations.json')
def reservations=file.exists()?new groovy.json.JsonSlurperClassic().parseText(file.text):[:]
def heap=java.lang.management.ManagementFactory.memoryMXBean.heapMemoryUsage
return [leases:state.leases.keySet().toList(), settingsVersion:state.version,
 running:['x-ran-cloud','xcn'].collectMany{ name -> j.getItem(name).builds.findAll{it.isBuilding()}.collect{it.externalizableId}},
 locked:org.jenkins.plugins.lockableresources.LockableResourcesManager.get().resources.findAll{it.isLocked()}.collect{it.name},
 reservations:reservations.keySet().toList(), heapUsed:heap.used, heapCommitted:heap.committed,
 homeFree:j.rootDir.usableSpace, homeTotal:j.rootDir.totalSpace,
 threads:java.lang.management.ManagementFactory.threadMXBean.threadCount]
""")
        pods = json.loads(self.kube('-n', self.namespace, 'get', 'pods', '-o', 'json'))['items']
        value['pods'] = [p for p in pods if p['metadata'].get('labels', {}).get('xran-ci') == 'build']
        controller = next(p for p in pods if p['metadata']['name'] == 'jenkins-0')
        value['controllerUid'] = controller['metadata']['uid']
        value['controllerRestarts'] = sum(s.get('restartCount', 0) for s in controller['status'].get('containerStatuses', []))
        value['at'] = self.now()
        return value

    def validate_artifacts(self, item, observed):
        api = self.clients[(item['job'], item['user'])]
        prefix = api.job_path + str(item['number']) + '/artifact/publish/'
        meta = api.get(prefix + 'metadata.json')
        expected = self.fixtures['soak' if item['kind'] == 'xcn' else 'X-RAN-CLOUD']
        require(meta['commit'] == expected and meta['userId'] == item['user'] and meta['result'] == 'SUCCESS',
                '构建来源、操作者或结果与产物元数据不一致')
        require(meta['builderImage'] == self.args.compat_image and meta['builderImagePullPolicy'] == 'Never', '编译环境不一致')
        require(meta.get('startedAt') and meta.get('finishedAt') and meta['durationMillis'] > 0, '缺少构建时间')
        require(observed and meta['builderImageId'] == observed['imageId'], '未捕获实际 Pod 镜像 ID 或与元数据不符')
        require(equal_resources(meta['resourceAllocation'], observed['allocation']), '实际 Pod 资源与元数据不一致')
        wanted = 4 if item['kind'] == 'xcn' else 1
        require(len(meta['files']) == wanted, '产物数量不符')
        with api.request(prefix + 'SHA256SUMS') as response:
            sums = response.read().decode()
        for record in meta['files']:
            require(record['name'] == Path(record['name']).name, '非法产物路径')
            with tempfile.TemporaryFile() as output:
                h = hashlib.sha256(); size = 0
                with api.request(prefix + record['name']) as response:
                    while True:
                        block = response.read(1024 * 1024)
                        if not block: break
                        h.update(block); size += len(block); output.write(block)
                require(h.hexdigest() == record['sha256'] and size == record['size'], '下载校验失败')
                require(f"{record['sha256']}  {record['name']}" in sums, 'SHA256SUMS 与元数据不一致')
                output.seek(0)
                with tarfile.open(fileobj=output, mode='r:*') as archive:
                    if record['name'].endswith('.tar'):
                        manifest = json.load(archive.extractfile('manifest.json'))
                        image = manifest[0]
                        ref = (f"xran-registry:5000/xcn/xcn-{record['name'][4:-4]}:{item['tag']}"
                               if item['kind'] == 'xcn' else f"xran-registry:5000/xran-demo/imagea:{item['tag']}")
                        require(ref in image['RepoTags'], '镜像归档标签错误')
                        require(all(archive.getmember(layer).isfile() for layer in image['Layers']), '镜像归档缺少层')
                        image_id = 'sha256:' + hashlib.sha256(archive.extractfile(image['Config']).read()).hexdigest()
                        expected_id = next(x['imageId'] for x in meta['images'] if x['reference'] == ref) if item['kind'] == 'xcn' else meta['imageId']
                        require(image_id == expected_id, '镜像包实际 ID 与元数据不一致')
                    elif item['kind'] == 'service':
                        records = [json.load(archive.extractfile(m)) for m in archive if m.name.endswith('/build.json')]
                        require(records and all(r['commit'] == expected for r in records), '服务包内 commit 不一致')
                    else:
                        chart = yaml.safe_load(archive.extractfile('xcn/Chart.yaml'))
                        require(chart['version'] == item['chart'] and chart['appVersion'] == item['tag'], 'Chart 版本错误')
        return {'job': item['job'], 'number': item['number'], 'kind': item['kind'], 'commit': expected,
                'builderImageId': meta['builderImageId'], 'files': meta['files'], 'settingsVersion': meta['settingsVersion']}

    def soak(self):
        self.idle()
        self.settings(concurrency=6, xranAdminOnly=False, xcnAdminOnly=False)
        originals = {}
        for job in ('x-ran-cloud', 'xcn'):
            original = self.pipeline(job)
            originals[job] = original
            self.pipeline(job, self.short_pipeline(original,job,20))
        active = []; completed = []; observed = {}; index = 0
        summary = {'elapsedSeconds': 0, 'completed': 0, 'kinds': {}, 'maximumLeases': 0, 'maximumRunningPods': 0,
                   'maximumWaiting': 0, 'unexpectedFailures': 0}
        samples = self.directory / 'soak-samples.jsonl'
        deadline = None; baseline = self.sample()
        warmup = time.monotonic() + 300
        last_print = 0; measurements = []
        try:
            with samples.open('w') as stream:
                while deadline is None or time.monotonic() < deadline or active:
                    submitting = deadline is None or time.monotonic() < deadline
                    while submitting and len(active) < 8:
                        kind = ('service', 'image', 'xcn')[index % 3]
                        user = 'builder1' if index % 2 == 0 else 'builder2'
                        job = 'xcn' if kind == 'xcn' else 'x-ran-cloud'
                        tag = 'soak-' + str(time.time_ns())
                        params = dict(COMPILER_IMAGE=self.args.compat_image, COMPILER_IMAGE_PULL_POLICY='Never',
                                      REF_TYPE='branch', GIT_REF='reliability-soak' if kind == 'xcn' else 'main',
                                      BUILD_KIND='image' if kind == 'image' else 'service', SERVICE_TARGET='serviceA',
                                      IMAGE_TARGET='imageA', IMAGE_TAG=tag, CHART_VERSION='1.0.0', APP_VERSION=tag)
                        api = self.clients[(job, user)]
                        queue = api.trigger(params)
                        active.append(dict(job=job, user=user, kind=kind, queue=queue, tag=tag, chart='1.0.0',
                                           number=None, submitted=time.monotonic()))
                        index += 1
                    value = self.sample()
                    require(value['controllerUid'] == baseline['controllerUid'] and value['controllerRestarts'] == baseline['controllerRestarts'],
                            '稳定性阶段 Controller 非预期重启')
                    require(len(value['leases']) <= 6, '超过全局六并发上限')
                    for p in value['pods']:
                        require(p['status'].get('reason') != 'Evicted', '稳定性构建 Pod 被驱逐')
                        statuses=p['status'].get('containerStatuses', [])+p['status'].get('initContainerStatuses', [])
                        for s in statuses:
                            require(s.get('restartCount', 0) == 0 and all(
                                    s.get(key, {}).get('terminated', {}).get('reason') != 'OOMKilled' for key in ('state','lastState')),
                                    '稳定性构建容器重启或 OOM')
                        labels = p['metadata']['labels']; number = labels.get('xran-build', '')[1:]
                        status = next((s for s in p['status'].get('containerStatuses', []) if s['name'] == 'builder'), {})
                        if number and status.get('imageID'):
                            observed[labels['xran-job'] + '#' + number] = {
                                'imageId': status['imageID'], 'allocation': {c['name']: c['resources'] for c in p['spec']['containers']}}
                    waiting = len(value['running']) - len(value['leases'])
                    summary['maximumLeases'] = max(summary['maximumLeases'], len(value['leases']))
                    summary['maximumRunningPods'] = max(summary['maximumRunningPods'],
                        sum(p['status'].get('phase') == 'Running' for p in value['pods']))
                    summary['maximumWaiting'] = max(summary['maximumWaiting'], waiting)
                    measurements.append({key:value[key] for key in ('heapUsed', 'threads', 'homeFree')})
                    if deadline is None:
                        require(time.monotonic() < warmup, '未在五分钟内形成六并发负载')
                        if len(value['leases']) == 6 and waiting >= 2:
                            started = time.monotonic(); deadline = started + self.args.soak_minutes * 60
                    value['pods'] = [{'name': p['metadata']['name'], 'phase': p['status'].get('phase'),
                                      'deleting':bool(p['metadata'].get('deletionTimestamp')),
                                      'initStatuses':p['status'].get('initContainerStatuses',[])} for p in value['pods']]
                    stream.write(json.dumps(value) + '\n'); stream.flush()
                    for item in list(active):
                        api = self.clients[(item['job'], item['user'])]
                        require(time.monotonic() - item['submitted'] < 600, '稳定性单任务超过十分钟')
                        if item['number'] is None:
                            queue = api.get(item['queue'].rstrip('/') + '/api/json')
                            require(not queue.get('cancelled'), '稳定性任务被取消')
                            if not queue.get('executable'): continue
                            item['number'] = queue['executable']['number']
                        build = api.get(api.job_path + str(item['number']) + '/api/json')
                        if build['building']: continue
                        if build['result'] != 'SUCCESS':
                            summary['unexpectedFailures'] += 1
                            (self.directory / 'soak-failure.log').write_text(api.console(item['number']))
                            raise RuntimeError('稳定性构建失败：' + item['job'] + '#' + str(item['number']))
                        completed.append(self.validate_artifacts(item, observed.get(item['job'] + '#' + str(item['number']))))
                        active.remove(item)
                    summary.update(completed=len(completed), kinds=dict(Counter(c['kind'] for c in completed)),
                                   elapsedSeconds=round(min(time.monotonic() - started, self.args.soak_minutes * 60), 3) if deadline else 0)
                    (self.directory / 'soak-progress.json').write_text(json.dumps(summary, indent=2) + '\n')
                    if time.monotonic() - last_print >= 30:
                        print('稳定性：' + json.dumps(summary, ensure_ascii=False), flush=True); last_print = time.monotonic()
                    if active or submitting: time.sleep(5)
            final=self.idle()
            require(final['controllerUid']==baseline['controllerUid'] and final['controllerRestarts']==baseline['controllerRestarts'],
                    '稳定性收尾时 Controller 非预期重启')
            with samples.open('a') as stream:stream.write(json.dumps(final)+'\n')
            measurements.append({key:final[key] for key in ('heapUsed','threads','homeFree')})
            summary['elapsedSeconds']=round(time.monotonic()-started,3)
            validate_soak(summary, self.args.soak_minutes)
            result = dict(summary, artifacts=completed, samples=str(samples.relative_to(ROOT)), samplesSha256=digest(samples),
                          observations=len(measurements), resourceTrends={
                              key:{'first':measurements[0][key], 'last':measurements[-1][key],
                                   'minimum':min(m[key] for m in measurements), 'maximum':max(m[key] for m in measurements)}
                              for key in ('heapUsed', 'threads', 'homeFree')})
            validate_soak_evidence(result, self.args.soak_minutes)
            return result
        finally:
            for item in active:
                api = self.clients[(item['job'], item['user'])]
                try:
                    if item['number'] is None: api.post('queue/cancelItem', {'id': item['queue'].rstrip('/').split('/')[-1]})
                    else: api.post(api.job_path + str(item['number']) + '/stop')
                except Exception as error: print('清理测试构建失败：' + str(error), file=sys.stderr)
            for job, original in originals.items():
                self.pipeline(job, original)

    def run(self):
        try:
            for executable in ('node', 'node_modules/@playwright/cli/playwright-cli.js',
                               'chromium/chrome-headless-shell-linux64/chrome-headless-shell'):
                require((ROOT/'.cache/verification-tools'/executable).is_file(),
                        '缺少完整验收浏览器缓存，先准备可选 verification-tools，见 docs/reliability.md')
            if self.args.resume:
                previous = self.report['phases'].get('compiler', {}).get('details') or {}
                for label, ref in [('compat',self.args.compat_image), ('sdk',self.args.sdk_image)]:
                    if label in previous:
                        current = json.loads(subprocess.check_output(['docker','image','inspect',ref],text=True))[0]
                        require(previous[label]['image'] == ref and previous[label]['imageId'] == current['Id'],
                                '编译镜像引用或实际 ID 已变化，拒绝复用旧验收：'+label)
            self.phase('isolation', self.guard)
            if 'protectedBefore' not in self.report:
                self.report['protectedBefore']=self.primary_snapshot();self.save()
            self.phase('python', self.python_tests)
            self.phase('plugin', self.plugin_tests)
            self.phase('compiler', self.compiler_tests)
            self.phase('functional', lambda: self.child('verify-build-compatibility.py', '--compat-image', self.args.compat_image,
                       '--sdk-image', self.args.sdk_image, '--pull-policy', 'Never', '--source', self.args.source))
            self.phase('real-xcn', self.real_xcn)
            self.phase('parameters', lambda: self.child('verify-parameters.py'))
            self.phase('admission-restart', lambda: self.child('verify-platform.py'))
            self.phase('shared-storage', lambda: self.child('verify-shared.py'))
            self.phase('resource-profiles',self.resource_profiles)
            self.phase('faults-restart',self.faults)
            with self.registry() as refs:
                self.phase('compiler-selection', lambda: self.compiler_selection(refs))
                self.phase('artifact-visibility', lambda: self.child('verify-artifact-visibility.py', '--compat-image', refs['compat'],
                           '--sdk-image', refs['sdk'], '--old-image', refs['legacy'], '--source', self.args.source))
            self.phase('browser',lambda:self.child('reliability_browser.py','--xcn-number',
                       str(self.report['phases']['real-xcn']['details']['number'])))
            self.phase('backup-restore',self.backup_restore)
            self.phase('soak', self.soak)
            self.phase('final-idle', self.idle)
            self.phase('primary-unchanged',self.primary_unchanged)
            require(source_fingerprint() == self.fingerprint, '验收期间发布源文件发生变化')
            require(all(p['status'] == 'PASS' for p in self.report['phases'].values()), '存在未完成或失败的阶段')
            self.report['status'] = 'PASS'
        except BaseException as error:
            self.report.update(status='FAIL', error=str(error)); raise
        finally:
            self.report['finishedAt'] = self.now(); self.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', required=True)
    parser.add_argument('--source', default=str(ROOT.parent / 'X-RAN-CLOUD'))
    parser.add_argument('--xcn-source', default=str(ROOT.parent / 'xcn'))
    parser.add_argument('--compat-image', required=True)
    parser.add_argument('--sdk-image', required=True)
    parser.add_argument('--soak-minutes', type=int, default=30)
    parser.add_argument('--registry-port', type=int, default=15060)
    parser.add_argument('--apt-mirror', default='')
    parser.add_argument('--xcn-number', type=int)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    require(args.soak_minutes >= 30, '完整验收的稳定性时长至少 30 分钟')
    Suite(args).run()


if __name__ == '__main__':
    main()
