# StudyAgent 部署运行手册

本文档承接阶段 6.3，目标是把“如何启动、如何验收、如何回滚、如何巡检”整理成一次可执行的运行手册。

## 适用范围

当前手册对应的是本仓库现有单机基线：

- 应用：FastAPI + Vue 3 + Celery + Redis + ChromaDB
- 编排：Docker Compose
- 监控：Prometheus + Grafana
- 反向代理：Nginx

说明：

- 当前基础 `docker-compose.yml` 主要用于联调验收、部署演练和发布前验证
- 日常开发请优先参考仓库内 `DEVELOPMENT_WORKFLOW.md`
- 当前方案仍适合内网试运行、联调和验收，不等同于最终生产硬化方案

## 一、部署前检查

部署前至少确认以下项目：

1. 机器具备 Docker / Docker Compose 能力
2. 目标端口未被占用：
   - `8080`
   - `3000`
   - `8002`
   - `9090`
3. 已准备 `.env`
4. 已确认外部 LLM API Key 可用
5. 若希望使用 GPU Embedding，已确认当前 Python / 容器运行时确实可见 CUDA

建议先复制环境变量模板：

```bash
cp .env.example .env
```

重点环境变量：

- `JWT_SECRET_KEY`
- `BOOTSTRAP_ADMIN_USERNAME`
- `BOOTSTRAP_ADMIN_PASSWORD`
- `LLM_PRIMARY_BASE_URL`
- `LLM_PRIMARY_API_KEY`
- `LLM_PRIMARY_MODEL`
- `LLM_FALLBACK_BASE_URL`
- `LLM_FALLBACK_API_KEY`
- `LLM_FALLBACK_MODEL`

## 二、首次启动

### 方式 A：本地开发联调

后端：

```bash
source .venv/bin/activate
uvicorn backend.main:app --reload --host 127.0.0.1 --port 8001
```

前端：

```bash
cd frontend
npm run dev
```

监控：

```bash
docker compose up -d --no-deps prometheus grafana
```

依赖容器：

```bash
docker compose up -d redis postgres chromadb
```

### 方式 A2：容器化热更新开发

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d backend worker redis postgres chromadb frontend-dev
```

说明：

- 该方式用于开发，不用于发布前最终镜像验证
- `backend` 在 dev 覆盖层中启用 `--reload`
- `worker` 使用宿主机代码挂载，任务代码变更后通常只需重启容器
- 前端开发默认走 `http://127.0.0.1:5173`

### 方式 B：Compose 试运行

```bash
docker compose up -d --build
```

说明：

- 当前 Compose 默认只暴露前台联调所需端口：`8080`、`3000`、`8002`、`9090`
- `redis`、`postgres`、`chromadb` 仅在 Compose 内部网络中互通，不再占用宿主机端口
- 这样可以避免与开发机上常见的本地 Redis / PostgreSQL / 向量库服务冲突
- 当前机器上已有其他服务占用 `80` 和 `8000` 时，可直接使用这组默认映射并行运行
- 当前 Compose 下不再依赖 Vite dev server，局域网访问走 Nginx 静态文件，稳定性更高
- 这一步用于发布前验证时才值得执行；不要把它当成每次小改后的默认开发命令

查看状态：

```bash
docker compose ps
```

查看日志：

```bash
docker compose logs -f backend
docker compose logs -f worker
docker compose logs -f nginx
```

## 三、部署后验收

### 1. 一键脚本验收

本仓库已提供：

- [scripts/post_deploy_check.sh](/home/binyu/文档/trae_projects/StudyAgent/scripts/post_deploy_check.sh)

本地开发联调默认命令：

```bash
bash scripts/post_deploy_check.sh
```

若是其他地址，可覆盖：

```bash
API_BASE_URL=http://127.0.0.1:8002 \
WEB_BASE_URL=http://127.0.0.1:8080 \
PROMETHEUS_URL=http://127.0.0.1:9090 \
GRAFANA_URL=http://127.0.0.1:3000 \
ADMIN_USERNAME=admin \
ADMIN_PASSWORD=StudyAgent123 \
bash scripts/post_deploy_check.sh
```

说明：

- 脚本主要覆盖 API、登录接口、监控入口的存活检查
- 知识库切分预览、题目拆分质量、推荐题卡片仍需要人工验收

### 2. 人工验收清单

部署完成后，至少按下面顺序做一轮人工验收：

1. 打开 `http://127.0.0.1:8080/login`，确认登录页可访问
2. 在终端补测一次 `8080 -> nginx -> backend` 登录代理链路：

```bash
curl -i -s http://127.0.0.1:8080/login | head

curl -i -s -X POST http://127.0.0.1:8080/api/auth/staff/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"你的管理员密码"}'
```

3. 用管理员账号实际登录后台，确认能进入 `统计看板`、`知识库`、`审计日志`、`用户管理`
4. 用学生测试账号实际登录，并完成 1 次流式问答
5. 在知识库上传至少 1 份讲义类资料；如果本轮包含题库能力验收，再补传 1 份题库/习题类资料
6. 上传完成后，确认“最新任务”或任务列表里能看到切分摘要，例如片段数、题目数、答案数、解析数、章节数、图片数
7. 点击 `查看切分`，抽查 chunk 实际内容、章节/小节、题干/答案/解析配对、图片预览是否正确
8. 如果把资料类型从 `知识讲义` 改成 `题库试卷` 或反过来，页面应明确提示“不会自动重新切分，需要删除后重新上传”
9. 对扫描版 PDF、空文档或不支持格式，页面应优先显示教师可理解的中文失败提示，而不是直接暴露技术异常
10. 学生端点击 `推荐同类题`，确认推荐卡片、`换一批`、`带入输入框` 与题图保留都正常
11. 管理端统计页、审计日志页、Grafana `StudyAgent Overview` 均可正常打开

