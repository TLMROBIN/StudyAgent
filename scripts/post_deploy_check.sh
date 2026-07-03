#!/usr/bin/env bash
#
# StudyAgent 部署验收脚本
#
# 功能：
#   1. 等待 docker compose 各容器就绪/healthy（带超时轮询）
#   2. API 冒烟：GET /health、GET /metrics、登录接口可达性（区分"连接拒绝"与"服务活着但拒绝请求"）
#   3. 验证 alembic 已升级到 head（docker compose exec backend alembic current 对比 heads）
#   4. Postgres / Redis / ChromaDB 连通性
#   5. 前端 bundle 与监控端点（Prometheus/Grafana，不可达时记 skip）
#   6. 汇总 JSON 报告输出到 stdout 与 $REPORT_PATH（默认 /tmp/deploy_check_report.json）
#
# 约定：
#   - 单项检查失败不会中断脚本，全部跑完后按整体结果设置退出码（0=全过，1=有失败）
#   - 不内置任何密码；需要凭据的检查（管理员登录）从环境变量 ADMIN_USERNAME/ADMIN_PASSWORD
#     读取，未设置则记 skip
#   - SKIP_DOCKER_CHECKS=1 可跳过所有依赖 docker 的检查（如在远端机器上只做 HTTP 冒烟）
#   - --selftest 仅演练记录/JSON 汇总逻辑（CI/本地无 docker 环境时校验脚本本身）

set -euo pipefail

API_BASE_URL="${API_BASE_URL:-http://127.0.0.1:8002}"
WEB_BASE_URL="${WEB_BASE_URL:-http://127.0.0.1:8080}"
PROMETHEUS_URL="${PROMETHEUS_URL:-http://127.0.0.1:9090}"
GRAFANA_URL="${GRAFANA_URL:-http://127.0.0.1:3000}"
ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
# 安全约定：不在脚本里写死密码。未提供 ADMIN_PASSWORD 时跳过登录成功性检查，
# 只做"登录接口可达性"检查（4xx 也算服务活着）。
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"
COMPOSE="${COMPOSE:-docker compose}"
WAIT_TIMEOUT_SECONDS="${WAIT_TIMEOUT_SECONDS:-180}"
WAIT_INTERVAL_SECONDS="${WAIT_INTERVAL_SECONDS:-5}"
CURL_MAX_TIME="${CURL_MAX_TIME:-15}"
REPORT_PATH="${REPORT_PATH:-/tmp/deploy_check_report.json}"
SKIP_DOCKER_CHECKS="${SKIP_DOCKER_CHECKS:-0}"
ALEMBIC_INI_IN_CONTAINER="${ALEMBIC_INI_IN_CONTAINER:-backend/alembic.ini}"

ACCESS_TOKEN=""
OVERALL_STATUS="pass"
CHECK_NAMES=()
CHECK_STATUSES=()
CHECK_DETAILS=()

# ---------------------------------------------------------------------------
# 记录与报告
# ---------------------------------------------------------------------------

record() { # record <name> <pass|fail|skip> <detail>
  local name="$1" status="$2" detail="${3:-}"
  CHECK_NAMES+=("$name")
  CHECK_STATUSES+=("$status")
  CHECK_DETAILS+=("$detail")
  case "$status" in
    pass) printf '[PASS] %s\n' "$name" ;;
    skip) printf '[SKIP] %s%s\n' "$name" "${detail:+ — $detail}" ;;
    fail)
      printf '[FAIL] %s%s\n' "$name" "${detail:+ — $detail}" >&2
      OVERALL_STATUS="fail"
      ;;
  esac
}

# run_check <name> <function...>：捕获检查函数输出作为 detail，失败不中断脚本
run_check() {
  local name="$1"
  shift
  local detail rc
  set +e
  detail="$("$@" 2>&1)"
  rc=$?
  set -e
  if [[ $rc -eq 0 ]]; then
    record "$name" "pass" "$detail"
  else
    record "$name" "fail" "$detail"
  fi
}

