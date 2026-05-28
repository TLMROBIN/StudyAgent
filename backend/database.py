from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from backend.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
engine = create_engine(
    settings.sqlalchemy_database_url,
    connect_args={"check_same_thread": False} if settings.sqlalchemy_database_url.startswith("sqlite") else {},
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False, class_=Session)


def apply_runtime_schema_updates() -> None:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    statements: list[str] = []
    if "knowledge_documents" in table_names:
        columns = {column["name"] for column in inspector.get_columns("knowledge_documents")}
        if "resource_type" not in columns:
            statements.append("ALTER TABLE knowledge_documents ADD COLUMN resource_type VARCHAR(32)")
        if "grade" not in columns:
            statements.append("ALTER TABLE knowledge_documents ADD COLUMN grade INTEGER")
        if "chapter" not in columns:
            statements.append("ALTER TABLE knowledge_documents ADD COLUMN chapter VARCHAR(255)")
        if "section" not in columns:
            statements.append("ALTER TABLE knowledge_documents ADD COLUMN section VARCHAR(255)")
        if "difficulty" not in columns:
            statements.append("ALTER TABLE knowledge_documents ADD COLUMN difficulty VARCHAR(32)")
        if "tags_json" not in columns:
            statements.append("ALTER TABLE knowledge_documents ADD COLUMN tags_json JSON")

    if "users" in table_names:
        user_columns = {column["name"] for column in inspector.get_columns("users")}
        if "last_grade_promotion_year" not in user_columns:
            statements.append("ALTER TABLE users ADD COLUMN last_grade_promotion_year INTEGER")
        if "graduated_at" not in user_columns:
            statements.append("ALTER TABLE users ADD COLUMN graduated_at DATETIME")

    if "conversations" in table_names:
        conversation_columns = {column["name"] for column in inspector.get_columns("conversations")}
        if "deleted_by_student_at" not in conversation_columns:
            statements.append("ALTER TABLE conversations ADD COLUMN deleted_by_student_at DATETIME")

    if "messages" in table_names:
        message_columns = {column["name"] for column in inspector.get_columns("messages")}
        if "llm_model_key" not in message_columns:
            statements.append("ALTER TABLE messages ADD COLUMN llm_model_key VARCHAR(64)")

    if "llm_model_configs" in table_names:
        model_columns = {column["name"] for column in inspector.get_columns("llm_model_configs")}
        if "vision_understanding_priority" not in model_columns:
            statements.append("ALTER TABLE llm_model_configs ADD COLUMN vision_understanding_priority BOOLEAN DEFAULT 0 NOT NULL")

    if "notifications" in table_names:
        notification_columns = {column["name"] for column in inspector.get_columns("notifications")}
        if "archived_at" not in notification_columns:
            statements.append("ALTER TABLE notifications ADD COLUMN archived_at DATETIME")

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
        if "knowledge_documents" in table_names:
            connection.execute(
                text(
                    "UPDATE knowledge_documents "
                    "SET resource_type = 'knowledge_note' "
                    "WHERE resource_type IS NULL OR resource_type = ''"
                )
            )
            connection.execute(
                text(
                    "UPDATE knowledge_documents "
                    "SET tags_json = '[]' "
                    "WHERE tags_json IS NULL OR tags_json = ''"
                )
            )


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
