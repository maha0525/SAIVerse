"""時間割 (day plan) の保存とコマ発火配線 (自律行動 v2 §4.2)。

「駆動の実体は、朝、自分で組む時間割」— 起床判断 (day_open) の成果物として
編成された一日のコマ配列を保存し、各コマの開始時刻を EventScheduler へ push
する。以後の駆動の骨格 (発火・移動・帳簿) は決定論であり、**コマ開始は判断点
ではない = 時間割を書き換える構造化判断の LLM を呼ばない**
(docs/intent/persona_cognition/judgment_points.md §2、設計原理 6)。コマの
「実行本体」がハンドラの中で LLM を使うことはある (作業セッション、出かける/
自室で過ごす のコマ開始の Pulse、自由時間の種別選択 — 時間割改修 T3)。

コマ発火 (``_fire_slot``) の処理:
1. ユーザー会話中なら繰り下げ (10 分後に再 push、上限 3 回で skipped)
2. 予算ゲート (v2 §4.5): セッション系 (consumes_budget) コマは日次予算台帳の
   残高でラウンドを切り詰める (残高 0 なら skipped + WARN、ハンドラ実行しない)
3. facility が現在地と違えば OccupancyManager で移動 (失敗は WARN + 続行)
4. kind 別ハンドラ実行 → 実 rounds_used を台帳へ積算 → status 更新

日次予算台帳 (v2 §4.5) は persona_day_plan.meta_json の
``{"budget_total_rounds": N, "budget_used_rounds": M}``。total は起床判断
(day_open) の finalize が編成時に書き (``init_budget_ledger``)、used は発火後の
``consume_budget`` が実測値で積算する。台帳の無い日 (day_open 前 / 旧データ)
はゲート無効 = 従来挙動 (後方互換)。

「ユーザー会話中」の判定は開いている kind='conversation' の出来事 (Episode)
の有無 (``saiverse.episodes.get_open_episode``)。無応答タイムアウト (既定 30 分、
AI.USER_CONV_TIMEOUT_MINUTES) が会話の出来事を閉じる瞬間が v2 の「会話終了」に
相当する (life.md §7 案 Y, 2026-07-13)。旧実装は running Track が
user_conversation 種別かで判定していたが、Track はもう時間経過で状態を
動かさない (running のまま残り続けうる) ため、この述語には使えなくなった —
「いま」の真実は Track ではなく開いている出来事が持つ。

kind 別ハンドラはレジストリ方式 (``register_slot_handler``)。kind の語彙は
コマ種別カタログ (``saiverse.slot_kind_catalog``、timetable_redesign.md §5.5)
から組み立てられ、本モジュールが組み込みで登録するのは:
- 作業セッション系 (execution_type='work_session': 調べる/絵を描く/日記を書く/
  随筆を書く): カタログの指示書テンプレートで指示書を組み ``run_work_session``
  を運転 (予算ゲート対象)
- 「出かける」「自室で過ごす」: 実移動 (presence) + 一回の軽い Pulse (T3。
  既存 schedule 型 Pulse 経路の再利用 — 「組み込みハンドラ」節の冒頭コメント
  参照)。「自由時間」: 開始時に本人が軽量構造化出力で種別を選び、選んだ種別の
  ハンドラへ委譲する (選択失敗は自室で過ごす相当へ縮退)。Pulse が起動でき
  なかったコマは slot へ ``record_level='presence_only'`` を永続化し、表示側
  (一日新聞 / 就寝判断の状況テキスト) が「実行済み」でなく「時間を過ごした
  （詳細な記録なし）」と正直に提示する — していない活動の詳細をペルソナが
  ふりかえりで捏造しないため (soft-confabulation、2026-07-05 実 LLM シム
  異常 #4。旧 暮らし/休む スタブの流儀の継承 = intent §9-3)
未登録 kind のコマは WARN + skipped (``skip_reason='no_handler'``)。旧語彙
(六型 + 暮らし/休む) は封印済み (intent §5.5) でハンドラを持たない — 移行前に
保存された旧 kind のコマが発火した場合はこの経路で正直に skipped になる。

コマの skipped はシステム都合とペルソナ判断を区別して記録する (``skip_reason``)。
システム都合のスキップ (ハンドラ未登録 / 予算切れ / 会話優先の流れ) を
「見送り」= 本人の判断としてペルソナに提示すると、就寝判断がしてもいない判断の
理由を捏造する (接地原則違反、2026-07-05 実 LLM シムで実証)。実績ラベルは
:func:`slot_result_label` が skip_reason 込みで返す。

時刻はすべて ``saiverse.clock.now()`` を読む (v2 §12 の不変条件)。
EventScheduler の同一 key 再 push は既存の再スケジュール挙動 (古い予約の
cancel + 上書き) に従うため、``schedule_day_plan`` / ``reschedule_pending_slots``
の二重呼び出しで二重発火しない (冪等)。
"""
from __future__ import annotations

import json
import logging
import math
import random
import re
import threading
import uuid
from datetime import date, datetime, time as dt_time, timedelta
from typing import Any, Callable, Dict, Iterable, List, NamedTuple, Optional, Tuple

from sqlalchemy.orm import Session

from saiverse import clock, slot_kind_catalog

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# kind 語彙 (時間割改修 T1、timetable_redesign.md §5.5)
#
# 時間割の有効な kind 語彙はコマ種別カタログ (saiverse.slot_kind_catalog、
# 3 層ローダ) が供給する。モジュールロード時に末尾の
# :func:`_rebuild_kind_vocabulary` でキャッシュし、カタログを reload したら
# :func:`reload_kind_vocabulary` で再構築する。
#
# 旧語彙 (六型 + 暮らし/休む) は時間割の kind としては**封印** (intent §5.5) —
# 新規時間割の検証 (_validate_and_normalize_slots / sanitize_timetable) では
# 拒否される。定数として残すのは:
# - 欲求の六型分類 (desire_engine.DESIRE_TYPES / purpose_seed) が**別概念**と
#   してこの語彙を使い続けるため (欲求タクソノミの再設計は T1 のスコープ外)
# - 旧 kind で保存済みの時間割の表示・帳簿保持を壊さないため (表示経路は
#   kind 文字列を素通しする)
# ---------------------------------------------------------------------------

# 旧・六型 (autonomous_behavior_v2.md §5.1。時間割 kind としては封印済み)
KIND_TALK = "話す"
KIND_LISTEN = "聞く"
KIND_CREATE = "作る"
KIND_LEARN = "知る"
KIND_EXPERIENCE = "経験する"
KIND_SELF_UPDATE = "自分を更新する"

SIX_KINDS = (
    KIND_TALK, KIND_LISTEN, KIND_CREATE,
    KIND_LEARN, KIND_EXPERIENCE, KIND_SELF_UPDATE,
)

# 旧・六型以外のコマ (時間割 kind としては封印済み)
KIND_LIVING = "暮らし"
KIND_REST = "休む"

#: 封印された旧語彙 (帳簿・表示の後方互換の識別にのみ使う)
LEGACY_KINDS = SIX_KINDS + (KIND_LIVING, KIND_REST)

#: 時間割の有効な kind (カタログ順)。実体はモジュール末尾の
#: :func:`_rebuild_kind_vocabulary` が構築する。他モジュールからは値の
#: from-import ではなく :func:`all_kinds` / :func:`worker_session_kinds` を
#: 使うこと (reload 後も最新の語彙が見える)。
ALL_KINDS: Tuple[str, ...] = ()

#: 作業セッション運転で処理する kind (execution_type='work_session'。予算
#: ゲート対象)。ScenarioPlayer のセッション終了判断ラップもこの集合を使う。
WORKER_SESSION_KINDS: Tuple[str, ...] = ()

#: kind → 指示書テンプレート (カタログの instruction_template。{note} と
#: {target} を展開する)。
_WORKER_INSTRUCTION_TEMPLATES: Dict[str, str] = {}


def all_kinds() -> Tuple[str, ...]:
    """時間割の有効な kind 語彙 (カタログ駆動・reload 追従の読み取り口)。"""
    return ALL_KINDS


def worker_session_kinds() -> Tuple[str, ...]:
    """作業セッション系 kind の集合 (カタログ駆動・reload 追従の読み取り口)。"""
    return WORKER_SESSION_KINDS

# コマ status
STATUS_PENDING = "pending"
STATUS_FIRED = "fired"
STATUS_DEFERRED = "deferred"
STATUS_SKIPPED = "skipped"
STATUS_DONE = "done"

SLOT_STATUSES = (
    STATUS_PENDING, STATUS_FIRED, STATUS_DEFERRED, STATUS_SKIPPED, STATUS_DONE,
)

# skipped の理由 (slot の skip_reason フィールド)。システム都合のスキップを
# 「見送り」= 本人の判断としてペルソナ/ユーザーに提示しないための区別。
SKIP_REASON_NO_HANDLER = "no_handler"          # kind の実行手段が未登録 (システム側)
SKIP_REASON_BUDGET_EXHAUSTED = "budget_exhausted"  # 日次予算の残高ゼロ
SKIP_REASON_DEFERRAL_LIMIT = "deferral_limit"  # 会話優先の繰り下げ上限で流れた
# kind がカタログの現行語彙に存在しない (旧バージョンの語彙 / 無効化された
# アドオンの種別)。意味の分からない旧 kind を新語彙へ勝手に読み替える移行は
# しない — どの目的の行動だったかの推測は意図の捏造になる (Codex 一巡目 #1 の
# 裁定)。正直に「もう無い種別」と記録し、翌朝から新語彙で組み直される。
SKIP_REASON_KIND_NOT_IN_VOCABULARY = "kind_not_in_vocabulary"
# 起床判断が開始時刻より後に走った (サーバー未起動等) ため発火機会が無かった
# テンプレートコマ (時間割改修 T2、timetable_redesign.md §11-12 裁定)。
# 過去コマは現在時刻へ丸めず「流れた」と正直に記録して今の時刻から合流する。
SKIP_REASON_MISSED_START = "missed_start"

#: コマ status → 実績ラベル (skipped 以外)。skipped は skip_reason で細分化する
#: ため :func:`slot_result_label` を使うこと。
SLOT_STATUS_LABELS = {
    STATUS_PENDING: "未実施",
    STATUS_FIRED: "実行した（完了記録なし）",
    STATUS_DEFERRED: "繰り下げのまま",
    STATUS_DONE: "実行済み",
}

#: skip_reason → 実績ラベル。ペルソナが自分の判断でないものを自分の判断として
#: 振り返らされないよう、システム都合であることを明示する文言にする。
SKIP_REASON_LABELS = {
    SKIP_REASON_NO_HANDLER: "実行できず（システム側の問題: このコマ種別の実行手段が未実装）",
    SKIP_REASON_BUDGET_EXHAUSTED: "実行できず（作業ラウンドの日次予算切れ）",
    SKIP_REASON_DEFERRAL_LIMIT: "流れた（ユーザーとの会話を優先したため）",
    SKIP_REASON_MISSED_START: "流れた（サーバーが起動していなかったため）",
    SKIP_REASON_KIND_NOT_IN_VOCABULARY: "実行できず（このコマ種別は現在の時間割では使われていません）",
}

#: slot の record_level: 完了記録の詳しさ。presence_only は「その場に居た
#: (施設への実移動) 以外の詳細な実行記録が無い」— 出かける/自室で過ごす/
#: 自由時間 のハンドラが、コマ開始の Pulse を起動できなかったとき (T3 の
#: fail-open。旧 暮らし/休む スタブは常時) に付ける。Pulse が走ったコマには
#: 付けない — 実際の思考記録が SAIMemory に残るため。マーカーの無い done
#: (旧データ / セッション系) は従来どおり「実行済み」(後方互換)。
RECORD_LEVEL_PRESENCE_ONLY = "presence_only"

#: record_level='presence_only' な done コマ (スタブハンドラ) の実績ラベル。
#: 「実行済み」と提示すると、ペルソナが就寝ふりかえりで具体的な活動内容
#: (食事の選定等) を捏造する (soft-confabulation、2026-07-05 実 LLM シムで観測)。
LABEL_DONE_PRESENCE_ONLY = "時間を過ごした（詳細な記録なし）"


def slot_result_label(slot: Dict[str, Any]) -> str:
    """コマの実績ラベル (就寝判断の状況テキストと一日新聞が共用する語彙)。

    skipped はシステム都合 (skip_reason) を明示して返す。「見送り」のような
    本人判断を示唆する語は、実際に本人が判断した記録があるときにしか使わない
    (現状、本人判断でコマを skipped にする機構は無い — 残り時間割の全置換で
    コマ自体が消える)。理由の無い旧データは中立の「実行されず」に倒す。
    """
    status = str(slot.get("status") or STATUS_PENDING)
    if status == STATUS_SKIPPED:
        reason = str(slot.get("skip_reason") or "")
        return SKIP_REASON_LABELS.get(reason, "実行されず（理由の記録なし）")
    if status == STATUS_DONE \
            and str(slot.get("record_level") or "") == RECORD_LEVEL_PRESENCE_ONLY:
        # 詳細な実行記録の無い done (presence スタブ)。「実行済み」と提示
        # するとペルソナが活動内容を捏造してふりかえる (soft-confabulation)。
        return LABEL_DONE_PRESENCE_ONLY
    return SLOT_STATUS_LABELS.get(status, status)

REF_NONE = "none"
FACILITY_OWN_ROOM = "own_room"

#: ユーザー会話中の繰り下げ幅 (分) と上限回数 (v2 §4.2「割り込み」)
DEFER_MINUTES = 10
MAX_DEFERRALS = 3

def _coerce_defer_count(slot: Dict[str, Any]) -> int:
    """slot の defer_count を安全に読む (保存 JSON 由来 — 型不正は 0 扱い)。

    通常発火の繰り下げと再起動回復の両方が使う。不正値 1 件で発火 callback や
    回復ループを例外死させない (コマ単位の隔離。Codex 四巡目 #5)。
    """
    raw = slot.get("defer_count")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw
    if raw is not None:
        LOGGER.warning(
            "[day_plan] invalid defer_count %r; treating as 0 (slot id=%s)",
            raw, slot.get("id"),
        )
    return 0


#: 開始時刻からこの分数を超えて過ぎたコマは「流れた」(missed_start) — 丸め
#: (現在時刻へのクランプ) による救済は数分のズレに限定する (§11-12 裁定)。
#: 起床判断の途中起動 (timetable_template の compose) と、再起動後の予約回復
#: (:func:`reschedule_pending_slots` の downtime_recovery) が同じ閾値を見る —
#: 同じ停止に対して入口ごとに意味が割れないため (Codex 一巡目 #2)。
MISSED_GRACE_MINUTES = 10

#: budget_rounds が 0 / 未指定の作業コマに使う既定ラウンド予算
DEFAULT_BUDGET_ROUNDS = 8

#: 日次予算台帳 (persona_day_plan.meta_json) のキー (v2 §4.5)
META_BUDGET_TOTAL = "budget_total_rounds"
META_BUDGET_USED = "budget_used_rounds"

# ---------------------------------------------------------------------------
# ライフ (life.md v0.5 §3/§4: ユーザーが設定する起床・就寝の区間)
#
# v0.4 までは起床判断 (day_open) で LLM がライフを「宣言」していたが、実機初日
# (2026-07-13) にペルソナが過去起点・予算不整合のライフを宣言できてしまう
# 破綻が起き、まはー裁定で責任分界を全面改訂した — ライフ = ユーザーが設定する
# 起床・就寝の区間 (PersonaSchedule が器)。ペルソナは宣言しない。以下の宣言口
# 検証 (重なり・谷コマ・均等モード間隔) は廃止し、システムが day_open 発火時に
# :func:`confirm_life_for_today` で確定して焼く (呼び出し元は
# saiverse.autonomy_wiring.fire_judgment_point)。永続化・台帳・予算ゲート・
# keep-alive 連動 (Phase 2〜4 実装) はそのまま生きる — 書き手が LLM から
# システムに変わるだけ。
# ---------------------------------------------------------------------------

#: ライフのモード (life.md §5.1): 均等 = 標準パルスの間隔を TTL 内に保つ
#: (Anthropic/OpenAI 系のキャッシュ延命に最適)。自由 = 間隔制約なし (Gemini 等)。
LIFE_MODE_EVEN = "even"
LIFE_MODE_FREE = "free"
LIFE_MODES = (LIFE_MODE_EVEN, LIFE_MODE_FREE)

#: 均等モードで許容するコマ間隔の上限 (分)。TTL (Anthropic 1h) ちょうどは
#: 遅延で割れるため安全マージンを引いた初期値 (life.md §12-2)。均等モードの
#: 最低予算 (:func:`_min_life_budget`) の基準値としても使う (life.md §4.2)。
LIFE_EVEN_MAX_GAP_MINUTES = 50

#: ラウンド消費 → パルス予算換算の減衰係数 κ (life.md §8.1)。
#: 消費 = used_pulses + used_rounds × LIFE_ROUND_BUDGET_FACTOR。
LIFE_ROUND_BUDGET_FACTOR = 0.2

#: persona_day_plan.meta_json の lives 配列のキー (life.md §11.2)。
META_LIVES = "lives"

#: DEFAULT_MODEL の provider がこの集合に属せば既定モードは均等 (life.md §5.1)。
_EVEN_MODE_PROVIDERS = frozenset({"anthropic", "openai"})

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
# コマは目的ノードを任意階層で指せる (P5, life_concept_map.md §3.1):
# task:N (採用済み) / desire:N (候補=お試し採用) / track:N (大枝=関心そのもの)
_REF_RE = re.compile(r"^(task|desire|track):(\d+)$")
_TRACK_REF_RE = re.compile(r"^track:(\d+)$")


def is_valid_hhmm(value: Any) -> bool:
    """"HH:MM" 形式の妥当性チェック (ライフ設定 API 等、保存前バリデーションの共用口)。"""
    return isinstance(value, str) and bool(_TIME_RE.match(value))

# ---------------------------------------------------------------------------
# kind 別ハンドラのレジストリ
# ---------------------------------------------------------------------------

#: ハンドラ signature: fn(manager, persona_id, plan_date, slot, index) -> Optional[int]
#: 戻り値は「実際に消費したラウンド数」(None / 0 = 予算消費なし)。
#: consumes_budget=True で登録された kind は、戻り値が予算台帳へ積算される。
SlotHandler = Callable[[Any, str, str, Dict[str, Any], int], Optional[int]]

_SLOT_HANDLERS: Dict[str, SlotHandler] = {}

#: 予算ゲート (v2 §4.5) の対象 kind (register_slot_handler の consumes_budget)
_BUDGET_GATED_KINDS: set = set()

#: manager に execution_ledger が無い環境 (旧テストスタブ等) への WARN を
#: persona ごと一度だけに抑える (autonomy_wiring._LEDGER_MISSING_WARNED と同流儀)。
#: 台帳が無ければ :func:`_fire_slot` は従来挙動 (:func:`_fire_slot_legacy`) に縮退する。
_LEDGER_MISSING_WARNED: set = set()


def register_slot_handler(kind: str, fn: SlotHandler, *, consumes_budget: bool = False) -> None:
    """kind に対するコマ発火ハンドラを登録する (同 kind は上書き)。

    組み込みではカタログの execution_type ごとに、作業セッション運転
    (work_session) / 実移動 + 暮らしプロファイルのセッション (outing /
    stay_home) / 開始時選択 + 委譲 (free_choice) が登録される (モジュール末尾の
    :func:`_rebuild_kind_vocabulary`)。差し替えたい実装はここへ上書き登録する
    ことで配線に乗る (シムの ScenarioPlayer が使う口)。

    Args:
        consumes_budget: True なら予算ゲートの対象 (v2 §4.5)。発火前に日次
            残高で budget_rounds が切り詰められ (残高 0 なら skipped)、
            ハンドラの戻り値 (実 rounds_used) が台帳へ積算される。
    """
    if kind in _SLOT_HANDLERS:
        LOGGER.info("[day_plan] slot handler overridden: kind=%s", kind)
    _SLOT_HANDLERS[kind] = fn
    if consumes_budget:
        _BUDGET_GATED_KINDS.add(kind)
    else:
        _BUDGET_GATED_KINDS.discard(kind)


# ---------------------------------------------------------------------------
# バリデーションと保存
# ---------------------------------------------------------------------------


def _normalize_plan_date(plan_date: Any) -> str:
    """plan_date を "YYYY-MM-DD" 文字列へ正規化する。"""
    if isinstance(plan_date, datetime):
        return plan_date.date().isoformat()
    if isinstance(plan_date, date):
        return plan_date.isoformat()
    if isinstance(plan_date, str):
        try:
            return date.fromisoformat(plan_date.strip()).isoformat()
        except ValueError as exc:
            raise ValueError(f"invalid plan_date: {plan_date!r} (expected YYYY-MM-DD)") from exc
    raise ValueError(f"invalid plan_date type: {type(plan_date).__name__}")


