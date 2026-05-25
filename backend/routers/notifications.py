from fastapi import APIRouter
from sqlalchemy import select

from backend.dependencies import CurrentUser, DbSession
from backend.models.notification import Notification
from backend.models.schemas import NotificationRead

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.get("/active", response_model=list[NotificationRead])
def list_active_notifications(db: DbSession, current_user: CurrentUser) -> list[NotificationRead]:
    items = db.scalars(
        select(Notification)
        .where(Notification.archived_at.is_(None))
        .order_by(Notification.created_at.desc(), Notification.id.desc())
    ).all()
    return [NotificationRead.model_validate(item) for item in items]
