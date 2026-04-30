from sqlalchemy import (
    Column,
    Index,
    Integer,
    String,
    DateTime,
    ForeignKey,
    Boolean,
    UniqueConstraint,
    func,
    Text,
    Float,
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()

# --- テーブルモデル定義 ---

class User(Base):
    __tablename__ = "user"
    USERID = Column(Integer, primary_key=True)
    PASSWORD = Column(String(32), nullable=False)
    USERNAME = Column(String(32), nullable=False)
    MAILADDRESS = Column(String(64))
    LOGGED_IN = Column(Boolean, default=False, nullable=False)
    CURRENT_CITYID = Column(Integer, ForeignKey("city.CITYID"), nullable=True)
    CURRENT_BUILDINGID = Column(String(255), ForeignKey("building.BUILDINGID"), nullable=True)
    AVATAR_IMAGE = Column(String(255))

class AI(Base):
    __tablename__ = "ai"
    AIID = Column(String(255), primary_key=True)  # persona_id
    HOME_CITYID = Column(Integer, ForeignKey("city.CITYID"), nullable=False)
    AINAME = Column(String(32), nullable=False)
    SYSTEMPROMPT = Column(String(4096), default="", nullable=False)
    DESCRIPTION = Column(String(1024), default="", nullable=False)
    AVATAR_IMAGE = Column(String(255))  # UI icon
    APPEARANCE_IMAGE_PATH = Column(String(512), nullable=True)  # Persona appearance image for LLM visual context
    EMOTION = Column(String(1024))  # JSON形式で保存
    AUTO_COUNT = Column(Integer, default=0, nullable=False)
    LAST_AUTO_PROMPT_TIMES = Column(String(2048)) # JSON形式で保存
    IS_DISPATCHED = Column(Boolean, default=False, nullable=False)
    DEFAULT_MODEL = Column(String(255), nullable=True)
    LIGHTWEIGHT_MODEL = Column(String(255), nullable=True)
    LIGHTWEIGHT_VISION_MODEL = Column(String(255), nullable=True)
    VISION_MODEL = Column(String(255), nullable=True)
    PRIVATE_ROOM_ID = Column(String(255), ForeignKey("building.BUILDINGID"), nullable=True)
    CHRONICLE_ENABLED = Column(Boolean, default=True, nullable=False)  # Per-persona Chronicle auto-generation toggle
    MEMORY_WEAVE_CONTEXT = Column(Boolean, default=True, nullable=False)  # Per-persona Memory Weave context injection toggle
    SPELL_ENABLED = Column(Boolean, default=False, nullable=False)  # Per-persona spell system toggle
    METABOLISM_ANCHORS = Column(Text, nullable=True)  # JSON: per-model anchor state {"model": {"anchor_id": "...", "updated_at": "..."}}
    # Cognitive model (Intent A v0.9 / Intent B v0.6): ACTIVITY_STATE 4-state
    # 'Stop' (機能停止) / 'Sleep' (寝てる、ユーザー発言で起きる) /
    # 'Idle' (起きてるが自発的には行動しない) / 'Active' (活発に自律稼働)
    ACTIVITY_STATE = Column(String(32), default='Idle', nullable=False)
    # When TRUE, an Idle persona transitions to Sleep automatically once the heavyweight
    # model cache TTL has elapsed. Protects against runaway API costs on idle personas.
    SLEEP_ON_CACHE_EXPIRE = Column(Boolean, default=True, nullable=False)
    # Last SAIVerse version this persona was successfully running on. NULL means
    # the persona predates the version-aware system (treat as v0.3.0 or earlier).
    LAST_KNOWN_VERSION = Column(String(64), nullable=True)

class Building(Base):
    __tablename__ = "building"
    CITYID = Column(Integer, ForeignKey("city.CITYID"), nullable=False)
    BUILDINGID = Column(String(255), primary_key=True)  # building_id
    BUILDINGNAME = Column(String(32), nullable=False)
    CAPACITY = Column(Integer, default=1, nullable=False)
    SYSTEM_INSTRUCTION = Column(String(4096), default="", nullable=False)
    ENTRY_PROMPT = Column(String(4096), default="", nullable=False)
    AUTO_PROMPT = Column(String(4096), default="", nullable=False)
    DESCRIPTION = Column(String(1024), default="", nullable=False)
    AUTO_INTERVAL_SEC = Column(Integer, default=10, nullable=False)
    IMAGE_PATH = Column(String(512), nullable=True)  # Building interior image for LLM visual context
    EXTRA_PROMPT_FILES = Column(Text, nullable=True)  # JSON: ["body_control.txt", "other.txt"]
    __table_args__ = (UniqueConstraint('CITYID', 'BUILDINGNAME', name='uq_city_building_name'),)


class City(Base):
    __tablename__ = "city"
    USERID = Column(Integer, ForeignKey("user.USERID"), nullable=False)
    CITYID = Column(Integer, primary_key=True, autoincrement=True)
    CITYNAME = Column(String(32), nullable=False)
    DESCRIPTION = Column(String(1024), default="", nullable=False)
    TIMEZONE = Column(String(64), default="UTC", nullable=False)
    UI_PORT = Column(Integer, nullable=False)
    API_PORT = Column(Integer, nullable=False)
    START_IN_ONLINE_MODE = Column(Boolean, default=False, nullable=False)
    HOST_AVATAR_IMAGE = Column(String(255))
    # Last SAIVerse version this city was successfully running on. NULL means
    # the city predates the version-aware system (treat as v0.3.0 or earlier).
    LAST_KNOWN_VERSION = Column(String(64), nullable=True)
    __table_args__ = (UniqueConstraint('USERID', 'CITYNAME', name='uq_user_city_name'), UniqueConstraint('UI_PORT', name='uq_ui_port'), UniqueConstraint('API_PORT', name='uq_api_port'))

class Tool(Base):
    __tablename__ = "tool"
    TOOLID = Column(Integer, primary_key=True)
    TOOLNAME = Column(String(32), nullable=False, unique=True)
    MODULE_PATH = Column(String(255), nullable=False, unique=True)
    FUNCTION_NAME = Column(String(255), nullable=False, default="")
    DESCRIPTION = Column(String(1024), default="", nullable=False)

class UserAiLink(Base):
    __tablename__ = "user_ai_link"
    USERID = Column(Integer, ForeignKey("user.USERID"), primary_key=True)
    AIID = Column(String(255), ForeignKey("ai.AIID"), primary_key=True)

class AiToolLink(Base):
    __tablename__ = "ai_tool_link"
    AIID = Column(String(255), ForeignKey("ai.AIID"), primary_key=True)
    TOOLID = Column(Integer, ForeignKey("tool.TOOLID"), primary_key=True)

class BuildingToolLink(Base):
    __tablename__ = "building_tool_link"
    BUILDINGID = Column(String(255), ForeignKey("building.BUILDINGID"), primary_key=True)
    TOOLID = Column(Integer, ForeignKey("tool.TOOLID"), primary_key=True)

class BuildingOccupancyLog(Base):
    __tablename__ = "building_occupancy_log"
    ID = Column(Integer, primary_key=True, autoincrement=True)
    CITYID = Column(Integer, ForeignKey("city.CITYID"), nullable=False)
    BUILDINGID = Column(String(255), ForeignKey("building.BUILDINGID"), nullable=False)
    AIID = Column(String(255), ForeignKey("ai.AIID"), nullable=False)
    ENTRY_TIMESTAMP = Column(DateTime, nullable=False)
    EXIT_TIMESTAMP = Column(DateTime)

class ThinkingRequest(Base):
    __tablename__ = "thinking_request"
    id = Column(Integer, primary_key=True, autoincrement=True)
    request_id = Column(String(36), nullable=False, unique=True)
    city_id = Column(Integer, ForeignKey("city.CITYID"), nullable=False)
    persona_id = Column(String(255), ForeignKey("ai.AIID"), nullable=False)
    request_context_json = Column(String, nullable=False)
    response_text = Column(String)
    status = Column(String(32), default='pending', nullable=False) # pending, processed, error
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

class VisitingAI(Base):
    __tablename__ = "visiting_ai"
    id = Column(Integer, primary_key=True, autoincrement=True)
    city_id = Column(Integer, ForeignKey("city.CITYID"), nullable=False)
    persona_id = Column(String(255), nullable=False)
    profile_json = Column(String, nullable=False) # JSON文字列でプロファイルを保存
    status = Column(String(32), default='requested', nullable=False) # requested, accepted, rejected
    reason = Column(String(255)) # 拒否された場合の理由など
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    __table_args__ = (UniqueConstraint('city_id', 'persona_id', name='uq_visiting_city_persona'),)


class Playbook(Base):
    __tablename__ = "playbooks"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), unique=True, nullable=False)
    display_name = Column(String(255), nullable=True)  # Human-readable display name for UI
    description = Column(String(1024), default="", nullable=False)
    scope = Column(String(32), nullable=False, default="public")  # public/personal/building
    created_by_persona_id = Column(String(255), ForeignKey("ai.AIID"), nullable=True)
    building_id = Column(String(255), ForeignKey("building.BUILDINGID"), nullable=True)
    schema_json = Column(Text, nullable=False)
    nodes_json = Column(Text, nullable=False)
    router_callable = Column(Boolean, nullable=False, default=False)  # Can be called from router
    user_selectable = Column(Boolean, nullable=False, default=False)  # Can be selected by user in UI
    dev_only = Column(Boolean, nullable=False, default=False)  # Only available when developer mode is enabled
    required_credentials = Column(Text, nullable=True, default=None)  # JSON list of required credential types e.g. '["x"]'
    source_file = Column(String(512), nullable=True, default=None)  # Relative path from project root (file-originated playbooks)
    source_hash = Column(String(64), nullable=True, default=None)   # SHA256 of canonical nodes_json (for diff detection)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class PlaybookPermission(Base):
    """City-scoped playbook execution permission levels.

    Stored separately from Playbook table so force-updating playbooks
    (via import_all_playbooks.py --force) does not overwrite user preferences.
    """
    __tablename__ = "playbook_permission"
    id = Column(Integer, primary_key=True, autoincrement=True)
    CITYID = Column(Integer, ForeignKey("city.CITYID"), nullable=False)
    playbook_name = Column(String(255), nullable=False)
    permission_level = Column(String(32), nullable=False, default="ask_every_time")
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    __table_args__ = (
        UniqueConstraint("CITYID", "playbook_name", name="uq_city_playbook_perm"),
    )


