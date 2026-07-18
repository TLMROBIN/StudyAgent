"""端到端业务场景集成测试（跨 router 的完整业务链路）。

四条链路（按文件内顺序执行，共享 module 级 fixture 的内存数据库与向量库）：

1. 学生链路   登录 → SSE 流式提问 → 查历史 → 同会话追问（触发安全改写）
2. 教师链路   登录 → 上传 txt 到知识库 → 导入任务（无 Celery，BackgroundTasks 同步兜底）
              → 轮询任务到完成 → 学生提问 RAG 命中该文档
3. 管理链路   登录 → 统计概览 → 审计日志（教师链路的 knowledge_upload）
              → 会话归档（学生链路的会话）→ filter-rules/status
4. 负向链路   学生用 adversarial_cases.json 的越界问题提问 → 三层过滤拦截
              → 拒答落库 + filter_blocked_total 指标 + 统计可见

约定复用自既有测试：LLM 全程 mock（确定性流式输出）、EMBEDDING_BACKEND=hash、
内存 SQLite（StaticPool）、TestClient；Redis 相关 store 使用 MemoryStore 回退。
与既有模块级测试不同的是：这里不 override get_current_user，走真实 JWT 登录链路。
"""

from __future__ import annotations

import json
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.config import Settings
from backend.database import Base, get_db
from backend.models import (  # noqa: F401  (确保建表覆盖全部模型)
    agent_config,
    audit_log,
    conversation,
    feedback,
    knowledge,
    learning_profile,
    llm_provider,
    notification,
    user,
)
from backend.models.conversation import GuidanceStage, Message, MessageRole
from backend.models.user import User, UserRole
from backend.routers import admin as admin_router
from backend.routers import auth as auth_router
from backend.routers import chat as chat_router
from backend.routers import knowledge as knowledge_router
from backend.routers import stats as stats_router
from backend.security import get_password_hash
from backend.services.auth_service import auth_service
from backend.services.embed_service import EmbedService
from backend.services.filter_service import filter_service
from backend.services.metrics_service import filter_blocked_total
from backend.services.rag_service import RagService
from backend.services.socratic_service import socratic_service
from backend.services.store_service import MemoryStore
from backend.services.vector_store_service import VectorStoreService
from backend.tasks import ingest as ingest_module

PASSWORD = "E2e@Passw0rd2026"
ADVERSARIAL_CASES_PATH = Path(__file__).parent / "adversarial_cases.json"


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _step(step: str, condition: bool, detail: object = "") -> None:
    """链路断点断言：失败信息直接指出链路断在哪一步。"""
    assert condition, f"[链路断点] {step} | 现场: {detail!r}"


