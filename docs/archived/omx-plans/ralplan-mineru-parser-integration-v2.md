# MinerU PDF Parser Integration — Consensus Plan v2

## Context and Evidence
- Input spec: `.omx/specs/deep-interview-mineru-parser-integration.md`
- Prior plan revised: `.omx/plans/ralplan-mineru-parser-integration-initial.md`
- Current ingest spine: `backend/tasks/ingest.py` → `rag_service.extract_content()` → `rag_service.prepare_document_chunks()` → `ingest_document_chunks()`
- Current PDF path rejects scanned PDFs via `_extract_pdf_text()`
- Current DOCX path already owns image extraction + asset URLs via `_extract_docx_content()`
- Current question splitting semantics live in StudyAgent via `_prepare_question_chunks()` and remain StudyAgent-owned in phase 1
- Original spec remains binding: PDF-only first, no DOCX/TXT refactor, no historical backfill, no frontend rewrite, no MinerU-owned question splitting semantics

---

## 1) RALPLAN-DR Summary

### Principles
1. **PDF-only first**: phase 1 changes only the PDF parser path; DOCX/TXT stay behaviorally unchanged.
2. **Two-boundary contract only**: MinerU must stop at a StudyAgent-owned `PDFParseResult`, and a single bridge must own conversion into downstream currency.
3. **StudyAgent owns semantics**: question splitting, chunk-kind decisions, and recommendation semantics stay in StudyAgent.
4. **Runtime readiness is part of scope**: local deployment mode, actual GPU usage, timeout/retry/cancel behavior, and cleanup are phase-1 gates, not deferred follow-ups.
5. **Compatibility is frozen at the app boundary**: downstream/API-visible fields required today stay stable in phase 1 even if internal metadata evolves.

### Top Decision Drivers
1. **Material parsing uplift for scanned/complex PDFs**
2. **Safe compatibility with existing recommendation/RAG consumers**
3. **Operational reliability of a local GPU-backed parser inside the ingest pipeline**

### Viable Options

#### Option A — Thin MinerU adapter returning current `ExtractionResult(text, assets)` only
- **Pros**: smallest diff; easy rollback
- **Cons**: discards layout/asset locality too early; weak support for textbook structure and question/image association; poor basis for stable compatibility tests

#### Option B — MinerU adapter → StudyAgent `PDFParseResult` → single PDF bridge to `PreparedChunk` + stable asset payload **(preferred)**
- **Boundary A**: MinerU adapter returns an internal, StudyAgent-owned `PDFParseResult`
- **Boundary B**: one PDF bridge converts `PDFParseResult` into downstream currency: `PreparedChunk` plus stable API-visible asset payload shape
- **Pros**: preserves parser structure without leaking MinerU schema; keeps semantics local; creates one explicit compatibility/rollback seam; supports exact bridge-contract tests
- **Cons**: moderate refactor in PDF ingest preparation and test fixtures

#### Option C — Full multi-format parser abstraction for PDF/DOCX/TXT now
- **Pros**: cleaner long-term architecture on paper
- **Cons**: over-scoped; raises regression risk for unchanged formats; delays PDF value and runtime stabilization

### Preferred Option and Why It Wins
**Option B wins** because it is the narrowest plan that both captures MinerU’s value and creates an explicit compatibility boundary. The design prevents raw MinerU payloads from leaking into application contracts while still preserving enough structure to improve scanned/complex PDFs and image locality.

---

## 2) Phase-1 Contract Freeze

### Boundary A — Internal parser contract
The MinerU adapter returns a **StudyAgent-owned `PDFParseResult`** and nothing downstream consumes raw MinerU schema directly.

### Boundary B — Downstream bridge contract
A single PDF bridge converts `PDFParseResult` into:
1. `PreparedChunk` instances used by existing ingest/recommendation flows
2. The **stable API-visible asset payload shape** already expected by downstream routers/chat/recommendation consumers

