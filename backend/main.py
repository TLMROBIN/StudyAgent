import asyncio
from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from backend.config import get_settings
from backend.database import Base, SessionLocal, apply_runtime_schema_updates, engine
from backend.middleware.auth import RequestContextMiddleware
from backend.middleware.rate_limit import SlidingWindowRateLimitMiddleware
from backend.models import agent_config, audit_log, conversation, knowledge, user  # noqa: F401
from backend.observability import setup_logging
from backend.routers import admin, agent_config as agent_config_router, auth, chat, knowledge as knowledge_router, stats
from backend.security import get_password_hash
from backend.services.auth_service import auth_service
from backend.services.metrics_service import render_metrics
from backend.services.rag_service import rag_service
from backend.services.socratic_service import socratic_service
from backend.services.store_service import store

logger = logging.getLogger(__name__)


def seed_default_agent_config() -> None:
    from sqlalchemy import select

    from backend.models.agent_config import AgentConfig

    session = SessionLocal()
    try:
        active = session.scalar(select(AgentConfig).where(AgentConfig.is_active.is_(True)).limit(1))
        if active:
            return
        config = AgentConfig(
            version=1,
            system_prompt=socratic_service.base_prompt,
            guidance_params={"fallback_after_turns": 3, "max_guidance_turns": 4},
            subject_prompts={},
            filter_rules={"blocked_modes": ["chat", "prompt_injection", "non_subject"]},
            is_active=True,
        )
        session.add(config)
        session.commit()
    finally:
        session.close()


def warmup_embedding_model() -> None:
    try:
        rag_service.embedder.embed_text("数学 物理 预热")
    except Exception as exc:
        logger.warning("Embedding warmup failed: %s", exc)


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    apply_runtime_schema_updates()
    session = SessionLocal()
    try:
        auth_service.seed_bootstrap_admin(session)
    finally:
        session.close()
    seed_default_agent_config()
    warmup_task = asyncio.create_task(asyncio.to_thread(warmup_embedding_model))
    yield
    warmup_task.cancel()


settings = get_settings()
setup_logging(settings.log_level, settings.log_format)
app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(SlidingWindowRateLimitMiddleware, limit=240, window_seconds=60)
app.add_middleware(RequestContextMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(knowledge_router.router)
app.include_router(stats.router)
app.include_router(agent_config_router.router)
app.include_router(admin.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "store": store.health_snapshot(), "rag": rag_service.health_snapshot()}


@app.get("/metrics")
def metrics() -> Response:
    payload, content_type = render_metrics()
    return Response(content=payload, media_type=content_type)
