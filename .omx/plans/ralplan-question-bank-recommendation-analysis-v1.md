# Question-Bank Recommendation Optimization — Consensus Plan v1

## Context and Evidence
- Input spec: `.omx/specs/deep-interview-question-bank-recommendation-analysis.md`
- Source analysis: `.omx/specs/question-bank-recommendation-analysis-2026-04-07.md`
- Context snapshot: `.omx/context/question-bank-recommendation-analysis-20260407T034000Z.md`
- Current recommendation path lives in `backend/services/rag_service.py::recommend_questions()`.
- Current recommendation filtering already limits to `exercise/question_set`, but `_is_question_row()` still accepts `chunk_kind in {None, "", "question_item"}`.
- Current image bonus is query-conditional only; no systemic no-image penalty exists.
- PDF already has a parser-to-bridge separation via `backend/services/pdf_parse_bridge.py`; MinerU should remain parser-only.
- DOCX already reaches the question chunking path with question splitting, answer/explanation pairing, and image extraction.
- Metadata preservation currently depends on `QUESTION_METADATA_PRESERVE_KEYS`; new fields must be explicitly preserved.
- Vector metadata indexing is intentionally narrow today; phase 1 should not assume all new fields belong in retrieval filters.
- Binding constraints: no frontend interaction overhaul, no historical reingest/replay, no TXT work, textbooks stay out of question recommendation, DOCX alignment is only for question-bank path.

---

## 1) RALPLAN-DR Summary

### Principles
1. **Unify semantics, not parsers**: PDF and DOCX should converge at a StudyAgent-owned question-bank post-processing layer, while parser-specific extraction stays format-local.
2. **Prefer stable question units, preserve safe fallback**: recommendation should prefer `question_item`, but phase 1 must protect recall for legacy or imperfectly segmented data.
3. **Model only what phase 1 can use**: add deterministic metadata that directly supports ingest correctness, recommendation safety, or diagnostics; defer decorative fields and speculative scoring.
4. **Keep rollout reversible**: no historical replay, no frontend dependency, and no mandatory vector-schema expansion in phase 1.
5. **Textbooks remain separate**: textbook structure/tagging can reuse metadata patterns but must not enter the question recommendation corpus.

### Top Decision Drivers
1. **Recommendation quality uplift without recall collapse**
2. **Cross-format consistency for question-bank ingest metadata**
3. **Low-risk rollout under mixed old/new stored data**

### Viable Options

#### Option A — Patch current `rag_service` branches directly
- **Pros**: smallest immediate diff; lower module churn
- **Cons**: duplicates PDF/DOCX business rules longer-term; makes mixed-data logic harder to reason about; keeps semantics coupled to format-specific branches

#### Option B — Introduce a shared question-bank post-processor used by both PDF and DOCX question-bank paths **(preferred)**
- **Pros**: creates one owner for question-unit assembly, image expectation/binding checks, and quality flags; aligns PDF/DOCX without large multi-format refactor; supports phased fallback rules cleanly
- **Cons**: moderate service refactor; requires careful compatibility seams in `rag_service` and `pdf_parse_bridge`

#### Option C — Full ingestion architecture rewrite across PDF/DOCX/TXT/textbook paths now
- **Pros**: theoretically cleanest long-term abstraction
- **Cons**: over-scoped, violates current non-goals, raises regression risk on unaffected paths

### Preferred Option and Why It Wins
**Option B** is the narrowest approach that solves the real problem: inconsistent question-unit post-processing. It preserves current parser boundaries, avoids rewriting DOCX extraction, and gives recommendation a single contract to trust while still allowing mixed-data fallback during rollout.

---

## 2) Recommended Architecture

### Decision
Add a StudyAgent-owned **question-bank post-processing layer** for question-bank resources only. This layer should sit **after format-specific extraction/parsing** and **before final recommendation-facing chunk metadata is persisted**.

### Proposed ownership boundary
- **Format-specific extraction stays where it is now**
  - PDF parse structure/assets: `pdf_parse_bridge.py` + parser adapters
  - DOCX text/assets extraction: `rag_service.py`
