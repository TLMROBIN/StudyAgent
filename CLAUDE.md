# StudyAgent — AI Agent 操作合约

本文件适用于整个 `StudyAgent/` 仓库，约束所有 AI 编码助手（Claude Code、Codex CLI/OMX 等）在此项目中的行为准则。

---

## 项目定位

局域网内部署的**高中学科 AI 答疑助手**：

- 采用**苏格拉底"助产术"**教学法：不直接给答案，通过阶段性反问引导学生自主推导
- 硬约束：系统严禁在任何引导阶段直接给出最终答案或标准解，兜底阶段（`fallback_walkthrough`）仍保留最后一步
- 支持千人并发、平板可访问、拒绝闲聊
- LAN 内网部署，无公网暴露

**核心链路**：`话题过滤 → RAG 检索 → 苏格拉底 Prompt → LLM 流式输出 → 输出后校验 → SSE 推送`

---

## 工作风格

- 优先**最小可逆 diff**，不在任务范围外主动扩大改动
- 复用已有模式，引入新抽象须有明确理由
- 保留现有行为，除非任务明确要求变更
- **绝不静默扩展范围**：PDF 改动不应意外影响 DOCX/TXT 链路，除非明确要求
- 不在 `.omx/plans/` 下的规划文件中做任何修改（只读）

---

## 技术栈速查

| 层次 | 技术 | 版本 |
|---|---|---|
| 后端框架 | FastAPI + uvicorn[standard] | 0.116.1 / 0.35.0 |
| ORM / 迁移 | SQLAlchemy 2.0 + Alembic | 2.0.43 / 1.16.5 |
| 向量数据库 | ChromaDB | 1.0.20 |
| LLM 接入 | httpx AsyncClient（OpenAI 兼容） | 0.28.1 |
| 缓存 / 限流 | Redis（回退：进程内存） | 6.4.0 |
| 异步任务 | Celery + Redis broker | 5.5.3 |
| Embedding | sentence-transformers（bge-m3，CUDA/CPU 自动） | 5.1.1 |
| PDF 解析 | pypdf / PyMuPDF / pdfplumber（legacy）或 MinerU（GPU） | — |
| 前端 | Vue 3 + Vite + Element Plus + Pinia | npm |
| 监控 | Prometheus + Grafana | — |
| 测试 | pytest | 8.4.2 |
| 压测 | Locust | 2.37.14 |

**LLM Provider**：主 MiniMax（`MiniMax-M2.7`），备 通义千问（`qwen-plus`）；均为 OpenAI 兼容接口，可通过环境变量切换到 DeepSeek。

---

## 目录结构