### Phase-1 compatibility fields that must remain stable
The bridge must preserve these fields/semantics in phase 1:
- `asset_refs`
- `url`
- `title`
- `description`
- `contains_images`
- `image_count`
- `question_text`
- `question_number`
- `chunk_kind`

These are frozen compatibility outputs for phase 1 even if internal parser metadata or storage contracts change.

---

## 3) Minimum Internal Representation (IR)

The minimum `PDFParseResult` / normalized IR should include only what phase 1 needs:
- **text blocks**
- **page/span locality**
- **asset anchors**
- **hierarchy hints**
- **parser provenance**

### Explicit non-goal for the IR
The IR must **not** encode question-splitting semantics, question boundaries as authoritative truth, or other MinerU-owned chunk semantics. StudyAgent continues to decide how questions are split and labeled.

---

## 4) Revised Phased Plan

### Phase 1 — Runtime/deployment gate + parser rollout boundary
- Define and implement the **local MinerU deployment mode** used in production on this host.
- Prove **actual GPU usage on the parse path**, not just CUDA visibility at install time.
- Set explicit **timeout budget** for PDF parse tasks under Celery/task execution constraints.
- Define and implement **retry behavior** for transient MinerU/runtime failures.
- Define and implement **task cancellation cleanup** and idempotent temp/artifact cleanup.
- Add a **parser feature flag / rollout switch** so MinerU PDF parsing can be enabled, disabled, or rolled back without code surgery.
- Ensure PDF routing uses the feature flag and does not affect DOCX/TXT paths.

**Exit criteria**
- Local MinerU deployment mode is documented/configured for StudyAgent runtime.
- Parse-path evidence shows actual GPU-backed MinerU execution.
- Timeout, retry, and cancellation behavior are implemented and tested.
- Parser rollout switch exists and cleanly falls back away from the MinerU PDF path.
- DOCX/TXT tests remain green with unchanged routing.

### Phase 2 — Boundary A: MinerU adapter → StudyAgent-owned `PDFParseResult`
- Introduce a MinerU adapter/service that translates raw MinerU output into a StudyAgent-owned `PDFParseResult`.
- Keep the IR minimal: text blocks, locality, asset anchors, hierarchy hints, provenance.
- Do not expose raw MinerU payloads beyond the adapter boundary.
- Model failure surfaces explicitly: parse timeout, transient execution error, malformed payload, partial asset extraction.

**Exit criteria**
- PDFs no longer depend on legacy `_extract_pdf_text()` on the main MinerU-enabled path.
- `PDFParseResult` is internal, minimal, and independent of raw MinerU schema.
- Partial parse/asset outcomes have explicit handling semantics.

### Phase 3 — Boundary B: single PDF bridge to downstream currency
- Build one PDF bridge that converts `PDFParseResult` into `PreparedChunk` plus the stable asset payload shape expected downstream.
- Preserve phase-1 compatibility fields exactly: `asset_refs`, `url`, `title`, `description`, `contains_images`, `image_count`, `question_text`, `question_number`, `chunk_kind`.
- Keep `_prepare_question_chunks()` and related StudyAgent logic as the owner of question splitting and chunk-kind semantics.
- Ensure textbook PDFs can carry hierarchy/tag hints into retrieval without letting parser structure directly dictate question semantics.

**Exit criteria**
- PDF bridge is the only route from parse IR into chunking/output contracts.
- Recommendation/textbook flows consume stable outputs without router/API contract drift.
- Question/image association improves without delegating question splitting to MinerU.

### Phase 4 — Failure-mode hardening and compatibility verification
- Handle **retries** with bounded policy and no duplicate side effects.
- Guarantee **idempotent cleanup** for temp files/assets on retry, failure, or cancellation.
- Define policy for **partial asset emission**: what can be committed, what must be dropped, and how metadata remains consistent.
- Verify behavior under **Celery time/task limits**, including cancellation and worker interruption.
- Confirm no historical backfill or replay is introduced.

