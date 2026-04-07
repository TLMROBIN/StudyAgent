# MinerU PDF Parser Integration — Consensus Plan v3

## Context and Evidence
- Input spec: `.omx/specs/deep-interview-mineru-parser-integration.md`
- Prior plans: `.omx/plans/ralplan-mineru-parser-integration-initial.md`, `.omx/plans/ralplan-mineru-parser-integration-v2.md`
- Current ingest spine: `backend/tasks/ingest.py` → `rag_service.extract_content()` → `rag_service.prepare_document_chunks()` → `ingest_document_chunks()`
- Current PDF path rejects scanned PDFs via `_extract_pdf_text()`
- Current DOCX path already owns image extraction + asset URLs via `_extract_docx_content()`
- Current question splitting semantics live in StudyAgent via `_prepare_question_chunks()` and remain StudyAgent-owned in phase 1
- Current task limits in `backend/config.py`: `INGEST_SOFT_TIME_LIMIT_SECONDS=300`, `INGEST_HARD_TIME_LIMIT_SECONDS=330`
- Local CPU-only MinerU pipeline test on a 118-page textbook PDF took ~313 seconds, which already exceeds the current 300-second soft limit
- Local GPU is available for real ingest (`RTX 4080 SUPER 16GB`, torch CUDA available)
- Original spec remains binding: PDF-only first, no DOCX/TXT refactor, no historical backfill, no frontend rewrite, no MinerU-owned question splitting semantics

---

## 1) RALPLAN-DR Summary

### Principles
1. **PDF-only first**: phase 1 changes only the PDF parser path; DOCX/TXT stay behaviorally unchanged.
2. **Two-boundary contract only**: MinerU must stop at a StudyAgent-owned `PDFParseResult`, and a single bridge must own conversion into downstream currency.
3. **StudyAgent owns semantics**: question splitting, answer/explanation assignment, chunk-kind decisions, and recommendation semantics stay in StudyAgent.
4. **Runtime readiness is part of scope**: local deployment mode, actual GPU usage, timeout/retry/cancel behavior, and cleanup are phase-1 gates, not deferred follow-ups.
5. **Compatibility is frozen at the app boundary**: downstream/API-visible fields required today stay stable in phase 1, including `answer_text` and `explanation_text`; API assets remain projections derived from stable `asset_refs`, not independent parser outputs.

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
- `answer_text`
- `explanation_text`
- `chunk_kind`

### Asset contract clarification
- `asset_refs` is the canonical stored/output asset list.
- `url`, `title`, `description`, `contains_images`, and `image_count` remain stable because they are derived from `asset_refs` through one bridge/serializer path.
- MinerU must not emit an alternate API asset contract beside `asset_refs`.

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
The IR must **not** encode question-splitting semantics, authoritative question boundaries, or MinerU-owned answer/explanation semantics. StudyAgent continues to decide how questions are split, labeled, and attached to `answer_text` / `explanation_text`.

---

## 4) Concrete Runtime and Deployment Decisions

### Config / feature-flag surface
Use `backend/config.py` as the single config surface and add these settings there:
- `PDF_PARSER_BACKEND` (`legacy` | `mineru`), default **`legacy`**
- `MINERU_DEVICE` (`cuda` | `cpu`), default **`cuda`**
- `MINERU_REQUIRE_GPU_PROOF` (`true` | `false`), default **`true`** outside tests
- `MINERU_PARSE_TIMEOUT_SECONDS`, default **`480`**
- `MINERU_PARSE_WARN_SECONDS`, default **`240`**

### Default rollout posture
- Repo/default env stays **`PDF_PARSER_BACKEND=legacy`** until the Phase-1 runtime gate is green.
- Production/local deploy flips explicitly to **`PDF_PARSER_BACKEND=mineru`** by env change only.
- When backend is `mineru`, PDF ingest **fails closed** on MinerU/runtime errors; it does **not** silently fall back to legacy parsing inside the same task. Rollback is a deliberate config flip back to `legacy`.
- DOCX/TXT never consult this flag in phase 1.