def _parse_sse(payload: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for frame in payload.strip().split("\n\n"):
        if not frame.strip():
            continue
        event_name = ""
        data: dict = {}
        for line in frame.splitlines():
            if line.startswith("event:"):
                event_name = line.replace("event:", "", 1).strip()
            if line.startswith("data:"):
                data = json.loads(line.replace("data:", "", 1).strip())
        if event_name:
            events.append((event_name, data))
    return events


def _event_payload(events: list[tuple[str, dict]], event_name: str) -> dict:
    return next(data for name, data in events if name == event_name)


class ScriptedLLM:
    """确定性流式 LLM mock：按脚本队列吐 chunk，可禁止调用。"""

    def __init__(self) -> None:
        self.scripts: deque[list[str]] = deque()
        self.call_count = 0
        self.forbid_calls = False

    def push(self, chunks: list[str]) -> None:
        self.scripts.append(list(chunks))

    async def stream_response(self, messages, fallback_text, *, model_key=None, **kwargs):
        assert not self.forbid_calls, "本步骤不应触发 LLM 调用（应被过滤层拦截）"
        self.call_count += 1
        chunks = self.scripts.popleft() if self.scripts else ["先别急着要结论，你觉得第一步该确认什么？"]
        for chunk in chunks:
            yield chunk


def _login(env, username: str, step: str) -> dict[str, str]:
    """本地密码登录端点已停用（403），链路测试改为直接签发 JWT，继续走真实鉴权链路。"""
    session = env.session_factory()
    try:
        user = session.scalar(select(User).where(User.username == username))
    finally:
        session.close()
    _step(f"{step}·签发令牌 {username}", user is not None, username)
    tokens = auth_service.issue_token_pair(user)
    token = tokens.get("access_token")
    _step(f"{step}·签发返回 access_token", bool(token), list(tokens))
    return {"Authorization": f"Bearer {token}"}


def _seed_user(session_factory, *, username: str, role: UserRole, grade: int | None = None, password_hash: str) -> None:
    session = session_factory()
    try:
        session.add(
            User(
                username=username,
                student_no=f"2026{username[-1]}001" if role == UserRole.STUDENT else None,
                full_name=f"E2E{role.value}",
                role=role,
                password_hash=password_hash,
                must_change_password=False,
                is_active=True,
                grade=grade,
            )
        )
        session.commit()
    finally:
        session.close()


# ---------------------------------------------------------------------------
# module 级共享环境：四条链路串在同一套 app/DB/向量库上
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    tmp_path = tmp_path_factory.mktemp("e2e-scenarios")
    for name in ("uploads", "backups", "tasks", "chromadb"):
        (tmp_path / name).mkdir()

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)

    rag_settings = Settings(
        CHROMADB_MODE="persistent",
        CHROMADB_PATH=str(tmp_path / "chromadb"),
        CHROMADB_COLLECTION_PREFIX="studyagent-e2e-test",
        TASK_ARTIFACT_PATH=str(tmp_path / "tasks"),
        UPLOAD_PATH=str(tmp_path / "uploads"),
        EMBEDDING_MODEL_NAME="BAAI/bge-m3",
        EMBEDDING_BACKEND="hash",
        EMBEDDING_DEVICE="cpu",
        EMBEDDING_FALLBACK_TO_HASH=True,
    )
    embedder = EmbedService(rag_settings)
    rag = RagService(settings=rag_settings, embedder=embedder, vector_store=VectorStoreService(rag_settings, embedder))
    llm = ScriptedLLM()

    # chat 链路：LLM/RAG/各类 store 全部换成确定性实现
    mp.setattr(chat_router, "rag_service", rag)
    mp.setattr(chat_router, "SessionLocal", session_factory)
    mp.setattr(chat_router.llm_service, "stream_response", llm.stream_response)
    mp.setattr(chat_router.question_cache_service, "is_cacheable", lambda **kwargs: False)
    mp.setattr(chat_router.question_cache_service, "store_backend", MemoryStore())
    mp.setattr(chat_router.request_replay_service, "store_backend", MemoryStore())
    mp.setattr(chat_router.llm_quota_service, "store", MemoryStore())
    mp.setattr(chat_router, "rag_session_store", MemoryStore())

    async def deterministic_suggested_replies(**kwargs):
        return ["我先说一个关键词，你帮我判断方向对不对。"]

    mp.setattr(chat_router.suggested_reply_service, "generate", deterministic_suggested_replies)

    # 知识库导入链路：无 Celery broker → enqueue 返回 None，走 BackgroundTasks 同步兜底
    mp.setattr(knowledge_router, "rag_service", rag)
    mp.setattr(ingest_module, "rag_service", rag)
    mp.setattr(ingest_module, "SessionLocal", session_factory)
    mp.setattr(ingest_module, "enqueue_ingest_task", lambda document_id, task_id: None)

    # 共享 settings 单例（knowledge/ingest 同一实例）：文件目录指向临时目录
    mp.setattr(knowledge_router.settings, "upload_path", str(tmp_path / "uploads"), raising=False)
    mp.setattr(knowledge_router.settings, "document_backup_path", str(tmp_path / "backups"), raising=False)
    mp.setattr(knowledge_router.settings, "task_artifact_path", str(tmp_path / "tasks"), raising=False)

    app = FastAPI()
    for router_module in (auth_router, chat_router, knowledge_router, stats_router, admin_router):
        app.include_router(router_module.router)

    def override_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_db

    password_hash = get_password_hash(PASSWORD)
    _seed_user(session_factory, username="e2e_student1", role=UserRole.STUDENT, grade=2, password_hash=password_hash)
    _seed_user(session_factory, username="e2e_teacher1", role=UserRole.TEACHER, password_hash=password_hash)
    _seed_user(session_factory, username="e2e_admin1", role=UserRole.ADMIN, password_hash=password_hash)

    client = TestClient(app)
    try:
        yield SimpleNamespace(
            client=client,
            session_factory=session_factory,
            llm=llm,
            rag=rag,
            tmp_path=tmp_path,
            state={},
        )
    finally:
        client.close()
        mp.undo()


