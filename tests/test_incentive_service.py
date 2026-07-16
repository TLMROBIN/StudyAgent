from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from threading import Barrier, Thread

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base, get_db
from backend.dependencies import get_current_user
from backend.models.agent_config import AgentConfig
from backend.models.conversation import Conversation, GuidanceStage, Message, MessageRole
from backend.models.incentive import StudentIncentiveEvent, StudentIncentiveProfile
from backend.models.user import Classroom, User, UserRole
from backend.routers import incentive as incentive_router
from backend.services.incentive_service import (
    IncentiveEventDraft,
    QualitySignalFilter,
    evaluate_resolve,
    evaluate_turn,
    extract_practice_verdict,
    rebuild_profile,
    record_events,
    reflections_are_similar,
    resolve_config,
)
from backend.time_utils import now_beijing


def build_session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)
    return factory


def build_file_session_factory(path: Path):
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)
    return factory


def create_user(factory, role: UserRole, name: str, classroom: Classroom | None = None) -> User:
    with factory() as db:
        user = User(
            username=f"{role.value}-{name}",
            full_name=name,
            role=role,
            password_hash="hash",
            grade=classroom.grade if classroom else None,
            classroom_id=classroom.id if classroom else None,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        db.expunge(user)
        return user


def build_client(factory, user: User) -> TestClient:
    app = FastAPI()
    app.include_router(incentive_router.router)

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def test_config_defaults_off_and_deep_merges_nested_points():
    params = resolve_config({"incentive": {"enabled": True, "points": {"followup_answered": 4}}})
    assert params["enabled"] is True
    assert params["points"]["followup_answered"] == 4
    assert params["points"]["early_resolved"] == 15
    assert resolve_config({})["enabled"] is False


def test_evaluate_turn_and_resolve_use_explicit_signals():
    params = resolve_config({"incentive": {"enabled": True}})
    turn = evaluate_turn(
        student_id=8,
        conversation_id=12,
        turn_index=2,
        subject="物理",
        followup_answered=True,
        practice_verdict="correct",
        first_learning_turn_today=True,
        params=params,
    )
    assert [item.event_type for item in turn] == ["followup_answered", "practice_passed", "daily_first_conversation"]
    assert turn[-1].dedup_key.startswith("student:8:")

    early = evaluate_resolve(
        conversation_id=12,
        subject="物理",
        user_turn_count=2,
        had_followup=True,
        had_fallback=False,
        reflection="我先分析受力方向，再利用平衡条件逐项检查未知量。",
        params=params,
    )
    assert {item.event_type for item in early} == {"early_resolved", "conversation_completed", "reflection_submitted"}
    fallback = evaluate_resolve(
        conversation_id=13,
        subject="物理",
        user_turn_count=4,
        had_followup=True,
        had_fallback=True,
        reflection=None,
        params=params,
    )
    assert "resolved_after_fallback" in {item.event_type for item in fallback}
    assert "early_resolved" not in {item.event_type for item in fallback}


def test_record_events_is_idempotent_caps_and_rolls_streak():
    factory = build_session_factory()
    student = create_user(factory, UserRole.STUDENT, "学生")
    params = resolve_config(
        {
            "incentive": {
                "enabled": True,
                "daily_total_cap": 3,
                "points": {"followup_answered": 2},
                "daily_event_caps": {"followup_answered": 1},
            }
        }
    )
    first_day = now_beijing()
    with factory() as db:
        first = record_events(
            db,
            student_id=student.id,
            drafts=[IncentiveEventDraft("followup_answered", 2, "dedup:1", payload={"valid_learning": True})],
            params=params,
            occurred_at=first_day,
        )
        db.commit()
        duplicate = record_events(
            db,
            student_id=student.id,
            drafts=[IncentiveEventDraft("followup_answered", 2, "dedup:1", payload={"valid_learning": True})],
            params=params,
            occurred_at=first_day,
        )
        capped = record_events(
            db,
            student_id=student.id,
            drafts=[IncentiveEventDraft("followup_answered", 2, "dedup:2", payload={"valid_learning": True})],
            params=params,
            occurred_at=first_day,
        )
        db.commit()
        profile = db.scalar(select(StudentIncentiveProfile).where(StudentIncentiveProfile.student_id == student.id))
        assert first.points_awarded == 2
        assert duplicate.points_awarded == 0
        assert capped.points_awarded == 0
        assert profile.total_points == 2
        assert profile.current_streak_days == 1
        assert db.query(StudentIncentiveEvent).count() == 2

    with factory() as db:
        next_day = record_events(
            db,
            student_id=student.id,
            drafts=[IncentiveEventDraft("practice_passed", 1, "dedup:3")],
            params=params,
            occurred_at=first_day + timedelta(days=1),
        )
        db.commit()
        profile = db.scalar(select(StudentIncentiveProfile).where(StudentIncentiveProfile.student_id == student.id))
        assert next_day.points_awarded == 1
        assert profile.current_streak_days == 2


def test_practice_verdict_is_conservative():
    assert extract_practice_verdict("【判定】正确") == "correct"
    assert extract_practice_verdict("【判定】部分正确") == "partial"
    assert extract_practice_verdict("看起来不错，继续想想") is None


def test_quality_signal_filter_strips_cross_chunk_marker():
    signal_filter = QualitySignalFilter()
    visible = "".join(
        signal_filter.feed(chunk)
        for chunk in ("先整理条件。\n[[sig:ans", "wer_quality=hi", "gh]]")
    ) + signal_filter.flush()
    assert visible == "先整理条件。\n"
    assert signal_filter.signal == "high"


def test_reflection_similarity_and_profile_rebuild():
    assert reflections_are_similar("我先整理条件，再检查公式。", "我先整理条件， 再检查公式！")
    factory = build_session_factory()
    student = create_user(factory, UserRole.STUDENT, "重建")
    params = resolve_config({"incentive": {"enabled": True}})
    with factory() as db:
        record_events(
            db,
            student_id=student.id,
            drafts=[IncentiveEventDraft("early_resolved", 15, "rebuild:1", subject="物理")],
            params=params,
        )
        db.commit()
        profile = db.scalar(select(StudentIncentiveProfile).where(StudentIncentiveProfile.student_id == student.id))
        profile.total_points = 9999
        profile.counters = {}
        db.commit()
        rebuilt = rebuild_profile(db, student.id, params)
        db.commit()
        assert rebuilt.total_points == 15
        assert rebuilt.counters["early_resolved"] == 1


def test_two_sessions_serialize_dedup_and_daily_cap(tmp_path):
    factory = build_file_session_factory(tmp_path / "incentive-concurrency.db")
    student = create_user(factory, UserRole.STUDENT, "并发")
    params = resolve_config({"incentive": {"enabled": True, "daily_total_cap": 2}})
    barrier = Barrier(2)
    results: list[int] = []
    failures: list[Exception] = []

    def worker(dedup_key: str) -> None:
        try:
            with factory() as db:
                barrier.wait()
                grant = record_events(
                    db,
                    student_id=student.id,
                    drafts=[IncentiveEventDraft("followup_answered", 2, dedup_key)],
                    params=params,
                )
                db.commit()
                results.append(grant.points_awarded)
        except Exception as exc:  # pragma: no cover - assertion reports the concrete exception
            failures.append(exc)

    threads = [Thread(target=worker, args=(key,)) for key in ("concurrent:1", "concurrent:2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert sorted(results) == [0, 2]
    with factory() as db:
        profile = db.scalar(select(StudentIncentiveProfile).where(StudentIncentiveProfile.student_id == student.id))
        assert profile.total_points == 2
        assert db.query(StudentIncentiveEvent).count() == 2


def test_runtime_schema_path_creates_incentive_tables(monkeypatch):
    from backend import database

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE conversations (id INTEGER PRIMARY KEY)"))
    monkeypatch.setattr(database, "engine", engine)
    database.apply_runtime_schema_updates()
    inspector = inspect(engine)
    assert inspector.has_table("student_incentive_events")
    assert inspector.has_table("student_incentive_profiles")
    assert "ix_student_incentive_events_student_created" in {
        item["name"] for item in inspector.get_indexes("student_incentive_events")
    }


def test_teacher_scope_reflections_and_praise_roundtrip():
    factory = build_session_factory()
    with factory() as db:
        own_class = Classroom(grade=1, name="1班")
        other_class = Classroom(grade=1, name="2班")
        db.add_all([own_class, other_class])
        db.commit()
        teacher = User(username="teacher", full_name="老师", role=UserRole.TEACHER, password_hash="hash")
        teacher.teacher_classrooms.append(own_class)
        db.add(teacher)
        db.commit()
        for item in (teacher, own_class, other_class):
            db.refresh(item)
            db.expunge(item)
    own_student = create_user(factory, UserRole.STUDENT, "甲", own_class)
    other_student = create_user(factory, UserRole.STUDENT, "乙", other_class)
    with factory() as db:
        db.add(
            AgentConfig(
                version=1,
                system_prompt="test",
                guidance_params={"incentive": {"enabled": True}},
                subject_prompts={},
                filter_rules={},
                is_active=True,
            )
        )
        db.add(
            StudentIncentiveEvent(
                student_id=own_student.id,
                subject="数学",
                event_type="reflection_submitted",
                points=5,
                payload={"reflection": "我先整理已知条件，再检查每一步使用的公式是否匹配。"},
                dedup_key="reflection:1",
            )
        )
        db.commit()
    client = build_client(factory, teacher)
    reflections = client.get(f"/api/incentive/teacher/students/{own_student.id}/reflections")
    assert reflections.status_code == 200
    assert reflections.json()["items"][0]["reflection"].startswith("我先整理")
    assert client.get(f"/api/incentive/teacher/students/{other_student.id}/reflections").status_code == 403
    praised = client.post(
        "/api/incentive/teacher/praise",
        json={"student_id": own_student.id, "content": "你能主动检查条件，推理过程很认真。"},
    )
    assert praised.status_code == 201
    assert praised.json()["points"] == 10
