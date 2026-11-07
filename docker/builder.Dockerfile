ARG DOCKER_CLI_IMAGE=docker:29.9.3-cli
FROM ${DOCKER_CLI_IMAGE} AS dockercli
FROM ubuntu:22.04
ARG APT_SOURCES=config/sources.online.list
ENV DEBIAN_FRONTEND=noninteractive TZ=Asia/Shanghai
COPY ${APT_SOURCES} /etc/apt/sources.list
COPY .cache/compile-apt/ /tmp/apt-cache/
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d \
    && chmod +x /usr/sbin/policy-rc.d \
    && apt-get install -y --no-install-recommends /tmp/apt-cache/*.deb \
    && dpkg-query -W > /opt/packages.lock \
    && rm -rf /var/lib/apt/lists/* /tmp/apt-cache \
    && groupadd -g 1000 jenkins && useradd -m -u 1000 -g 1000 -s /bin/bash jenkins
ENV PATH=/usr/local/protobuf/bin:/usr/local/protobuf-c-1.5.2/bin:${PATH} \
    PKG_CONFIG_PATH=/usr/local/protobuf/lib/pkgconfig:/usr/local/protobuf-c-1.5.2/lib/pkgconfig:/usr/local/lib/pkgconfig:/usr/lib64/pkgconfig:/opt/dpdk-22.03/build/meson-uninstalled \
    CMAKE_PREFIX_PATH=/usr/local/protobuf:/usr/local/protobuf-c-1.5.2:/usr/local
COPY .cache/compile-sources/ /opt/xran/sources/
COPY vendor/compile/manifest.json /opt/xran/compile-sources.lock.json
COPY docker/install-compile-deps.sh /opt/xran/install-compile-deps.sh
ARG BUILD_JOBS=4
RUN cd /opt/xran/sources && sha256sum -c SHA256SUMS
RUN bash /opt/xran/install-compile-deps.sh protobuf
RUN bash /opt/xran/install-compile-deps.sh protobuf-c
RUN bash /opt/xran/install-compile-deps.sh boost
RUN bash /opt/xran/install-compile-deps.sh abseil
RUN bash /opt/xran/install-compile-deps.sh grpc
# Keep upstream warnings visible with GCC 11/OpenSSL 3 without treating these
# compatibility diagnostics as errors in the supplied cpprestsdk source.
RUN CXXFLAGS="-Wno-error=format-truncation -Wno-error=deprecated-declarations" bash /opt/xran/install-compile-deps.sh cpprestsdk
RUN bash /opt/xran/install-compile-deps.sh etcd
RUN bash /opt/xran/install-compile-deps.sh telemetry
RUN bash /opt/xran/install-compile-deps.sh networking
RUN bash /opt/xran/install-compile-deps.sh asn1c
COPY docker/install-compile-static.sh /opt/xran/install-compile-static.sh
RUN bash /opt/xran/install-compile-static.sh
RUN mkdir -p /tmp/xran-spdlog \
    && tar -xf /opt/xran/sources/spdlog-1.14.1.tar.gz -C /tmp/xran-spdlog --strip-components=1 \
    && cmake -S /tmp/xran-spdlog -B /tmp/xran-spdlog-build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
        -DSPDLOG_BUILD_SHARED=OFF -DSPDLOG_BUILD_EXAMPLE=OFF -DSPDLOG_BUILD_TESTS=OFF \
    && cmake --build /tmp/xran-spdlog-build --parallel ${BUILD_JOBS} --target spdlog \
    && cp /tmp/xran-spdlog-build/libspdlog.a /usr/local/lib/ \
    && rm -rf /tmp/xran-spdlog /tmp/xran-spdlog-build
# Meson's uninstalled DPDK .pc references optional build-only directories;
# consumers use the fully installed SDK while the source/build tree is retained.
ENV PKG_CONFIG_DISABLE_UNINSTALLED=1
COPY .cache/compile-extra-apt/ /tmp/extra-apt/
RUN apt-get install -y --no-install-recommends /tmp/extra-apt/*.deb \
    && dpkg-query -W > /opt/packages.lock && rm -rf /tmp/extra-apt /var/lib/apt/lists/*
COPY .cache/compile-tools/helm /usr/local/bin/helm
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=dockercli /usr/local/libexec/docker/cli-plugins/docker-buildx /usr/local/libexec/docker/cli-plugins/docker-buildx
COPY --chown=1000:1000 .cache/base-images.tar /opt/xran/base-images.tar
COPY runtime/run-build.py /opt/xran/run-build.py
COPY runtime/demo-server.py /opt/xran/demo-server.py
COPY runtime/compile-check/ /opt/xran/compile-check/
COPY runtime/verify-compile.sh /opt/xran/verify-compile.sh
COPY docker/package-protobuf-c.sh /opt/xran/package-protobuf-c.sh
RUN bash /opt/xran/package-protobuf-c.sh
USER 1000:1000
WORKDIR /home/jenkins/agent
CMD ["sleep", "infinity"]
