#!/usr/bin/env python3
"""Real builds and browser checks on the existing, isolated Git-cache instance."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
from urllib.parse import quote
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
from ci import kube_env
from jenkins_api import JenkinsAPI
from reliability import source_fingerprint
from reliability_browser import Browser

NAMESPACE = 'jenkins-git-cache-verify'
FIXTURE = r'''
import io,json,subprocess,tarfile
from pathlib import Path
root=Path('/tmp/git-cache-fixtures');work=root/'X-RAN-CLOUD-work';bare=root/'X-RAN-CLOUD.git'
def git(directory,*args):
 return subprocess.check_output(['git','-C',str(directory),*args],text=True).strip()
git(work,'checkout','main')
original=(work/'services/build.sh').read_text()
assert original.count('\nname=')==1 and 'FIXTURE_BUILD_READY' in original
custom='\n'.join("name={'serviceA':'任意服务 发布.tar.gz','services_all':'all-custom-release.tar.gz'}.get(target,'other.tar.gz')" if line.startswith('name=') else line for line in original.splitlines())+'\n'
multiple="""#!/usr/bin/env python3
import io,tarfile
from pathlib import Path
d=Path('../JDBG/Release/services');d.mkdir(parents=True,exist_ok=True)
for name in ['one.tar.gz','two.tar.gz']:
 with tarfile.open(d/name,'w:gz') as archive:
  item=tarfile.TarInfo('app');item.size=3;archive.addfile(item,io.BytesIO(b'app'))
