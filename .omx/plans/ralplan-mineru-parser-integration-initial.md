# MinerU PDF Parser Integration — Initial Consensus Draft

## Context and Evidence
- Input spec: `.omx/specs/deep-interview-mineru-parser-integration.md`
- Current ingest spine: `backend/tasks/ingest.py` → `rag_service.extract_content()` → `rag_service.prepare_document_chunks()` → `ingest_document_chunks()`
- Current PDF path rejects scanned PDFs via `_extract_pdf_text()`
- Current DOCX path already owns image extraction + asset URLs via `_extract_docx_content()`
- Current question splitting semantics live in StudyAgent via `_prepare_question_chunks()` and must remain owned here in phase 1

---

## 1) RALPLAN-DR Summary

### Principles
1. **PDF-only first**: replace only the PDF parsing layer in phase 1; DOCX/TXT behavior stays intact.
2. **One downstream contract**: parsed PDF output must enter a StudyAgent-owned normalized content layer before chunking/tagging/image handling.
3. **StudyAgent owns semantics**: MinerU extracts structure/assets/text; question splitting, chunk boundaries, and recommendation semantics remain in StudyAgent.
4. **Prefer improvement where it matters most**: optimize for scanned/complex PDFs first, while preventing severe regression on clean question-bank PDFs.
5. **No surprise migration blast radius**: no automatic historical reprocessing; changes apply to new/re-imported PDFs only.

### Top Decision Drivers
1. **Parsing quality uplift for scanned/complex PDFs**
2. **Safe integration with existing recommendation/RAG pipelines**
3. **Controlled scope and rollback surface in a brownfield codebase**

### Viable Options

#### Option A — Thin MinerU adapter returning current `ExtractionResult(text, assets)` only
- **Pros**: smallest diff; easiest rollback; minimal ingest churn
- **Cons**: throws away MinerU structure too early; weak basis for textbook hierarchy/tagging; forces image/question association back into brittle text heuristics

#### Option B — PDF-only MinerU adapter → normalized StudyAgent content IR → existing chunk/recommendation pipeline bridge **(preferred)**
- **Pros**: captures structure/assets once; keeps chunking semantics in StudyAgent; enables shared downstream handling for question recommendation + textbook RAG; isolates DOCX/TXT
- **Cons**: moderate refactor in ingest/chunk-prep path; requires new internal contract + test fixtures

#### Option C — General parser abstraction for PDF/DOCX/TXT upfront
- **Pros**: clean long-term architecture
- **Cons**: over-scoped vs current need; raises regression risk for DOCX/TXT; delays PDF value

### Preferred Option and Why It Wins
**Option B wins** because it is the smallest design that solves the real problem: MinerU is useful only if its structure survives long enough to drive chunking, tagging, and image association. It also preserves the spec guardrails: PDF-only first, no DOCX/TXT behavior changes, no MinerU-owned question splitting semantics.

---

## 2) Concrete Phased Plan

### Phase 1 — PDF parser boundary and MinerU integration
- Add a **PDF parsing adapter/service** that invokes MinerU asynchronously/safely and returns a StudyAgent-owned normalized PDF payload.
- Route `extract_content()` by file type so PDFs use the new adapter; DOCX/TXT paths remain untouched.
- Preserve per-document artifact lifecycle and cleanup behavior for failed/cancelled imports.

**Exit criteria**
- New PDF uploads no longer call legacy `_extract_pdf_text()` on the main happy path.
- DOCX/TXT tests remain green without behavioral changes.

### Phase 2 — Normalized content IR for PDF outputs
- Introduce a **normalized content representation** for PDF parse results: blocks/sections/pages/assets/anchors/basic tags.
- Keep this IR internal to StudyAgent; MinerU output is translated into it.
- Include enough structure to support both textbook hierarchy and question/image locality, without encoding question-splitting decisions.

**Exit criteria**
- PDF parse result can represent text blocks, hierarchy hints, and asset references without flattening to raw text first.

### Phase 3 — Downstream unification for chunking / tagging / image management
- Refactor chunk preparation so PDF IR goes through a StudyAgent-owned normalization pipeline before chunk creation.
- Keep `_prepare_question_chunks()` as the semantic owner for question-set splitting; feed it improved text/asset locality instead of delegating splitting to MinerU.
- Unify asset reference creation and metadata tagging so recommendation rows and textbook RAG rows use the same asset/tag conventions.

**Exit criteria**
- Question PDFs produce chunks with stable `asset_refs` / `contains_images` / `image_count` metadata.
- Textbook PDFs produce chunks with usable hierarchy/tag metadata for retrieval.

### Phase 4 — Persistence, compatibility, and rollout guardrails
- Adjust internal metadata/storage contracts where needed, but keep router/API payloads backward-compatible enough to avoid major frontend changes.
- Ensure only newly ingested PDFs use the new path; no historical backfill/re-run job is introduced.
- Add observability/progress messaging for long-running MinerU imports.

