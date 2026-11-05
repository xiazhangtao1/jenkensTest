#!/bin/bash
# author zzk
########################################
# 自动检测执行模式: HTTP本地执行
########################################
set -euo pipefail

echo "[INFO] Detected remote install mode (via wget | bash)"
BASE_URL="http://192.168.10.36/xran_compile_dir"
WORKDIR=$(mktemp -d /tmp/xran_compile_XXXX)
echo "[INFO] Working directory: $WORKDIR"

# 下载压缩包（显示进度）
echo "[INFO] Downloading package archives..."
wget --progress=bar:force:noscroll -O "$WORKDIR/ict_env.tar.gz" "$BASE_URL/ict_env.tar.gz"
wget --progress=bar:force:noscroll -O "$WORKDIR/extend_install.tar.gz" "$BASE_URL/extend_install.tar.gz"

#校验
[[ -s "$WORKDIR/ict_env.tar.gz" ]] || { echo "[ERROR] ict_env.tar.gz DL failed"; exit 1; }
[[ -s "$WORKDIR/extend_install.tar.gz" ]] || { echo "[ERROR] extend_install.tar.gz DL failed"; exit 1; }

echo "[INFO] Extracting archives..."
tar -xzf "$WORKDIR/ict_env.tar.gz" -C "$WORKDIR"
tar -xzf "$WORKDIR/extend_install.tar.gz" -C "$WORKDIR"

CURR_PATH="$WORKDIR"
CURR_PATH1="$WORKDIR/ict_env"
CURR_PATH2="$WORKDIR/extend_install"

########################################
# 全局变量定义
########################################
GRUB_CONF="/etc/default/grub"
MIRROR_IP=192.168.10.36

echo "[INFO] Using CURR_PATH:  $CURR_PATH"
echo "[INFO] Using ICT_ENV:    $CURR_PATH1"
echo "[INFO] Using EXT_ENV:    $CURR_PATH2"

# VERSION
V=21.11; VC=1.5.2; LWS=4.5.8
P=/usr/local/protobuf; PC=/usr/local/protobuf-c-$VC
ENV_FILE=/etc/profile.d/protobuf.sh
########################################
# Installation begins
########################################

# APT PACKETS INSTALL
sudo apt install -y build-essential python3-pyelftools libnuma-dev python3-pip pkgconfig libxml2-dev libhugetlbfs-bin \
    net-tools nettle-dev openssl libssl-dev libhugetlbfs-dev libconfig-dev libconfig9 libcurl4 libcurl4-openssl-dev \
    libevent-dev libyaml-dev libcyaml-dev gdb jq curl libjson-c-dev openvswitch-switch openvswitch-common

sudo apt install -y ca-certificates ccache cmake libcurl4-openssl-dev libgrpc-dev libgrpc++-dev libprotobuf-dev libssl-dev libz-dev lsb-release protobuf-compiler-grpc screenfetch wget libboost-all-dev libsctp-dev libzmq3-dev uuid-dev

# DPDK COMPILE PYPI PACKETS INSTALL
pip3 install meson ninja -i http://$MIRROR_IP/pypi/simple/ --trusted-host $MIRROR_IP

# DPDK INSTALL
cd "$CURR_PATH1"
tar -xzvf dpdk-22.03.tar.gz
mv dpdk-22.03 /opt && cd /opt/dpdk-22.03
rm -rf /opt/dpdk-22.03/build
meson setup -Dplatform=generic build
cd build && ninja
sudo ninja install
ldconfig


# ROHC INSTALL
cd "$CURR_PATH1"
tar -xvf rohc-1.7.0.tar.xz
cd ./rohc-1.7.0
./configure --prefix=/usr --libdir=/usr/lib64
make clean
make && make install

# REMOVE ROHC UNTAR FOLDER
cd "$CURR_PATH1"
rm -rf rohc-1.7.0

# ADD ROHC INSTALL PATH TO LDCONFIG DIRECTORY
cd "$CURR_PATH1"
cp rohc.conf /etc/ld.so.conf.d
ldconfig


