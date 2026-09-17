#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
mode=${1:-parallel}
case "$mode" in
  parallel|single) ;;
  *) echo "Usage: $0 [parallel|single]" >&2; exit 2 ;;
esac

cd "$repo_root"
# Device nodes can appear after Docker/PM2 during boot. Never load host modules
# or restart the shared Docker daemon from this application launcher.
for ((attempt=0; attempt<60; attempt++)); do
  if [[ -c /dev/nvidia-uvm && -c /dev/nvidia-uvm-tools ]]; then
    break
  fi
  if ((attempt == 59)); then
    echo "CUDA UVM devices missing; check host NVIDIA driver initialization." >&2
    exit 1
  fi
  sleep 2
done
compose=(docker compose --project-name "mineru-vlm-$mode" --file "$repo_root/compose.mineru.yaml")
if [[ "$mode" == parallel ]]; then
  compose+=(--file "$repo_root/compose.mineru.parallel.yaml")
fi

# Stay attached so PM2 stop/restart also stops/restarts the container.
exec "${compose[@]}" up --build --abort-on-container-exit --exit-code-from mineru-vlm mineru-vlm
