#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${ROOT_DIR}"

BUILD=0
SKIP_CHECKS=0
FRONTEND_BUNDLE_STAMP="${FRONTEND_BUNDLE_STAMP:-${ROOT_DIR}/data/.frontend-bundle.sha256}"

usage() {
  cat <<'EOF'
Usage: ./start-studyagent.sh [--build] [--skip-checks]

Options:
  --build        Rebuild local images before starting the stack.
  --skip-checks  Skip post-deploy HTTP checks.
  -h, --help     Show this help.

Environment:
  GRAFANA_PORT   Host port for Grafana. Defaults to 3001 in this script to
                 avoid the existing 3000 port conflict on this machine.

Notes:
  If frontend/ or nginx/ changed since the last local publish, this script
  republishes the frontend bundle through scripts/publish_frontend_bundle.sh
  after the stack is up. A plain docker compose up does not refresh the
  nginx-baked Vue bundle.
EOF
}

frontend_bundle_fingerprint() {
  if ! command -v git >/dev/null 2>&1; then
    return 1
  fi
  if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    return 1
  fi

  {
    git ls-files frontend nginx | LC_ALL=C sort | while IFS= read -r path; do
      printf '%s\n' "$path"
      git hash-object "$path"
    done
    git status --porcelain -- frontend nginx
  } | shasum -a 256 | awk '{print $1}'
}

frontend_changes_detected() {
  local current_fingerprint previous_fingerprint

  current_fingerprint="$(frontend_bundle_fingerprint)" || return 1
  if [[ ! -f "${FRONTEND_BUNDLE_STAMP}" ]]; then
    return 0
  fi

  previous_fingerprint="$(< "${FRONTEND_BUNDLE_STAMP}")"
  [[ "${current_fingerprint}" != "${previous_fingerprint}" ]]
}

write_frontend_bundle_stamp() {
  local current_fingerprint

  current_fingerprint="$(frontend_bundle_fingerprint)" || return 0
  mkdir -p "$(dirname "${FRONTEND_BUNDLE_STAMP}")"
  printf '%s\n' "${current_fingerprint}" > "${FRONTEND_BUNDLE_STAMP}"
}

git_frontend_state_available() {
  if ! command -v git >/dev/null 2>&1; then
    return 1
  fi
  if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    return 1
  fi
  return 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --build)
      BUILD=1
      shift
      ;;
    --skip-checks)
      SKIP_CHECKS=1
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

if ! command -v docker >/dev/null 2>&1; then
  printf 'docker is required but was not found in PATH.\n' >&2
  exit 1
fi

if [[ ! -f .env ]]; then
  if [[ -f .env.example ]]; then
    cp .env.example .env
    printf 'Created .env from .env.example\n'
  else
    printf '.env is missing and .env.example was not found.\n' >&2
    exit 1
  fi
fi

# This host already has port 3000 occupied, so keep Grafana off that port
# unless the caller explicitly overrides it.
export GRAFANA_PORT="${GRAFANA_PORT:-3001}"

if [[ "${BUILD}" -eq 1 ]]; then
  docker compose up -d --build
else
  docker compose up -d
fi

if frontend_changes_detected; then
  printf '\nFrontend/nginx files changed since the last local publish; publishing the nginx-baked frontend bundle.\n'
  RUN_POST_DEPLOY_CHECK=0 bash scripts/publish_frontend_bundle.sh
  write_frontend_bundle_stamp
elif ! git_frontend_state_available; then
  printf '\n[WARN] Git state unavailable; cannot tell whether the frontend bundle is stale.\n' >&2
fi

docker compose ps

if [[ "${SKIP_CHECKS}" -eq 0 ]]; then
  GRAFANA_URL="http://127.0.0.1:${GRAFANA_PORT}" bash scripts/post_deploy_check.sh
fi

printf '\nStudyAgent is starting with these entry points:\n'
printf '  Web:        http://127.0.0.1:8080/login\n'
printf '  API:        http://127.0.0.1:8002/health\n'
printf '  Prometheus: http://127.0.0.1:9090\n'
printf '  Grafana:    http://127.0.0.1:%s\n' "${GRAFANA_PORT}"
