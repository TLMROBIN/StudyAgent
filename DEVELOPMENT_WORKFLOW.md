# StudyAgent 开发工作流

本文档用于约束本仓库的日常开发方式，避免每次小改都重新构建整套 Docker 镜像，导致 Docker cache、悬空镜像和旧层不断膨胀。

## 推荐原则

- 日常功能开发优先使用宿主机本地开发：
  - 后端：`.venv + uvicorn --reload`
  - 前端：`frontend/npm run dev`
- 需要 Redis / PostgreSQL / ChromaDB 等依赖时，优先只启动依赖容器
- 需要做整体验收、联调、部署验证时，再使用基础 Compose 完整构建

## 文件分工

- `docker-compose.yml`
  - 用于联调验收、部署演练、发布前验证
  - 默认按镜像内容运行，不挂载宿主机代码
  - 适合验证“构建后的真实运行结果”
- `docker-compose.dev.yml`
  - 只作为开发覆盖层使用
  - 为 `backend` / `worker` 挂载宿主机代码
  - 为 `backend` 启用 `--reload`
  - 提供 `frontend-dev` Vite 热更新服务

## 开发方式一：宿主机本地开发

这是默认推荐方式，速度最快，也最省 Docker 空间。

后端：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
cp .env.example .env
uvicorn backend.main:app --reload --host 127.0.0.1 --port 8001
```

前端：

```bash
cd frontend
npm install
npm run dev
```

如果需要依赖容器：

```bash
docker compose up -d redis postgres chromadb
docker compose up -d prometheus grafana
```

说明：

- 改后端 Python 代码：通常无需任何 Docker rebuild
- 改前端页面：Vite 会热更新
- 改依赖容器配置时，再单独重启对应容器

## 开发方式二：容器化热更新开发

如果希望后端与依赖都在容器中运行，可使用：

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d backend worker redis postgres chromadb frontend-dev
```

说明：

- `backend` 挂载宿主机代码，并启用 `--reload`
- `worker` 挂载宿主机代码；改任务代码后通常需要手动重启一次：

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml restart worker
```

- 前端开发地址默认为：
  - `http://127.0.0.1:5173`

## 部署 / 验收方式

发布前、联调验收或需要验证镜像构建结果时，使用基础 Compose：

```bash
docker compose up -d --build
```

说明：

- 这一步会真正构建镜像
- 适合发现“本地开发没问题，但构建镜像后缺文件/缺依赖”的问题
- 不应该作为每次小改代码后的默认操作

## 什么时候需要重新构建镜像

以下场景才建议重新 `build`：

- 修改了 `backend/requirements.txt`
- 修改了任意 `Dockerfile`
- 修改了 Nginx 静态前端构建链路
- 修改了系统级依赖、基础镜像或安装命令
- 需要验证发布产物是否可独立运行

## 什么时候通常不需要重新构建

- 只修改后端 Python 业务代码
- 只修改 Celery 任务逻辑
- 只修改前端 Vue / TS / CSS 页面逻辑
- 只修改大多数 `.env` 中的常规配置值

## 常用命令

启动依赖容器：

```bash
docker compose up -d redis postgres chromadb
```

启动开发覆盖层：

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d backend worker redis postgres chromadb frontend-dev
```

只重启 worker：

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml restart worker
```

查看开发日志：

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml logs -f backend
docker compose -f docker-compose.yml -f docker-compose.dev.yml logs -f worker
docker compose -f docker-compose.yml -f docker-compose.dev.yml logs -f frontend-dev
```

发布前完整构建验证：

```bash
docker compose up -d --build
```

## 维护要求

- 日常开发不要默认执行 `docker compose up --build`
- 每完成一批功能，至少执行一次完整构建验证
- 若 Docker 空间再次快速增长，优先检查：
  - build cache
  - 悬空镜像
  - 旧容器层
- 当前项目的 Docker 数据已迁移到 `/srv/workspace/docker-data`，后续增长不会再挤占系统盘，但仍应保持清理习惯
