# StudyAgent AGENTS.md

This file applies to the entire `StudyAgent/` repository.

## Intent

StudyAgent is a LAN-facing high-school Q&A system built around:
- FastAPI backend
- Vue 3 frontend
- RAG retrieval with ChromaDB
- Redis / Celery async ingestion
- Docker Compose deployment

Optimize for **answer quality, retrieval correctness, and safe incremental changes** over cleverness or broad rewrites.

## Working style

- Prefer small, reversible diffs.
- Reuse existing patterns before introducing new abstractions.
- Preserve current behavior unless the task explicitly asks to change it.
- For PDF/RAG work, prefer **structure-aware** fixes over global text hacks.
- Do not silently widen scope from PDF-only to DOCX/TXT/general ingestion unless explicitly requested.

## Repository-specific priorities

1. **RAG correctness beats cosmetic cleanup**
   - Better retrieval and answering quality is more important than prettier chunk text.
2. **Preserve structure-bearing content**
   - Chapter/section headings
   - Captions / prompts / experiment-step text
   - Table cell content
3. **Under uncertainty, preserve**
   - Do not delete ambiguous content just because it looks noisy.
4. **Prefer document-internal evidence**
   - Frequency, page position, block type, and parser metadata are preferred over book-specific hardcoding.

## PDF-specific guidance

The PDF path has project-specific constraints:

- `backend/services/pdf_parse_bridge.py` is the preferred place for **PDF-only** structure-aware cleaning and transformation.
- Avoid turning `backend/services/rag_service.py::_normalize_pdf_text()` into a global catch-all cleaner.
- For repeated boilerplate suppression, require strong evidence:
  - repeated within the same document,
  - page-edge/header-footer evidence,
  - not a title block / protected structure signal.
- For table cleanup:
  - normalize markup if possible,
  - preserve table content even if formatting degrades,
  - do not drop table content on low confidence.

## Key files and likely ownership

- `backend/services/rag_service.py`
  - chunk building
  - retrieval behavior
  - metadata shaping
- `backend/services/pdf_parse_bridge.py`
  - parsed-PDF block → prepared chunk conversion
  - structure-aware PDF handling
- `backend/services/mineru_service.py`
  - MinerU parse normalization and parser metadata
- `backend/tasks/ingest.py`
  - ingestion pipeline and runtime gating
- `tests/test_rag.py`
  - primary regression coverage for RAG/PDF behavior
- `tests/test_ingest.py`
  - ingestion regression coverage
- `tests/test_mineru_service.py`
  - MinerU runtime/parse regression coverage

## Test commands

Use the project venv explicitly:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest tests/test_rag.py -q
.venv/bin/python -m pytest tests/test_ingest.py tests/test_mineru_service.py -q
.venv/bin/python -m compileall backend tests locustfile.py
```

Do not assume `python` or `pytest` are on PATH; prefer `.venv/bin/python`.

## Frontend notes

- Frontend code lives under `frontend/`
- Package manager is `npm`
- Only touch frontend when the task truly requires it

## Docker / deployment guidance

Production-like compose runs are defined in `docker-compose.yml`.

Important:
- `backend` and `worker` mount the repo into `/app`
- For **code-only** backend/worker changes, a restart is usually enough:

```bash
sg docker -c '/usr/bin/docker compose restart backend worker nginx'
```

- Full rebuilds are slower and may re-download large ML dependencies; use them only when needed:

```bash
sg docker -c '/usr/bin/docker compose up -d --build'
```

Health verification:

```bash
sg docker -c '/usr/bin/docker compose ps'
curl -fsS http://127.0.0.1:8002/health
```

If checking Docker from a shell where group membership is not applied, prefer `sg docker -c '...'`.

## Git / commit guidance

- Follow the repository Lore commit protocol.
- Commit only task-relevant files.
- Avoid committing OMX runtime state unless the task explicitly requires it.

## Files to treat carefully

- `.omx/`
  - runtime/planning artifacts; useful for workflow state, but usually not part of product behavior
- `data/`
  - may contain environment-specific artifacts
- deployment/config files
  - verify real runtime impact before changing

## When making RAG or PDF changes

Before finishing, try to provide evidence for:
- targeted regression tests,
- broader regression tests,
- compile/static sanity,
- if relevant, container health after restart.

For PDF cleanup changes specifically, prefer proving:
- markup noise is reduced,
- headings are preserved,
- captions/steps are preserved,
- table content remains retrievable,
- non-PDF paths are unchanged.

## Avoid

- broad global cleanup passes without acceptance tests
- book-title or publisher-name hardcoding as the primary mechanism
- relying on the LLM alone to “ignore” ingestion noise when retrieval can be improved upstream
- unnecessary dependency additions
- silent behavioral changes outside the requested scope
