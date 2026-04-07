# Deep Interview Transcript Summary — question-bank-recommendation-analysis

- Generated: 2026-04-07T03:40:00Z
- Mode: deep-interview (assessment-only)
- Profile: standard
- Context type: brownfield
- Interactive rounds: 0

## Why no interactive rounds
用户提供了一个明确的待评估方案文件，当前任务是判断该方案是否适合作为后续规划输入，而不是继续收集用户需求。因此本次 deep-interview 采用“文档压力测试 + 仓库事实核验”形式完成。

## Assessment flow
1. 读取目标方案：`.omx/specs/question-bank-recommendation-analysis-2026-04-07.md`
2. 核验现有实现：
   - `backend/services/rag_service.py`
   - `backend/services/pdf_parse_bridge.py`
   - `backend/services/vector_store_service.py`
   - `backend/models/schemas.py`
   - 相关测试
3. 进行两轮 pressure pass：
   - Contrarian：挑战“DOCX 仍然是弱结构路径”与“question_item-only 必然更优”两个假设
   - Simplifier：压缩为最小可落地改动，识别第一阶段真正必须实现的契约

## Pressure-pass findings
### Contrarian
- 方案把 DOCX 描述得偏弱；但仓库事实显示 DOCX 已具备题目切分、答案/解析配对、图片资产抽取与 question_item 产出能力。真实差距更像“metadata 契约不完全对齐”，而不是“DOCX 还没进题库后处理层”。
- 方案建议推荐入口尽量只吃 `question_item`，方向是对的，但如果不先定义“切题失败时的召回兜底策略”，会把 ingest 质量问题直接放大为召回缺失问题。

### Simplifier
最小第一阶段不必一次性上齐全部新字段。真正必须先统一的是：
- `chunk_kind/question_number/question_text/answer_text/explanation_text`
- `asset_refs/contains_images/image_count`
- `structure_path`
- 来源定位字段（哪怕先做简化版）
- 一组最小质量标记（不一定一开始就要 quality_score）

## Result
该方案**适合作为下一步 ralplan 输入**，但**还不建议直接进入实现**。缺口主要在：
- 缺少混合旧/新数据兼容策略
- 缺少统一后处理层的落点与边界
- 缺少更可验证的验收标准与排序/召回保护线
