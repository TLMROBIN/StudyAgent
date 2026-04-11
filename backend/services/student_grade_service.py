from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.grade_utils import effective_promotion_year
from backend.models.user import User, UserRole
from backend.time_utils import assume_utc, now_beijing


class StudentGradeService:
    def apply_manual_grade_state(
        self,
        user: User,
        *,
        grade: int | None,
        is_graduated: bool = False,
        now: datetime | None = None,
    ) -> None:
        current_time = now or now_beijing()
        if user.role != UserRole.STUDENT:
            user.grade = grade
            user.graduated_at = None
            user.last_grade_promotion_year = None
            return

        if is_graduated:
            user.grade = None
            user.graduated_at = assume_utc(current_time)
        else:
            user.grade = grade
            user.graduated_at = None
        user.last_grade_promotion_year = effective_promotion_year(current_time)

    def ensure_user_grade_current(self, db: Session, user: User, *, now: datetime | None = None) -> bool:
        current_time = now or now_beijing()
        if user.role != UserRole.STUDENT:
            return False

        current_cycle = effective_promotion_year(current_time)
        if user.last_grade_promotion_year is None:
            user.last_grade_promotion_year = current_cycle
            db.add(user)
            db.commit()
            db.refresh(user)
            return True

        if user.graduated_at is not None or user.grade is None or current_cycle <= user.last_grade_promotion_year:
            return False

        changed = self._advance_grade(user, steps=current_cycle - user.last_grade_promotion_year, now=current_time)
        user.last_grade_promotion_year = current_cycle
        if not changed:
            return False

        db.add(user)
        db.commit()
        db.refresh(user)
        return True

    def initialize_students(self, db: Session, *, now: datetime | None = None) -> int:
        current_time = now or now_beijing()
        current_cycle = effective_promotion_year(current_time)
        changed = 0
        students = db.scalars(select(User).where(User.role == UserRole.STUDENT)).all()
        for student in students:
            if student.last_grade_promotion_year is None:
                student.last_grade_promotion_year = current_cycle
                db.add(student)
                changed += 1
                continue
            if student.graduated_at is not None or student.grade is None or current_cycle <= student.last_grade_promotion_year:
                continue
            if self._advance_grade(student, steps=current_cycle - student.last_grade_promotion_year, now=current_time):
                student.last_grade_promotion_year = current_cycle
                db.add(student)
                changed += 1
        if changed:
            db.commit()
        return changed

    def _advance_grade(self, user: User, *, steps: int, now: datetime) -> bool:
        changed = False
        for _ in range(steps):
            if user.grade == 1:
                user.grade = 2
                changed = True
            elif user.grade == 2:
                user.grade = 3
                changed = True
            elif user.grade == 3:
                user.grade = None
                user.graduated_at = assume_utc(now)
                changed = True
                break
            else:
                break
        return changed


student_grade_service = StudentGradeService()
