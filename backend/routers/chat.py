from __future__ import annotations

import asyncio
from contextlib import suppress
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.database import SessionLocal
from backend.dependencies import CurrentUser, DbSession
from backend.models.agent_config import AgentConfig
from backend.models.conversation import Conversation, Message, MessageRole
from backend.models.schemas import ChatRequest, ConversationRead, QuestionRecommendationRead, QuestionRecommendationRequest, ResolveConversationRequest
from backend.models.user import User, UserRole
from backend.services.filter_service import filter_service
from backend.services.llm_service import llm_service
from backend.services.metrics_service import (
    chat_first_token_seconds,
    chat_full_response_seconds,
    chat_request_total,
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
from backend.services.socratic_service import socratic_service
from time import perf_counter

router = APIRouter(prefix="/api/chat", tags=["chat"])
STREAM_HEARTBEAT_SECONDS = 15
STREAM_FORCE_FLUSH_CHARS = 96
STREAM_GUARD_TAIL_CHARS = 24
STREAM_BOUNDARY_CHARS = {"。", "！", "？", "!", "?", "；", ";", "\n"}


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


def _retrieve_context_for_chat(subject: str, question: str, *, student_grade: int | None = None) -> RetrievalResult:
    session = SessionLocal()
    try:
        return rag_service.retrieve(session, subject, question, student_grade=student_grade)
    finally:
        session.close()


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


def _instant_stream(
    *,
    conversation_id: int,
    guidance_stage: str,
    content: str,
    request: Request,
    context_chunks: int = 0,
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
                },
            )
            yield _sse_event("done", {"content": content})
        finally:
            sse_active_connections.dec()

    return stream()


def _ensure_conversation(db: DbSession, student_id: int, payload: ChatRequest) -> Conversation:
    if payload.conversation_id:
        conversation = db.scalar(
            select(Conversation).where(Conversation.id == payload.conversation_id, Conversation.student_id == student_id)
        )
        if not conversation:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
        return conversation

    conversation = Conversation(student_id=student_id, subject=payload.subject)
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


@router.get("/history", response_model=list[ConversationRead])
def list_conversations(db: DbSession, current_user: CurrentUser) -> list[ConversationRead]:
    conversations = db.scalars(
        select(Conversation)
        .options(selectinload(Conversation.messages))
        .where(Conversation.student_id == current_user.id)
        .order_by(Conversation.updated_at.desc())
    ).all()
    return [ConversationRead.model_validate(item) for item in conversations]


@router.get("/history/{conversation_id}", response_model=ConversationRead)
def get_conversation(conversation_id: int, db: DbSession, current_user: CurrentUser) -> ConversationRead:
    conversation = db.scalar(
        select(Conversation)
        .options(selectinload(Conversation.messages))
        .where(Conversation.id == conversation_id, Conversation.student_id == current_user.id)
    )
    if not conversation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    return ConversationRead.model_validate(conversation)


@router.post("/{conversation_id}/resolve", response_model=ConversationRead)
def resolve_conversation(
    conversation_id: int,
    payload: ResolveConversationRequest,
    db: DbSession,
    current_user: CurrentUser,
) -> ConversationRead:
    conversation = db.scalar(
        select(Conversation).where(Conversation.id == conversation_id, Conversation.student_id == current_user.id)
    )
    if not conversation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    conversation.resolved = payload.resolved
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return ConversationRead.model_validate(conversation)


@router.post("/recommendations", response_model=list[QuestionRecommendationRead])
def recommend_questions(
    payload: QuestionRecommendationRequest,
    db: DbSession,
    current_user: CurrentUser,
) -> list[QuestionRecommendationRead]:
    decision = filter_service.check_question(payload.question, payload.subject)
    if not decision.allowed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Question is not a supported academic prompt")

    subject = decision.subject or payload.subject
    include_solutions = bool(payload.include_solutions and current_user.role in {UserRole.TEACHER, UserRole.ADMIN})
    rows = rag_service.recommend_questions(
        db,
        subject,
        payload.question,
        student_grade=_effective_recommendation_grade(current_user, payload),
        limit=payload.limit,
        difficulty_preference=payload.difficulty_preference,
    )
    return [_recommendation_read(row, include_solutions=include_solutions) for row in rows]


