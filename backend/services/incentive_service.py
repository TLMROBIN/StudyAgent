from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from difflib import SequenceMatcher
import logging
from typing import Any, Iterable

from sqlalchemy import distinct, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.models.conversation import GuidanceStage, Message, MessageRole
from backend.models.incentive import StudentIncentiveEvent, StudentIncentiveProfile
from backend.services.metrics_service import incentive_events_total
from backend.time_utils import BEIJING_TZ, now_beijing, now_utc

logger = logging.getLogger(__name__)

DEFAULT_INCENTIVE_PARAMS: dict[str, Any] = {
    "enabled": False,
    "points": {
        "followup_answered": 2,
        "early_resolved": 15,
        "resolved_after_fallback": 5,
        "practice_passed": 8,
        "practice_partial": 3,
        "reflection_submitted": 5,
        "daily_first_conversation": 2,
        "conversation_completed": 2,
        "teacher_praise": 10,
    },
    "daily_total_cap": 60,
    "daily_event_caps": {
        "followup_answered": 5,
        "conversation_completed": 3,
        "practice_per_conversation": 2,
    },
    "resolve_min_user_turns": 2,
    "reflection_min_chars": 20,
    "reflection_max_chars": 500,
    "level_thresholds": [0, 50, 120, 250, 450, 700, 1000, 1400, 1900, 2500],
    "streak_weekend_grace": False,
    "teacher_praise_daily_cap": 20,
    "followup_min_interval_seconds": 10,
    "llm_signal_enabled": False,
}

VALID_LEARNING_EVENTS = {
    "followup_answered",
    "practice_passed",
    "early_resolved",
    "resolved_after_fallback",
}
QUALITY_RESOLVE_EVENTS = {"early_resolved", "resolved_after_fallback"}

BADGE_RULES: tuple[tuple[str, str, int], ...] = (
    ("追问者", "followup_answered", 50),
    ("推导达人", "early_resolved", 10),
    ("独立解题", "practice_passed", 20),
    ("反思者", "reflection_submitted", 15),
    ("教师之选", "teacher_praise", 1),
)


@dataclass(slots=True)
class IncentiveEventDraft:
    event_type: str
    points: int
    dedup_key: str
    conversation_id: int | None = None
    subject: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    created_by: int | None = None


@dataclass(slots=True)
class IncentiveGrant:
    points_awarded: int = 0
    awarded_events: list[str] = field(default_factory=list)
    new_badges: list[str] = field(default_factory=list)
    level_up: int | None = None
    level: int = 1
    total_points: int = 0
    streak: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "points_awarded": self.points_awarded,
            "awarded_events": list(self.awarded_events),
            "new_badges": list(self.new_badges),
            "level_up": self.level_up,
            "level": self.level,
            "total_points": self.total_points,
            "streak": self.streak,
        }


class QualitySignalFilter:
    """Strip an optional quality marker from a chunked model stream."""

    OPEN = "[[sig:answer_quality="
    CLOSE = "]]"
    VALID_SIGNALS = {"high", "medium", "low", "unknown"}

    def __init__(self) -> None:
        self.buffer = ""
        self.inside_signal = False
        self.signal: str | None = None

    @staticmethod
    def _partial_suffix(text: str, token: str) -> int:
        maximum = min(len(text), len(token) - 1)
        for size in range(maximum, 0, -1):
            if text.endswith(token[:size]):
                return size
        return 0

    def feed(self, text: str) -> str:
        self.buffer += text or ""
        visible: list[str] = []
        while self.buffer:
            if self.inside_signal:
                close_index = self.buffer.find(self.CLOSE)
                if close_index < 0:
                    return "".join(visible)
                value = self.buffer[:close_index].strip().lower()
                self.signal = value if value in self.VALID_SIGNALS else "unknown"
                self.buffer = self.buffer[close_index + len(self.CLOSE) :]
                self.inside_signal = False
                continue
            open_index = self.buffer.find(self.OPEN)
            if open_index >= 0:
                visible.append(self.buffer[:open_index])
                self.buffer = self.buffer[open_index + len(self.OPEN) :]
                self.inside_signal = True
                continue
            keep = self._partial_suffix(self.buffer, self.OPEN)
            emit_upto = len(self.buffer) - keep
            if emit_upto:
                visible.append(self.buffer[:emit_upto])
                self.buffer = self.buffer[emit_upto:]
            break
        return "".join(visible)

    def flush(self) -> str:
        if self.inside_signal:
            self.buffer = ""
            return ""
        output = self.buffer
        self.buffer = ""
        return output


