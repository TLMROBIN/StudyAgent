# Execution-ready Spec: MinerU PDF Parser Integration for StudyAgent

## Metadata
- Slug: mineru-parser-integration
- Generated: 2026-04-06T07:58:20Z
- Profile: standard
- Context type: brownfield
- Final ambiguity: 18.4%
- Threshold: 20%
- Context snapshot: `.omx/context/mineru-parser-integration-20260406T070227Z.md`
- Transcript: `.omx/interviews/mineru-parser-integration-20260406T075820Z.md`

## Clarity breakdown
| Dimension | Score |
|---|---:|
| Intent | 0.66 |
| Outcome | 0.86 |
| Scope | 0.87 |
| Constraints | 0.88 |
| Success | 0.82 |
| Context | 0.90 |

## Intent
用户希望正式把 MinerU 部署到本机并纳入 StudyAgent，以显著提升 PDF 尤其是扫描版、复杂版式 PDF 的解析质量；随后围绕解析后的统一内容对象重构切片、标签化和图片管理，使题目推荐能准确关联题目与图片，教材资料也具备结构与标签以服务 RAG。

## Desired Outcome
1. MinerU 正式部署在本机并作为 StudyAgent 的 **PDF 解析器**。
2. 首期仅替换/接管 PDF 解析层，不改 DOCX/TXT 入口。
3. 解析后的 PDF 内容进入统一的下游表示，支持：
   - 切片
   - 标签化
   - 图片管理
4. 题目推荐结果能够准确带出题目文本及其对应图片。
5. 教材类 PDF 具备层级结构和标签，提升 RAG 检索与召回质量。
6. 系统允许内部元数据/存储契约调整，只要最终功能增强。

## In Scope
- 正式部署 MinerU 到本机生产使用环境
- 将 MinerU 接入 StudyAgent 的 PDF 导入/解析链路
- 重新设计或重构 PDF 解析后的统一内容对象/中间表示
- 基于新中间表示重构：
  - chunk 切片逻辑
  - 标签识别/标签化逻辑
  - 图片抽取与图片引用管理逻辑
- 为题目推荐与教材 RAG 补齐可消费的结构与标签
- 使用本机 GPU 加速，并允许首期直接采用更高精度的 MinerU 路线（hybrid/VLM 类）
- 保留对“干净题库 PDF”的基本可用性，避免严重退化

## Out-of-Scope / Non-goals
- DOCX/TXT 改造
- 对历史已入库文档做批量重跑/回灌
- 前端交互大改
- 让 MinerU 在首期直接接管“切题”语义本身

## Decision Boundaries
以下内容 OMX 可自行决策，无需再次确认：
- 内部元数据结构调整
- 内部存储契约调整
- PDF 解析链的服务形态（只要符合首期目标）
- 为实现目标所需的下游 chunk/tag/image 统一中间层设计
- 在 MinerU 允许范围内选择更高精度 GPU 路线，而非保守 pipeline-only 路线

以下内容应保持与本规格一致，不应擅自反向收缩：
- 首期只接管 PDF
- 不触碰 DOCX/TXT
- 不做历史文档重跑
- 不大改前端
- 不让 MinerU 首期接管切题

## Constraints
- 当前主干代码围绕：`run_ingest_pipeline -> rag_service.extract_content() -> prepare_document_chunks()`
- 现有系统 phase-1 设计里 OCR 原本不在范围内，且扫描版 PDF 直接失败；本次工作等价于重新定义 PDF 能力边界
- 用户接受异步导入、排队导入；导入时间不是阻塞项
- 本机 GPU 可用：RTX 4080 SUPER 16GB，CUDA 对 torch 可见
- 首期优先级：扫描版/复杂 PDF 能力提升 > 完美保持干净题库 PDF 现状
- 但干净题库 PDF 不可出现“严重退化”

## Testable Acceptance Criteria
### A. 部署与运行
- 本机可稳定启动 MinerU 生产使用所需组件
- StudyAgent 上传 PDF 后，实际走 MinerU 路径而非旧 PDF 文本提取路径
- GPU 在运行链路中可被利用（不是仅安装可见）

### B. PDF 解析能力
- 扫描版 PDF 不再被统一拒绝；可进入 OCR/结构化解析流程
- 复杂版式 PDF 能产出结构化文本、图片引用、必要的层级结构
- 教材类 PDF 能形成用于 RAG 的结构信息与标签信息

### C. 下游统一表示
- PDF 解析结果不直接裸写旧文本；而是进入统一内容对象/中间层
- 该中间层可支撑 chunk 切片、标签化、图片管理三类下游能力

### D. 题库/推荐可用性
- PDF 题目资料在推荐时可准确返回题目与对应图片
- 干净题库 PDF 首期即使局部策略变化，也不能出现严重的题图错配或明显不可用

### E. 非目标保护
- DOCX/TXT 链路行为不被本次改动破坏
- 历史已入库文档不被自动重跑
- 前端无大范围重构依赖
- 切题语义主逻辑仍由 StudyAgent 自己掌控，而非直接外包给 MinerU

## Assumptions Exposed + Resolutions
1. 假设：只接管 PDF 就意味着必须保守兼容。
   - 结论：否。用户更看重扫描版/复杂 PDF 能力提升，只要求干净题库 PDF 不严重退化。
2. 假设：GPU 只带来速度收益。
   - 结论：部分成立。若仍走 pipeline，主要是提速；若允许 hybrid/VLM，则也允许追求更高精度。
3. 假设：必须维持原有内部元数据与存储结构。
   - 结论：否。允许为更强最终功能调整内部契约。

## Pressure-pass finding
对“先接管 PDF”的假设做了反向压力测试后，用户明确选择：
- 首期应优先解决扫描版/复杂 PDF
- 允许干净题库 PDF 有轻微变化
- 但不能严重退化

## Technical Context Findings
- 当前 ingest 主干：`backend/tasks/ingest.py:153-164`
- 当前解析入口：`backend/services/rag_service.py:406-422`
- 当前 question chunking：`backend/services/rag_service.py:645-694`
- 当前 DOCX 抽取与图片管理：`backend/services/rag_service.py:1142-1352`
- 当前 PDF 扫描件拦截：`backend/services/rag_service.py:1532-1679`
- 现有题库计划明确主张“复用当前知识库主链路，而不是重造子系统”：`QUESTION_BANK_PLAN.md:7-24`, `28-46`

## Evidence vs Inference Notes
### Evidence
- 用户明确回答：
  - 首期只接管 PDF
  - 不做 DOCX/TXT 改造、历史重跑、前端大改、MinerU 接管切题
  - 允许改内部元数据/存储契约
  - 接受异步排队导入
  - 允许直接上更高精度 MinerU 路线
- 本机核验：GPU 可用，torch CUDA 可见

### Inference
- “2” 被解释为：优先扫描版/复杂 PDF 能力，而不是保守兼容优先
- “2” 在 backend choice 中被解释为：允许 hybrid/VLM 类更高精度路线

## Recommended Handoff
- **Recommended:** `$ralplan`
- Why: 现在需求已足够清晰，但架构分歧仍然存在（PDF-only parser replacement、MinerU backend choice、统一中间表示、与现有 chunk/tag/image/recommendation 主干的拼接方式），适合先做架构与测试规格共识，再进入实现。
