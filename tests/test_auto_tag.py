from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.database import Base
from backend.models import agent_config, audit_log, conversation, knowledge, user  # noqa: F401
from backend.models.knowledge import KnowledgeChunk, KnowledgeDocument, ResourceType
from backend.services.auto_tag_service import AutoTagService


def setup_db() -> sessionmaker:
    engine = create_engine("sqlite:///:memory:")
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)
    return factory


def _seed_textbook(db: Session, subject: str, tags: list[str]) -> KnowledgeDocument:
    doc = KnowledgeDocument(
        subject=subject,
        filename="textbook.pdf",
        file_path="/tmp/textbook.pdf",
        mime_type="application/pdf",
        size_bytes=0,
        resource_type=ResourceType.TEXTBOOK.value,
        tags_json=tags,
    )
    db.add(doc)
    db.commit()
    return doc


def _seed_textbook_chunk(
    db: Session,
    *,
    document: KnowledgeDocument,
    chapter: str | None = None,
    section: str | None = None,
) -> None:
    db.add(
        KnowledgeChunk(
            document_id=document.id,
            subject=document.subject,
            chunk_index=0,
            content="示例教材片段",
            metadata_json={
                "document_id": document.id,
                "chapter": chapter,
                "section": section,
            },
        )
    )
    db.commit()


class TestCleanTitle:
    def test_removes_extension(self):
        svc = AutoTagService()
        assert svc._clean_title("牛顿第二定律.pdf") == "牛顿第二定律"

    def test_removes_copy_number(self):
        svc = AutoTagService()
        assert svc._clean_title("力学基础(1).docx") == "力学基础"

    def test_removes_version_suffix(self):
        svc = AutoTagService()
        assert svc._clean_title("电磁学_v2.pdf") == "电磁学"

    def test_strips_whitespace(self):
        svc = AutoTagService()
        assert svc._clean_title("  运动学  .pdf  ") == "运动学"


class TestMatchTags:
    def test_matches_substring(self):
        svc = AutoTagService()
        vocab = ["牛顿第二定律", "运动学", "力学"]
        result = svc._match_tags("牛顿第二定律及其应用", vocab)
        assert "牛顿第二定律" in result
        assert "力学" not in result

    def test_longer_tags_first_avoids_redundancy(self):
        svc = AutoTagService()
        vocab = ["牛顿第二定律", "牛顿"]
        result = svc._match_tags("牛顿第二定律练习题", vocab)
        assert result == ["牛顿第二定律"]

    def test_no_match(self):
        svc = AutoTagService()
        vocab = ["运动学", "力学"]
        result = svc._match_tags("电磁学基础", vocab)
        assert result == []

    def test_multiple_non_overlapping_matches(self):
        svc = AutoTagService()
        vocab = ["运动学", "动力学", "力学"]
        result = svc._match_tags("运动学与动力学导论", vocab)
        assert "运动学" in result
        assert "动力学" in result
        assert "力学" not in result


