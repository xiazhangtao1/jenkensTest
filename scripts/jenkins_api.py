"""用于验收/运维的标准库客户端；代理绕过、Cookie/CSRF、身份独立。"""
import base64
import http.cookiejar
import json
from pathlib import Path
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request


class JenkinsAPI:
    def __init__(self, url, user, password):
        self.url = url.rstrip('/') + '/'
        self.authorization = 'Basic ' + base64.b64encode((user + ':' + password).encode()).decode()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.crumb = None

    def request(self, path, form=None):
        headers = {'Authorization': self.authorization}
        if form is not None:
            if self.crumb is None:
                self.crumb = self.get('crumbIssuer/api/json')
            headers[self.crumb['crumbRequestField']] = self.crumb['crumb']
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        return self.opener.open(urllib.request.Request(self.url + path.lstrip('/'), data=data,
            headers=headers), timeout=60)

    def get(self, path):
        with self.request(path) as response:
            return json.load(response)

    def post(self, path, form=None):
        with self.request(path, {} if form is None else form) as response:
            return response.headers.get('Location'), response.read().decode()

    def script(self, text):
        return self.post('scriptText', {'script': text})[1]

    def set_pipeline(self, text):
        encoded = base64.b64encode(text.encode()).decode()
        self.script('''import org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition
import org.jenkinsci.plugins.scriptsecurity.scripts.ScriptApproval
import org.jenkinsci.plugins.scriptsecurity.scripts.languages.GroovyLanguage
def job=jenkins.model.Jenkins.get().getItem('x-ran-cloud')
def text=new String(''' + "'" + encoded + "'" + '''.decodeBase64(),'UTF-8')
ScriptApproval.get().preapprove(text,GroovyLanguage.get())
job.definition=new CpsFlowDefinition(text,false)
job.save()
return null
''')

    def trigger(self, parameters):
        location, _ = self.post('job/x-ran-cloud/buildWithParameters', parameters)
        if not location:
            raise RuntimeError('构建请求未返回队列地址')
        return location.removeprefix(self.url)

    def wait_number(self, queue, timeout=600):
        deadline = time.time() + timeout
        while time.time() < deadline:
            item = self.get(queue.rstrip('/') + '/api/json')
            if item.get('cancelled'):
                raise RuntimeError('任务被取消')
            if item.get('executable'):
                return item['executable']['number']
            time.sleep(1)
        raise TimeoutError('等待构建编号超时')

    def wait(self, number, timeout=900):
        deadline = time.time() + timeout
        while time.time() < deadline:
            build = self.get(f'job/x-ran-cloud/{number}/api/json')
            if not build['building']:
                return build
            time.sleep(2)
        raise TimeoutError(f'构建 {number} 超时')

    def download(self, path, target):
        with self.request(path) as response, Path(target).open('wb') as output:
            shutil.copyfileobj(response, output, 1024 * 1024)

    def console(self, number):
        with self.request(f'job/x-ran-cloud/{number}/consoleText') as response:
            return response.read().decode()
