from __future__ import annotations

from pathlib import Path
import shutil

from backend.config import Settings, get_settings
from backend.models.knowledge import KnowledgeDocument


def _slug(value: str | None, *, fallback: str) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return fallback
    chars: list[str] = []
    for char in raw:
        if char.isalnum() or "\u4e00" <= char <= "\u9fff":
            chars.append(char)
        else:
            chars.append("-")
    normalized = "".join(chars).strip("-")
    while "--" in normalized:
        normalized = normalized.replace("--", "-")
    return normalized or fallback


class DocumentBackupService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def backup_root(self) -> Path:
        return Path(self.settings.document_backup_path)

    def backup_path_for(self, document: KnowledgeDocument) -> Path:
        extension = Path(document.file_path or document.filename or "").suffix.lower()
        if not extension:
            extension = Path(document.filename or "").suffix.lower()
        subject_dir = _slug(document.subject, fallback="unknown-subject")
        resource_dir = _slug(document.resource_type, fallback="unknown-resource")
        safe_name = _slug(Path(document.filename or "").stem, fallback=f"document-{document.id}")
        filename = f"{int(document.id):04d}__{safe_name}{extension}"
        return self.backup_root() / resource_dir / subject_dir / filename

    def persist_uploaded_file(self, source_path: str | Path, document: KnowledgeDocument) -> Path:
        source = Path(source_path)
        target = self.backup_path_for(document)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() == target.resolve():
            return target
        shutil.move(str(source), str(target))
        return target


document_backup_service = DocumentBackupService()
