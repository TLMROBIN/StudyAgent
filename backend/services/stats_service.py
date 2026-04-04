from __future__ import annotations

from collections import Counter, defaultdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from backend.models.conversation import Conversation, GuidanceStage, Message, MessageRole
from backend.models.user import User, UserRole


class StatsService:
    def overview(self, db: Session, viewer: User) -> dict:
        students = self._scoped_students(db, viewer)
        conversations = self._flatten_conversations(students)
        total_questions = len(conversations)
        resolved_rate = (sum(1 for item in conversations if item.resolved) / total_questions) if total_questions else 0.0
        average_turns = (
            sum(self._conversation_turns(conversation) for conversation in conversations) / total_questions
            if total_questions
            else 0.0
        )
        subject_counter = Counter(conversation.subject for conversation in conversations if conversation.subject)
        return {
            "total_questions": total_questions,
            "resolved_rate": round(resolved_rate, 4),
            "average_turns": round(average_turns, 2),
            "by_subject": [
                {"subject": subject, "count": count}
                for subject, count in sorted(subject_counter.items(), key=lambda item: (-item[1], item[0]))
            ],
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
        }

    def classroom_breakdown(self, db: Session, viewer: User) -> list[dict]:
        students = self._scoped_students(db, viewer)
        buckets: dict[str, dict] = defaultdict(
            lambda: {
                "grade": None,
                "classroom_name": None,
                "student_ids": set(),
                "conversations": [],
            }
        )
        for student in students:
            label = student.classroom_label or "未分班"
            bucket = buckets[label]
            bucket["grade"] = student.grade
            bucket["classroom_name"] = student.classroom.name if student.classroom else None
            bucket["student_ids"].add(student.id)
            bucket["conversations"].extend(student.conversations)

        rows = []
        for label, bucket in buckets.items():
            conversations = bucket["conversations"]
            total = len(conversations)
            rows.append(
                {
                    "classroom_label": label,
                    "grade": bucket["grade"],
                    "classroom_name": bucket["classroom_name"],
                    "student_count": len(bucket["student_ids"]),
                    "total_conversations": total,
                    "resolved_rate": round(
                        (sum(1 for conversation in conversations if conversation.resolved) / total) if total else 0.0,
                        4,
                    ),
                    "average_turns": round(
                        (sum(self._conversation_turns(conversation) for conversation in conversations) / total) if total else 0.0,
                        2,
                    ),
                }
            )
        return sorted(rows, key=lambda item: (-item["total_conversations"], item["classroom_label"]))

    def student_portraits(self, db: Session, viewer: User, limit: int = 12) -> list[dict]:
        students = [student for student in self._scoped_students(db, viewer) if student.conversations]
        portraits = []
        for student in students:
            conversations = list(student.conversations)
            total = len(conversations)
            subject_counter = Counter(conversation.subject for conversation in conversations if conversation.subject)
            portraits.append(
                {
                    "student_id": student.id,
                    "student_name": student.full_name,
                    "student_no": student.student_no,
                    "classroom_label": student.classroom_label,
                    "total_conversations": total,
                    "resolved_rate": round(
                        (sum(1 for conversation in conversations if conversation.resolved) / total) if total else 0.0,
                        4,
                    ),
                    "focus_subject": max(subject_counter, key=subject_counter.get) if subject_counter else None,
                    "fallback_ratio": round(
                        (sum(1 for conversation in conversations if conversation.guidance_stage == GuidanceStage.FALLBACK) / total)
                        if total
                        else 0.0,
                        4,
                    ),
                    "last_active_at": max((conversation.updated_at for conversation in conversations), default=None),
                }
            )
        portraits.sort(
            key=lambda item: (
                -item["total_conversations"],
                item["resolved_rate"],
                item["student_name"],
            )
        )
        return portraits[:limit]

    def _scoped_students(self, db: Session, viewer: User) -> list[User]:
        statement = (
            select(User)
            .where(User.role == UserRole.STUDENT)
            .options(
                selectinload(User.classroom),
                selectinload(User.conversations).selectinload(Conversation.messages),
            )
            .order_by(User.full_name.asc())
        )
        if viewer.role == UserRole.TEACHER:
            classroom_ids = [classroom.id for classroom in viewer.teacher_classrooms]
            if not classroom_ids:
                return []
            statement = statement.where(User.classroom_id.in_(classroom_ids))
        elif viewer.role == UserRole.STUDENT:
            statement = statement.where(User.id == viewer.id)
        return db.scalars(statement).unique().all()

    @staticmethod
    def _flatten_conversations(students: list[User]) -> list[Conversation]:
        return [conversation for student in students for conversation in student.conversations]

    @staticmethod
    def _conversation_turns(conversation: Conversation) -> int:
        return sum(1 for message in conversation.messages if message.role == MessageRole.USER)


stats_service = StatsService()