json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  s="${s//$'\n'/\\n}"
  s="${s//$'\r'/}"
  s="${s//$'\t'/\\t}"
  printf '%s' "$s"
}

emit_report() {
  local timestamp passed=0 failed=0 skipped=0 i
  timestamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  for i in "${!CHECK_NAMES[@]}"; do
    case "${CHECK_STATUSES[$i]}" in
      pass) passed=$((passed + 1)) ;;
      fail) failed=$((failed + 1)) ;;
      skip) skipped=$((skipped + 1)) ;;
    esac
  done
  {
    printf '{\n'
    printf '  "report": "studyagent_post_deploy_check",\n'
    printf '  "timestamp": "%s",\n' "$timestamp"
    printf '  "overall": "%s",\n' "$OVERALL_STATUS"
    printf '  "summary": {"pass": %d, "fail": %d, "skip": %d},\n' "$passed" "$failed" "$skipped"
    printf '  "checks": [\n'
    for i in "${!CHECK_NAMES[@]}"; do
      printf '    {"name": "%s", "status": "%s", "detail": "%s"}' \
        "$(json_escape "${CHECK_NAMES[$i]}")" \
        "$(json_escape "${CHECK_STATUSES[$i]}")" \
        "$(json_escape "${CHECK_DETAILS[$i]}")"
      if [[ $i -lt $((${#CHECK_NAMES[@]} - 1)) ]]; then
        printf ',\n'
      else
        printf '\n'
      fi
    done
    printf '  ]\n'
    printf '}\n'
  } | tee "$REPORT_PATH"
  printf '报告已写入 %s\n' "$REPORT_PATH" >&2
}

# ---------------------------------------------------------------------------
# HTTP 工具
# ---------------------------------------------------------------------------

http_body() {
  curl -fsS --max-time "$CURL_MAX_TIME" "$1"
}

http_status() {
  # 输出 HTTP 状态码；连接失败时输出 curl 语义（refused/timeout/error）而非 000
  local status rc
  set +e
  status="$(curl -s -o /dev/null --max-time "$CURL_MAX_TIME" -w '%{http_code}' "$@")"
  rc=$?
  set -e
  case "$rc" in
    0) printf '%s' "$status" ;;
    7) printf 'refused' ;;
    28) printf 'timeout' ;;
    *) printf 'error(curl=%d)' "$rc" ;;
  esac
}

http_content_type() {
  curl -fsSI --max-time "$CURL_MAX_TIME" "$1" | awk 'BEGIN{IGNORECASE=1} /^Content-Type:/ {print $2; exit}' | tr -d '\r'
}

require_contains() {
  local haystack="$1" needle="$2" label="$3"
  if [[ "$haystack" != *"$needle"* ]]; then
    printf '%s\n' "$label"
    return 1
  fi
}

# ---------------------------------------------------------------------------
# 检查项：容器健康
# ---------------------------------------------------------------------------

docker_available() {
  [[ "$SKIP_DOCKER_CHECKS" != "1" ]] && command -v docker >/dev/null 2>&1 && $COMPOSE ps >/dev/null 2>&1
}

container_state() { # container_state <service> -> "running healthy" / "running -" / "missing"
  local cid
  cid="$($COMPOSE ps -q "$1" 2>/dev/null | head -n1)"
  if [[ -z "$cid" ]]; then
    printf 'missing'
    return 0
  fi
  docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}-{{end}}' "$cid" 2>/dev/null \
    || printf 'missing'
}

