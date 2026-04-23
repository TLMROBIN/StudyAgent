# QUESTION_CHUNK_RECONCILIATION_PLAN (Approval-Ready Revision)

## 1. Scope / Non-goals

### Scope (this plan)
- Target path: **question-resource non-PDF ingestion path only** (`resource_type in {exercise, question_set}` via `RagService._prepare_question_chunks`).
- Execution order is fixed: **Stage-A reconciliation first, Stage-B upstream split logic second**.
- Preferred reconciliation key remains: **`(question_number, group_id)`**, where `group_id` is **transient in-memory only**.
- Required touchpoints:
  - `backend/services/rag_service.py`
  - `backend/services/question_bank_post_processor.py`
  - `backend/tasks/ingest.py`
  - `scripts/audit_question_slices.py`
  - `scripts/backfill_question_slices.py`
  - `tests/test_rag.py`, `tests/test_ingest.py`

### Non-goals (explicit)
- No behavior change for **PDF-specific parsing path** in `backend/services/pdf_parse_bridge.py`.
- No behavior change for non-question resources (`knowledge_note`, textbook-like paragraph chunking).
- No retrieval/rerank strategy redesign.
- No schema migration and no persisted `group_id` field.

## 2. Principles / Drivers / Options

### RALPLAN-DR

#### Principles
1. Deterministic reconciliation over heuristic post-hoc patching.
2. Preserve existing external behavior when no collision/repetition exists.
3. Keep data lineage explainable (rebuild from canonical fields, avoid raw content concat).
4. Roll forward safely with audit + canary + reversible backfill.

#### Top Drivers
1. Duplicate `question_number` chunks cause wrong completion counts and unstable retrieval candidates.
2. Current `source_locator=question:{number}` can collide when same number appears in different groups.
3. Need low-risk stop-loss before modifying upstream split heuristics.

#### Options (fair comparison)

| Option | Summary | Pros | Cons | Decision |
|---|---|---|---|---|
| A | Stage-A deterministic reconciliation (`(question_number, group_id)`), then Stage-B upstream split hardening | Fast containment, reversible, preserves architecture | Requires explicit merge contract and compatibility handling | **Chosen** |
| B | Stage-B only (fix split logic first, no reconciliation) | Cleaner long-term model | Historical/stale docs remain inconsistent; slower risk burn-down | Rejected |
| C | DB-only dedupe by `question_number` after ingest | Quick patch in storage | Mis-merges cross-group questions; loses structured answer/explanation semantics | Rejected |

### Rollback trigger
- Trigger rollback if any of the following occurs on canary set:
  - Non-expected duplicate logical-item flags (same `question_number` within same logical group) do not improve by at least **80%**.
  - Any canary document loses > **5%** question-item count without matching source evidence.
  - New `question_uid` collisions detected in same document.

## 3. Invariants + Deterministic Merge Contract

### Hard invariants (must hold)
1. `group_id` is transient only and **must never persist** into `metadata_json` or DB.
2. Reconciled chunk `content` is **rebuilt only** via `_compose_question_chunk_text()` from reconciled fields.
3. Metadata `question_text` / `answer_text` / `explanation_text` must exactly match the rebuilt content sections.
4. Output order is deterministic by first-seen order inside `_prepare_question_chunks`; never depend on DB `chunk_index` for merge ordering.
5. Reconciliation key is `(question_number, group_id)` inside one document parse run.

### Deterministic field contract
- `question_text`: choose longest non-empty normalized candidate in the key-group.
- `answer_text`:
  - single-question mode: deterministic precedence table:
    1. local block answer extracted from the same numbered chunk
    2. parsed answer-bank entry for the same question number
    3. repeated tail-bank answer recovered from numbered tail fragments
    4. grouped fallback answer only when the whole logical unit is intentionally stored as a grouped question block
  - tie-break rule: within the same precedence bucket, use first-seen stable order
  - grouped-question mode: keep grouped answer-bank text.
- `explanation_text`: stable-order concat with stable de-duplication.
- `asset_refs`: stable-order union by (`asset_id`, `filename`, `url`), no duplicates.
- `content`: regenerated from reconciled fields only.

