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
    if "knowledge_documents" not in table_names:
        return

    columns = {column["name"] for column in inspector.get_columns("knowledge_documents")}
    statements: list[str] = []
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

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
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
