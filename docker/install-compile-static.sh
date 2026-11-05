#!/usr/bin/env bash
# Keep static archives required by the original environment alongside the shared SDK.
# Preserve shared CMake exports as the default; explicit .a users can use the same headers/ABI.
set -euo pipefail
jobs=${BUILD_JOBS:-4}
static_work=/tmp/xran-sdk-static
mkdir -p "$static_work"
trap 'rm -rf "$static_work"' EXIT
extract() {
    mkdir -p "$static_work/$1"
    tar -xf "/opt/xran/sources/$2" -C "$static_work/$1" --strip-components=1
}
configure() {
    local name=$1
    shift
    cmake -S "$static_work/$name" -B "$static_work/$name/_static" -G Ninja \
        -DCMAKE_BUILD_TYPE=Release -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
        -DCMAKE_INSTALL_PREFIX=/usr/local -DCMAKE_INSTALL_LIBDIR=lib \
        -DBUILD_SHARED_LIBS=OFF "$@"
    cmake --build "$static_work/$name/_static" --parallel "$jobs"
}
archives() {
    python3 - "$static_work/$1/_static" "$2" <<'PY'
from pathlib import Path
import shutil, sys
target = Path(sys.argv[2])
target.mkdir(parents=True, exist_ok=True)
for path in Path(sys.argv[1]).rglob('*.a'):
    if path.name.startswith('lib'):
        shutil.copy2(path, target / path.name)
PY
}
extract protobuf protobuf-all-21.11.tar.gz
cmake -S "$static_work/protobuf/cmake" -B "$static_work/protobuf/_static" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
    -Dprotobuf_BUILD_TESTS=OFF -Dprotobuf_BUILD_SHARED_LIBS=OFF
cmake --build "$static_work/protobuf/_static" --parallel "$jobs" --target libprotobuf libprotobuf-lite libprotoc
archives protobuf /usr/local/protobuf/lib

extract abseil abseil-20220623.1.tar.gz
configure abseil -DABSL_BUILD_TESTING=OFF -DABSL_PROPAGATE_CXX_STD=ON -DCMAKE_CXX_STANDARD=14
archives abseil /usr/local/lib

extract grpc grpc-1.49.1.tar.gz
for dependency in envoy-api googleapis opencensus-proto xds protoc-gen-validate upb; do
    mkdir -p "$static_work/grpc/third_party/$dependency"
    tar -xf "/opt/xran/sources/grpc-$dependency.tar.gz" \
        -C "$static_work/grpc/third_party/$dependency" --strip-components=1
done
configure grpc -DgRPC_BUILD_TESTS=OFF -DgRPC_BUILD_CSHARP_EXT=OFF -DgRPC_BUILD_CODEGEN=OFF \
    -DgRPC_ABSL_PROVIDER=package -DgRPC_PROTOBUF_PROVIDER=package -DgRPC_PROTOBUF_PACKAGE_TYPE=CONFIG \
    -DgRPC_SSL_PROVIDER=package -DgRPC_ZLIB_PROVIDER=package -DgRPC_CARES_PROVIDER=package -DgRPC_RE2_PROVIDER=package
archives grpc /usr/local/lib

extract telemetry opentelemetry-cpp-1.17.0.tar.gz
extract telemetry-proto opentelemetry-proto-1.3.2.tar.gz
configure telemetry -DBUILD_TESTING=OFF -DWITH_EXAMPLES=OFF -DWITH_BENCHMARK=OFF \
    -DWITH_OTLP_GRPC=ON -DWITH_OTLP_HTTP=ON -DWITH_ABSEIL=OFF \
    -DOTELCPP_PROTO_PATH="$static_work/telemetry-proto" -DCMAKE_CXX_STANDARD=14
archives telemetry /usr/local/lib
ldconfig
