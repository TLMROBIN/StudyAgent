# Execution-ready Assessment Spec: Question Bank Recommendation Analysis

## Metadata
- Slug: question-bank-recommendation-analysis
- Generated: 2026-04-07T03:40:00Z
- Profile: standard
- Context type: brownfield
- Final ambiguity: 19.5%
- Threshold: 20%
- Context snapshot: `.omx/context/question-bank-recommendation-analysis-20260407T034000Z.md`
- Transcript: `.omx/interviews/question-bank-recommendation-analysis-20260407T034000Z.md`
- Source plan: `.omx/specs/question-bank-recommendation-analysis-2026-04-07.md`

## Clarity breakdown
| Dimension | Score |
|---|---:|
| Intent | 0.92 |
| Outcome | 0.84 |
| Scope | 0.88 |
| Constraints | 0.63 |
| Success | 0.56 |
| Context | 0.92 |

## Assessment verdict
**结论：方向正确，适合进入 `$ralplan`，但还不是可直接编码的实现规格。**

这份方案已经把核心问题从“换 PDF 解析器”收敛到了“统一题库后处理层”，这一判断与当前代码事实基本一致；同时它也准确识别了推荐侧对 `question_item`、图片绑定和资源类型过滤的依赖。

但它仍缺少几项实现前必须补清的东西：
1. **兼容边界不够明确**：本轮不重跑历史文档，意味着推荐链路必须兼容旧 metadata 与新 metadata 混用；方案尚未定义这个兼容策略。
2. **统一后处理层的落点不够明确**：是扩展 `rag_service`、抽新 service，还是让 `pdf_parse_bridge`/DOCX 共用一个下游 builder，尚未落图。
3. **验收条件不够可测**：当前“更稳”“更可信”这类目标多，量化保护线少，容易导致实现时漂移。

## Intent
用户想确认：题库推荐质量问题的主瓶颈究竟是不是“解析器”，还是更下游的题目结构化/图片绑定/推荐过滤契约；并在进入实施前，先得到一份更接近真实代码现状的评估。

## Desired Outcome
- 明确该方案哪些判断已被当前仓库事实支持。
- 指出哪些判断过于绝对、需要修正或细化。
- 判断该方案是否足以作为下一步规划输入。
- 输出建议的下一步 handoff，而不是直接编码。

## In Scope
- 评估题库推荐方案与现有实现的一致性
- 识别缺失的决策边界 / 非目标 / 兼容性约束 / 验收标准
- 给出后续 planning handoff 建议

## Out-of-Scope / Non-goals
- 不直接修改推荐逻辑
- 不直接重构 PDF / DOCX ingest 流程
- 不直接新增 metadata 字段
- 不直接改 API / 前端展示

## Decision Boundaries
以下判断已足够明确，可作为下一步规划前提：
- 题目推荐应继续只面向 `exercise/question_set`
- MinerU 应继续只做 PDF 解析，不接管题目语义切分
- 无图题不应被系统性降权
- 图片加分应只在 query 明显带图题意图时触发

以下边界在实施前仍需补清：
- 推荐入口是否 **完全** 切到 `question_item-only`，还是保留切题失败时的兜底召回
- 新 metadata 字段中，哪些只做内部诊断，哪些要进 vector store / API schema / metadata preserve keys
- 历史题库不重跑的前提下，混合数据期如何保证推荐不明显退化

## Constraints
- 当前代码已经部分具备统一契约雏形：PDF 和 DOCX 都能产出 `question_item`、答案/解析、图片资产引用。
- `sync_document_metadata()` 只保留 `QUESTION_METADATA_PRESERVE_KEYS` 中声明的字段；新字段若不补这里，会在同步流程中丢失。
- `vector_store_service` 目前只索引少量 metadata；新增质量/图像状态字段默认不会进入向量过滤层。
- API schema (`KnowledgeChunkRead` / `QuestionRecommendationRead`) 目前也未暴露计划中的新字段。
- `omx explore` 在本机会话不可用，本次结论来自直接代码核验。

