from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json

from backend.config import get_settings
from backend.models.conversation import GuidanceStage
from backend.services.store_service import BaseStore, store


@dataclass
class RequestReplayState:
    request_id: str
    question_hash: str
    conversation_id: int
    turn_index: int
    status: str
    subject: str
    guidance_stage: str | None = None
    final_content: str | None = None


class RequestReplayService:
    def __init__(self, store_backend: BaseStore | None = None) -> None:
        self.settings = get_settings()
        self.store_backend = store_backend or store

    def fingerprint(
        self,
        *,
        subject: str,
        question: str,
        conversation_id: int | None,
        image_sha256: str | None = None,
        llm_model: str | None = None,
    ) -> str:
        payload = {
            "subject": subject.strip(),
            "question": question.strip(),
            "conversation_id": conversation_id,
            "image_sha256": image_sha256,
            "llm_model": (llm_model or "").strip(),
        }
        return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    def load(self, *, user_id: int, request_id: str | None) -> RequestReplayState | None:
        if not request_id:
            return None
        raw = self.store_backend.get(self._key(user_id, request_id))
        if not raw:
            return None
        return RequestReplayState(**json.loads(raw))

    def remember_request(
        self,
        *,
        user_id: int,
        request_id: str,
        question_hash: str,
        conversation_id: int,
        turn_index: int,
        subject: str,
    ) -> None:
        self._save(
            user_id,
            RequestReplayState(
                request_id=request_id,
                question_hash=question_hash,
                conversation_id=conversation_id,
                turn_index=turn_index,
                status="accepted",
                subject=subject,
            ),
        )

    def mark_completed(
        self,
        *,
        user_id: int,
        request_id: str,
        question_hash: str,
        conversation_id: int,
        turn_index: int,
        subject: str,
        guidance_stage: GuidanceStage,
        final_content: str,
    ) -> None:
        self._save(
            user_id,
            RequestReplayState(
                request_id=request_id,
                question_hash=question_hash,
                conversation_id=conversation_id,
                turn_index=turn_index,
                status="completed",
                subject=subject,
                guidance_stage=guidance_stage.value,
                final_content=final_content,
            ),
        )

    def _save(self, user_id: int, state: RequestReplayState) -> None:
        self.store_backend.set(
            self._key(user_id, state.request_id),
            json.dumps(asdict(state), ensure_ascii=False),
            ttl_seconds=self.settings.chat_request_replay_ttl_seconds,
        )

    @staticmethod
    def _key(user_id: int, request_id: str) -> str:
        return f"chat_request:{user_id}:{request_id}"


request_replay_service = RequestReplayService()
