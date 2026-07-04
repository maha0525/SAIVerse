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


def _current_saiverse_version():
    """新規 City / AI 行に「作成時点の SAIVerse バージョン」を刻む column default。

    起動時のバージョン認識アップグレード (saiverse/upgrade.py) は
    ``LAST_KNOWN_VERSION IS NULL`` を「version-aware 以前 (v0.0.0)」とみなして
    全ハンドラを適用する。この default が無いと、実行中のバージョンで新規作成
    したペルソナも NULL のまま残り、次回起動時にアップグレードハンドラ (SAIMemory
    への「アップデート検知」通知を含む) が走ってしまい、最初の会話にノイズが乗る。
    作成時点で現行バージョンを刻んでおけば ``current >= target`` で no-op になる。

    - ORM 経由の INSERT (ペルソナ/シティ作成・seed) にのみ効く。
    - マイグレーション (生 SQL INSERT / ALTER ADD COLUMN) は Python default を
      発火させないため、既存の pre-version-aware 行は NULL のまま保たれる
      (= 意図通りハンドラ対象に残る)。
    - バージョン取得に失敗した場合は None を返し、従来どおり NULL 扱いにする。

    循環 import (saiverse -> database.models) を避けるため関数内で遅延 import する。
    """
    try:
        from saiverse import __version__
        return __version__
    except Exception:
        return None


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
    AUDIO_MODEL = Column(String(255), nullable=True)
    VIDEO_MODEL = Column(String(255), nullable=True)
    MEMORY_WEAVE_MODEL = Column(String(255), nullable=True)
    PRIVATE_ROOM_ID = Column(String(255), ForeignKey("building.BUILDINGID"), nullable=True)
    CHRONICLE_ENABLED = Column(Boolean, default=True, nullable=False)  # Per-persona Chronicle auto-generation toggle
    # 自律/schedule Pulse でも確認ダイアログなしで General Chronicle 生成を実行するかの
    # per-persona トグル (docs/intent/memory_architecture_v2.md §6.3, Phase 0)。デフォルト ON:
    # 生成コストは軽量モデルで小さく、記憶が残らない実害の方が大きいため (2026-07-04 決定)。
    # ユーザー Pulse の確認ダイアログ挙動には無関係 (そちらは常に確認あり)。
    AUTONOMOUS_CHRONICLE_ENABLED = Column(Boolean, default=True, nullable=False)
    # 自動想起 (記憶アーキv2 ゾーン C, sea/auto_recall.py) の per-persona トグル。
    # False の場合、_maybe_inject_auto_recall (sea/runtime_context.py) は末尾注入を
    # 一切行わず粘着台帳もリセットする。手動想起 (recall_entry/recall_navigate スペル)
    # には影響しない。デフォルト ON (2026-07-04 決定)。
    AUTO_RECALL_ENABLED = Column(Boolean, default=True, nullable=False)
    MEMORY_WEAVE_CONTEXT = Column(Boolean, default=True, nullable=False)  # Per-persona Memory Weave context injection toggle
    MEMOPEDIA_INDEX_LIMIT = Column(Integer, nullable=True)  # Max pages per category in Memory Weave index (NULL → default 100)
    # Memopedia 索引の head 常時表示 (旧方式) を per-persona で復活させる後方互換トグル。
    # 記憶アーキv2 §7.1 (2026-07-04) で自動想起 (ゾーンC) + 深掘りスペルに一本化した際、
    # head への Memopedia 全ページ一覧の常時掲示は廃止された。本トグルを True にすると、
    # MemoryWeaveSection.capture が旧実装 (_get_memopedia_context) を呼び、Memopedia 索引を
    # 独立 user メッセージとして head に含める。メモ帳として能動的に Memopedia を使う
    # ユーザー向けの脱出経路。デフォルト OFF (トークン消費増のため既定は新方式に従う)。
    MEMOPEDIA_INDEX_ENABLED = Column(Boolean, default=False, nullable=False)
    # コア記憶 (記憶アーキv2 ゾーン A, docs/intent/memory_architecture_v2.md §5) の
    # 容量目安 (文字数)。ペルソナが自分で刻む恒常知識の合計文字数がこの値を超えると、
    # core_memory 系スペルの返り値に「整理を検討」の通知を添える (切り詰め・拒否は
    # 一切しない)。NULL → 既定 2000 字。コストと記憶量のトレードオフはユーザーに
    # よって異なるため per-persona で変更可能 (2026-07-04 決定)。
    CORE_MEMORY_CHAR_BUDGET = Column(Integer, nullable=True)
    SPELL_ENABLED = Column(Boolean, default=True, nullable=False)  # Per-persona spell system toggle (基幹機能化に伴い v0.3.0.dev3 でデフォルト ON 化)
    # Per-persona toggle for the realtime info section (現在時刻 / 前回発言時刻 / 空間情報)
    # injected by sea/runtime.py:_build_realtime_context. OFF にすると、その動的
    # コンテキストブロックをこのペルソナには一切送らない。夜になると時刻を気にして
    # 会話が成立しなくなるモデル向けの脱出経路 (docs/issues/realtime_info_current_time_toggle.md)。
    REALTIME_INFO_ENABLED = Column(Boolean, default=True, nullable=False)
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
    # 新規作成時は現行バージョンを刻む (_current_saiverse_version) ことで、作成直後の
    # ペルソナが次回起動でアップグレード通知を受けてしまうのを防ぐ。
    LAST_KNOWN_VERSION = Column(String(64), nullable=True, default=_current_saiverse_version)
    # Phase 4-e: Per-persona meta-judgment Pulse configuration (JSON).
    # Keys: cache_threshold_ratio (float 0-1), max_retries (int), retry_backoff_seconds (int).
    # NULL means use built-in defaults (see saiverse/meta_layer.py:_load_judgment_config).
    META_JUDGMENT_CONFIG = Column(Text, nullable=True)
    # 特殊ペルソナの役割。NULL=通常ペルソナ、'ruler'=Region RPG の GM ペルソナ
    # (召喚一覧から除外され、Region に常駐する)。設計: temp/region_rpg_intent.md §B
    PERSONA_ROLE = Column(String(32), nullable=True)
    # 2026-05-09: post_complete_behavior='wait_response' Track の自動 pause タイマー閾値 (分)。
    # ユーザー会話 Track の長期 idle で自律稼働が止まる症状の脱出経路として導入
    # (docs/intent/persona_cognition/handoff_2026-05-09.md §4)。NULL なら既定値 30 分。
    # 軽量モデルと重量級モデルで「自然な対話の間」が違うためペルソナ別に持つ。
    USER_CONV_TIMEOUT_MINUTES = Column(Integer, nullable=True)
    # 自律の源泉 (autonomous_desire.md §4): ペルソナの「生きる目的 / 趣味 / 仕事」。
    # 自律初回 ON 時に C 案聞き取りでドラフト→ユーザー確認→確定し保存する。
    # JSON {"purpose": str, "interests": [str], "vocations": [str]}。NULL = 未設定
    # (= 聞き取り未実施)。AUTONOMOUS / META プロンプトに常駐注入される (§4.5)。
    LIFE_PURPOSE = Column(Text, nullable=True)

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
    # 街マップ上の絶対座標 (world 座標系、px相当)。NULL なら擬似配置にフォールバック。
    MAP_X = Column(Float, nullable=True)
    MAP_Y = Column(Float, nullable=True)
    # 物理機体 (Stack-chan 等) に紐付く Vessel Building の識別子。NULL なら通常 Building、
    # 非NULL なら身体メタファーの Vessel Building として扱う (capacity=1 と組み合わせて
    # 「ペルソナが物理身体に降りている状態」を表現)。識別子の発番・解釈はアドオン側で行う。
    # 詳細: docs/intent/stackchan_vessel.md
    PHYSICAL_VESSEL_ID = Column(String(64), nullable=True)
    # 所属する Region または SubRegion。NULL なら無所属 (従来どおりの Building)。
    REGION_ID = Column(String(255), ForeignKey("region.REGION_ID"), nullable=True)
    __table_args__ = (UniqueConstraint('CITYID', 'BUILDINGNAME', name='uq_city_building_name'),)


