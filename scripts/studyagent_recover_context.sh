#!/usr/bin/env bash

set -u

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE_HOST="${REMOTE_HOST:-4080s}"
REMOTE_DIR="${REMOTE_DIR:-/home/binyu/文档/trae_projects/StudyAgent}"

section() {
  printf '\n== %s ==\n' "$1"
}

run() {
  printf '+ %s\n' "$*"
  "$@"
  local status=$?
  if [[ "$status" -ne 0 ]]; then
    printf '[WARN] command exited with %s: %s\n' "$status" "$*" >&2
  fi
  return 0
}

section "StudyAgent local context"
cd "$ROOT_DIR" || exit 1
printf 'local_dir=%s\n' "$ROOT_DIR"
run git status --short --branch
run git worktree list
run git log --oneline --decorate -n 8
run git remote -v
run git rev-parse HEAD
run git ls-remote origin refs/heads/main

section "StudyAgent local changed files"
run git diff --name-status
run git diff --cached --name-status

section "StudyAgent remote 4080s context"
ssh -o BatchMode=yes "$REMOTE_HOST" bash -s -- "$REMOTE_DIR" <<'REMOTE_RECOVER'
set -u
remote_dir="$1"
section() { printf '\n-- %s --\n' "$1"; }
run() {
  printf '+ %s\n' "$*"
  "$@"
  local status=$?
  if [[ "$status" -ne 0 ]]; then
    printf '[WARN] command exited with %s: %s\n' "$status" "$*" >&2
  fi
  return 0
}

cd "$remote_dir" || exit 1
section "remote git"
printf 'remote_dir=%s\n' "$remote_dir"
run git status --short --branch
run git rev-parse HEAD
run git rev-parse origin/main
run git ls-remote origin refs/heads/main

section "remote docker"
run docker compose ps

section "remote HTTP probes"
python3 - <<'PY'
import json
from urllib.request import urlopen

for url in [
    "http://127.0.0.1:8002/health",
    "http://127.0.0.1:8002/openapi.json",
    "http://127.0.0.1:8080/login",
]:
    try:
        with urlopen(url, timeout=8) as response:
            body = response.read(200000)
            print(url, response.status, response.headers.get("Content-Type"), len(body))
            if url.endswith("/openapi.json"):
                print("openapi_path_count", len(json.loads(body).get("paths", {})))
    except Exception as exc:
        print(url, type(exc).__name__, exc)
PY
REMOTE_RECOVER
