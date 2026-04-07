# Deep Interview Transcript Summary

- Slug: mineru-parser-integration
- Timestamp: 2026-04-06T07:58:20Z
- Profile: standard
- Context: brownfield
- Final ambiguity: 18.4%
- Threshold: 20%

## Context snapshot
- `.omx/context/mineru-parser-integration-20260406T070227Z.md`

## Condensed transcript

### Round 1 — Scope
Q: MinerU 是首日统一所有格式，还是先接管 PDF？
A: 先只接管 PDF；后续统一做切片、标签化、图片管理；题目推荐要给出准确题目和对应图片；教材要有结构和标签方便 RAG。

### Round 2 — Non-goals
Q: 首期明确不做什么？
A: 不做 DOCX/TXT 改造、不重跑已入库文档、不大改前端、不让 MinerU 接管切题。

### Round 3 — Decision boundary
Q: 是否允许调整内部元数据/存储契约？
A: 允许，只要最终功能更强。

### Round 4 — Pressure pass / tradeoff
Q: 若扫描/复杂 PDF 变好，但干净题库 PDF 推荐/题图配对有所下降，优先哪边？
A: 选 2 —— 优先把扫描版/复杂版 PDF 做起来，只要干净题库 PDF 不出现严重退化即可接受。

### Round 5 — Feasibility
Q: 是否接受 PDF 导入统一走异步任务、允许更慢但更强？
A: 接受，导入时间不是问题，可以排队导入。
Follow-up: 本机 GPU 是否可用，是否更快更准？
Evidence: 本机 `RTX 4080 SUPER 16GB`；`torch.cuda.is_available() == True`。
Conclusion: GPU 能明确提速；若采用 hybrid/VLM 路线，可能一起提升准确度，但复杂度更高。

### Round 6 — Backend choice
Q: 首期锁定 `pipeline` 还是允许直接上 `hybrid-auto-engine` / `vlm-auto-engine`？
A: 选 2 —— 允许直接上更高精度路线。

## Pressure-pass finding
- Revisited an earlier assumption: “先接管 PDF” does not mean “先保持最稳兼容”; user explicitly prioritized improved scanned/complex PDF handling over perfect preservation of clean question-bank PDF behavior, as long as there is no severe regression.

## Brownfield evidence
- Ingest entry: `backend/tasks/ingest.py:153-164`
- Parser entry: `backend/services/rag_service.py:406-422`
- Question chunking: `backend/services/rag_service.py:645-694`
- DOCX extraction: `backend/services/rag_service.py:1142-1352`
- Scanned-PDF rejection: `backend/services/rag_service.py:1532-1679`
- Plan baseline saying OCR was out of scope in phase 1: `DEVELOPMENT_PLAN.md:237-239`
- Question-bank plan favoring reuse of current knowledge pipeline: `QUESTION_BANK_PLAN.md:7-24`, `28-46`