class Blueprint(Base):
    __tablename__ = "blueprint"
    BLUEPRINT_ID = Column(Integer, primary_key=True, autoincrement=True)
    CITYID = Column(Integer, ForeignKey("city.CITYID"), nullable=False)
    NAME = Column(String(255), nullable=False)
    ENTITY_TYPE = Column(String(50), default='ai', nullable=False) # 例: 'ai', 'drone'
    DESCRIPTION = Column(String(1024), default="", nullable=False)
    BASE_SYSTEM_PROMPT = Column(String(4096), default="", nullable=False)
    BASE_AVATAR = Column(String(255), nullable=True)
    __table_args__ = (UniqueConstraint('CITYID', 'NAME', name='uq_city_blueprint_name'),)


class Item(Base):
    __tablename__ = "item"
    ITEM_ID = Column(String(36), primary_key=True)
    NAME = Column(String(255), nullable=False)
    TYPE = Column(String(64), nullable=False, default="object")
    DESCRIPTION = Column(String(2048), default="", nullable=False)
    FILE_PATH = Column(String(512), nullable=True)
    STATE_JSON = Column(String, nullable=True)
    CREATOR_ID = Column(String(255), nullable=True)
    SOURCE_CONTEXT = Column(String, nullable=True)
    CREATED_AT = Column(DateTime, server_default=func.now(), nullable=False)
    UPDATED_AT = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class ItemLocation(Base):
    __tablename__ = "item_location"
    LOCATION_ID = Column(Integer, primary_key=True, autoincrement=True)
    ITEM_ID = Column(String(36), ForeignKey("item.ITEM_ID"), nullable=False)
    OWNER_KIND = Column(String(32), nullable=False)  # building / persona / world / bag
    OWNER_ID = Column(String(255), nullable=False)
    SLOT_NUMBER = Column(Integer, nullable=True)
    UPDATED_AT = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    __table_args__ = (
        UniqueConstraint("ITEM_ID", name="uq_item_location_item_id"),
        UniqueConstraint("OWNER_KIND", "OWNER_ID", "SLOT_NUMBER", name="uq_item_slot"),
    )


