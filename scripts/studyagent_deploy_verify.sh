#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

API_BASE_URL="${API_BASE_URL:-http://127.0.0.1:8002}"
WEB_BASE_URL="${WEB_BASE_URL:-http://127.0.0.1:8080}"
VERIFY_TARGET="${VERIFY_TARGET:-remote}"
REMOTE_HOST="${REMOTE_HOST:-4080s}"
REMOTE_DIR="${REMOTE_DIR:-/home/binyu/文档/trae_projects/StudyAgent}"
REMOTE_API_BASE_URL="${REMOTE_API_BASE_URL:-http://127.0.0.1:8002}"
REMOTE_WEB_BASE_URL="${REMOTE_WEB_BASE_URL:-http://127.0.0.1:8080}"
RUN_POST_DEPLOY_CHECK="${RUN_POST_DEPLOY_CHECK:-1}"
CHECK_OPENAPI="${CHECK_OPENAPI:-1}"
CHECK_GIT="${CHECK_GIT:-1}"
REQUIRE_CLEAN_WORKTREE="${REQUIRE_CLEAN_WORKTREE:-0}"
REQUIRE_UPSTREAM_PARITY="${REQUIRE_UPSTREAM_PARITY:-0}"
REQUIRE_REMOTE_HEAD_MATCH="${REQUIRE_REMOTE_HEAD_MATCH:-0}"
EXPECTED_OPENAPI_PATHS="${EXPECTED_OPENAPI_PATHS:-}"

pass() {
  printf '[PASS] %s\n' "$1"
}

warn() {
  printf '[WARN] %s\n' "$1" >&2
}

fail() {
  printf '[FAIL] %s\n' "$1" >&2
  exit 1
}

check_git_state() {
  local status upstream counts
  status="$(git status --short)"
  if [[ -z "$status" ]]; then
    pass "git worktree clean"
  elif [[ "$REQUIRE_CLEAN_WORKTREE" == "1" ]]; then
    printf '%s\n' "$status" >&2
    fail "git worktree has uncommitted changes"
  else
    warn "git worktree has uncommitted changes; continuing because REQUIRE_CLEAN_WORKTREE=0"
    printf '%s\n' "$status"
  fi

  if upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null)"; then
    counts="$(git rev-list --left-right --count "HEAD...${upstream}")"
    printf 'upstream=%s\n' "$upstream"
    printf 'HEAD...upstream=%s\n' "$counts"
    if [[ "$counts" == "0	0" || "$counts" == "0 0" ]]; then
      pass "git upstream parity"
    elif [[ "$REQUIRE_UPSTREAM_PARITY" == "1" ]]; then
      fail "git upstream parity check failed"
    else
      warn "git upstream parity is not 0 0; continuing because REQUIRE_UPSTREAM_PARITY=0"
    fi
  else
    warn "no upstream branch configured; skipping upstream parity"
  fi
}

fetch_openapi() {
  curl -fsS "${API_BASE_URL%/}/openapi.json"
}

check_expected_openapi_paths() {
  local openapi_json="$1"
  local path
  [[ -n "$EXPECTED_OPENAPI_PATHS" ]] || return 0

  IFS=',' read -r -a expected_paths <<< "$EXPECTED_OPENAPI_PATHS"
  for path in "${expected_paths[@]}"; do
    path="$(printf '%s' "$path" | sed -E 's/^[[:space:]]+|[[:space:]]+$//g')"
    [[ -n "$path" ]] || continue
    if [[ "$openapi_json" != *"\"${path}\""* ]]; then
      fail "OpenAPI missing expected path: ${path}"
    fi
    pass "OpenAPI path ${path}"
  done
}

check_openapi() {
  local openapi_json path_count
  openapi_json="$(fetch_openapi)"
  path_count="$(printf '%s' "$openapi_json" | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("paths", {})))')"
  [[ "$path_count" =~ ^[0-9]+$ ]] || fail "OpenAPI path count is not numeric"
  [[ "$path_count" -gt 0 ]] || fail "OpenAPI exposes zero paths"
  printf 'openapi_path_count=%s\n' "$path_count"
  check_expected_openapi_paths "$openapi_json"
  pass "OpenAPI route inventory"
}

check_local() {
  printf '\n== Local StudyAgent verification ==\n'
  printf 'API_BASE_URL=%s\n' "$API_BASE_URL"
  printf 'WEB_BASE_URL=%s\n' "$WEB_BASE_URL"

  if [[ "$CHECK_GIT" == "1" ]]; then
    check_git_state
  else
    printf '[SKIP] git checks disabled\n'
  fi

  if [[ "$CHECK_OPENAPI" == "1" ]]; then
    check_openapi
  else
    printf '[SKIP] OpenAPI checks disabled\n'
  fi

  if [[ "$RUN_POST_DEPLOY_CHECK" == "1" ]]; then
    API_BASE_URL="$API_BASE_URL" WEB_BASE_URL="$WEB_BASE_URL" bash scripts/post_deploy_check.sh
  else
    printf '[SKIP] post-deploy runtime checks disabled\n'
  fi

  pass "local StudyAgent verification"
}