class Region(Base):
    """Building の上位グルーピング。PARENT_REGION_ID の自己参照で 1 段の入れ子
    (トップ Region > SubRegion) を表現する。region_type='game' のトップ Region は
    Ruler (GM ペルソナ)・ゲーム進行状態を持つ。
    すべての Region / SubRegion は入口 Building (ENTRANCE_BUILDING_ID) を必ず持つ。
    入口は親スコープに属する (トップ Region の入口は REGION_ID なし = City 直属、
    SubRegion の入口は REGION_ID = 親 Region)。設計意図: docs/intent/region.md
    """
    __tablename__ = "region"
    REGION_ID = Column(String(255), primary_key=True)
    CITYID = Column(Integer, ForeignKey("city.CITYID"), nullable=False)
    # NULL=トップ Region、非NULL=SubRegion (入れ子は 1 段まで。アプリ層で強制)
    PARENT_REGION_ID = Column(String(255), ForeignKey("region.REGION_ID"), nullable=True)
    NAME = Column(String(64), nullable=False)
    DESCRIPTION = Column(String(2048), default="", nullable=False)
    REGION_TYPE = Column(String(32), default="generic", nullable=False)  # 'generic' | 'game'
    # RULER_ID は game トップ Region のみ。FK 制約は意図的に付けない:
    # region → building (入口) と building → region (所属) を両方 FK にすると
    # 循環依存になり Base.metadata.sorted_tables (migrate.py の差分検出) が壊れるため。
    RULER_ID = Column(String(255), nullable=True)              # ai.AIID (GM ペルソナ)
    # 入口 Building (旧 LOBBY_BUILDING_ID。migrate.py の KNOWN_COLUMN_RENAMES でリネーム)
    ENTRANCE_BUILDING_ID = Column(String(255), nullable=True)  # building.BUILDINGID
    MAP_BACKGROUND_IMAGE = Column(String(1024), nullable=True)  # Region 内マップの背景画像
    STATE_JSON = Column(Text, nullable=True)   # ゲーム進行状態 (phase/scene/参加者) / SubRegion ではメタデータ
    CONFIG_JSON = Column(Text, nullable=True)  # ルール設定・クリア条件・entry policy / SubRegion では隣接グラフ等
    CREATED_AT = Column(DateTime, server_default=func.now(), nullable=False)
    UPDATED_AT = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


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
    # 街マップビューの背景画像 (City単位で1枚)。NULL なら星空グラデのみ。
    MAP_BACKGROUND_IMAGE = Column(String(512), nullable=True)
    # Last SAIVerse version this city was successfully running on. NULL means
    # the city predates the version-aware system (treat as v0.3.0 or earlier).
    # 新規作成時は現行バージョンを刻む (AI と同様、作成直後のアップグレード通知を防ぐ)。
    LAST_KNOWN_VERSION = Column(String(64), nullable=True, default=_current_saiverse_version)
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
    COST_USD = Column(Float, nullable=True)  # Calculated cost in the model's native currency
    CURRENCY = Column(String(8), nullable=True, default="USD")  # ISO 4217 currency code
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