def _validate_and_normalize_slots(
    slots: Any, *, ascending_from: int = 0,
    order_key: Optional[Callable[[str], int]] = None,
    fresh_ids_from: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """コマ配列を検証し、正規化したコピーを返す。不正は ValueError。

    検証項目 (judgment_points.md §3.2 の finalize 検証のうち保存時に決まるもの):
    - start は "HH:MM" で、一日の流れ順に厳密昇順 (同時刻も不可)。
      「流れ順」の基準は ``order_key`` が決める — 深夜跨ぎのライフでは暦の
      時刻順と一致しないため (:func:`day_order_minutes` 参照)
    - kind はコマ種別カタログの有効な語彙 (:data:`ALL_KINDS`) のみ。封印済みの
      旧語彙 (六型 + 暮らし/休む) は拒否する
    - ref は "task:N" / "desire:N" / "none"。作業セッション系でない kind は
      "none" 必須
    - facility は非空文字列 (building_id or "own_room")
    - budget_rounds は非負 int (bool は不可)
    - title は文字列 (「○○をする」という短い表題。旧データは無いので省略可 = "")
    - status は既知の値のみ (省略時 pending)

    Args:
        ascending_from: 厳密昇順を検証する開始 index。残りコマの全置換
            (:func:`replace_remaining_slots`) では消化済みコマ (帳簿) を先頭に
            残すため、昇順検証は **新コマ区間のみ** に適用する — 消化済み区間との
            境界を跨いだ比較はしない (直前に消化したコマと同時刻・過去時刻の
            新コマは正当な組み替えであり、EventScheduler は過去時刻を即時扱い
            する。予約 key はコマの不変 id ベースなので同時刻でも衝突しない)。
            index < ascending_from のコマは**消化済みの帳簿**であり、昇順に
            加えて kind 語彙・kind/ref 整合の検証も受けない (kind は非空文字列
            であればよい) — 封印前の旧 kind で消化されたコマを含む時間割の
            日中組み替えを、歴史の語彙を理由に全却下しないため (時間割改修 T1
            の移行互換。他のフィールドの構造検証は全て受ける)。
            2026-07-05 実 LLM シム 3回目: 消化済み 13:30 コマの直後に 13:30 の
            新コマを置く組み替えが「昇順でない」で全却下された不具合の修正。
        order_key: "HH:MM" を並び順の数値へ変換する関数。省略時は暦の時刻順
            (ライフ未宣言の日の後方互換)。ライフのある日は呼び出し元が
            :func:`day_order_minutes` を束ねて渡し、深夜跨ぎでも「一日の流れ」
            の順で検証させる。
        fresh_ids_from: この index 以降のコマは、入力が id を持っていても**必ず
            新しい id を採番する** (id の新世代化)。置換経路
            (:func:`replace_day_plan` は 0 / :func:`replace_remaining_slots` は
            消化済み区間の直後) が使う — 呼び出し元 (LLM の判断出力等) が旧コマを
            id ごと写して渡すと、新旧の予約 key (:func:`_slot_key`) が同一になり
            「cancel 失敗の残留予約は id 不一致で無害」という置換の安全性が
            無効化されるため (2026-07-20 Codex レビュー第四陣 P2)。None (省略) は
            従来どおり有効な既存 id を保持する (保存・編集経路の安定 identity)。
    """
    if order_key is None:
        order_key = _life_minutes
    if not isinstance(slots, list) or not slots:
        raise ValueError("slots must be a non-empty list")

    normalized: List[Dict[str, Any]] = []
    seen_ids: set = set()
    prev_minutes = -1
    for i, slot in enumerate(slots):
        if not isinstance(slot, dict):
            raise ValueError(f"slot[{i}] must be a dict (got {type(slot).__name__})")

        start = slot.get("start")
        if not isinstance(start, str) or not _TIME_RE.match(start):
            raise ValueError(f"slot[{i}].start must be 'HH:MM' (got {start!r})")
        if i >= ascending_from:
            minutes = order_key(start)
            if minutes <= prev_minutes:
                raise ValueError(
                    f"slot[{i}].start={start!r} is not strictly ascending "
                    "(slots must be sorted in the order of the day)"
                )
            prev_minutes = minutes

        kind = slot.get("kind")
        if i >= ascending_from:
            if kind not in ALL_KINDS:
                raise ValueError(
                    f"slot[{i}].kind={kind!r} is not a valid kind {ALL_KINDS}"
                )
        elif not isinstance(kind, str) or not kind:
            # 帳簿区間 (消化済み) は旧語彙を含め素通し — ただし文字列であること
            raise ValueError(
                f"slot[{i}].kind must be a non-empty string (got {kind!r})"
            )

        ref = slot.get("ref", REF_NONE)
        if not isinstance(ref, str) or (ref != REF_NONE and not _REF_RE.match(ref)):
            raise ValueError(
                f"slot[{i}].ref={ref!r} must be 'task:N', 'desire:N', 'track:N' or 'none'"
            )
        if (
            i >= ascending_from
            and kind not in WORKER_SESSION_KINDS
            and ref != REF_NONE
        ):
            # 目的参照 (予算・帰属) は作業セッション系のコマだけが持てる
            raise ValueError(
                f"slot[{i}]: kind={kind!r} must have ref='none' (got {ref!r})"
            )

        facility = slot.get("facility")
        if not isinstance(facility, str) or not facility.strip():
            raise ValueError(
                f"slot[{i}].facility must be a building_id or 'own_room' (got {facility!r})"
            )

        budget = slot.get("budget_rounds", 0)
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 0:
            raise ValueError(
                f"slot[{i}].budget_rounds must be a non-negative int (got {budget!r})"
            )

        title = slot.get("title", "")
        if not isinstance(title, str):
            raise ValueError(f"slot[{i}].title must be a string (got {type(title).__name__})")

        note = slot.get("note", "")
        if not isinstance(note, str):
            raise ValueError(f"slot[{i}].note must be a string (got {type(note).__name__})")

        status = slot.get("status", STATUS_PENDING)
        if status not in SLOT_STATUSES:
            raise ValueError(f"slot[{i}].status={status!r} is not one of {SLOT_STATUSES}")

        defer_count = slot.get("defer_count", 0)
        if isinstance(defer_count, bool) or not isinstance(defer_count, int) or defer_count < 0:
            raise ValueError(
                f"slot[{i}].defer_count must be a non-negative int (got {defer_count!r})"
            )

        skip_reason = slot.get("skip_reason", "")
        if not isinstance(skip_reason, str):
            raise ValueError(
                f"slot[{i}].skip_reason must be a string (got {type(skip_reason).__name__})"
            )

        record_level = slot.get("record_level", "")
        if not isinstance(record_level, str):
            raise ValueError(
                f"slot[{i}].record_level must be a string (got {type(record_level).__name__})"
            )

        close_outcome = slot.get("close_outcome", "")
        if not isinstance(close_outcome, str):
            raise ValueError(
                f"slot[{i}].close_outcome must be a string (got {type(close_outcome).__name__})"
            )

        # 不変 ID (コマの stable identity)。既存を保持し、無ければ採番する。
        # 配列 index はハンドラ中の時間割組み替え (post_session の
        # replace_remaining_slots) で移動しうるため、発火・精算・回復・冪等キー・
        # EventScheduler の予約 key は index ではなくこの ID で対象コマを一意に指す
        # (A6 二重 done の是正)。plan 内での重複は許さない — id が唯一の照準に
        # なったため、複製コマ (LLM が既存コマを id ごと写した等) は後の方に
        # 新しい id を振り直す。置換経路は fresh_ids_from で世代ごと採番し直す
        # (docstring 参照)。
        slot_id = slot.get("id")
        force_fresh = fresh_ids_from is not None and i >= fresh_ids_from
        if (
            force_fresh
            or not isinstance(slot_id, str)
            or not slot_id
            or slot_id in seen_ids
        ):
            slot_id = uuid.uuid4().hex[:12]
        seen_ids.add(slot_id)

        normalized_slot = {
            "id": slot_id,
            "start": start,
            "kind": kind,
            "ref": ref,
            "facility": facility.strip(),
            "budget_rounds": budget,
            "title": title.strip(),
            "note": note,
            "status": status,
            "defer_count": defer_count,
        }
        # skipped の理由・完了記録の詳しさ・締めの結果は帳簿の一部 — 消化済み
        # コマを残す全置換 (replace_remaining_slots) の再検証を通っても保持する
        # (close_outcome の欠落は「帰属済みなのに未済扱い → post_session が
        # 再棚入れ」の二重宣言を招く。Codex 二巡目 #1)。
        if skip_reason:
            normalized_slot["skip_reason"] = skip_reason
        if record_level:
            normalized_slot["record_level"] = record_level
        if close_outcome:
            normalized_slot["close_outcome"] = close_outcome
        normalized.append(normalized_slot)
    return normalized


def _validate_and_normalize_for_save(
    manager: Any,
    persona_id: str,
    plan_date_str: str,
    slots: List[Dict[str, Any]],
    *,
    fresh_ids: bool = False,
    ledger_prefix: int = 0,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """保存前の検証 + ライフ範囲正規化 (``save_day_plan`` / ``replace_day_plan`` 共通)。

    純粋な読み取り + 計算のみ (DB もスケジューラも一切変更しない) — 呼び出し元が
    「検証を先に済ませてから (cancel を含む) 破壊的操作に入る」原子性を組み立てる
    ための土台。書式検証は「一日の流れ」の順で昇順を見る (深夜跨ぎのライフでは
    暦の時刻順と一致しない :func:`day_order_minutes`。ライフ未宣言の日は暦順に
    退化する)。続けてライフ宣言日は :func:`_normalize_slots_within_organized_range`
    で「今〜就寝」の編成できる範囲へ丸め・部分救済する。

    Args:
        fresh_ids: True で全コマの id を新世代へ採番し直す (入力の id は無視)。
            全置換 (:func:`replace_day_plan`) 用 — 旧コマの id 持ち越しによる
            予約 key 衝突を契約レベルで封じる。保存・編集 (:func:`save_day_plan`)
            は False で既存 id を保持する。
        ledger_prefix: 先頭のこの件数を**帳簿区間** (消化済み扱い) として検証する
            (時間割改修 T2)。テンプレート経路の「流れた」コマ (status=skipped、
            :data:`SKIP_REASON_MISSED_START`) がここに入る — 帳簿区間は昇順・
            kind 語彙の検証を受けず (``_validate_and_normalize_slots`` の
            ``ascending_from`` と同じ意味論)、**組織化範囲の丸め・除外もしない**
            (過去開始のまま「流れた」と正直に記録するのが §11-12 裁定。丸めは
            pending 区間の数分のズレ救済に限定)。0 (既定) = 全コマを通常検証。

    Returns:
        ``(kept, notes)``。``kept`` は生き残ったコマ、``notes`` は日常語の調整メモ。

    Raises:
        ValueError: コマ配列の書式検証失敗 / 正規化後にコマが 1 件も残らなかった
            場合 (帳簿区間だけが残った場合は正当 — 全コマが流れた日も記録する)。
    """
    lives = get_lives(manager, persona_id, plan_date_str)
    normalized = _validate_and_normalize_slots(
        slots, ascending_from=ledger_prefix,
        order_key=lambda h: day_order_minutes(lives, h),
        fresh_ids_from=0 if fresh_ids else None,
    )
    ledger_part = normalized[:ledger_prefix]
    kept_new, notes = _normalize_slots_within_organized_range(
        manager, persona_id, plan_date_str, normalized[ledger_prefix:],
    )
    kept = ledger_part + kept_new
    if not kept:
        reasons = "; ".join(n.strip("（）") for n in notes) or "コマが活動時間の範囲外でした"
        raise ValueError(f"編成できる範囲 (今〜就寝) に収まるコマがありませんでした ({reasons})")
    return kept, notes


def save_day_plan(
    manager: Any, persona_id: str, plan_date: Any, slots: List[Dict[str, Any]]
) -> List[str]:
    """時間割を検証して upsert する (1 ペルソナ 1 日 1 行)。

    ライフが宣言されている日 (life.md v0.5 §4.4/§11.2/§3 追補) は、保存前に
    :func:`_normalize_slots_within_organized_range` でコマを「今〜就寝」の
    編成できる範囲へ正規化する — 過去開始のコマは現在時刻へ丸め (クランプ)、
    丸めてもなお範囲外のコマだけを個別に除外する (部分救済。3 分のズレで
    一日を全滅させない)。ライフが宣言されていない日は skip = 後方互換。

    Returns:
        調整メモ (List[str])。丸め・除外が起きた場合の日常語の説明
        (「n番目の予定は開始時刻を...に調整しました」等)。無調整なら空リスト。

    Raises:
        ValueError: persona_id 空 / plan_date 不正 / コマ配列の書式検証失敗 /
            正規化後に編成できる範囲へ収まるコマが 1 件も残らなかった場合
            (旧「時間割なし」相当。理由は調整メモを埋め込んだメッセージに残す)。
    """
    if not persona_id:
        raise ValueError("persona_id is required")
    plan_date_str = _normalize_plan_date(plan_date)
    kept, notes = _validate_and_normalize_for_save(
        manager, persona_id, plan_date_str, slots,
    )
    _upsert_plan_slots(manager, persona_id, plan_date_str, kept)
    if notes:
        LOGGER.info(
            "[day_plan] slots adjusted to organized range: persona=%s date=%s notes=%s",
            persona_id, plan_date_str, notes,
        )
    return notes


_UNCONDITIONAL = object()


def _upsert_plan_slots(
    manager: Any,
    persona_id: str,
    plan_date_str: str,
    normalized: List[Dict[str, Any]],
    *,
    expected_payload: Any = _UNCONDITIONAL,
) -> bool:
    """検証済みコマ配列を upsert する (save_day_plan / replace 系共用)。

    Args:
        expected_payload: 省略 (既定) = 無条件で upsert する (全置換 =
            :func:`replace_day_plan` / 新規保存の意味論)。文字列 = 「slots_json が
            この値のときだけ」更新する条件付きモード (:func:`replace_remaining_slots`
            の CAS — 読んだ世代が古ければ書かない、第五陣 P1)。None = 「行が
            まだ無いときだけ」INSERT する。

    Returns:
        書けたら True。条件付きモードで世代が合わず書かなかったら False
        (呼び出し元が最新 plan で再構築して再試行する)。
    """
    from database.models import PersonaDayPlan

    now = clock.now()
    payload = json.dumps(normalized, ensure_ascii=False)
    ok = False
    db = manager.SessionLocal()
    try:
        if expected_payload is _UNCONDITIONAL or expected_payload is None:
            row = (
                db.query(PersonaDayPlan)
                .filter_by(persona_id=persona_id, plan_date=plan_date_str)
                .first()
            )
            if row is None:
                db.add(PersonaDayPlan(
                    persona_id=persona_id,
                    plan_date=plan_date_str,
                    slots_json=payload,
                    created_at=now,
                    updated_at=now,
                ))
                db.commit()
                ok = True
            elif expected_payload is _UNCONDITIONAL:
                row.slots_json = payload
                row.updated_at = now
                db.commit()
                ok = True
            else:
                # 「行が無いときだけ」を期待したが、別の書き手が先に作っていた
                ok = False
        else:
            changed = (
                db.query(PersonaDayPlan)
                .filter(
                    PersonaDayPlan.persona_id == persona_id,
                    PersonaDayPlan.plan_date == plan_date_str,
                    PersonaDayPlan.slots_json == expected_payload,
                )
                .update(
                    {
                        PersonaDayPlan.slots_json: payload,
                        PersonaDayPlan.updated_at: now,
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
            ok = bool(changed)
    finally:
        db.close()
    if ok:
        LOGGER.info(
            "[day_plan] saved: persona=%s date=%s slots=%d",
            persona_id, plan_date_str, len(normalized),
        )
    return ok


def load_day_plan(manager: Any, persona_id: str, plan_date: Any) -> Optional[List[Dict[str, Any]]]:
    """保存済み時間割のコマ配列を返す。無ければ None。"""
    plan_date_str = _normalize_plan_date(plan_date)
    from database.models import PersonaDayPlan

    db = manager.SessionLocal()
    try:
        row = (
            db.query(PersonaDayPlan)
            .filter_by(persona_id=persona_id, plan_date=plan_date_str)
            .first()
        )
        if row is None:
            return None
        return json.loads(row.slots_json)
    finally:
        db.close()


_CAS_MAX_RETRIES = 5


def _mutate_slots_cas(
    manager: Any,
    persona_id: str,
    plan_date_str: str,
    mutate: Callable[[List[Dict[str, Any]]], Optional[Any]],
    *,
    context: str = "",
) -> Optional[Any]:
    """slots_json を「読んだ世代と同じとき**だけ**」書き換える CAS 更新 (lost update 防止)。

    「読む → 手元で変異 → 無条件で全体を書き戻す」という旧 ``_write_slots`` 方式は、
    読みと書きの間に別の書き手 (:func:`replace_remaining_slots` 等) が commit すると、
    古い配列の書き戻しが**その置換を静かに消す** (2026-07-20 Codex レビュー第五陣 P1
    — ペルソナが決めた組み替えの喪失 = データ保全問題)。

    ここでは読み・変異・保存を同一トランザクションにまとめ、保存は
    「``UPDATE ... WHERE slots_json = 読んだ時点の payload``」の条件付き一括更新で
    行う — 世代が変わっていれば 1 行も更新されず (rowcount 0)、最新 plan を読み
    直して変異からやり直す (最大 ``_CAS_MAX_RETRIES`` 回)。

    Args:
        mutate: そのループ回で読んだ**最新の**コマ配列を受け取り、書くべき変更が
            あれば配列をその場で書き換えて任意の非 None 値 (成功時に呼び出し元へ
            返す結果) を返す。None を返すと何も書かずに全体を None で終える
            (対象コマ消失などの中止)。再試行のたびに**新しい配列で呼び直される**
            ため、対象の解決 (id 逆引き等) は必ずこの中で行うこと。
        context: ログ用の呼び出し元ラベル。

    Returns:
        mutate の返した結果 (書き込み成功時) / None (行なし・中止・再試行枯渇)。
    """
    from database.models import PersonaDayPlan

    for attempt in range(_CAS_MAX_RETRIES):
        db = manager.SessionLocal()
        try:
            row = _load_plan_row(db, persona_id, plan_date_str)
            if row is None:
                LOGGER.warning(
                    "[day_plan] cannot mutate slots (%s): plan row missing "
                    "(persona=%s date=%s)", context, persona_id, plan_date_str,
                )
                return None
            original_payload = row.slots_json
            slots = _row_slots(row)
            result = mutate(slots)
            if result is None:
                return None
            changed = (
                db.query(PersonaDayPlan)
                .filter(
                    PersonaDayPlan.persona_id == persona_id,
                    PersonaDayPlan.plan_date == plan_date_str,
                    PersonaDayPlan.slots_json == original_payload,
                )
                .update(
                    {
                        PersonaDayPlan.slots_json: json.dumps(
                            slots, ensure_ascii=False,
                        ),
                        PersonaDayPlan.updated_at: clock.now(),
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
            if changed:
                return result
        finally:
            db.close()
        LOGGER.info(
            "[day_plan] slots CAS conflict (%s): plan changed since read; retrying "
            "with fresh plan (persona=%s date=%s attempt=%d/%d)",
            context, persona_id, plan_date_str, attempt + 1, _CAS_MAX_RETRIES,
        )
    LOGGER.error(
        "[day_plan] slots CAS gave up after %d attempts (%s) — update NOT applied "
        "(persona=%s date=%s)",
        _CAS_MAX_RETRIES, context, persona_id, plan_date_str,
    )
    return None


def _update_slot(
    manager: Any,
    persona_id: str,
    plan_date_str: str,
    index: int,
    *,
    expected_id: Optional[str] = None,
    **changes: Any,
) -> Optional[Dict[str, Any]]:
    """slot[index] に changes を適用して永続化し、更新後の slot を返す。

    Args:
        expected_id: 対象コマの不変 id。指定時、読み直した plan で ``slots[index]``
            の id が一致しなければ **id で現在位置を引き直す** — 呼び出し元が
            index を掴んでからこの書き込みまでの間に時間割が組み替わっても、
            別コマを書き換えない (発火経路の照準保持、2026-07-20 Codex レビュー
            第四陣 P1 と同族の窓)。id が plan から消えていれば何も書かず None を
            返す。

    保存は :func:`_mutate_slots_cas` (読んだ世代と同じときだけ書く条件付き更新) —
    照合と保存の間に別の書き手が commit しても、その決定を古い配列で上書きしない
    (第五陣 P1)。
    """

    def _apply(slots: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        target = index
        if expected_id is not None:
            if target >= len(slots) or slots[target].get("id") != expected_id:
                resolved = _find_slot_index_by_id(slots, expected_id)
                if resolved is None:
                    LOGGER.warning(
                        "[day_plan] cannot update slot: id=%s vanished from plan "
                        "(persona=%s date=%s stale_index=%d)",
                        expected_id, persona_id, plan_date_str, index,
                    )
                    return None
                target = resolved
        elif target >= len(slots):
            LOGGER.warning(
                "[day_plan] cannot update slot: not found (persona=%s date=%s index=%d)",
                persona_id, plan_date_str, index,
            )
            return None
        slots[target].update(changes)
        return slots[target]

    return _mutate_slots_cas(
        manager, persona_id, plan_date_str, _apply, context="update_slot",
    )


# ---------------------------------------------------------------------------
# 日付付帯情報 (meta_json): tomorrow_memo (明日の自分へのメモ) 等の置き場
# ---------------------------------------------------------------------------


def load_plan_meta(
    manager: Any, persona_id: str, plan_date: Any, *, strict: bool = False
) -> Dict[str, Any]:
    """plan 行の付帯情報 (meta_json) を dict で返す。行なし / 不正 JSON は空 dict。

    就寝判断 (day_close) が書いた ``tomorrow_memo`` 等を、翌朝の起床判断
    (day_open) が読む入口 (judgment_points.md §4/§8)。

    Args:
        strict: True で「壊れていて読めない」(不正 JSON / dict でない) を
            例外にする。既定 (False) は空 dict へ縮退 = 従来どおり
            「付帯情報が無い日」と同じ扱い。**壊れた行を「無い」と読むと
            危険な判断** (:func:`resolve_business_day` の営業日選択) だけが
            True で呼ぶ — 行が無い日と壊れた行を混同しないため。

    Raises:
        ValueError: ``strict`` かつ meta_json が壊れている場合。
    """
    plan_date_str = _normalize_plan_date(plan_date)
    from database.models import PersonaDayPlan

    db = manager.SessionLocal()
    try:
        row = (
            db.query(PersonaDayPlan)
            .filter_by(persona_id=persona_id, plan_date=plan_date_str)
            .first()
        )
        if row is None or not row.meta_json:
            return {}
        try:
            meta = json.loads(row.meta_json)
        except (TypeError, ValueError):
            if strict:
                raise ValueError(
                    f"meta_json is not valid JSON (persona={persona_id} "
                    f"date={plan_date_str})"
                )
            LOGGER.warning(
                "[day_plan] meta_json is not valid JSON (persona=%s date=%s); returning {}",
                persona_id, plan_date_str,
            )
            return {}
        if not isinstance(meta, dict):
            if strict:
                raise ValueError(
                    f"meta_json is not a JSON object (persona={persona_id} "
                    f"date={plan_date_str} got={type(meta).__name__})"
                )
            return {}
        return meta
    finally:
        db.close()


def mutate_plan_meta(
    manager: Any,
    persona_id: str,
    plan_date: Any,
    mutate: Callable[[Dict[str, Any]], Optional[Any]],
    *,
    context: str = "",
    in_session_extra: Optional[Callable[[Any], None]] = None,
) -> Optional[Any]:
    """meta_json を CAS 再試行つきで変異させる — **増分・引き継ぎ計算の正しい口**。

    「外で meta を読んで完成値を作り、:func:`update_plan_meta` へ渡す」型は、
    CAS が最新 meta を読み直しても ``merged.update(完成値)`` が古い完成値で同じ
    キーを上書きするため、並走した積算 (judgment_pulses 等) が失われる
    (2026-07-20 Codex レビュー第七陣 P1)。読み → 増分計算 → 保存を**同じ CAS
    試行の内側**で行うのが本関数 — 競合のたびに ``mutate`` が**最新 meta** で
    呼び直され、増分が最新値の上に積まれる。

    行が無ければ ``mutate({})`` を評価し、書くもの (非 None) があれば meta のみの
    行 (slots_json="[]") を新規作成する (:func:`update_plan_meta` の従来挙動)。
    ``mutate`` が None を返したら何も書かず None で終える (対象なし等の no-op —
    行も作らない)。

    Args:
        mutate: その試行で読んだ**最新の** meta dict を受け取り、書くべき変更が
            あれば dict をその場で書き換えて任意の非 None 値 (呼び出し元へ返す
            結果) を返す。None = 書かずに中止。
        in_session_extra: 書き込みが確定する試行 (CAS 勝ち / 新規行 INSERT) の
            **commit 直前**に、同じ Session を渡して一度だけ呼ばれるフック
            (W5)。meta の書き込みと追加の world-DB 書き込み (実行台帳の
            applied 遷移 + outbox 積みなど) を**単一 commit** に同梱するための
            口。例外は試行ごと rollback して伝播する (meta も書かれない)。
            CAS 負け試行では呼ばれない。mutate が None (no-op) のときも
            呼ばれない。

    Returns:
        mutate の返した結果 (書き込み成功時) / None (中止時)。

    Raises:
        RuntimeError: 再試行が枯渇した場合 (書けていない — silent 消失にしない)。
    """
    plan_date_str = _normalize_plan_date(plan_date)
    from sqlalchemy.exc import IntegrityError

    from database.models import PersonaDayPlan

    for _attempt in range(_CAS_MAX_RETRIES):
        now = clock.now()
        db = manager.SessionLocal()
        try:
            row = _load_plan_row(db, persona_id, plan_date_str)
            if row is None:
                meta: Dict[str, Any] = {}
                result = mutate(meta)
                if result is None:
                    return None
                db.add(PersonaDayPlan(
                    persona_id=persona_id,
                    plan_date=plan_date_str,
                    slots_json="[]",
                    meta_json=json.dumps(meta, ensure_ascii=False),
                    created_at=now,
                    updated_at=now,
                ))
                if in_session_extra is not None:
                    try:
                        in_session_extra(db)
                    except Exception:
                        db.rollback()
                        raise
                try:
                    db.commit()
                    return result
                except IntegrityError:
                    # 並走の書き手が先に行を作った — 更新経路で再試行
                    db.rollback()
                    continue
            original_meta = row.meta_json
            meta = _row_meta(row)
            result = mutate(meta)
            if result is None:
                return None
            changed = (
                db.query(PersonaDayPlan)
                .filter(
                    PersonaDayPlan.persona_id == persona_id,
                    PersonaDayPlan.plan_date == plan_date_str,
                    PersonaDayPlan.meta_json == original_meta,
                )
                .update(
                    {
                        PersonaDayPlan.meta_json: json.dumps(
                            meta, ensure_ascii=False,
                        ),
                        PersonaDayPlan.updated_at: now,
                    },
                    synchronize_session=False,
                )
            )
            if changed and in_session_extra is not None:
                try:
                    in_session_extra(db)
                except Exception:
                    db.rollback()
                    raise
            db.commit()
            if changed:
                return result
        finally:
            db.close()
        LOGGER.info(
            "[day_plan] plan meta CAS conflict (%s): meta changed since read; "
            "retrying with fresh meta (persona=%s date=%s attempt=%d/%d)",
            context, persona_id, plan_date_str, _attempt + 1, _CAS_MAX_RETRIES,
        )
    raise RuntimeError(
        f"plan meta mutation kept conflicting with concurrent writes "
        f"(persona={persona_id} date={plan_date_str} context={context})"
    )


def update_plan_meta(
    manager: Any, persona_id: str, plan_date: Any, updates: Dict[str, Any]
) -> Dict[str, Any]:
    """plan 行の付帯情報 (meta_json) へ updates をマージして永続化する。

    行が無ければ meta のみの行 (slots_json="[]") を作る — 就寝判断が「時間割の
    無かった日」にも明日の自分へのメモを残せるようにするため。slots_json="[]" は
    ``load_day_plan`` では空配列、``schedule_day_plan`` では push 0 件として
    無害に振る舞う (save_day_plan で本物の時間割が上書きされたら meta は残る)。

    保存は :func:`mutate_plan_meta` の CAS — 読みと書きの間に別の meta 書き込みが
    commit されても、古い meta の書き戻しでそれを消さない (第六陣 P1)。

    **注意 (第七陣 P1)**: ここへ渡す updates は「上書きしてよい完成値」だけに
    すること。既存 meta から計算した増分・引き継ぎ値を渡すと、CAS が最新 meta を
    読み直しても古い完成値で上書きして並走の積算を消す。増分・引き継ぎは
    :func:`mutate_plan_meta` に計算ごと渡す。

    Returns:
        マージ後の meta dict。

    Raises:
        RuntimeError: 再試行が枯渇した場合 (書けていない — 呼び出し元へ正直に表明)。
    """
    if not isinstance(updates, dict):
        raise ValueError(f"updates must be a dict (got {type(updates).__name__})")

    def _merge(meta: Dict[str, Any]) -> Dict[str, Any]:
        meta.update(updates)
        return meta

    merged = mutate_plan_meta(
        manager, persona_id, plan_date, _merge, context="update_plan_meta",
    )
    # _merge は常に非 None を返すので、ここで merged が None になることはない
    return merged if merged is not None else dict(updates)


# ---------------------------------------------------------------------------
# 日次予算台帳 (v2 §4.5): meta_json の budget_total_rounds / budget_used_rounds
# ---------------------------------------------------------------------------


def _read_nonneg_int(value: Any) -> Optional[int]:
    """非負 int なら int を、そうでなければ None を返す (bool は不可)。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def init_budget_ledger(
    manager: Any, persona_id: str, plan_date: Any, total_rounds: int
) -> Dict[str, int]:
    """日次予算台帳を初期化する (起床判断 day_open の finalize が編成時に呼ぶ)。

    total を書き、used は既存値を保持する (起床判断のやり直しで消費済み分は
    リセットされない — 使った予算は使ったまま)。

    Returns:
        :func:`get_budget_state` と同形の ``{"total", "used", "remaining"}``。
    """
    total = _read_nonneg_int(total_rounds)
    if total is None:
        raise ValueError(f"total_rounds must be a non-negative int (got {total_rounds!r})")

    # used の引き継ぎは最新 meta から CAS の内側で計算する (外で読んだ古い used を
    # 完成値として書くと、並走した消費の積算を巻き戻す — 第七陣 P1)。
    def _init(meta: Dict[str, Any]) -> int:
        used = _read_nonneg_int(meta.get(META_BUDGET_USED)) or 0
        meta[META_BUDGET_TOTAL] = total
        meta[META_BUDGET_USED] = used
        return used

    used = mutate_plan_meta(
        manager, persona_id, plan_date, _init, context="init_budget_ledger",
    )
    used = used if used is not None else 0
    LOGGER.info(
        "[day_plan] budget ledger initialized: persona=%s date=%s total=%d used=%d",
        persona_id, _normalize_plan_date(plan_date), total, used,
    )
    return {"total": total, "used": used, "remaining": max(0, total - used)}


def get_budget_state(
    manager: Any, persona_id: str, plan_date: Any
) -> Optional[Dict[str, Any]]:
    """日次予算の残高 ``{"total", "used", "remaining"}`` を返す。

    ライフ宣言がある日 (life.md Phase2 §7「ライフの世代交代」): 標準パルス予算
    (Σ lives.budget_pulses) を単位とし、消費はΣ (used_pulses + used_rounds ×
    :data:`LIFE_ROUND_BUDGET_FACTOR`)。**ライフの無い日は台帳が無ければ None**
    (:data:`META_BUDGET_TOTAL` 未設定) — 旧実装 (日次ラウンド台帳) にそのまま
    フォールバックする (後方互換。既存シグネチャ・呼び出し元は不変)。
    """
    plan_date_str = _normalize_plan_date(plan_date)
    lives = get_lives(manager, persona_id, plan_date_str)
    if lives:
        total = sum(int(life.get("budget_pulses") or 0) for life in lives)
        used = sum(life_consumed(life) for life in lives)
        return {"total": total, "used": used, "remaining": max(0.0, total - used)}
    meta = load_plan_meta(manager, persona_id, plan_date_str)
    total_rounds = _read_nonneg_int(meta.get(META_BUDGET_TOTAL))
    if total_rounds is None:
        return None
    used_rounds = _read_nonneg_int(meta.get(META_BUDGET_USED)) or 0
    return {
        "total": total_rounds, "used": used_rounds,
        "remaining": max(0, total_rounds - used_rounds),
    }


def consume_budget(
    manager: Any, persona_id: str, plan_date: Any, rounds: int
) -> Optional[Dict[str, int]]:
    """実際に消費したラウンド数を台帳へ積算する (発火後の実測値)。

    台帳 (total) がまだ無い日でも used だけは記録する — 後から day_open が
    :func:`init_budget_ledger` で total を書いたとき、既消費分が保持される。

    Returns:
        積算後の :func:`get_budget_state` (total 未設定の日は None)。
    """
    inc = _read_nonneg_int(rounds)
    if inc is None:
        LOGGER.warning(
            "[day_plan] consume_budget: rounds=%r is not a non-negative int; ignored",
            rounds,
        )
        return get_budget_state(manager, persona_id, plan_date)
    if inc == 0:
        return get_budget_state(manager, persona_id, plan_date)

    # 増分は最新 meta の上で CAS の内側で積む (外で読んだ古い used + inc の完成値
    # を書くと並走した消費が失われる — 第七陣 P1)。
    def _add(meta: Dict[str, Any]) -> int:
        new_used = (_read_nonneg_int(meta.get(META_BUDGET_USED)) or 0) + inc
        meta[META_BUDGET_USED] = new_used
        return new_used

    used = mutate_plan_meta(
        manager, persona_id, plan_date, _add, context="consume_budget",
    )
    state = get_budget_state(manager, persona_id, plan_date)
    LOGGER.info(
        "[day_plan] budget consumed: persona=%s date=%s +%d rounds (used=%d remaining=%s)",
        persona_id, _normalize_plan_date(plan_date), inc, used,
        state["remaining"] if state else "?",
    )
    return state


# ---------------------------------------------------------------------------
# ライフ (life.md Phase 2 §11.2「新設」): 宣言の永続化・予算台帳・境界イベント
# ---------------------------------------------------------------------------


def _life_minutes(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:])


def get_lives(
    manager: Any, persona_id: str, plan_date: Any, *, strict: bool = False
) -> List[Dict[str, Any]]:
    """保存済みライフ宣言 (meta_json.lives) を返す。無ければ空リスト。

    「lives が無い日 (旧データ・宣言なし) は検証もゲートも従来挙動」の判定は
    すべてこの関数の戻り値が空かどうかで行う (life.md §4.1)。

    Args:
        strict: True で「壊れていて読めない」を例外にする (:func:`load_plan_meta`
            の strict + lives が list でない / 要素が dict でない)。営業日の
            選択だけが True で呼ぶ — 壊れた台帳を「ライフ未宣言の日」と読むと、
            現行スケジュール基準で別の営業日を駆動してしまう。既定 (False) は
            従来どおり空リスト / 不正要素の除去へ縮退する。

    Raises:
        ValueError: ``strict`` かつ台帳が壊れている場合。
    """
    meta = load_plan_meta(manager, persona_id, plan_date, strict=strict)
    lives = meta.get(META_LIVES)
    if not isinstance(lives, list):
        if strict and lives is not None:
            raise ValueError(
                f"meta_json.{META_LIVES} is not a list (persona={persona_id} "
                f"got={type(lives).__name__})"
            )
        return []
    if strict and any(not isinstance(life, dict) for life in lives):
        raise ValueError(
            f"meta_json.{META_LIVES} contains non-object entries "
            f"(persona={persona_id})"
        )
    return [life for life in lives if isinstance(life, dict)]


def _life_is_overnight(life: Dict[str, Any]) -> bool:
    """深夜跨ぎのライフ (end <= start) かどうか (life.md v0.5 §4.1)。

    ``autonomy_wiring.is_overnight`` と同じ意味論 (close < wake)。ライフは
    :func:`confirm_life_for_today` がユーザー設定の起床・就寝からそのまま
    確定するため、跨ぎは異常ではなく正常形として扱う。
    """
    start = life.get("start")
    end = life.get("end")
    return bool(start and end and end <= start)


def _life_extended_minutes(life: Dict[str, Any], hhmm: str) -> int:
    """ライフの開始を 0 とした経過分。深夜跨ぎでは 1440 を超えうる。

    跨ぎライフ (例: 07:00〜01:00) で "23:30" と "03:00" を同じ数直線上に
    正しく並べるための変換 (life.md v0.5 §11.2「区間内判定の書き直し」)。
    hhmm がライフの開始より前の時刻なら「翌暦日の続き」とみなして +1440 する。
    """
    start_min = _life_minutes(life["start"])
    target_min = _life_minutes(hhmm)
    if target_min < start_min:
        target_min += 24 * 60
    return target_min - start_min


def _life_span_minutes(life: Dict[str, Any]) -> int:
    """ライフの長さ (分)。深夜跨ぎ込み (例: 07:00〜01:00 = 1080 分)。"""
    start_min = _life_minutes(life["start"])
    end_min = _life_minutes(life["end"])
    if end_min <= start_min:
        end_min += 24 * 60
    return end_min - start_min


def _hhmm_from_life_extended(life: Dict[str, Any], ext: int) -> str:
    """:func:`_life_extended_minutes` の逆変換: ライフ開始からの経過分を
    "HH:MM" に戻す (深夜跨ぎで 24:00 を超えても暦日内の時刻へ mod する)。

    コマの丸め (:func:`_normalize_slots_within_organized_range`) が、拡張分
    単位でクランプした後の値を保存用の "HH:MM" へ戻すために使う。
    """
    start_min = _life_minutes(life["start"])
    total = (start_min + ext) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def day_order_minutes(lives: List[Dict[str, Any]], hhmm: str) -> int:
    """コマの並び順キー: 「一日の始まり」を 0 とした経過分 (0..1439)。

    深夜跨ぎのライフ (例: 07:00〜01:00) では、**暦の時刻の大小が一日の
    前後関係と一致しない** — 就寝の "00:30" は朝の "07:30" より数字は
    小さいが、一日の流れでは後に来る。時刻の文字列で並べると就寝が先頭に
    立ち、以降の丸め・重複判定が全て狂う。

    2026-07-14 実機 (air_city_a, ライフ 07:00〜01:00): 就寝コマ "00:30" が
    暦順ソートで先頭に固定され、:func:`_normalize_slots_within_organized_range`
    がそれをライフ内の最後尾 (経過分 1050) と解釈した結果、後続コマが全て
    「直前のコマと衝突」と判定されて 00:31, 00:32... へ 1 分ずつ押し込まれ、
    一日の時間割が 00:30〜00:35 の 6 分間に潰れた。

    そこで**最初のライフの開始時刻をその日の起点 (0)** とし、そこからの
    経過分で並べる。ライフが宣言されていない日 (旧データ・宣言なし) は
    起点の概念が無いので暦の時刻をそのまま返す (後方互換)。
    """
    target = _life_minutes(hhmm)
    if not lives or not lives[0].get("start"):
        return target
    origin = _life_minutes(lives[0]["start"])
    return (target - origin) % (24 * 60)


def get_life_for_time(lives: List[Dict[str, Any]], hhmm: str) -> Optional[int]:
    """hhmm ("HH:MM") が属するライフの index を返す (無ければ None)。

    深夜跨ぎ (end <= start) を正常形として扱う (life.md v0.5 §4.1)。
    """
    for i, life in enumerate(lives):
        if not life.get("start") or not life.get("end"):
            continue
        ext = _life_extended_minutes(life, hhmm)
        if 0 <= ext < _life_span_minutes(life):
            return i
    return None


def is_keepalive_allowed(manager: Any, persona_id: str) -> bool:
    """keep-alive 連鎖のライフ従属ゲート (life.md §5.2)。

    その日 lives が宣言されていれば、現在時刻がいずれかのライフ区間内のときだけ
    True (谷では False = keep-alive を止める)。lives 未宣言の日 / ペルソナは
    常に True (旧挙動のまま — 完全後方互換)。判定失敗時は True にフォールバック
    する (安全側は「温め続ける」— keep-alive を止める方向に倒さない)。

    ``sea.runtime.SEARuntime.run_cache_keepalive`` が唯一の呼び出し元
    (keep-alive 連鎖の判定を 1 箇所に集約する設計、life.md Phase3)。

    見る営業日は :func:`resolve_business_day` — 予約・watchdog と同じ解決器
    (現在時刻を含む確定ライフ優先)。ここだけ現行 PersonaSchedule で日を決めると、
    起床設定を日中に変えた日はライフの真っ最中に「ライフ未宣言の日」を読む。
    """
    try:
        basis = resolve_business_day(manager, persona_id)
        if basis is None or not basis.lives:
            return True
        hhmm = clock.now().strftime("%H:%M")
        return get_life_for_time(basis.lives, hhmm) is not None
    except Exception:
        LOGGER.warning(
            "[day_plan] is_keepalive_allowed failed (persona=%s); defaulting to allow",
            persona_id, exc_info=True,
        )
        return True


def get_life_status_now(manager: Any, persona_id: str) -> Dict[str, Any]:
    """現在時刻のライフ状態 — life.md §9.1「話しかけやすさ」表示の唯一の判定源。

    試金石「エアは今話しかけて大丈夫か」への機械回答。API 層 (occupants の
    常在インジケータ / day-plan のライフ状態) はどちらもこの関数を呼び、
    判定ロジックを二重化しない。

    Returns:
        {
          "lives_declared": bool,   # その営業日にライフが宣言されているか
          "in_life": bool,          # lives_declared かつ現在時刻がいずれかの区間内
          "life_index": int | None, # in_life なら対象ライフの index (get_lives の並び)
          "life": dict | None,      # in_life なら対象ライフの宣言 dict そのもの
          "plan_date": str | None,  # 判定に使った営業日 ("YYYY-MM-DD")
        }

        判定失敗時は lives_declared=False 側にフォールバックする——「未宣言」
        表示 (何も出さない) の方が「熱くないのに熱いと見せる」より安全
        (不変条件5)。is_keepalive_allowed の「失敗時は許可側 (True)」とは
        安全方向が逆であることに注意 (あちらは延命を止めない方が安全、
        こちらは嘘の「話しかけやすい」を出さない方が安全)。

    見る営業日は :func:`resolve_business_day` — 予約・watchdog と同じ解決器。
    起床設定を日中に変えた日の深夜、ペルソナが確定ライフの真っ最中でも
    「未宣言」と表示していたのはここが現行 PersonaSchedule で日を決めていたため。
    """
    try:
        basis = resolve_business_day(manager, persona_id)
        if basis is None:
            # ライフを読めない = どの営業日の状態も判定できない。「未宣言」
            # (何も出さない) 側へ倒す — 上の Returns の方針そのもの。
            LOGGER.warning(
                "[day_plan] get_life_status_now: lives unreadable (persona=%s); "
                "reporting lives_declared=False", persona_id,
            )
            return {
                "lives_declared": False, "in_life": False,
                "life_index": None, "life": None, "plan_date": None,
            }
        if not basis.lives:
            return {
                "lives_declared": False, "in_life": False,
                "life_index": None, "life": None, "plan_date": basis.plan_date,
            }
        hhmm = clock.now().strftime("%H:%M")
        idx = get_life_for_time(basis.lives, hhmm)
        return {
            "lives_declared": True,
            "in_life": idx is not None,
            "life_index": idx,
            "life": basis.lives[idx] if idx is not None else None,
            "plan_date": basis.plan_date,
        }
    except Exception:
        LOGGER.warning(
            "[day_plan] get_life_status_now failed (persona=%s); "
            "defaulting to lives_declared=False", persona_id, exc_info=True,
        )
        return {
            "lives_declared": False, "in_life": False,
            "life_index": None, "life": None, "plan_date": None,
        }


def _organized_range_exclude_note(position: int) -> str:
    return f"（{position}番目の予定は活動時間の外のため外しました）"


def _organized_range_clamp_note(position: int, new_start: str) -> str:
    return f"（{position}番目の予定は開始時刻を{new_start}に調整しました）"


def _normalize_slots_within_organized_range(
    manager: Any, persona_id: str, plan_date_str: str, slots: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """コマ配列を「編成できる範囲」に正規化する (life.md v0.5 §3 の丸め方針)。

    v0.5 追補 (2026-07-14): 実機で「01:03 起床判断が 01:00〜02:00 のライフを
    見て slot[0]=01:00 の時間割を編成 → 過去時刻が保存時に raise → 時間割が
    1 件も保存されない」という全滅事故が起きた。**不正な値は弾くのでなく
    解釈で正規化する** (life.md §3) — 3 分のズレで一日を全滅させない。

    正規化方針 (旧 ``_check_slots_within_organized_range`` の raise を置換):

    - コマの start が「今」が属するライフ (``now_life``) より前なら、start を
      現在時刻へ丸める (クランプ)。``now_life`` に属さない (谷・就寝後等の)
      コマも、丸め先である ``now_life`` の拡張分に投影して同じ判定にかける —
      それでも「今より前」にならない (=後ろにありすぎる) ものは丸めようが
      ないので除外する
    - 複数コマが丸めで同時刻に競合する場合は、元の順序を保ったまま 1 分ずつ
      後ろへずらし、``_validate_and_normalize_slots`` が要求する「開始時刻の
      厳密昇順」と整合させる
    - ずらしてもなお ``now_life`` の範囲 (今〜就寝) をはみ出すコマは、
      そのコマだけ除外する (部分救済 — 1 コマの異常で時間割全体を潰さない)
    - ``now_life`` 以外の (将来の窓の) コマはそのまま通す (v0.4 まで同様、
      1 日 1 窓の初期実装では実質常に素通り)
    - 「今」自体がどのライフにも属さない (谷) 場合、丸め先が無いので該当
      コマは全て除外する
    - ライフが宣言されていない日 (:func:`get_lives` が空) は何もしない
      (旧データ・宣言なしの日は「編成できる範囲」の概念自体が無い —
      後方互換最優先)

    ``save_day_plan`` / ``replace_remaining_slots`` の両方が呼ぶことで、
    起床判断以外の経路 (会話終了判断の残り時間割編集等) からの保存も守る。

    Args:
        slots: ``_validate_and_normalize_slots`` 済みのコマ配列 (書式は保証
            済み)。この関数は書式検証をしない — 呼び出し元が先に済ませること。

    Returns:
        ``(kept, notes)``。``kept`` は生き残ったコマ (丸めた分は start を
        更新したコピー)。``notes`` は日常語の調整メモ (「n番目の予定は...」、
        n は ``slots`` 内の 1-based 位置)。無調整なら空リスト。
    """
    lives = get_lives(manager, persona_id, plan_date_str)
    if not lives:
        return list(slots), []

    now_hhmm = clock.now().strftime("%H:%M")
    now_life_idx = get_life_for_time(lives, now_hhmm)
    now_life = lives[now_life_idx] if now_life_idx is not None else None
    now_ext = _life_extended_minutes(now_life, now_hhmm) if now_life is not None else None

    kept: List[Dict[str, Any]] = []
    notes: List[str] = []
    # now_life 内で丸めた直前のコマの拡張分 (昇順維持のための下限)。
    floor_ext: Optional[int] = None

    for i, slot in enumerate(slots):
        position = i + 1
        start = slot.get("start")
        life_idx = get_life_for_time(lives, start)

        if life_idx is not None and life_idx != now_life_idx:
            # 「今」以外の (将来の) 窓に属するコマ — 手を加えず通す。
            kept.append(slot)
            continue

        if now_life is None:
            # 「今」がどのライフにも属していない (谷) — 丸め先が無い。
            notes.append(_organized_range_exclude_note(position))
            continue

        ext = _life_extended_minutes(now_life, start)
        clamped = False
        if ext < now_ext:
            # 過去開始 (今より前) — 現在時刻へ丸める。
            ext = now_ext
            clamped = True
        if floor_ext is not None and ext <= floor_ext:
            # 丸め済みの直前コマと同時刻以下になる衝突 — 1 分ずらす。
            ext = floor_ext + 1
            clamped = True

        if ext >= _life_span_minutes(now_life):
            # 丸めて (ずらして) もなお活動時間の外 — このコマだけ除外。
            notes.append(_organized_range_exclude_note(position))
            continue

        floor_ext = ext
        new_start = _hhmm_from_life_extended(now_life, ext)
        if clamped and new_start != start:
            notes.append(_organized_range_clamp_note(position, new_start))
            slot = {**slot, "start": new_start}
        kept.append(slot)

    return kept, notes


def _validate_and_normalize_lives(lives: Any) -> List[Dict[str, Any]]:
    """ライフ配列の型検証 (v0.5): システムが構築した値の型だけを守る。

    v0.4 までの LLM 宣言口前提の検証 (ライフ同士の重なり・谷コマ・均等モード
    間隔) は**廃止した**——ライフはユーザー設定 (PersonaSchedule の起床・
    就寝) からシステム (:func:`confirm_life_for_today`) が確定するため、
    その手の不整合は書ける口ごと構造的に無くなった (life.md v0.5 §3
    「不正な値は検証で弾くのでなく、書ける口をなくす」)。

    検証項目 (フォーマットのみ):
    - start/end は "HH:MM"
    - start と end が同一でない (長さ 0 のライフは無効)
    - budget_pulses は正の int
    - mode は "even" / "free" のみ

    深夜跨ぎ (end <= start、例 07:00〜01:00) はここでは**正常形として許容**
    する (:func:`autonomy_wiring.in_waking_window` と同じ意味論)。

    Raises:
        ValueError: 上記いずれかの違反。
    """
    if not isinstance(lives, list):
        raise ValueError(f"lives must be a list (got {type(lives).__name__})")
    if not lives:
        return []

    normalized: List[Dict[str, Any]] = []
    for i, life in enumerate(lives):
        if not isinstance(life, dict):
            raise ValueError(f"lives[{i}] must be a dict (got {type(life).__name__})")
        start = life.get("start")
        if not isinstance(start, str) or not _TIME_RE.match(start):
            raise ValueError(f"lives[{i}].start must be 'HH:MM' (got {start!r})")
        end = life.get("end")
        if not isinstance(end, str) or not _TIME_RE.match(end):
            raise ValueError(f"lives[{i}].end must be 'HH:MM' (got {end!r})")
        if end == start:
            raise ValueError(
                f"lives[{i}]: start と end が同一です ({start!r}) — 長さ 0 のライフは無効です"
            )
        budget = life.get("budget_pulses")
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
            raise ValueError(
                f"lives[{i}].budget_pulses must be a positive int (got {budget!r})"
            )
        mode = life.get("mode")
        if mode not in LIFE_MODES:
            raise ValueError(f"lives[{i}].mode must be one of {LIFE_MODES} (got {mode!r})")
        normalized.append({"start": start, "end": end, "budget_pulses": budget, "mode": mode})
    return normalized


def save_lives(
    manager: Any, persona_id: str, plan_date: Any, lives: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """ライフを検証して保存する (v0.5: システムが day_open 確定時に呼ぶ書き手)。

    既存ライフと (start, end) が一致する行は積算済み消費 (used_pulses /
    used_rounds / judgment_pulses) を引き継ぐ (init_budget_ledger が used を
    保持するのと同じ思想 — 再確定 (day_open の再発火等) で消費や判断点回数の
    帳簿をリセットしない)。一致しない (新規・時刻変更の) ライフは 0 から
    始まる。

    Raises:
        ValueError: :func:`_validate_and_normalize_lives` の検証失敗。
    """
    plan_date_str = _normalize_plan_date(plan_date)
    normalized = _validate_and_normalize_lives(lives)

    # 消費の引き継ぎは最新 meta から CAS の内側で行う (外で読んだ古い消費を
    # 完成値として書くと、読みと書きの間に積まれた消費が巻き戻る — 第七陣 P1)。
    def _apply(meta: Dict[str, Any]) -> List[Dict[str, Any]]:
        raw = meta.get(META_LIVES)
        existing = {
            (life.get("start"), life.get("end")): life
            for life in (raw if isinstance(raw, list) else [])
            if isinstance(life, dict)
        }
        for life in normalized:
            prev = existing.get((life["start"], life["end"]))
            if prev is not None:
                life["used_pulses"] = int(prev.get("used_pulses") or 0)
                life["used_rounds"] = int(prev.get("used_rounds") or 0)
                life["judgment_pulses"] = int(prev.get("judgment_pulses") or 0)
            else:
                life["used_pulses"] = 0
                life["used_rounds"] = 0
                life["judgment_pulses"] = 0
        meta[META_LIVES] = normalized
        return normalized

    mutate_plan_meta(
        manager, persona_id, plan_date_str, _apply, context="save_lives",
    )
    LOGGER.info(
        "[day_plan] lives saved: persona=%s date=%s lives=%d",
        persona_id, plan_date_str, len(normalized),
    )
    return normalized


def derive_default_life_mode(manager: Any, persona_id: str) -> str:
    """ペルソナの標準モデル (DEFAULT_MODEL) の provider からライフモードの既定を導出する。

    Anthropic/OpenAI 系は均等 (explicit/implicit cache の TTL 再送延命が効く)、
    それ以外 (Gemini/Ollama 等) は自由 (life.md §5.1)。モデル未解決・provider
    不明時は安全側 (自由 = 間隔制約なし) に倒す。
    """
    persona = (getattr(manager, "personas", None) or {}).get(persona_id)
    model = getattr(persona, "model", None)
    if not model:
        return LIFE_MODE_FREE
    try:
        from saiverse.model_configs import get_model_config
        provider = str(get_model_config(model).get("provider") or "")
    except Exception:
        LOGGER.warning(
            "[day_plan] failed to resolve provider for model=%r (persona=%s); "
            "defaulting life mode to free", model, persona_id, exc_info=True,
        )
        return LIFE_MODE_FREE
    return LIFE_MODE_EVEN if provider in _EVEN_MODE_PROVIDERS else LIFE_MODE_FREE


def _life_window_minutes(wake: str, close: str) -> int:
    """wake〜close の長さ (分)。深夜跨ぎ (close < wake) を正常形として扱う。"""
    start_min = _life_minutes(wake)
    end_min = _life_minutes(close)
    if end_min <= start_min:
        end_min += 24 * 60
    return end_min - start_min


def _min_life_budget(mode: str, window_minutes: int) -> int:
    """ライフの最低予算 (life.md v0.5 §4.2)。

    均等モード: キャッシュを繋ぐには :data:`LIFE_EVEN_MAX_GAP_MINUTES`
    (既定 50 分) に 1 回のパルスが物理的に必要 → ``ceil(窓の長さ ÷ 50分)``。
    自由モード: キャッシュ制約は無いが、コマが 1 つも打てない予算は無意味
    なので最低 1。
    """
    if mode == LIFE_MODE_EVEN:
        return max(1, math.ceil(window_minutes / LIFE_EVEN_MAX_GAP_MINUTES))
    return 1


def life_mode_and_min_budget(
    manager: Any,
    persona_id: str,
    wake: Optional[str],
    close: Optional[str],
    mode_override: Optional[str] = None,
) -> Dict[str, Any]:
    """ライフ設定 UI (life.md v0.5 §9.2-1) 向け: 実効モードと最低予算をまとめて
    計算する副作用なしの読み取り専用ヘルパ。

    :func:`confirm_life_for_today` と同じ「モード決定」「最低予算」ロジックを
    共有するが、DB へは何も書かない (プレビュー・バリデーション専用)。

    Args:
        wake/close: "HH:MM"。どちらか欠けていれば窓長・最低予算は計算できない
            ( ``window_minutes`` / ``min_budget_pulses`` は None)。
        mode_override: ユーザーによる明示上書き ("even"/"free")。
            :data:`LIFE_MODES` に無い値は無視して自動判定にフォールバックする
            (life.md §5.1: 上書きは設定 UI からの脱出口のみ)。

    Returns:
        ``{"derived_mode", "effective_mode", "window_minutes", "min_budget_pulses"}``
    """
    derived = derive_default_life_mode(manager, persona_id)
    effective = mode_override if mode_override in LIFE_MODES else derived
    window_minutes: Optional[int] = None
    min_budget: Optional[int] = None
    if wake and close and is_valid_hhmm(wake) and is_valid_hhmm(close):
        window_minutes = _life_window_minutes(wake, close)
        min_budget = _min_life_budget(effective, window_minutes)
    return {
        "derived_mode": derived,
        "effective_mode": effective,
        "window_minutes": window_minutes,
        "min_budget_pulses": min_budget,
    }


def confirm_life_for_today(
    manager: Any,
    persona_id: str,
    plan_date: Any,
    wake: Optional[str],
    close: Optional[str],
    requested_budget_pulses: Optional[int] = None,
    mode_override: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """起床判断 (day_open) 発火時、ユーザー設定 (PersonaSchedule の起床・就寝 +
    予算) から今日のライフを確定して meta_json.lives に焼く
    (life.md v0.5 §3/§4/§8.1)。呼び出し元は
    :func:`saiverse.autonomy_wiring.fire_judgment_point`。

    - **区間**: wake〜close をそのまま使う (深夜跨ぎも正常形)
    - **モード**: ``mode_override`` (ユーザー設定、"even"/"free") が
      :data:`LIFE_MODES` に入っていればそれを、無ければ
      :func:`derive_default_life_mode` (provider 導出) を使う。上書きは
      ライフ設定 UI からの明示的な脱出口のみ (life.md §5.1) —
      ペルソナ自身は選ばない
    - **予算**: ``requested_budget_pulses`` (ユーザー設定、PersonaSchedule の
      PLAYBOOK_PARAMS.daily_budget_pulses 由来) が最低値
      (:func:`_min_life_budget`) 以上ならそれを、未設定/最低値未満なら
      最低値へ切り上げる (INFO ログ)

    **冪等**: 当日すでにライフが焼かれていれば (:func:`get_lives` が非空)
    何もせず既存の 1 件目をそのまま返す — 再起動での watchdog 再発火等で
    二重に焼き直して used_pulses / judgment_pulses の帳簿をリセットしない。

    就寝スケジュール未設定 (``close`` が None) は「ライフ無し日」(従来動作) —
    起床時刻だけでは活動区間が定義できない。``wake`` が無い場合も同様
    (v2 の一日リズム自体が未設定)。

    Returns:
        確定した (または既存の) ライフ dict。ライフ無し日は None。
    """
    plan_date_str = _normalize_plan_date(plan_date)
    existing = get_lives(manager, persona_id, plan_date_str)
    if existing:
        LOGGER.debug(
            "[day_plan] life already confirmed for today; skipping re-confirmation "
            "(persona=%s date=%s)", persona_id, plan_date_str,
        )
        return existing[0]

    if not wake or not close:
        LOGGER.info(
            "[day_plan] no wake/close schedule; no life declared today "
            "(persona=%s date=%s wake=%r close=%r)",
            persona_id, plan_date_str, wake, close,
        )
        return None

    if mode_override in LIFE_MODES:
        mode = mode_override
        LOGGER.info(
            "[day_plan] life mode overridden by user setting: persona=%s mode=%s",
            persona_id, mode,
        )
    else:
        mode = derive_default_life_mode(manager, persona_id)
    window_minutes = _life_window_minutes(wake, close)
    min_budget = _min_life_budget(mode, window_minutes)

    budget = requested_budget_pulses
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
        budget = min_budget
        LOGGER.info(
            "[day_plan] no valid budget configured; using minimum %dパルス "
            "(persona=%s mode=%s window=%d分)",
            min_budget, persona_id, mode, window_minutes,
        )
    elif budget < min_budget:
        LOGGER.info(
            "[day_plan] configured budget %dパルス below minimum %d; "
            "clamped up (persona=%s mode=%s window=%d分)",
            budget, min_budget, persona_id, mode, window_minutes,
        )
        budget = min_budget

    saved = save_lives(manager, persona_id, plan_date_str, [
        {"start": wake, "end": close, "budget_pulses": budget, "mode": mode},
    ])
    life = saved[0]
    LOGGER.info(
        "[day_plan] life confirmed: persona=%s date=%s %s-%s budget=%dパルス mode=%s",
        persona_id, plan_date_str, wake, close, budget, mode,
    )
    return life


def _ledger_plan_date(
    manager: Any, persona_id: str, plan_date: Any, *, what: str
) -> Optional[str]:
    """ライフ台帳へ記帳する対象の営業日。省略時は自己解決する。

    解決器は予約・watchdog・表示と同じ :func:`resolve_business_day` (現在時刻を
    含む確定ライフ優先)。**解決できないときは None** — 呼び出し元は記帳しない
    こと。どの日か分からないまま積むと、別のライフの帳簿に数字が乗る (欠落は
    後から追えるが、他人の帳簿に乗った数字は追えない)。
    """
    if plan_date is not None:
        return _normalize_plan_date(plan_date)
    basis = resolve_business_day(manager, persona_id)
    if basis is None:
        LOGGER.warning(
            "[day_plan] cannot resolve the business day (lives unreadable); "
            "not recording %s (persona=%s)", what, persona_id,
        )
        return None
    return basis.plan_date


def consume_life_pulse(
    manager: Any,
    persona_id: str,
    plan_date: Any = None,
    *,
    at_time: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """自発活動 1 回をその時刻が属するライフの予算へ積算する
    (life.md §5.3/§8.2、2026-08-08 追補で単位を改訂)。

    予算が数えるのは **ペルソナが自分から動いた 1 回** — 利用者向けの意味は
    「その日の自発活動の回数 (used / budget)」。

    **現在この関数を呼ぶ実体は :func:`_run_slot_life_session` 1 つだけ**
    (暮らしコマ = 出かける / 自室で過ごす、1 コマ 1 消費)。器の統合
    (autonomous_pulse_vehicle.md §A) で暮らしの一手は WORKER アスペクト =
    軽量モデルになったが、計上単位はコマのまま維持している — 利用者が数えて
    いるのは呼ばれたモデルの階層ではなく活動の回数だから。作業セッション系
    コマは今のところラウンド台帳 (:func:`consume_life_rounds`) にしか計上
    されず、``used_pulses`` へは入らない (life.md §5.3「数える予定 (未実装)」)。

    数えないもの: コマの発火そのもの (開始時刻が来ただけで AI を呼ばない
    presence 記録・移動・keep-alive) と、判断点 (起床・会話終了・セッション
    終了・イベント・就寝) の発火 — 判断点の回数は別枠
    (:func:`record_judgment_pulse`) で観測する。

    lives が無い日 / パルス時刻がどのライフにも属さない場合は no-op
    (None、後方互換)。

    Args:
        plan_date: 省略時は現在時刻が属する営業日を自己解決する
            (:func:`resolve_business_day`)。解決できない (ライフを読めない) ときは
            記帳せず no-op + WARN — 別のライフの帳簿へ積むより欠落の方が軽い。
        at_time: パルス時刻 "HH:MM"。省略時は ``clock.now()``。
    """
    plan_date_str = _ledger_plan_date(manager, persona_id, plan_date, what="pulse")
    if plan_date_str is None:
        return None
    hhmm = at_time or clock.now().strftime("%H:%M")
    return _increment_life_field(
        manager, persona_id, plan_date_str, hhmm, "used_pulses", 1, what="pulse",
    )


def _increment_life_field(
    manager: Any,
    persona_id: str,
    plan_date_str: str,
    hhmm: str,
    field: str,
    inc: int,
    *,
    what: str,
) -> Optional[Dict[str, Any]]:
    """``hhmm`` が属するライフの ``field`` へ ``inc`` を積算する共通実装。

    ライフの解決と増分計算を :func:`mutate_plan_meta` の CAS 試行の**内側**で行う
    — 外で読んだ lives に増分を足した完成値を書くと、並走した積算が失われる
    (第七陣 P1: record_judgment_pulse 2 本並走で judgment_pulses が 2 でなく 1 に
    なる再現)。lives が無い日 / どのライフにも属さない時刻は no-op (None、行も
    作らない)。
    """

    def _add(meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        raw = meta.get(META_LIVES)
        lives = (
            [life for life in raw if isinstance(life, dict)]
            if isinstance(raw, list) else []
        )
        if not lives:
            return None
        idx = get_life_for_time(lives, hhmm)
        if idx is None:
            LOGGER.info(
                "[day_plan] %s at %s does not belong to any declared life "
                "(persona=%s date=%s); not counted",
                what, hhmm, persona_id, plan_date_str,
            )
            return None
        lives[idx][field] = int(lives[idx].get(field) or 0) + inc
        meta[META_LIVES] = lives
        return lives[idx]

    return mutate_plan_meta(
        manager, persona_id, plan_date_str, _add, context=f"increment:{field}",
    )


def _life_mark_mutator(
    index: int, field: str
) -> Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """lives[index] に境界マーカー (``started`` / ``ended``) を立てる mutate 閉包。

    :func:`mutate_plan_meta` の CAS 試行の内側で評価される。lives が無い日 /
    index 外 / 既マークは None (no-op — 書かない)。
    """
    def _mark(meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        raw = meta.get(META_LIVES)
        lives = (
            [life for life in raw if isinstance(life, dict)]
            if isinstance(raw, list) else []
        )
        if index >= len(lives):
            return None
        if lives[index].get(field):
            return None  # 既にマーク済み — 書かない
        lives[index][field] = True
        meta[META_LIVES] = lives
        return lives[index]

    return _mark


def mark_life_ended(
    manager: Any,
    persona_id: str,
    plan_date: Any,
    index: int = 0,
) -> Optional[Dict[str, Any]]:
    """ライフ終了の節目処理が済んだことを永続マークする (``lives[index].ended``)。

    day_close の節目処理 (:func:`_handle_life_end` — keep-alive cancel + TTL
    同期 + 「（活動終了）」通知) は非冪等で、呼ぶたびに副作用が再適用される。
    判断 runtime の失敗 → schedule 側 backoff 再試行で
    :func:`saiverse.autonomy_wiring.fire_judgment_point` が再突入すると境界
    副作用が重複するため (2026-07-20 Codex W3 第二陣 P1)、day_open 側の
    「当日はじめての確定のときだけ節目処理」と対称に、**ライフ終了の節目は
    (persona, 営業日) につき一度**を本マーカーで保証する。呼び出し元
    (:func:`saiverse.autonomy_wiring._apply_life_end_at_day_close`) は
    「確認 → 適用 → マーク」の順で使う (マーク先行だと適用されないまま
    封印される)。

    書き込みは :func:`mutate_plan_meta` の CAS 試行の内側で行う (第七陣 P1 の
    契約 — 外で読んだ meta から完成値を作らない)。lives が無い日 / index 外 /
    既にマーク済みの場合は何も書かず None (no-op)。

    Returns:
        マークを書き込んだ場合はそのライフ dict、no-op は None。

    Raises:
        RuntimeError: CAS 再試行が枯渇した場合 (:func:`mutate_plan_meta` 準拠)。
    """
    plan_date_str = _normalize_plan_date(plan_date)
    return mutate_plan_meta(
        manager, persona_id, plan_date_str,
        _life_mark_mutator(index, "ended"),
        context="mark_life_ended",
    )


def mark_life_started(
    manager: Any,
    persona_id: str,
    plan_date: Any,
    index: int = 0,
) -> Optional[Dict[str, Any]]:
    """ライフ開始の節目処理が済んだことを永続マークする (``lives[index].started``)。

    :func:`mark_life_ended` の鏡像 (Codex W3 第八陣 — day_close に入れた
    「境界の冪等ガード + 失敗伝播」が day_open に横展開されていなかった)。
    day_open の節目処理 (:func:`_handle_life_start` — TTL override + 「（活動
    開始）」通知) は非冪等で、従来の「当日はじめての確定のときだけ」ガードは
    **確定は済んだが節目が失敗した**場合に節目を永久スキップしてしまう。
    呼び出し元 (:func:`saiverse.autonomy_wiring._confirm_life_at_day_open`) は
    「確認 → 適用 → マーク」の順で使う。

    書き込みは :func:`mutate_plan_meta` の CAS 試行の内側 (第七陣契約)。
    lives が無い日 / index 外 / 既マークは何も書かず None。
    """
    plan_date_str = _normalize_plan_date(plan_date)
    return mutate_plan_meta(
        manager, persona_id, plan_date_str,
        _life_mark_mutator(index, "started"),
        context="mark_life_started",
    )


#: ライフ境界の実行台帳 KIND (W5)。冪等キーは "{persona}:{plan_date}" —
#: (persona, 営業日, 境界種) につき一つの実行。
LIFE_BOUNDARY_KIND_START = "life.boundary_start"
LIFE_BOUNDARY_KIND_END = "life.boundary_end"


def _life_boundary_outbox_items(
    manager: Any, persona_id: str, text: str
) -> list:
    """境界通知の outbox item 列を組み立てる (W5)。

    配送先 (ペルソナの adapter) が無ければ空 — 旧 :func:`_notify_life_boundary`
    の「配送先が無い場合は no-op = True」と同義 (通知なしで決着する)。
    本文・時刻は enqueue 時点で凍結する (台帳 不変条件 6) — 配送が遅延しても
    節目の時刻がずれない。時刻は仮想クロック (clock.now) を尊重しつつ
    tz-aware UTC ISO にする (naive だと adapter が UTC と解釈して ±9h ずれる)。
    """
    persona = (getattr(manager, "personas", {}) or {}).get(persona_id)
    adapter = getattr(persona, "sai_memory", None) if persona is not None else None
    if adapter is None or not hasattr(adapter, "append_persona_message"):
        return []
    from datetime import timezone
    timestamp = clock.now().astimezone(timezone.utc).isoformat()
    message = {
        "role": "user",
        "content": f"<system>[システム通知] {text}</system>",
        "timestamp": timestamp,
        "metadata": {"tags": ["internal", "event_message", "day_plan"]},
    }
    return [{
        "target": "saimemory.append",
        "payload": {"message": message, "building_id": None, "thread_suffix": None},
        "persona_id": persona_id,
    }]


def apply_life_boundary(
    manager: Any,
    persona_id: str,
    plan_date: Any,
    life: Dict[str, Any],
    *,
    boundary: str,
    index: int = 0,
) -> bool:
    """ライフ境界 (開始 / 終了) の節目を実行台帳の下で決着させる (W5)。

    W3 第六陣 P2 の恒久解 — 旧構造の二つの窓を閉じる:

    (a) 「通知の追記成功 → 成功報告前の crash」で再試行が通知を再適用する
        at-least-once 窓 → **マーカーと通知 outbox を world DB の単一 commit**
        (:func:`mutate_plan_meta` の ``in_session_extra`` で
        :meth:`~saiverse.execution_ledger.ExecutionLedger.mark_applied` を同梱)
        にし、配送は outbox_id 冪等 (append_ledger_message) で一度きり。
    (b) 「マーカー書き込み失敗の無条件 True + 即時リトライ 1 回」の暫定 →
        マーカーが書けなければ台帳 failed + False で正直に失敗し、schedule 側
        backoff が再試行する (claim は failed キーを退避して新 prepared を取る)。

    順序: claim → try_mark_running → 冪等段 (keep-alive cancel / TTL 同期 —
    すべて再試行安全) → 「マーカー + applied + 通知 outbox」単一 commit →
    即時配送試行 (失敗しても durable、関所 / 回復 tick が引き継ぐ)。

    ledger の無い環境 (旧テストスタブ等) は従来経路 (:func:`_handle_life_start`
    / :func:`_handle_life_end` の直接通知 + マーカー) に縮退する (W2 の慣行)。

    Returns:
        境界が決着したか。True = 適用済み (今回適用 / 既に決着済み / 通知先
        なし)。False = 今回の適用が失敗 — 呼び出し元は判断を走らせず
        ``submitted=False`` で戻す (W3 の失敗伝播)。
    """
    plan_date_str = _normalize_plan_date(plan_date)
    if boundary == "start":
        kind = LIFE_BOUNDARY_KIND_START
        marker_field = "started"
        notice = f"（活動開始）今日は {life['start']}〜{life['end']}。"
    elif boundary == "end":
        kind = LIFE_BOUNDARY_KIND_END
        marker_field = "ended"
        notice = "（活動終了）今日の活動時間はここまで。"
    else:
        raise ValueError(f"unknown life boundary: {boundary!r}")

    ledger = getattr(manager, "execution_ledger", None)
    if ledger is None:
        warn_key = f"life_boundary:{persona_id}"
        if warn_key not in _LEDGER_MISSING_WARNED:
            _LEDGER_MISSING_WARNED.add(warn_key)
            LOGGER.warning(
                "[day_plan] manager has no execution_ledger; life boundary "
                "runs in legacy direct mode (persona=%s)", persona_id,
            )
        handler = _handle_life_start if boundary == "start" else _handle_life_end
        if not handler(manager, persona_id, plan_date_str, index, life):
            return False
        marker = (
            mark_life_started if boundary == "start" else mark_life_ended
        )
        try:
            marker(manager, persona_id, plan_date_str, index=index)
        except Exception:
            LOGGER.error(
                "[day_plan] failed to persist life-%s marker (legacy mode, "
                "persona=%s date=%s)", boundary, persona_id, plan_date_str,
                exc_info=True,
            )
        return True

    execution_id, runnable, existing = ledger.claim_execution(
        kind, idempotency_key=f"{persona_id}:{plan_date_str}",
        persona_id=persona_id,
    )
    if not runnable:
        if existing in ("applied", "completed"):
            LOGGER.info(
                "[day_plan] life %s boundary already settled (execution=%s "
                "persona=%s date=%s)", boundary, execution_id, persona_id,
                plan_date_str,
            )
            return True
        LOGGER.warning(
            "[day_plan] life %s boundary claim not runnable (status=%s "
            "persona=%s date=%s) — leaving retry to the caller",
            boundary, existing, persona_id, plan_date_str,
        )
        return False
    if not ledger.try_mark_running(execution_id):
        # ほぼ同時の並走者が席を取った — 台帳へ書かず離脱 (敗者契約)
        return False

    if boundary == "start":
        steps_ok = _sync_cache_ttl_for_life_start(manager, persona_id, life)
    else:
        steps_ok = (
            _cancel_keepalive_reservation(manager, persona_id)
            and _sync_cache_ttl_for_life_end(manager, persona_id, life)
        )
    if not steps_ok:
        try:
            ledger.mark_failed(
                execution_id, f"life-{boundary} idempotent steps failed"
            )
        except Exception:
            LOGGER.error(
                "[day_plan] failed to record life-%s boundary failure "
                "(execution=%s)", boundary, execution_id, exc_info=True,
            )
        return False

    outbox_items = _life_boundary_outbox_items(manager, persona_id, notice)

    def _extra(db: Any) -> None:
        ledger.mark_applied(
            execution_id,
            result={"boundary": boundary, "notified": bool(outbox_items)},
            outbox_items=outbox_items,
            session=db,
        )

    try:
        marked = mutate_plan_meta(
            manager, persona_id, plan_date_str,
            _life_mark_mutator(index, marker_field),
            context=f"life_boundary_{boundary}",
            in_session_extra=_extra,
        )
    except Exception:
        LOGGER.warning(
            "[day_plan] life %s boundary marker tx failed (persona=%s "
            "date=%s)", boundary, persona_id, plan_date_str, exc_info=True,
        )
        try:
            ledger.mark_failed(
                execution_id, f"life-{boundary} marker tx failed"
            )
        except Exception:
            LOGGER.error(
                "[day_plan] failed to record life-%s marker tx failure "
                "(execution=%s)", boundary, execution_id, exc_info=True,
            )
        return False
    if marked is None:
        # 並走が先にマークした / lives が消えた — 通知なしでこの実行を閉じる
        # (境界そのものは決着済み)。
        try:
            ledger.mark_applied(
                execution_id,
                result={"boundary": boundary, "note": "marker already set"},
            )
            ledger.mark_completed(execution_id)
        except Exception:
            LOGGER.warning(
                "[day_plan] failed to close no-op life-%s boundary execution "
                "(execution=%s)", boundary, execution_id, exc_info=True,
            )
        return True

    LOGGER.info(
        "[day_plan] life %s boundary applied: marker + %d notice outbox in "
        "one commit (execution=%s persona=%s date=%s)",
        boundary, len(outbox_items), execution_id, persona_id, plan_date_str,
    )
    try:
        ledger.flush_pending_for_persona(persona_id)
    except Exception:
        LOGGER.warning(
            "[day_plan] life boundary notice delivery deferred "
            "(persona=%s) — outbox remains pending for the gate / recovery "
            "tick", persona_id, exc_info=True,
        )
    return True


def record_judgment_pulse(
    manager: Any,
    persona_id: str,
    plan_date: Any = None,
    *,
    at_time: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """判断点の発火 1 回をその時刻が属するライフへ「別枠」で記帳する
    (life.md v0.5 §5.3/§8.2)。

    予算 (budget_pulses / used_pulses) には一切触れない。判断点 (起床・
    会話終了・セッション終了・イベント・就寝) はペルソナが編成でコントロール
    できない発火 (会話がいつ終わるかはペルソナ次第ではない) であり、同じ
    財布に入れると「N コマ編成したら予算 N+M 必要」という構造矛盾が生じる
    (実機初日の教訓)。判断点の回数は新聞・ライフビューに別枠で表示するための
    観測値であり、:func:`saiverse.autonomy_wiring.fire_judgment_point` が
    判断点発火の都度呼ぶ。

    lives が無い日 / 発火時刻がどのライフにも属さない場合は no-op
    (None、:func:`consume_life_pulse` と同じ判定)。

    Args:
        plan_date: 省略時は現在時刻が属する営業日を自己解決する
            (:func:`resolve_business_day`)。解決できない (ライフを読めない) ときは
            記帳せず no-op + WARN — 別のライフの帳簿へ積むより欠落の方が軽い。
        at_time: 発火時刻 "HH:MM"。省略時は ``clock.now()``。
    """
    plan_date_str = _ledger_plan_date(
        manager, persona_id, plan_date, what="judgment pulse",
    )
    if plan_date_str is None:
        return None
    hhmm = at_time or clock.now().strftime("%H:%M")
    return _increment_life_field(
        manager, persona_id, plan_date_str, hhmm, "judgment_pulses", 1,
        what="judgment pulse",
    )


def consume_life_rounds(
    manager: Any,
    persona_id: str,
    plan_date: Any,
    rounds: int,
    *,
    at_time: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """作業ラウンドの実測値をその時刻が属するライフへ積算する (life.md Phase2 §7)。

    κ 減衰 (:data:`LIFE_ROUND_BUDGET_FACTOR`) は保存時でなく消費計算時
    (:func:`get_budget_state` / ゲート判定) に適用する — ここには生のラウンド数を
    積算する。lives が無い日は no-op (None)。
    """
    inc = _read_nonneg_int(rounds)
    if not inc:
        return None
    plan_date_str = _normalize_plan_date(plan_date)
    hhmm = at_time or clock.now().strftime("%H:%M")
    return _increment_life_field(
        manager, persona_id, plan_date_str, hhmm, "used_rounds", inc,
        what="work rounds",
    )


def life_consumed(life: Dict[str, Any]) -> float:
    """ライフ 1 件の消費量 (パルス換算): used_pulses + used_rounds × κ。

    公開関数 (Phase 4 の見せ方 API がライフごとの消費/残高を表示するために使う。
    life.md §9.2)。
    """
    used_pulses = int(life.get("used_pulses") or 0)
    used_rounds = int(life.get("used_rounds") or 0)
    return used_pulses + used_rounds * LIFE_ROUND_BUDGET_FACTOR


def _apply_life_budget_gate(
    manager: Any,
    persona_id: str,
    plan_date_str: str,
    index: int,
    slot: Dict[str, Any],
    lives: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """ライフ単位の予算ゲート (life.md Phase2 §7)。

    旧ゲート (:func:`_apply_budget_gate` の日次ラウンド台帳) と異なり、ラウンド数
    の切り詰めはしない二値判定 — そのコマが属するライフの残
    (budget_pulses − 消費) が 0 以下なら skip、それ以外はコマをそのまま通す
    (パルスとラウンドは単位が異なり、ラウンド予算をパルス残高で比例配分しても
    意味を持たないため)。

    Returns:
        通す場合は slot をそのまま返す。skip する場合は None
        (呼び出し側の :func:`_fire_slot` が status=skipped を永続化する)。
    """
    idx = get_life_for_time(lives, slot["start"])
    if idx is None:
        # 谷にコマは置けない検証 (save_lives/save_day_plan) を通っていれば
        # 起こらないはずだが、保存後にライフだけ組み替わった場合の防御。
        LOGGER.warning(
            "[day_plan] slot start=%s doesn't belong to any declared life "
            "(persona=%s date=%s index=%d); life budget gate skipped "
            "(allowing through)",
            slot["start"], persona_id, plan_date_str, index,
        )
        return slot
    life = lives[idx]
    remaining = int(life.get("budget_pulses") or 0) - life_consumed(life)
    if remaining <= 0:
        _update_slot(
            manager, persona_id, plan_date_str, index,
            expected_id=slot.get("id"),
            status=STATUS_SKIPPED, skip_reason=SKIP_REASON_BUDGET_EXHAUSTED,
        )
        LOGGER.warning(
            "[day_plan] slot skipped: life budget exhausted (persona=%s date=%s "
            "index=%d life=%s-%s used_pulses=%s used_rounds=%s budget_pulses=%s)",
            persona_id, plan_date_str, index, life.get("start"), life.get("end"),
            life.get("used_pulses"), life.get("used_rounds"), life.get("budget_pulses"),
        )
        return None
    return slot


def _notify_life_boundary(manager: Any, persona_id: str, text: str) -> bool:
    """ライフ境界 (活動開始・終了) のシステム通知を tail (末尾イベント) として
    SAIMemory へ**直接** append する — **W5 以降は縮退経路のみ**。

    本番経路では通知は :func:`apply_life_boundary` が outbox item として
    マーカーと同一 commit で凍結し、配送器 (append_ledger_message) が冪等に
    届ける。本関数が呼ばれるのは execution_ledger の無い環境の縮退時
    (:func:`_handle_life_start` / :func:`_handle_life_end`) だけ。
    様式は Track 切替通知と同じ (``<system>`` ラップの user メッセージ、
    event_message タグ、キャッシュ無破壊 — life.md §9.3)。

    Returns:
        追記に成功したか (Codex W3 第四陣 P2: day_close 側は成否で ended
        マーカーの可否を決めるため、例外は握ったまま False を返す)。配送先が
        無い (ペルソナ未ロード等) は従来どおりの no-op = True — 再試行しても
        届く見込みが無く、失敗扱いにすると節目が永久に closed されない。
    """
    persona = (getattr(manager, "personas", {}) or {}).get(persona_id)
    adapter = getattr(persona, "sai_memory", None) if persona is not None else None
    if adapter is None or not hasattr(adapter, "append_persona_message"):
        return True
    message = {
        "role": "user",
        "content": f"<system>[システム通知] {text}</system>",
        "metadata": {"tags": ["internal", "event_message", "day_plan"]},
    }
    try:
        adapter.append_persona_message(message)
        return True
    except Exception:
        LOGGER.warning(
            "[day_plan] failed to record life boundary notice (persona=%s)",
            persona_id, exc_info=True,
        )
        return False


#: 均等モード中に運転する explicit cache TTL (life.md §5.1)。均等モードの
#: 最大コマ間隔 (:data:`LIFE_EVEN_MAX_GAP_MINUTES` 既定 50 分) は TTL=1h を
#: 前提に設計されている — global 既定の "5m" のままだと keep-alive が
#: 3〜4 分おきに artificial touch を打ち続けることになり、意図した「実パルス
#: 自身がキャッシュを繋ぐ」設計にならない。
_EVEN_MODE_CACHE_TTL = "1h"


def _life_ttl_clear_key(persona_id: str) -> str:
    """均等モードの TTL override 遅延解除の EventScheduler 予約 key。"""
    return f"life_ttl_clear:{persona_id}"


def _resolve_ttl_clear_delay_seconds(manager: Any, persona_id: str) -> int:
    """TTL override 遅延解除の待ち秒数 (= anchor validity 秒) を解決する。

    ``SessionLifecycle.get_anchor_validity_seconds`` は anchor の生存を
    「**現在の** TTL 設定」で評価する — override (1h) がまだ生きている
    ライフ終端のこの瞬間に読めば、実キャッシュの寿命評価と同じ 3600 秒が
    返る。runtime / model が引けない異常系は 3600 秒 (Anthropic explicit 1h
    相当) にフォールバックする (長すぎる側に倒す — 早すぎる clear が
    「実キャッシュは生きているのに anchor は失効扱い」の欠陥の源)。
    """
    default = 3600
    persona = (getattr(manager, "personas", {}) or {}).get(persona_id)
    model_key = getattr(persona, "model", None) if persona is not None else None
    runtime = getattr(manager, "sea_runtime", None) or getattr(manager, "runtime", None)
    lifecycle = getattr(runtime, "session_lifecycle", None)
    if not model_key or lifecycle is None:
        return default
    try:
        seconds = int(lifecycle.get_anchor_validity_seconds(model_key, persona_id))
        return seconds if seconds > 0 else default
    except Exception:
        LOGGER.warning(
            "[day_plan] failed to resolve TTL clear delay (persona=%s); using %ds",
            persona_id, default, exc_info=True,
        )
        return default


def _clear_life_ttl_override(manager: Any, persona_id: str) -> None:
    """遅延解除の発火体: ライフが設定した TTL override を厳密一致チェック付きで外す。

    現在の override が「ライフが設定した値」({"enabled": True, "ttl": "1h"}) と
    厳密一致するときだけ clear する — 予約〜発火の間にユーザーが人設定タブで
    別の値へ明示的に変更していた場合はそれを尊重して触らない。
    """
    get_override = getattr(manager, "get_persona_cache_override", None)
    clear_override = getattr(manager, "clear_persona_cache_override", None)
    if get_override is None or clear_override is None:
        return
    try:
        current = get_override(persona_id)
        if current == {"enabled": True, "ttl": _EVEN_MODE_CACHE_TTL}:
            clear_override(persona_id)
            LOGGER.info(
                "[day_plan] life TTL override cleared (delayed, persona=%s)",
                persona_id,
            )
        else:
            LOGGER.debug(
                "[day_plan] life TTL clear fired but override does not match "
                "life-set value; leaving as-is (persona=%s current=%r)",
                persona_id, current,
            )
    except Exception:
        LOGGER.warning(
            "[day_plan] failed to clear life TTL override (persona=%s)",
            persona_id, exc_info=True,
        )


def _sync_cache_ttl_for_life_start(manager: Any, persona_id: str, life: Dict[str, Any]) -> bool:
    """life.md §5.1: 均等モードのライフ中は persona の explicit cache TTL を
    1h override する。

    前のライフの遅延解除予約 (:func:`_sync_cache_ttl_for_life_end`) が残って
    いれば先に cancel する — TTL 経過前に次のライフが始まったケースで、
    ライフの最中に解除が発火して override が外れる事故を防ぐ。

    persona に既存の cache override (人設定タブでユーザーが明示設定したもの、
    または前のライフが設定してまだ解除されていない 1h) があればそのまま触ら
    ない — ライフの宣言は既定値の補完であって、明示指定を上書きしない (前の
    ライフの 1h が残っている場合は望む値が既に入っているので set 不要)。
    自由モードのライフでは何もしない (間隔制約が無いので TTL を強制する理由
    がない)。
    """
    if life.get("mode") != LIFE_MODE_EVEN:
        return True
    scheduler = getattr(manager, "event_scheduler", None)
    if scheduler is not None:
        try:
            if scheduler.cancel(_life_ttl_clear_key(persona_id)):
                LOGGER.info(
                    "[day_plan] pending life TTL clear cancelled by next life "
                    "start (persona=%s)", persona_id,
                )
        except Exception:
            # cancel 失敗を成功扱いにしてはならない (Codex W3 第九陣): 残った旧
            # 解除予約がライフ中に発火して override を外すのに、直後の「既存
            # override あり」分岐が True を返すと started マーカーで封印され、
            # 再試行が二度と TTL 同期をやり直せない — 部分失敗の成功封印そのもの。
            LOGGER.warning(
                "[day_plan] failed to cancel pending life TTL clear (persona=%s)",
                persona_id, exc_info=True,
            )
            return False
    get_override = getattr(manager, "get_persona_cache_override", None)
    set_override = getattr(manager, "set_persona_cache_override", None)
    if get_override is None or set_override is None:
        return True
    try:
        if get_override(persona_id) is not None:
            LOGGER.debug(
                "[day_plan] life start (mode=even): existing cache override "
                "present; not forcing TTL=%s (persona=%s)",
                _EVEN_MODE_CACHE_TTL, persona_id,
            )
            return True
        set_override(persona_id, enabled=True, ttl=_EVEN_MODE_CACHE_TTL)
        LOGGER.info(
            "[day_plan] life start (mode=even): cache TTL set to %s (persona=%s)",
            _EVEN_MODE_CACHE_TTL, persona_id,
        )
        return True
    except Exception:
        LOGGER.warning(
            "[day_plan] failed to apply even-mode cache TTL (persona=%s)",
            persona_id, exc_info=True,
        )
        return False


def _sync_cache_ttl_for_life_end(manager: Any, persona_id: str, life: Dict[str, Any]) -> bool:
    """ライフ終端で TTL override の**遅延**解除を予約する (life.md §6.2 v0.4)。

    即時に clear してはいけない: anchor の生存判定
    (``SessionLifecycle.get_anchor_validity_seconds``) は「**現在の** TTL 設定」
    で評価されるため、終端で即時に global 既定 (5m) へ戻すと、実キャッシュは
    1h 生きているのに anchor は 5m で失効扱いになり、惜しい谷 (終了直後〜TTL
    内) の再訪が Case 3 に落ちて生きたキャッシュを捨てる。終端 + anchor
    validity 秒 (= 実キャッシュが確実に切れた後) に発火する予約を入れ、発火体
    (:func:`_clear_life_ttl_override`) が厳密一致チェック付きで clear する。

    次のライフが TTL 経過前に始まる場合は :func:`_sync_cache_ttl_for_life_start`
    が同 key の予約を cancel する。

    Returns:
        予約に成功したか (Codex W3 第四陣 P2)。予約が不要なケース (均等モード
        以外 / scheduler の無い構成) は True。
    """
    if life.get("mode") != LIFE_MODE_EVEN:
        return True
    scheduler = getattr(manager, "event_scheduler", None)
    if scheduler is None:
        return True
    try:
        delay = _resolve_ttl_clear_delay_seconds(manager, persona_id)
        fire_at = clock.now() + timedelta(seconds=delay)
        scheduler.schedule(
            fire_at=fire_at,
            callback=lambda: _clear_life_ttl_override(manager, persona_id),
            key=_life_ttl_clear_key(persona_id),
        )
        LOGGER.info(
            "[day_plan] life end (mode=even): TTL override clear scheduled in "
            "%ds (persona=%s)", delay, persona_id,
        )
        return True
    except Exception:
        LOGGER.warning(
            "[day_plan] failed to schedule life TTL clear (persona=%s)",
            persona_id, exc_info=True,
        )
        return False


def _cancel_keepalive_reservation(manager: Any, persona_id: str) -> bool:
    """ライフ終端で keep-alive 予約を全 model 分 cancel する (谷では温めない)。

    ``sea.session_lifecycle.SessionLifecycle.schedule_cache_ttl_pulse`` /
    ``_schedule_session_watchdog`` の予約 key は (persona, model) 単位の
    ``ttl:{persona_id}:{model_key}`` (beat_execution_context.md §3.1)。終端側は
    どの model の Session が見張り中か列挙できないため prefix で一括 cancel する。
    ライフ中に未発火の予約が残っていても、この cancel で確実に止まる —
    :func:`is_keepalive_allowed` による発火時ゲートは二重の安全網。

    Returns:
        cancel に成功したか (Codex W3 第四陣 P2)。scheduler の無い構成は True。
    """
    scheduler = getattr(manager, "event_scheduler", None)
    if scheduler is None:
        return True
    try:
        cancelled = scheduler.cancel_prefix(f"ttl:{persona_id}:")
        if cancelled:
            LOGGER.info(
                "[day_plan] keep-alive reservations cancelled at life end (persona=%s, keys=%s)",
                persona_id, ", ".join(sorted(cancelled)),
            )
        return True
    except Exception:
        LOGGER.warning(
            "[day_plan] failed to cancel keep-alive reservation at life end (persona=%s)",
            persona_id, exc_info=True,
        )
        return False


def _handle_life_start(
    manager: Any, persona_id: str, plan_date_str: str, index: int, life: Dict[str, Any]
) -> bool:
    """ライフ開始の節目処理 — **W5 以降は縮退経路のみ**。

    本番経路は :func:`apply_life_boundary` (実行台帳の claim + マーカーと通知
    outbox の単一 commit)。本関数が直接呼ばれるのは manager に
    execution_ledger が無い環境 (旧テストスタブ等) の縮退時だけで、そのとき
    通知は従来どおり直接 append (:func:`_notify_life_boundary`) になる。

    v0.4 までは専用のライフ境界イベント (EventScheduler 予約) の発火時に
    呼ばれていたが、v0.5 でその専用予約は廃止した — 「ライフ開始 = 起床判断
    (day_open)」そのもの。

    Returns:
        全段成功したか (:func:`_handle_life_end` の鏡像、Codex W3 第八陣)。
        順序契約も同じ — 冪等な TTL override を先に、非冪等な通知を最後に。
        途中失敗は通知の前に False で戻り、再試行しても通知は重複しない。
    """
    LOGGER.info(
        "[day_plan] life started: persona=%s date=%s index=%d %s-%s "
        "(budget=%dパルス mode=%s)",
        persona_id, plan_date_str, index, life["start"], life["end"],
        life["budget_pulses"], life["mode"],
    )
    # life.md §6.1 / Phase3 調査 → arasuji_levels.md §13 (2026-07-29) で意味が
    # 変わった: 「ライフ開始 = 新しい Session 開始」は**温度 (キャッシュ) の話**
    # としては今も成り立つ — 前のライフの終端で keep-alive が止まっていれば
    # anchor の TTL は失効しており、次の Pulse は冷えた状態から始まる。ただし
    # §13 以降、TTL 失効は提示範囲 (ウィンドウ) を変えない — anchor は張り
    # 直されず、前のライフからの提示コンテキストが地続きで提示される。提示が
    # 縮むのは予算超過の畳みだけ。谷が TTL より短ければキャッシュヒットで再開
    # する (惜しい谷、life.md §8.3)。
    # ここで明示的に head capture 等を行う必要は無い — ログのみ残す。
    LOGGER.info(
        "[day_plan] session boundary: next pulse continues or freshly starts "
        "a session depending on anchor TTL (persona=%s)", persona_id,
    )
    if not _sync_cache_ttl_for_life_start(manager, persona_id, life):
        return False
    # life.md §9.2-3 (改修B): 実装語 ("ライフ") を排した確定文言。
    return _notify_life_boundary(
        manager, persona_id,
        f"（活動開始）今日は {life['start']}〜{life['end']}。",
    )


def _handle_life_end(
    manager: Any, persona_id: str, plan_date_str: str, index: int, life: Dict[str, Any]
) -> bool:
    """ライフ終了の節目処理 — **W5 以降は縮退経路のみ**。

    本番経路は :func:`apply_life_boundary` (実行台帳の claim + マーカーと通知
    outbox の単一 commit)。本関数が直接呼ばれるのは manager に
    execution_ledger が無い環境 (旧テストスタブ等) の縮退時だけ。

    v0.4 までは専用のライフ境界イベント (EventScheduler 予約) の発火時に
    呼ばれていたが、v0.5 でその専用予約は廃止した — 「ライフ終了 = 就寝判断
    (day_close)」そのもの。

    Returns:
        全段成功したか (Codex W3 第四陣 P2)。呼び出し元は True のときだけ
        ended マーカー (:func:`mark_life_ended`) を立てる — 下請け各段は例外を
        内部で握るため、bool を返さないと部分失敗が「成功」として封印され、
        失敗した節目 (例: 活動終了通知) が永久に回復されない。

    順序契約: **冪等な後始末 (keep-alive cancel / TTL 解除予約) を先に、
    非冪等な通知 (SAIMemory 追記) を最後に**。途中失敗は通知の前に False で
    戻る — 再試行では冪等段だけが再実行され、通知はまだ一度も出ていないので
    重複しない。通知自体の失敗も False (追記されていないので再試行安全)。
    重複しうる窓は「通知の追記は成功したが成功報告の前に crash」だけ
    (at-least-once、従来の毎回再適用よりはるかに狭い)。
    """
    consumed = life_consumed(life)
    LOGGER.info(
        "[day_plan] life ended: persona=%s date=%s index=%d %s-%s "
        "(消費 %.1f/%d パルス, 判断点 %d 回)",
        persona_id, plan_date_str, index, life["start"], life["end"],
        consumed, life["budget_pulses"], int(life.get("judgment_pulses") or 0),
    )
    # ライフ終端の節目 (life.md §6.2 v0.4、v0.5 でも不変): 終端が能動的に
    # 行うのは keep-alive の停止 (予約 cancel) と TTL override の遅延解除予約
    # だけ。anchor は**触らない** — touch が止まれば TTL で自然失効し、
    # Metabolism 本体 (Chronicle 化・eviction) は失効後の最初の活動の既存経路
    # (runtime_context.py Case 3) が行う。anchor を即時失効させると、惜しい谷
    # (終了直後〜TTL 内の再訪、実キャッシュはまだ生きている) の最初の Pulse が
    # Case 3 で履歴を組み替え、生きたキャッシュを捨ててしまう (§8.3 裁定と
    # 矛盾。v0.3 の「即時失効」は v0.4 で誤りと訂正済み)。
    if not _cancel_keepalive_reservation(manager, persona_id):
        return False
    if not _sync_cache_ttl_for_life_end(manager, persona_id, life):
        return False
    # life.md §9.2-3 (改修B): 実装語 ("ライフ") を排した確定文言。
    return _notify_life_boundary(
        manager, persona_id,
        "（活動終了）今日の活動時間はここまで。",
    )


# ---------------------------------------------------------------------------
# EventScheduler への push
# ---------------------------------------------------------------------------


def _slot_key(persona_id: str, plan_date_str: str, slot_id: str) -> str:
    """EventScheduler の予約 key。コマの**不変 id** ベース (index ではない)。

    index ベースだった旧 key は、時間割の全置換で新旧 plan の key 文字列が衝突
    し、(1) cancel 失敗で残留した旧時刻の予約が新 plan の別コマを誤発火させる、
    (2) watchdog (:func:`find_lost_slot_reservations`) が「key の有無」だけでは
    残留 (旧) と正規 (新) を区別できず途絶を見逃す、という二重の穴になっていた
    (2026-07-20 Codex レビュー第三陣)。id ベースなら置換後の残留予約は「その id
    のコマはもう無い」で無害に空振りし、新コマの key 不在は watchdog が正しく
    検出して再 push する。

    NOTE: 出来事の origin_ref (:func:`_slot_origin_ref`) は**別物** — あちらは
    回復系の逆引き互換のため index ベースの旧形式を維持している。
    """
    return f"day_plan:{persona_id}:{plan_date_str}:{slot_id}"


def _resolve_wake(manager: Any, persona_id: str) -> Optional[str]:
    """ペルソナの起床時刻 "HH:MM" を PersonaSchedule から解決する (無ければ None)。

    深夜跨ぎリズムの暦日補正 (_slot_fire_at) に使う。呼び出し元が wake を
    明示しなくても、コマ予約の全経路が跨ぎ対応になるようにするための自己解決。

    NOTE: 当日 plan に対する予約では :func:`_resolve_wake_for_plan` を使うこと —
    確定済みライフのある日は現行スケジュールでなくライフの開始が基準
    (Codex 七巡目)。
    """
    try:
        from saiverse.autonomy_wiring import _find_day_schedules
        return _find_day_schedules(manager, persona_id).get("wake")
    except Exception:
        LOGGER.warning(
            "[day_plan] failed to resolve wake time (persona=%s)", persona_id,
            exc_info=True,
        )
        return None


#: ライフを**読めなかった**ことの印。「その日にライフが宣言されていない」
#: (空リスト) と厳密に区別する — 混同すると、DB ロック等で読み出しが一時的に
#: 失敗しただけの日に現行 PersonaSchedule 基準へ黙って落ち、起床設定を変えた
#: 日は予約の暦日補正が丸一日ずれる (深夜帯コマを「流れた」と誤確定しうる)。
#: close_outcome の :data:`_RELOAD_FAILED` と同じ三状態化 (Codex 八巡目 #2)。
_LIVES_UNREADABLE = object()


def _load_lives_or_unreadable(
    manager: Any, persona_id: str, plan_date_str: str
) -> Any:
    """基準解決のためのライフ読み出し。読めなければ :data:`_LIVES_UNREADABLE`。

    読取の失敗は例外 (DB ロック等) だけではない — 壊れた meta_json は
    :func:`load_plan_meta` の既定経路では空 dict へ縮退し、「ライフ未宣言の日」と
    区別がつかなくなる。ここは ``strict=True`` で読み、**壊れている**も
    **読めなかった**側に数える (縮退したまま現行スケジュール基準で別の営業日を
    駆動する方が害が大きい。Codex 九巡目 #2)。

    Returns:
        ライフ配列 (宣言なしは空リスト) / :data:`_LIVES_UNREADABLE` (読取失敗)。
    """
    try:
        return get_lives(manager, persona_id, plan_date_str, strict=True)
    except Exception:
        LOGGER.warning(
            "[day_plan] failed to read lives (persona=%s date=%s); callers must "
            "not fall back to the current schedule — the basis would split",
            persona_id, plan_date_str, exc_info=True,
        )
        return _LIVES_UNREADABLE


def _wake_from_lives(lives: Optional[List[Dict[str, Any]]]) -> Optional[str]:
    """確定ライフ基準の起点 (最初のライフの開始時刻)。使えなければ None。

    並び順の起点 (:func:`day_order_minutes` の origin) と同じ「最初のライフの
    開始」— 予約の暦日補正と並びで物差しを割らないため (Codex 六〜七巡目)。
    """
    if not lives:
        return None
    start = str(lives[0].get("start") or "")
    return start if is_valid_hhmm(start) else None


def _resolve_wake_for_plan(
    manager: Any, persona_id: str, plan_date_str: str
) -> Any:
    """当日 plan の予約 (暦日補正) に使う起床基準。

    **確定済みライフのある日はライフの開始時刻を最優先する** — 編成・検証
    (day_order_minutes(lives, ...)) と同じ物差し。plan を確定した後に
    PersonaSchedule の起床だけが変わると、並び (確定ライフ基準) と予約の
    暦日補正 (現行 wake 基準) が分裂し、start < 現行 wake のコマが翌暦日へ
    丸一日ずれて予約される (Codex 七巡目)。ライフの無い日は従来どおり
    PersonaSchedule から解決する。

    Returns:
        起床時刻 "HH:MM" / None (ライフも PersonaSchedule の起床も無い) /
        :data:`_LIVES_UNREADABLE` (ライフを読めなかった)。**読めなかったときに
        現行 PersonaSchedule へ落ちてはいけない** — 呼び出し元は予約も再分類も
        進めず、次の watchdog へ委ねること (Codex 八巡目 #2)。
    """
    lives = _load_lives_or_unreadable(manager, persona_id, plan_date_str)
    if lives is _LIVES_UNREADABLE:
        return _LIVES_UNREADABLE
    return _wake_from_lives(lives) or _resolve_wake(manager, persona_id)


class BusinessDay(NamedTuple):
    """:func:`resolve_business_day` の答え — 営業日と、その日の暦日補正の起点。"""

    #: 営業日 "YYYY-MM-DD"
    plan_date: str
    #: 起点となる起床時刻 "HH:MM" (解決できなければ None)
    wake: Optional[str]
    #: 由来: ``"life"`` = 現在時刻を含む確定ライフ (いま起きている) /
    #: ``"life_ended"`` = その日のライフは始まっていたが今は区間の外 (谷・就寝後) /
    #: ``"schedule"`` = ライフの記録が無く現行 PersonaSchedule から決めた日。
    #: **ゲートを外してよいのは ``"life"`` だけ** (watchdog の窓・曜日判定)。
    source: str
    #: その営業日の確定ライフ (宣言なしは空リスト)。解決器が既に読んだものを
    #: そのまま持たせる — 消費側が引き直すと、営業日を決めた読みと表示・記帳の
    #: 読みが別世代になりうるうえ、10 秒ポーリングの表示経路で無駄な問い合わせが
    #: 増える。
    lives: List[Dict[str, Any]]


def _life_span_at(
    plan_date: date, life: Dict[str, Any]
) -> Optional[Tuple[datetime, datetime]]:
    """営業日 ``plan_date`` の暦日に錨を下ろしたライフの区間 ``[開始, 終了)``。

    開始は plan_date の start、終端は跨ぎ (end <= start) なら翌暦日。"HH:MM" だけ
    で判定する :func:`get_life_for_time` は暦日を持たないため、「その時刻はどの
    営業日のライフに属するか」を問う経路では使えない (07-04 の 23:00〜06:00 と
    07-05 の 23:00〜06:00 を区別できない)。

    **区間として成立しないライフは None** (start == end / 不正な "HH:MM")。
    書き手 (:func:`_validate_and_normalize_lives`) は start == end を「長さ 0 の
    ライフ」として拒否しており、読み手がそれを ``_life_span_minutes`` の跨ぎ規約
    (end <= start は +24h) で 24 時間ライフと読み替えるのは書き手の契約に反する。
    壊れた行 (手編集・旧データ) がその日の営業日を名乗り、watchdog の窓・曜日
    ゲートまで外してしまう (Codex 十巡目 #2)。
    """
    start, end = life.get("start"), life.get("end")
    if not is_valid_hhmm(start) or not is_valid_hhmm(end) or start == end:
        return None
    start_dt = datetime.combine(plan_date, dt_time(int(start[:2]), int(start[3:])))
    return start_dt, start_dt + timedelta(minutes=_life_span_minutes(life))


def _life_start_covering(
    plan_date: date, lives: List[Dict[str, Any]], now_dt: datetime
) -> Optional[datetime]:
    """``now_dt`` を**区間に含む**ライフの開始 datetime (無ければ None)。

    複数が該当する場合は開始が最も新しいもの (直近に始まったライフ)。
    """
    latest: Optional[datetime] = None
    for life in lives:
        span = _life_span_at(plan_date, life)
        if span is None:
            continue
        start_dt, end_dt = span
        if start_dt <= now_dt < end_dt and (latest is None or start_dt > latest):
            latest = start_dt
    return latest


def _latest_life_start(
    plan_date: date, lives: List[Dict[str, Any]], now_dt: datetime
) -> Optional[datetime]:
    """**もう始まっている**ライフのうち、開始が最も新しいもの (無ければ None)。

    区間に含まれていなくてよい — 「今日はもう終わったライフ」も数える。まだ
    始まっていないライフ (今日の起床前に確定だけ済んでいる等) は数えない。
    """
    latest: Optional[datetime] = None
    for life in lives:
        span = _life_span_at(plan_date, life)
        if span is None:
            continue
        start_dt = span[0]
        if start_dt <= now_dt and (latest is None or start_dt > latest):
            latest = start_dt
    return latest


def resolve_business_day(
    manager: Any, persona_id: str, *, now: Optional[datetime] = None
) -> Optional[BusinessDay]:
    """いま駆動中の営業日と、その日の暦日補正の起点を**一度に**決める。

    営業日と起点 (wake) を別々に解決すると基準が割れる: 現行 PersonaSchedule で
    営業日を選んでから確定ライフで wake を引くと、**日中に起床設定を変えた日**は
    営業日そのものを取り違える。例 — 営業日 D の確定ライフが 23:00〜06:00、
    設定変更後の現行スケジュールが 07:00〜22:00 のとき、D+1 の 00:30 に再起動
    すると (現行スケジュールは跨ぎでないので) 営業日を D+1 と読み、D の pending
    コマは検査されないまま回復 0 件で静かに終わる (Codex 八巡目 #1)。

    そこで基準は**確定ライフ**。暦日とその前日のライフを読み、次の順で決める:

    1. ``"life"`` — 現在時刻を**区間に含む**ライフのある日 (いま起きている日)
    2. ライフが走っていないときは、**最後に始まったライフの日**と、現行
       PersonaSchedule の営業日 (:func:`~autonomy_wiring.effective_plan_date`) の
       **遅い方** — 一日は前へしか進まないから。選んだ日にライフの記録があれば
       ``"life_ended"``、無ければ ``"schedule"``

    「最後に始まったライフの日」が要るのは、起床設定を変えた日の谷で設定だけを
    見ると**過去の日**を指してしまうから (例: 確定ライフ D 07:00〜10:00、変更後の
    設定が跨ぎリズム、現在 D 12:00 → effective_plan_date は D-1)。そこにライフが
    無いので「ライフ未宣言の日」と読まれ、keep-alive が終わったライフを温め続ける
    (Codex 十二巡目 #1)。逆に「遅い方」を採らないと、朝の day_open が失敗した日
    (D+1 08:00、D のライフは終了済み、D+1 にライフ無し) に前日 D を指し、watchdog
    が「前の営業日がまだ続いている」と読んで day_open を撃ち直さなくなる — 一日が
    始まらないまま止まる。通常運用 (設定を触らない日) では両者は一致する。

    **「その日に plan がある」ことは候補の選択に使わない** — 複数日に plan が
    あるのは普通で (昨日と今日)、存在の有無は「いまどちらの日を生きているか」を
    区別しない。日ごとに記録された時間の基準は確定ライフだけであり、ライフの
    無い日には基準そのものが存在しない (現行スケジュールが唯一の手掛かり)。

    Returns:
        :class:`BusinessDay` / **None = ライフを読めなかった** (読取失敗)。
        None のとき呼び出し元は予約も再分類も進めず、次の watchdog へ委ねること
        (Codex 八巡目 #2)。
    """
    now_dt = now or clock.now()
    today = now_dt.date()
    lives_by_date: Dict[date, List[Dict[str, Any]]] = {}
    started: Optional[Tuple[datetime, date]] = None
    # 候補は暦日と前日の 2 日 — 前日が要るのは深夜跨ぎライフの尻尾
    # (effective_plan_date が返しうるのもこの 2 日)。暦日を先に見て、そこに
    # 走っているライフがあれば前日は読まない: 前日のライフの開始は必ず暦日の
    # それより前なので、暦日が勝つと決まっている (表示は 10 秒ポーリングで
    # 呼ばれるため、無駄な問い合わせを残さない。Codex 十二巡目 #2)。
    for candidate in (today, today - timedelta(days=1)):
        lives = _load_lives_or_unreadable(
            manager, persona_id, candidate.isoformat(),
        )
        if lives is _LIVES_UNREADABLE:
            return None
        lives_by_date[candidate] = lives
        if _life_start_covering(candidate, lives, now_dt) is not None:
            return _business_day_from_lives(
                manager, persona_id, candidate, lives, "life",
            )
        start_dt = _latest_life_start(candidate, lives, now_dt)
        if start_dt is not None and (started is None or start_dt > started[0]):
            started = (start_dt, candidate)

    from saiverse.autonomy_wiring import _find_day_schedules, effective_plan_date

    sched = _find_day_schedules(manager, persona_id)
    plan_date = effective_plan_date(now_dt, sched.get("wake"), sched.get("close"))
    if started is not None and started[1] > plan_date:
        plan_date = started[1]
    # 起点はその日のライフを優先する — 並び (day_order_minutes) が lives[0].start を
    # 起点にする以上、ここで現行スケジュールの起床を返すと予約の暦日補正と並びが
    # 割れる (七巡目の欠陥)。まだ始まっていないライフ (起床前に確定だけ済んだ日)
    # の起点もこれで拾う。
    lives = lives_by_date.get(plan_date) or []
    wake = _wake_from_lives(lives)
    # 由来は「その日に**始まったライフ**があるか」で決める (区間として成立しない
    # 行や、まだ始まっていない行は数えない — 名乗りが実態とずれないように)。
    source = "life_ended" if started is not None and started[1] == plan_date \
        else "schedule"
    return BusinessDay(
        plan_date.isoformat(), wake or sched.get("wake"), source, lives,
    )


def _business_day_from_lives(
    manager: Any,
    persona_id: str,
    plan_date: date,
    lives: List[Dict[str, Any]],
    source: str,
) -> BusinessDay:
    """ライフ基準で決まった営業日の :class:`BusinessDay` を組む。

    起点 (wake) は最初のライフの開始 — 並び順の起点 (:func:`day_order_minutes`)
    と同じ物差し。ライフの開始が使えないときだけ現行 PersonaSchedule へ退く。
    """
    wake = _wake_from_lives(lives)
    return BusinessDay(
        plan_date.isoformat(), wake or _resolve_wake(manager, persona_id),
        source, lives,
    )


def _slot_fire_at(
    plan_date_str: str, slot: Dict[str, Any], *, wake: Optional[str] = None
) -> datetime:
    """コマの開始時刻 (naive datetime)。過去時刻は EventScheduler が即時扱いする。

    深夜跨ぎリズム対応: ``wake`` が指定され、かつコマの start < wake のとき、
    そのコマは「同じ営業日の深夜帯 (翌暦日)」に属するため、
    ``plan_date + 1 日`` の日付で combine する。

    例: plan_date="2026-07-04", wake="07:00", slot.start="00:30" → 2026-07-05 00:30
        plan_date="2026-07-04", wake="07:00", slot.start="09:00" → 2026-07-04 09:00

    Args:
        plan_date_str: 営業日の "YYYY-MM-DD"
        slot:          コマ dict (``start`` フィールド必須)
        wake:          起床時刻 "HH:MM"。None なら従来どおり同日で combine
    """
    d = date.fromisoformat(plan_date_str)
    start = slot["start"]
    if wake and start < wake:
        # 深夜帯 (同営業日の翌暦日)
        d = d + timedelta(days=1)
    hh, mm = start.split(":")
    return datetime.combine(d, dt_time(int(hh), int(mm)))


def _push_slot(
    manager: Any, persona_id: str, plan_date_str: str, slot_id: str, fire_at: datetime
) -> None:
    manager.event_scheduler.schedule(
        fire_at=fire_at,
        callback=lambda: _fire_slot_by_id(manager, persona_id, plan_date_str, slot_id),
        key=_slot_key(persona_id, plan_date_str, slot_id),
    )


def _fire_slot_by_id(
    manager: Any, persona_id: str, plan_date_str: str, slot_id: str
) -> None:
    """EventScheduler callback: 不変 id を照準として発火する。

    id → 現在位置の解決は **ここでは行わず** :func:`_fire_slot` に id ごと渡す —
    ここで index に変換してから渡すと、変換と ``_fire_slot`` 自身の plan 再読込の
    間に時間割が組み替わったとき別コマを発火する (2026-07-20 Codex レビュー
    第四陣 P1)。解決は発火に使う配列を読んだ ``_fire_slot`` 本体が行う。

    id が現 plan に無い = そのコマは置換等で消えている。旧予約の残留発火
    (cancel 失敗の生き残り) は ``_fire_slot`` 側で無害に空振りする
    (:func:`_slot_key` docstring 参照)。
    """
    _fire_slot(manager, persona_id, plan_date_str, 0, slot_id=slot_id)


def _ensure_slot_ids(
    manager: Any, persona_id: str, plan_date_str: str, slots: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """全コマに不変 id があることを保証する (無ければ採番して一括永続化)。

    保存経路 (_validate_and_normalize_slots) は採番済みだが、それ以前に保存された
    legacy plan のコマには id が無い — 予約 key が id ベースになったため、push
    前にここで補填する。全コマ id 済みなら書き込みは発生しない (冪等)。
    """
    if all(isinstance(s.get("id"), str) and s.get("id") for s in slots):
        return slots

    def _backfill(fresh: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        for slot in fresh:
            if not (isinstance(slot.get("id"), str) and slot.get("id")):
                slot["id"] = uuid.uuid4().hex[:12]
        return fresh

    result = _mutate_slots_cas(
        manager, persona_id, plan_date_str, _backfill, context="ensure_slot_ids",
    )
    if result is None:
        # 行が消えている等 — push を止めないため手元の配列だけ補填して返す
        # (永続化されないが、予約 key の採番には足りる)。
        for slot in slots:
            if not (isinstance(slot.get("id"), str) and slot.get("id")):
                slot["id"] = uuid.uuid4().hex[:12]
        return slots
    LOGGER.info(
        "[day_plan] backfilled slot ids for legacy plan (persona=%s date=%s)",
        persona_id, plan_date_str,
    )
    return result


def schedule_day_plan(
    manager: Any, persona_id: str, plan_date: Any, *, wake: Optional[str] = None
) -> int:
    """pending コマを EventScheduler に push し、push した数を返す。

    key は ``day_plan:{persona_id}:{plan_date}:{slot_id}`` (コマの不変 id ベース、
    :func:`_slot_key`)。同 key の再 push は EventScheduler の既存挙動 (古い予約
    cancel + 上書き) に従うため冪等。過去時刻のコマは即時扱い
    (EventScheduler.schedule の仕様)。保存済みの時間割が無ければ WARN + 0 を返す
    (watchdog 経路で安全)。id の無い legacy plan は push 前に採番して永続化する。

    Args:
        wake: 起床時刻 "HH:MM"。深夜跨ぎリズムのとき、start < wake のコマは
            ``plan_date + 1 日`` の時刻で push される。None (省略) は従来動作。
    """
    plan_date_str = _normalize_plan_date(plan_date)
    if wake is None:
        # 呼び出し元 (起床判断 finalize 等) に配線を強要しない — 深夜跨ぎの
        # 暦日補正はコマ予約の全経路で常に効くべき (検収追加 2026-07-12)。
        # 基準は当日確定ライフ優先 (Codex 七巡目)。
        resolved = _resolve_wake_for_plan(manager, persona_id, plan_date_str)
        if resolved is _LIVES_UNREADABLE:
            # 起点が分からないまま push すると、深夜帯コマの暦日補正が丸一日
            # ずれる。押さなければ実行もされない — 予約途絶として watchdog
            # (find_lost_slot_reservations) が拾い直す (Codex 八巡目 #2)。
            LOGGER.warning(
                "[day_plan] schedule_day_plan: lives unreadable; scheduling "
                "nothing and leaving it to the next watchdog (persona=%s date=%s)",
                persona_id, plan_date_str,
            )
            return 0
        wake = resolved
    slots = load_day_plan(manager, persona_id, plan_date_str)
    if slots is None:
        LOGGER.warning(
            "[day_plan] schedule_day_plan: no plan saved (persona=%s date=%s)",
            persona_id, plan_date_str,
        )
        return 0

    slots = _ensure_slot_ids(manager, persona_id, plan_date_str, slots)
    pushed = 0
    for slot in slots:
        if slot.get("status") != STATUS_PENDING:
            continue
        _push_slot(
            manager, persona_id, plan_date_str, slot["id"],
            _slot_fire_at(plan_date_str, slot, wake=wake),
        )
        pushed += 1
    LOGGER.info(
        "[day_plan] scheduled: persona=%s date=%s pushed=%d/%d",
        persona_id, plan_date_str, pushed, len(slots),
    )
    return pushed


def reschedule_pending_slots(
    manager: Any,
    persona_id: str,
    plan_date: Any = None,
    *,
    wake: Optional[str] = None,
    downtime_recovery: bool = False,
) -> int:
    """当日 (営業日) plan の pending / deferred コマを再 push する (watchdog / 再起動後の再接続)。

    deferred コマも開始時刻 (過去なら即時扱い) で再 push する。繰り下げ待ちの
    残り時間は再起動を跨いで保持しない — 即時に発火し、まだ会話中なら
    ``_fire_slot`` が改めて繰り下げる (defer_count は永続化済みなので上限 3 回の
    帳簿は保たれる)。

    同 key 上書きなので二重呼び出ししても二重発火しない (冪等)。
    plan が無ければ 0。

    Args:
        plan_date: 引く営業日 (date / datetime / "YYYY-MM-DD")。None のときは
            :func:`resolve_business_day` が営業日と起点 (wake) を一度に自己解決
            する (確定ライフ優先)。呼び出し元 (watchdog_tick) も同じ解決器の
            答えを渡すので、経路によって営業日の解釈が割れない。
        wake: 起床時刻 "HH:MM"。_slot_fire_at に透過し、深夜帯コマの暦日補正
            に使う。省略時は営業日と同じ解決器から取る。
        downtime_recovery: True = プロセス再起動後の回復 (サーバーが落ちていた
            ことが確実な入口 — saiverse_manager.on_persona_registered)。開始
            時刻を :data:`MISSED_GRACE_MINUTES` (+ 繰り下げ済みぶんの猶予
            defer_count×DEFER_MINUTES) を超えて過ぎたコマは遅延実行せず
            「流れた（サーバーが起動していなかったため）」に確定する —
            起床判断の途中起動 (compose) と同じ意味論 (Codex 一巡目 #2。
            停止がどのタイミングかで「流れた」と「遅れて実行」に割れない)。
            watchdog のプロセス内回復 (予約途絶) では False のまま — サーバーは
            生きているので同じ理由文が嘘になる (長セッション待ち等の遅れを
            停止と誤記しない)。
    """
    basis: Optional[BusinessDay] = None
    if plan_date is None:
        # 営業日と起点は同じ解決器から一度に取る (:func:`resolve_business_day`)
        # — 別々に解決すると、日中に起床設定を変えた日は営業日そのものを
        # 取り違えて回復が空振りする (Codex 八巡目 #1)。
        basis = resolve_business_day(manager, persona_id)
        if basis is None:
            LOGGER.warning(
                "[day_plan] cannot resolve the business day (lives unreadable); "
                "leaving pending slots untouched for the next watchdog "
                "(persona=%s)", persona_id,
            )
            return 0
        plan_date_str = basis.plan_date
    else:
        plan_date_str = _normalize_plan_date(plan_date)
    if wake is None:
        # 予約の暦日補正の基準は当日確定ライフ優先 (Codex 七巡目 —
        # schedule_day_plan と同じ物差し)
        if basis is not None:
            wake = basis.wake  # 同じ解決器の答え (二度引かない)
        else:
            resolved = _resolve_wake_for_plan(manager, persona_id, plan_date_str)
            if resolved is _LIVES_UNREADABLE:
                # 起点が分からない = grace 判定の基準も分からない。押さず、
                # 「流れた」への再分類もせず次の watchdog へ委ねる
                # (Codex 八巡目 #2)。
                LOGGER.warning(
                    "[day_plan] lives unreadable; neither rescheduling nor "
                    "reclassifying slots (persona=%s date=%s)",
                    persona_id, plan_date_str,
                )
                return 0
            wake = resolved
    slots = load_day_plan(manager, persona_id, plan_date_str)
    if slots is None:
        return 0

    slots = _ensure_slot_ids(manager, persona_id, plan_date_str, slots)
    now = clock.now()
    pushed = 0
    reclassified = 0
    for index, slot in enumerate(slots):
        if slot.get("status") not in (STATUS_PENDING, STATUS_DEFERRED):
            continue
        try:
            # 発火予定時刻の正典 (:func:`_slot_fire_at` — 深夜跨ぎの暦日補正
            # 込み)。grace 判定も同じ解釈を使う — day_order (時刻文字列の相対
            # 順序) 比較は暦日を無視するため、深夜跨ぎ営業日で前夜のコマを
            # 「未来」と誤読して即時実行していた (Codex 二巡目 #2)。
            fire_at = _slot_fire_at(plan_date_str, slot, wake=wake)
        except Exception:
            # 時刻が解釈できないコマは fail-closed — 遅延実行に化けさせず
            # 保留する (押さなければ実行もされない)。
            LOGGER.warning(
                "[day_plan] cannot interpret slot start; leaving unscheduled "
                "(persona=%s date=%s index=%d start=%r)",
                persona_id, plan_date_str, index, slot.get("start"),
                exc_info=True,
            )
            continue
        if downtime_recovery:
            late_minutes = (now - fire_at).total_seconds() / 60.0
            allowance = MISSED_GRACE_MINUTES \
                + _coerce_defer_count(slot) * DEFER_MINUTES
            if late_minutes > allowance:
                updated = _update_slot(
                    manager, persona_id, plan_date_str, index,
                    expected_id=slot.get("id"),
                    status=STATUS_SKIPPED, skip_reason=SKIP_REASON_MISSED_START,
                )
                if updated is None:
                    # CAS 失敗 = 並走で状態が動いた。push も確定もせず次の
                    # watchdog / 回復に委ねる。
                    LOGGER.info(
                        "[day_plan] missed-start reclassify lost CAS; leaving "
                        "slot as-is (persona=%s date=%s index=%d)",
                        persona_id, plan_date_str, index,
                    )
                    continue
                reclassified += 1
                continue
        _push_slot(manager, persona_id, plan_date_str, slot["id"], fire_at)
        pushed += 1
    if pushed or reclassified:
        LOGGER.info(
            "[day_plan] rescheduled pending slots: persona=%s date=%s pushed=%d "
            "missed_start=%d",
            persona_id, plan_date_str, pushed, reclassified,
        )
    return pushed


def find_lost_slot_reservations(
    manager: Any, persona_id: str, plan_date: Any
) -> List[int]:
    """EventScheduler 予約が消えている pending / deferred コマの index を返す。

    watchdog (saiverse/autonomy_wiring.py) の「コマ予約の途絶」判定に使う。
    予約はインメモリなので再起動・EventScheduler 再生成で失われるが、
    slots_json の pending / deferred は残る — その差分が「途絶」。

    deferred コマは繰り下げ再 push (10 分後) の予約 key が同じなので、
    正常な繰り下げ待ち中は「予約あり」と判定される (途絶と誤認しない)。

    key はコマの不変 id ベース (:func:`_slot_key`) — 時間割の全置換で旧予約が
    残留しても key は衝突しないため、「新コマの予約が無い」を残留予約に隠されず
    正しく検出できる。id の無い legacy コマは常に「途絶」と判定する (回復側の
    :func:`reschedule_pending_slots` が id を採番して push する)。
    """
    plan_date_str = _normalize_plan_date(plan_date)
    scheduler = getattr(manager, "event_scheduler", None)
    if scheduler is None:
        return []
    slots = load_day_plan(manager, persona_id, plan_date_str) or []
    lost: List[int] = []
    for index, slot in enumerate(slots):
        if slot.get("status") not in (STATUS_PENDING, STATUS_DEFERRED):
            continue
        slot_id = slot.get("id")
        if not (isinstance(slot_id, str) and slot_id):
            lost.append(index)
            continue
        if not scheduler.has_key(_slot_key(persona_id, plan_date_str, slot_id)):
            lost.append(index)
    return lost


def cancel_scheduled_slots(
    manager: Any,
    persona_id: str,
    plan_date: Any,
    *,
    extra_ids: Optional[Iterable[str]] = None,
) -> int:
    """保存済み plan の全コマ分の EventScheduler 予約を cancel し、数を返す。

    時間割の全置換 (起床判断のやり直し / remaining_timetable の置換) の前処理。
    key はコマの不変 id ベース (:func:`_slot_key`) なので、置換で消えるコマの
    予約は「そのコマの id」で落とす。cancel を取りこぼしても残留予約は id 不一致
    で無害に空振りする (:func:`_fire_slot_by_id`) が、無駄な発火を残さないため
    ここで掃除する (発火済み key の cancel は no-op)。

    Args:
        extra_ids: 現 DB plan に**もう載っていない**コマの id 列。保存を先に
            済ませてから cancel する経路 (:func:`replace_day_plan`) が、置換前に
            控えた旧 plan のコマ id を渡して旧予約を落とすための口。
    """
    plan_date_str = _normalize_plan_date(plan_date)
    scheduler = getattr(manager, "event_scheduler", None)
    if scheduler is None:
        return 0
    slots = load_day_plan(manager, persona_id, plan_date_str) or []
    ids = [
        s.get("id") for s in slots if isinstance(s.get("id"), str) and s.get("id")
    ]
    for extra in extra_ids or ():
        if isinstance(extra, str) and extra and extra not in ids:
            ids.append(extra)
    cancelled = 0
    for slot_id in ids:
        if scheduler.cancel(_slot_key(persona_id, plan_date_str, slot_id)):
            cancelled += 1
    if cancelled:
        LOGGER.info(
            "[day_plan] cancelled scheduled slots: persona=%s date=%s cancelled=%d",
            persona_id, plan_date_str, cancelled,
        )
    return cancelled


def replace_remaining_slots(
    manager: Any, persona_id: str, plan_date: Any, new_slots: List[Dict[str, Any]]
) -> Tuple[int, List[str]]:
    """残りコマ (pending / deferred) を new_slots で全置換する (judgment_points.md §3.3)。

    消化済みコマ (fired / done / skipped) は帳簿として残し、残りコマだけを
    new_slots に差し替える。検証は保存時と同一 (``_validate_and_normalize_slots``)
    で、失敗時は ValueError を投げて **plan も予約も一切変更しない**。

    厳密昇順の検証は **new_slots の区間のみ** に適用する
    (``ascending_from=len(kept)``)。消化済みコマは書き換え不可の歴史であり、
    「直前に消化したコマと同時刻・過去時刻から始まる組み替え」(直近コマの
    やり直し等) は正当な意志なので、消化済み区間との境界比較で全却下しない
    (2026-07-05 実 LLM シム 3回目の不具合)。過去時刻の新コマは
    EventScheduler が即時扱いする。

    ライフが宣言されている日の組織化範囲の正規化 (丸め・部分救済。
    :func:`_normalize_slots_within_organized_range`) は **new_slots の区間のみ**
    に適用する — 消化済みコマは既に確定した過去であり、丸め・除外の対象では
    ない (昇順検証と同じ「歴史は保護する」思想)。

    Returns:
        ``(置換後に EventScheduler へ push した pending コマ数, 調整メモ)``。
        調整メモは丸め・除外が起きた場合の日常語の説明 (無調整なら空リスト)。

    Raises:
        ValueError: 書式検証失敗、または正規化後に new_slots 側が 1 件も
            残らなかった場合 (plan も予約も一切変更しない)。
    """
    plan_date_str = _normalize_plan_date(plan_date)
    notes: List[str] = []
    # CAS ループ (第五陣 P1): 「現 plan を読む → 差し替え配列を組む → 読んだ世代と
    # 同じときだけ保存」。読みと保存の間に別の書き手 (コマ発火の fired 書き込み等)
    # が commit していたら、最新 plan で組み直す — 古い配列の書き戻しでその決定を
    # 消さない。
    for _attempt in range(_CAS_MAX_RETRIES):
        db = manager.SessionLocal()
        try:
            row = _load_plan_row(db, persona_id, plan_date_str)
            current_payload = row.slots_json if row is not None else None
            current = _row_slots(row) if row is not None else []
        finally:
            db.close()
        kept_history = [
            s for s in current
            if s.get("status") not in (STATUS_PENDING, STATUS_DEFERRED)
        ]
        candidate = kept_history + [
            {**slot, "status": STATUS_PENDING, "defer_count": 0} for slot in new_slots
        ]
        # 失敗時はここで raise (昇順検証は新コマ区間のみ — docstring 参照)。
        # 順序の基準は save_day_plan と同じ「一日の流れ」順 (深夜跨ぎ対応)。
        # fresh_ids_from: 新コマ区間は id を新世代へ採番し直す (消化済み区間は帳簿
        # なので既存 id を保持 — 精算・回復が台帳 payload の slot_id で逆引きする)。
        # 呼び出し元が置換前の pending コマを id ごと写しても旧予約 key と衝突しない
        # (2026-07-20 Codex レビュー第四陣 P2 と同根)。
        lives = get_lives(manager, persona_id, plan_date_str)
        normalized = _validate_and_normalize_slots(
            candidate, ascending_from=len(kept_history),
            order_key=lambda h: day_order_minutes(lives, h),
            fresh_ids_from=len(kept_history),
        )
        history_part = normalized[:len(kept_history)]
        new_part = normalized[len(kept_history):]
        kept_new, notes = _normalize_slots_within_organized_range(
            manager, persona_id, plan_date_str, new_part,
        )
        if not kept_new:
            reasons = "; ".join(n.strip("（）") for n in notes) or "コマが活動時間の範囲外でした"
            raise ValueError(f"編成できる範囲 (今〜就寝) に収まるコマがありませんでした ({reasons})")

        # 検証が通ってから旧予約を落とし、保存 (読んだ世代と同じときだけ) → 再 push。
        # 保存が世代不一致で見送られた場合、cancel 済み予約は watchdog が回復する
        # (id key なので途絶は正しく検出される)。
        final_slots = history_part + kept_new
        cancel_scheduled_slots(manager, persona_id, plan_date_str)
        if _upsert_plan_slots(
            manager, persona_id, plan_date_str, final_slots,
            expected_payload=current_payload,
        ):
            break
        LOGGER.info(
            "[day_plan] replace_remaining_slots CAS conflict: plan changed since "
            "read; rebuilding from fresh plan (persona=%s date=%s attempt=%d/%d)",
            persona_id, plan_date_str, _attempt + 1, _CAS_MAX_RETRIES,
        )
    else:
        raise RuntimeError(
            f"remaining-slot replacement kept conflicting with concurrent plan "
            f"writes; giving up (persona={persona_id} date={plan_date_str} — "
            f"current plan retained, watchdog restores its reservations)"
        )
    if notes:
        LOGGER.info(
            "[day_plan] remaining slots adjusted to organized range: "
            "persona=%s date=%s notes=%s", persona_id, plan_date_str, notes,
        )
    pushed = schedule_day_plan(manager, persona_id, plan_date_str)
    return pushed, notes


def replace_day_plan(
    manager: Any,
    persona_id: str,
    plan_date: Any,
    new_slots: List[Dict[str, Any]],
    *,
    ledger_prefix: int = 0,
) -> Tuple[int, List[str]]:
    """時間割を ``new_slots`` で原子的に全置換する (A1、起床判断 day_open の finalize)。

    :func:`replace_remaining_slots` (残りコマの置換) の全置換版。消化済みコマの
    帳簿を残さない — day_open は一日の最初の編成なので、旧 plan (前回の day_open
    のやり直し等) はまるごと ``new_slots`` に差し替える。

    ``ledger_prefix`` (時間割改修 T2): ``new_slots`` の先頭のこの件数を帳簿区間
    (消化済み扱い — テンプレート経路の「流れた」コマ) として扱い、昇順・kind
    語彙・組織化範囲の丸めを免除する (:func:`_validate_and_normalize_for_save`
    参照)。帳簿区間は pending でないため EventScheduler へは push されない。

    **原子性 (A1 の是正) — 保存を先に**: 「旧 plan は可視なのに予約だけ消えた」孤児を
    どの失敗経路でも作らないため、**cancel より先に保存**する:

    1. :func:`save_day_plan` と同一の検証・ライフ範囲正規化を **先に** 済ませる —
       ``ValueError`` を投げる場合、DB / スケジューラは一切未変更。
    2. 新 plan を **保存** する (``_upsert_plan_slots``)。ここで失敗した場合、旧 plan は
       DB に残り、旧予約は **まだ cancel していない** ので無傷 — **継続的な DB 障害でも
       孤児を作らない** (旧実装の「先に cancel → 保存 raise」も、前修正の「復元も同じ
       保存に依存」も、継続障害で予約消失が再発した。保存先行はその依存を断つ)。
    3. 保存成功 = 置換の durable 部分は完了。旧予約を落とし (保存前に控えた旧コマ
       id で cancel) 新 plan を再 push する。この張り替えが失敗しても DB は既に
       新 plan なので、watchdog (``find_lost_slot_reservations``) が新 plan の
       pending 予約を回復する — 例外にせず WARN で返す (収束先は新 plan。旧 plan
       への逆戻り孤児は起きない)。**cancel 失敗で旧時刻の予約が残留しても安全**:
       予約 key はコマの不変 id ベースなので、残留予約は発火時に「その id のコマは
       もう無い」で空振りし (:func:`_fire_slot_by_id`)、新コマの key 不在は
       watchdog が残留予約に隠されず検出できる (旧 index ベース key では key
       文字列が衝突し、この両方が破れていた — 2026-07-20 Codex レビュー第三陣)。

    Returns:
        ``(EventScheduler へ push した pending コマ数, 調整メモ)``。予約張り替えが
        失敗した場合 pushed=0 (watchdog 回復に委ねる)。

    Raises:
        ValueError: persona_id 空 / コマ配列の書式検証失敗 / 正規化後にコマが
            1 件も残らなかった場合 (この時点で DB もスケジューラも未変更)。
        Exception: **保存** (``_upsert_plan_slots``) の失敗はそのまま再送出する
            (この時点で旧 plan / 旧予約はともに無傷)。
    """
    if not persona_id:
        raise ValueError("persona_id is required")
    plan_date_str = _normalize_plan_date(plan_date)
    # 1. 検証・正規化を先に (raise してもこの時点まで DB / スケジューラ未変更)。
    # fresh_ids=True: 全置換は id の**新世代**を必須にする。呼び出し元が旧コマを
    # id ごと写した入力でも、新 plan の予約 key は旧予約と必ず別物になる —
    # 「cancel 失敗の残留予約は id 不一致で無害に空振り + watchdog が新コマの
    # key 不在を検出」という手順 3 の安全性を、入力の形に依存させず契約で保証する
    # (2026-07-20 Codex レビュー第四陣 P2)。
    kept, notes = _validate_and_normalize_for_save(
        manager, persona_id, plan_date_str, new_slots, fresh_ids=True,
        ledger_prefix=ledger_prefix,
    )
    # 旧予約 cancel 用に、置換で消える旧コマの id を保存前に控える (保存後は
    # DB から旧 plan が消えるため後からは引けない)。
    old_ids = [
        s.get("id") for s in (load_day_plan(manager, persona_id, plan_date_str) or [])
        if isinstance(s.get("id"), str) and s.get("id")
    ]
    # 2. 保存を先に。失敗すれば旧 plan は DB に残り旧予約も未 cancel = 無傷 (継続障害でも
    #    「旧 plan 可視・予約消失」の孤児を作らない — A1 の恒久修正)。そのまま再送出。
    _upsert_plan_slots(manager, persona_id, plan_date_str, kept)
    # 3. 保存成功後に旧予約を id で落とし、新 plan を push。ここが失敗しても DB は
    #    新 plan なので watchdog が回復する — 例外にせず pushed=0 で返す (残留した
    #    旧予約は id 不一致で無害に空振りする — docstring 手順 3 参照)。
    try:
        cancel_scheduled_slots(
            manager, persona_id, plan_date_str, extra_ids=old_ids,
        )
        pushed = schedule_day_plan(manager, persona_id, plan_date_str)
    except Exception:
        LOGGER.exception(
            "[day_plan] replace_day_plan saved new plan but reservation swap failed; "
            "watchdog will recover reservations (persona=%s date=%s)",
            persona_id, plan_date_str,
        )
        pushed = 0
    if notes:
        LOGGER.info(
            "[day_plan] day plan replaced within organized range: "
            "persona=%s date=%s notes=%s", persona_id, plan_date_str, notes,
        )
    return pushed, notes


# ---------------------------------------------------------------------------
# コマ発火 (LLM を呼ばない決定論処理)
# ---------------------------------------------------------------------------


def get_user_conversation_state(manager: Any, persona_id: str) -> Optional[bool]:
    """ユーザー会話中か。**読めなかったときは None** (不明) を返す三値版。

    :func:`is_in_user_conversation` の実装本体。判定そのものはここ 1 つに保つ
    (下の docstring 参照)。「不明」を「会話していない」へ丸めるかは呼び出し側の
    判断なので、丸めない生の答えをここが返す:

    - 既定の呼び出し側 (時間割・判断点) は fail-open で構わない — 会話でない
      前提で自律を進めても、取り返しのつかない出来事は起きない
    - ユーザーへ声を出す側 (tell スペル) は fail-closed にする — 会話中に
      重ねて話しかける失敗は届いた後では取り消せない
    """
    try:
        from saiverse import episodes

        ep = episodes.get_open_episode(
            manager, persona_id, kind=episodes.KIND_CONVERSATION,
        )
    except Exception:
        LOGGER.warning(
            "[day_plan] get_open_episode failed (persona=%s); conversation state unknown",
            persona_id, exc_info=True,
        )
        return None
    return ep is not None


def is_in_user_conversation(manager: Any, persona_id: str) -> bool:
    """ユーザー会話中か。開いている kind='conversation' の出来事があれば True。

    読み取りに失敗したときは False (fail-open)。不明と「会話していない」を
    区別したい呼び出し側は :func:`get_user_conversation_state` を使う。

    **「いま会話中か」を判定したい全ての箇所はこの関数を使うこと** (2026-07-29 公開化)。
    running Track の種別を見る旧判定が `judgment_points.build_on_event_situation_text`
    に残っており、終了済みの会話を「ユーザーと会話中です」と LLM へ渡していた
    (案 Y の追従漏れ)。同型の再発を防ぐため、判定の実装はここ 1 つに保つ。

    life.md §7 案 Y (2026-07-13): 「いま」の真実は開いているエピソードが持つ。
    無応答タイムアウトが会話の出来事を閉じた瞬間が v2 の「会話終了」に相当する
    (``autonomy_wiring.handle_conversation_end``)。旧実装 (running Track が
    user_conversation 種別か) は、Track がもう時間経過で pending に落ちない
    (running のまま残り続けうる) ため使えない。
    """
    return get_user_conversation_state(manager, persona_id) is True


def _building_display_name(manager: Any, building_id: Any) -> str:
    """building_id を表示名へ解決する (building_map が無い / 未登録なら ID のまま)。"""
    building = (getattr(manager, "building_map", {}) or {}).get(building_id)
    name = getattr(building, "name", None)
    return str(name or building_id)


def _record_move_failure(
    manager: Any, persona: Any, slot: Dict[str, Any],
    current: Any, target: Any, reason: Any,
) -> None:
    """施設移動の失敗をペルソナに見える形で記録する (知覚バッファ経由)。

    移動失敗時のフォールバックは「移動せず現在地で実行」だが、それが黙って
    起きるとペルソナは「予定の場所で作業した」つもりのまま現在地の文脈で
    振る舞う (接地原則違反の温床)。知覚バッファ (kind='world_state') に積み、
    次の Beat 頭の消費でペルソナの context へ乗る (W14: event_message 直挿しの
    移送, perception_buffer.md §10.6)。
    """
    adapter = getattr(persona, "sai_memory", None)
    if adapter is None or not hasattr(adapter, "push_perception"):
        return
    target_name = _building_display_name(manager, target)
    current_name = _building_display_name(manager, current)
    title = str(slot.get("title") or slot.get("kind") or "").strip()
    reason_text = str(reason or "理由不明")
    content = (
        f"時間割のコマ「{title}」で予定していた場所「{target_name}」へ"
        f"移動できませんでした（{reason_text}）。"
        f"このコマは現在地「{current_name}」で行います。"
    )
    try:
        adapter.push_perception("world_state", content)
    except Exception:
        LOGGER.warning(
            "[day_plan] failed to record move failure notice (persona=%s)",
            getattr(persona, "persona_id", "?"), exc_info=True,
        )


def _move_to_facility(manager: Any, persona_id: str, slot: Dict[str, Any]) -> bool:
    """facility が現在地と違えば OccupancyManager で移動する。

    移動の実体 (occupancy / DB / host メッセージ) も
    ``persona.current_building_id`` / cursor 儀式の更新も ``move_entity`` に
    集約済み (W7 柱5: 属性更新は移動 service の責務。かつては呼び出し側責務で、
    更新漏れがコマの作業セッションを終日 stale な旧建物の文脈で走らせた —
    2026-07-05 実 LLM シム 異常 #1)。

    移動失敗 (満員等) は「移動せず現在地で実行」に倒すが、黙って現在地に
    ならないようその事実を WARN + ペルソナへの system 通知で記録する。

    Returns:
        True = コマの場所に居る (移動成功 / 既に現地 / 移動の指定なし)。
        False = 移動が必要だったのにできなかった (handler が「出かけた」体の
        記録を書かないための事実。呼び出し側は ``_apply_slot_move`` 経由で
        一時キー ``_move_failed`` として handler へ運ぶ)。
    """
    persona = (getattr(manager, "personas", {}) or {}).get(persona_id)
    if persona is None:
        LOGGER.warning("[day_plan] persona %s not loaded; skipping facility move", persona_id)
        return False

    target = slot.get("facility")
    if target == FACILITY_OWN_ROOM:
        target = getattr(persona, "private_room_id", None)
        if not target:
            LOGGER.warning(
                "[day_plan] persona %s has no private_room_id; skipping facility move",
                persona_id,
            )
            return False
    current = getattr(persona, "current_building_id", None)
    if not target or target == current:
        return True

    occupancy = getattr(manager, "occupancy_manager", None)
    if occupancy is None:
        LOGGER.warning("[day_plan] manager has no occupancy_manager; skipping facility move")
        return False
    try:
        ok, msg = occupancy.move_entity(persona_id, "ai", current, target)
    except Exception:
        LOGGER.warning(
            "[day_plan] move_entity raised (persona=%s %s -> %s); continuing",
            persona_id, current, target, exc_info=True,
        )
        _record_move_failure(manager, persona, slot, current, target, "内部エラー")
        return False
    if not ok:
        LOGGER.warning(
            "[day_plan] facility move failed (persona=%s %s -> %s): %s — continuing in place",
            persona_id, current, target, msg,
        )
        _record_move_failure(manager, persona, slot, current, target, msg)
        return False

    # 位置属性と cursor 儀式 (_mark_entry / _save_session_metadata) は
    # move_entity が canonical sync 済み (W7 柱5)
    LOGGER.info(
        "[day_plan] moved for slot: persona=%s %s -> %s", persona_id, current, target
    )
    return True


def _apply_slot_move(
    manager: Any, persona_id: str, slot: Dict[str, Any]
) -> Dict[str, Any]:
    """コマの場所へ移動し、結果を一時キーで slot に載せて返す。

    - ``_outing_unresolved`` (行き先未解決) のコマは移動しない — own_room を
      「自室へ移動」と誤読して逆移動しないため。
    - 移動が必要だったのにできなかったときは ``_move_failed`` を立てる。
      handler はこれを見て「出かけて来ました」体の捏造を避け、実際の現在地の
      事実だけを提示する (接地原則)。

    どちらの一時キーも in-memory 限りで永続化されない (:func:`_update_slot` は
    明示フィールドだけを書く)。
    """
    if slot.get("_outing_unresolved"):
        return slot
    if _move_to_facility(manager, persona_id, slot):
        return slot
    return {**slot, "_move_failed": True}


def _effective_budget_rounds(slot: Dict[str, Any]) -> int:
    """コマの実効ラウンド予算 (0 / 未指定は既定値 DEFAULT_BUDGET_ROUNDS)。"""
    budget = int(slot.get("budget_rounds") or 0)
    return budget if budget >= 1 else DEFAULT_BUDGET_ROUNDS


def effective_budget_total(slots: Iterable[Dict[str, Any]]) -> int:
    """予算ゲート対象コマの実効ラウンド合計。

    表示・警告 (judgment_finalize の日次予算検算) が、実行時のゲート
    (:func:`_apply_budget_gate` が引く実効値 — 0/未指定は既定
    :data:`DEFAULT_BUDGET_ROUNDS`) と同じ単位・同じ値で合計するための正典
    (Codex 三巡目 — 保存値の素朴な合計は「空欄の作業コマ複数=合計 0 表示なのに
    実行時は各 8 ラウンド」の不一致を生む)。非ゲート kind (出かける等) は
    予算を消費しないため数えない。
    """
    total = 0
    for s in slots:
        if str(s.get("kind") or "") in _BUDGET_GATED_KINDS:
            total += _effective_budget_rounds(s)
    return total


# ---------------------------------------------------------------------------
# コマの出来事 (Episode): 実行区間の記録 (life_concept_map.md §8.1)
# ---------------------------------------------------------------------------


def _episode_kind_for_slot(slot_kind: Any) -> str:
    """コマ種別 → 出来事 kind の写像。

    作業セッション系のコマは中の作業セッションが別の出来事
    (kind='work_session') を開くため、コマの実行区間そのものは kind='slot'
    として並存させる (セッション側の origin_ref がコマ出来事を指して親子が
    読める)。それ以外 (出かける/自室で過ごす/自由時間) は presence — 実行の
    中身 (コマ開始の Pulse の思考) は SAIMemory 側に残り、出来事としては
    「その場に居た」区間の記録でよい (旧 暮らし/休む と同じ扱い)。
    """
    from saiverse import episodes

    if slot_kind in WORKER_SESSION_KINDS:
        return episodes.KIND_SLOT
    return episodes.KIND_PRESENCE


def _slot_origin_ref(persona_id: str, plan_date_str: str, index: int) -> str:
    """コマ参照 (出来事の origin_ref)。発火時 index ベースの決定論文字列。

    EventScheduler の予約 key (:func:`_slot_key`) が不変 id ベースへ移行した後も、
    こちらは**意図的に旧形式 (index) のまま**据え置く — 回復
    (execution_ledger_wiring の settle-close / 孤児 episode close) が台帳 payload
    の index から同じ文字列を再構成して既存 open episode を逆引きするため、形式を
    変えると移行時点で走行中だった実行の episode が閉じられなくなる。コマ参照の
    統一文法化 (id への一本化を含む) は P5 (life_concept_map.md §14) で再訪する。
    """
    return f"day_plan:{persona_id}:{plan_date_str}:{index}"


def _open_slot_episode(
    manager: Any, persona_id: str, plan_date_str: str, slot: Dict[str, Any], index: int
) -> Optional[str]:
    """コマの実行区間の出来事を開き、episode_ref を返す (失敗時 None)。

    呼び出し点は発火チェック (繰り下げ / 予算 / ハンドラ有無) と施設移動を
    抜けた後 — skip されたコマは出来事を作らない。場所は移動後の現在地。
    出来事は記録専用でコマの実行には影響しない (失敗は WARN のみ)。
    """
    try:
        from saiverse import episodes

        persona = (getattr(manager, "personas", {}) or {}).get(persona_id)
        building_id = getattr(persona, "current_building_id", None)
        meta: Dict[str, Any] = {"slot_kind": str(slot.get("kind") or "")}
        title = str(slot.get("title") or "").strip()
        if title:
            meta["title"] = title
        ep = episodes.open_episode(
            manager, persona_id,
            _episode_kind_for_slot(slot.get("kind")),
            building_id=building_id,
            participants=[persona_id],
            origin_ref=_slot_origin_ref(persona_id, plan_date_str, index),
            meta=meta,
        )
        return ep.get("episode_ref")
    except Exception:
        LOGGER.warning(
            "[day_plan] failed to open slot episode (persona=%s date=%s index=%d)",
            persona_id, plan_date_str, index, exc_info=True,
        )
        return None


def _close_slot_episode(
    manager: Any,
    persona_id: str,
    episode_ref: Optional[str],
    slot_after: Optional[Dict[str, Any]],
) -> None:
    """コマの出来事を閉じる。record_level (presence_only 等) を meta に透過する。"""
    if not episode_ref:
        return
    try:
        from saiverse import episodes

        meta: Optional[Dict[str, Any]] = None
        record_level = str((slot_after or {}).get("record_level") or "")
        if record_level:
            meta = {"record_level": record_level}
        episodes.close_episode(manager, persona_id, episode_ref, meta=meta)
    except Exception:
        LOGGER.warning(
            "[day_plan] failed to close slot episode %s (persona=%s)",
            episode_ref, persona_id, exc_info=True,
        )


def _apply_budget_gate(
    manager: Any, persona_id: str, plan_date_str: str, index: int, slot: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """予算ゲート (v2 §4.5 / life.md Phase2 §7)。切り詰め後の slot を返す。
    発火中止なら None。

    - ライフが宣言されている日 (:func:`get_lives` 非空): そのコマが属する
      ライフの残 (budget_pulses − 消費) を見る二値ゲート
      (:func:`_apply_life_budget_gate`)。ラウンド数のクランプはしない —
      パルスとラウンドは単位が異なるため
    - ライフの無い日: 旧ゲート (台帳が無ければ無効 = slot をそのまま返す。
      残高 < 実効予算 → 残高まで切り詰め + WARN。残高 0 → skipped + WARN)
    """
    lives = get_lives(manager, persona_id, plan_date_str)
    if lives:
        return _apply_life_budget_gate(manager, persona_id, plan_date_str, index, slot, lives)

    requested = _effective_budget_rounds(slot)
    state = get_budget_state(manager, persona_id, plan_date_str)
    if state is None:
        return slot
    remaining = state["remaining"]
    if remaining <= 0:
        _update_slot(
            manager, persona_id, plan_date_str, index,
            expected_id=slot.get("id"),
            status=STATUS_SKIPPED, skip_reason=SKIP_REASON_BUDGET_EXHAUSTED,
        )
        LOGGER.warning(
            "[day_plan] slot skipped: daily budget exhausted "
            "(persona=%s date=%s index=%d kind=%s used=%d/%d)",
            persona_id, plan_date_str, index, slot.get("kind"),
            state["used"], state["total"],
        )
        return None
    if remaining < requested:
        LOGGER.warning(
            "[day_plan] slot budget clamped to remaining daily budget: %d -> %d "
            "(persona=%s date=%s index=%d kind=%s used=%d/%d)",
            requested, remaining, persona_id, plan_date_str, index,
            slot.get("kind"), state["used"], state["total"],
        )
        requested = remaining
    if requested != int(slot.get("budget_rounds") or 0):
        # 切り詰め (または既定値の具体化) を slots_json に永続化する — 就寝判断の
        # 予定 vs 実績が「実際に許可された予算」を見られるようにするため。
        updated = _update_slot(
            manager, persona_id, plan_date_str, index,
            expected_id=slot.get("id"), budget_rounds=requested,
        )
        if updated is not None:
            return updated
        slot = {**slot, "budget_rounds": requested}
    return slot


def _fire_slot(
    manager: Any,
    persona_id: str,
    plan_date_str: str,
    index: int,
    slot_id: Optional[str] = None,
) -> None:
    """コマ発火。判断点ではない — 発火の骨格 (繰り下げ・予算・移動・帳簿) は
    LLM を呼ばない (judgment_points.md §2。実行本体 = ハンドラの中の LLM 使用は
    別 — モジュール docstring 参照)。

    1. ユーザー会話中 → 繰り下げ (10 分後に同 key 再 push、上限 3 回で skipped)
    2. 予算ゲート (consumes_budget な kind のみ): 残高で切り詰め / 残高 0 は skipped
    3. facility が現在地と違えば移動 (失敗は WARN + 続行)
    4. kind 別ハンドラ実行 → 実 rounds_used を台帳へ積算 →
       status 更新 (fired → done / 未登録 kind は skipped)

    Args:
        slot_id: 発火対象コマの不変 id (EventScheduler 経由 = :func:`_fire_slot_by_id`
            は必ず渡す)。指定時は **本関数自身が読み込んだ最新 plan** で id から
            現在位置を解決し、``index`` は使わない — 呼び出し元の id→index 解決と
            本関数の再読込の間に時間割が組み替わると別コマを発火する窓
            (2026-07-20 Codex レビュー第四陣 P1) を、照準に使う配列と読んだ配列を
            同一にすることで閉じる。id が見つからなければ無害に空振りする。
            None はテスト等の index 直指定 (後方互換)。
    """
    slots = load_day_plan(manager, persona_id, plan_date_str)
    if slots is None:
        LOGGER.warning(
            "[day_plan] fire: plan not found (persona=%s date=%s index=%d id=%s)",
            persona_id, plan_date_str, index, slot_id,
        )
        return
    if slot_id is not None:
        resolved = _find_slot_index_by_id(slots, slot_id)
        if resolved is None:
            LOGGER.info(
                "[day_plan] fire: slot id=%s not in current plan; ignoring stale "
                "fire (persona=%s date=%s)", slot_id, persona_id, plan_date_str,
            )
            return
        index = resolved
    elif index >= len(slots):
        LOGGER.warning(
            "[day_plan] fire: slot not found (persona=%s date=%s index=%d)",
            persona_id, plan_date_str, index,
        )
        return
    slot = slots[index]
    status = slot.get("status")
    if status not in (STATUS_PENDING, STATUS_DEFERRED):
        LOGGER.info(
            "[day_plan] fire: slot already %s; ignoring (persona=%s date=%s index=%d)",
            status, persona_id, plan_date_str, index,
        )
        return

    # 不変 ID を確保する。creation (_validate_and_normalize_slots) と予約 push
    # (_ensure_slot_ids) で採番済みのはずだが、テスト等が index 直指定で呼ぶ経路の
    # ため防御的に補填する。以降の繰り下げ再 push・claim・精算・回復は配列 index
    # ではなくこの ID で対象コマを指す (ハンドラ中の時間割組み替え = post_session の
    # replace_remaining_slots で index が移動しても正しいコマを done にするため。
    # index ベース冪等キーの衝突 = 旧 index の別コマが誤 dedup される問題も同時に断つ)。
    slot_id = slot.get("id")
    if not isinstance(slot_id, str) or not slot_id:
        slot_id = uuid.uuid4().hex[:12]
        backfilled = _update_slot(manager, persona_id, plan_date_str, index, id=slot_id)
        slot = backfilled if backfilled is not None else {**slot, "id": slot_id}

    # (a) ユーザー会話中なら繰り下げ (v2 §4.2「割り込み」/ 会話の至上性)
    if is_in_user_conversation(manager, persona_id):
        defer_count = _coerce_defer_count(slot)
        if defer_count >= MAX_DEFERRALS:
            _update_slot(
                manager, persona_id, plan_date_str, index, expected_id=slot_id,
                status=STATUS_SKIPPED, skip_reason=SKIP_REASON_DEFERRAL_LIMIT,
            )
            LOGGER.info(
                "[day_plan] slot skipped after %d deferrals (persona=%s date=%s index=%d kind=%s)",
                defer_count, persona_id, plan_date_str, index, slot.get("kind"),
            )
            return
        _update_slot(
            manager, persona_id, plan_date_str, index, expected_id=slot_id,
            status=STATUS_DEFERRED, defer_count=defer_count + 1,
        )
        retry_at = clock.now() + timedelta(minutes=DEFER_MINUTES)
        _push_slot(manager, persona_id, plan_date_str, slot_id, retry_at)
        LOGGER.info(
            "[day_plan] slot deferred (user conversation in progress): persona=%s date=%s "
            "index=%d kind=%s defer=%d/%d retry_at=%s",
            persona_id, plan_date_str, index, slot.get("kind"),
            defer_count + 1, MAX_DEFERRALS, retry_at.isoformat(timespec="seconds"),
        )
        return

    kind = slot.get("kind")
    handler = _SLOT_HANDLERS.get(kind)
    if handler is None:
        if slot_kind_catalog.get_kind_by_name(str(kind or "")) is None:
            # 現行カタログに無い kind (旧語彙 / 無効化されたアドオン種別)。
            # システム障害ではなく語彙の世代交代 — 理由を分けて正直に記録する
            # (勝手な旧→新読み替えはしない。定数定義部の裁定コメント参照)。
            _update_slot(
                manager, persona_id, plan_date_str, index, expected_id=slot_id,
                status=STATUS_SKIPPED,
                skip_reason=SKIP_REASON_KIND_NOT_IN_VOCABULARY,
            )
            LOGGER.info(
                "[day_plan] slot kind %r is not in the current catalog "
                "vocabulary; slot skipped honestly (persona=%s date=%s index=%d)",
                kind, persona_id, plan_date_str, index,
            )
            return
        _update_slot(
            manager, persona_id, plan_date_str, index, expected_id=slot_id,
            status=STATUS_SKIPPED, skip_reason=SKIP_REASON_NO_HANDLER,
        )
        LOGGER.warning(
            "[day_plan] no handler registered for kind=%r; slot skipped "
            "(persona=%s date=%s index=%d)",
            kind, persona_id, plan_date_str, index,
        )
        return

    # (b) 予算ゲート (v2 §4.5): セッション系コマのみ。残高 0 なら skipped で終了
    gated = kind in _BUDGET_GATED_KINDS
    if gated:
        gated_slot = _apply_budget_gate(manager, persona_id, plan_date_str, index, slot)
        if gated_slot is None:
            return
        slot = gated_slot

    # (c) 施設へ移動 (型 → 行き先の実行。移動自体が接地した行動 — v2 §6.1)。
    # 「出かける」コマの行き先の穴 (own_room のまま) は移動の前に決定論で
    # 確定する (T3。LLM は使わない — 行き先の具体は着いた場所が与える)。
    slot = _resolve_outing_destination(manager, persona_id, plan_date_str, index, slot)
    slot = _apply_slot_move(manager, persona_id, slot)

    # (d) コマ発火を実行台帳で包む (A5/A6, W2 Chunk B)。台帳の無い環境
    # (旧テストスタブ) は従来挙動へ縮退する。
    ledger = getattr(manager, "execution_ledger", None)
    if ledger is None:
        if persona_id not in _LEDGER_MISSING_WARNED:
            _LEDGER_MISSING_WARNED.add(persona_id)
            LOGGER.warning(
                "[day_plan] manager has no execution_ledger; slot firing runs "
                "without ledger tracking / atomic settlement (persona=%s)",
                persona_id,
            )
        _fire_slot_legacy(manager, persona_id, plan_date_str, index, slot, kind, gated, handler)
        return

    # 予約する実効ラウンド (ゲート後の clamped slot に対して算出)。非 gated は 0。
    reserved = _effective_budget_rounds(slot) if gated else 0
    payload = {
        "persona_id": persona_id,
        "plan_date": plan_date_str,
        "index": index,            # 発火時の index (episode origin_ref の再構成用)
        "slot_id": slot_id,        # 不変 ID (精算・回復の対象コマ特定用)
        "slot_kind": kind,
        "reserved_rounds": reserved,
    }
    exec_id, runnable, _existing = ledger.claim_execution(
        "slot.fire",
        idempotency_key=f"{persona_id}:{plan_date_str}:{slot_id}",
        persona_id=persona_id,
        payload=payload,
    )
    if not runnable:
        # 既に発火済み (running/applied/completed) or unknown ブロック。二重発火
        # ガード — EventScheduler 二重発火・watchdog 再 push の重複を吸収する。
        LOGGER.info(
            "[day_plan] slot.fire not runnable (already fired / blocked); skipping "
            "double-fire (persona=%s date=%s index=%d id=%s kind=%s exec=%s status=%s)",
            persona_id, plan_date_str, index, slot_id, kind, exec_id, _existing,
        )
        return

    # --- 予約 tx (単一 commit): running + slot fired + 予算予約 + episode open ---
    # v0.5 (life.md §5.3): コマの発火そのものは標準パルスの消費に数えない
    # (暮らし/休む スタブの発火が予算を食い潰した実機初日の破綻を避ける)。数える
    # のは gated な作業コマの実効ラウンド予約だけ。
    try:
        episode_ref = None
        for _attempt in range(_CAS_MAX_RETRIES):
            try:
                episode_ref = _reserve_slot_tx(
                    manager, ledger, exec_id, persona_id, plan_date_str, index,
                    slot_id, slot, gated, reserved,
                )
                break
            except _PlanGenerationConflict as exc:
                # 読んだ世代が置換で古くなった — 最新 plan で対象を id から引き
                # 直して再試行 (置換で対象が消えていれば _SlotVanished へ落ちる)。
                LOGGER.info(
                    "[day_plan] reservation hit plan generation conflict; retrying "
                    "(attempt=%d/%d): %s", _attempt + 1, _CAS_MAX_RETRIES, exc,
                )
        else:
            LOGGER.warning(
                "[day_plan] reservation kept conflicting with plan rewrites; "
                "giving up this fire — slot left pending, watchdog will re-push "
                "(persona=%s date=%s id=%s exec=%s)",
                persona_id, plan_date_str, slot_id, exec_id,
            )
            return
    except _ClaimLost as exc:
        # prepared → running の席取りに負けた = 同じ exec_id を共有する並走発火の
        # 勝者が走行中 (または既に完了)。予約 tx は全ロールバック済みで副作用ゼロ。
        # 台帳は勝者の所有物 — 一切書かずに離脱する。
        LOGGER.info("[day_plan] slot.fire lost claim race; skipping: %s", exc)
        return
    except _SlotVanished as exc:
        # コマが claim 後・予約前に消えた / 発火不能になった (組み替え、または並走
        # 発火の勝者が先に fired にした)。mark_running より前に中断され副作用ゼロ。
        # 台帳は **prepared のときだけ** failed に落とす — 二重 claim で同じ exec_id
        # を共有する勝者が running 中なら、その台帳を壊さず離脱する (壊すと勝者の
        # 精算が failed → applied の不正遷移で爆発する — Codex レビュー第三陣)。
        LOGGER.info(
            "[day_plan] slot.fire aborted before reservation: %s (exec=%s)", exc, exec_id,
        )
        try:
            abandoned = ledger.abandon_prepared(
                exec_id, f"slot vanished before reservation: {exc}"
            )
            if not abandoned:
                LOGGER.info(
                    "[day_plan] slot.fire ledger left untouched (owned by concurrent "
                    "winner): exec=%s", exec_id,
                )
        except Exception:
            LOGGER.exception(
                "[day_plan] failed to abandon vanished slot.fire (exec=%s)", exec_id,
            )
        return
    except Exception:
        # 予約 tx は全ロールバック済み: slot pending・予算不変・episode 無し・
        # 台帳は prepared のまま。ハンドラは呼ばない (watchdog が pending を
        # 再 push、claim が prepared を再利用して安全再実行)。
        LOGGER.exception(
            "[day_plan] slot.fire reservation tx failed; handler NOT called, slot "
            "left pending, budget unchanged (persona=%s date=%s index=%d kind=%s exec=%s)",
            persona_id, plan_date_str, index, kind, exec_id,
        )
        return

    LOGGER.info(
        "[day_plan] slot fired: persona=%s date=%s index=%d id=%s kind=%s ref=%s "
        "facility=%s reserved=%d exec=%s",
        persona_id, plan_date_str, index, slot_id, kind, slot.get("ref"),
        slot.get("facility"), reserved, exec_id,
    )

    # desire 参照コマの発火 = 欲求への再訪。帳簿 (touch_count / 鮮度) に記録する
    # (v2 §5.3「何度も選ばれ再訪される欲求は関心に深まる」)。**予約 tx が成立した後**
    # に付ける — 予約前だと episode open 失敗等で予約が転けても touch_count だけ増え、
    # 再試行のたびに実際には取り組んでいない欲求を昇格候補に押し上げてしまう
    # (予約成立 = 「取り組みに向かった」の確定点)。ハンドラの成否には依らない。
    ref = slot.get("ref") or REF_NONE
    if ref != REF_NONE and ref.startswith("task:"):
        try:
            from saiverse.desire_engine import touch_desire
            touch_desire(manager, persona_id, ref)
        except Exception:
            LOGGER.warning(
                "[day_plan] touch_desire failed (persona=%s ref=%s); continuing",
                persona_id, ref, exc_info=True,
            )

    # --- ハンドラ (running 区間) ---
    try:
        used_rounds = handler(manager, persona_id, plan_date_str, slot, index)
    except Exception:
        # 防御経路 (run_work_session は raise しない契約)。LLM が動いたか不明な
        # ので mark_unknown (自動再実行禁止の照合対象)。slot は fired のまま
        # (「実行したが完了記録なし」の観察)、予約額は保持。出来事は best-effort
        # で閉じる (実行区間は終わった)。
        LOGGER.exception(
            "[day_plan] slot handler raised (persona=%s date=%s index=%d kind=%s exec=%s); "
            "slot left 'fired', budget reservation retained, marking execution unknown",
            persona_id, plan_date_str, index, kind, exec_id,
        )
        _close_slot_episode(manager, persona_id, episode_ref, None)
        try:
            ledger.mark_unknown(exec_id, "slot.fire handler raised")
        except Exception:
            LOGGER.exception(
                "[day_plan] failed to mark slot.fire execution unknown after handler "
                "raise (persona=%s exec=%s)", persona_id, exec_id,
            )
        return

    # --- 精算 tx (単一 commit): 予算調整 + slot done + episode close + applied ---
    try:
        for _attempt in range(_CAS_MAX_RETRIES):
            try:
                _settle_slot_tx(
                    manager, ledger, exec_id, persona_id, plan_date_str, index,
                    slot_id, slot, episode_ref, gated, reserved, used_rounds,
                )
                break
            except _PlanGenerationConflict as exc:
                # 読んだ世代が古い — 最新 plan で対象を id から引き直して再精算。
                LOGGER.info(
                    "[day_plan] settlement hit plan generation conflict; retrying "
                    "(attempt=%d/%d): %s", _attempt + 1, _CAS_MAX_RETRIES, exc,
                )
        else:
            raise _PlanGenerationConflict(
                f"settlement kept conflicting with plan rewrites "
                f"(persona={persona_id} date={plan_date_str} slot={slot_id})"
            )
    except Exception:
        # 精算 tx は全ロールバック: slot fired・episode open・台帳 running・予算は
        # 予約額のまま (予約 tx で確定済み)。→ 後続コマは予約額を消費済みとして
        # 見る (A5「精算失敗時も予約額を残す」)。回復 tick が settle-close する
        # (Chunk C)。EventScheduler callback を殺さないため再送出はしない。
        LOGGER.exception(
            "[day_plan] slot.fire settlement tx failed; slot left 'fired', episode "
            "open, ledger 'running', reserved budget retained — recovery will "
            "settle-close (persona=%s date=%s index=%d kind=%s exec=%s)",
            persona_id, plan_date_str, index, kind, exec_id,
        )
        return


def _fire_slot_legacy(
    manager: Any,
    persona_id: str,
    plan_date_str: str,
    index: int,
    slot: Dict[str, Any],
    kind: Any,
    gated: bool,
    handler: SlotHandler,
) -> None:
    """台帳の無い環境向けの発火経路 (三区間化前の従来挙動そのまま)。

    :func:`_fire_slot` が ``manager.execution_ledger`` を持たない旧テストスタブ
    で呼ばれたときの縮退。予約/精算の原子性 (A5/A6) は無いが、台帳を前提に
    しない既存テストを壊さないための後方互換経路 (WARN は persona 一度だけ)。
    """
    # fired を先に永続化することで、ハンドラ実行中のクラッシュ後に watchdog
    # (reschedule_pending_slots) が同じコマを二重発火させない
    # (pending/deferred のみ再 push されるため)。
    updated = _update_slot(
        manager, persona_id, plan_date_str, index,
        expected_id=slot.get("id"), status=STATUS_FIRED,
    )
    if updated is not None:
        # 一時キー (_outing_unresolved / _move_failed 等、"_" 始まり) は永続化
        # されない — ストアからの読み直しで落とさず引き継ぐ (handler が読む)。
        slot = {**updated, **{k: v for k, v in slot.items() if k.startswith("_")}}
    LOGGER.info(
        "[day_plan] slot fired (no-ledger): persona=%s date=%s index=%d kind=%s "
        "ref=%s facility=%s",
        persona_id, plan_date_str, index, kind, slot.get("ref"), slot.get("facility"),
    )

    episode_ref = _open_slot_episode(manager, persona_id, plan_date_str, slot, index)

    ref = slot.get("ref") or REF_NONE
    if ref != REF_NONE and ref.startswith("task:"):
        try:
            from saiverse.desire_engine import touch_desire
            touch_desire(manager, persona_id, ref)
        except Exception:
            LOGGER.warning(
                "[day_plan] touch_desire failed (persona=%s ref=%s); continuing",
                persona_id, ref, exc_info=True,
            )

    try:
        used_rounds = handler(manager, persona_id, plan_date_str, slot, index)
    except Exception:
        LOGGER.exception(
            "[day_plan] slot handler failed (persona=%s date=%s index=%d kind=%s); "
            "slot left as 'fired'",
            persona_id, plan_date_str, index, kind,
        )
        _close_slot_episode(manager, persona_id, episode_ref, None)
        return

    if gated and isinstance(used_rounds, int) and not isinstance(used_rounds, bool) \
            and used_rounds > 0:
        try:
            consume_budget(manager, persona_id, plan_date_str, used_rounds)
        except Exception:
            LOGGER.exception(
                "[day_plan] consume_budget failed (persona=%s date=%s index=%d); "
                "continuing",
                persona_id, plan_date_str, index,
            )
        try:
            consume_life_rounds(
                manager, persona_id, plan_date_str, used_rounds, at_time=slot.get("start"),
            )
        except Exception:
            LOGGER.exception(
                "[day_plan] consume_life_rounds failed (persona=%s date=%s index=%d); "
                "continuing",
                persona_id, plan_date_str, index,
            )
    done_slot = _update_slot(
        manager, persona_id, plan_date_str, index,
        expected_id=slot.get("id"), status=STATUS_DONE,
    )
    _close_slot_episode(manager, persona_id, episode_ref, done_slot)


# ---------------------------------------------------------------------------
# コマ発火の予約 / 精算トランザクション (A5/A6, W2 Chunk B)
#
# slots_json と meta_json は同じ PersonaDayPlan 行。台帳遷移・slot 状態・予算・
# episode を単一 manager.SessionLocal() の 1 commit に束ねることで、
# 「予算記帳の非原子性 (A5)」と「done 保存失敗で episode 永久 open (A6)」を
# 同一患部で解く。予算計算 (κ・ライフ判定) は既存関数を流用し、負 delta (返金)
# を許す点だけ独自 (既存 consume_* は _read_nonneg_int で負を弾くため)。
# ---------------------------------------------------------------------------


def _load_plan_row(db: Session, persona_id: str, plan_date_str: str) -> Any:
    """PersonaDayPlan 行を与えられた Session で読む (無ければ None)。"""
    from database.models import PersonaDayPlan

    return (
        db.query(PersonaDayPlan)
        .filter_by(persona_id=persona_id, plan_date=plan_date_str)
        .first()
    )


def _row_slots(row: Any) -> List[Dict[str, Any]]:
    """行の slots_json を list に読む (不正/空は空リスト)。"""
    try:
        slots = json.loads(row.slots_json) if row.slots_json else []
    except (TypeError, ValueError):
        LOGGER.warning("[day_plan] slots_json is not valid JSON: %r", row.slots_json)
        return []
    return slots if isinstance(slots, list) else []


def _row_meta(row: Any) -> Dict[str, Any]:
    """行の meta_json を dict に読む (不正/空は空 dict)。"""
    try:
        meta = json.loads(row.meta_json) if row.meta_json else {}
    except (TypeError, ValueError):
        LOGGER.warning("[day_plan] meta_json is not valid JSON: %r", row.meta_json)
        return {}
    return meta if isinstance(meta, dict) else {}


def _apply_budget_delta_to_meta(
    meta: Dict[str, Any], slot: Dict[str, Any], delta: int
) -> None:
    """予算消費を ``delta`` だけ調整して ``meta`` (meta_json の dict) を書き換える。

    lives のある日は ``lives[idx].used_rounds`` (生ラウンド。κ は消費計算時に
    :func:`get_budget_state` が掛ける) が正典、無い日は ``META_BUDGET_USED``。
    idx は :func:`get_life_for_time` で流用。**delta は負 = 返金になりうる**ため
    ``max(0, cur + delta)`` でクランプする (既存 consume_* は負を弾くのでここでは
    使えない)。lives のある日は旧 ``META_BUDGET_USED`` を書かない (A5 の二重台帳
    廃止・lives 正典一本化)。
    """
    if not delta:
        return
    raw_lives = meta.get(META_LIVES)
    lives = [life for life in raw_lives if isinstance(life, dict)] \
        if isinstance(raw_lives, list) else []
    if lives:
        idx = get_life_for_time(lives, slot.get("start"))
        if idx is None:
            # 谷にコマは置けない検証を通っていれば起こらないが、保存後にライフだけ
            # 組み替わった場合の防御 (積算先が無いので no-op)。
            LOGGER.warning(
                "[day_plan] budget delta %+d has no owning life for start=%s; skipped",
                delta, slot.get("start"),
            )
            return
        cur = int(lives[idx].get("used_rounds") or 0)
        lives[idx]["used_rounds"] = max(0, cur + delta)
        meta[META_LIVES] = lives
    else:
        cur = _read_nonneg_int(meta.get(META_BUDGET_USED)) or 0
        meta[META_BUDGET_USED] = max(0, cur + delta)


def _find_slot_index_by_id(
    slots: List[Dict[str, Any]], slot_id: Optional[str]
) -> Optional[int]:
    """slots から不変 ID が一致するコマの現 index を返す (無ければ None)。

    配列 index はハンドラ中の時間割組み替え (post_session の
    :func:`replace_remaining_slots`) で移動しうるため、発火時に捕捉した index では
    なくこの逆引きで精算・回復の対象コマを特定する (A6 二重 done の是正)。
    """
    if not slot_id:
        return None
    for i, slot in enumerate(slots):
        if slot.get("id") == slot_id:
            return i
    return None


class _SlotVanished(Exception):
    """コマが claim 後・予約前に時間割から消えた (組み替えで除去) / 発火不能になった。

    不可逆処理 (ハンドラ) を始めず、mark_running より前に中断するためのシグナル。
    副作用ゼロが保証される (invariant 1)。呼び出し元は台帳を **prepared のときだけ**
    failed に落とす (:meth:`~saiverse.execution_ledger.ExecutionLedger.abandon_prepared`)
    — 二重 claim で同じ execution_id を共有した勝者が走行中の場合、その running
    台帳を壊さないため (2026-07-20 Codex レビュー第三陣)。
    """


class _ClaimLost(Exception):
    """予約 tx の席取り (prepared → running の条件付き遷移) に負けた。

    :meth:`~saiverse.execution_ledger.ExecutionLedger.claim_execution` は既存
    prepared 行を再利用するため、ほぼ同時の二重発火は同じ execution_id を両方に
    runnable として返しうる。席が取れなかった側はこのシグナルで予約 tx を全
    ロールバックし、**台帳に一切書かずに**離脱する (勝者が所有している)。
    """


class _PlanGenerationConflict(Exception):
    """tx が読んだ plan が、書き込むまでの間に別の書き手に置き換えられた。

    予約 tx / 精算 tx / 回復 settle の slots+予算書き込みは
    「``UPDATE ... WHERE slots_json = 読んだ payload``」の条件付き更新で行う —
    読んだ世代が既に古ければ 1 行も更新されずこの例外で全ロールバックする
    (古い配列の書き戻しでペルソナの組み替えを消さない — 2026-07-20 Codex
    レビュー第五陣 P1 の tx 側)。呼び出し元は最新 plan で対象を id から引き直して
    再試行する (置換で対象が消えていれば :class:`_SlotVanished` 等の既存安全経路へ
    自然に落ちる)。
    """


def _reserve_slot_tx(
    manager: Any,
    ledger: Any,
    exec_id: str,
    persona_id: str,
    plan_date_str: str,
    index: int,
    slot_id: Optional[str],
    slot: Dict[str, Any],
    gated: bool,
    reserved: int,
) -> Optional[str]:
    """予約 tx: 台帳 running + slot fired + 予算予約 + episode open を単一 commit。

    対象コマは発火時 ``index`` ではなく不変 ``slot_id`` で **同 session 内で** 引く —
    claim 後・予約前に別判断が :func:`replace_remaining_slots` で配列を組み替えても、
    正しいコマを fired にする。対象が消えていれば :class:`_SlotVanished` を投げて
    ハンドラを始めさせない (mark_running より前なので副作用ゼロ)。

    Returns:
        開いた出来事の episode_ref (episodes.open_episode の戻り)。

    Raises:
        _SlotVanished: 対象コマが消えた / 発火不能 (呼び出し元は prepared のときだけ
            failed に落として中断 — abandon_prepared)。
        _ClaimLost: prepared → running の席取りに負けた (同 execution_id を共有する
            並走発火の勝者が既に running / 完了済み)。全ロールバック済み — 呼び出し
            元は台帳に触らず離脱する。
        Exception: その他の失敗は全ロールバック (slot pending・予算不変・episode 無し・
            台帳 prepared のまま) して再送出 — 呼び出し元がハンドラを呼ばずに return する。
    """
    from database.models import PersonaDayPlan
    from saiverse import episodes

    db = manager.SessionLocal()
    try:
        row = _load_plan_row(db, persona_id, plan_date_str)
        if row is None:
            raise RuntimeError(
                f"plan row missing during reservation (persona={persona_id} "
                f"date={plan_date_str})"
            )
        original_payload = row.slots_json
        slots = _row_slots(row)
        # 発火時 index ではなく不変 id で現在位置を引く (組み替え耐性)。
        target = _find_slot_index_by_id(slots, slot_id)
        if target is None:
            raise _SlotVanished(
                f"slot id={slot_id} vanished before reservation "
                f"(persona={persona_id} date={plan_date_str})"
            )
        if slots[target].get("status") not in (STATUS_PENDING, STATUS_DEFERRED):
            # 既に別経路で発火/消化済み — claim ガードの隙を塞ぎ二重発火しない。
            raise _SlotVanished(
                f"slot id={slot_id} not fireable "
                f"(status={slots[target].get('status')!r})"
            )

        # 台帳 prepared → running (同 tx。不変条件 1) — 対象確定後に行う。
        # 条件付き遷移 (早い者勝ち): 二重 claim で同じ exec_id を掴んだ並走発火が
        # いても、席が取れるのは一人だけ。負けたら全ロールバックして離脱する
        # (勝者の tx と衝突しても、二重 fired・予算二重予約・出来事二重 open を
        # 起こさない)。
        if not ledger.try_mark_running(exec_id, session=db):
            raise _ClaimLost(
                f"execution {exec_id} seat already taken by concurrent fire "
                f"(persona={persona_id} date={plan_date_str} slot={slot_id})"
            )

        # slot pending/deferred → fired + 予算予約を、**読んだ世代と同じときだけ**
        # 書く条件付き更新で同梱する (ORM 属性書き込みだと無条件 UPDATE になり、
        # 読みと commit の間に成立した置換を古い配列で消してしまう — 第五陣 P1)。
        slots[target]["status"] = STATUS_FIRED
        world_update: Dict[Any, Any] = {
            PersonaDayPlan.slots_json: json.dumps(slots, ensure_ascii=False),
            PersonaDayPlan.updated_at: clock.now(),
        }
        conditions = [
            PersonaDayPlan.persona_id == persona_id,
            PersonaDayPlan.plan_date == plan_date_str,
            PersonaDayPlan.slots_json == original_payload,
        ]
        # 予算予約 (gated のみ)。meta_json は**書くときだけ** SET に含め、含める
        # ときは読んだ meta も CAS 条件へ加える — slots が無傷でも並走の
        # update_plan_meta (明日メモ等) の commit を古い meta の書き戻しで消さない
        # (第六陣 P1)。書かないとき meta には一切触らない。
        if gated and reserved:
            original_meta = row.meta_json
            meta = _row_meta(row)
            _apply_budget_delta_to_meta(meta, slot, int(reserved))
            world_update[PersonaDayPlan.meta_json] = json.dumps(
                meta, ensure_ascii=False,
            )
            conditions.append(PersonaDayPlan.meta_json == original_meta)
        changed = (
            db.query(PersonaDayPlan)
            .filter(*conditions)
            .update(world_update, synchronize_session=False)
        )
        if not changed:
            raise _PlanGenerationConflict(
                f"plan changed during reservation (persona={persona_id} "
                f"date={plan_date_str} slot={slot_id})"
            )

        # 出来事を開く (同 session。origin_ref は発火時 index — 回復が payload の index
        # から同じ origin_ref を再構成して逆引きするため一貫させる)。
        persona = (getattr(manager, "personas", {}) or {}).get(persona_id)
        building_id = getattr(persona, "current_building_id", None)
        ep_meta: Dict[str, Any] = {"slot_kind": str(slot.get("kind") or "")}
        title = str(slot.get("title") or "").strip()
        if title:
            ep_meta["title"] = title
        ep = episodes.open_episode(
            manager, persona_id,
            _episode_kind_for_slot(slot.get("kind")),
            building_id=building_id,
            participants=[persona_id],
            origin_ref=_slot_origin_ref(persona_id, plan_date_str, index),
            meta=ep_meta,
            session=db,
        )
        episode_ref = ep.get("episode_ref")

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    # commit 成功後: open キャッシュを無効化 (未コミット状態を映さない契約)。
    episodes.invalidate_open_cache(manager, persona_id)
    return episode_ref


def _settle_slot_tx(
    manager: Any,
    ledger: Any,
    exec_id: str,
    persona_id: str,
    plan_date_str: str,
    index: int,
    slot_id: Optional[str],
    slot: Dict[str, Any],
    episode_ref: Optional[str],
    gated: bool,
    reserved: int,
    used_rounds: Any,
) -> None:
    """精算 tx: 予算調整 + slot done + episode close + 台帳 applied を単一 commit。

    対象コマは発火時 ``index`` ではなく不変 ``slot_id`` で引く — ハンドラ中の時間割
    組み替え (post_session の replace_remaining_slots) で index が移動しても正しい
    コマを done にするため (A6 二重 done の是正)。

    ``used_rounds`` が **非負** int でない (None・負数・非 int) なら delta=0 で予約額を
    そのまま消費として残す (負数は旧 consume 系と同じく信頼せず弾く安全側)。有効な
    実測なら delta は負 = 返金になりうる。例外時は全ロールバック (slot fired・episode
    open・台帳 running・予算は予約額のまま) して再送出する。
    """
    from saiverse import episodes

    valid_used = (
        isinstance(used_rounds, int)
        and not isinstance(used_rounds, bool)
        and used_rounds >= 0
    )
    if used_rounds is not None and not valid_used:
        # 負数・非 int の used_rounds はハンドラのバグ。旧 consume 系 (_read_nonneg_int)
        # と同じく弾き、予約額をそのまま消費として残す (過剰返金を防ぐ安全側、A5)。
        LOGGER.warning(
            "[day_plan] slot.fire settlement got invalid used_rounds=%r "
            "(expected non-negative int); keeping reservation, no refund "
            "(persona=%s exec=%s)", used_rounds, persona_id, exec_id,
        )
    used_for_result: Optional[int] = int(used_rounds) if valid_used else None
    delta = (int(used_rounds) - int(reserved)) if (gated and valid_used) else 0

    from database.models import PersonaDayPlan

    db = manager.SessionLocal()
    try:
        row = _load_plan_row(db, persona_id, plan_date_str)
        if row is None:
            raise RuntimeError(
                f"plan row missing during settlement (persona={persona_id} "
                f"date={plan_date_str})"
            )

        # slot fired → done。配列 index はハンドラ中の時間割組み替えで移動しうるので
        # 発火時 index ではなく不変 slot_id で対象コマを引く (record_level はハンドラが
        # 別 tx で永続化済み)。書き込みは予算精算と合わせ、**読んだ世代と同じとき
        # だけ**の条件付き更新で行う (古い配列の書き戻しで置換を消さない — 第五陣 P1)。
        original_payload = row.slots_json
        slots = _row_slots(row)
        target = _find_slot_index_by_id(slots, slot_id)
        done_slot: Optional[Dict[str, Any]] = None
        world_update: Dict[Any, Any] = {}
        if target is not None:
            slots[target]["status"] = STATUS_DONE
            done_slot = slots[target]
            world_update[PersonaDayPlan.slots_json] = json.dumps(
                slots, ensure_ascii=False,
            )
        else:
            LOGGER.warning(
                "[day_plan] slot id=%s not found during settlement (persona=%s "
                "date=%s len=%d); done not written (episode/台帳 は精算する)",
                slot_id, persona_id, plan_date_str, len(slots),
            )

        # 予算の精算 (予約 → 実測。通常は返金の負 delta)。meta_json を書くときは
        # 読んだ meta も CAS 条件へ加える — 並走の update_plan_meta (明日メモ等) の
        # commit を古い meta の書き戻しで消さない (第六陣 P1)。
        conditions = [
            PersonaDayPlan.persona_id == persona_id,
            PersonaDayPlan.plan_date == plan_date_str,
            PersonaDayPlan.slots_json == original_payload,
        ]
        if gated and delta:
            original_meta = row.meta_json
            meta = _row_meta(row)
            _apply_budget_delta_to_meta(meta, slot, delta)
            world_update[PersonaDayPlan.meta_json] = json.dumps(
                meta, ensure_ascii=False,
            )
            conditions.append(PersonaDayPlan.meta_json == original_meta)

        if world_update:
            world_update[PersonaDayPlan.updated_at] = clock.now()
            changed = (
                db.query(PersonaDayPlan)
                .filter(*conditions)
                .update(world_update, synchronize_session=False)
            )
            if not changed:
                raise _PlanGenerationConflict(
                    f"plan changed during settlement (persona={persona_id} "
                    f"date={plan_date_str} slot={slot_id})"
                )

        # 出来事を閉じる (同 session。record_level を meta へ透過)
        if episode_ref:
            close_meta: Optional[Dict[str, Any]] = None
            record_level = str((done_slot or {}).get("record_level") or "")
            if record_level:
                close_meta = {"record_level": record_level}
            episodes.close_episode(
                manager, persona_id, episode_ref, meta=close_meta, session=db,
            )

        # 台帳 running → applied (outbox 無し。memory.db を跨ぐ書き込みは精算段階に
        # 無い — ハンドラが memory.db に書くのは running 区間)
        ledger.mark_applied(exec_id, session=db, result={
            "kind": "slot.fire",
            "reserved": int(reserved),
            "used_rounds": used_for_result,
            "gated": bool(gated),
        })

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    # commit 成功後: open キャッシュ無効化 → 台帳 completed (outbox 無しなので
    # applied → completed が通る。別 commit でよい)。
    if episode_ref:
        episodes.invalidate_open_cache(manager, persona_id)
    ledger.mark_completed(exec_id)


def settle_stale_slot(
    manager: Any,
    ledger: Any,
    execution_id: str,
    persona_id: str,
    plan_date_str: str,
    index: int,
    slot_id: Optional[str],
    episode_ref: Optional[str],
) -> None:
    """精算 tx が転けて running のまま残ったコマ発火を保守的に settle-close する (A6 回復)。

    回復 tick (:func:`saiverse.execution_ledger_wiring._collect_stale_slot_executions`)
    から呼ばれる。精算 tx (:func:`_settle_slot_tx`) と同形の単一 commit だが、
    **LLM を再実行しない掃除** なので:

    - **予算は調整しない** — 予約額 (= 実効予算 = 使える上限) をそのまま消費として
      残す (実測が不明な crash 回復では返金しない保守側。A5 の設計思想)。
    - **episode_ref は逆引き結果** (origin_ref からの :func:`get_open_episode_by_origin`)
      を使う。無ければ close を省く。
    - slot は ``fired → done`` (既に done 等なら触らない)。
    - 台帳は ``running → applied`` (result に ``recovered: True``)、commit 後に
      ``completed``。

    冪等: 実行が running でなければ (= 既に別 tick が settle 済み) 何もしない。
    ``mark_applied`` は running→applied の合法遷移なので、二重 tick の 2 本目が
    IllegalTransitionError で落ちる前に status を確認して skip する。
    """
    from saiverse import episodes
    from saiverse.execution_ledger import STATUS_RUNNING as _LEDGER_RUNNING

    # 冪等ガード: running でなければ触らない (二重 tick の 2 本目は既に applied)。
    try:
        current_status = ledger.get_execution(execution_id).get("status")
    except Exception:
        LOGGER.warning(
            "[day_plan] settle_stale_slot: could not read execution %s; skipping",
            execution_id, exc_info=True,
        )
        return
    if current_status != _LEDGER_RUNNING:
        LOGGER.debug(
            "[day_plan] settle_stale_slot: execution %s is %s (not running); "
            "skipping (already settled)", execution_id, current_status,
        )
        return

    db = manager.SessionLocal()
    closed_episode = False
    try:
        row = _load_plan_row(db, persona_id, plan_date_str)
        # slot fired → done (既に done/skipped 等なら触らない — 帳簿を上書きしない)。
        # 対象は発火時 index ではなく不変 slot_id で引く (ハンドラ中の時間割組み替えで
        # index が移動していても正しいコマを締める)。
        if row is not None:
            from database.models import PersonaDayPlan

            original_payload = row.slots_json
            slots = _row_slots(row)
            target = _find_slot_index_by_id(slots, slot_id)
            if target is not None and slots[target].get("status") == STATUS_FIRED:
                slots[target]["status"] = STATUS_DONE
                changed = (
                    db.query(PersonaDayPlan)
                    .filter(
                        PersonaDayPlan.persona_id == persona_id,
                        PersonaDayPlan.plan_date == plan_date_str,
                        PersonaDayPlan.slots_json == original_payload,
                    )
                    .update(
                        {
                            PersonaDayPlan.slots_json: json.dumps(
                                slots, ensure_ascii=False,
                            ),
                            PersonaDayPlan.updated_at: clock.now(),
                        },
                        synchronize_session=False,
                    )
                )
                if not changed:
                    # 読んだ世代が古い — 古い配列で置換を消さない。全ロールバック
                    # して raise: 台帳は running のままなので次の回復 tick が最新
                    # plan で settle し直す (第五陣 P1 の tx 側)。
                    raise _PlanGenerationConflict(
                        f"plan changed during settle-close (persona={persona_id} "
                        f"date={plan_date_str} slot={slot_id})"
                    )
        else:
            LOGGER.warning(
                "[day_plan] settle_stale_slot: plan row missing "
                "(persona=%s date=%s index=%d id=%s); settling ledger/episode only",
                persona_id, plan_date_str, index, slot_id,
            )

        # 出来事を閉じる (逆引き結果、同 session)。既に閉じていれば close は no-op。
        if episode_ref:
            try:
                episodes.close_episode(
                    manager, persona_id, episode_ref, session=db,
                )
                closed_episode = True
            except episodes.EpisodeNotFoundError:
                LOGGER.warning(
                    "[day_plan] settle_stale_slot: episode %s not found "
                    "(persona=%s); closing ledger without it",
                    episode_ref, persona_id,
                )

        # 台帳 running → applied (outbox 無し。予算は予約額のまま = 保守精算)
        ledger.mark_applied(execution_id, session=db, result={
            "kind": "slot.fire",
            "recovered": True,
            "index": index,
            "note": "settle-close after settlement tx failure (budget kept at reserved)",
        })

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    # commit 成功後: open キャッシュ無効化 → 台帳 completed。
    if closed_episode:
        episodes.invalidate_open_cache(manager, persona_id)
    ledger.mark_completed(execution_id)
    LOGGER.info(
        "[day_plan] settle_stale_slot: recovered slot.fire settled "
        "(persona=%s date=%s index=%d exec=%s episode=%s)",
        persona_id, plan_date_str, index, execution_id, episode_ref,
    )


# ---------------------------------------------------------------------------
# ref 解決 (task:N / desire:N → タイトル等)
# ---------------------------------------------------------------------------


def _resolve_ref(manager: Any, persona_id: str, ref: str) -> Optional[str]:
    """ref を人間可読なタイトル/目標へ解決する。none / 解決不能は None。

    desire は親なし + stage='candidate' の目的ノード (P3c-0 desire 正規化) で
    あり、task:N と同じ short_id 参照空間を共有する (persona_task_manager.py)。
    したがって "desire:N" も同じ短縮参照 N で解決する。
    """
    if not ref or ref == REF_NONE:
        return None
    m = _REF_RE.match(ref)
    if m is None:
        LOGGER.warning("[day_plan] unresolvable ref format: %r", ref)
        return None

    from saiverse.persona_task_manager import (
        STAGE_CANDIDATE,
        PersonaTaskManager,
        TaskNotFoundError,
    )

    task_manager = PersonaTaskManager(manager.SessionLocal)
    try:
        task_id = task_manager.resolve_task_ref(persona_id, f"task:{m.group(2)}")
        task = task_manager.get_task(task_id, persona_id=persona_id)
    except TaskNotFoundError:
        LOGGER.warning(
            "[day_plan] ref %r not found for persona=%s; instruction will use note only",
            ref, persona_id,
        )
        return None

    if m.group(1) == "desire" and task.get("stage") != STAGE_CANDIDATE:
        LOGGER.warning(
            "[day_plan] ref %r resolved to a non-desire task (stage=%r); using it anyway",
            ref, task.get("stage"),
        )

    title = task.get("title") or "(無題)"
    goal = (task.get("goal") or "").strip()
    return f"{title}（目標: {goal}）" if goal else title


# ---------------------------------------------------------------------------
# 大枝 (track:N) コマの指示書 (P5: コマ参照の任意階層化、life_concept_map.md §3.1)
# ---------------------------------------------------------------------------


def _read_track_desk_memo(track: Any) -> Optional[Dict[str, Any]]:
    """Track metadata から作業メモ (desk_memo) を読む。無ければ None。"""
    raw = getattr(track, "track_metadata", None)
    if not raw:
        return None
    try:
        metadata = json.loads(raw)
    except (TypeError, ValueError):
        return None
    memo = metadata.get("desk_memo") if isinstance(metadata, dict) else None
    return memo if isinstance(memo, dict) else None


def _build_track_instruction(
    manager: Any, persona_id: str, slot: Dict[str, Any], ref: str
) -> tuple[Optional[str], Optional[str]]:
    """track:N コマの指示書を Track の文脈 (title・机メモ・配下の生存タスク) から組む。

    大枝コマの実行意味 (§3.1「中身はその場の判断」): 発火時に Track の状況を
    見てセッション 1 本を回す。**中身が空の Track** (配下に生存タスク・机メモ・
    コマの覚え書き (note) のいずれも無い) は presence 相当に縮退する — 充填生成で
    セッションを回すと v1 の空回りに逆戻りするため (§6 無意味の予算の注意)。

    ただし note があれば縮退しない: ペルソナが編成時に書いた覚え書き
    (「構想を練る」等) はこのコマの意図そのものなので、生存タスクが無くても
    それを目標にセッションを回す — day_open の約束「関心を指せば開始時に状況を
    見て決める」との整合 (issue track_slot_empty_degradation)。

    Returns:
        ``(instruction, track_id)``。縮退時は ``(None, track_id)``。
        Track が解決できない場合は ``(None, None)`` (呼び出し側で WARN 済み想定)。
    """
    track_manager = getattr(manager, "track_manager", None)
    if track_manager is None:
        LOGGER.warning("[day_plan] track ref %r but manager has no track_manager", ref)
        return None, None
    try:
        track_id = track_manager.resolve_track_ref(persona_id, ref)
        track = track_manager.get(track_id)
    except Exception:
        LOGGER.warning(
            "[day_plan] track ref %r could not be resolved (persona=%s)",
            ref, persona_id, exc_info=True,
        )
        return None, None

    title = getattr(track, "title", None) or "(無題)"
    intent = (getattr(track, "intent", None) or "").strip()
    memo = _read_track_desk_memo(track)
    note = (slot.get("note") or "").strip()

    from saiverse.persona_task_manager import PersonaTaskManager

    try:
        ptm = PersonaTaskManager(manager.SessionLocal)
        live_tasks = ptm.list_tasks(
            persona_id, track_id=track_id,
            statuses=("pending", "active", "paused"), include_steps=False,
        )
    except Exception:
        LOGGER.warning(
            "[day_plan] failed to list tasks under track %s (persona=%s)",
            track_id, persona_id, exc_info=True,
        )
        live_tasks = []

    if not live_tasks and not memo and not note:
        # 中身が空 (生存タスク・机メモ・コマの覚え書きのいずれも無い) →
        # presence 縮退 (呼び出し側が record_level を刻む)
        return None, track_id

    parts = [f"目的: 関心「{title}」を前に進める。"]
    if intent:
        parts.append(f"この関心の意図: {intent}")
    if note:
        parts.append(f"このコマの覚え書き: {note}")
    if memo:
        memo_label = "詰まり" if memo.get("status") == "blocked" else "続き"
        parts.append(
            f"前回の作業メモ [{memo_label}]: {str(memo.get('text') or '').strip() or '(記載なし)'}"
        )
    if live_tasks:
        lines = ["この関心の下にあるタスク:"]
        for t in live_tasks:
            t_ref = t.get("task_ref") or "task:?"
            goal = (t.get("goal") or "").strip()
            lines.append(
                f"- {t_ref} [{t.get('status')}] {t.get('title') or '(無題)'}"
                + (f"（目標: {goal}）" if goal else "")
            )
        parts.append("\n".join(lines))
        opening = "この中から今のコマで実際に進められることを選んで取り組むこと。"
    else:
        # 生存タスク一覧が無い (note / 机メモだけで回る大枝) — 指示語の指す先が
        # 無いので、意図と覚え書きに沿って取り組ませる。
        opening = (
            "この関心の意図とこのコマの覚え書きに沿って、今のコマで実際に"
            "進められることに取り組むこと。"
        )
    parts.append(
        opening
        + "スペルで実際に実行・確認できたことだけを行い、成果は document_create 等の"
        "スペルで実際に残すこと。"
        "完成条件: 実際にやったことが読み返せる形で残っていること。"
        "実際にやったこと以外を「やった」と書かないこと。"
    )
    return "\n".join(parts), track_id


# ---------------------------------------------------------------------------
# 組み込みハンドラ
# ---------------------------------------------------------------------------

# 作業セッション系の指示書テンプレートはコマ種別カタログ
# (slot_kind_catalog、builtin_data/slot_kinds/*.json) の instruction_template
# から組む — 実体はモジュール末尾の :func:`_rebuild_kind_vocabulary` が
# :data:`_WORKER_INSTRUCTION_TEMPLATES` へ構築する。builtin 4 種 (調べる/
# 絵を描く/日記を書く/随筆を書く) は「実際に起きたこと以外を書かせない」
# 接地文言 (接地原則 §3-1) を含む。{note} / {target} プレースホルダ契約は
# 旧・六型テンプレートから継承。

_NO_REF_TARGET = "(参照タスクなし。目的の記述に従うこと)"


def run_worker_slot_session(
    manager: Any, persona_id: str, plan_date_str: str, slot: Dict[str, Any], index: int
) -> Any:
    """作業セッション系コマの作業セッション 1 本を運転し、結果をそのまま返す。

    種別別の決定論テンプレート (カタログの instruction_template) で指示書を組み
    ``run_work_session`` を呼ぶ実体。組み込み
    ハンドラ (:func:`_handle_worker_slot`) と、セッション終了判断へ接続する
    上位層 (``saiverse.day_scenario.ScenarioPlayer`` のラップハンドラ) が共有する
    — 後者は post_session 判断の入力として ``WorkSessionResult`` 全体が要る。

    ref の階層でセッションの形が変わる (P5, life_concept_map.md §3.1):

    - task:N / desire:N / none — 型別テンプレートの指示書 (従来どおり。
      desire コマ = お試し採用: 発火時にそのまま欲求を生きる。採用への昇格は
      しない — それは判断点の仕事)
    - track:N (大枝) — Track の title・机メモ・コマの note・配下の生存タスクから
      指示書を組む (:func:`_build_track_instruction`)。**生存タスク・机メモ・note が
      すべて空の Track だけ presence 相当に縮退** し、セッションを回さず ``None``
      を返す (呼び出し側は判断点を撃たず予算も消費しない)。note があれば縮退せず、
      それを目標にセッションを回す (issue track_slot_empty_degradation)

    Returns:
        ``sea.work_session.WorkSessionResult`` (raise しない契約)。
        presence 縮退時のみ ``None``。
    """
    kind = slot["kind"]
    ref = slot.get("ref") or REF_NONE
    track_id: Optional[str] = None
    if _TRACK_REF_RE.match(ref):
        instruction, track_id = _build_track_instruction(manager, persona_id, slot, ref)
        if instruction is None:
            # 中身が空 (または解決不能) → presence 縮退。詳細な実行記録が
            # 無いことを slot に永続化する (暮らし/休む スタブと同じ正直さ)。
            LOGGER.info(
                "[day_plan] track slot degraded to presence (empty track): "
                "persona=%s date=%s index=%d ref=%s",
                persona_id, plan_date_str, index, ref,
            )
            _update_slot(
                manager, persona_id, plan_date_str, index,
                expected_id=slot.get("id"),
                record_level=RECORD_LEVEL_PRESENCE_ONLY,
            )
            return None
    else:
        template = _WORKER_INSTRUCTION_TEMPLATES[kind]
        target = _resolve_ref(manager, persona_id, ref) or _NO_REF_TARGET
        note = (slot.get("note") or "").strip() or "(記載なし)"
        instruction = template.format(note=note, target=target)

    budget = int(slot.get("budget_rounds") or 0)
    if budget < 1:
        # 予算ゲート (_apply_budget_gate) が実効値を永続化済みのはずだが、
        # 台帳の無い日 / 直接呼び出しのフォールバックとして既定値を保つ。
        budget = _effective_budget_rounds(slot)
        LOGGER.info(
            "[day_plan] slot budget_rounds < 1; using default %d "
            "(persona=%s date=%s index=%d)",
            budget, persona_id, plan_date_str, index,
        )

    from sea.work_session import run_work_session
    from saiverse.slot_close import make_close_hook

    # track:N コマではセッションの対象タスクは無い (Track 単位の取り組み)。
    # task_ref を track 参照で埋めると post_session の task_verdict が偽対象を
    # 裁定してしまうため、track_id 側に流す。
    #
    # close_hook = コマ締めの一手 (T4: 帰属判定 + 経験値ノート、同一コール)。
    # v1 で締めコールを持つのは作業セッション系コマ (=この関数) のみ — 軽い
    # 一手コマ (出かける/自室で過ごす) は Pulse 記録が SAIMemory に残り、
    # あらすじ→関与タグは代謝側 (B2) の担当なので、コマごとの LLM コスト
    # 倍化を避けて締めコールを足さない (saiverse/slot_close.py 冒頭)。
    is_track_ref = track_id is not None
    close_hook = make_close_hook(manager, persona_id, plan_date_str, slot, index)
    result = run_work_session(
        persona_id,
        instruction,
        budget,
        task_ref=ref if (ref != REF_NONE and not is_track_ref) else None,
        metadata={"day_plan": {"plan_date": plan_date_str, "slot_index": index, "kind": kind}},
        manager=manager,
        track_id=track_id,
        title=str(slot.get("title") or "").strip() or None,
        close_hook=close_hook,
    )
    # 締めの結果のプロセス内手渡し (Codex 四巡目 #2): slot への永続化 (第二
    # 経路) が CAS 競合等で欠けても、帰属抑止の判定はこの値が担う。
    try:
        result.close_outcome_inproc = getattr(close_hook, "last_outcome", None)
    except Exception:
        LOGGER.debug("[day_plan] failed to attach in-process close outcome",
                     exc_info=True)
    LOGGER.info(
        "[day_plan] work session for slot finished: persona=%s date=%s index=%d kind=%s "
        "ended_reason=%s rounds=%d artifacts=%d",
        persona_id, plan_date_str, index, kind,
        result.ended_reason, result.rounds_used, len(result.artifacts),
    )
    return result


def worker_session_rounds_used(result: Any) -> int:
    """WorkSessionResult から予算台帳へ積算する実測ラウンド数を安全に読む。"""
    rounds_used = getattr(result, "rounds_used", 0) or 0
    if isinstance(rounds_used, bool) or not isinstance(rounds_used, int):
        return 0
    return int(rounds_used)


#: :func:`_reload_slot_field` の「読み出し自体が失敗した」印。「値が未設定
#: (None)」と区別する — close_outcome の読者は失敗を未設定と混同すると、
#: 帰属済みセッションへ post_session の代替帰属を重ねてしまう (Codex 三巡目)。
_RELOAD_FAILED = object()


def _reload_slot_field(
    manager: Any, persona_id: str, plan_date_str: str, slot_id: Any, field: str
) -> Any:
    """保存済み plan から id 一致のコマの 1 フィールドを読み直す。

    close_hook がセッション内で永続化した close_outcome を、handler 側の
    (発火時点の) stale な slot dict を経由せず読むための小道具。

    Returns:
        フィールド値 (未設定・コマ不在は None)。**読み出し自体の失敗は**
        :data:`_RELOAD_FAILED` — 「無い」と「読めなかった」を混同しない。
    """
    if not slot_id:
        return None
    try:
        slots = load_day_plan(manager, persona_id, plan_date_str) or []
        for s in slots:
            if s.get("id") == slot_id:
                return s.get(field)
        return None
    except Exception:
        LOGGER.warning(
            "[day_plan] failed to reload slot field %r (persona=%s date=%s id=%s)",
            field, persona_id, plan_date_str, slot_id, exc_info=True,
        )
        return _RELOAD_FAILED


def _handle_worker_slot(
    manager: Any, persona_id: str, plan_date_str: str, slot: Dict[str, Any], index: int
) -> Optional[int]:
    """作業セッション系コマの組み込みハンドラ (本番の恒久配線)。

    セッション運転の後に **セッション終了判断 (post_session)** を撃つ
    (v2 §4.2 の背骨。かつては ScenarioPlayer のラップハンドラだけが担っていた
    接続の恒久化)。判断は ``saiverse.autonomy_wiring.fire_judgment_point`` の
    本番ゲート (Active のみ / Playbook 欠如は WARNING スキップ) を通る。
    シム (ScenarioPlayer) は実行中このハンドラ自体を自前のラップに差し替える
    ため二重発火しない (day_scenario.py の register_slot_handler 上書き)。

    NOTE: post_session 判断はコマの status が done になる前 (fired のまま) に
    走る — 判断が見る「残りの時間割」に当該コマは含まれない。予算台帳への
    積算は判断の後、戻り値経由で行われる (ScenarioPlayer のラップと同順序)。

    Returns:
        実際に消費したラウンド数 (``_fire_slot`` が予算台帳へ積算する)。
    """
    result = run_worker_slot_session(manager, persona_id, plan_date_str, slot, index)
    if result is None:
        # 中身が空の track コマの presence 縮退 (P5)。セッションが走っていない
        # ので判断点も撃たない (偽前提の状況テキストは作話を誘発する)。
        return 0

    # --- 締めの結果 (close_outcome) の回収 (Codex 一巡目 #3/#5) ---
    # エラー終了は close_hook 自体が呼ばれない — 「締めが走らなかった」ことも
    # 状態として残す (欠落の沈黙化を防ぐ)。帰属が確定済みのセッションでは
    # post_session の層2 棚入れ欄を出させない (同一セッションの二重帰属宣言は
    # revisit_count の偽増加 = recall 順位の汚染)。
    from saiverse.slot_close import (
        ATTRIBUTION_SETTLED_OUTCOMES,
        CLOSE_OUTCOME_NOT_RUN_SESSION_ERROR,
    )

    # 第一経路: run_worker_slot_session が result に載せた in-process の締め結果
    # (永続化の成否に依らない)。無いとき (旧経路・シム等) だけ slot から読み戻す。
    close_outcome = getattr(result, "close_outcome_inproc", None)
    if close_outcome is None:
        close_outcome = _reload_slot_field(
            manager, persona_id, plan_date_str, slot.get("id"), "close_outcome",
        )
    if close_outcome is _RELOAD_FAILED:
        # 読めなかった = 帰属が済んでいるか分からない。二重帰属 (revisit の
        # 偽増加 = 恒久的な汚染) より帰属の欠落 (状態から追える) の方が軽い —
        # 抑止側 (済み扱い) に倒す。
        LOGGER.warning(
            "[day_plan] close_outcome unreadable; suppressing post_session "
            "shelving to avoid double attribution (persona=%s date=%s index=%d)",
            persona_id, plan_date_str, index,
        )
        attribution_done = True
    else:
        if close_outcome is None and getattr(result, "error", None):
            _update_slot(
                manager, persona_id, plan_date_str, index,
                expected_id=slot.get("id"),
                close_outcome=CLOSE_OUTCOME_NOT_RUN_SESSION_ERROR,
            )
            close_outcome = CLOSE_OUTCOME_NOT_RUN_SESSION_ERROR
        attribution_done = close_outcome in ATTRIBUTION_SETTLED_OUTCOMES

    try:
        from saiverse.autonomy_wiring import fire_judgment_point
        from saiverse.judgment_points import KIND_POST_SESSION

        ref = str(slot.get("ref") or REF_NONE)
        context: Dict[str, Any] = {
            "session_result": result,
            "budget_rounds": int(slot.get("budget_rounds") or 0) or None,
            "episode_attribution_done": attribution_done,
        }
        # track:N コマの対象は Track (WorkSessionResult.track_id 経由で判断点へ
        # 届く)。task_ref に track 参照を入れると task_verdict が壊れる。
        if ref != REF_NONE and not _TRACK_REF_RE.match(ref):
            context["task_ref"] = ref
        fire_judgment_point(manager, persona_id, KIND_POST_SESSION, context)
    except Exception:
        LOGGER.exception(
            "[day_plan] post_session judgment failed (persona=%s date=%s index=%d); "
            "slot bookkeeping continues",
            persona_id, plan_date_str, index,
        )
    return worker_session_rounds_used(result)


def _record_presence_only(
    manager: Any, persona_id: str, plan_date_str: str, slot: Dict[str, Any], index: int
) -> None:
    """「詳細な実行記録が無い」ことを slot に永続化する (fail-open の正直表示)。

    コマ開始の Pulse が起動できなかったコマ (T3) と、中身が空の track コマの
    presence 縮退が使う。表示側 (一日新聞 / 就寝判断の状況テキスト) が
    「実行済み」と偽らず「時間を過ごした（詳細な記録なし）」と提示するための
    刻印 (:data:`RECORD_LEVEL_PRESENCE_ONLY`、intent §9-3)。
    """
    _update_slot(
        manager, persona_id, plan_date_str, index,
        expected_id=slot.get("id"),
        record_level=RECORD_LEVEL_PRESENCE_ONLY,
    )


# ---------------------------------------------------------------------------
# 出かける / 自室で過ごす / 自由時間 の実行本体 (T3):
# presence (実移動) + 一回の軽い Pulse
#
# Pulse は既存の schedule 型経路 (PulseDispatcher.dispatch_schedule_fire →
# PulseController → SEARuntime.run_meta_user → 既定 Playbook
# track_user_conversation = LLM ノード 1 個) をそのまま使う — エリスが
# 「朝が来ました」だけのスケジュール Pulse でタイムラインを見る習慣を自作した
# 実証 (intent timetable_redesign §4-6) と同じ経路・同じ粒度で、新しい Pulse
# 機構は発明しない。状況テキストは場所と状況の提示のみで、行動・発話の義務を
# 課さない (「必ず何か書け」と迫る文面は充填独白 — v2 §2.1「LLM は構造的に
# 沈黙できない」— を呼び戻すため禁止)。反応の材料は run_meta_user の知覚検知
# (フィード新着等のバッファ) と visual context (施設の STATE_JSON) が Pulse の
# 中で自然に供給する。
#
# Pulse が起動できなかった場合 (ペルソナ未ロード / dispatcher 無し / 関所閉鎖
# 等) は従来どおり record_level='presence_only' を刻む (fail-open の正直記録)。
# Pulse が走った場合は presence_only を付けない — 実際の思考記録が SAIMemory に
# 残るため、「実行済み」表示が正直になる。
# ---------------------------------------------------------------------------


def _building_display_name(manager: Any, building_id: Optional[str]) -> str:
    """Building の表示名 (見つからなければ id をそのまま返す)。"""
    if not building_id:
        return "どこか"
    for b in getattr(manager, "buildings", None) or []:
        if getattr(b, "building_id", None) == building_id:
            return getattr(b, "name", "") or building_id
    return building_id


def _resolve_outing_destination(
    manager: Any, persona_id: str, plan_date_str: str, index: int, slot: Dict[str, Any]
) -> Dict[str, Any]:
    """「出かける」コマの行き先を移動前に確定し、確定後の slot を返す (T3)。

    - facility が実在施設で確定していればそのまま (テンプレ / 朝の埋めの決定を
      尊重する)。
    - own_room (穴の既定埋め) のままなら、ペルソナが行ける公共施設
    (:func:`saiverse.facility_map.candidate_buildings` — head の「行ける場所」と
      同じ集合) から**決定論の乱数**で選ぶ。LLM に選ばせない (行き先の中身は
      着いた場所が与える — intent §5.3「場所を確定、種別・方針は未定」の鏡像)。
      乱数の種は (persona, 日付, コマ id) — 日ごとに変わり、同じコマの繰り下げ
      再発火では同じ行き先に落ちる (冪等)。自室は「出かける」の意味論から除外
      する (明示的に own_room が書かれていても穴と同じ扱いで外へ出す)。
    - 候補が無い環境 (施設ゼロの City 等) は WARN し、一時キー
      ``_outing_unresolved`` を立てて返す。own_room は下流
      (:func:`_move_to_facility`) では「自室へ移動」という有効な値なので、
      失敗をそのまま (facility=own_room) で返すと自室へ逆移動してしまう —
      未解決は帯域外のフラグで運び、移動をスキップして handler が正直に
      記録する (このキーは in-memory 限り、永続化しない)。

    選んだ行き先は slot に永続化する — 帳簿 (一日新聞 / 就寝ふりかえり) が
    「実際にどこへ行ったか」を読めるようにするため。
    """
    definition = slot_kind_catalog.get_kind_by_name(str(slot.get("kind") or ""))
    if not definition or definition.get("execution_type") != slot_kind_catalog.EXECUTION_OUTING:
        return slot
    facility = str(slot.get("facility") or "").strip()
    if facility and facility != FACILITY_OWN_ROOM:
        return slot

    persona = (getattr(manager, "personas", {}) or {}).get(persona_id)
    private_room = getattr(persona, "private_room_id", None) if persona is not None else None
    current = getattr(persona, "current_building_id", None) if persona is not None else None
    from saiverse.facility_map import candidate_buildings

    # 自室に加えて現在地も除外する — 立っている場所へは「出かけ」られない
    # (現在地を選ぶと移動ゼロのまま「出かけて来ました」の捏造記録になる)。
    # 繰り下げ再発火時に現在地が変わっていれば候補集合も変わりうる (決定論の
    # 種は同じでも choice の母集団が違う) — 行き先の同一性より記録の正直さを
    # 優先する。
    candidates: List[str] = []
    for b in candidate_buildings(manager):
        bid = getattr(b, "building_id", None)
        if bid and bid != private_room and bid != current:
            candidates.append(bid)
    if not candidates:
        LOGGER.warning(
            "[day_plan] outing slot has no destination candidates; staying in "
            "place (persona=%s date=%s index=%d)",
            persona_id, plan_date_str, index,
        )
        return {**slot, "_outing_unresolved": True}

    rng = random.Random(f"{persona_id}:{plan_date_str}:{slot.get('id') or index}")
    target = rng.choice(candidates)
    LOGGER.info(
        "[day_plan] outing destination resolved: persona=%s date=%s index=%d "
        "facility=%r -> %s (deterministic pick from %d candidates)",
        persona_id, plan_date_str, index, facility, target, len(candidates),
    )
    updated = _update_slot(
        manager, persona_id, plan_date_str, index,
        expected_id=slot.get("id"), facility=target,
    )
    return updated if updated is not None else {**slot, "facility": target}


def _run_slot_life_session(
    manager: Any,
    persona_id: str,
    plan_date_str: str,
    slot: Dict[str, Any],
    index: int,
    situation_text: str,
) -> None:
    """暮らしコマ (出かける/自室で過ごす) の一手を暮らしプロファイルで運転する。

    器は作業コマと同じ :func:`sea.work_session.run_work_session`
    (autonomous_pulse_vehicle.md §A)。暮らしプロファイル = ラウンド予算 1・
    close_hook なし (帰属/経験値ノートなし)・出来事を開かない・許可形の状況
    提示。WORKER aspect で走るため出力は独白 (発話にならない) で、Track 操作
    スペルは既存のモードゲートが遮断する。

    帳簿 (record_level / ライフ予算) の正直さは旧実装 (会話の器で走る一回きりの Pulse) と同じ:

    - 走った (ended_reason が error 以外): record_level は付けない (思考記録が
      SAIMemory に残る) + 活動 1 回をライフ予算へ積算 (:func:`consume_life_pulse`。
      器の統合でモデルは軽量になったが「その日の自発活動の回数」という利用者
      向け意味を優先して 1 コマ 1 消費を維持 — intent §A)
    - 走らなかった: presence_only を刻む (fail-open の正直記録)
    """
    from sea.work_session import run_work_session

    result = run_work_session(
        persona_id,
        situation_text,
        1,
        metadata={
            "day_plan": {
                "plan_date": plan_date_str,
                "slot_index": index,
                "kind": str(slot.get("kind") or ""),
            },
        },
        manager=manager,
        title=str(slot.get("title") or "").strip() or None,
        close_hook=None,
        profile="life",
    )
    if result is None or getattr(result, "ended_reason", "error") == "error":
        LOGGER.warning(
            "[day_plan] life session did not run (persona=%s date=%s index=%d "
            "kind=%s error=%s); falling back to presence-only record",
            persona_id, plan_date_str, index, slot.get("kind"),
            getattr(result, "error", None),
        )
        _record_presence_only(manager, persona_id, plan_date_str, slot, index)
        return
    # 暮らしコマは締め (close_hook) を持たないため、この間に作られた成果物は
    # どのタスク・Track にも帰属しないまま残る。遮断はしない (「暮らしの中で
    # 世界に触れてよい」は意図した自由) が、無帳簿で増えるのは見えるようにする
    # — 帰属の器を暮らしにも付けるかは未裁定 (Codex レビュー 2026-08-08 #5)。
    artifacts = list(getattr(result, "artifacts", None) or [])
    if artifacts:
        LOGGER.warning(
            "[day_plan] life session produced artifacts with no attribution step "
            "(persona=%s date=%s index=%d kind=%s artifacts=%s)",
            persona_id, plan_date_str, index, slot.get("kind"), artifacts,
        )
    try:
        consume_life_pulse(
            manager, persona_id, plan_date_str, at_time=slot.get("start"),
        )
    except Exception:
        LOGGER.warning(
            "[day_plan] consume_life_pulse failed for life session "
            "(persona=%s date=%s index=%d); continuing",
            persona_id, plan_date_str, index, exc_info=True,
        )


def _handle_outing_slot(
    manager: Any, persona_id: str, plan_date_str: str, slot: Dict[str, Any], index: int
) -> None:
    """「出かける」コマの実行本体 (T3): 実移動 + コマ開始の Pulse (暮らしプロファイル)。

    行き先は ``_fire_slot`` (c) の :func:`_resolve_outing_destination` で確定・
    移動済み (自由時間からの委譲時は委譲側が同じ手順を踏む)。ここでは移動後の
    **実際の現在地**から状況テキストを組む — 移動に失敗して外へ出られなかった
    場合も、出たふりをせずその事実を提示する (接地原則)。
    """
    persona = (getattr(manager, "personas", {}) or {}).get(persona_id)
    current = getattr(persona, "current_building_id", None) if persona is not None else None
    if persona is None or not current:
        LOGGER.warning(
            "[day_plan] outing slot: persona not loaded or unplaced; "
            "presence-only record (persona=%s date=%s index=%d)",
            persona_id, plan_date_str, index,
        )
        _record_presence_only(manager, persona_id, plan_date_str, slot, index)
        return
    private_room = getattr(persona, "private_room_id", None)
    place = _building_display_name(manager, current)
    if slot.get("_outing_unresolved"):
        # 行き先候補ゼロ (施設の無い City 等)。移動していないので「出かけた」
        # 体にしない — 事実だけを提示する。
        text = (
            f"出かける時間でしたが、いま行ける場所が見つからないため"
            f"「{place}」で過ごします。"
        )
    elif slot.get("_move_failed") or (private_room is not None and current == private_room):
        # 移動が必要だったのにできなかった (満員 / 移動エラー等)。現在地が
        # 自室とは限らない — どこに居ようと「移動できずここにいる」が事実。
        text = f"出かける時間でしたが、移動できずに「{place}」にいます。"
    else:
        text = f"出かけて、「{place}」に来ました。"
    _run_slot_life_session(manager, persona_id, plan_date_str, slot, index, text)


def _handle_stay_home_slot(
    manager: Any, persona_id: str, plan_date_str: str, slot: Dict[str, Any], index: int
) -> None:
    """「自室で過ごす」コマの実行本体 (T3): own_room でコマ開始の Pulse (暮らしプロファイル)。

    「休む」(接触ゼロが合法) との違いは、知覚バッファに世界 (フィード新着等)
    が流れ込む前提で休むこと (intent §5.5) — その流れ込みはセッションの
    Beat 頭の知覚消費が担うので、ここは状況の提示だけをする。
    desire への積み込みは**許可形の一文**で促すにとどめる (「積んでいい」。
    義務形は充填独白 v2 §2.2 を呼び戻すため使わない — intent §5.5 確定)。
    """
    persona = (getattr(manager, "personas", {}) or {}).get(persona_id)
    current = getattr(persona, "current_building_id", None) if persona is not None else None
    if persona is None or not current:
        LOGGER.warning(
            "[day_plan] stay-home slot: persona not loaded or unplaced; "
            "presence-only record (persona=%s date=%s index=%d)",
            persona_id, plan_date_str, index,
        )
        _record_presence_only(manager, persona_id, plan_date_str, slot, index)
        return
    private_room = getattr(persona, "private_room_id", None)
    desire_line = (
        "思いついたことがあれば、やりたいこと候補に積んでおいてもいい時間です。"
    )
    if private_room is not None and current == private_room:
        text = f"自室で過ごす時間です。{desire_line}"
    else:
        # 自室へ戻れなかった (部屋なし / 移動失敗)。居る場所を偽らない。
        place = _building_display_name(manager, current)
        text = (
            f"自室で過ごす時間ですが、部屋に戻れず「{place}」で過ごしています。"
            f"{desire_line}"
        )
    _run_slot_life_session(manager, persona_id, plan_date_str, slot, index, text)


def _free_time_choices(manager: Any, persona_id: str, plan_date_str: str) -> List[str]:
    """自由時間コマで選べる kind の一覧 (カタログ順の決定論)。

    カタログの実行可能種別 (ハンドラ登録済み) から、自由時間系
    (execution_type='free_choice') 自身を除いたもの。日次予算の残高が 0 の
    ときは予算ゲート対象 (作業セッション系) も外す — 選ばせてから予算切れで
    転ばせない (読み取り専用の粗いゲート。厳密な per-life 判定はしない)。
    """
    choices: List[str] = []
    for definition in slot_kind_catalog.SLOT_KIND_CATALOG.values():
        name = definition.get("name")
        if definition.get("execution_type") == slot_kind_catalog.EXECUTION_FREE_CHOICE:
            continue
        if not name or name not in _SLOT_HANDLERS:
            continue
        choices.append(name)
    try:
        state = get_budget_state(manager, persona_id, plan_date_str)
    except Exception:
        LOGGER.warning(
            "[day_plan] budget state unavailable for free-time choices; "
            "keeping all kinds (persona=%s)", persona_id, exc_info=True,
        )
        state = None
    if state is not None and state["remaining"] <= 0:
        choices = [c for c in choices if c not in _BUDGET_GATED_KINDS]
    return choices


def _resolve_free_choice_client(persona: Any) -> Tuple[Optional[Any], str]:
    """自由時間の種別選択に使う軽量 LLM クライアントを ``(client, model名)`` で返す。

    ペルソナの LIGHTWEIGHT_MODEL があればそのクライアント
    (``persona.lightweight_llm_client``)、無ければ既定軽量モデル
    (:func:`sea.pulse_context.default_lightweight_model`) から構築する。
    構造化出力に対応しないモデルは None (呼び出し側が縮退する) — 対応可否は
    ``saiverse.model_configs.supports_structured_output`` が正典。
    """
    from saiverse.model_configs import (
        get_context_length,
        get_model_provider,
        supports_structured_output,
    )
    from sea.pulse_context import default_lightweight_model

    model = getattr(persona, "lightweight_model", None) or default_lightweight_model()
    if not supports_structured_output(model):
        return None, model
    if getattr(persona, "lightweight_model", None):
        client = getattr(persona, "lightweight_llm_client", None)
        if client is not None:
            return client, model
    from llm_clients import get_llm_client

    return (
        get_llm_client(model, get_model_provider(model), get_context_length(model)),
        model,
    )


def _choose_free_time_kind(
    manager: Any, persona_id: str, slot: Dict[str, Any], choices: List[str]
) -> Optional[str]:
    """自由時間の kind を本人 (軽量モデルの構造化出力一発) に選ばせる。

    失敗 (クライアント不在 / 非対応モデル / LLM エラー / enum 外の出力) は
    None — 呼び出し側が「自室で過ごす」相当へ縮退する (WARNING)。選択は
    行動の実行の一部であって、時間割を書き換える判断点ではない。
    """
    persona = (getattr(manager, "personas", {}) or {}).get(persona_id)
    if persona is None:
        LOGGER.warning(
            "[day_plan] free-time choice skipped: persona %s not loaded", persona_id,
        )
        return None
    try:
        client, model = _resolve_free_choice_client(persona)
    except Exception:
        LOGGER.warning(
            "[day_plan] free-time choice: failed to resolve lightweight client "
            "(persona=%s)", persona_id, exc_info=True,
        )
        return None
    if client is None:
        LOGGER.warning(
            "[day_plan] free-time choice: model %r does not support structured "
            "output (persona=%s)", model, persona_id,
        )
        return None

    lines: List[str] = []
    for name in choices:
        definition = slot_kind_catalog.get_kind_by_name(name) or {}
        description = str(definition.get("description") or "").strip()
        lines.append(f"- {name}: {description}" if description else f"- {name}")
    persona_name = str(getattr(persona, "persona_name", "") or persona_id)
    parts = [
        f"あなたは {persona_name} です。自由時間になりました。"
        "今から何をするか、次の種別から一つ選んでください。",
        "\n".join(lines),
    ]
    note = str(slot.get("note") or "").strip()
    if note:
        parts.append(f"このコマの方針メモ: {note}")
    schema = {
        "type": "object",
        "properties": {"kind": {"type": "string", "enum": list(choices)}},
        "required": ["kind"],
    }
    try:
        raw = client.generate(
            [{"role": "user", "content": "\n\n".join(parts)}],
            response_schema=schema,
        )
    except Exception:
        LOGGER.warning(
            "[day_plan] free-time choice LLM call failed (persona=%s model=%s)",
            persona_id, model, exc_info=True,
        )
        return None
    data: Any = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            data = None
    kind = data.get("kind") if isinstance(data, dict) else None
    if kind not in choices:
        LOGGER.warning(
            "[day_plan] free-time choice returned invalid kind %r (persona=%s "
            "model=%s choices=%s)", kind, persona_id, model, choices,
        )
        return None
    LOGGER.info(
        "[day_plan] free-time choice: persona=%s chose %r (model=%s)",
        persona_id, kind, model,
    )
    return kind


def _delegation_budget_clamp(
    manager: Any, persona_id: str, plan_date_str: str, index: int,
    slot: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """自由時間の委譲先 (作業セッション系) の予算判定 (Codex 四巡目 #3)。

    通常発火の (b) ゲート (:func:`_apply_budget_gate`) と同じ基準だが、
    **slot への skip 書き込みはしない** — 委譲は :func:`_fire_slot` の精算
    (slot_id で done を書く) の内側で走るため、ここで skipped を書いても精算が
    done で上書きして記録が矛盾する。

    Returns:
        通す場合は slot (残高超過は budget_rounds をクランプした複製)。
        拒否 (残高なし) は None — 呼び出し側が自室縮退する。
    """
    lives = get_lives(manager, persona_id, plan_date_str)
    if lives:
        # ライフ日はパルス残の二値判定 (ラウンドのクランプはしない —
        # _apply_life_budget_gate と同じ理由: 単位が異なる)
        idx = get_life_for_time(lives, slot["start"])
        if idx is None:
            return slot
        life = lives[idx]
        remaining = int(life.get("budget_pulses") or 0) - life_consumed(life)
        return slot if remaining > 0 else None
    state = get_budget_state(manager, persona_id, plan_date_str)
    if state is None:
        return slot
    remaining = state["remaining"]
    if remaining <= 0:
        return None
    requested = _effective_budget_rounds(slot)
    if remaining < requested:
        LOGGER.warning(
            "[day_plan] free-choice delegation budget clamped to remaining "
            "daily budget: %d -> %d (persona=%s date=%s index=%d kind=%s)",
            requested, remaining, persona_id, plan_date_str, index,
            slot.get("kind"),
        )
        return {**slot, "budget_rounds": remaining}
    return slot


def _handle_free_choice_slot(
    manager: Any, persona_id: str, plan_date_str: str, slot: Dict[str, Any], index: int
) -> Optional[int]:
    """「自由時間」コマの実行本体 (T3): 開始時に本人が選び、選んだ種別へ委譲する。

    intent §5.3 の「種別の穴の名前付き版」。§11-11 (選択タイミング: 朝の埋めか
    コマ開始時か) の決着はこの実装の形 — **テンプレの kind の穴は朝の判断で
    埋まり** (T2 ``timetable_template._default_hole_kind`` は穴のままの kind を
    「自由時間」に落とす)、**自由時間コマは開始時に本人が選ぶ**。「朝に決める
    穴」と「開始時に決める自由時間」の両方が共存するのが自然な決着である。

    選択は軽量モデルの構造化出力一発 (:func:`_choose_free_time_kind`)。失敗時は
    「自室で過ごす」相当へ縮退する (WARNING — 静かな別物化をしない)。委譲先が
    予算ゲート対象 (作業セッション系) のときは、実測ラウンドを台帳へ積算する
    (自由時間自身は非ゲートのため ``_fire_slot`` の精算では数えられない —
    legacy 経路の実測積算と同じ二本 (:func:`consume_budget` /
    :func:`consume_life_rounds`) をここで呼ぶ)。

    Returns:
        委譲先ハンドラの戻り値 (実測ラウンド数など)。縮退時は None。
    """
    choices = _free_time_choices(manager, persona_id, plan_date_str)
    chosen = (
        _choose_free_time_kind(manager, persona_id, slot, choices)
        if choices else None
    )
    handler = _SLOT_HANDLERS.get(chosen) if chosen else None
    if chosen is None or handler is None:
        LOGGER.warning(
            "[day_plan] free-choice slot could not settle a choice "
            "(chosen=%r handler=%s) — degrading to stay-home behaviour: "
            "persona=%s date=%s index=%d",
            chosen, "yes" if handler else "no", persona_id, plan_date_str, index,
        )
        _move_to_facility(manager, persona_id, {**slot, "facility": FACILITY_OWN_ROOM})
        _handle_stay_home_slot(manager, persona_id, plan_date_str, slot, index)
        return None

    chosen_slot = {**slot, "kind": chosen}
    # 委譲先が予算ゲート対象 (作業セッション系) なら、通常発火 (b) と同じ
    # 判定を通す — 自由時間経由だけ残高チェックとクランプを迂回して
    # 残高超過の実行・消費ができてしまう穴を塞ぐ (Codex 四巡目 #3。
    # _free_time_choices の除外は残高 0 の粗い篩いで、残 1 でも作業を選べる)。
    # 拒否時は選択失敗と同じ自室縮退 (自由時間コマ自体は presence として完了)。
    if chosen in _BUDGET_GATED_KINDS:
        clamped_slot = _delegation_budget_clamp(
            manager, persona_id, plan_date_str, index, chosen_slot,
        )
        if clamped_slot is None:
            LOGGER.warning(
                "[day_plan] free-choice delegation blocked by budget "
                "(persona=%s date=%s index=%d chosen=%s) — degrading to "
                "stay-home behaviour",
                persona_id, plan_date_str, index, chosen,
            )
            _move_to_facility(manager, persona_id, {**slot, "facility": FACILITY_OWN_ROOM})
            _handle_stay_home_slot(manager, persona_id, plan_date_str, slot, index)
            return None
        chosen_slot = clamped_slot
    definition = slot_kind_catalog.get_kind_by_name(chosen) or {}
    execution_type = definition.get("execution_type")
    if execution_type == slot_kind_catalog.EXECUTION_OUTING:
        # 委譲先が「出かける」系: _fire_slot (c) は自由時間としての facility で
        # 移動済みなので、ここで行き先を確定して出直す。
        chosen_slot = _resolve_outing_destination(
            manager, persona_id, plan_date_str, index, chosen_slot,
        )
        chosen_slot = _apply_slot_move(manager, persona_id, chosen_slot)
    elif execution_type == slot_kind_catalog.EXECUTION_STAY_HOME:
        chosen_slot = {**chosen_slot, "facility": FACILITY_OWN_ROOM}
        chosen_slot = _apply_slot_move(manager, persona_id, chosen_slot)
    # 作業セッション系は現在地で実施する (コマの facility は (c) で反映済み)

    used = handler(manager, persona_id, plan_date_str, chosen_slot, index)

    if chosen in _BUDGET_GATED_KINDS and isinstance(used, int) \
            and not isinstance(used, bool) and used > 0:
        try:
            consume_budget(manager, persona_id, plan_date_str, used)
        except Exception:
            LOGGER.exception(
                "[day_plan] consume_budget failed for delegated free-time work "
                "(persona=%s date=%s index=%d); continuing",
                persona_id, plan_date_str, index,
            )
        try:
            consume_life_rounds(
                manager, persona_id, plan_date_str, used, at_time=slot.get("start"),
            )
        except Exception:
            LOGGER.exception(
                "[day_plan] consume_life_rounds failed for delegated free-time "
                "work (persona=%s date=%s index=%d); continuing",
                persona_id, plan_date_str, index,
            )
    return used


# ---------------------------------------------------------------------------
# kind 語彙の構築 (カタログ → ALL_KINDS / テンプレート / ハンドラ配線)
# ---------------------------------------------------------------------------

#: execution_type → (組み込みハンドラ, 予算ゲート対象か)
_EXECUTION_TYPE_HANDLERS: Dict[str, Tuple[SlotHandler, bool]] = {
    slot_kind_catalog.EXECUTION_WORK_SESSION: (_handle_worker_slot, True),
    slot_kind_catalog.EXECUTION_OUTING: (_handle_outing_slot, False),
    slot_kind_catalog.EXECUTION_STAY_HOME: (_handle_stay_home_slot, False),
    slot_kind_catalog.EXECUTION_FREE_CHOICE: (_handle_free_choice_slot, False),
}


#: 語彙・ハンドラ再構築の直列化 (reload の並走と、構築途中の観測を防ぐ)。
#: 現状ランタイムの reload 経路は無い (呼ぶのは起動時とテストのみ) が、
#: 将来 API 経由の reload が生えたときに備えて配線順とロックを固めておく
#: (Codex 三巡目)。
_VOCAB_REBUILD_LOCK = threading.RLock()


def _rebuild_kind_vocabulary() -> None:
    """コマ種別カタログから kind 語彙・指示書テンプレート・ハンドラ配線を構築する。

    モジュールロード時 (末尾) と :func:`reload_kind_vocabulary` から呼ばれる。
    カタログから消えた kind のハンドラは掃除する (reload 経路)。

    配線順: **ハンドラを先に登録してから語彙 (ALL_KINDS) を公開する** —
    「語彙にあるのに未配線」の窓を作ると、その瞬間の発火が no_handler の
    偽スキップ (システム障害扱い) でコマを焼く。
    """
    global ALL_KINDS, WORKER_SESSION_KINDS, _WORKER_INSTRUCTION_TEMPLATES

    with _VOCAB_REBUILD_LOCK:
        previous_kinds = set(ALL_KINDS)
        names = slot_kind_catalog.kind_names()
        worker = slot_kind_catalog.kind_names_for_execution(
            slot_kind_catalog.EXECUTION_WORK_SESSION
        )
        templates = slot_kind_catalog.instruction_templates()
        # カタログのローダが work_session の instruction_template 必須を検証して
        # いるため通常は成立する。破れたらここで止める (旧 assert の新構成での維持)。
        assert set(worker) == set(templates), (
            "worker session kinds and instruction templates must stay in sync"
        )

        for definition in slot_kind_catalog.SLOT_KIND_CATALOG.values():
            handler, gated = _EXECUTION_TYPE_HANDLERS[definition["execution_type"]]
            register_slot_handler(definition["name"], handler, consumes_budget=gated)

        ALL_KINDS = tuple(names)
        WORKER_SESSION_KINDS = tuple(worker)
        _WORKER_INSTRUCTION_TEMPLATES = dict(templates)

        for stale_kind in previous_kinds - set(names):
            _SLOT_HANDLERS.pop(stale_kind, None)
            _BUDGET_GATED_KINDS.discard(stale_kind)
            LOGGER.info(
                "[day_plan] slot handler removed (kind no longer in catalog): %s",
                stale_kind,
            )


def reload_kind_vocabulary() -> Tuple[str, ...]:
    """カタログをディスクから読み直し、kind 語彙とハンドラ配線を再構築する。

    カタログの置換と語彙の再構築を同一ロック区間で行う (reload の並走で
    片方だけ新しい状態を観測させない)。

    Returns:
        再構築後の :data:`ALL_KINDS`。
    """
    with _VOCAB_REBUILD_LOCK:
        slot_kind_catalog.reload_catalog()
        _rebuild_kind_vocabulary()
    LOGGER.info(
        "[day_plan] kind vocabulary reloaded: %d kinds (%d work-session)",
        len(ALL_KINDS), len(WORKER_SESSION_KINDS),
    )
    return ALL_KINDS


_rebuild_kind_vocabulary()
