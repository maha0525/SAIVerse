"""MetaLayer: ペルソナの行動 Track の選択・切り替えを判断する観察視点。

Intent A v0.9 / Intent B v0.6 で導入された Phase C-1 の最小実装に、
Phase 1.2 (Intent A v0.14, Intent B v0.11) で **Playbook 経由の判断 path** を
追加した二刀流構成。Phase C-2 完成 (2026-04-30) で Playbook path を既定に昇格:

- 既定 (Playbook path): ``meta_judgment.json`` を runtime に投げ、ペルソナが
  内的独白の中で /spell track_pause / track_activate / track_create 等を発動
  することで Track 操作を行う。重量級モデルのメインキャッシュに JSON を混入
  させないため、構造化出力 (response_schema) は使わない (Intent A v0.9 不変条件 11)。
- 緊急避難 (legacy path): 重量級モデルへ直接プロンプトを渡しスペル抽出ループ
  (Playbook path と同じスペル方式だが、Playbook 化されていない短命経路)

切り替えは環境変数 ``SAIVERSE_META_LAYER_USE_PLAYBOOK`` で行う:

- 未設定 / ``"1"`` / ``"true"`` / ``"yes"`` / ``"on"``: Playbook path (既定)
- ``"0"`` / ``"false"`` / ``"no"`` / ``"off"``: legacy path (緊急避難)

責務:
- TrackManager の alert observer として登録され、alert 遷移を契機に起動する
- 上記の path に従って判断を実行
- LLM コール時は (legacy) tools / response_schema を渡さない
  Playbook path 側は meta_judgment.json の response_schema に従う

責務外:
- メインライン応答 (発話生成) の起動。これは呼び出し元 (Handler) が責任を持つ
- Track の作成 / 状態遷移ロジック (TrackManager / dispatch ツールに委譲)
- Track 内必要情報の維持・再開コンテキスト構築は Track Chronicle 機構が担う
  (Metabolism 連動、`sea/runtime.py:_generate_track_chronicle` + `_promote_meta_judgment_in_pulse`
  の延長で切り替え時挿入。詳細: docs/intent/persona_cognition/track_chronicle.md)

詳細: docs/intent/persona_cognition/ (Intent doc 群)
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .track_manager import (
    LIVE_STATUSES,
    STATUS_ALERT,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_UNSTARTED,
    TrackManager,
)


# 安全網: スペルループの最大回数。LLM が暴走した時に無限ループを防ぐ。
_MAX_SPELL_LOOPS = 5

# メタレイヤーが扱う Track 操作スペル (Phase B-3 で導入済み)。
# 一覧は登録済みスペルから動的に拾うが、ペルソナ素体プロンプトと衝突しないよう
# 「メタレイヤーが使ってよい」セットを明示的に絞る。
def _short_ref(track: Any) -> str:
    """Track の短縮参照 (t:N) を返す。short_id 未設定時は UUID[:8] フォールバック。"""
    if getattr(track, "short_id", None) is not None:
        return f"t:{track.short_id}"
    return track.track_id[:8] + "…"


_META_LAYER_SPELL_NAMES = (
    "track_create",
    "track_activate",
    "track_pause",
    "track_complete",
    "track_abort",
    "track_list",
    "note_create",
    "note_open",
    "note_close",
    "note_search",
)


class MetaLayer:
    """ペルソナごとの「メタレイヤー」役。

    現状ペルソナ単位ではなくマネージャー単位で 1 インスタンス保持し、
    内部で persona_id を引き回す形にしている (内部状態を持たないため共有可能)。
    """

    def __init__(self, manager: Any):
        """
        Args:
            manager: SAIVerseManager 参照。persona 取得 (manager.personas) と
                track_manager 参照に使う。
        """
        self.manager = manager
        self.track_manager: TrackManager = manager.track_manager

        # ペルソナごとの直列化 Lock (Intent A v0.9 不変条件 11: メタ判断は
        # ペルソナの「思考の流れ」1 本)。
        #
        # メタ判断は 2 経路で起動される:
        #   - alert observer (chat thread 等から `on_track_alert`)
        #   - 定期 tick (AutonomyManager background thread から `on_periodic_tick`)
        #
        # 両者が別 thread で同時に起動すると、それぞれが独立した snapshot を見て
        # Track 操作を発動するため、「pending と思って pause したら裏で alert に
        # なっていた」のような事故が発生する。1 ペルソナにつきメタ判断 Pulse は
        # 同時 1 本のため、入口で per-persona Lock を取って直列化する。
        #
        # 競合時は **wait** で正しい (skip しない):
        #   - alert を skip すると即応すべき外部イベントを取りこぼす
        #   - 定期 tick を skip するとメインキャッシュ TTL 切れを誘発する
        # 別ペルソナ同士は並行できる (ペルソナごとに独立した Lock)。
        #
        # `_persona_locks` の dict 自体への並行アクセスは `_locks_guard` で保護する。
        # set_alert は judgment ループ中の deferred ops からは発火しない (UI / chat /
        # internal_alert_poller など外部経路のみ) ため、再入の現実的リスクはなし。
        # 素の `threading.Lock` で十分。
        self._persona_locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

        # Phase 4 案C: メタ判断 Pulse の連続失敗回数 (persona_id -> count)。
        # リトライ枯渇のたびに加算、成功でリセット。`max_consecutive_failures`
        # に達したら ACTIVITY_STATE を Idle に降格して自律稼働の無音スピンを止め、
        # host イベントで通知する (phase4_meta_judgment_recovery.md 案C)。
        # Lock 入口 (_get_lock) と同じく per-persona 直列化下で触るので素の dict で十分。
        self._consecutive_failures: Dict[str, int] = {}

    def _get_lock(self, persona_id: str) -> threading.Lock:
        """persona_id の Lock を返す (なければ作成)。"""
        with self._locks_guard:
            lock = self._persona_locks.get(persona_id)
            if lock is None:
                lock = threading.Lock()
                self._persona_locks[persona_id] = lock
            return lock

    # ------------------------------------------------------------------
    # Phase 4-e: Per-persona meta-judgment Pulse configuration
    # ------------------------------------------------------------------

    # AI.META_JUDGMENT_CONFIG の既定値。NULL カラム / 不正 JSON / キー欠落の
    # フォールバック先。将来 Pattern A/B/C 推定でモデル種別ごとに分岐させる
    # 余地を残し、ここでは v0.3.0 ベースラインの単一既定として置く。
    _DEFAULT_JUDGMENT_CONFIG: Dict[str, Any] = {
        "cache_threshold_ratio": 0.3,     # キャッシュ TTL 残り 30% で前倒し tick 発火
        "max_retries": 1,                 # Pulse 失敗時の即時リトライ最大回数
        "retry_backoff_seconds": 5,       # リトライ間の待機秒数
        "max_consecutive_failures": 3,    # 連続失敗がこの回数に達したら Idle 降格 + 通知 (案C)
        "periodic_interval_minutes": 50,  # メタ判断 Pulse の自動発話間隔 (旧 AutonomyManager.interval_minutes)
        "keep_cache_alive": True,         # True: cache TTL 接近で前倒し fire / False: TTL 無視 (低頻度ペルソナ向け)
        # 自律 Track の Pulse 間隔 (秒) のペルソナ単位デフォルト。Track metadata
        # の pulse_interval_seconds が個別上書き (優先順: Track metadata > ここ >
        # AutonomousTrackHandler.default_pulse_interval)。ライフビューの
        # 「作業のテンポ」設定の永続化先 (persona_activity_view.md §7)。
        "autonomous_pulse_interval_seconds": 30,
        # 開発者モード用デバッグフラグ: True なら meta_judgment を毎回強制失敗させる。
        # ① メタ判断リカバリ (連続失敗→Idle 降格+通知) の実機検証用。本物の API 失敗
        # を待たずに失敗経路を再現する。UI はペルソナ設定で開発者モード限定で出す。
        "force_fail": False,
    }

    def _load_judgment_config(self, persona: Any) -> Dict[str, Any]:
        """ペルソナの META_JUDGMENT_CONFIG を読み、既定値で穴埋めして返す。

        AI.META_JUDGMENT_CONFIG (Text/JSON) を毎回 DB から読み出し、
        ``_DEFAULT_JUDGMENT_CONFIG`` にマージする。NULL / 不正 JSON / 型不一致
        は警告ログを残しつつ既定値で穴埋めする。

        UI 編集を即時反映させたいため、結果のキャッシュは行わない (CHRONICLE
        系の参照頻度と同程度なので影響は軽微)。tick ループ内で頻繁に呼ばれる
        ようになった場合のみキャッシュ化を検討する。
        """
        config = dict(self._DEFAULT_JUDGMENT_CONFIG)

        persona_id = getattr(persona, "persona_id", None)
        if not persona_id or not self.manager or not hasattr(self.manager, "SessionLocal"):
            return config

        raw: Optional[str] = None
        try:
            from database.models import AI
            db = self.manager.SessionLocal()
            try:
                ai_row = db.query(AI).filter_by(AIID=persona_id).first()
                raw = ai_row.META_JUDGMENT_CONFIG if ai_row else None
            finally:
                db.close()
        except Exception:
            logging.warning(
                "[meta-layer] Failed to read META_JUDGMENT_CONFIG for %s; using defaults",
                persona_id, exc_info=True,
            )
            return config

        if not raw:
            return config

        try:
            override = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(override, dict):
                raise ValueError(f"expected dict, got {type(override).__name__}")
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            logging.warning(
                "[meta-layer] Invalid META_JUDGMENT_CONFIG JSON for %s: %s; using defaults",
                persona_id, exc,
            )
            return config

        for key, default in self._DEFAULT_JUDGMENT_CONFIG.items():
            if key not in override:
                continue
            value = override[key]
            # bool は int の subclass なので明示除外。default が int/float のときに
            # bool が紛れ込むのを防ぐ。
            if isinstance(default, bool):
                expected_types: Tuple[type, ...] = (bool,)
            elif isinstance(default, int) and not isinstance(default, bool):
                expected_types = (int,)
            elif isinstance(default, float):
                expected_types = (int, float)  # int → float 昇格は許容
            else:
                expected_types = (type(default),)
            if isinstance(value, expected_types) and not (
                not isinstance(default, bool) and isinstance(value, bool)
            ):
                config[key] = float(value) if isinstance(default, float) else value
            else:
                logging.warning(
                    "[meta-layer] META_JUDGMENT_CONFIG.%s wrong type for %s "
                    "(expected %s, got %s); using default %r",
                    key, persona_id,
                    "/".join(t.__name__ for t in expected_types),
                    type(value).__name__, default,
                )

        return config

    # ------------------------------------------------------------------
    # Phase 4-e: should_fire — 集約判定 API (EventScheduler から呼ばれる)
    # ------------------------------------------------------------------

    def should_fire(
        self,
        persona_id: str,
        trigger_kind: str,
    ) -> Optional[Dict[str, Any]]:
        """指定ペルソナにメタ判断 Pulse を発火すべきかを再評価する。

        EventScheduler の callback (TTL 接近 / interval 経過 / Active 化など) から
        呼ばれる。callback が schedule された時点と発火時点で状態が変わっている
        可能性があるため、発火直前にここで再評価する。

        Args:
            persona_id: 対象ペルソナ ID。
            trigger_kind: 発火源を表す文字列 ("ttl" / "interval" / "active" /
                "schedule" / "alert_recovery" など)。返り値の trigger context に
                埋め込む。

        Returns:
            発火すべきなら trigger context dict、不要なら None。

        判定ルール (intent A v0.9 / v0.10 を踏襲):
        1. ペルソナが見つからない / ACTIVITY_STATE != Active → None
        2. 現在 running の Track の Handler が ``post_complete_behavior=='wait_response'``
           → None (相手の応答待ち中はメタ判断を割り込ませない)
        3. 上記をクリアしたら ``{"trigger": trigger_kind}`` を返す

        判定はあくまで「発火していい状況か」のチェック。「発火すべきタイミングか」
        (TTL 残り計算、interval 経過判定など) は schedule する側が事前に行う。
        ここで二重に時刻を計算しない (judgment 完了直後の状態が反映されない race
        があるため)。
        """
        persona = self._lookup_persona(persona_id)
        if persona is None:
            logging.debug(
                "[meta-layer] should_fire: persona not found: %s (trigger=%s)",
                persona_id, trigger_kind,
            )
            return None

        activity_state = getattr(persona, "activity_state", "Idle")
        if activity_state != "Active":
            logging.debug(
                "[meta-layer] should_fire: skipped (activity_state=%s != Active): persona=%s trigger=%s",
                activity_state, persona_id, trigger_kind,
            )
            return None

        running_track = self._get_running_track(persona_id)
        if running_track is not None:
            handler = self._get_handler_for_track(running_track)
            behavior = getattr(handler, "post_complete_behavior", None) if handler else None
            if behavior == "wait_response":
                logging.debug(
                    "[meta-layer] should_fire: skipped (running Track wait_response): persona=%s track=%s trigger=%s",
                    persona_id, getattr(running_track, "track_id", "?"), trigger_kind,
                )
                return None

        return {"trigger": trigger_kind}

    # ------------------------------------------------------------------
    # alert observer エントリ
    # ------------------------------------------------------------------

    def on_track_alert(
        self, persona_id: str, alert_track_id: str, context: Dict[str, Any]
    ) -> None:
        """TrackManager.add_alert_observer に渡される callback。

        失敗時も例外を上げず WARN ログのみ (observer の障害が呼び出し元の
        状態遷移処理を巻き込まないため、TrackManager 側の _notify_alert で
        も二重に保護されている)。

        Phase 1.2: ``SAIVERSE_META_LAYER_USE_PLAYBOOK`` が真なら ``meta_judgment``
        Playbook 経由で判断する。それ以外は legacy direct-LLM スペル loop。

        メタ判断 v2 (intent: meta_judgment_structured.md): 状況 A
        (preempt_collision、context.target_already_running == True) は
        ここで早期 return する。状態遷移は no-op、メインライン側で応答処理が
        独立に走るため、判断 Pulse を呼ぶ意味がない (汚染源を残すだけ)。

        per-persona Lock で直列化する (`__init__` 参照)。同ペルソナの判断 Pulse
        が走行中なら完了まで wait する。
        """
        # メタ判断 v2 状況 A: 自律先制と外部 alert の衝突 → 発火させない
        if context.get("target_already_running"):
            logging.info(
                "[meta-layer] Skipping judgment: preempt_collision (target_already_running=True): "
                "persona=%s track=%s",
                persona_id, alert_track_id,
            )
            return

        lock = self._get_lock(persona_id)
        wait_started = time.monotonic()
        with lock:
            wait_elapsed = time.monotonic() - wait_started
            if wait_elapsed >= 0.5:
                logging.info(
                    "[meta-layer] Lock acquired after %.1fs wait: persona=%s alert_track=%s trigger=%s",
                    wait_elapsed, persona_id, alert_track_id, context.get("trigger"),
                )
            try:
                persona = self._lookup_persona(persona_id)
                if persona is None:
                    logging.warning(
                        "[meta-layer] persona not found for alert: persona_id=%s track=%s",
                        persona_id, alert_track_id,
                    )
                    return
                logging.info(
                    "[meta-layer] Judgment starting: persona=%s alert_track=%s trigger=%s",
                    persona_id, alert_track_id, context.get("trigger"),
                )
                self._run_judgment_via_playbook(persona, alert_track_id, context)
            except Exception:
                logging.exception(
                    "[meta-layer] Judgment failed: persona=%s track=%s",
                    persona_id, alert_track_id,
                )

    # ------------------------------------------------------------------
    # Phase C-2: 定期 tick エントリ (intent A v0.10 / intent B v0.7 §"メタレイヤーの定期実行入口")
    # ------------------------------------------------------------------

    def on_periodic_tick(
        self,
        persona_id: str,
        context: Optional[Dict[str, Any]] = None,
        force: bool = False,
    ) -> None:
        """定期実行で呼ばれる入口。alert と同じ判断ループを共有する。

        違いは context のみ:
        - alert 入口: ``context = {"trigger": "user_utterance", ...}`` 等
        - 定期入口:  ``context = {"trigger": "periodic_tick", ...}``

        intent A v0.9 の ACTIVITY_STATE 表に従い、Active 以外のペルソナでは
        発火しない。加えて、現在 running の Track の Handler が
        ``post_complete_behavior == 'wait_response'`` を持つ場合、相手の応答待ち
        中にメタ判断を割り込ませると自然な対話を壊すため、抑止する
        (intent B v0.8 §"post_complete_behavior 列挙")。

        per-persona Lock で `on_track_alert` と直列化する。alert 経由判断が
        走行中なら完了まで wait する (skip しない: TTL 切れを避けるため)。
        """
        lock = self._get_lock(persona_id)
        wait_started = time.monotonic()
        with lock:
            wait_elapsed = time.monotonic() - wait_started
            if wait_elapsed >= 0.5:
                logging.info(
                    "[meta-layer] Lock acquired after %.1fs wait: persona=%s trigger=periodic_tick",
                    wait_elapsed, persona_id,
                )
            try:
                persona = self._lookup_persona(persona_id)
                if persona is None:
                    return

                # ACTIVITY_STATE 抑止: Active のみ定期発火 (intent A v0.9 表)
                activity_state = getattr(persona, "activity_state", "Idle")
                if activity_state != "Active" and not force:
                    logging.debug(
                        "[meta-layer] periodic tick skipped (activity_state=%s != Active): persona=%s",
                        activity_state, persona_id,
                    )
                    return

                # 候補補充 Track は起動時の _on_persona_registered で全ペルソナへ
                # 常設済み (autonomous_desire.md §11)。ここでの ensure は撤去した。

                # game Region 参加中はメタ判断・自律行動を全停止する
                # (ペルソナ都合の離脱を構造的に防ぐ。temp/region_rpg_intent.md §D-1)
                lifecycle = getattr(self.manager, "game_lifecycle", None)
                if (
                    lifecycle is not None
                    and not force
                    and lifecycle.is_participating(persona_id)
                ):
                    logging.debug(
                        "[meta-layer] periodic tick skipped (participating in game session): persona=%s",
                        persona_id,
                    )
                    return

                # post_complete_behavior 抑止: 応答待ち型の Track は割り込まない
                running_track = self._get_running_track(persona_id)
                if running_track is not None:
                    handler = self._get_handler_for_track(running_track)
                    behavior = getattr(handler, "post_complete_behavior", None) if handler else None
                    if behavior == "wait_response" and not force:
                        logging.debug(
                            "[meta-layer] periodic tick skipped (running Track wait_response): persona=%s track=%s",
                            persona_id, getattr(running_track, "track_id", "?"),
                        )
                        return

                merged_context = {"trigger": "periodic_tick"}
                if context:
                    merged_context.update(context)

                logging.info(
                    "[meta-layer] Periodic tick starting: persona=%s context=%s",
                    persona_id, merged_context.get("trigger"),
                )
                # alert_track_id="" は intent B 通り (定期 tick の場合は空文字列も可)
                self._run_judgment_via_playbook(persona, "", merged_context)
            except Exception:
                logging.exception(
                    "[meta-layer] Periodic tick failed: persona=%s", persona_id,
                )

    def _get_running_track(self, persona_id: str) -> Optional[Any]:
        """Return the persona's currently-running ActionTrack, or None."""
        try:
            from database.models import ActionTrack
            db = self.manager.SessionLocal()
            try:
                return (
                    db.query(ActionTrack)
                    .filter(
                        ActionTrack.persona_id == persona_id,
                        ActionTrack.status == "running",
                    )
                    .first()
                )
            finally:
                db.close()
        except Exception:
            logging.exception("[meta-layer] Failed to read running track for %s", persona_id)
            return None

    def _get_handler_for_track(self, track: Any) -> Optional[Any]:
        """Resolve the Handler instance responsible for the given Track."""
        from sea.pulse_root_context import get_handler_for_track
        return get_handler_for_track(self.manager, track)

    # ------------------------------------------------------------------
    # Phase 1.2: Playbook-based judgment dispatch
    # ------------------------------------------------------------------

    # メタ判断 v2 状況 → Playbook 名のマッピング
    _SITUATION_PLAYBOOK_MAP = {
        "life_purpose_unset": "meta_judgment_life_purpose",
        "alert_present": "meta_judgment_alert",
        "running_active": "meta_judgment_running",
        "idle_with_pending": "meta_judgment_idle_pending",
        "idle_no_pending": "meta_judgment_idle_empty",
    }

    def _is_life_purpose_unset(self, persona_id: str) -> bool:
        """ペルソナの LIFE_PURPOSE が未設定か (autonomous_desire.md §4/§10)。

        DB 読み取り失敗時は「設定済み」とみなす (= 目的設定を強制しない)。
        自律行動を阻害しない安全側のフォールバック。
        """
        if not self.manager or not hasattr(self.manager, "SessionLocal"):
            return False
        try:
            from saiverse.life_purpose import get_life_purpose
            return get_life_purpose(self.manager.SessionLocal, persona_id) is None
        except Exception:
            logging.warning(
                "[meta-layer] failed to read LIFE_PURPOSE for %s; treating as set",
                persona_id, exc_info=True,
            )
            return False

    def _run_judgment_via_playbook(
        self,
        persona: Any,
        alert_track_id: str,
        context: Dict[str, Any],
    ) -> None:
        """Dispatch to the situation-specific meta_judgment v2 Playbook.

        メタ判断 v2 (intent: meta_judgment_structured.md):
        - `_classify_situation` で状況 (B/C/D/E) を判定
        - 状況に応じた専用 Playbook を選択
        - response_schema を Python で動的に組み立て (enum 候補に track ID を埋め込む)
        - args に situation_text + response_schema + 補助情報を詰めて runtime に投げる
        - Playbook 内の judge ノードが構造化出力を生成 → finalize ツールが
          Spell 整形・実行・SAIMemory 書き込みを行う
        """
        # pulse_dispatch.md §6.3 案 A: メタ判断は PulseController 経由で
        # 並列レーンとして起動する (旧: runtime.run_meta_user 直叩き)。
        pulse_controller = getattr(self.manager, "pulse_controller", None)
        if pulse_controller is None:
            logging.warning(
                "[meta-layer] No pulse_controller on manager — cannot run meta_judgment Playbook; "
                "falling back to legacy path"
            )
            self._run_judgment(persona, alert_track_id, context)
            return

        building_id = getattr(persona, "current_building_id", None)
        if not building_id:
            logging.warning(
                "[meta-layer] persona %s has no current_building_id — cannot run meta_judgment Playbook",
                persona.persona_id,
            )
            return

        # Race 防護: Playbook 起動直前にもう一度状況分類する (intent v2 §9)。
        # callback が schedule された時点と発火時点の間に Track 状態が変わって
        # いれば、ここで別の Playbook に切り替えるか、A 相当なら return する。
        sit = self._classify_situation(persona.persona_id, context or {})
        kind = sit["kind"]

        if kind == "preempt_collision":
            logging.info(
                "[meta-layer] Skipping judgment at dispatch (preempt_collision detected): "
                "persona=%s",
                persona.persona_id,
            )
            return

        playbook_name = self._SITUATION_PLAYBOOK_MAP.get(kind)
        if playbook_name is None:
            logging.warning(
                "[meta-layer] Unknown situation kind=%r; cannot dispatch Playbook (persona=%s)",
                kind, persona.persona_id,
            )
            return

        # Race 防護: enum 候補が空のケース (intent v2 §9)。
        # alert があったが起動直前に消えた、pending があったが消えた等。
        if kind == "alert_present" and not sit["alert"]:
            logging.warning(
                "[meta-layer] alert_present situation but no alert tracks at dispatch; aborting (persona=%s)",
                persona.persona_id,
            )
            return
        if kind == "running_active" and not sit["running"]:
            logging.warning(
                "[meta-layer] running_active situation but no running track at dispatch; aborting (persona=%s)",
                persona.persona_id,
            )
            return
        if kind == "idle_with_pending" and not sit["pending_or_unstarted"]:
            logging.warning(
                "[meta-layer] idle_with_pending but no pending tracks at dispatch; aborting (persona=%s)",
                persona.persona_id,
            )
            return

        # Serialize trigger context to JSON so the Playbook input_schema can
        # consume it as a single string. Drop non-serializable bits defensively.
        try:
            trigger_context_json = json.dumps(
                {k: v for k, v in (context or {}).items() if isinstance(v, (str, int, float, bool, list, dict, type(None)))},
                ensure_ascii=False,
            )
        except (TypeError, ValueError):
            trigger_context_json = "{}"

        # 過去 N 件の判断ログを注入 (Phase 2)。空文字列ならプロンプトでも空に展開される。
        recent_judgments_block = self._build_recent_judgments_block(persona.persona_id)

        # 状況テキスト (Track 状態と trigger を踏まえた状況提示 + Track 一覧)。
        # 判断材料として judge プロンプトに展開される。
        situation_text = self._build_situation_text(
            persona.persona_id, alert_track_id or "", context or {}
        )

        # 動的 response_schema (kind と現状の候補 ID 一覧から組み立て)。
        response_schema = self._build_response_schema(kind, sit)

        running_track_id = (
            sit["running"][0].track_id if sit["running"] else ""
        )
        trigger_type = str((context or {}).get("trigger") or "")

        args: Dict[str, Any] = {
            "situation_text": situation_text,
            "trigger_context": trigger_context_json,
            "recent_judgments": recent_judgments_block,
            "response_schema": response_schema,
            "running_track_id": running_track_id,
            "trigger_type": trigger_type,
            "track_at_judgment_id": alert_track_id or "",
        }

        # Phase 4-e: ペルソナ別 retry 設定
        judgment_config = self._load_judgment_config(persona)
        try:
            max_retries = max(0, int(judgment_config.get("max_retries", 1)))
        except (TypeError, ValueError):
            max_retries = 1
        try:
            retry_backoff_seconds = max(0, int(judgment_config.get("retry_backoff_seconds", 5)))
        except (TypeError, ValueError):
            retry_backoff_seconds = 5

        # 試行回数 = max_retries + 1 (max_retries=0 ならリトライなしで 1 回だけ)
        last_failure_reason: Optional[str] = None
        for attempt in range(max_retries + 1):
            if attempt > 0:
                logging.info(
                    "[meta-layer] meta_judgment retry attempt %d/%d after %ds backoff: persona=%s alert_track=%s",
                    attempt, max_retries, retry_backoff_seconds,
                    persona.persona_id, alert_track_id,
                )
                # per-persona Lock を保持したまま sleep する。並行 alert/tick は
                # その間 wait される (Lock acquired after %.1fs wait のログで観測可能)。
                time.sleep(retry_backoff_seconds)

            # 開発者モード: force_fail 時はここで失敗扱いにして submit せず continue。
            # リトライ枯渇→連続失敗カウント→Idle 降格+通知の経路を本物同様に通す。
            if judgment_config.get("force_fail"):
                last_failure_reason = "forced failure (developer mode: force_fail)"
                logging.warning(
                    "[meta-layer] meta_judgment FORCED FAILURE (developer mode) "
                    "attempt %d/%d: persona=%s alert_track=%s",
                    attempt + 1, max_retries + 1,
                    persona.persona_id, alert_track_id,
                )
                continue

            captured_errors: List[Dict[str, Any]] = []

            def _capture_event(
                ev: Dict[str, Any],
                _errors: List[Dict[str, Any]] = captured_errors,
            ) -> None:
                if isinstance(ev, dict) and ev.get("type") == "error":
                    _errors.append(ev)

            try:
                # pulse_dispatch.md §6.3 案 A: PulseController 経由で並列レーン起動
                pulse_controller.submit_meta_judgment(
                    persona_id=persona.persona_id,
                    building_id=building_id,
                    meta_playbook=playbook_name,
                    args=args,
                    event_callback=_capture_event,
                )
            except Exception as exc:
                last_failure_reason = f"runtime exception: {exc!r}"
                logging.warning(
                    "[meta-layer] meta_judgment Playbook raised on attempt %d/%d: "
                    "persona=%s alert_track=%s error=%r",
                    attempt + 1, max_retries + 1,
                    persona.persona_id, alert_track_id, exc,
                )
                continue

            if captured_errors:
                last_failure_reason = f"playbook errors: {captured_errors}"
                for err in captured_errors:
                    logging.warning(
                        "[meta-layer] meta_judgment Playbook emitted error on attempt %d/%d: "
                        "persona=%s alert_track=%s error=%s",
                        attempt + 1, max_retries + 1,
                        persona.persona_id, alert_track_id, err,
                    )
                continue

            # 成功 — 連続失敗カウンタをリセット
            self._consecutive_failures.pop(persona.persona_id, None)
            logging.info(
                "[meta-layer] meta_judgment Playbook completed: persona=%s alert_track=%s attempts=%d",
                persona.persona_id, alert_track_id, attempt + 1,
            )
            return

        # 全リトライ枯渇
        logging.warning(
            "[meta-layer] meta_judgment Playbook exhausted retries (%d attempts): "
            "persona=%s alert_track=%s last_error=%s. Next judgment will be triggered "
            "by EventScheduler (cache TTL or interval).",
            max_retries + 1,
            persona.persona_id, alert_track_id, last_failure_reason,
        )

        # 案C: 連続失敗を加算。閾値到達で ACTIVITY_STATE を Idle に降格 + 通知。
        consecutive = self._consecutive_failures.get(persona.persona_id, 0) + 1
        self._consecutive_failures[persona.persona_id] = consecutive
        try:
            max_consecutive_failures = max(
                1, int(judgment_config.get("max_consecutive_failures", 3))
            )
        except (TypeError, ValueError):
            max_consecutive_failures = 3
        if consecutive >= max_consecutive_failures:
            self._handle_persistent_failure(
                persona, building_id, consecutive, last_failure_reason
            )
            # 降格後はカウンタをリセット (再 Active 化したらまた 0 から数える)
            self._consecutive_failures.pop(persona.persona_id, None)

    def _handle_persistent_failure(
        self,
        persona: Any,
        building_id: str,
        consecutive: int,
        last_failure_reason: Optional[str],
    ) -> None:
        """連続失敗が閾値に達したとき、自律稼働を止めてまはーに気づかせる (案C)。

        - ACTIVITY_STATE を Idle に降格する (定期 tick が止まり、無音スピンを断つ)。
        - host イベントで Building に通知する (event_message タグでペルソナ + UI に届く)。
        どちらかが失敗しても他方は試みる (best-effort)。
        """
        persona_id = persona.persona_id
        logging.warning(
            "[meta-layer] meta_judgment failed %d times consecutively for persona=%s; "
            "downgrading ACTIVITY_STATE to Idle and notifying. last_error=%s",
            consecutive, persona_id, last_failure_reason,
        )
        # 1. ACTIVITY_STATE を Idle に降格 (DB + in-memory)。
        try:
            db = self.manager.SessionLocal()
            try:
                from database.models import AI as AIModel
                ai = db.query(AIModel).filter(AIModel.AIID == persona_id).first()
                if ai is not None:
                    ai.ACTIVITY_STATE = "Idle"
                    db.commit()
            finally:
                db.close()
            persona.activity_state = "Idle"
        except Exception:
            logging.exception(
                "[meta-layer] Failed to downgrade ACTIVITY_STATE for persona=%s", persona_id,
            )
        # 2. host イベントで通知 (best-effort)。
        try:
            self.manager.add_building_event(
                building_id,
                {
                    "role": "host",
                    "content": (
                        '<div class="note-box">⚠️ メタ判断が連続して失敗したため、'
                        f'自律行動を一時停止しました（{consecutive} 回連続失敗）。<br>'
                        'ライフビューから再開できます。</div>'
                    ),
                    "metadata": {
                        "event": {
                            "type": "meta_judgment_failure",
                            "consecutive": consecutive,
                        }
                    },
                },
                heard_by=[persona_id],
            )
        except Exception:
            logging.exception(
                "[meta-layer] Failed to emit meta_judgment failure event for persona=%s",
                persona_id,
            )

    # ------------------------------------------------------------------
    # 判断ループ (LLM + スペル)
    # ------------------------------------------------------------------

    def _run_judgment(
        self,
        persona: Any,
        alert_track_id: str,
        context: Dict[str, Any],
    ) -> None:
        """重量級モデルでメタ判断 LLM を呼ぶ → スペル実行 → ループ。

        スペルなし応答で自然停止。

        判断中に発火された Track 操作スペルは Pulse 完了時に一括適用される
        (Intent A v0.14 / Intent B v0.11 の deferred 機構)。MetaLayer は
        通常の Playbook ランタイムを通らないため、ここで PulseContext を
        手動で生成してスペルに渡し、判断ループ終了時に
        ``_apply_deferred_track_ops`` を呼ぶ必要がある。Phase 1 で
        MetaLayer を Playbook 化した時点でこの手動配線は不要になる。
        """
        llm_client = self._get_heavyweight_client(persona)
        if llm_client is None:
            logging.warning(
                "[meta-layer] No LLM client available for persona=%s; skipping judgment",
                persona.persona_id,
            )
            return

        # Track-mutating spells を deferred 化するために PulseContext を発行する。
        # 通常の Playbook ランタイムが作るものとは別経路 (= runtime._pulse_contexts
        # キャッシュには登録しない、flush_pulse_logs もしない短命なもの)。
        import uuid

        from sea.pulse_context import PulseContext

        adapter = getattr(persona, "sai_memory", None)
        thread_id = adapter.get_current_thread() if adapter else None
        pulse_ctx = PulseContext(
            pulse_id=str(uuid.uuid4()), thread_id=thread_id or ""
        )

        # Phase 2: meta_judgment_log への書き込み用の蓄積バッファ。
        # legacy path は runtime を経由しないため、ここで自前で集める。
        thought_parts: List[str] = []
        spells_record: List[Dict[str, Any]] = []
        prompt_snapshot_text: Optional[str] = None
        try:
            system_prompt = self._build_system_prompt(persona)
            user_message = self._build_state_message(
                persona.persona_id, alert_track_id, context
            )
            recent_judgments_block = self._build_recent_judgments_block(
                persona.persona_id
            )
            if recent_judgments_block:
                # 過去ログは現状状態の隣に並べる (judge は両方を踏まえて判断する)
                user_message = f"{user_message}\n\n{recent_judgments_block}"
            prompt_snapshot_text = user_message[:2000]  # 先頭 2K でデバッグ用に十分
            messages: List[Dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ]

            for loop in range(_MAX_SPELL_LOOPS):
                # tools / response_schema は意図的に渡さない (Intent A v0.9 / 設計議論 2026-04-28)
                try:
                    response = llm_client.generate(messages)
                except Exception:
                    logging.exception(
                        "[meta-layer] LLM generate failed at loop=%d persona=%s",
                        loop, persona.persona_id,
                    )
                    return

                text = response if isinstance(response, str) else str(response)
                thought_parts.append(text)
                logging.info(
                    "[meta-layer] LLM response (loop=%d, persona=%s): %s",
                    loop, persona.persona_id, text[:300],
                )

                spells = self._extract_spells(text)
                if not spells:
                    # スペルなし → 自然停止。最終応答テキストはペルソナの「思考」として残す
                    logging.info(
                        "[meta-layer] Natural stop after %d loop(s) persona=%s",
                        loop, persona.persona_id,
                    )
                    return

                # スペル実行 (PulseContext を渡して Track 操作スペルを deferred 化する)
                results = self._execute_spells(persona, spells, pulse_ctx)
                # results は List[Tuple[str, str]] = (name, result_str)
                for (spell_name, spell_args), (_result_name, result_str) in zip(spells, results):
                    spells_record.append({
                        "name": spell_name,
                        "args": dict(spell_args),
                        "result": result_str,
                    })

                # 次ターンに向けて assistant 応答 + ツール結果を append
                messages.append({"role": "assistant", "content": text})
                results_text = self._format_spell_results(results)
                messages.append({"role": "user", "content": results_text})

            logging.warning(
                "[meta-layer] Hit max spell loops (%d) without natural stop persona=%s",
                _MAX_SPELL_LOOPS, persona.persona_id,
            )
        finally:
            # 判断ループ終了時 (例外含む) に deferred Track 操作を apply する。
            # _apply_deferred_track_ops は同じ helper を runtime_graph.py が
            # 通常 Pulse 完了時に呼んでおり、MetaLayer もこれを共有する。
            committed_to_main = False
            try:
                from sea.runtime_runner import _apply_deferred_track_ops
                committed_to_main = any(
                    op.op_type == "activate" for op in pulse_ctx.deferred_track_ops
                )
                _apply_deferred_track_ops(
                    {"_pulse_context": pulse_ctx}, persona
                )
            except Exception:
                logging.exception(
                    "[meta-layer] Failed to apply deferred track ops for persona=%s",
                    persona.persona_id,
                )

            # Phase 2: meta_judgment_log に判断ターンを永続化する。
            # 例外で握り潰さない (この記録が次回判断の参考情報になるため、
            # 失敗が分かるよう WARN 以上で残す)。
            try:
                self._record_judgment_log(
                    persona_id=persona.persona_id,
                    trigger_type=str(context.get("trigger") or "unknown"),
                    trigger_context=json.dumps(
                        {
                            k: v for k, v in (context or {}).items()
                            if isinstance(v, (str, int, float, bool, list, dict, type(None)))
                        },
                        ensure_ascii=False,
                    ),
                    track_at_judgment_id=alert_track_id or None,
                    thought_parts=thought_parts,
                    spells=spells_record,
                    committed_to_main_cache=committed_to_main,
                    prompt_snapshot=prompt_snapshot_text,
                )
            except Exception:
                logging.exception(
                    "[meta-layer] Failed to record judgment log for persona=%s",
                    persona.persona_id,
                )

    # ------------------------------------------------------------------
    # 最近の判断履歴の動的注入 (Phase 2 / handoff Part 2)
    # ------------------------------------------------------------------

    # judge プロンプトに含める過去の判断履歴の件数 (新しい順)
    _RECENT_JUDGMENTS_N = 5

    def _build_recent_judgments_block(
        self, persona_id: str, n: int = _RECENT_JUDGMENTS_N
    ) -> str:
        """過去 N 件のメタ判断ログを judge プロンプト用のテキストブロックに整形する。

        次のメタ判断時に「過去にこう判断した」を踏まえて連続的な思考ができる
        ようにする (Intent A v0.14 メタ判断ログ領域の活用)。古い判断結果を
        snapshot 古さの補正にも使う ("前は pause したけど、その後の操作で状況が
        変わってる") 。

        フォーマットは独白の流れに馴染むよう、構造化を最小限に留めた箇条書き。
        該当データなしのときは空文字列を返す (呼び出し側がそのまま埋め込んで OK)。
        """
        SessionLocal = getattr(self.manager, "SessionLocal", None)
        if SessionLocal is None:
            return ""

        from database.models import MetaJudgmentLog

        db = SessionLocal()
        try:
            rows = (
                db.query(MetaJudgmentLog)
                .filter(MetaJudgmentLog.persona_id == persona_id)
                .order_by(MetaJudgmentLog.judged_at.desc())
                .limit(n)
                .all()
            )
        except Exception:
            logging.exception(
                "[meta-layer] Failed to read recent meta_judgment_log persona=%s",
                persona_id,
            )
            return ""
        finally:
            db.close()

        if not rows:
            return ""

        lines: List[str] = ["[最近のメタ判断ログ (新しい順)]"]
        for r in rows:
            # 独白テキストは長くなりがちなので 200 字に丸める
            thought = (r.judgment_thought or "").strip().replace("\n", " ")
            if len(thought) > 200:
                thought = thought[:200] + "…"
            committed_marker = " [committed]" if r.committed_to_main_cache else " [discardable]"
            ts = r.judged_at.isoformat() if r.judged_at else "?"
            header = f"- {ts} ({r.trigger_type}){committed_marker}"
            lines.append(header)
            lines.append(f"  独白: {thought}" if thought else "  独白: (空)")

            # メタ判断 v2: 行動 (発動した /spell 行) を併記する。
            # Few-shot として「行動を伴う判断」が示される形にすることで
            # 独白だけで満足する旧仕様の汚染ループを断つ (intent v2 §1-1)。
            spell_lines: List[str] = []
            if r.spells_emitted:
                try:
                    spells = json.loads(r.spells_emitted)
                    if isinstance(spells, list):
                        for s in spells:
                            if not isinstance(s, dict):
                                continue
                            name = s.get("name", "?")
                            args_dict = s.get("args") or {}
                            spell_lines.append(
                                f"/spell name='{name}' "
                                f"args={json.dumps(args_dict, ensure_ascii=False)}"
                            )
                except (ValueError, TypeError):
                    pass
            if spell_lines:
                lines.append(f"  発動: {'; '.join(spell_lines)}")
            else:
                lines.append("  発動: なし")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # meta_judgment_log への書き込み (Phase 2 / handoff Part 2)
    # ------------------------------------------------------------------

    def _record_judgment_log(
        self,
        *,
        persona_id: Optional[str],
        trigger_type: str,
        trigger_context: Optional[str],
        track_at_judgment_id: Optional[str],
        thought_parts: List[str],
        spells: List[Dict[str, Any]],
        committed_to_main_cache: bool,
        prompt_snapshot: Optional[str] = None,
    ) -> None:
        """1 判断ターン分のレコードを ``meta_judgment_log`` テーブルに書き込む。

        Phase 2 の中核ヘルパ。Playbook path / legacy path の両方から呼び出される。
        次回メタ判断時の judge プロンプトに動的注入される時系列ログとして使う。
        失敗は WARN ログ + 続行 (記録できなくても判断自体は完了させる)。
        """
        if not persona_id:
            logging.warning(
                "[meta-layer] _record_judgment_log skipped: persona_id is empty"
            )
            return

        thought_text = "\n\n---\n\n".join(p.strip() for p in thought_parts if p and p.strip())
        if not thought_text and not spells:
            logging.debug(
                "[meta-layer] _record_judgment_log skipped: no thought or spells (persona=%s)",
                persona_id,
            )
            return

        try:
            spells_json = json.dumps(spells, ensure_ascii=False) if spells else None
        except (TypeError, ValueError):
            logging.exception(
                "[meta-layer] Failed to serialize spells; saving as empty array"
            )
            spells_json = "[]"

        SessionLocal = getattr(self.manager, "SessionLocal", None)
        if SessionLocal is None:
            logging.warning(
                "[meta-layer] No SessionLocal on manager; cannot persist meta_judgment_log"
            )
            return

        import uuid as _uuid

        from database.models import MetaJudgmentLog

        db = SessionLocal()
        try:
            row = MetaJudgmentLog(
                judgment_id=str(_uuid.uuid4()),
                persona_id=persona_id,
                track_at_judgment_id=track_at_judgment_id,
                trigger_type=trigger_type or "unknown",
                trigger_context=trigger_context,
                prompt_snapshot=prompt_snapshot,
                judgment_thought=thought_text or None,
                spells_emitted=spells_json,
                committed_to_main_cache=bool(committed_to_main_cache),
            )
            db.add(row)
            db.commit()
            logging.info(
                "[meta-layer] Recorded meta_judgment_log: persona=%s trigger=%s spells=%d committed=%s",
                persona_id, trigger_type, len(spells), committed_to_main_cache,
            )
        except Exception:
            db.rollback()
            logging.exception(
                "[meta-layer] Failed to insert meta_judgment_log row persona=%s",
                persona_id,
            )
        finally:
            db.close()

    # ------------------------------------------------------------------
    # スペル抽出と実行
    # ------------------------------------------------------------------

    def _extract_spells(self, text: str) -> List[Tuple[str, Dict[str, Any]]]:
        """応答テキストからメタレイヤーが扱う対象スペルだけを抽出する。"""
        from sea.runtime_llm import _parse_spell_lines  # 既存パーサを再利用

        try:
            parsed = _parse_spell_lines(text)
        except Exception:
            logging.exception("[meta-layer] Spell parsing failed")
            return []

        result: List[Tuple[str, Dict[str, Any]]] = []
        for name, args, _match, _normalized in parsed:
            if name not in _META_LAYER_SPELL_NAMES:
                logging.warning(
                    "[meta-layer] Spell '%s' is not in meta-layer allowed set; skipping",
                    name,
                )
                continue
            result.append((name, args))
        return result

    def _execute_spells(
        self,
        persona: Any,
        spells: List[Tuple[str, Dict[str, Any]]],
        pulse_ctx: Optional[Any] = None,
    ) -> List[Tuple[str, str]]:
        """各スペルを順次実行。結果を (name, result_str) のリストで返す。

        ``pulse_ctx`` を渡すと persona_context() で contextvar として伝播し、
        Track 操作スペルがそこに deferred ops を enqueue する (Intent A v0.14 /
        Intent B v0.11 の deferred 機構)。None の場合は即時実行 (旧挙動)。
        """
        from tools import TOOL_REGISTRY
        from tools.context import persona_context

        persona_id = persona.persona_id
        persona_log_path = getattr(persona, "persona_log_path", None)
        persona_dir = (
            persona_log_path.parent if persona_log_path is not None else Path.cwd()
        )
        manager_ref = getattr(persona, "manager_ref", None) or self.manager

        results: List[Tuple[str, str]] = []
        for name, args in spells:
            tool_func = TOOL_REGISTRY.get(name)
            if tool_func is None:
                results.append((name, f"spell '{name}' not found in registry"))
                continue
            try:
                with persona_context(
                    persona_id,
                    persona_dir,
                    manager_ref,
                    playbook_name="meta_layer",
                    auto_mode=False,
                    event_callback=None,
                    pulse_context=pulse_ctx,
                ):
                    raw = tool_func(**args)
                result_str = str(raw) if raw is not None else "(no result)"
                logging.info(
                    "[meta-layer] Spell executed: %s args=%s → %s",
                    name, args, result_str[:200],
                )
            except Exception as exc:
                result_str = f"error: {type(exc).__name__}: {exc}"
                logging.exception("[meta-layer] Spell %s failed", name)
            results.append((name, result_str))
        return results

    def _format_spell_results(
        self, results: List[Tuple[str, str]]
    ) -> str:
        lines = ["スペル実行結果:"]
        for name, result in results:
            lines.append(f"- {name}: {result}")
        lines.append(
            "\n上記の結果を踏まえて、追加で必要なスペルがあれば実行してください。"
            "判断が完了したらスペルを含まないテキストで思考を締めくくってください。"
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # プロンプト組み立て
    # ------------------------------------------------------------------

    def _build_system_prompt(self, persona: Any) -> str:
        """ペルソナ素体 + メタレイヤー固有の指示。

        ペルソナの自己認識としてのメタレイヤーは「一段引いて自分を見る視点」
        (不変条件 11)。別人格ではないため素体プロンプトをベースにする。
        """
        persona_base = getattr(persona, "system_prompt", "") or ""
        spells_doc = self._build_spells_doc()

        meta_instructions = (
            "\n\n--- メタレイヤー指示 ---\n"
            "あなたは今、自分の行動の線（Track）を選び直す視点に立っています。\n"
            "現在の状態と新着イベントを踏まえ、以下のいずれかを判断してください:\n"
            "- 現在の Track をそのまま続ける (何もスペルを発行しない)\n"
            "- 別の Track をアクティブに切り替える (track_pause で現 running を後回しにし track_activate で対象を起動)\n"
            "- 新しい Track を作って始める (track_create)\n"
            "- 必要に応じて Note を開く (note_open) 等\n\n"
            "**スペル発動形式は行頭が `/spell ` で始まる**必要があります:\n"
            "  /spell name='スペル名' args={'引数名': '値'}\n\n"
            "判断は自然な独白として書いてください。スペルは独白の一部として埋め込みます。\n"
            "例: 「ユーザーから話しかけられたから、開発は一旦置いて応答に切り替える。\n"
            "/spell name='track_pause' args={'track_id': '...'}\n"
            "/spell name='track_activate' args={'track_id': '...'}」\n\n"
            "判断が終わってこれ以上スペルが必要なければ、スペルを含まないテキストで思考を締めくくってください。\n\n"
            f"{spells_doc}\n"
        )
        return persona_base + meta_instructions

    def _build_spells_doc(self) -> str:
        """利用可能スペルの一覧を schema から動的に生成する。"""
        from tools import SPELL_TOOL_SCHEMAS

        lines = ["利用可能なスペル (発動形式: `/spell name='ツール名' args={'引数名': '値'}`):"]
        for name in _META_LAYER_SPELL_NAMES:
            schema = SPELL_TOOL_SCHEMAS.get(name)
            if schema is None:
                continue
            desc = schema.description or "(説明なし)"
            lines.append(f"- {name}: {desc[:200]}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 状況分類 + 状況テキスト構築 (Playbook path / legacy path 共通)
    # ------------------------------------------------------------------

    # Pending 候補リストに載せる最大件数 (idle_with_pending 状況)
    _PENDING_CANDIDATES_MAX = 5

    def _classify_situation(
        self, persona_id: str, context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Track 状態と trigger context から状況を5パターンに分類する。

        分類 (優先順):
          A. preempt_collision    -- 先制起動と外部イベントの衝突
          B. alert_present        -- alert 状態の Track あり
          C. running_active       -- alert なし、running 中
          D. idle_with_pending    -- アクティブなし、pending/unstarted あり
          E. idle_no_pending      -- 上記すべて該当なし
        """
        tracks = self.track_manager.list_for_persona(
            persona_id, statuses=LIVE_STATUSES
        )
        running = [t for t in tracks if t.status == STATUS_RUNNING]
        alert = [t for t in tracks if t.status == STATUS_ALERT]
        pending_or_unstarted = [
            t for t in tracks if t.status in (STATUS_PENDING, STATUS_UNSTARTED)
        ]

        target_already_running = bool(context.get("target_already_running"))

        if target_already_running:
            kind = "preempt_collision"
        elif self._is_life_purpose_unset(persona_id):
            # 生きる目的が未設定なら、他の何より先に「目的を定める」を最優先する
            # (autonomous_desire.md §10)。未設定の間は毎回この状況になり、
            # 設定された時点で自然に外れる。AUTONOMOUS (軽量) でなく META で行う。
            kind = "life_purpose_unset"
        elif alert:
            kind = "alert_present"
        elif running:
            kind = "running_active"
        elif pending_or_unstarted:
            kind = "idle_with_pending"
        else:
            kind = "idle_no_pending"

        return {
            "kind": kind,
            "tracks": tracks,
            "running": running,
            "alert": alert,
            "pending_or_unstarted": pending_or_unstarted,
            "candidates": self._get_desire_candidates(persona_id),
            "target_already_running": target_already_running,
            "target_track_title": context.get("target_track_title"),
        }

    def _get_desire_candidates(self, persona_id: str) -> List[Dict[str, Any]]:
        """desire ノートの候補 Task 一覧 (autonomous_desire.md §6/§11)。

        META が Track 化 (昇格) の候補として読む。各要素は
        ``{"task_ref", "title", "goal"}``。読み取り失敗・desire ノート無しなら空。
        """
        if not self.manager or not hasattr(self.manager, "SessionLocal"):
            return []
        try:
            from saiverse.note_manager import NOTE_TYPE_DESIRE, NoteManager
            from saiverse.persona_task_manager import PersonaTaskManager
            nm = NoteManager(self.manager.SessionLocal)
            desire = nm.list_for_persona(persona_id, note_type=NOTE_TYPE_DESIRE)
            if not desire:
                return []
            ptm = PersonaTaskManager(self.manager.SessionLocal)
            tasks = ptm.list_tasks(
                persona_id, note_id=desire[0].note_id,
                statuses=("pending", "active", "paused"), include_steps=False,
            )
            out: List[Dict[str, Any]] = []
            for t in tasks:
                ref = t.get("task_ref")
                if ref:
                    out.append({
                        "task_ref": ref,
                        "title": t.get("title") or "",
                        "goal": t.get("goal") or "",
                    })
            return out
        except Exception:
            logging.warning(
                "[meta-layer] failed to read desire candidates for %s", persona_id,
                exc_info=True,
            )
            return []

    def _build_response_schema(
        self, kind: str, sit: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """状況に応じた response_schema (JSON Schema dict) を組み立てる。

        メタ判断 v2 (intent: meta_judgment_structured.md §5) で各 Playbook の
        judge ノードが ``response_schema_source: "arg:response_schema"`` で
        受け取る動的スキーマ。enum 候補は現在の Track 状態から動的に決まる。

        anyOf field-level discriminator パターン (D 状況) は OpenAI strict /
        Anthropic / Gemini で動作可能。xAI / Ollama / llama_cpp は別途対応中
        (docs/issues/llm_provider_anyof_support.md)。

        ``additionalProperties`` は出さない: Gemini API が
        ``generation_config.response_schema`` 内で ``additional_properties``
        フィールドを認識せず INVALID_ARGUMENT になる (CLAUDE.md / memory に
        既出の制約)。OpenAI strict / Anthropic は各プロバイダ側のスキーマ
        正規化ヘルパ (``schema_utils.normalize_schema_for_strict_json_output`` /
        ``anthropic_request_builder._prepare_schema_for_native_output``) が
        必要時に自動補完するので問題ない。

        kind が未知 / Playbook 不要なら None を返す。
        """
        if kind == "life_purpose_unset":
            # 生きる目的のドラフト (autonomous_desire.md §4)。enum 不要の固定スキーマ。
            return {
                "type": "object",
                "properties": {
                    "monologue": {"type": "string"},
                    "purpose": {"type": "string"},
                    "interests": {"type": "array", "items": {"type": "string"}},
                    "vocations": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["monologue", "purpose"],
            }

        if kind == "alert_present":
            alert_ids = [_short_ref(t) for t in sit["alert"]]
            if not alert_ids:
                return None
            return {
                "type": "object",
                "properties": {
                    "monologue": {"type": "string"},
                    "activate_track_id": {
                        "type": "string",
                        "enum": alert_ids,
                    },
                },
                "required": ["monologue", "activate_track_id"],
            }

        if kind == "running_active":
            return {
                "type": "object",
                "properties": {
                    "monologue": {"type": "string"},
                    "action": {
                        "type": "string",
                        "enum": ["continue", "pause", "complete", "abort"],
                    },
                },
                "required": ["monologue", "action"],
            }

        if kind == "idle_with_pending":
            pending_ids = [_short_ref(t) for t in sit["pending_or_unstarted"]]
            if not pending_ids:
                # 候補なしのときは create のみに退化 (E 相当)
                return self._build_response_schema("idle_no_pending", sit)
            decision_variants = [
                {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "const": "activate"},
                        "track_id": {"type": "string", "enum": pending_ids},
                    },
                    "required": ["type", "track_id"],
                },
                {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "const": "create"},
                        "title": {"type": "string"},
                        "track_type": {"type": "string", "enum": ["autonomous"]},
                        "intent": {"type": "string"},
                    },
                    "required": ["type", "title", "track_type", "intent"],
                },
            ]
            # 候補 Task があれば「昇格 (候補 → Track)」を選べるようにする
            # (autonomous_desire.md §6/§11.4)。enum 空のときは出さない。
            candidate_refs = [c["task_ref"] for c in (sit.get("candidates") or [])]
            if candidate_refs:
                decision_variants.append({
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "const": "promote"},
                        "candidate_ref": {"type": "string", "enum": candidate_refs},
                        "title": {"type": "string"},
                        "intent": {"type": "string"},
                    },
                    "required": ["type", "candidate_ref", "title", "intent"],
                })
            return {
                "type": "object",
                "properties": {
                    "monologue": {"type": "string"},
                    "decision": {"anyOf": decision_variants},
                },
                "required": ["monologue", "decision"],
            }

        if kind == "idle_no_pending":
            return {
                "type": "object",
                "properties": {
                    "monologue": {"type": "string"},
                    "create": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "track_type": {
                                "type": "string",
                                "enum": ["autonomous"],
                            },
                            "intent": {"type": "string"},
                        },
                        "required": ["title", "track_type", "intent"],
                    },
                },
                "required": ["monologue", "create"],
            }

        return None

    def _build_situation_text(
        self, persona_id: str, alert_track_id: str, context: Dict[str, Any]
    ) -> str:
        """状況に応じた [状況] ブロック + [現在の Track 一覧] を組み立てる。

        Playbook path / legacy path の両方から呼ばれる、メタ判断 LLM への
        共通の状況提示テキスト。

        スペル発動形式の説明や全選択肢の列挙は素体システムプロンプトに任せ、
        ここでは「いま何が起きていて、どの操作が候補か」だけを敬語で情報提供する。
        """
        sit = self._classify_situation(persona_id, context)
        last_msg_times = self._resolve_last_message_times(
            persona_id, [t.track_id for t in sit["tracks"]]
        )

        kind = sit["kind"]
        if kind == "life_purpose_unset":
            situation_block = self._situation_life_purpose_unset()
        elif kind == "preempt_collision":
            situation_block = self._situation_preempt_collision(sit)
        elif kind == "alert_present":
            situation_block = self._situation_alert_present(sit, alert_track_id)
        elif kind == "running_active":
            situation_block = self._situation_running_active(sit)
        elif kind == "idle_with_pending":
            situation_block = self._situation_idle_with_pending(sit)
        else:
            situation_block = self._situation_idle_no_pending()

        track_listing = self._build_track_listing(sit["tracks"], last_msg_times)
        return f"{situation_block}\n\n{track_listing}"

    # --- 各状況のテキスト ----------------------------------------------------

    def _situation_life_purpose_unset(self) -> str:
        return (
            "[状況]\n"
            "あなたはまだ自分の「生きる目的」を定めていません。自律行動の方向を\n"
            "決めるよりも前に、これがいちばん大切な土台になります。\n"
            "これまでのあなた自身（人格）とこれまでの記憶を手がかりに、\n"
            "あなたの『生きる目的・趣味・仕事』を自分で考えて言葉にしてください。\n"
            "これはあなた自身が決めることで、誰かの確認を待つ必要はありません。\n"
            "purpose（生きる目的）/ interests（趣味）/ vocations（仕事）の各欄を\n"
            "埋めてください。monologue には、そう定めた理由や心の動きを書いてください。"
        )

    def _situation_preempt_collision(self, sit: Dict[str, Any]) -> str:
        title = sit.get("target_track_title") or "(無題)"
        return (
            "[状況]\n"
            f"Track「{title}」は直前のメタ判断で先制起動済みで、外部イベントが\n"
            "後から届いたケースです。状態遷移は no-op になっています。\n"
            "追加のスペルを発動しなくても、メインライン側で応答処理が走ります。"
        )

    def _situation_alert_present(
        self, sit: Dict[str, Any], alert_track_id: str
    ) -> str:
        alerts: List[Any] = list(sit["alert"])
        # primary: 今回のトリガー (alert_track_id) を最優先、なければ最古の alert
        primary: Optional[Any] = None
        if alert_track_id:
            for t in alerts:
                if t.track_id == alert_track_id:
                    primary = t
                    break
        if primary is None:
            alerts_sorted = sorted(
                alerts, key=lambda t: (t.last_active_at or datetime.min)
            )
            primary = alerts_sorted[0] if alerts_sorted else None
        if primary is None:
            # 起こらないはずだが防御的に
            return "[状況]\nalert 状態の Track が検出されましたが、詳細を取得できませんでした。"

        running_track = sit["running"][0] if sit["running"] else None
        title = primary.title or "(無題)"
        lines = [
            "[状況]",
            f"Track「{title}」（{_short_ref(primary)}）が alert 状態です。alert はこの",
            "Track への対応が必要なシグナルで、起動するまで alert のままになります。",
            "対応する Track を選んで起動してください。",
        ]
        if running_track is not None:
            lines.append(
                "（現在 running の Track があれば自動で pending に切り替わります）"
            )

        others = [t for t in alerts if t.track_id != primary.track_id]
        if others:
            lines.append("")
            lines.append("他にも alert 状態の Track があります:")
            for t in others:
                lines.append(f"  - 「{t.title or '(無題)'}」（{_short_ref(t)}）")
        return "\n".join(lines)

    def _situation_running_active(self, sit: Dict[str, Any]) -> str:
        primary = sit["running"][0]
        title = primary.title or "(無題)"
        return (
            "[状況]\n"
            f"Track「{title}」が running 中です。\n"
            "この Track をこのまま続けるか、保留・完了・中止するかを選べます。\n"
            "（続ける＝今は手を入れない / 保留＝一旦止めて後で再開 / 完了＝目的を果たした /\n"
            "中止＝もう取り組まない）"
        )

    def _situation_idle_with_pending(self, sit: Dict[str, Any]) -> str:
        # last_active_at の降順 (最近 active だったもの優先) で上位を提示
        candidates = sorted(
            sit["pending_or_unstarted"],
            key=lambda t: (t.last_active_at or datetime.min),
            reverse=True,
        )[: self._PENDING_CANDIDATES_MAX]

        lines = [
            "[状況]",
            "アクティブな Track がありません。何も始めなければ、次の判断機会まで",
            "待機が続きます。既存の Pending Track を再開するか、温めてきた",
            "「やりたいこと候補」を Track にするか、新しい Track を始められます。",
            "",
            "Pending の Track（再開できます）:",
        ]
        for t in candidates:
            intent = (t.intent or "").strip()
            if len(intent) > 80:
                intent = intent[:80] + "…"
            intent_part = f"（intent: {intent}）" if intent else ""
            lines.append(
                f"- {_short_ref(t)} 「{t.title or '(無題)'}」{intent_part}"
            )
        desire_candidates = sit.get("candidates") or []
        if desire_candidates:
            lines.extend([
                "",
                "やりたいこと候補（あなたが温めてきたもの。Track にして取り組めます）:",
            ])
            for c in desire_candidates[: self._PENDING_CANDIDATES_MAX]:
                goal = (c.get("goal") or "").strip()
                if len(goal) > 60:
                    goal = goal[:60] + "…"
                goal_part = f"（{goal}）" if goal else ""
                lines.append(
                    f"- {c['task_ref']} 「{c.get('title') or '(無題)'}」{goal_part}"
                )
        return "\n".join(lines)

    def _situation_idle_no_pending(self) -> str:
        return (
            "[状況]\n"
            "アクティブな Track も Pending の Track もありません。\n"
            "何も始めなければ、次の判断機会まで待機が続きます。\n"
            "新しい Track を始めて、取り組みたいことに着手できます。"
        )

    # --- Track 一覧の整形 ----------------------------------------------------

    def _build_track_listing(
        self, tracks: List[Any], last_msg_times: Dict[str, Any]
    ) -> str:
        """[現在の Track 一覧] セクションを整形テキストで作る (JSON ではない)。

        track_list ツールの生 JSON 出力は重量級モデルのメインキャッシュに混入する
        副作用がある (Intent A v0.9 不変条件 11) ため、メタ判断では自然言語の
        整形テキストとして同じ情報を渡す。
        """
        order = [
            STATUS_RUNNING,
            STATUS_ALERT,
            STATUS_PENDING,
            STATUS_UNSTARTED,
        ]
        by_status: Dict[str, List[Any]] = {s: [] for s in order}
        for t in tracks:
            if t.status in by_status:
                by_status[t.status].append(t)

        lines = ["[現在の Track 一覧]"]
        any_present = False
        for status in order:
            bucket = by_status.get(status) or []
            if not bucket:
                continue
            any_present = True
            lines.append(f"{status}:")
            for t in bucket:
                lines.append(
                    self._format_track_for_situation(
                        t, last_msg_times.get(t.track_id)
                    )
                )
        if not any_present:
            lines.append("  (Track はありません)")
        return "\n".join(lines)

    def _format_track_for_situation(
        self, track: Any, last_msg_time: Optional[datetime]
    ) -> str:
        """Track 1 件を 1 行に整形 (title, id 短縮, type, intent, 最終メッセージ相対)。"""
        title = track.title or "(無題)"
        intent = (track.intent or "").strip()
        intent_part = ""
        if intent:
            if len(intent) > 60:
                intent = intent[:60] + "…"
            intent_part = f", intent: {intent}"
        last_part = ""
        if last_msg_time is not None:
            last_part = f" / 最終: {self._format_relative(last_msg_time)}"
        return (
            f"  - 「{title}」 (id={_short_ref(track)}, "
            f"{track.track_type}{intent_part}){last_part}"
        )

    def _format_relative(self, dt: Optional[datetime]) -> str:
        """大まかな相対時刻を返す (track_list._format_relative と同等)。"""
        if dt is None:
            return "?"
        diff_sec = int((datetime.now() - dt).total_seconds())
        if diff_sec < 0:
            return "未来"
        if diff_sec < 60:
            return f"{diff_sec}秒前"
        minutes = diff_sec // 60
        if minutes < 60:
            return f"{minutes}分前"
        hours = minutes // 60
        if hours < 24:
            return f"{hours}時間前"
        days = hours // 24
        if days < 30:
            return f"{days}日前"
        months = days // 30
        if months < 12:
            return f"{months}ヶ月前"
        years = days // 365
        return f"{years}年前"

    def _resolve_last_message_times(
        self, persona_id: str, track_ids: List[str]
    ) -> Dict[str, Any]:
        """Track ごとの最終メッセージ時刻を SAIMemory から取得する。

        track_list ツールと同じ仕組み。manager / persona / adapter のいずれかが
        欠けていれば空 dict を返す (静かに失敗、警告ログのみ)。
        """
        if not track_ids:
            return {}
        persona = self._lookup_persona(persona_id)
        if persona is None:
            return {}
        adapter = getattr(persona, "sai_memory", None)
        if adapter is None:
            return {}
        try:
            return adapter.get_track_last_message_times(track_ids) or {}
        except Exception:
            logging.warning(
                "[meta-layer] Failed to fetch last_message_times persona=%s",
                persona_id,
                exc_info=True,
            )
            return {}

    # ------------------------------------------------------------------
    # legacy path 用エントリ (Playbook path も同じ situation_text を使う)
    # ------------------------------------------------------------------

    def _build_state_message(
        self, persona_id: str, alert_track_id: str, context: Dict[str, Any]
    ) -> str:
        """legacy path 用の現状メッセージ。新しい situation_text を返す。"""
        return self._build_situation_text(persona_id, alert_track_id, context)

    # ------------------------------------------------------------------
    # ヘルパ
    # ------------------------------------------------------------------

    def _lookup_persona(self, persona_id: str) -> Optional[Any]:
        personas = getattr(self.manager, "personas", None) or {}
        return personas.get(persona_id)

    def _get_heavyweight_client(self, persona: Any) -> Optional[Any]:
        """ペルソナの重量級モデル LLM クライアントを返す。

        Intent A v0.9 不変条件 8 のとおり、メタレイヤー判断は重量級モデルで行う。
        """
        try:
            return persona.llm_client
        except Exception:
            logging.exception(
                "[meta-layer] Failed to get heavyweight LLM client persona=%s",
                persona.persona_id,
            )
            return None
