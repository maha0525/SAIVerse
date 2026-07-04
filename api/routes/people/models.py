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
    # 2026-05-20: Gemini 3.x の thoughtSignature が永続化されているかを示すフラグ。
    # bytes 中身そのものは公開せず、フロントで「signature あり」アイコン表示に使う。
    # 詳細は docs/intent/thought_signature_persistence.md
    has_thought_signature: bool = False

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

class MetaJudgmentConfig(BaseModel):
    """Phase 4-e: Per-persona meta-judgment Pulse parameters.

    All fields optional — missing keys fall back to MetaLayer's built-in defaults.
    """
    cache_threshold_ratio: Optional[float] = None    # 0.0–1.0, default 0.3
    max_retries: Optional[int] = None                # default 1
    retry_backoff_seconds: Optional[int] = None      # default 5
    periodic_interval_minutes: Optional[int] = None  # default 50 (メタ判断自動発話間隔)
    keep_cache_alive: Optional[bool] = None          # default True (TTL 接近で前倒し fire)
    # ライフビュー「作業のテンポ」: 自律 Track の Pulse 間隔 (秒) のペルソナ既定値。
    # update_ai は META_JUDGMENT_CONFIG を丸ごと置換するため、ここに定義しないと
    # SettingsModal 保存時にキーが消える (persona_activity_view.md §7)。
    autonomous_pulse_interval_seconds: Optional[int] = None  # default 30
    # 開発者モード用デバッグフラグ: True なら meta_judgment を毎回強制失敗させる
    # (① リカバリの実機検証用)。UI は開発者モード限定で表示する。
    force_fail: Optional[bool] = None  # default False


class AIConfigResponse(BaseModel):
    name: str
    description: str
    system_prompt: str
    default_model: Optional[str]
    lightweight_model: Optional[str] = None
    vision_model: Optional[str] = None
    audio_model: Optional[str] = None
    video_model: Optional[str] = None
    memory_weave_model: Optional[str] = None
    activity_state: str  # 'Stop' / 'Sleep' / 'Idle' / 'Active'
    chronicle_enabled: bool = True
    autonomous_chronicle_enabled: bool = True
    auto_recall_enabled: bool = True
    memory_weave_context: bool = True
    memopedia_index_enabled: bool = False
    core_memory_char_budget: Optional[int] = None  # 記憶アーキv2 ゾーンA 容量目安 (NULL → 既定 2000)
    spell_enabled: bool = False
    realtime_info_enabled: bool = True
    avatar_path: Optional[str] = None
    appearance_image_path: Optional[str] = None  # Visual context appearance image
    home_city_id: int
    linked_user_id: Optional[int] = None  # First linked user ID
    meta_judgment_config: Optional[MetaJudgmentConfig] = None  # Phase 4-e
    user_conv_timeout_minutes: Optional[int] = None  # 2026-05-09 wait_response auto-pause

class UpdateAIConfigRequest(BaseModel):
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    default_model: Optional[str] = None
    lightweight_model: Optional[str] = None
    vision_model: Optional[str] = None
    audio_model: Optional[str] = None
    video_model: Optional[str] = None
    memory_weave_model: Optional[str] = None
    activity_state: Optional[str] = None  # 'Stop' / 'Sleep' / 'Idle' / 'Active'
    chronicle_enabled: Optional[bool] = None
    autonomous_chronicle_enabled: Optional[bool] = None
    auto_recall_enabled: Optional[bool] = None
    memory_weave_context: Optional[bool] = None
    memopedia_index_enabled: Optional[bool] = None
    # 記憶アーキv2 ゾーンA 容量目安 (文字数)。
    #   None = no change, 0 (or any non-positive) = clear to default (= 2000),
    #   positive int = override.
    core_memory_char_budget: Optional[int] = None
    spell_enabled: Optional[bool] = None
    realtime_info_enabled: Optional[bool] = None
    avatar_path: Optional[str] = None
    appearance_image_path: Optional[str] = None  # Visual context appearance image
    linked_user_id: Optional[int] = None  # Set linked user (None = no change, 0 = clear)
    meta_judgment_config: Optional[MetaJudgmentConfig] = None  # Phase 4-e
    # 2026-05-09: wait_response Track auto-pause timeout (minutes).
    #   None = no change, 0 (or any non-positive) = clear to default (= 30 min),
    #   positive int = override.
    user_conv_timeout_minutes: Optional[int] = None