def _validated_nonnegative_int(value: Any, default: int, *, maximum: int = 100000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > maximum:
        return default
    return value


def resolve_config(guidance_params: dict[str, Any] | None) -> dict[str, Any]:
    raw = (guidance_params or {}).get("incentive")
    override = raw if isinstance(raw, dict) else {}
    resolved = {**DEFAULT_INCENTIVE_PARAMS, **{k: v for k, v in override.items() if k not in {"points", "daily_event_caps"}}}

    raw_points = override.get("points") if isinstance(override.get("points"), dict) else {}
    resolved_points = dict(DEFAULT_INCENTIVE_PARAMS["points"])
    for key, default in resolved_points.items():
        resolved_points[key] = _validated_nonnegative_int(raw_points.get(key), default, maximum=1000)
    resolved["points"] = resolved_points

    raw_caps = override.get("daily_event_caps") if isinstance(override.get("daily_event_caps"), dict) else {}
    resolved_caps = dict(DEFAULT_INCENTIVE_PARAMS["daily_event_caps"])
    for key, default in resolved_caps.items():
        resolved_caps[key] = _validated_nonnegative_int(raw_caps.get(key), default, maximum=10000)
    resolved["daily_event_caps"] = resolved_caps

    resolved["enabled"] = bool(resolved.get("enabled", False))
    for key in (
        "daily_total_cap",
        "resolve_min_user_turns",
        "reflection_min_chars",
        "reflection_max_chars",
        "teacher_praise_daily_cap",
        "followup_min_interval_seconds",
    ):
        resolved[key] = _validated_nonnegative_int(
            resolved.get(key), int(DEFAULT_INCENTIVE_PARAMS[key]), maximum=100000
        )
    if resolved["reflection_max_chars"] < resolved["reflection_min_chars"]:
        resolved["reflection_max_chars"] = int(DEFAULT_INCENTIVE_PARAMS["reflection_max_chars"])

    thresholds = resolved.get("level_thresholds")
    if not (
        isinstance(thresholds, list)
        and thresholds
        and all(isinstance(item, int) and item >= 0 for item in thresholds)
        and thresholds == sorted(set(thresholds))
    ):
        thresholds = list(DEFAULT_INCENTIVE_PARAMS["level_thresholds"])
    resolved["level_thresholds"] = thresholds
    resolved["streak_weekend_grace"] = bool(resolved.get("streak_weekend_grace", False))
    resolved["llm_signal_enabled"] = bool(resolved.get("llm_signal_enabled", False))
    return resolved


def extract_practice_verdict(text: str) -> str | None:
    normalized = "".join((text or "").split())
    for marker, verdict in (
        ("【判定】部分正确", "partial"),
        ("【判定】不正确", "incorrect"),
        ("【判定】正确", "correct"),
    ):
        if marker in normalized:
            return verdict
    conservative_correct = ("回答正确", "完全正确", "判断正确", "做对了")
    conservative_partial = ("部分正确", "思路正确但", "方向正确但")
    if any(marker in normalized for marker in conservative_partial):
        return "partial"
    if any(marker in normalized for marker in conservative_correct) and not any(
        marker in normalized for marker in ("不完全正确", "并不正确", "不是正确")
    ):
        return "correct"
    return None


def evaluate_turn(
    *,
    student_id: int,
    conversation_id: int,
    turn_index: int,
    subject: str,
    followup_answered: bool,
    practice_verdict: str | None,
    first_learning_turn_today: bool,
    params: dict[str, Any],
) -> list[IncentiveEventDraft]:
    points = params["points"]
    drafts: list[IncentiveEventDraft] = []
    if followup_answered:
        drafts.append(
            IncentiveEventDraft(
                event_type="followup_answered",
                points=points["followup_answered"],
                dedup_key=f"conv:{conversation_id}:followup:{turn_index}",
                conversation_id=conversation_id,
                subject=subject,
                payload={"turn_index": turn_index, "valid_learning": turn_index >= 2},
            )
        )
    if practice_verdict in {"correct", "partial"}:
        event_type = "practice_passed" if practice_verdict == "correct" else "practice_partial"
        drafts.append(
            IncentiveEventDraft(
                event_type=event_type,
                points=points[event_type],
                dedup_key=f"conv:{conversation_id}:practice:{turn_index}:{practice_verdict}",
                conversation_id=conversation_id,
                subject=subject,
                payload={"turn_index": turn_index, "verdict": practice_verdict},
            )
        )
    if first_learning_turn_today:
        drafts.append(
            IncentiveEventDraft(
                event_type="daily_first_conversation",
                points=points["daily_first_conversation"],
                dedup_key=f"student:{student_id}:day:first:{now_beijing().date().isoformat()}",
                conversation_id=conversation_id,
                subject=subject,
                payload={"turn_index": turn_index},
            )
        )
    return drafts


def evaluate_resolve(
    *,
    conversation_id: int,
    subject: str,
    user_turn_count: int,
    had_followup: bool,
    had_fallback: bool,
    reflection: str | None,
    params: dict[str, Any],
) -> list[IncentiveEventDraft]:
    if user_turn_count < params["resolve_min_user_turns"]:
        return []
    points = params["points"]
    drafts: list[IncentiveEventDraft] = []
    if had_fallback:
        drafts.append(
            IncentiveEventDraft(
                event_type="resolved_after_fallback",
                points=points["resolved_after_fallback"],
                dedup_key=f"conv:{conversation_id}:resolved_after_fallback",
                conversation_id=conversation_id,
                subject=subject,
                payload={"user_turn_count": user_turn_count},
            )
        )
    elif had_followup:
        drafts.append(
            IncentiveEventDraft(
                event_type="early_resolved",
                points=points["early_resolved"],
                dedup_key=f"conv:{conversation_id}:early_resolved",
                conversation_id=conversation_id,
                subject=subject,
                payload={"user_turn_count": user_turn_count},
            )
        )
    drafts.append(
        IncentiveEventDraft(
            event_type="conversation_completed",
            points=points["conversation_completed"],
            dedup_key=f"conv:{conversation_id}:completed",
            conversation_id=conversation_id,
            subject=subject,
            payload={"user_turn_count": user_turn_count},
        )
    )
    reflection_text = (reflection or "").strip()
    if params["reflection_min_chars"] <= len(reflection_text) <= params["reflection_max_chars"]:
        drafts.append(
            IncentiveEventDraft(
                event_type="reflection_submitted",
                points=points["reflection_submitted"],
                dedup_key=f"conv:{conversation_id}:reflection",
                conversation_id=conversation_id,
                subject=subject,
                payload={"reflection": reflection_text},
            )
        )
    return drafts


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=BEIJING_TZ).astimezone(UTC)
    return start, start + timedelta(days=1)


