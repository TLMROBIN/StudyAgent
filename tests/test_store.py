from backend.config import Settings
from backend.services.store_service import MemoryStore, build_store


def test_memory_store_ttl_and_sets():
    backend = MemoryStore()
    backend.set("foo", "bar")
    assert backend.get("foo") == "bar"
    backend.delete("foo")
    assert backend.get("foo") is None

    backend.sadd("families", "a")
    backend.sadd("families", "b")
    assert backend.smembers("families") == {"a", "b"}
    backend.srem("families", "a")
    assert backend.smembers("families") == {"b"}
    backend.delete_set("families")
    assert backend.smembers("families") == set()


def test_memory_store_sliding_window():
    backend = MemoryStore()
    assert backend.hit_sliding_window("ip-1", limit=2, window_seconds=60) is True
    assert backend.hit_sliding_window("ip-1", limit=2, window_seconds=60) is True
    assert backend.hit_sliding_window("ip-1", limit=2, window_seconds=60) is False


def test_build_store_falls_back_to_memory_when_redis_unavailable():
    settings = Settings(
        REDIS_URL="redis://127.0.0.1:6399/0",
        REDIS_KEY_PREFIX="studyagent-test",
        REDIS_CONNECT_TIMEOUT_SECONDS=0.01,
    )
    backend = build_store(settings)
    assert backend.backend_name == "memory"
