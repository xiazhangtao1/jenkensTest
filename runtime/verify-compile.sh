#!/usr/bin/env bash
set -euo pipefail
sdk_check_dir=$(mktemp -d)
trap 'rm -rf "$sdk_check_dir"' EXIT
command -v pkgconf
pkgconf --version
while read -r line; do
    for package in ${line%%#*}; do
        package_name=${package%%=*}
        test "$(dpkg-query -W -f='${Status}' "$package_name")" = 'install ok installed'
        if [[ "$package" == *=* ]]; then
            test "$(dpkg-query -W -f='${Version}' "$package_name")" = "${package#*=}"
        fi
    done
done < /opt/xran/extra-apt-packages.txt
pkg-config --exists libxml++-2.6 libnghttp2 libxslt libibverbs
command -v unrar
command -v xxd
# Exercise the same binary-to-header command used by XTUE's T tracer.
(cd "$sdk_check_dir" && printf 'XTUE' > T_messages.txt && xxd -i T_messages.txt T_messages.txt.h)
cat > "$sdk_check_dir/xxd-check.c" <<'C'
#include <string.h>
#include "T_messages.txt.h"
int main(void) {
    return T_messages_txt_len != 4 || memcmp(T_messages_txt, "XTUE", 4) != 0;
}
C
gcc -Wall -Wextra -Werror "$sdk_check_dir/xxd-check.c" -o "$sdk_check_dir/xxd-check"
"$sdk_check_dir/xxd-check"
echo 'xxd -i header generation: OK'
infocmp xterm-256color >/dev/null
test -d /opt/dpdk-22.03/build
python3 - <<'PY'
import json
from pathlib import Path
import re
import subprocess

options = json.loads(subprocess.check_output(
    ['meson', 'introspect', '--buildoptions', '/opt/dpdk-22.03/build'], text=True))
value = next(item['value'] for item in options if item['name'] == 'max_lcores')
if str(value) != '256':
    raise SystemExit('DPDK Meson max_lcores 必须为 256，实际为 ' + str(value))
print('DPDK Meson max_lcores=256')
for name in ['/opt/dpdk-22.03/build/rte_build_config.h', '/usr/local/include/rte_build_config.h']:
    if not re.search(r'^#define\s+RTE_MAX_LCORE\s+256\s*$', Path(name).read_text(), re.MULTILINE):
        raise SystemExit('DPDK RTE_MAX_LCORE 必须为 256: ' + name)
    print(name + ': RTE_MAX_LCORE=256')
PY
test "$(pkg-config --modversion libprotobuf-c)" = "$XRAN_PROTOBUF_C_VERSION"
protoc-c --version | grep -qx 'protobuf-c 1.5.2'
python3 /opt/xran/run-build.py capabilities | python3 -c \
    'import json,sys; c=json.load(sys.stdin); assert c["xranServiceCompatibility"] == c["xranServiceArtifacts"] == c["xranImageArchiveFallback"] == c["projectRecipes"] == 1'
for package in protobuf-c-compiler libprotobuf-c-dev libprotobuf-c1 uuid-dev libzstd-dev libhwloc-dev libreadline-dev; do
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
(cd "$sdk_check_dir/asn1" && asn1c -pdu=all -fcompound-names -no-gen-OER -no-gen-example check.asn1 > generation.log 2>&1)
test -s "$sdk_check_dir/asn1/Value.c"
cat > "$sdk_check_dir/asn1/roundtrip.c" <<'C'
#include "Value.h"
#include "der_encoder.h"
#include "ber_decoder.h"
#include "per_encoder.h"
#include "per_decoder.h"
int main(void) {
    Value_t input = 42, *output = 0;
    unsigned char buffer[64];
    asn_enc_rval_t encoded = der_encode_to_buffer(&asn_DEF_Value, &input, buffer, sizeof(buffer));
    if (encoded.encoded < 0) return 1;
    asn_dec_rval_t decoded = ber_decode(0, &asn_DEF_Value, (void **)&output, buffer, encoded.encoded);
    int failed = decoded.code != RC_OK || !output || *output != input;
    ASN_STRUCT_FREE(asn_DEF_Value, output);
    if (failed) return failed;
    output = 0;
    encoded = uper_encode_to_buffer(&asn_DEF_Value, 0, &input, buffer, sizeof(buffer));
    if (encoded.encoded < 0) return 1;
    decoded = uper_decode_complete(0, &asn_DEF_Value, (void **)&output, buffer, (encoded.encoded + 7) / 8);
    failed = decoded.code != RC_OK || !output || *output != input;
    ASN_STRUCT_FREE(asn_DEF_Value, output);
    return failed;
}
C
find "$sdk_check_dir/asn1" -name '*.c' ! -name 'converter-*.c' -print0 | \
    xargs -0 gcc -DASN_DISABLE_OER_SUPPORT -I"$sdk_check_dir/asn1" -o "$sdk_check_dir/asn1/roundtrip"
"$sdk_check_dir/asn1/roundtrip"
pkg-config --modversion libdpdk rohc protobuf libprotobuf-c libwebsockets libzstd hwloc readline
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
