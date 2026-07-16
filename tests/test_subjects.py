import asyncio

from fastapi import HTTPException
import pytest

from backend.config import Settings
from backend.models.schemas import ChatRequest, QuestionRecommendationRequest
from backend.routers import chat as chat_router
from backend.routers import knowledge as knowledge_router
from backend.services.filter_rule_engine import DEFAULT_CONFIG, SOURCE_BUILTIN_DEFAULT, compile_config
from backend.services.socratic_service import socratic_service
from backend.services.subject_guidance_service import SUBJECT_STRATEGY_RULES
from backend.services.vector_store_service import SUBJECT_COLLECTION_NAMES, VectorStoreService
from backend.subjects import SUBJECTS, SUBJECT_SET, is_valid_subject


def test_supported_subject_domain_is_complete_and_stable():
    assert SUBJECTS == ("语文", "数学", "英语", "物理", "化学", "生物", "政治", "历史", "地理")
    assert set(SUBJECT_COLLECTION_NAMES) == SUBJECT_SET
    assert set(SUBJECT_STRATEGY_RULES) == SUBJECT_SET - {"物理"}
    assert all(is_valid_subject(subject) for subject in SUBJECTS)
    assert not is_valid_subject("天文")
    assert not is_valid_subject("")
    assert not is_valid_subject(None)


def test_socratic_subject_list_stays_derived_from_supported_domain():
    assert socratic_service.base_prompt.endswith(f"只回答高中{'、'.join(SUBJECTS)}。")


def test_filter_config_requires_exact_supported_subject_keys():
    compile_config(DEFAULT_CONFIG, source=SOURCE_BUILTIN_DEFAULT, config_path="<builtin>")
    missing_subject = {**DEFAULT_CONFIG, "subjects": dict(DEFAULT_CONFIG["subjects"])}
    missing_subject["subjects"].pop("地理")

    with pytest.raises(ValueError, match="subjects keys must match"):
        compile_config(missing_subject, source="test", config_path="<test>")


def test_vector_collection_rejects_unsupported_subject_before_client_access():
    service = VectorStoreService(Settings(CHROMADB_COLLECTION_PREFIX="subject-test"))

    assert service._collection_name("化学") == "subject-test-chemistry"
    with pytest.raises(ValueError, match="Unsupported subject"):
        service._collection_name("天文")


def test_chat_entrypoints_reject_unsupported_subject_before_side_effects():
    invalid_chat = ChatRequest(subject="天文", message="这题怎么做")
    with pytest.raises(HTTPException) as stream_error:
        asyncio.run(chat_router.stream_chat(invalid_chat, None, None, None))
    assert stream_error.value.status_code == 422

    invalid_recommendation = QuestionRecommendationRequest(subject="天文", question="恒星演化")
    with pytest.raises(HTTPException) as recommendation_error:
        chat_router.recommend_questions(invalid_recommendation, None, None)
    assert recommendation_error.value.status_code == 422


def test_knowledge_upload_rejects_unsupported_subject_before_file_access():
    with pytest.raises(HTTPException) as upload_error:
        asyncio.run(
            knowledge_router.upload_document(
                background_tasks=None,
                subject="天文",
                file=None,
                db=None,
                current_user=None,
                request=None,
            )
        )
    assert upload_error.value.status_code == 422
