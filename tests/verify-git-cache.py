#!/usr/bin/env python3
"""End-to-end verification in a dedicated namespace, never the main Jenkins."""
import argparse
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
from urllib.parse import urlsplit
import yaml

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import ci
from jenkins_api import JenkinsAPI
from reliability import source_fingerprint

NAMESPACE='jenkins-git-cache-verify'


def kube(*args, data=None):
    return subprocess.check_output(['kubectl',*map(str,args)],env=ci.kube_env(),input=data,text=True)


def fixture(site,image):
    ns=site['namespace']
    manifests=[{'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'git-cache-fixture','namespace':ns},
       'data':{'server.py':(ROOT/'tests/fixtures/git-cache-server.py').read_text(),
               'xcn-release.sh':(ROOT/'tests/fixtures/xcn-release.sh').read_text()}},
      {'apiVersion':'v1','kind':'Pod','metadata':{'name':'git-cache-fixture','namespace':ns,'labels':{'app':'git-cache-fixture'}},
       'spec':{'automountServiceAccountToken':False,'securityContext':{'runAsUser':1000,'runAsGroup':1000},
        'containers':[{'name':'git','image':image,'imagePullPolicy':'Never','command':['python3','/fixture/server.py'],
          'resources':{'requests':{'cpu':'100m','memory':'128Mi'},'limits':{'cpu':'1','memory':'1Gi'}},
          'readinessProbe':{'httpGet':{'path':'/pins.json','port':8000},'periodSeconds':2},
          'volumeMounts':[{'name':'fixture','mountPath':'/fixture','readOnly':True}]}],
        'volumes':[{'name':'fixture','configMap':{'name':'git-cache-fixture'}}]}},
      {'apiVersion':'v1','kind':'Service','metadata':{'name':'git-cache-fixture','namespace':ns},
       'spec':{'selector':{'app':'git-cache-fixture'},'ports':[{'port':8000,'targetPort':8000}]}}]
    kube('apply','-f','-',data=yaml.safe_dump_all(manifests))


