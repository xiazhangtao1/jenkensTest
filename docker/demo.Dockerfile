ARG BUILDER_IMAGE=xran-ci/builder:jammy-1
FROM ${BUILDER_IMAGE}
COPY .cache/demo.git/ /opt/demo/X-RAN-CLOUD.git/
CMD ["python3", "/opt/xran/demo-server.py"]