```
backend/
  main.py                    # FastAPI app、lifespan、中间件注册
  dependencies.py            # JWT 依赖注入（CurrentStudent/Teacher/Admin）
  models/
    user.py                  # User, Classroom, UserRole, teacher_classes
    conversation.py          # Conversation, Message, GuidanceStage, MessageRole
    knowledge.py             # KnowledgeDocument, KnowledgeChunk, ImportTask
    agent_config.py          # AgentConfig（版本化提示词）
    audit_log.py             # AuditLog
    schemas.py               # 30+ Pydantic request/response schema
  routers/
    auth.py                  # /api/auth — 登录、刷新、登出、改密
    chat.py                  # /api/chat — SSE 流式答疑、历史、题目推荐
    knowledge.py             # /api/knowledge — 文档上传、导入任务、切块预览
    stats.py                 # /api/stats — 全校/班级/学生画像统计
    agent_config.py          # /api/agent-config — 版本管理、激活、对比
    admin.py                 # /api/admin — 用户管理、批量导入、审计日志
  services/
    auth_service.py          # JWT family、bcrypt、登录限速、token 黑名单
    llm_service.py           # LLM 双 provider + 熔断 + ThinkingContentFilter
    rag_service.py           # RAG 核心：解析、切分、检索、题目推荐（~700行）
    socratic_service.py      # 苏格拉底三阶段状态机、Prompt 构建
    filter_service.py        # 三层话题过滤（黑名单/白名单/输出校验）
    embed_service.py         # sentence-transformers，hash-fallback
    vector_store_service.py  # ChromaDB 按学科分 Collection
    store_service.py         # BaseStore → RedisStore / MemoryStore
    queue_service.py         # asyncio Semaphore 请求排队（max=20，等待上限=200）
    question_cache_service.py# 热点问题缓存（SHA-256 key，TTL=30min）
    request_replay_service.py# SSE 断连幂等重放（request_id）
    stats_service.py         # 统计聚合（按角色 scope）
    metrics_service.py       # 15+ Prometheus 指标
    audit_service.py         # 审计日志写入
    mineru_service.py        # MinerU PDF 解析（子进程，GPU preflight）
    pdf_parse_bridge.py      # MinerU → 标准 PDFParseResult 转换
    gpu_runtime.py           # CUDA 可用性检测
    question_bank_post_processor.py  # 题库题号识别、答案配对
  tasks/
    celery_app.py            # Celery 配置（broker=Redis DB1，result=Redis DB2）
    ingest.py                # 文档导入 Pipeline（解析→切分→Embedding→ChromaDB）
tests/
  test_filter.py             # 话题过滤（50+ 对抗用例）
  test_socratic.py           # 苏格拉底引导阶段
  test_rag.py                # RAG 检索（核心回归）
  test_ingest.py             # 导入 pipeline 回归
  test_mineru_service.py     # MinerU 解析回归
  test_llm_service.py        # LLM 服务（ThinkingContentFilter 等）
  test_chat_stream.py        # SSE 流式聊天
  test_store.py              # MemoryStore / RedisStore
  test_admin_stats.py        # 统计 API
  test_knowledge_management.py
  test_conversation_topic.py
  test_celery_app.py
  test_observability.py
  test_security.py
  test_gpu_runtime.py
  adversarial_cases.json     # 对抗测试集
frontend/src/
  views/                     # Login, StudentChat, AdminDashboard, KnowledgeManage,
                             # AuditLogs, AgentConfig, UserManage（共7页面）
  utils/api.ts               # axios 封装 + streamChat() SSE + request_id 幂等
  utils/richText.ts          # Markdown + KaTeX 渲染（$...$ / $$...$$ / \(...\)）
  utils/authSession.ts       # localStorage token 读写
  stores/auth.ts             # Pinia：accessToken / refreshToken / user
  router/index.ts            # 路由（含 requiresAuth 守卫、角色权限）
```

---

## 关键文件与归属

| 文件 | 职责 | 注意事项 |
|---|---|---|
| `backend/services/rag_service.py` | chunk 构建、RAG 检索、元数据塑形 | 不要把它变成全局文本清洗器 |
| `backend/services/pdf_parse_bridge.py` | PDF 结构化 block → chunk 转换 | PDF-only 清洗逻辑的首选位置 |
| `backend/services/mineru_service.py` | MinerU 解析归一化与解析元数据 | 子进程调用，注意超时/GPU 依赖 |
| `backend/services/socratic_service.py` | 苏格拉底三阶段 Prompt 构建 | 引导参数来自 AgentConfig，不要硬编码 |
| `backend/services/filter_service.py` | 三层过滤守卫 + 输出校验 | 改动须过对抗测试集 |
| `backend/tasks/ingest.py` | 文档导入 pipeline + Celery 任务 | 回退模式（无 Celery）走 BackgroundTasks |
| `backend/models/schemas.py` | 全部 Pydantic Schema | 修改时同步更新前端调用侧 |
| `tests/test_rag.py` | RAG / PDF 行为主要回归覆盖 | RAG 改动后必须过此文件 |
| `tests/adversarial_cases.json` | 过滤对抗用例（50+） | 过滤改动后必须验证 |
| `.omx/plans/*.md` | OMX 规划文档 | **只读**，不可修改 |

---

## 测试命令