### Timeout and task-limit decisions
- Raise Celery ingest limits for the MinerU rollout from the current **300/330** to **540/600** seconds.
- Set MinerU internal parse timeout to **480 seconds**, leaving **60 seconds** of soft-limit headroom and **120 seconds** of hard-limit headroom for cleanup/state updates.
- Emit a structured slow-parse warning at **240 seconds** so large PDFs are visible before they threaten the limit.
- Rationale: the observed CPU-only run already took ~313 seconds; async ingest is acceptable; phase 1 should optimize for successful GPU-backed parsing plus cleanup headroom, not for preserving the old 300/330 envelope.

### Retry owner + exception matrix
**Retry owner:** `backend/tasks/ingest.py` Celery task wrapper owns retries. `mineru_service` and the PDF bridge raise typed exceptions only; they do not loop internally.

| Failure class | Example source | Retry? | Max retries | Notes |
|---|---|---:|---:|---|
| `MineruStartupError` | service/process boot failure | yes | 1 | cleanup temp dir first |
| `MineruTransientIOError` | temp-dir IO / ephemeral subprocess failure | yes | 1 | cleanup then retry once |
| `MineruTimeoutError` | MinerU parse exceeds 480s | no | 0 | fail task, preserve evidence |
| `GPUProofFailedError` | backend claims MinerU but no real GPU proof observed | no | 0 | treat as deployment misconfiguration |
| `MineruMalformedOutputError` | invalid/partial payload shape | no | 0 | contract bug, fail closed |
| `UnsupportedPdfError` / corrupt input | bad file/password/corrupt PDF | no | 0 | user-visible failure |
| `SoftTimeLimitExceeded` / `HardTimeLimitExceeded` | Celery limits | no | 0 | fail closed, cleanup in finally |

### GPU-proof mechanism during real ingest
Implement a structured runtime-proof artifact at `data/tasks/<task_id>/mineru-runtime.json` for every MinerU PDF ingest containing:
- selected parser backend (`mineru`)
- selected device (`cuda` expected)
- worker pid / parser child pid
- start/end timestamps
- at least one NVML or `nvidia-smi` sample during the active parse window showing the parser pid consumed GPU memory/utilization above baseline
- parse duration seconds

**Gate rule:** if `PDF_PARSER_BACKEND=mineru` and `MINERU_REQUIRE_GPU_PROOF=true`, ingest fails with `GPUProofFailedError` unless the runtime artifact proves a real CUDA-backed parse. Torch CUDA visibility alone is insufficient.

---

## 5) Revised Implementation Order

### Phase 1 — Boundary A skeleton + runtime gate smoke path
Start with the smallest runnable MinerU path that proves operations before bridge work:
- Add `PDF_PARSER_BACKEND`, MinerU timeout/device settings, and raised Celery limits.
- Land `pdf_parse_types.py` with a minimal `PDFParseResult` skeleton and provenance fields.
- Add `mineru_service.py` with a **smoke-path** method (`parse_smoke` or equivalent) that parses one PDF through MinerU, records the GPU/runtime proof artifact, and returns enough data to confirm Boundary A ownership.
- Wire the smoke path behind the PDF feature flag on a minimal service path or immediately after the Boundary A skeleton exists; do **not** postpone runtime proof until after full bridge work.
- Implement temp-dir cleanup and typed exceptions now.

**Exit criteria**
- Runtime gate is proven through a real PDF smoke ingest on the MinerU path.
- GPU proof artifact exists and passes the gate.
- Timeout, retry owner, and cleanup semantics are implemented.
- Flagged PDF routing exists; DOCX/TXT remain untouched.

### Phase 2 — Full Boundary A: MinerU adapter → StudyAgent-owned `PDFParseResult`
- Expand the smoke-path adapter into the full normalized `PDFParseResult`.
- Keep the IR minimal: text blocks, locality, asset anchors, hierarchy hints, provenance.
- Do not expose raw MinerU payloads beyond the adapter boundary.
- Model failure surfaces explicitly: timeout, startup/transient runtime error, malformed payload, partial asset extraction.

