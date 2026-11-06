#!/usr/bin/env python3
"""Build the internal plugin with a pinned JDK/Maven image; OFFLINE never uses network."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / 'plugins/xran-build-platform'
CACHE = ROOT / '.cache'
IMAGE = 'maven:3.9.9-eclipse-temurin-21-jammy'
LOCK = ROOT / 'config/platform-plugin.lock.json'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_sha():
    h = hashlib.sha256()
    paths = [PROJECT / 'pom.xml', ROOT / 'scripts/platform-plugin.py', ROOT / 'config/plugins.lock']
    paths += sorted((PROJECT / 'src').rglob('*'))
    for path in paths:
        if path.is_file():
            h.update(str(path.relative_to(ROOT)).encode() + b'\0' + path.read_bytes())
    return h.hexdigest()


def build(offline=False):
    initial_sources = source_sha()
    ns = {'m': 'http://maven.apache.org/POM/4.0.0'}
    pom = ET.parse(PROJECT / 'pom.xml')
    managed = {d.findtext('m:artifactId', namespaces=ns): d.findtext('m:version', namespaces=ns)
               for d in pom.findall('./m:dependencyManagement/m:dependencies/m:dependency', ns)}
    official = dict(line.split(':', 1) for line in (ROOT / 'config/plugins.lock').read_text().splitlines() if line)
    if managed != official:
        raise RuntimeError('插件 POM 的官方依赖版本与 config/plugins.lock 不一致')
    m2 = CACHE / 'platform-m2'
    artifacts = CACHE / 'local-plugins'
    m2.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    # HPI and JPI are the same official archive. Reuse the controller's already
    # verified cache for JenkinsRule instead of downloading these large files twice.
    for plugin in (CACHE / 'plugins').glob('*.jpi'):
        with zipfile.ZipFile(plugin) as archive:
            name = next(n for n in archive.namelist() if n.startswith('META-INF/maven/') and n.endswith('/pom.properties'))
            props = dict(line.split('=', 1) for line in archive.read(name).decode().splitlines()
                         if '=' in line and not line.startswith('#'))
        artifact, version = props['artifactId'], props['version']
        if official.get(artifact) != version:
            raise RuntimeError('官方插件缓存与锁文件版本不符：' + artifact)
        directory = m2 / props['groupId'].replace('.', '/') / artifact / version
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f'{artifact}-{version}.hpi'
        if not target.exists() or sha(target) != sha(plugin):
            shutil.copy2(plugin, target)
        target.with_suffix('.hpi.sha1').write_text(hashlib.sha1(target.read_bytes()).hexdigest())
        tracking = directory / '_remote.repositories'
        text = tracking.read_text() if tracking.exists() else ''
        line = target.name + '>repo.jenkins-ci.org=\n'
        if line not in text:
            tracking.write_text(text + line)
    inspected = subprocess.run(['docker', 'image', 'inspect', IMAGE], capture_output=True, text=True)
    if inspected.returncode:
        archive = CACHE / 'images/maven_3.9.9-eclipse-temurin-21-jammy.tar'
        if offline:
            raise RuntimeError('离线模式缺少固定 Maven 镜像；先 docker load -i .cache/build-base-images.tar')
        if not archive.exists():
            subprocess.run(['python3', ROOT / 'scripts/fetch-image.py', IMAGE, archive], check=True)
        subprocess.run(['docker', 'load', '-i', archive], check=True)
        inspected = subprocess.run(['docker', 'image', 'inspect', IMAGE], check=True, capture_output=True, text=True)
    image_id = json.loads(inspected.stdout)[0]['Id']
    old = json.loads(LOCK.read_text()) if LOCK.exists() else {}
    if old and image_id != old['buildImageId']:
        raise RuntimeError('Maven 构建镜像 ID 与锁文件不符')
    settings = ET.Element('settings')
    # Maven does not automatically honor the shell's HTTPS_PROXY. This file is
    # deliberately outside the bundled cache; no proxy credentials enter artifacts.
    proxy = urllib.parse.urlsplit(os.environ.get('HTTPS_PROXY', os.environ.get('https_proxy', '')))
    if proxy.hostname and not offline:
        p = ET.SubElement(ET.SubElement(settings, 'proxies'), 'proxy')
        for key, value in {'id': 'operator-proxy', 'active': 'true', 'protocol': 'http',
                           'host': proxy.hostname, 'port': str(proxy.port or 80),
                           'nonProxyHosts': 'localhost|127.0.0.1'}.items():
            ET.SubElement(p, key).text = value
        if proxy.username:
            ET.SubElement(p, 'username').text = urllib.parse.unquote(proxy.username)
            ET.SubElement(p, 'password').text = urllib.parse.unquote(proxy.password or '')
    settings_file = CACHE / 'platform-settings.xml'
    settings_file.touch(mode=0o600, exist_ok=True)
    settings_file.chmod(0o600)
    ET.ElementTree(settings).write(settings_file, encoding='utf-8', xml_declaration=True)
    command = ['docker', 'run', '--rm', '--network', 'none' if offline else 'host',
               '--user', f'{os.getuid()}:{os.getgid()}', '-e', 'MAVEN_CONFIG=/tmp/.m2',
               '-e', 'MAVEN_OPTS=-Xmx2g -Duser.home=/tmp',
               '-v', f'{ROOT}:/workspace', '-v', f'{m2}:/m2',
               '-v', f'{settings_file}:/tmp/settings.xml:ro',
               '-w', '/workspace/plugins/xran-build-platform', image_id,
               'mvn', '-B', '-ntp', '-s', '/tmp/settings.xml', '-Dmaven.repo.local=/m2',
               '-Daccess-modifier-checker.failOnError=true', '-Daether.connector.connectTimeout=15000',
               '-Daether.connector.requestTimeout=30000', *(['-o'] if offline else ['-U']), 'clean', 'verify']
    subprocess.run(command, check=True)
    if source_sha() != initial_sources:
        raise RuntimeError('构建期间插件源码有变化，请重新执行构建')
    hpi = PROJECT / 'target/xran-build-platform.hpi'
    with zipfile.ZipFile(hpi) as z:
        text = z.read('META-INF/MANIFEST.MF').decode().replace('\r\n ', '').replace('\r\n', '\n')
        manifest = dict(line.split(': ', 1) for line in text.splitlines() if ': ' in line)
        own = next(name for name in z.namelist() if name.startswith('WEB-INF/lib/xran-build-platform') and name.endswith('.jar'))
        with zipfile.ZipFile(io.BytesIO(z.read(own))) as jar:
            majors = {int.from_bytes(jar.read(name)[6:8], 'big') for name in jar.namelist() if name.endswith('.class')}
    if not majors or max(majors) > 65:
        raise RuntimeError('插件字节码超出 Java 21 兼容范围')
    target = artifacts / 'xran-build-platform.jpi'
    shutil.copy2(hpi, target)
    record = {'id': 'xran-build-platform', 'version': manifest['Plugin-Version'],
              'jenkinsVersion': manifest['Jenkins-Version'], 'buildImage': IMAGE, 'buildImageId': image_id,
              'sourceSha256': source_sha(), 'sha256': sha(target), 'classMajor': max(majors),
              'dependencies': manifest.get('Plugin-Dependencies', '')}
    LOCK.write_text(json.dumps(record, indent=2) + '\n')
    (artifacts / 'manifest.json').write_text(json.dumps(record, indent=2) + '\n')
    print(f"内部插件构建验证完成：{record['id']}:{record['version']}，SHA256={record['sha256']}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--offline', action='store_true')
    build(parser.parse_args().offline)
