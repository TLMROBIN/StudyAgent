# Test Spec — MinerU PDF Parser Integration

## Test Strategy
Protect the new MinerU PDF path with exact contract tests plus regression tests for existing consumers.

## Automated Test Areas
### 1. Routing and rollout
- PDF uses `PDF_PARSER_BACKEND`
- DOCX/TXT ignore `PDF_PARSER_BACKEND`
- Rollback to `legacy` is proven by test

### 2. Runtime / operations
- GPU-proof artifact is emitted on real/smoke ingest
- `MINERU_REQUIRE_GPU_PROOF=true` fails if proof is absent
- MinerU timeout at `480s` raises typed failure
- Celery wrapper owns retries per exception matrix
- Cancel/retry cleanup is idempotent

### 3. Boundary A tests
- Mocked/recorded MinerU payload -> exact `PDFParseResult`
- Raw MinerU payload schema does not leak downstream

### 4. Boundary B tests
- `PDFParseResult` -> exact `PreparedChunk` output
- Stable metadata keys preserved:
  - `question_text`
  - `question_number`
  - `answer_text`
  - `explanation_text`
  - `chunk_kind`
  - `contains_images`
  - `image_count`
  - `asset_refs`
- API-visible asset fields are deterministic projections from `asset_refs`

## Golden Fixtures
### A. Clean question-bank PDF
Files:
- `tests/fixtures/mineru/clean-question-bank.pdf`
- `tests/fixtures/mineru/clean-question-bank.assert.json`

Assertions:
- `question_item` count delta == 0
- `question_number` sequence exact match
- sentinel `answer_text` exact match after whitespace normalization
- sentinel `explanation_text` exact match after whitespace normalization
- `contains_images == bool(asset_refs)`
- `image_count == len(asset_refs)`
- `question_text` never empty

### B. Scanned PDF
Files:
- `tests/fixtures/mineru/scanned-paper.pdf`
- `tests/fixtures/mineru/scanned-paper.assert.json`

Assertions:
- ingest succeeds
- no `无可用文本层` error
- minimum `chunk_count >= 3`
- minimum `total_text_chars >= 200`
- runtime GPU-proof artifact exists when required

### C. Textbook PDF
Files:
- `tests/fixtures/mineru/textbook-118p.pdf` or reduced excerpt
- `tests/fixtures/mineru/textbook-118p.assert.json`

Assertions:
- sentinel chunks exact-match `chapter`
- sentinel chunks exact-match `section`
- sentinel chunks exact-match `tags`
- hierarchy mapping does not swap chapter/section
- if assets exist, `asset_refs` and derived API asset fields remain stable

## Existing Test Files Expected To Change
- `tests/test_ingest.py`
- `tests/test_rag.py`
- `tests/test_knowledge_management.py`
- `tests/test_chat_stream.py`

## Manual Comparison Set
- 1 clean question-bank PDF
- 1 scanned/low-text PDF
- 1 complex textbook PDF with figures/sections

Manual pass criteria:
- MinerU path visibly active when enabled
- GPU-backed parse evidenced during ingest
- scanned PDFs no longer hard-fail as no-text-layer
- clean question-bank PDF has no severe question/image mismatch
- textbook chunks carry expected hierarchy/tag metadata
- rollback switch disables MinerU path cleanly

## Release Gate
Release is blocked until:
- acceptance criteria 1–13 in `.omx/plans/ralplan-mineru-parser-integration-v3.md` are evidenced
- DOCX/TXT unchanged by tests
- no historical replay path exists
