"""MetaLayer: ペルソナの行動 Track の選択・切り替えを判断する観察視点。

Intent A v0.9 / Intent B v0.6 が定める「メタレイヤー」の最小実装 (Phase C-1)。

責務:
- TrackManager の alert observer として登録され、alert 遷移を契機に起動する
- 重量級モデルに「現状 + 新着イベント」をプロンプトとして渡し、
  Track 操作スペル (/track_*, /note_*) を含む自由文応答を得る
- スペルが含まれていれば既存の TOOL_REGISTRY 経由で実行 → 結果を再びプロンプトに
  含めて再呼び出し。スペルが含まれない応答が返ってきた時点で自然停止
- LLM コール時には tools / response_schema を一切渡さない
  (キャッシュ親和性とコンテキスト汚染回避のため)

責務外:
- メインライン応答 (発話生成) の起動。これは呼び出し元 (Handler) が責任を持つ
- Track の作成 / 状態遷移ロジック (TrackManager に委譲)
- 中断時 pause_summary 作成 / 再開コンテキスト構築 (Phase C-7/C-8)

詳細: docs/intent/persona_cognitive_model.md, docs/intent/persona_action_tracks.md
"""
from __future__ import annotations

import json
import logging
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
        """
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
            self._run_judgment(persona, alert_track_id, context)
        except Exception:
            logging.exception(
                "[meta-layer] Judgment failed: persona=%s track=%s",
                persona_id, alert_track_id,
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
        """
        llm_client = self._get_heavyweight_client(persona)
        if llm_client is None:
            logging.warning(
                "[meta-layer] No LLM client available for persona=%s; skipping judgment",
                persona.persona_id,
            )
            return

        system_prompt = self._build_system_prompt(persona)
        user_message = self._build_state_message(
            persona.persona_id, alert_track_id, context
        )
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

            # スペル実行
            results = self._execute_spells(persona, spells)

            # 次ターンに向けて assistant 応答 + ツール結果を append
            messages.append({"role": "assistant", "content": text})
            results_text = self._format_spell_results(results)
            messages.append({"role": "user", "content": results_text})

        logging.warning(
            "[meta-layer] Hit max spell loops (%d) without natural stop persona=%s",
            _MAX_SPELL_LOOPS, persona.persona_id,
        )

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
        self, persona: Any, spells: List[Tuple[str, Dict[str, Any]]]
    ) -> List[Tuple[str, str]]:
        """各スペルを順次実行。結果を (name, result_str) のリストで返す。"""
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
