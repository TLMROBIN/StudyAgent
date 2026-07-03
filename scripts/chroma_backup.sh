#!/usr/bin/env bash
#
# chroma_backup.sh —— ChromaDB 持久化数据一致性备份
#
# 数据位置：
#   docker-compose.yml 中 chromadb 服务（chromadb/chroma:1.0.20）把命名卷
#   "chromadata" 挂载到容器内 /chroma/chroma。宿主机上实际位于
#   /var/lib/docker/volumes/<compose项目名>_chromadata/_data（默认项目名为
#   仓库目录名小写，如 studyagent_chromadata）。本脚本通过一次性 helper 容器
#   挂载该卷进行打包，无需 root 直接访问 /var/lib/docker。
#
# 一致性策略（默认 cold 模式）：
#   1) docker compose stop chromadb   —— 停机窗口开始（通常 1~3 秒）
#   2) tar.gz 打包卷内数据（耗时取决于数据量，通常秒级）
#   3) docker compose start chromadb  —— 立即恢复服务（1~2 秒）
#   停机期间 backend/worker 对 Chroma 的向量检索会失败（HTTP 连接拒绝），
#   backend 本身不受影响。总窗口 = 停止 + 打包 + 启动，一般在个位数秒；
#   请安排在凌晨低峰执行（见 docs/CHROMA_BACKUP.md 的 cron 建议）。
#
# --hot 模式：
#   不停容器直接拷贝。零停机，但 Chroma 1.x 底层是 SQLite(WAL) + 分段索引
#   文件，若打包期间恰有写入（文档入库/删除），归档内部可能不一致，恢复后
#   个别 collection 可能损坏。仅在完全不能接受停机时使用，且建议每天至少
#   保留一份 cold 备份。
#
# 用法：
#   scripts/chroma_backup.sh [--hot]
#
# 环境变量（均可选）：
#   BACKUP_DIR        备份目录（默认 <仓库根>/backups/chroma/）
#   KEEP_BACKUPS      保留最近 N 份归档，更旧的自动删除（默认 14）
#   CHROMA_VOLUME     命名卷名覆盖（默认自动探测，兜底 <项目名>_chromadata）
#   CHROMA_DATA_DIR   若设置，则直接打包该宿主机目录（bind-mount 场景/测试）
#   COMPOSE_CMD       compose 命令（默认 "docker compose"）
#   CHROMA_SERVICE    服务名（默认 chromadb）
#   HELPER_IMAGE      helper 容器镜像，需自带 tar（默认 alpine:3）
#   LOG_FILE          日志文件（默认 $BACKUP_DIR/backup.log）
#   SKIP_DOCKER=1     跳过一切 docker 操作（需同时设 CHROMA_DATA_DIR；
#                     用于无 docker 环境下测试文件逻辑）
#   HEARTBEAT_CMD / CHROMA_HEARTBEAT_URL
#                     备份后健康检查方式覆盖（默认经 backend 容器内 python
#                     访问 http://chromadb:8000/api/v2/heartbeat）
#
# 退出码：0 成功；1 打包或校验失败；64 参数错误。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

BACKUP_DIR="${BACKUP_DIR:-${PROJECT_ROOT}/backups/chroma}"
KEEP_BACKUPS="${KEEP_BACKUPS:-14}"
COMPOSE_CMD="${COMPOSE_CMD:-docker compose}"
CHROMA_SERVICE="${CHROMA_SERVICE:-chromadb}"
CHROMA_VOLUME="${CHROMA_VOLUME:-}"
CHROMA_DATA_DIR="${CHROMA_DATA_DIR:-}"
HELPER_IMAGE="${HELPER_IMAGE:-alpine:3}"
SKIP_DOCKER="${SKIP_DOCKER:-0}"
LOG_FILE="${LOG_FILE:-${BACKUP_DIR}/backup.log}"
HOT=0

usage() {
    sed -n '2,50p' "$0" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --hot) HOT=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "未知参数: $1（支持 --hot / --help）" >&2; exit 64 ;;
    esac
    shift
done

mkdir -p "${BACKUP_DIR}"
mkdir -p "$(dirname "${LOG_FILE}")"

log() {
    local line
    line="$(date '+%Y-%m-%dT%H:%M:%S%z') | $*"
    echo "${line}"
    echo "${line}" >> "${LOG_FILE}"
}

if [[ "${SKIP_DOCKER}" == "1" && -z "${CHROMA_DATA_DIR}" ]]; then
    echo "SKIP_DOCKER=1 时必须设置 CHROMA_DATA_DIR（无 docker 无法访问命名卷）" >&2
    exit 64
fi
if [[ -n "${CHROMA_DATA_DIR}" && ! -d "${CHROMA_DATA_DIR}" ]]; then
    echo "CHROMA_DATA_DIR 不存在: ${CHROMA_DATA_DIR}" >&2
    exit 64
fi

# 自动探测命名卷：优先从已创建的 chromadb 容器的 Mounts 里找 /chroma/chroma，
# 找不到则按 compose 默认命名规则兜底。
resolve_volume() {
    if [[ -n "${CHROMA_VOLUME}" ]]; then
        return 0
    fi
    local cid=""
    cid="$(${COMPOSE_CMD} ps -aq "${CHROMA_SERVICE}" 2>/dev/null | head -n1 || true)"
    if [[ -n "${cid}" ]]; then
        CHROMA_VOLUME="$(docker inspect "${cid}" \
            --format '{{range .Mounts}}{{if eq .Destination "/chroma/chroma"}}{{.Name}}{{end}}{{end}}' 2>/dev/null || true)"
    fi
    if [[ -z "${CHROMA_VOLUME}" ]]; then
        local project
        project="${COMPOSE_PROJECT_NAME:-$(basename "${PROJECT_ROOT}" | tr '[:upper:]' '[:lower:]')}"
        CHROMA_VOLUME="${project}_chromadata"
        log "WARN | 未能从容器探测卷名，按默认规则使用: ${CHROMA_VOLUME}"
    fi
}

