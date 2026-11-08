#!/usr/bin/env python3
"""独立 Jenkins：真实 DinD 缺包导出、项目可见性和原 ACL 恢复。"""
import argparse
import base64
import hashlib
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
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--site', required=True)
    cli.add_argument('--compat-image', required=True)
    cli.add_argument('--sdk-image', required=True)
    cli.add_argument('--old-image', required=True)
    cli.add_argument('--source', default=str(ROOT / '.cache/demo.git'))
    cli.add_argument('--resume', action='store_true')
    args = cli.parse_args()
    cfg = yaml.safe_load(Path(args.site).read_text())
    assert cfg.get('verificationInstance') is True and cfg['namespace'] != 'jenkins'
    report = ROOT / '.state' / cfg['namespace'] / 'artifact-visibility.json'
    v = Verification(SimpleNamespace(site=args.site, source=args.source, report=str(report),
        compiler_image=args.compat_image, compiler_image_pull_policy='Always', services_only=False))
    if args.resume:
        old = json.loads(report.read_text())
        assert old['namespace'] == cfg['namespace'] and old['builds'][0]['parameters']['COMPILER_IMAGE'] == args.compat_image
        v.report = old
        for key in ('status', 'error', 'finishedAt'): v.report.pop(key, None)

    def state():
        return json.loads(v.admin.script("println(new File(jenkins.model.Jenkins.get().rootDir,'xran-platform/state.json').text);return null"))

    def save(**changes):
        previous = state()
        candidate = dict(previous['settings'], revision=previous['version'], **changes)
        data = base64.b64encode(json.dumps(candidate).encode()).decode()
        v.admin.script("io.xran.jenkins.BuildPlatformConfiguration.get().configure((org.kohsuke.stapler.StaplerRequest2)null,"
            "net.sf.json.JSONObject.fromObject(new String('" + data + "'.decodeBase64(),'UTF-8')));return null")
        saved = state()
        assert saved['version'] == saved['appliedVersion'] and not saved.get('applyError'), saved.get('applyError')
        return saved

    def done(name):
        return any(c['name'] == name and c['status'] == 'PASS' for c in v.report['checks'])

    def acl():
        return json.loads(v.admin.script("def p=jenkins.model.Jenkins.get().getItem('xcn').getProperty(hudson.security.AuthorizationMatrixProperty);"
            "println(groovy.json.JsonOutput.toJson(p==null?null:jenkins.model.Jenkins.XSTREAM2.toXML(p)));return null"))

    def restore_acl(xml):
        data = base64.b64encode(json.dumps({'xml': xml}).encode()).decode()
        v.admin.script("def j=jenkins.model.Jenkins.get().getItem('xcn');"
            "def x=groovy.json.JsonSlurperClassic.newInstance().parseText(new String('" + data + "'.decodeBase64(),'UTF-8')).xml;"
            "j.removeProperty(hudson.security.AuthorizationMatrixProperty);"
            "if(x!=null)j.addProperty(jenkins.model.Jenkins.XSTREAM2.fromXML(x));j.save();return null")
        assert acl() == xml, '测试 ACL 未恢复'

    original = json.loads(v.admin.script("println(groovy.json.JsonOutput.toJson(jenkins.model.Jenkins.get().getItem('x-ran-cloud').definition.script));return null"))
    settings = state()['settings']
    original_acl = acl()
    saveblock = 'docker save -o "$dir/$image.tar.tmp" "$reference"\nmv "$dir/$image.tar.tmp" "$dir/$image.tar"'

    def fixture(mode, registry=None):
        # Modify only this disposable checkout. The real business repository is untouched.
        substitutions = {'legacy':'', 'corrupt':'printf broken > "$dir/$image.tar"',
            'missing':'docker tag "$reference" "$reference-other"\ndocker image rm "$reference"',
            'unwritable':''}
        program = "from pathlib import Path\np=Path('source/platform/k8s/images-build/images-build.sh')\nt=p.read_text()\n"
        program += 'old='+repr(saveblock)+'\nassert old in t\nt=t.replace(old,'+repr(substitutions[mode])+')\n'
        if mode == 'missing': program += "t=t.replace('docker push \"$reference\"','true')\n"
        if mode == 'unwritable': program += "t+='\\nchmod a-w \"$dir\"\\n'\n"
        program += 'p.write_text(t)\n'
        encoded = base64.b64encode(program.encode()).decode()
        script = original.replace("stage('编译与打包') {", "stage('编译与打包') {\n"
            + "sh '''python3 -c \"import base64; exec(base64.b64decode('"+encoded+"'))\"'''\n", 1)
        if registry:
            script = script.replace('def sourceType =', "cfg.registry = '"+registry+"'\ndef sourceType =", 1)
        v.set_script(script)

    def visibility(xran, xcn, xran_number):
        save(xranAdminOnly=xran, xcnAdminOnly=xcn)
        for user, client in v.clients.items():
            visible = {item['name'] for item in client.get('api/json?tree=jobs[name]')['jobs']}
            assert ('x-ran-cloud' in visible) is (not xran)
            assert ('xcn' in visible) is (not xcn)
            assert 'storage-maintenance' not in visible
            for name, hidden, number in [('x-ran-cloud',xran,xran_number),('xcn',xcn,1)]:
                prefix = 'job/'+name+'/'
                if not hidden:
                    client.get(prefix+'api/json')
                    with client.request(prefix+str(number)+'/artifact/publish/metadata.json') as response: assert response.status == 200
                else:
                    for path, form in [('',None),('api/json',None),('config.xml',None),
                        (str(number)+'/consoleText',None),(str(number)+'/artifact/publish/metadata.json',None),
                        ('buildWithParameters',{'GIT_REF':'main'})]:
                        try:
                            with client.request(prefix+path,form): raise AssertionError(user+' can access '+prefix+path)
                        except urllib.error.HTTPError as error: assert error.code in (403,404),error.code
        assert {j['name'] for j in v.admin.get('api/json?tree=jobs[name]')['jobs']} >= {'x-ran-cloud','xcn','storage-maintenance'}

    try:
        save(xranAdminOnly=False, xcnAdminOnly=False)
        v.ready(); v.maintenance_access()
        with v.admin.request('configure') as response:
            page = response.read().decode()
            assert 'X-RAN-CLOUD 仅管理员可见' in page and 'xcn 仅管理员可见' in page
        if not done('push 失败仍能下载并运行有效镜像 tar'): v.functional()
        for image, label in [(args.compat_image,'compat'),(args.sdk_image,'sdk')]:
            name = label+' 旧版仅生成镜像时自动导出并下载运行'
            if done(name): continue
            fixture('legacy')
            number = v.submit(BUILD_KIND='image',COMPILER_IMAGE=image,IMAGE_TAG='legacy-'+label+'-'+str(time.time_ns()))
            v.completed(number)
            meta = v.artifacts(number,v.head,'builder1',compiler_image=image)
            assert meta['imageArchiveSource']=='docker-save'
            v.record(name,{'build':number,'imageId':meta['imageId']})
        for mode, message in [('corrupt','镜像产物'),('missing','找不到镜像'),('unwritable','Permission denied')]:
            name = mode+' 不发布错误镜像包'
            if done(name): continue
            fixture(mode)
            number=v.submit(BUILD_KIND='image',IMAGE_TAG=mode+'-'+str(time.time_ns()))
            v.completed(number,'FAILURE')
            meta=v.artifacts(number,v.head,'builder1',expected_result='FAILURE')
            assert not meta['files'] and message in '\n'.join(meta['collectionErrors']),meta
            v.record(name,{'build':number,'errors':meta['collectionErrors']})
        name='旧版 push 失败仍自动导出有效镜像并保留失败结果'
        if not done(name):
            fixture('legacy','127.0.0.1:1')
            number=v.submit(BUILD_KIND='image',IMAGE_TAG='legacy-push-'+str(time.time_ns()))
            v.completed(number,'FAILURE')
            meta=v.artifacts(number,v.head,'builder1',expected_result='FAILURE',expected_push='failed-or-not-reached')
            assert meta['imageArchiveSource']=='docker-save'
            v.record(name,{'build':number})
        v.set_script(original)
        name='旧编译镜像明确提示缺少自动导出能力'
        if not done(name):
            number=v.submit(BUILD_KIND='image',COMPILER_IMAGE=args.old_image)
            v.completed(number,'FAILURE')
            assert '缺少镜像包自动导出能力' in v.admin.console(number)
            v.record(name,{'build':number})
        number=v.submit(); v.completed(number)
        v.artifacts(number,v.head,'builder1')
        for a,b in [(False,False),(True,False),(False,True),(True,True)]:
            visibility(a,b,number)
            v.record('可见性组合 '+str((a,b)),{'xran':a,'xcn':b})
        save(xranAdminOnly=False,xcnAdminOnly=False)
        save(concurrency=1)
        v.set_script(original.replace('def sourceType =','cfg.demoBuildDelaySeconds = 12\ndef sourceType =',1))
        first=v.submit(); second=v.submit('builder2')
        deadline=time.time()+120
        while not state()['leases']:
            assert time.time()<deadline
            time.sleep(1)
        save(xranAdminOnly=True)
        v.completed(first); v.completed(second)
        save(xranAdminOnly=False,concurrency=settings['concurrency'])
        v.set_script(original)
        v.artifacts(first,v.head,'builder1'); v.artifacts(second,v.head,'builder2')
        v.record('限制可见性不取消已运行和等待槽位的构建',{'builds':[first,second]})

        v.admin.script("def j=jenkins.model.Jenkins.get().getItem('xcn');"
            "j.addProperty(new hudson.security.AuthorizationMatrixProperty([(hudson.model.Item.READ):"
            "[org.jenkinsci.plugins.matrixauth.PermissionEntry.user('builder1'),org.jenkinsci.plugins.matrixauth.PermissionEntry.group('developers')] as Set],"
            "new org.jenkinsci.plugins.matrixauth.inheritance.NonInheritingStrategy()));j.save();return null")
        custom=acl()
        before=save(xranAdminOnly=True,xcnAdminOnly=True)
        subprocess.run(['kubectl','-n',cfg['namespace'],'rollout','restart','statefulset/jenkins'],env=kube_env(),check=True)
        subprocess.run(['kubectl','-n',cfg['namespace'],'rollout','status','statefulset/jenkins','--timeout=300s'],env=kube_env(),check=True)
        for client in [v.admin,*v.clients.values()]: client.crumb=None
        deadline=time.time()+240
        while True:
            try: after=state(); break
            except (urllib.error.URLError,TimeoutError):
                assert time.time()<deadline
                v.admin.crumb=None; time.sleep(2)
        assert after['settings']['xranAdminOnly'] is after['settings']['xcnAdminOnly'] is True
        assert after['version']==before['version'] and after['authorizationBackups']==before['authorizationBackups']
        save(xranAdminOnly=False,xcnAdminOnly=False)
        assert acl()==custom
        v.clients['builder1'].get('job/xcn/api/json')
        try:
            v.clients['builder2'].get('job/xcn/api/json'); raise AssertionError('custom ACL lost')
        except urllib.error.HTTPError as error: assert error.code in (403,404)
        v.record('Controller 重启保留限制状态；关闭后精确恢复原用户、组和继承方式',
            {'version':after['version'],'aclSha256':hashlib.sha256(custom.encode()).hexdigest()})
        restore_acl(original_acl)
        visibility(False,False,number)
        v.report['status']='PASS'
    except Exception as error:
        v.report.update(status='FAIL',error=str(error)); raise
    finally:
        v.set_script(original)
        save(xranAdminOnly=settings['xranAdminOnly'],xcnAdminOnly=settings['xcnAdminOnly'],concurrency=settings['concurrency'])
        restore_acl(original_acl)
        v.report['finishedAt']=time.strftime('%FT%T%z'); v.save()


if __name__=='__main__': main()