**Exit criteria**
- No automatic reprocessing of existing docs.
- Import UX remains asynchronous and operationally understandable.

### Phase 5 — Verification and release hardening
- Lock behavior with regression tests around DOCX/TXT, clean question-bank PDF, scanned PDF, and textbook PDF flows.
- Run a manual comparison set covering: clean question-bank PDF, scanned worksheet/exam PDF, structured textbook PDF.
- Ship behind a PDF-path integration switch only if needed for rollback speed.

**Exit criteria**
- Verification evidence shows strong improvement on scanned/complex PDFs and no severe clean-PDF regression.

---

## 3) Specific Codebase Touchpoints / Likely Files

### Existing files likely to change
- `backend/tasks/ingest.py`
- `backend/services/rag_service.py`
- `backend/routers/knowledge.py` *(only if asset serving/metadata exposure needs minor compatibility updates)*
- `backend/services/vector_store_service.py` *(only if stored metadata fields expand materially)*
- `tests/test_ingest.py`
- `tests/test_rag.py`
- `tests/test_knowledge_management.py`
- `tests/test_chat_stream.py` *(if recommendation asset payload expectations tighten)*

### Likely new files/modules
- `backend/services/mineru_service.py` *(or equivalent PDF parser adapter)*
- `backend/services/pdf_content_normalizer.py` *(or equivalent normalized IR / bridge layer)*
- `tests/fixtures/pdf/...` and/or mocked MinerU payload fixtures
- Optional config surface in `backend/config.py` for MinerU binary/service/runtime settings

---

## 4) Risks, Mitigations, Verification Strategy

### Key risks
1. **Clean question-bank PDF regression**
   - *Mitigation*: keep StudyAgent question splitting in `_prepare_question_chunks()`; add regression fixtures comparing chunk count, question numbers, and image association.
2. **MinerU structure leaks too directly into app contracts**
   - *Mitigation*: require a StudyAgent-owned normalized IR boundary; do not pass raw MinerU schema downstream.
3. **Long-running / GPU-heavy imports cause unstable ingest UX**
   - *Mitigation*: keep async ingest, improve progress messages, preserve cleanup/cancel semantics, and allow parser invocation isolation.
4. **Asset/path contract drift breaks recommendation or document asset serving**
   - *Mitigation*: preserve `asset_refs` payload shape expected by routers/chat responses; regression-test asset URLs and image counts.
5. **Over-scoping into DOCX/TXT or historical migration**
   - *Mitigation*: explicit test coverage and routing guards proving unchanged DOCX/TXT code paths and no bulk reprocessing behavior.

### Concrete verification strategy

#### Automated
- **Unit/service**
  - PDF routing test: PDF uses MinerU adapter, DOCX/TXT do not
  - IR normalization test: MinerU output becomes stable internal blocks/assets/tags
  - Question chunking regression: question numbers, answers, explanations, image refs still come from StudyAgent semantics
  - Textbook chunking test: hierarchy/tag metadata present for RAG chunks
- **Integration**
  - `run_ingest_pipeline()` succeeds on mocked scanned/complex PDF path
  - Asset URLs remain retrievable via existing document asset routes
  - `recommend_questions()` still returns image-backed rows correctly
- **Non-regression**
  - Existing DOCX asset extraction tests unchanged
  - TXT ingestion unchanged

#### Manual comparison set
- 1 clean question-bank PDF
- 1 scanned/low-text PDF
- 1 complex textbook PDF with figures/sections

**Manual pass criteria**
- scanned PDF no longer hard-fails as “无可用文本层”
- clean question PDF does not show severe question/image mismatch
- textbook PDF yields visibly improved structure/tags in stored chunk metadata

---

## 5) Explicit Acceptance Criteria
1. **PDF-only routing**: new PDF imports use MinerU-backed parsing; DOCX/TXT behavior remains unchanged.
2. **No historical replay**: no automatic reprocessing/backfill is introduced for existing documents.
3. **Normalized IR present**: PDF parse output enters a StudyAgent-owned intermediate representation before chunking.
4. **Question semantics remain local**: phase-1 question splitting semantics are still owned by StudyAgent, not MinerU.
5. **Unified downstream metadata**: question recommendation and textbook RAG chunks share consistent image/tag metadata conventions.
6. **Scanned/complex uplift**: scanned/complex PDFs that previously failed now ingest successfully and produce usable chunks/assets.
7. **No severe clean-PDF regression**: clean question-bank PDFs remain usable with no major question/image mismatch.
8. **Frontend stability**: no major frontend rewrite is required; existing asset/read APIs remain usable.

---

## 6) ADR

### Decision
Adopt **MinerU as the formal PDF parser only**, behind a StudyAgent-owned normalized PDF content layer, and refactor downstream PDF chunking/tagging/image management to consume that layer while keeping DOCX/TXT and StudyAgent-owned question semantics unchanged in phase 1.