## 四、日常巡检

建议每日或每次变更后巡检：

1. `/health` 返回 `status=ok`
2. `/metrics` 可访问
3. `http://127.0.0.1:8080/login` 可打开，且登录 POST 不返回 `502`
4. Grafana 登录页可访问
5. Prometheus target 中 backend 抓取为 `up`
6. Redis / Postgres / ChromaDB 容器状态正常
7. 学生聊天抽测 1 次，并确认推荐题卡片链路可用
8. 如果当天发生过资料导入，至少抽查 1 条任务摘要或 `查看切分`
9. 知识库检索抽测 1 次

推荐命令：

```bash
docker compose ps
docker compose logs --tail=200 backend
docker compose logs --tail=200 worker
curl -s http://127.0.0.1:8002/health
curl -s http://127.0.0.1:8002/metrics | head
curl -i -s http://127.0.0.1:8080/login | head
```

## 五、常见异常与处理

### 1. 登录失败

优先检查：

1. 浏览器是否访问的是 `http://127.0.0.1:8080/login`
2. `GET /login` 与 `POST /api/auth/staff/login` 是否都能通过 `8080` 返回 `200`
3. `nginx` 是否仍指向当前后端地址
4. 管理员账号是否被锁定
5. `BOOTSTRAP_ADMIN_PASSWORD` 是否与实际数据库状态一致

推荐先执行：

```bash
curl -i -s http://127.0.0.1:8080/login | head

curl -i -s -X POST http://127.0.0.1:8080/api/auth/staff/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"你的管理员密码"}'

docker compose logs --tail=200 nginx
docker compose logs --tail=200 backend
```

如果登录页能打开，但登录 POST 返回 `502 Bad Gateway`，优先怀疑 `nginx -> backend` 代理仍指向旧地址；可先重建 Nginx：

```bash
docker compose up -d --build --no-deps nginx
```

### 2. Grafana 正常但看板无数据

优先检查：

1. Prometheus 是否 `ready`
2. Prometheus target 中 `studyagent-backend` 是否为 `up`
3. 是否只启动了 `grafana/prometheus`，但没启动 Compose 内的 `backend`

### 3. SSE 聊天很慢

优先检查：

1. 外部 LLM 是否响应变慢
2. `chat_first_token_seconds` 与 `chat_full_response_seconds` 指标是否升高
3. `llm_queue_depth` 是否持续堆积

### 4. 文档导入卡住或切分结果不符合预期

优先检查：

1. `worker` 容器是否正常
2. Redis 是否正常
3. Celery 日志是否存在重试 / 超时
4. 是否为扫描版 PDF
5. 上传时资料类型是否选对
6. “最新任务”摘要与 `查看切分` 里系统实际学到了什么
7. 是否先改了 metadata 资料类型，再期待系统自动重新切分

推荐先执行：

```bash
docker compose logs --tail=200 worker
docker compose logs --tail=200 backend
```

补充判断：

- 如果资料上传成功，但切分结果明显不对，先点 `查看切分` 判断系统当前是按章节/段落切分，还是按题目切分
- 如果资料类型后来从 `知识讲义` 改成 `题库试卷`，当前系统只会同步 metadata，不会自动重新切分；需要删除后重新上传
- 如果失败提示直接暴露原始技术异常，建议同时记录前端提示与后端日志，后续统一补充友好错误文案

### 5. 推荐题卡片没有出现或图片丢失

优先检查：

1. 题库资料是否真的按题目拆分成功
2. `查看切分` 里是否能看到题干、答案、解析、图片
3. 资料上传时是否误选成 `知识讲义` / `教材`
4. 学生当前问题是否与题库内容相关
5. 后端日志里是否有推荐或图片处理异常

## 六、回滚策略

当前试运行基线建议按以下粒度回滚：

### 1. 配置回滚

- 回滚 `.env`
- 重启相关服务

### 2. 应用回滚

- 回到上一版代码目录
- 重新执行 `docker compose up -d --build`

### 3. 数据回滚

当前仓库默认使用：

- `data/` 目录
- Compose volume：`pgdata`、`chromadata`、`grafanadata`

回滚前原则：

1. 先停写入
2. 先备份
3. 再恢复数据

### 4. 回滚后必须复验

至少重跑：

```bash
bash scripts/post_deploy_check.sh
```

并人工确认：

1. 登录页可打开，且 `8080` 登录 POST 不返回 `502`
2. 学生聊天正常
3. 知识库可上传 1 份资料，并能看到切分摘要或 `查看切分`
4. 推荐题卡片正常
5. 统计页
6. 监控页

## 七、当前已知限制

1. 当前基础 Compose 主要用于联调验收和发布验证，距离最终生产硬化方案仍有差距
2. Grafana 默认账号密码仍为 `admin / admin`，正式试运行前应修改
3. 更高并发压测结果仍需在目标试运行环境补测
4. 若目标机器存在其他占用 `8000/8001/8080` 的服务，需先调整端口规划

## 八、交付建议

当前仓库已经具备：

- 本地联调
- 内网试运行
- 基础可观测性
- 可执行 smoke 压测
- 部署验收脚本

若进入下一轮，可优先考虑：

1. 继续细化独立的生产硬化编排
2. 后端改为更明确的多 worker / 进程管理策略
3. 增加数据备份脚本
4. 增加正式试运行环境的并发基线报告
