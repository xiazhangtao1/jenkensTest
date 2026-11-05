#!/bin/sh
set -eu
# On cgroup v1 a privileged nested daemon otherwise creates siblings of its
# Kubernetes container, escaping the Pod's memory/CPU limits.
parent=$(awk -F: '$2 == "memory" || $1 == "0" { print $3; exit }' /proc/self/cgroup)
test -n "$parent" || { echo 'Cannot determine DinD parent cgroup' >&2; exit 1; }
echo "DinD parent cgroup: $parent"
exec dockerd-entrypoint.sh dockerd --host=unix:///var/run/xran/docker.sock \
    --group=1000 --storage-driver=overlay2 --cgroup-parent="$parent" "$@"
