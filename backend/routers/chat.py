from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
import logging
import mimetypes
import re
from time import perf_counter
from typing import Any

from fastapi import APIRouter, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.database import SessionLocal
from backend.dependencies import CurrentUser, DbSession
from backend.models.agent_config import AgentConfig
from backend.models.conversation import (
    ChatMessageAttachment,
    Conversation,
    GuidanceStage,
    IMAGE_ONLY_MESSAGE_PLACEHOLDER,
    Message,
    MessageRole,
    normalize_conversation_seed,
)
from backend.models.llm_model import LLMModelConfig, QuotaBillingMode
from backend.models.knowledge import KnowledgeChunk
from backend.models.incentive import StudentIncentiveEvent
from backend.models.schemas import (
    ChatModelOptionRead,
    ChatModelQuotaRead,
    ChatModelStatusRead,
    ChatRequest,
    ConversationRead,
    QuestionRecommendationRead,
    QuestionRecommendationRequest,
    ResolveConversationRequest,
)
from backend.models.user import User, UserRole
from backend.services.audit_service import audit_service
from backend.services.agent_role_service import (
    ResolvedRoleSnapshot,
    agent_role_service,
    snapshot_from_replay,
)
from backend.services.chat_attachment_service import StoredChatAttachment, chat_attachment_service
from backend.services.chat_image_understanding_service import ImageUnderstandingResult, chat_image_understanding_service
from backend.services.filter_service import FilterDecision, filter_service
from backend.services.llm_quota_service import QuotaDenied, QuotaReservation, llm_quota_service
from backend.services.llm_service import LeadingModeTagParser, LLMService, LLMStreamEvent, LLMUsage, llm_service
from backend.services.metrics_service import (
    chat_fact_mode_total,
    chat_first_token_seconds,
    chat_full_response_seconds,
    chat_image_understanding_seconds,
    chat_intent_classify_seconds,
    chat_practice_review_total,
    chat_queue_wait_seconds,
    chat_rag_retrieval_seconds,
    chat_role_request_total,
    chat_role_resolution_seconds,
    chat_request_total,
    chat_suggested_replies_seconds,
    chat_stream_disconnect_total,
    chat_stream_safety_rewrite_total,
    filter_blocked_total,
    guidance_stage_total,
    llm_queue_depth,
    sse_active_connections,
)
from backend.services.question_cache_service import QuestionCacheLookup, question_cache_service
from backend.services.queue_service import QueueFullError, queue_service
from backend.services.rag_service import RetrievalResult, rag_service
from backend.services.request_replay_service import request_replay_service
from backend.services.physics_error_profile_service import physics_error_profile_service
from backend.services.physics_guidance_service import physics_guidance_service
from backend.services.socratic_service import socratic_service
from backend.services.subject_guidance_service import SUBJECT_STRATEGY_RULES, SubjectTeachingMode, subject_guidance_service
from backend.services.intent_classify_service import intent_classify_service
from backend.services.incentive_service import QualitySignalFilter, incentive_service
from backend.services.subject_profile_service import subject_profile_service
from backend.services.suggested_reply_service import suggested_reply_service
from backend.services.store_service import BaseStore, store
from backend.subjects import is_valid_subject
from backend.time_utils import now_utc

router = APIRouter(prefix="/api/chat", tags=["chat"])
logger = logging.getLogger(__name__)
STREAM_HEARTBEAT_SECONDS = 15
STREAM_FORCE_FLUSH_CHARS = 96
STREAM_GUARD_TAIL_CHARS = 24
STREAM_BOUNDARY_CHARS = {"。", "！", "？", "!", "?", "；", ";", "\n"}
SUGGESTED_REPLIES_TIMEOUT_SECONDS = 2.0
AGENT_CONFIG_CACHE_TTL_SECONDS = 60.0
RAG_SESSION_CACHE_TTL_SECONDS = 600
_active_agent_config_cache: tuple[AgentConfig | None, float, int | None] = (None, 0.0, None)
rag_session_store: BaseStore = store
EMPTY_CHAT_RESPONSE_FALLBACK = (
    "我刚刚没有生成出有效内容。我们换一种方式继续："
    "请你把题目条件或卡住的一步再发我一次，我会先帮你整理已知条件。"
)
ANSWER_SUBMISSION_RE = re.compile(r"[A-Da-d]|\d|[=≈]|^是|^不是|^对|^错|^能|^不能|^正确|^不正确")
ANSWER_REQUEST_RE = re.compile(r"直接(告诉|给|说)|答案是什么|是多少|不会|不知道|怎么做")
SHORT_FOLLOWUP_BLOCK_RE = re.compile(r"怎么|为什么|什么是|讲|解释|求|计算|证明|答案|不会|不知道|直接|再来|出一道|推荐")
SHORT_FOLLOWUP_PROMPT_MARKERS = ("？", "?", "请你", "你先", "试着", "能不能", "能否", "哪些", "什么条件", "是否", "吗")


def _looks_like_answer_submission(text: str) -> bool:
    stripped = text.strip()
    if not stripped or len(stripped) > 200:
        return False
    if ANSWER_REQUEST_RE.search(stripped):
        return False
    return bool(ANSWER_SUBMISSION_RE.search(stripped))


def _clip_followup_context(text: str, limit: int) -> str:
    normalized = re.sub(r"\s+", " ", (text or "").strip())
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit].rstrip()


def _previous_user_topic(history_pairs: list[tuple[str, str]]) -> str:
    for role, content in reversed(history_pairs):
        if role == MessageRole.USER.value and content.strip():
            return content.strip()
    return ""


def _previous_assistant_prompt(history_pairs: list[tuple[str, str]]) -> str:
    if not history_pairs or history_pairs[-1][0] != MessageRole.ASSISTANT.value:
        return ""
    prompt = history_pairs[-1][1].strip()
    if not prompt:
        return ""
    if not any(marker in prompt for marker in SHORT_FOLLOWUP_PROMPT_MARKERS):
        return ""
    return prompt


def _looks_like_short_followup_answer(text: str) -> bool:
    stripped = re.sub(r"\s+", "", (text or "").strip())
    if not stripped or len(stripped) > 24:
        return False
    if "?" in stripped or "？" in stripped:
        return False
    return not SHORT_FOLLOWUP_BLOCK_RE.search(stripped)


def _short_followup_context(
    *,
    subject: str,
    current_answer: str,
    history_pairs: list[tuple[str, str]],
) -> dict[str, str] | None:
    answer = (current_answer or "").strip()
    previous_prompt = _previous_assistant_prompt(history_pairs)
    if not previous_prompt or not _looks_like_short_followup_answer(answer):
        return None

    topic = _previous_user_topic(history_pairs)
    topic_line = f"上一轮学习主题：{_clip_followup_context(topic, 140)}\n" if topic else ""
    clipped_prompt = _clip_followup_context(previous_prompt, 360)
    clipped_answer = _clip_followup_context(answer, 80)
    prompt_question = (
        f"学生正在回答上一轮{subject}概念问题。\n"
        f"{topic_line}"
        f"上一轮导师问题：{clipped_prompt}\n"
        f"学生回答：{clipped_answer}\n"
        "请先判断学生回答是否完全或部分正确；如果不完整，用一句话补足关键条件；"
        "然后继续提出 1 个聚焦的引导问题。不要把它当成新的题目重新开始，也不要要求学生画无关图景。"
    )
    retrieval_query = " ".join(
        part
        for part in (
            _clip_followup_context(topic, 120),
            _clip_followup_context(previous_prompt, 180),
            clipped_answer,
        )
        if part
    )
    return {"prompt_question": prompt_question, "retrieval_query": retrieval_query or clipped_answer}


def _chat_model_key_or_422(model_key: str | None) -> str:
    try:
        return llm_service.normalize_chat_model_key(model_key)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


def _stream_llm_response(messages: list[dict[str, str]], fallback_text: str, *, model_key: str):
    try:
        return llm_service.stream_response(messages, fallback_text, model_key=model_key)
    except TypeError as exc:
        if "model_key" not in str(exc):
            raise
        return llm_service.stream_response(messages, fallback_text)


async def _stream_llm_events(
    messages: list[dict[str, str]],
    fallback_text: str,
    *,
    model_key: str,
    max_completion_tokens: int | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
):
    stream_response_func = getattr(llm_service.stream_response, "__func__", None)
    if stream_response_func is not LLMService.stream_response:
        async for chunk in _stream_llm_response(messages, fallback_text, model_key=model_key):
            yield LLMStreamEvent(type="chunk", content=chunk)
        return
    async for event in llm_service.stream_events(
        messages,
        fallback_text,
        model_key=model_key,
        max_completion_tokens=max_completion_tokens,
        temperature=temperature,
        top_p=top_p,
    ):
        yield event


def _subject_llm_params(guidance_params: dict | None, subject: str) -> dict[str, float | int]:
    """从 guidance_params（含 by_subject 分学科覆盖）提取合法的 LLM 采样参数。

    非法值（类型错/越界）忽略并告警，回落默认——配置错误不可击穿聊天链路。
    """
    effective = socratic_service._effective_guidance_params(guidance_params, subject)
    raw = effective.get("llm_params")
    result: dict[str, float | int] = {}
    if not isinstance(raw, dict):
        return result
    temperature = raw.get("temperature")
    if isinstance(temperature, (int, float)) and not isinstance(temperature, bool) and 0.0 <= float(temperature) <= 1.5:
        result["temperature"] = float(temperature)
    elif temperature is not None:
        logger.warning("忽略非法 llm_params.temperature=%r (subject=%s)", temperature, subject)
    top_p = raw.get("top_p")
    if isinstance(top_p, (int, float)) and not isinstance(top_p, bool) and 0.0 < float(top_p) <= 1.0:
        result["top_p"] = float(top_p)
    elif top_p is not None:
        logger.warning("忽略非法 llm_params.top_p=%r (subject=%s)", top_p, subject)
    max_tokens = raw.get("max_tokens")
    if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) and max_tokens > 0:
        result["max_tokens"] = max_tokens
    elif max_tokens is not None:
        logger.warning("忽略非法 llm_params.max_tokens=%r (subject=%s)", max_tokens, subject)
    return result


