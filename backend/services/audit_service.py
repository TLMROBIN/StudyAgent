from __future__ import annotations

from sqlalchemy.orm import Session

from backend.models.audit_log import AuditLog
from backend.models.user import User


class AuditService:
    def log(
        self,
        db: Session,
        *,
        actor: User | None,
        action: str,
        target_type: str,
        target_id: str | None,
        result: str,
        ip_address: str | None,
        detail: dict | None = None,
    ) -> AuditLog:
        entry = AuditLog(
            actor_id=actor.id if actor else None,
            action=action,
            target_type=target_type,
            target_id=target_id,
            result=result,
            ip_address=ip_address,
            detail=detail or {},
        )
        db.add(entry)
        db.commit()
        db.refresh(entry)
        return entry


audit_service = AuditService()
