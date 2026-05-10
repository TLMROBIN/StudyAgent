from datetime import datetime
from typing import Any
from typing import Literal

from pydantic import AliasChoices, BaseModel as PydanticBaseModel, ConfigDict, Field, field_serializer, model_validator

from backend.models.conversation import GuidanceStage, MessageRole
from backend.models.knowledge import DifficultyLevel, DocumentStatus, ResourceType
from backend.models.user import UserRole
from backend.time_utils import serialize_datetime_for_api


class BaseModel(PydanticBaseModel):
    @field_serializer("*", when_used="json", check_fields=False)
    def serialize_datetime_fields(self, value: Any) -> Any:
        if isinstance(value, datetime):
            return serialize_datetime_for_api(value)
        return value


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in_seconds: int
    must_change_password: bool


class StudentLoginRequest(BaseModel):
    username: str = Field(validation_alias=AliasChoices("username", "student_no"))
    password: str


class StaffLoginRequest(BaseModel):
    username: str
    password: str


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=6, max_length=128)


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    full_name: str
    role: UserRole
    grade: int | None
    grade_label: str | None = None
    is_graduated: bool = False
    classroom_id: int | None
    classroom_name: str | None = None
    classroom_label: str | None = None
    must_change_password: bool
    is_active: bool


class ClassroomOptionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    grade: int
    name: str
    label: str


class UserCreate(BaseModel):
    full_name: str = Field(min_length=1, max_length=64)
    role: UserRole
    grade: int | None = Field(default=None, ge=1, le=3)
    is_graduated: bool = False
    classroom_name: str | None = Field(default=None, max_length=50)


class UserUpdate(BaseModel):
    full_name: str = Field(min_length=1, max_length=64)
    role: UserRole
    grade: int | None = Field(default=None, ge=1, le=3)
    is_graduated: bool = False
    classroom_name: str | None = Field(default=None, max_length=50)
    is_active: bool = True


class PasswordResetRequest(BaseModel):
    user_id: int


class ChatAttachmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    attachment_id: str
    filename: str
    content_type: str
    url: str
    ocr_status: str | None = None


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    role: MessageRole
    content: str
    attachment: ChatAttachmentRead | None = None
    turn_index: int
    guidance_stage: GuidanceStage
    created_at: datetime


class ConversationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    subject: str
    topic: str
    guidance_stage: GuidanceStage
    resolved: bool
    duration_seconds: int
    created_at: datetime
    messages: list[MessageRead] = []


class ChatRequest(BaseModel):
    subject: str
    message: str = ""
    conversation_id: int | None = None
    request_id: str | None = None


class QuestionRecommendationRequest(BaseModel):
    subject: str
    recommendation_mode: Literal["context", "keyword"] = "keyword"
    question: str | None = Field(default=None, max_length=500)
    conversation_id: int | None = None
    limit: int = Field(default=3, ge=1, le=3)
    student_grade: int | None = Field(default=None, ge=1, le=3)
    include_solutions: bool = False
    difficulty_preference: Literal["basic", "standard", "advanced"] = "basic"

    @model_validator(mode="after")
    def validate_seed(self) -> "QuestionRecommendationRequest":
        self.question = (self.question or "").strip() or None
        if self.recommendation_mode == "keyword":
            if not self.question or len(self.question) < 2:
                raise ValueError("Keyword query must contain at least 2 characters")
            return self
        if self.conversation_id is None:
            raise ValueError("Conversation id is required for context recommendations")
        return self


class ResolveConversationRequest(BaseModel):
    resolved: bool = True


class KnowledgeDocumentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    subject: str
    filename: str
    mime_type: str
    size_bytes: int
    resource_type: str
    grade: int | None = None
    chapter: str | None = None
    section: str | None = None
    difficulty: str | None = None
    tags: list[str] = Field(default_factory=list)
    status: DocumentStatus
    has_active_task: bool = False
    error_message: str | None
    created_at: datetime
    chunk_total: int = 0
    question_chunk_count: int = 0
    answer_count: int = 0
    explanation_count: int = 0
    image_count: int = 0
    split_mode: str = "按段落切分"
    count_mismatch: bool = False
    count_mismatch_kind: str = "aligned"


class KnowledgeDocumentUpdate(BaseModel):
    resource_type: ResourceType = ResourceType.KNOWLEDGE_NOTE
    grade: int | None = Field(default=None, ge=1, le=3)
    chapter: str | None = Field(default=None, max_length=255)
    section: str | None = Field(default=None, max_length=255)
    difficulty: DifficultyLevel | None = None
    tags: list[str] = Field(default_factory=list, max_length=20)


class KnowledgeDocumentBulkUpdate(BaseModel):
    document_ids: list[int] = Field(min_length=1, max_length=200)
    resource_type: ResourceType | None = None
    grade: int | None = Field(default=None, ge=1, le=3)
    chapter: str | None = Field(default=None, max_length=255)
    section: str | None = Field(default=None, max_length=255)
    difficulty: DifficultyLevel | None = None
    tags: list[str] | None = Field(default=None, max_length=20)


class KnowledgeStructureOptionRead(BaseModel):
    chapter: str
    sections: list[str] = Field(default_factory=list)


class KnowledgeAssetRead(BaseModel):
    asset_id: str
    filename: str
    content_type: str
    url: str
    title: str | None = None
    description: str | None = None


