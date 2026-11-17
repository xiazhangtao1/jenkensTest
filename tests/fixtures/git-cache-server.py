#!/usr/bin/env python3
"""Disposable smart-HTTP Git service with three buildable fixture projects."""
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import threading
from urllib.parse import urlsplit

ROOT = Path('/tmp/git-cache-fixtures')
ROOT.mkdir(exist_ok=True)


def git(directory, *args):
    return subprocess.check_output(['git', '-C', str(directory), *args], text=True).strip()


SERVICE = '''#!/usr/bin/env python3
import datetime,io,json,os,subprocess,sys,tarfile,time
assert sys.argv[1:3]==['-c','-m']
target=sys.argv[3]
sha=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
print('FIXTURE_BUILD_READY',flush=True)
time.sleep(float(os.environ.get('XRAN_BUILD_DELAY_SECONDS','0')))
subprocess.run(['gcc','fixture.c','-o','fixture'],check=True)
from pathlib import Path
output=Path('../JDBG/Release/services');output.mkdir(parents=True,exist_ok=True)
name=('L2L3_'+datetime.datetime.now().strftime('%m%d%H%M%S')+'_'+sha[:9] if target=='services_all' else target)+'.tar.gz'
with tarfile.open(output/name,'w:gz') as archive:
 archive.add('fixture',arcname='fixture')
 data=json.dumps({'commit':sha,'target':target}).encode();m=tarfile.TarInfo('fixture.json');m.size=len(data);archive.addfile(m,io.BytesIO(data))
'''

IMAGE = '''#!/bin/sh
set -eu
context=$(mktemp -d)
trap 'rm -rf "$context"' EXIT
printf 'FROM ubuntu:22.04\\nCOPY fixture.txt /fixture.txt\\n' > "$context/Dockerfile"
git rev-parse HEAD > "$context/fixture.txt"
docker build --pull=false --network none -t "$XRAN_IMAGE_REFERENCE" "$context"
docker save -o "$1.tar" "$XRAN_IMAGE_REFERENCE"
docker push "$XRAN_IMAGE_REFERENCE"
'''

XTUE = '''#!/usr/bin/env python3
import io,json,os,subprocess,sys,tarfile,time
assert sys.argv[1]=='-k'
name={'--nrUE':'tue.tar.gz','--tuePhy':'tue_simuphy.tar.gz'}[sys.argv[2]]
sha=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
print('FIXTURE_BUILD_READY',flush=True)
time.sleep(float(os.environ.get('XRAN_BUILD_DELAY_SECONDS','0')))
subprocess.run(['gcc','fixture.c','-o','fixture'],check=True)
with tarfile.open(name,'w:gz') as archive:
 archive.add('fixture',arcname='fixture')
 data=json.dumps({'commit':sha,'target':sys.argv[2]}).encode();m=tarfile.TarInfo('fixture.json');m.size=len(data);archive.addfile(m,io.BytesIO(data))
'''

sources = {
 'X-RAN-CLOUD': {'services/build.sh':SERVICE,'services/fixture.c':'int main(void){return 0;}\n',
                 'platform/k8s/images-build/images-build.sh':IMAGE},
 'xcn': {'helm/build-release.sh':Path('/fixture/xcn-release.sh').read_text(),
         'helm/xcn/Chart.yaml':'apiVersion: v2\nname: xcn\nversion: 1.0.0\nappVersion: "1.0.0"\n',
         'helm/xcn/values.yaml':'{}\n',
         'helm/xcn/templates/test.yaml':'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: cache-fixture\n'},
 'XTUE-CLOUD': {'cmake_targets/build_oai':XTUE,'cmake_targets/fixture.c':'int main(void){return 0;}\n'}
}
pins={}
for name, files in sources.items():
 work=ROOT/(name+'-work');work.mkdir()
 git(work,'init','-b','main');git(work,'config','user.name','Cache fixture')
 git(work,'config','user.email','fixture@example.invalid');git(work,'config','commit.gpgsign','false')
 for path,body in files.items():
  path=work/path;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(body)
  if body.startswith('#!'):path.chmod(0o755)
 (work/'payload').write_bytes(os.urandom(256*1024));(work/'VERSION').write_text('1')
 git(work,'add','.');git(work,'commit','-m','fixture v1');git(work,'tag','-a','cache-v1','-m','fixture v1')
 old=git(work,'rev-parse','HEAD');(work/'VERSION').write_text('2');git(work,'commit','-am','fixture main')
 head=git(work,'rev-parse','HEAD');git(work,'branch','feature/cache',old)
 subprocess.run(['git','clone','--bare',str(work),str(ROOT/(name+'.git'))],check=True)
 pins[name]={'head':head,'old':old}
(ROOT/'pins.json').write_text(json.dumps(pins))
traffic={name:0 for name in sources};traffic_lock=threading.Lock()


class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def handle_git(self):
  parsed=urlsplit(self.path)
  if parsed.path in ('/pins.json','/metrics.json'):
   with traffic_lock:body=json.dumps(pins if parsed.path=='/pins.json' else traffic).encode()
   self.send_response(200);self.end_headers();self.wfile.write(body);return
  body=self.rfile.read(int(self.headers.get('Content-Length','0')))
  if self.headers.get('Content-Encoding')=='gzip':body=gzip.decompress(body)
  env=dict(os.environ,GIT_PROJECT_ROOT=str(ROOT),GIT_HTTP_EXPORT_ALL='1',PATH_INFO=parsed.path,
    QUERY_STRING=parsed.query,REQUEST_METHOD=self.command,CONTENT_TYPE=self.headers.get('Content-Type',''),
    CONTENT_LENGTH=str(len(body)),HTTP_GIT_PROTOCOL=self.headers.get('Git-Protocol',''))
  result=subprocess.run(['git','http-backend'],input=body,stdout=subprocess.PIPE,env=env,check=True)
  headers,_,body=result.stdout.partition(b'\r\n\r\n')
  headers=[line.split(':',1) for line in headers.decode().splitlines() if ':' in line]
  status=next((int(v.strip().split()[0]) for n,v in headers if n.lower()=='status'),200)
  self.send_response(status)
  for name,value in headers:
   if name.lower()!='status':self.send_header(name,value.strip())
  self.end_headers();self.wfile.write(body)
  if parsed.path.endswith('/git-upload-pack'):
   project=parsed.path.split('/')[1].removesuffix('.git')
   with traffic_lock:traffic[project]+=len(body)
 do_GET=handle_git
 do_POST=handle_git


ThreadingHTTPServer(('0.0.0.0',8000),Handler).serve_forever()
