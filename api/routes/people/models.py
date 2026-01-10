from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime

# -----------------------------------------------------------------------------
# Summon / Persona Info Models
# -----------------------------------------------------------------------------

class PersonaInfo(BaseModel):
    id: str
    name: str
    avatar: Optional[str] = None
    status: str # "available", "conversing", "dispatched"

class SummonRequest(BaseModel):
    persona_id: str


# -----------------------------------------------------------------------------
# Memory Management (Chat Logs) Models
# -----------------------------------------------------------------------------

class ThreadSummary(BaseModel):
    thread_id: str
    suffix: str
    preview: str
    active: bool

class MessageItem(BaseModel):
    id: str
    thread_id: str
    role: str
    content: str
    created_at: Optional[float] = None
    metadata: Optional[dict] = None

class MessagesResponse(BaseModel):
    items: List[MessageItem]
    total: int
    page: int
    page_size: int
    first_created_at: Optional[float] = None
    last_created_at: Optional[float] = None

class UpdateMessageRequest(BaseModel):
    content: Optional[str] = None
    created_at: Optional[float] = None


# -----------------------------------------------------------------------------
# Memory Recall Models
# -----------------------------------------------------------------------------

class MemoryRecallRequest(BaseModel):
    query: str
    topk: int = 4
    max_chars: int = 1200

class MemoryRecallResponse(BaseModel):
    query: str
    result: str
    topk: int
    max_chars: int


# -----------------------------------------------------------------------------
# Configuration Models
# -----------------------------------------------------------------------------

class AIConfigResponse(BaseModel):
    name: str
    description: str
    system_prompt: str
    default_model: Optional[str]
    lightweight_model: Optional[str] = None
    interaction_mode: str
    avatar_path: Optional[str] = None
    appearance_image_path: Optional[str] = None  # Visual context appearance image
    home_city_id: int

class UpdateAIConfigRequest(BaseModel):
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    default_model: Optional[str] = None
    lightweight_model: Optional[str] = None
    interaction_mode: Optional[str] = None
    avatar_path: Optional[str] = None
    appearance_image_path: Optional[str] = None  # Visual context appearance image


# -----------------------------------------------------------------------------
# Autonomous Status Models
# -----------------------------------------------------------------------------

class AutonomousStatusResponse(BaseModel):
    persona_id: str
    interaction_mode: str
    system_running: bool
    is_active: bool  # True if actually doing autonomous conversation


# -----------------------------------------------------------------------------
# Import / Export Models
# -----------------------------------------------------------------------------

class ConversationSummary(BaseModel):
    idx: int
    id: str
    conversation_id: Optional[str]
    title: str
    create_time: Optional[str]
    update_time: Optional[str]
    message_count: int
    preview: Optional[str]

class PreviewResponse(BaseModel):
    conversations: List[ConversationSummary]
    cache_key: str
    total_count: int

class ImportRequest(BaseModel):
    cache_key: str
    conversation_ids: List[str]  # List of conversation_id or idx as string
    skip_embedding: bool = False

class OfficialImportStatusResponse(BaseModel):
    running: bool
    progress: Optional[int] = None
    total: Optional[int] = None
    message: Optional[str] = None
    success: Optional[bool] = None
    conversations: Optional[int] = None
    messages: Optional[int] = None

class ExtensionImportStatusResponse(BaseModel):
    running: bool
    progress: Optional[int] = None
    total: Optional[int] = None
    message: Optional[str] = None
    success: Optional[bool] = None
    title: Optional[str] = None


# -----------------------------------------------------------------------------
# Re-embed Models
# -----------------------------------------------------------------------------

class ReembedRequest(BaseModel):
    force: bool = False  # If true, re-embed all messages regardless of current status

class ReembedStatusResponse(BaseModel):
    running: bool
    progress: Optional[int] = None
    total: Optional[int] = None
    message: Optional[str] = None


# -----------------------------------------------------------------------------
# Memopedia Models
# -----------------------------------------------------------------------------

class UpdateMemopediaPageRequest(BaseModel):
    title: Optional[str] = None
    summary: Optional[str] = None
    content: Optional[str] = None
    keywords: Optional[List[str]] = None
    vividness: Optional[str] = None


# -----------------------------------------------------------------------------
# Schedule Models
# -----------------------------------------------------------------------------

class ScheduleItem(BaseModel):
    schedule_id: int
    schedule_type: str
    meta_playbook: str
    description: Optional[str]
    priority: int
    enabled: bool
    days_of_week: Optional[List[int]] = None
    time_of_day: Optional[str] = None
    scheduled_datetime: Optional[datetime] = None
    interval_seconds: Optional[int] = None
    last_executed_at: Optional[datetime] = None
    completed: bool

class CreateScheduleRequest(BaseModel):
    schedule_type: str # periodic, oneshot, interval
    meta_playbook: str
    description: str = ""
    priority: int = 0
    enabled: bool = True
    # periodic
    days_of_week: Optional[List[int]] = None # 0=Mon, 6=Sun
    time_of_day: Optional[str] = None # HH:MM
    # oneshot
    scheduled_datetime: Optional[str] = None # "YYYY-MM-DD HH:MM" (in persona TZ)
    # interval
    interval_seconds: Optional[int] = None

class UpdateScheduleRequest(BaseModel):
    schedule_type: Optional[str] = None
    meta_playbook: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[int] = None
    enabled: Optional[bool] = None
    days_of_week: Optional[List[int]] = None
    time_of_day: Optional[str] = None
    scheduled_datetime: Optional[str] = None  # "YYYY-MM-DD HH:MM" (in persona TZ)
    interval_seconds: Optional[int] = None


# -----------------------------------------------------------------------------
# Task Management Models
# -----------------------------------------------------------------------------

class TaskStep(BaseModel):
    id: str
    position: int
    title: str
    description: Optional[str]
    status: str
    notes: Optional[str]
    updated_at: str

class TaskRecordModel(BaseModel):
    id: str
    title: str
    goal: str
    summary: str
    status: str
    priority: str
    active_step_id: Optional[str]
    updated_at: str
    steps: List[TaskStep]

class CreateTaskRequest(BaseModel):
    title: str
    goal: str
    summary: str
    notes: Optional[str] = None
    priority: str = "normal"
    steps: List[dict] # {title, description, ...}

class UpdateTaskStatusRequest(BaseModel):
    status: str
    reason: Optional[str] = None


# -----------------------------------------------------------------------------
# Inventory Models
# -----------------------------------------------------------------------------

class InventoryItem(BaseModel):
    id: str
    name: str
    type: str # document, picture, object, etc.
    description: str
    file_path: Optional[str] = None
    created_at: datetime


# -----------------------------------------------------------------------------
# Arasuji (Episode Summary) Models
# -----------------------------------------------------------------------------

class ArasujiStatsResponse(BaseModel):
    max_level: int
    counts_by_level: dict  # {level: count}
    total_count: int

class ArasujiEntryItem(BaseModel):
    id: str
    level: int
    content: str
    start_time: Optional[int] = None
    end_time: Optional[int] = None
    message_count: int
    is_consolidated: bool
    created_at: Optional[int] = None
    source_ids: List[str] = []
    # For level 1: message number range (1-indexed, for build_arasuji.py --offset)
    source_start_num: Optional[int] = None  # first message number
    source_end_num: Optional[int] = None    # last message number

class ArasujiListResponse(BaseModel):
    entries: List[ArasujiEntryItem]
    total: int
    level_filter: Optional[int] = None

class SourceMessageItem(BaseModel):
    id: str
    role: str
    content: str
    created_at: int
