ARG BUILDER_IMAGE=192.168.10.35/xnet/xran-compile:latest
FROM ${BUILDER_IMAGE}
COPY .cache/demo.git/ /opt/demo/X-RAN-CLOUD.git/
COPY .cache/open5gs.git/ /opt/demo/open5gs.git/
CMD ["python3", "/opt/xran/demo-server.py"]
