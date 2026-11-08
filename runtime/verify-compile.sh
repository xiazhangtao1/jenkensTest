#!/usr/bin/env bash
set -euo pipefail
sdk_check_dir=$(mktemp -d)
trap 'rm -rf "$sdk_check_dir"' EXIT
command -v pkgconf
pkgconf --version
test -d /opt/dpdk-22.03/build
test "$(pkg-config --modversion libprotobuf-c)" = "$XRAN_PROTOBUF_C_VERSION"
protoc-c --version | grep -qx 'protobuf-c 1.5.2'
python3 /opt/xran/run-build.py capabilities | python3 -c \
    'import json,sys; c=json.load(sys.stdin); assert c["xranServiceCompatibility"] == c["xranImageArchiveFallback"] == 1'
for package in protobuf-c-compiler libprotobuf-c-dev libprotobuf-c1 uuid-dev; do
    test "$(dpkg-query -W -f='${Status}' "$package")" = 'install ok installed'
done
for package in protobuf-c-compiler libprotobuf-c-dev libprotobuf-c1; do
    test "$(dpkg-query -W -f='${Version}' "$package")" = \
        "$(case "$XRAN_PROTOBUF_C_VERSION" in 1.3.3) echo 1.3.3-1ubuntu2.1;; 1.5.2) echo 1.5.2-1xran1;; *) exit 1;; esac)"
done
test -z "$(dpkg --verify protobuf-c-compiler libprotobuf-c-dev libprotobuf-c1)"
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
cat > "$sdk_check_dir/protobuf-c-runtime.c" <<'C'
#include <protobuf-c/protobuf-c.h>
int main(void) {
    return protobuf_c_version_number() == PROTOBUF_C_VERSION_NUMBER ? 0 : 1;
}
C
# Also exercise standard include/library paths used by legacy build scripts.
gcc "$sdk_check_dir/protobuf-c-runtime.c" -lprotobuf-c -o "$sdk_check_dir/protobuf-c-runtime"
"$sdk_check_dir/protobuf-c-runtime"
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
