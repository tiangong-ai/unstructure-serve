#!/usr/bin/env bash
set -euo pipefail
repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_root"
mkdir -p output/logs
config="$repo_root/deploy/pm2/ecosystem.config.cjs"
action=${1:-status}
group=${2:-app}
case "$group" in
  api) names=unstructured-gunicorn ;;
  workers) names=celery-two-stage-parse,celery-two-stage-parse-2,celery-two-stage-parse-3,celery-two-stage-vision,celery-two-stage-dispatch,celery-two-stage-merge ;;
  ordinary) names=celery-worker ;;
  model) names=mineru-vlm-docker-parallel ;;
  app) names=unstructured-gunicorn,celery-two-stage-parse,celery-two-stage-parse-2,celery-two-stage-parse-3,celery-two-stage-vision,celery-two-stage-dispatch,celery-two-stage-merge ;;
  *) echo "Unknown group: $group" >&2; exit 2 ;;
esac
case "$action" in
  start) pm2 start "$config" --only "$names" ;;
  restart) pm2 restart "$config" --only "$names" --update-env ;;
  stop) IFS=, read -ra targets <<< "$names"; for target in "${targets[@]}"; do pm2 stop "$target"; done ;;
  status) pm2 status ;;
  logs) pm2 logs "${names%%,*}" --lines 100 ;;
  *) echo "Usage: $0 {start|restart|stop|status|logs} {app|api|workers|ordinary|model}" >&2; exit 2 ;;
esac