**必须使用项目 venv**，不要假设 `python` / `pytest` 在 PATH 中：

```bash
# 全量测试
.venv/bin/python -m pytest -q

# RAG / 导入 / MinerU 回归（PDF 改动后必须执行）
.venv/bin/python -m pytest tests/test_rag.py -q
.venv/bin/python -m pytest tests/test_ingest.py tests/test_mineru_service.py -q

# 过滤对抗测试（filter/socratic 改动后必须执行）
.venv/bin/python -m pytest tests/test_filter.py tests/test_socratic.py -q

# 编译检查（全量静态语法验证）
.venv/bin/python -m compileall backend tests locustfile.py

# 单测文件快速运行示例
.venv/bin/python -m pytest tests/test_security.py -v
```

---

## 本地开发启动

```bash
# 后端（宿主机直接运行）
source .venv/bin/activate
uvicorn backend.main:app --reload --host 127.0.0.1 --port 8001

# 前端
cd frontend && npm run dev

# Celery Worker（可选，文档导入用）
celery -A backend.tasks.celery_app.celery_app worker -l info

# 容器化热更新开发（挂载宿主机代码，backend --reload）
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d backend worker redis postgres chromadb frontend-dev
```

健康检查：`curl -fsS http://127.0.0.1:8001/health`

---

## Docker / 部署

```bash
# 完整构建（发布验证 / 联调验收）
docker compose up -d --build

# 代码变更后快速重启（无需重新构建镜像）
sg docker -c '/usr/bin/docker compose restart backend worker nginx'

# 健康验证
sg docker -c '/usr/bin/docker compose ps'
curl -fsS http://127.0.0.1:8002/health
```

- 默认访问地址：`http://127.0.0.1:8080/login`（Compose 模式，Nginx 8080）
- 宿主机直跑地址：`http://127.0.0.1:8001`
- 后端 Compose 内暴露端口：`8002 → 8000`
- `backend` 和 `worker` 均挂载宿主机代码（`/app`），**代码改动只需 restart，无需重建**
- GPU 场景：backend / worker 配置了 `gpus: all`（MinerU + CUDA Embedding）

---

## 环境变量关键项

```bash
# LLM Provider
LLM_PRIMARY_BASE_URL=https://api.minimax.chat/v1
LLM_PRIMARY_MODEL=MiniMax-M2.7
LLM_FALLBACK_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_FALLBACK_MODEL=qwen-plus

# PDF 解析后端（legacy / mineru）
PDF_PARSER_BACKEND=mineru
MINERU_PYTHON_BIN=/tmp/mineru-venv/bin/python
MINERU_REQUIRE_GPU_PROOF=true

# Embedding
EMBEDDING_DEVICE=auto          # auto → CUDA / CPU 自动
EMBEDDING_MODEL=BAAI/bge-m3

# Redis
REDIS_URL=redis://127.0.0.1:6379/0   # 宿主机直跑
# REDIS_URL=redis://redis:6379/0      # Compose 内

# ChromaDB
CHROMADB_PATH=./data/chromadb        # 持久化模式
# CHROMADB_HTTP_HOST=chromadb        # HTTP 模式（Compose）
```

---

## PDF 改动优先级规则

PDF 解析路径有项目级约束，违反会导致回归失败：

1. **PDF-only 结构清洗优先放在** `pdf_parse_bridge.py`，不要堆入 `rag_service._normalize_pdf_text()`
2. **重复套头抑制**需满足全部条件：同文档内重复 + 页边位置证据 + 不是标题/受保护结构
3. **表格内容**：格式降级可接受，内容不可丢弃
4. **改动后须提供**：定向回归 + 宽泛回归 + compile 检查 + （如有）容器重启验证

非 PDF 路径（DOCX / TXT / MD）改动不应受 PDF 约束，但需独立回归验证。

---

## RAG / 知识库优先级