@router.post("/stream")
async def stream_chat(payload: ChatRequest, db: DbSession, current_user: CurrentUser, request: Request):
    started = perf_counter()
    chat_request_total.inc()
    decision = filter_service.check_question(payload.message, payload.subject)
    request_fingerprint = request_replay_service.fingerprint(
        subject=payload.subject,
        question=payload.message,
        conversation_id=payload.conversation_id,
    )
    replay_state = request_replay_service.load(user_id=current_user.id, request_id=payload.request_id)
    if replay_state and replay_state.question_hash != request_fingerprint:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Request id already used with different payload")

    if replay_state:
        conversation = db.scalar(
            select(Conversation).where(
                Conversation.id == replay_state.conversation_id,
                Conversation.student_id == current_user.id,
            )
        )
        if not conversation:
            replay_state = None
            conversation = _ensure_conversation(db, current_user.id, payload)
        elif replay_state.status == "completed" and replay_state.final_content:
            return StreamingResponse(
                _instant_stream(
                    conversation_id=conversation.id,
                    guidance_stage=replay_state.guidance_stage or conversation.guidance_stage.value,
                    content=replay_state.final_content,
                    request=request,
                ),
                media_type="text/event-stream",
            )
    else:
        conversation = _ensure_conversation(db, current_user.id, payload)

    if replay_state:
        user_turn_index = replay_state.turn_index
        history_pairs = _history_pairs_before_turn(conversation, user_turn_index)
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
                )
            return StreamingResponse(
                _instant_stream(
                    conversation_id=conversation.id,
                    guidance_stage=existing_assistant.guidance_stage.value,
                    content=existing_assistant.content,
                    request=request,
                ),
                media_type="text/event-stream",
            )
    else:
        history_pairs = [(message.role.value, message.content) for message in conversation.messages]
        user_turn_index = len([message for message in conversation.messages if message.role == MessageRole.USER]) + 1
        user_message = Message(
            conversation_id=conversation.id,
            role=MessageRole.USER,
            content=payload.message,
            turn_index=user_turn_index,
            guidance_stage=conversation.guidance_stage,
        )
        db.add(user_message)
        db.commit()
        if payload.request_id:
            request_replay_service.remember_request(
                user_id=current_user.id,
                request_id=payload.request_id,
                question_hash=request_fingerprint,
                conversation_id=conversation.id,
                turn_index=user_turn_index,
                subject=payload.subject,
            )

    if not decision.allowed:
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
            )
        return StreamingResponse(
            _instant_stream(
                conversation_id=conversation.id,
                guidance_stage=conversation.guidance_stage.value,
                content=refusal,
                request=request,
            ),
            media_type="text/event-stream",
        )

    subject = decision.subject or payload.subject
    retrieval = await asyncio.to_thread(
        _retrieve_context_for_chat,
        subject,
        payload.message,
        student_grade=current_user.grade,
    )
    active_config = db.scalar(select(AgentConfig).where(AgentConfig.is_active.is_(True)).order_by(AgentConfig.version.desc()))
    prompt = socratic_service.build_prompt(
        question=payload.message,
        subject=subject,
        history=history_pairs,
        retrieved_context=retrieval.context,
        system_prompt=active_config.system_prompt if active_config else socratic_service.base_prompt,
        student_grade=current_user.grade,
    )
    cache_lookup = QuestionCacheLookup(cache_key=None, answer=None)
    if question_cache_service.is_cacheable(history_pairs=history_pairs, question=payload.message):
        cache_lookup = question_cache_service.lookup(
            subject=subject,
            question=payload.message,
            guidance_stage=prompt.stage,
            agent_version=active_config.version if active_config else 0,
            chunks=retrieval.chunks,
        )
        if cache_lookup.answer:
            conversation.subject = subject
            conversation.guidance_stage = prompt.stage
            db.add(conversation)
            existing_assistant = _assistant_message_for_turn(db, conversation.id, user_turn_index)
            if not existing_assistant:
                assistant_message = Message(
                    conversation_id=conversation.id,
                    role=MessageRole.ASSISTANT,
                    content=cache_lookup.answer,
                    turn_index=user_turn_index,
                    guidance_stage=prompt.stage,
                )
                db.add(assistant_message)
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
                    final_content=cache_lookup.answer,
                )
            return StreamingResponse(
                _instant_stream(
                    conversation_id=conversation.id,
                    guidance_stage=prompt.stage.value,
                    content=cache_lookup.answer,
                    request=request,
                    context_chunks=len(retrieval.chunks),
                ),
                media_type="text/event-stream",
            )

    try:
        llm_queue_depth.set(queue_service.waiting)
        ticket_context = queue_service.reserve()
        ticket = await ticket_context.__aenter__()
    except QueueFullError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="当前排队人数较多，请稍后重试") from exc
    finally:
        llm_queue_depth.set(queue_service.waiting)

    async def event_stream():
        emitted_text = ""
        pending_buffer = ""
        llm_stream = None
        disconnected = False
        first_token_observed = False
        should_send_done = True
        stop_streaming = False
        sse_active_connections.inc()
        try:
            yield _sse_event(
                "meta",
                {
                    "conversation_id": conversation.id,
                    "guidance_stage": prompt.stage.value,
                    "queue_waiting_before": ticket.waiting_before,
                    "context_chunks": len(retrieval.chunks),
                    "request_id": getattr(request.state, "request_id", None),
                },
            )
            llm_stream = llm_service.stream_response(prompt.messages, prompt.fallback_text)

            while True:
                if await request.is_disconnected():
                    disconnected = True
                    should_send_done = False
                    chat_stream_disconnect_total.inc()
                    break

                try:
                    provider_chunk = await asyncio.wait_for(anext(llm_stream), timeout=STREAM_HEARTBEAT_SECONDS)
                except TimeoutError:
                    yield _sse_event("heartbeat", {"conversation_id": conversation.id})
                    continue
                except StopAsyncIteration:
                    break

                pending_buffer += provider_chunk
                segments, pending_buffer = _split_stream_buffer(pending_buffer)
                for segment in segments:
                    candidate_text = f"{emitted_text}{segment}"
                    validation = filter_service.validate_answer(candidate_text)
                    if not validation.allowed:
                        chat_stream_safety_rewrite_total.inc()
                        rewritten_text = _compose_safe_rewrite(
                            emitted_text,
                            socratic_service.safe_guided_rewrite(payload.message, subject, prompt.stage),
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

            if not disconnected and pending_buffer:
                segments, pending_buffer = _split_stream_buffer(pending_buffer, force=True)
                for segment in segments:
                    candidate_text = f"{emitted_text}{segment}"
                    validation = filter_service.validate_answer(candidate_text)
                    if not validation.allowed:
                        chat_stream_safety_rewrite_total.inc()
                        rewritten_text = _compose_safe_rewrite(
                            emitted_text,
                            socratic_service.safe_guided_rewrite(payload.message, subject, prompt.stage),
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

            if should_send_done:
                yield _sse_event("done", {"content": emitted_text})
        finally:
            if llm_stream is not None:
                with suppress(Exception):
                    await llm_stream.aclose()
            with suppress(Exception):
                await ticket_context.__aexit__(None, None, None)
            llm_queue_depth.set(queue_service.waiting)

            try:
                conversation.subject = subject
                conversation.guidance_stage = prompt.stage
                db.add(conversation)
                if should_send_done and emitted_text:
                    existing_assistant = _assistant_message_for_turn(db, conversation.id, user_turn_index)
                    if existing_assistant:
                        emitted_text = existing_assistant.content
                    else:
                        assistant_message = Message(
                            conversation_id=conversation.id,
                            role=MessageRole.ASSISTANT,
                            content=emitted_text,
                            turn_index=user_turn_index,
                            guidance_stage=prompt.stage,
                        )
                        db.add(assistant_message)
                        guidance_stage_total.labels(stage=prompt.stage.value).inc()
                db.commit()
                if should_send_done and emitted_text:
                    if cache_lookup.cache_key:
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
                        )
            except Exception:
                db.rollback()
                raise
            finally:
                if should_send_done:
                    chat_full_response_seconds.observe(perf_counter() - started)
                sse_active_connections.dec()

    return StreamingResponse(event_stream(), media_type="text/event-stream")