**Exit criteria**
- PDFs no longer depend on legacy `_extract_pdf_text()` on the main MinerU-enabled path.
- `PDFParseResult` is internal, minimal, and independent of raw MinerU schema.
- Partial parse/asset outcomes have explicit handling semantics.

### Phase 3 — Boundary B: single PDF bridge to downstream currency
- Build one PDF bridge that converts `PDFParseResult` into `PreparedChunk` plus stable asset payloads.
- Preserve phase-1 compatibility fields exactly: `asset_refs`, `url`, `title`, `description`, `contains_images`, `image_count`, `question_text`, `question_number`, `answer_text`, `explanation_text`, `chunk_kind`.
- Keep `_prepare_question_chunks()` and related StudyAgent logic as the owner of question splitting and chunk-kind semantics.
- Ensure API assets remain derived from stable `asset_refs` only.
- Ensure textbook PDFs can carry hierarchy/tag hints into retrieval without letting parser structure directly dictate question semantics.

**Exit criteria**
- PDF bridge is the only route from parse IR into chunking/output contracts.
- Recommendation/textbook flows consume stable outputs without router/API contract drift.
- Question/image association improves without delegating question splitting to MinerU.

### Phase 4 — Fixture-backed verification + failure-mode hardening
- Add fixture-backed contract tests from mocked/recorded MinerU payload → exact `PDFParseResult` outputs.
- Add fixture-backed bridge tests from `PDFParseResult` → exact `PreparedChunk` metadata/assets.
- Harden partial asset, retry, timeout, and cancellation cleanup semantics.
- Verify rollback by flipping `PDF_PARSER_BACKEND` back to `legacy`.

**Exit criteria**
- Retry/failure/cancel flows do not leak temp artifacts or produce inconsistent metadata.
- Partial asset scenarios have deterministic, test-backed output behavior.
- Rollback switch is proven and DOCX/TXT remain unchanged.

### Phase 5 — Release readiness
- Run targeted integration/regression coverage for ingest, RAG, knowledge, and chat payloads touched by the bridge.
- Capture one successful real-ingest runtime proof artifact for release evidence.
- Confirm no historical backfill path and no frontend rewrite were introduced.

**Exit criteria**
- Acceptance criteria 1–13 are evidenced.
- Release can enable `PDF_PARSER_BACKEND=mineru` by env flip only.

---

## 6) Specific Codebase Touchpoints / Likely Files

### Existing files likely to change
- `backend/config.py`
- `backend/tasks/ingest.py`
- `backend/services/rag_service.py`
- `backend/routers/knowledge.py` *(only if compatibility assertions require minor stabilization, not redesign)*
- `backend/routers/chat.py` *(only if asset/solution compatibility assertions require minor stabilization)*
- `backend/services/vector_store_service.py` *(only if stored metadata expansion is necessary)*
- `tests/test_ingest.py`
- `tests/test_rag.py`
- `tests/test_knowledge_management.py`
- `tests/test_chat_stream.py`

### Likely new files/modules
- `backend/services/mineru_service.py`
- `backend/services/pdf_parse_bridge.py`
- `backend/services/pdf_parse_types.py`
- `tests/fixtures/mineru/clean-question-bank.pdf`
- `tests/fixtures/mineru/clean-question-bank.assert.json`
- `tests/fixtures/mineru/scanned-paper.pdf`
- `tests/fixtures/mineru/scanned-paper.assert.json`
- `tests/fixtures/mineru/textbook-118p.pdf` *(or a reduced checked-in excerpt if the full file is too large)*
- `tests/fixtures/mineru/textbook-118p.assert.json`

---

## 7) Explicit Fixture-Backed Verification Assertions

### A. Clean question-bank PDF fixture
Fixture pair:
- `tests/fixtures/mineru/clean-question-bank.pdf`
- `tests/fixtures/mineru/clean-question-bank.assert.json`

