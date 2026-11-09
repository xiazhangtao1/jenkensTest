#!/usr/bin/env python3
"""Test-only read-only Ubuntu package mirror; never a general HTTP proxy."""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
from pathlib import Path
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request


def upstream_path(path):
    parsed = urllib.parse.urlsplit(path)
    decoded = re.sub(r'%(?:2[bB]|7[eE]|3[aA])', lambda m: urllib.parse.unquote(m[0]), parsed.path)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or '%' in decoded or '..' in decoded:
        raise ValueError('Only literal Ubuntu repository paths are accepted')
    patterns = [r'/ubuntu/dists/jammy(?:-updates|-backports|-security)?/[A-Za-z0-9/_.:+~-]+',
                r'/ubuntu/pool/(?:main|restricted|universe|multiverse)/[A-Za-z0-9+_.:~/-]+\.deb']
    if not any(re.fullmatch(pattern, decoded) for pattern in patterns):
        raise ValueError('Not an Ubuntu Jammy index or package')
    return 'https://archive.ubuntu.com' + decoded


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, 'Repository redirects are disabled', headers, fp)


def valid_index(path, url):
    match=re.search(r'/by-hash/(SHA256|SHA512)/([0-9a-f]+)$',url)
    if not match:return True
    value=hashlib.new(match[1].lower())
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):value.update(block)
    return value.hexdigest()==match[2]


class Mirror(BaseHTTPRequestHandler):
    cache = None
    opener = None

    def serve(self, head=False):
        try:
            if not ipaddress.ip_address(self.client_address[0]).is_private or int(self.headers.get('Content-Length', '0')) != 0:
                raise ValueError('Private-network read-only requests only')
            url = upstream_path(self.path)
        except ValueError as error:
            self.send_error(403, str(error)); return
        target = self.cache / urllib.parse.urlsplit(url).path.removeprefix('/ubuntu/')
        if target.is_file() and not valid_index(target,url):target.unlink()
        if not target.is_file():
            try:
                with self.opener.open(urllib.request.Request(url, headers={'User-Agent': 'Ubuntu-reliability-package-cache',
                        'Cache-Control':'no-cache','Pragma':'no-cache'}), timeout=90) as response:
                    if head:
                        self.send_response(200)
                        if response.headers.get('Content-Length'): self.send_header('Content-Length', response.headers['Content-Length'])
                        self.end_headers(); return
                    import tempfile
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as out:
                        temporary = Path(out.name)
                        try:
                            shutil.copyfileobj(response, out)
                            out.flush()
                            if not valid_index(temporary,url):raise OSError('Upstream by-hash index checksum mismatch')
                            temporary.replace(target)
                        finally: temporary.unlink(missing_ok=True)
            except urllib.error.HTTPError as error:
                self.send_error(error.code); return
            except OSError as error:
                self.send_error(502, str(error)); return
        self.send_response(200)
        self.send_header('Content-Length', str(target.stat().st_size))
        self.end_headers()
        if not head:
            try:
                with target.open('rb') as stream: shutil.copyfileobj(stream, self.wfile)
            except (BrokenPipeError, ConnectionResetError): pass

    def do_GET(self): self.serve()
    def do_HEAD(self): self.serve(head=True)
    # BaseHTTPRequestHandler rejects POST/PUT/CONNECT; no request body is forwarded.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--listen', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=18083)
    parser.add_argument('--cache', type=Path, required=True)
    args = parser.parse_args()
    Mirror.cache = args.cache; Mirror.cache.mkdir(parents=True, exist_ok=True)
    Mirror.opener = urllib.request.build_opener(NoRedirect())
    ThreadingHTTPServer((args.listen, args.port), Mirror).serve_forever()


if __name__ == '__main__': main()