### Metadata recompute vs preserve
- Recompute via `_build_question_bank_chunk` / `QuestionBankPostProcessor.build_metadata`:
  - `question_text`, `answer_text`, `explanation_text`, `source_locator`, `question_uid`, `image_count`, `contains_images`, `quality_flags`.
- Preserve from parse context when unchanged:
  - `chapter`, `section`, `source_format`, `parser_backend`, `parser_provenance`, `resource_type`.

### Collision-safe ID contract (`source_locator` / `question_uid`)
- Baseline locator remains `question:{number}` for non-collision cases (compatibility).
- If same `question_number` appears in multiple `group_id` buckets in one document parse, append deterministic variant suffix by first-seen group order:
  - `source_locator = question:{number}|v:{k}` where `k` starts at 1.
- `question_uid` continues `qb:{document_id}:{source_locator}` and is therefore collision-safe.

### Compatibility note
- Existing consumers that parse `question:{number}` remain valid for non-collision documents.
- Collision suffix is additive and scoped to previously ambiguous cases only.

## 4. Phase Cards

### Phase A — Reconciliation + deterministic IDs (must land first)

**Touchpoints**
- `backend/services/rag_service.py`
  - `_prepare_question_chunks`
  - add internal reconcile helper (key `(question_number, group_id)`)
  - deterministic first-seen ordering + collision suffixing
- `backend/services/question_bank_post_processor.py`
  - ensure deterministic `source_locator/question_uid` compatibility behavior
- `backend/tasks/ingest.py`
  - `_build_completion_message` counts from reconciled question-item set

**Tests (required before merge)**
- `tests/test_rag.py`
  - add duplicate-number cross-group reconciliation case with explicit assertions for:
    - single output per `(question_number, group_id)`
    - rebuilt content sections equal metadata fields
    - `group_id` absent from metadata
  - add collision case asserting distinct locators (`question:21|v:1`, `question:21|v:2`) and distinct `question_uid`.
- `scripts/audit_question_slices.py`
  - extend report schema to separate:
    - raw duplicate question-number flags
    - non-expected duplicate logical-item flags (same number inside same logical group or unresolved collision)
    - same-document `question_uid` collision flags
  - define before/after canary threshold using the non-expected logical-item flag count as numerator and total `question_item` logical outputs as denominator.
- `tests/test_ingest.py`
  - add completion-message count case ensuring question/answer/explanation counts align after reconciliation.
- `tests/test_rag.py`
  - add targeted PDF fallback invariance case covering `PDFParseBridge.prepare_chunks -> rag_service._prepare_question_chunks` fallback path for question resources.

**Rollback**
- Revert Phase-A patch if rollback trigger is hit in canary.

**Exit criteria**
- All new + existing `tests/test_rag.py` question-splitting tests pass.
- `tests/test_ingest.py` targeted assertions pass.
- Audit on canary meets threshold using non-expected logical-item duplicate metric and shows zero same-doc `question_uid` collisions.

---

### Phase B — Upstream split hardening (after Phase A stable)

**Touchpoints**
- `backend/services/rag_service.py`
  - `_split_question_groups`
  - `_line_starts_new_question_group`
  - related answer/explanation section boundary logic

**Known remaining target**
- `document_id=344` (`11.3 实验：导体电阻率的测量.docx`) remains the canonical Phase B canary.
- Verified after Phase A canary re-slice: `325` and `327` are stable, but `344` still shows repeated question-number fragments from answer/explanation regions.
- Therefore the remaining defect is explicitly classified as an upstream grouping/start-of-question problem, not a Phase A reconciliation or deployment failure.

**Tests (required before merge)**
- Extend existing regression suite around:
  - `test_prepare_question_chunks_merges_repeated_numbered_answer_bank_at_document_end`
  - `test_prepare_question_chunks_parses_inline_numbered_answer_bank_with_section_markers`
  - `test_prepare_question_chunks_keeps_multiline_explanation_entries_bound_to_numbered_fillins`
  - `test_prepare_question_chunks_does_not_false_split_wrapped_explanation_items_between_questions`
  - `test_prepare_question_chunks_groups_multi_question_blocks_with_shared_answer_sections`