1. **RAG 正确性 > 代码美观**：检索质量提升比 chunk 文本格式更重要
2. **保留结构性内容**：章节标题、图表说明、实验步骤、表格单元格
3. **不确定时保留**：看起来有噪音的内容，不确定就保留
4. **优先文档内证据**：频率、页面位置、block 类型优于硬编码书名/出版社

---

## 安全约束（不可破坏）

| 约束 | 位置 |
|---|---|
| 三层话题过滤（输入黑名单 / 学科白名单 / 输出校验） | `filter_service.py` |
| 苏格拉底三阶段，兜底阶段不给最终答案 | `socratic_service.py` |
| JWT token family，重放攻击自动撤销全家族 | `auth_service.py` |
| 登录失败 5 次锁定 15 分钟 | `auth_service.py` |
| 文件上传白名单扩展名 + MIME 双校验 + 50MB 限制 | `knowledge.py` router |
| 资产路径遍历防护（`resolve()` 限制在 asset_dir 内） | `knowledge.py` router |
| 密码修改后全量 token 失效（`password_changed_at`） | `auth_service.py` |

**严禁**：任何形式的直接给答案输出，包括 `validate_answer()` 后校验识别的 5 类模式。

---

## 苏格拉底引导阶段说明

代码中 `GuidanceStage` 枚举控制三个阶段，修改引导逻辑时必须理解：

| 阶段 | 触发条件 | 策略 |
|---|---|---|
| `initial_guidance` | 对话轮次 = 0 | 反问，不给任何提示 |
| `scaffold_hint` | 轮次 1–2 | 适度支架提示，不直接给解题步骤 |
| `fallback_walkthrough` | 轮次 ≥ 3（可配） | 分步解析，**保留最后一步由学生完成** |

引导参数（`fallback_after_turns`、`max_guidance_turns`）来自 `AgentConfig.guidance_params`，不要硬编码。

---

## Git / Commit 规范（Lore 协议）

每次 commit 应遵循 Lore 格式——以"为什么"开头，而非"做了什么"：

```
<intent line: 说明为何做此改动，而非改了什么>

<body: 背景约束与方案选择理由>

Constraint: <外部约束>
Rejected: <被否定的方案> | <原因>
Confidence: <low|medium|high>
Scope-risk: <narrow|moderate|broad>
Directive: <给未来改动者的警告>
Tested: <验证内容>
Not-tested: <已知覆盖盲点>
```

- 只写有价值的 trailer，不强制所有字段
- 仅提交任务相关文件，不提交 `.omx/` 运行时状态（除非任务明确要求）

---

## 压测

```bash
source .venv/bin/activate
python scripts/ensure_loadtest_student.py
locust -f locustfile.py --host http://127.0.0.1:8001 --headless -u 2 -r 1 -t 20s
```

SLO 基线与口径见 `monitoring/SLO_BASELINE.md`。

---

## 常见反模式（避免）

- 无回归测试地做大范围 cleanup / refactor
- 以书名或出版社名硬编码为主要过滤机制
- 仅靠 LLM "忽略"噪音，而不改进上游检索
- 在不了解上下文的情况下添加新依赖
- 在任务范围外静默修改行为
- 声明完成但未提供工具验证证据

---

## 参考文档

| 文档 | 内容 |
|---|---|
| `DEVELOPMENT_PLAN.md` | 原始规划与功能边界 |
| `IMPLEMENTATION_ROADMAP.md` | 7 个推进阶段与验收标准 |
| `DEPLOYMENT_RUNBOOK.md` | 部署运行手册（启动/验收/巡检/回滚） |
| `DEVELOPMENT_WORKFLOW.md` | 日常开发工作流 |
| `QUESTION_BANK_PLAN.md` | 题库功能扩展规划 |
| `QUESTION_BANK_RECOMMENDATION_PHASE1.md` | 题目推荐 Phase 1 PRD 与验证口径 |
| `P0_端到端联调与真机平板测试手把手指南.md` | 真机平板联调指南 |
| `monitoring/SLO_BASELINE.md` | 压测 SLO 基线 |
| `.omx/plans/` | OMX 规划文档（**只读**） |
