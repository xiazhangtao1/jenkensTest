#!/usr/bin/env bash
set -euo pipefail
# Intel packages are used as locked binary SDK inputs. Extract only the
# selected CPU SDK/runtime components; do not install a DPC++/MPI toolchain.
python3 - <<'PY'
import hashlib
import json
from pathlib import Path
import subprocess

lock = json.loads(Path('/opt/xran/mkl.lock.json').read_text())
directory = Path('/opt/xran/mkl-deps')
for item in lock['packages']:
    path = directory / item['file']
    value = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            value.update(block)
    if path.stat().st_size != item['bytes'] or value.hexdigest() != item['sha256']:
        raise SystemExit('MKL 依赖校验失败：' + path.name)
for item in lock['packages']:
    subprocess.run(['dpkg-deb', '--extract', str(directory / item['file']), '/'], check=True)
root = Path(lock['root'])
for name in ('mkl.h', 'mkl_vsl.h', 'mkl_version.h'):
    if not (root / 'include' / name).is_file():
        raise SystemExit('MKL 头文件缺失：' + name)
libraries = [p for name in ('libmkl_rt.so', 'libiomp5.so', 'libtbb.so.12')
             for p in Path('/opt/intel/oneapi').rglob(name) if p.is_file()]
if {p.name for p in libraries} != {'libmkl_rt.so', 'libiomp5.so', 'libtbb.so.12'}:
    raise SystemExit('MKL/OpenMP/TBB CPU 运行库不完整')
Path('/etc/ld.so.conf.d/xran-mkl.conf').write_text(
    ''.join(str(p) + '\n' for p in sorted({p.parent for p in libraries})))
PY
ldconfig
