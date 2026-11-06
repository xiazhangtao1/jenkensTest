#!/usr/bin/env python3
"""独立 Jenkins：动态并发、入场快照、等待超时、取消与重启恢复。"""
import argparse
import base64
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.error

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from ci import kube_env
from jenkins_api import JenkinsAPI


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--site', required=True)
    p.add_argument('--skip-timeouts', action='store_true')
    args = p.parse_args()
    cfg = yaml.safe_load(Path(args.site).read_text())
    assert cfg.get('verificationInstance') is True and cfg['namespace'] != 'jenkins'
    directory = ROOT / '.state' / cfg['namespace']
    users = json.loads((directory / 'users.json').read_text())
    admin = JenkinsAPI(cfg['jenkinsUrl'], 'admin', users['ADMIN_PASSWORD'])
    clients = {name: JenkinsAPI(cfg['jenkinsUrl'], 'builder1', users['BUILDER1_PASSWORD'], job=name)
               for name in ['x-ran-cloud', 'xcn']}
    report = {'namespace': cfg['namespace'], 'checks': []}
    builds, originals = [], {}

    def state():
        return json.loads(admin.script("println(new File(jenkins.model.Jenkins.get().rootDir,'xran-platform/state.json').text);return null"))

    def save(**changes):
        s = state()
        candidate = dict(s['settings'], revision=s['version'], **{})
        candidate.update(changes)
        encoded = base64.b64encode(json.dumps(candidate).encode()).decode()
        admin.script("def c=io.xran.jenkins.BuildPlatformConfiguration.get();c.configure((org.kohsuke.stapler.StaplerRequest2)null,net.sf.json.JSONObject.fromObject(new String('" + encoded + "'.decodeBase64(),'UTF-8')));println(c.status);return null")
        applied = state()
        assert 'applyError' not in applied and applied['version'] == applied['appliedVersion'], applied
        return applied

    def record(name, details):
        report['checks'].append(dict(name=name, status='PASS', details=details))
        (directory / 'platform-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print('PASS:', name, flush=True)

    def until(check, timeout=120):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                value = check()
                if value: return value
            except (urllib.error.URLError, TimeoutError):
                pass  # The controller is deliberately restarted by this test.
            time.sleep(1)
        raise TimeoutError('构建平台验收等待超时，状态=' + json.dumps(state(), ensure_ascii=False))

    def submit(job='x-ran-cloud', **params):
        defaults = dict(COMPILER_IMAGE_PULL_POLICY='Never', REF_TYPE='branch', GIT_REF='main',
                        BUILD_KIND='service', SERVICE_TARGET='serviceA', CHART_VERSION='1.0.0', APP_VERSION='verify-' + str(time.time_ns()))
        defaults.update(params)
        number = clients[job].wait_number(clients[job].trigger(defaults))
        pair = (job, number)
        builds.append(pair)
        return pair

    def key(pair): return pair[0] + '#' + str(pair[1])

    def release(pair):
        admin.script("def r=hudson.model.Run.fromExternalizableId('" + key(pair) + "');new File(r.rootDir,'platform-release').text='release';return null")
        result = clients[pair[0]].wait(pair[1], timeout=100)
        assert result['result'] == 'SUCCESS', clients[pair[0]].console(pair[1])

    def abort(pair):
        clients[pair[0]].post(clients[pair[0]].job_path + str(pair[1]) + '/stop')
        result = clients[pair[0]].wait(pair[1], timeout=100)
        assert result['result'] == 'ABORTED', result
        until(lambda: key(pair) not in state()['leases'])

    baseline = state()['settings']
    try:
        descriptor = 'descriptorByName/io.xran.jenkins.BuildPlatformConfiguration/'
        for action in ['preview', 'reapply', 'state']:
            try:
                clients['x-ran-cloud'].post(descriptor + action)
                raise AssertionError('普通用户访问了管理员接口：' + action)
            except urllib.error.HTTPError as error:
                assert error.code == 403, (action, error.code)
        _, preview = admin.post(descriptor + 'preview', {'serviceCpu': 6})
        assert '5750m' in preview and 'service' in preview
        _, invalid = admin.post(descriptor + 'preview', {'xcnDindMemoryGiB': 8})
        assert 'error' in invalid.lower(), invalid
        record('管理员资源预览及普通用户接口拒绝', {'actions': ['preview', 'reapply', 'state']})
        for name in clients:
            originals[name] = json.loads(admin.script("println(groovy.json.JsonOutput.toJson(jenkins.model.Jenkins.get().getItem('" + name + "').definition.script));return null"))
        marker = 'def pod = XranCompiler.template('
        body = """echo('platform-verification-admitted')
        while (!new File(currentBuild.rawBuild.rootDir, 'platform-release').exists()) sleep(time: 1, unit: 'SECONDS')
        return
        """
        def short_flows():
            for name in clients:
                assert marker in originals[name]
                JenkinsAPI(cfg['jenkinsUrl'], 'admin', users['ADMIN_PASSWORD'], job=name).set_pipeline(originals[name].replace(marker, body + marker, 1))
        short_flows()
        initial = save(concurrency=6, buildTimeoutMinutes=240, agentStartupTimeoutMinutes=15)
        batch = [submit('x-ran-cloud' if i % 2 == 0 else 'xcn') for i in range(8)]
        until(lambda: len(state()['leases']) == 6)
        old = state()
        active = [pair for pair in batch if key(pair) in old['leases']]
        queued = [pair for pair in batch if pair not in active]
        reduced = save(concurrency=2, serviceCpu=6, serviceTemporaryStorageGiB=7)
        assert len(state()['leases']) == 6  # Includes locks whose labels were disabled.
        for pair in active[:4]: release(pair)
        time.sleep(3)
        s = state()
        assert len(s['leases']) == 2 and all(key(pair) not in s['leases'] for pair in queued), s
        assert all(v['settingsVersion'] == initial['version'] for v in s['leases'].values())
        release(active[4])
        until(lambda: any(key(pair) in state()['leases'] for pair in queued))
        save(concurrency=4)
        until(lambda: all(key(pair) in state()['leases'] for pair in queued))
        new = state()
        for pair in queued:
            lease = new['leases'][key(pair)]
            assert lease['settingsVersion'] >= reduced['version']
            if pair[0] == 'x-ran-cloud':
                assert lease['config']['buildCpuLimit'] == 6 and lease['config']['temporaryStorageGiB'] == 7
        record('并发 6→2→4；旧构建不变、排队构建使用新配置', {'oldVersion': initial['version'], 'newVersion': new['version'], 'builds': batch})

        # Restart real Jenkins while the durable Pipeline and admission leases are active.
        before_restart = state()
        subprocess.run(['kubectl', '-n', cfg['namespace'], 'rollout', 'restart', 'statefulset/jenkins'], env=kube_env(), check=True)
        subprocess.run(['kubectl', '-n', cfg['namespace'], 'rollout', 'status', 'statefulset/jenkins', '--timeout=240s'], env=kube_env(), check=True)
        admin.crumb = None
        for api in clients.values(): api.crumb = None
        until(lambda: state().get('appliedVersion') == before_restart['version'])
        after_restart = state()
        assert after_restart['leases'] == before_restart['leases']
        assert after_restart['settings'] == before_restart['settings'] and after_restart['audit'] == before_restart['audit']
        short_flows()
        abort(queued[0])
        release(queued[1])
        release(active[5])
        until(lambda: not state()['leases'])
        record('运行中重启保持快照；取消及完成均释放持久化槽位', {'version': after_restart['version'], 'restoredLeases': len(after_restart['leases'])})

        # A same-tag waiter holds neither a lease nor a global lock.
        save(concurrency=2)
        same = 'platform-same-' + str(time.time_ns())
        first = submit('xcn', APP_VERSION=same)
        until(lambda: key(first) in state()['leases'])
        second = submit('xcn', APP_VERSION=same)
        time.sleep(3)
        assert key(second) not in state()['leases']
        independent = submit()
        until(lambda: key(independent) in state()['leases'])
        release(independent)
        release(first)
        until(lambda: key(second) in state()['leases'])
        release(second)
        record('同标签等待不占全局容量', {'first': first, 'waiter': second, 'independent': independent})

        if not args.skip_timeouts:
            for label, same_tag in [('全局槽位', False), ('同标签', True)]:
                save(concurrency=1, buildTimeoutMinutes=240, agentStartupTimeoutMinutes=15)
                params = dict(APP_VERSION='timeout-' + str(time.time_ns())) if same_tag else {}
                first = submit('xcn' if same_tag else 'x-ran-cloud', **params)
                until(lambda: key(first) in state()['leases'])
                save(buildTimeoutMinutes=1, agentStartupTimeoutMinutes=1)
                waiter = submit('xcn' if same_tag else 'x-ran-cloud', **params)
                # Polling continues to produce visible output in the caller's log.
                deadline = time.time() + 65
                while time.time() < deadline:
                    assert clients[waiter[0]].get(clients[waiter[0]].job_path + str(waiter[1]) + '/api/json')['building']
                    time.sleep(2)
                assert key(waiter) not in state()['leases']
                release(first)
                until(lambda: key(waiter) in state()['leases'])
                release(waiter)
                record(label + '等待超过总超时后仍可正常入场', {'waiter': waiter, 'waitSeconds': 65, 'timeoutMinutes': 1})
            timed = submit()
            until(lambda: key(timed) in state()['leases'])
            admitted = state()['leases'][key(timed)]['admittedAtMillis'] / 1000
            result = clients[timed[0]].wait(timed[1], timeout=100)
            assert result['result'] == 'ABORTED' and 55 <= time.time() - admitted <= 85
            until(lambda: key(timed) not in state()['leases'])
            record('总超时从入场开始并释放槽位', {'build': timed, 'result': result['result']})
        report['status'] = 'PASS'
        (directory / 'platform-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    finally:
        admin.crumb = None
        until(lambda: admin.get('api/json'), timeout=240)
        for api in clients.values(): api.crumb = None
        for job, number in builds:
            api = clients[job]
            if api.get(api.job_path + str(number) + '/api/json')['building']:
                api.post(api.job_path + str(number) + '/stop')
                api.wait(number, timeout=100)
        for job, script in originals.items(): JenkinsAPI(cfg['jenkinsUrl'], 'admin', users['ADMIN_PASSWORD'], job=job).set_pipeline(script)
        save(**baseline)


if __name__ == '__main__':
    main()
