from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import mimetypes
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, status
from PIL import Image, UnidentifiedImageError

from backend.config import get_settings


@dataclass
class StoredChatAttachment:
    storage_key: str
    original_filename: str
    mime_type: str
    file_size: int
    sha256: str


class ChatAttachmentService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.base_dir = Path(self.settings.chat_attachment_path)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def save_bytes(
        self,
        *,
        content: bytes,
        filename: str,
        content_type: str | None,
        student_id: int,
        conversation_id: int,
    ) -> StoredChatAttachment:
        normalized_content_type = self._resolve_content_type(filename=filename, content_type=content_type)
        self._validate_image(content=content, content_type=normalized_content_type)

        suffix = (Path(filename).suffix.lower() or mimetypes.guess_extension(normalized_content_type) or ".png")
        storage_key = str(Path(str(student_id)) / str(conversation_id) / f"{uuid4().hex}{suffix}")
        target = self.resolve_path(storage_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return StoredChatAttachment(
            storage_key=storage_key,
            original_filename=filename or target.name,
            mime_type=normalized_content_type,
            file_size=len(content),
            sha256=sha256(content).hexdigest(),
        )

    def delete(self, storage_key: str | None) -> None:
        if not storage_key:
            return
        path = self.resolve_path(storage_key)
        path.unlink(missing_ok=True)

    def resolve_path(self, storage_key: str) -> Path:
        target = (self.base_dir / storage_key).resolve()
        base = self.base_dir.resolve()
        if target != base and base not in target.parents:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found")
        return target

    def _resolve_content_type(self, *, filename: str, content_type: str | None) -> str:
        normalized = (content_type or "").strip().lower()
        if normalized:
            return normalized
        guessed = mimetypes.guess_type(filename or "")[0]
        return (guessed or "application/octet-stream").lower()

    def _validate_image(self, *, content: bytes, content_type: str) -> None:
        if not content:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded image is empty")
        if len(content) > self.settings.chat_upload_max_bytes:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded image exceeds size limit")
        if content_type not in self.settings.chat_image_mime_type_list:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported chat image type")
        try:
            with Image.open(BytesIO(content)) as image:
                image.verify()
        except (UnidentifiedImageError, OSError) as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is not a valid image") from exc


chat_attachment_service = ChatAttachmentService()
