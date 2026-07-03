#!/usr/bin/env bash
#
# chroma_restore.sh —— 从 chroma_backup.sh 生成的归档恢复 ChromaDB 数据
#
# 用法：
#   scripts/chroma_restore.sh [--yes] <备份文件.tar.gz>
#
# 流程：
#   1) 校验归档完整性（tar -tzf）
#   2) 交互确认（--yes 跳过，供自动化演练使用）
#   3) docker compose stop chromadb
#   4) 把当前数据整体移入卷内 .pre-restore-<时间戳>/ 目录保底
#   5) 解包归档到数据目录
#   6) docker compose start chromadb，轮询 v2 heartbeat
#      （chroma 1.0.20：GET /api/v2/heartbeat，经 backend 容器内 python 访问，
#        因为 chromadb 未向宿主机暴露端口）
#   7) heartbeat 失败 → 自动回滚到 .pre-restore-<时间戳> 并重启，退出非零
#
# 恢复成功后保底目录会保留，确认业务正常后请手动清理（见脚本尾部输出）。
#
# 环境变量（与 chroma_backup.sh 一致）：
#   CHROMA_VOLUME / CHROMA_DATA_DIR / COMPOSE_CMD / CHROMA_SERVICE /
#   HELPER_IMAGE / LOG_FILE（默认 <仓库根>/backups/chroma/restore.log）
#   SKIP_DOCKER=1     跳过 docker 操作（需 CHROMA_DATA_DIR；测试文件逻辑用；
#                     此时除非显式设置 HEARTBEAT_CMD/CHROMA_HEARTBEAT_URL，
#                     否则跳过 heartbeat 校验）
#   HEARTBEAT_CMD     自定义健康检查命令（返回 0 视为健康）
#   CHROMA_HEARTBEAT_URL  直接 curl 该 URL 做健康检查
#   HEARTBEAT_RETRIES 轮询次数，间隔 2s（默认 30，即最多约 60s）
#
# 退出码：0 成功；1 恢复失败（已尽力回滚）；64 参数错误。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

COMPOSE_CMD="${COMPOSE_CMD:-docker compose}"
CHROMA_SERVICE="${CHROMA_SERVICE:-chromadb}"
CHROMA_VOLUME="${CHROMA_VOLUME:-}"
CHROMA_DATA_DIR="${CHROMA_DATA_DIR:-}"
HELPER_IMAGE="${HELPER_IMAGE:-alpine:3}"
SKIP_DOCKER="${SKIP_DOCKER:-0}"
LOG_FILE="${LOG_FILE:-${PROJECT_ROOT}/backups/chroma/restore.log}"
HEARTBEAT_RETRIES="${HEARTBEAT_RETRIES:-30}"

YES=0
ARCHIVE=""

usage() {
    sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --yes|-y) YES=1 ;;
        -h|--help) usage; exit 0 ;;
        -*) echo "未知参数: $1" >&2; exit 64 ;;
        *)
            if [[ -n "${ARCHIVE}" ]]; then
                echo "只接受一个备份文件参数" >&2
                exit 64
            fi
            ARCHIVE="$1"
            ;;
    esac
    shift
done

if [[ -z "${ARCHIVE}" ]]; then
    usage
    exit 64
fi
if [[ ! -f "${ARCHIVE}" ]]; then
    echo "备份文件不存在: ${ARCHIVE}" >&2
    exit 64
fi
if [[ "${SKIP_DOCKER}" == "1" && -z "${CHROMA_DATA_DIR}" ]]; then
    echo "SKIP_DOCKER=1 时必须设置 CHROMA_DATA_DIR" >&2
    exit 64
fi

mkdir -p "$(dirname "${LOG_FILE}")"
log() {
    local line
    line="$(date '+%Y-%m-%dT%H:%M:%S%z') | $*"
    echo "${line}"
    echo "${line}" >> "${LOG_FILE}"
}

# ---- 归档完整性预检 ----
if ! tar -tzf "${ARCHIVE}" > /dev/null 2>&1; then
    log "FAIL | 归档损坏或不是 tar.gz，拒绝恢复: ${ARCHIVE}"
    exit 1
fi

# ---- 目标定位 ----
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
    fi
}

TARGET_DESC=""
if [[ -n "${CHROMA_DATA_DIR}" ]]; then
    TARGET_DESC="目录 ${CHROMA_DATA_DIR}"
else
    resolve_volume
    TARGET_DESC="docker 卷 ${CHROMA_VOLUME}（容器内 /chroma/chroma）"
fi

# ---- 交互确认 ----
if [[ ${YES} -ne 1 ]]; then
    echo "即将用备份覆盖 ChromaDB 数据："
    echo "  备份文件: ${ARCHIVE}"
    echo "  恢复目标: ${TARGET_DESC}"
    echo "  当前数据将移入保底目录 .pre-restore-<时间戳>，heartbeat 失败会自动回滚。"
    reply=""
    read -r -p "输入 yes 继续: " reply || reply=""
    if [[ "${reply}" != "yes" ]]; then
        echo "已取消。"
        exit 1
    fi
fi

TS="$(date +%Y%m%d-%H%M%S)"
PRE_NAME=".pre-restore-${TS}"
PRE_DIR=""          # 仅目录模式使用

log "INFO | 恢复开始 | archive=$(basename "${ARCHIVE}") | target=${TARGET_DESC} | pre=${PRE_NAME}"

# ---- 停止服务 ----
if [[ "${SKIP_DOCKER}" != "1" ]]; then
    ${COMPOSE_CMD} stop "${CHROMA_SERVICE}"
    log "INFO | ${CHROMA_SERVICE} 已停止"
