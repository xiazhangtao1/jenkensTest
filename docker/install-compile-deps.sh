#!/usr/bin/env bash
set -euo pipefail
jobs=${BUILD_JOBS:-4}
sdk_work=/tmp/xran-sdk
mkdir -p "$sdk_work"
extract() {
    local name=$1 archive=$2
    mkdir -p "$sdk_work/$name"
    tar -xf "/opt/xran/sources/$archive" -C "$sdk_work/$name" --strip-components=1
}
cmake_install() {
    local name=$1
    shift
    cmake -S "$sdk_work/$name" -B "$sdk_work/$name/_build" -G Ninja \
        -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr/local \
        -DCMAKE_INSTALL_LIBDIR=lib -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
        -DBUILD_SHARED_LIBS=ON "$@"
    cmake --build "$sdk_work/$name/_build" --parallel "$jobs"
    cmake --install "$sdk_work/$name/_build"
}
case "$1" in
protobuf)
    extract protobuf protobuf-all-21.11.tar.gz
    cmake -S "$sdk_work/protobuf/cmake" -B "$sdk_work/protobuf/_build" -G Ninja \
        -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr/local/protobuf \
        -DCMAKE_INSTALL_LIBDIR=lib -Dprotobuf_BUILD_TESTS=OFF -Dprotobuf_BUILD_SHARED_LIBS=ON
    cmake --build "$sdk_work/protobuf/_build" --parallel "$jobs"
    cmake --install "$sdk_work/protobuf/_build"
    echo /usr/local/protobuf/lib > /etc/ld.so.conf.d/xran-protobuf.conf
    ;;
protobuf-c)
    extract protobuf-c protobuf-c-1.5.2.tar.gz
    cd "$sdk_work/protobuf-c"
    ./configure --prefix=/usr/local/protobuf-c-1.5.2
    make -j"$jobs"
    make install
    echo /usr/local/protobuf-c-1.5.2/lib > /etc/ld.so.conf.d/xran-protobuf-c.conf
    ;;
boost)
    extract boost boost_1_83_0.tar.bz2
    cd "$sdk_work/boost"
    ./bootstrap.sh --prefix=/usr/local
    ./b2 -j"$jobs" variant=release link=shared,static threading=multi cxxflags=-fPIC \
        --without-python --without-mpi --without-graph_parallel install
    ;;
abseil)
    extract abseil abseil-20220623.1.tar.gz
    cmake_install abseil -DABSL_BUILD_TESTING=OFF -DABSL_PROPAGATE_CXX_STD=ON -DCMAKE_CXX_STANDARD=14
    ;;
grpc)
    extract grpc grpc-1.49.1.tar.gz
    for dependency in envoy-api googleapis opencensus-proto xds protoc-gen-validate upb; do
        mkdir -p "$sdk_work/grpc/third_party/$dependency"
        tar -xf "/opt/xran/sources/grpc-$dependency.tar.gz" \
            -C "$sdk_work/grpc/third_party/$dependency" --strip-components=1
    done
    cmake_install grpc -DgRPC_INSTALL=ON -DgRPC_BUILD_TESTS=OFF -DgRPC_BUILD_CSHARP_EXT=OFF \
        -DgRPC_ABSL_PROVIDER=package -DgRPC_PROTOBUF_PROVIDER=package -DgRPC_PROTOBUF_PACKAGE_TYPE=CONFIG \
        -DgRPC_SSL_PROVIDER=package -DgRPC_ZLIB_PROVIDER=package -DgRPC_CARES_PROVIDER=package \
        -DgRPC_RE2_PROVIDER=package -DgRPC_BUILD_GRPC_CSHARP_PLUGIN=OFF \
        -DgRPC_BUILD_GRPC_NODE_PLUGIN=OFF -DgRPC_BUILD_GRPC_OBJECTIVE_C_PLUGIN=OFF \
        -DgRPC_BUILD_GRPC_PHP_PLUGIN=OFF -DgRPC_BUILD_GRPC_PYTHON_PLUGIN=OFF -DgRPC_BUILD_GRPC_RUBY_PLUGIN=OFF
    ;;
cpprestsdk)
    extract cpprestsdk cpprestsdk-2.10.19.tar.gz
    cmake_install cpprestsdk -DCPPREST_EXCLUDE_WEBSOCKETS=ON -DBUILD_TESTS=OFF -DBUILD_SAMPLES=OFF
    ;;
etcd)
    extract etcd etcd-cpp-apiv3-0.15.4.tar.gz
    cmake_install etcd -DBUILD_ETCD_TESTS=OFF -DETCD_W_STRICT=OFF -DETCD_CMAKE_CXX_STANDARD=14
    ;;
telemetry)
    extract json json-3.11.3.tar.gz
    cmake_install json -DJSON_BuildTests=OFF -DJSON_Install=ON
    extract spdlog spdlog-1.14.1.tar.gz
    cmake_install spdlog -DSPDLOG_BUILD_TESTS=OFF -DSPDLOG_BUILD_EXAMPLE=OFF -DSPDLOG_BUILD_SHARED=ON
    extract telemetry opentelemetry-cpp-1.17.0.tar.gz
    extract telemetry-proto opentelemetry-proto-1.3.2.tar.gz
    cmake_install telemetry -DBUILD_TESTING=OFF -DWITH_EXAMPLES=OFF -DWITH_BENCHMARK=OFF \
        -DWITH_OTLP_GRPC=ON -DWITH_OTLP_HTTP=ON -DWITH_ABSEIL=OFF \
        -DOTELCPP_PROTO_PATH="$sdk_work/telemetry-proto" -DCMAKE_CXX_STANDARD=14
    ;;
networking)
    extract websockets libwebsockets-4.5.8.tar.gz
    cmake_install websockets -DLWS_WITHOUT_TESTAPPS=ON -DLWS_WITH_STATIC=ON
    extract rohc rohc-1.7.0.tar.gz
    cd "$sdk_work/rohc"
    ./configure --prefix=/usr --libdir=/usr/lib64 CFLAGS=-fcommon
    make -j"$jobs"
    make install
    echo /usr/lib64 > /etc/ld.so.conf.d/rohc.conf
    mkdir -p /opt/dpdk-22.03
    tar -xf /opt/xran/sources/dpdk-22.03.tar.gz -C /opt/dpdk-22.03 --strip-components=1
    cd /opt/dpdk-22.03
    meson setup build --prefix=/usr/local --libdir=lib -Dplatform=generic -Dtests=false -Denable_kmods=false
    ninja -C build -j"$jobs"
    ninja -C build install
    tar -xf /opt/xran/sources/airtracer.tar.gz -C /usr/local/lib --strip-components=1
    ;;
asn1c)
    extract asn1c asn1c-0.9.29.tar.gz
    cd "$sdk_work/asn1c"
    autoreconf -iv
    ./configure --prefix=/usr/local
    make -j"$jobs"
    make install
    ;;
*) echo "Unknown dependency: $1" >&2; exit 2 ;;
esac
ldconfig
rm -rf "$sdk_work"
