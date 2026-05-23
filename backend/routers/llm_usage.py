from __future__ import annotations

from datetime import UTC, date, datetime, time

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from backend.dependencies import CurrentAdmin, DbSession
from backend.models.llm_usage import LLMUsageEvent
from backend.models.schemas import LLMUsageSummaryRead

router = APIRouter(prefix="/api/llm-usage", tags=["llm-usage"])


@router.get("/summary", response_model=list[LLMUsageSummaryRead])
def usage_summary(
    db: DbSession,
    current_user: CurrentAdmin,
    date_: date | None = Query(default=None, alias="date"),
) -> list[LLMUsageSummaryRead]:
    target_date = date_ or datetime.now(UTC).date()
    start = datetime.combine(target_date, time.min, tzinfo=UTC)
    end = datetime.combine(target_date, time.max, tzinfo=UTC)
    rows = db.execute(
        select(
            LLMUsageEvent.model_key,
            LLMUsageEvent.billing_mode,
            func.coalesce(func.sum(LLMUsageEvent.request_count), 0),
            func.coalesce(func.sum(LLMUsageEvent.total_tokens), 0),
        )
        .where(LLMUsageEvent.created_at >= start, LLMUsageEvent.created_at <= end)
        .group_by(LLMUsageEvent.model_key, LLMUsageEvent.billing_mode)
        .order_by(LLMUsageEvent.model_key.asc())
    ).all()
    return [
        LLMUsageSummaryRead(
            model_key=row[0],
            billing_mode=row[1],
            request_count=int(row[2] or 0),
            total_tokens=int(row[3] or 0),
        )
        for row in rows
    ]
