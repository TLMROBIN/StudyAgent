# StudyAgent

局域网内高中学科 AI 答疑助手骨架工程，按 `DEVELOPMENT_PLAN.md` 已落下第一轮可运行基线：

- FastAPI 后端：认证、聊天、知识库、统计、智能体配置、用户管理
- Vue 3 前端：学生答疑界面、管理端看板、知识库、智能体配置、用户管理
- 异步任务：Celery + Redis 任务入口，资料导入支持本地回退执行
- 监控与部署：Prometheus、Nginx、Docker Compose、Locust 压测脚本
- 测试：话题过滤、苏格拉底引导、RAG 基础检索测试

## 当前实现范围

当前仓库已经从纯计划文档推进到工程骨架阶段，重点覆盖了以下链路：

1. 学生/教师/管理员登录与 JWT 会话。
2. 学生端 SSE 流式问答接口，内置学科过滤、RAG 检索、苏格拉底提示和输出校验。
3. 知识库文件上传、导入任务记录、文档切分与基础检索。
4. 管理端用户管理（CSV / XLSX 导入）、统计导出（XLSX / CSV）、提示词版本管理。
5. Docker Compose、Nginx、Prometheus、Grafana 的基础编排文件。

## 目录

```text
backend/      FastAPI 后端
frontend/     Vue 3 前端
tests/        pytest 测试
monitoring/   Prometheus 配置
nginx/        反向代理配置
data/         本地数据目录
```

## 本地启动

推荐顺序：

1. 日常开发优先使用宿主机本地开发
2. 需要容器化热更新时，再叠加 `docker-compose.dev.yml`
3. 需要联调验收或发布验证时，再执行完整 `docker compose up -d --build`

详细说明见：

- `DEVELOPMENT_WORKFLOW.md`

### 1. 后端

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
cp .env.example .env
uvicorn backend.main:app --reload --host 127.0.0.1 --port 8001
```

### 2. 前端

```bash
cd frontend
npm install
npm run dev
```

- 前端开发代理默认转发到 `http://127.0.0.1:8001`
- 如需改端口，可临时设置 `VITE_API_PROXY_TARGET=http://127.0.0.1:你的端口 npm run dev`

### 3. 异步任务

```bash
celery -A backend.tasks.celery_app.celery_app worker -l info
```

### PDF 解析器（本机 MinerU 部署）

- 当前仓库已支持通过环境变量把 **PDF** 解析路径切到 MinerU，`DOCX/TXT` 不受影响
- 本机正式部署建议在 `.env` 中使用：

```bash
PDF_PARSER_BACKEND=mineru
MINERU_PYTHON_BIN=/tmp/mineru-venv/bin/python
MINERU_BACKEND=pipeline
MINERU_PARSE_METHOD=auto
MINERU_DEVICE=cuda
MINERU_DEVICE_MODE=cuda
MINERU_MODEL_SOURCE=local
MINERU_REQUIRE_GPU_PROOF=true
MINERU_PARSE_TIMEOUT_SECONDS=480
INGEST_SOFT_TIME_LIMIT_SECONDS=540
INGEST_HARD_TIME_LIMIT_SECONDS=600
```

- 回滚到旧 PDF 解析器时，只需要把：

```bash
PDF_PARSER_BACKEND=legacy
```

- MinerU 路径会在导入任务目录下写出运行时证明文件：
  - `data/tasks/<task_id>/mineru-runtime.json`
- 当 `MINERU_REQUIRE_GPU_PROOF=true` 时，如果真实导入过程中没有检测到 GPU 使用证明，PDF 导入会失败关闭，而不是静默回退到旧解析器

### 4. 一键容器编排

```bash
docker compose up --build
```

- Compose 试运行模式下，前端会在构建阶段打包，并由 Nginx 直接提供静态文件
- 默认访问地址为 `http://127.0.0.1:8080/login`
- 后端健康检查地址为 `http://127.0.0.1:8002/health`
- 这条命令更适合“发布前验证 / 联调验收”，不建议作为每次小改代码后的默认开发命令

### 5. 容器化热更新开发

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d backend worker redis postgres chromadb frontend-dev
```

- `backend` 会挂载宿主机代码，并启用 `--reload`
- `worker` 会挂载宿主机代码，任务代码更新后通常只需 `restart worker`
- 前端开发地址为 `http://127.0.0.1:5173`
- 这条命令用于开发，不等同于部署验证

## 已知取舍

- Redis 会话、限流和队列目前提供了本地内存回退，便于单机开发先跑通链路；生产环境应接 Redis。
- RAG 检索已升级为 ChromaDB 优先，SQL 仅作回退路径。
- SSE 输出为“先生成后分块发送”的安全实现，优先保证输出校验；若要追求更低首 token 延迟，可以继续扩展成边流式边校验。
- Alembic 已补基础目录和初始 revision，后续应按真实演化继续新增细粒度迁移。
- Embedding 现已支持 `sentence-transformers`，并按 `EMBEDDING_DEVICE` 自动选择 `cuda/cpu`；若当前环境驱动或 CUDA 不可见，会自动回退。

## Redis 说明

- 应用启动时会优先尝试连接 `REDIS_URL`
- 若 Redis 不可用，认证会话状态与限流会自动回退到进程内存
- 本地直接运行后端时，推荐把 `.env` 中的 Redis 地址写为 `redis://127.0.0.1:6379/0`
- 使用 Compose 时，后端和 worker 会自动改为容器内的 `redis://redis:6379/*`
- 可通过 `GET /health` 查看当前使用的是 `redis` 还是 `memory`

## Embedding / ChromaDB 说明

- 默认已支持 ChromaDB 持久化模式，使用 `CHROMADB_PATH`
- Compose 场景下，后端与 worker 已配置为走独立 ChromaDB HTTP 服务
- 当前本地默认 embedding 模型已切换为 `BAAI/bge-m3`
- 若要真正启用 GPU，需要当前 Python 环境下 `torch.cuda.is_available()` 为 `True`
- 若模型运行时不可用，系统会自动回退到哈希向量，保证研发不中断

## LLM 默认配置

- `.env.example` 当前默认主模型为 MiniMax `MiniMax-M2.7`
- 默认备选模型为通义千问 `qwen-plus`
- 若你的正式环境仍使用 DeepSeek，可直接覆盖对应 `LLM_PRIMARY_*` 环境变量

## 测试

```bash
pytest
python3 -m compileall backend tests locustfile.py
```

## 监控与日志

- 后端默认输出 JSON 结构化日志，可通过 `LOG_FORMAT=text` 切回纯文本
- `docker compose up -d prometheus grafana` 后可访问：
  - Prometheus: `http://127.0.0.1:9090`
  - Grafana: `http://127.0.0.1:3000`
- Grafana 默认账号：
  - 用户名：`admin`
  - 密码：`admin`
- 已预置 `StudyAgent Overview` 仪表盘，数据源会自动连接到 Compose 内的 Prometheus
- 若只单独启动 `prometheus` / `grafana` 而未启动 Compose 内的 `backend`，Grafana 和仪表盘仍可正常加载，但 Prometheus 中 `backend:8000` 抓取目标会显示为 `down`

## 压测

```bash
source .venv/bin/activate
python scripts/ensure_loadtest_student.py
locust -f locustfile.py --host http://127.0.0.1:8001 --headless -u 2 -r 1 -t 20s
```

- 详细 SLO 与压测口径见 `monitoring/SLO_BASELINE.md`

## 部署与验收

- 部署运行手册见 `DEPLOYMENT_RUNBOOK.md`
- 一键验收脚本：

```bash
bash scripts/post_deploy_check.sh
```
