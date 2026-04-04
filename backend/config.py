from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = Field(default="StudyAgent API", alias="APP_NAME")
    app_env: str = Field(default="development", alias="APP_ENV")
    api_prefix: str = Field(default="/api", alias="API_PREFIX")
    cors_origins: str = Field(default="http://localhost:5173,http://127.0.0.1:5173", alias="CORS_ORIGINS")

    jwt_secret_key: str = Field(default="studyagent-dev-secret", alias="JWT_SECRET_KEY")
    jwt_algorithm: str = Field(default="HS256", alias="JWT_ALGORITHM")
    access_token_expire_minutes: int = Field(default=30, alias="ACCESS_TOKEN_EXPIRE_MINUTES")
    refresh_token_expire_days: int = Field(default=7, alias="REFRESH_TOKEN_EXPIRE_DAYS")

    bootstrap_admin_username: str = Field(default="admin", alias="BOOTSTRAP_ADMIN_USERNAME")
    bootstrap_admin_password: str = Field(default="StudyAgent123", alias="BOOTSTRAP_ADMIN_PASSWORD")

    database_url: str | None = Field(default=None, alias="DATABASE_URL")
    sqlite_path: str = Field(default="data/studyagent.db", alias="SQLITE_PATH")
    chromadb_path: str = Field(default="data/chromadb", alias="CHROMADB_PATH")
    chromadb_mode: str = Field(default="persistent", alias="CHROMADB_MODE")
    chromadb_host: str = Field(default="127.0.0.1", alias="CHROMADB_HOST")
    chromadb_port: int = Field(default=8000, alias="CHROMADB_PORT")
    chromadb_ssl: bool = Field(default=False, alias="CHROMADB_SSL")
    chromadb_collection_prefix: str = Field(default="studyagent", alias="CHROMADB_COLLECTION_PREFIX")
    upload_path: str = Field(default="data/uploads", alias="UPLOAD_PATH")
    task_artifact_path: str = Field(default="data/tasks", alias="TASK_ARTIFACT_PATH")

    redis_url: str = Field(default="redis://redis:6379/0", alias="REDIS_URL")
    celery_broker_url: str = Field(default="redis://redis:6379/1", alias="CELERY_BROKER_URL")
    celery_result_backend: str = Field(default="redis://redis:6379/2", alias="CELERY_RESULT_BACKEND")
    redis_key_prefix: str = Field(default="studyagent", alias="REDIS_KEY_PREFIX")
    redis_connect_timeout_seconds: float = Field(default=1.0, alias="REDIS_CONNECT_TIMEOUT_SECONDS")

    llm_primary_name: str = Field(default="minimax", alias="LLM_PRIMARY_NAME")
    llm_primary_base_url: str | None = Field(default="https://api.minimaxi.com/v1", alias="LLM_PRIMARY_BASE_URL")
    llm_primary_api_key: str | None = Field(default=None, alias="LLM_PRIMARY_API_KEY")
    llm_primary_model: str = Field(default="MiniMax-M2.7", alias="LLM_PRIMARY_MODEL")

    llm_fallback_name: str = Field(default="qwen", alias="LLM_FALLBACK_NAME")
    llm_fallback_base_url: str | None = Field(default=None, alias="LLM_FALLBACK_BASE_URL")
    llm_fallback_api_key: str | None = Field(default=None, alias="LLM_FALLBACK_API_KEY")
    llm_fallback_model: str = Field(default="qwen-plus", alias="LLM_FALLBACK_MODEL")

    llm_max_qps: int = Field(default=10, alias="LLM_MAX_QPS")
    llm_request_timeout_seconds: int = Field(default=30, alias="LLM_REQUEST_TIMEOUT_SECONDS")
    llm_circuit_breaker_threshold: int = Field(default=3, alias="LLM_CIRCUIT_BREAKER_THRESHOLD")
    llm_circuit_breaker_seconds: int = Field(default=60, alias="LLM_CIRCUIT_BREAKER_SECONDS")
    hot_question_cache_ttl_seconds: int = Field(default=1800, alias="HOT_QUESTION_CACHE_TTL_SECONDS")
    chat_request_replay_ttl_seconds: int = Field(default=900, alias="CHAT_REQUEST_REPLAY_TTL_SECONDS")

    queue_max_waiting: int = Field(default=200, alias="QUEUE_MAX_WAITING")
    queue_max_concurrent: int = Field(default=20, alias="QUEUE_MAX_CONCURRENT")
    ingest_soft_time_limit_seconds: int = Field(default=300, alias="INGEST_SOFT_TIME_LIMIT_SECONDS")
    ingest_hard_time_limit_seconds: int = Field(default=330, alias="INGEST_HARD_TIME_LIMIT_SECONDS")
    ingest_poll_interval_seconds: int = Field(default=2, alias="INGEST_POLL_INTERVAL_SECONDS")

    upload_max_bytes: int = Field(default=50 * 1024 * 1024, alias="UPLOAD_MAX_BYTES")
    allowed_upload_extensions: str = Field(default=".pdf,.docx,.txt,.md,.tex", alias="ALLOWED_UPLOAD_EXTENSIONS")
    allowed_upload_mime_types: str = Field(
        default=(
            "application/pdf,"
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document,"
            "text/plain,"
            "text/markdown,"
            "text/x-markdown,"
            "text/x-tex,"
            "application/x-tex"
        ),
        alias="ALLOWED_UPLOAD_MIME_TYPES",
    )

    embedding_model_name: str = Field(default="bge-small-zh-v1.5", alias="EMBEDDING_MODEL_NAME")
    embedding_dimension: int = Field(default=128, alias="EMBEDDING_DIMENSION")
    embedding_device: str = Field(default="auto", alias="EMBEDDING_DEVICE")
    embedding_batch_size: int = Field(default=16, alias="EMBEDDING_BATCH_SIZE")
    embedding_backend: str = Field(default="sentence-transformers", alias="EMBEDDING_BACKEND")
    embedding_fallback_to_hash: bool = Field(default=True, alias="EMBEDDING_FALLBACK_TO_HASH")
    rag_chunk_size: int = Field(default=512, alias="RAG_CHUNK_SIZE")
    rag_chunk_overlap: int = Field(default=64, alias="RAG_CHUNK_OVERLAP")
    rag_top_k: int = Field(default=4, alias="RAG_TOP_K")

    default_school_name: str = Field(default="示例高中", alias="DEFAULT_SCHOOL_NAME")
    default_semester: str = Field(default="2025-2026-2", alias="DEFAULT_SEMESTER")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_format: str = Field(default="json", alias="LOG_FORMAT")

    @property
    def sqlalchemy_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        if self.sqlite_path.startswith("sqlite"):
            return self.sqlite_path
        return f"sqlite:///{self.sqlite_path}"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def upload_extension_list(self) -> list[str]:
        return [item.strip().lower() for item in self.allowed_upload_extensions.split(",") if item.strip()]

    @property
    def upload_mime_type_list(self) -> list[str]:
        return [item.strip().lower() for item in self.allowed_upload_mime_types.split(",") if item.strip()]

    def ensure_storage(self) -> None:
        base_paths = [self.sqlite_path, self.chromadb_path, self.upload_path, self.task_artifact_path]
        for path in base_paths:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True) if target.suffix else target.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_storage()
    return settings