class PersonaEventLog(Base):
    __tablename__ = "persona_event_log"
    EVENT_ID = Column(Integer, primary_key=True, autoincrement=True)
    PERSONA_ID = Column(String(255), ForeignKey("ai.AIID"), nullable=False)
    CREATED_AT = Column(DateTime, server_default=func.now(), nullable=False)
    CONTENT = Column(String, nullable=False)
    STATUS = Column(String(32), default="pending", nullable=False)  # pending / archived
    EVENT_TYPE = Column(String(64), nullable=True)  # "x_mention", "switchbot_open", etc.
    PAYLOAD = Column(Text, nullable=True)  # JSON structured data


class PersonaSchedule(Base):
    __tablename__ = "persona_schedule"
    SCHEDULE_ID = Column(Integer, primary_key=True, autoincrement=True)
    PERSONA_ID = Column(String(255), ForeignKey("ai.AIID"), nullable=False)
    SCHEDULE_TYPE = Column(String(32), nullable=False)  # "periodic", "oneshot", "interval"
    META_PLAYBOOK = Column(String(255), nullable=False)  # メタプレイブック名
    ENABLED = Column(Boolean, default=True, nullable=False)
    DESCRIPTION = Column(String(512), default="", nullable=False)
    PRIORITY = Column(Integer, default=0, nullable=False)  # 優先度（大きいほど優先）

    # 定期スケジュール用 (periodic)
    DAYS_OF_WEEK = Column(String(255), nullable=True)  # JSON: [0,1,2,3,4,5,6] or null (毎日)
    TIME_OF_DAY = Column(String(8), nullable=True)  # "09:00" 形式

    # 単発スケジュール用 (oneshot)
    SCHEDULED_DATETIME = Column(DateTime, nullable=True)
    COMPLETED = Column(Boolean, default=False, nullable=False)

    # 恒常スケジュール用 (interval)
    INTERVAL_SECONDS = Column(Integer, nullable=True)
    LAST_EXECUTED_AT = Column(DateTime, nullable=True)

    # Playbook parameters (JSON)
    PLAYBOOK_PARAMS = Column(Text, nullable=True)  # JSON string: {"selected_playbook": "xxx", ...}

    CREATED_AT = Column(DateTime, server_default=func.now(), nullable=False)
    UPDATED_AT = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class PhenomenonRule(Base):
    """フェノメノン（現象）発火ルールを管理するテーブル。

    トリガー条件に一致したときに、指定されたフェノメノンを引数付きで発火させる。
    """
    __tablename__ = "phenomenon_rule"
    RULE_ID = Column(Integer, primary_key=True, autoincrement=True)

    # トリガー条件
    TRIGGER_TYPE = Column(String(64), nullable=False)  # e.g., "persona_speech", "persona_move", "server_start"
    CONDITION_JSON = Column(Text, nullable=True)  # JSON: 条件設定 {"persona_id": "air", "to_building": "user_room"}

    # 発火するフェノメノン
    PHENOMENON_NAME = Column(String(255), nullable=False)  # レジストリ内の名前
    ARGUMENT_MAPPING_JSON = Column(Text, nullable=True)  # JSON: 引数マッピング {"actor": "$trigger.persona_id"}

    # メタデータ
    ENABLED = Column(Boolean, default=True, nullable=False)
    PRIORITY = Column(Integer, default=0, nullable=False)  # 大きいほど優先
    DESCRIPTION = Column(String(1024), default="", nullable=False)
    CREATED_AT = Column(DateTime, server_default=func.now(), nullable=False)
    UPDATED_AT = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class LLMUsageLog(Base):
    """LLM API使用量ログテーブル。

    各LLM呼び出しのトークン使用量とコストを記録する。
    """
    __tablename__ = "llm_usage_log"
    ID = Column(Integer, primary_key=True, autoincrement=True)
    TIMESTAMP = Column(DateTime, server_default=func.now(), nullable=False)
    PERSONA_ID = Column(String(255), nullable=True)  # Null = System/User call
    BUILDING_ID = Column(String(255), nullable=True)
    MODEL_ID = Column(String(255), nullable=False)
    INPUT_TOKENS = Column(Integer, nullable=False)
    OUTPUT_TOKENS = Column(Integer, nullable=False)
    CACHED_TOKENS = Column(Integer, nullable=True, default=0)  # Tokens served from cache
    COST_USD = Column(Float, nullable=True)  # Calculated cost in USD
    NODE_TYPE = Column(String(64), nullable=True)  # llm, router, tool_detection, etc.
    PLAYBOOK_NAME = Column(String(255), nullable=True)
    CATEGORY = Column(String(64), nullable=True)  # persona_speak, memory_weave_generate, etc.


