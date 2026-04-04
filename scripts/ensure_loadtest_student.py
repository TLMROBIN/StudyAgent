from __future__ import annotations

from pathlib import Path
import sys

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.database import SessionLocal
from backend.models.user import Classroom, User, UserRole
from backend.security import get_password_hash

STUDENT_NO = "20269999"
PASSWORD = "Loadtest123"
FULL_NAME = "压测学生"


def ensure_classroom(db) -> Classroom:
    classroom = db.scalar(select(Classroom).where(Classroom.grade == 2, Classroom.name == "压测班"))
    if classroom:
        return classroom

    classroom = Classroom(grade=2, name="压测班")
    db.add(classroom)
    db.commit()
    db.refresh(classroom)
    return classroom


def ensure_student() -> None:
    db = SessionLocal()
    try:
        classroom = ensure_classroom(db)
        user = db.scalar(select(User).where(User.student_no == STUDENT_NO))
        if user:
            user.username = STUDENT_NO
            user.full_name = FULL_NAME
            user.role = UserRole.STUDENT
            user.grade = 2
            user.classroom_id = classroom.id
            user.password_hash = get_password_hash(PASSWORD)
            user.must_change_password = False
            user.is_active = True
            user.failed_login_count = 0
            user.locked_until = None
            db.add(user)
            db.commit()
            print(f"updated loadtest student: {STUDENT_NO} / {PASSWORD}")
            return

        user = User(
            username=STUDENT_NO,
            student_no=STUDENT_NO,
            full_name=FULL_NAME,
            role=UserRole.STUDENT,
            password_hash=get_password_hash(PASSWORD),
            grade=2,
            classroom_id=classroom.id,
            must_change_password=False,
            is_active=True,
        )
        db.add(user)
        db.commit()
        print(f"created loadtest student: {STUDENT_NO} / {PASSWORD}")
    finally:
        db.close()


if __name__ == "__main__":
    ensure_student()