check_containers_healthy() {
  local deadline=$((SECONDS + WAIT_TIMEOUT_SECONDS))
  local services pending svc state status health
  services="$($COMPOSE ps --services 2>/dev/null | sort)"
  [[ -n "$services" ]] || { printf 'docker compose 未发现任何服务'; return 1; }
  while :; do
    pending=""
    for svc in $services; do
      state="$(container_state "$svc")"
      status="${state%% *}"
      health="${state##* }"
      if [[ "$status" != "running" ]]; then
        pending+="${svc}=${status} "
      elif [[ "$health" != "-" && "$health" != "healthy" ]]; then
        pending+="${svc}=${health} "
      fi
    done
    if [[ -z "$pending" ]]; then
      printf '全部服务 running/healthy: %s' "$(printf '%s ' $services)"
      return 0
    fi
    if [[ $SECONDS -ge $deadline ]]; then
      printf '等待 %ss 后仍未就绪: %s' "$WAIT_TIMEOUT_SECONDS" "$pending"
      return 1
    fi
    sleep "$WAIT_INTERVAL_SECONDS"
  done
}

# ---------------------------------------------------------------------------
# 检查项：API 冒烟
# ---------------------------------------------------------------------------

# 健康端点是 backend/main.py 的 GET /health（挂在应用根路径，不带 /api 前缀）
check_api_health() {
  local body
  body="$(http_body "${API_BASE_URL}/health")" || { printf 'GET /health 不可达'; return 1; }
  require_contains "$body" '"status":"ok"' 'API /health 未返回 status=ok'
}

check_metrics() {
  local body
  body="$(http_body "${API_BASE_URL}/metrics")" || { printf 'GET /metrics 不可达'; return 1; }
  require_contains "$body" 'http_request_total' 'API /metrics 未暴露 http_request_total'
}

# 登录接口"可达性"：路由是 POST /api/auth/staff/login（见 backend/routers/auth.py）。
# 发送空凭据探测：返回 2xx/4xx 都说明服务活着；refused/timeout/5xx 判为失败。
check_login_reachable() {
  local status
  status="$(http_status -X POST "${API_BASE_URL}/api/auth/staff/login" \
    -H 'Content-Type: application/json' -d '{"username":"","password":""}')"
  case "$status" in
    2??|4??) printf '登录接口可达（HTTP %s）' "$status"; return 0 ;;
    refused) printf '连接被拒绝（服务未监听 %s）' "$API_BASE_URL"; return 1 ;;
    timeout) printf '连接超时'; return 1 ;;
    *) printf '登录接口异常（%s）' "$status"; return 1 ;;
  esac
}