class LineHeadSnapshot(Base):
    """Cached Head Architecture: ライン単位の head snapshot 永続化。

    1 行 = 1 ペルソナ × 1 ライン (line_id)。SECTIONS_JSON は全 Section の
    snapshot を section.name -> serialized JSON の dict として保持する
    (= Section ごとの serialize_snapshot 出力をまとめた構造)。

    LAST_NOTIFIED_JSON は B 相当 (= 最後に末尾通知済みの各 Section snapshot)。
    diff チェック時に SECTIONS_JSON とではなくこちらと比較する。

    詳細: docs/intent/cached_head_architecture.md §3.3
    """
    __tablename__ = "line_head_snapshot"
    PERSONA_ID = Column(String(255), ForeignKey("ai.AIID"), primary_key=True)
    LINE_ID = Column(String(255), primary_key=True)
    LINE_ROLE = Column(String(32), nullable=False)
    MODEL_KEY = Column(String(128), nullable=False)
    SECTIONS_JSON = Column(Text, nullable=False)        # A: section.name -> serialized snapshot
    LAST_NOTIFIED_JSON = Column(Text, nullable=False)   # B: 最後に通知済みの各 Section snapshot
    SNAPSHOT_VERSION = Column(Integer, default=1, nullable=False)
    CAPTURED_AT = Column(DateTime, server_default=func.now(), nullable=False)
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
    short_id = Column(Integer, nullable=True)  # Per-persona sequential ID (t:1, t:2, ...)
    title = Column(String(255), nullable=True)
    track_type = Column(String(64), nullable=False)
    # user_conversation / social / autonomous / external / ...
    is_persistent = Column(Boolean, default=False, nullable=False)
    # Output target: 'none' / 'building:current' / 'external:<channel>:<address>'
    output_target = Column(String(255), default='none', nullable=False)
    status = Column(String(32), default='unstarted', nullable=False)
    # running / alert / pending / unstarted / completed / aborted
    is_forgotten = Column(Boolean, default=False, nullable=False)
    intent = Column(Text, nullable=True)
    # NOTE (unified_task_model.md §5 step 6, 2026-06-28): tasks_json カラムは
    # Task 一本化に伴い廃止した。Track 内タスクは統合 persona_task テーブル
    # (track_id バインド) へ移行済み。既存 DB の tasks_json は migrate.py の全書換
    # パスで列ドロップされ、post-migration フック
    # _migrate_track_tasks_json_to_persona_task が backup から persona_task へ
    # データを保全する。
    track_metadata = Column(Text, nullable=True)
    # JSON: target identifiers (user_id, persona_id), external refs, etc.
    # NOTE: pause_summary / pause_summary_updated_at カラムは v0.32 (2026-05-09) で
    # Track Chronicle 機構に置き換えられたため ORM 定義から外した。既存 DB に残るカラムは
    # 無害 (= 書き込まれないし読み出されない)。完全削除には別途 migration が必要だが、
    # 安全側の判断で現状は未使用カラムとして残置。
    # 詳細: docs/intent/persona_cognition/track_chronicle.md §8
    last_active_at = Column(DateTime, nullable=True)
    # NOTE: waiting_for / waiting_timeout_at カラムは v0.31 (2026-05-09) で「待ち
    # Track」機構の廃止に伴い ORM 定義から外した。Phase 5 の時間差ツール基盤が
    # 同等機能 (= 何かを待つ Pulse の中断 → 完了通知時に再開) を提供する。
    # 既存 DB は database/migrate.py の post-migration で `status='waiting'` を
    # 'pending' に降ろし、カラムはスキーマ差分でドロップされる。
    # 詳細: docs/intent/persona_cognition/handoff_waiting_track_removal.md
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    completed_at = Column(DateTime, nullable=True)  # always NULL for is_persistent=true
    aborted_at = Column(DateTime, nullable=True)    # always NULL for is_persistent=true
    __table_args__ = (
        Index("idx_action_track_persona_status", "persona_id", "status", "is_forgotten"),
        Index("idx_action_track_last_active", "persona_id", "last_active_at"),
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


class PersonaPulseCursor(Base):
    """ペルソナごとの (per-building) pulse_cursor / entry_marker の永続化。

    旧 conscious_log.json の pulse_cursors / entry_markers を移管したテーブル。
    cursor_seq: その building でペルソナが「ここまで処理した」 seq (新採番空間)
    entry_marker_seq: ペルソナが当該 building に入った時の seq (= ここより前は
      入室以前の出来事として無視する基準)

    詳細: docs/intent/building_memory_unified.md
    """
    __tablename__ = "persona_pulse_cursor"
    PERSONA_ID = Column(String(255), ForeignKey("ai.AIID"), primary_key=True)
    BUILDING_ID = Column(String(255), ForeignKey("building.BUILDINGID"), primary_key=True)
    CURSOR_SEQ = Column(Integer, nullable=False, default=0)
    ENTRY_MARKER_SEQ = Column(Integer, nullable=False, default=0)
    UPDATED_AT = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class BuildingMessage(Base):
    """Building のチャットログ。`cities/<bid>/log.json` 置き換え先。

    詳細: docs/intent/building_memory_unified.md
    """
    __tablename__ = "building_messages"
    id = Column(Integer, primary_key=True, autoincrement=True)
    building_id = Column(String(255), ForeignKey("building.BUILDINGID"), nullable=False)
    seq = Column(Integer, nullable=False)
    role = Column(String(32), nullable=False)  # 'user' | 'assistant' | 'host'
    persona_id = Column(String(255), ForeignKey("ai.AIID"), nullable=True)
    content = Column(Text, nullable=False)
    timestamp = Column(String(64), nullable=False)  # ISO format string (既存 JSON との互換維持)
    heard_by = Column(Text, nullable=False, default='[]')   # JSON array
    ingested_by = Column(Text, nullable=False, default='[]')  # JSON array
    event_type = Column(String(32), nullable=True)  # 'occupancy' | 'world' | 'spawn' | NULL
    event_data = Column(Text, nullable=True)        # JSON: entity_id, from/to_building_id, action 等
    metadata_json = Column(Text, nullable=True)     # 残りメタデータ JSON (SQLAlchemy 予約語の `metadata` を避けて _json サフィックス)
    message_id = Column(String(255), nullable=True)  # legacy `building_id:seq` 互換、後方互換のため保持
    client_message_id = Column(String(64), nullable=True)  # クライアント生成 UUID (idempotency_key)。NULL は重複可
    origin_track_id = Column(String(36), nullable=True)
    pulse_id = Column(String(36), nullable=True)
    # migration script (scripts/migrate_building_logs_to_db.py) で過去 log.json
    # を取り込んだ際の元 seq / 元 message_id。 通常の dual-write 経路では NULL。
    # 取り込み時に重複 seq が発生したケースを失わずに全件保存しつつ、 既存
    # AddonMessageMetadata 等との接続を traceable に保つために使う。
    legacy_seq = Column(Integer, nullable=True)
    legacy_message_id = Column(String(255), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    __table_args__ = (
        UniqueConstraint('building_id', 'seq', name='uq_building_msg_bid_seq'),
        UniqueConstraint('client_message_id', name='uq_building_msg_client_mid'),
        Index('idx_building_msg_bid_seq', 'building_id', 'seq'),
        Index('idx_building_msg_event', 'building_id', 'event_type'),
        Index('idx_building_msg_client_mid', 'client_message_id'),
        Index('idx_building_msg_legacy_mid', 'building_id', 'legacy_message_id'),
    )


# --- Fixture / Observer ---

class Fixture(Base):
    __tablename__ = "fixture"
    FIXTURE_ID = Column(String(36), primary_key=True)
    BUILDING_ID = Column(String(255), ForeignKey("building.BUILDINGID"), nullable=False)
    NAME = Column(String(255), nullable=False)
    TYPE = Column(String(64), nullable=False, default="object")
    DESCRIPTION = Column(String(2048), default="", nullable=False)
    STATE_JSON = Column(Text, nullable=True)
    FILE_PATH = Column(String(512), nullable=True)
    CREATOR_ID = Column(String(255), nullable=True)
    SOURCE_CONTEXT = Column(String, nullable=True)
    CREATED_AT = Column(DateTime, server_default=func.now(), nullable=False)
    UPDATED_AT = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class ObserverConfig(Base):
    __tablename__ = "observer_config"
    OBSERVER_ID = Column(String(36), primary_key=True)
    FIXTURE_ID = Column(String(36), ForeignKey("fixture.FIXTURE_ID"), nullable=False)
    ENABLED = Column(Boolean, default=True, nullable=False)
    EXEC_KIND = Column(String(32), nullable=False)  # "tool" | "playbook" | "push"
    EXEC_TARGET = Column(String(255), nullable=True)
    EXEC_ARGS_JSON = Column(Text, nullable=True)
    INTERVAL_SEC = Column(Integer, nullable=True)
    METRIC_KEYS_JSON = Column(Text, nullable=True)
    NOTIFY_RULES_JSON = Column(Text, nullable=True)
    CREATED_AT = Column(DateTime, server_default=func.now(), nullable=False)
    UPDATED_AT = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class ObserverMetric(Base):
    __tablename__ = "observer_metrics"
    id = Column(Integer, primary_key=True, autoincrement=True)
    OBSERVER_ID = Column(String(36), ForeignKey("observer_config.OBSERVER_ID"), nullable=False)
    METRIC_NAME = Column(String(64), nullable=False)
    VALUE_NUM = Column(Float, nullable=True)
    VALUE_TEXT = Column(Text, nullable=True)
    RECORDED_AT = Column(DateTime, server_default=func.now(), nullable=False)
    __table_args__ = (
        Index('idx_obs_metric_lookup', 'OBSERVER_ID', 'METRIC_NAME', 'RECORDED_AT'),
    )


class RealtimeSpellBinding(Base):
    """リアルタイム情報枠に自動挿入するスペルの設定。

    ペルソナ単位 or Building 単位で設定可能。両方に設定がある場合は両方実行される。
    Pulse 冒頭で1回だけ実行され、結果はリアルタイム情報メッセージに追記される。
    """
    __tablename__ = "realtime_spell_binding"
    BINDING_ID = Column(Integer, primary_key=True, autoincrement=True)
    OWNER_KIND = Column(String(32), nullable=False)  # "persona" | "building"
    OWNER_ID = Column(String(255), nullable=False)
    SPELL_NAME = Column(String(255), nullable=False)
    SPELL_ARGS_JSON = Column(Text, nullable=True)
    LABEL = Column(String(255), nullable=True)
    ENABLED = Column(Boolean, default=True, nullable=False)
    PRIORITY = Column(Integer, default=0, nullable=False)
    CREATED_AT = Column(DateTime, server_default=func.now(), nullable=False)
    UPDATED_AT = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    __table_args__ = (
        Index('idx_realtime_spell_owner', 'OWNER_KIND', 'OWNER_ID'),
    )


# ============================================================================
# 統合 Task モデル (unified_task_model.md v0.1)
# ============================================================================
# 旧 track_task (ActionTrack.tasks_json 軽量チェックリスト) と旧 standalone Task
# (per-persona tasks.db のリッチタスク) を 1 枚のテーブルに統合する。standalone の
# 豊かなフィールドを正とし、親を note_id / track_id (or なし) で持つ:
#   note_id あり = 候補 (desire ノート内のやりたいこと) ← 自律の源泉 ③
#   track_id あり = Track 内の実行小目標 ← 旧 track_task
#   どちらもなし = 未所属 (生成直後 等)
# 昇格 (候補→Track) = 親を note_id → track_id に張り替えるだけ (コピー/破棄しない)。
# 詳細: docs/intent/persona_cognition/unified_task_model.md

class PersonaTask(Base):
    """統合 Task: ペルソナの「やること」最小単位。

    親バインド (parent_kind + note_id / track_id) で「候補」「Track 内小目標」
    「未所属」を区別する。standalone Task のリッチフィールド (goal / status /
    priority / steps / history) を踏襲する。
    """
    __tablename__ = "persona_task"
    id = Column(String(36), primary_key=True)  # UUID hex
    persona_id = Column(String(255), ForeignKey("ai.AIID"), nullable=False)
    # persona 内連番 (task:N 参照子)。所属 (track/note/なし) を横断する単一の参照空間。
    # **不変条件**: タスク行は物理削除しない (掃除は status での論理削除)。これにより
    # MAX(short_id)+1 採番が単調増加し、番号は二度と再利用されない (Track の short_id と対称)。
    short_id = Column(Integer, nullable=True)
    # 親バインド (排他: note か track の一方、or どちらも NULL)
    parent_kind = Column(String(16), nullable=True)  # 'note' | 'track' | None
    note_id = Column(String(36), ForeignKey("note.note_id"), nullable=True)
    track_id = Column(String(36), ForeignKey("action_track.track_id"), nullable=True)
    # 本体フィールド (standalone 踏襲)
    title = Column(String(255), nullable=False)
    goal = Column(Text, nullable=False, default="")
    summary = Column(Text, nullable=False, default="")
    notes = Column(Text, nullable=True)
    status = Column(String(32), nullable=False, default="pending")
    # pending / active / paused / completed / cancelled
    priority = Column(String(16), nullable=False, default="normal")
    origin = Column(String(32), nullable=False, default="auto")
    active_step_id = Column(String(36), nullable=True)  # references persona_task_step.id (no FK: 自己参照順序)
    due_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    completed_at = Column(DateTime, nullable=True)
    version = Column(Integer, nullable=False, default=0)
    last_actor = Column(String(255), nullable=True)
    # --- 欲求の帳簿 (desire ledger; 自律行動 v2 §5「意志 — 六型欲求」) ---
    # parent_kind='note' の候補 (desire) 行でのみ使う。運用 (減衰・再訪・昇格候補) は
    # saiverse/desire_engine.py の決定論関数群が担う。全て nullable = 追加系
    # migration (try_additive_migration の自動 ADD COLUMN) で安全に足せる。
    desire_type = Column(String(32), nullable=True)    # 六型 (話す/聞く/作る/知る/経験する/自分を更新する)。NULL=未分類
    desire_source = Column(Text, nullable=True)        # この欲求を生んだ実経験への参照/引用 (接地の証跡)
    desire_state = Column(String(16), nullable=True)   # 'fresh' | 'fading' | 'expired' (NULL は fresh 扱い)
    last_touched_at = Column(DateTime, nullable=True)  # 最終再訪時刻 (鮮度の基準。無ければ created_at)
    touch_count = Column(Integer, nullable=True)       # 再訪回数 (NULL は 0 扱い)
    __table_args__ = (
        Index("idx_persona_task_persona_status", "persona_id", "status"),
        Index("idx_persona_task_note", "note_id"),
        Index("idx_persona_task_track", "track_id"),
        Index("idx_persona_task_updated", "persona_id", "updated_at"),
        Index("idx_persona_task_short", "persona_id", "short_id"),
    )


class PersonaTaskStep(Base):
    """統合 Task のステップ (standalone task_steps 踏襲)。"""
    __tablename__ = "persona_task_step"
    id = Column(String(36), primary_key=True)  # UUID hex
    task_id = Column(String(36), ForeignKey("persona_task.id", ondelete="CASCADE"), nullable=False)
    position = Column(Integer, nullable=False)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(String(32), nullable=False, default="pending")
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    completed_at = Column(DateTime, nullable=True)
    version = Column(Integer, nullable=False, default=0)
    __table_args__ = (
        Index("idx_persona_task_step_task_pos", "task_id", "position"),
    )


class PersonaTaskHistory(Base):
    """統合 Task の履歴 (standalone task_history 踏襲)。"""
    __tablename__ = "persona_task_history"
    id = Column(String(36), primary_key=True)  # UUID hex
    task_id = Column(String(36), ForeignKey("persona_task.id", ondelete="CASCADE"), nullable=False)
    step_id = Column(String(36), ForeignKey("persona_task_step.id", ondelete="CASCADE"), nullable=True)
    event_type = Column(String(64), nullable=False)
    payload = Column(Text, nullable=True)  # JSON
    actor = Column(String(255), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    __table_args__ = (
        Index("idx_persona_task_history_task_created", "task_id", "created_at"),
    )


class PersonaDayPlan(Base):
    """時間割 (day plan): 起床判断が編成した一日の駆動データ (自律行動 v2 §4.2)。

    1 ペルソナ 1 日 1 行 (複合 PK)。slots_json はコマの配列 (JSON):
    [{start: "HH:MM", kind: 六型|"暮らし"|"休む", ref: "task:N"|"desire:N"|"none",
      facility: building_id|"own_room", budget_rounds: int, note: str,
      status: "pending"|"fired"|"deferred"|"skipped"|"done", defer_count: int}, ...]

    コマ開始の駆動は saiverse/day_plan.py が EventScheduler へ push する決定論
    処理であり、コマ開始は判断点ではない = LLM を呼ばない
    (docs/intent/persona_cognition/judgment_points.md §2)。

    NOTE: 時刻刻印 (created_at / updated_at) は server_default にせず、コード側で
    ``saiverse.clock.now()`` を書き込む (一日シミュレータの仮想時刻を尊重するため。
    autonomous_behavior_v2.md §12 の不変条件)。
    """
    __tablename__ = "persona_day_plan"
    persona_id = Column(String(255), ForeignKey("ai.AIID"), primary_key=True)
    plan_date = Column(String(10), primary_key=True)  # "YYYY-MM-DD"
    slots_json = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)

