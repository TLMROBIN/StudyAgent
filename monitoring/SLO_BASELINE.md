# StudyAgent 阶段 6.2 压测与 SLO 基线

本文档用于定义阶段 6.2 的压测入口、SLO 目标、执行命令与验收记录口径。

## 目标

- 把 `locustfile.py` 从占位脚本升级为可执行的登录后真实场景压测入口
- 为学生聊天链路、教师管理链路定义最小可执行 SLO 目标
- 给出本地 / 试运行环境统一的压测命令

## SLO 目标

当前按 `DEVELOPMENT_PLAN.md` 的试运行口径，先定义以下核心 SLO：

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
