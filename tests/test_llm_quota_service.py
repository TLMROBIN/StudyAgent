from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base
from backend.models import agent_config, audit_log, conversation, knowledge, user  # noqa: F401
from backend.models.llm_account import AccountBillingType, LLMProviderAccount
from backend.models.llm_model import LLMModelConfig, LLMQuotaPolicy, QuotaBillingMode
from backend.models.llm_usage import LLMUsageEvent
from backend.models.user import User, UserRole
from backend.services.llm_quota_service import LLMQuotaService, QuotaDenied
from backend.services.store_service import MemoryStore, QuotaCounterKey


def _build_session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)
    return TestingSessionLocal


def _create_student(session: Session) -> User:
    student = User(
        username="student1",
        student_no="20260001",
        full_name="学生",
        role=UserRole.STUDENT,
        password_hash="fake-hash",
        must_change_password=False,
        is_active=True,
    )
    session.add(student)
    session.commit()
    session.refresh(student)
    return student


def _create_model(session: Session, *, billing_mode: QuotaBillingMode) -> tuple[LLMModelConfig, LLMQuotaPolicy]:
    account = LLMProviderAccount(
        provider_name="minimax" if billing_mode == QuotaBillingMode.REQUEST_COUNT else "deepseek",
        display_name="Provider",
        base_url="https://api.example.test/v1",
        api_key="secret",
        account_billing_type=(
            AccountBillingType.TOKEN_PLAN
            if billing_mode == QuotaBillingMode.REQUEST_COUNT
            else AccountBillingType.PAY_AS_YOU_GO
        ),
    )
    session.add(account)
    session.flush()
    model = LLMModelConfig(
        model_key="minimax-m27" if billing_mode == QuotaBillingMode.REQUEST_COUNT else "deepseek-chat",
        display_name="Model",
        description="",
        provider_account_id=account.id,
        provider_model="upstream-model",
        is_primary=True,
    )
    session.add(model)
    session.flush()
    policy = LLMQuotaPolicy(
        model_config_id=model.id,
        billing_mode=billing_mode,
        user_daily_request_limit=2 if billing_mode == QuotaBillingMode.REQUEST_COUNT else None,
        provider_rolling_5h_request_limit=3 if billing_mode == QuotaBillingMode.REQUEST_COUNT else None,
        user_daily_token_limit=100 if billing_mode == QuotaBillingMode.TOKEN_USAGE else None,
        max_completion_tokens=50 if billing_mode == QuotaBillingMode.TOKEN_USAGE else None,
    )
    session.add(policy)
    session.commit()
    session.refresh(model)
    session.refresh(policy)
    return model, policy


def test_memory_store_reserves_rejects_releases_and_reconciles_quota():
    store = MemoryStore()
    key = QuotaCounterKey(key="quota:req:user:1:model:minimax-m27:day:2026-05-23", limit=2)

    first = store.reserve_quota([key], "reservation-1", amount=1, ttl_seconds=3600)
    second = store.reserve_quota([key], "reservation-2", amount=1, ttl_seconds=3600)
    rejected = store.reserve_quota([key], "reservation-3", amount=1, ttl_seconds=3600)

    assert first.allowed is True
    assert second.allowed is True
    assert rejected.allowed is False
    assert rejected.exceeded_key == key.key

    store.release_quota("reservation-2")
    after_release = store.reserve_quota([key], "reservation-4", amount=1, ttl_seconds=3600)
    assert after_release.allowed is True

    token_key = QuotaCounterKey(key="quota:tok:user:1:model:deepseek-chat:day:2026-05-23", limit=100)
    reserved = store.reserve_quota([token_key], "reservation-token", amount=80, ttl_seconds=3600)
    assert reserved.allowed is True
    reconciled = store.reconcile_quota("reservation-token", actual_amount=30)
    assert reconciled.allowed is True
    snapshot = store.quota_snapshot([token_key])[0]
    assert snapshot.used == 30
    assert snapshot.remaining == 70


