# Student Feedback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a student feedback entry with daily anti-abuse limits, admin review/reply, and per-student feedback bans.

**Architecture:** Add a dedicated feedback model/table and expose student-facing endpoints under `/api/feedback`, while admin review, reply, and ban controls live under `/api/admin/feedback`. The frontend adds one student page, one admin page, shared API types, and sidebar navigation entries that reuse the existing StudyAgent shell.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, Alembic, Pydantic, pytest, Vue 3, Vite, Element Plus.

---

### Task 1: Backend Feedback API

**Files:**
- Create: `backend/models/feedback.py`
- Create: `backend/routers/feedback.py`
- Create: `backend/alembic/versions/20260618_0010_add_student_feedback.py`
- Modify: `backend/models/__init__.py`
- Modify: `backend/models/user.py`
- Modify: `backend/models/schemas.py`
- Modify: `backend/routers/admin.py`
- Modify: `backend/main.py`
- Test: `tests/test_feedback.py`

- [ ] Write failing API tests covering student submission, two-per-day limit, admin list metadata, admin reply, feedback ban, and student-visible replies.
- [ ] Run `./.venv/bin/python -m pytest tests/test_feedback.py -q` and confirm tests fail because endpoints/models do not exist.
- [ ] Implement the model, schemas, migration, student router, and admin router handlers.
- [ ] Run `./.venv/bin/python -m pytest tests/test_feedback.py -q` and confirm the tests pass.

### Task 2: Frontend Feedback Pages

**Files:**
- Create: `frontend/src/views/StudentFeedback.vue`
- Create: `frontend/src/views/AdminFeedback.vue`
- Modify: `frontend/src/router/index.ts`
- Modify: `frontend/src/App.vue`
- Modify: `frontend/src/utils/api.ts`

- [ ] Add typed API helpers for student feedback, admin feedback list/reply, and ban toggling.
- [ ] Add the student page with submit form, remaining quota copy, and previous feedback/reply list.
- [ ] Add the admin page with feedback list, student class/name display, reply form, and ban/unban control.
- [ ] Add sidebar routes under “学生答疑” and admin navigation.

### Task 3: Verification

**Files:**
- Compile: `backend tests locustfile.py`
- Build: `frontend`

- [ ] Run `./.venv/bin/python -m pytest tests/test_feedback.py -q`.
- [ ] Run `./.venv/bin/python -m compileall backend tests locustfile.py`.
- [ ] Run `npm --prefix frontend run build`.