- **New shared post-processing owner**
  - Suggested module: `backend/services/question_bank_post_processor.py`
  - Suggested companion types: `backend/services/question_bank_types.py` or internal dataclasses in the same module
- **Recommendation stays in `rag_service.py`**, but consumes a tighter, more explicit contract from persisted metadata

### Phase-1 post-processing responsibilities
The shared layer should own:
1. Normalizing extracted question candidates into stable `question_item` units
2. Filling/normalizing these core fields:
   - `chunk_kind`
   - `question_number`
   - `question_text`
   - `answer_text`
   - `explanation_text`
   - `asset_refs`
   - `contains_images`
   - `image_count`
   - `structure_path`
   - `source_format`
   - `source_locator`
   - `parser_backend`
3. Deterministic image analysis:
   - `image_expectation = not_needed | optional | required`
   - `image_binding_status = bound | missing_required | none_needed | optional_unbound`
4. Minimal quality diagnostics:
   - `quality_flags` as string list, not numeric score in phase 1
5. Stable per-document question identity:
   - `question_uid` derived from document id + source locator/question number, for dedupe/diagnostics only in phase 1

### Explicit non-goals for the shared layer
- No textbook recommendation participation
- No TXT support changes
- No model-based scoring/classification requirement in phase 1
- No mandatory vector-filter participation for all new fields
- No frontend contract expansion unless a field is already needed by existing surfaces

---

## 3) Mixed-Data Compatibility Strategy

### Problem
Historical rows will remain in storage without the new metadata contract because historical reingest is out of scope.

### Plan
Adopt a **two-tier recommendation strategy** during rollout:

#### Tier 1 — Preferred rows
Rows qualify as preferred recommendation candidates when:
- resource type is `exercise/question_set`, and
- `chunk_kind == "question_item"`

#### Tier 2 — Legacy fallback rows
Rows qualify as fallback candidates only when:
- they are in question-bank resource types, and
- they fail preferred qualification but still represent legacy question-bank content

### Fallback rule
Use fallback rows **only when preferred rows are insufficient** for the requested limit or when a document has no `question_item` rows yet.

### Duplicate suppression rule
If a document already contains one or more `question_item` rows, suppress blank/legacy chunk candidates from that same document in recommendation results unless explicitly running a debug/admin path.

### Rollout effect
- New or corrected docs get cleaner recommendations immediately.
- Old docs remain available until they are naturally reimported later.
- Recall loss is bounded by explicit fallback rather than accidental permissiveness.

---

## 4) Metadata Layering Decision

### A. Persisted ingest-only / diagnostics fields
Keep in `metadata_json`, preserve through sync, but do **not** index in vector store or expose in user APIs in phase 1 unless needed for internal admin/debugging:
- `source_format`
- `source_locator`
- `parser_backend`
- `parser_provenance`
- `structure_path`
- `image_expectation`
- `image_binding_status`
- `quality_flags`
- `question_uid`

### B. Retrieval/filtering-relevant fields
Existing or phase-1-usable fields that can affect recommendation selection/ranking:
- `chunk_kind`
- `question_number`
- `question_text`
- `contains_images`
- `asset_refs`
- `image_count`
- `chapter`
- `section`

**Phase-1 indexing guidance:**
- keep current vector scalar indexing as-is unless a concrete filter need appears
- do not add `quality_flags` or `image_binding_status` to vector metadata in phase 1 by default

### C. API-exposed fields in phase 1
Keep public surfaces minimal and compatible:
- existing question fields
- existing image presence/count/assets fields
- no requirement to expose `quality_flags`, `image_expectation`, or `question_uid` on current user-facing APIs in phase 1

### Required preservation work
Any field needed after metadata sync must be added to `QUESTION_METADATA_PRESERVE_KEYS`; otherwise rollout is incomplete.

---

## 5) Deterministic Quality and Image Model (Phase 1)

### Image expectation rules
Use simple rule-based detection on `question_text` only. Examples of `required` triggers:
- `如图`
- `下图`
- `图示`
- `图中`
- `根据图像`
- `看图`
- `电路图`
- `受力图`
- `几何图形`
- `装置图`