def test_llm_quota_service_reserves_request_count_and_rejects_when_exhausted():
    session_factory = _build_session_factory()
    session = session_factory()
    try:
        student = _create_student(session)
        model, _policy = _create_model(session, billing_mode=QuotaBillingMode.REQUEST_COUNT)
        service = LLMQuotaService(store=MemoryStore())

        first = service.check_and_reserve(
            db=session,
            user=student,
            model_config=model,
            request_id="request-1",
            prompt_messages=[{"role": "user", "content": "函数怎么分析"}],
        )
        second = service.check_and_reserve(
            db=session,
            user=student,
            model_config=model,
            request_id="request-2",
            prompt_messages=[{"role": "user", "content": "函数怎么分析"}],
        )
        denied = service.check_and_reserve(
            db=session,
            user=student,
            model_config=model,
            request_id="request-3",
            prompt_messages=[{"role": "user", "content": "函数怎么分析"}],
        )

        assert first.allowed is True
        assert second.allowed is True
        assert isinstance(denied, QuotaDenied)
        assert denied.reason == "user_daily_limit"
    finally:
        session.close()


def test_llm_quota_service_reserves_tokens_and_reconciles_usage_event():
    session_factory = _build_session_factory()
    session = session_factory()
    try:
        student = _create_student(session)
        model, _policy = _create_model(session, billing_mode=QuotaBillingMode.TOKEN_USAGE)
        service = LLMQuotaService(store=MemoryStore())

        reservation = service.check_and_reserve(
            db=session,
            user=student,
            model_config=model,
            request_id="request-token",
            prompt_messages=[{"role": "user", "content": "abc"}],
        )

        assert reservation.allowed is True
        assert reservation.reserved_amount >= 50

        service.reconcile(
            db=session,
            reservation=reservation,
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
            source="provider_usage",
            estimated=False,
        )

        snapshot = service.quota_snapshot_for_user(db=session, user=student, model_config=model)
        assert snapshot.remaining_tokens == 70
        assert snapshot.quota_exhausted is False
        assert model.usage_events[0].total_tokens == 30
    finally:
        session.close()


def test_llm_quota_service_reuses_same_request_id_reservation():
    session_factory = _build_session_factory()
    session = session_factory()
    try:
        student = _create_student(session)
        model, _policy = _create_model(session, billing_mode=QuotaBillingMode.REQUEST_COUNT)
        service = LLMQuotaService(store=MemoryStore())

        first = service.check_and_reserve(
            db=session,
            user=student,
            model_config=model,
            request_id="same-request",
            prompt_messages=[],
        )
        second = service.check_and_reserve(
            db=session,
            user=student,
            model_config=model,
            request_id="same-request",
            prompt_messages=[],
        )

        assert first.allowed is True
        assert second.allowed is True
        assert first.reservation_key == second.reservation_key
        snapshot = service.quota_snapshot_for_user(db=session, user=student, model_config=model)
        assert snapshot.remaining_requests == 1
    finally:
        session.close()


def test_llm_quota_service_reconcile_is_idempotent_for_same_reservation_key():
    session_factory = _build_session_factory()
    session = session_factory()
    try:
        student = _create_student(session)
        model, _policy = _create_model(session, billing_mode=QuotaBillingMode.REQUEST_COUNT)
        service = LLMQuotaService(store=MemoryStore())

        reservation = service.check_and_reserve(
            db=session,
            user=student,
            model_config=model,
            request_id="same-usage-request",
            prompt_messages=[],
        )

        assert reservation.allowed is True
        first_event = service.reconcile(
            db=session,
            reservation=reservation,
            source="request_count",
            estimated=False,
        )
        second_event = service.reconcile(
            db=session,
            reservation=reservation,
            source="request_count",
            estimated=False,
        )

        events = session.query(LLMUsageEvent).all()
        assert first_event.id == second_event.id
        assert len(events) == 1
    finally:
        session.close()
