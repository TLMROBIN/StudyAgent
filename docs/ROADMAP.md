# StudyAgent 路线图（当前有效）

> 本文件取代 `docs/archived/IMPLEMENTATION_ROADMAP.md`，反映 **2026-07-03** 的真实状态。
> 状态判定均经代码抽查验证，证据以"文件:行号"标注。

项目定位：局域网内高中学科 AI 答疑助手。技术栈：FastAPI + Vue3 + ChromaDB + Celery + Redis + Prometheus/Grafana。

---

## 一、阶段状态总览

> 说明：原 IMPLEMENTATION_ROADMAP.md 正式列出阶段 1–6（阶段 6 拆分为 6.1/6.2/6.3）；"阶段 7 生产硬化"为其隐含的后续目标，此处一并列出以便追踪。

| 阶段 | 状态 | 实际情况备注 |
|------|------|------|
| 阶段 1：Redis 真实接入 | ✅ 完成 | `backend/services/store_service.py` 中 `RedisStore` 完整实现，含基于 Lua 脚本的分布式配额计数 |
| 阶段 2：真实 RAG / ChromaDB 链路 | ✅ 完成 | `backend/services/vector_store_service.py` 支持 HTTP / 持久化两种模式，9 个学科集合，upsert/query 闭环可用 |
| 阶段 3：Celery 导入闭环 | ✅ 完成 | `backend/tasks/ingest.py`（571 行）：上传 → 校验 → MinerU/文本解析 → 分块 → 向量化 → ChromaDB 入库全链路打通；**超出原计划**：含 MinerU GPU preflight（`backend/services/mineru_service.py:504` `collect_cuda_requirement_snapshot()`）及 GPU watchdog（`scripts/gpu_runtime_watchdog.sh` + systemd 单元） |
| 阶段 4：真实流式聊天 | ✅ 完成 | **已验证为真流式**（此前"伪流式"结论过时）：`backend/services/llm_service.py:613` 以 `stream: True` 发起请求，`client.stream()` + `aiter_lines()` 逐 token 接收（:620-623）；`backend/routers/chat.py:1513-1560` 逐事件转发，按句子边界分块（`_split_stream_buffer`，chat.py:120）；含心跳保活（chat.py:1521-1523）、断线检测释放（chat.py:1514-1518）、逐段安全校验与安全改写（chat.py:1538-1554 `filter_service.validate_answer`） |
| 阶段 5：管理端补齐 | 🔶 部分完成 | 审计日志查看、统计概览等管理接口已就绪（`/api/admin/audit-logs`、`/api/stats/overview` 等，见 locustfile.py 压测覆盖）；**题库推荐仅 Phase 1 骨架**：`backend/routers/chat.py:827` `/api/chat/recommendations` 只有 `keyword` / `context` 两种模式（`backend/models/schemas.py:214-232`），基于向量相似度 + 年级/难度过滤，结果硬编码上限 3 条，无排序/个性化算法 |
| 阶段 6：监控、压测与部署验收 | 🔶 部分完成 | 框架就绪：`monitoring/prometheus.yml`、`monitoring/grafana/`、`monitoring/SLO_BASELINE.md`（SLO 目标已定义：非流式错误率 <1%、聊天错误率 <3%、首 token p95 <3s、完整回答 p95 <30s）；`locustfile.py`（165 行）覆盖教师看板 + 学生 SSE 聊天场景。**但仅 2026-04-02 本地 smoke 通过（完整回答 p95 ≈12.3s），10+ 并发的正式基线与真实环境压测未做** |
| 阶段 7：生产硬化 | ⬜ 未开始 | e2e 集成测试（tests/ 有 42 个文件 / 350+ 单测，但无独立 e2e 套件）、ChromaDB 备份恢复、Alembic 回滚验证、真实环境压测等均在排期中，见下方"下一步" |

## 二、超出原计划已交付

以下功能不在原路线图中，已在代码中逐一确认存在：

| 功能 | 代码证据 |
|------|------|
| LLM 配额管理 | `backend/models/llm_model.py`：`LLMModelConfig` / `LLMQuotaPolicy`（用户日限、学校日限、provider 5 小时滚动限额）；`backend/routers/chat.py:1446` 流式链路接入 `llm_quota_service.check_and_reserve` |
| OIDC 认证 | `backend/services/oidc_service.py`：完整授权码流程（`create_login_state` / `exchange_code_for_claims`），支持 PKCE |
| 学生反馈 | `backend/routers/feedback.py` + `backend/models/feedback.py`：`StudentFeedback` / `StudentFeedbackAttachment` / `StudentFeedbackBan`，含图片附件、回复、封禁管理 |
| 审计日志 | `backend/models/audit_log.py` + `backend/services/audit_service.py`：actor/action/target/result/ip 全字段记录 |
| 版本化智能体配置 | `backend/models/agent_config.py:16`：`AgentConfig.version` 版本字段 |
| MinerU GPU preflight | `backend/services/mineru_service.py:504`：CUDA 环境快照检查，Celery 导入任务执行前预检 |

## 三、下一步（已排期）

| # | 事项 | 说明 |
|---|------|------|
| 1 | 拆分 rag_service | `backend/services/rag_service.py` 已达约 3,570 行（另 `routers/knowledge.py` 约 1,790 行），混合 PDF 解析 / 文本提取 / 分块 / 元数据等多职责，拟拆分为 pdf/docx/text builder + chunk/metadata service |
| 2 | 真流式 SSE + 分块校验 | ⚠️ 代码抽查显示真流式与逐段校验**已实现**（见阶段 4 证据），此项建议改为"联调验证 + 收尾关闭"，确认前端逐段渲染与断线释放后即可销项 |
| 3 | LLM 输出校验可配置引擎 | 现有 `filter_service.validate_answer` 为固定规则，改造为可配置规则引擎 |
| 4 | ChromaDB 备份恢复 | 建立定期备份 + 恢复演练脚本（现仅有 `scripts/backup_imported_documents.py` 覆盖原始文档） |
| 5 | e2e 集成测试 | 补齐独立端到端测试套件（上传→导入→提问→流式回答全链路） |
| 6 | 真实环境压测 | 在目标部署环境跑 10+ 并发正式基线，补齐 `monitoring/SLO_BASELINE.md` 验收记录 |

## 四、历史文档索引

原规划文档已归档至 `docs/archived/`：

- `docs/archived/DEVELOPMENT_PLAN.md` — 原始总体开发计划
- `docs/archived/IMPLEMENTATION_ROADMAP.md` — 原 7 阶段实施路线图（本文件取代之）
- `docs/archived/QUESTION_BANK_PLAN.md` — 题库长期规划
- `docs/archived/QUESTION_BANK_RECOMMENDATION_PHASE1.md` — 题目推荐 Phase 1 PRD
- `docs/archived/QUESTION_CHUNK_RECONCILIATION_PLAN.md` — 题目分块对账方案
- `docs/archived/P0_端到端联调与真机平板测试手把手指南.md` — 早期真机联调指南
- `docs/archived/omx-plans/` — .omx 规划文件的历史版本（现行版本仍在 `.omx/plans/`）