def _level_for_points(total_points: int, thresholds: list[int]) -> int:
    level = 1
    for index, threshold in enumerate(thresholds, start=1):
        if total_points >= threshold:
            level = index
    return level


def _streak_continues(previous: date, current: date, weekend_grace: bool) -> bool:
    if current <= previous:
        return current == previous
    if not weekend_grace:
        return current == previous + timedelta(days=1)
    cursor = previous + timedelta(days=1)
    while cursor < current:
        if cursor.weekday() < 5:
            return False
        cursor += timedelta(days=1)
    return True


def _profile_or_create(db: Session, student_id: int) -> StudentIncentiveProfile:
    profile = db.scalar(select(StudentIncentiveProfile).where(StudentIncentiveProfile.student_id == student_id))
    if profile is not None:
        return profile
    profile = StudentIncentiveProfile(student_id=student_id, badges=[], counters={})
    try:
        with db.begin_nested():
            db.add(profile)
            db.flush()
        return profile
    except IntegrityError:
        return db.scalar(
            select(StudentIncentiveProfile).where(StudentIncentiveProfile.student_id == student_id)
        )


def _insert_event(db: Session, student_id: int, draft: IncentiveEventDraft) -> StudentIncentiveEvent | None:
    event = StudentIncentiveEvent(
        student_id=student_id,
        subject=draft.subject,
        conversation_id=draft.conversation_id,
        event_type=draft.event_type,
        points=0,
        payload={**draft.payload, "proposed_points": draft.points},
        dedup_key=draft.dedup_key,
        created_by=draft.created_by,
    )
    try:
        with db.begin_nested():
            db.add(event)
            db.flush()
        return event
    except IntegrityError:
        return None


