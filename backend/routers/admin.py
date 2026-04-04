from __future__ import annotations

import csv
import io

from fastapi import APIRouter, File, HTTPException, Request, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.dependencies import CurrentAdmin, DbSession
from backend.models.audit_log import AuditLog
from backend.models.schemas import AuditLogRead, PasswordResetRequest, StudentImportIssue, StudentImportResult, UserCreate, UserRead
from backend.models.user import Classroom, User, UserRole
from backend.security import get_password_hash
from backend.services.audit_service import audit_service
from backend.services.auth_service import auth_service

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _get_or_create_classroom(db: DbSession, grade: int | None, classroom_name: str | None) -> Classroom | None:
    if grade is None or not classroom_name:
        return None
    classroom = db.scalar(select(Classroom).where(Classroom.grade == grade, Classroom.name == classroom_name))
    if classroom:
        return classroom
    classroom = Classroom(grade=grade, name=classroom_name)
    db.add(classroom)
    db.commit()
    db.refresh(classroom)
    return classroom


@router.get("/users", response_model=list[UserRead])
def list_users(db: DbSession, current_user: CurrentAdmin) -> list[UserRead]:
    users = db.scalars(select(User).order_by(User.created_at.desc())).all()
    return [UserRead.model_validate(user) for user in users]


@router.post("/users", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def create_user(payload: UserCreate, db: DbSession, current_user: CurrentAdmin, request: Request) -> UserRead:
    existing = db.scalar(select(User).where(User.username == payload.username).limit(1))
    if existing:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Username already exists")

    user = User(
        username=payload.username,
        student_no=payload.student_no,
        full_name=payload.full_name,
        role=payload.role,
        password_hash=get_password_hash(payload.password),
        grade=payload.grade,
        classroom_id=payload.classroom_id,
        must_change_password=payload.role == UserRole.STUDENT,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    audit_service.log(
        db,
        actor=current_user,
        action="create_user",
        target_type="user",
        target_id=str(user.id),
        result="success",
        ip_address=request.client.host if request.client else None,
        detail={"role": user.role.value, "username": user.username},
    )
    return UserRead.model_validate(user)


@router.post("/users/reset-password", response_model=UserRead)
def reset_password(payload: PasswordResetRequest, db: DbSession, current_user: CurrentAdmin, request: Request) -> UserRead:
    user = db.get(User, payload.user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    updated = auth_service.update_password(db, user, payload.new_password)
    updated.must_change_password = True
    db.add(updated)
    db.commit()
    db.refresh(updated)
    audit_service.log(
        db,
        actor=current_user,
        action="reset_password",
        target_type="user",
        target_id=str(updated.id),
        result="success",
        ip_address=request.client.host if request.client else None,
        detail={"username": updated.username},
    )
    return UserRead.model_validate(updated)


@router.post("/students/import", response_model=StudentImportResult)
async def import_students(
    file: UploadFile = File(...),
    db: DbSession = None,
    current_user: CurrentAdmin = None,
    request: Request = None,
) -> StudentImportResult:
    suffix = (file.filename or "").lower().rsplit(".", 1)[-1]
    content = await file.read()
    rows: list[dict[str, str]] = []

    if suffix == "csv":
        reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
        rows = [dict(row) for row in reader]
    elif suffix == "xlsx":
        try:
            from openpyxl import load_workbook
        except ImportError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="缺少 openpyxl，当前无法解析 xlsx") from exc

        workbook = load_workbook(io.BytesIO(content))
        sheet = workbook.active
        headers = [str(cell.value).strip() if cell.value else "" for cell in sheet[1]]
        for row in sheet.iter_rows(min_row=2, values_only=True):
            rows.append({headers[index]: str(value).strip() if value is not None else "" for index, value in enumerate(row)})
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only csv/xlsx supported")

    created = 0
    skipped_existing = 0
    invalid = 0
    issues: list[StudentImportIssue] = []
    seen_student_nos: set[str] = set()

    for index, row in enumerate(rows, start=2):
        student_no = row.get("student_no") or row.get("学号")
        if not student_no:
            invalid += 1
            issues.append(StudentImportIssue(row_number=index, student_no=None, reason="missing_student_no"))
            continue
        if student_no in seen_student_nos:
            invalid += 1
            issues.append(StudentImportIssue(row_number=index, student_no=student_no, reason="duplicate_student_no_in_file"))
            continue
        seen_student_nos.add(student_no)
        existing = db.scalar(select(User).where(User.student_no == student_no))
        if existing:
            skipped_existing += 1
            issues.append(StudentImportIssue(row_number=index, student_no=student_no, reason="student_already_exists"))
            continue
        raw_grade = row.get("grade") or row.get("年级") or ""
        try:
            grade = int(raw_grade) if raw_grade else None
        except ValueError:
            invalid += 1
            issues.append(StudentImportIssue(row_number=index, student_no=student_no, reason="invalid_grade"))
            continue
        class_name = row.get("class_name") or row.get("班级")
        classroom = _get_or_create_classroom(db, grade, class_name)
        password = row.get("password") or student_no[-6:]
        user = User(
            username=student_no,
            student_no=student_no,
            full_name=row.get("full_name") or row.get("姓名") or student_no,
            role=UserRole.STUDENT,
            password_hash=get_password_hash(password),
            grade=grade,
            classroom_id=classroom.id if classroom else None,
            must_change_password=True,
        )
        db.add(user)
        created += 1

    db.commit()
    audit_service.log(
        db,
        actor=current_user,
        action="import_students",
        target_type="batch",
        target_id=None,
        result="success",
        ip_address=request.client.host if request and request.client else None,
        detail={
            "created": created,
            "rows": len(rows),
            "skipped_existing": skipped_existing,
            "invalid": invalid,
        },
    )
    return StudentImportResult(
        rows=len(rows),
        created=created,
        skipped_existing=skipped_existing,
        invalid=invalid,
        issues=issues[:50],
    )


@router.get("/audit-logs", response_model=list[AuditLogRead])
def list_audit_logs(
    limit: int = 200,
    action: str | None = None,
    result: str | None = None,
    db: DbSession = None,
    current_user: CurrentAdmin = None,
) -> list[AuditLogRead]:
    query = select(AuditLog).options(selectinload(AuditLog.actor)).order_by(AuditLog.created_at.desc()).limit(limit)
    if action:
        query = query.where(AuditLog.action == action)
    if result:
        query = query.where(AuditLog.result == result)
    rows = db.scalars(query).all()
    return [AuditLogRead.model_validate(row) for row in rows]