If no such cue exists:
- mark `not_needed` by default
- allow `optional` only for a bounded rule set where images help but are not required

### Image binding status rules
- `bound`: `required|optional` and one or more assets are attached appropriately
- `missing_required`: `image_expectation == required` and no assets bound
- `none_needed`: `image_expectation == not_needed`
- `optional_unbound`: optional image expectation with no asset

### Minimum `quality_flags`
Phase 1 should start with deterministic flags only:
- `legacy_fallback_chunk`
- `missing_required_image`
- `multi_question_suspected`
- `answer_pairing_suspected`
- `empty_question_text`

### Explicit deferral
Do **not** add a composite `quality_score` in phase 1. Scores hide rule causes and create ranking debates before deterministic diagnostics are stable.

---

## 6) Recommendation Logic Plan

### Recommendation selection changes
1. Keep resource-type filtering unchanged: only `exercise/question_set`
2. Tighten `_is_question_row()` semantics into two explicit paths:
   - preferred `question_item`
   - controlled legacy fallback
3. Keep current image bonus philosophy:
   - no default no-image penalty
   - add image bonus only when query clearly expresses image intent
4. Add optional safety penalty for `missing_required_image` when ranking rows that otherwise qualify, but only after fallback logic is in place

### Recommendation output compatibility
Do not change existing response shape in phase 1. Continue using current fields returned by chat/knowledge recommendation outputs.

---

## 7) Concrete Implementation Phases

### Phase 1 — Contract definition and compatibility seam
- Define the shared question-bank post-processing contract and metadata schema
- Add preservation keys for required new metadata
- Document fallback semantics before code changes

**Exit criteria**
- Contract fields and rollout rules are frozen in PRD/test spec
- No unresolved ambiguity remains around fallback or metadata layering

### Phase 2 — Shared post-processor extraction
- Create the shared post-processing owner for question-bank resources
- Route DOCX question-bank preparation through it without rebuilding DOCX extraction
- Route PDF question-bank bridge output through the same post-processing rules

**Exit criteria**
- PDF and DOCX question-bank rows produce the same core metadata contract
- Existing answer/explanation/image behavior is preserved for passing fixtures/tests

### Phase 3 — Recommendation tightening with fallback
- Update recommendation filtering/ranking to prefer `question_item`
- Add mixed-data fallback and same-document duplicate suppression
- Preserve current image bonus semantics

**Exit criteria**
- New-format rows are preferred
- Legacy rows still backfill when needed
- No obvious recall cliff on mixed datasets

### Phase 4 — Diagnostics and image-binding rules
- Add deterministic image expectation/binding evaluation
- Add minimal quality flags
- Keep these internal unless API exposure is proven necessary

**Exit criteria**
- `missing_required_image` and related diagnostics are test-backed
- No composite score is introduced yet

### Phase 5 — Regression hardening
- Expand regression coverage for ingest, metadata sync, recommendation, and serializers
- Verify old/new mixed behavior and textbook exclusion remain stable

**Exit criteria**
- Automated tests cover all agreed scenarios
- No frontend rewrite is required
- No historical replay path is introduced

---

## 8) ADR

### Decision
Implement a shared StudyAgent-owned question-bank post-processing layer for PDF and DOCX question-bank paths, and tighten recommendation to prefer `question_item` with explicit legacy fallback.

### Drivers
- Current quality bottleneck is post-processing consistency, not parser replacement alone.
- PDF and DOCX already share much of the downstream question metadata shape.
- Historical reingest is out of scope, so mixed-data compatibility must be explicit.

### Alternatives considered
1. **Directly patch existing `rag_service` branches only**
   - Rejected because it prolongs duplicated semantics and obscures fallback rules.
2. **Full ingestion architecture rewrite now**
   - Rejected because it violates current scope and increases regression surface.

### Why chosen
This approach centralizes question-unit semantics where the actual problem exists while staying narrow enough for a safe rollout.

### Consequences
- Moderate service refactor in ingest preparation paths
- New metadata preservation responsibilities
- Recommendation logic becomes more explicit and testable
- Some planned metadata stays internal until proven useful externally