class UserSettings(Base):
    """ユーザー設定テーブル。

    チュートリアル完了状態などのユーザー固有の設定を管理する。
    """
    __tablename__ = "user_settings"
    USERID = Column(Integer, ForeignKey("user.USERID"), primary_key=True)
    TUTORIAL_COMPLETED = Column(Boolean, default=False, nullable=False)
    TUTORIAL_COMPLETED_AT = Column(DateTime, nullable=True)
    LAST_TUTORIAL_VERSION = Column(Integer, default=1, nullable=False)
    SELECTED_META_PLAYBOOK = Column(String(255), nullable=True)  # User's preferred meta playbook
    FAVORITE_MODELS = Column(Text, nullable=True)  # JSON array of favorite model IDs


class AddonConfig(Base):
    """アドオンのグローバル設定テーブル。

    アドオンの有効/無効状態とデフォルトパラメータを管理する。
    addon_name は expansion_data/ 下のディレクトリ名と一致する。
    """
    __tablename__ = "addon_config"
    addon_name = Column(String(255), primary_key=True)
    is_enabled = Column(Boolean, default=True, nullable=False)
    params_json = Column(Text, nullable=True)  # JSON: アドオンのグローバルパラメータ
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class AddonPersonaConfig(Base):
    """ペルソナごとのアドオンパラメータ上書きテーブル。

    アドオンのパラメータをペルソナ単位で上書きする。
    設定がない場合は AddonConfig のデフォルト値が使われる。
    """
    __tablename__ = "addon_persona_config"
    id = Column(Integer, primary_key=True, autoincrement=True)
    addon_name = Column(String(255), nullable=False)
    persona_id = Column(String(255), ForeignKey("ai.AIID"), nullable=False)
    params_json = Column(Text, nullable=False)  # JSON: ペルソナ固有のパラメータ上書き
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    __table_args__ = (
        UniqueConstraint("addon_name", "persona_id", name="uq_addon_persona"),
    )