**Rollback**
- Keep Phase A intact; revert only Stage-B split changes if regressions appear.

**Exit criteria**
- No regression in normal single-question progression (`question -> answer -> next question`).
- Canary dry-run audit shows additional reduction in false splits without count underflow.

## 5. Verification Matrix

| Area | File | Assertion target |
|---|---|---|
| Merge invariants | `tests/test_rag.py` | Rebuilt `content` sections equal metadata `question_text/answer_text/explanation_text`; no `group_id` in metadata |
| Key collision safety | `tests/test_rag.py` | Same `question_number` in different groups yields unique `source_locator` + `question_uid` |
| Logical duplicate metric | `scripts/audit_question_slices.py` | Report distinguishes raw repeated numbers from non-expected logical-item duplicates and same-doc ID collisions |
| Existing split regressions | `tests/test_rag.py` | Existing assertions in numbered answer-bank / wrapped explanation / grouped blocks remain green |
| PDF fallback invariance | `tests/test_rag.py` | `pdf_parse_bridge -> rag_service._prepare_question_chunks` fallback path preserves prior non-PDF reconciliation semantics without changing PDF-specific block-prep behavior |
| Completion message counts | `tests/test_ingest.py` | `导入完成...按题目拆分/答案/解析` counts match reconciled results |
| Static sanity | `backend + tests` | `.venv/bin/python -m compileall backend tests locustfile.py` succeeds |

## 6. Rollout / Backfill Runbook (Environment split)

### Guardrails (all envs)
1. Pre-run dry audit:
   - `./.venv/bin/python scripts/audit_question_slices.py`
2. Define canary list (minimum):
   - include `document_id=344`
   - include one grouped-range doc (e.g. 21-24/25-29 shape)
   - include one normal non-repeated doc as control
3. Canary pass condition:
   - non-expected duplicate logical-item flags reduced >= 80%
   - no same-doc `question_uid` collisions
   - no >5% unexplained question-count drop
   - documents with legitimate same `question_number` across different groups only pass when `source_locator` / `question_uid` remain unique and deterministic

### Local / SQLite (`data/studyagent.db`)
1. Backup:
   - reuse `scripts/backfill_question_slices.py` backup behavior (file copy into `data/backups/`).
2. Backfill:
   - run `./.venv/bin/python scripts/backfill_question_slices.py [optional audit_report.json]`.
3. Verify:
   - rerun audit script; compare classification/count delta against pre-run.
4. Rollback:
   - stop services and restore DB file from backup copy.

### Server / PostgreSQL
1. Backup:
   - DB-engine native logical backup before backfill (e.g. `pg_dump` snapshot with timestamp).
2. Authoritative backfill path:
   - do not run the current SQLite-only `scripts/backfill_question_slices.py` in PostgreSQL environments.
   - production backfill is fixed to one path only: reingest an explicitly selected document-id list after the external DB snapshot is complete.
   - selection source must be the audited canary/target document list from `scripts/audit_question_slices.py`.
3. Idempotency / identity guarantees:
   - each reingest rewrites chunks for the selected document only; rerunning the same document-id list is allowed after rollback or verification.
   - post-backfill verification must confirm deterministic `source_locator` / `question_uid` values for the same code revision and same source file.
   - rollback authority remains the external PostgreSQL snapshot, not an in-script backup branch.
3. Verify:
   - run audit report and compare canary metrics.
4. Rollback:
   - restore from snapshot and re-run audit on restored state.

## 7. ADR

### ADR-Question-Chunk-Reconciliation-v2
- **Decision**: Implement Stage-A deterministic reconciliation first using transient `group_id` with merge key `(question_number, group_id)`, then Stage-B upstream split hardening.
- **Drivers**:
  - Immediate containment of duplicate slices.
  - Prevent `source_locator/question_uid` collisions.
  - Preserve compatibility for non-collision documents.
- **Alternatives considered**:
  - Stage-B-only fix first (rejected: leaves historical inconsistency).
  - DB-only dedupe by `question_number` (rejected: unsafe merges).
