#!/usr/bin/env bash
# Package the installed SDK files so legacy dpkg-based checks work offline.
set -euo pipefail
sdk_version=1.5.2
package_version="$sdk_version-1xran1"
prefix="/usr/local/protobuf-c-$sdk_version"
architecture=$(dpkg --print-architecture)
multiarch=$(dpkg-architecture -qDEB_HOST_MULTIARCH)
package_work=$(mktemp -d)
trap 'rm -rf "$package_work"' EXIT

test -x "$prefix/bin/protoc-gen-c"
test "$(PKG_CONFIG_PATH="$prefix/lib/pkgconfig" pkg-config --modversion libprotobuf-c)" = "$sdk_version"
tar -xOf "/opt/xran/sources/protobuf-c-$sdk_version.tar.gz" \
    "protobuf-c-$sdk_version/LICENSE" > "$package_work/LICENSE"

runtime_root="$package_work/libprotobuf-c1"
devel_root="$package_work/libprotobuf-c-dev"
compiler_root="$package_work/protobuf-c-compiler"
install -d "$runtime_root$prefix/lib" "$runtime_root/usr/lib/$multiarch" \
    "$devel_root$prefix/lib" "$devel_root/usr/lib/$multiarch/pkgconfig" \
    "$devel_root/usr/include/google" "$compiler_root$prefix" "$compiler_root/usr/bin"

cp -a "$prefix/lib/"libprotobuf-c.so.* "$runtime_root$prefix/lib/"
ln -s "$prefix/lib/libprotobuf-c.so.1" "$runtime_root/usr/lib/$multiarch/libprotobuf-c.so.1"
cp -a "$prefix/include" "$devel_root$prefix/"
cp -a "$prefix/lib/libprotobuf-c.a" "$prefix/lib/libprotobuf-c.la" \
    "$prefix/lib/libprotobuf-c.so" "$prefix/lib/pkgconfig" "$devel_root$prefix/lib/"
ln -s "$prefix/include/protobuf-c" "$devel_root/usr/include/protobuf-c"
ln -s "$prefix/include/google/protobuf-c" "$devel_root/usr/include/google/protobuf-c"
for library in libprotobuf-c.a libprotobuf-c.la libprotobuf-c.so; do
    ln -s "$prefix/lib/$library" "$devel_root/usr/lib/$multiarch/$library"
done
ln -s "$prefix/lib/pkgconfig/libprotobuf-c.pc" \
    "$devel_root/usr/lib/$multiarch/pkgconfig/libprotobuf-c.pc"
cp -a "$prefix/bin" "$compiler_root$prefix/"
for executable in protoc-c protoc-gen-c; do
    ln -s "$prefix/bin/$executable" "$compiler_root/usr/bin/$executable"
done

build_package() {
    local name=$1 description=$2 dependencies=$3
    local package_root="$package_work/$name"
    install -d "$package_root/DEBIAN" "$package_root/usr/share/doc/$name"
    install -m 0644 "$package_work/LICENSE" "$package_root/usr/share/doc/$name/copyright"
    cat > "$package_root/DEBIAN/control" <<EOF
Package: $name
Version: $package_version
Architecture: $architecture
Maintainer: X-RAN-CLOUD CI <ci@example.invalid>
Section: devel
Priority: optional
Depends: $dependencies
Description: $description
 Built from the pinned protobuf-c $sdk_version sources in the X-RAN-CLOUD SDK.
 Compiler binaries use protobuf 3.21.11 supplied by the complete SDK image.
EOF
    (
        cd "$package_root"
        find usr -type f -print0 | LC_ALL=C sort -z | xargs -0 -r md5sum
    ) > "$package_root/DEBIAN/md5sums"
    dpkg-deb --root-owner-group --build "$package_root" "$package_work/$name.deb"
}

build_package libprotobuf-c1 'protobuf-c SDK runtime library' 'libc6 (>= 2.35)'
build_package libprotobuf-c-dev 'protobuf-c SDK headers and development libraries' \
    "libprotobuf-c1 (= $package_version)"
build_package protobuf-c-compiler 'protobuf-c SDK code generator' \
    'libc6 (>= 2.35), libgcc-s1, libstdc++6, zlib1g'
dpkg --install "$package_work/libprotobuf-c1.deb" \
    "$package_work/libprotobuf-c-dev.deb" "$package_work/protobuf-c-compiler.deb"
ldconfig
dpkg-query -W > /opt/packages.lock
