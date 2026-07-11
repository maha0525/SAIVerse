"""シナリオプレイヤー — 一日シミュレータの上位層 (自律行動 v2 §12)。

DES ドライバ (``saiverse.day_simulator.DaySimulator``) の上に載り、シナリオ定義
(起床・就寝・種の欲求とタスク・ユーザーの在不在・イベント) を仮想時刻で再生する。
判断点 (``saiverse.judgment_points``) と時間割のコマ発火 (``saiverse.day_plan``)
は既存機構をそのまま使い、本モジュールは「いつ・何を撃つか」の配線だけを持つ。

一日の流れ (intent §7.1 / §12):

1. wake 時刻に **起床判断** (day_open) — finalize が時間割を保存し、コマの発火を
   同じ EventScheduler へ push する (以後は決定論)
2. コマ発火は day_plan の既存機構。ただし六型の作業コマは
   ScenarioPlayer がラップハンドラを登録し、``run_work_session`` の後に
   **セッション終了判断** (post_session) を続けて撃つ (v2 §4.2 の背骨)。
   本番の恒久配線は day_plan の組み込みハンドラ (_handle_worker_slot →
   autonomy_wiring.fire_judgment_point) に実装済みだが、シム中は本プレイヤーの
   ラップが同 kind を **上書き登録** して置き換えるため二重発火しない
   (ラップは result への記録と mock/sync ディスパッチを担う)
3. ユーザーイベント (message / leave) はドライバが再生する。mock
   (:class:`TrackSimUserEventDriver`) は「会話中フラグ = running な
   user_conversation Track」の作成 / 完了のみ、実 LLM モード
   (:class:`RealConversationUserEventDriver`) は building_messages への発話記録
   + Track activate 経由の main_line Pulse という実チャット経路を同期に通す。
   leave で **会話終了判断** (post_conversation) を撃つ — ただし 1 往復も
   成立しなかった会話では撃たない (偽前提の状況テキストは作話を誘発するため)
4. events は **イベント到着判断** (on_event)
5. sleep 時刻に **就寝判断** (day_close)

シナリオ定義 (JSON。:func:`load_scenario` / :func:`parse_scenario`)::

    {
      "persona_id": "alice",
      "plan_date": "2026-07-04",
      "wake": "09:00",
      "sleep": "22:00",
      "daily_budget_rounds": 20,
      "seed": {
        "desires": [{"title": "言葉の標本集", "type": "作る",
                     "source": "会話で言い回しを褒められた"}],
        "tasks": [{"title": "共有文の下書きを書く", "goal": "本文が実在すること"}]
      },
      "user_events": [
        {"at": "15:00", "type": "message", "text": "ただいま"},
        {"at": "15:30", "type": "leave"}
      ],
      "events": [
        {"at": "18:00", "description": "掲示板の告知", "is_alert": false}
      ]
    }

前提 (DES 単一スレッド):

- ``manager.event_scheduler`` は ``start()`` していないこと
- ``manager.pulse_controller`` は **同期** に判断点を処理すること。
  配線テスト (mock LLM) は :class:`MockJudgmentPulseController`、行動テスト
  (実 LLM) は :class:`SyncJudgmentDispatcher` を使う
- 実行後も仮想クロックは有効なまま (レポート生成で仮想時刻を参照できる)。
  実時刻へ戻すのは呼び出し側の責務 (``clock.disable_virtual()``)

時刻はすべて ``saiverse.clock.now()`` を読む (v2 §12 の不変条件)。
"""
from __future__ import annotations

import importlib.util
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from saiverse import clock
from saiverse import day_plan
from saiverse.day_simulator import DaySimulator
from saiverse.judgment_points import (
    JUDGMENT_PLAYBOOK_MAP,
    KIND_DAY_CLOSE,
    KIND_DAY_OPEN,
    KIND_ON_EVENT,
    KIND_POST_CONVERSATION,
    KIND_POST_SESSION,
    run_judgment_point,
)

LOGGER = logging.getLogger(__name__)

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# ユーザーイベント種別
USER_EVENT_MESSAGE = "message"
USER_EVENT_LEAVE = "leave"
USER_EVENT_ABSENT_ALL_DAY = "absent_all_day"
USER_EVENT_TYPES = (USER_EVENT_MESSAGE, USER_EVENT_LEAVE, USER_EVENT_ABSENT_ALL_DAY)

#: Playbook 名 → 判断点 kind (mock ディスパッチャが逆引きに使う)
PLAYBOOK_TO_KIND: Dict[str, str] = {v: k for k, v in JUDGMENT_PLAYBOOK_MAP.items()}


# ---------------------------------------------------------------------------
# シナリオ定義
# ---------------------------------------------------------------------------


@dataclass
class SeedDesire:
    """種の欲求 (desire ノート内の候補 Task として植える)。"""
    title: str
    type: Optional[str] = None
    source: str = ""


@dataclass
class SeedTask:
    """種のタスク (バックログとして植える)。"""
    title: str
    goal: str = ""


@dataclass
class UserEvent:
    """ユーザーの在不在イベント。``absent_all_day`` のみ ``at`` 不要。"""
    type: str
    at: Optional[str] = None
    text: str = ""


