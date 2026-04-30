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
- 中断時 pause_summary 作成 / 再開コンテキスト構築 (Phase 1.3 後段 / Phase 2)

詳細: docs/intent/persona_cognitive_model.md, docs/intent/persona_action_tracks.md
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .track_manager import (
    LIVE_STATUSES,
    STATUS_ALERT,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_UNSTARTED,
    STATUS_WAITING,
    TrackManager,
)


# 安全網: スペルループの最大回数。LLM が暴走した時に無限ループを防ぐ。
_MAX_SPELL_LOOPS = 5

# メタレイヤーが扱う Track 操作スペル (Phase B-3 で導入済み)。
# 一覧は登録済みスペルから動的に拾うが、ペルソナ素体プロンプトと衝突しないよう
# 「メタレイヤーが使ってよい」セットを明示的に絞る。
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

    def _get_lock(self, persona_id: str) -> threading.Lock:
        """persona_id の Lock を返す (なければ作成)。"""
        with self._locks_guard:
            lock = self._persona_locks.get(persona_id)
            if lock is None:
                lock = threading.Lock()
                self._persona_locks[persona_id] = lock
            return lock

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

        per-persona Lock で直列化する (`__init__` 参照)。同ペルソナの判断 Pulse
        が走行中なら完了まで wait する。
        """
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
                use_playbook = self._use_playbook_path()
                logging.info(
                    "[meta-layer] Judgment starting: persona=%s alert_track=%s trigger=%s path=%s",
                    persona_id, alert_track_id, context.get("trigger"),
                    "playbook" if use_playbook else "legacy",
                )
                if use_playbook:
                    self._run_judgment_via_playbook(persona, alert_track_id, context)
                else:
                    self._run_judgment(persona, alert_track_id, context)
            except Exception:
                logging.exception(
                    "[meta-layer] Judgment failed: persona=%s track=%s",
                    persona_id, alert_track_id,
                )

    @staticmethod
    def _use_playbook_path() -> bool:
        """Read the ``SAIVERSE_META_LAYER_USE_PLAYBOOK`` env flag.

        Phase C-2 完成 (2026-04-30) で Playbook 経路を既定に昇格。
        legacy direct-LLM スペル loop は緊急避難用に残し、明示的に
        ``SAIVERSE_META_LAYER_USE_PLAYBOOK=0/false/no/off`` を指定したときだけ
        切り替わる。
        """
        raw = os.environ.get("SAIVERSE_META_LAYER_USE_PLAYBOOK", "").strip().lower()
        if raw in ("0", "false", "no", "off"):
            return False
        return True

    # ------------------------------------------------------------------
    # Phase C-2: 定期 tick エントリ (intent A v0.10 / intent B v0.7 §"メタレイヤーの定期実行入口")
    # ------------------------------------------------------------------

    def on_periodic_tick(
        self,
        persona_id: str,
        context: Optional[Dict[str, Any]] = None,
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
                if activity_state != "Active":
                    logging.debug(
                        "[meta-layer] periodic tick skipped (activity_state=%s != Active): persona=%s",
                        activity_state, persona_id,
                    )
                    return

                # post_complete_behavior 抑止: 応答待ち型の Track は割り込まない
                running_track = self._get_running_track(persona_id)
                if running_track is not None:
                    handler = self._get_handler_for_track(running_track)
                    behavior = getattr(handler, "post_complete_behavior", None) if handler else None
                    if behavior == "wait_response":
                        logging.debug(
                            "[meta-layer] periodic tick skipped (running Track wait_response): persona=%s track=%s",
                            persona_id, getattr(running_track, "track_id", "?"),
                        )
                        return

                merged_context = {"trigger": "periodic_tick"}
                if context:
                    merged_context.update(context)

                use_playbook = self._use_playbook_path()
                logging.info(
                    "[meta-layer] Periodic tick starting: persona=%s path=%s context=%s",
                    persona_id,
                    "playbook" if use_playbook else "legacy",
                    merged_context.get("trigger"),
                )
                # alert_track_id="" は intent B 通り (定期 tick の場合は空文字列も可)
                if use_playbook:
                    self._run_judgment_via_playbook(persona, "", merged_context)
                else:
                    self._run_judgment(persona, "", merged_context)
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

    def _run_judgment_via_playbook(
        self,
        persona: Any,
        alert_track_id: str,
        context: Dict[str, Any],
    ) -> None:
        """Dispatch to the ``meta_judgment`` Playbook through the runtime.

        Resolves the persona's current building (needed by run_meta_user's
        pulse-root pipeline) and delegates. The Playbook itself records its
        LLM turn as ``scope='discardable'`` and the dispatch tool promotes
        the row to ``'committed'`` when the action is ``switch`` (Phase 1.3).
        """
        runtime = getattr(self.manager, "sea_runtime", None)
        if runtime is None:
            logging.warning(
                "[meta-layer] No sea_runtime on manager — cannot run meta_judgment Playbook; "
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

        args = {
            "alert_track_id": alert_track_id or "",
            "trigger_context": trigger_context_json,
            "recent_judgments": recent_judgments_block,
        }

        captured_errors: List[Dict[str, Any]] = []

        def _capture_event(ev: Dict[str, Any]) -> None:
            if isinstance(ev, dict) and ev.get("type") == "error":
                captured_errors.append(ev)

        try:
            runtime.run_meta_user(
                persona,
                user_input=None,
                building_id=building_id,
                meta_playbook="meta_judgment",
                args=args,
                event_callback=_capture_event,
                pulse_type="meta_judgment",
            )
        except Exception:
            logging.exception(
                "[meta-layer] meta_judgment Playbook failed: persona=%s alert_track=%s",
                persona.persona_id, alert_track_id,
            )
            return

        if captured_errors:
            for err in captured_errors:
                logging.error(
                    "[meta-layer] meta_judgment Playbook emitted error: persona=%s alert_track=%s error=%s",
                    persona.persona_id, alert_track_id, err,
                )
            return

        logging.info(
            "[meta-layer] meta_judgment Playbook completed: persona=%s alert_track=%s",
            persona.persona_id, alert_track_id,
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
            spells_summary = ""
            if r.spells_emitted:
                try:
                    spells = json.loads(r.spells_emitted)
                    if isinstance(spells, list) and spells:
                        names = [s.get("name", "?") for s in spells if isinstance(s, dict)]
                        spells_summary = f" | spells={','.join(names)}"
                except (ValueError, TypeError):
                    pass
            committed_marker = " [switch]" if r.committed_to_main_cache else ""
            lines.append(
                f"- {r.judged_at.isoformat() if r.judged_at else '?'} "
                f"({r.trigger_type}){committed_marker}{spells_summary}\n  独白: {thought}"
            )
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
            "  /spell <スペル名> key='value' key2=value2 ...\n\n"
            "判断は自然な独白として書いてください。スペルは独白の一部として埋め込みます。\n"
            "例: 「ユーザーから話しかけられたから、開発は一旦置いて応答に切り替える。\n"
            "/spell track_pause track_id='...'\n"
            "/spell track_activate track_id='...'」\n\n"
            "判断が終わってこれ以上スペルが必要なければ、スペルを含まないテキストで思考を締めくくってください。\n\n"
            f"{spells_doc}\n"
        )
        return persona_base + meta_instructions

    def _build_spells_doc(self) -> str:
        """利用可能スペルの一覧を schema から動的に生成する。"""
        from tools import SPELL_TOOL_SCHEMAS

        lines = ["利用可能なスペル (発動形式: `/spell <名前> key='value' ...`):"]
        for name in _META_LAYER_SPELL_NAMES:
            schema = SPELL_TOOL_SCHEMAS.get(name)
            if schema is None:
                continue
            desc = schema.description or "(説明なし)"
            lines.append(f"- {name}: {desc[:200]}")
        return "\n".join(lines)

    def _build_state_message(
        self, persona_id: str, alert_track_id: str, context: Dict[str, Any]
    ) -> str:
        """現状 (Track 一覧 + 新着イベント) を user メッセージとして組み立てる。"""
        tracks = self.track_manager.list_for_persona(
            persona_id, statuses=LIVE_STATUSES
        )
        running = [t for t in tracks if t.status == STATUS_RUNNING]
        alert_tracks = [t for t in tracks if t.status == STATUS_ALERT]
        pending = [t for t in tracks if t.status == STATUS_PENDING]
        waiting = [t for t in tracks if t.status == STATUS_WAITING]
        unstarted = [t for t in tracks if t.status == STATUS_UNSTARTED]

        lines: List[str] = ["[現状]"]
        lines.append("\nrunning Track:")
        if running:
            for t in running:
                lines.append(self._format_track(t))
        else:
            lines.append("  (なし)")

        lines.append("\nalert Track (今回のトリガー対象を含む):")
        if alert_tracks:
            for t in alert_tracks:
                marker = " ★今回のトリガー" if t.track_id == alert_track_id else ""
                lines.append(self._format_track(t) + marker)
        else:
            lines.append("  (なし)")

        if pending:
            lines.append("\npending Track:")
            for t in pending:
                lines.append(self._format_track(t))
        if waiting:
            lines.append("\nwaiting Track:")
            for t in waiting:
                lines.append(
                    self._format_track(t) + f"  (waiting_for={t.waiting_for})"
                )
        if unstarted:
            lines.append("\nunstarted Track:")
            for t in unstarted:
                lines.append(self._format_track(t))

        # 新着イベント
        lines.append("\n[新着イベント]")
        trigger = context.get("trigger", "(unknown)")
        lines.append(f"trigger={trigger}")
        if "user_id" in context:
            lines.append(f"user_id={context['user_id']}")
        event_obj = context.get("event")
        if isinstance(event_obj, dict):
            content = event_obj.get("content")
            if content:
                lines.append(f"event_content: {content}")

        # Phase 2.6: 自律先制と外部 alert のレース対応 (intent A v0.18)。
        # 既 running の Track に対して set_alert が来たケースを自然言語で示す。
        # (set_alert が no-op パスでも observer 通知するようになったため、
        # メタ判断者がこの状況を識別できるようにする)
        if context.get("target_already_running"):
            target_title = context.get("target_track_title") or "(無題)"
            lines.append("")
            lines.append(
                f"※ 対象 Track「{target_title}」は既に running 状態 "
                "(直前のメタ判断などで先制起動済み)。"
            )
            lines.append(
                "  状態遷移は発生しなかったが、外部からのイベントが Track に届いた。"
            )
            lines.append(
                "  メインライン側で応答処理が自然に走るため、"
                "通常は track_activate / track_pause を発動せず継続判断で良い。"
            )

        lines.append(
            "\n上記を踏まえて、Track をどう扱うか判断してください。"
        )
        return "\n".join(lines)

    def _format_track(self, track: Any) -> str:
        title = track.title or "(無題)"
        intent = track.intent or ""
        intent_part = f" intent={intent[:60]}" if intent else ""
        return (
            f"  - id={track.track_id} title={title!r} type={track.track_type}"
            f" persistent={bool(track.is_persistent)}{intent_part}"
        )

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
