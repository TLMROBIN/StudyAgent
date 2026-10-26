from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, selectinload

from backend.models.conversation import Conversation, GuidanceStage, Message, MessageRole
from backend.models.user import User, UserRole, teacher_classes
from backend.services.physics_error_profile_service import physics_error_profile_service


class StatsService:
    def overview(self, db: Session, viewer: User) -> dict:
        scope = self._student_scope(viewer)
        total_questions, resolved = db.execute(
            select(func.count(Conversation.id), func.sum(case((Conversation.resolved.is_(True), 1), else_=0)))
            .where(Conversation.student_id.in_(scope))
        ).one()
        turns = db.scalar(
            select(func.count(Message.id))
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(Conversation.student_id.in_(scope), Message.role == MessageRole.USER)
        ) or 0
        subject_rows = db.execute(
            select(Conversation.subject, func.count(Conversation.id))
            .where(Conversation.student_id.in_(scope), Conversation.subject != "")
            .group_by(Conversation.subject)
        ).all()
        resolved_rate = (resolved or 0) / total_questions if total_questions else 0.0
        average_turns = turns / total_questions if total_questions else 0.0
        return {
            "total_questions": total_questions,
            "resolved_rate": round(resolved_rate, 4),
            "average_turns": round(average_turns, 2),
            "by_subject": [
                {"subject": subject, "count": count}
                for subject, count in sorted(subject_rows, key=lambda item: (-item[1], item[0]))
            ],
        }

    def usage_trend(
        self,
        db: Session,
        viewer: User,
        *,
        granularity: Literal["day", "week", "month"] = "day",
        start_date: str | date | None = None,
        end_date: str | date | None = None,
        subjects: list[str] | None = None,
    ) -> dict:
        scope = self._student_scope(viewer)
        requested_subjects = [subject.strip() for subject in (subjects or []) if subject.strip()]
        available_subjects = list(db.scalars(
            select(Conversation.subject).distinct()
            .where(Conversation.student_id.in_(scope), Conversation.subject != "")
            .order_by(Conversation.subject)
        ))

        today = datetime.now(UTC).date()
        start = self._coerce_date(start_date) or (today - timedelta(days=29))
        end = self._coerce_date(end_date) or today
        if start > end:
            start, end = end, start

        labels = self._period_labels(start, end, granularity)
        label_set = set(labels)
        total_counts = dict.fromkeys(labels, 0)
        subject_counts: dict[str, dict[str, int]] = {
            subject: dict.fromkeys(labels, 0) for subject in requested_subjects
        }

        # Read only daily counts within the requested range, never message bodies.
        day = func.date(Conversation.created_at)
        daily_counts = db.execute(
            select(day, Conversation.subject, func.count(Conversation.id))
            .where(
                Conversation.student_id.in_(scope),
                Conversation.created_at >= datetime.combine(start, time.min, tzinfo=UTC),
                Conversation.created_at < datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC),
            )
            .group_by(day, Conversation.subject)
        )
        for day_value, subject, count in daily_counts:
            conversation_date = self._coerce_date(day_value)
            label = self._period_label(conversation_date, granularity)
            if label not in label_set:
                continue
            total_counts[label] += count
            if subject in subject_counts:
                subject_counts[subject][label] += count

        series = [
            {
                "name": "总次数",
                "subject": None,
                "data": [total_counts[label] for label in labels],
            }
        ]
        for subject in requested_subjects:
            if subject in subject_counts:
                series.append(
                    {
                        "name": subject,
                        "subject": subject,
                        "data": [subject_counts[subject][label] for label in labels],
                    }
                )

        return {
            "granularity": granularity,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "labels": labels,
            "available_subjects": available_subjects,
            "series": series,
        }

    def student_profile(self, db: Session, student_id: int) -> dict:
        student = db.scalar(
            select(User)
            .where(User.id == student_id)
            .options(selectinload(User.conversations).selectinload(Conversation.messages))
        )
        conversations = list(student.conversations) if student else []
        total = len(conversations)
        resolved = sum(1 for item in conversations if item.resolved)
        subject_counter = Counter(conversation.subject for conversation in conversations if conversation.subject)
        return {
            "student_id": student_id,
            "total_conversations": total,
            "resolved_rate": round((resolved / total) if total else 0.0, 4),
            "subject_breakdown": [
                {"subject": subject, "count": count}
                for subject, count in sorted(subject_counter.items(), key=lambda item: (-item[1], item[0]))
            ],
            "focus_subject": max(subject_counter, key=subject_counter.get) if subject_counter else None,
            "fallback_ratio": round(
                (sum(1 for conversation in conversations if conversation.guidance_stage == GuidanceStage.FALLBACK) / total)
                if total
                else 0.0,
                4,
            ),
            "last_active_at": max((conversation.updated_at for conversation in conversations), default=None),
            "physics_error_profile": physics_error_profile_service.profile_summary(db, student_id=student_id, subject="物理"),
        }

    def classroom_breakdown(self, db: Session, viewer: User) -> list[dict]:
        students = self._scoped_students(db, viewer)
        totals = self._student_totals(db, viewer)
        turns = dict(db.execute(
            select(Conversation.student_id, func.count(Message.id))
            .join(Message, Message.conversation_id == Conversation.id)
            .where(Conversation.student_id.in_(self._student_scope(viewer)), Message.role == MessageRole.USER)
            .group_by(Conversation.student_id)
        ).all())
        buckets: dict[str, dict] = defaultdict(
            lambda: {
                "grade": None,
                "classroom_name": None,
                "student_ids": set(),
                "total": 0,
                "resolved": 0,
                "turns": 0,
            }
        )
        for student in students:
            label = student.classroom_label or "未分班"
            bucket = buckets[label]
            bucket["grade"] = student.grade
            bucket["classroom_name"] = student.classroom.name if student.classroom else None
            bucket["student_ids"].add(student.id)
            summary = totals.get(student.id)
            if summary:
                bucket["total"] += summary.total
                bucket["resolved"] += summary.resolved
            bucket["turns"] += turns.get(student.id, 0)

        rows = []
        for label, bucket in buckets.items():
            total = bucket["total"]
            rows.append(
                {
                    "classroom_label": label,
                    "grade": bucket["grade"],
                    "classroom_name": bucket["classroom_name"],
                    "student_count": len(bucket["student_ids"]),
                    "total_conversations": total,
                    "resolved_rate": round(
                        (bucket["resolved"] / total) if total else 0.0,
                        4,
                    ),
                    "average_turns": round(
                        (bucket["turns"] / total) if total else 0.0,
                        2,
                    ),
                }
            )
        return sorted(rows, key=lambda item: (-item["total_conversations"], item["classroom_label"]))

    def student_portraits(self, db: Session, viewer: User, limit: int = 12) -> list[dict]:
        totals = self._student_totals(db, viewer)
        portraits = []
        for student in self._scoped_students(db, viewer):
            summary = totals.get(student.id)
            if summary is None:
                continue
            portraits.append(
                {
                    "student_id": student.id,
                    "student_name": student.full_name,
                    "login_account": student.username,
                    "classroom_label": student.classroom_label,
                    "total_conversations": summary.total,
                    "resolved_rate": round(summary.resolved / summary.total, 4),
                    "focus_subject": None,
                    "fallback_ratio": round(summary.fallback / summary.total, 4),
                    "last_active_at": summary.last_active_at,
                }
            )
        portraits.sort(key=lambda item: (-item["total_conversations"], item["resolved_rate"], item["student_name"]))
        portraits = portraits[:limit]
        if portraits:
            # Match Counter's original tie break: the subject seen in the first conversation wins.
            subject_rows = db.execute(
                select(Conversation.student_id, Conversation.subject, func.count(Conversation.id), func.min(Conversation.id))
                .where(
                    Conversation.student_id.in_([item["student_id"] for item in portraits]),
                    Conversation.subject != "",
                )
                .group_by(Conversation.student_id, Conversation.subject)
            ).all()
            focus = {}
            for student_id, subject, count, first_id in sorted(subject_rows, key=lambda row: (-row[2], row[3])):
                focus.setdefault(student_id, subject)
            for portrait in portraits:
                portrait["focus_subject"] = focus.get(portrait["student_id"])
        return portraits

    @staticmethod
    def _student_scope(viewer: User):
        statement = select(User.id).where(User.role == UserRole.STUDENT)
        if viewer.role == UserRole.TEACHER:
            statement = statement.where(User.classroom_id.in_(
                select(teacher_classes.c.classroom_id).where(teacher_classes.c.teacher_id == viewer.id)
            ))
        elif viewer.role == UserRole.STUDENT:
            statement = statement.where(User.id == viewer.id)
        return statement

    def _student_totals(self, db: Session, viewer: User) -> dict:
        rows = db.execute(
            select(
                Conversation.student_id,
                func.count(Conversation.id).label("total"),
                func.sum(case((Conversation.resolved.is_(True), 1), else_=0)).label("resolved"),
                func.sum(case((Conversation.guidance_stage == GuidanceStage.FALLBACK, 1), else_=0)).label("fallback"),
                func.max(Conversation.updated_at).label("last_active_at"),
            )
            .where(Conversation.student_id.in_(self._student_scope(viewer)))
            .group_by(Conversation.student_id)
        ).all()
        return {row.student_id: row for row in rows}

    def _scoped_students(self, db: Session, viewer: User) -> list[User]:
        # Metadata only. Loading conversation/message relationships here causes
        # every dashboard request to materialize the school's full chat history.
        return list(db.scalars(
            select(User).where(User.id.in_(self._student_scope(viewer)))
            .options(selectinload(User.classroom))
            .order_by(User.full_name.asc())
        ))

    @staticmethod
    def _coerce_date(value: str | date | None) -> date | None:
        if value is None:
            return None
        if isinstance(value, date):
            return value
        return date.fromisoformat(value)

    @staticmethod
    def _as_date(value: datetime | None) -> date | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.date()
        return value.astimezone(UTC).date()

    @classmethod
    def _period_labels(cls, start: date, end: date, granularity: str) -> list[str]:
        labels = []
        cursor = cls._period_start(start, granularity)
        final = cls._period_start(end, granularity)
        while cursor <= final:
            labels.append(cls._period_label(cursor, granularity))
            cursor = cls._next_period(cursor, granularity)
        return labels

    @staticmethod
    def _period_start(value: date, granularity: str) -> date:
        if granularity == "week":
            return value - timedelta(days=value.weekday())
        if granularity == "month":
            return value.replace(day=1)
        return value

    @classmethod
    def _period_label(cls, value: date, granularity: str) -> str:
        start = cls._period_start(value, granularity)
        if granularity == "month":
            return start.strftime("%Y-%m")
        if granularity == "week":
            return start.isoformat()
        return value.isoformat()

    @staticmethod
    def _next_period(value: date, granularity: str) -> date:
        if granularity == "week":
            return value + timedelta(days=7)
        if granularity == "month":
            if value.month == 12:
                return value.replace(year=value.year + 1, month=1, day=1)
            return value.replace(month=value.month + 1, day=1)
        return value + timedelta(days=1)


stats_service = StatsService()