### Follow-ups
- Consider exposing selected diagnostics to admin surfaces later
- Consider adding quality scoring only after deterministic flags prove stable
- Revisit vector metadata indexing only if retrieval filters demonstrate a need

---

## 9) Acceptance Criteria

1. PDF and DOCX question-bank ingest paths produce the same core `question_item` metadata contract for newly imported question-bank documents.
2. Recommendation continues to exclude textbooks from question recommendations.
3. Recommendation prefers `question_item` rows for new-format docs.
4. Legacy non-`question_item` rows are used only as explicit fallback, not as co-equal primary candidates.
5. If a document contains `question_item` rows, legacy blank-kind chunks from that same document are suppressed in recommendation results.
6. No default ranking penalty is introduced for no-image questions.
7. Queries with explicit image intent still boost image-backed questions.
8. Required-image questions with missing assets are diagnosable via deterministic metadata.
9. Metadata sync preserves all newly required internal fields.
10. Existing chat/knowledge recommendation response shapes remain compatible in phase 1.
11. No TXT path changes are introduced.
12. No historical replay path is introduced.
13. No frontend interaction overhaul is required.

---

## 10) Test Spec Summary

### Automated areas
1. **DOCX/PDF contract parity**
   - same core metadata keys for representative question-bank fixtures
2. **Mixed-data recommendation behavior**
   - new `question_item` rows preferred
   - legacy fallback activates only when needed
   - same-document legacy suppression works
3. **Image expectation/binding diagnostics**
   - text-only question => no penalty, `none_needed`
   - required-image question with asset => `bound`
   - required-image question without asset => `missing_required`
4. **Metadata preservation**
   - `sync_document_metadata()` does not drop new required keys
5. **Surface compatibility**
   - knowledge/chat serializers still produce stable existing payloads
6. **Corpus boundary**
   - textbook rows do not enter recommendation set

### Manual checks
- Compare a PDF question bank and a DOCX question bank containing both image and non-image questions
- Verify recommended results do not regress into blank legacy chunks when structured rows exist
- Verify a legacy-only dataset still returns results rather than none

---

## 11) Available Agent Types / Suggested Staffing

### Available agent types most relevant here
- `architect`
- `executor`
- `test-engineer`
- `debugger`
- `verifier`
- `code-reviewer`
- `writer`

### Ralph staffing guidance
- `executor` — **high**: implement shared post-processor + recommendation fallback seam
- `architect` — **high**: checkpoint the module boundary and fallback design before merge
- `test-engineer` — **medium/high**: expand regression matrix and metadata-sync coverage
- `debugger` — **medium**: mixed-data edge cases and duplicate suppression failures
- `verifier` — **high**: acceptance-criteria evidence and serializer compatibility proof

### Team staffing guidance
- **Lane A / executor / high**: shared post-processor and metadata contract
- **Lane B / executor or debugger / high**: recommendation fallback + duplicate suppression
- **Lane C / test-engineer / high**: regression tests, metadata-sync tests, serializer checks
- **Lane D / verifier / medium**: final evidence pass and acceptance checklist

### Team launch hints
- `$team .omx/plans/ralplan-question-bank-recommendation-analysis-v1.md`
- `omx team "Implement .omx/plans/ralplan-question-bank-recommendation-analysis-v1.md with shared question-bank post-processing, mixed-data fallback, and regression coverage"`

### Ralph launch hint
- `$ralph .omx/plans/ralplan-question-bank-recommendation-analysis-v1.md`

### Team verification path
1. Lane C lands regression tests first or in lockstep with implementation.
2. Lane D validates acceptance criteria against test outputs and spot-checks mixed-data behavior.
3. Final verifier confirms: no textbook leakage, no no-image penalty regression, metadata preservation intact, serializers stable.

---

## 12) Assumptions
- Existing DOCX fixtures are representative enough to reuse when refactoring toward a shared post-processor.
- A simple rule-based image expectation model is sufficient for phase 1.
- Current API surfaces do not need to expose new diagnostic fields immediately.
