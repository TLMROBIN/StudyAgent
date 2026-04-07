# Question-Bank Recommendation Phase 1 Notes

> This file is the implementation-facing source of truth for the Phase 1 rollout defined in `.omx/plans/ralplan-question-bank-recommendation-analysis-v2.md`.
> It exists to keep code review, implementation, and verification aligned without editing `.omx/plans/*` artifacts.

## Why this note exists

The long-range `QUESTION_BANK_PLAN.md` explores a broader question-bank architecture, including `QuestionMeta`, backfill flows, and future scoring ideas.
This rollout is intentionally narrower:

- question-bank resources only (`exercise` / `question_set`)
- PDF and DOCX alignment only on the question-bank ingest path
- recommendation quality improvements must preserve mixed-data recall
- textbook recommendation, TXT scope, historical replay, and `quality_score` are out of scope for Phase 1

Use this file when reviewing or modifying the Phase 1 implementation.

## Ownership boundaries

### Parser / extractor responsibilities stay format-local
- PDF structure + assets remain in `backend/services/pdf_parse_bridge.py`
- DOCX text / asset extraction remains in `backend/services/rag_service.py`

### Recommendation-facing semantics stay StudyAgent-owned
The Phase 1 rollout should converge the persisted question-bank metadata contract used by:
- `backend/services/rag_service.py`
- `backend/routers/chat.py`
- `backend/routers/knowledge.py`
- regression coverage in `tests/test_rag.py`, `tests/test_chat_stream.py`, and `tests/test_knowledge_management.py`

Review rule: keep parser-specific extraction concerns separate from question-unit semantics.
Do not couple MinerU or DOCX parsing details to recommendation policy.

## Phase 1 metadata contract

### Core question metadata
These fields define the question-unit contract recommendation code can rely on:

- `chunk_kind`
- `question_number`
- `question_text`
- `answer_text`
- `explanation_text`
- `asset_refs`
- `contains_images`
- `image_count`
- `chapter`
- `section`
- `structure_path`

### Internal diagnostics / provenance metadata
These fields stay internal in Phase 1, but must be preserved during metadata sync if introduced:

- `source_format`
- `source_locator`
- `parser_backend`
- `parser_provenance`
- `image_expectation`
- `image_binding_status`
- `quality_flags`
- `question_uid`

### Explicit Phase 1 constraints
- No `quality_score`
- No textbook recommendation participation
- No TXT ingestion changes
- No mandatory vector-metadata expansion unless a concrete filter need appears
- No public API expansion for diagnostic-only fields unless a caller truly needs them

## Recommendation rollout rules

1. Keep recommendation corpus filtering limited to `exercise` and `question_set`.
2. Prefer rows with `chunk_kind == "question_item"`.
3. Use legacy non-`question_item` question-bank rows only as explicit fallback when structured rows are absent or insufficient.
4. If a document already has `question_item` rows, suppress blank / legacy rows from that same document in normal recommendation results.
5. Preserve the current image-bonus philosophy:
   - image-backed questions can receive extra weight when the query clearly asks for image-based practice
   - text-only questions must not receive a default no-image penalty
6. Keep required-image diagnostics deterministic (`image_expectation`, `image_binding_status`, `quality_flags`) rather than introducing composite scoring.

## Public API expectations

Phase 1 keeps the existing response shape for recommendation and knowledge surfaces.
The stable public fields are:

- question text / number
- answer / explanation when the caller is allowed to see them
- image presence, image count, and asset payloads
- existing document metadata already exposed by serializers

Diagnostic-only fields such as `quality_flags`, `image_binding_status`, and `question_uid` should remain internal until a concrete admin/debug surface is approved.

## Review checklist

Use this checklist before merging question-bank recommendation changes:

- [ ] parser/extractor logic is still separated from question-bank semantics
- [ ] newly introduced metadata keys are either preserved intentionally or kept out of sync paths on purpose
- [ ] recommendation logic distinguishes preferred `question_item` rows from explicit legacy fallback
- [ ] same-document duplicate suppression is covered when structured rows exist
- [ ] textbooks are still excluded from recommendation
- [ ] no `quality_score` appears in Phase 1 code or docs
- [ ] API responses stay backward compatible

## Verification checklist

Suggested evidence commands for this rollout:

```bash
pytest tests/test_rag.py -k "question or recommend"
pytest tests/test_chat_stream.py -k recommend_questions
pytest tests/test_knowledge_management.py -k "question or preserves"
python -m compileall backend tests
```

If implementation touches recommendation serializers or metadata sync, include the command output in the task handoff.

## Documentation note

`QUESTION_BANK_PLAN.md` remains a broader roadmap document.
For this rollout, if it conflicts with this note, follow the narrower Phase 1 contract here and the `.omx/plans/` PRD / test-spec artifacts.