class KnowledgeChunkRead(BaseModel):
    id: int
    document_id: int
    chunk_index: int
    content: str
    subject: str
    resource_type: str
    grade: int | None = None
    chapter: str | None = None
    section: str | None = None
    difficulty: str | None = None
    tags: list[str] = Field(default_factory=list)
    chunk_kind: str | None = None
    question_number: str | None = None
    question_text: str | None = None
    answer_text: str | None = None
    explanation_text: str | None = None
    is_disabled: bool = False
    contains_images: bool = False
    image_count: int = 0
    assets: list[KnowledgeAssetRead] = Field(default_factory=list)


class KnowledgeQuestionRead(BaseModel):
    id: int
    document_id: int
    document_filename: str | None = None
    subject: str
    resource_type: str
    grade: int | None = None
    chapter: str | None = None
    section: str | None = None
    difficulty: str | None = None
    tags: list[str] = Field(default_factory=list)
    question_number: str | None = None
    question_text: str
    is_disabled: bool = False
    contains_images: bool = False
    image_count: int = 0
    assets: list[KnowledgeAssetRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class KnowledgeQuestionUpdate(BaseModel):
    chapter: str | None = Field(default=None, max_length=255)
    section: str | None = Field(default=None, max_length=255)
    difficulty: DifficultyLevel | None = None
    tags: list[str] = Field(default_factory=list, max_length=20)


class PaginatedKnowledgeQuestionRead(BaseModel):
    items: list[KnowledgeQuestionRead]
    page: int
    page_size: int
    total: int


class QuestionRecommendationRead(BaseModel):
    chunk_id: int
    document_id: int
    document_filename: str | None = None
    subject: str
    resource_type: str
    grade: int | None = None
    chapter: str | None = None
    section: str | None = None
    difficulty: str | None = None
    question_number: str | None = None
    question_text: str
    contains_images: bool = False
    image_count: int = 0
    assets: list[KnowledgeAssetRead] = Field(default_factory=list)
    answer_text: str | None = None
    explanation_text: str | None = None


class ImportTaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    document_id: int
    celery_task_id: str | None
    progress: int
    status: DocumentStatus
    error_message: str | None
    status_message: str
    document_filename: str | None = None
    document_subject: str | None = None
    created_at: datetime
    updated_at: datetime


class StatusSummaryRead(BaseModel):
    total: int = 0
    active: int = 0
    failed: int = 0
    completed: int = 0
    cancelled: int = 0


class PaginatedImportTaskRead(BaseModel):
    items: list[ImportTaskRead]
    page: int
    page_size: int
    total: int
    summary: StatusSummaryRead


class PaginatedKnowledgeDocumentRead(BaseModel):
    items: list[KnowledgeDocumentRead]
    page: int
    page_size: int
    total: int
    summary: StatusSummaryRead


class AgentConfigCreate(BaseModel):
    system_prompt: str
    guidance_params: dict[str, Any] = Field(default_factory=dict)
    subject_prompts: dict[str, Any] = Field(default_factory=dict)
    filter_rules: dict[str, Any] = Field(default_factory=dict)


class AgentConfigRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    version: int
    system_prompt: str
    guidance_params: dict[str, Any]
    subject_prompts: dict[str, Any]
    filter_rules: dict[str, Any]
    is_active: bool
    created_at: datetime


class LLMProviderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    base_url: str = Field(min_length=1, max_length=255)
    api_key: str = Field(min_length=1, max_length=512)
    model: str = Field(min_length=1, max_length=128)


class LLMProviderUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    base_url: str = Field(min_length=1, max_length=255)
    api_key: str | None = Field(default=None, max_length=512)
    model: str = Field(min_length=1, max_length=128)


class LLMProviderSelectionUpdate(BaseModel):
    active_provider_id: int
    fallback_provider_id: int | None = None


class LLMProviderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    base_url: str
    model: str
    has_api_key: bool
    is_active: bool
    is_fallback: bool
    created_at: datetime


class AuditLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    actor_id: int | None
    actor_name: str | None = None
    action: str
    target_type: str
    target_id: str | None
    result: str
    ip_address: str | None
    detail: dict[str, Any]
    created_at: datetime


class SubjectStat(BaseModel):
    subject: str
    count: int


class ClassroomStat(BaseModel):
    classroom_label: str
    grade: int | None = None
    classroom_name: str | None = None
    student_count: int
    total_conversations: int
    resolved_rate: float
    average_turns: float


class StudentPortrait(BaseModel):
    student_id: int
    student_name: str
    login_account: str | None = None
    classroom_label: str | None = None
    total_conversations: int
    resolved_rate: float
    focus_subject: str | None = None
    fallback_ratio: float
    last_active_at: datetime | None = None


class StatsOverview(BaseModel):
    total_questions: int
    resolved_rate: float
    average_turns: float
    by_subject: list[SubjectStat]


class StudentProfile(BaseModel):
    student_id: int
    total_conversations: int
    resolved_rate: float
    subject_breakdown: list[SubjectStat]
    focus_subject: str | None = None
    fallback_ratio: float = 0.0
    last_active_at: datetime | None = None


class UserImportIssue(BaseModel):
    row_number: int
    full_name: str | None = None
    login_account: str | None = None
    reason: str


class UserImportResult(BaseModel):
    rows: int
    created: int
    skipped_existing: int
    invalid: int
    issues: list[UserImportIssue] = []
