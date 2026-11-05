ARG DOCKER_CLI_IMAGE=docker:29.9.3-cli
FROM ${DOCKER_CLI_IMAGE} AS dockercli
FROM ubuntu:22.04
ARG APT_SOURCES=config/sources.online.list
ENV DEBIAN_FRONTEND=noninteractive TZ=Asia/Shanghai
COPY ${APT_SOURCES} /etc/apt/sources.list
COPY config/apt-packages.txt /tmp/apt-packages.txt
COPY .cache/apt-archives/ /tmp/apt-cache/
RUN if ls /tmp/apt-cache/*.deb >/dev/null 2>&1; then \
        apt-get install -y --no-install-recommends /tmp/apt-cache/*.deb; \
    fi \
    && if ! xargs -r dpkg-query -W < /tmp/apt-packages.txt >/dev/null 2>&1; then \
        apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=30 update \
        && xargs -r apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=30 install -y --no-install-recommends < /tmp/apt-packages.txt; \
    fi \
    && dpkg-query -W > /opt/packages.lock \
    && rm -rf /var/lib/apt/lists/* /tmp/apt-packages.txt /tmp/apt-cache \
    && groupadd -g 1000 jenkins && useradd -m -u 1000 -g 1000 -s /bin/bash jenkins
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=dockercli /usr/local/libexec/docker/cli-plugins/docker-buildx /usr/local/libexec/docker/cli-plugins/docker-buildx
COPY --chown=1000:1000 .cache/base-images.tar /opt/xran/base-images.tar
COPY runtime/run-build.py /opt/xran/run-build.py
COPY runtime/demo-server.py /opt/xran/demo-server.py
USER 1000:1000
WORKDIR /home/jenkins/agent
CMD ["sleep", "infinity"]
