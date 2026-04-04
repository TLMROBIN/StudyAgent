import asyncio
import io
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base
from backend.models import agent_config, audit_log, conversation, knowledge, user  # noqa: F401
from backend.models.conversation import Conversation, GuidanceStage, Message, MessageRole
from backend.models.user import Classroom, User, UserRole
from backend.routers import admin as admin_router
from backend.routers import stats as stats_router
from backend.services.stats_service import stats_service


def build_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)
    return SessionLocal


class FakeUploadFile:
    def __init__(self, filename: str, payload: bytes):
        self.filename = filename
        self._payload = payload

    async def read(self) -> bytes:
        return self._payload


def test_stats_service_returns_classroom_breakdown_and_portraits():
    SessionLocal = build_session()
    session = SessionLocal()
    try:
        classroom = Classroom(grade=1, name="1班")
        teacher = User(
            username="teacher1",
            full_name="数学老师",
            role=UserRole.TEACHER,
            password_hash="hash",
            grade=1,
        )
        teacher.teacher_classrooms.append(classroom)
        student = User(
            username="20260001",
            student_no="20260001",
            full_name="张三",
            role=UserRole.STUDENT,
            password_hash="hash",
            grade=1,
            classroom=classroom,
        )
        session.add_all([classroom, teacher, student])
        session.commit()
        session.refresh(student)

        conversation_row = Conversation(
            student_id=student.id,
            subject="数学",
            resolved=True,
            guidance_stage=GuidanceStage.FALLBACK,
        )
        session.add(conversation_row)
        session.commit()
        session.refresh(conversation_row)
        session.add_all(
            [
                Message(conversation_id=conversation_row.id, role=MessageRole.USER, content="函数单调性怎么判断"),
                Message(conversation_id=conversation_row.id, role=MessageRole.ASSISTANT, content="先看定义域"),
            ]
        )
        session.commit()
        session.refresh(teacher)

        classroom_rows = stats_service.classroom_breakdown(session, teacher)
        portrait_rows = stats_service.student_portraits(session, teacher)

        assert len(classroom_rows) == 1
        assert classroom_rows[0]["classroom_label"] == "1年级1班"
        assert classroom_rows[0]["student_count"] == 1
        assert classroom_rows[0]["total_conversations"] == 1

        assert len(portrait_rows) == 1
        assert portrait_rows[0]["student_name"] == "张三"
        assert portrait_rows[0]["focus_subject"] == "数学"
        assert portrait_rows[0]["fallback_ratio"] == 1.0
    finally:
        session.close()


def test_import_students_returns_detailed_feedback(monkeypatch):
    SessionLocal = build_session()
    session = SessionLocal()
    try:
        monkeypatch.setattr(admin_router, "get_password_hash", lambda password: f"hash:{password}")
        admin_user = User(
            username="admin",
            full_name="管理员",
            role=UserRole.ADMIN,
            password_hash="hash",
        )
        existing_student = User(
            username="20260002",
            student_no="20260002",
            full_name="已存在学生",
            role=UserRole.STUDENT,
            password_hash="hash",
        )
        session.add_all([admin_user, existing_student])
        session.commit()
        session.refresh(admin_user)

        csv_payload = (
            "student_no,full_name,grade,class_name\n"
            "20260001,新学生,1,1班\n"
            "20260002,重复学生,1,1班\n"
            ",缺少学号,1,1班\n"
            "20260003,非法年级,abc,1班\n"
            "20260001,文件内重复,1,1班\n"
        )
        upload = FakeUploadFile(filename="students.csv", payload=csv_payload.encode("utf-8"))
        request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))

        result = asyncio.run(
            admin_router.import_students(
                file=upload,
                db=session,
                current_user=admin_user,
                request=request,
            )
        )

        assert result.rows == 5
        assert result.created == 1
        assert result.skipped_existing == 1
        assert result.invalid == 3
        assert len(result.issues) == 4
        assert {issue.reason for issue in result.issues} >= {
            "student_already_exists",
            "missing_student_no",
            "invalid_grade",
            "duplicate_student_no_in_file",
        }
    finally:
        session.close()


def test_import_students_supports_xlsx(monkeypatch):
    from openpyxl import Workbook

    SessionLocal = build_session()
    session = SessionLocal()
    try:
        monkeypatch.setattr(admin_router, "get_password_hash", lambda password: f"hash:{password}")
        admin_user = User(
            username="admin",
            full_name="管理员",
            role=UserRole.ADMIN,
            password_hash="hash",
        )
        session.add(admin_user)
        session.commit()
        session.refresh(admin_user)

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["student_no", "full_name", "grade", "class_name"])
        sheet.append(["20260011", "表格学生", 2, "3班"])
        payload = io.BytesIO()
        workbook.save(payload)

        upload = FakeUploadFile(filename="students.xlsx", payload=payload.getvalue())
        request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))

        result = asyncio.run(
            admin_router.import_students(
                file=upload,
                db=session,
                current_user=admin_user,
                request=request,
            )
        )

        created_student = session.query(User).filter(User.student_no == "20260011").one()
        assert result.rows == 1
        assert result.created == 1
        assert created_student.full_name == "表格学生"
        assert created_student.grade == 2
    finally:
        session.close()


def test_stats_export_supports_xlsx():
    from openpyxl import load_workbook

    SessionLocal = build_session()
    session = SessionLocal()
    try:
        classroom = Classroom(grade=1, name="1班")
        admin_user = User(
            username="admin",
            full_name="管理员",
            role=UserRole.ADMIN,
            password_hash="hash",
        )
        student = User(
            username="20260001",
            student_no="20260001",
            full_name="张三",
            role=UserRole.STUDENT,
            password_hash="hash",
            grade=1,
            classroom=classroom,
        )
        session.add_all([classroom, admin_user, student])
        session.commit()
        session.refresh(student)

        conversation_row = Conversation(
            student_id=student.id,
            subject="数学",
            resolved=True,
            guidance_stage=GuidanceStage.HINT,
        )
        session.add(conversation_row)
        session.commit()
        session.add_all(
            [
                Message(conversation_id=conversation_row.id, role=MessageRole.USER, content="函数题怎么做"),
                Message(conversation_id=conversation_row.id, role=MessageRole.ASSISTANT, content="先看定义域"),
            ]
        )
        session.commit()

        response = stats_router.export_stats(session, admin_user, format="xlsx")
        workbook = load_workbook(io.BytesIO(response.body))

        assert response.media_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        assert workbook.sheetnames == ["概览", "学科分布", "班级统计", "学生画像"]
        assert workbook["概览"]["A2"].value == "累计提问"
        assert workbook["概览"]["B2"].value == 1
        assert workbook["学科分布"]["A2"].value == "数学"
    finally:
        session.close()