class PersonaBuildingState(Base):
    """ペルソナごとのBuilding状態スナップショット（Dynamic State Sync用）。

    A状態（ベースライン）とB状態（最終通知済み状態）を永続化する。
    """
    __tablename__ = "persona_building_state"
    PERSONA_ID = Column(String(255), ForeignKey("ai.AIID"), primary_key=True)
    BUILDING_ID = Column(String(255), ForeignKey("building.BUILDINGID"), primary_key=True)
    BASELINE_JSON = Column(Text, nullable=True)       # A: Metabolism/入室時のスナップショット
    LAST_NOTIFIED_JSON = Column(Text, nullable=True)  # B: 最後にLLMへ通知した状態
    UPDATED_AT = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class AddonMessageMetadata(Base):
    """メッセージに紐付くアドオンメタデータテーブル。

    アドオンがチャットメッセージに対してメタデータを付与するために使用する。
    例: TTS アドオンが生成した音声ファイルのパスを message_id に紐付ける。
    アドオンが無効でもデータは保持される（無効時は単に参照されないだけ）。
    """
    __tablename__ = "addon_message_metadata"
    id = Column(Integer, primary_key=True, autoincrement=True)
    message_id = Column(String(255), nullable=False)
    addon_name = Column(String(100), nullable=False)
    key = Column(String(100), nullable=False)
    value = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    __table_args__ = (
        UniqueConstraint("message_id", "addon_name", "key", name="uq_addon_msg_meta"),
    )


# ============================================================================
# Cognitive model: Action Tracks and Notes
# Intent A: docs/intent/persona_cognitive_model.md (v0.9)
# Intent B: docs/intent/persona_action_tracks.md (v0.6)
# ============================================================================