def _event_cap_reached(
    db: Session,
    *,
    student_id: int,
    draft: IncentiveEventDraft,
    day_start: datetime,
    day_end: datetime,
    params: dict[str, Any],
) -> bool:
    caps = params["daily_event_caps"]
    cap = caps.get(draft.event_type)
    if draft.event_type in {"practice_passed", "practice_partial"}:
        cap = caps.get("practice_per_conversation")
        count = db.scalar(
            select(func.count(StudentIncentiveEvent.id)).where(
                StudentIncentiveEvent.student_id == student_id,
                StudentIncentiveEvent.conversation_id == draft.conversation_id,
                StudentIncentiveEvent.event_type.in_(["practice_passed", "practice_partial"]),
                StudentIncentiveEvent.created_at >= day_start,
                StudentIncentiveEvent.created_at < day_end,
                StudentIncentiveEvent.points > 0,
            )
        ) or 0
        return cap is not None and count >= cap
    if cap is None:
        return False
    count = db.scalar(
        select(func.count(StudentIncentiveEvent.id)).where(
            StudentIncentiveEvent.student_id == student_id,
            StudentIncentiveEvent.event_type == draft.event_type,
            StudentIncentiveEvent.created_at >= day_start,
            StudentIncentiveEvent.created_at < day_end,
            StudentIncentiveEvent.points > 0,
        )
    ) or 0
    return count >= cap


def _badge_names(profile: StudentIncentiveProfile) -> list[str]:
    counters = profile.counters or {}
    badges = list(profile.badges or [])
    for badge, event_type, threshold in BADGE_RULES:
        if int(counters.get(event_type, 0)) >= threshold and badge not in badges:
            badges.append(badge)
    if profile.current_streak_days >= 3 and "三日火苗" not in badges:
        badges.append("三日火苗")
    if profile.current_streak_days >= 7 and "七日星火" not in badges:
        badges.append("七日星火")
    if profile.current_streak_days >= 14 and "两周恒心" not in badges:
        badges.append("两周恒心")
    if profile.current_streak_days >= 30 and "月度恒星" not in badges:
        badges.append("月度恒星")
    subjects = counters.get("quality_resolve_subjects") or []
    if len(set(subjects)) >= 3 and "全科探索" not in badges:
        badges.append("全科探索")
    return badges