# ---------------------------------------------------------------------------
# 链路 0：本地账号密码登录端点已停用（仅保留统一认证登录）
# ---------------------------------------------------------------------------


def test_e2e_password_login_endpoints_disabled(env):
    client = env.client
    for path in ("/api/auth/student/login", "/api/auth/staff/login"):
        response = client.post(path, json={"username": "e2e_student1", "password": PASSWORD})
        _step(f"本地登录端点停用 {path}", response.status_code == 403, f"{response.status_code} {response.text}")
        _step(
            f"本地登录端点提示统一认证 {path}",
            "统一认证" in response.json().get("detail", ""),
            response.json(),
        )


# ---------------------------------------------------------------------------
# 链路 1：学生 登录 → SSE 提问 → 查历史 → 同会话追问
# ---------------------------------------------------------------------------


def test_e2e_student_chain_login_ask_history_followup(env):
    client = env.client
    headers = _login(env, "e2e_student1", "学生链路")
    env.state["student_headers"] = headers

    # -- 步骤 2：首次提问，逐事件解析 SSE --
    scripted_chunks = ["我们先不给结论。", "判断单调性之前，你觉得第一步要确认什么？", "提示：从定义域入手想一想。"]
    env.llm.push(scripted_chunks)
    response = client.post(
        "/api/chat/stream",
        json={"subject": "数学", "message": "函数单调性第一步怎么想？"},
        headers=headers,
    )
    _step("学生链路·首次提问 HTTP", response.status_code == 200, f"{response.status_code} {response.text[:300]}")
    _step(
        "学生链路·SSE Content-Type",
        response.headers["content-type"].startswith("text/event-stream"),
        response.headers.get("content-type"),
    )

    events = _parse_sse(response.text)
    names = [name for name, _ in events]
    _step(
        "学生链路·SSE 事件序列",
        names == ["meta", "chunk", "chunk", "chunk", "done", "suggested_replies"],
        names,
    )

    meta = events[0][1]
    _step(
        "学生链路·meta 事件字段",
        {"conversation_id", "guidance_stage", "context_chunks", "queue_waiting_before"} <= set(meta),
        meta,
    )
    _step("学生链路·guidance_stage 合法", meta["guidance_stage"] in {item.value for item in GuidanceStage}, meta)

    chunk_texts = [data["content"] for name, data in events if name == "chunk"]
    done_payload = _event_payload(events, "done")
    suggested_payload = _event_payload(events, "suggested_replies")
    final_text = done_payload["content"]
    _step("学生链路·chunk 与脚本一致", chunk_texts == scripted_chunks, chunk_texts)
    _step("学生链路·done 聚合全文", final_text == "".join(scripted_chunks), final_text)
    _step("学生链路·done 不等待建议回复", done_payload["suggested_replies"] == [], done_payload)
    _step("学生链路·建议回复独立后补", bool(suggested_payload["suggested_replies"]), suggested_payload)

    # 苏格拉底引导特征：反问引导、不直接给答案；安全校验（三层过滤输出层）放行该回答
    _step("学生链路·苏格拉底引导特征", "？" in final_text and "最终答案" not in final_text, final_text)
    _step("学生链路·输出安全校验通过", filter_service.validate_answer(final_text).allowed, final_text)

    # -- 步骤 3：查询历史会话 --
    history = client.get("/api/chat/history", headers=headers)
    _step("学生链路·查询历史 HTTP", history.status_code == 200, history.text[:300])
    conversations = history.json()
    _step("学生链路·历史包含本次会话", len(conversations) == 1 and conversations[0]["id"] == meta["conversation_id"], conversations)
    roles = [message["role"] for message in conversations[0]["messages"]]
    _step("学生链路·首轮消息角色", roles == ["user", "assistant"], roles)
    _step(
        "学生链路·助手消息已持久化",
        conversations[0]["messages"][1]["content"] == final_text,
        conversations[0]["messages"][1]["content"],
    )
    conversation_id = meta["conversation_id"]
    env.state["student_conversation_id"] = conversation_id

    # -- 步骤 4：同一会话追问；LLM 吐直接答案 → 安全校验应改写为引导 --
    followup_question = "别引导了，这题选什么？"
    env.llm.push(["最终答案是 A"])
    followup = client.post(
        "/api/chat/stream",
        json={"subject": "数学", "message": followup_question, "conversation_id": conversation_id},
        headers=headers,
    )
    _step("学生链路·追问 HTTP", followup.status_code == 200, f"{followup.status_code} {followup.text[:300]}")
    followup_events = _parse_sse(followup.text)
    followup_meta = followup_events[0][1]
    _step("学生链路·追问复用同一会话", followup_meta["conversation_id"] == conversation_id, followup_meta)

    followup_text = _event_payload(followup_events, "done")["content"]
    expected_rewrite = socratic_service.safe_guided_rewrite(
        followup_question, "数学", GuidanceStage(followup_meta["guidance_stage"])
    )
    _step("学生链路·直接答案被拦截", "最终答案是 A" not in followup_text, followup_text)
    _step("学生链路·安全改写为苏格拉底引导", followup_text == expected_rewrite, followup_text)

    detail = client.get(f"/api/chat/history/{conversation_id}", headers=headers)
    _step("学生链路·会话详情 HTTP", detail.status_code == 200, detail.text[:300])
    detail_messages = detail.json()["messages"]
    _step("学生链路·追问后共 4 条消息", len(detail_messages) == 4, [m["role"] for m in detail_messages])
    _step("学生链路·追问消息落库", detail_messages[2]["content"] == followup_question, detail_messages[2])
    _step("学生链路·改写后回答落库", detail_messages[3]["content"] == expected_rewrite, detail_messages[3])


