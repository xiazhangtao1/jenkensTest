#!/usr/bin/env bash
set -euo pipefail
sdk_check_dir=$(mktemp -d)
trap 'rm -rf "$sdk_check_dir"' EXIT
cmake -S /opt/xran/compile-check -B "$sdk_check_dir" -G Ninja
cmake --build "$sdk_check_dir" --parallel 2
ctest --test-dir "$sdk_check_dir" --output-on-failure
if ldd "$sdk_check_dir/protobuf-static-check" | python3 -c 'import sys; sys.exit(0 if "libprotobuf.so" in sys.stdin.read() else 1)'; then
    echo '静态链接检查错误地依赖 libprotobuf.so' >&2
    exit 1
fi
protoc --version
protoc-c --version
asn1c -version
protoc-c --proto_path=/opt/xran/compile-check --c_out="$sdk_check_dir" /opt/xran/compile-check/check.proto
gcc -c "$sdk_check_dir/check.pb-c.c" $(pkg-config --cflags libprotobuf-c) -o "$sdk_check_dir/check.pb-c.o"
mkdir "$sdk_check_dir/asn1"
printf 'Check DEFINITIONS ::= BEGIN Value ::= INTEGER END\n' > "$sdk_check_dir/asn1/check.asn1"
(cd "$sdk_check_dir/asn1" && asn1c -pdu=all -fcompound-names check.asn1 > generation.log 2>&1)
test -s "$sdk_check_dir/asn1/Value.c"
pkg-config --modversion libdpdk rohc protobuf libprotobuf-c libwebsockets
docker --version
python3 - <<'PY'
import ctypes
from pathlib import Path
for archive in ('/usr/local/protobuf/lib/libprotobuf.a', '/usr/local/lib/libgrpc++.a',
                '/usr/local/lib/libabsl_base.a', '/usr/local/lib/libopentelemetry_trace.a', '/usr/local/lib/libspdlog.a'):
    assert Path(archive).stat().st_size > 0, archive
for library in ('libairtracer.so', 'librohc.so', 'librte_eal.so', 'libgrpc++.so', 'libetcd-cpp-api.so'):
    ctypes.CDLL(library)
    print(library + ': loaded')
PY