**Exit criteria**
- Retry/failure/cancel flows do not leak temp artifacts or produce inconsistent metadata.
- Partial asset scenarios have deterministic, test-backed output behavior.
- No bulk reprocessing path exists for existing documents.

### Phase 5 — Contract tests, regression coverage, and release readiness
- Add **bridge-contract tests** from mocked MinerU payload → exact `PreparedChunk` outputs.
- Add **API-visible asset payload tests** from mocked MinerU payload → exact stable asset payloads.
- Add routing tests proving PDFs use the feature-flagged MinerU path and DOCX/TXT do not.
- Add regression coverage for:
  - clean question-bank PDF
  - scanned/low-text PDF
  - complex textbook PDF
- Add rollback verification proving the parser switch disables MinerU cleanly.

**Exit criteria**
- Exact-output contract tests protect `PreparedChunk` and asset payload compatibility.
- Scanned PDFs ingest successfully instead of failing as “无可用文本层”.
- Clean question-bank PDFs remain basically usable with no severe question/image mismatch.
- Rollback switch is proven, not merely planned.

---

## 5) Specific Codebase Touchpoints / Likely Files

### Existing files likely to change
- `backend/tasks/ingest.py`
- `backend/services/rag_service.py`
- `backend/config.py` *(or equivalent runtime/config surface for feature flag, timeout, retry, deployment mode)*
- `backend/routers/knowledge.py` *(only if compatibility assertions require minor stabilization, not redesign)*
- `backend/services/vector_store_service.py` *(only if stored metadata expansion is necessary)*
- `tests/test_ingest.py`
- `tests/test_rag.py`
- `tests/test_knowledge_management.py`
- `tests/test_chat_stream.py` *(if API-visible asset payload expectations are asserted here)*

### Likely new files/modules
- `backend/services/mineru_service.py` *(MinerU runtime adapter)*
- `backend/services/pdf_parse_bridge.py` *(single bridge from `PDFParseResult` to downstream currency)*
- `backend/services/pdf_parse_types.py` *(StudyAgent-owned `PDFParseResult` / IR types)*
- `tests/fixtures/mineru/...` *(mocked MinerU payloads and expected bridge outputs)*

---

## 6) Risks, Mitigations, Verification Strategy

### Key risks
1. **Runtime looks installed but parse path is not actually GPU-backed**
   - *Mitigation*: make GPU-path proof a phase-1 exit gate with parse-path evidence, not an environment note.
2. **Bridge drift breaks recommendation/API consumers**
   - *Mitigation*: freeze compatibility fields and add exact-output bridge-contract tests.
3. **Clean question-bank regression**
   - *Mitigation*: keep StudyAgent-owned question splitting; compare question numbers, question text, chunk kind, and image refs against fixtures.
4. **Retries/cancellation create duplicate or leaked assets**
   - *Mitigation*: idempotent cleanup, bounded retry policy, and explicit partial-emission rules.
5. **Raw MinerU schema leaks into application internals**
   - *Mitigation*: enforce Boundary A and Boundary B with dedicated types/modules and tests.
6. **Rollback is too slow or too implicit**
   - *Mitigation*: ship with planned parser feature flag / rollout switch from the start.

### Concrete verification strategy

#### Automated
- **Runtime / operational**
  - Feature-flag routing test: PDF path can switch between legacy and MinerU mode
  - Timeout/retry/cancel tests under Celery-compatible constraints
  - Cleanup idempotency tests for retried/aborted parses
- **Contract**
  - Mocked MinerU payload → exact `PDFParseResult` normalization assertions
  - Mocked MinerU payload → exact `PreparedChunk` outputs
  - Mocked MinerU payload → exact API-visible asset payload outputs
- **Regression**
  - DOCX asset extraction unchanged
  - TXT ingest unchanged
  - Clean question-bank PDF remains non-severely regressed
  - Scanned PDF now ingests successfully
  - Textbook PDF preserves useful structure/tag hints for retrieval