## Testable acceptance criteria for the plan itself
要把该方案升级成可实现规格，至少需要补齐以下可测条件：
1. **召回保护线**：当新文档已按新契约入库时，题目推荐默认只优先选 `question_item`；但必须定义旧文档或切题失败时的兜底策略与触发条件。
2. **兼容性规则**：明确“不重跑旧文档”情况下，推荐、列表页、聊天输出对旧 metadata 的容忍方式。
3. **字段分层**：把新字段分成三层：
   - ingestion-only
   - retrieval/filtering-needed
   - API-exposed
4. **质量规则**：至少定义一组 deterministic 规则，说明何时打 `missing_required_image`、何时仅记 warning、不阻断入库。
5. **测试矩阵**：除文档中已有场景外，再补“旧数据 + 新数据混合推荐”“question_item 缺失时兜底召回”“metadata sync 不丢字段”三个回归场景。

## Assumptions exposed + resolutions
1. **假设：DOCX 目前还没真正进入题库后处理层。**
   - 结论：不准确。DOCX 已通过 `_prepare_question_chunks()` 进入题目结构化路径；真实问题是与 PDF 的 metadata 契约仍未完全对齐。
2. **假设：只推荐 `question_item` 一定会提升质量。**
   - 结论：方向成立，但前提是切题稳定性足够高，且定义了失败兜底，否则会伤召回。
3. **假设：新增很多 metadata 字段本身就能提升推荐效果。**
   - 结论：不一定。若这些字段既不参与过滤/排序，也不进入 API/诊断链路，它们只会增加维护面。

## Pressure-pass finding
- 经 Contrarian 检查，方案对 DOCX 现状略有低估，应从“补齐契约”而不是“重建 DOCX 路径”来表述第一阶段目标。
- 经 Simplifier 检查，第一阶段最该统一的是 **question_item 基础契约 + 来源定位 + 最小质量标记**；`quality_score`、复杂标签体系可以后置。

## Technical Context Findings
- 推荐入口与过滤：`backend/services/rag_service.py:222-260`, `1095-1148`
- DOCX 解析与图片抽取：`backend/services/rag_service.py:1203-1415`
- PDF 解析后桥接与 question_item 构建：`backend/services/pdf_parse_bridge.py:1-240`
- metadata 保留边界：`backend/services/rag_service.py:106-123`, `350-379`
- 向量 metadata 建模：`backend/services/vector_store_service.py:126-140`
- API schema 暴露边界：`backend/models/schemas.py:164-210`
- DOCX question_item + 图片/答案配对测试：`tests/test_rag.py:600-677`

## Evidence vs Inference Notes
### Evidence
- `_is_question_row()` 当前确实接受 `chunk_kind in {None, "", "question_item"}`。
- `_question_recommendation_bonus()` 仅在 query 出现图题信号时对含图题加分。
- PDF 的 MinerU 路径目前由 bridge 在 StudyAgent 内做 chunking / metadata 组装，而非把切题外包给解析器。
- DOCX 当前已具备 question_item、答案/解析和图片绑定能力。

### Inference
- “统一题库后处理层” 更合理的第一步是抽象公共 question-item builder，而不是重写 DOCX 入口。
- `quality_score` 应延后，先落 deterministic `quality_flags` / image binding status 更稳。

## Recommended Handoff
- **Recommended:** `$ralplan`
- Why: 方案已经足够清晰，可进入架构/测试规格细化；但兼容策略、字段分层、召回兜底与验收保护线仍需在 planning 阶段定稿。

## Suggested focus for ralplan
1. 给出“统一题库后处理层”的落点图与调用边界。
2. 定义 mixed-data 兼容策略（旧数据 vs 新数据）。
3. 把新增字段按 ingestion / retrieval / API 三层拆分。
4. 定义 `question_item-only` 与 fallback 的切换规则。
5. 把测试矩阵转成具体 test-spec。
