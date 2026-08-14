"""MetaLayer: 判断 Pulse の共有基盤 (per-persona Lock・判断設定・判断ログ)。

かつては Track 状態機械を読む v1 メタ判断の本体だった — alert observer /
定期 tick を入口に、状況分類 (`_classify_situation`) で 6 状況へ分け、状況専用の
``meta_judgment_*`` Playbook を dispatch して Track 操作スペルを撃つ円環。
自律行動 v2 の判断点 (時間割・出来事駆動 — ``saiverse/judgment_points.py`` +
``saiverse/autonomy_wiring.py``) への世代交代を経て、2026-08-14 に v1 判断一式を
退役した (docs/intent/track_retirement.md §7.4)。別行動中のユーザー発話の仲裁は
on_event 判断点への直結 (``autonomy_wiring.handle_user_utterance_conflict``) が
後継。

残る責務は判断系が共有する基盤のみ:

- **per-persona Lock** (``_get_lock``): 判断 Pulse の直列化。判断点の起動
  (``autonomy_wiring._judgment_lock``) が共有する
- **判断設定** (``_load_judgment_config``): AI.META_JUDGMENT_CONFIG の読み出しと
  既定値の穴埋め。autonomy_manager (watchdog 間隔) / session_lifecycle
  (cache keepalive) も参照する
- **判断ログ** (``_record_judgment_log``): ``meta_judgment_log`` テーブルへの
  書き込み。判断 Pulse の flush (``sea/runtime_graph.py``) から呼ばれる
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any, Dict, List, Optional, Tuple


class MetaLayer:
    """判断 Pulse の共有基盤。

    マネージャー単位で 1 インスタンス保持し、内部で persona_id を引き回す
    (可変な内部状態は per-persona Lock の dict のみ)。
    """

    def __init__(self, manager: Any):
        """
        Args:
            manager: SAIVerseManager 参照。persona 取得 (manager.personas) と
                SessionLocal 参照に使う。
        """
        self.manager = manager

        # ペルソナごとの直列化 Lock (Intent A v0.9 不変条件 11: 判断 Pulse は
        # ペルソナの「思考の流れ」1 本)。判断点 (autonomy_wiring._judgment_lock)
        # がこの Lock を共有し、同一ペルソナの判断 Pulse を直列化する。
        # 競合時は wait が正しい (skip すると判断機会を取りこぼす)。
        # `_persona_locks` の dict 自体への並行アクセスは `_locks_guard` で保護する。
        self._persona_locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _get_lock(self, persona_id: str) -> threading.Lock:
        """persona_id の Lock を返す (なければ作成)。"""
        with self._locks_guard:
            lock = self._persona_locks.get(persona_id)
            if lock is None:
                lock = threading.Lock()
                self._persona_locks[persona_id] = lock
            return lock

    # ------------------------------------------------------------------
    # Phase 4-e: Per-persona judgment Pulse configuration
    # ------------------------------------------------------------------

    # AI.META_JUDGMENT_CONFIG の既定値。NULL カラム / 不正 JSON / キー欠落の
    # フォールバック先。v1 メタ判断の退役 (track_retirement.md §7.4) 後も
    # キーは全て残している — UI (ライフビュー等) が読み書きする永続語彙のため。
    # リトライ系 (max_retries / retry_backoff_seconds / max_consecutive_failures /
    # force_fail) は旧 v1 リトライ機構の読み手を失った休眠キー。
    _DEFAULT_JUDGMENT_CONFIG: Dict[str, Any] = {
        "cache_threshold_ratio": 0.3,     # キャッシュ TTL 残り 30% で keepalive 発火
        "max_retries": 1,                 # (休眠) 旧 v1: Pulse 失敗時の即時リトライ最大回数
        "retry_backoff_seconds": 5,       # (休眠) 旧 v1: リトライ間の待機秒数
        "max_consecutive_failures": 3,    # (休眠) 旧 v1: 連続失敗で Idle 降格 + 通知 (案C)
        "periodic_interval_minutes": 50,  # watchdog 系の間隔 (旧 AutonomyManager.interval_minutes)
        "keep_cache_alive": True,         # True: cache TTL 接近で keepalive / False: TTL 無視
        # 自律 Track の Pulse 間隔 (秒) のペルソナ単位デフォルト。Track metadata
        # の pulse_interval_seconds が個別上書き (優先順: Track metadata > ここ >
        # AutonomousTrackHandler.default_pulse_interval)。ライフビューの
        # 「作業のテンポ」設定の永続化先 (persona_activity_view.md §7)。
        "autonomous_pulse_interval_seconds": 30,
        # (休眠) 旧 v1 開発者モード: メタ判断を毎回強制失敗させるデバッグフラグ。
        "force_fail": False,
    }

    def _load_judgment_config(self, persona: Any) -> Dict[str, Any]:
        """ペルソナの META_JUDGMENT_CONFIG を読み、既定値で穴埋めして返す。

        AI.META_JUDGMENT_CONFIG (Text/JSON) を毎回 DB から読み出し、
        ``_DEFAULT_JUDGMENT_CONFIG`` にマージする。NULL / 不正 JSON / 型不一致
        は警告ログを残しつつ既定値で穴埋めする。

        UI 編集を即時反映させたいため、結果のキャッシュは行わない (参照頻度が
        低いので影響は軽微)。
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

        判断 Pulse (pulse_type='meta_judgment') の完了時 flush
        (``sea/runtime_graph.py``) から呼ばれる。失敗は WARN ログ + 続行
        (記録できなくても判断自体は完了させる)。
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
    # ヘルパ
    # ------------------------------------------------------------------

    def _lookup_persona(self, persona_id: str) -> Optional[Any]:
        personas = getattr(self.manager, "personas", None) or {}
        return personas.get(persona_id)