def record_events(
    db: Session,
    *,
    student_id: int,
    drafts: Iterable[IncentiveEventDraft],
    params: dict[str, Any],
    occurred_at: datetime | None = None,
) -> IncentiveGrant:
    now = occurred_at or now_beijing()
    local_day = now.astimezone(BEIJING_TZ).date() if now.tzinfo else now.date()
    day_start, day_end = _day_bounds(local_day)
    inserted: list[tuple[StudentIncentiveEvent, IncentiveEventDraft]] = []
    # The first ledger INSERT obtains SQLite's writer lock. Reading/updating the
    # profile only afterwards prevents two writers from calculating from the
    # same stale projection snapshot.
    for draft in drafts:
        event = _insert_event(db, student_id, draft)
        if event is not None:
            inserted.append((event, draft))

    profile = _profile_or_create(db, student_id)
    old_level = profile.level or 1
    old_badges = set(profile.badges or [])
    counters = dict(profile.counters or {})

    if profile.daily_points_date != local_day:
        profile.daily_points_date = local_day
        profile.daily_points = 0

    for event, draft in inserted:
        capped = _event_cap_reached(
            db,
            student_id=student_id,
            draft=draft,
            day_start=day_start,
            day_end=day_end,
            params=params,
        )
        if profile.daily_points + draft.points > params["daily_total_cap"]:
            capped = True
        event.points = 0 if capped else draft.points
        event.payload = {**(event.payload or {}), "capped": capped}
        if not capped:
            profile.daily_points += draft.points
            profile.total_points += draft.points
        counters[draft.event_type] = int(counters.get(draft.event_type, 0)) + 1
        if draft.event_type in QUALITY_RESOLVE_EVENTS and draft.subject:
            subjects = list(counters.get("quality_resolve_subjects") or [])
            if draft.subject not in subjects:
                subjects.append(draft.subject)
            counters["quality_resolve_subjects"] = subjects

    valid_learning = any(
        draft.event_type in VALID_LEARNING_EVENTS and bool(draft.payload.get("valid_learning", True))
        for _, draft in inserted
    )
    if valid_learning and profile.last_valid_learning_date != local_day:
        if profile.last_valid_learning_date and _streak_continues(
            profile.last_valid_learning_date, local_day, params["streak_weekend_grace"]
        ):
            profile.current_streak_days += 1
        else:
            profile.current_streak_days = 1
        profile.longest_streak_days = max(profile.longest_streak_days, profile.current_streak_days)
        profile.last_valid_learning_date = local_day

    profile.counters = counters
    profile.level = _level_for_points(profile.total_points, params["level_thresholds"])
    profile.badges = _badge_names(profile)
    db.add(profile)
    for event, _ in inserted:
        db.add(event)
    db.flush()
    for event, draft in inserted:
        incentive_events_total.labels(event_type=draft.event_type, awarded=str(event.points > 0).lower()).inc()

    awarded = [(event, draft) for event, draft in inserted if event.points > 0]
    return IncentiveGrant(
        points_awarded=sum(event.points for event, _ in awarded),
        awarded_events=[draft.event_type for _, draft in awarded],
        new_badges=[badge for badge in profile.badges if badge not in old_badges],
        level_up=profile.level if profile.level > old_level else None,
        level=profile.level,
        total_points=profile.total_points,
        streak=profile.current_streak_days,
    )


def reflections_are_similar(left: str | None, right: str | None, *, threshold: float = 0.9) -> bool:
    normalized_left = "".join((left or "").split())
    normalized_right = "".join((right or "").split())
    if not normalized_left or not normalized_right:
        return False
    return SequenceMatcher(None, normalized_left, normalized_right).ratio() >= threshold


def rebuild_profile(
    db: Session,
    student_id: int,
    params: dict[str, Any] | None = None,
) -> StudentIncentiveProfile:
    """Rebuild the mutable student projection from the append-only ledger."""

    resolved = params or DEFAULT_INCENTIVE_PARAMS
    events = db.scalars(
        select(StudentIncentiveEvent)
        .where(StudentIncentiveEvent.student_id == student_id)
        .order_by(StudentIncentiveEvent.created_at.asc(), StudentIncentiveEvent.id.asc())
    ).all()
    profile = _profile_or_create(db, student_id)
    last_praise_read_at = profile.last_praise_read_at
    counters: dict[str, Any] = {}
    quality_subjects: list[str] = []
    learning_days: list[date] = []
    today = now_beijing().date()
    daily_points = 0
    for event in events:
        counters[event.event_type] = int(counters.get(event.event_type, 0)) + 1
        if event.event_type in QUALITY_RESOLVE_EVENTS and event.subject and event.subject not in quality_subjects:
            quality_subjects.append(event.subject)
        payload = event.payload or {}
        if event.event_type in VALID_LEARNING_EVENTS and bool(payload.get("valid_learning", True)):
            event_time = event.created_at
            if event_time.tzinfo is None:
                event_time = event_time.replace(tzinfo=UTC)
            learning_days.append(event_time.astimezone(BEIJING_TZ).date())
        event_time = event.created_at
        if event_time.tzinfo is None:
            event_time = event_time.replace(tzinfo=UTC)
        if event_time.astimezone(BEIJING_TZ).date() == today:
            daily_points += event.points
    if quality_subjects:
        counters["quality_resolve_subjects"] = quality_subjects

    unique_days = sorted(set(learning_days))
    longest = 0
    current_run = 0
    previous: date | None = None
    for learning_day in unique_days:
        current_run = (
            current_run + 1
            if previous is not None and _streak_continues(previous, learning_day, resolved["streak_weekend_grace"])
            else 1
        )
        longest = max(longest, current_run)
        previous = learning_day

    profile.total_points = sum(event.points for event in events)
    profile.level = _level_for_points(profile.total_points, resolved["level_thresholds"])
    profile.current_streak_days = current_run if unique_days else 0
    profile.longest_streak_days = longest
    profile.last_valid_learning_date = unique_days[-1] if unique_days else None
    profile.daily_points = daily_points
    profile.daily_points_date = today
    profile.counters = counters
    profile.badges = []
    profile.badges = _badge_names(profile)
    profile.last_praise_read_at = last_praise_read_at
    db.add(profile)
    db.flush()
    return profile


