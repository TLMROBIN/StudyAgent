#!/usr/bin/env bash

set -euo pipefail

if [[ -n "${STUDYAGENT_PROJECT_DIR:-}" ]]; then
  PROJECT_DIR="${STUDYAGENT_PROJECT_DIR}"
elif [[ -f docker-compose.yml ]]; then
  PROJECT_DIR="$(pwd)"
else
  PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi
API_BASE_URL="${API_BASE_URL:-http://127.0.0.1:8002}"
GRAFANA_URL="${GRAFANA_URL:-http://127.0.0.1:${GRAFANA_PORT:-3001}}"
LOCK_FILE="${STUDYAGENT_GPU_WATCHDOG_LOCK:-/tmp/studyagent-gpu-watchdog.lock}"
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: scripts/gpu_runtime_watchdog.sh [--dry-run]

Checks whether the StudyAgent GPU-dependent containers still have a healthy
NVIDIA runtime. If backend/worker are unhealthy or /health reports MinerU CUDA
runtime unavailable while the host GPU is healthy, the script recreates only
backend and worker so Docker re-injects NVIDIA devices.

Environment:
  STUDYAGENT_PROJECT_DIR          Project directory. Defaults to repo root.
  API_BASE_URL                    Backend API base URL. Defaults to 127.0.0.1:8002.
  GRAFANA_URL                     Grafana URL used by post-deploy checks.
  STUDYAGENT_GPU_WATCHDOG_LOCK    Lock file path.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown option: %s\n\n' "$1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

log() {
  printf '[%(%Y-%m-%d %H:%M:%S %z)T] %s\n' -1 "$*"
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    log "missing required command: $1"
    exit 1
  fi
}

compose_args() {
  printf '%s\n' -f docker-compose.yml
  if [[ -f docker-compose.override.yml ]]; then
    printf '%s\n' -f docker-compose.override.yml
  fi
}

host_gpu_ready() {
  nvidia-smi >/dev/null 2>&1
}

service_health() {
  local service="$1"
  local container_id
  container_id="$(docker compose "${COMPOSE_FILES[@]}" ps -q "$service")"
  if [[ -z "$container_id" ]]; then
    printf 'missing'
    return
  fi
  docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}no-health{{end}}' "$container_id"
}

api_gpu_runtime_ready() {
  local body
  if ! body="$(curl -fsS "${API_BASE_URL}/health" 2>/dev/null)"; then
    return 1
  fi
  python3 -c '
import json
import sys

data = json.load(sys.stdin)
parser = data.get("rag", {}).get("pdf_parser", {})
ok = data.get("status") == "ok" and ((not parser.get("enabled")) or bool(parser.get("runtime_ready")))
sys.exit(0 if ok else 1)
' <<<"$body"
}

recreate_gpu_services() {
  local command=(docker compose "${COMPOSE_FILES[@]}" up -d --force-recreate backend worker)
  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "dry-run: ${command[*]}"
    return
  fi
  log "recreating backend and worker to refresh NVIDIA runtime"
  "${command[@]}"
}

post_check() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "dry-run: GRAFANA_URL=${GRAFANA_URL} bash scripts/post_deploy_check.sh"
    return
  fi
  GRAFANA_URL="${GRAFANA_URL}" bash scripts/post_deploy_check.sh
}

main() {
  require_cmd docker
  require_cmd nvidia-smi
  require_cmd curl
  require_cmd python3

  exec 9>"${LOCK_FILE}"
  if ! flock -n 9; then
    log "another StudyAgent GPU watchdog task is already running"
    exit 0
  fi

  cd "$PROJECT_DIR"
  mapfile -t COMPOSE_FILES < <(compose_args)

  if ! host_gpu_ready; then
    log "host nvidia-smi is not ready; skip container recreate"
    exit 2
  fi

  local backend_health worker_health api_ready=0
  backend_health="$(service_health backend)"
  worker_health="$(service_health worker)"
  if api_gpu_runtime_ready; then
    api_ready=1
  fi

  log "backend=${backend_health} worker=${worker_health} api_gpu_runtime_ready=${api_ready}"

  if [[ "$backend_health" == "running healthy" && "$worker_health" == "running healthy" && "$api_ready" -eq 1 ]]; then
    log "GPU runtime is healthy; no action"
    exit 0
  fi

  recreate_gpu_services
  post_check
}

main "$@"
