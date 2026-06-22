#!/usr/bin/env bash

set -euo pipefail

API_BASE_URL="${API_BASE_URL:-http://127.0.0.1:8002}"
WEB_BASE_URL="${WEB_BASE_URL:-http://127.0.0.1:8080}"
PROMETHEUS_URL="${PROMETHEUS_URL:-http://127.0.0.1:9090}"
GRAFANA_URL="${GRAFANA_URL:-http://127.0.0.1:3000}"
ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-StudyAgent123}"

ACCESS_TOKEN=""

pass() {
  printf '[PASS] %s\n' "$1"
}

fail() {
  printf '[FAIL] %s\n' "$1" >&2
  exit 1
}

require_contains() {
  local haystack="$1"
  local needle="$2"
  local label="$3"
  if [[ "$haystack" != *"$needle"* ]]; then
    fail "$label"
  fi
}

http_body() {
  curl -fsS "$1"
}

http_status() {
  curl -s -o /dev/null -w '%{http_code}' "$1"
}

http_content_type() {
  curl -fsSI "$1" | awk 'BEGIN{IGNORECASE=1} /^Content-Type:/ {print $2; exit}' | tr -d '\r'
}

absolute_asset_url() {
  local asset_path="$1"
  local web_origin
  web_origin="$(printf '%s\n' "$WEB_BASE_URL" | sed -E 's#^(https?://[^/]+).*$#\1#')"
  if [[ "$asset_path" == http://* || "$asset_path" == https://* ]]; then
    printf '%s\n' "$asset_path"
  elif [[ "$asset_path" == /* ]]; then
    printf '%s%s\n' "$web_origin" "$asset_path"
  else
    printf '%s/%s\n' "${WEB_BASE_URL%/}" "$asset_path"
  fi
}

extract_frontend_assets() {
  local html="$1"
  printf '%s\n' "$html" \
    | grep -Eo '(src|href)="[^"]+\.(js|css)(\?[^"]*)?"' \
    | sed -E 's/^(src|href)="([^"]+)".*$/\2/' \
    | sort -u
}

check_frontend_bundle_assets() {
  local html asset_url content_type
  html="$(http_body "${WEB_BASE_URL}/login")"
  require_contains "$html" "/assets/" "前端 HTML 未引用 Vite bundle，可能不是发布构建产物"

  local assets
  assets="$(extract_frontend_assets "$html")"
  [[ -n "$assets" ]] || fail "前端 HTML 未解析到 JS/CSS bundle 资源"

  while IFS= read -r asset; do
    [[ -n "$asset" ]] || continue
    asset_url="$(absolute_asset_url "$asset")"
    content_type="$(http_content_type "$asset_url")"
    case "$asset" in
      *.js|*.js\?*)
        [[ "$content_type" == application/javascript* || "$content_type" == text/javascript* ]] \
          || fail "前端 JS bundle Content-Type 异常：${asset_url} -> ${content_type:-empty}"
        ;;
      *.css|*.css\?*)
        [[ "$content_type" == text/css* ]] \
          || fail "前端 CSS bundle Content-Type 异常：${asset_url} -> ${content_type:-empty}"
        ;;
    esac
  done <<< "$assets"
  pass "前端 bundle assets"
}

check_api_health() {
  local body
  body="$(http_body "${API_BASE_URL}/health")"
  require_contains "$body" '"status":"ok"' "API /health 未返回 status=ok"
  pass "API /health"
}

check_metrics() {
  local body
  body="$(http_body "${API_BASE_URL}/metrics")"
  require_contains "$body" 'http_request_total' "API /metrics 未暴露 http_request_total"
  pass "API /metrics"
}

check_admin_login() {
  local payload response
  payload=$(printf '{"username":"%s","password":"%s"}' "$ADMIN_USERNAME" "$ADMIN_PASSWORD")
  response="$(curl -fsS -X POST "${API_BASE_URL}/api/auth/staff/login" -H 'Content-Type: application/json' -d "$payload")"
  require_contains "$response" '"access_token":"' "管理员登录未返回 access_token"
  ACCESS_TOKEN="$(printf '%s' "$response" | sed -n 's/.*"access_token":"\([^"]*\)".*/\1/p')"
  [[ -n "$ACCESS_TOKEN" ]] || fail "管理员登录后未能解析 access_token"
  pass "管理员登录"
}

check_auth_me() {
  local body
  body="$(curl -fsS "${API_BASE_URL}/api/auth/me" -H "Authorization: Bearer ${ACCESS_TOKEN}")"
  require_contains "$body" "\"username\":\"${ADMIN_USERNAME}\"" "/api/auth/me 未返回当前管理员"
  pass "API /api/auth/me"
}

check_web_login() {
  local status
  status="$(http_status "${WEB_BASE_URL}/login")"
  [[ "$status" == "200" ]] || fail "前端登录页不可访问，状态码=${status}"
  pass "前端 /login"
}

check_prometheus() {
  local status
  status="$(http_status "${PROMETHEUS_URL}/-/ready")"
  [[ "$status" == "200" ]] || fail "Prometheus 未 ready，状态码=${status}"
  pass "Prometheus ready"
}

check_grafana() {
  local status
  status="$(http_status "${GRAFANA_URL}/login")"
  [[ "$status" == "200" ]] || fail "Grafana 登录页不可访问，状态码=${status}"
  pass "Grafana /login"
}

main() {
  printf 'StudyAgent post-deploy checks\n'
  printf 'API_BASE_URL=%s\n' "$API_BASE_URL"
  printf 'WEB_BASE_URL=%s\n' "$WEB_BASE_URL"

  check_api_health
  check_metrics
  check_admin_login
  check_auth_me
  check_web_login
  check_frontend_bundle_assets

  if curl -fsS "${PROMETHEUS_URL}/-/ready" >/dev/null 2>&1; then
    check_prometheus
  else
    printf '[SKIP] Prometheus 未启用或当前不可达\n'
  fi

  if curl -fsS "${GRAFANA_URL}/login" >/dev/null 2>&1; then
    check_grafana
  else
    printf '[SKIP] Grafana 未启用或当前不可达\n'
  fi

  printf 'All checks passed.\n'
}

main "$@"
