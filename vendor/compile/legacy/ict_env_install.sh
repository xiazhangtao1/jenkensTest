#!/bin/bash -exv
CURR_PATH=$PWD
MIRROR_IP=192.168.10.36

# OVERWRITE SOURCES.LIST
cp -rf sources.list /etc/apt
apt update

# APT PACKETS INSTALL  
apt install -y build-essential python3-pyelftools libnuma-dev python3-pip pkgconf libxml2-dev libhugetlbfs-bin \
    net-tools nettle-dev openssl libssl-dev libhugetlbfs-dev  libconfig-dev libconfig9 libcurl4 libcurl4-openssl-dev \
    libevent-dev libyaml-dev libcyaml-dev gdb 
  
# DPDK COMPILE PYPI PACKETS INSTALL  
pip3 install meson ninja -i http://$MIRROR_IP/pypi/simple/ --trusted-host $MIRROR_IP

# DPDK INSTALL
cd $CURR_PATH
tar -xvf dpdk-22.03.tar
mv dpdk-22.03 /opt && cd /opt/dpdk-22.03
rm -rf /opt/dpdk-22.03/build
meson setup -Dplatform=generic build
cd build && ninja
ldconfig

# WLS_MODE INSTALL
cd $CURR_PATH
tar -xvf wls_mod.tar
mv wls_mod /opt

# ROHC INSTALL
cd $CURR_PATH
tar -xvf rohc-1.7.0.tar.xz
cd ./rohc-1.7.0
./configure --prefix=/usr --libdir=/usr/lib64
make clean
make && make install

# REMOVE ROHC UNTAR FOLDER
cd $CURR_PATH
rm -rf rohc-1.7.0

# ADD ROHC INSTALL PATH TO LDCONFIG DIRECTORY
cd $CURR_PATH
cp rohc.conf /etc/ld.so.conf.d
ldconfig

# ADD libairtracer.so
cd $CURR_PATH
cp libairtracer.so /usr/lib/x86_64-linux-gnu/
ldconfig

./l_BaseKit_p_2022.2.0.262_offline.sh

