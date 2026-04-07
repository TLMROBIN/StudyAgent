# Test Spec — Question-Bank Recommendation Optimization

## Test Strategy
Protect recommendation quality and rollout safety by testing contract parity, mixed-data behavior, deterministic image diagnostics, metadata preservation, and existing API compatibility.

## Automated Test Areas

### 1. Contract parity for new imports
- DOCX question-bank import emits required core metadata keys
- PDF question-bank import emits the same core metadata keys
- `structure_path`, answer/explanation fields, and asset fields remain populated consistently

### 2. Mixed-data recommendation behavior
- `question_item` rows outrank legacy blank-kind rows
- legacy fallback activates only when structured rows are absent or insufficient
- same-document legacy chunks are suppressed when structured rows exist in that document
- textbook rows remain excluded even when fallback is active

### 3. Image expectation and binding
- text-only question -> `image_expectation=not_needed`, `image_binding_status=none_needed`
- required-image question with bound asset -> `bound`
- required-image question without asset -> `missing_required`
- no default recommendation penalty for text-only questions
- image-intent query still increases ranking for image-backed questions

### 4. Metadata preservation and sync
- new internal metadata keys survive `sync_document_metadata()`
- serializer paths ignore non-exposed diagnostics unless explicitly added

### 5. Compatibility and surface stability
- knowledge chunk list still returns stable existing fields
- chat/recommendation responses remain backward compatible
- no TXT behavior changes

## Manual Checks
1. Compare one DOCX and one PDF question bank with both image and non-image questions.
2. Verify a mixed dataset returns structured rows first but still returns legacy-only results when needed.
3. Verify a required-image question missing its asset is diagnosable but does not crash ingest or recommendation.

## Suggested Test Files
- `tests/test_rag.py`
- `tests/test_knowledge_management.py`
- `tests/test_chat_stream.py`
- `tests/test_ingest.py` (only if ingest-path contract plumbing changes)

## Release Gate
Release is blocked until:
- mixed-data recommendation tests pass
- metadata preservation tests pass
- no textbook leakage appears in recommendation tests
- serializer compatibility remains green
