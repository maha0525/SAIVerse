from sqlalchemy import (
    CheckConstraint,
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
    event,
    select,
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
    # 2026-07-14: MEMOPEDIA_INDEX_LIMIT を読むコードは存在しない (get_memory_weave_context
    # の memopedia_index_limit 引数は死にコードだったため、MEMOPEDIA_INDEX_ENABLED の
    # 一本化と合わせて削除済み)。カラム自体は死置き。
    MEMOPEDIA_INDEX_LIMIT = Column(Integer, nullable=True)  # Unused as of 2026-07-14 (dead column)
    # Memopedia 索引の head 常時表示 (旧方式) を per-persona で復活させる後方互換トグル。
    # 記憶アーキv2 §7.1 (2026-07-04) で自動想起 (ゾーンC) + 深掘りスペルに一本化した際、
    # head への Memopedia 全ページ一覧の常時掲示は廃止された。本トグルを True にすると、
    # MemopediaIndexSection.capture (sea/head_pipeline/sections/memopedia_index.py) が
    # 目次 (summary あり・深さ無制限・旧方式相当) を組み立て、head に含める。P4-d
    # (2026-07-11) で器を MemoryWeaveSection からこちらへ一本化、2026-07-14 に描画内容の
    # 回帰 (summary 欠落・深さ制限) を修正済み。メモ帳として能動的に Memopedia を使う
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
    # ⚠️ legacy: anchor 状態は session_anchor テーブルへ行分離済み (beat_execution_context.md §3.1)。
    # この列は backfill_session_anchors (database/migrate.py) の変換元としてのみ残存 (変換後は常に NULL)。
    # 列 DROP は後続の掃除 wave (破壊的 migration になるため)。
    METABOLISM_ANCHORS = Column(Text, nullable=True)  # JSON: per-model anchor state {"model": {"anchor_id": "...", "updated_at": "..."}}
    # 自律行動の ON/OFF (2026-07-14: ACTIVITY_STATE 4値 'Stop'/'Sleep'/'Idle'/'Active' を
    # 廃止し1本化。旧実装は全ゲートが == 'Active' の二値判定のみで、Stop/Sleep/Idle は
    # 実装上区別されておらず、Stop=機能停止も未実装 (ユーザー発言への応答は常に素通り)
    # だった。既定 True。スケジュール未設定のペルソナは autonomy_wiring.watchdog_tick /
    # day_plan.confirm_life_for_today が skip するため ON でも動かない (安全)。
    AUTONOMY_ENABLED = Column(Boolean, default=True, nullable=False)
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
    # ⚠️ 退役済みの列 (autonomous_behavior_v3.md §9-5「LIFE_PURPOSE 列は丸ごと
    # 退役」)。**読み手も書き手も既に存在しない** — ヘルパ (saiverse/life_purpose.py)、
    # head セクション、保存スペル (life_purpose_set) は 2026-08-21 に全て撤去した。
    # 列と既存データだけを残してあるのは、v0.3 の移行 (§9-8 の機械写し) が
    # 入力として読むため: purpose の一文 → コア記憶 / interests・vocations →
    # 手帳のアクティビティ。**その移行を流し終えるまで列を落とさないこと**
    # (落とすと写し元が消える)。
    # 形式: JSON {"purpose": str, "interests": [str], "vocations": [str]} / NULL = 未設定。
    LIFE_PURPOSE = Column(Text, nullable=True)


class SessionAnchor(Base):
    """Session anchor / TTL の (persona, model) 行分離 (beat_execution_context.md §3.1)。

    旧形式は AI.METABOLISM_ANCHORS (単一 JSON テキスト列に全 model 分) で、更新が
    JSON 全体の read-modify-write になっていた (SEA 監査 S8)。本テーブルは 1 行 =
    1 (persona, model) の Session anchor 状態で、更新は行単位 upsert。旧列からの
    移行は database/migrate.py の backfill_session_anchors (冪等、起動時に実行)。

    - ANCHOR_MESSAGE_ID: SAIMemory messages 内の anchor メッセージ ID (提示コンテキストの起点)。
    - TTL_SECONDS: この anchor を最後に書いた時点の cache TTL (書き込み時 TTL 固定
      評価、docs/intent/cache_lifecycle_control.md §5.4)。NULL = 旧 anchor / TTL
      不明 (読み出し側が現行設定から算出する後方互換)。
    - UPDATED_AT: 最終 touch 時刻 (epoch 秒)。LLM 呼び出し成功時にのみ前進する
      prompt cache 書き込みの真の起点。
    - FOLDED_RANGES_JSON: 提示コンテキストの**途中**で digest に畳まれた範囲のリスト (JSON)。
      退場が episode 単位になり「古い会話を生で残したまま新しい作業の古い部分を
      畳む」が起きるため、anchor 一点では提示コンテキストを表せなくなった。提示コンテキスト = anchor 以降 −
      畳まれた範囲 + その位置の digest (docs/intent/chronicle_eviction.md §6)。
      退役は model ごとなのでこの行が持ち主。NULL = 圧縮区間なし。
    """
    __tablename__ = "session_anchor"
    PERSONA_ID = Column(String, primary_key=True)
    MODEL_KEY = Column(String(128), primary_key=True)
    ANCHOR_MESSAGE_ID = Column(String, nullable=True)
    TTL_SECONDS = Column(Integer, nullable=True)
    UPDATED_AT = Column(Integer, nullable=False)  # epoch 秒
    FOLDED_RANGES_JSON = Column(Text, nullable=True)


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
    # 公共施設のロールタグ (自律行動 v2 §6.1)。JSON 配列 (例: '["library", "plaza"]')。
    # 語彙は saiverse/facility_map.py の FACILITY_ROLE_VOCAB:
    #   "plaza"(広場: 話す/聞く) / "workshop"(工房: 作る) /
    #   "library"(図書館: 知る) / "park"(公園: 経験する)
    # 1 Building 複数ロール可。NULL/空 = ロールなし (私室・通常 Building)。
    # タグ付け UI/CLI は将来フェーズ — 手動 SQL 例は facility_map.py の docstring 参照。
    FACILITY_ROLES = Column(Text, nullable=True)
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
    # use_alter=True で city→user の FK をテーブル作成後の ALTER に回し、
    # user⇄city / user→building→(region→)city→user の FK 循環を1点で断つ。
    # city.USERID は user へ戻る唯一の辺なので、ここ1箇所で全閉路が DAG 化され、
    # SQLAlchemy の sorted_tables が出す unresolvable cycles 警告 (将来 error 化) を解消する。
    USERID = Column(Integer, ForeignKey("user.USERID", use_alter=True, name="fk_city_user"), nullable=False)
    CITYID = Column(Integer, primary_key=True, autoincrement=True)
    # 内部の識別子 (ASCII 英数字とアンダースコアのみ)。起動引数・user_room の
    # BUILDINGID・ペルソナ ID・建物ログの保存先フォルダ・二重起動チェックの鍵が
    # すべてこの文字列から組み立てられるため、**City 作成後は変更できない**
    # (docs/intent/city_identity.md §4 不変条件 2)。表示名は CITYNAME。
    CITY_SLUG = Column(String(32), nullable=False)
    # 表示名。自由な文字列 (日本語可) で一意性を要求しない。空なら UI は
    # CITY_SLUG を代わりに表示する。**NAME が付く列は表示名** という規則を
    # BUILDINGNAME / AINAME と共有する (docs/intent/city_identity.md §3)。
    CITYNAME = Column(String(64), default="", nullable=False)
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
    __table_args__ = (UniqueConstraint('USERID', 'CITY_SLUG', name='uq_user_city_slug'), UniqueConstraint('UI_PORT', name='uq_ui_port'), UniqueConstraint('API_PORT', name='uq_api_port'))

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
    # 不変条件 (分離監査 P1-2 / W7 柱5): AIID ごとに EXIT_TIMESTAMP IS NULL の
    # active 行は高々 1 行。部分一意 index `uq_occupancy_active_ai` が DB 側で
    # 強制するが、意図的にモデル metadata には載せず起動時の
    # database/occupancy_repair.py (修復→CREATE INDEX) が冪等に作成する
    # (全書換 migration が修復前の重複データのコピーで詰まないように)。
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
    # 世界全体で一意の安定連番。参照アドレッシング統一 (item:N / saiverse://item/N)
    # の同一性キー。位置 (ItemLocation.SLOT_NUMBER) とは別概念で、アイテムが動いても
    # 変わらない。採番は下の before_insert リスナーが世界全体の MAX+1 で行う。
    # 詳細: docs/intent/reference_addressing.md
    SHORT_ID = Column(Integer, nullable=True)
    NAME = Column(String(255), nullable=False)
    TYPE = Column(String(64), nullable=False, default="object")
    DESCRIPTION = Column(String(2048), default="", nullable=False)
    FILE_PATH = Column(String(512), nullable=True)
    STATE_JSON = Column(String, nullable=True)
    CREATOR_ID = Column(String(255), nullable=True)
    SOURCE_CONTEXT = Column(String, nullable=True)
    CREATED_AT = Column(DateTime, server_default=func.now(), nullable=False)
    UPDATED_AT = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


@event.listens_for(Item, "before_insert")
def _assign_item_short_id(mapper, connection, target) -> None:
    """挿入時に SHORT_ID 未設定なら世界全体の連番を刻む (world スコープ)。

    全 item 生成経路を一箇所でカバーする (create_document_item / create_picture_item
    など各メソッドが個別に採番する必要がない)。削除後も MAX ベースなので再利用せず、
    一度 item:N が指したアイテムが消えても N が別物に化けない (I5)。

    バッチ安全性: SQLAlchemy は同一クラスの複数行を executemany にまとめ、before_insert
    を全行ぶん先に発火させてから INSERT するため、DB の MAX は同一フラッシュ内の先行行を
    見られない。接続スコープの high-water mark (``connection.info``) と DB の MAX の大きい
    方 +1 を採ることで、単発・バッチ・後続トランザクションのいずれでも重複しない連番になる。
    """
    if getattr(target, "SHORT_ID", None) is not None:
        return
    db_max = connection.execute(
        select(func.coalesce(func.max(Item.SHORT_ID), 0))
    ).scalar() or 0
    next_val = max(connection.info.get("_item_short_id_hwm", 0), db_max) + 1
    connection.info["_item_short_id_hwm"] = next_val
    target.SHORT_ID = next_val


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

    # 予約同期の世代トークン (W3 A12, docs/handoff/2026-07-20_w3_schedule_ledger_handoff.md D2)。
    # 発火時刻・内容に影響する設定変更 (create / update / toggle / life PUT /
    # schedule_add tool) が同じ commit 内で +1 する。実行簿記 (COMPLETED /
    # LAST_EXECUTED_AT) は世代を上げない。EventScheduler の予約 closure が
    # 登録時の世代を運び、発火時・reconciliation 時に DB の現世代と照合する。
    # NOT NULL + scalar default 0 なので try_additive_migration の軽量 ALTER
    # パスで既存 DB に安全に足せる (既存行は 0)。
    SYNC_GENERATION = Column(Integer, default=0, nullable=False)

    # 台帳冪等キーの行一生トークン — SCHEDULE_ID 再利用との分離 (W3 Codex 第三陣)。
    # SQLite は AUTOINCREMENT 無しの INTEGER PK で削除済み最大 ID を再利用しうる
    # ため、台帳キーが {schedule_id}:{occurrence} だけだと「実行済み行を削除 →
    # 新規作成」で旧行の completed 台帳行が新行の claim を永久ブロックする。
    # 行作成時に一度だけ採番し、更新では変えない (世代 SYNC_GENERATION=設定の版、
    # トークン=行の同一性)。nullable — 既存行は migrate.py の backfill が埋め、
    # 読み手は NULL を "legacy" として扱う。
    INSTANCE_TOKEN = Column(String(32), nullable=True)

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
    """⚠️ legacy: head snapshot は session_head_snapshot テーブル (PK=(PERSONA_ID,
    MODEL_KEY)) へ移行済み (beat_execution_context.md §3.1、2026-07-17)。

    このテーブルは backfill_session_head_snapshots (database/migrate.py) の
    変換元としてのみ残存し、読み書きの現役経路はない (store の読み口は
    SessionHeadSnapshot へ切り替え済み)。テーブル DROP は後続の掃除 wave
    (drop_empty_legacy_note_tables の先例に倣う)。

    旧キーは (PERSONA_ID, LINE_ID) だったが、LINE_ID は実質常に 'main' で、
    MODEL_KEY 列は非キーの参考値 (実装バグで常に 'default' が入っていた —
    backfill が ai.DEFAULT_MODEL から実 model 名へ解決して移行する)。
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


class SessionHeadSnapshot(Base):
    """Cached Head Architecture: Session (persona, model) 単位の head snapshot 永続化。

    beat_execution_context.md §3.1: head は (persona_id, model_key) に一つ。
    line はキーに含めない (まはー裁定 2026-07-16 — line で分けると prefix cache の
    共用という head の存在意義が死ぬ。サブラインも同じ model なら同じ head を共有)。
    旧 line_head_snapshot (キー=(PERSONA_ID, LINE_ID)) からの移行は
    database/migrate.py の backfill_session_head_snapshots (冪等、起動時に実行)。

    - SECTIONS_JSON: A 相当。全 Section の snapshot を section.name ->
      serialized JSON の dict として保持 (Section ごとの serialize_snapshot 出力)。
    - LAST_NOTIFIED_JSON: B 相当 (= 最後に末尾通知済みの各 Section snapshot)。
      diff 既読状態も (persona, model) で分離される — 各 Session が「自分がまだ
      知らない変化」を独立に受け取るため (§3.1)。
    - LINE_ROLE: capture したラインの役割の記録 (参考情報、キーではない)。

    詳細: docs/intent/cached_head_architecture.md §3.3 / beat_execution_context.md §3.1
    """
    __tablename__ = "session_head_snapshot"
    PERSONA_ID = Column(String(255), ForeignKey("ai.AIID"), primary_key=True)
    MODEL_KEY = Column(String(128), primary_key=True)
    LINE_ROLE = Column(String(32), nullable=False)
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


# NOTE (2026-07-11, P3c①): Note / NotePage / NoteMessage / TrackOpenNote の
# ORM クラス (旧テーブル note / note_page / note_message / track_open_note) は
# concept_consolidation.md「Note → テーマノード移行」でここから削除した。
# Note は per-persona memory.db 側の memopedia ページ (trunk root_theme、
# category "theme") へ物理統合済み — saiverse/note_theme_migration.py が
# SAIVerseManager._on_persona_registered で扇形移行し、main DB 側の行は
# 移行後 DELETE する。旧テーブル自体は database/migrate.py の
# _drop_empty_legacy_note_tables が「note が存在してかつ空」になった時点で
# DROP する (扇形移行のため即時 DROP はできない — 詳細は同関数の docstring)。
# persona_task.note_id は死カラムとして残す (コメント参照)。


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


# --- Feed intake (RSS/Atom) — docs/intent/rss_feed_intake.md ---

class FeedSubscription(Base):
    """フィード購読 1 本。持ち主は Fixture (intent §10-1 裁定: Building 直でなく Fixture)。

    ETAG / LAST_MODIFIED は条件付き GET (304) 用のキャッシュ帳簿。
    CONSECUTIVE_FAILURES / LAST_ERROR は健康チェック (連続失敗フィードの検出) 用。
    これらは内部帳簿であり、ペルソナに見える Fixture.STATE_JSON へは書かない。
    """
    __tablename__ = "feed_subscription"
    SUBSCRIPTION_ID = Column(String(36), primary_key=True)
    FIXTURE_ID = Column(String(36), ForeignKey("fixture.FIXTURE_ID"), nullable=False)
    FEED_URL = Column(String(512), nullable=False)
    SITE_URL = Column(String(512), nullable=True)
    TITLE = Column(String(255), default="", nullable=False)
    ENABLED = Column(Boolean, default=True, nullable=False)
    ETAG = Column(String(255), nullable=True)
    LAST_MODIFIED = Column(String(255), nullable=True)
    LAST_OK_AT = Column(DateTime, nullable=True)
    LAST_ERROR = Column(String(512), nullable=True)
    CONSECUTIVE_FAILURES = Column(Integer, default=0, nullable=False)
    CREATED_AT = Column(DateTime, server_default=func.now(), nullable=False)
    UPDATED_AT = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    __table_args__ = (
        Index('idx_feed_sub_fixture', 'FIXTURE_ID'),
        # 同一 Fixture への同一 URL 購読の重複防止 (add_subscription は
        # get-or-create、この制約は DB 側の最終防衛線)。既存 DB へは
        # migrate.py の _ensure_feed_tables が UNIQUE INDEX として適用する。
        UniqueConstraint('FIXTURE_ID', 'FEED_URL', name='uq_feed_sub_fixture_url'),
    )


class FeedItem(Base):
    """取得済みフィード記事 1 件。転載のみ (要約・言い換えなし — intent 不変条件 1)。

    (SUBSCRIPTION_ID, GUID) 一意で再取得時の重複保存を防ぐ。id (autoincrement) が
    ペルソナ既読カーソル (FeedReadCursor.LAST_ITEM_ID) の座標になる。
    カーソル (id > LAST_ITEM_ID) と剪定の id 上限ガードは「一度使った id は
    再利用されない」単調性を前提にするため、SQLite の AUTOINCREMENT を明示する
    (sqlite_autoincrement) — 素の INTEGER PRIMARY KEY は最大 id の行を削除
    (published 順の剪定・購読削除) すると次の INSERT が同じ id を再利用し、
    カーソルより小さい id の新着が永久に配送から漏れる (二十巡目 V1)。
    既存 DB のテーブルへは migrate.py の _rebuild_feed_item_with_autoincrement
    が再構築で適用する。
    """
    __tablename__ = "feed_item"
    id = Column(Integer, primary_key=True, autoincrement=True)
    SUBSCRIPTION_ID = Column(
        String(36), ForeignKey("feed_subscription.SUBSCRIPTION_ID"), nullable=False
    )
    GUID = Column(String(512), nullable=False)
    TITLE = Column(Text, default="", nullable=False)
    SUMMARY = Column(Text, default="", nullable=False)
    LINK = Column(String(512), default="", nullable=False)
    PUBLISHED_AT = Column(DateTime, nullable=True)
    FETCHED_AT = Column(DateTime, server_default=func.now(), nullable=False)
    __table_args__ = (
        UniqueConstraint('SUBSCRIPTION_ID', 'GUID', name='uq_feed_item_sub_guid'),
        Index('idx_feed_item_sub', 'SUBSCRIPTION_ID', 'id'),
        # CREATE TABLE に AUTOINCREMENT を出す (id 単調性 — docstring 参照)
        {"sqlite_autoincrement": True},
    )


class FeedReadCursor(Base):
    """ペルソナごとの「ここまで見た」カーソル (intent §13)。

    既読状態はペルソナの持ち物 — Building を共有しても既読は混ざらない
    (intent 不変条件 3)。フィード (購読) ごとに位置を 1 つ持つ。
    """
    __tablename__ = "feed_read_cursor"
    id = Column(Integer, primary_key=True, autoincrement=True)
    PERSONA_ID = Column(String(255), nullable=False)
    SUBSCRIPTION_ID = Column(
        String(36), ForeignKey("feed_subscription.SUBSCRIPTION_ID"), nullable=False
    )
    LAST_ITEM_ID = Column(Integer, nullable=False, default=0)
    UPDATED_AT = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    __table_args__ = (
        UniqueConstraint('PERSONA_ID', 'SUBSCRIPTION_ID', name='uq_feed_cursor_persona_sub'),
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
    # 親バインド (排他: note か track の一方、or どちらも NULL)。
    # 'note' は P3c-0 (desire 正規化) で撤去 — 欲求候補は親なし+stage='candidate'
    # で表現するため、新規行はもう parent_kind='note' を持たない。
    parent_kind = Column(String(16), nullable=True)  # 'note' (旧) | 'track' | None
    # P3c-0 以降は死カラム (書き手・読み手ゼロ)。P3c① で note テーブル自体が
    # models.py から削除された (Note はテーマノードページへ物理統合済み) ため、
    # FK 宣言は外した (存在しないテーブルへの ForeignKey は create_all で解決
    # できない)。物理 DROP はまだしない (このカラム自体の削除は別途判断)。
    note_id = Column(String(36), nullable=True)
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
    # --- 成果物参照 (JSON 配列; judgment_points.md §6 の接地の証跡) ---
    # セッション終了判断の done 裁定で「このセッションが実際に作った成果物」の
    # ref (Item ID 等) を刻む。notes (自由記述) と分けるのは、起床判断のバックログ
    # 提示 (§4「成果物参照の有無つき」) や接地監査が機械可読に読むため。
    # nullable = 追加系 migration (try_additive_migration) で安全に足せる。
    artifact_refs = Column(Text, nullable=True)
    # --- 目的ノードの段階 (stage; life_concept_map.md §3.1「種・段階・位置」) ---
    # 'candidate' (候補=未採用) | 'adopted' (採用済=木の中) | 'dormant' (休眠) |
    # 'completed' | 'aborted'。P3c-0 (desire 正規化) 以降は全書き込み点
    # (create_task/update_task_status/promote_to_track) で常に物理刻印される
    # — NULL は移行前の既存行のみで、database/migrate.py の一回きり移行
    # ステップが saiverse/persona_task_manager.py の derive_stage() と同じ規則
    # で刻印する。nullable のままなのは追加系 migration の都合 (列自体は必須)。
    stage = Column(String(16), nullable=True)
    # ノード種別 (life_concept_map.md §3 の大枝二種、将来用):
    # 'practice' (営み: 細分化した先がいくら完了しても終わらない) |
    # 'venture' (企て: 細分化した全ての完了で自動的に終わる)。
    # 語の選定理由: practice は「日々続ける営み」(a daily practice) の英語慣用で
    # 無限継続の含意があり、venture は「結末のある企て」(undertaking) の含意が強い。
    # project (Note の note_type と衝突) / activity (ACTIVITY_STATE と衝突) を避けた。
    # NULL = 未指定 (葉ノードや未分類)。
    nature = Column(String(16), nullable=True)
    # 昇格・命名の来歴 (JSON 配列の ref 群; artifact_refs と同形式)。
    # 収穫 (mark → candidate) や命名 (テーマノード ← 航跡クラスタのメンバー) の
    # 由来ノード参照を刻む (§3.1「昇格の来歴リンクを残せば意志がクエリ可能な実体になる」)。
    # NULL = 来歴なし。
    promoted_from = Column(Text, nullable=True)
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
    [{start: "HH:MM", kind: コマ種別カタログの名前 (saiverse/slot_kind_catalog.py、
      builtin は 調べる/絵を描く/日記を書く/随筆を書く/出かける/自室で過ごす/自由時間),
      ref: "task:N"|"desire:N"|"none",
      facility: building_id|"own_room", budget_rounds: int, note: str,
      status: "pending"|"fired"|"deferred"|"skipped"|"done", defer_count: int}, ...]
    旧語彙 (六型/暮らし/休む) は封印済みだが、既存行の帳簿としては残りうる。

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
    # 日付に紐づく付帯情報 (JSON dict)。就寝判断 (day_close) が書く
    # {"tomorrow_memo": 明日の自分へのメモ, "day_theme": ...} 等を格納し、
    # 翌朝の起床判断 (day_open) が読む (judgment_points.md §4/§8)。
    # 読み書きは saiverse/day_plan.py の load_plan_meta / update_plan_meta。
    meta_json = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)


class PersonaTimetableTemplate(Base):
    """習慣テンプレート: 時間割の枠 (時間割改修 T2、timetable_redesign.md §5.1)。

    1 ペルソナ 1 行。SLOTS_JSON はコマ雛形の配列 (JSON):
    [{start: "HH:MM" (必須),
      kind: コマ種別カタログの名前 | 省略/null,
      title: str | 省略/null, facility: building_id|"own_room" | 省略/null,
      note: str | 省略/null, budget_rounds: int | 省略/null,
      ref: "task:N"|"desire:N"|"track:N"|"none" | 省略/null}, ...]
    null / 欠落 = **穴** (朝、起床判断でペルソナが埋める。intent §5.3)。

    不変条件 (intent §9-1): このテーブルを書き換える唯一の経路は
    ``saiverse.timetable_template.save_template`` / ``delete_template`` で、
    呼び出し元はユーザー API のみ。**LLM の出力が直接ここを書き換える経路は
    作らない** (変更はユーザー承認の相談ルートのみ)。

    NOTE: 時刻刻印はコード側で ``saiverse.clock.now()`` を書き込む
    (PersonaDayPlan と同じ判断 — 一日シミュレータの仮想時刻を尊重)。
    """
    __tablename__ = "persona_timetable_template"
    id = Column(Integer, primary_key=True, autoincrement=True)
    PERSONA_ID = Column(String(255), ForeignKey("ai.AIID"), nullable=False, unique=True)
    SLOTS_JSON = Column(Text, nullable=False)
    ENABLED = Column(Boolean, default=True, nullable=False)
    CREATED_AT = Column(DateTime, nullable=False)
    UPDATED_AT = Column(DateTime, nullable=False)


class Episode(Base):
    """出来事 (Episode): 実際に時間を満たしたものの「薄い封筒」。

    life_concept_map.md §8/§8.1 の出来事テーブル。会話・作業セッション・コマ実績
    など既存の実体を置換せず、kind + 実体参照で包む共通エンベロープ。一日新聞・
    ライフビューの一次データ源 (一覧は SELECT のみで LLM 不要)。

    - 行はペルソナ単位 (複数主観: 境界と意味は参加者ごとに引かれる §9)。
      同一の世界的出来事は OCCURRENCE_ID で束ねる。
    - 無計画の出来事は ORIGIN_REF が NULL (= 自発)。予定に偽のコマを起こさない (§6)。
    - 閉じ処理は意味を書かず再訪の鍵 (DIGEST_REF) だけ書く (§9)。
    - 時刻は epoch 秒 int。刻印は必ず ``saiverse.clock.now()`` 経由
      (一日シミュレータの仮想クロック尊重、autonomous_behavior_v2.md §12)。
      そのため server_default は使わない (PersonaDayPlan と同じ判断)。

    NOTE: PersonaEventLog (外部イベントの受信箱) とは別物。混同しないこと。
    操作は saiverse/episodes.py に集約する。
    """
    __tablename__ = "episodes"
    EPISODE_ID = Column(String(36), primary_key=True)  # UUID
    PERSONA_ID = Column(String(255), ForeignKey("ai.AIID"), nullable=False)
    # ペルソナ内で安定の整数連番 (episode:N 参照子)。MAX+1 採番 (ActionTrack と同流儀)
    # で、行を物理削除しない限り番号は再利用されない。
    SHORT_ID = Column(Integer, nullable=True)
    # 'conversation' | 'work_session' | 'slot' | 'presence' | 'stroll' | 'other'
    KIND = Column(String(32), nullable=False)
    # 同一の世界的出来事を複数ペルソナ行で束ねる相関 ID (§8.1 複数主観)。NULL = 単独。
    OCCURRENCE_ID = Column(String(64), nullable=True)
    STARTED_AT = Column(Integer, nullable=False)  # epoch 秒
    ENDED_AT = Column(Integer, nullable=True)     # epoch 秒。open の間は NULL
    BUILDING_ID = Column(String(255), nullable=True)
    PARTICIPANTS_JSON = Column(Text, nullable=True)  # JSON 配列 (entity id 群)
    # 出自参照 (コマ・呼びかけ等)。NULL = 自発 (無計画の出来事は出自なしが合法)。
    ORIGIN_REF = Column(String(255), nullable=True)
    STATUS = Column(String(16), nullable=False, default="open")  # 'open' | 'closed'
    # 閉じダイジェスト参照 (再訪の鍵。意味は書かない §9)。NULL = 未発行。
    DIGEST_REF = Column(String(255), nullable=True)
    META_JSON = Column(Text, nullable=True)
    __table_args__ = (
        Index("idx_episodes_persona_status", "PERSONA_ID", "STATUS"),
        Index("idx_episodes_persona_started", "PERSONA_ID", "STARTED_AT"),
        Index("idx_episodes_occurrence", "OCCURRENCE_ID"),
    )


class EpisodeInheritance(Base):
    """継承エッジ (継承 DAG): 範囲ノード (出来事) 間の認識の連続性を表す第二の関係。

    experience_structure.md §3.3 の「継承 DAG」の器。包含の木 (episodes の
    kind + occurrence による親子) とは**独立**な、体験の連続性 (どの範囲を
    直接の元として続きを始めたか / どの範囲をメモリ・digest 経由で知っているか)
    を張る有向エッジ。1 つの子範囲ノードは 0..n 親を持てる (DAG)。

    - **継承 ≠ 時刻**。従来 created_at を継承の代用にしていたことが時系列の嘘の
      根本原因 (§3.3)。時刻は Episode の属性に降り、語り (咀嚼) の文脈は
      このエッジに従う。
    - エッジには**層の型** (:data:`LAYER`) がある: ``fact`` (事実層の継承 —
      スレッド継続・リプランティング・分岐再生成の直接の元) / ``digest``
      (咀嚼層の継承 — メモリ・digest 経由で知っている、並列体験の統合の非直接親)。
      連続性は 0/1 でなく強度で、層はその強度の形式化 (§3.3)。
    - 同一機構が: ①会話の分岐・再生成 (親 = 元範囲、``ANCHOR_REF`` = 分岐点の
      pulse 関節メッセージ) ②並列体験の統合 (ε が両親を digest 層で持つ)
      ③SAIVerse Lite への一時引っ越しと帰還マージ ④メティス取り込み
      (transcript の parentUuid がエッジ原料) を表現する。
    - **ネイティブ構造** — 取り込み用の特例にせず、範囲が開いた瞬間に (選択が
      あれば) 機械的に記帳する (§11-4)。エッジが無い既存データは「直列」の
      縮退で無害。

    行はペルソナ単位 (Episode と同じ)。範囲ノードは
    ``CHILD_EPISODE_ID`` / ``PARENT_EPISODE_ID`` で ``episodes.EPISODE_ID``
    (UUID) を指す。episodes 行は物理削除しない運用 (SHORT_ID の単調性、
    saiverse/episodes.py) なのでエッジの参照先は安定する。操作は
    saiverse/experience_inheritance.py に集約する — 生 SQL で書かないこと。
    """
    __tablename__ = "episode_inheritance"
    EDGE_ID = Column(String(36), primary_key=True)  # uuid4
    PERSONA_ID = Column(String(255), ForeignKey("ai.AIID"), nullable=False)
    # 継承する側 (新しく開いた範囲ノード) の episodes.EPISODE_ID。
    CHILD_EPISODE_ID = Column(String(36), nullable=False)
    # 継承元 (前駆) の範囲ノードの episodes.EPISODE_ID。
    PARENT_EPISODE_ID = Column(String(36), nullable=False)
    # 'fact' (事実層の継承) | 'digest' (咀嚼層の継承)。
    LAYER = Column(String(16), nullable=False)
    # 分岐点 = 親範囲内の特定メッセージ (pulse 関節) への参照 (例 'message:...')。
    # NULL = 親範囲を丸ごと継承 (通常のスレッド継続・並列統合)。W13 では不透明に
    # 保持するだけ (消費は分岐・再生成 wave)。
    ANCHOR_REF = Column(String(255), nullable=True)
    # エッジの由来 ('branch' | 'merge' | 'import' | 'replant' | 'continue' 等)。
    # 観測・後段消費のヒント。NULL 可。
    ORIGIN = Column(String(32), nullable=True)
    CREATED_AT = Column(Integer, nullable=False)  # epoch 秒 (clock.now() 経由)
    META_JSON = Column(Text, nullable=True)
    __table_args__ = (
        # 同一 (子, 親, 層) の重複エッジを禁止 (記帳を冪等に倒す土台)。
        UniqueConstraint(
            "CHILD_EPISODE_ID", "PARENT_EPISODE_ID", "LAYER",
            name="uq_episode_inheritance_edge",
        ),
        # 子 → 親の探索 (継承祖先を遡る咀嚼生成) 用
        Index("idx_episode_inheritance_child", "PERSONA_ID", "CHILD_EPISODE_ID"),
        # 親 → 子の探索 (ある範囲を元にした続きの列挙) 用
        Index("idx_episode_inheritance_parent", "PERSONA_ID", "PARENT_EPISODE_ID"),
    )


# ============================================================================
# 実行台帳 (Execution Ledger) — 不可逆な実行と記録の分裂を防ぐ共通基盤
# docs/intent/execution_ledger.md (v0.3, Phase 0)
# ============================================================================

class ExecutionLedgerEntry(Base):
    """実行台帳: 一回の不可逆な実行 (LLM 実行・世界状態の変更) の identity と状態。

    状態機械 (intent §2.1):
    ``prepared → running → applied → completed`` / ``failed`` / ``unknown``。
    遷移の合法性は saiverse/execution_ledger.py のヘルパーが強制する —
    生 SQL で STATUS を書き換えないこと (intent §5 責任分界表)。

    - ``(KIND, IDEMPOTENCY_KEY)`` の UNIQUE 制約が境界イベントの二重発火を
      一意に収束させる (§2.1 冪等キー)。IDEMPOTENCY_KEY は NULL 可 —
      SQLite は UNIQUE 制約で NULL 同士を衝突させないため、一意性不要な
      実行は素通りする (意図どおり)。
    - PERSONA_ID に FK は張らない (intent §4 スキーマ案どおり。世界横断の
      実行は NULL、LLMUsageLog と同じ判断)。
    - 時刻は epoch 秒 int。刻印は必ず ``saiverse.clock.now()`` 経由
      (仮想クロック尊重、Episode / PersonaDayPlan と同じ判断で
      server_default は使わない)。
    """
    __tablename__ = "execution_ledger"
    EXECUTION_ID = Column(String(36), primary_key=True)  # uuid4
    # 'judgment.day_open' / 'judgment.on_event' / 'slot.fire' / 'metabolism.run' 等
    KIND = Column(String(255), nullable=False)
    # kind 内の境界イベント一意キー。NULL 可 (一意性不要な実行)
    IDEMPOTENCY_KEY = Column(String(255), nullable=True)
    PERSONA_ID = Column(String(255), nullable=True)  # 対象ペルソナ (世界横断は NULL)
    # prepared / running / applied / completed / failed / unknown
    STATUS = Column(String(16), nullable=False)
    PAYLOAD_JSON = Column(Text, nullable=True)  # 実行の入力文脈 (再開・照合に必要な最小限)
    RESULT_JSON = Column(Text, nullable=True)   # 実行結果の要約 (精算ラウンド数、生成物 ref 等)
    ERROR = Column(Text, nullable=True)         # 最後の失敗理由 / unknown 化の理由
    CREATED_AT = Column(Integer, nullable=False)  # epoch 秒
    UPDATED_AT = Column(Integer, nullable=False)  # epoch 秒
    __table_args__ = (
        UniqueConstraint("KIND", "IDEMPOTENCY_KEY", name="uq_execution_kind_idem"),
        # 回復処理の走査 (running の期限監視 / unknown・dead の観測) 用
        Index("idx_execution_ledger_status", "STATUS", "UPDATED_AT"),
        Index("idx_execution_ledger_persona", "PERSONA_ID", "STATUS"),
    )


class ExecutionOutboxItem(Base):
    """送信トレイ: world DB と memory.db を跨ぐ書き込みの配達保証 (intent §2.2)。

    「memory.db にこれを書く」というやること自体を、世界側の適用 (applied) と
    **同一トランザクション**で world DB に書く。配送は persona 単位 FIFO
    (OUTBOX_ID 昇順) — 先頭が失敗している間、後続は試行しない (記憶の順序
    一貫性、不変条件 8)。dead (再試行上限超過) は人裁定に回す終端で、
    黙って捨てない。FIFO 上は「先頭」とみなさず飛ばす (後続をブロックしない)。

    PAYLOAD_JSON は配送内容 (本文・タグ・名義・実行時刻) を**実行時点で凍結**
    したもの。配送遅延があっても変形しない (不変条件 6)。
    """
    __tablename__ = "execution_outbox"
    OUTBOX_ID = Column(Integer, primary_key=True, autoincrement=True)
    EXECUTION_ID = Column(String(36), ForeignKey("execution_ledger.EXECUTION_ID"), nullable=False)
    # 'saimemory.append' / 'perception.push' / 'building.ingest_mark' 等
    TARGET = Column(String(255), nullable=False)
    PERSONA_ID = Column(String(255), nullable=True)  # 配送先 persona (FIFO 順序の単位)
    PAYLOAD_JSON = Column(Text, nullable=False)      # 配送内容 (実行時点で凍結)
    STATUS = Column(String(16), nullable=False)      # pending / delivered / dead
    ATTEMPTS = Column(Integer, nullable=False, default=0)
    LAST_ERROR = Column(Text, nullable=True)
    CREATED_AT = Column(Integer, nullable=False)     # epoch 秒
    DELIVERED_AT = Column(Integer, nullable=True)    # epoch 秒
    __table_args__ = (
        # persona 単位 FIFO の pending 走査 (関所 flush) 用
        Index("idx_execution_outbox_persona_status", "PERSONA_ID", "STATUS"),
        # execution 単位の全配送確認 (applied → completed の証跡) 用
        Index("idx_execution_outbox_execution", "EXECUTION_ID", "STATUS"),
    )


# ============================================================================
# タスク帳 (Task Book) — 相手のある一件の台帳
# docs/intent/autonomous_behavior_v3.md §4.1-2 / §4.2 / §9-5
# ============================================================================

class TaskBookEntry(Base):
    """タスク帳: 相手のある約束・依頼の一件 (autonomous_behavior_v3.md §4.1-2)。

    現行の統合タスクテーブル (persona_task) を痩せた形に作り直したもの。
    UI にも「タスク帳」の名で出る。設計の軸は二本 (2026-08-19 裁定):

    - **器を決めるのは相手の有無**。相手のいる約束・依頼は、期限が無くても
      タスク帳に入る (失くすことが許されないから機械が持つ)。
    - **機械の引き当て (締め切り優先の決定論) に乗るのは期限のある行だけ**。
      ``DUE_AT`` は省略可で、**NULL = 期限なし** — 「ずっと一緒にいようね」型の
      約束に嘘の期限を発明させない。「永続」と「未定」の区別は持たない
      (機械の扱いが同じで、読む消費者がいない)。期限なしの行は急かされず、
      空きティックの選択材料として目に入るだけ。

    この軸から、受け入れられる行は三形: **相手のある一件** (期限任意) /
    **期限つきの自分だけの一件** (「帰宅前に仕上げたいもの」型) /
    **システムタスク** (ORIGIN='system'、期限も相手も任意)。期限も相手も
    無い行はタスク帳ではなく手帳 (やりたいメモ) の領分で、受け入れない。

    状態は §4.2 の三値 (ある / やり終えた / 取り下げた) = 'open' / 'done' /
    'withdrawn'。完遂の接地 (§9-5): 相手のある一件の完遂には成果物参照
    (``ARTIFACT_REF``) か顛末一行 (``OUTCOME``) が要る — v2 の空洞完了
    (やった風の独白で done) を防ぐ証跡。自分だけの一件は任意。

    システムタスク (§9-5) もこの行の亜種 (``ORIGIN`` = 'system'): 機械が
    ペルソナに差し込む急ぎでない依頼。引き当て順は 締め切り → システムタスク
    → プール。

    - 時刻は epoch 秒 int。刻印は必ず ``saiverse.clock.now()`` 経由
      (仮想クロック尊重 — ExecutionLedgerEntry と同じ判断で server_default は
      使わない)。
    - 操作は saiverse/task_book.py に集約する — 生 SQL で書かないこと。
    """
    __tablename__ = "task_book"
    TASK_ID = Column(String(36), primary_key=True)  # uuid4
    PERSONA_ID = Column(String(255), ForeignKey("ai.AIID"), nullable=False)
    # 中身 — 実行の瞬間に再発明が要らない具体さ (指示書)。
    CONTENT = Column(Text, nullable=False)
    # 期限 (epoch 秒)。NULL = 期限なし — 期限のない約束は正当な行。
    # 機械の締め切り引き当てに乗るのは NOT NULL の行だけ。
    DUE_AT = Column(Integer, nullable=True)
    # 相手 ('user' / ペルソナ ID / 'system' 等)。タスク帳に入る行は原則相手がいる。
    COUNTERPART = Column(String(255), nullable=True)
    # 出自 ('user' | 'sluice' | 'system' | 'migration' 等)。
    ORIGIN = Column(String(32), nullable=False)
    # 出どころへの参照 (メッセージ ID 等)。不透明に保持するだけ。
    ORIGIN_REF = Column(String(255), nullable=True)
    # 'open' (ある) | 'done' (やり終えた) | 'withdrawn' (取り下げた)。
    STATUS = Column(String(16), nullable=False, default="open")
    # 成果物参照 — 相手のある完遂の証跡。
    ARTIFACT_REF = Column(String(255), nullable=True)
    # 顛末一行 — 参照できる物が無い行為系の完遂の代替。
    OUTCOME = Column(Text, nullable=True)
    CREATED_AT = Column(Integer, nullable=False)  # epoch 秒 (clock.now() 経由)
    CLOSED_AT = Column(Integer, nullable=True)    # done / withdrawn になった時刻
    META_JSON = Column(Text, nullable=True)
    # 冪等キー — 同じ書き込み試行の再実行 (スルースの再試行等) で同じ約束が
    # 別 TASK_ID として増えるのを防ぐ。キーは呼び手が採番する
    # (例: スルース実行 ID + 操作番号)。NULL の行は一意制約にかからない。
    IDEM_KEY = Column(String(255), nullable=True)
    # 楽観ロックの版数 — update/update の並行競合 (後書きが先書きを黙って
    # 上書きする消失) を検出する。遷移 UPDATE の WHERE に「読んだ時点の値」を
    # 含め、SET で +1 する (saiverse/task_book.py の _guarded_transition)。
    # add_entry は 0 で作る。軽量シンク (schema_sync) の後付け列は NULL で
    # 入るため、migrate.py の _ensure_task_book_table が 0 へ埋める。
    REVISION = Column(Integer, nullable=False, default=0)
    __table_args__ = (
        # open 一覧 (ティックの引き当て・UI) 用
        Index("idx_task_book_persona_status", "PERSONA_ID", "STATUS"),
        # 締め切り引き当て (DUE_AT 昇順走査) 用
        Index("idx_task_book_persona_due", "PERSONA_ID", "DUE_AT"),
        # 冪等キーの一意性 (NULL は SQLite では制約にかからず、既存動作は不変)。
        # UniqueConstraint ではなく UNIQUE インデックスなのは、migrate.py の
        # 軽量シンク (schema_sync) が table.indexes しか同期しないため —
        # インデックスなら既存 DB にも CREATE UNIQUE INDEX で追従できる。
        Index("uq_task_book_idem", "PERSONA_ID", "IDEM_KEY", unique=True),
        # DUE_AT の型を DB 境界でも守る — SQLite は列型を強制しないため、生 SQL
        # で文字列の期限が永続すると期限順比較 (DUE_AT 昇順の引き当て) を毒する。
        # 注意: schema_sync の軽量シンク (ensure_table_columns_indexes) は CHECK
        # を既存テーブルへ後付けできない (ALTER TABLE ADD CONSTRAINT が SQLite に
        # 無い)。task_book はこの変更時点で未リリースの新設テーブルなので、
        # 新規作成時 (table.create) に効けば十分。
        CheckConstraint(
            "DUE_AT IS NULL OR typeof(DUE_AT) = 'integer'",
            name="ck_task_book_due_at_integer",
        ),
    )

