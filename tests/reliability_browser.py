#!/usr/bin/env python3
"""Real browser regression through the cached Playwright CLI, never the main site."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import yaml

from reliability import ROOT, require, validate_site, source_fingerprint
sys.path.insert(0,str(ROOT/'scripts'))
from jenkins_api import JenkinsAPI


def field(snapshot,label,role):
    lines=snapshot.splitlines()
    for index,line in enumerate(lines):
        if label in line:
            for following in lines[index:index+8]:
                match=re.search(r'\b'+role+r'.*?\[ref=((?:f\d+)?e\d+)\]',following)
                if match:return match[1]
    raise RuntimeError('浏览器未找到字段：'+label)


class Browser:
    def __init__(self,cfg,user,users,directory):
        self.cfg=cfg;self.user=user;self.directory=directory
        tools=ROOT/'.cache/verification-tools'
        self.command=[str(tools/'node'),str(tools/'node_modules/@playwright/cli/playwright-cli.js'),'-s=reliability-'+user]
        executable=tools/'chromium/chrome-headless-shell-linux64/chrome-headless-shell'
        require(Path(self.command[0]).is_file() and executable.is_file(),'缺少独立验收浏览器工具缓存，见 docs/reliability.md')
        self.config=directory/('browser-'+user+'.json')
        authorization=base64.b64encode((user+':'+users[user.upper()+'_PASSWORD']).encode()).decode()
        config={'browser':{'browserName':'chromium','launchOptions':{'headless':True,'executablePath':str(executable),
                'args':['--no-sandbox']},'contextOptions':{'extraHTTPHeaders':{'Authorization':'Basic '+authorization}}},
                'network':{'allowedOrigins':[cfg['jenkinsUrl'].rstrip('/')]},
                'outputDir':str(ROOT/'output/playwright/reliability')}
        self.config.write_text(json.dumps(config));self.config.chmod(0o600)
        self.cli('open',cfg['jenkinsUrl'],'--config',str(self.config))

    def cli(self,*args):
        result=subprocess.run([*self.command,*args],cwd=ROOT,env=dict(os.environ,CI='1',NO_UPDATE_NOTIFIER='1'),
                              capture_output=True,text=True,timeout=90)
        require(result.returncode==0 and '### Error' not in result.stdout,result.stdout[-2500:]+result.stderr[-1500:])
        return result.stdout

    def goto(self,path):
        self.cli('goto',self.cfg['jenkinsUrl']+path)
        return self.snapshot()

    def snapshot(self):
        result=self.cli('snapshot')
        match=re.search(r'\[Snapshot\]\(([^)]+\.yml)\)',result)
        if match:
            path=(ROOT/match[1]).resolve()
            require(path.is_relative_to(ROOT/'output/playwright/reliability'),'浏览器快照路径异常')
            return path.read_text()
        return result

    def close(self):
        try:self.cli('close')
        finally:self.config.unlink(missing_ok=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site',required=True)
    parser.add_argument('--xcn-number',type=int,required=True)
    args=parser.parse_args()
    cfg=yaml.safe_load(Path(args.site).read_text());namespace=validate_site(cfg)
    directory=ROOT/'.state'/namespace;users=json.loads((directory/'users.json').read_text())
    api=JenkinsAPI(cfg['jenkinsUrl'],'admin',users['ADMIN_PASSWORD'])
    def state():return json.loads(api.script("println(new File(jenkins.model.Jenkins.get().rootDir,'xran-platform/state.json').text);return null"))
    identity=json.loads(api.script("println(new File('/var/xran/site.json').text);return null"))
    require(identity.get('namespace')==namespace and identity.get('verificationInstance') is True,'浏览器站点实际身份不符')
    baseline=state()['settings'];browsers=[];checks=[]
    try:
        admin=Browser(cfg,'admin',users,directory);browsers.append(admin)
        snapshot=admin.goto('configure')
        require('构建平台设置' in snapshot,'管理员页面缺少构建平台设置')
        memory=field(snapshot,'服务包 Pod 内存总上限','spinbutton')
        admin.cli('fill',memory,'9')
        snapshot=admin.snapshot()
        save=re.search(r'button "Save" \[ref=((?:f\d+)?e\d+)\]',snapshot)
        require(save is not None,'未找到原生保存按钮')
        admin.cli('click',save[1])
        require(state()['settings']['serviceMemoryGiB']==9,'原生网页内存修改未保存')
        snapshot=admin.goto('configure')
        memory=field(snapshot,'服务包 Pod 内存总上限','spinbutton')
        require(re.search(r'\[ref='+memory+r'\].*"9"',snapshot) is not None,'网页刷新后内存值未保留')
        checks.append({'name':'管理员原生系统表单保存 9GiB 并刷新保留','status':'PASS'})
        scripts=admin.cli('--raw','eval',"() => Array.from(document.scripts).filter(s=>s.src.includes('usage.jenkins.io')).length")
        require(scripts.strip()=='0','系统页面仍加载外网使用统计脚本')
        checks.append({'name':'离线页面无外网使用统计脚本','status':'PASS'})
        builder=Browser(cfg,'builder1',users,directory);browsers.append(builder)
        home=builder.snapshot()
        require('storage-maintenance' not in home,'普通账号看到维护任务')
        snapshot=builder.goto('job/x-ran-cloud/build?delay=0sec')
        require('SERVICE_TARGET' in snapshot and 'IMAGE_TARGET' not in snapshot,'服务表单没有隐藏镜像字段')
        kind=field(snapshot,'BUILD_KIND','combobox')
        builder.cli('select',kind,'image');snapshot=builder.snapshot()
        require('SERVICE_TARGET' not in snapshot and 'IMAGE_TARGET' in snapshot,'镜像表单没有隐藏服务字段')
        tag=field(snapshot,'IMAGE_TAG','textbox');builder.cli('fill',tag,'browser-retained-tag')
        kind=field(builder.snapshot(),'BUILD_KIND','combobox');builder.cli('select',kind,'service')
        kind=field(builder.snapshot(),'BUILD_KIND','combobox');builder.cli('select',kind,'image')
        require('browser-retained-tag' in builder.snapshot(),'切换类型丢失输入')
        checks.append({'name':'普通用户 BUILD_KIND 双向切换及输入保留','status':'PASS'})
        snapshot=builder.goto('job/xcn/build?delay=0sec')
        require('CHART_VERSION' in snapshot and 'APP_VERSION' in snapshot and 'OFFLINE' not in snapshot,'xcn 构建参数不符')
        snapshot=builder.goto(f'job/xcn/{args.xcn_number}/parameters/')
        require('CHART_VERSION' in snapshot and 'APP_VERSION' in snapshot,'xcn 历史版本未显示')
        copy=re.search(r'button "复制参数值" \[ref=((?:f\d+)?e\d+)\]',snapshot)
        require(copy is not None,'HTTP 页面没有可用的参数复制按钮')
        # Copy feedback is transient; observe it in the same browser command as the real click.
        result=builder.cli('--raw','run-code',"async page => { await page.locator('aria-ref="+copy[1]+"').click(); "
                           "return await page.locator('[data-xran-http-copy]').evaluateAll(bs=>bs.some(b=>b.title==='已复制')); }")
        require(result.strip()=='true','HTTP 历史参数复制失败')
        checks.append({'name':'xcn 四类参数、历史 Chart/app 版本和 HTTP 复制','status':'PASS'})
        report={'status':'PASS','namespace':namespace,'sourceFingerprint':source_fingerprint(),
                'checks':checks,'testedAt':time.time(),'xcnNumber':args.xcn_number,
                'tools':{'cli':'0.1.23','node':'20.20.2','chromiumHeadlessRevision':'1248'}}
        (directory/'browser-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps(report,ensure_ascii=False),flush=True)
    finally:
        try:
            for browser in browsers:
                try:browser.close()
                except Exception as error:print('浏览器清理失败：'+str(error),file=sys.stderr)
        finally:
            current=state()
            api.script("io.xran.jenkins.BuildPlatformConfiguration.get().configure((org.kohsuke.stapler.StaplerRequest2)null,"
                       "net.sf.json.JSONObject.fromObject(new String('"+base64.b64encode(json.dumps(dict(baseline,revision=current['version'])).encode()).decode()+"'.decodeBase64(),'UTF-8')));return null")


if __name__=='__main__':main()
