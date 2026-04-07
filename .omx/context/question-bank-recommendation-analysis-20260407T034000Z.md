# Context Snapshot — question-bank-recommendation-analysis

- Generated: 2026-04-07T03:40:00Z
- Task statement: 评估 `.omx/specs/question-bank-recommendation-analysis-2026-04-07.md` 的可执行性与需求清晰度。
- Desired outcome: 判断该方案是否已达到可进入规划/实施的清晰度，并指出与当前代码事实不一致、缺失或风险较高的部分。
- Stated solution: 用户要求以 `$deep-interview` 方式评估现有方案文档，而不是直接实施。
- Probable intent hypothesis: 在进入下一步 `$ralplan` 或实现前，先确认方案是否抓住了真正瓶颈，以及是否遗漏关键边界/验收条件。

## Known facts / evidence
- 题目推荐入口存在于 `backend/services/rag_service.py::recommend_questions()`，且先做 `QUESTION_RESOURCE_TYPES` 过滤，再用 `_is_question_row()` 过滤。
- `_is_question_row()` 当前允许 `chunk_kind in {None, "", "question_item"}`。
- `_question_recommendation_bonus()` 当前只在 query 明显包含图题信号时才给含图题额外加分，不会系统性惩罚无图题。
- PDF 的 MinerU 接入已经通过 `backend/services/pdf_parse_bridge.py` 让解析与下游 question chunking 分离；MinerU 并未直接接管切题语义。
- DOCX 当前并非“仅纯文本弱结构”：`backend/services/rag_service.py::_extract_docx_content()` 已抽取图片资产，`_prepare_question_chunks()` 已能对 DOCX 题库生成 `question_item`、答案/解析配对与图片绑定；测试见 `tests/test_rag.py::test_prepare_question_chunks_keeps_docx_images_and_pairs_answers`。
- 现有 metadata 持久保留键位于 `QUESTION_METADATA_PRESERVE_KEYS`；新增字段若不加入此集合，调用 `sync_document_metadata()` 时可能被丢失。
- 向量库目前仅索引少量标量字段：`resource_type/grade/chapter/section/difficulty/chunk_kind/question_number`。

## Constraints
- 本轮是方案评估，不直接改代码。
- 需遵循 deep-interview 的 artifact 输出约定。
- `omx explore` 当前不可用（本机缺少 cargo / prebuilt harness），因此改用直接仓库检查。

## Unknowns / open questions
- 统一“题库后处理层”应落在 `rag_service`、新 service，还是扩展 `pdf_parse_bridge`/DOCX pipeline。
- 对历史已入库但没有新 metadata 的题库，推荐链路是否要兼容混合数据状态。
- `quality_score` / `image_binding_status` 是否只做内部诊断，还是参与检索/排序。

## Decision-boundary unknowns
- 是否允许在题目切分失败时保留“普通段落召回兜底”，还是强制 question_item-only。
- 新字段哪些只进 metadata，哪些需要暴露到 API / vector store / DB 同步链路。
- 图题检测是规则优先还是允许后续模型判别。

## Likely codebase touchpoints
- `backend/services/rag_service.py`
- `backend/services/pdf_parse_bridge.py`
- `backend/services/vector_store_service.py`
- `backend/models/schemas.py`
- `backend/routers/chat.py`
- `backend/routers/knowledge.py`
- `tests/test_rag.py`
- `tests/test_knowledge_management.py`
