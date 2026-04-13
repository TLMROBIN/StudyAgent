from backend.services.request_replay_service import RequestReplayService
from backend.services.store_service import MemoryStore


def test_request_replay_fingerprint_includes_image_hash():
    service = RequestReplayService(store_backend=MemoryStore())

    first = service.fingerprint(subject="数学", question="[图片提问]", conversation_id=None, image_sha256="a")
    second = service.fingerprint(subject="数学", question="[图片提问]", conversation_id=None, image_sha256="b")
    same = service.fingerprint(subject="数学", question="[图片提问]", conversation_id=None, image_sha256="a")

    assert first != second
    assert first == same
