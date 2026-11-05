#!/usr/bin/env python3
"""只读 dumb-HTTP Git 镜像；仅演示/断网验收启用。"""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', 8000),
        partial(SimpleHTTPRequestHandler, directory='/opt/demo')).serve_forever()
