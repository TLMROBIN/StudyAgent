from __future__ import annotations

import asyncio
from contextlib import suppress
from hashlib import sha256
import json
import mimetypes
from time import perf_counter
from typing import Any

from fastapi import APIRouter, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, StreamingResponse
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
from backend.models.schemas import (
    ChatModelOptionRead,
    ChatModelStatusRead,
    ChatRequest,
    ConversationRead,
    QuestionRecommendationRead,
    QuestionRecommendationRequest,
    ResolveConversationRequest,
)
from backend.models.user import User, UserRole
from backend.services.chat_attachment_service import StoredChatAttachment, chat_attachment_service
from backend.services.chat_image_understanding_service import ImageUnderstandingResult, chat_image_understanding_service
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

router = APIRouter(prefix="/api/chat", tags=["chat"])
STREAM_HEARTBEAT_SECONDS = 15
STREAM_FORCE_FLUSH_CHARS = 96
STREAM_GUARD_TAIL_CHARS = 24
STREAM_BOUNDARY_CHARS = {"。", "！", "？", "!", "?", "；", ";", "\n"}
EMPTY_CHAT_RESPONSE_FALLBACK = (
    "我刚刚没有生成出有效内容。我们换一种方式继续："
    "请你把题目条件或卡住的一步再发我一次，我会先帮你整理已知条件。"
)


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
            select(Conversation)
            .options(_message_load_option())
            .where(Conversation.id == payload.conversation_id, Conversation.student_id == student_id)
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
    if "multipart/form-data" in content_type:
        form = await request.form()
        image_items = [item for item in form.getlist("image") if item]
        if len(image_items) > 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only one chat image is allowed")
        image = image_items[0] if image_items else None
        if image is not None and not all(hasattr(image, attr) for attr in ("filename", "content_type", "read")):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid chat image upload")
        conversation_id_raw = str(form.get("conversation_id") or "").strip()
        request_id_raw = str(form.get("request_id") or "").strip()
        payload = ChatRequest(
            subject=str(form.get("subject") or "").strip(),
            message=str(form.get("message") or ""),
            conversation_id=int(conversation_id_raw) if conversation_id_raw else None,
            request_id=request_id_raw or None,
            llm_model=str(form.get("llm_model") or "").strip() or None,
        )
        return payload, image

    body = await request.json()
    return ChatRequest.model_validate(body), None


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


def _build_short_circuit_reply(subject: str) -> str:
    return socratic_service.image_low_confidence_text(subject)


@router.get("/models", response_model=list[ChatModelOptionRead])
def list_chat_models(current_user: CurrentUser) -> list[ChatModelOptionRead]:
    return [ChatModelOptionRead(**item) for item in llm_service.chat_model_options()]


@router.get("/models/status", response_model=list[ChatModelStatusRead])
async def list_chat_model_statuses(current_user: CurrentUser) -> list[ChatModelStatusRead]:
    return [ChatModelStatusRead(**item) for item in await llm_service.chat_model_statuses()]


@router.get("/history", response_model=list[ConversationRead])
def list_conversations(db: DbSession, current_user: CurrentUser) -> list[ConversationRead]:
    conversations = db.scalars(
        select(Conversation)
        .options(_message_load_option())
        .where(Conversation.student_id == current_user.id)
        .order_by(Conversation.updated_at.desc())
    ).all()
    return [ConversationRead.model_validate(item) for item in conversations]


@router.get("/history/{conversation_id}", response_model=ConversationRead)
def get_conversation(conversation_id: int, db: DbSession, current_user: CurrentUser) -> ConversationRead:
    conversation = db.scalar(
        select(Conversation)
        .options(_message_load_option())
        .where(Conversation.id == conversation_id, Conversation.student_id == current_user.id)
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
        select(Conversation).where(Conversation.id == conversation_id, Conversation.student_id == current_user.id)
    )
    if not conversation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    conversation.resolved = payload.resolved
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return ConversationRead.model_validate(conversation)


@router.delete("/{conversation_id}")
def delete_conversation(conversation_id: int, db: DbSession, current_user: CurrentUser) -> dict[str, str]:
    conversation = db.scalar(
        select(Conversation).where(Conversation.id == conversation_id, Conversation.student_id == current_user.id)
    )
    if not conversation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    db.delete(conversation)
    db.commit()
    return {"status": "deleted"}