async def _classify_subject_mode(
    *,
    guidance_params: dict | None,
    question: str,
    subject: str,
    history_pairs: list[tuple[str, str]],
    has_image_turn: bool,
    practice_review_turn: bool,
    model_key: str | None,
) -> SubjectTeachingMode | None:
    """混合式意图识别：规则快路径 + 低置信时 LLM 分类兜底。

    仅在全部满足时才调用 LLM（默认关闭，配置灰度）：
    1. guidance_params.intent_classifier.enabled 为 True；
    2. 学科在注册表内且非物理（物理走深度专项服务）、命中可选学科白名单；
    3. 规则结果命中末位兜底规则（matched_by_trigger=False，低置信）；
    4. 非图片轮、非练习判卷轮。
    分类失败/超时/非法输出一律返回 None（fail-open 回纯规则）。
    """
    intent_cfg = (guidance_params or {}).get("intent_classifier")
    if not isinstance(intent_cfg, dict) or intent_cfg.get("enabled") is not True:
        return None
    if has_image_turn or practice_review_turn:
        return None
    if subject == "物理" or subject not in SUBJECT_STRATEGY_RULES:
        return None
    allowed_subjects = intent_cfg.get("subjects")
    if isinstance(allowed_subjects, list) and subject not in allowed_subjects:
        return None
    stage_estimate = socratic_service.infer_stage(len(history_pairs) // 2)
    rule_result = subject_guidance_service.analyze(question, subject, stage_estimate)
    if rule_result is None or rule_result.matched_by_trigger:
        return None
    return await intent_classify_service.classify(
        question=question,
        subject=subject,
        model_key=model_key,
        timeout_seconds=intent_cfg.get("timeout_seconds"),
    )


def _sse_event(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _split_stream_buffer(buffer: str, *, force: bool = False) -> tuple[list[str], str]:
    if not buffer:
        return [], ""

    segments: list[str] = []
    start = 0
    last_boundary = -1
    for index, char in enumerate(buffer):
        if char in STREAM_BOUNDARY_CHARS:
            segment = buffer[start : index + 1]
            if segment:
                segments.append(segment)
            start = index + 1
            last_boundary = index

    remainder = buffer[last_boundary + 1 :] if last_boundary >= 0 else buffer
    if force:
        if remainder:
            segments.append(remainder)
        return segments, ""

    if len(remainder) > STREAM_FORCE_FLUSH_CHARS:
        flush_upto = len(remainder) - STREAM_GUARD_TAIL_CHARS
        if flush_upto > 0:
            segments.append(remainder[:flush_upto])
            remainder = remainder[flush_upto:]

    return [segment for segment in segments if segment], remainder


def _compose_safe_rewrite(existing_text: str, rewrite_text: str) -> str:
    cleaned_existing = existing_text.rstrip()
    cleaned_rewrite = rewrite_text.strip()
    if not cleaned_existing:
        return cleaned_rewrite
    if not cleaned_rewrite:
        return cleaned_existing
    separator = "" if cleaned_existing.endswith(("。", "！", "？", "!", "?", "\n")) else "\n\n"
    return f"{cleaned_existing}{separator}{cleaned_rewrite}"


def _message_load_option():
    return selectinload(Conversation.messages).selectinload(Message.attachment)


def _history_pairs_before_turn(conversation: Conversation, turn_index: int) -> list[tuple[str, str]]:
    return [
        (message.role.value, message.content)
        for message in conversation.messages
        if message.turn_index < turn_index
    ]


def _assistant_message_for_turn(db: DbSession, conversation_id: int, turn_index: int) -> Message | None:
    return db.scalar(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.turn_index == turn_index,
            Message.role == MessageRole.ASSISTANT,
        )
        .order_by(Message.id.desc())
        .limit(1)
    )


def _user_message_for_turn(db: DbSession, conversation_id: int, turn_index: int) -> Message | None:
    return db.scalar(
        select(Message)
        .options(selectinload(Message.attachment))
        .where(
            Message.conversation_id == conversation_id,
            Message.turn_index == turn_index,
            Message.role == MessageRole.USER,
        )
        .order_by(Message.id.desc())
        .limit(1)
    )


def _retrieve_context_for_chat(subject: str, question: str, *, student_grade: int | None = None) -> RetrievalResult:
    session = SessionLocal()
    try:
        return rag_service.retrieve(session, subject, question, student_grade=student_grade)
    finally:
        session.close()


def _get_active_agent_config(db: DbSession) -> AgentConfig | None:
    global _active_agent_config_cache

    now = perf_counter()
    cached_config, expires_at, cached_bind_id = _active_agent_config_cache
    bind_id = id(db.get_bind())
    if cached_bind_id == bind_id and now < expires_at:
        return cached_config

    active_config = db.scalar(
        select(AgentConfig).where(AgentConfig.is_active.is_(True)).order_by(AgentConfig.version.desc())
    )
    _active_agent_config_cache = (active_config, now + AGENT_CONFIG_CACHE_TTL_SECONDS, bind_id)
    return active_config


def invalidate_active_agent_config_cache() -> None:
    global _active_agent_config_cache
    _active_agent_config_cache = (None, 0.0, None)


def _rag_session_cache_key(conversation_id: int) -> str:
    return f"rag_session:{conversation_id}"


def _rag_topic_fingerprint(topic: str) -> str:
    return sha256(topic[:120].encode("utf-8")).hexdigest()[:16]


def _rag_topic_seed(
    retrieval_query: str,
    history_pairs: list[tuple[str, str]],
    *,
    is_short_followup: bool,
) -> str:
    if not is_short_followup:
        return retrieval_query
    for role, content in reversed(history_pairs):
        if role != MessageRole.USER.value or not content.strip():
            continue
        if not _looks_like_short_followup_answer(content):
            return content.strip()
    return retrieval_query


def _load_rag_session_cache(
    db: DbSession,
    *,
    conversation_id: int,
    subject: str,
    agent_version: int,
    topic_fingerprint: str,
) -> RetrievalResult | None:
    try:
        cached_raw = rag_session_store.get(_rag_session_cache_key(conversation_id))
        if not cached_raw:
            return None
        cached = json.loads(cached_raw)
        if not isinstance(cached, dict):
            return None
        if (
            cached.get("subject") != subject
            or cached.get("agent_ver") != agent_version
            or cached.get("topic_fp") != topic_fingerprint
        ):
            return None
        chunk_ids = cached.get("chunk_ids")
        if not isinstance(chunk_ids, list) or not chunk_ids:
            return None
        if any(not isinstance(chunk_id, int) or isinstance(chunk_id, bool) for chunk_id in chunk_ids):
            return None
        rows = db.scalars(
            select(KnowledgeChunk)
            .options(selectinload(KnowledgeChunk.document))
            .where(KnowledgeChunk.id.in_(chunk_ids), KnowledgeChunk.is_disabled.is_(False))
        ).all()
        rows_by_id = {row.id: row for row in rows}
        if len(rows_by_id) != len(chunk_ids):
            return None
        ordered_rows = [rows_by_id[chunk_id] for chunk_id in chunk_ids]
        return RetrievalResult(context=rag_service.format_context(ordered_rows), chunks=ordered_rows)
    except Exception:
        logger.exception("RAG session cache read failed for conversation_id=%s", conversation_id)
        return None


def _store_rag_session_cache(
    *,
    conversation_id: int,
    subject: str,
    agent_version: int,
    topic_fingerprint: str,
    chunks: list[KnowledgeChunk],
) -> None:
    if not chunks:
        return
    try:
        rag_session_store.set(
            _rag_session_cache_key(conversation_id),
            json.dumps(
                {
                    "subject": subject,
                    "agent_ver": agent_version,
                    "topic_fp": topic_fingerprint,
                    "chunk_ids": [chunk.id for chunk in chunks],
                },
                ensure_ascii=False,
            ),
            ttl_seconds=RAG_SESSION_CACHE_TTL_SECONDS,
        )
    except Exception:
        logger.exception("RAG session cache write failed for conversation_id=%s", conversation_id)


def _recommendation_read(row, *, include_solutions: bool) -> QuestionRecommendationRead:
    metadata = row.metadata_json or {}
    document = row.document
    return QuestionRecommendationRead(
        chunk_id=row.id,
        document_id=row.document_id,
        document_filename=document.filename if document else None,
        subject=row.subject,
        resource_type=document.resource_type if document else str(metadata.get("resource_type") or ""),
        grade=metadata.get("grade") or (document.grade if document else None),
        chapter=metadata.get("chapter") or (document.chapter if document else None),
        section=metadata.get("section") or (document.section if document else None),
        difficulty=metadata.get("difficulty") or (document.difficulty if document else None),
        question_number=metadata.get("question_number"),
        question_text=str(metadata.get("question_text") or row.content),
        contains_images=bool(metadata.get("contains_images")),
        image_count=int(metadata.get("image_count") or 0),
        assets=list(metadata.get("asset_refs") or []),
        answer_text=metadata.get("answer_text") if include_solutions else None,
        explanation_text=metadata.get("explanation_text") if include_solutions else None,
    )


def _effective_recommendation_grade(current_user: User, payload: QuestionRecommendationRequest) -> int | None:
    if current_user.role == UserRole.STUDENT:
        return current_user.grade
    return payload.student_grade


def _conversation_recommendation_seed(conversation: Conversation) -> str:
    topic = conversation.topic.strip()
    recent_prompts: list[str] = []
    for message in reversed(conversation.messages):
        if message.role != MessageRole.USER:
            continue
        seed = normalize_conversation_seed(message.content)
        if not seed:
            continue
        if any(seed == existing for existing in recent_prompts):
            continue
        recent_prompts.append(seed)
        if len(recent_prompts) >= 3:
            break

    ordered_parts = [topic, *reversed(recent_prompts)]
    deduped_parts: list[str] = []
    for part in ordered_parts:
        normalized = part.strip()
        if not normalized:
            continue
        if any(normalized in existing or existing in normalized for existing in deduped_parts):
            continue
        deduped_parts.append(normalized)
    return "；".join(deduped_parts)[:500].rstrip("； ")


def _resolve_recommendation_query(
    db: DbSession,
    current_user: User,
    payload: QuestionRecommendationRequest,
) -> str:
    if payload.recommendation_mode == "keyword":
        return payload.question or ""

    conversation = db.scalar(
        select(Conversation)
        .options(_message_load_option())
        .where(
            Conversation.id == payload.conversation_id,
            Conversation.student_id == current_user.id,
            Conversation.deleted_by_student_at.is_(None),
        )
    )
    if not conversation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")

    seed = _conversation_recommendation_seed(conversation)
    if not seed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Conversation has no usable context")
    return seed


def _instant_stream(
    *,
    conversation_id: int,
    guidance_stage: str,
    content: str,
    request: Request,
    context_chunks: int = 0,
    assets: list[dict[str, Any]] | None = None,
    suggested_replies: list[str] | None = None,
    role_snapshot: ResolvedRoleSnapshot | None = None,
    incentive: dict[str, Any] | None = None,
):
    async def stream():
        sse_active_connections.inc()
        try:
            yield _sse_event(
                "meta",
                {
                    "conversation_id": conversation_id,
                    "guidance_stage": guidance_stage,
                    "queue_waiting_before": 0,
                    "context_chunks": context_chunks,
                    "request_id": getattr(request.state, "request_id", None),
                    **(role_snapshot or ResolvedRoleSnapshot.none()).meta_payload(),
                },
            )
            yield _sse_event(
                "done",
                {
                    "content": content,
                    "assets": list(assets or []),
                    "suggested_replies": list(suggested_replies or []),
                },
            )
            if incentive and (
                incentive.get("points_awarded")
                or incentive.get("new_badges")
                or incentive.get("level_up")
            ):
                yield _sse_event("incentive", incentive)
        finally:
            sse_active_connections.dec()

    return stream()


async def _generate_suggested_replies(
    *,
    subject: str,
    guidance_stage: GuidanceStage,
    current_question: str,
    assistant_response: str,
    history_pairs: list[tuple[str, str]],
    model_key: str | None,
) -> list[str]:
    started = perf_counter()
    try:
        return await asyncio.wait_for(
            suggested_reply_service.generate(
                subject=subject,
                guidance_stage=guidance_stage,
                current_question=current_question,
                assistant_response=assistant_response,
                history=history_pairs,
                model_key=model_key,
            ),
            timeout=SUGGESTED_REPLIES_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning(
            "suggested_replies_generation_failed subject=%s stage=%s error_type=%s error=%s",
            subject,
            guidance_stage.value,
            type(exc).__name__,
            str(exc)[:300],
        )
        return []
    finally:
        chat_suggested_replies_seconds.observe(perf_counter() - started)


def _normalize_practice_assets(raw_assets: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_assets, list):
        return []
    assets: list[dict[str, Any]] = []
    for item in raw_assets:
        if not isinstance(item, dict):
            continue
        asset_id = str(item.get("asset_id") or "").strip()
        url = str(item.get("url") or "").strip()
        content_type = str(item.get("content_type") or "").strip()
        filename = str(item.get("filename") or asset_id or "asset").strip()
        if not asset_id or not url or not content_type:
            continue
        assets.append(
            {
                "asset_id": asset_id,
                "filename": filename,
                "content_type": content_type,
                "url": url,
                "title": item.get("title"),
                "description": item.get("description"),
            }
        )
    return assets


def _ensure_practice_image_markers(question_text: str, assets: list[dict[str, Any]]) -> str:
    text = question_text.strip()
    if not assets or "【附图" in text:
        return text
    markers = []
    for index, asset in enumerate(assets, start=1):
        if not str(asset.get("content_type") or "").startswith("image/"):
            continue
        title = str(asset.get("title") or asset.get("filename") or "题图").strip()
        markers.append(f"【附图{index}：{title}】")
    if not markers:
        return text
    return f"{text}\n\n{''.join(markers)}"


def _practice_reference_from_metadata(metadata: dict[str, Any]) -> tuple[str | None, str | None]:
    answer_text = str(
        metadata.get("answer_text")
        or metadata.get("answer")
        or metadata.get("correct_answer")
        or metadata.get("standard_answer")
        or ""
    ).strip()
    explanation_text = str(
        metadata.get("explanation_text")
        or metadata.get("explanation")
        or metadata.get("solution_text")
        or metadata.get("analysis_text")
        or ""
    ).strip()
    return answer_text or None, explanation_text or None


def _practice_context(
    *,
    question_text: str,
    answer_text: str | None,
    explanation_text: str | None,
    source: str,
    issued_turn_index: int,
) -> dict[str, Any]:
    return {
        "question_text": question_text.strip(),
        "answer_text": answer_text.strip() if isinstance(answer_text, str) and answer_text.strip() else None,
        "explanation_text": explanation_text.strip() if isinstance(explanation_text, str) and explanation_text.strip() else None,
        "source": source,
        "issued_turn_index": issued_turn_index,
    }


def _physics_practice_from_row(row: Any, *, issued_turn_index: int) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    metadata = row.metadata_json or {}
    assets = _normalize_practice_assets(metadata.get("asset_refs"))
    question_text = str(metadata.get("question_text") or row.content or "").strip()
    question_text = _ensure_practice_image_markers(question_text, assets)
    answer_text, explanation_text = _practice_reference_from_metadata(metadata)
    number = str(metadata.get("question_number") or "").strip()
    title = f"**巩固练习：第{number}题**" if number else "**巩固练习**"
    content = (
        f"{title}\n\n"
        f"{question_text}\n\n"
        "先别急着要答案。请你先完成两步：\n"
        "1. 这道题应该先画哪类物理图景？\n"
        "2. 你判断它属于哪个物理模型？"
    )
    active_practice = _practice_context(
        question_text=question_text,
        answer_text=answer_text,
        explanation_text=explanation_text,
        source="question_bank",
        issued_turn_index=issued_turn_index,
    )
    return content, assets, active_practice


def _generated_physics_practice(
    question: str,
    *,
    profile_summary: dict[str, Any] | None = None,
    issued_turn_index: int,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    top_error_type = (profile_summary or {}).get("top_error_type")
    top_error_label = (profile_summary or {}).get("top_error_label")
    prefix = f"针对你的高频错因：{top_error_label}\n\n" if top_error_label else ""
    if top_error_type == "formula_misuse":
        stem = (
            "一个电热器标有 $220\\,\\text{V}$、$1100\\,\\text{W}$。现在把它接入实际电压为 $200\\,\\text{V}$ 的电路中。"
        )
        guide = "请先判断应该用额定功率还是实际功率公式，并写出公式适用条件；代入前检查单位。"
        answer_text = "实际功率约为 $909\\,\\text{W}$。"
        explanation_text = "先由额定值求电阻 $R=U^2/P=44\\,\\Omega$，再用实际电压计算 $P=U^2/R$。"
    elif top_error_type == "math_tool":
        stem = "一辆车以 $72\\,\\text{km/h}$ 的速度匀速行驶 $15\\,\\text{min}$，求通过的路程。"
        guide = "请先完成单位换算，再列式；特别检查 $\\text{km/h}$ 到 $\\text{m/s}$、分钟到秒。"
        answer_text = "$18\\,\\text{km}$。"
        explanation_text = "$72\\,\\text{km/h}=20\\,\\text{m/s}$，$15\\,\\text{min}=900\\,\\text{s}$，路程 $s=vt=18000\\,\\text{m}$。"
    elif top_error_type == "concept_confusion":
        stem = "同样大小的压力分别作用在针尖和手掌大小的接触面上，产生的效果明显不同。"
        guide = "请先用生活经验解释压力和压强的区别，再判断哪个物理量随受力面积改变。"
        answer_text = "压力大小相同，但压强不同；受力面积越小，压强越大。"
        explanation_text = "压强 $p=F/S$，压力相同时，接触面积越小压强越大，作用效果越明显。"
    elif top_error_type == "process_analysis":
        stem = "一个物块先沿光滑斜面下滑，随后进入粗糙水平面并逐渐停下。"
        guide = "请先把过程分成两个阶段，分别画出每段的运动状态和受力情况。"
        answer_text = "斜面段加速下滑；水平粗糙段受摩擦力减速，直到停止。"
        explanation_text = "光滑斜面上重力沿斜面分力提供加速度；粗糙水平面上摩擦力方向与运动方向相反。"
    elif any(keyword in question for keyword in ("电路", "电流", "电压", "电阻")):
        stem = (
            "一个电路中有电源、开关、定值电阻 $R_1$ 和滑动变阻器 $R_2$ 串联，"
            "电压表测 $R_2$ 两端电压。闭合开关后，滑片向右移动时，电压表示数发生变化。"
        )
        guide = "请先画等效电路图，并判断电流路径有几条。"
        answer_text = "需要先明确滑动变阻器接入的是哪一段电阻；若接入电阻变大，$R_2$ 分压变大。"
        explanation_text = "串联电路电流相同，分压大小与电阻成正比，不能只凭“向右移动”直接判断。"
    elif any(keyword in question for keyword in ("能量", "机械能", "动能", "势能")):
        stem = "一个小球从光滑斜面顶端由静止释放，滑到底端后进入粗糙水平面并逐渐停下。"
        guide = "请先画能量转化图，标出每一段能量从哪里来、到哪里去。"
        answer_text = "斜面段重力势能转化为动能；粗糙水平面上机械能转化为内能。"
        explanation_text = "光滑斜面近似机械能守恒；水平粗糙段摩擦力做负功，机械能减少。"
    else:
        stem = (
            "一个物块在水平桌面上受到水平向右的拉力 $F=5\\,\\text{N}$，"
            "并做匀速直线运动。物块重力为 $20\\,\\text{N}$。"
        )
        guide = "请先画受力分析图，标出重力、支持力、拉力和摩擦力。"
        answer_text = "摩擦力大小为 $5\\,\\text{N}$，方向水平向左；支持力大小为 $20\\,\\text{N}$。"
        explanation_text = "匀速直线运动表示合力为零，水平方向拉力与摩擦力平衡，竖直方向支持力与重力平衡。"
    content = (
        "**巩固练习（系统生成）**\n\n"
        f"{prefix}"
        f"{stem}\n\n"
        f"{guide}\n"
        "先回答：这题属于什么物理模型？你会列出哪一个方向上的关系式？"
    )
    active_practice = _practice_context(
        question_text=stem,
        answer_text=answer_text,
        explanation_text=explanation_text,
        source="generated",
        issued_turn_index=issued_turn_index,
    )
    return content, [], active_practice


def _build_physics_practice_response(
    db: DbSession,
    *,
    subject: str,
    question: str,
    student_id: int,
    student_grade: int | None,
    issued_turn_index: int,
) -> tuple[str, list[dict[str, Any]], int, dict[str, Any]]:
    profile_summary = physics_error_profile_service.profile_summary(db, student_id=student_id, subject=subject)
    rag_started = perf_counter()
    try:
        rows = rag_service.recommend_questions(
            db,
            subject,
            question,
            student_grade=student_grade,
            limit=1,
            difficulty_preference="basic",
        )
    finally:
        chat_rag_retrieval_seconds.observe(perf_counter() - rag_started)
    if rows:
        content, assets, active_practice = _physics_practice_from_row(rows[0], issued_turn_index=issued_turn_index)
        if profile_summary.get("top_error_label"):
            content = f"针对你的高频错因：{profile_summary['top_error_label']}\n\n{content}"
        return content, assets, 1, active_practice
    content, assets, active_practice = _generated_physics_practice(
        question,
        profile_summary=profile_summary,
        issued_turn_index=issued_turn_index,
    )
    return content, assets, 0, active_practice


def _math_practice_from_row(row: Any, *, issued_turn_index: int) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    metadata = row.metadata_json or {}
    assets = _normalize_practice_assets(metadata.get("asset_refs"))
    question_text = str(metadata.get("question_text") or row.content or "").strip()
    question_text = _ensure_practice_image_markers(question_text, assets)
    answer_text, explanation_text = _practice_reference_from_metadata(metadata)
    number = str(metadata.get("question_number") or "").strip()
    title = f"**数学巩固练习：第{number}题**" if number else "**数学巩固练习**"
    content = (
        f"{title}\n\n"
        f"{question_text}\n\n"
        "先别急着要答案。请你先完成两步：\n"
        "1. 圈出已知量、未知量和目标关系。\n"
        "2. 判断这题应该用定义、公式、图形关系，还是设元列方程。"
    )
    active_practice = _practice_context(
        question_text=question_text,
        answer_text=answer_text,
        explanation_text=explanation_text,
        source="question_bank",
        issued_turn_index=issued_turn_index,
    )
    return content, assets, active_practice


def _generated_math_practice(
    question: str,
    *,
    profile_summary: dict[str, Any] | None = None,
    issued_turn_index: int,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    top_error_type = (profile_summary or {}).get("top_error_type")
    top_error_label = (profile_summary or {}).get("top_error_label")
    prefix = f"针对你的高频错因：{top_error_label}\n\n" if top_error_label else ""
    if top_error_type == "word_problem_modeling" or any(keyword in question for keyword in ("应用题", "数量关系", "设", "列方程", "行程")):
        stem = "甲、乙两人同时从相距 $600\\,\\text{m}$ 的两地相向而行。甲每分钟走 $70\\,\\text{m}$，乙每分钟走 $50\\,\\text{m}$，几分钟后两人相遇？"
        guide = "请按“识别量→说关系→转方程”来做：先圈出速度、时间、路程，再用一句话说出总路程和两人路程的关系。"
        answer_text = "5 分钟。"
        explanation_text = "两人相向而行，总速度为 $70+50=120\\,\\text{m/min}$，时间 $600\\div120=5$。"
    elif top_error_type == "calculation_error":
        stem = "化简：$3(2x-1)-2(x+4)$。"
        guide = "请先逐项展开，再合并同类项；特别检查负号和括号。"
        answer_text = "$4x-11$。"
        explanation_text = "$3(2x-1)=6x-3$，$-2(x+4)=-2x-8$，合并同类项得到 $4x-11$。"
    elif top_error_type == "concept_confusion":
        stem = "已知函数 $y=2x+1$。请判断当 $x$ 增大时，$y$ 如何变化。"
        guide = "请先用一个具体数值例子建立直觉，再说出一次函数中系数 $2$ 的意义。"
        answer_text = "$y$ 随 $x$ 的增大而增大。"
        explanation_text = "一次函数的系数 $2>0$，表示 $x$ 每增加 1，$y$ 增加 2。"
    else:
        stem = "已知 $2x+3=11$，求 $x$。"
        guide = "请先说出目标是把哪个量单独留下，再说明第一步为什么要两边同时减去 $3$。"
        answer_text = "$x=4$。"
        explanation_text = "两边先减去 3 得 $2x=8$，再两边同时除以 2，得到 $x=4$。"
    content = (
        "**数学巩固练习（系统生成）**\n\n"
        f"{prefix}"
        f"{stem}\n\n"
        f"{guide}\n"
        "最后一步计算请你自己完成，我可以继续帮你检查思路。"
    )
    active_practice = _practice_context(
        question_text=stem,
        answer_text=answer_text,
        explanation_text=explanation_text,
        source="generated",
        issued_turn_index=issued_turn_index,
    )
    return content, [], active_practice


def _build_math_practice_response(
    db: DbSession,
    *,
    subject: str,
    question: str,
    student_id: int,
    student_grade: int | None,
    issued_turn_index: int,
) -> tuple[str, list[dict[str, Any]], int, dict[str, Any]]:
    profile_summary = subject_profile_service.profile_summary(db, student_id=student_id, subject=subject)
    rag_started = perf_counter()
    try:
        rows = rag_service.recommend_questions(
            db,
            subject,
            question,
            student_grade=student_grade,
            limit=1,
            difficulty_preference="basic",
        )
    finally:
        chat_rag_retrieval_seconds.observe(perf_counter() - rag_started)
    if rows:
        content, assets, active_practice = _math_practice_from_row(rows[0], issued_turn_index=issued_turn_index)
        if profile_summary.get("top_error_label"):
            content = f"针对你的高频错因：{profile_summary['top_error_label']}\n\n{content}"
        return content, assets, 1, active_practice
    content, assets, active_practice = _generated_math_practice(
        question,
        profile_summary=profile_summary,
        issued_turn_index=issued_turn_index,
    )
    return content, assets, 0, active_practice


def _physics_error_record_evidence(prompt_question: str, history_pairs: list[tuple[str, str]]) -> str:
    current = prompt_question.strip()
    if "：" in current:
        suffix = current.rsplit("：", 1)[-1].strip()
        if suffix:
            return suffix
    if ":" in current:
        suffix = current.rsplit(":", 1)[-1].strip()
        if suffix:
            return suffix
    for role, content in reversed(history_pairs):
        if role == MessageRole.USER.value and content.strip():
            return content.strip()
    return current


def _physics_error_record_response(error_type: str, knowledge_point: str | None, profile_summary: dict[str, Any]) -> str:
    label = profile_summary.get("top_error_label") or error_type
    count = int(profile_summary.get("error_counts", {}).get(error_type, 0))
    point_text = f"；关联知识点：{knowledge_point}" if knowledge_point else ""
    return (
        f"已记录到物理错因档案：{label}{point_text}。\n\n"
        f"这类错因目前累计 {count} 次。后面你说“再来一题”时，我会优先围绕这个弱点出巩固题。"
    )


def _subject_error_record_response(subject: str, error_type: str, knowledge_point: str | None, profile_summary: dict[str, Any]) -> str:
    label = profile_summary.get("top_error_label") or error_type
    count = int(profile_summary.get("error_counts", {}).get(error_type, 0))
    point_text = f"；关联知识点：{knowledge_point}" if knowledge_point else ""
    return (
        f"已记录到{subject}错因档案：{label}{point_text}。\n\n"
        f"这类错因目前累计 {count} 次。后面你说“再来一题”时，我会优先围绕这个弱点出巩固题。"
    )


def _english_vocabulary_response(result: dict[str, Any]) -> str:
    added = [str(word) for word in result.get("added", [])]
    if added:
        return (
            f"已加入英语词汇 DNA：{', '.join(added)}。\n\n"
            "下一次复习时，先说词义和词性，再用它自己造一个短句。"
        )
    return "这次没有识别到新的英文词汇。你可以直接发：把 photosynthesis 存入词汇DNA。"


def _chinese_material_response(result: dict[str, Any]) -> str:
    tags = [str(tag) for tag in result.get("tags", []) if str(tag).strip()]
    tag_text = "、".join(tags) if tags else "待归类"
    return (
        f"已存入语文素材库，标签：{tag_text}。\n\n"
        "写作时你可以说“帮我找关于某个主题的素材”，我会优先从素材库里帮你调取。"
    )


def _record_turn_incentives(
    db: DbSession,
    *,
    student_id: int,
    conversation_id: int,
    turn_index: int,
    subject: str,
    followup_answered: bool,
    practice_review_turn: bool,
    response_text: str,
    incentive_params: dict[str, Any] | None,
    llm_quality: str | None = None,
) -> dict[str, Any] | None:
    params = incentive_params or {}
    if not params.get("enabled"):
        return None
    if followup_answered and params.get("followup_min_interval_seconds", 0) > 0:
        recent_followup = db.scalar(
            select(StudentIncentiveEvent.id).where(
                StudentIncentiveEvent.student_id == student_id,
                StudentIncentiveEvent.event_type == "followup_answered",
                StudentIncentiveEvent.created_at
                >= now_utc() - timedelta(seconds=params["followup_min_interval_seconds"]),
            ).limit(1)
        )
        followup_answered = recent_followup is None
    verdict = incentive_service.extract_practice_verdict(response_text) if practice_review_turn else None
    drafts = incentive_service.evaluate_turn(
        student_id=student_id,
        conversation_id=conversation_id,
        turn_index=turn_index,
        subject=subject,
        followup_answered=followup_answered,
        practice_verdict=verdict,
        first_learning_turn_today=True,
        params=params,
    )
    if llm_quality:
        for draft in drafts:
            draft.payload["llm_quality"] = llm_quality
    if not drafts:
        return None
    return incentive_service.record_events(
        db,
        student_id=student_id,
        drafts=drafts,
        params=params,
    ).as_dict()


def _persist_direct_assistant_response(
    db: DbSession,
    *,
    conversation: Conversation,
    user_turn_index: int,
    subject: str,
    guidance_stage: GuidanceStage,
    selected_model_key: str,
    response_text: str,
    assets: list[dict[str, Any]] | None = None,
    active_practice: dict[str, Any] | None = None,
    student_id: int | None = None,
    followup_answered: bool = False,
    practice_review_turn: bool = False,
    incentive_params: dict[str, Any] | None = None,
) -> tuple[str, list[dict[str, Any]], dict[str, Any] | None]:
    response_assets = list(assets or [])
    conversation.subject = subject
    conversation.guidance_stage = guidance_stage
    if active_practice is not None:
        conversation.active_practice = active_practice
    db.add(conversation)
    existing_assistant = _assistant_message_for_turn(db, conversation.id, user_turn_index)
    if existing_assistant:
        db.commit()
        return existing_assistant.content, list(existing_assistant.assets or []), None

    assistant_message = Message(
        conversation_id=conversation.id,
        role=MessageRole.ASSISTANT,
        content=response_text,
        assets=response_assets,
        turn_index=user_turn_index,
        guidance_stage=guidance_stage,
        llm_model_key=selected_model_key,
    )
    db.add(assistant_message)
    db.flush()
    grant = None
    if student_id is not None:
        grant = _record_turn_incentives(
            db,
            student_id=student_id,
            conversation_id=conversation.id,
            turn_index=user_turn_index,
            subject=subject,
            followup_answered=followup_answered,
            practice_review_turn=practice_review_turn,
            response_text=response_text,
            incentive_params=incentive_params,
        )
    guidance_stage_total.labels(stage=guidance_stage.value).inc()
    db.commit()
    return response_text, response_assets, grant


def _complete_early_special_response(
    db: DbSession,
    *,
    conversation: Conversation,
    user_turn_index: int,
    current_user: User,
    payload: ChatRequest,
    request: Request,
    request_fingerprint: str,
    subject: str,
    guidance_stage: GuidanceStage,
    selected_model_key: str,
    response_text: str,
    assets: list[dict[str, Any]] | None = None,
    context_chunks: int = 0,
    active_practice: dict[str, Any] | None = None,
    role_snapshot: ResolvedRoleSnapshot,
    incentive_params: dict[str, Any] | None = None,
    followup_answered: bool = False,
) -> StreamingResponse:
    bypassed_role = role_snapshot.bypassed()
    response_text, response_assets, grant = _persist_direct_assistant_response(
        db,
        conversation=conversation,
        user_turn_index=user_turn_index,
        subject=subject,
        guidance_stage=guidance_stage,
        selected_model_key=selected_model_key,
        response_text=response_text,
        assets=assets,
        active_practice=active_practice,
        student_id=current_user.id,
        followup_answered=followup_answered,
        incentive_params=incentive_params,
    )
    if payload.request_id:
        request_replay_service.mark_completed(
            user_id=current_user.id,
            request_id=payload.request_id,
            question_hash=request_fingerprint,
            conversation_id=conversation.id,
            turn_index=user_turn_index,
            subject=subject,
            guidance_stage=guidance_stage,
            final_content=response_text,
            assets=response_assets,
            role_snapshot=bypassed_role.replay_snapshot(),
        )
    return StreamingResponse(
        _instant_stream(
            conversation_id=conversation.id,
            guidance_stage=guidance_stage.value,
            content=response_text,
            request=request,
            context_chunks=context_chunks,
            assets=response_assets,
            role_snapshot=bypassed_role,
            incentive=grant,
        ),
        media_type="text/event-stream",
    )


def _ensure_conversation(db: DbSession, student_id: int, payload: ChatRequest) -> Conversation:
    if payload.conversation_id:
        conversation = db.scalar(
            select(Conversation)
            .options(_message_load_option())
            .where(
                Conversation.id == payload.conversation_id,
                Conversation.student_id == student_id,
                Conversation.deleted_by_student_at.is_(None),
            )
        )
        if not conversation:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
        return conversation

    conversation = Conversation(student_id=student_id, subject=payload.subject)
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


def _normalize_chat_message_content(message: str, *, has_attachment: bool) -> str:
    trimmed = (message or "").strip()
    if trimmed:
        return trimmed
    if has_attachment:
        return IMAGE_ONLY_MESSAGE_PLACEHOLDER
    return ""


async def _parse_stream_request_payload(request: Request) -> tuple[ChatRequest, UploadFile | None]:
    content_type = (request.headers.get("content-type") or "").lower()
    if "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
        form = await request.form()
        image_items = [item for item in form.getlist("image") if item]
        if len(image_items) > 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only one chat image is allowed")
        image = image_items[0] if image_items else None
        if image is not None and not all(hasattr(image, attr) for attr in ("filename", "content_type", "read")):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid chat image upload")
        conversation_id_raw = str(form.get("conversation_id") or "").strip()
        request_id_raw = str(form.get("request_id") or "").strip()
        role_id_raw = str(form.get("role_id") or "").strip()
        try:
            payload = ChatRequest.model_validate(
                {
                    "subject": str(form.get("subject") or "").strip(),
                    "message": str(form.get("message") or ""),
                    "conversation_id": conversation_id_raw or None,
                    "request_id": request_id_raw or None,
                    "llm_model": str(form.get("llm_model") or "").strip() or None,
                    "role_id": role_id_raw or None,
                }
            )
        except ValidationError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid chat payload") from exc
        return payload, image

    body = await request.json()
    try:
        return ChatRequest.model_validate(body), None
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid chat payload") from exc


def _build_filter_question(*, payload_message: str, understanding: ImageUnderstandingResult | None) -> str:
    payload_text = (payload_message or "").strip()
    if understanding is None:
        return payload_text
    if payload_text and understanding.filter_text:
        return f"{payload_text}\n{understanding.filter_text}".strip()
    return payload_text or understanding.filter_text


def _build_prompt_question(*, payload_message: str, subject: str, understanding: ImageUnderstandingResult | None) -> str:
    payload_text = (payload_message or "").strip()
    if payload_text:
        return payload_text
    if understanding and understanding.prompt_summary:
        return understanding.prompt_summary
    return socratic_service.placeholder_question(subject)


def _build_short_circuit_reply(subject: str, understanding: ImageUnderstandingResult | None = None) -> str:
    if understanding and not understanding.is_academic:
        return socratic_service.image_off_topic_text()
    return socratic_service.image_low_confidence_text(
        subject,
        image_summary=understanding.prompt_summary if understanding else None,
        quality_issues=understanding.quality_issues if understanding else None,
    )


def _should_short_circuit_image_turn(
    *,
    payload_message: str,
    understanding: ImageUnderstandingResult | None,
) -> bool:
    if not understanding or not understanding.must_short_circuit:
        return False
    if understanding.is_academic:
        return True
    return not (payload_message or "").strip()


def _build_image_grounded_fallback(
    *,
    fallback_text: str,
    subject: str,
    understanding: ImageUnderstandingResult | None,
) -> str:
    if not understanding or understanding.confidence_level == "low":
        return fallback_text
    summary = " ".join((understanding.prompt_summary or "").split())
    if not summary:
        return fallback_text
    if len(summary) > 160:
        summary = f"{summary[:160].rstrip()}..."
    return f"我识别到这张{subject}题图里主要有：{summary}\n\n{fallback_text}"


def _chat_model_read_from_config(db: DbSession, current_user: User, model: LLMModelConfig) -> ChatModelOptionRead:
    policy = model.quota_policy
    snapshot = llm_quota_service.quota_snapshot_for_user(db=db, user=current_user, model_config=model)
    return ChatModelOptionRead(
        key=model.model_key,
        name=model.display_name,
        description=model.description,
        billing_mode=(policy.billing_mode.value if policy else QuotaBillingMode.FREE_LOCAL.value),
        quota=ChatModelQuotaRead(**snapshot.__dict__),
    )


@router.get("/models", response_model=list[ChatModelOptionRead])
def list_chat_models(db: DbSession, current_user: CurrentUser) -> list[ChatModelOptionRead]:
    configured = db.scalars(
        select(LLMModelConfig)
        .options(selectinload(LLMModelConfig.quota_policy))
        .where(LLMModelConfig.is_enabled.is_(True), LLMModelConfig.capability_text.is_(True))
        .order_by(LLMModelConfig.sort_order.asc(), LLMModelConfig.id.asc())
    ).all()
    if configured:
        return [_chat_model_read_from_config(db, current_user, item) for item in configured]
    return [
        ChatModelOptionRead(
            **item,
            quota=ChatModelQuotaRead(quota_exhausted=False, message="默认模型"),
        )
        for item in llm_service.chat_model_options()
    ]


@router.get("/models/status", response_model=list[ChatModelStatusRead])
async def list_chat_model_statuses(current_user: CurrentUser) -> list[ChatModelStatusRead]:
    return [ChatModelStatusRead(**item) for item in await llm_service.chat_model_statuses()]


@router.get("/history", response_model=list[ConversationRead])
def list_conversations(db: DbSession, current_user: CurrentUser) -> list[ConversationRead]:
    conversations = db.scalars(
        select(Conversation)
        .options(_message_load_option())
        .where(Conversation.student_id == current_user.id, Conversation.deleted_by_student_at.is_(None))
        .order_by(Conversation.updated_at.desc())
    ).all()
    return [ConversationRead.model_validate(item) for item in conversations]


@router.get("/history/{conversation_id}", response_model=ConversationRead)
def get_conversation(conversation_id: int, db: DbSession, current_user: CurrentUser) -> ConversationRead:
    conversation = db.scalar(
        select(Conversation)
        .options(_message_load_option())
        .where(
            Conversation.id == conversation_id,
            Conversation.student_id == current_user.id,
            Conversation.deleted_by_student_at.is_(None),
        )
    )
    if not conversation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    return ConversationRead.model_validate(conversation)


@router.get("/attachments/{attachment_id}")
def get_chat_attachment(attachment_id: int, db: DbSession, current_user: CurrentUser):
    attachment = db.scalar(select(ChatMessageAttachment).where(ChatMessageAttachment.id == attachment_id))
    if not attachment or attachment.owner_student_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found")
    file_path = chat_attachment_service.resolve_path(attachment.storage_key)
    if not file_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found")
    media_type = mimetypes.guess_type(file_path.name)[0] or attachment.mime_type or "application/octet-stream"
    return FileResponse(file_path, media_type=media_type, filename=attachment.original_filename)


@router.post("/{conversation_id}/resolve", response_model=ConversationRead)
def resolve_conversation(
    conversation_id: int,
    payload: ResolveConversationRequest,
    db: DbSession,
    current_user: CurrentUser,
) -> ConversationRead:
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.student_id == current_user.id,
            Conversation.deleted_by_student_at.is_(None),
        )
    )
    if not conversation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    grant: dict[str, Any] | None = None
    first_resolve = payload.resolved and not conversation.resolved
    conversation.resolved = payload.resolved
    db.add(conversation)
    if first_resolve:
        active_config = _get_active_agent_config(db)
        params = incentive_service.resolve_config(active_config.guidance_params if active_config else None)
        if params.get("enabled"):
            snapshot = incentive_service.conversation_signal_snapshot(db, conversation.id)
            had_followup = bool(
                db.scalar(
                    select(StudentIncentiveEvent.id).where(
                        StudentIncentiveEvent.student_id == current_user.id,
                        StudentIncentiveEvent.conversation_id == conversation.id,
                        StudentIncentiveEvent.event_type == "followup_answered",
                    ).limit(1)
                )
            )
            reflection = payload.reflection
            if reflection:
                recent_reflections = db.scalars(
                    select(StudentIncentiveEvent)
                    .where(
                        StudentIncentiveEvent.student_id == current_user.id,
                        StudentIncentiveEvent.event_type == "reflection_submitted",
                    )
                    .order_by(StudentIncentiveEvent.created_at.desc())
                    .limit(5)
                ).all()
                if any(
                    incentive_service.reflections_are_similar(
                        str((item.payload or {}).get("reflection") or ""),
                        reflection,
                    )
                    for item in recent_reflections
                ):
                    reflection = None
            drafts = incentive_service.evaluate_resolve(
                conversation_id=conversation.id,
                subject=conversation.subject,
                user_turn_count=snapshot["user_turn_count"],
                had_followup=had_followup,
                had_fallback=snapshot["had_fallback"],
                reflection=reflection,
                params=params,
            )
            if drafts:
                grant = incentive_service.record_events(
                    db,
                    student_id=current_user.id,
                    drafts=drafts,
                    params=params,
                ).as_dict()
    db.commit()
    db.refresh(conversation)
    return ConversationRead.model_validate(conversation).model_copy(update={"incentive": grant})


@router.delete("/{conversation_id}")
def delete_conversation(conversation_id: int, db: DbSession, current_user: CurrentUser, request: Request) -> dict[str, str]:
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.student_id == current_user.id,
            Conversation.deleted_by_student_at.is_(None),
        )
    )
    if not conversation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    conversation.deleted_by_student_at = datetime.now(UTC)
    db.add(conversation)
    db.commit()
    audit_service.log(
        db,
        actor=current_user,
        action="student_clear_conversation",
        target_type="conversation",
        target_id=str(conversation_id),
        result="success",
        ip_address=request.client.host if request.client else None,
        detail={"student_id": current_user.id, "subject": conversation.subject},
    )
    return {"status": "deleted"}