class ActionTrack(Base):
    """行動 Track: ペルソナの行動制御単位。

    Track はペルソナが進行中の作業文脈を表す。同時にアクティブ (running) なものは
    1 ペルソナにつき 1 本のみ。永続 Track (is_persistent=true) は対ユーザー会話 Track
    と交流 Track が該当し、completed/aborted への遷移を許さない。

    詳細: docs/intent/persona_action_tracks.md
    """
    __tablename__ = "action_track"
    track_id = Column(String(36), primary_key=True)  # UUID
    persona_id = Column(String(255), ForeignKey("ai.AIID"), nullable=False)
    title = Column(String(255), nullable=True)
    track_type = Column(String(64), nullable=False)
    # user_conversation / social / autonomous / waiting / external / ...
    is_persistent = Column(Boolean, default=False, nullable=False)
    # Output target: 'none' / 'building:current' / 'external:<channel>:<address>'
    output_target = Column(String(255), default='none', nullable=False)
    status = Column(String(32), default='unstarted', nullable=False)
    # running / alert / pending / waiting / unstarted / completed / aborted
    is_forgotten = Column(Boolean, default=False, nullable=False)
    intent = Column(Text, nullable=True)
    track_metadata = Column(Text, nullable=True)
    # JSON: target identifiers (user_id, persona_id), external refs, etc.
    pause_summary = Column(Text, nullable=True)
    pause_summary_updated_at = Column(DateTime, nullable=True)
    last_active_at = Column(DateTime, nullable=True)
    waiting_for = Column(Text, nullable=True)
    # JSON: {"type": "user_response" | "persona_response" | "kitchen_completion" | ...}
    waiting_timeout_at = Column(DateTime, nullable=True)  # NULL = no timeout
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    completed_at = Column(DateTime, nullable=True)  # always NULL for is_persistent=true
    aborted_at = Column(DateTime, nullable=True)    # always NULL for is_persistent=true
    __table_args__ = (
        Index("idx_action_track_persona_status", "persona_id", "status", "is_forgotten"),
        Index("idx_action_track_last_active", "persona_id", "last_active_at"),
        Index("idx_action_track_waiting_timeout", "waiting_timeout_at"),
        Index("idx_action_track_persistent", "persona_id", "is_persistent", "track_type"),
    )


class Note(Base):
    """Note: 関心の固まり (恒久的な資産)。

    Memopedia ページとメッセージ群を束ねる「スクラップブック」。
    type は person / project / vocation の 3 種のみ (Intent A v0.6 で確定)。
    Track が close されても Note は残り続ける。
    """
    __tablename__ = "note"
    note_id = Column(String(36), primary_key=True)  # UUID
    persona_id = Column(String(255), ForeignKey("ai.AIID"), nullable=False)
    title = Column(String(255), nullable=False)
    note_type = Column(String(32), nullable=False)  # person / project / vocation
    description = Column(Text, nullable=True)
    note_metadata = Column(Text, nullable=True)
    # JSON: target persona_id (for person), deadline (for project), etc.
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    last_opened_at = Column(DateTime, nullable=True)
    closed_at = Column(DateTime, nullable=True)  # used when project completes
    __table_args__ = (
        Index("idx_note_persona_type", "persona_id", "note_type", "is_active"),
    )


class NotePage(Base):
    """Note と Memopedia ページの関連 (多対多)。

    page_id は SAIMemory 側 (memopedia_pages) に存在するため、メインDBからは
    外部キー制約をかけない。
    """
    __tablename__ = "note_page"
    note_id = Column(String(36), ForeignKey("note.note_id"), primary_key=True)
    page_id = Column(String(255), primary_key=True)
    __table_args__ = (
        Index("idx_note_page_page", "page_id"),
    )


class NoteMessage(Base):
    """Note とメッセージの関連 (多対多)。

    message_id は SAIMemory 側に存在するため、メインDBからは外部キー制約を
    かけない。同一メッセージが複数 Note に属することができ、3 人会話のメッセージ
    重複問題はこの構造で解決される (Intent B v0.3)。
    """
    __tablename__ = "note_message"
    note_id = Column(String(36), ForeignKey("note.note_id"), primary_key=True)
    message_id = Column(String(255), primary_key=True)
    added_at = Column(DateTime, server_default=func.now(), nullable=False)
    auto_added = Column(Boolean, default=False, nullable=False)
    # auto: derived from audience metadata; manual: persona explicitly added
    __table_args__ = (
        Index("idx_note_message_msg", "message_id"),
    )


class TrackOpenNote(Base):
    """行動 Track と「開いている Note」の関連 (多対多)。

    Track ごとに複数の Note が開かれる。Track が pending になっても open は維持
    され、再 active 化時に Note の差分が再開コンテキストに挿入される。
    """
    __tablename__ = "track_open_note"
    track_id = Column(String(36), ForeignKey("action_track.track_id"), primary_key=True)
    note_id = Column(String(36), ForeignKey("note.note_id"), primary_key=True)
    opened_at = Column(DateTime, server_default=func.now(), nullable=False)
    __table_args__ = (
        Index("idx_track_open_note_track", "track_id"),
        Index("idx_track_open_note_note", "note_id"),
    )