Required assertions:
1. `question_item` chunk count delta is **exactly 0** versus the fixture manifest baseline.
2. `question_number` sequence matches the manifest **exactly**.
3. For every question marked in the manifest as having solutions, `answer_text` and `explanation_text` are preserved **exactly** after normalization (whitespace-normalized string equality).
4. For every question, `contains_images == bool(asset_refs)` and `image_count == len(asset_refs)`.
5. Any API-visible asset fields checked in routers/serializers (`url`, `title`, `description`) are asserted as deterministic projections of the stored `asset_refs` entries from the same chunk.
6. No question may lose all text content; each `question_text` must stay non-empty.

### B. Scanned PDF fixture
Fixture pair:
- `tests/fixtures/mineru/scanned-paper.pdf`
- `tests/fixtures/mineru/scanned-paper.assert.json`

Required assertions:
1. Ingest completes successfully on the MinerU path.
2. Task/error output does **not** contain `无可用文本层`.
3. Output contains at least the manifest minimums: **`min_chunks >= 3`** and **`min_total_text_chars >= 200`**.
4. Runtime proof artifact exists and passes the GPU-proof gate when `MINERU_REQUIRE_GPU_PROOF=true`.

### C. Textbook PDF fixture
Fixture pair:
- `tests/fixtures/mineru/textbook-118p.pdf` (or reduced excerpt)
- `tests/fixtures/mineru/textbook-118p.assert.json`

Required assertions:
1. Sentinel chunks identified by the manifest must carry **exact-match metadata** for: `resource_type`, `chapter`, `section`, and `tags`.
2. The bridge must preserve the manifest’s expected hierarchy mapping on those sentinel chunks; no `chapter`/`section` swapping.
3. If a sentinel chunk has assets, `asset_refs` must be present and its derived API asset fields remain stable.
4. Textbook chunks must remain ingestible by existing retrieval code without introducing a new chunk schema outside current metadata patterns.

### Test-shape requirement
Do not leave these as prose/manual checks. Each fixture must have a checked-in assertion manifest consumed by automated tests so the executor is implementing exact assertions, not re-deciding the contract during coding.

---

## 8) Risks, Mitigations, Verification Strategy

### Key risks
1. **Runtime looks installed but parse path is not actually GPU-backed**
   - *Mitigation*: runtime proof artifact plus `MINERU_REQUIRE_GPU_PROOF=true` gate in phase 1.
2. **Bridge drift breaks recommendation/API consumers**
   - *Mitigation*: freeze compatibility fields including `answer_text` / `explanation_text`; assert API assets derive from `asset_refs`.
3. **Clean question-bank regression**
   - *Mitigation*: exact fixture assertions on question count, question numbering, answers/explanations, and asset derivation.
4. **Retries/cancellation create duplicate or leaked assets**
   - *Mitigation*: Celery-owned bounded retry matrix, no internal service retry loops, idempotent cleanup.
5. **Raw MinerU schema leaks into application internals**
   - *Mitigation*: enforce Boundary A and Boundary B with dedicated types/modules and tests.
6. **Rollback is too slow or too implicit**
   - *Mitigation*: `PDF_PARSER_BACKEND` env switch ships first and is proven in tests.

### Concrete verification strategy

#### Automated
- **Runtime / operational**
  - Feature-flag routing test: PDF path switches by `PDF_PARSER_BACKEND`; DOCX/TXT do not
  - Timeout/retry/cancel tests for the explicit exception matrix
  - GPU-proof artifact test on a real or mocked MinerU smoke path
  - Cleanup idempotency tests for retried/aborted parses
- **Contract**
  - Mocked/recorded MinerU payload → exact `PDFParseResult` normalization assertions
  - `PDFParseResult` → exact `PreparedChunk` outputs
  - `PDFParseResult` / chunk metadata → exact API-visible asset payload outputs derived from `asset_refs`
- **Regression**
  - clean question-bank PDF assertions listed above
  - scanned PDF assertions listed above
  - textbook PDF hierarchy/tag assertions listed above
  - DOCX asset extraction unchanged
  - TXT ingest unchanged

