from backend.services.request_replay_service import RequestReplayService
from backend.services.store_service import MemoryStore


def test_request_replay_fingerprint_includes_image_hash():
    service = RequestReplayService(store_backend=MemoryStore())

    first = service.fingerprint(subject="数学", question="[图片提问]", conversation_id=None, image_sha256="a")
    second = service.fingerprint(subject="数学", question="[图片提问]", conversation_id=None, image_sha256="b")
    same = service.fingerprint(subject="数学", question="[图片提问]", conversation_id=None, image_sha256="a")

    assert first != second
    assert first == same


def test_request_replay_fingerprint_and_state_include_role_snapshot():
    service = RequestReplayService(store_backend=MemoryStore())
    without_role = service.fingerprint(subject="数学", question="函数怎么判断", conversation_id=None)
    with_role = service.fingerprint(subject="数学", question="函数怎么判断", conversation_id=None, requested_role_id=7)
    snapshot = {"requested_role_id": 7, "applied": True, "status": "applied", "revision_id": 11}

    assert without_role != with_role
    service.remember_request(
        user_id=3,
        request_id="req-role",
        question_hash=with_role,
        conversation_id=9,
        turn_index=1,
        subject="数学",
        role_snapshot=snapshot,
    )
    restored = service.load(user_id=3, request_id="req-role")
    assert restored is not None
    assert restored.role_snapshot == snapshot
