# PRD — Question-Bank Recommendation Optimization

## Goal
Improve question recommendation quality by unifying PDF and DOCX question-bank post-processing into a shared StudyAgent-owned layer that produces stable `question_item` metadata, explicit image-binding diagnostics, and mixed-data-safe recommendation behavior.

## Scope
- Question-bank resources only (`exercise/question_set`)
- PDF and DOCX alignment on the question-bank path
- Recommendation tightening toward `question_item`
- Mixed old/new data compatibility
- Deterministic image expectation/binding diagnostics
- Regression and metadata-preservation coverage

## Non-goals
- No frontend interaction overhaul
- No historical reingest/replay
- No TXT path work
- Textbooks do not enter question recommendation
- No composite quality scoring in phase 1
- No broad vector-metadata expansion without proven need

## Product Decisions
1. Introduce a shared question-bank post-processing layer after format-specific extraction.
2. Keep parser ownership separate from question semantics.
3. Prefer `question_item` in recommendation, but retain explicit fallback for legacy rows.
4. Keep image bonus query-conditional only; do not penalize text-only questions by default.
5. Add deterministic image/quality diagnostics as internal metadata first.

## Required Metadata Contract (phase 1)
### Core recommendation fields
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

### Internal diagnostics fields
- `source_format`
- `source_locator`
- `parser_backend`
- `parser_provenance`
- `image_expectation`
- `image_binding_status`
- `quality_flags`
- `question_uid`

## Rollout Rules
1. New imports use the unified contract.
2. Historical rows remain valid but are fallback-only when structured rows exist.
3. Same-document legacy blank-kind rows are suppressed if structured `question_item` rows exist.
4. Textbooks stay excluded from recommendation.

## Acceptance Criteria
1. New PDF and DOCX question-bank imports emit the same core question metadata shape.
2. Recommendation results prefer `question_item` rows when present.
3. Legacy rows still backfill when structured rows are absent or insufficient.
4. Image-intent queries boost image-backed questions without penalizing no-image questions by default.
5. Required-image questions missing assets are tagged deterministically.
6. Metadata sync preserves all newly required keys.
7. Existing recommendation/chat/knowledge payloads stay compatible.
8. No frontend, TXT, or historical replay scope is introduced.

## Risks
- Over-tightening recommendation may reduce recall on legacy datasets.
- New metadata may silently disappear if preservation is incomplete.
- Shared post-processing extraction may accidentally regress existing DOCX/PDF answer/image behavior.

## Mitigations
- Explicit legacy fallback tier
- Regression tests for metadata sync and mixed-data recommendation
- Keep phase 1 diagnostics deterministic and internal

## Staffing Guidance
### Ralph
- `executor` high
- `architect` high
- `test-engineer` medium/high
- `verifier` high

### Team
- Lane A: shared post-processor + metadata contract
- Lane B: recommendation fallback + duplicate suppression
- Lane C: regression tests + serializer compatibility
- Lane D: verification/evidence