# ============================================================================
# 7-layer storage model (Intent A v0.14, Intent B v0.11): metadata stores
# ============================================================================

class MetaJudgmentLog(Base):
    """[1] メタ判断ログ領域: メタ判断の全履歴を独立保存する。

    メタ判断は Track 内メインラインの一瞬の分岐として動く (Intent A v0.15
    独白 + /spell 方式)。Track 続行時は分岐ターンをメインキャッシュには残さない
    が、本テーブルには必ず保存する。次のメタ判断時に「過去にこう判断した」を
    参考情報として動的注入する時系列ログ。

    Track 移動時の分岐は committed_to_main_cache=TRUE になり、メインキャッシュ
    にも来歴として残る (= ペルソナの自己認識として「移動の理由」が見える)。

    詳細: docs/intent/persona_cognition/02_mechanics.md
    """
    __tablename__ = "meta_judgment_log"
    judgment_id = Column(String(36), primary_key=True)  # UUID
    persona_id = Column(String(255), ForeignKey("ai.AIID"), nullable=False)
    judged_at = Column(DateTime, server_default=func.now(), nullable=False)
    track_at_judgment_id = Column(String(36), nullable=True)
    # Active Track at the moment of judgment (NULL if persona was idle)
    trigger_type = Column(String(32), nullable=False)
    # 'periodic_tick' / 'alert' / 'pulse_completion' / ...
    trigger_context = Column(Text, nullable=True)  # JSON: alert track_id, reason, etc.
    prompt_snapshot = Column(Text, nullable=True)
    # Summarized prompt used at judgment time (for debugging)
    judgment_thought = Column(Text, nullable=True)
    # ペルソナの独白テキスト (LLM 応答全体の生テキスト、複数ラウンドある場合は連結)
    spells_emitted = Column(Text, nullable=True)
    # JSON array of {"name": str, "args": dict, "result": str}.
    # 判断ループ中に発動された /spell とその実行結果をまとめて保存。
    committed_to_main_cache = Column(Boolean, default=False, nullable=False)
    # TRUE if this judgment was committed to the main cache (= track switch happened)
    __table_args__ = (
        Index("idx_meta_judgment_persona_time", "persona_id", "judged_at"),
        Index("idx_meta_judgment_track", "track_at_judgment_id"),
    )


class TrackLocalLog(Base):
    """[5] Track ローカルログ: Track 内のイベント・モニタログ・起点サブの中間ステップ。

    Track 内では参照できるが、想起対象 ([6] SAIMemory) には乗らない。Track 種別
    ごとに固有のイベントを受ける場 (入室イベント → 交流 Track のローカルログ、
    Chronicle 完了 → 記憶整理 Track のローカルログ、Track 削除通知 → メタ用
    特殊 Track のローカルログ等)。

    visible_to_other_tracks は将来の Track 越境参照用の予約フィールド (例: ユーザー
    会話中に「さっきエイドが入室したよね」と話題化する経路)。v0.11 では FALSE
    固定、運用機構は後送り。

    詳細: docs/intent/persona_action_tracks.md (v0.11)
    """
    __tablename__ = "track_local_log"
    log_id = Column(String(36), primary_key=True)  # UUID
    track_id = Column(String(36), ForeignKey("action_track.track_id"), nullable=False)
    occurred_at = Column(DateTime, server_default=func.now(), nullable=False)
    log_kind = Column(String(64), nullable=False)
    # 'event_message' / 'monitor_signal' / 'sub_step' / 'tool_trace' / ...
    payload = Column(Text, nullable=True)  # JSON: event details, monitor values, sub-step info, etc.
    source_line_id = Column(String(36), nullable=True)
    # Originating line ID (NULL = Track-level event, not from a specific line)
    visible_to_other_tracks = Column(Boolean, default=False, nullable=False)
    # Reserved for future Track-cross-reference mechanism (always FALSE in v0.11)
    __table_args__ = (
        Index("idx_track_local_log_track_time", "track_id", "occurred_at"),
        Index("idx_track_local_log_kind", "track_id", "log_kind", "occurred_at"),
    )