"""
scripts={'custom':custom,'missing':'#!/bin/sh\nexit 0\n','multiple':multiple,
 'corrupt':"#!/bin/sh\nmkdir -p ../JDBG/Release/services\nprintf corrupt > ../JDBG/Release/services/custom.tar.gz\n"}
pins={}
for kind,body in scripts.items():
 branch='ui-artifacts-'+kind
 git(work,'checkout','-B',branch,'main')
 (work/'services/build.sh').write_text(body)
 old=work/'JDBG/Release/services/tracked-old.tar.gz';old.parent.mkdir(parents=True,exist_ok=True)
 with tarfile.open(old,'w:gz') as archive:
  item=tarfile.TarInfo('old');item.size=3;archive.addfile(item,io.BytesIO(b'old'))
 git(work,'add','.');git(work,'commit','--allow-empty','-m','UI artifact fixture '+kind)
 pins[kind]=git(work,'rev-parse','HEAD')
git(work,'checkout','main')
git(bare,'fetch','--force',str(work),'+refs/heads/ui-artifacts-*:refs/heads/ui-artifacts-*')
print(json.dumps(pins))
'''


def kube(*args, data=None):
    return subprocess.check_output(['kubectl', *args], input=data, text=True, env=kube_env())


def description_ref(snapshot, started):
    for line in snapshot.splitlines():
        if 'link "' + started in line:
            match = re.search(r'\[ref=((?:f\d+)?e\d+)\]', line)
            if match: return match[1]
    raise AssertionError('历史描述没有可访问的原生链接: ' + started)


def dom(browser, body):
    # DOM assertions supplement ordinary CLI navigation/clicks with fresh snapshots.
    return browser.cli('run-code', 'async page => { ' + body + ' }')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', default='.state/jenkins-git-cache-verify/site.yaml')
    parser.add_argument('--compiler-image', default='localhost:5000/xran-ci/xran-compile:26.11.17-compat-xtue')
    parser.add_argument('--browser-only', action='store_true', help='复用同一源码和脚本版本下已完成的八个实际构建')
    parser.add_argument('--restart-only', action='store_true', help='页面已通过时仅补测接口恢复和重启持久化')
    args = parser.parse_args()
    cfg = yaml.safe_load(Path(args.site).read_text()); ns = cfg['namespace']
    assert ns == NAMESPACE and cfg.get('verificationInstance') is True, '只允许专用隔离实例'
    os.environ['PATH'] = str(ROOT/'.cache/tools') + os.pathsep + os.environ['PATH']
    users = json.loads((ROOT/'.state'/ns/'users.json').read_text())
    admin = JenkinsAPI(cfg['jenkinsUrl'], 'admin', users['ADMIN_PASSWORD'])
    clients = {name:JenkinsAPI(cfg['jenkinsUrl'], 'builder1', users['BUILDER1_PASSWORD'], name)
               for name in ('x-ran-cloud','xcn','xtue-cloud')}
    remote = json.loads(admin.script("println(new File('/var/xran/site.json').text);return null"))
    assert remote['namespace'] == ns and remote.get('verificationInstance') is True
    assert set(clients) <= remote['projects'].keys()
    assert not admin.get('queue/api/json')['items'], '等待隔离实例空闲再运行'
    before = admin.get('api/json?tree=jobs[name,displayName,builds[number,result]]')
    expected_names = {'x-ran-cloud':'X-RAN-CLOUD','xcn':'X-CN-CLOUD','xtue-cloud':'X-TUE-CLOUD'}
    assert all(item['displayName'] == expected_names.get(item['name'],item['name']) for item in before['jobs'])
    report = {'namespace':ns,'url':cfg['jenkinsUrl'],'fixtureOnly':True,'sourceFingerprint':source_fingerprint(),
              'compilerImage':args.compiler_image,'runtimeScriptsSha256':remote['runtimeScriptsSha256'],
              'checks':[],'builds':[]}
    browsers = []
    output = ROOT/'docs/verification/ui-artifacts.json'
    if args.browser_only or args.restart_only:
        previous = json.loads(output.read_text())
        assert previous['namespace'] == ns and previous['runtimeScriptsSha256'] == report['runtimeScriptsSha256']
        assert previous['sourceFingerprint'] == report['sourceFingerprint'] and len(previous['builds']) >= 8
        assert previous['compilerImage'] == args.compiler_image
        report = previous
        if args.browser_only:
            report['checks'] = [item for item in report['checks'] if item['name'] in (
                '旧编译镜像执行平台下发脚本及普通用户下载','单服务/全服务任意名称、旧包清理及三个项目回归')]
        for name in ('status','error'): report.pop(name,None)

    def record(name, **detail):
        report['checks'].append(dict(name=name,status='PASS',**detail));print('PASS',name,flush=True)

    def build(job, parameters, expected='SUCCESS', files=None):
        client = clients[job]
        parameters = {'COMPILER_IMAGE':args.compiler_image,'COMPILER_IMAGE_PULL_POLICY':'IfNotPresent',
                      'REF_TYPE':'branch','GIT_REF':'main',**parameters}
        number = client.wait_number(client.trigger(parameters))
        result = client.wait(number)
        assert result['result'] == expected, client.console(number)
        with client.request(client.job_path+f'{number}/artifact/publish/metadata.json') as response: meta=json.load(response)
        assert meta['runtimeScriptsSha256'] == remote['runtimeScriptsSha256']
        assert meta['builderImage'] == args.compiler_image and meta['userId'] == 'builder1'
        names = [item['name'] for item in meta['files']]
        if files is not None: assert names == files, names
        for item in meta['files']:
            with client.request(client.job_path+f'{number}/artifact/publish/'+quote(item['name'],safe='')) as response:
                digest=hashlib.sha256(response.read()).hexdigest()
            assert digest == item['sha256']
        report['builds'].append(dict(job=job,number=number,result=result['result'],files=names,commit=meta['commit']))
        record('旧编译镜像执行平台下发脚本及普通用户下载',job=job,number=number,result=expected,files=names)
        return number,meta

    def archive_page(browser, path, extensions):
        browser.goto(path)
        dom(browser, '''const views=await page.locator('#main-panel a[href$="/*view*/"]').evaluateAll(nodes=>nodes.map(a=>({href:a.getAttribute('href'),visible:!!a.getClientRects().length})));
          const extensions='''+json.dumps(extensions)+''';
          for(const extension of extensions) {
            const links=views.filter(a=>a.href.toLowerCase().endsWith(extension+'/*view*/'));
            if(!links.length || links.some(a=>a.visible)) throw new Error('压缩包预览未隐藏: '+extension);
          }
          for(const name of ['metadata.json','SHA256SUMS']) {
            if(!views.some(a=>a.href.endsWith(name+'/*view*/') && a.visible)) throw new Error('文本预览被隐藏');
          }
          const downloads=await page.locator('#main-panel a[href*="/artifact/"]').evaluateAll(nodes=>nodes.filter(a=>!a.href.includes('/*') && /\.(tar|gz|tgz)$/.test(a.pathname)).length);
          if(!downloads) throw new Error('归档下载链接丢失');''')

    def restart_and_check(browser):
        kube('-n',ns,'rollout','restart','statefulset/jenkins')
        kube('-n',ns,'rollout','status','statefulset/jenkins','--timeout=300s')
        admin.crumb = None
        deadline = time.time()+180
        while True:
            try:
                after = admin.get('api/json?tree=jobs[name,displayName,builds[number,result]]')
                break
            except (urllib.error.URLError,TimeoutError):
                if time.time() >= deadline: raise
                time.sleep(2)
        for previous in before['jobs']:
            current = next(item for item in after['jobs'] if item['name']==previous['name'])
            assert current['displayName'] == expected_names.get(current['name'],current['name'])
            # The default API list has a 100-build window. Startup maintenance
            # may move a preserved record out of that window; check it directly.
            results = {item['number']:item['result'] for item in current['builds']}
            for old in previous['builds']:
                if old['number'] not in results:
                    item = admin.get('job/'+quote(previous['name'],safe='')+f"/{old['number']}/api/json?tree=result")
                    results[old['number']] = item['result']
                assert results[old['number']] == old['result']
        snapshot = browser.goto('')
        assert all(name in snapshot for name in expected_names.values())
        archive_page(browser,'job/x-ran-cloud/', ['.tar.gz'])
        record('Controller 重启后显示名、历史、脚本和交互保留')

    try:
        if args.restart_only:
            assert any(item['name']=='参数显隐与 X-CN 历史版本参数继续可用' for item in report['checks'])
            browser = Browser(cfg,'builder1',users,ROOT/'.state'/ns);browsers.append(browser)
            restart_and_check(browser)
            report['status']='PASS'
            return
        if args.browser_only:
            def restore(index):
                item=report['builds'][index];client=clients[item['job']]
                with client.request(client.job_path+f"{item['number']}/artifact/publish/metadata.json") as response: meta=json.load(response)
                assert meta['runtimeScriptsSha256'] == remote['runtimeScriptsSha256']
                return item['number'],meta
            single,bundle,image,xcn,xtue=(restore(index) for index in (0,1,5,6,7))
        else:
            pins = json.loads(kube('-n',ns,'exec','-i','git-cache-fixture','--','python3','-',data=FIXTURE))
            single = build('x-ran-cloud',dict(BUILD_KIND='service',GIT_REF='ui-artifacts-custom',SERVICE_TARGET='serviceA'),files=['任意服务 发布.tar.gz'])
            bundle = build('x-ran-cloud',dict(BUILD_KIND='service',GIT_REF='ui-artifacts-custom',SERVICE_TARGET='services_all'),files=['all-custom-release.tar.gz'])
            assert single[1]['commit'] == bundle[1]['commit'] == pins['custom']
            for kind,files in [('missing',[]),('corrupt',[]),('multiple',['one.tar.gz','two.tar.gz'])]:
                number,meta = build('x-ran-cloud',dict(BUILD_KIND='service',GIT_REF='ui-artifacts-'+kind,SERVICE_TARGET='serviceA'),expected='FAILURE',files=files)
                assert meta['collectionErrors'] and 'tracked-old.tar.gz' not in files
            image = build('x-ran-cloud',dict(BUILD_KIND='image',IMAGE_TARGET='imageA',IMAGE_TAG='ui22-'+str(int(time.time()))),files=['imageA.tar'])
            xcn = build('xcn',dict(CHART_VERSION='26.11.18-ui22',APP_VERSION='26.11.18-ui22'))
            xtue = build('xtue-cloud',dict(SERVICE_TARGET='nrUE'),files=['tue.tar.gz'])
            record('单服务/全服务任意名称、旧包清理及三个项目回归')

        directory = ROOT/'.state'/ns
        for user in ('admin','builder1'):
            browser = Browser(cfg,user,users,directory);browsers.append(browser)
            snapshot = browser.goto('')
            assert all(name in snapshot for name in expected_names.values())
            assert ('storage-maintenance' in snapshot) == (user == 'admin')
        browser = browsers[1]
        archive_page(browser,f'job/x-ran-cloud/{image[0]}/', ['.tar'])
        archive_page(browser,f'job/x-ran-cloud/{bundle[0]}/',['.tar.gz'])
        archive_page(browser,f'job/xcn/{xcn[0]}/',['.tar','.tgz'])
        archive_page(browser,f'job/xtue-cloud/{xtue[0]}/',['.tar.gz'])
        record('两类账号显示新名称；首页及构建详情的压缩包 view 隐藏、文本预览和下载保留')

        browser.goto('job/x-ran-cloud/')
        dom(browser, '''await page.locator('#side-panel .xran-build-description-link').first().waitFor({state:'visible'});
          const descriptions=await page.locator('#side-panel .xran-build-description-link').evaluateAll(nodes=>nodes.map(a=>({href:a.href,heading:a.closest('.app-builds-container__item').querySelector('.app-builds-container__item__inner__link').href})));
          if(!descriptions.length || descriptions.some(a=>a.href!==a.heading)) throw new Error('历史描述目标不一致');''')
        snapshot = browser.snapshot()
        browser.cli('click',description_ref(snapshot,image[1]['startedAt']))
        dom(browser,f"if(new URL(page.url()).pathname!='/job/x-ran-cloud/{image[0]}/') throw new Error('历史描述点击未跳转');")
        browser.goto('job/x-ran-cloud/')
        dom(browser,"await page.locator('#side-panel .xran-build-description-link').first().waitFor({state:'visible'});")
        snapshot = browser.snapshot()
        browser.cli('eval','element => element.focus()',description_ref(snapshot,image[1]['startedAt']))
        browser.cli('press','Enter')
        dom(browser,f"if(new URL(page.url()).pathname!='/job/x-ran-cloud/{image[0]}/') throw new Error('历史描述键盘操作未跳转');")
        record('历史描述鼠标点击和键盘 Enter 均跳转对应构建')

        browser.goto('job/x-ran-cloud/')
        latest = build('x-ran-cloud',dict(BUILD_KIND='service',GIT_REF='ui-artifacts-custom',SERVICE_TARGET='serviceA'),files=['任意服务 发布.tar.gz'])
        dom(browser, "await page.waitForFunction(number=>Array.from(document.querySelectorAll('#side-panel .xran-build-description-link')).some(a=>a.pathname.endsWith('/'+number+'/')),"+str(latest[0])+",{timeout:45000});")
        snapshot = browser.snapshot()
        browser.cli('click',description_ref(snapshot,latest[1]['startedAt']))
        dom(browser,f"if(new URL(page.url()).pathname!='/job/x-ran-cloud/{latest[0]}/') throw new Error('刷新后的历史描述点击未跳转');")
        record('实际新构建触发历史列表自动刷新后描述仍可点击')

        snapshot = browser.goto('job/x-ran-cloud/build')
        assert 'SERVICE_TARGET' in snapshot and 'IMAGE_TARGET' not in snapshot
        snapshot = browser.goto(f'job/xcn/{xcn[0]}/parameters/')
        assert 'CHART_VERSION' in snapshot and 'APP_VERSION' in snapshot
        record('参数显隐与 X-CN 历史版本参数继续可用')

        restart_and_check(browser)
        report['status'] = 'PASS'
    except Exception as error:
        report.update(status='FAIL',error=str(error));raise
    finally:
        for browser in browsers: browser.close()
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')


if __name__ == '__main__':
    main()