class TestAutoTag:
    def test_auto_tag_from_textbook_vocabulary(self):
        factory = setup_db()
        svc = AutoTagService(cache_ttl=0)
        with factory() as db:
            _seed_textbook(db, "物理", ["牛顿第二定律", "运动学", "力学"])
            result = svc.auto_tag(db, "牛顿第二定律及其应用.pdf", "物理")
            assert "牛顿第二定律" in result

    def test_merges_with_existing_tags(self):
        factory = setup_db()
        svc = AutoTagService(cache_ttl=0)
        with factory() as db:
            _seed_textbook(db, "数学", ["函数", "单调性"])
            result = svc.auto_tag(
                db, "函数单调性讲义.pdf", "数学", existing_tags=["已有标签"]
            )
            assert "已有标签" in result
            assert "函数" in result

    def test_no_duplicate_when_existing_matches(self):
        factory = setup_db()
        svc = AutoTagService(cache_ttl=0)
        with factory() as db:
            _seed_textbook(db, "数学", ["函数", "单调性"])
            result = svc.auto_tag(
                db, "函数单调性讲义.pdf", "数学", existing_tags=["函数"]
            )
            assert result.count("函数") == 1
            assert "单调性" in result

    def test_empty_when_no_textbooks(self):
        factory = setup_db()
        svc = AutoTagService(cache_ttl=0)
        with factory() as db:
            result = svc.auto_tag(db, "牛顿第二定律.pdf", "物理")
            assert result == ["牛顿第二定律"]

    def test_uses_textbook_section_and_chapter_vocabulary_when_tags_are_empty(self):
        factory = setup_db()
        svc = AutoTagService(cache_ttl=0)
        with factory() as db:
            textbook = _seed_textbook(db, "物理", [])
            _seed_textbook_chunk(
                db,
                document=textbook,
                chapter="第七章 万有引力与宇宙航行",
                section="第五节 牛顿第二定律",
            )

            result = svc.auto_tag(
                db,
                "物理思维向导：牛顿第二定律的深度解译与实战自学手册.md",
                "物理",
            )

            assert "牛顿第二定律" in result
            assert "第五节 牛顿第二定律" not in result

    def test_respects_max_20_tags(self):
        factory = setup_db()
        svc = AutoTagService(cache_ttl=0)
        with factory() as db:
            long_tags = [f"标签{i}" for i in range(30)]
            _seed_textbook(db, "化学", long_tags)
            result = svc.auto_tag(
                db, "标签" + "".join(str(i) for i in range(30)) + ".pdf", "化学"
            )
            assert len(result) <= 20

    def test_scoped_to_same_subject(self):
        factory = setup_db()
        svc = AutoTagService(cache_ttl=0)
        with factory() as db:
            _seed_textbook(db, "物理", ["牛顿第二定律"])
            _seed_textbook(db, "数学", ["函数"])
            result = svc.auto_tag(db, "牛顿第二定律与函数.pdf", "物理")
            assert "牛顿第二定律" in result
            assert "函数" not in result

    def test_preserves_existing_tags_when_no_match(self):
        factory = setup_db()
        svc = AutoTagService(cache_ttl=0)
        with factory() as db:
            _seed_textbook(db, "英语", ["定语从句"])
            result = svc.auto_tag(db, "完形填空.pdf", "英语", existing_tags=["完形"])
            assert result == ["完形"]

    def test_fallback_extracts_core_tag_from_quoted_title_when_vocab_missing(self):
        factory = setup_db()
        svc = AutoTagService(cache_ttl=0)
        with factory() as db:
            result = svc.auto_tag(db, "《时间与位移》深度思维向导手册.md", "物理")
            assert result == ["时间与位移"]

    def test_fallback_extracts_core_tag_from_colon_title_when_vocab_missing(self):
        factory = setup_db()
        svc = AutoTagService(cache_ttl=0)
        with factory() as db:
            result = svc.auto_tag(
                db,
                "课程思维向导：重力势能 (Gravitational Potential Energy) 自学手册.md",
                "物理",
            )
            assert result == ["重力势能"]

    def test_match_textbook_structure_returns_parent_chapter_and_section(self):
        factory = setup_db()
        svc = AutoTagService(cache_ttl=0)
        with factory() as db:
            textbook = _seed_textbook(db, "物理", [])
            _seed_textbook_chunk(
                db,
                document=textbook,
                chapter="第十一章 交变电流",
                section="第一节 交变电流",
            )

            result = svc.match_textbook_structure(db, "交变电流.docx", "物理")

            assert result == {
                "chapter": "第十一章 交变电流",
                "section": "第一节 交变电流",
            }

    def test_match_textbook_structure_returns_none_when_title_misses_catalog(self):
        factory = setup_db()
        svc = AutoTagService(cache_ttl=0)
        with factory() as db:
            textbook = _seed_textbook(db, "物理", [])
            _seed_textbook_chunk(
                db,
                document=textbook,
                chapter="第十一章 交变电流",
                section="第一节 交变电流",
            )

            result = svc.match_textbook_structure(db, "完全不匹配.docx", "物理")

            assert result == {"chapter": None, "section": None}


class TestCache:
    def test_cache_reuses_within_ttl(self):
        factory = setup_db()
        svc = AutoTagService(cache_ttl=60)
        with factory() as db:
            _seed_textbook(db, "物理", ["运动学"])
            result1 = svc.auto_tag(db, "运动学基础.pdf", "物理")
            assert "运动学" in result1
            assert svc._cache_time > 0

    def test_invalidate_cache_resets(self):
        factory = setup_db()
        svc = AutoTagService(cache_ttl=60)
        with factory() as db:
            _seed_textbook(db, "物理", ["运动学"])
            svc.auto_tag(db, "运动学基础.pdf", "物理")
            assert svc._cache_time > 0
            svc.invalidate_cache()
            assert svc._cache_time == 0