### Drivers
- Need materially better scanned/complex PDF parsing
- Need one downstream content model for recommendation + textbook RAG
- Need bounded rollout risk in existing ingest architecture

### Alternatives considered
- **Keep legacy PDF extraction and only add OCR fallback**: too weak for layout/asset/hierarchy goals
- **MinerU text-only adapter**: insufficient structure retention
- **Full all-format parser abstraction now**: too broad; unnecessary DOCX/TXT risk

### Why chosen
This path captures MinerU’s value without surrendering application semantics or widening scope beyond PDF. It also creates the minimum reusable foundation for later convergence of parser contracts across formats.

### Consequences
- Internal metadata and storage contracts may evolve
- `rag_service` ingest preparation will likely split into clearer parser/normalizer/chunker stages
- Test fixture complexity increases, especially around PDF payloads/assets
- Clean rollback remains possible because the scope is limited to PDF routing

### Follow-ups
- Decide whether MinerU runs as local service, subprocess, or queued worker adapter
- Define exact normalized PDF IR schema and minimum metadata keys
- If phase 1 succeeds, evaluate later convergence of DOCX onto the same IR

---

## 7) Available-agent-types Roster + Follow-up Staffing Guidance

### Available agent types relevant here
- `architect` — boundary/contract review
- `executor` — implementation
- `debugger` — ingest/runtime failure diagnosis
- `test-engineer` — regression/fixture strategy
- `verifier` — completion evidence
- `researcher` — MinerU docs/runtime integration checks
- `build-fixer` — dependency/build failures if MinerU runtime integration is tricky

### Recommended Ralph follow-up
Use **`$ralph` with a single implementation owner** after plan approval.

**Suggested lanes inside Ralph**
- Owner: `executor` **high** — implement parser boundary + IR + chunk bridge
- Checkpoint reviewer: `architect` **high** — review IR boundary before wide refactor
- Test lane: `test-engineer` **medium** — add regression fixtures early
- Final evidence: `verifier` **high** — validate acceptance criteria before exit
- Optional docs/runtime lane: `researcher` **high** only if MinerU runtime details are still unresolved

**Ralph hint**
- `$ralph Implement approved MinerU PDF-only integration plan from .omx/plans/ralplan-mineru-parser-integration-initial.md`

### Recommended Team follow-up
Because `omx team` CLI currently shares one worker role prompt per run, the best default is a **delivery-heavy executor team**, with leader-owned verification and/or a later verifier pass.

**Recommended team shape**
- Team run: **3 executors**
  - Lane A: MinerU adapter + ingest routing
  - Lane B: normalized PDF IR + chunk/tag/image bridge
  - Lane C: tests/fixtures + compatibility wiring
- Leader follow-up: native `verifier` high or a later dedicated verification pass

**Suggested reasoning by lane**
- Delivery lanes: `executor` **high**
- Test-heavy lane (if still executor in CLI team): assign explicit verification checklist, but keep reasoning **high** due to brownfield coupling
- Post-team final audit: `verifier` **high**, `test-engineer` **medium**

**Explicit launch hints**
- `$team 3:executor "Implement MinerU PDF-only parser integration with normalized PDF IR, unified chunk/tag/image pipeline, and regression tests per .omx/plans/ralplan-mineru-parser-integration-initial.md"`
- `omx team 3:executor "Implement MinerU PDF-only parser integration with normalized PDF IR, unified chunk/tag/image pipeline, and regression tests per .omx/plans/ralplan-mineru-parser-integration-initial.md"`

**If mixed-role staffing is required**
- Run the main delivery with `executor` workers first
- Then run a smaller verification pass, or keep final verification leader-owned with native `verifier` / `test-engineer`

---

## 8) Concrete Team Verification Path
1. **Before launch**: snapshot current evidence and attach this plan to `.omx/context/...` / team brief.
2. **Executor team run**: complete parser routing, IR, chunk/tag/image unification, and tests.
3. **Mandatory evidence collection before shutdown**
   - targeted test subset for `tests/test_ingest.py`, `tests/test_rag.py`, `tests/test_knowledge_management.py`, and any chat/recommendation regression tests touched
   - one mocked scanned-PDF ingest success proof
   - one clean question-PDF non-severe-regression proof
   - one textbook hierarchy/tag proof
4. **Leader verifier pass**
   - inspect diffs against plan scope
   - confirm DOCX/TXT unchanged by tests and routing evidence
   - confirm no historical replay code path exists
5. **Terminal gate**
   - acceptance criteria 1–8 all evidenced
   - only then close team/shutdown workers

---

## Execution Handoff Notes
- Start with tests that freeze current DOCX/TXT and clean-question-PDF expectations.
- Keep MinerU output behind a translation layer; do not couple downstream logic to MinerU raw schema.
- Treat scanned/complex PDF improvement as the primary success metric, but block shipment on severe clean-PDF regression.