# 打包：CHROMA_DATA_DIR 模式用宿主机 tar；否则通过 helper 容器挂载命名卷。
# 排除历史恢复留下的 .pre-restore-* 保底目录，避免备份体积滚雪球。
create_archive() {
    local archive="$1"
    if [[ -n "${CHROMA_DATA_DIR}" ]]; then
        tar -czf "${archive}" --exclude='./.pre-restore-*' -C "${CHROMA_DATA_DIR}" .
    else
        local backup_abs
        backup_abs="$(cd "$(dirname "${archive}")" && pwd)"
        docker run --rm \
            -v "${CHROMA_VOLUME}:/data:ro" \
            -v "${backup_abs}:/backup" \
            "${HELPER_IMAGE}" \
            tar -czf "/backup/$(basename "${archive}")" --exclude='./.pre-restore-*' -C /data .
    fi
}

check_heartbeat() {
    if [[ -n "${HEARTBEAT_CMD:-}" ]]; then
        bash -c "${HEARTBEAT_CMD}"
    elif [[ -n "${CHROMA_HEARTBEAT_URL:-}" ]]; then
        curl -fsS -m 5 "${CHROMA_HEARTBEAT_URL}" > /dev/null
    else
        # chromadb 未向宿主机暴露端口，经 backend 容器访问 v2 heartbeat（chroma 1.0.20）
        ${COMPOSE_CMD} exec -T backend python -c "import sys,urllib.request; r=urllib.request.urlopen('http://${CHROMA_SERVICE}:8000/api/v2/heartbeat', timeout=5); sys.exit(0 if r.status==200 else 1)"
    fi
}

wait_heartbeat() {
    local retries="${1:-10}" i
    for ((i = 1; i <= retries; i++)); do
        if check_heartbeat > /dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    return 1
}

# 保留最近 KEEP_BACKUPS 份，删除更旧的归档
rotate_backups() {
    local old
    while IFS= read -r old; do
        rm -f -- "${old}"
        log "INFO | 轮转删除旧备份: $(basename "${old}")"
    done < <(ls -1t "${BACKUP_DIR}"/chroma-*.tar.gz 2>/dev/null | tail -n +"$((KEEP_BACKUPS + 1))")
}

# ---- 主流程 ----
TS="$(date +%Y%m%d-%H%M%S)"
ARCHIVE="${BACKUP_DIR}/chroma-${TS}.tar.gz"
MODE="cold"
[[ ${HOT} -eq 1 ]] && MODE="hot"
START_EPOCH="$(date +%s)"

if [[ "${SKIP_DOCKER}" != "1" && -z "${CHROMA_DATA_DIR}" ]]; then
    resolve_volume
    log "INFO | 备份开始 | mode=${MODE} | volume=${CHROMA_VOLUME} | target=${ARCHIVE}"
else
    log "INFO | 备份开始 | mode=${MODE} | data_dir=${CHROMA_DATA_DIR} | target=${ARCHIVE}"
fi

# 保证异常退出时也会把容器拉起来
STOPPED=0
cleanup() {
    if [[ ${STOPPED} -eq 1 ]]; then
        ${COMPOSE_CMD} start "${CHROMA_SERVICE}" > /dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

if [[ "${MODE}" == "cold" && "${SKIP_DOCKER}" != "1" ]]; then
    STOP_EPOCH="$(date +%s)"
    ${COMPOSE_CMD} stop "${CHROMA_SERVICE}"
    STOPPED=1
    log "INFO | ${CHROMA_SERVICE} 已停止，进入停机窗口"
fi

if ! create_archive "${ARCHIVE}"; then
    rm -f -- "${ARCHIVE}"
    log "FAIL | 打包失败 | mode=${MODE} | file=$(basename "${ARCHIVE}")"
    exit 1
fi

if [[ ${STOPPED} -eq 1 ]]; then
    ${COMPOSE_CMD} start "${CHROMA_SERVICE}"
    STOPPED=0
    log "INFO | ${CHROMA_SERVICE} 已重启 | 停机窗口约 $(( $(date +%s) - STOP_EPOCH ))s"
fi

# 归档完整性校验
if ! tar -tzf "${ARCHIVE}" > /dev/null 2>&1; then
    log "FAIL | 归档校验失败(tar -tzf)，已删除损坏文件: $(basename "${ARCHIVE}")"
    rm -f -- "${ARCHIVE}"
    exit 1
fi

SIZE="$(du -h "${ARCHIVE}" | awk '{print $1}')"
DURATION="$(( $(date +%s) - START_EPOCH ))"
log "OK | mode=${MODE} | file=$(basename "${ARCHIVE}") | size=${SIZE} | duration=${DURATION}s | keep=${KEEP_BACKUPS}"

# cold 模式重启后确认服务恢复（尽力而为，不影响备份成功状态）
if [[ "${MODE}" == "cold" && "${SKIP_DOCKER}" != "1" ]]; then
    if wait_heartbeat 10; then
        log "INFO | heartbeat 确认 ${CHROMA_SERVICE} 已恢复服务"
    else
        log "WARN | 备份成功，但 heartbeat 未确认服务恢复，请人工检查 ${CHROMA_SERVICE}"
    fi
fi

rotate_backups
exit 0
