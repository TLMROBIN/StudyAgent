# StudyAgent 阶段 6.2 压测与 SLO 基线

本文档用于定义阶段 6.2 的压测入口、SLO 目标、执行命令与验收记录口径。

## 目标

- 把 `locustfile.py` 从占位脚本升级为可执行的登录后真实场景压测入口
- 为学生聊天链路、教师管理链路定义最小可执行 SLO 目标
- 给出本地 / 试运行环境统一的压测命令

## SLO 目标

当前按 `docs/archived/DEVELOPMENT_PLAN.md`（已归档）的试运行口径，先定义以下核心 SLO：

| 指标 | 目标 |
|------|------|
| HTTP 非流式接口错误率 | `< 1%` |
| 学生聊天接口错误率 | `< 3%` |
| 聊天首 token p95 | `< 3s` |
| 聊天完整回答 p95 | `< 30s` |
| 队列深度长期堆积 | 不持续增长 |
| SSE 活跃连接 | 与并发规模基本一致，无异常泄漏 |

## 压测用户

默认使用以下账号：

- 管理员：`admin / StudyAgent123`
- 压测学生：`20269999 / Loadtest123`

创建或重置压测学生账号：

```bash
source .venv/bin/activate
python scripts/ensure_loadtest_student.py
```

## 压测场景

### 1. 教师 / 管理员读接口场景

覆盖：

- `/health`
- `/api/auth/me`
- `/api/stats/overview`
- `/api/stats/classes`
- `/api/stats/portraits`
- `/api/admin/audit-logs`

### 2. 学生答疑场景

覆盖：

- `/api/auth/student/login`
- `/api/chat/history`
- `/api/chat/stream`

`/api/chat/stream` 压测时会校验 SSE 响应中至少包含：

- `event: chunk`
- `event: done`

## 常用命令

本地 smoke：

```bash
source .venv/bin/activate
python scripts/ensure_loadtest_student.py
locust -f locustfile.py --host http://127.0.0.1:8001 --headless -u 2 -r 1 -t 20s
```

仅压教师端：

```bash
source .venv/bin/activate
LOCUST_STUDENT_WEIGHT=0 locust -f locustfile.py --host http://127.0.0.1:8001 --headless -u 5 -r 2 -t 30s
```

仅压学生聊天：

```bash
source .venv/bin/activate
LOCUST_STAFF_WEIGHT=0 LOCUST_ENABLE_STREAM=true locust -f locustfile.py --host http://127.0.0.1:8001 --headless -u 5 -r 1 -t 30s
```

导出报表：

```bash
source .venv/bin/activate
locust -f locustfile.py --host http://127.0.0.1:8001 --headless -u 10 -r 2 -t 1m --csv monitoring/locust-baseline
```

## 当前阶段验收记录

### 2026-07-03（真实环境，10.50.151.230）

- 部署版本：`31e3fba`（含 rag_service 拆分、可配置过滤引擎、e2e 测试、备份脚本）
- 部署验收：`scripts/post_deploy_check.sh` 13/14 pass（alembic 版本表漂移已 `stamp head` 修复；admin 登录项因未提供密码 skip）
- **教师/管理端**（nginx 入口，10 并发 60s）：58 请求 0 失败
  - 登录 med 170ms / 审计日志 med 22ms ✅
  - ⚠️ `/api/stats/overview|classes|portraits` med 19-26s——统计聚合无缓存，待优化
- **学生聊天**（backend 直连 8002 绕过 nginx limit_conn，3 并发 60s，`LOCUST_UNIQUE_NONCE=true`）：
  - `/api/chat/stream` 12 次 0 失败，完整回答 med 9.4s / p95 12s（SLO < 30s ✅）
  - 错误率 0%（SLO < 3% ✅）
- **首 token 单测**（3 次独立提问）：5.5-6.5s——❌ 未达 SLO < 3s。已确认为真流式（44-59 chunk 持续推送），瓶颈在上游 LLM 首响 + 句子边界缓冲；优化方向：首块提前 flush / 前端检索占位提示
- **压测方法论备注**：
  - 单账号 + 固定题池会命中幂等重放缓存（meta+done 无 chunk），务必 `LOCUST_UNIQUE_NONCE=true`
  - nginx `limit_conn perip 2` 会使单机压测大量 503，走 8002 直连或多源发压
  - 经 nginx 入口 5 并发混测：183 次命中重放缓存 + 10 次 503（限流生效），两机制均符合预期
- ChromaDB 备份：生产为 `CHROMADB_MODE=persistent`（数据在 `data/chromadb`，独立 chromadb 容器实为闲置），备份需 `CHROMA_DATA_DIR` 模式；首备 19MB/1s，已装每日 03:30 cron

### 2026-04-02

- 已完成：
  - `locustfile.py` 升级为真实登录 + 管理端 + 学生聊天混合压测脚本
  - 新增 `scripts/ensure_loadtest_student.py`
  - 新增本 SLO 基线文档
- 本机 smoke 结果：
  - 教师端 smoke：`LOCUST_STUDENT_WEIGHT=0 ... -u 2 -t 10s`
  - 教师端结果：
    - `/api/auth/staff/login`：1 次，0 失败，161ms 级
    - `/api/stats/overview`：1 次，0 失败，5ms 级
  - 学生端 smoke：`LOCUST_STAFF_WEIGHT=0 ... -u 1 -t 20s`
  - 学生端结果：
    - `/api/auth/student/login`：1 次，0 失败，161ms 级
    - `/api/chat/stream`：1 次，0 失败，约 12.3s 完整响应
- 当前结论：
  - 脚本已可执行
  - 教师读接口 smoke 通过
  - 学生流式聊天 smoke 通过
  - 当前完整回答时延样本低于 30s 目标
- 后续待补：
  - 更高并发环境下的正式基线结果
  - 首 token 指标建议结合 Prometheus / Grafana 在真实并发下观察
