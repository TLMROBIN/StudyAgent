from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backend.models.conversation import GuidanceStage, MessageRole
from backend.models.knowledge import DifficultyLevel, DocumentStatus, ResourceType
from backend.models.user import UserRole


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in_seconds: int
    must_change_password: bool


class StudentLoginRequest(BaseModel):
    student_no: str
    password: str


class StaffLoginRequest(BaseModel):
    username: str
    password: str


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    student_no: str | None
    full_name: str
    role: UserRole
    grade: int | None
    classroom_id: int | None
    classroom_label: str | None = None
    must_change_password: bool
    is_active: bool


class UserCreate(BaseModel):
    username: str
    full_name: str
    role: UserRole
    password: str
    student_no: str | None = None
    grade: int | None = None
    classroom_id: int | None = None


class PasswordResetRequest(BaseModel):
    user_id: int
    new_password: str = Field(min_length=8)


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    role: MessageRole
    content: str
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
    message: str
    conversation_id: int | None = None
    request_id: str | None = None


class QuestionRecommendationRequest(BaseModel):
    subject: str
    question: str = Field(min_length=2, max_length=500)
    limit: int = Field(default=3, ge=1, le=10)
    student_grade: int | None = Field(default=None, ge=1, le=12)
    include_solutions: bool = False


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
    error_message: str | None
    created_at: datetime


class KnowledgeDocumentUpdate(BaseModel):
    resource_type: ResourceType = ResourceType.KNOWLEDGE_NOTE
    grade: int | None = Field(default=None, ge=1, le=12)
    chapter: str | None = Field(default=None, max_length=255)
    section: str | None = Field(default=None, max_length=255)
    difficulty: DifficultyLevel | None = None
    tags: list[str] = Field(default_factory=list, max_length=20)


class KnowledgeDocumentBulkUpdate(BaseModel):
    document_ids: list[int] = Field(min_length=1, max_length=200)
    resource_type: ResourceType | None = None
    grade: int | None = Field(default=None, ge=1, le=12)
    chapter: str | None = Field(default=None, max_length=255)
    section: str | None = Field(default=None, max_length=255)
    difficulty: DifficultyLevel | None = None
    tags: list[str] | None = Field(default=None, max_length=20)


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
    contains_images: bool = False
    image_count: int = 0
    assets: list[KnowledgeAssetRead] = Field(default_factory=list)


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
    student_no: str | None = None
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


class StudentImportIssue(BaseModel):
    row_number: int
    student_no: str | None = None
    reason: str


class StudentImportResult(BaseModel):
    rows: int
    created: int
    skipped_existing: int
    invalid: int
    issues: list[StudentImportIssue] = []