# -----------------------------------------------------------------------------
# Autonomous Status Models
# -----------------------------------------------------------------------------

class AutonomousStatusResponse(BaseModel):
    persona_id: str
    activity_state: str  # 'Stop' / 'Sleep' / 'Idle' / 'Active'
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
    args: Optional[dict] = None  # Playbook arguments (e.g., {"selected_playbook": "xxx"})

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
    # playbook args
    args: Optional[dict] = None  # Playbook arguments (e.g., {"selected_playbook": "xxx"})

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
    args: Optional[dict] = None  # Playbook arguments (e.g., {"selected_playbook": "xxx"})


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
    # 統合 Task の所属 (unified_task_model.md): 'note'=候補 / 'track'=Track 小目標 / None=単独。
    parent_kind: Optional[str] = None
    task_ref: Optional[str] = None        # "task:N" (persona 内通し番号)
    parent_label: Optional[str] = None    # UI 表示用: "候補" / "t:N {title}" / "単独"

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
    include_timestamp: bool = True  # 日時情報をLLMに渡すか（インポートログ等で日時が不正確な場合はFalse）


class GenerateMemopediaRequest(BaseModel):
    """Memopediaページ生成リクエスト（キーワード指定）"""
    keyword: str
    directions: Optional[str] = None  # 調査の方向性・まとめ方の指示
    category: Optional[str] = None  # people, terms, plans (None = auto-detect)
    max_loops: int = 5  # 最大検索ループ数
    context_window: int = 5  # 周辺メッセージ取得数
    with_chronicle: bool = True  # Chronicle（あらすじ）を参照するか
    model: Optional[str] = None  # デフォルトはMEMORY_WEAVE_MODEL


class BuildMemopediaFromLogsRequest(BaseModel):
    """ログからMemopediaを構築するリクエスト"""
    batch_size: int = 20  # バッチサイズ
    limit: int = 0  # 処理対象メッセージ上限 (0=全件)
    start_after: float = 0  # このタイムスタンプ以降のメッセージを処理
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
    currency: str = "USD"


class GenerationJobStatus(BaseModel):
    """生成ジョブのステータス"""
    job_id: str
    status: str  # "pending", "running", "completed", "failed"
    progress: Optional[int] = None  # 処理済みメッセージ数
    total: Optional[int] = None  # 総処理対象メッセージ数
    message: Optional[str] = None  # ステータスメッセージ
    entries_created: Optional[int] = None  # 作成されたエントリ数
    error: Optional[str] = None  # エラーメッセージ（ユーザー向け）
    error_code: Optional[str] = None  # エラーコード (payment, authentication, rate_limit, etc.)
    error_detail: Optional[str] = None  # 技術的詳細（開発者向け）
    error_meta: Optional[dict] = None  # エラー発生バッチのメタデータ (message_ids, start_time, end_time)


class UpdateArasujiEntryRequest(BaseModel):
    """Chronicleエントリ更新リクエスト"""
    content: str


class MessagesByIdsRequest(BaseModel):
    """メッセージID指定取得リクエスト"""
    ids: List[str]


# -----------------------------------------------------------------------------
# Pulse Logs Models
# -----------------------------------------------------------------------------

class PulseSummaryItem(BaseModel):
    pulse_id: str
    entry_count: int
    latest_created_at: int
    playbook_name: Optional[str] = None

class PulseListResponse(BaseModel):
    items: List[PulseSummaryItem]
    total: int
    page: int
    page_size: int