check_admin_login() {
  local payload response
  payload=$(printf '{"username":"%s","password":"%s"}' "$ADMIN_USERNAME" "$ADMIN_PASSWORD")
  response="$(curl -fsS --max-time "$CURL_MAX_TIME" -X POST "${API_BASE_URL}/api/auth/staff/login" \
    -H 'Content-Type: application/json' -d "$payload")" || { printf '管理员登录请求失败'; return 1; }
  require_contains "$response" '"access_token":"' '管理员登录未返回 access_token' || return 1
  ACCESS_TOKEN="$(printf '%s' "$response" | sed -n 's/.*"access_token":"\([^"]*\)".*/\1/p')"
  [[ -n "$ACCESS_TOKEN" ]] || { printf '未能解析 access_token'; return 1; }
}

check_auth_me() {
  local body
  body="$(curl -fsS --max-time "$CURL_MAX_TIME" "${API_BASE_URL}/api/auth/me" \
    -H "Authorization: Bearer ${ACCESS_TOKEN}")" || { printf 'GET /api/auth/me 请求失败'; return 1; }
  require_contains "$body" "\"username\":\"${ADMIN_USERNAME}\"" '/api/auth/me 未返回当前管理员'
}

# ---------------------------------------------------------------------------
# 检查项：alembic 已升级到 head
# ---------------------------------------------------------------------------

check_alembic_head() {
  local current heads current_rev heads_rev
  current="$($COMPOSE exec -T -e PYTHONPATH=/app backend alembic -c "$ALEMBIC_INI_IN_CONTAINER" current 2>/dev/null | grep -Eo '^[0-9a-z_]+' | tail -n1)" || true
  heads="$($COMPOSE exec -T -e PYTHONPATH=/app backend alembic -c "$ALEMBIC_INI_IN_CONTAINER" heads 2>/dev/null | grep -Eo '^[0-9a-z_]+' | tail -n1)" || true
  current_rev="${current:-<none>}"
  heads_rev="${heads:-<none>}"
  if [[ -z "$heads" ]]; then
    printf '无法获取 alembic heads（容器内 alembic 执行失败？）'
    return 1
  fi
  if [[ -z "$current" ]]; then
    printf 'alembic current 为空：数据库未被 stamp/upgrade（head=%s）。见 docs/MIGRATION_RISKS.md' "$heads_rev"
    return 1
  fi
  if [[ "$current_rev" != "$heads_rev" ]]; then
    printf 'alembic 未升级到 head：current=%s head=%s' "$current_rev" "$heads_rev"
    return 1
  fi
  printf 'current=head=%s' "$heads_rev"
}

# ---------------------------------------------------------------------------
# 检查项：依赖组件连通性
# ---------------------------------------------------------------------------

check_postgres() {
  local out
  out="$($COMPOSE exec -T postgres pg_isready -U "${POSTGRES_USER:-postgres}" 2>&1)" \
    || { printf 'pg_isready 失败: %s' "$out"; return 1; }
  require_contains "$out" 'accepting connections' "Postgres 未接受连接: $out"
}

check_redis() {
  local out
  out="$($COMPOSE exec -T redis redis-cli ping 2>&1)" || { printf 'redis-cli ping 失败: %s' "$out"; return 1; }
  require_contains "$out" 'PONG' "Redis 未返回 PONG: $out"
}

# chroma 官方镜像里没有 curl，借 backend 容器的 python 访问其 v2 心跳端点
check_chromadb() {
  local out
  out="$($COMPOSE exec -T backend python -c "
import urllib.request, sys
for path in ('/api/v2/heartbeat', '/api/v1/heartbeat'):
    try:
        with urllib.request.urlopen('http://chromadb:8000' + path, timeout=10) as resp:
            print('heartbeat ok via', path)
            sys.exit(0)
    except Exception as exc:
        last = exc
print('heartbeat failed:', last)
sys.exit(1)
" 2>&1)" || { printf 'ChromaDB 心跳失败: %s' "$out"; return 1; }
  printf '%s' "$out"
}

# ---------------------------------------------------------------------------
# 检查项：前端与监控
# ---------------------------------------------------------------------------

check_web_login() {
  local status
  status="$(http_status "${WEB_BASE_URL}/login")"
  [[ "$status" == "200" ]] || { printf '前端登录页不可访问，状态=%s' "$status"; return 1; }
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
  html="$(http_body "${WEB_BASE_URL}/login")" || { printf '前端登录页不可达'; return 1; }
  require_contains "$html" "/assets/" '前端 HTML 未引用 Vite bundle，可能不是发布构建产物' || return 1

  local assets
  assets="$(extract_frontend_assets "$html")"
  [[ -n "$assets" ]] || { printf '前端 HTML 未解析到 JS/CSS bundle 资源'; return 1; }

  while IFS= read -r asset; do
    [[ -n "$asset" ]] || continue
    asset_url="$(absolute_asset_url "$asset")"
    content_type="$(http_content_type "$asset_url")"
    case "$asset" in
      *.js|*.js\?*)
        [[ "$content_type" == application/javascript* || "$content_type" == text/javascript* ]] \
          || { printf '前端 JS bundle Content-Type 异常：%s -> %s' "$asset_url" "${content_type:-empty}"; return 1; }
        ;;
      *.css|*.css\?*)
        [[ "$content_type" == text/css* ]] \
          || { printf '前端 CSS bundle Content-Type 异常：%s -> %s' "$asset_url" "${content_type:-empty}"; return 1; }
        ;;
    esac
  done <<< "$assets"
}