def setup(args):
    if Path(args.site).exists():raise RuntimeError('拒绝覆盖已有验收站点；已部署时不再传 --setup')
    if kube('get','namespace',NAMESPACE,'--ignore-not-found','-o','name').strip():raise RuntimeError('验收 namespace 已存在')
    services=json.loads(kube('get','service','-A','-o','json'))['items']
    if any(p.get('nodePort')==args.node_port for s in services for p in s['spec'].get('ports',[])):
        raise RuntimeError('验收端口已占用')
    site=copy.deepcopy(yaml.safe_load(Path(args.base_site).read_text()))
    site.update(namespace=NAMESPACE,release='jenkins',storageClass='jenkins-cache-verify-local-path',
      nodePort=args.node_port,jenkinsUrl=f'http://{urlsplit(site["jenkinsUrl"]).hostname}:{args.node_port}/',
      controllerRegistry='localhost:5000',verificationInstance=True,offline=True,demo=True,pvcSize='30Gi',
      gitCacheEnabled=True,gitCacheMaxGiB=20,gitCachePvcSize='20Gi',demoBuildDelaySeconds=0,
      gitUrl='http://git-cache-fixture:8000/X-RAN-CLOUD.git',gitCredentialsId='',
      services=['serviceA','serviceB'],images=['imageA'],imageRepositories={'imageA':'xran-demo/imagea'},
      registry='xran-registry:5000',registryInsecure=True,registryCredentialsId='',proxy='')
    site.pop('caSecret',None)
    site['xcn'].update(gitUrl='http://git-cache-fixture:8000/xcn.git',gitCredentialsId='',offline=True,
      registry='xran-registry:5000',registryInsecure=True,registryCredentialsId='')
    site['projects']={'xtue-cloud':{'gitUrl':'http://git-cache-fixture:8000/XTUE-CLOUD.git','gitCredentialsId':''}}
    path=Path(args.site);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(yaml.safe_dump(site,sort_keys=False))
    ci.create_namespace(site)
    fixture(site,args.fixture_image)
    # Pull through the already configured node Registry. Never mount a host runtime socket.
    image='localhost:5000/'+ci.versions()['CONTROLLER_IMAGE']
    pull={'apiVersion':'v1','kind':'Pod','metadata':{'name':'controller-prepull','namespace':NAMESPACE},'spec':{
      'restartPolicy':'Never','automountServiceAccountToken':False,'securityContext':{'runAsUser':1000,'runAsGroup':1000},
      'containers':[{'name':'pull','image':image,'imagePullPolicy':'Always','command':['sh','-c','true'],
        'resources':{'requests':{'cpu':'10m','memory':'16Mi'},'limits':{'cpu':'100m','memory':'64Mi'}}}]}}
    kube('apply','-f','-',data=yaml.safe_dump(pull))
    deadline=time.time()+180
    while time.time()<deadline:
        pod=json.loads(kube('-n',NAMESPACE,'get','pod','controller-prepull','-o','json'))
        if pod['status'].get('phase')=='Succeeded':break
        if pod['status'].get('phase')=='Failed':raise RuntimeError('Controller 预拉取失败')
        time.sleep(2)
    else:raise TimeoutError('Controller 预拉取超时')
    kube('-n',NAMESPACE,'delete','pod','controller-prepull','--wait=true')
    name,tag=args.compiler_image.rsplit(':',1)
    cfg=ci.render(argparse.Namespace(site=args.site,namespace=None,offline=True,image_name=name,image_tag=tag))
    values_path=ci.state_dir(cfg)/'values.yaml'
    values=yaml.safe_load(values_path.read_text())
    values['controller']['resources']['limits']['cpu']=args.controller_cpu_limit
    values_path.write_text(yaml.safe_dump(values,sort_keys=False))
    ci.apply_deployment(cfg)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--site',required=True);p.add_argument('--setup',action='store_true')
    p.add_argument('--runtime-only',action='store_true',help='针对最终检出脚本补测，追加到已有完整验收报告')
    p.add_argument('--base-site',default=str(ROOT/'config/site.local.yaml'));p.add_argument('--node-port',type=int,default=32190)
    p.add_argument('--compiler-image',required=True);p.add_argument('--fixture-image',default='xran-ci/demo:4')
    p.add_argument('--controller-cpu-limit',default='1500m',help='仅隔离 Controller 使用；兼容测试节点的整数 CPU 池限制')
    args=p.parse_args();os.environ['PATH']=str(ROOT/'.cache/tools')+os.pathsep+os.environ['PATH']
    if args.setup:setup(args)
    site=yaml.safe_load(Path(args.site).read_text());ns=site['namespace']
    if not site.get('verificationInstance') or ns!=NAMESPACE:raise RuntimeError('仅允许专用 Git 缓存隔离验收 namespace')
    service=json.loads(kube('-n',ns,'get','service','jenkins','-o','json'))
    ports={port.get('nodePort') for port in service['spec']['ports']}
    if urlsplit(site['jenkinsUrl']).port not in ports:raise RuntimeError('验收 URL 与专用 Service 的 NodePort 不一致')
    configuration=json.loads(kube('-n',ns,'get','configmap','xran-config','-o','json'))
    driver=(ROOT/'runtime/git_checkout.py').read_text()
    if configuration['data']['git_checkout.py']!=driver:raise RuntimeError('验收 ConfigMap 中的检出脚本与当前源码不同')
    kube('-n',ns,'wait','--for=condition=Ready','pod/git-cache-fixture','--timeout=180s')
    pins=json.loads(kube('-n',ns,'exec','git-cache-fixture','--','cat','/tmp/git-cache-fixtures/pins.json'))
    users=json.loads((ROOT/'.state'/ns/'users.json').read_text())
    admin=JenkinsAPI(site['jenkinsUrl'],'admin',users['ADMIN_PASSWORD'])
    clients={name:JenkinsAPI(site['jenkinsUrl'],'builder1',users['BUILDER1_PASSWORD'],name)
             for name in ('x-ran-cloud','xcn','xtue-cloud')}
    def primary_snapshot():
        pod=json.loads(kube('-n','jenkins','get','pod','jenkins-0','-o','json'))
        return {'uid':pod['metadata']['uid'],'containers':[{k:c.get(k) for k in ('name','imageID','restartCount')}
                 for c in pod['status']['containerStatuses']]}
    baseline=primary_snapshot()
    report={'namespace':ns,'url':site['jenkinsUrl'],'fixtureOnly':True,'checks':[],'builds':[],
            'sourceFingerprint':source_fingerprint(),'primaryBefore':baseline,
            'checkoutDriverSha256':hashlib.sha256(driver.encode()).hexdigest()}
    def record(name,**detail):
        report['checks'].append(dict(name=name,status='PASS',**detail));print('PASS',name,flush=True)
    def ready():
        admin.crumb=None
        for c in clients.values():c.crumb=None
        deadline=time.time()+300
        while time.time()<deadline:
            try:
                if set(clients)<=set(x['name'] for x in admin.get('api/json?tree=jobs[name]')['jobs']):return
            except (urllib.error.URLError,TimeoutError):pass
            time.sleep(2)
        raise TimeoutError('Jenkins 未就绪')
    ready()
    remote=json.loads(admin.script("def c=new groovy.json.JsonSlurperClassic().parseText(new File('/var/xran/site.json').text);"+
        "println(groovy.json.JsonOutput.toJson([namespace:c.namespace,verificationInstance:c.verificationInstance]));return null"))
    if remote!={'namespace':NAMESPACE,'verificationInstance':True}:raise RuntimeError('远端不是专用验收 Jenkins，拒绝操作')
    actual_plugin=kube('-n',ns,'exec','jenkins-0','-c','jenkins','--','sha256sum',
                       '/var/jenkins_home/plugins/xran-build-platform.jpi').split()[0]
    assert actual_plugin==json.loads((ROOT/'config/platform-plugin.lock.json').read_text())['sha256']
    report['pluginSha256']=actual_plugin
    controller=json.loads(kube('-n',ns,'get','pod','jenkins-0','-o','json'))
    report['controllerCpuLimit']=controller['spec']['containers'][0]['resources']['limits']['cpu']
    descriptor='descriptorByName/io.xran.jenkins.BuildPlatformConfiguration/'
    def settings(**values):
        encoded=base64.b64encode(json.dumps(values).encode()).decode()
        admin.script("def c=io.xran.jenkins.BuildPlatformConfiguration.get();def f=c.form;f.revision=c.revision;f.putAll(new groovy.json.JsonSlurperClassic().parseText(new String('"+encoded+"'.decodeBase64(),'UTF-8')));c.configure((org.kohsuke.stapler.StaplerRequest2)null,f);return null")
    def get_settings():return json.loads(admin.script('println(io.xran.jenkins.BuildPlatformConfiguration.get().form.toString());return null'))
    def traffic():
        return json.loads(kube('-n',ns,'exec','git-cache-fixture','--','python3','-c',
            "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/metrics.json').read().decode())"))
    def submit(project,**changes):
        parameters={'COMPILER_IMAGE':args.compiler_image,'COMPILER_IMAGE_PULL_POLICY':'Always','REF_TYPE':'branch','GIT_REF':'main'}
        parameters.update({'BUILD_KIND':'service','SERVICE_TARGET':'services_all'} if project=='x-ran-cloud' else
                          {'CHART_VERSION':'26.11.17','APP_VERSION':'26.11.17-cache'} if project=='xcn' else {'SERVICE_TARGET':'nrUE'})
        parameters.update(changes);api=clients[project]
        return api.wait_number(api.trigger(parameters))
    def finish(project,number,hit=None,sha=None):
        api=clients[project];result=api.wait(number,timeout=600)
        if result['result']!='SUCCESS':raise AssertionError(api.console(number))
        with api.request(api.job_path+str(number)+'/artifact/publish/metadata.json') as response:meta=json.load(response)
        if hit is not None:assert meta['gitCache']['hit']==hit,meta['gitCache']
        if sha:assert meta['commit']==sha,(meta['commit'],sha)
        for item in meta['files']:
            digest=hashlib.sha256()
            with api.request(api.job_path+str(number)+'/artifact/publish/'+item['name']) as response:
                while block:=response.read(1024*1024):digest.update(block)
            assert digest.hexdigest()==item['sha256']
        report['builds'].append({'project':project,'number':number,'commit':meta['commit'],
                                'cache':meta['gitCache'],'artifacts':meta['files']})
        return meta
    settings(gitCacheEnabled=True,gitCacheMaxGiB=20,compilerImage=args.compiler_image)
    pvc=json.loads(kube('-n',ns,'get','pvc','jenkins','xran-git-cache','-o','json'))
    uids={x['metadata']['name']:x['metadata']['uid'] for x in pvc['items']}
    if args.runtime_only:
        target=ROOT/'docs/verification/git-cache.json'
        previous=json.loads(target.read_text())
        if previous.get('status')!='PASS' or previous.get('namespace')!=ns:raise RuntimeError('需要已有完整 PASS 报告')
        jobs=admin.get('api/json?tree=jobs[name,builds[building]]')['jobs']
        if any(build['building'] for job in jobs for build in job['builds']):raise RuntimeError('请先等待隔离实例构建结束')
        _,body=admin.post(descriptor+'clearGitCache',{'gitCacheProject':'x-ran-cloud'});assert '已清空' in body
        image=finish('x-ran-cloud',submit('x-ran-cloud',BUILD_KIND='image',IMAGE_TARGET='imageA',IMAGE_TAG='cache-final'),False,pins['X-RAN-CLOUD']['head'])
        finish('x-ran-cloud',submit('x-ran-cloud',SERVICE_TARGET='serviceA'),True,pins['X-RAN-CLOUD']['head'])
        record('最终打包限制及真实 root → UID1000 缓存复用')
        blob=kube('-n',ns,'exec','git-cache-fixture','--','git','-C','/tmp/git-cache-fixtures/X-RAN-CLOUD.git',
                  'rev-parse','main:payload').strip()
        def corrupt_blob(key):
            path='/var/xran-git-cache/x-ran-cloud/'+key+'.git/objects/'+blob[:2]+'/'+blob[2:]
            # The same kernel lock as the driver; unlinking keeps immutable Git
            # object modes intact and needs only the shared parent permissions.
            kube('-n',ns,'exec','jenkins-0','-c','jenkins','--','/usr/bin/flock','-E','75','-n',
                 '/var/xran-git-cache/x-ran-cloud/.lock','sh','-ec',
                 'rm -- "$1"; printf "%s" "corrupt cached blob" > "$1"','cache-verify',path)
        corrupt_blob(image['gitCache']['key'])
        recovered=finish('x-ran-cloud',submit('x-ran-cloud',SERVICE_TARGET='serviceB'),False,pins['X-RAN-CLOUD']['head'])
        record('跨 UID 本地传输遇到损坏 blob，受锁保护重建并成功构建')
        corrupt_blob(recovered['gitCache']['key'])
        recovered=finish('x-ran-cloud',submit('x-ran-cloud',SERVICE_TARGET='serviceA'),False,pins['X-RAN-CLOUD']['head'])
        assert recovered['gitCache']['checkoutRetried'] is True
        record('独立工作树展开时发现损坏 blob，复查后重建且仅重试一次')
        report['primaryAfter']=primary_snapshot();assert report['primaryAfter']==baseline
        assert report['sourceFingerprint']==source_fingerprint(),'补测期间发布源码变化'
        report['status']='PASS';previous['runtimeFollowUp']=report
        target.write_text(json.dumps(previous,ensure_ascii=False,indent=2)+'\n')
        print('Runtime report:',target,flush=True)
        return
    for project,repo in [('x-ran-cloud','X-RAN-CLOUD'),('xcn','xcn'),('xtue-cloud','XTUE-CLOUD')]:
        _,body=admin.post(descriptor+'clearGitCache',{'gitCacheProject':project});assert '已清空' in body
        before=traffic()[repo]
        finish(project,submit(project),hit=False,sha=pins[repo]['head'])
        cold=traffic()[repo]-before;before=traffic()[repo]
        finish(project,submit(project),hit=True,sha=pins[repo]['head'])
        warm=traffic()[repo]-before;assert cold>256*1024 and warm<cold//10
        record('冷/热缓存构建及产物 SHA256',project=project,coldTransferBytes=cold,warmTransferBytes=warm)
    finish('xtue-cloud',submit('xtue-cloud',REF_TYPE='tag',GIT_REF='cache-v1',SERVICE_TARGET='tuePhy'),True,pins['XTUE-CLOUD']['old'])
    finish('x-ran-cloud',submit('x-ran-cloud',REF_TYPE='commit',GIT_REF=pins['X-RAN-CLOUD']['old'][:9]),True,pins['X-RAN-CLOUD']['old'])
    _,body=admin.post(descriptor+'clearGitCache',{'gitCacheProject':'x-ran-cloud'});assert '已清空' in body
    finish('x-ran-cloud',submit('x-ran-cloud',BUILD_KIND='image',IMAGE_TARGET='imageA',IMAGE_TAG='cache-mixed-uid'),False,pins['X-RAN-CLOUD']['head'])
    finish('x-ran-cloud',submit('x-ran-cloud',SERVICE_TARGET='serviceA'),True,pins['X-RAN-CLOUD']['head'])
    record('tag/短 SHA，镜像 root 首次建立缓存后 UID1000 服务复用')
    # Only the isolated fixture delay changes; all resource/admission/cache logic
    # remains the shipped pipeline. Restore the definitions even after failure.
    originals={}
    for project in clients:
        encoded=base64.b64encode(project.encode()).decode()
        saved=admin.script("println(jenkins.model.Jenkins.get().getItem(new String('"+encoded+
            "'.decodeBase64(),'UTF-8')).definition.script.getBytes('UTF-8').encodeBase64().toString());return null")
        originals[project]=base64.b64decode(saved.strip()).decode()
    concurrent=[]
    try:
        for project,original in originals.items():
            token='XRAN_BUILD_DELAY_SECONDS=${cfg.demoBuildDelaySeconds ?: 0}'
            assert token in original
            JenkinsAPI(site['jenkinsUrl'],'admin',users['ADMIN_PASSWORD'],project).set_pipeline(
                original.replace(token,'XRAN_BUILD_DELAY_SECONDS=60'))
        cases=[('x-ran-cloud',{'SERVICE_TARGET':'serviceA'}),('xcn',{'APP_VERSION':'cache-concurrent-1'}),
               ('xtue-cloud',{'SERVICE_TARGET':'nrUE'}),('x-ran-cloud',{'SERVICE_TARGET':'serviceB'}),
               ('xcn',{'APP_VERSION':'cache-concurrent-2'}),('xtue-cloud',{'SERVICE_TARGET':'tuePhy'})]
        concurrent=[(project,submit(project,**parameters)) for project,parameters in cases]
        deadline=time.time()+180;peak=0
        while time.time()<deadline:
            pods=json.loads(kube('-n',ns,'get','pods','-l','xran-ci=build','-o','json'))['items']
            running=[pod for pod in pods if not pod['metadata'].get('deletionTimestamp') and
                     pod['status'].get('phase')=='Running']
            peak=max(peak,len(running))
            ready_builds=all(('FIXTURE_BUILD_READY' if project!='xcn' else 'Loaded image:') in
                            clients[project].console(number) for project,number in concurrent)
            if len(running)==6 and ready_builds:
                for pod in running:
                    builder=next(c for c in pod['spec']['containers'] if c['name']=='builder')
                    mount=next(m for m in builder['volumeMounts'] if m['name']=='git-cache')
                    assert mount['subPath']==pod['metadata']['labels']['xran-job']
                break
            time.sleep(1)
        else:raise TimeoutError('六个实际 Pod 未同时进入构建阶段，peak='+str(peak))
        _,body=admin.post(descriptor+'clearGitCache',{'gitCacheProject':'x-ran-cloud'})
        assert '已清空' in body
        for project,number in concurrent:finish(project,number,hit=True)
        record('六个实际 Pod 并发，项目 subPath 隔离，编译期间清空已复制缓存不影响产物',peak=peak)
    finally:
        for project,original in originals.items():
            JenkinsAPI(site['jenkinsUrl'],'admin',users['ADMIN_PASSWORD'],project).set_pipeline(original)
    with admin.request('configure') as response:
        page=response.read().decode();assert '所有项目 Git 缓存合计预算' in page and '清空指定项目 Git 缓存' in page
    try:clients['xcn'].post(descriptor+'clearGitCache',{'gitCacheProject':'xcn'})
    except urllib.error.HTTPError as denied:assert denied.code==403
    else:raise AssertionError('普通用户可以清空缓存')
    record('管理员网页显示状态，普通用户清空接口拒绝')
    settings(gitCacheEnabled=False,gitCacheMaxGiB=7)
    kube('-n',ns,'rollout','restart','statefulset/jenkins')
    kube('-n',ns,'rollout','status','statefulset/jenkins','--timeout=300s');ready()
    saved=get_settings();assert saved['gitCacheEnabled'] is False and saved['gitCacheMaxGiB']==7
    direct=finish('xtue-cloud',submit('xtue-cloud'),False,pins['XTUE-CLOUD']['head']);assert direct['gitCache']['enabled'] is False
    settings(gitCacheEnabled=True,gitCacheMaxGiB=20)
    finish('xtue-cloud',submit('xtue-cloud'),True,pins['XTUE-CLOUD']['head'])
    restored=json.loads(kube('-n',ns,'get','pvc','jenkins','xran-git-cache','-o','json'))
    assert uids=={x['metadata']['name']:x['metadata']['uid'] for x in restored['items']}
    record('网页设置、两块 PVC 及 Git 缓存跨 Controller 重启保留，关闭后直接检出')
    # Hold a Python flock on the real PVC; the Java administrator action must skip it.
    holder={'apiVersion':'v1','kind':'Pod','metadata':{'name':'git-cache-lock-test','namespace':ns},'spec':{
      'automountServiceAccountToken':False,'securityContext':{'runAsUser':1000,'runAsGroup':1000},
      'containers':[{'name':'holder','image':args.compiler_image,'imagePullPolicy':'Never',
       'command':['python3','-u','-c',"import fcntl,time;from pathlib import Path;f=open('/cache/xcn/.lock','r+');fcntl.flock(f,fcntl.LOCK_EX);Path('/tmp/ready').touch();time.sleep(120)"],
       'readinessProbe':{'exec':{'command':['test','-f','/tmp/ready']},'periodSeconds':1},
       'resources':{'requests':{'cpu':'10m','memory':'32Mi'},'limits':{'cpu':'100m','memory':'128Mi'}},
       'volumeMounts':[{'name':'cache','mountPath':'/cache'}]}],
      'volumes':[{'name':'cache','persistentVolumeClaim':{'claimName':'xran-git-cache'}}]}}
    kube('apply','-f','-',data=yaml.safe_dump(holder))
    try:
        kube('-n',ns,'wait','--for=condition=Ready','pod/git-cache-lock-test','--timeout=120s')
        _,body=admin.post(descriptor+'clearGitCache',{'gitCacheProject':'xcn'});assert '正在更新或复制' in body
    finally:kube('-n',ns,'delete','pod','git-cache-lock-test','--wait=true')
    _,body=admin.post(descriptor+'clearGitCache',{'gitCacheProject':'xcn'});assert '已清空' in body
    finish('xcn',submit('xcn'),False,pins['xcn']['head'])
    record('Python/Java 跨进程锁互通，忙碌时拒绝清空，释放后可清空并重建')
    # Storage policy: reclaim a known oldest artifact, keeping another build protected.
    pressure=admin.script("""def j=jenkins.model.Jenkins.get()
def t=new GroovyShell(j.pluginManager.uberClassLoader).evaluate(new File('/opt/xran/storage.groovy').text+'\\nreturn XranStorage')
def candidates=t.candidates().findAll{!it.artifacts.empty};assert candidates.size()>=2
def oldest=candidates[0];def kept=candidates[1];kept.keepLog(true)
def sentinel=new File(oldest.artifactsDir,'pressure-fixture.bin');sentinel.withOutputStream{out->def b=new byte[1048576];20.times{out.write(b)}}
def cfg=new groovy.json.JsonSlurperClassic().parseText(new File('/var/xran/site.json').text);cfg.minimumFreeBytes=j.rootDir.usableSpace+4194304L
def result=t.enforce(cfg,[:],null);assert kept.isKeepLog() && kept.artifactsDir.exists()
result.expected=oldest.externalizableId;result.protected=kept.externalizableId
println(groovy.json.JsonOutput.toJson(result));return null""")
    pressure=json.loads(pressure);assert pressure['deleted'] and not pressure['gitCache']['deleted']
    assert pressure['deleted'][0]['build']==pressure['expected']
    record('磁盘压力先清理历史产物，缓存保留且长期保留构建受保护')
    report['primaryAfter']=primary_snapshot();assert report['primaryAfter']==baseline
    record('主 Jenkins Pod、镜像与重启次数未改变')
    assert report['sourceFingerprint']==source_fingerprint(),'验收期间发布源码变化'
    report['status']='PASS';report['limitations']=['仅验证模拟业务与平台流程，内网大型仓库提速需按日志实测','子模块/LFS 未新增专用缓存']
    target=ROOT/'docs/verification/git-cache.json';target.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print('Report:',target,flush=True)


if __name__=='__main__':main()