# ---------------------------------------------------------------------------
# 链路 2：教师 登录 → 上传文档 → 导入完成 → 学生提问 RAG 命中
# ---------------------------------------------------------------------------


def test_e2e_teacher_chain_upload_ingest_and_rag_hit(env):
    client = env.client
    headers = _login(env, "e2e_teacher1", "教师链路")
    env.state["teacher_headers"] = headers

    # -- 步骤 2：上传一份小的 txt 文档 --
    lecture_text = (
        "回旋加速器利用垂直于粒子速度的匀强磁场使带电粒子做圆周运动，"
        "并在两个 D 形盒之间的缝隙处用交变电场对粒子反复加速。"
        "由于粒子回旋周期与速度无关，交变电场的频率保持不变即可持续加速。"
    )
    upload = client.post(
        "/api/knowledge/upload",
        params={"subject": "物理"},
        data={"resource_type": "knowledge_note", "grade": "2", "tags": "回旋加速器"},
        files={"file": ("回旋加速器讲义.txt", lecture_text.encode("utf-8"), "text/plain")},
        headers=headers,
    )
    _step("教师链路·上传文档 HTTP 202", upload.status_code == 202, f"{upload.status_code} {upload.text[:300]}")
    task_payload = upload.json()
    task_id = task_payload["id"]
    document_id = task_payload["document_id"]
    env.state["knowledge_document_id"] = document_id

    # -- 步骤 3：导入已由 BackgroundTasks 同步执行（Celery 不可用时的本地兜底），轮询任务状态 --
    task_detail: dict = {}
    for _ in range(20):
        task_response = client.get(f"/api/knowledge/tasks/{task_id}", headers=headers)
        _step("教师链路·查询任务 HTTP", task_response.status_code == 200, task_response.text[:300])
        task_detail = task_response.json()
        if task_detail["status"] in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.2)
    _step(
        "教师链路·导入任务完成",
        task_detail.get("status") == "completed" and task_detail.get("progress") == 100,
        task_detail,
    )
    _step("教师链路·完成信息含切片统计", "导入完成" in str(task_detail.get("error_message")), task_detail)

    document = client.get(f"/api/knowledge/documents/{document_id}", headers=headers)
    _step("教师链路·查询文档 HTTP", document.status_code == 200, document.text[:300])
    document_payload = document.json()
    _step(
        "教师链路·文档已完成且有切片",
        document_payload["status"] == "completed" and document_payload["chunk_total"] >= 1,
        document_payload,
    )

    # -- 步骤 4：学生提问，RAG 检索命中该文档（meta.context_chunks 出现在 SSE 响应中） --
    student_headers = env.state.get("student_headers") or _login(
        env, "e2e_student1", "教师链路"
    )
    env.llm.push(["先想一想：磁场在回旋加速器里起什么作用？粒子的周期和速度有关吗？"])
    response = client.post(
        "/api/chat/stream",
        json={"subject": "物理", "message": "回旋加速器为什么能反复加速带电粒子？"},
        headers=student_headers,
    )
    _step("教师链路·学生提问 HTTP", response.status_code == 200, f"{response.status_code} {response.text[:300]}")
    events = _parse_sse(response.text)
    meta = events[0][1]
    _step("教师链路·RAG 元数据出现在响应 meta 中", meta.get("context_chunks", 0) >= 1, meta)
    done_payload = _event_payload(events, "done")
    _step("教师链路·回答正常收尾", bool(done_payload["content"]), done_payload)
    _step(
        "教师链路·建议回复在 done 后补发",
        [name for name, _ in events][-2:] == ["done", "suggested_replies"],
        events[-2:],
    )

    # 服务层复核：检索命中的正是刚导入的文档
    session = env.session_factory()
    try:
        retrieval = env.rag.retrieve(session, "物理", "回旋加速器为什么能反复加速带电粒子？", student_grade=2)
        _step(
            "教师链路·检索命中新导入文档",
            bool(retrieval.chunks) and retrieval.chunks[0].document_id == document_id,
            [(chunk.document_id, chunk.content[:20]) for chunk in retrieval.chunks],
        )
        _step("教师链路·检索上下文含原文关键内容", "回旋" in retrieval.context, retrieval.context[:120])
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 链路 3：管理员 登录 → 统计 → 审计日志 → 会话归档 → filter-rules/status
# ---------------------------------------------------------------------------


