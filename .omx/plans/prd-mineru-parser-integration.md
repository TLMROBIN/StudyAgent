# PRD — MinerU PDF Parser Integration

## Goal
Formally deploy MinerU on this machine as StudyAgent's PDF parser only, then normalize parsed PDF content into a StudyAgent-owned pipeline for chunking, tagging, and image management so question recommendation can return accurate question+image pairs and textbook PDFs gain structure/tags for RAG.

## Scope
- PDF only in phase 1
- DOCX/TXT unchanged
- No historical document replay
- No major frontend rewrite
- MinerU does not own question splitting semantics in phase 1

## Decision
Adopt a two-boundary architecture:
1. **Boundary A**: `mineru_service` returns internal `PDFParseResult`
2. **Boundary B**: `pdf_parse_bridge` converts `PDFParseResult` to `PreparedChunk` + stable asset payloads

## Runtime Decisions
- Config surface in `backend/config.py`
- `PDF_PARSER_BACKEND=legacy|mineru`, default `legacy`
- `MINERU_DEVICE=cuda|cpu`, default `cuda`
- `MINERU_REQUIRE_GPU_PROOF=true` outside tests
- `MINERU_PARSE_TIMEOUT_SECONDS=480`
- `MINERU_PARSE_WARN_SECONDS=240`
- Celery ingest limits raised to `540/600`
- MinerU path fails closed; rollback is explicit config flip back to `legacy`
- Retry owner is Celery task wrapper, not MinerU service internals

## Frozen Compatibility Contract
Phase-1 outputs must preserve:
- `asset_refs`
- `url`, `title`, `description`
- `contains_images`, `image_count`
- `question_text`, `question_number`
- `answer_text`, `explanation_text`
- `chunk_kind`

API-visible assets remain derived from stable `asset_refs`.

## Minimum Internal IR
`PDFParseResult` contains only:
- text blocks
- page/span locality
- asset anchors
- hierarchy hints
- parser provenance

It must not encode question splitting semantics.

## Phases
### Phase 1 — Runtime gate + smoke path
- Add config/feature flag
- Add minimal `PDFParseResult`
- Add MinerU smoke path and runtime artifact proof
- Prove real GPU-backed PDF ingest path
- Implement timeout/retry/cancel/cleanup foundations

### Phase 2 — Full Boundary A
- Normalize MinerU output into `PDFParseResult`
- Model typed parse failures and partial asset handling

### Phase 3 — Boundary B bridge
- Convert `PDFParseResult` into `PreparedChunk`
- Preserve frozen compatibility fields
- Keep StudyAgent-owned question splitting

### Phase 4 — Hardening
- Idempotent cleanup
- Retry/cancel/timeout behavior
- Rollback verification

### Phase 5 — Release evidence
- Fixture-backed regression and contract tests
- Manual comparison set
- Enable mineru by env flip only after acceptance evidence is complete

## Acceptance Criteria
1. PDFs route through feature-flagged MinerU path; DOCX/TXT unchanged
2. Runtime gate proves real GPU use during ingest
3. Raw MinerU payloads do not leak beyond Boundary A
4. A single bridge owns conversion into downstream chunk/assets contracts
5. Frozen compatibility fields remain stable
6. Scanned/complex PDFs that previously failed now ingest successfully
7. Clean question-bank PDFs show no severe regression in question/image/answer-explanation fidelity
8. Textbook PDFs expose usable chapter/section/tag metadata for retrieval
9. Retry/cancel/cleanup/failure semantics are explicit and tested
10. Rollback to legacy parser is config-driven and verified
11. No historical replay path is introduced
12. No major frontend rewrite is required

## Risks
- Global Celery timeout increase could widen blast radius
- Bridge drift could break existing metadata consumers
- MinerU runtime could appear GPU-capable but not actually use GPU without proof gate

## Mitigations
- Keep PDF feature flag explicit
- Add exact fixture-backed bridge and API-payload tests
- Require runtime GPU-proof artifact on real ingest

## Staffing Guidance
### Ralph
- `executor` high: main implementation
- `architect` high: boundary checkpoint
- `test-engineer` high: fixture assertions
- `debugger` high: failure-mode validation
- `verifier` high: release evidence

### Team
- Lane A: config + runtime + GPU-proof + retry/cancel
- Lane B: `PDFParseResult` + bridge + metadata freeze
- Lane C: fixtures/tests + regression evidence

Launch hints:
- `$ralph Implement .omx/plans/ralplan-mineru-parser-integration-v3.md with runtime smoke proof, Boundary A/B contract, exact fixture-backed tests, and config-driven rollback`
- `$team 3:executor "Implement .omx/plans/ralplan-mineru-parser-integration-v3.md with runtime smoke proof, Boundary A/B contract, config-driven rollback, and exact fixture-backed tests"`