#### Manual comparison set
- 1 clean question-bank PDF
- 1 scanned/low-text PDF
- 1 complex textbook PDF with figures/sections

**Manual pass criteria**
- MinerU path is visibly active for PDFs when feature flag is enabled
- GPU-backed parse path is evidenced during actual PDF processing
- Scanned PDF no longer hard-fails as “无可用文本层”
- Clean question PDF has no severe question/image mismatch
- Textbook PDF yields improved hierarchy/tag locality in stored chunk metadata
- Rollback switch can disable the MinerU path without frontend/API breakage

---

## 7) Explicit Acceptance Criteria
1. **PDF-only routing**: new PDF imports use the feature-flagged MinerU path; DOCX/TXT behavior remains unchanged.
2. **Runtime gate complete**: local deployment mode, actual GPU usage, timeout budget, retry behavior, and cancellation cleanup are implemented before rollout.
3. **Boundary A enforced**: MinerU adapter returns a StudyAgent-owned internal `PDFParseResult`; raw MinerU payloads do not leak downstream.
4. **Boundary B enforced**: a single PDF bridge converts `PDFParseResult` into `PreparedChunk` plus stable API-visible asset payloads.
5. **Compatibility frozen**: phase-1 outputs preserve `asset_refs`, `url`, `title`, `description`, `contains_images`, `image_count`, `question_text`, `question_number`, and `chunk_kind`.
6. **IR minimality**: the internal IR includes only text blocks, page/span locality, asset anchors, hierarchy hints, and parser provenance.
7. **StudyAgent keeps semantics**: question splitting/chunk semantics remain StudyAgent-owned in phase 1.
8. **Failure modes covered**: retries, idempotent cleanup, partial asset emission, and task cancellation under Celery limits have explicit tested behavior.
9. **Rollback ready**: parser feature flag / rollout switch is implemented and verified, not optional.
10. **Scanned/complex uplift**: scanned/complex PDFs that previously failed now produce usable chunks/assets.
11. **No severe clean-PDF regression**: clean question-bank PDFs remain basically usable with no major question/image mismatch.
12. **No historical replay / no frontend rewrite**: existing documents are not auto-reprocessed and no large frontend refactor is required.

---

## 8) ADR

### Decision
Adopt **Option B in narrowed form**: MinerU becomes the formal PDF parser behind two explicit StudyAgent-owned boundaries — **Boundary A** (`MinerU adapter -> PDFParseResult`) and **Boundary B** (`PDFParseResult -> PreparedChunk + stable asset payload`) — while phase 1 also includes runtime readiness, rollback control, and failure-mode handling as release gates.

### Drivers
- Need materially better scanned/complex PDF parsing
- Need strict compatibility for recommendation/RAG consumers
- Need local GPU-backed runtime behavior to be operationally reliable, not aspirational
- Need fast rollback in a brownfield ingest pipeline

### Alternatives considered
- **Legacy PDF extraction + OCR fallback**: insufficient for structure/image locality goals
- **Thin MinerU text/assets adapter**: too lossy; no explicit downstream contract protection
- **Full multi-format abstraction now**: over-scoped and riskier than required

### Why chosen
This design captures MinerU’s value while sharply limiting where parser-specific complexity can spread. The two-boundary contract gives the implementation a clean seam for compatibility tests, rollback, and later iteration, while preserving StudyAgent ownership over semantics and unchanged formats.

### Consequences
- PDF ingest will be split more clearly into runtime adapter, parse IR, and bridge stages
- Test coverage must become more exact, especially for bridge outputs and failure modes
- Internal metadata/storage may evolve, but API-visible outputs are constrained
- Runtime/deployment work cannot be deferred; it is part of the first implementation gate

