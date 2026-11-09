#!/usr/bin/env bash
# Short, real Docker/Helm workload for the isolated reliability Git branch only.
set -euo pipefail
offline=false
if [[ ${1:-} == --offline ]]; then offline=true; shift; fi
[[ $# == 2 ]] || exit 2
chart=$1
app=$2
root=$(git rev-parse --show-toplevel)
revision=$(git rev-parse HEAD)
sleep "${XRAN_BUILD_DELAY_SECONDS:-0}"
context=$(mktemp -d)
trap 'rm -rf "$context"' EXIT
printf '{"commit":"%s","offline":%s}\n' "$revision" "$offline" > "$context/release.json"
printf 'FROM 192.168.10.35/xnet/ubuntu:22.04\nCOPY release.json /release.json\nCMD ["cat", "/release.json"]\n' > "$context/Dockerfile"
for name in runtime webui monitor; do
    image="$XRAN_REGISTRY/xcn/xcn-$name:$app"
    docker build --pull=false --network none -t "$image" "$context"
    docker save -o "$root/helm/xcn-$name.tar" "$image"
    docker push "$image"
    printf '%s: build OK, push OK\n' "$image"
done
python3 - "$root/helm/xcn" "$app" <<'PY'
import sys
from pathlib import Path
import yaml
directory=Path(sys.argv[1])
(directory/'values.yaml').write_text(yaml.safe_dump({'images':{
    'runtime':{'tag':sys.argv[2]},'webui':{'tag':sys.argv[2]}},
    'monitor':{'image':{'tag':sys.argv[2]}}}))
PY
helm package "$root/helm/xcn" --version "$chart" --app-version "$app" --destination "$root/helm"
