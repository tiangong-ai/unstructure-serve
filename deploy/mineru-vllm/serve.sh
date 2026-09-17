#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
mode=${1:-parallel}
case "$mode" in
  parallel|single) ;;
  *) echo "Usage: $0 [parallel|single]" >&2; exit 2 ;;
esac

cd "$repo_root"
compose=(docker compose --project-name "mineru-vlm-$mode" --file "$repo_root/compose.mineru.yaml")
if [[ "$mode" == parallel ]]; then
  compose+=(--file "$repo_root/compose.mineru.parallel.yaml")
fi

# Stay attached so PM2 stop/restart also stops/restarts the container.
exec "${compose[@]}" up --build --abort-on-container-exit --exit-code-from mineru-vlm mineru-vlm
