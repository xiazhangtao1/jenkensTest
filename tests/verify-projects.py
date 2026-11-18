#!/usr/bin/env python3
"""Isolated Jenkins: configured service targets, refs, downloads, ACL and restart."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
from ci import kube_env
from jenkins_api import JenkinsAPI

FIXTURE = r'''import io,json,subprocess,os
from pathlib import Path
from functools import partial
from http.server import ThreadingHTTPServer,SimpleHTTPRequestHandler
root=Path('/tmp/xtue-fixture');repo=root/'repo';(repo/'cmake_targets').mkdir(parents=True)
def git(*args):return subprocess.check_output(['git','-C',str(repo),*args],text=True).strip()
git('init','-b','main');git('config','user.name','CI fixture');git('config','user.email','fixture@example.invalid')
script=repo/'cmake_targets/build_oai'
script.write_text("""#!/usr/bin/env python3
import io,json,tarfile,sys,subprocess
assert sys.argv[1]=='-k'
name={'--nrUE':'tue.tar.gz','--tuePhy':'tue_simuphy.tar.gz'}[sys.argv[2]]
data=json.dumps({'target':sys.argv[2],'commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()}).encode()
with tarfile.open(name,'w:gz') as archive:
 m=tarfile.TarInfo('fixture.json');m.size=len(data);archive.addfile(m,io.BytesIO(data))
""")
script.chmod(0o755);(repo/'VERSION').write_text('1');git('add','.');git('commit','-m','fixture v1');git('tag','fixture-v1')
old=git('rev-parse','HEAD');(repo/'VERSION').write_text('2');git('commit','-am','fixture v2');head=git('rev-parse','HEAD')
for branch,body in [('missing-output','#!/bin/sh\nexit 0\n'),('corrupt-output','#!/bin/sh\nprintf bad > tue.tar.gz\n'),('script-failure','#!/bin/sh\nexit 3\n'),('slow-output','#!/bin/sh\nsleep 120\nexit 0\n')]:
 git('checkout','-b',branch,head);script.write_text(body)
 (repo/'cmake_targets/tue.tar.gz').write_bytes(b'stale archive from repository')
 git('add','.');git('commit','-m',branch)
git('checkout','main')
subprocess.check_call(['git','clone','--bare',str(repo),str(root/'XTUE-CLOUD.git')]);subprocess.check_call(['git','-C',str(root/'XTUE-CLOUD.git'),'update-server-info'])
(root/'pins.json').write_text(json.dumps({'old':old,'head':head}))
ThreadingHTTPServer(('0.0.0.0',8000),partial(SimpleHTTPRequestHandler,directory=str(root))).serve_forever()
'''


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--site',required=True);p.add_argument('--compiler-image',required=True)
    p.add_argument('--fixture-image',default='192.168.10.35/xnet/xran-compile:26.11.08-compat-r2')
    args=p.parse_args();site=yaml.safe_load(Path(args.site).read_text());ns=site['namespace']
    if not site.get('verificationInstance') or ns=='jenkins':raise RuntimeError('仅允许明确标记的独立验收实例')
    directory=ROOT/'.state'/ns;users=json.loads((directory/'users.json').read_text())
    admin=JenkinsAPI(site['jenkinsUrl'],'admin',users['ADMIN_PASSWORD'],'xtue-cloud')
    builder=JenkinsAPI(site['jenkinsUrl'],'builder1',users['BUILDER1_PASSWORD'],'xtue-cloud')
    def kube(*args,**kw):return subprocess.check_output(['kubectl',*args],env=kube_env(),**kw)
    def wait_api():
        admin.crumb=None;builder.crumb=None
        deadline=time.time()+240
        while time.time()<deadline:
            try:
                if 'xtue-cloud' in [x['name'] for x in admin.get('api/json?tree=jobs[name]')['jobs']]:return
            except (urllib.error.URLError,TimeoutError):pass
            time.sleep(2)
        raise TimeoutError('新 Controller 尚未就绪')
    manifest=[{'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'xtue-fixture','namespace':ns},'data':{'fixture.py':FIXTURE}},
      {'apiVersion':'v1','kind':'Pod','metadata':{'name':'xtue-fixture','namespace':ns,'labels':{'app':'xtue-fixture'}},'spec':{
       'automountServiceAccountToken':False,'securityContext':{'runAsUser':1000,'runAsGroup':1000},
       'containers':[{'name':'fixture','image':args.fixture_image,'imagePullPolicy':'Never','command':['python3','/fixture/fixture.py'],
        'resources':{'requests':{'cpu':'100m','memory':'128Mi'},'limits':{'cpu':'1','memory':'512Mi'}},'volumeMounts':[{'name':'script','mountPath':'/fixture','readOnly':True}]}],
       'volumes':[{'name':'script','configMap':{'name':'xtue-fixture'}}]}},
      {'apiVersion':'v1','kind':'Service','metadata':{'name':'xtue-fixture','namespace':ns},'spec':{'selector':{'app':'xtue-fixture'},'ports':[{'port':8000,'targetPort':8000}]}}]
    kube('apply','-f','-',input=yaml.safe_dump_all(manifest).encode());kube('-n',ns,'wait','--for=condition=Ready','pod/xtue-fixture','--timeout=120s')
    pins=None
    for _ in range(30):
        result=subprocess.run(['kubectl','-n',ns,'exec','xtue-fixture','--','cat','/tmp/xtue-fixture/pins.json'],env=kube_env(),capture_output=True,text=True)
        if result.returncode==0:pins=json.loads(result.stdout);break
        time.sleep(1)
    if pins is None:raise RuntimeError('Git fixture 未初始化')
    wait_api();report={'namespace':ns,'fixtureOnly':True,'checks':[],'builds':[]}
    def record(name,**details):report['checks'].append(dict(name=name,status='PASS',**details));print('PASS',name,flush=True)
    definitions=admin.get('job/xtue-cloud/api/json?tree=property[parameterDefinitions[name,choices,defaultParameterValue[value]]]')['property']
    names=[x['name'] for prop in definitions for x in prop.get('parameterDefinitions',[])]
    assert names==['COMPILER_IMAGE','COMPILER_IMAGE_PULL_POLICY','REF_TYPE','GIT_REF','SERVICE_TARGET'],names
    record('XTUE 专用五参数，无镜像/Chart 字段')
    with admin.request('configure') as response:
        page=response.read().decode();assert 'X-TUE-CLOUD 仅管理员可见' in page
    record('管理员系统表单动态渲染 XTUE 权限开关')
    for kind,ref,sha in [('branch','main',pins['head']),('tag','fixture-v1',pins['old']),('commit',pins['old'],pins['old']),('commit',pins['head'][:9],pins['head'])]:
      for target,artifact in [('nrUE','tue.tar.gz'),('tuePhy','tue_simuphy.tar.gz')]:
        number=builder.wait_number(builder.trigger(dict(COMPILER_IMAGE=args.compiler_image,COMPILER_IMAGE_PULL_POLICY='IfNotPresent',REF_TYPE=kind,GIT_REF=ref,SERVICE_TARGET=target)))
        result=builder.wait(number,timeout=600)
        if result['result']!='SUCCESS':raise AssertionError(builder.console(number))
        with builder.request(f'job/xtue-cloud/{number}/artifact/publish/metadata.json') as r:meta=json.load(r)
        assert meta['commit']==sha and meta['userId']=='builder1' and meta['project']=='xtue-cloud'
        assert [x['name'] for x in meta['files']]==[artifact]
        with builder.request(f'job/xtue-cloud/{number}/artifact/publish/{artifact}') as r:payload=r.read()
        assert hashlib.sha256(payload).hexdigest()==meta['files'][0]['sha256']
        report['builds'].append({'number':number,'refType':kind,'target':target,'commit':sha,'sha256':meta['files'][0]['sha256']})
        record('精确引用构建及普通用户下载',number=number,refType=kind,target=target)
    for branch in ('missing-output','corrupt-output','script-failure'):
        number=builder.wait_number(builder.trigger(dict(COMPILER_IMAGE=args.compiler_image,COMPILER_IMAGE_PULL_POLICY='IfNotPresent',REF_TYPE='branch',GIT_REF=branch,SERVICE_TARGET='nrUE')))
        assert builder.wait(number,timeout=600)['result']=='FAILURE'
        items=builder.get(f'job/xtue-cloud/{number}/api/json?tree=artifacts[fileName]')['artifacts']
        assert 'tue.tar.gz' not in [item['fileName'] for item in items]
        record('异常产物拒绝且旧包不发布',number=number,branch=branch)
    original=json.loads(admin.script('println(io.xran.jenkins.BuildPlatformConfiguration.get().form.toString());return null'))
    def settings(values):
        encoded=base64.b64encode(json.dumps(values).encode()).decode()
        admin.script("def c=io.xran.jenkins.BuildPlatformConfiguration.get();def f=c.form;f.putAll(new groovy.json.JsonSlurperClassic().parseText(new String('"+encoded+"'.decodeBase64(),'UTF-8')));f.revision=c.revision;c.configure((org.kohsuke.stapler.StaplerRequest2)null,f);return null")
    xran=JenkinsAPI(site['jenkinsUrl'],'builder1',users['BUILDER1_PASSWORD'],'x-ran-cloud')
    try:
        settings({'concurrency':1,'buildTimeoutMinutes':1,'agentStartupTimeoutMinutes':1})
        number=builder.wait_number(builder.trigger(dict(COMPILER_IMAGE=args.compiler_image,COMPILER_IMAGE_PULL_POLICY='IfNotPresent',REF_TYPE='branch',GIT_REF='slow-output',SERVICE_TARGET='nrUE')))
        for _ in range(60):
            state=json.loads(admin.script("println(new File(jenkins.model.Jenkins.get().rootDir,'xran-platform/state.json').text);return null"))
            if f'xtue-cloud#{number}' in state['leases']:break
            time.sleep(1)
        else:raise AssertionError('XTUE 未获得共享槽位')
        queued=xran.wait_number(xran.trigger(dict(COMPILER_IMAGE=args.compiler_image,COMPILER_IMAGE_PULL_POLICY='IfNotPresent',REF_TYPE='branch',GIT_REF='main',BUILD_KIND='service',SERVICE_TARGET='serviceA')))
        state=json.loads(admin.script("println(new File(jenkins.model.Jenkins.get().rootDir,'xran-platform/state.json').text);return null"))
        assert f'x-ran-cloud#{queued}' not in state['leases']
        assert builder.wait(number,timeout=180)['result'] in ('ABORTED','FAILURE')
        assert xran.wait(queued,timeout=180)['result']=='SUCCESS'
        record('XTUE 总超时释放共享槽位，等待中的 X-RAN 服务随后成功',xtue=number,xran=queued)
    finally:settings(original)
    # Validate catalog-driven pressure candidates without deleting existing lab history.
    result=json.loads(admin.script("def t=new GroovyShell(jenkins.model.Jenkins.get().pluginManager.uberClassLoader).evaluate(new File('/opt/xran/storage.groovy').text+'\\nreturn XranStorage');println(groovy.json.JsonOutput.toJson(t.candidates().collect{it.parent.name}));return null"))
    assert 'xtue-cloud' in result
    record('新项目历史加入磁盘压力清理候选')
    def visibility(value):
        script="""def c=io.xran.jenkins.BuildPlatformConfiguration.get();def f=c.form;f.revision=c.revision;f['projectOnly_xtue-cloud']=%s;c.configure((org.kohsuke.stapler.StaplerRequest2)null,f);return null"""%str(value).lower()
        admin.script(script)
    visibility(True)
    try:
        assert 'xtue-cloud' not in [x['name'] for x in builder.get('api/json?tree=jobs[name]')['jobs']]
        kube('-n',ns,'rollout','restart','statefulset/jenkins');kube('-n',ns,'rollout','status','statefulset/jenkins','--timeout=240s');wait_api()
        assert 'xtue-cloud' not in [x['name'] for x in builder.get('api/json?tree=jobs[name]')['jobs']]
        assert len(admin.get('job/xtue-cloud/api/json?tree=builds[number]')['builds'])>=8
        record('仅管理员可见，重启后 ACL 及构建历史保留')
    finally:visibility(False)
    assert 'xtue-cloud' in [x['name'] for x in builder.get('api/json?tree=jobs[name]')['jobs']]
    record('关闭开关恢复普通用户授权')
    report['status']='PASS';report['limitations']=['XTUE 私有源码不可见；此报告验证平台流程，不代表真实业务编译通过']
    (ROOT/'docs/verification/xtue-projects.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')


if __name__=='__main__':main()
