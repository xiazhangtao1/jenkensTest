#!/bin/sh
set -eu
# Keep nested containers inside the Kubernetes container's resource budget.
# A hybrid host also has a 0:: entry; detect v2 from the mounted hierarchy.
if [ -f /sys/fs/cgroup/cgroup.controllers ]; then
    parent=$(awk -F: '$1 == "0" && $2 == "" { print $3; exit }' /proc/self/cgroup)
else
    parent=$(awk -F: '$2 ~ /(^|,)memory(,|$)/ { print $3; exit }' /proc/self/cgroup)
fi
test -n "$parent" || { echo 'Cannot determine DinD parent cgroup' >&2; exit 1; }
case "$parent" in
    /*) ;;
    *) echo 'Invalid DinD parent cgroup' >&2; exit 1 ;;
esac
case "/${parent#/}/" in
    */../*|*/./*) echo 'Invalid DinD parent cgroup' >&2; exit 1 ;;
esac

if [ -f /sys/fs/cgroup/cgroup.controllers ]; then
    directory="/sys/fs/cgroup${parent%/}"
    # Domain controllers cannot be delegated from a group containing processes.
    # The official DinD wrapper prepares only its mount root, which is not the
    # container's cgroup when Kubernetes exposes the host cgroup namespace.
    IFS= read -r controllers < "$directory/cgroup.controllers"
    for required in cpu memory; do
        case " $controllers " in
            *" $required "*) ;;
            *) echo "DinD cgroup v2 controller is not delegated: $required" >&2; exit 1 ;;
        esac
    done
    mkdir -p "$directory/xran-daemon"
    enabled=''
    for controller in $controllers; do enabled="$enabled +$controller"; done
    ready=false
    for attempt in 1 2 3 4 5; do
        # A concurrent exec or an exiting process can race with this move.
        while IFS= read -r pid; do
            printf '%s\n' "$pid" > "$directory/xran-daemon/cgroup.procs" 2>/dev/null || :
        done < "$directory/cgroup.procs"
        if printf '%s\n' "$enabled" > "$directory/cgroup.subtree_control" 2>/dev/null; then
            ready=true
            break
        fi
        sleep 0.1
    done
    "$ready" || { echo "Cannot delegate DinD cgroup v2 controllers: $directory" >&2; exit 1; }
    parent="${parent%/}/xran-builds"
    # There is no systemd daemon inside the DinD container.
    set -- --exec-opt=native.cgroupdriver=cgroupfs "$@"
fi
echo "DinD parent cgroup: $parent"
exec dockerd-entrypoint.sh dockerd --host=unix:///var/run/xran/docker.sock \
    --group=1000 --storage-driver=overlay2 --cgroup-parent="$parent" "$@"