check_remote() {
  local local_head expected_openapi_paths_arg
  local_head="$(git rev-parse HEAD)"
  expected_openapi_paths_arg="${EXPECTED_OPENAPI_PATHS:-__EMPTY__}"

  printf '\n== Remote StudyAgent verification ==\n'
  printf 'REMOTE_HOST=%s\n' "$REMOTE_HOST"
  printf 'REMOTE_DIR=%s\n' "$REMOTE_DIR"
  printf 'REMOTE_API_BASE_URL=%s\n' "$REMOTE_API_BASE_URL"
  printf 'REMOTE_WEB_BASE_URL=%s\n' "$REMOTE_WEB_BASE_URL"

  ssh -o BatchMode=yes "$REMOTE_HOST" bash -s -- \
    "$REMOTE_DIR" \
    "$local_head" \
    "$REMOTE_API_BASE_URL" \
    "$REMOTE_WEB_BASE_URL" \
    "$expected_openapi_paths_arg" \
    "$REQUIRE_REMOTE_HEAD_MATCH" <<'REMOTE_STUDYAGENT_VERIFY'
set -euo pipefail

remote_dir="$1"
local_head="$2"
api_base_url="$3"
web_base_url="$4"
expected_openapi_paths="$5"
require_remote_head_match="$6"
if [[ "$expected_openapi_paths" == "__EMPTY__" ]]; then
  expected_openapi_paths=""
fi

pass() { printf '[PASS] %s\n' "$1"; }
warn() { printf '[WARN] %s\n' "$1" >&2; }
fail() { printf '[FAIL] %s\n' "$1" >&2; exit 1; }

cd "$remote_dir"

remote_head="$(git rev-parse HEAD)"
origin_head="$(git rev-parse origin/main 2>/dev/null || true)"
github_head="$(git ls-remote origin refs/heads/main 2>/dev/null | awk '{print $1}' || true)"

printf 'remote_head=%s\n' "$remote_head"
printf 'local_head=%s\n' "$local_head"
printf 'origin_main=%s\n' "${origin_head:-unknown}"
printf 'github_main=%s\n' "${github_head:-unknown}"

if [[ "$remote_head" == "$local_head" ]]; then
  pass "remote checkout matches local HEAD"
elif [[ "$require_remote_head_match" == "1" ]]; then
  fail "remote checkout does not match local HEAD"
else
  warn "remote checkout does not match local HEAD; continuing because REQUIRE_REMOTE_HEAD_MATCH=0"
fi

if [[ -n "$origin_head" && -n "$github_head" && "$origin_head" == "$github_head" ]]; then
  pass "remote origin/main matches GitHub main"
else
  warn "could not prove origin/main and GitHub main match"
fi

python3 - "$api_base_url" "$web_base_url" "$expected_openapi_paths" <<'PY'
import json
import sys
from urllib.parse import urljoin, urlparse
from urllib.request import urlopen

api_base, web_base, expected = sys.argv[1:4]

def fail(message):
    print(f"[FAIL] {message}", file=sys.stderr)
    raise SystemExit(1)

def passed(message):
    print(f"[PASS] {message}")

def get(url):
    with urlopen(url, timeout=8) as response:
        return response.status, response.headers.get("Content-Type", ""), response.read()

status, content_type, body = get(api_base.rstrip("/") + "/health")
if status != 200:
    fail(f"API /health status={status}")
try:
    health = json.loads(body)
except json.JSONDecodeError as exc:
    fail(f"API /health did not return JSON: {exc}")
if health.get("status") not in {"ok", "healthy"}:
    fail(f"API /health status field unexpected: {health.get('status')!r}")
passed("remote API /health")

status, content_type, body = get(api_base.rstrip("/") + "/openapi.json")
if status != 200:
    fail(f"OpenAPI status={status}")
openapi = json.loads(body)
paths = openapi.get("paths", {})
print(f"openapi_path_count={len(paths)}")
if not paths:
    fail("OpenAPI exposes zero paths")
for path in [item.strip() for item in expected.split(",") if item.strip()]:
    if path not in paths:
        fail(f"OpenAPI missing expected path: {path}")
    passed(f"OpenAPI path {path}")
passed("remote OpenAPI route inventory")

status, content_type, html = get(web_base.rstrip("/") + "/login")
if status != 200:
    fail(f"frontend /login status={status}")
text = html.decode("utf-8", errors="replace")
if "/assets/" not in text:
    fail("frontend HTML does not reference Vite bundle assets")
passed("remote frontend /login")
PY

running_services="$(docker compose ps --services --filter status=running)"
for service in backend worker nginx; do
  if printf '%s\n' "$running_services" | grep -qx "$service"; then
    pass "docker compose service running: $service"
  else
    fail "docker compose service not running: $service"
  fi
done
REMOTE_STUDYAGENT_VERIFY

  pass "remote StudyAgent verification"
}

main() {
  printf 'StudyAgent deploy verification\n'
  printf 'VERIFY_TARGET=%s\n' "$VERIFY_TARGET"

  case "$VERIFY_TARGET" in
    local)
      check_local
      ;;
    remote)
      check_remote
      ;;
    all)
      check_local
      check_remote
      ;;
    *)
      fail "VERIFY_TARGET must be one of: local, remote, all"
      ;;
  esac

  printf 'StudyAgent deploy verification passed.\n'
}

main "$@"
