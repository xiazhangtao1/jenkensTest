ARG STORAGE_HELPER_IMAGE=busybox:1.37.0
FROM ${STORAGE_HELPER_IMAGE}
COPY --chown=1000:1000 .cache/open5gs-base-images.tar /opt/open5gs-base-images.tar
USER 1000:1000
CMD ["cp", "/opt/open5gs-base-images.tar", "/base/images.tar"]