#### Manual comparison set
- 1 clean question-bank PDF
- 1 scanned/low-text PDF
- 1 complex textbook PDF with figures/sections

**Manual pass criteria**
- MinerU path is visibly active for PDFs when feature flag is enabled
- GPU-backed parse path is evidenced during actual PDF processing
- Scanned PDF no longer hard-fails as `无可用文本层`
- Clean question PDF has no question-number drift and no lost answer/explanation fields for fixture sentinel items
- Textbook PDF yields the exact expected chapter/section/tag metadata for fixture sentinel chunks
- Rollback switch disables the MinerU path without frontend/API breakage

---

## 9) Explicit Acceptance Criteria
1. **PDF-only routing**: new PDF imports use the feature-flagged parser path; DOCX/TXT behavior remains unchanged.
2. **Runtime gate complete**: local deployment mode, actual GPU usage proof, timeout budget, retry owner/matrix, and cancellation cleanup are implemented before rollout.
3. **Boundary A enforced**: MinerU adapter returns a StudyAgent-owned internal `PDFParseResult`; raw MinerU payloads do not leak downstream.
4. **Boundary B enforced**: a single PDF bridge converts `PDFParseResult` into `PreparedChunk` plus stable API-visible asset payloads.
5. **Compatibility frozen**: phase-1 outputs preserve `asset_refs`, `url`, `title`, `description`, `contains_images`, `image_count`, `question_text`, `question_number`, `answer_text`, `explanation_text`, and `chunk_kind`.
6. **Asset derivation frozen**: API asset fields remain derived from stable `asset_refs`; no alternate parser-owned asset payload contract is introduced.
7. **IR minimality**: the internal IR includes only text blocks, page/span locality, asset anchors, hierarchy hints, and parser provenance.
8. **StudyAgent keeps semantics**: question splitting/chunk semantics remain StudyAgent-owned in phase 1.
9. **Failure modes covered**: retries, idempotent cleanup, partial asset emission, GPU-proof failure, and task cancellation under Celery limits have explicit tested behavior.
10. **Rollback ready**: `PDF_PARSER_BACKEND` is implemented and verified as the rollback switch.
11. **Scanned/complex uplift**: scanned/complex PDFs that previously failed now produce usable chunks/assets and pass the explicit fixture minimums.
12. **No severe clean-PDF regression**: clean question-bank PDFs preserve question count, numbering, answer/explanation fields, and asset derivation per fixture assertions.
13. **No historical replay / no frontend rewrite**: existing documents are not auto-reprocessed and no large frontend refactor is required.

---

## 10) ADR

### Decision
Adopt **Option B in narrowed form**: MinerU becomes the formal PDF parser behind two explicit StudyAgent-owned boundaries — **Boundary A** (`MinerU adapter -> PDFParseResult`) and **Boundary B** (`PDFParseResult -> PreparedChunk + stable asset payload`) — while phase 1 also includes runtime readiness, rollback control, GPU-proof gating, and failure-mode handling as release gates.

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
- Test coverage must become exact and fixture-backed, especially for bridge outputs and failure modes
- Internal metadata/storage may evolve, but API-visible outputs are constrained and assets stay derived from `asset_refs`
- Runtime/deployment work cannot be deferred; it is part of the first implementation gate
- MinerU rollout requires deliberate Celery limit changes and explicit GPU-proof evidence

### Follow-through decisions already made
- Config surface: `backend/config.py`
- Rollout switch: `PDF_PARSER_BACKEND`, default `legacy`
- Runtime posture: `mineru` fails closed; rollback is config-driven, not silent fallback
- Celery limits for MinerU rollout: `540/600`
- MinerU parse timeout/warn thresholds: `480/240`
- Retry owner: Celery task wrapper with the exception matrix above
- GPU-proof gate: required runtime artifact on real ingest

---

## 11) Available-agent-types Roster + Staffing Guidance