check_prometheus() {
  local status
  status="$(http_status "${PROMETHEUS_URL}/-/ready")"
  [[ "$status" == "200" ]] || { printf 'Prometheus 未 ready，状态=%s' "$status"; return 1; }
}

check_grafana() {
  local status
  status="$(http_status "${GRAFANA_URL}/login")"
  [[ "$status" == "200" ]] || { printf 'Grafana 登录页不可访问，状态=%s' "$status"; return 1; }
}

# ---------------------------------------------------------------------------
# 自测模式：仅验证记录 / JSON 汇总逻辑（无需 docker / 网络）
# ---------------------------------------------------------------------------

selftest() {
  record "selftest_pass" "pass" "示例通过项"
  record "selftest_skip" "skip" '示例跳过项，含 "引号"、反斜杠 \ 与
换行'
  record "selftest_fail" "fail" "示例失败项"
  emit_report
  [[ "$OVERALL_STATUS" == "pass" ]]
}

# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

main() {
  if [[ "${1:-}" == "--selftest" ]]; then
    selftest
    return $?
  fi

  printf 'StudyAgent post-deploy checks\n'
  printf 'API_BASE_URL=%s\n' "$API_BASE_URL"
  printf 'WEB_BASE_URL=%s\n' "$WEB_BASE_URL"

  # 1) 容器健康 + docker 相关检查
  if docker_available; then
    run_check "containers_healthy" check_containers_healthy
    run_check "alembic_upgraded_to_head" check_alembic_head
    run_check "postgres_connectivity" check_postgres
    run_check "redis_connectivity" check_redis
    run_check "chromadb_connectivity" check_chromadb
  else
    record "containers_healthy" "skip" "docker 不可用或 SKIP_DOCKER_CHECKS=1"
    record "alembic_upgraded_to_head" "skip" "依赖 docker compose exec"
    record "postgres_connectivity" "skip" "依赖 docker compose exec"
    record "redis_connectivity" "skip" "依赖 docker compose exec"
    record "chromadb_connectivity" "skip" "依赖 docker compose exec"
  fi

  # 2) API 冒烟
  run_check "api_health" check_api_health
  run_check "api_metrics" check_metrics
  run_check "login_endpoint_reachable" check_login_reachable
  if [[ -n "$ADMIN_PASSWORD" ]]; then
    run_check "admin_login" check_admin_login
    if [[ -n "$ACCESS_TOKEN" ]]; then
      run_check "auth_me" check_auth_me
    else
      record "auth_me" "skip" "admin_login 未取得 token"
    fi
  else
    record "admin_login" "skip" "未设置 ADMIN_PASSWORD 环境变量（脚本不内置密码）"
    record "auth_me" "skip" "未设置 ADMIN_PASSWORD 环境变量"
  fi

  # 3) 前端
  run_check "web_login_page" check_web_login
  run_check "frontend_bundle_assets" check_frontend_bundle_assets

  # 4) 监控（可选组件，不可达记 skip 而非 fail）
  if curl -fsS --max-time 5 "${PROMETHEUS_URL}/-/ready" >/dev/null 2>&1; then
    run_check "prometheus_ready" check_prometheus
  else
    record "prometheus_ready" "skip" "Prometheus 未启用或当前不可达"
  fi
  if curl -fsS --max-time 5 "${GRAFANA_URL}/login" >/dev/null 2>&1; then
    run_check "grafana_login" check_grafana
  else
    record "grafana_login" "skip" "Grafana 未启用或当前不可达"
  fi

  # 5) 汇总
  emit_report
  if [[ "$OVERALL_STATUS" == "pass" ]]; then
    printf 'All checks passed.\n' >&2
    return 0
  fi
  printf 'Some checks FAILED, see report above.\n' >&2
  return 1
}

main "$@"