class PulseLogEntry(BaseModel):
    id: str
    pulse_id: str
    thread_id: Optional[str] = None
    role: str
    content: Optional[str] = None
    node_id: Optional[str] = None
    playbook_name: Optional[str] = None
    important: bool = False
    tool_calls: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_name: Optional[str] = None
    created_at: int

class PulseLogsResponse(BaseModel):
    items: List[PulseLogEntry]
    pulse_id: str
    total: int


# -----------------------------------------------------------------------------
# Storage Layers Models (Intent A v0.14, Intent B v0.11 — 7-layer storage view)
# -----------------------------------------------------------------------------

class StorageLayerStat(BaseModel):
    """Per-layer summary: count + latest timestamp + freeform note."""
    layer: str  # 'meta_judgment' / 'main_cache' / 'sub_cache' / 'nested_temp' / 'track_local' / 'saimemory_core' / 'archive'
    layer_index: int  # 1..7
    label: str  # human-readable name e.g. "[1] メタ判断ログ"
    count: int
    latest_at: Optional[float] = None  # unix epoch
    note: Optional[str] = None  # e.g. "未実装", "揮発のため表示できません"


class StorageLayerEntry(BaseModel):
    """One row from any of the storage layers, normalized for the unified list view."""
    layer: str  # same vocabulary as StorageLayerStat.layer
    entry_id: str
    created_at: Optional[float] = None  # unix epoch

    # SAIMemory message fields (used when layer in {main_cache, sub_cache})
    role: Optional[str] = None  # user / assistant / system / ...
    content: Optional[str] = None
    line_role: Optional[str] = None  # main_line / sub_line / meta_judgment / nested
    line_id: Optional[str] = None
    origin_track_id: Optional[str] = None
    scope: Optional[str] = None  # committed / discardable / volatile
    paired_action_text: Optional[str] = None

    # meta_judgment_log fields (used when layer == meta_judgment)
    # v0.15 独白 + /spell 方式に整合化済み (旧 judgment_action enum は廃止)
    judgment_thought: Optional[str] = None
    spells_emitted: Optional[str] = None  # JSON array of {name, args, result}
    trigger_type: Optional[str] = None
    trigger_context: Optional[str] = None
    committed_to_main_cache: Optional[bool] = None
    track_at_judgment_id: Optional[str] = None
    prompt_snapshot: Optional[str] = None

    # track_local_log fields (used when layer == track_local)
    log_kind: Optional[str] = None
    payload: Optional[str] = None
    source_line_id: Optional[str] = None
    track_id: Optional[str] = None  # track_local_log の track_id


class StorageLayersResponse(BaseModel):
    summary: List[StorageLayerStat]
    items: List[StorageLayerEntry]
    total_returned: int
    truncated: bool  # true when limit was reached


# -----------------------------------------------------------------------------
# Tracks Viewer Models (Intent A v0.14, Intent B v0.11 — action_track 一覧表示)
# -----------------------------------------------------------------------------

class TrackItem(BaseModel):
    """One ActionTrack row, with metadata JSON parsed for the UI."""
    track_id: str
    persona_id: str
    title: Optional[str] = None
    track_type: str
    is_persistent: bool
    output_target: str
    status: str  # running / alert / pending / unstarted / completed / aborted
    is_forgotten: bool
    intent: Optional[str] = None
    track_metadata: Optional[dict] = None  # parsed JSON, None if not set
    last_active_at: Optional[float] = None
    last_message_at: Optional[float] = None  # MAX(messages.created_at) WHERE origin_track_id=track_id
    created_at: Optional[float] = None
    completed_at: Optional[float] = None
    aborted_at: Optional[float] = None


class TracksStatusCount(BaseModel):
    status: str
    count: int


class TracksResponse(BaseModel):
    items: List[TrackItem]
    total: int
    status_counts: List[TracksStatusCount]