### Relevant agent types
- `architect` — enforce boundary/contract discipline
- `executor` — implement adapter, IR, bridge, rollout controls, and runtime proof
- `debugger` — runtime/Celery/retry/cancellation failure analysis
- `test-engineer` — exact-output contract and fixture manifests
- `verifier` — acceptance evidence and rollback proof
- `researcher` — MinerU runtime/deployment specifics if a doc gap blocks execution
- `build-fixer` — dependency/runtime integration issues

### Recommended Ralph follow-up
Use **`$ralph` with one primary implementation owner** after plan approval.

**Suggested lanes inside Ralph**
- Owner: `executor` **high** — implement config surface, runtime smoke path, `PDFParseResult`, and PDF bridge
- Contract checkpoint: `architect` **high** — review Boundary A/B and frozen compatibility fields before bridge rollout
- Test lane: `test-engineer` **high** — add fixture manifests and exact-output assertions early
- Failure-mode lane: `debugger` **high** — validate retry/cancel/Celery-limit/GPU-proof behavior
- Final evidence: `verifier` **high** — confirm acceptance criteria and rollback proof

**Ralph hint**
- `$ralph Implement .omx/plans/ralplan-mineru-parser-integration-v3.md with runtime smoke proof, Boundary A/B contract, exact fixture-backed tests, and config-driven rollback`

### Recommended Team follow-up
If using team mode, prefer a **3-executor delivery team plus leader-owned verifier pass**.

**Recommended team shape**
- Lane A: config surface + MinerU runtime smoke path + timeout/retry/cancel/GPU-proof behavior
- Lane B: `PDFParseResult` types + single PDF bridge + compatibility freeze
- Lane C: fixture manifests/tests for clean question-bank, scanned PDF, textbook hierarchy/tag outputs

**Suggested reasoning by lane**
- All delivery lanes: `executor` **high**
- Post-team validation: `verifier` **high**, `test-engineer` **medium/high**

**Explicit launch hints**
- `$team 3:executor "Implement .omx/plans/ralplan-mineru-parser-integration-v3.md with runtime smoke proof, Boundary A/B contract, config-driven rollback, and exact fixture-backed tests"`
- `omx team 3:executor "Implement .omx/plans/ralplan-mineru-parser-integration-v3.md with runtime smoke proof, Boundary A/B contract, config-driven rollback, and exact fixture-backed tests"`

---

## 12) Concrete Verification Path
1. **Before implementation**
   - Freeze compatibility fields, asset derivation rules, and fixture assertion manifests.
   - Add config settings and set explicit MinerU rollout values (`legacy` default, `540/600`, `480/240`).
2. **Immediately after Boundary A skeleton exists**
   - Run the minimal MinerU smoke path on a real PDF.
   - Produce `mineru-runtime.json` and prove the GPU gate.
3. **During implementation**
   - Prove Boundary A with type/module ownership.
   - Prove Boundary B with exact `PreparedChunk` and asset-derivation assertions.
   - Validate timeout/retry/cancel flows against the explicit exception matrix.
4. **Pre-release evidence**
   - targeted tests for `tests/test_ingest.py`, `tests/test_rag.py`, `tests/test_knowledge_management.py`, and `tests/test_chat_stream.py`
   - clean question-bank fixture passes exact count/numbering/answer/explanation/asset assertions
   - scanned-PDF fixture passes success/no-`无可用文本层`/minimum-output assertions
   - textbook fixture passes exact chapter/section/tag sentinel assertions
   - rollback-switch proof by flipping `PDF_PARSER_BACKEND`
5. **Terminal gate**
   - acceptance criteria 1–13 evidenced
   - DOCX/TXT unchanged by tests/routing evidence
   - no historical replay path introduced
   - only then close Ralph/team execution

---

## Execution Handoff Notes
- Start with fixture manifests and the minimal Boundary A smoke path; do not begin with the full bridge.
- Treat `PDF_PARSER_BACKEND` and the GPU-proof gate as mandatory infrastructure, not polish.
- Keep the IR minimal; avoid prematurely encoding MinerU-specific question semantics.
- Block rollout on unresolved retry/cancel/cleanup/GPU-proof behavior, even if core parsing already works.