def get_summary(db: Session, student_id: int, params: dict[str, Any] | None = None) -> dict[str, Any]:
    resolved = params or DEFAULT_INCENTIVE_PARAMS
    profile = db.scalar(select(StudentIncentiveProfile).where(StudentIncentiveProfile.student_id == student_id))
    if profile is None:
        profile = StudentIncentiveProfile(student_id=student_id, badges=[], counters={})
    thresholds = resolved["level_thresholds"]
    level = max(1, profile.level or 1)
    next_threshold = thresholds[level] if level < len(thresholds) else None
    return {
        "total_points": profile.total_points or 0,
        "level": level,
        "next_level_points": next_threshold,
        "current_streak_days": profile.current_streak_days or 0,
        "longest_streak_days": profile.longest_streak_days or 0,
        "badges": list(profile.badges or []),
        "counters": dict(profile.counters or {}),
        "has_unread_praise": False,
    }


def get_report(db: Session, student_id: int, period: str) -> dict[str, Any]:
    days = 7 if period == "week" else 30
    since = now_utc() - timedelta(days=days)
    events = db.scalars(
        select(StudentIncentiveEvent)
        .where(StudentIncentiveEvent.student_id == student_id, StudentIncentiveEvent.created_at >= since)
        .order_by(StudentIncentiveEvent.created_at.asc())
    ).all()
    event_counts: dict[str, int] = {}
    subject_points: dict[str, int] = {}
    daily_points: dict[str, int] = {}
    for event in events:
        event_counts[event.event_type] = event_counts.get(event.event_type, 0) + 1
        if event.subject:
            subject_points[event.subject] = subject_points.get(event.subject, 0) + event.points
        event_time = event.created_at
        if event_time.tzinfo is None:
            event_time = event_time.replace(tzinfo=UTC)
        day = event_time.astimezone(BEIJING_TZ).date().isoformat()
        daily_points[day] = daily_points.get(day, 0) + event.points
    followups = event_counts.get("followup_answered", 0)
    completions = event_counts.get("conversation_completed", 0)
    early = event_counts.get("early_resolved", 0)
    narrative = "你正在稳定积累思考过程。"
    if followups >= 5:
        narrative = "你本期多次回应引导问题，主动推理的习惯正在形成。"
    if early >= 3:
        narrative = "你本期多次在兜底讲解前完成推导，独立解题能力有明显进步。"
    return {
        "period": period,
        "event_counts": event_counts,
        "subject_points": subject_points,
        "daily_points": [{"date": key, "points": value} for key, value in sorted(daily_points.items())],
        "followup_rate": round(followups / max(1, completions), 3),
        "early_resolve_rate": round(early / max(1, completions), 3),
        "narrative": narrative,
    }


def conversation_signal_snapshot(db: Session, conversation_id: int) -> dict[str, Any]:
    messages = db.scalars(select(Message).where(Message.conversation_id == conversation_id)).all()
    return {
        "user_turn_count": sum(1 for message in messages if message.role == MessageRole.USER),
        "had_fallback": any(
            message.role == MessageRole.ASSISTANT and message.guidance_stage == GuidanceStage.FALLBACK
            for message in messages
        ),
    }


incentive_service = type(
    "IncentiveServiceFacade",
    (),
    {
        "resolve_config": staticmethod(resolve_config),
        "extract_practice_verdict": staticmethod(extract_practice_verdict),
        "evaluate_turn": staticmethod(evaluate_turn),
        "evaluate_resolve": staticmethod(evaluate_resolve),
        "record_events": staticmethod(record_events),
        "rebuild_profile": staticmethod(rebuild_profile),
        "reflections_are_similar": staticmethod(reflections_are_similar),
        "get_summary": staticmethod(get_summary),
        "get_report": staticmethod(get_report),
        "conversation_signal_snapshot": staticmethod(conversation_signal_snapshot),
    },
)()
