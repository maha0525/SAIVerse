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
    # Stelis thread info
    is_stelis: bool = False
    stelis_parent_id: Optional[str] = None
    stelis_depth: Optional[int] = None
    stelis_status: Optional[str] = None  # "active", "completed", "aborted"
    stelis_label: Optional[str] = None

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


class CreateMessageRequest(BaseModel):
    role: str  # "user", "assistant", "system"
    content: str
    created_at: Optional[float] = None  # Unix timestamp, defaults to current time
    metadata: Optional[dict] = None  # Optional tags, etc.


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


class MemoryRecallDebugRequest(BaseModel):
    """Debug-friendly recall request: returns raw search results with scores."""
    query: str = ""  # Semantic query (can be empty if using keywords only)
    keywords: List[str] = []  # Keywords for BM25-like matching
    topk: int = 50  # Allow higher values for debugging
    use_rrf: bool = False  # Enable Reciprocal Rank Fusion (split query by spaces)
    use_hybrid: bool = False  # Enable hybrid search (keywords + semantic)
    rrf_k: int = 60  # RRF constant (higher = more weight to lower ranks)
    start_date: Optional[str] = None  # Filter: start date (YYYY-MM-DD)
    end_date: Optional[str] = None  # Filter: end date (YYYY-MM-DD)


class MemoryRecallDebugHit(BaseModel):
    """A single search hit with its metadata."""
    rank: int
    score: float
    message_id: str
    thread_id: str
    role: str
    content: str
    created_at: float  # Unix timestamp
    created_at_str: str  # Human-readable datetime


class MemoryRecallDebugResponse(BaseModel):
    """Debug-friendly recall response with raw search results."""
    query: str
    topk: int
    total_hits: int
    hits: List[MemoryRecallDebugHit]


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
    chronicle_enabled: bool = True
    memory_weave_context: bool = True
    avatar_path: Optional[str] = None
    appearance_image_path: Optional[str] = None  # Visual context appearance image
    home_city_id: int
    linked_user_id: Optional[int] = None  # First linked user ID

class UpdateAIConfigRequest(BaseModel):
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    default_model: Optional[str] = None
    lightweight_model: Optional[str] = None
    interaction_mode: Optional[str] = None
    chronicle_enabled: Optional[bool] = None
    memory_weave_context: Optional[bool] = None
    avatar_path: Optional[str] = None
    appearance_image_path: Optional[str] = None  # Visual context appearance image
    linked_user_id: Optional[int] = None  # Set linked user (None = no change, 0 = clear)


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

class NativeImportStatusResponse(BaseModel):
    running: bool
    progress: Optional[int] = None
    total: Optional[int] = None
    message: Optional[str] = None
    success: Optional[bool] = None
    threads_imported: Optional[int] = None
    messages_imported: Optional[int] = None


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
    is_trunk: Optional[bool] = None


class CreateMemopediaPageRequest(BaseModel):
    parent_id: str
    title: str
    summary: str = ""
    content: str = ""
    keywords: Optional[List[str]] = None
    vividness: str = "rough"
    is_trunk: bool = False


class SetTrunkRequest(BaseModel):
    is_trunk: bool


class SetImportantRequest(BaseModel):
    is_important: bool


class MovePagesToTrunkRequest(BaseModel):
    page_ids: List[str]
    trunk_id: str


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
    playbook_params: Optional[dict] = None  # Playbook parameters (e.g., {"selected_playbook": "xxx"})

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
    # playbook params
    playbook_params: Optional[dict] = None  # Playbook parameters (e.g., {"selected_playbook": "xxx"})

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
    playbook_params: Optional[dict] = None  # Playbook parameters (e.g., {"selected_playbook": "xxx"})


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


# -----------------------------------------------------------------------------
# Generation Job Models (Memory Weave)
# -----------------------------------------------------------------------------

class GenerateArasujiRequest(BaseModel):
    """Chronicle生成リクエスト"""
    max_messages: int = 500  # 最大処理メッセージ数
    batch_size: int = 20     # バッチサイズ（これ未満のメッセージは処理しない）
    consolidation_size: int = 10  # 統合サイズ
    model: Optional[str] = None  # デフォルトはMEMORY_WEAVE_MODEL
    with_memopedia: bool = False  # Memopedia同時生成


class GenerateMemopediaRequest(BaseModel):
    """Memopediaページ生成リクエスト（キーワード指定）"""
    keyword: str
    directions: Optional[str] = None  # 調査の方向性・まとめ方の指示
    category: Optional[str] = None  # people, terms, plans (None = auto-detect)
    max_loops: int = 5  # 最大検索ループ数
    context_window: int = 5  # 周辺メッセージ取得数
    with_chronicle: bool = True  # Chronicle（あらすじ）を参照するか
    model: Optional[str] = None  # デフォルトはMEMORY_WEAVE_MODEL


class ChronicleCostEstimate(BaseModel):
    """Chronicle生成のコスト推定"""
    total_messages: int
    processed_messages: int
    unprocessed_messages: int
    estimated_llm_calls: int
    estimated_cost_usd: float
    model_name: str
    is_free_tier: bool
    batch_size: int


class GenerationJobStatus(BaseModel):
    """生成ジョブのステータス"""
    job_id: str
    status: str  # "pending", "running", "completed", "failed"
    progress: Optional[int] = None  # 処理済みメッセージ数
    total: Optional[int] = None  # 総処理対象メッセージ数
    message: Optional[str] = None  # ステータスメッセージ
    entries_created: Optional[int] = None  # 作成されたエントリ数
    error: Optional[str] = None  # エラーメッセージ
