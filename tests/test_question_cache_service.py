from backend.services.question_cache_service import QuestionCacheService
from backend.services.store_service import MemoryStore
from backend.models.conversation import GuidanceStage


def test_question_cache_disables_image_turns():
    service = QuestionCacheService(store_backend=MemoryStore())

    assert service.is_cacheable(history_pairs=[], question="函数单调性第一步怎么想", has_image_turn=False) is True
    assert service.is_cacheable(history_pairs=[], question="函数单调性第一步怎么想", has_image_turn=True) is False


def test_question_cache_key_isolated_by_role_revision_hash():
    service = QuestionCacheService(store_backend=MemoryStore())
    common = {
        "subject": "物理",
        "question": "牛顿第二定律是什么意思",
        "guidance_stage": GuidanceStage.INITIAL,
        "agent_version": 1,
        "chunks": [],
    }

    default_key = service._build_key(**common)
    role_v1_key = service._build_key(**common, role_revision_hash="hash-v1")
    role_v2_key = service._build_key(**common, role_revision_hash="hash-v2")

    assert len({default_key, role_v1_key, role_v2_key}) == 3