def test_e2e_admin_chain_stats_audit_and_filter_rules(env):
    client = env.client
    headers = _login(env, "e2e_admin1", "管理链路")
    env.state["admin_headers"] = headers

    # -- 步骤 2：统计概览（应能看到前两条链路的会话） --
    overview = client.get("/api/stats/overview", headers=headers)
    _step("管理链路·统计概览 HTTP", overview.status_code == 200, overview.text[:300])
    overview_payload = overview.json()
    subjects = {row["subject"] for row in overview_payload["by_subject"]}
    _step("管理链路·统计覆盖两条链路会话", overview_payload["total_questions"] >= 2, overview_payload)
    _step("管理链路·学科分布含数学与物理", {"数学", "物理"} <= subjects, subjects)

    # -- 步骤 3：审计日志（教师链路上传动作应已记录；聊天链路无审计点，用会话归档佐证） --
    audit = client.get("/api/admin/audit-logs", headers=headers)
    _step("管理链路·审计日志 HTTP", audit.status_code == 200, audit.text[:300])
    audit_rows = audit.json()
    upload_logs = [row for row in audit_rows if row["action"] == "knowledge_upload"]
    _step("管理链路·存在教师上传审计记录", len(upload_logs) == 1, sorted({row["action"] for row in audit_rows}))
    _step(
        "管理链路·上传审计指向教师链路文档",
        upload_logs[0]["target_id"] == str(env.state.get("knowledge_document_id"))
        and upload_logs[0]["target_type"] == "knowledge_document"
        and upload_logs[0]["result"] == "accepted"
        and upload_logs[0]["detail"].get("filename") == "回旋加速器讲义.txt",
        upload_logs[0],
    )

    archive = client.get("/api/admin/conversation-archive", params={"subject": "数学"}, headers=headers)
    _step("管理链路·会话归档 HTTP", archive.status_code == 200, archive.text[:300])
    archive_ids = [item["id"] for item in archive.json()["items"]]
    _step(
        "管理链路·归档可见学生链路会话",
        env.state.get("student_conversation_id") in archive_ids,
        archive_ids,
    )

    # -- 步骤 4：filter-rules/status --
    rules = client.get("/api/admin/filter-rules/status", headers=headers)
    _step("管理链路·filter-rules/status HTTP", rules.status_code == 200, rules.text[:300])
    rules_payload = rules.json()
    _step(
        "管理链路·过滤规则引擎状态字段完整",
        {"source", "enabled_rules", "disabled_rule_ids", "config_path"} <= set(rules_payload),
        rules_payload,
    )
    _step("管理链路·过滤规则已启用", int(rules_payload["enabled_rules"]) > 0, rules_payload)

    # 越权保护：学生访问管理端点应被拒绝
    student_headers = env.state.get("student_headers")
    if student_headers:
        forbidden = client.get("/api/admin/audit-logs", headers=student_headers)
        _step("管理链路·学生访问审计日志被拒", forbidden.status_code == 403, forbidden.status_code)


