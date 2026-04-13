from backend.services.question_cache_service import QuestionCacheService
from backend.services.store_service import MemoryStore


def test_question_cache_disables_image_turns():
    service = QuestionCacheService(store_backend=MemoryStore())

    assert service.is_cacheable(history_pairs=[], question="函数单调性第一步怎么想", has_image_turn=False) is True
    assert service.is_cacheable(history_pairs=[], question="函数单调性第一步怎么想", has_image_turn=True) is False