- **Why chosen**:
  - Lowest-risk path that is reversible, testable, and production-auditable.
- **Consequences**:
  - Need explicit deterministic contracts and additional test coverage.
  - Backfill required to clean historical docs.
- **Follow-ups**:
  1. Land Phase A tests + implementation.
  2. Canary dry-run and threshold check.
  3. Execute environment-specific backfill.
  4. Land Phase B only after Phase A metrics stabilize.

## 8. Execution Handoff

### Available Agent Types

- `architect`: plan review, invariants, identity rules, rollout design
- `executor`: code implementation for `rag_service.py`, `ingest.py`, and scripts
- `test-engineer`: regression design and test gap closure
- `verifier`: completion evidence, count/identity validation, rollout checks
- `debugger`: failure diagnosis if grouped answer-bank regressions appear
- `writer`: plan/doc updates if execution uncovers contract drift

### Suggested Reasoning by Lane

- `architect`: `high`
- `executor`:
  - Phase A reconciliation/invariants: `high`
  - Phase B upstream split hardening: `high`
  - audit/backfill script work: `medium`
- `test-engineer`: `medium`
- `verifier`: `high`
- `debugger`: `high`

### Staffing Guidance

#### If executing via `ralph`

Use a single-owner sequential lane:

1. Phase 0 tests first
2. Phase A reconciliation + ID contract
3. Completion-message alignment
4. Phase A verification
5. Stage-B parser hardening only after Phase A is green
6. Audit + canary + backfill runbook validation

Best fit when:
- correctness is more important than throughput
- you want one owner to preserve invariants across `rag_service.py` and `ingest.py`
- parser behavior is still volatile

#### If executing via `team`

Use parallel lanes with bounded ownership:

- Lane 1: `executor`
  Ownership: `backend/services/rag_service.py`
  Scope: transient `group_id` handling, reconciliation pass, upstream split hardening

- Lane 2: `test-engineer`
  Ownership: `tests/test_rag.py`, `tests/test_ingest.py`
  Scope: regression lock, completion-message assertions, PDF fallback invariance

- Lane 3: `executor` or `writer`
  Ownership: `scripts/audit_question_slices.py`, `scripts/backfill_question_slices.py`, docs/runbook sections
  Scope: logical-duplicate KPI, engine-specific rollout and backfill mechanics

- Lane 4: `verifier`
  Ownership: evidence only
  Scope: run required suites, check canary metrics, validate `question_uid` / `source_locator` outcomes

Best fit when:
- Phase A implementation and test work can proceed independently
- script/runbook work is non-blocking to core merge logic
- you want faster turnaround with explicit verification separation

### Launch Hints

#### `ralph`

Recommended prompt:

```text
$ralph Implement Phase A and Phase 0 from QUESTION_CHUNK_RECONCILIATION_PLAN.md only. Do not start Phase B until Phase A tests and completion-message checks are green.
```

#### `team`

Recommended prompt:

```text
$team Execute QUESTION_CHUNK_RECONCILIATION_PLAN.md with 4 lanes:
1. rag_service reconciliation + ID contract
2. regression tests and ingest count tests
3. audit/backfill script updates
4. verification only
```

Equivalent shell-oriented hint when OMX runtime orchestration is preferred:

```text
omx team "Execute Phase A of QUESTION_CHUNK_RECONCILIATION_PLAN.md with separate implementation, test, script, and verification lanes"
```

### Team Verification Path

Execution is not complete until all of the following are true:

1. `tests/test_rag.py` passes with new duplicate-number, grouped-block, and collision cases
2. `tests/test_ingest.py` passes with direct completion-message count assertions
3. `pdf_parse_bridge -> rag_service._prepare_question_chunks` fallback invariance check passes
4. `scripts/audit_question_slices.py` emits the logical-duplicate KPI needed by the plan
5. Canary docs show:
   - no same-document `question_uid` collisions
   - no unexplained >5% question-count drop
   - non-expected duplicate logical-item flags reduced by target threshold
6. Backfill procedure is runnable and reversible in the intended environment