# NEWLY ADDED
if dpkg -l 2>/dev/null | grep -qE '^(ii\s+(libprotobuf-dev|protobuf-compiler))'; then
    echo "[INFO] 检测到 apt 版 protobuf，卸载中..."
    sudo apt remove -y libprotobuf-dev protobuf-compiler protobuf-compiler-grpc
fi

sudo apt install -y autoconf automake libtool pkg-config
sudo tee $ENV_FILE >/dev/null <<EOF
export PATH=$P/bin:$PC/bin:\$PATH
export PKG_CONFIG_PATH=$PC/lib/pkgconfig:$P/lib/pkgconfig:/usr/local/lib/pkgconfig:\$PKG_CONFIG_PATH
EOF
source $ENV_FILE

cd "$CURR_PATH1"
# protobuf
tar -zxvf protobuf-all-$V.tar.gz && cd protobuf-$V
./autogen.sh && ./configure --prefix=$P && make -j$(nproc) && sudo make install
echo "$P/lib" | sudo tee /etc/ld.so.conf.d/protobuf.conf && sudo ldconfig
cd ..

# protobuf-c
tar -zxvf protobuf-c-$VC.tar.gz && cd protobuf-c-$VC
./configure --prefix=$PC && make -j$(nproc) && sudo make install
echo "$PC/lib" | sudo tee /etc/ld.so.conf.d/protobuf-c.conf && sudo ldconfig
cd ..

# libwebsockets
tar -zxvf libwebsockets-$LWS.tar.gz && cd libwebsockets-$LWS
mkdir build && cd build
cmake .. && make -j$(nproc) && sudo make install && sudo ldconfig
cd ../..

protoc --version && protoc-c --version && \
pkg-config --modversion protobuf libprotobuf-c libwebsockets
echo 'source $ENV_FILE' >> ~/.bashrc

# libboost
cd "$CURR_PATH2"
tar -xvf boost_1_83_0.tar.bz2
cd ./boost_1_83_0
./bootstrap.sh
sudo ./b2 install

# lib confirm
cd "$CURR_PATH2"
sudo cp libetcd-cpp-api.so /usr/local/lib/
sudo chmod 755 /usr/local/lib/libetcd-cpp-api.so
sudo ldconfig

# etcd env
cd "$CURR_PATH2"
cd ./etcd_env/cpprestsdk
rm -r build && mkdir build && cd build
sudo cmake .. -DCPPREST_EXCLUDE_WEBSOCKETS=ON
sudo make -j$(nproc) && sudo make install

cd "$CURR_PATH2"
cd ./etcd_env/etcd-cpp-apiv3
rm -r build && mkdir build && cd build
sudo cmake ..
sudo make -j$(nproc) && sudo make install

# opentelemetry_env install
cd "$CURR_PATH2/opentelemetry_env/spdlog/spdlog" && \
    mkdir build && \
    cd build && \
    cmake .. -DCMAKE_INSTALL_PREFIX=/usr/local && \
    make -j$(nproc) && \
    make install

cd "$CURR_PATH2/opentelemetry_env/json/json" && \
    mkdir build && \
    cd build && \
    cmake .. -DCMAKE_INSTALL_PREFIX=/usr/local && \
    make -j$(nproc) && \
    make install

cp "$CURR_PATH2/opentelemetry_env/usr-local-lib/"lib* /usr/local/lib
cp -r "$CURR_PATH2/opentelemetry_env/usr-local-include/opentelemetry/" /usr/local/include
sudo ldconfig


# env soft-link
#ln -s /opt/dpdk-22.03/usertools/dpdk-devbind.py /usr/local/bin/dpdk-devbind.py
export PKG_CONFIG_PATH=$PKG_CONFIG_PATH:/usr/lib64/pkgconfig:/opt/dpdk-22.03/build/meson-uninstalled

########################################
# 结束提示 & 可选清理
########################################
echo "[SUCCESS] xran environment installation complete."

# 可选：清理临时目录（如果是远程执行）
if [[ -d "$WORKDIR" ]]; then
    echo "[INFO] Cleaning up temporary directory..."
    rm -rf "$WORKDIR"
fi
