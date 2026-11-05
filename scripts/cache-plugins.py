#!/usr/bin/env python3
"""缓存官方管理器已解析的插件版本；curl 重试并验证发布 SHA256/ZIP/插件身份。"""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / '.cache'


def sha256(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def main():
    import zipfile
    text = (CACHE / 'plugins-resolved.log').read_text()
    if 'Resulting plugin list:' not in text:
        raise RuntimeError('官方插件管理器未输出完整结果')
    selected = dict(re.findall(r'^([A-Za-z0-9_-]+) ([^\s]+)$', text.split('Resulting plugin list:')[-1], re.M))
    if not selected:
        raise RuntimeError('解析插件列表为空')
    (CACHE / 'plugins.complete').unlink(missing_ok=True)
    meta_dir = CACHE / 'plugin-manager-cache/jenkins-plugin-management-cli'
    available = json.loads((meta_dir / 'plugin-versions.json').read_text())['plugins']
    uc = json.loads((meta_dir / 'update-center-2.580.2.json').read_text())['plugins']

    def download(item):
        name, version = item
        expected = available.get(name, {}).get(version, {}).get('sha256')
        if not expected and uc.get(name, {}).get('version') == version:
            expected = uc[name].get('sha256')
        if not expected:
            raise RuntimeError(f'官方元数据缺少 SHA256: {name}:{version}')
        expected = expected.lower() if len(expected) == 64 else base64.b64decode(expected).hex()
        path = CACHE / 'plugins' / (name + '.jpi')
        if not path.exists() or sha256(path) != expected:
            part = path.with_suffix('.part')
            subprocess.run(['curl', '-fsSL', '--retry', '8', '--retry-all-errors', '--connect-timeout', '12',
                '--max-time', '180', f'https://archives.jenkins.io/plugins/{name}/{version}/{name}.hpi', '-o', part], check=True)
            if sha256(part) != expected:
                raise RuntimeError(f'插件 SHA256 不匹配: {name}')
            part.replace(path)
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None:
                raise RuntimeError('损坏的插件: ' + name)
            manifest = archive.read('META-INF/MANIFEST.MF').decode().replace('\r\n ', '').replace('\r\n', '\n')
            fields = dict(line.split(': ', 1) for line in manifest.splitlines() if ': ' in line)
            if fields['Short-Name'] != name or fields['Plugin-Version'] != version:
                raise RuntimeError('插件身份不匹配: ' + name)
        print('Verified plugin ' + name + ':' + version, flush=True)
        return name, version, expected

    with ThreadPoolExecutor(max_workers=4) as pool:
        verified = sorted(pool.map(download, sorted(selected.items())))
    (ROOT / 'config/plugins.lock').write_text(''.join(f'{name}:{version}\n' for name, version, _ in verified))
    (ROOT / 'config/plugins.sha256').write_text(''.join(f'{digest}  {name}.jpi\n' for name, _, digest in verified))
    (CACHE / 'plugins.complete').write_text('complete\n')


if __name__ == '__main__':
    main()