fi

# ---- 移开当前数据 + 解包 ----
restore_dir_mode() {
    if [[ -d "${CHROMA_DATA_DIR}" ]]; then
        PRE_DIR="${CHROMA_DATA_DIR%/}${PRE_NAME}"
        mv "${CHROMA_DATA_DIR}" "${PRE_DIR}"
    fi
    mkdir -p "${CHROMA_DATA_DIR}"
    tar -xzf "${ARCHIVE}" -C "${CHROMA_DATA_DIR}"
}

rollback_dir_mode() {
    rm -rf "${CHROMA_DATA_DIR}"
    if [[ -n "${PRE_DIR}" && -d "${PRE_DIR}" ]]; then
        mv "${PRE_DIR}" "${CHROMA_DATA_DIR}"
    else
        mkdir -p "${CHROMA_DATA_DIR}"
    fi
}

restore_volume_mode() {
    local arch_dir arch_name
    arch_dir="$(cd "$(dirname "${ARCHIVE}")" && pwd)"
    arch_name="$(basename "${ARCHIVE}")"
    docker run --rm \
        -v "${CHROMA_VOLUME}:/data" \
        -v "${arch_dir}:/backup:ro" \
        "${HELPER_IMAGE}" sh -ceu "
            mkdir -p /data/${PRE_NAME}
            find /data -mindepth 1 -maxdepth 1 ! -name '.pre-restore-*' -exec mv {} /data/${PRE_NAME}/ \;
            tar -xzf '/backup/${arch_name}' -C /data
        "
}

rollback_volume_mode() {
    docker run --rm \
        -v "${CHROMA_VOLUME}:/data" \
        "${HELPER_IMAGE}" sh -ceu "
            find /data -mindepth 1 -maxdepth 1 ! -name '.pre-restore-*' -exec rm -rf {} +
            if [ -d /data/${PRE_NAME} ]; then
                find /data/${PRE_NAME} -mindepth 1 -maxdepth 1 -exec mv {} /data/ \;
                rmdir /data/${PRE_NAME}
            fi
        "
}

do_restore() {
    if [[ -n "${CHROMA_DATA_DIR}" ]]; then
        restore_dir_mode
    else
        restore_volume_mode
    fi
}

do_rollback() {
    if [[ -n "${CHROMA_DATA_DIR}" ]]; then
        rollback_dir_mode
    else
        rollback_volume_mode
    fi
}

start_service() {
    if [[ "${SKIP_DOCKER}" != "1" ]]; then
        ${COMPOSE_CMD} start "${CHROMA_SERVICE}"
    fi
}

if ! do_restore; then
    log "FAIL | 解包失败，回滚到 ${PRE_NAME}"
    do_rollback || log "FAIL | 回滚也失败，请人工处理！保底数据在 ${PRE_NAME}"
    start_service || true
    exit 1
fi
RESTORE_DONE=1

start_service
log "INFO | 数据已恢复，${CHROMA_SERVICE} 已启动，开始 heartbeat 校验"

# ---- heartbeat 校验 ----
check_heartbeat() {
    if [[ -n "${HEARTBEAT_CMD:-}" ]]; then
        bash -c "${HEARTBEAT_CMD}"
    elif [[ -n "${CHROMA_HEARTBEAT_URL:-}" ]]; then
        curl -fsS -m 5 "${CHROMA_HEARTBEAT_URL}" > /dev/null
    else
        ${COMPOSE_CMD} exec -T backend python -c "import sys,urllib.request; r=urllib.request.urlopen('http://${CHROMA_SERVICE}:8000/api/v2/heartbeat', timeout=5); sys.exit(0 if r.status==200 else 1)"
    fi
}

wait_heartbeat() {
    local i
    for ((i = 1; i <= HEARTBEAT_RETRIES; i++)); do
        if check_heartbeat > /dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    return 1
}

HEALTH_OK=1
if [[ "${SKIP_DOCKER}" == "1" && -z "${HEARTBEAT_CMD:-}" && -z "${CHROMA_HEARTBEAT_URL:-}" ]]; then
    log "WARN | SKIP_DOCKER=1 且未提供 HEARTBEAT_CMD/URL，跳过 heartbeat 校验"
else
    if wait_heartbeat; then
        log "INFO | heartbeat 校验通过（/api/v2/heartbeat）"
    else
        HEALTH_OK=0
    fi
fi

if [[ ${HEALTH_OK} -ne 1 ]]; then
    log "FAIL | heartbeat 校验失败，回滚到恢复前数据（${PRE_NAME}）"
    if [[ "${SKIP_DOCKER}" != "1" ]]; then
        ${COMPOSE_CMD} stop "${CHROMA_SERVICE}" || true
    fi
    if do_rollback; then
        start_service || true
        log "FAIL | 已回滚并重启，恢复操作失败，请检查备份文件与服务日志"
    else
        log "FAIL | 回滚失败，请人工处理！保底数据在 ${PRE_NAME}"
    fi
    exit 1
fi

log "OK | 恢复成功 | archive=$(basename "${ARCHIVE}") | 保底数据保留在 ${PRE_NAME}"
echo ""
echo "恢复完成。确认业务正常后清理保底数据："
if [[ -n "${CHROMA_DATA_DIR}" ]]; then
    if [[ -n "${PRE_DIR}" ]]; then
        echo "  rm -rf '${PRE_DIR}'"
    fi
else
    echo "  docker run --rm -v ${CHROMA_VOLUME}:/data ${HELPER_IMAGE} rm -rf /data/${PRE_NAME}"
fi
exit 0