### Follow-ups
- Decide exact feature-flag/config surface and default rollout posture
- Define concrete timeout/retry values aligned with Celery limits
- Choose exact evidence mechanism for proving GPU-backed execution on parse path
- If phase 1 succeeds, later consider whether DOCX should converge onto a similar internal bridge

---

## 9) Available-agent-types Roster + Staffing Guidance

### Relevant agent types
- `architect` — enforce boundary/contract discipline
- `executor` — implement adapter, IR, bridge, and rollout controls
- `debugger` — runtime/Celery/retry/cancellation failure analysis
- `test-engineer` — exact-output contract and regression fixtures
- `verifier` — acceptance evidence and rollback proof
- `researcher` — MinerU runtime/deployment specifics if unresolved
- `build-fixer` — dependency/runtime integration issues

### Recommended Ralph follow-up
Use **`$ralph` with one primary implementation owner** after plan approval.

**Suggested lanes inside Ralph**
- Owner: `executor` **high** — implement feature flag, runtime adapter, `PDFParseResult`, and PDF bridge
- Contract checkpoint: `architect` **high** — review Boundary A/B and frozen compatibility fields before bridge rollout
- Test lane: `test-engineer` **high** — add exact-output bridge-contract fixtures early
- Failure-mode lane: `debugger` **high** — validate retry/cancel/Celery-limit behavior
- Final evidence: `verifier` **high** — confirm acceptance criteria and rollback proof

**Ralph hint**
- `$ralph Implement .omx/plans/ralplan-mineru-parser-integration-v2.md with Boundary A/B contract, runtime gate, exact bridge-contract tests, and rollout switch`

### Recommended Team follow-up
If using team mode, prefer a **3-executor delivery team plus leader-owned verifier pass**.

**Recommended team shape**
- Lane A: MinerU runtime adapter + feature flag + timeout/retry/cancel behavior
- Lane B: `PDFParseResult` types + single PDF bridge + compatibility freeze
- Lane C: fixtures/tests for exact `PreparedChunk` output, exact asset payloads, and regression coverage

**Suggested reasoning by lane**
- All delivery lanes: `executor` **high**
- Post-team validation: `verifier` **high**, `test-engineer` **medium/high**

**Explicit launch hints**
- `$team 3:executor "Implement .omx/plans/ralplan-mineru-parser-integration-v2.md with MinerU runtime gate, PDFParseResult boundary, single PDF bridge, rollout switch, and exact-output tests"`
- `omx team 3:executor "Implement .omx/plans/ralplan-mineru-parser-integration-v2.md with MinerU runtime gate, PDFParseResult boundary, single PDF bridge, rollout switch, and exact-output tests"`

---

## 10) Concrete Verification Path
1. **Before implementation**
   - Freeze compatibility fields and expected bridge outputs in fixtures/tests.
   - Define feature-flag default and rollback procedure.
2. **During implementation**
   - Prove Boundary A with type/module ownership.
   - Prove Boundary B with exact `PreparedChunk` and asset payload snapshots.
   - Validate timeout/retry/cancel flows under task-runner limits.
3. **Pre-release evidence**
   - targeted tests for `tests/test_ingest.py`, `tests/test_rag.py`, `tests/test_knowledge_management.py`, and any chat/recommendation tests touched
   - one mocked scanned-PDF ingest success proof
   - one clean question-PDF non-severe-regression proof
   - one textbook hierarchy/tag proof
   - one rollback-switch proof
4. **Terminal gate**
   - acceptance criteria 1–12 evidenced
   - DOCX/TXT unchanged by tests/routing evidence
   - no historical replay path introduced
   - only then close Ralph/team execution

---

## Execution Handoff Notes
- Start with tests that freeze phase-1 compatibility fields and exact bridge outputs.
- Treat the parser rollout switch as mandatory infrastructure, not a nice-to-have.
- Keep the IR minimal; avoid prematurely encoding MinerU-specific structure or question semantics.
- Block rollout on unresolved retry/cancel/cleanup behavior, even if core parsing looks good.