@dataclass
class WorldEvent:
    """世界からのイベント (on_event 判断の入力)。"""
    at: str
    description: str
    is_alert: bool = False


@dataclass
class DayScenario:
    """一日シナリオ。:func:`parse_scenario` が検証済みの形で作る。"""
    persona_id: str
    plan_date: str
    wake: str
    sleep: str
    daily_budget_rounds: Optional[int] = None
    seed_desires: List[SeedDesire] = field(default_factory=list)
    seed_tasks: List[SeedTask] = field(default_factory=list)
    user_events: List[UserEvent] = field(default_factory=list)
    events: List[WorldEvent] = field(default_factory=list)


def _require_hhmm(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _TIME_RE.match(value):
        raise ValueError(f"scenario.{label} must be 'HH:MM' (got {value!r})")
    return value


def parse_scenario(data: Dict[str, Any]) -> DayScenario:
    """シナリオ dict を検証して :class:`DayScenario` に正規化する。

    Raises:
        ValueError: 必須フィールド欠落・時刻書式不正・未知のイベント種別等。
    """
    if not isinstance(data, dict):
        raise ValueError(f"scenario must be a dict (got {type(data).__name__})")

    persona_id = str(data.get("persona_id") or "").strip()
    if not persona_id:
        raise ValueError("scenario.persona_id is required")

    plan_date_raw = data.get("plan_date")
    if plan_date_raw is None:
        plan_date = clock.now().date().isoformat()
    else:
        try:
            plan_date = date.fromisoformat(str(plan_date_raw).strip()).isoformat()
        except ValueError as exc:
            raise ValueError(
                f"scenario.plan_date must be 'YYYY-MM-DD' (got {plan_date_raw!r})"
            ) from exc

    wake = _require_hhmm(data.get("wake"), "wake")
    sleep = _require_hhmm(data.get("sleep"), "sleep")
    if sleep <= wake:
        raise ValueError(
            f"scenario.sleep ({sleep}) must be after wake ({wake}) — "
            "日を跨ぐシナリオは未対応です"
        )

    budget = data.get("daily_budget_rounds")
    if budget is not None:
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
            raise ValueError(
                f"scenario.daily_budget_rounds must be a positive int (got {budget!r})"
            )

    seed = data.get("seed") or {}
    if not isinstance(seed, dict):
        raise ValueError(f"scenario.seed must be a dict (got {type(seed).__name__})")
    desires: List[SeedDesire] = []
    for i, d in enumerate(seed.get("desires") or []):
        if not isinstance(d, dict) or not str(d.get("title") or "").strip():
            raise ValueError(f"scenario.seed.desires[{i}] requires a title")
        dtype = d.get("type")
        if dtype is not None:
            from saiverse.desire_engine import DESIRE_TYPES
            if dtype not in DESIRE_TYPES:
                raise ValueError(
                    f"scenario.seed.desires[{i}].type={dtype!r} is not one of {DESIRE_TYPES}"
                )
        desires.append(SeedDesire(
            title=str(d["title"]).strip(),
            type=dtype,
            source=str(d.get("source") or ""),
        ))
    tasks: List[SeedTask] = []
    for i, t in enumerate(seed.get("tasks") or []):
        if not isinstance(t, dict) or not str(t.get("title") or "").strip():
            raise ValueError(f"scenario.seed.tasks[{i}] requires a title")
        tasks.append(SeedTask(
            title=str(t["title"]).strip(),
            goal=str(t.get("goal") or ""),
        ))

    user_events: List[UserEvent] = []
    for i, ue in enumerate(data.get("user_events") or []):
        if not isinstance(ue, dict):
            raise ValueError(f"scenario.user_events[{i}] must be a dict")
        ue_type = ue.get("type")
        if ue_type not in USER_EVENT_TYPES:
            raise ValueError(
                f"scenario.user_events[{i}].type={ue_type!r} is not one of {USER_EVENT_TYPES}"
            )
        at = ue.get("at")
        if ue_type == USER_EVENT_ABSENT_ALL_DAY:
            at = None  # 終日不在は時刻を持たない (マーカーのみ)
        else:
            at = _require_hhmm(at, f"user_events[{i}].at")
        text = str(ue.get("text") or "")
        if ue_type == USER_EVENT_MESSAGE and not text.strip():
            raise ValueError(f"scenario.user_events[{i}] (message) requires text")
        user_events.append(UserEvent(type=ue_type, at=at, text=text))

    events: List[WorldEvent] = []
    for i, ev in enumerate(data.get("events") or []):
        if not isinstance(ev, dict):
            raise ValueError(f"scenario.events[{i}] must be a dict")
        at = _require_hhmm(ev.get("at"), f"events[{i}].at")
        description = str(ev.get("description") or "").strip()
        if not description:
            raise ValueError(f"scenario.events[{i}] requires description")
        events.append(WorldEvent(
            at=at, description=description, is_alert=bool(ev.get("is_alert")),
        ))

    return DayScenario(
        persona_id=persona_id,
        plan_date=plan_date,
        wake=wake,
        sleep=sleep,
        daily_budget_rounds=budget,
        seed_desires=desires,
        seed_tasks=tasks,
        user_events=user_events,
        events=events,
    )


def load_scenario(path: Path | str) -> DayScenario:
    """シナリオ JSON ファイルを読み込んで検証する。"""
    raw = Path(path).read_text(encoding="utf-8")
    return parse_scenario(json.loads(raw))


# ---------------------------------------------------------------------------
# ユーザーイベントドライバ (在不在の再生)
# ---------------------------------------------------------------------------


def _ensure_conversation_episode(manager: Any, persona_id: str) -> None:
    """会話の出来事 (kind='conversation') を開く (冪等)。

    実 manager (--real) では UserConversationTrackHandler の track_activated
    hook が既に開いている場合があり、その場合は no-op になる。mock 構成
    (最小スタブ manager) では observer が居ないため、ここが唯一の開き点。
    出来事は記録専用 — 失敗してもシナリオ再生を止めない。
    """
    try:
        persona = (getattr(manager, "personas", None) or {}).get(persona_id)
        building_id = getattr(persona, "current_building_id", None)
        participants = [persona_id]
        user_id = getattr(manager, "user_id", None)
        if user_id is not None:
            participants.append(str(user_id))
        from saiverse.episodes import open_conversation_episode
        open_conversation_episode(
            manager, persona_id,
            building_id=building_id, participants=participants,
        )
    except Exception:
        LOGGER.warning(
            "[day_scenario] failed to open conversation episode (persona=%s)",
            persona_id, exc_info=True,
        )


def _close_conversation_episode(manager: Any, persona_id: str) -> None:
    """開いている会話の出来事を閉じる (無ければ no-op)。"""
    try:
        from saiverse.episodes import close_conversation_episode
        close_conversation_episode(manager, persona_id)
    except Exception:
        LOGGER.warning(
            "[day_scenario] failed to close conversation episode (persona=%s)",
            persona_id, exc_info=True,
        )


class UserEventDriver:
    """ユーザーの在不在をシムに反映するドライバのインターフェイス。"""

    def begin_conversation(self, manager: Any, persona_id: str, text: str) -> None:
        """message イベント: ユーザー会話の開始 (会話中フラグを立てる)。"""
        raise NotImplementedError

    def end_conversation(self, manager: Any, persona_id: str) -> bool:
        """leave イベント: ユーザー会話の終了。

        Returns:
            会話が実際に終了した (= 会話中だった) なら True。
        """
        raise NotImplementedError

    def conversation_had_exchange(self, manager: Any, persona_id: str) -> bool:
        """現在の会話で 1 往復以上 (ユーザー発話 + ペルソナ応答) 成立したか。

        ScenarioPlayer が leave 時に読む — False なら会話終了判断
        (post_conversation) を撃たない (存在しない会話の振り返り = 作話を
        誘発しないため。接地原則 v2 §3-1)。

        既定は True: mock 配線テスト (TrackSimUserEventDriver) は会話本文を
        再生せず「会話が成立した」ものとして扱う (検証対象は判断の配線)。
        """
        return True


class TrackSimUserEventDriver(UserEventDriver):
    """会話中フラグを running な user_conversation Track で表現するシム内ドライバ。

    day_plan の「ユーザー会話中」判定 (``_is_in_user_conversation``) と同じ述語
    (running Track が user_conversation 種別) を成立させるため、message で
    running Track を作り、leave で complete する。会話本文の再生 (実 Pulse) は
    しない — 配線テストの対象は「会話中の繰り下げ」と「会話終了判断の発火」。
    """

    def begin_conversation(self, manager: Any, persona_id: str, text: str) -> None:
        track_manager = manager.track_manager
        running = track_manager.get_running(persona_id)
        if running is not None and getattr(running, "track_type", None) == "user_conversation":
            LOGGER.info(
                "[day_scenario] user message while already in conversation "
                "(persona=%s); keeping the running track", persona_id,
            )
            _ensure_conversation_episode(manager, persona_id)
            return
        track_id = track_manager.create(
            persona_id=persona_id,
            track_type="user_conversation",
            title="ユーザーとの会話 (シナリオ再生)",
            metadata=json.dumps(
                {"scenario": True, "first_message": text[:120]}, ensure_ascii=False,
            ),
            initial_status="running",
        )
        _ensure_conversation_episode(manager, persona_id)
        LOGGER.info(
            "[day_scenario] conversation started: persona=%s track=%s text=%r",
            persona_id, track_id, text[:60],
        )

    def end_conversation(self, manager: Any, persona_id: str) -> bool:
        track_manager = manager.track_manager
        running = track_manager.get_running(persona_id)
        if running is None or getattr(running, "track_type", None) != "user_conversation":
            LOGGER.warning(
                "[day_scenario] leave event but no user conversation is running "
                "(persona=%s); ignoring", persona_id,
            )
            return False
        # 本番由来の対ユーザー会話 Track は永続 (is_persistent=true) で
        # completed への遷移が禁止されている。leave の意味論は本番の
        # wait_response 自動 pause と同じ running → pending (world clone した
        # 実ペルソナで 2026-07-06 に顕在化。シム内で新規作成した非永続 Track
        # は従来どおり complete で畳む)
        if getattr(running, "is_persistent", False):
            track_manager.pause(running.track_id)
        else:
            track_manager.complete(running.track_id)
        # 会話の出来事を閉じる (本番の wait_response タイムアウト経路に対応する
        # シム側の閉じ点。leave = 運用の線 §8)
        _close_conversation_episode(manager, persona_id)
        LOGGER.info(
            "[day_scenario] conversation ended: persona=%s track=%s",
            persona_id, running.track_id,
        )
        return True


class RealConversationUserEventDriver(TrackSimUserEventDriver):
    """行動テスト (実 LLM モード) 用: ユーザー発話を本物の会話経路へ注入するドライバ。

    mock の :class:`TrackSimUserEventDriver` が「会話中フラグ (Track)」だけを
    再生するのに対し、本ドライバは実チャット経路 (``manager/runtime.py``
    ``handle_user_input_stream`` の backend_worker) と同じ順序で正規経路を叩く:

    1. ユーザー発話を building_messages へ記録 (heard_by = ペルソナ + ユーザー)
    2. user_conversation Track を running で作成 — 実 TrackManager の
       track_activated observer (UserConversationTrackHandler) が Track 切替通知の
       注入と main_line Pulse の起動 (``manager.run_sea_user``) を行い、Pulse
       冒頭の auto_ingest が (1) の発話をペルソナ記憶 (memory.db) へ取り込む。
       Pulse は :class:`SyncJudgmentDispatcher` の ``submit_user`` 経由で
       呼び出しスレッド上で同期実行される (DES 単一スレッド)
    3. 会話中の追加メッセージは実経路の「running Track → 直接メインライン起動」
       と同型に ``manager.run_sea_user`` を直接呼ぶ
    4. Pulse 後にペルソナ応答が building_messages に実在するかを検査して
       「会話が成立したか」を記録する (:meth:`conversation_had_exchange`)。
       応答ゼロの会話では ScenarioPlayer が会話終了判断を撃たない

    前提: manager は実 SAIVerseManager (persona に history_manager がある)。
    """

    def __init__(self) -> None:
        #: persona_id → 現在の会話で 1 往復以上成立したか
        self._had_exchange: Dict[str, bool] = {}

    def begin_conversation(self, manager: Any, persona_id: str, text: str) -> None:
        persona = (getattr(manager, "personas", None) or {}).get(persona_id)
        if persona is None:
            raise RuntimeError(f"persona '{persona_id}' not found on manager")
        building_id = getattr(persona, "current_building_id", None)
        if not building_id:
            raise RuntimeError(f"persona '{persona_id}' has no current building")

        # (1) ユーザー発話を building_messages へ記録 (実チャット経路の pre-add)
        seq_before = self._record_user_message(manager, persona, building_id, text)

        track_manager = manager.track_manager
        running = track_manager.get_running(persona_id)
        if running is not None and getattr(running, "track_type", None) == "user_conversation":
            # (3) 会話継続: 実経路の「既存 running → 直接メインライン起動」と同型
            LOGGER.info(
                "[day_scenario] user message in ongoing conversation "
                "(persona=%s track=%s); invoking main line directly",
                persona_id, running.track_id,
            )
            _ensure_conversation_episode(manager, persona_id)
            manager.run_sea_user(
                persona, building_id, text, origin_track_id=running.track_id,
            )
        else:
            # (2) Track を running で作成 → track_activated observer が Track
            # 切替通知 + main_line Pulse (user_input は auto_ingest 経由) を起動
            track_id = track_manager.create(
                persona_id=persona_id,
                track_type="user_conversation",
                title="ユーザーとの会話 (シナリオ再生)",
                output_target="building:current",
                metadata=json.dumps(
                    {"scenario": True, "first_message": text[:120]},
                    ensure_ascii=False,
                ),
                initial_status="running",
            )
            LOGGER.info(
                "[day_scenario] conversation started via real path: "
                "persona=%s track=%s text=%r",
                persona_id, track_id, text[:60],
            )
            # 実 manager では track_activated observer (UserConversationTrackHandler)
            # が既に出来事を開いている — 冪等なので二重には開かない。
            _ensure_conversation_episode(manager, persona_id)

        # (4) 応答の実在検査 (building_messages の追記で確認 — 接地)
        replied = self._persona_replied_after(manager, persona, building_id, seq_before)
        self._had_exchange[persona_id] = self._had_exchange.get(persona_id, False) or replied
        if not replied:
            LOGGER.warning(
                "[day_scenario] persona did not reply to user message "
                "(persona=%s text=%r) — this conversation has no exchange yet",
                persona_id, text[:60],
            )

    def end_conversation(self, manager: Any, persona_id: str) -> bool:
        ended = super().end_conversation(manager, persona_id)
        if ended:
            # 次の会話のために往復フラグを畳む (leave 時の判定は ScenarioPlayer が
            # end_conversation の前に conversation_had_exchange で読む)
            self._had_exchange.pop(persona_id, None)
        return ended

    def conversation_had_exchange(self, manager: Any, persona_id: str) -> bool:
        return self._had_exchange.get(persona_id, False)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _canonical_building_id(manager: Any, building_id: str) -> str:
        """実 manager の building_id 正規化 (無ければ素通し)。"""
        runtime = getattr(manager, "runtime", None)
        fn = getattr(runtime, "_canonical_building_id", None)
        if callable(fn):
            try:
                return fn(building_id)
            except Exception:
                LOGGER.warning(
                    "[day_scenario] _canonical_building_id failed for %r; "
                    "using it as-is", building_id, exc_info=True,
                )
        return building_id

    def _record_user_message(
        self, manager: Any, persona: Any, building_id: str, text: str
    ) -> int:
        """ユーザー発話を building_messages へ記録し、その seq を返す。

        実チャット経路 (backend_worker) と同じ ``add_to_building_only`` +
        heard_by。auto_ingest は heard_by にペルソナが居るメッセージだけを
        取り込むため、heard_by は必須。
        """
        history_manager = getattr(persona, "history_manager", None)
        if history_manager is None:
            raise RuntimeError(
                f"persona '{persona.persona_id}' has no history_manager — "
                "RealConversationUserEventDriver は実 SAIVerseManager 専用です"
            )
        canonical_bid = self._canonical_building_id(manager, building_id)
        heard = [persona.persona_id]
        user_id = getattr(manager, "user_id", None)
        if user_id is not None:
            heard.append(str(user_id))
        saved = history_manager.add_to_building_only(
            canonical_bid, {"role": "user", "content": text}, heard_by=heard,
        )
        try:
            return int((saved or {}).get("seq") or 0)
        except (TypeError, ValueError):
            return 0

    def _persona_replied_after(
        self, manager: Any, persona: Any, building_id: str, seq_before: int
    ) -> bool:
        """seq_before より後にペルソナの assistant 発言が実在するか (接地検査)。"""
        canonical_bid = self._canonical_building_id(manager, building_id)
        try:
            hist = persona.history_manager.get_building_history(canonical_bid) or []
        except Exception:
            LOGGER.warning(
                "[day_scenario] failed to read building history for reply check "
                "(persona=%s building=%s)",
                persona.persona_id, canonical_bid, exc_info=True,
            )
            return False
        for msg in hist:
            try:
                seq = int(msg.get("seq") or 0)
            except (TypeError, ValueError):
                seq = 0
            if seq <= seq_before:
                continue
            if msg.get("role") == "assistant" and msg.get("persona_id") == persona.persona_id:
                return True
        return False


# ---------------------------------------------------------------------------
# 判断点ディスパッチャ (manager.pulse_controller 互換)
# ---------------------------------------------------------------------------


_FINALIZE_MODULE: Optional[Any] = None


def _load_judgment_finalize() -> Any:
    """builtin_data/tools/judgment_finalize.py を直接ロードする (キャッシュ)。

    ツールレジストリを経由しないのは、mock 構成 (最小スタブ manager) では
    ツールロード一式が走っていないため。
    """
    global _FINALIZE_MODULE
    if _FINALIZE_MODULE is not None:
        return _FINALIZE_MODULE
    from saiverse.data_paths import BUILTIN_DATA_DIR

    tool_path = BUILTIN_DATA_DIR / "tools" / "judgment_finalize.py"
    spec = importlib.util.spec_from_file_location(
        "_day_scenario_judgment_finalize", tool_path,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _FINALIZE_MODULE = module
    return module


class MockJudgmentPulseController:
    """配線テスト (mock LLM) 用の同期判断点ディスパッチャ。

    ``PulseController.submit_meta_judgment`` 互換のシグネチャを持ち、Playbook の
    judge LLM ノードの代わりに ``judge_fn(kind, args) -> dict`` (構造化出力の
    mock) を呼び、続けて ``judgment_finalize`` ツールを同期適用する — 判断点
    Playbook の「judge → finalize」2 ノードの決定論置換。LLM コストはゼロ。

    Args:
        judge_fn: ``(kind, args) -> judgment_output dict``。args は
            ``run_judgment_point`` が組んだ ``{situation_text, response_schema,
            judgment_context}``。
        persona_path: ツールコンテキスト (``tools.context.persona_context``) に
            渡すペルソナ作業ディレクトリ (テストでは tmp_path で良い)。
    """

    def __init__(
        self,
        manager: Any,
        judge_fn: Callable[[str, Dict[str, Any]], Dict[str, Any]],
        persona_path: Path | str,
    ) -> None:
        self.manager = manager
        self.judge_fn = judge_fn
        self.persona_path = Path(persona_path)
        #: 適用済み判断の記録 (テスト / レポートの観察用)
        self.finalized: List[Dict[str, Any]] = []

    def submit_meta_judgment(
        self,
        persona_id: str,
        building_id: str,
        meta_playbook: str,
        args: Optional[Dict[str, Any]] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        args = args or {}
        kind = PLAYBOOK_TO_KIND.get(meta_playbook)
        if kind is None:
            raise ValueError(f"unknown judgment playbook: {meta_playbook!r}")
        output = self.judge_fn(kind, args)
        if not isinstance(output, dict):
            raise ValueError(
                f"judge_fn must return a dict (kind={kind}, got {type(output).__name__})"
            )
        finalize_mod = _load_judgment_finalize()
        from tools.context import persona_context

        with persona_context(persona_id, self.persona_path, manager=self.manager):
            summary, _, _ = finalize_mod.judgment_finalize(
                judgment_output=output,
                kind=kind,
                judgment_context=str(args.get("judgment_context") or ""),
                situation_text=str(args.get("situation_text") or ""),
            )
        self.finalized.append({
            "kind": kind,
            "persona_id": persona_id,
            "at": clock.now().isoformat(timespec="seconds"),
            "summary": summary,
        })
        LOGGER.info("[day_scenario] mock judgment finalized: %s (%s)", kind, summary)
        return None


class SyncJudgmentDispatcher:
    """行動テスト (実 LLM) 用の同期 Pulse ディスパッチャ。

    実 ``PulseController`` はレーン管理 (優先度・並列メタ判断レーン・キュー) を
    持ち、単一スレッド前提の DES とは合わない。本ディスパッチャは
    ``manager.sea_runtime.run_meta_user`` を呼び出しスレッドでそのまま実行する
    (Playbook・finalize・SAIMemory 書き込みはすべて正規経路)。

    シム中に叩かれる入口は 2 つ:

    - ``submit_meta_judgment``: 判断点 (``run_judgment_point``) の起動経路
    - ``submit_user``: ユーザー会話 Pulse。実チャット経路
      (UserConversationTrackHandler → ``manager.run_sea_user``) がシム中の
      Track activate から呼んでくる。実 ``PulseController.submit_user`` と
      同シグネチャ (これが無いと --real でユーザー会話が AttributeError で
      不発になる — 2026-07-05 クオン一日シムの実バグ)

    使い方: シナリオ実行の間だけ ``manager.pulse_controller`` をこれに
    差し替える (``scripts/run_day_sim.py`` の実 LLM モード参照)。
    """

    def __init__(self, manager: Any) -> None:
        self.manager = manager

    def _require_persona(self, persona_id: str) -> Any:
        persona = (getattr(self.manager, "personas", None) or {}).get(persona_id)
        if persona is None:
            raise RuntimeError(f"persona '{persona_id}' not found on manager")
        return persona

    def submit_meta_judgment(
        self,
        persona_id: str,
        building_id: str,
        meta_playbook: str,
        args: Optional[Dict[str, Any]] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Optional[List[str]]:
        persona = self._require_persona(persona_id)
        return self.manager.sea_runtime.run_meta_user(
            persona,
            user_input=None,
            building_id=building_id,
            meta_playbook=meta_playbook,
            args=args,
            event_callback=event_callback,
            pulse_type="meta_judgment",
        )

    def submit_user(
        self,
        persona_id: str,
        building_id: str,
        user_input: str,
        metadata: Optional[Dict[str, Any]] = None,
        meta_playbook: Optional[str] = None,
        args: Optional[Dict[str, Any]] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        pre_spells: Optional[List[str]] = None,
        origin_track_id: Optional[str] = None,
    ) -> Optional[List[str]]:
        """ユーザー会話 Pulse を呼び出しスレッドで同期実行する。

        実 ``PulseController.submit_user`` → ``_do_execute`` と同じく
        ``run_meta_user(pulse_type="user")`` (CONVERSATION アスペクト) を叩く。
        ユーザー発話本体は building_messages に事前記録され、Pulse 冒頭の
        auto_ingest がペルソナ記憶へ取り込む (実チャット経路と同型)。
        """
        persona = self._require_persona(persona_id)
        return self.manager.sea_runtime.run_meta_user(
            persona,
            user_input=user_input,
            building_id=building_id,
            metadata=metadata,
            meta_playbook=meta_playbook,
            args=args,
            event_callback=event_callback,
            pre_spells=pre_spells,
            origin_track_id=origin_track_id,
            pulse_type="user",
        )


# ---------------------------------------------------------------------------
# ScenarioPlayer
# ---------------------------------------------------------------------------


@dataclass
class ScenarioRunResult:
    """シナリオ 1 本の実行結果 (観察用の帳簿)。"""
    scenario: DayScenario
    executed_events: int = 0
    judgments: List[Dict[str, Any]] = field(default_factory=list)
    seeded_task_refs: List[str] = field(default_factory=list)
    seeded_desire_refs: List[str] = field(default_factory=list)


class ScenarioPlayer:
    """シナリオを仮想クロックで再生するプレイヤー (モジュール docstring 参照)。"""

    def __init__(self, user_event_driver: Optional[UserEventDriver] = None) -> None:
        self.user_event_driver = user_event_driver or TrackSimUserEventDriver()

    # ------------------------------------------------------------------
    # 実行
    # ------------------------------------------------------------------

    def run(self, manager: Any, scenario: DayScenario | Dict[str, Any]) -> ScenarioRunResult:
        """シナリオを端から端まで実行する。

        実行後も仮想クロックは有効なまま (仮想時刻 = sleep)。実時刻に戻すのは
        呼び出し側の責務 (``clock.disable_virtual()``)。
        """
        sc = scenario if isinstance(scenario, DayScenario) else parse_scenario(scenario)
        persona_id = sc.persona_id
        plan_day = date.fromisoformat(sc.plan_date)
        wake_dt = self._combine(plan_day, sc.wake)
        sleep_dt = self._combine(plan_day, sc.sleep)

        result = ScenarioRunResult(scenario=sc)

        # 仮想クロックを wake で立ててから種を植える (created_at が「今日」になる)
        clock.enable_virtual(wake_dt)
        self._seed(manager, sc, result)

        scheduler = manager.event_scheduler
        # シナリオ外の既存予約を除去する。実 SAIVerseManager は __init__ で
        # 実時刻シードの定期イベント (db_polling 3 秒周期 / SDS heartbeat 等) を
        # 積むため、そのまま DES を回すと仮想時刻がそこへ達した時点から 3 秒
        # 刻みの no-op ステップを一日ぶん (数千回) 消化してしまう
        # (2026-07-05 実 LLM シム 3回目: steps=8,350 の 99% が db_polling 空回り)。
        # シムの前提は「シナリオ由来のイベントだけを回す」— 除去はシム側の責務
        # (本番コードに sim 分岐を持ち込まない)。
        stale_keys = scheduler.cancel_all()
        if stale_keys:
            LOGGER.info(
                "[day_scenario] cleared %d pre-existing reservations before sim: %s",
                len(stale_keys), ", ".join(sorted(stale_keys)),
            )
        saved_handlers = dict(day_plan._SLOT_HANDLERS)
        saved_gated = set(day_plan._BUDGET_GATED_KINDS)
        # 六型の作業コマにセッション終了判断を接続するラップハンドラ。
        # 本番の恒久配線 (_handle_worker_slot 内の fire_judgment_point) を
        # シム実行中だけ **上書き** して置き換え (二重発火しない)、必ず戻す。
        # ラップ側は result への記録と、mock/sync ディスパッチャ経由の
        # run_judgment_point 直呼び (Active ゲート無し) を担う。
        for worker_kind in day_plan.WORKER_SESSION_KINDS:
            day_plan.register_slot_handler(
                worker_kind,
                self._make_session_slot_handler(result),
                consumes_budget=True,
            )
        try:
            self._schedule_all(manager, sc, result, plan_day, wake_dt, sleep_dt)
            sim = DaySimulator(scheduler, start=wake_dt, end=sleep_dt)
            result.executed_events = sim.run()
        finally:
            day_plan._SLOT_HANDLERS.clear()
            day_plan._SLOT_HANDLERS.update(saved_handlers)
            day_plan._BUDGET_GATED_KINDS.clear()
            day_plan._BUDGET_GATED_KINDS.update(saved_gated)

        LOGGER.info(
            "[day_scenario] run finished: persona=%s date=%s events=%d judgments=%d",
            persona_id, sc.plan_date, result.executed_events, len(result.judgments),
        )
        return result

    # ------------------------------------------------------------------
    # 内部: 種まき
    # ------------------------------------------------------------------

    @staticmethod
    def _combine(plan_day: date, hhmm: str) -> datetime:
        hh, mm = hhmm.split(":")
        return datetime.combine(plan_day, dt_time(int(hh), int(mm)))

    def _seed(self, manager: Any, sc: DayScenario, result: ScenarioRunResult) -> None:
        """種のタスク・欲求を DB に植える (シナリオの初期状態)。"""
        from saiverse import purpose_tree
        from saiverse.persona_task_manager import PersonaTaskManager

        ptm = PersonaTaskManager(manager.SessionLocal)
        for seed_task in sc.seed_tasks:
            task = ptm.create_task(
                persona_id=sc.persona_id,
                title=seed_task.title,
                goal=seed_task.goal,
                origin="autonomous",
                auto_activate=False,
                actor="day_scenario_seed",
            )
            result.seeded_task_refs.append(task.get("task_ref") or task["id"])
        if sc.seed_desires:
            for seed_desire in sc.seed_desires:
                # purpose_tree.create_candidate は唯一の候補作成入口 (P3c-0)。
                # 接地原則で source_ref が必須のため、シナリオ側が空にしている
                # ケースはシナリオ自体を来歴として使う (シム専用の種まきなので
                # 実接地の対象外)。
                node = purpose_tree.create_candidate(
                    manager, sc.persona_id, seed_desire.title,
                    seed_desire.source or f"day_scenario_seed:{sc.plan_date}",
                    desire_type=seed_desire.type, actor="day_scenario_seed",
                    origin="autonomous",
                )
                ref = node.get("ref") or node["id"]
                result.seeded_desire_refs.append(ref)
        LOGGER.info(
            "[day_scenario] seeded: persona=%s tasks=%s desires=%s",
            sc.persona_id, result.seeded_task_refs, result.seeded_desire_refs,
        )

    # ------------------------------------------------------------------
    # 内部: 判断点の起動と記録
    # ------------------------------------------------------------------

    def _judge(
        self,
        manager: Any,
        persona_id: str,
        kind: str,
        context: Optional[Dict[str, Any]],
        result: ScenarioRunResult,
    ) -> Dict[str, Any]:
        jr = run_judgment_point(manager, persona_id, kind, context)
        result.judgments.append({
            "kind": kind,
            "at": clock.now().isoformat(timespec="seconds"),
            "submitted": jr.get("submitted"),
            "reason": jr.get("reason"),
        })
        if not jr.get("submitted"):
            LOGGER.warning(
                "[day_scenario] judgment %s not submitted (persona=%s): %s",
                kind, persona_id, jr.get("reason"),
            )
        return jr

    def _make_session_slot_handler(self, result: ScenarioRunResult) -> day_plan.SlotHandler:
        """六型の作業コマ: 既存のセッション運転 + セッション終了判断の接続。

        NOTE: post_session 判断はコマの status が done になる前 (fired のまま)
        に走る — 判断が見る「残りの時間割」に当該コマは含まれない
        (pending/deferred のみ表示されるため) が、予算台帳への積算は判断の後、
        ハンドラ戻り値経由で行われる (既存 ``_fire_slot`` の帳簿順序を変えない)。
        """

        def _handler(
            manager: Any, persona_id: str, plan_date_str: str,
            slot: Dict[str, Any], index: int,
        ) -> Optional[int]:
            session_result = day_plan.run_worker_slot_session(
                manager, persona_id, plan_date_str, slot, index,
            )
            if session_result is None:
                # 中身が空の track コマの presence 縮退 (P5)。本番の
                # _handle_worker_slot と同じく判断点は撃たない。
                return 0
            ref = str(slot.get("ref") or day_plan.REF_NONE)
            context: Dict[str, Any] = {
                "session_result": session_result,
                "budget_rounds": int(slot.get("budget_rounds") or 0) or None,
            }
            # track:N コマの対象は Track (session_result.track_id 経由)。
            if ref != day_plan.REF_NONE and not ref.startswith("track:"):
                context["task_ref"] = ref
            self._judge(manager, persona_id, KIND_POST_SESSION, context, result)
            return day_plan.worker_session_rounds_used(session_result)

        return _handler

    # ------------------------------------------------------------------
    # 内部: イベント予約
    # ------------------------------------------------------------------

    def _schedule_all(
        self,
        manager: Any,
        sc: DayScenario,
        result: ScenarioRunResult,
        plan_day: date,
        wake_dt: datetime,
        sleep_dt: datetime,
    ) -> None:
        scheduler = manager.event_scheduler
        persona_id = sc.persona_id

        day_open_context: Dict[str, Any] = {}
        if sc.daily_budget_rounds is not None:
            day_open_context["daily_budget_rounds"] = sc.daily_budget_rounds
        scheduler.schedule(
            fire_at=wake_dt,
            callback=lambda: self._judge(
                manager, persona_id, KIND_DAY_OPEN, day_open_context, result,
            ),
            key=f"scenario:{persona_id}:day_open",
        )

        for i, ue in enumerate(sc.user_events):
            if ue.type == USER_EVENT_ABSENT_ALL_DAY:
                LOGGER.info(
                    "[day_scenario] user is absent all day (persona=%s)", persona_id,
                )
                continue
            scheduler.schedule(
                fire_at=self._combine(plan_day, ue.at),
                callback=self._make_user_event_callback(manager, persona_id, ue, result),
                key=f"scenario:{persona_id}:user_event:{i}",
            )

        for i, ev in enumerate(sc.events):
            scheduler.schedule(
                fire_at=self._combine(plan_day, ev.at),
                callback=self._make_world_event_callback(manager, persona_id, ev, result),
                key=f"scenario:{persona_id}:event:{i}",
            )

        scheduler.schedule(
            fire_at=sleep_dt,
            callback=lambda: self._judge(
                manager, persona_id, KIND_DAY_CLOSE, {}, result,
            ),
            key=f"scenario:{persona_id}:day_close",
        )

    def _make_user_event_callback(
        self, manager: Any, persona_id: str, ue: UserEvent, result: ScenarioRunResult,
    ) -> Callable[[], None]:
        def _callback() -> None:
            if ue.type == USER_EVENT_MESSAGE:
                self.user_event_driver.begin_conversation(manager, persona_id, ue.text)
            elif ue.type == USER_EVENT_LEAVE:
                had_exchange = self.user_event_driver.conversation_had_exchange(
                    manager, persona_id,
                )
                ended = self.user_event_driver.end_conversation(manager, persona_id)
                if not ended:
                    return
                if had_exchange:
                    # 会話終了 = 会話終了判断の発火点 (intent §4.2)
                    self._judge(manager, persona_id, KIND_POST_CONVERSATION, {}, result)
                else:
                    # 1 往復も成立しなかった会話 (応答生成の失敗等) では判断を
                    # 撃たない。「会話がひと区切りつきました」という前提が偽の
                    # 状況テキストは、存在しない会話の振り返り (作話) を誘発する
                    # (接地原則 v2 §3-1。2026-07-05 実 LLM シムで実証)。
                    LOGGER.warning(
                        "[day_scenario] conversation ended with zero exchange "
                        "(persona=%s); skipping post_conversation judgment",
                        persona_id,
                    )
                    result.judgments.append({
                        "kind": KIND_POST_CONVERSATION,
                        "at": clock.now().isoformat(timespec="seconds"),
                        "submitted": False,
                        "reason": "conversation had no exchange; judgment skipped",
                    })
        return _callback

    def _make_world_event_callback(
        self, manager: Any, persona_id: str, ev: WorldEvent, result: ScenarioRunResult,
    ) -> Callable[[], None]:
        def _callback() -> None:
            self._judge(manager, persona_id, KIND_ON_EVENT, {
                "event_text": ev.description,
                "is_alert": ev.is_alert,
            }, result)
        return _callback
