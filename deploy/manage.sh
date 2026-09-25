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
  model4) names=mineru-vlm-docker-parallel4 ;;
  workers4) names=celery-two-stage-parse,celery-two-stage-parse-2,celery-two-stage-parse-3,celery-two-stage-parse-4,celery-two-stage-vision,celery-two-stage-dispatch,celery-two-stage-merge ;;
  app4) names=unstructured-gunicorn,celery-two-stage-parse,celery-two-stage-parse-2,celery-two-stage-parse-3,celery-two-stage-parse-4,celery-two-stage-vision,celery-two-stage-dispatch,celery-two-stage-merge,vision-health-monitor ;;
  vision-health) names=vision-health-monitor ;;
  app) names=unstructured-gunicorn,celery-two-stage-parse,celery-two-stage-parse-2,celery-two-stage-parse-3,celery-two-stage-vision,celery-two-stage-dispatch,celery-two-stage-merge,vision-health-monitor ;;
  *) echo "Unknown group: $group" >&2; exit 2 ;;
esac
case "$action" in
  start)
    pending=$(pm2 jlist | node -e '
      let data=""; process.stdin.on("data", chunk => data += chunk);
      process.stdin.on("end", () => {
        const live = new Map(JSON.parse(data).map(p => [p.name, p.pm2_env.status]));
        console.log(process.argv[1].split(",").filter(name =>
          !["online", "launching"].includes(live.get(name))).join(","));
      });' "$names")
    if [[ -n "$pending" ]]; then
      pm2 start "$config" --only "$pending"
    else
      echo "Selected project processes are already running."
    fi
    ;;
  restart) pm2 restart "$config" --only "$names" --update-env ;;
  stop) IFS=, read -ra targets <<< "$names"; for target in "${targets[@]}"; do pm2 stop "$target"; done ;;
  status) pm2 status ;;
  logs) pm2 logs "${names%%,*}" --lines 100 ;;
  *) echo "Usage: $0 {start|restart|stop|status|logs} {app|app4|api|workers|workers4|ordinary|model|model4|vision-health}" >&2; exit 2 ;;
esac