@router.post("/recommendations", response_model=list[QuestionRecommendationRead])
def recommend_questions(
    payload: QuestionRecommendationRequest,
    db: DbSession,
    current_user: CurrentUser,
) -> list[QuestionRecommendationRead]:
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
            )
        )
        if not conversation:
            replay_state = None
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
            )

    if has_image_turn and image_content and attachment_record:
        image_understanding = await chat_image_understanding_service.understand(
            image_bytes=image_content,
            mime_type=attachment_record.mime_type,
            subject=payload.subject,
            user_text=payload.message,
            model_key=selected_model_key,
            image_path=str(chat_attachment_service.resolve_path(attachment_record.storage_key)),
            attachment_id=attachment_record.id,
        )
        attachment_record.ocr_status = {
            "mineru_ocr": "mineru_ocr",
            "ocr": "llm_ocr",
            "multimodal": "multimodal_fallback",
            "failed": "failed",
        }.get(image_understanding.source, "pending")
        attachment_record.ocr_confidence = image_understanding.ocr_confidence_value
        db.add(attachment_record)
        db.commit()

    filter_question = _build_filter_question(payload_message=payload.message, understanding=image_understanding)
    if has_image_turn and image_understanding and image_understanding.must_short_circuit:
        short_circuit_text = _build_short_circuit_reply(payload.subject)
        existing_assistant = _assistant_message_for_turn(db, conversation.id, user_turn_index)
        if not existing_assistant:
            assistant_message = Message(
                conversation_id=conversation.id,
                role=MessageRole.ASSISTANT,
                content=short_circuit_text,
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
                final_content=short_circuit_text,
            )
        return StreamingResponse(
            _instant_stream(
                conversation_id=conversation.id,
                guidance_stage=conversation.guidance_stage.value,
                content=short_circuit_text,
                request=request,
            ),
            media_type="text/event-stream",
        )

    decision = filter_service.check_question(filter_question, payload.subject)
    if not decision.allowed:
        filter_blocked_total.inc()
        refusal = filter_service.refusal_text
        if has_image_turn:
            refusal = filter_service.ensure_image_disclaimer(refusal)
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
    prompt_question = _build_prompt_question(payload_message=payload.message, subject=subject, understanding=image_understanding)
    retrieval_query = filter_question or prompt_question
    retrieval = await asyncio.to_thread(
        _retrieve_context_for_chat,
        subject,
        retrieval_query,
        student_grade=current_user.grade,
    )
    active_config = db.scalar(select(AgentConfig).where(AgentConfig.is_active.is_(True)).order_by(AgentConfig.version.desc()))
    prompt = socratic_service.build_prompt(
        question=prompt_question,
        subject=subject,
        history=history_pairs,
        retrieved_context=retrieval.context,
        system_prompt=active_config.system_prompt if active_config else socratic_service.base_prompt,
        student_grade=current_user.grade,
        image_summary=image_understanding.prompt_summary if image_understanding else None,
        image_confidence=image_understanding.confidence_level if image_understanding else None,
        image_related=has_image_turn,
    )
    cache_lookup = QuestionCacheLookup(cache_key=None, answer=None)
    if question_cache_service.is_cacheable(
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
        )
        if cache_lookup.answer:
            response_text = filter_service.ensure_image_disclaimer(cache_lookup.answer) if has_image_turn else cache_lookup.answer
            conversation.subject = subject
            conversation.guidance_stage = prompt.stage
            db.add(conversation)
            existing_assistant = _assistant_message_for_turn(db, conversation.id, user_turn_index)
            if not existing_assistant:
                assistant_message = Message(
                    conversation_id=conversation.id,
                    role=MessageRole.ASSISTANT,
                    content=response_text,
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
                    final_content=response_text,
                )
            return StreamingResponse(
                _instant_stream(
                    conversation_id=conversation.id,
                    guidance_stage=prompt.stage.value,
                    content=response_text,
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
            llm_stream = _stream_llm_response(prompt.messages, prompt.fallback_text, model_key=selected_model_key)

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

            if not disconnected and pending_buffer:
                segments, pending_buffer = _split_stream_buffer(pending_buffer, force=True)
                for segment in segments:
                    candidate_text = f"{emitted_text}{segment}"
                    validation = filter_service.validate_answer(candidate_text)
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

            if has_image_turn:
                emitted_text = filter_service.ensure_image_disclaimer(emitted_text)
            if should_send_done and not emitted_text.strip():
                emitted_text = EMPTY_CHAT_RESPONSE_FALLBACK
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
                    if cache_lookup.cache_key and not has_image_turn:
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
