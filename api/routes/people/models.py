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

    ⚠ v1 メタ判断の退役 (track_retirement.md §7.4) で読み手を失った休眠キーが
    混ざっている (max_retries / retry_backoff_seconds / force_fail)。既存行が
    round-trip で消えないよう受け口は残すが、**編集 UI は 2026-08-14 に削除した**
    — 新しい読み手を生やす前に、そのキーが何を意味するかを決め直すこと。
    """
    cache_threshold_ratio: Optional[float] = None    # 0.0–1.0, default 0.3
    max_retries: Optional[int] = None                # (休眠) 旧 v1 リトライ回数
    retry_backoff_seconds: Optional[int] = None      # (休眠) 旧 v1 リトライ待機秒数
    periodic_interval_minutes: Optional[int] = None  # default 50 (watchdog の間隔)
    keep_cache_alive: Optional[bool] = None          # default True (TTL 接近で温め直す)
    # ライフビュー「作業のテンポ」: 自律 Track の Pulse 間隔 (秒) のペルソナ既定値。
    # update_ai は META_JUDGMENT_CONFIG を丸ごと置換するため、ここに定義しないと
    # SettingsModal 保存時にキーが消える (persona_activity_view.md §7)。
    autonomous_pulse_interval_seconds: Optional[int] = None  # default 30
    # (休眠) 旧 v1 開発者モードのデバッグフラグ。読み手は退役済み。
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
    autonomy_enabled: bool = True
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
    autonomy_enabled: Optional[bool] = None
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
    # P4-c: vividness は廃止。フィールドを除去した。
    is_trunk: Optional[bool] = None


class CreateMemopediaPageRequest(BaseModel):
    parent_id: str
    title: str
    summary: str = ""
    content: str = ""
    keywords: Optional[List[str]] = None
    # P4-c: vividness は廃止。
    is_trunk: bool = False


class SetTrunkRequest(BaseModel):
    is_trunk: bool


class SetImportantRequest(BaseModel):
    is_important: bool


class DeskPageRequest(BaseModel):
    open: bool  # True = 机に開く、False = 机から閉じる


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
    # 省略可。未指定 / 空文字なら create_schedule が既定 Playbook
    # (ScheduleManager.DEFAULT_META_PLAYBOOK) へ正規化する。どの Playbook で
    # 動くかはアラームの作成者に選ばせない (2026-09-01 裁定) ので、UI は送らない。
    meta_playbook: Optional[str] = None
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


# タスク管理 API の直列化モデル (TaskStep / TaskRecordModel / CreateTaskRequest /
# UpdateTaskStatusRequest) は、束 6c (2026-08-22) でタスク管理 UI とルート
# (api/routes/people/tasks.py) を撤去したときに読み手ごと消えた
# (autonomous_behavior_v3.md §11「運転 UI は隠す」)。「やること」の器は v3 で
# ルーチン / タスク帳 / 手帳の三つに分かれ、タスク帳の読み書きは
# saiverse/task_book.py が素の dict で行う。


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
    # 一覧は常に全件を返す (2026-08-05, docs/issues/arasuji_modal_500_limit_truncation.md)。
    # 件数上限とその通知フィールド (total_available / hidden_oldest) は撤去した。
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
    """Chronicle生成リクエスト。

    2026-07-29 (arasuji_levels.md §13 裁定4): 手動生成は run_manual_compaction
    (残す量より古い側だけを畳む) へ合流し、範囲・モデル・出力に関わる全フィールドが
    廃止された。現行 frontend は空 body を送る。旧 frontend からのリクエストを
    422 にしないため、全フィールドを受理して無視する (deprecated)。

    2026-08-31 (arasuji_levels.md §16): ``mode`` を追加。既定 "compaction" は
    従来どおり窓の畳み (run_manual_compaction)。"repair" は被覆補修
    (run_coverage_repair) — 止め線より古い未被覆の編纂対象を一次あらすじにする。
    """
    # "compaction" = 窓の畳み (従来) / "repair" = 被覆補修 (§16)
    mode: str = "compaction"
    # repair モードの時点ずれの歯止め (任意): UI が見積もり (cost-estimate) で
    # ユーザーに見せた unprocessed_messages。実行直前の再計算がこれより
    # **増えて**いたら、承認した範囲より広い編纂 (課金) になるので実行せず
    # estimate_stale で返す。減る方向 (安くなる) は嘘にならないので走ってよい。
    confirmed_unprocessed_messages: Optional[int] = None
    max_messages: int = 500  # deprecated (§13: 範囲は残す量が決める)
    batch_size: int = 20  # deprecated (W4: チャンク分割は episode 境界とサイズ束ね)
    consolidation_size: int = 10  # deprecated (W4)
    model: Optional[str] = None  # deprecated (§13: persona の MEMORY_WEAVE_MODEL 固定)
    with_memopedia: bool = False  # deprecated (§13: Fragment 抽出は編纂に常時相乗り)
    include_timestamp: bool = True  # deprecated (§13: executor 既定に従う)


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
    # 再開位置。時刻だけだと同じ秒のメッセージの順序を表せないので、行番号
    # (rowid) と対で持つ。前回の結果の last_message_timestamp /
    # last_message_rowid をそのまま渡す
    start_after: float = 0
    start_after_rowid: int = 0
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
    # W4: 固定バッチ廃止。旧 frontend 型互換のため 0 固定で残す (deprecated)。
    batch_size: int = 0
    currency: str = "USD"
    # 極小 run 吸収 (arasuji_tiny_run_absorption 裁定 6): 前回の補修/再編纂
    # ジョブが完了していない (上位あらすじの再生成が残っている)。frontend の
    # Chronicle タブの帯が「前回の処理が完了していません。再実行してください」
    # を併記するための印。
    repair_incomplete: bool = False


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


# NOTE: Tracks Viewer のモデル (TrackItem / TracksStatusCount / TracksResponse) は
# 2026-08-21 に API ルートごと退役した (track_retirement.md §2 住人 9)。フロントの
# 消費はゼロで、残っていた読み手は debug スクリプトだけだった。