@router.post("/recommendations", response_model=list[QuestionRecommendationRead])
def recommend_questions(
    payload: QuestionRecommendationRequest,
    db: DbSession,
    current_user: CurrentUser,
) -> list[QuestionRecommendationRead]:
    if not is_valid_subject(payload.subject):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unsupported subject")
    recommendation_query = _resolve_recommendation_query(db, current_user, payload)
    decision = filter_service.check_question(recommendation_query, payload.subject)
    if not decision.allowed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Question is not a supported academic prompt")

    subject = decision.subject or payload.subject
    include_solutions = bool(payload.include_solutions and current_user.role in {UserRole.TEACHER, UserRole.ADMIN})
    rows = rag_service.recommend_questions(
        db,
        subject,
        recommendation_query,
        student_grade=_effective_recommendation_grade(current_user, payload),
        limit=payload.limit,
        difficulty_preference=payload.difficulty_preference,
    )
    return [_recommendation_read(row, include_solutions=include_solutions) for row in rows]


@router.post("/stream")
async def stream_chat_endpoint(request: Request, db: DbSession, current_user: CurrentUser):
    payload, image_upload = await _parse_stream_request_payload(request)
    return await stream_chat(payload, db, current_user, request, image_upload=image_upload)


async def stream_chat(
    payload: ChatRequest,
    db: DbSession,
    current_user: CurrentUser,
    request: Request,
    *,
    image_upload: UploadFile | None = None,
):
    if not is_valid_subject(payload.subject):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unsupported subject")
    started = perf_counter()
    chat_request_total.inc()
    has_image_turn = image_upload is not None
    selected_model_key = _chat_model_key_or_422(payload.llm_model)
    image_content: bytes | None = None
    stored_attachment: StoredChatAttachment | None = None
    attachment_record: ChatMessageAttachment | None = None
    image_understanding: ImageUnderstandingResult | None = None
    user_message_content = _normalize_chat_message_content(payload.message, has_attachment=has_image_turn)
    image_sha256: str | None = None

    if has_image_turn:
        image_content = await image_upload.read()
        image_sha256 = sha256(image_content or b"").hexdigest()
    request_fingerprint = request_replay_service.fingerprint(
        subject=payload.subject,
        question=user_message_content,
        conversation_id=payload.conversation_id,
        image_sha256=image_sha256,
        llm_model=selected_model_key,
        requested_role_id=payload.role_id,
    )

    replay_state = request_replay_service.load(user_id=current_user.id, request_id=payload.request_id)
    if replay_state and replay_state.question_hash != request_fingerprint:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Request id already used with different payload")

    conversation: Conversation | None = None
    if replay_state:
        conversation = db.scalar(
            select(Conversation)
            .options(_message_load_option())
            .where(
                Conversation.id == replay_state.conversation_id,
                Conversation.student_id == current_user.id,
                Conversation.deleted_by_student_at.is_(None),
            )
        )
        if not conversation:
            replay_state = None
        elif replay_state.status == "completed" and replay_state.final_content:
            completed_role = snapshot_from_replay(replay_state.role_snapshot, payload.role_id) or ResolvedRoleSnapshot.none(
                payload.role_id
            )
            return StreamingResponse(
                _instant_stream(
                    conversation_id=conversation.id,
                    guidance_stage=replay_state.guidance_stage or conversation.guidance_stage.value,
                    content=replay_state.final_content,
                    request=request,
                    assets=list(replay_state.assets or []),
                    suggested_replies=list(replay_state.suggested_replies or []),
                    role_snapshot=completed_role,
                ),
                media_type="text/event-stream",
            )

    role_snapshot = snapshot_from_replay(replay_state.role_snapshot, payload.role_id) if replay_state else None
    if role_snapshot is None:
        role_started = perf_counter()
        try:
            role_snapshot = agent_role_service.resolve(db, payload.role_id, payload.subject)
        finally:
            chat_role_resolution_seconds.observe(perf_counter() - role_started)
        chat_role_request_total.labels(status=role_snapshot.status).inc()

    if not conversation:
        conversation = _ensure_conversation(db, current_user.id, payload)

    if replay_state:
        user_turn_index = replay_state.turn_index
        history_pairs = _history_pairs_before_turn(conversation, user_turn_index)
        replayed_user_message = _user_message_for_turn(db, conversation.id, user_turn_index)
        attachment_record = replayed_user_message.attachment if replayed_user_message else None
        if attachment_record:
            image_content = chat_attachment_service.resolve_path(attachment_record.storage_key).read_bytes()
            has_image_turn = True
        existing_assistant = _assistant_message_for_turn(db, conversation.id, user_turn_index)
        if existing_assistant:
            if payload.request_id:
                request_replay_service.mark_completed(
                    user_id=current_user.id,
                    request_id=payload.request_id,
                    question_hash=request_fingerprint,
                    conversation_id=conversation.id,
                    turn_index=user_turn_index,
                    subject=conversation.subject,
                    guidance_stage=existing_assistant.guidance_stage,
                    final_content=existing_assistant.content,
                    assets=list(existing_assistant.assets or []),
                    suggested_replies=list(existing_assistant.suggested_replies or []),
                    role_snapshot=role_snapshot.replay_snapshot(),
                )
            return StreamingResponse(
                _instant_stream(
                    conversation_id=conversation.id,
                    guidance_stage=existing_assistant.guidance_stage.value,
                    content=existing_assistant.content,
                    request=request,
                    assets=list(existing_assistant.assets or []),
                    suggested_replies=list(existing_assistant.suggested_replies or []),
                    role_snapshot=role_snapshot,
                ),
                media_type="text/event-stream",
            )
    else:
        history_pairs = [(message.role.value, message.content) for message in conversation.messages]
        user_turn_index = len([message for message in conversation.messages if message.role == MessageRole.USER]) + 1
        if has_image_turn:
            stored_attachment = chat_attachment_service.save_bytes(
                content=image_content or b"",
                filename=image_upload.filename or "chat-image.png",
                content_type=image_upload.content_type,
                student_id=current_user.id,
                conversation_id=conversation.id,
            )
        user_message = Message(
            conversation_id=conversation.id,
            role=MessageRole.USER,
            content=user_message_content,
            turn_index=user_turn_index,
            guidance_stage=conversation.guidance_stage,
        )
        try:
            db.add(user_message)
            db.flush()
            if stored_attachment:
                attachment_record = ChatMessageAttachment(
                    message_id=user_message.id,
                    owner_student_id=current_user.id,
                    storage_key=stored_attachment.storage_key,
                    original_filename=stored_attachment.original_filename,
                    mime_type=stored_attachment.mime_type,
                    file_size=stored_attachment.file_size,
                    sha256=stored_attachment.sha256,
                )
                db.add(attachment_record)
            db.commit()
        except Exception:
            db.rollback()
            if stored_attachment:
                chat_attachment_service.delete(stored_attachment.storage_key)
            raise
        db.refresh(user_message)
        if attachment_record:
            db.refresh(attachment_record)
        if payload.request_id:
            request_replay_service.remember_request(
                user_id=current_user.id,
                request_id=payload.request_id,
                question_hash=request_fingerprint,
                conversation_id=conversation.id,
                turn_index=user_turn_index,
                subject=payload.subject,
                role_snapshot=role_snapshot.replay_snapshot(),
            )

    if has_image_turn and image_content and attachment_record:
        image_understanding_started = perf_counter()
        try:
            image_understanding = await chat_image_understanding_service.understand(
                image_bytes=image_content,
                mime_type=attachment_record.mime_type,
                subject=payload.subject,
                user_text=payload.message,
                model_key=selected_model_key,
                image_path=str(chat_attachment_service.resolve_path(attachment_record.storage_key)),
                attachment_id=attachment_record.id,
            )
        finally:
            chat_image_understanding_seconds.observe(perf_counter() - image_understanding_started)
        attachment_record.ocr_status = {
            "paddleocr": "paddleocr",
            "ocr": "llm_ocr",
            "multimodal": "multimodal_fallback",
            "off_topic": "off_topic",
            "failed": "failed",
        }.get(image_understanding.source, "pending")
        attachment_record.ocr_confidence = image_understanding.ocr_confidence_value
        attachment_record.understanding_json = (
            json.dumps(image_understanding.understanding_json, ensure_ascii=False)
            if image_understanding.understanding_json is not None
            else None
        )
        db.add(attachment_record)
        db.commit()

    filter_question = _build_filter_question(payload_message=payload.message, understanding=image_understanding)
    if has_image_turn and _should_short_circuit_image_turn(
        payload_message=payload.message,
        understanding=image_understanding,
    ):
        bypassed_role = role_snapshot.bypassed()
        short_circuit_text = _build_short_circuit_reply(payload.subject, image_understanding)
        existing_assistant = _assistant_message_for_turn(db, conversation.id, user_turn_index)
        if not existing_assistant:
            assistant_message = Message(
                conversation_id=conversation.id,
                role=MessageRole.ASSISTANT,
                content=short_circuit_text,
                turn_index=user_turn_index,
                guidance_stage=conversation.guidance_stage,
                llm_model_key=selected_model_key,
            )
            db.add(assistant_message)
            db.commit()
            guidance_stage_total.labels(stage=conversation.guidance_stage.value).inc()
        if payload.request_id:
            request_replay_service.mark_completed(
                user_id=current_user.id,
                request_id=payload.request_id,
                question_hash=request_fingerprint,
                conversation_id=conversation.id,
                turn_index=user_turn_index,
                subject=payload.subject,
                guidance_stage=conversation.guidance_stage,
                final_content=short_circuit_text,
                role_snapshot=bypassed_role.replay_snapshot(),
            )
        return StreamingResponse(
            _instant_stream(
                conversation_id=conversation.id,
                guidance_stage=conversation.guidance_stage.value,
                content=short_circuit_text,
                request=request,
                role_snapshot=bypassed_role,
            ),
            media_type="text/event-stream",
        )

    practice_review_turn = bool(conversation.active_practice and _looks_like_answer_submission(payload.message))
    decision = filter_service.check_question(filter_question, payload.subject)
    if (
        not decision.allowed
        and decision.reason == "subject_not_recognized"
        and practice_review_turn
    ):
        decision = FilterDecision(True, "practice_answer_submission", payload.subject or conversation.subject)
    if not decision.allowed:
        bypassed_role = role_snapshot.bypassed()
        filter_blocked_total.inc()
        refusal = filter_service.refusal_text
        existing_assistant = _assistant_message_for_turn(db, conversation.id, user_turn_index)
        if not existing_assistant:
            assistant_message = Message(
                conversation_id=conversation.id,
                role=MessageRole.ASSISTANT,
                content=refusal,
                turn_index=user_turn_index,
                guidance_stage=conversation.guidance_stage,
                llm_model_key=selected_model_key,
            )
            db.add(assistant_message)
            db.commit()
            guidance_stage_total.labels(stage=conversation.guidance_stage.value).inc()
        if payload.request_id:
            request_replay_service.mark_completed(
                user_id=current_user.id,
                request_id=payload.request_id,
                question_hash=request_fingerprint,
                conversation_id=conversation.id,
                turn_index=user_turn_index,
                subject=payload.subject,
                guidance_stage=conversation.guidance_stage,
                final_content=refusal,
                role_snapshot=bypassed_role.replay_snapshot(),
            )
        return StreamingResponse(
            _instant_stream(
                conversation_id=conversation.id,
                guidance_stage=conversation.guidance_stage.value,
                content=refusal,
                request=request,
                role_snapshot=bypassed_role,
            ),
            media_type="text/event-stream",
        )

    subject = decision.subject or payload.subject
    prompt_question = _build_prompt_question(payload_message=payload.message, subject=subject, understanding=image_understanding)
    followup_context = (
        None
        if has_image_turn or practice_review_turn
        else _short_followup_context(
            subject=subject,
            current_answer=prompt_question,
            history_pairs=history_pairs,
        )
    )
    retrieval_query = filter_question or prompt_question
    if followup_context:
        prompt_question = followup_context["prompt_question"]
        retrieval_query = followup_context["retrieval_query"]

    active_config = _get_active_agent_config(db)
    incentive_params = incentive_service.resolve_config(active_config.guidance_params if active_config else None)
    early_stage = socratic_service.infer_stage(len(history_pairs) // 2)
    if not has_image_turn and not practice_review_turn:
        if subject == "数学" and subject_profile_service.is_record_request(prompt_question, subject=subject):
            user_message = _user_message_for_turn(db, conversation.id, user_turn_index)
            event = subject_profile_service.record_error_event(
                db,
                student_id=current_user.id,
                subject=subject,
                conversation_id=conversation.id,
                message_id=user_message.id if user_message else None,
                evidence_text=_physics_error_record_evidence(prompt_question, history_pairs),
            )
            profile_summary = subject_profile_service.profile_summary(db, student_id=current_user.id, subject=subject)
            response_text = _subject_error_record_response(subject, event.error_type, event.knowledge_point, profile_summary)
            return _complete_early_special_response(
                db,
                conversation=conversation,
                user_turn_index=user_turn_index,
                current_user=current_user,
                payload=payload,
                request=request,
                request_fingerprint=request_fingerprint,
                subject=subject,
                guidance_stage=early_stage,
                selected_model_key=selected_model_key,
                response_text=response_text,
                role_snapshot=role_snapshot,
                incentive_params=incentive_params,
                followup_answered=followup_context is not None,
            )
        if subject == "数学" and subject_guidance_service.is_math_practice_request(prompt_question):
            response_text, response_assets, practice_context_chunks, active_practice = _build_math_practice_response(
                db,
                subject=subject,
                question=retrieval_query,
                student_id=current_user.id,
                student_grade=current_user.grade,
                issued_turn_index=user_turn_index,
            )
            return _complete_early_special_response(
                db,
                conversation=conversation,
                user_turn_index=user_turn_index,
                current_user=current_user,
                payload=payload,
                request=request,
                request_fingerprint=request_fingerprint,
                subject=subject,
                guidance_stage=early_stage,
                selected_model_key=selected_model_key,
                response_text=response_text,
                assets=response_assets,
                context_chunks=practice_context_chunks,
                active_practice=active_practice,
                role_snapshot=role_snapshot,
                incentive_params=incentive_params,
                followup_answered=followup_context is not None,
            )
        if subject == "英语" and subject_profile_service.is_vocabulary_request(prompt_question):
            result = subject_profile_service.add_english_vocabulary(
                db,
                student_id=current_user.id,
                source_text=prompt_question,
            )
            return _complete_early_special_response(
                db,
                conversation=conversation,
                user_turn_index=user_turn_index,
                current_user=current_user,
                payload=payload,
                request=request,
                request_fingerprint=request_fingerprint,
                subject=subject,
                guidance_stage=early_stage,
                selected_model_key=selected_model_key,
                response_text=_english_vocabulary_response(result),
                role_snapshot=role_snapshot,
                incentive_params=incentive_params,
                followup_answered=followup_context is not None,
            )
        if subject == "语文" and subject_profile_service.is_chinese_material_request(prompt_question):
            result = subject_profile_service.add_chinese_material(
                db,
                student_id=current_user.id,
                source_text=prompt_question,
            )
            return _complete_early_special_response(
                db,
                conversation=conversation,
                user_turn_index=user_turn_index,
                current_user=current_user,
                payload=payload,
                request=request,
                request_fingerprint=request_fingerprint,
                subject=subject,
                guidance_stage=early_stage,
                selected_model_key=selected_model_key,
                response_text=_chinese_material_response(result),
                role_snapshot=role_snapshot,
                incentive_params=incentive_params,
                followup_answered=followup_context is not None,
            )
        if subject == "物理" and physics_error_profile_service.is_record_request(prompt_question):
            user_message = _user_message_for_turn(db, conversation.id, user_turn_index)
            event = physics_error_profile_service.record_event(
                db,
                student_id=current_user.id,
                subject=subject,
                conversation_id=conversation.id,
                message_id=user_message.id if user_message else None,
                evidence_text=_physics_error_record_evidence(prompt_question, history_pairs),
            )
            profile_summary = physics_error_profile_service.profile_summary(
                db,
                student_id=current_user.id,
                subject=subject,
            )
            response_text = _physics_error_record_response(event.error_type, event.knowledge_point, profile_summary)
            return _complete_early_special_response(
                db,
                conversation=conversation,
                user_turn_index=user_turn_index,
                current_user=current_user,
                payload=payload,
                request=request,
                request_fingerprint=request_fingerprint,
                subject=subject,
                guidance_stage=early_stage,
                selected_model_key=selected_model_key,
                response_text=response_text,
                role_snapshot=role_snapshot,
                incentive_params=incentive_params,
                followup_answered=followup_context is not None,
            )
        if subject == "物理" and physics_guidance_service.is_practice_request(prompt_question):
            response_text, response_assets, practice_context_chunks, active_practice = _build_physics_practice_response(
                db,
                subject=subject,
                question=retrieval_query,
                student_id=current_user.id,
                student_grade=current_user.grade,
                issued_turn_index=user_turn_index,
            )
            return _complete_early_special_response(
                db,
                conversation=conversation,
                user_turn_index=user_turn_index,
                current_user=current_user,
                payload=payload,
                request=request,
                request_fingerprint=request_fingerprint,
                subject=subject,
                guidance_stage=early_stage,
                selected_model_key=selected_model_key,
                response_text=response_text,
                assets=response_assets,
                context_chunks=practice_context_chunks,
                active_practice=active_practice,
                role_snapshot=role_snapshot,
                incentive_params=incentive_params,
                followup_answered=followup_context is not None,
            )

    rag_agent_version = active_config.version if active_config else 0
    rag_topic_seed = _rag_topic_seed(
        retrieval_query,
        history_pairs,
        is_short_followup=followup_context is not None,
    )
    rag_topic_fingerprint = _rag_topic_fingerprint(rag_topic_seed)
    retrieval = None
    if not has_image_turn:
        retrieval = _load_rag_session_cache(
            db,
            conversation_id=conversation.id,
            subject=subject,
            agent_version=rag_agent_version,
            topic_fingerprint=rag_topic_fingerprint,
        )
    if retrieval is None:
        rag_started = perf_counter()
        try:
            retrieval = await asyncio.to_thread(
                _retrieve_context_for_chat,
                subject,
                retrieval_query,
                student_grade=current_user.grade,
            )
        finally:
            chat_rag_retrieval_seconds.observe(perf_counter() - rag_started)
        if not has_image_turn:
            _store_rag_session_cache(
                conversation_id=conversation.id,
                subject=subject,
                agent_version=rag_agent_version,
                topic_fingerprint=rag_topic_fingerprint,
                chunks=retrieval.chunks,
            )
    subject_supplement = ((active_config.subject_prompts or {}).get(subject) or None) if active_config else None
    subject_llm_params = _subject_llm_params(active_config.guidance_params if active_config else None, subject)
    intent_started = perf_counter()
    try:
        subject_mode_override = await _classify_subject_mode(
            guidance_params=active_config.guidance_params if active_config else None,
            question=prompt_question,
            subject=subject,
            history_pairs=history_pairs,
            has_image_turn=has_image_turn,
            practice_review_turn=practice_review_turn,
            model_key=selected_model_key,
        )
    finally:
        chat_intent_classify_seconds.observe(perf_counter() - intent_started)
    prompt = socratic_service.build_prompt(
        question=prompt_question,
        subject=subject,
        history=history_pairs,
        retrieved_context=retrieval.context,
        system_prompt=active_config.system_prompt if active_config else socratic_service.base_prompt,
        student_grade=current_user.grade,
        image_summary=image_understanding.prompt_summary if image_understanding else None,
        image_confidence=image_understanding.confidence_level if image_understanding else None,
        image_uncertainties=image_understanding.uncertainties if image_understanding else None,
        image_related=has_image_turn,
        guidance_params=active_config.guidance_params if active_config else None,
        practice_context=conversation.active_practice if practice_review_turn else None,
        subject_supplement=subject_supplement,
        subject_mode_override=subject_mode_override,
        role_prompt=role_snapshot.rendered_prompt if role_snapshot.applied else None,
    )
    bypassed_role = role_snapshot.bypassed()
    if subject == "数学" and subject_profile_service.is_record_request(prompt_question, subject=subject):
        user_message = _user_message_for_turn(db, conversation.id, user_turn_index)
        event = subject_profile_service.record_error_event(
            db,
            student_id=current_user.id,
            subject=subject,
            conversation_id=conversation.id,
            message_id=user_message.id if user_message else None,
            evidence_text=_physics_error_record_evidence(prompt_question, history_pairs),
        )
        profile_summary = subject_profile_service.profile_summary(db, student_id=current_user.id, subject=subject)
        response_text = _subject_error_record_response(subject, event.error_type, event.knowledge_point, profile_summary)
        response_text, response_assets, grant = _persist_direct_assistant_response(
            db,
            conversation=conversation,
            user_turn_index=user_turn_index,
            subject=subject,
            guidance_stage=prompt.stage,
            selected_model_key=selected_model_key,
            response_text=response_text,
            student_id=current_user.id,
            followup_answered=followup_context is not None,
            incentive_params=incentive_params,
        )
        if payload.request_id:
            request_replay_service.mark_completed(
                user_id=current_user.id,
                request_id=payload.request_id,
                question_hash=request_fingerprint,
                conversation_id=conversation.id,
                turn_index=user_turn_index,
                subject=subject,
                guidance_stage=prompt.stage,
                final_content=response_text,
                assets=response_assets,
                role_snapshot=bypassed_role.replay_snapshot(),
            )
        return StreamingResponse(
            _instant_stream(
                conversation_id=conversation.id,
                guidance_stage=prompt.stage.value,
                content=response_text,
                request=request,
                assets=response_assets,
                role_snapshot=bypassed_role,
                incentive=grant,
            ),
            media_type="text/event-stream",
        )
    if subject == "数学" and subject_guidance_service.is_math_practice_request(prompt_question):
        response_text, response_assets, practice_context_chunks, active_practice = _build_math_practice_response(
            db,
            subject=subject,
            question=retrieval_query,
            student_id=current_user.id,
            student_grade=current_user.grade,
            issued_turn_index=user_turn_index,
        )
        response_text, response_assets, grant = _persist_direct_assistant_response(
            db,
            conversation=conversation,
            user_turn_index=user_turn_index,
            subject=subject,
            guidance_stage=prompt.stage,
            selected_model_key=selected_model_key,
            response_text=response_text,
            assets=response_assets,
            active_practice=active_practice,
            student_id=current_user.id,
            followup_answered=followup_context is not None,
            incentive_params=incentive_params,
        )
        if payload.request_id:
            request_replay_service.mark_completed(
                user_id=current_user.id,
                request_id=payload.request_id,
                question_hash=request_fingerprint,
                conversation_id=conversation.id,
                turn_index=user_turn_index,
                subject=subject,
                guidance_stage=prompt.stage,
                final_content=response_text,
                assets=response_assets,
                role_snapshot=bypassed_role.replay_snapshot(),
            )
        return StreamingResponse(
            _instant_stream(
                conversation_id=conversation.id,
                guidance_stage=prompt.stage.value,
                content=response_text,
                request=request,
                context_chunks=practice_context_chunks,
                assets=response_assets,
                role_snapshot=bypassed_role,
                incentive=grant,
            ),
            media_type="text/event-stream",
        )
    if subject == "英语" and subject_profile_service.is_vocabulary_request(prompt_question):
        result = subject_profile_service.add_english_vocabulary(
            db,
            student_id=current_user.id,
            source_text=prompt_question,
        )
        response_text = _english_vocabulary_response(result)
        response_text, response_assets, grant = _persist_direct_assistant_response(
            db,
            conversation=conversation,
            user_turn_index=user_turn_index,
            subject=subject,
            guidance_stage=prompt.stage,
            selected_model_key=selected_model_key,
            response_text=response_text,
            student_id=current_user.id,
            followup_answered=followup_context is not None,
            incentive_params=incentive_params,
        )
        if payload.request_id:
            request_replay_service.mark_completed(
                user_id=current_user.id,
                request_id=payload.request_id,
                question_hash=request_fingerprint,
                conversation_id=conversation.id,
                turn_index=user_turn_index,
                subject=subject,
                guidance_stage=prompt.stage,
                final_content=response_text,
                assets=response_assets,
                role_snapshot=bypassed_role.replay_snapshot(),
            )
        return StreamingResponse(
            _instant_stream(
                conversation_id=conversation.id,
                guidance_stage=prompt.stage.value,
                content=response_text,
                request=request,
                assets=response_assets,
                role_snapshot=bypassed_role,
                incentive=grant,
            ),
            media_type="text/event-stream",
        )
    if subject == "语文" and subject_profile_service.is_chinese_material_request(prompt_question):
        result = subject_profile_service.add_chinese_material(
            db,
            student_id=current_user.id,
            source_text=prompt_question,
        )
        response_text = _chinese_material_response(result)
        response_text, response_assets, grant = _persist_direct_assistant_response(
            db,
            conversation=conversation,
            user_turn_index=user_turn_index,
            subject=subject,
            guidance_stage=prompt.stage,
            selected_model_key=selected_model_key,
            response_text=response_text,
            student_id=current_user.id,
            followup_answered=followup_context is not None,
            incentive_params=incentive_params,
        )
        if payload.request_id:
            request_replay_service.mark_completed(
                user_id=current_user.id,
                request_id=payload.request_id,
                question_hash=request_fingerprint,
                conversation_id=conversation.id,
                turn_index=user_turn_index,
                subject=subject,
                guidance_stage=prompt.stage,
                final_content=response_text,
                assets=response_assets,
                role_snapshot=bypassed_role.replay_snapshot(),
            )
        return StreamingResponse(
            _instant_stream(
                conversation_id=conversation.id,
                guidance_stage=prompt.stage.value,
                content=response_text,
                request=request,
                assets=response_assets,
                role_snapshot=bypassed_role,
                incentive=grant,
            ),
            media_type="text/event-stream",
        )
    if subject == "物理" and physics_error_profile_service.is_record_request(prompt_question):
        user_message = _user_message_for_turn(db, conversation.id, user_turn_index)
        event = physics_error_profile_service.record_event(
            db,
            student_id=current_user.id,
            subject=subject,
            conversation_id=conversation.id,
            message_id=user_message.id if user_message else None,
            evidence_text=_physics_error_record_evidence(prompt_question, history_pairs),
        )
        profile_summary = physics_error_profile_service.profile_summary(db, student_id=current_user.id, subject=subject)
        response_text = _physics_error_record_response(event.error_type, event.knowledge_point, profile_summary)
        response_text, response_assets, grant = _persist_direct_assistant_response(
            db,
            conversation=conversation,
            user_turn_index=user_turn_index,
            subject=subject,
            guidance_stage=prompt.stage,
            selected_model_key=selected_model_key,
            response_text=response_text,
            student_id=current_user.id,
            followup_answered=followup_context is not None,
            incentive_params=incentive_params,
        )
        if payload.request_id:
            request_replay_service.mark_completed(
                user_id=current_user.id,
                request_id=payload.request_id,
                question_hash=request_fingerprint,
                conversation_id=conversation.id,
                turn_index=user_turn_index,
                subject=subject,
                guidance_stage=prompt.stage,
                final_content=response_text,
                role_snapshot=bypassed_role.replay_snapshot(),
            )
        return StreamingResponse(
            _instant_stream(
                conversation_id=conversation.id,
                guidance_stage=prompt.stage.value,
                content=response_text,
                request=request,
                role_snapshot=bypassed_role,
                incentive=grant,
            ),
            media_type="text/event-stream",
        )
    if subject == "物理" and physics_guidance_service.is_practice_request(prompt_question):
        response_text, response_assets, practice_context_chunks, active_practice = _build_physics_practice_response(
            db,
            subject=subject,
            question=retrieval_query,
            student_id=current_user.id,
            student_grade=current_user.grade,
            issued_turn_index=user_turn_index,
        )
        response_text, response_assets, grant = _persist_direct_assistant_response(
            db,
            conversation=conversation,
            user_turn_index=user_turn_index,
            subject=subject,
            guidance_stage=prompt.stage,
            selected_model_key=selected_model_key,
            response_text=response_text,
            assets=response_assets,
            active_practice=active_practice,
            student_id=current_user.id,
            followup_answered=followup_context is not None,
            incentive_params=incentive_params,
        )
        if payload.request_id:
            request_replay_service.mark_completed(
                user_id=current_user.id,
                request_id=payload.request_id,
                question_hash=request_fingerprint,
                conversation_id=conversation.id,
                turn_index=user_turn_index,
                subject=subject,
                guidance_stage=prompt.stage,
                final_content=response_text,
                assets=response_assets,
                role_snapshot=bypassed_role.replay_snapshot(),
            )
        return StreamingResponse(
            _instant_stream(
                conversation_id=conversation.id,
                guidance_stage=prompt.stage.value,
                content=response_text,
                request=request,
                context_chunks=practice_context_chunks,
                assets=response_assets,
                role_snapshot=bypassed_role,
                incentive=grant,
            ),
            media_type="text/event-stream",
        )
    fallback_text = _build_image_grounded_fallback(
        fallback_text=prompt.fallback_text,
        subject=subject,
        understanding=image_understanding if has_image_turn else None,
    )
    cache_lookup = QuestionCacheLookup(cache_key=None, answer=None)
    selected_model_config = db.scalar(
        select(LLMModelConfig)
        .options(selectinload(LLMModelConfig.quota_policy), selectinload(LLMModelConfig.provider_account))
        .where(LLMModelConfig.model_key == selected_model_key, LLMModelConfig.is_enabled.is_(True))
    )
    if not practice_review_turn and question_cache_service.is_cacheable(
        history_pairs=history_pairs,
        question=retrieval_query,
        has_image_turn=has_image_turn,
    ):
        cache_lookup = question_cache_service.lookup(
            subject=subject,
            question=retrieval_query,
            guidance_stage=prompt.stage,
            agent_version=active_config.version if active_config else 0,
            chunks=retrieval.chunks,
            llm_model=selected_model_key,
            teaching_mode=subject_mode_override.value if subject_mode_override else None,
            role_revision_hash=role_snapshot.content_hash if role_snapshot.applied else None,
        )
        if cache_lookup.answer:
            response_text = cache_lookup.answer
            suggested_replies: list[str] = []
            grant: dict[str, Any] | None = None
            conversation.subject = subject
            conversation.guidance_stage = prompt.stage
            db.add(conversation)
            existing_assistant = _assistant_message_for_turn(db, conversation.id, user_turn_index)
            if existing_assistant:
                suggested_replies = list(existing_assistant.suggested_replies or [])
            else:
                suggested_replies = await _generate_suggested_replies(
                    subject=subject,
                    guidance_stage=prompt.stage,
                    current_question=prompt_question,
                    assistant_response=response_text,
                    history_pairs=history_pairs,
                    model_key=selected_model_key,
                )
                assistant_message = Message(
                    conversation_id=conversation.id,
                    role=MessageRole.ASSISTANT,
                    content=response_text,
                    suggested_replies=suggested_replies,
                    turn_index=user_turn_index,
                    guidance_stage=prompt.stage,
                    llm_model_key=selected_model_key,
                    agent_role_revision_id=role_snapshot.revision_id if role_snapshot.applied else None,
                    agent_role_snapshot=role_snapshot.public_snapshot() if role_snapshot.applied else None,
                )
                db.add(assistant_message)
                db.flush()
                grant = _record_turn_incentives(
                    db,
                    student_id=current_user.id,
                    conversation_id=conversation.id,
                    turn_index=user_turn_index,
                    subject=subject,
                    followup_answered=followup_context is not None,
                    practice_review_turn=False,
                    response_text=response_text,
                    incentive_params=incentive_params,
                )
                guidance_stage_total.labels(stage=prompt.stage.value).inc()
            db.commit()
            if payload.request_id:
                request_replay_service.mark_completed(
                    user_id=current_user.id,
                    request_id=payload.request_id,
                    question_hash=request_fingerprint,
                    conversation_id=conversation.id,
                    turn_index=user_turn_index,
                    subject=subject,
                    guidance_stage=prompt.stage,
                    final_content=response_text,
                    suggested_replies=suggested_replies,
                    role_snapshot=role_snapshot.replay_snapshot(),
                )
            return StreamingResponse(
                _instant_stream(
                    conversation_id=conversation.id,
                    guidance_stage=prompt.stage.value,
                    content=response_text,
                    request=request,
                    context_chunks=len(retrieval.chunks),
                    suggested_replies=suggested_replies,
                    role_snapshot=role_snapshot,
                    incentive=grant,
                ),
                media_type="text/event-stream",
            )

    quota_reservation: QuotaReservation | None = None
    quota_usage: LLMUsage | None = None
    if selected_model_config is not None:
        quota_result = llm_quota_service.check_and_reserve(
            db=db,
            user=current_user,
            model_config=selected_model_config,
            request_id=payload.request_id,
            prompt_messages=prompt.messages,
        )
        if isinstance(quota_result, QuotaDenied):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE
                if quota_result.code == "llm_quota_unavailable"
                else status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "code": quota_result.code,
                    "message": quota_result.message,
                    "model_key": quota_result.model_key,
                    "billing_mode": quota_result.billing_mode,
                    "reset_hint": quota_result.reset_hint,
                },
            )
        quota_reservation = quota_result

    queue_wait_started = perf_counter()
    try:
        llm_queue_depth.set(queue_service.waiting)
        ticket_context = queue_service.reserve()
        ticket = await ticket_context.__aenter__()
    except QueueFullError as exc:
        if quota_reservation is not None:
            llm_quota_service.release(quota_reservation)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="当前排队人数较多，请稍后重试") from exc
    finally:
        chat_queue_wait_seconds.observe(perf_counter() - queue_wait_started)
        llm_queue_depth.set(queue_service.waiting)

    async def event_stream():
        nonlocal quota_usage
        emitted_text = ""
        emitted_visible = False
        mode_parser = LeadingModeTagParser() if prompt.fact_mode_offered else None
        quality_filter = QualitySignalFilter() if incentive_params.get("llm_signal_enabled") else None
        resolved_mode: str | None = None
        pending_buffer = ""
        llm_stream = None
        disconnected = False
        first_token_observed = False
        should_send_done = True
        stop_streaming = False
        provider_stream_started = False
        usage_recorded = False
        ticket_released = False
        suggested_replies: list[str] = []
        turn_persisted = False
        assistant_message_id: int | None = None
        practice_review_persisted = False
        incentive_grant: dict[str, Any] | None = None
        sse_active_connections.inc()

        async def release_ticket_once() -> None:
            nonlocal ticket_released
            if ticket_released:
                return
            with suppress(Exception):
                await ticket_context.__aexit__(None, None, None)
            ticket_released = True
            llm_queue_depth.set(queue_service.waiting)

        try:
            yield _sse_event(
                "meta",
                {
                    "conversation_id": conversation.id,
                    "guidance_stage": prompt.stage.value,
                    "queue_waiting_before": ticket.waiting_before,
                    "context_chunks": len(retrieval.chunks),
                    "request_id": getattr(request.state, "request_id", None),
                    **role_snapshot.meta_payload(),
                },
            )
            quota_max_tokens = (
                selected_model_config.quota_policy.max_completion_tokens
                if selected_model_config and selected_model_config.quota_policy
                else None
            )
            subject_max_tokens = subject_llm_params.get("max_tokens")
            candidate_max_tokens = [value for value in (quota_max_tokens, subject_max_tokens) if value]
            llm_stream = _stream_llm_events(
                prompt.messages,
                fallback_text,
                model_key=selected_model_key,
                max_completion_tokens=min(candidate_max_tokens) if candidate_max_tokens else None,
                temperature=subject_llm_params.get("temperature"),
                top_p=subject_llm_params.get("top_p"),
            )

            while True:
                if await request.is_disconnected():
                    disconnected = True
                    should_send_done = False
                    chat_stream_disconnect_total.inc()
                    break

                try:
                    provider_event = await asyncio.wait_for(anext(llm_stream), timeout=STREAM_HEARTBEAT_SECONDS)
                except TimeoutError:
                    yield _sse_event("heartbeat", {"conversation_id": conversation.id})
                    continue
                except StopAsyncIteration:
                    break

                if provider_event.type == "usage":
                    quota_usage = provider_event.usage
                    continue

                provider_chunk = provider_event.content
                provider_stream_started = provider_stream_started or bool(provider_chunk)
                if mode_parser is not None and resolved_mode is None:
                    parsed_mode, provider_chunk = mode_parser.feed(provider_chunk)
                    if parsed_mode is not None:
                        resolved_mode = parsed_mode
                if quality_filter is not None:
                    provider_chunk = quality_filter.feed(provider_chunk)
                pending_buffer += provider_chunk
                segments, pending_buffer = _split_stream_buffer(pending_buffer)
                for segment in segments:
                    if not emitted_visible:
                        segment = segment.lstrip()
                        if not segment:
                            continue
                        emitted_visible = True
                    candidate_text = f"{emitted_text}{segment}"
                    validation = filter_service.validate_answer(candidate_text, skip_direct_answer=practice_review_turn, subject=subject)
                    if not validation.allowed:
                        chat_stream_safety_rewrite_total.inc()
                        rewritten_text = _compose_safe_rewrite(
                            emitted_text,
                            socratic_service.safe_guided_rewrite(prompt_question, subject, prompt.stage, image_related=has_image_turn),
                        )
                        delta = rewritten_text[len(emitted_text) :]
                        emitted_text = rewritten_text
                        pending_buffer = ""
                        if delta:
                            if not first_token_observed:
                                chat_first_token_seconds.observe(perf_counter() - started)
                                first_token_observed = True
                            yield _sse_event("chunk", {"content": delta})
                        stop_streaming = True
                        break

                    emitted_text = candidate_text
                    if not first_token_observed:
                        chat_first_token_seconds.observe(perf_counter() - started)
                        first_token_observed = True
                    yield _sse_event("chunk", {"content": segment})

                if stop_streaming:
                    break

            if mode_parser is not None and resolved_mode is None:
                pending_buffer += mode_parser.flush()
                resolved_mode = mode_parser.resolved_mode
            if quality_filter is not None and not disconnected:
                pending_buffer += quality_filter.flush()

            if not disconnected and pending_buffer:
                segments, pending_buffer = _split_stream_buffer(pending_buffer, force=True)
                for segment in segments:
                    if not emitted_visible:
                        segment = segment.lstrip()
                        if not segment:
                            continue
                        emitted_visible = True
                    candidate_text = f"{emitted_text}{segment}"
                    validation = filter_service.validate_answer(candidate_text, skip_direct_answer=practice_review_turn, subject=subject)
                    if not validation.allowed:
                        chat_stream_safety_rewrite_total.inc()
                        rewritten_text = _compose_safe_rewrite(
                            emitted_text,
                            socratic_service.safe_guided_rewrite(prompt_question, subject, prompt.stage, image_related=has_image_turn),
                        )
                        delta = rewritten_text[len(emitted_text) :]
                        emitted_text = rewritten_text
                        if delta:
                            if not first_token_observed:
                                chat_first_token_seconds.observe(perf_counter() - started)
                                first_token_observed = True
                            yield _sse_event("chunk", {"content": delta})
                        break
                    emitted_text = candidate_text
                    if not first_token_observed:
                        chat_first_token_seconds.observe(perf_counter() - started)
                        first_token_observed = True
                    yield _sse_event("chunk", {"content": segment})

            if should_send_done and not emitted_text.strip():
                emitted_text = fallback_text if has_image_turn else EMPTY_CHAT_RESPONSE_FALLBACK
            if should_send_done:
                emitted_text = emitted_text.strip()
                await release_ticket_once()
                yield _sse_event(
                    "done",
                    {
                        "content": emitted_text,
                        "assets": [],
                        "suggested_replies": [],
                    },
                )
                suggested_replies = await _generate_suggested_replies(
                    subject=subject,
                    guidance_stage=prompt.stage,
                    current_question=prompt_question,
                    assistant_response=emitted_text,
                    history_pairs=history_pairs,
                    model_key=selected_model_key,
                )
                conversation.subject = subject
                conversation.guidance_stage = prompt.stage
                practice_review_persisted = practice_review_turn and bool(emitted_text)
                if practice_review_persisted:
                    conversation.active_practice = None
                db.add(conversation)
                existing_assistant = _assistant_message_for_turn(db, conversation.id, user_turn_index)
                if existing_assistant:
                    emitted_text = existing_assistant.content
                    suggested_replies = list(existing_assistant.suggested_replies or [])
                    assistant_message_id = existing_assistant.id
                else:
                    assistant_message = Message(
                        conversation_id=conversation.id,
                        role=MessageRole.ASSISTANT,
                        content=emitted_text,
                        suggested_replies=suggested_replies,
                        turn_index=user_turn_index,
                        guidance_stage=prompt.stage,
                        llm_model_key=selected_model_key,
                        agent_role_revision_id=role_snapshot.revision_id if role_snapshot.applied else None,
                        agent_role_snapshot=role_snapshot.public_snapshot() if role_snapshot.applied else None,
                    )
                    db.add(assistant_message)
                    db.flush()
                    assistant_message_id = assistant_message.id
                    incentive_grant = _record_turn_incentives(
                        db,
                        student_id=current_user.id,
                        conversation_id=conversation.id,
                        turn_index=user_turn_index,
                        subject=subject,
                        followup_answered=followup_context is not None,
                        practice_review_turn=practice_review_turn,
                        response_text=emitted_text,
                        incentive_params=incentive_params,
                        llm_quality=quality_filter.signal if quality_filter is not None else None,
                    )
                    guidance_stage_total.labels(stage=prompt.stage.value).inc()
                db.commit()
                turn_persisted = True
                if suggested_replies:
                    yield _sse_event("suggested_replies", {"suggested_replies": suggested_replies})
                if incentive_grant and (
                    incentive_grant.get("points_awarded")
                    or incentive_grant.get("new_badges")
                    or incentive_grant.get("level_up")
                ):
                    yield _sse_event("incentive", incentive_grant)
        finally:
            if llm_stream is not None:
                with suppress(Exception):
                    await llm_stream.aclose()
            await release_ticket_once()

            try:
                if not turn_persisted:
                    conversation.subject = subject
                    conversation.guidance_stage = prompt.stage
                    practice_review_persisted = should_send_done and practice_review_turn and bool(emitted_text)
                    if practice_review_persisted:
                        conversation.active_practice = None
                    db.add(conversation)
                if emitted_text and not turn_persisted:
                    emitted_text = emitted_text.strip()
                    existing_assistant = _assistant_message_for_turn(db, conversation.id, user_turn_index)
                    if existing_assistant:
                        emitted_text = existing_assistant.content
                        suggested_replies = list(existing_assistant.suggested_replies or [])
                        assistant_message_id = existing_assistant.id
                    else:
                        assistant_message = Message(
                            conversation_id=conversation.id,
                            role=MessageRole.ASSISTANT,
                            content=emitted_text,
                            suggested_replies=suggested_replies,
                            turn_index=user_turn_index,
                            guidance_stage=prompt.stage,
                            llm_model_key=selected_model_key,
                            agent_role_revision_id=role_snapshot.revision_id if role_snapshot.applied else None,
                            agent_role_snapshot=role_snapshot.public_snapshot() if role_snapshot.applied else None,
                        )
                        db.add(assistant_message)
                        db.flush()
                        assistant_message_id = assistant_message.id
                        if should_send_done:
                            incentive_grant = _record_turn_incentives(
                                db,
                                student_id=current_user.id,
                                conversation_id=conversation.id,
                                turn_index=user_turn_index,
                                subject=subject,
                                followup_answered=followup_context is not None,
                                practice_review_turn=practice_review_turn,
                                response_text=emitted_text,
                                incentive_params=incentive_params,
                                llm_quality=quality_filter.signal if quality_filter is not None else None,
                            )
                        guidance_stage_total.labels(stage=prompt.stage.value).inc()
                if not turn_persisted:
                    db.commit()
                if practice_review_persisted:
                    chat_practice_review_total.labels(subject=subject).inc()
                if should_send_done and resolved_mode == "fact":
                    chat_fact_mode_total.labels(subject=subject).inc()
                    logger.info(
                        "chat_fact_mode",
                        extra={
                            "student_id": current_user.id,
                            "conversation_id": conversation.id,
                            "subject": subject,
                        },
                    )
                if quota_reservation is not None and provider_stream_started and not usage_recorded:
                    usage_recorded = True
                    if quota_usage is not None:
                        llm_quota_service.reconcile(
                            db=db,
                            reservation=quota_reservation,
                            prompt_tokens=quota_usage.prompt_tokens,
                            completion_tokens=quota_usage.completion_tokens,
                            total_tokens=quota_usage.total_tokens,
                            reasoning_tokens=quota_usage.reasoning_tokens,
                            prompt_cache_hit_tokens=quota_usage.prompt_cache_hit_tokens,
                            prompt_cache_miss_tokens=quota_usage.prompt_cache_miss_tokens,
                            source="provider_usage",
                            estimated=False,
                            user_id=current_user.id,
                            conversation_id=conversation.id,
                            message_id=assistant_message_id,
                            request_id=payload.request_id,
                        )
                    else:
                        llm_quota_service.reconcile(
                            db=db,
                            reservation=quota_reservation,
                            total_tokens=quota_reservation.reserved_amount
                            if quota_reservation.billing_mode == QuotaBillingMode.TOKEN_USAGE
                            else 0,
                            source="local_estimate"
                            if quota_reservation.billing_mode == QuotaBillingMode.TOKEN_USAGE
                            else "request_count",
                            estimated=quota_reservation.billing_mode == QuotaBillingMode.TOKEN_USAGE,
                            user_id=current_user.id,
                            conversation_id=conversation.id,
                            message_id=assistant_message_id,
                            request_id=payload.request_id,
                        )
                elif quota_reservation is not None and not provider_stream_started:
                    llm_quota_service.release(quota_reservation)
                if emitted_text:
                    if should_send_done and cache_lookup.cache_key and not has_image_turn:
                        question_cache_service.store_answer(cache_lookup.cache_key, emitted_text)
                    if payload.request_id:
                        request_replay_service.mark_completed(
                            user_id=current_user.id,
                            request_id=payload.request_id,
                            question_hash=request_fingerprint,
                            conversation_id=conversation.id,
                            turn_index=user_turn_index,
                            subject=subject,
                            guidance_stage=prompt.stage,
                            final_content=emitted_text,
                            suggested_replies=suggested_replies,
                            role_snapshot=role_snapshot.replay_snapshot(),
                        )
            except Exception:
                db.rollback()
                raise
            finally:
                if should_send_done:
                    chat_full_response_seconds.observe(perf_counter() - started)
                sse_active_connections.dec()

    return StreamingResponse(event_stream(), media_type="text/event-stream")