# ---------------------------------------------------------------------------
# 链路 4（负向）：越界问题被三层过滤拦截，拒答落库且指标/统计留痕
# ---------------------------------------------------------------------------


def test_e2e_adversarial_question_blocked_and_recorded(env):
    client = env.client
    cases = json.loads(ADVERSARIAL_CASES_PATH.read_text(encoding="utf-8"))
    blocked_case = next(case for case in cases if not case["allowed"] and "系统提示词" in case["text"])

    headers = env.state.get("student_headers") or _login(
        env, "e2e_student1", "负向链路"
    )
    admin_headers = env.state.get("admin_headers") or _login(
        env, "e2e_admin1", "负向链路"
    )

    questions_before = client.get("/api/stats/overview", headers=admin_headers).json()["total_questions"]
    blocked_before = filter_blocked_total._value.get()

    # -- 越界提问：不允许触发 LLM 调用 --
    env.llm.forbid_calls = True
    try:
        response = client.post(
            "/api/chat/stream",
            json={"subject": "数学", "message": blocked_case["text"]},
            headers=headers,
        )
    finally:
        env.llm.forbid_calls = False

    _step("负向链路·提问 HTTP", response.status_code == 200, f"{response.status_code} {response.text[:300]}")
    events = _parse_sse(response.text)
    names = [name for name, _ in events]
    _step("负向链路·拦截后仅 meta+done（无 chunk）", names == ["meta", "done"], names)
    _step("负向链路·返回标准拒答文案", events[-1][1]["content"] == filter_service.refusal_text, events[-1][1])

    # -- 记录机制 1：Prometheus 拦截计数 --
    blocked_after = filter_blocked_total._value.get()
    _step("负向链路·filter_blocked_total 指标 +1", blocked_after == blocked_before + 1, (blocked_before, blocked_after))

    # -- 记录机制 2：拒答作为 assistant 消息落库，可被管理端回溯 --
    session = env.session_factory()
    try:
        blocked_user_message = session.scalar(
            select(Message).where(Message.content == blocked_case["text"], Message.role == MessageRole.USER)
        )
        _step("负向链路·越界提问已落库", blocked_user_message is not None, blocked_case["text"])
        refusal_message = session.scalar(
            select(Message).where(
                Message.conversation_id == blocked_user_message.conversation_id,
                Message.turn_index == blocked_user_message.turn_index,
                Message.role == MessageRole.ASSISTANT,
            )
        )
        _step(
            "负向链路·拒答回复已落库",
            refusal_message is not None and refusal_message.content == filter_service.refusal_text,
            getattr(refusal_message, "content", None),
        )
        blocked_conversation_id = blocked_user_message.conversation_id
    finally:
        session.close()

    # -- 记录机制 3：管理端统计与会话归档可见该次拦截 --
    questions_after = client.get("/api/stats/overview", headers=admin_headers).json()["total_questions"]
    _step("负向链路·统计新增一次提问", questions_after == questions_before + 1, (questions_before, questions_after))

    archive = client.get(
        "/api/admin/conversation-archive", params={"subject": "数学"}, headers=admin_headers
    )
    _step("负向链路·归档 HTTP", archive.status_code == 200, archive.text[:300])
    archived = {item["id"]: item for item in archive.json()["items"]}
    _step("负向链路·归档可见被拦截会话", blocked_conversation_id in archived, list(archived))
    archived_contents = [message["content"] for message in archived[blocked_conversation_id]["messages"]]
    _step(
        "负向链路·归档保留提问与拒答原文",
        blocked_case["text"] in archived_contents and filter_service.refusal_text in archived_contents,
        archived_contents,
    )
