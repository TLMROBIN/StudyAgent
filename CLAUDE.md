# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Status

项目处于规划阶段，详见 [DEVELOPMENT_PLAN.md](./DEVELOPMENT_PLAN.md)。

**目标**：局域网内高中学科 AI 答疑助手，基于 RAG + LLM，支持千人并发，平板可访问，拒绝闲聊。

**技术栈**：FastAPI + Vue 3 + ChromaDB + Redis + Nginx + Docker Compose，LLM 首选 DeepSeek（OpenAI 兼容接口）。

## Project Context

This repository lives alongside several sibling projects in `/home/binyu/文档/trae_projects/`:

- **EduSimu** — Educational animation platform (FastAPI + Vue 3 + SQLite/PostgreSQL). Handles multi-subject HTML animations, learning records, and ratings. Backend: `uvicorn app.main:app --reload` on port 8000; frontend: `npm run dev` inside `frontend/`.
- **Futureclaw** — School campus assistant bot (FastAPI + Vue 3 + ChromaDB + Dingtalk). Has RAG knowledge retrieval, APScheduler jobs, and Docker Compose deployment.
- **ClassManager Multi** — Class point/incentive management system (Node.js + Express + SQLite). Entry point: `npm start`; database init: `npm run init-db`.

These sibling projects may serve as design references for StudyAgent (e.g., shared patterns for FastAPI auth, Vue 3 frontend structure, SQLite/ChromaDB usage).

## Common Patterns in This Workspace

- **Python backend**: FastAPI + SQLAlchemy 2.0 + Pydantic, JWT auth via `python-jose` + `bcrypt`, Alembic for migrations
- **Frontend**: Vue 3 + Element Plus + Pinia + Vite, with a dev proxy to the backend
- **Database**: SQLite for local dev; ChromaDB added when RAG/vector search is needed
- **Deployment**: Systemd user services or Docker Compose + Nginx reverse proxy
