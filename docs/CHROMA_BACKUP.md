# ChromaDB 备份与恢复

适用于 `docker-compose.yml` 中的 `chromadb` 服务（`chromadb/chroma:1.0.20`）。

## 数据在哪里

- compose 把命名卷 `chromadata` 挂载到容器内 `/chroma/chroma`（Chroma 1.x 的持久化目录：`chroma.sqlite3` + 各 segment 目录）。
- 宿主机实际路径为 `/var/lib/docker/volumes/<项目名>_chromadata/_data`（项目名默认为仓库目录名小写，如 `studyagent_chromadata`）。
- 脚本**不直接读写** `/var/lib/docker`，而是通过一次性 helper 容器（默认 `alpine:3`）挂载卷来打包/解包，普通 docker 用户即可运行。
- `chromadb` 服务未向宿主机暴露端口，backend/worker 以 HTTP 模式经 compose 内部网络访问 `chromadb:8000`；健康检查用 Chroma 1.0.x 的 v2 心跳端点 `GET /api/v2/heartbeat`，脚本默认借 `backend` 容器内的 python 发起请求。

## 备份：scripts/chroma_backup.sh

```bash
# 冷备（默认，推荐）：短暂停 chromadb → 打包 → 立即拉起，停机窗口秒级
scripts/chroma_backup.sh

# 热备：不停机拷贝，零停机但打包期间若有写入，归档可能内部不一致
scripts/chroma_backup.sh --hot
```

行为：

1. cold 模式 `docker compose stop chromadb`（停机窗口开始，期间 RAG 向量检索会失败，backend 本身不受影响）。
2. 打包为 `BACKUP_DIR/chroma-<YYYYmmdd-HHMMSS>.tar.gz`（自动排除卷内历史 `.pre-restore-*` 保底目录）。
3. `docker compose start chromadb` 立即恢复服务，并轮询 heartbeat 确认（失败仅告警，不影响备份成功状态）。
4. `tar -tzf` 校验归档完整性，损坏则删除并以非零退出。
5. 只保留最近 `KEEP_BACKUPS`（默认 14）份，更旧的自动删除。
6. 结果（模式、文件、大小、耗时）追加到 `BACKUP_DIR/backup.log`。

常用环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `BACKUP_DIR` | `<仓库根>/backups/chroma/` | 备份目录 |
| `KEEP_BACKUPS` | `14` | 保留份数 |
| `CHROMA_VOLUME` | 自动探测 | 命名卷名（探测失败时兜底 `<项目名>_chromadata`） |
| `CHROMA_DATA_DIR` | 空 | 设置后直接打包该宿主机目录（bind-mount 场景/演练） |
| `HELPER_IMAGE` | `alpine:3` | helper 镜像，需自带 tar；离线环境请提前 `docker pull` |
| `COMPOSE_CMD` | `docker compose` | 旧环境可设 `docker-compose` |
| `SKIP_DOCKER=1` | - | 跳过所有 docker 操作（配合 `CHROMA_DATA_DIR` 做无 docker 演练） |

## 恢复：scripts/chroma_restore.sh

```bash
# 交互确认
scripts/chroma_restore.sh backups/chroma/chroma-20260703-030002.tar.gz

# 自动化/演练（跳过确认）
scripts/chroma_restore.sh --yes backups/chroma/chroma-20260703-030002.tar.gz
```

流程与安全网：

1. 先 `tar -tzf` 预检归档，损坏直接拒绝。
2. 停 `chromadb`，把当前数据整体移入卷内 `.pre-restore-<时间戳>/` 保底目录（不删除任何旧数据）。
3. 解包归档 → 启动容器 → 轮询 `/api/v2/heartbeat`（默认最多 30 次 × 2s）。
4. **heartbeat 失败自动回滚**到保底数据并重启，退出非零。
5. 成功后保底目录保留，脚本末尾会打印清理命令，确认业务正常后再手动清理。

注意：恢复后向量库回到备份时刻，而 PostgreSQL 里的 `KnowledgeChunk` 元数据是当前时刻——两者可能出现窗口差。若备份后有新文档入库，恢复后需对这些文档重新触发向量化（或重新导入），否则检索会缺这部分内容。

## 建议的 cron（凌晨低峰）

```cron
# 每天 03:30 冷备 ChromaDB（停机窗口秒级），保留 14 天
30 3 * * * cd /path/to/StudyAgent && BACKUP_DIR=/data/backups/chroma ./scripts/chroma_backup.sh >> /var/log/chroma_backup.cron.log 2>&1
```

现成模板见 `scripts/chroma_backup.cron.example`。要点：

- cron 环境 PATH 精简，必要时在 crontab 顶部补 `PATH=/usr/local/bin:/usr/bin:/bin`，确保能找到 `docker`。
- 运行用户需在 `docker` 组。
- 建议把 `BACKUP_DIR` 指到独立磁盘/挂载点，并对该目录另做异地同步（rsync/对象存储）。
- 备份成功与否可通过退出码接告警；`backup.log` 中 `FAIL`/`WARN` 行可被日志采集监控。

## 恢复演练步骤（建议每季度一次）

1. 挑最近一份备份，在演练目录解压做无 docker 演练：
   ```bash
   mkdir -p /tmp/drill/data && tar -xzf backups/chroma/chroma-<ts>.tar.gz -C /tmp/drill/data
   SKIP_DOCKER=1 CHROMA_DATA_DIR=/tmp/drill/data BACKUP_DIR=/tmp/drill/backups ./scripts/chroma_backup.sh
   SKIP_DOCKER=1 CHROMA_DATA_DIR=/tmp/drill/data ./scripts/chroma_restore.sh --yes /tmp/drill/backups/chroma-*.tar.gz
   ```
2. 真实环境演练（低峰期）：
   ```bash
   ./scripts/chroma_backup.sh                      # 先做一份最新冷备
   ./scripts/chroma_restore.sh backups/chroma/chroma-<ts>.tar.gz
   ```
3. 验证服务与数据：
   ```bash
   curl -fsS http://127.0.0.1:8002/health | python -m json.tool   # 看 rag/vector_store 状态
   # 或直接看 heartbeat（经 backend 容器）：
   docker compose exec backend python -c "import urllib.request;print(urllib.request.urlopen('http://chromadb:8000/api/v2/heartbeat').read())"
   ```
   再抽查一次 RAG 问答，确认能召回旧文档。
4. 确认正常后清理保底目录（脚本结尾打印的 `rm` 命令）。

## 注意事项

- **热备风险**：Chroma 底层是 SQLite(WAL) + segment 文件，`--hot` 期间如有写入（文档导入/删除任务在跑），归档可能不一致，恢复后个别 collection 打不开。生产上优先 cold；若必须 hot，请避开 Celery worker 有导入任务的时段。
- **停机窗口**：cold 模式窗口 = stop + tar + start，数据量 GB 级时 tar 是大头；期间 backend 的向量检索请求会报错但会自动恢复，无需重启 backend。
- helper 容器以 root 写文件，生成的归档在宿主机上可能属主为 root，需要时 `chown` 一下。
- 备份目录默认在仓库内 `backups/chroma/`，注意别把它提交进 git（必要时加入 `.gitignore`）。
- 恢复会整体替换向量库；多学科 collection（`studyagent-<subject>`）一起回滚，无法单科恢复。
- `chroma.sqlite3` 内含全部 collection 元数据与向量引用，**不要**尝试只备份/恢复部分 segment 目录。
