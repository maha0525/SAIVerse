from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import uuid
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from database.models import Playbook as PlaybookModel
from llm_clients.exceptions import LLMError
from saiverse.logging_config import log_sea_trace
from saiverse.model_configs import get_model_parameter_defaults
from saiverse.usage_tracker import get_usage_tracker
from sea.cancellation import CancellationToken, ExecutionCancelledException
from sea.langgraph_runner import compile_playbook
from sea.mcp_tool_refresh import refresh_mcp_tools_at_head
from sea.playbook_models import NodeType, PlaybookSchema, PlaybookValidationError, validate_playbook_graph
from sea.pulse_context import ExecutionContext, default_lightweight_model, resolve_execution_context
from sea.runtime_context import prepare_context as prepare_context_impl
from sea.runtime_engine import RuntimeEngine
from sea.runtime_context import preview_context as preview_context_impl
from sea.runtime_graph import compile_with_langgraph as compile_with_langgraph_impl
from sea.runtime_llm import lg_llm_node as lg_llm_node_impl
from sea.runtime_runner import run_playbook
from sea.runtime_nodes import (
    lg_exec_node as lg_exec_node_impl,
    lg_stelis_end_node as lg_stelis_end_node_impl,
    lg_stelis_start_node as lg_stelis_start_node_impl,
    lg_subplay_node as lg_subplay_node_impl,
    lg_tool_call_node as lg_tool_call_node_impl,
)
from sea.runtime_state import (
    apply_output_mapping,
    eval_arithmetic_expression,
    extract_structured_json,
    flatten_dict,
    process_structured_output,
    resolve_nested_value,
    resolve_set_value,
    resolve_state_value,
    set_playbook_var,
    store_structured_result,
)

from .runtime_emitters import RuntimeEmitters
from .session_lifecycle import SessionLifecycle
from .runtime_utils import _format, _is_llm_streaming_enabled
LOGGER = logging.getLogger(__name__)


def _get_default_lightweight_model() -> str:
    """Get the default lightweight model from environment or fallback.

    実体は ``sea.pulse_context.default_lightweight_model`` に一本化
    (resolve_execution_context と選択結果を一致させるため)。
    """
    return default_lightweight_model()




class SEARuntime:
    """Lightweight executor for meta playbooks until full LangGraph port."""

    def __init__(self, manager_ref: Any):
        self.manager = manager_ref
        self.playbooks_dir = Path(__file__).parent / "playbooks"
        self._playbook_cache: Dict[str, PlaybookSchema] = {}
        self._trace = bool(os.getenv("SAIVERSE_SEA_TRACE"))
        self._emitters = RuntimeEmitters(runtime=self)
        self.session_lifecycle = SessionLifecycle(runtime=self, manager_ref=manager_ref)
        self._runtime_engine = RuntimeEngine(
            runtime=self,
            manager_ref=manager_ref,
            llm_selector=self._select_llm_client,
            emitters={"speak": self._emit_speak, "say": self._emit_say, "think": self._emit_think},
        )
        # Pulse-level log contexts (keyed by pulse_id)
        self._pulse_contexts: Dict[str, Any] = {}  # Dict[str, PulseContext]

    # ---------------- meta entrypoints -----------------
    def run_meta_user(
        self,
        persona,
        user_input: str,
        building_id: str,
        metadata: Optional[Dict[str, Any]] = None,
        meta_playbook: Optional[str] = None,
        args: Optional[Dict[str, Any]] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        cancellation_token: Optional[CancellationToken] = None,
        pulse_type: str = "user",
        pre_spells: Optional[List[str]] = None,
        origin_track_id: Optional[str] = None,
    ) -> List[str]:
        """Router -> subgraph -> speak. Returns spoken strings for gateway/UI.

        ``origin_track_id`` 指定時はその Track の文脈で Pulse を走らせる。
        Handler 起動経路 (例: UserConversationTrackHandler) が、pending/alert
        状態の Track でも文脈を保持するために渡してくる。
        未指定時は ``_resolve_pulse_root_line`` が ``get_running`` で取りに行く。
        """
        # Check for cancellation before starting
        if cancellation_token:
            cancellation_token.raise_if_cancelled()

        # Beat ロック + 関所 (beat_execution_context.md §3.4 / execution_ledger.md
        # §2.2-2.3): Pulse 本体 (playbook 実行 + 応答後 Metabolism まで) を
        # persona 単位で直列化する。関所 (pending flush) が通らない場合は
        # hold が BeatGateClosedError を投げ、そのまま呼び出し側
        # (PulseController) へ伝播する — 実行は始まっておらず副作用ゼロ。
        # spell → run_playbook の子ライン、Pulse 内の Metabolism / gold_panning
        # は同一スレッド再入 (RLock) で親 Beat の直列域を継承する。
        # manager に beat_gate が無い環境 (テストの SimpleNamespace 等) では
        # hold_beat が nullcontext を返して no-op。
        from sea.beat_gate import hold_beat
        with hold_beat(
            self.manager,
            getattr(persona, "persona_id", None),
            purpose=pulse_type or "pulse",
        ):
            return self._run_meta_user_locked(
                persona,
                user_input,
                building_id,
                metadata=metadata,
                meta_playbook=meta_playbook,
                args=args,
                event_callback=event_callback,
                cancellation_token=cancellation_token,
                pulse_type=pulse_type,
                pre_spells=pre_spells,
                origin_track_id=origin_track_id,
            )

    def _run_meta_user_locked(
        self,
        persona,
        user_input: str,
        building_id: str,
        metadata: Optional[Dict[str, Any]] = None,
        meta_playbook: Optional[str] = None,
        args: Optional[Dict[str, Any]] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        cancellation_token: Optional[CancellationToken] = None,
        pulse_type: str = "user",
        pre_spells: Optional[List[str]] = None,
        origin_track_id: Optional[str] = None,
    ) -> List[str]:
        """:meth:`run_meta_user` の本体 (Beat ロック保持下で実行される)。"""
        # Store pulse_type in persona for tools to access
        persona._current_pulse_type = pulse_type

        # Resolve Pulse-root track_id up-front so downstream injectors
        # (DynamicState event message, building auto-ingest, emit_*) can all
        # tag their writes with the Track scope. Caller-supplied origin_track_id
        # wins; otherwise falls back to ``get_running``. Same resolver as the
        # one used for push_line below.
        _root_role, _root_track_id = self._resolve_pulse_root_line(
            persona, override_track_id=origin_track_id,
        )
        # 認知モデル v0.2 (§10.2): Pulse-root のアスペクトを pulse_type から導出する。
        # auto→AUTONOMOUS / meta_judgment→META / それ以外→CONVERSATION。これが
        # line_role / scope / model tier の供給源となり、track の entry_line_role
        # (legacy, main_line/sub_line) には依存しない。
        from sea.pulse_context import aspect_from_pulse_type
        _root_aspect = aspect_from_pulse_type(pulse_type)

        # --- per_persona MCP ツール一覧の取得 (Pulse 頭) ---
        # ツール一覧の真実はサーバー側にしか無く、証言できるのは本人の鍵で
        # 張った生きた接続だけ (docs/intent/mcp_addon_integration.md §I)。
        # ここで本人の接続を張って一覧を取得してから、下の検知が走る順序が肝 —
        # 逆だと一覧の変動が知覚バッファに積まれず、次の Pulse まで届かない。
        # notify=False: 直後の検知フェーズが無条件で全 Section を見るので、
        # ここで検知器を呼ぶと同じ差分を二度検知することになる。
        # per_persona サーバーが無い環境では has_per_persona_servers の
        # 前置き判定だけで即戻る。
        refresh_mcp_tools_at_head(
            persona, self.manager, building_id, connect=True, notify=False,
        )

        # --- 知覚の「検知」フェーズ (バッファへ push、まだ消費しない) ---
        # 世界状態の差分 (入退室・アイテム・スペル 等) と入室時想起を検知し、知覚
        # バッファへ push する (SAIMemory へは直接入れない)。Phase 2 で
        # inject_diff_notifications を「検知＝push」に変更した。詳細:
        # docs/intent/perception_buffer.md §4.5 / §5.1。
        try:
            from saiverse.dynamic_state import DynamicStateManager
            DynamicStateManager.maybe_inject_event_messages(persona, self.manager)
        except Exception:
            LOGGER.exception("[dynamic_state] Event detection failed in run_meta_user")

        # 同Building内の他ペルソナ発言とユーザーメッセージを building_histories から時系列順に取り込む
        # （ユーザーメッセージは manager が事前に building_histories へ追加済み）
        # ※会話取り込みの知覚バッファ統合は Phase 2 (会話統合) で対応予定。現状は従来経路。
        try:
            from builtin_data.tools.get_building_messages import auto_ingest_building_messages
            auto_ingest_building_messages(persona, self.manager, origin_track_id=_root_track_id)
        except Exception:
            LOGGER.exception("[auto_ingest] Failed in run_meta_user")

        # --- 知覚の「消費」フェーズ (flush) ---
        # 未消費の知覚 (REST 由来のメタ記憶訂正 + 上で検知した world_state/persona_recall)
        # を型別 reduce して 1 メッセージで SAIMemory へ書き出す。主観時間は Pulse でのみ
        # 進むので、ここが消費点。会話・schedule・auto の Pulse は全部 run_meta_user を
        # 通る (pulse_controller.py) ため、この 1 箇所でそれらの消費が成立する。
        # ⚠️ 「全ての Pulse がここを通る」ではない — 作業セッション
        # (sea/work_session.py) は自分の PulseContext を作る別の Pulse root で、
        # 頭の処理 (知覚消費・MCP ツール取得) を自前で持っている。頭に一手
        # 増やすときは両方に入れる (片方だけ直して素通しを作った実績あり)。
        # 検知 (上) → 消費 (ここ) の順序が重要 (同 Pulse 内で検知分も消費するため)。
        try:
            sai_mem = getattr(persona, "sai_memory", None)
            if sai_mem is not None:
                sai_mem.flush_perception_buffer()
        except Exception:
            LOGGER.exception("[perception_buffer] flush failed in run_meta_user")

        # スケジュール実行時はプロンプトをペルソナ自身のhistoryに直接追加（他のペルソナには見せない）
        if pulse_type == "schedule" and user_input:
            try:
                schedule_msg: Dict[str, Any] = {
                    "role": "user",
                    "content": user_input,
                    "metadata": {"with": ["system"], **(metadata or {})},
                }
                persona.history_manager.add_to_persona_only(schedule_msg)
                LOGGER.debug("[schedule] Recorded schedule prompt to persona history for %s", getattr(persona, "persona_id", "?"))
            except Exception:
                LOGGER.exception("[schedule] Failed to record schedule prompt to persona history")

        # Use user-selected meta playbook if specified, otherwise choose automatically
        if meta_playbook:
            playbook = self._load_playbook_for(meta_playbook, persona, building_id)
            if playbook is None:
                LOGGER.warning("Meta playbook '%s' not found; aborting execution", meta_playbook)
                if event_callback:
                    event_callback({
                        "type": "error",
                        "code": "playbook_not_found",
                        "meta_playbook": meta_playbook,
                    })
                return [f"指定されたプレイブック '{meta_playbook}' が見つかりません。プレイブックIDを確認してください。"]
        else:
            if pulse_type != "user":
                # 席違いフォールバックの機械検査 (autonomous_pulse_vehicle.md §D)。
                # 「auto 系 Pulse は必ず meta_playbook 指定」という不変条件は従来
                # docstring の散文にしか無く、破られても無音で会話の器に落ちて
                # いた (2026-08-08 のコマ開始 Pulse がこれで発話化した)。禁止は
                # しない — リマインド等、会話の器を意図して使う schedule があり
                # 得るため、まず観測可能にする。
                LOGGER.warning(
                    "[sea] non-user pulse fell back to the default conversation "
                    "playbook (type=%s persona=%s, meta_playbook unspecified) — "
                    "自律系 Pulse は器を明示すること",
                    pulse_type, getattr(persona, "persona_id", None),
                )
            playbook = self._choose_playbook(kind="user", persona=persona, building_id=building_id)
        # Build effective args: auto-include user_input as "input" if not explicitly set
        effective_args = dict(args or {})
        if user_input and "input" not in effective_args:
            effective_args["input"] = user_input
        # 非常畳み (arasuji_levels.md §14-3): 話しかけた時点で提示ウィンドウが
        # 高水位を既に超過しているイレギュラー (休眠 model の復帰等) は、応答
        # より先に畳んで呼び出し失敗の連鎖を断つ。通常は "skip" で素通り。
        _pre_model_key: Optional[str] = None
        try:
            _pre_probe_state: Dict[str, Any] = {}
            if pulse_type is not None:
                _pre_probe_state["_pulse_type"] = pulse_type
            _pre_model_key = resolve_execution_context(
                persona, None, state=_pre_probe_state,
            ).model_key
            self.session_lifecycle.maybe_run_emergency_precompaction(
                persona, building_id, event_callback, model_key=_pre_model_key,
            )
        except Exception:
            LOGGER.exception("[metabolism] Emergency pre-compaction failed")
        # 読み戻し (arasuji_levels.md §15): 非常畳みの対称。話しかけた時点で
        # 提示ウィンドウが残す量を下回っていたら、応答より先に畳んだところを
        # 開き直す (LLM なし・帳簿のみ)。通常は "skip" で素通り。
        # run_meta_user は user / schedule / auto の共通入口なので、§15-4 の
        # 「発火は user Pulse の会話開始時のみ」をここで絞る — 自律 Pulse の
        # 軽量 model に会話用の厚い生ログを開かない。
        if pulse_type in (None, "user"):
            try:
                self.session_lifecycle.maybe_run_window_refill(
                    persona, building_id, model_key=_pre_model_key,
                )
            except Exception:
                LOGGER.exception("[metabolism] window refill failed")

        # ``_root_role`` / ``_root_track_id`` were resolved up-front (above the
        # injector calls). Pulse 中に emit_speak/emit_say/emit_think などの
        # emitter 経路から書き込まれるメッセージにも origin_track_id を付与する
        # ため、persona に一時保持する。emitters 側はここから読む。
        # run_meta_user 終了時に finally でクリアする (handoff_2026-05-10)。
        prev_pulse_track = getattr(persona, "_current_pulse_origin_track_id", None)
        persona._current_pulse_origin_track_id = _root_track_id
        try:
            result = self._run_playbook(
                playbook, persona, building_id, user_input,
                # auto_mode = 「応答ループにユーザーが居ない Pulse か」。run_meta_user は
                # user / schedule / auto の共通入口なので pulse_type から導出する。
                # None は PulseController を経ない直接呼び出し (レガシー) のみで、
                # 確認ダイアログを黙って自動承認しない側 (=user 扱い) に倒す。
                auto_mode=(pulse_type not in (None, "user")),
                record_history=True, event_callback=event_callback,
                cancellation_token=cancellation_token, pulse_type=pulse_type,
                initial_params=effective_args if effective_args else None,
                pulse_line_role=_root_role,
                pulse_line_track_id=_root_track_id,
                pulse_line_aspect=_root_aspect,
                pre_spells=pre_spells,
            )
        finally:
            persona._current_pulse_origin_track_id = prev_pulse_track

        # Post-response metabolism check (DB ベースで件数比較)。
        # model_key = この Pulse の実行 model (beat_execution_context.md §3.2 —
        # 閾値と退役は model ごと)。run_meta_user は ExecutionContext を保持しない
        # (解決は _run_playbook 内で完結する) ため、runtime_runner の probe と同じ
        # 導出 (pulse_type → legacy tier フォールバック) をここで行う。root aspect
        # の tier (AUTONOMOUS=lightweight / CONVERSATION・META=standard) と一致する
        # ことは §6-3b 検収で照合済み。
        from database.building_messages import fetch_max_seq
        bh_before = fetch_max_seq(getattr(self.manager, "SessionLocal", None), building_id)
        try:
            _mk_probe_state: Dict[str, Any] = {}
            if pulse_type is not None:
                _mk_probe_state["_pulse_type"] = pulse_type
            _metabolism_model_key = resolve_execution_context(
                persona, None, state=_mk_probe_state,
            ).model_key
            self.session_lifecycle.maybe_run_metabolism(
                persona, building_id, event_callback, model_key=_metabolism_model_key,
            )
        except Exception:
            LOGGER.exception("[metabolism] Post-response metabolism failed")
        bh_after = fetch_max_seq(getattr(self.manager, "SessionLocal", None), building_id)
        if bh_before != bh_after:
            LOGGER.warning(
                "[metabolism] building_messages[%s] max_seq changed during metabolism: %d -> %d",
                building_id, bh_before, bh_after,
            )

        return result

    # ---------------- helpers -----------------
    def _resolve_pulse_root_line(
        self,
        persona: Any,
        *,
        override_track_id: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Resolve (entry_line_role, track_id) for the persona's current Pulse root.

        Resolution order:
        1. ``override_track_id`` if supplied — Handler-driven path. The Track
           may be in any status (running / alert / pending), but we still
           anchor messages to it so SAIMemory queries can find them
           (handoff_2026-05-10).
        2. ``track_manager.get_running`` — legacy Same-Persona invariant 1
           path: at most one running Track per persona.

        Returns (None, None) when neither resolves — runtime then skips
        ``push_line`` and stored messages get NULL line_role/origin_track_id
        (pre-v0.11 behavior).
        """
        try:
            track_manager = getattr(self.manager, "track_manager", None)
            if track_manager is None:
                return None, None
            if override_track_id:
                role = track_manager.get_entry_line_role(override_track_id)
                LOGGER.debug(
                    "[runtime] _resolve_pulse_root_line override: track_id=%s role=%s",
                    override_track_id, role,
                )
                return role, override_track_id
            persona_id = getattr(persona, "persona_id", None)
            if not persona_id:
                return None, None
            running = track_manager.get_running(persona_id)
            if running is None:
                LOGGER.debug(
                    "[runtime] _resolve_pulse_root_line: no running Track for persona=%s "
                    "(messages will be stored with NULL origin_track_id)",
                    persona_id,
                )
                return None, None
            role = track_manager.get_entry_line_role(running.track_id)
            return role, running.track_id
        except Exception:
            LOGGER.exception(
                "[runtime] Failed to resolve Pulse-root line for persona=%s",
                getattr(persona, "persona_id", None),
            )
            return None, None

    # ---------------- core runner -----------------
    def _run_playbook(
        self,
        playbook: PlaybookSchema,
        persona: Any,
        building_id: str,
        user_input: Optional[str],
        auto_mode: bool,
        record_history: bool = True,
        parent_state: Optional[Dict[str, Any]] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        cancellation_token: Optional[CancellationToken] = None,
        pulse_type: Optional[str] = None,
        initial_params: Optional[Dict[str, Any]] = None,
        isolate_pulse_context: bool = False,
        line: str = "main",
        pulse_line_role: Optional[str] = None,
        pulse_line_track_id: Optional[str] = None,
        pulse_line_aspect: Optional[Any] = None,  # sea.pulse_context.Aspect
        pre_spells: Optional[List[str]] = None,
    ) -> List[str]:
        return run_playbook(
            self,
            playbook,
            persona,
            building_id,
            user_input,
            auto_mode,
            record_history=record_history,
            parent_state=parent_state,
            event_callback=event_callback,
            cancellation_token=cancellation_token,
            pulse_type=pulse_type,
            initial_params=initial_params,
            isolate_pulse_context=isolate_pulse_context,
            line=line,
            pulse_line_role=pulse_line_role,
            pulse_line_track_id=pulse_line_track_id,
            pulse_line_aspect=pulse_line_aspect,
            pre_spells=pre_spells,
        )

    # LangGraph compile wrapper -----------------------------------------
    def _compile_with_langgraph(
        self,
        playbook: PlaybookSchema,
        persona: Any,
        building_id: str,
        user_input: Optional[str],
        auto_mode: bool,
        base_messages: List[Dict[str, Any]],
        pulse_id: str,
        parent_state: Optional[Dict[str, Any]] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        cancellation_token: Optional[CancellationToken] = None,
        pulse_type: Optional[str] = None,
        isolate_pulse_context: bool = False,
        pulse_line_role: Optional[str] = None,
        pulse_line_track_id: Optional[str] = None,
        pulse_line_aspect: Optional[Any] = None,  # sea.pulse_context.Aspect
        line: str = "main",
    ) -> Optional[List[str]]:
        return compile_with_langgraph_impl(
            self,
            playbook,
            persona,
            building_id,
            user_input,
            auto_mode,
            base_messages,
            pulse_id,
            parent_state=parent_state,
            event_callback=event_callback,
            cancellation_token=cancellation_token,
            pulse_type=pulse_type,
            isolate_pulse_context=isolate_pulse_context,
            pulse_line_role=pulse_line_role,
            pulse_line_track_id=pulse_line_track_id,
            pulse_line_aspect=pulse_line_aspect,
            line=line,
        )

    def _lg_llm_node(self, node_def: Any, persona: Any, building_id: str, playbook: PlaybookSchema, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None):
        return lg_llm_node_impl(self, node_def, persona, building_id, playbook, event_callback)

    def _default_temperature(self, persona: Any) -> Optional[float]:
        try:
            model_name = getattr(persona, "model", None)
            if not model_name:
                return None
            defaults = get_model_parameter_defaults(model_name)
            temp = defaults.get("temperature")
            if temp is None:
                return None
            try:
                return float(temp)
            except Exception:
                return None
        except Exception:
            return None

    def _accumulate_usage(
        self,
        state: Dict[str, Any],
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        cached_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> None:
        """Accumulate LLM usage into the pulse-level accumulator.

        Args:
            state: Current state dict containing pulse_usage_accumulator
            model: Model identifier
            input_tokens: Number of input tokens
            output_tokens: Number of output tokens
            cost_usd: Cost in USD
            cached_tokens: Number of tokens served from cache
            cache_write_tokens: Number of tokens written to cache
        """
        accumulator = state.get("_pulse_usage_accumulator")
        if accumulator is None:
            return
        accumulator["total_input_tokens"] += input_tokens
        accumulator["total_output_tokens"] += output_tokens
        accumulator["total_cached_tokens"] += cached_tokens
        accumulator["total_cache_write_tokens"] += cache_write_tokens
        accumulator["total_cost_usd"] += cost_usd
        accumulator["call_count"] += 1
        if model and model not in accumulator["models_used"]:
            accumulator["models_used"].append(model)

    def _resolve_cache_ttl_str(self, persona_id: Optional[str] = None) -> str:
        """Resolve the active cache TTL string ("5m"/"1h") for a persona.

        解決は ``manager.resolve_persona_cache`` に集約 (per-persona override →
        global 既定)。これが per-persona TTL の単一の解決点
        (docs/intent/cache_lifecycle_control.md §5.4 の付け替え先)。
        ``persona_id=None`` (= 旧呼び出し) は global を返す。
        """
        if self.manager and hasattr(self.manager, "resolve_persona_cache"):
            return self.manager.resolve_persona_cache(persona_id)[1]
        if self.manager and hasattr(self.manager, "state"):
            return getattr(self.manager.state, "cache_ttl", "5m")
        return "5m"

    def _resolve_cache_enabled(self, persona_id: Optional[str] = None) -> bool:
        """Resolve whether cache is enabled for a persona (per-persona → global)。"""
        if self.manager and hasattr(self.manager, "resolve_persona_cache"):
            return self.manager.resolve_persona_cache(persona_id)[0]
        if self.manager and hasattr(self.manager, "state"):
            return getattr(self.manager.state, "cache_enabled", True)
        return True

    def _get_cache_kwargs(self, persona_id: Optional[str] = None) -> Dict[str, Any]:
        """Get cache settings for LLM client calls.

        Returns:
            Dict with enable_cache and cache_ttl kwargs for Anthropic client.
            Non-Anthropic clients will ignore these kwargs.

        ``persona_id`` を渡すと per-persona override (enabled/ttl、無ければ global) を使う。
        """
        if self.manager and hasattr(self.manager, "state"):
            return {
                "enable_cache": self._resolve_cache_enabled(persona_id),
                "cache_ttl": self._resolve_cache_ttl_str(persona_id),
            }
        return {"enable_cache": True, "cache_ttl": "5m"}

    @staticmethod
    def _ensure_llama_server(model_name: str) -> None:
        """Re-launch llama.cpp server if it was stopped by idle timeout."""
        try:
            from saiverse.model_configs import get_model_config
            config = get_model_config(model_name)
            if not isinstance(config, dict) or not config.get("llama_server"):
                return
            from llm_clients.llama_server import get_server_manager
            server_base = config.get("base_url", "http://127.0.0.1:8080/v1")
            get_server_manager().ensure_running(server_base, config)
        except Exception as exc:
            LOGGER.debug("[sea] _ensure_llama_server check failed (non-fatal): %s", exc)

    def select_llm_client(
        self,
        node_def: Any,
        persona: Any,
        execution_context: Optional[ExecutionContext] = None,
        needs_structured_output: bool = False,
        state: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Any, str]:
        """Select the LLM client for one Beat and return ``(client, model_key)``.

        ExecutionContext 経由が主経路 (beat_execution_context §2.1): tier は
        ``execution_context.aspect``、model は Beat 開始時に一度だけ解決した
        ``execution_context.model_key`` を使い、persona の可変属性を再推測しない。
        ``execution_context=None`` の legacy 経路では従来どおり state の
        PulseContext / フラグから導出する (挙動は同一)。

        戻り値の model は「実際に使う client の model」。structured-output
        fallback 等で ``execution_context.model_key`` と異なる model になった
        場合、呼び出し側は ``execution_context.with_model()`` で差し替える。

        Args:
            node_def: Node definition from playbook
            persona: Persona object
            execution_context: Beat 開始点で解決した実行の身分証 (推奨経路)
            needs_structured_output: Whether this node requires structured output
            state: Current execution state. legacy 経路の tier 導出
                   (_force_lightweight_model / _pulse_type=='auto') に使う。
        """
        # 軽量モデル判定 (認知モデル v0.2 §10.3):
        # ExecutionContext があればその aspect、無ければ active LineFrame の
        # アスペクトから model tier を導出する。
        # WORKER (run_playbook サブライン) / AUTONOMOUS (自律) → lightweight、
        # CONVERSATION / META → standard。aspect の無い legacy frame では従来の
        # _force_lightweight_model / pulse_type=='auto' フォールバックで判定する。
        _aspect_tier: Optional[str] = None
        if execution_context is not None:
            if execution_context.aspect is not None:
                _aspect_tier = execution_context.aspect.model_tier
        elif state:
            _pc = state.get("_pulse_context")
            if _pc is not None:
                try:
                    _cur = _pc.current_line()
                except Exception:
                    _cur = None
                if _cur is not None:
                    _aspect_tier = getattr(_cur, "model_tier", None)
        if _aspect_tier is not None:
            force_lightweight = (_aspect_tier == "lightweight")
        else:
            force_lightweight = bool(state and (
                state.get("_force_lightweight_model")
                or state.get("_pulse_type") == "auto"
            ))
        model_type = "lightweight" if force_lightweight else "normal"

        LOGGER.info("[sea] Node model_type: %s (node_id=%s, force_light=%s)", model_type, getattr(node_def, "id", "unknown"), force_lightweight)

        # First, select base client based on model_type.
        # model 名は ExecutionContext があればその解決値 (resolve_execution_context
        # が同じチェーンで導出済み)、無ければ従来チェーンで導出する。
        if model_type == "lightweight":
            # Try persona's lightweight_llm_client first
            lightweight_client = getattr(persona, "lightweight_llm_client", None)
            LOGGER.info("[sea] lightweight_client exists: %s", lightweight_client is not None)
            lightweight_model_name = (
                execution_context.model_key if execution_context is not None
                else getattr(persona, "lightweight_model", None) or _get_default_lightweight_model()
            )
            if lightweight_client:
                LOGGER.info("[sea] Using persona's lightweight_llm_client")
                base_client = lightweight_client
                base_model = lightweight_model_name
            else:
                # Fallback: create a temporary lightweight client
                LOGGER.info("[sea] Persona has no lightweight_llm_client; creating temporary client with default model")
                LOGGER.info("[sea] Using lightweight model: %s", lightweight_model_name)
                try:
                    from llm_clients import get_llm_client
                    from saiverse.model_configs import get_context_length, get_model_provider
                    lw_context = get_context_length(lightweight_model_name)
                    provider = get_model_provider(lightweight_model_name)
                    base_client = get_llm_client(lightweight_model_name, provider, lw_context)
                    base_model = lightweight_model_name
                except Exception as exc:
                    LOGGER.warning("[sea] Failed to create lightweight client: %s; falling back to normal client", exc)
                    base_client = persona.llm_client
                    base_model = getattr(persona, "model", "unknown")
        else:
            # Default: use normal client
            LOGGER.info("[sea] Using normal llm_client")
            base_client = persona.llm_client
            base_model = (
                execution_context.model_key if execution_context is not None
                else getattr(persona, "model", "unknown")
            )
            LOGGER.info("[sea] persona.model=%s, llm_client type=%s", base_model, type(base_client).__name__)

        # Ensure llama.cpp server is running (may have been stopped by idle timeout)
        self._ensure_llama_server(base_model)

        # Guard: if no client was resolved, raise a clear error
        if base_client is None:
            persona_name = getattr(persona, "persona_name", "unknown")
            raise LLMError(
                f"LLM client is not initialized for persona '{persona_name}' (model={base_model})",
                user_message=f"ペルソナ「{persona_name}」のLLMクライアントが初期化されていません。チャットオプションでモデルを選択してください。",
            )

        # If structured output is needed, check if the selected model supports it
        if needs_structured_output:
            from saiverse.model_configs import get_context_length, get_model_provider, supports_structured_output
            if not supports_structured_output(base_model):
                lw_model = getattr(persona, "lightweight_model", None) or _get_default_lightweight_model()
                if not supports_structured_output(lw_model):
                    persona_name = getattr(persona, "persona_name", "unknown")
                    raise LLMError(
                        f"Neither DEFAULT_MODEL '{base_model}' nor LIGHTWEIGHT_MODEL '{lw_model}' "
                        f"supports structured output for persona '{persona_name}'",
                        user_message=(
                            f"現在選択されているモデル（{base_model}）も軽量モデル（{lw_model}）も"
                            "構造化出力に対応していません。チャットオプションから対応モデルに変更してください。"
                        ),
                    )
                LOGGER.info("[sea] Model '%s' doesn't support structured output, "
                            "falling back to lightweight model: %s", base_model, lw_model)
                try:
                    from llm_clients import get_llm_client
                    lw_context = get_context_length(lw_model)
                    lw_provider = get_model_provider(lw_model)
                    return get_llm_client(lw_model, lw_provider, lw_context), lw_model
                except Exception as exc:
                    LOGGER.warning("[sea] Failed to create lightweight client for structured output: %s; "
                                   "using base client", exc)
                    return base_client, base_model

        return base_client, base_model

    def _select_llm_client(self, node_def: Any, persona: Any, needs_structured_output: bool = False, state: Optional[Dict[str, Any]] = None, execution_context: Optional[ExecutionContext] = None) -> Any:
        """後方互換ラッパー — client のみ返す。実体は ``select_llm_client``。

        新しい呼び出しは Beat 開始点で ``resolve_execution_context`` した
        ExecutionContext を ``select_llm_client`` へ渡し、実 model 名も受け取ること。
        """
        client, _model = self.select_llm_client(
            node_def, persona,
            execution_context=execution_context,
            needs_structured_output=needs_structured_output,
            state=state,
        )
        return client

    def _build_tools_spec(self, tool_names: List[str], llm_client: Any) -> List[Any]:
        """Build tools spec for LLM based on available tool names and llm_client type."""
        from tools import GEMINI_TOOLS_SPEC, OPENAI_TOOLS_SPEC

        LOGGER.info("[sea] _build_tools_spec called with tool_names: %s", tool_names)

        # Determine provider from llm_client class name
        client_class_name = type(llm_client).__name__
        LOGGER.info("[sea] LLM client class: %s", client_class_name)

        if client_class_name in ("OpenAIClient", "AnthropicClient", "OllamaClient", "NvidiaNIMClient"):
            # Filter OpenAI tools spec (OpenAI-compatible)
            LOGGER.info("[sea] Using OpenAI-compatible tools format (client: %s)", client_class_name)
            LOGGER.info("[sea] Filtering from OPENAI_TOOLS_SPEC (total: %d)", len(OPENAI_TOOLS_SPEC))
            filtered = [
                tool for tool in OPENAI_TOOLS_SPEC
                if tool.get("function", {}).get("name") in tool_names
            ]
            LOGGER.info("[sea] Built OpenAI tools spec: %d tools", len(filtered))
            for tool in filtered:
                LOGGER.info("[sea] - OpenAI tool: %s", tool.get("function", {}).get("name"))
                LOGGER.info("[sea]   Full spec: %s", tool)
            return filtered
        else:
            # Filter Gemini tools spec - combine all matching declarations into a single Tool
            LOGGER.info("[sea] Using Gemini tools format (client: %s)", client_class_name)
            from google.genai import types
            all_matching_decls = []
            for tool in GEMINI_TOOLS_SPEC:
                if hasattr(tool, "function_declarations"):
                    matching_decls = [
                        decl for decl in tool.function_declarations
                        if decl.name in tool_names
                    ]
                    all_matching_decls.extend(matching_decls)

            if all_matching_decls:
                # Gemini requires all function_declarations in a single Tool object
                filtered = [types.Tool(function_declarations=all_matching_decls)]
                LOGGER.info("[sea] Built Gemini tools spec: 1 Tool with %d function_declarations", len(all_matching_decls))
                for decl in all_matching_decls:
                    LOGGER.info("[sea] - Gemini function_declaration: name=%s, description=%s", decl.name, decl.description)
                    LOGGER.info("[sea]   parameters: %s", decl.parameters)
            else:
                filtered = []
                LOGGER.info("[sea] Built Gemini tools spec: 0 tools")
            return filtered

    def _dump_llm_io(
        self,
        playbook_name: str,
        node_id: str,
        persona: Any,
        messages: List[Dict[str, Any]],
        output_text: str,
    ) -> None:
        """Log LLM I/O to the unified LLM log file."""
        try:
            from saiverse.logging_config import log_llm_request, log_llm_response
            persona_id = getattr(persona, "persona_id", None)
            persona_name = getattr(persona, "persona_name", None)
            source = f"sea/{playbook_name}"
            log_llm_request(source, node_id, persona_id, persona_name, messages)
            log_llm_response(source, node_id, persona_id, persona_name, output_text)
        except Exception:
            LOGGER.warning("failed to dump LLM io", exc_info=True)

    def _debug_playbook(self, pb: PlaybookSchema, source: str) -> None:
        if not self._trace:
            return
        try:
            summary = {
                "source": source,
                "name": pb.name,
                "start": pb.start_node,
                "nodes": [
                    {
                        "id": n.id,
                        "type": getattr(n, "type", None),
                        "next": getattr(n, "next", None),
                        "action": getattr(n, "action", None),
                    }
                    for n in pb.nodes
                ],
            }
            LOGGER.debug("[sea] playbook loaded: %s", json.dumps(summary, ensure_ascii=False))
        except Exception:
            LOGGER.debug("[sea] playbook debug failed", exc_info=True)

    def _add_playbook_enum(self, schema: Dict[str, Any], available_playbooks_json: str) -> Dict[str, Any]:
        """Dynamically add enum constraint to playbook field in response_schema."""
        import copy
        import json

        try:
            # Parse available_playbooks JSON
            playbooks_list = json.loads(available_playbooks_json) if isinstance(available_playbooks_json, str) else available_playbooks_json
            if not isinstance(playbooks_list, list):
                return schema

            # Extract playbook names
            playbook_names = [pb.get("name") for pb in playbooks_list if isinstance(pb, dict) and "name" in pb]
            if not playbook_names:
                return schema

            # Deep copy schema to avoid modifying the original
            schema_copy = copy.deepcopy(schema)

            # Add enum to playbook field if it exists
            if "properties" in schema_copy and "playbook" in schema_copy["properties"]:
                schema_copy["properties"]["playbook"]["enum"] = playbook_names
                LOGGER.debug("[sea] Added dynamic enum to playbook field: %s", playbook_names)

            return schema_copy

        except Exception as exc:
            LOGGER.warning("[sea] Failed to add playbook enum: %s", exc)
            return schema

    def _process_structured_output(self, node_def: Any, text: str, state: Dict[str, Any]) -> bool:
        return process_structured_output(node_def, text, state)

    def _apply_output_mapping(self, state: Dict[str, Any], output_key: str, mapping: Dict[str, str]) -> None:
        apply_output_mapping(state, output_key, mapping)

    def _resolve_nested_value(self, data: Any, path: str) -> Any:
        return resolve_nested_value(data, path)

    def _store_structured_result(self, state: Dict[str, Any], key: str, data: Any) -> None:
        store_structured_result(state, key, data)

    def _flatten_dict(self, value: Any, prefix: str = "") -> Dict[str, Any]:
        return flatten_dict(value, prefix)

    def _resolve_state_value(self, state: Dict[str, Any], key: str) -> Any:
        return resolve_state_value(state, key)

    def _extract_structured_json(self, text: str) -> Optional[Dict[str, Any]]:
        return extract_structured_json(text)

    def _lg_tool_node(self, node_def: Any, persona: Any, playbook: PlaybookSchema, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None, auto_mode: bool = False):
        return self._runtime_engine.lg_tool_node(node_def, persona, playbook, event_callback, auto_mode=auto_mode)

    def _lg_tool_call_node(self, node_def: Any, persona: Any, playbook: PlaybookSchema, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None, auto_mode: bool = False):
        return lg_tool_call_node_impl(self, node_def, persona, playbook, event_callback, auto_mode=auto_mode)

    # ── Playbook permission helpers ──────────────────────────────────
    def _get_playbook_permission(self, city_id: int, playbook_name: str) -> str:
        """Return the permission level for *playbook_name* in *city_id*.

        When no row exists the default is ``"ask_every_time"``.
        """
        from database.models import PlaybookPermission
        try:
            db = self.manager.SessionLocal()
            try:
                row = (
                    db.query(PlaybookPermission)
                    .filter(
                        PlaybookPermission.CITYID == city_id,
                        PlaybookPermission.playbook_name == playbook_name,
                    )
                    .first()
                )
                return row.permission_level if row else "ask_every_time"
            finally:
                db.close()
        except Exception:
            LOGGER.warning("[sea][perm] Failed to query permission for %s", playbook_name, exc_info=True)
            return "ask_every_time"

    def _set_playbook_permission(self, city_id: int, playbook_name: str, level: str) -> None:
        """Insert or update the permission level for *playbook_name* in *city_id*."""
        from database.models import PlaybookPermission
        try:
            db = self.manager.SessionLocal()
            try:
                row = (
                    db.query(PlaybookPermission)
                    .filter(
                        PlaybookPermission.CITYID == city_id,
                        PlaybookPermission.playbook_name == playbook_name,
                    )
                    .first()
                )
                if row:
                    row.permission_level = level
                else:
                    db.add(PlaybookPermission(
                        CITYID=city_id,
                        playbook_name=playbook_name,
                        permission_level=level,
                    ))
                db.commit()
                LOGGER.info("[sea][perm] Set %s → %s (city=%s)", playbook_name, level, city_id)
            finally:
                db.close()
        except Exception:
            LOGGER.warning("[sea][perm] Failed to set permission for %s", playbook_name, exc_info=True)

    def decide_playbook_permission(
        self,
        city_id: Optional[int],
        playbook_name: str,
        persona: Any,
        *,
        user_configured: bool,
        auto_mode: bool,
        event_callback: Optional[Callable[[Dict[str, Any]], None]],
    ) -> Tuple[bool, Optional[str]]:
        """City スコープの Playbook 許可を判定する、ただ一つの場所。

        Playbook を起こす口は 2 つある — Playbook の EXEC ノード
        (``sea/runtime_engine.py``) と ``/run_playbook`` スペル
        (``builtin_data/tools/run_playbook.py``)。同じ規則を二度書いた結果、
        片方だけに事前承認の考慮があり、スケジュール UI が実際に使うスペル側
        では設定済みの自動化が拒否されていた (2026-08-17 レビュー)。判定は
        ここに集約し、呼び出し側は結果の見せ方だけを持つ。

        判定順序そのものが仕様:

        1. **``blocked`` は常に拒否** — 完全封印。ユーザー自身の指定でも通さない
        2. **ユーザー自身が書いた起動なら許可** (``user_configured``) — チャット
           UI の「ツール指定」やスケジュール設定画面で、ユーザーがその Playbook
           を名指しした起動。指定した行為が承認にあたるので、確認し直さない。
           ``user_only`` (設定画面の表示は「ユーザー指定時のみ」、確認ダイアログの
           「ペルソナには使わせない」が書き込む値) もここで通る — 禁止の対象は
           ペルソナであってユーザー本人ではない (まはー裁定 2026-08-17)。
           **承認されているのは「その起動」であって Pulse ではない** — 同じ
           Pulse の中でペルソナが別の Playbook を思いついて唱えた場合は、
           下の通常の道を通る (Pulse 種別で許すと、設定画面で選んだ覚えのない
           Playbook まで無確認で走る)
        3. **``user_only`` はペルソナ起動なら拒否**
        4. **``ask_every_time`` 以外 (= ``auto_allow``) は許可**
        5. **応答ループにユーザーが居ない (auto) なら拒否** — 誰も見ていない
           UI へ確認を出してブロックしない
        6. **確認の宛先が無ければ拒否** — チャネル無しで黙って許可しない
        7. それ以外は確認ダイアログ (``always_allow`` / ``never_use`` は
           恒久設定として書き込む)

        Args:
            city_id: 判定対象の City。``None`` なら判定せず許可 (City 文脈の
                無い CLI / テスト経路)。
            user_configured: この起動をユーザー自身が指定したか。ペルソナの
                認知が選んだ起動では必ず False。

        Returns:
            ``(allowed, denial_reason)``。拒否のとき ``denial_reason`` は
            ペルソナへ見せる理由文。
        """
        if city_id is None:
            return True, None

        perm = self._get_playbook_permission(city_id, playbook_name)
        LOGGER.info(
            "[sea][perm] %s → %s (city=%s, user_configured=%s, auto_mode=%s)",
            playbook_name, perm, city_id, user_configured, auto_mode,
        )

        if perm == "blocked":
            return False, f"Playbook '{playbook_name}' is not available (permission: {perm})"

        if user_configured:
            # ユーザー本人が名指しした起動。user_only の禁止対象はペルソナで
            # あってユーザーではない (設定画面の表示も「ユーザー指定時のみ」)。
            return True, None

        if perm == "user_only":
            return False, (
                f"Playbook '{playbook_name}' is user-only and cannot be started "
                "by a persona or autonomous execution."
            )

        if perm != "ask_every_time":
            return True, None  # auto_allow

        if auto_mode:
            return False, (
                f"Playbook '{playbook_name}' requires user permission but running "
                "in auto mode. Skipped."
            )

        if event_callback is None:
            return False, (
                f"Playbook '{playbook_name}' requires explicit user permission but "
                "there is no channel to ask on. Skipped."
            )

        response = self._request_playbook_permission(playbook_name, persona, event_callback)

        if response == "always_allow":
            self._set_playbook_permission(city_id, playbook_name, "auto_allow")
            return True, None

        if response == "allow":
            return True, None

        if response == "never_use":
            self._set_playbook_permission(city_id, playbook_name, "user_only")
            return False, (
                f"User disabled playbook '{playbook_name}'. This playbook will not "
                "be available in future. Please respond without using this tool."
            )

        if response == "timeout":
            return False, (
                f"Permission request for playbook '{playbook_name}' timed out. "
                "Please respond without using this tool."
            )

        return False, (
            f"User denied execution of playbook '{playbook_name}'. "
            "Please respond without using this tool."
        )

    def _request_playbook_permission(
        self,
        playbook_name: str,
        persona: Any,
        event_callback: Optional[Callable[[Dict[str, Any]], None]],
    ) -> str:
        """Send a ``permission_request`` event and block until the user responds.

        Returns one of: ``"allow"``, ``"deny"``, ``"always_allow"``,
        ``"never_use"``, or ``"timeout"``.
        """
        import threading as _threading

        if not event_callback:
            LOGGER.warning("[sea][perm] No event_callback — auto-denying %s", playbook_name)
            return "deny"

        request_id = str(uuid.uuid4())
        event = _threading.Event()
        self.manager._pending_permission_requests[request_id] = event

        # Look up display name / description for the dialog
        display_name = playbook_name
        description = ""
        try:
            db = self.manager.SessionLocal()
            try:
                pb = db.query(PlaybookModel).filter(PlaybookModel.name == playbook_name).first()
                if pb:
                    display_name = pb.display_name or pb.name
                    description = pb.description or ""
            finally:
                db.close()
        except Exception:
            LOGGER.warning("[sea][perm] Failed to look up playbook info for %s", playbook_name, exc_info=True)

        persona_id = getattr(persona, "persona_id", None)
        persona_name = getattr(persona, "persona_name", None)

        event_callback({
            "type": "permission_request",
            "request_id": request_id,
            "playbook_name": playbook_name,
            "playbook_display_name": display_name,
            "playbook_description": description,
            "persona_id": persona_id,
            "persona_name": persona_name,
        })
        LOGGER.info("[sea][perm] Sent permission_request for %s (id=%s)", playbook_name, request_id)

        # Block until the user responds or timeout
        timeout_sec = 60
        responded = event.wait(timeout=timeout_sec)

        # Cleanup
        self.manager._pending_permission_requests.pop(request_id, None)
        response = self.manager._permission_responses.pop(request_id, None)

        if not responded or response is None:
            LOGGER.info("[sea][perm] Permission request %s timed out after %ds", request_id, timeout_sec)
            if event_callback:
                event_callback({
                    "type": "warning",
                    "content": f"Playbook実行の許可リクエストがタイムアウトしました（{display_name}）。スキップします。",
                    "warning_code": "permission_timeout",
                    "display": "toast",
                })
            return "timeout"

        LOGGER.info("[sea][perm] Permission response for %s: %s", playbook_name, response)
        return response

    def _notify_persona_permission_result(
        self,
        state: Dict[str, Any],
        persona: Any,
        playbook_name: str,
        message: str,
        event_callback: Optional[Callable[[Dict[str, Any]], None]],
    ) -> None:
        """Inform the persona about a permission denial/timeout.

        Records the result in state, message history, and SAIMemory so the
        persona can adjust its response accordingly.
        """
        state["last"] = message
        self._append_tool_result_message(state, playbook_name, message)
        if not self._store_memory(
            persona, message,
            role="system",
            tags=["permission", "denied", playbook_name],
            pulse_id=state.get("_pulse_id"),
        ):
            LOGGER.warning("[sea][perm] Failed to store permission result to SAIMemory")

    def _lg_exec_node(
        self,
        node_def: Any,
        playbook: PlaybookSchema,
        persona: Any,
        building_id: str,
        auto_mode: bool,
        outputs: Optional[List[str]] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        return lg_exec_node_impl(self, node_def, playbook, persona, building_id, auto_mode, outputs, event_callback)

    def _lg_memorize_node(self, node_def: Any, persona: Any, playbook: PlaybookSchema, outputs: Optional[List[str]] = None, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None):
        return self._runtime_engine.lg_memorize_node(node_def, persona, playbook, outputs, event_callback)

    def _lg_speak_node(self, state: dict, persona: Any, building_id: str, playbook: PlaybookSchema, outputs: Optional[List[str]] = None, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None):
        return self._runtime_engine.lg_speak_node(state, persona, building_id, playbook, outputs, event_callback)

    def _lg_say_node(self, node_def: Any, persona: Any, building_id: str, playbook: PlaybookSchema, outputs: Optional[List[str]] = None, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None):
        async def node(state: dict):
            # Send status event for node execution
            node_id = getattr(node_def, "id", "say")
            if event_callback:
                event_callback({"type": "status", "content": f"{playbook.name} / {node_id}", "playbook": playbook.name, "node": node_id})
            text = state.get("last") or ""
            reasoning_text = state.pop("_reasoning_text", "")
            reasoning_details_val = state.pop("_reasoning_details", None)
            auto_recall_text = state.pop("_auto_recall_text", None)
            pulse_id = state.get("_pulse_id")
            metadata_key = getattr(node_def, "metadata_key", None)
            base_metadata = state.get(metadata_key) if metadata_key else None

            # Build metadata with usage total from accumulator
            msg_metadata: Dict[str, Any] = {}
            if base_metadata:
                if isinstance(base_metadata, dict):
                    msg_metadata.update(base_metadata)
                else:
                    msg_metadata["metadata"] = base_metadata
            if reasoning_text:
                msg_metadata["reasoning"] = reasoning_text
            if reasoning_details_val is not None:
                msg_metadata["reasoning_details"] = reasoning_details_val
            if auto_recall_text:
                # 記憶アーキv2 §4.5: この Pulse で末尾注入された「ふと浮かんだ記憶」を
                # reasoning と同じ流儀で永続化する。LLM コンテキストには渡さない
                # (state.pop 済みで再利用不可、metadata は llm_clients 側で読まれない)。
                msg_metadata["auto_recall"] = auto_recall_text

            # Include pulse usage accumulator total for UI display
            accumulator = state.get("_pulse_usage_accumulator")
            if accumulator and accumulator.get("call_count", 0) > 0:
                msg_metadata["llm_usage_total"] = dict(accumulator)

            # Include activity trace for UI display
            activity_trace = state.get("_activity_trace")
            if activity_trace:
                msg_metadata["activity_trace"] = list(activity_trace)

            eff_bid = self._effective_building_id(persona, building_id)
            building_msg = self._emit_say(persona, eff_bid, text, pulse_id=pulse_id, metadata=msg_metadata if msg_metadata else None)
            if outputs is not None:
                outputs.append(text)
            if event_callback:
                say_event: Dict[str, Any] = {"type": "say", "content": text, "persona_id": getattr(persona, "persona_id", None), "metadata": msg_metadata if msg_metadata else None}
                if pulse_id:
                    say_event["pulse_id"] = pulse_id
                if building_msg and building_msg.get("message_id"):
                    say_event["message_id"] = str(building_msg["message_id"])
                if reasoning_text:
                    say_event["reasoning"] = reasoning_text
                if activity_trace:
                    say_event["activity_trace"] = list(activity_trace)
                event_callback(say_event)

            # Debug: log speak_content at end of say node
            speak_content = state.get("speak_content", "")
            LOGGER.info("[DEBUG] say node end: state['speak_content'] = '%s'", speak_content)

            # Append to PulseContext (say is not important — Building history only)
            _pulse_ctx = state.get("_pulse_context")
            if _pulse_ctx:
                from sea.pulse_context import PulseLogEntry
                _pulse_ctx.append(PulseLogEntry(
                    role="assistant", content=text,
                    node_id=node_id, playbook_name=playbook.name,
                    important=False))

            return state
        return node

    def _lg_think_node(self, state: dict, persona: Any, playbook: PlaybookSchema, outputs: Optional[List[str]] = None, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None):
        # Send status event for node execution
        if event_callback:
            event_callback({"type": "status", "content": f"{playbook.name} / think", "playbook": playbook.name, "node": "think"})
        text = state.get("last") or ""
        pulse_id = state.get("_pulse_id") or str(uuid.uuid4())
        self._emit_think(persona, pulse_id, text)
        if outputs is not None:
            outputs.append(text)
        if event_callback:
            event_callback({"type": "think", "content": text, "persona_id": getattr(persona, "persona_id", None)})

        # Append to PulseContext
        _pulse_ctx = state.get("_pulse_context")
        if _pulse_ctx:
            from sea.pulse_context import PulseLogEntry
            _pulse_ctx.append(PulseLogEntry(
                role="system", content=text,
                node_id="think", playbook_name=playbook.name,
                important=False))

        return state

    def _lg_subplay_node(self, node_def: Any, persona: Any, building_id: str, playbook: PlaybookSchema, auto_mode: bool, outputs: Optional[List[str]] = None, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None):
        return lg_subplay_node_impl(self, node_def, persona, building_id, playbook, auto_mode, outputs, event_callback)

    def _lg_set_node(self, node_def: Any, playbook: PlaybookSchema, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None):
        """Create a node that sets/modifies state variables."""
        assignments = getattr(node_def, "assignments", {}) or {}

        async def node(state: dict):
            # Send status event for node execution
            node_id = getattr(node_def, "id", "set")
            if event_callback:
                event_callback({"type": "status", "content": f"{playbook.name} / {node_id}", "playbook": playbook.name, "node": node_id})
            trace_parts = []
            for key, value_template in assignments.items():
                resolved_value = self._resolve_set_value(value_template, state)
                # system namespace (_messages / _pulse_context 等) への代入は
                # ロード時 validator が弾くが、値が効くのはここなので二重に防ぐ。
                if not set_playbook_var(
                    state, key, resolved_value,
                    where=f"playbook '{playbook.name}' node '{node_id}' assignment",
                ):
                    continue
                LOGGER.debug("[sea][set] %s = %s", key, resolved_value)
                trace_parts.append(f"{key}={str(resolved_value)[:80]}")
            log_sea_trace(playbook.name, node_id, "SET", ", ".join(trace_parts))

            # Special handling: if executed_playbooks_init is set, initialize executed_playbooks as empty list
            if state.get("executed_playbooks_init") and "executed_playbooks" not in state:
                state["executed_playbooks"] = []
                LOGGER.debug("[sea][set] Initialized executed_playbooks = []")

            return state
        return node

    def _resolve_set_value(self, value_template: Any, state: Dict[str, Any]) -> Any:
        return resolve_set_value(value_template, state)

    def _eval_arithmetic_expression(self, expr: str, state: Dict[str, Any]) -> Any:
        return eval_arithmetic_expression(expr, state)

    def _start_subagent_thread(self, persona, label: Optional[str] = None, pulse_context: Optional[Any] = None):
        """Create a temporary Stelis thread and switch the active thread to it.

        Used by subplay/exec nodes with execution='subagent' to isolate
        sub-playbook execution in a temporary thread.

        ``pulse_context``: 呼び出しノードの PulseContext (state["_pulse_context"])。
        渡されると thread 切替は push_thread 経由になり、end 不達 (例外/cancel)
        でも graph 実行の finally が親 thread を復元する (S4)。

        Returns:
            (thread_id, parent_thread_id) on success, (None, None) on failure.
        """
        memory_adapter = getattr(persona, "sai_memory", None)
        if not memory_adapter:
            LOGGER.warning("[subagent] No memory adapter found for persona %s", persona.persona_id)
            return None, None

        # Check depth limit (subagent uses max_depth=2 to prevent deep nesting)
        if not memory_adapter.can_start_stelis(max_depth=2):
            LOGGER.warning("[subagent] Stelis max depth exceeded for persona %s", persona.persona_id)
            return None, None

        # Get current thread as parent
        parent_thread_id = memory_adapter.get_current_thread()
        if parent_thread_id is None:
            parent_thread_id = memory_adapter._thread_id(None)

        # Create a new Stelis thread (no anchor message — subagent is transparent)
        stelis = memory_adapter.start_stelis_thread(
            parent_thread_id=parent_thread_id,
            window_ratio=0.8,
            max_depth=2,
            label=label or "Subagent",
        )

        if not stelis:
            LOGGER.error("[subagent] Failed to create subagent thread for persona %s", persona.persona_id)
            return None, None

        # Switch to the new thread (S4: pulse_context があれば push_thread 経由で
        # 親を記録し、復元を graph finally に保証させる)
        if pulse_context is not None and callable(getattr(pulse_context, "push_thread", None)):
            pulse_context.push_thread(memory_adapter, stelis.thread_id, parent_thread_id=parent_thread_id)
        else:
            memory_adapter.set_active_thread(stelis.thread_id)
        LOGGER.info(
            "[subagent] Started subagent thread %s (parent=%s, label=%s)",
            stelis.thread_id, parent_thread_id, label,
        )
        return stelis.thread_id, parent_thread_id

    def _end_subagent_thread(
        self,
        persona,
        thread_id: str,
        parent_thread_id: str,
        generate_chronicle: bool = True,
        pulse_context: Optional[Any] = None,
    ) -> Optional[str]:
        """End a subagent thread and switch back to the parent thread.

        Args:
            generate_chronicle: If True, generate a Chronicle summary before ending.
            pulse_context: start 側で push_thread した PulseContext。渡されると
                復元は pop_thread 経由になる (S4)。

        Returns:
            Chronicle summary string if generated, else None.
        """
        memory_adapter = getattr(persona, "sai_memory", None)
        if not memory_adapter:
            return None

        chronicle_summary = None
        if generate_chronicle:
            stelis_info = memory_adapter.get_stelis_info(thread_id)
            chronicle_prompt = stelis_info.chronicle_prompt if stelis_info else None
            chronicle_summary = self._generate_stelis_chronicle(
                persona, thread_id, chronicle_prompt
            )
            if chronicle_summary:
                LOGGER.info(
                    "[subagent] Generated Chronicle for thread %s: %s...",
                    thread_id, chronicle_summary[:100],
                )

        # End the Stelis thread
        success = memory_adapter.end_stelis_thread(
            thread_id=thread_id,
            status="completed",
            chronicle_summary=chronicle_summary,
        )
        if not success:
            LOGGER.error("[subagent] Failed to end subagent thread %s", thread_id)

        # Switch back to parent thread (S4: push した親を pop で復元)
        if (
            pulse_context is not None
            and callable(getattr(pulse_context, "pop_thread", None))
            and pulse_context.thread_stack_depth() > 0
        ):
            restored = pulse_context.pop_thread(memory_adapter)
            if restored and restored != parent_thread_id:
                LOGGER.warning(
                    "[subagent] Restored thread %s differs from recorded parent %s",
                    restored, parent_thread_id,
                )
        else:
            memory_adapter.set_active_thread(parent_thread_id)
        LOGGER.info(
            "[subagent] Ended subagent thread %s, returned to parent %s",
            thread_id, parent_thread_id,
        )
        return chronicle_summary

    # ---------------- Stelis Thread Nodes -----------------

    def _lg_stelis_start_node(
        self,
        node_def: Any,
        persona: Any,
        playbook: PlaybookSchema,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None
    ):
        return lg_stelis_start_node_impl(self, node_def, persona, playbook, event_callback)

    def _lg_stelis_end_node(
        self,
        node_def: Any,
        persona: Any,
        playbook: PlaybookSchema,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None
    ):
        return lg_stelis_end_node_impl(self, node_def, persona, playbook, event_callback)

    def _generate_stelis_chronicle(
        self,
        persona: Any,
        thread_id: str,
        chronicle_prompt: Optional[str] = None,
    ) -> Optional[str]:
        """Generate a Chronicle summary for a Stelis thread.

        This creates a concise summary of the conversation/work done in the
        Stelis thread, which will be stored and can be referenced later.
        """
        memory_adapter = getattr(persona, "sai_memory", None)
        if not memory_adapter:
            return None

        # Get messages from the Stelis thread
        try:
            messages = memory_adapter.get_thread_messages(thread_id, page=0, page_size=1000)
        except Exception as exc:
            LOGGER.warning("[stelis] Failed to get messages for Chronicle: %s", exc)
            return None

        if not messages:
            return None

        # Build full conversation content for summarization (no per-message truncation)
        content_parts = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if content:
                content_parts.append(f"[{role}]: {content}")

        if not content_parts:
            return None

        conversation_text = "\n".join(content_parts)

        # Use default prompt if not specified
        if not chronicle_prompt:
            chronicle_prompt = (
                "Please summarize the following conversation/work session concisely. "
                "Focus on: what was done, key decisions made, and any important outcomes."
            )

        # Get LLM client for summarization
        try:
            # Prefer persona's existing lightweight client (already configured)
            client = getattr(persona, "lightweight_llm_client", None)
            if client is None:
                # Fallback: create a temporary client
                from llm_clients import get_llm_client
                from saiverse.model_configs import get_context_length, get_model_provider

                lightweight_model = getattr(persona, "lightweight_model", None) or _get_default_lightweight_model()
                lw_context = get_context_length(lightweight_model)
                provider = get_model_provider(lightweight_model)
                client = get_llm_client(lightweight_model, provider, lw_context)

            summary_messages = [
                {"role": "system", "content": chronicle_prompt},
                {"role": "user", "content": f"Session content:\n\n{conversation_text}"}
            ]

            response = client.generate(summary_messages, temperature=0.3)
            if response and isinstance(response, str):
                return response.strip()

        except Exception as exc:
            LOGGER.warning("[stelis] Chronicle generation failed: %s", exc)

        return None

    # ---------------- context helpers -----------------
    def _append_router_function_call(
        self,
        state: Dict[str, Any],
        selection: Optional[Dict[str, Any]],
        raw_text: str,
    ) -> None:
        payload = selection if isinstance(selection, dict) else None
        if payload is None:
            payload = {"raw": raw_text}
        try:
            args_text = json.dumps(payload, ensure_ascii=False)
        except Exception:
            args_text = json.dumps({"raw": str(raw_text)}, ensure_ascii=False)
        conv = state.get("_messages")
        if not isinstance(conv, list):
            conv = []
        call_id = f"router_call_{uuid.uuid4().hex}"
        call_msg = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "route_playbook",
                        "arguments": args_text,
                    },
                }
            ],
        }
        if conv and isinstance(conv[-1], dict) and conv[-1].get("role") == "assistant":
            conv[-1] = call_msg
        else:
            conv.append(call_msg)
        state["_messages"] = conv
        state["_last_tool_call_id"] = call_id
        state["_last_tool_name"] = payload.get("playbook") or "sub_playbook"

    # ------------------------------------------------------------------
    # PulseContext management
    # ------------------------------------------------------------------

    def _get_or_create_pulse_context(self, pulse_id: str) -> Any:
        """Get an existing PulseContext or create a new one for this pulse_id."""
        from sea.pulse_context import PulseContext
        if pulse_id not in self._pulse_contexts:
            ctx = PulseContext(pulse_id=pulse_id)
            self._pulse_contexts[pulse_id] = ctx
            LOGGER.debug("[sea] Created PulseContext for pulse_id=%s", pulse_id)
        return self._pulse_contexts[pulse_id]

    def _cleanup_pulse_context(self, pulse_id: str) -> None:
        """Remove a PulseContext from the in-memory dict to free memory."""
        removed = self._pulse_contexts.pop(pulse_id, None)
        if removed:
            LOGGER.debug("[sea] Cleaned up PulseContext for pulse_id=%s (%d entries)", pulse_id, len(removed.logs))

    def _flush_pulse_logs(self, persona: Any, pulse_context: Any) -> None:
        """Write all PulseLogEntry items from a PulseContext to the pulse_logs DB table."""
        import json as _json
        adapter = getattr(persona, "sai_memory", None)
        if not adapter or not adapter.is_ready():
            LOGGER.warning("[sea] Cannot flush pulse_logs: SAIMemory adapter unavailable for persona=%s",
                           getattr(persona, "persona_id", None))
            return
        # thread の記帳は flush 時点の adapter 現在値。旧 PulseContext.thread_id は
        # 生成時に一度だけ解決され Stelis 切替を反映しない死に値だったため廃止
        # (beat_execution_context.md §3.4)。
        try:
            flush_thread_id = adapter.get_current_thread()
        except Exception:
            LOGGER.debug("[sea] get_current_thread failed at pulse_log flush", exc_info=True)
            flush_thread_id = None
        count = 0
        for entry in pulse_context.logs:
            tool_calls_json = _json.dumps(entry.tool_calls, ensure_ascii=False) if entry.tool_calls else None
            adapter.append_pulse_log(
                pulse_id=pulse_context.pulse_id,
                thread_id=flush_thread_id,
                role=entry.role,
                content=entry.content,
                node_id=entry.node_id,
                playbook_name=entry.playbook_name,
                important=entry.important,
                tool_calls=tool_calls_json,
                tool_call_id=entry.tool_call_id,
                tool_name=entry.tool_name,
                created_at=entry.created_at,
            )
            count += 1
        LOGGER.info("[sea] Flushed %d pulse_log entries for pulse_id=%s", count, pulse_context.pulse_id)
        self._cleanup_pulse_context(pulse_context.pulse_id)

    def _store_memory(
        self,
        persona: Any,
        text: str,
        *,
        role: str = "assistant",
        tags: Optional[List[str]] = None,
        pulse_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        playbook_name: Optional[str] = None,
        pulse_context: Optional[Any] = None,
        line_role: Optional[str] = None,
        line_id: Optional[str] = None,
        origin_track_id: Optional[str] = None,
        scope: Optional[str] = None,
        paired_action_text: Optional[str] = None,
        thought_signature: Optional[bytes] = None,
        spell_origin_id: Optional[str] = None,
        spell_seq: Optional[int] = None,
        return_message_id: bool = False,
    ) -> Any:
        """Store a message to SAIMemory. Returns True on success, False on failure.

        7-layer storage metadata (Intent A v0.14, Intent B v0.11):

        - ``pulse_context``: When supplied (and the explicit ``line_role`` /
          ``line_id`` / ``origin_track_id`` are not), the active line frame's
          metadata is read via ``current_line_metadata()`` so callers in the
          ``sea/runtime_llm.py`` Spell loop and LLM-node memorize paths can
          omit boilerplate.
        - Explicit overrides take precedence over the auto-resolved values
          (used e.g. when a meta-judgment branch turn must be tagged
          ``scope='discardable'`` regardless of the surrounding line).
        - ``scope`` defaults to the SQL-level ``'committed'`` when ``None``.
        - ``return_message_id``: When True, returns the inserted message id
          (str) on success, or empty string on no-op / failure. Used by Phase
          1.3 meta-judgment so the dispatch step can later promote the row
          from ``scope='discardable'`` to ``'committed'`` when action='switch'.
          Default False keeps the bool return so existing callers are
          unchanged.
        """
        if not text:
            return "" if return_message_id else True
        adapter = getattr(persona, "sai_memory", None)
        if not adapter or not adapter.is_ready():
            LOGGER.warning(
                "[_store_memory] SAIMemory adapter unavailable for persona=%s — message will NOT be stored. "
                "Check embedding model setup.",
                getattr(persona, "persona_id", None),
            )
            return "" if return_message_id else False
        # -- 層1マーカー (==語句==) の抽出・剥離 (life_concept_map.md §9.1 / P3) --
        # ペルソナ自身の生成テキスト (assistant) のみ対象。ここが SEA 経路の
        # 「本文最終確定点」: SAIMemory に永続化される content からマーカーを
        # 剥がし、抽出した観測点は insert 後に message_id 付きで marks へ保存
        # する。マーカーが無ければ extract_marks は文字列走査 1 回の no-op。
        mark_spans: List[Any] = []
        if (role or "assistant") == "assistant":
            from saiverse.marker_parser import extract_marks
            text, mark_spans = extract_marks(text)

        # -- message dict 構築 (try の外: ここでのバグは即座に上位に伝播させる) --
        current_thread = adapter.get_current_thread()
        LOGGER.debug("[_store_memory] Active thread: %s (persona_id=%s)", current_thread, getattr(persona, "persona_id", None))
        if current_thread is None:
            pid = getattr(persona, "persona_id", None) or "unknown"
            default_thread = f"{pid}:{adapter._PERSONA_THREAD_SUFFIX}"
            adapter.set_active_thread(default_thread)
            current_thread = default_thread
            LOGGER.info("[_store_memory] No active thread for %s — initialized default: %s", pid, default_thread)

        # Resolve 7-layer metadata: explicit args win, otherwise read from
        # the active LineFrame on the supplied PulseContext.
        resolved_line_role = line_role
        resolved_line_id = line_id
        resolved_track_id = origin_track_id
        resolved_scope = scope
        resolved_aspect: Optional[str] = None
        if pulse_context is not None and (
            resolved_line_role is None
            or resolved_line_id is None
            or resolved_track_id is None
            or resolved_scope is None
        ):
            try:
                meta = pulse_context.current_line_metadata()
            except AttributeError:
                meta = {}
            if resolved_line_role is None:
                resolved_line_role = meta.get("line_role")
            if resolved_line_id is None:
                resolved_line_id = meta.get("line_id")
            if resolved_track_id is None:
                resolved_track_id = meta.get("origin_track_id")
            if resolved_scope is None:
                resolved_scope = meta.get("scope")
            resolved_aspect = meta.get("aspect")

        message: Dict[str, Any] = {"role": role or "assistant", "content": text}
        if resolved_line_role is not None:
            message["line_role"] = resolved_line_role
        if resolved_line_id is not None:
            message["line_id"] = resolved_line_id
        if resolved_track_id is not None:
            message["origin_track_id"] = resolved_track_id
        if resolved_scope is not None:
            message["scope"] = resolved_scope
        if paired_action_text is not None:
            message["paired_action_text"] = paired_action_text
        if pulse_id:
            message["pulse_id"] = pulse_id
        if thought_signature:
            message["thought_signature"] = thought_signature
            LOGGER.debug(
                "[_store_memory] persona=%s role=%s thought_signature attached (%d bytes)",
                getattr(persona, "persona_id", None), role, len(thought_signature),
            )
        if spell_origin_id is not None:
            message["spell_origin_id"] = spell_origin_id
        if spell_seq is not None:
            message["spell_seq"] = spell_seq

        clean_tags = [str(tag) for tag in (tags or []) if tag]
        if playbook_name:
            clean_tags.append(f"playbook:{playbook_name}")
        msg_metadata: Dict[str, Any] = {}
        if clean_tags:
            msg_metadata["tags"] = clean_tags
        if isinstance(metadata, dict):
            for key, value in metadata.items():
                if key == "tags":
                    extra_tags = [str(t) for t in value if t] if isinstance(value, list) else []
                    msg_metadata.setdefault("tags", []).extend(extra_tags)
                else:
                    msg_metadata[key] = value

        if resolved_aspect:
            msg_metadata["aspect"] = resolved_aspect

        # audience: 同じ Building にいる他ペルソナ・ユーザーを記録
        if role == "assistant" and "audience" not in msg_metadata:
            persona_id = getattr(persona, "persona_id", None)
            building_id = getattr(persona, "current_building_id", None)
            if persona_id and building_id and self.manager:
                occupants = getattr(self.manager, "occupants", {}).get(building_id, [])
                audience_personas = [
                    str(oid) for oid in occupants
                    if str(oid) != persona_id and not str(oid).startswith("user_")
                ]
                if audience_personas:
                    audience_users = [
                        str(oid) for oid in occupants
                        if str(oid).startswith("user_")
                    ]
                    msg_metadata["audience"] = {
                        "personas": audience_personas,
                        "users": audience_users,
                    }

        # -- 層0タグ: 開いている出来事の参照を自動継承 (life_concept_map.md §9.1) --
        # ペルソナに open な出来事があれば origin_episode に episode_ref を刻む
        # (origin_track_id の一般化)。開いている出来事が無ければ何も付けない。
        # 高頻度経路のため get_open_episode は per-persona キャッシュ
        # (saiverse/episodes.py) で DB を引かずに済む。記録専用 — 失敗しても
        # メッセージ保存を止めない。
        l0_persona_id = getattr(persona, "persona_id", None)
        if (
            l0_persona_id
            and "origin_episode" not in msg_metadata
            and self.manager is not None
            and getattr(self.manager, "SessionLocal", None) is not None
        ):
            try:
                from saiverse.episodes import get_open_episode
                open_ep = get_open_episode(self.manager, l0_persona_id)
                if open_ep and open_ep.get("episode_ref"):
                    msg_metadata["origin_episode"] = open_ep["episode_ref"]
            except Exception:
                LOGGER.debug(
                    "[_store_memory] open-episode lookup failed (persona=%s); "
                    "message stored without origin_episode",
                    l0_persona_id, exc_info=True,
                )

        if msg_metadata:
            message["metadata"] = msg_metadata

        # -- DB 書き込み (ここだけ try で囲む: DB 障害は pulse を止めず WARNING) --
        try:
            thread_suffix = current_thread.split(":", 1)[1] if ":" in current_thread else current_thread
            inserted_id = adapter.append_persona_message(message, thread_suffix=thread_suffix)
            # 層1マーカー由来の観測点を点クリップとして保存 (アンカー = 挿入されたメッセージ)。
            # hasattr ガードはテストのスタブ adapter (add_clips 未実装) 対策。
            # 注意: このガードは綴りが違えば黙って no-op になる — メソッド名を変える
            # ときは必ず両方を揃えること (2026-07-15 の写真→クリップ改名で、ここが
            # add_photos のまま残りクリップが 1 枚も保存されなくなった)。
            if mark_spans and inserted_id and hasattr(adapter, "add_clips"):
                adapter.add_clips(inserted_id, mark_spans)
            if return_message_id:
                return inserted_id or ""
            return True
        except Exception:
            LOGGER.warning(
                "[_store_memory] DB write failed for persona=%s role=%s — message lost",
                getattr(persona, "persona_id", None), role, exc_info=True,
            )
            return "" if return_message_id else False

    def _append_tool_result_message(
        self,
        state: Dict[str, Any],
        source: str,
        payload: str,
    ) -> None:
        call_id = state.get("_last_tool_call_id")
        if not call_id:
            return
        conv = state.get("_messages")
        if not isinstance(conv, list):
            conv = []
        message = {
            "role": "tool",
            "tool_call_id": call_id,
            "name": source or state.get("_last_tool_name") or "sub_playbook",
            "content": payload,
        }
        conv.append(message)
        state["_messages"] = conv
        state["_last_tool_call_id"] = None

    # ---------------- helpers -----------------
    def _effective_building_id(self, persona: Any, fallback: str) -> str:
        """Return persona's actual building from occupancy map.

        After a move_persona tool changes the occupants dict, this returns
        the new building so that post-move utterances land in the correct
        building history.  For normal (non-move) conversations it returns
        the same value as *fallback*.
        """
        pid = getattr(persona, "persona_id", None)
        if pid:
            for bid, occ_list in self.manager.occupants.items():
                if pid in occ_list:
                    return bid
        return fallback

    def _emit_speak(self, persona: Any, building_id: str, text: str, pulse_id: Optional[str] = None, record_history: bool = True, extra_metadata: Optional[Dict[str, Any]] = None) -> None:
        self._emitters.emit_speak(persona, building_id, text, pulse_id=pulse_id, record_history=record_history, extra_metadata=extra_metadata)

    def _emit_say(self, persona: Any, building_id: str, text: str, pulse_id: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        return self._emitters.emit_say(persona, building_id, text, pulse_id=pulse_id, metadata=metadata)

    # Pipeline Streaming (Phase 2-β): emit_speak の 2 段階 API + sub-speak 発火。
    # 詳細: docs/intent/voice_tts_pipeline_streaming.md
    def _emit_speak_start(self, persona: Any, building_id: str, pulse_id: Optional[str] = None) -> Optional[str]:
        return self._emitters.emit_speak_start(persona, building_id, pulse_id=pulse_id)

    def _emit_sub_speak(self, persona: Any, building_id: str, message_id: str, sub_text: str, sub_seq: int, pulse_id: Optional[str] = None) -> None:
        self._emitters.emit_sub_speak(persona, building_id, message_id, sub_text, sub_seq, pulse_id=pulse_id)

    def _emit_speak_finalize(
        self,
        persona: Any,
        building_id: str,
        message_id: str,
        text: str,
        pulse_id: Optional[str] = None,
        extra_metadata: Optional[Dict[str, Any]] = None,
        final_sub_seq: Optional[int] = None,
        final_voice_text: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        return self._emitters.emit_speak_finalize(
            persona, building_id, message_id, text,
            pulse_id=pulse_id, extra_metadata=extra_metadata,
            final_sub_seq=final_sub_seq, final_voice_text=final_voice_text,
        )

    def _emit_think(self, persona: Any, pulse_id: str, text: str, record_history: bool = True) -> None:
        self._emitters.emit_think(persona, pulse_id, text, record_history=record_history)

    def _notify_unity_speak(self, persona: Any, text: str) -> None:
        self._emitters.notify_unity_speak(persona, text)

    # ---------------- history metabolism -----------------

    #: keep-alive の末尾メッセージ。意味的に不活性 (何のイベントでもない) で、
    #: SAIMemory には保存されない — 次の本物の呼び出しのリクエストには現れず、
    #: 共有 prefix (head + 履歴) だけがキャッシュ上で温め直される。
    _KEEPALIVE_TAIL = (
        "<system>（キャッシュ維持のための自動処理です。世界では何も起きていません。"
        "「.」とだけ返答してください）</system>"
    )

    def _spawn_session_close(self, persona_id: str) -> None:
        """セッションクローズ (砂金採り + Chronicle 前倒し) を daemon スレッドで実行する。

        呼び出し元 (:meth:`run_cache_keepalive` の not-Active 分岐) は EventScheduler の
        dispatch スレッド上で走るため、そこで重い LLM 処理を同期実行すると後続の予約が
        滞る (saiverse/event_scheduler.py の docstring: 重い処理は別 thread に投げる)。
        別スレッドに逃がして即 return する。
        """
        def _target() -> None:
            try:
                self.session_lifecycle.run_session_close_for(persona_id)
            except Exception:
                LOGGER.exception(
                    "[gold_panning] session close thread crashed (persona=%s)", persona_id,
                )

        threading.Thread(
            target=_target,
            daemon=True,
            name=f"gold-panning-close-{persona_id}",
        ).start()

    def run_cache_keepalive(self, persona_id: str, model_key: Optional[str] = None) -> bool:
        """メインキャッシュの keep-alive: 意味的に不活性な極小 LLM コール 1 回。

        TTL 接近時 (:meth:`SessionLifecycle.schedule_cache_ttl_pulse` の予約) に呼ばれる。
        予約は (persona, model) ごとに独立で (beat_execution_context.md §3.1)、
        ``model_key`` は「どの model の Session を守るか」。None (レガシー呼び出し)
        なら ``persona.model`` にフォールバックする。
        メインラインと同じ context (head + 履歴) を組み、末尾に不活性な 1 文を
        足して当該 model を 1 回だけ呼ぶ:

        - **判断はしない**: playbook もスペルも走らず、応答は破棄される
        - **記憶に残らない**: SAIMemory へは一切書かない (discardable ですらない)
        - **Beat ロック対象外**: 生成はするが記憶に書かない軽量 Beat のため、
          beat_gate.hold は取らない (直列化の目的 = 記憶の一直線性に関与しない。
          beat_execution_context.md §2.2 の工事で意図的に対象外とした)
        - **キャッシュ経済**: 共有 prefix が cache read でヒットし、プロバイダ側の
          TTL ウィンドウが更新される。成功時は ``SessionLifecycle.touch_anchor_after_llm_call``
          が anchor を touch → 次回 keep-alive が再予約される (従来と同じ連鎖)
        - **自然停止**: 失効済み (温め直しても意味がない) / 自律 OFF /
          呼び出し失敗のときは touch しない → 連鎖は止まり、次の本物の呼び出し
          まで keep-alive は走らない

        Returns:
            LLM コールまで到達し成功したら True (テスト・観察用)。
        """
        manager = self.manager
        persona = (getattr(manager, "personas", None) or {}).get(persona_id)
        if persona is None:
            LOGGER.debug("[keepalive] persona not found: %s", persona_id)
            return False
        if not bool(getattr(persona, "autonomy_enabled", False)):
            LOGGER.debug(
                "[keepalive] skipped (persona=%s autonomy disabled)", persona_id,
            )
            # ペルソナの自律行動が OFF = セッションが閉じた瞬間で、anchor がまだ
            # 温かい可能性が高い唯一の停止分岐 (docs/intent/gold_panning.md §3.6)。
            # ここで砂金採り (セッションクローズ) を別スレッドに委譲する。
            self._spawn_session_close(persona_id)
            return False

        # life.md §5.2: keep-alive 連鎖はライフに従属する。その日 lives が宣言
        # されている場合、現在時刻がいずれかのライフ区間内でなければ (= 谷)
        # touch せず連鎖を自然停止する — ここで return するため、この後の
        # schedule_cache_ttl_pulse への再予約にも到達しない (二重管理を避ける
        # 単一の集約点)。lives 未宣言の日 / ペルソナは常に許可 (完全後方互換)。
        # 判定失敗時は許可側にフォールバックする (安全側は「温め続ける」— 谷
        # 判定の誤りで温もりを止めてしまう方が実害が大きい)。
        try:
            from saiverse.day_plan import is_keepalive_allowed
            if not is_keepalive_allowed(manager, persona_id):
                LOGGER.debug(
                    "[keepalive] skipped (persona=%s outside declared life; valley)",
                    persona_id,
                )
                return False
        except Exception:
            LOGGER.warning(
                "[keepalive] life gate check failed (persona=%s); defaulting to allow",
                persona_id, exc_info=True,
            )

        if not model_key:
            model_key = getattr(persona, "model", None)
        if not model_key:
            return False
        model_key = str(model_key)

        # 非 explicit キャッシュ (gemini_explicit / implicit 等): keep-alive LLM は
        # 呼ばない。見張りとしてタイマーだけ再予約し、次の TTL 接近まで待つ。
        # クローズ採取は上の not-Active 分岐が担うため、Active のここでは温めない。
        # (docs/intent/gold_panning.md §3.6)
        try:
            from saiverse.model_configs import get_cache_config
            cache_type = (get_cache_config(model_key) or {}).get("type", "implicit")
        except Exception:
            cache_type = "implicit"
        if cache_type != "explicit":
            LOGGER.debug(
                "[keepalive] non-explicit cache; re-scheduling watchdog only "
                "(persona=%s model=%s type=%s)",
                persona_id, model_key, cache_type,
            )
            try:
                self.session_lifecycle.schedule_cache_ttl_pulse(persona, model_key, cache_type)
            except Exception:
                LOGGER.exception(
                    "[keepalive] failed to re-schedule session watchdog (persona=%s)",
                    persona_id,
                )
            return False

        # anchor の生存確認: 既に失効しているキャッシュは温め直さない
        # (全額書き直しになるだけ。次の本物の呼び出しが自然に張り直す)。
        try:
            entry = self.session_lifecycle.load_anchor_entry(persona_id, model_key)
            if not entry or not entry.get("updated_at"):
                LOGGER.debug(
                    "[keepalive] no anchor entry; skipping (persona=%s model=%s)",
                    persona_id, model_key,
                )
                return False
            updated_at = datetime.fromisoformat(entry["updated_at"])
            ttl_seconds = self.session_lifecycle.anchor_entry_ttl_seconds(
                entry, model_key, persona_id,
            )
            if datetime.now() >= updated_at + timedelta(seconds=ttl_seconds):
                LOGGER.info(
                    "[keepalive] cache already expired; not re-warming "
                    "(persona=%s model=%s)", persona_id, model_key,
                )
                return False
        except Exception:
            LOGGER.warning(
                "[keepalive] failed to read anchor state (persona=%s)",
                persona_id, exc_info=True,
            )
            return False

        building_id = getattr(persona, "current_building_id", None)
        if not building_id:
            LOGGER.debug(
                "[keepalive] persona %s has no current_building_id; skipping",
                persona_id,
            )
            return False

        from types import SimpleNamespace

        try:
            # メインラインと同じ既定 requirements で context を組む — 共有 prefix
            # (head + main_line 履歴) が前回の本物の呼び出しと一致することが
            # キャッシュヒットの条件。head は (persona, model) の Session ごとに
            # 一つ (beat_execution_context.md §3.1) なので、見張り対象 model の
            # head を明示で指定する (lightweight Session を default の head で
            # 温めると別 prefix になりキャッシュを壊す)。
            # persist_anchor_advance=False: keepalive は Beat ロックの外を走る
            # ため、組成中に §14-2 の anchor 前進 (行書き込み) を発火させない —
            # ロック外の書き込みは並走 Beat の前進・fold 更新と競合する
            # (Codex 6巡目 2026-07-30)。keepalive は touch の CAS まで一貫して
            # 読みだけで進む。
            _ka_meta: Dict[str, Any] = {}
            messages = list(
                self._prepare_context(
                    persona, building_id, None, model_key=model_key,
                    context_meta=_ka_meta, persist_anchor_advance=False,
                ) or []
            )
            composed_anchor = _ka_meta.get("prefix_anchor_id")
            if composed_anchor != entry.get("anchor_id"):
                # 生存確認から組成までの間に anchor が動いた (TTL 境界を跨いだ /
                # 並走 Beat が前進した)。この prefix はもうキャッシュと一致しない
                # ので温めても無駄 — LLM を呼ばず連鎖を自然停止する。
                LOGGER.info(
                    "[keepalive] prefix anchor diverged during composition "
                    "(persona=%s model=%s row=%s composed=%s); skipping warm",
                    persona_id, model_key, entry.get("anchor_id"), composed_anchor,
                )
                return False
            messages.append({"role": "user", "content": self._KEEPALIVE_TAIL})
            node_def = SimpleNamespace(id="cache_keepalive", memorize=None, speak=False)
            # Beat 相当の開始点 — Pulse 外なので pulse_context=None (standard tier)。
            execution_context = resolve_execution_context(persona, None)
            if model_key == execution_context.model_key:
                # 従来経路: persona.model の Session を守る keep-alive。
                llm_client, _ka_model = self.select_llm_client(
                    node_def, persona, execution_context=execution_context,
                )
                if _ka_model != execution_context.model_key:
                    execution_context = execution_context.with_model(_ka_model)
            else:
                # (persona, model) 独立監視 (beat_execution_context.md §3.1):
                # 見張り対象の Session が persona.model 以外 (自律 Pulse 等の
                # lightweight main-line Session) の場合は、その model の client
                # で温める。呼んでいない model の cache を触らない (不変条件 §4-2)。
                execution_context = execution_context.with_model(model_key)
                lightweight_model = (
                    getattr(persona, "lightweight_model", None)
                    or _get_default_lightweight_model()
                )
                if model_key == lightweight_model and getattr(persona, "lightweight_llm_client", None):
                    llm_client = persona.lightweight_llm_client
                else:
                    from llm_clients import get_llm_client
                    from saiverse.model_configs import get_context_length, get_model_provider
                    llm_client = get_llm_client(
                        model_key, get_model_provider(model_key), get_context_length(model_key),
                    )
                LOGGER.debug(
                    "[keepalive] warming non-default model session (persona=%s model=%s)",
                    persona_id, model_key,
                )
            llm_client.generate(
                messages,
                tools=[],
                temperature=self._default_temperature(persona),
                **self._get_cache_kwargs(persona_id),
            )
        except Exception:
            # 失敗時は touch しない → 予約も更新されず連鎖は自然停止する。
            LOGGER.warning(
                "[keepalive] keep-alive LLM call failed (persona=%s model=%s)",
                persona_id, model_key, exc_info=True,
            )
            return False

        usage = (
            llm_client.consume_usage()
            if hasattr(llm_client, "consume_usage") else None
        )
        if usage is not None:
            try:
                get_usage_tracker().record_usage(
                    model_id=usage.model,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cached_tokens=usage.cached_tokens,
                    cache_write_tokens=usage.cache_write_tokens,
                    cache_ttl=usage.cache_ttl,
                    persona_id=persona_id,
                    building_id=building_id,
                    node_type="cache_keepalive",
                    playbook_name="cache_keepalive",
                    category="cache_keepalive",
                )
            except Exception:
                LOGGER.warning(
                    "[keepalive] usage tracking failed (persona=%s)",
                    persona_id, exc_info=True,
                )
            # 成功 = anchor touch → 次の keep-alive が再予約される。
            # anchor_id は生存確認で読んだ「見張り対象 model の行」の値 (call-local)。
            # keepalive は Beat ロックの外を走る唯一の touch なので CAS で書く —
            # LLM 呼び出し中に anchor 前進 (§14-2 / 退場) が起きていたら、この
            # touch は捨てられた提示ウィンドウの主張であり棄却される
            # (Codex 3〜4巡目 2026-07-30)。
            self.session_lifecycle.touch_anchor_after_llm_call(
                persona, usage, anchor_id=entry.get("anchor_id"),
                only_if_anchor_unchanged=True,
            )
        LOGGER.info(
            "[keepalive] cache keep-alive completed (persona=%s model=%s "
            "cache_read=%s cache_write=%s)",
            persona_id, model_key,
            getattr(usage, "cached_tokens", None),
            getattr(usage, "cache_write_tokens", None),
        )
        return True

    def _is_auto_recall_enabled_for_persona(self, persona) -> bool:
        """Check per-persona 自動想起 (記憶アーキv2 ゾーン C) トグルを DB から確認する。

        False の場合、sea/runtime_context.py の _maybe_inject_auto_recall は
        末尾注入を行わず、粘着台帳もリセットする。手動想起 (recall_entry /
        recall_navigate スペル) には影響しない。デフォルト True。
        """
        persona_id = getattr(persona, "persona_id", None)
        if not persona_id or not self.manager:
            return True  # fallback: enabled
        db = self.manager.SessionLocal()
        try:
            from database.models import AI as AIModel
            ai = db.query(AIModel).filter_by(AIID=persona_id).first()
            return ai.AUTO_RECALL_ENABLED if ai else True
        finally:
            db.close()

    def _is_spell_enabled_for_persona(self, persona) -> bool:
        """Check per-persona spell system toggle from DB."""
        persona_id = getattr(persona, "persona_id", None)
        if not persona_id or not self.manager:
            return False  # fallback: disabled
        db = self.manager.SessionLocal()
        try:
            from database.models import AI as AIModel
            ai = db.query(AIModel).filter_by(AIID=persona_id).first()
            return ai.SPELL_ENABLED if ai else False
        finally:
            db.close()

    def _is_realtime_info_enabled_for_persona(self, persona) -> bool:
        """Check per-persona realtime info injection toggle from DB."""
        persona_id = getattr(persona, "persona_id", None)
        if not persona_id or not self.manager:
            return True  # fallback: enabled
        db = self.manager.SessionLocal()
        try:
            from database.models import AI as AIModel
            ai = db.query(AIModel).filter_by(AIID=persona_id).first()
            return ai.REALTIME_INFO_ENABLED if ai else True
        finally:
            db.close()

    # ---------------- context preparation -----------------

    def _prepare_context(self, persona: Any, building_id: str, user_input: Optional[str], requirements: Optional[Any] = None, pulse_id: Optional[str] = None, warnings: Optional[List[Dict[str, Any]]] = None, preview_only: bool = False, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None, cancellation_token: Optional[Any] = None, pulse_type: Optional[str] = None, model_key: Optional[str] = None, context_meta: Optional[Dict[str, Any]] = None, persona_voiced: bool = False, persist_anchor_advance: bool = True) -> List[Dict[str, Any]]:
        return prepare_context_impl(
            self,
            persona,
            building_id,
            user_input,
            requirements=requirements,
            pulse_id=pulse_id,
            warnings=warnings,
            preview_only=preview_only,
            event_callback=event_callback,
            cancellation_token=cancellation_token,
            pulse_type=pulse_type,
            model_key=model_key,
            context_meta=context_meta,
            persona_voiced=persona_voiced,
            persist_anchor_advance=persist_anchor_advance,
        )

    # ---- Context Preview (read-only, no side effects) ----

    def preview_context(
        self,
        persona: Any,
        building_id: str,
        user_input: str,
        meta_playbook: Optional[str] = None,
        image_count: int = 0,
        document_count: int = 0,
    ) -> Dict[str, Any]:
        return preview_context_impl(
            self, persona, building_id, user_input,
            meta_playbook=meta_playbook,
            image_count=image_count,
            document_count=document_count,
        )

    def _enrich_history_with_attachments(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Enrich history messages with attachment context.

        If a message has metadata with attached items (images/documents),
        append a system note about the created items to help persona understand context.
        """
        enriched = []
        for msg in messages:
            metadata = msg.get("metadata", {})
            if not metadata:
                enriched.append(msg)
                continue

            # Collect attachment info
            attachment_notes = []

            # Check for images with item_name
            images = metadata.get("images", [])
            for img in images:
                item_name = img.get("item_name")
                if item_name:
                    attachment_notes.append(f"画像「{item_name}」")

            # Check for documents with item_name
            documents = metadata.get("documents", [])
            for doc in documents:
                item_name = doc.get("item_name")
                if item_name:
                    attachment_notes.append(f"ドキュメント「{item_name}」")

            if attachment_notes:
                # Append system note to content
                original_content = msg.get("content", "")
                items_str = "、".join(attachment_notes)
                note = f"\n<system>添付アイテム作成: {items_str}</system>"
                enriched_msg = {**msg, "content": original_content + note}
                enriched.append(enriched_msg)
            else:
                enriched.append(msg)

        return enriched

    def _build_realtime_context(
        self,
        persona: Any,
        building_id: str,
        history_messages: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Build realtime context message with time-sensitive information.

        This message is placed near the end of context (before the current prompt)
        to improve LLM context caching efficiency. Time-sensitive info here doesn't
        invalidate the cached prefix (system prompt, persona info, building info, etc.).

        Contents:
        - Current timestamp (year/month/day, weekday, hour:minute)
        - Previous AI response timestamp (for time passage awareness)
        - Spatial info from Unity gateway (if connected)
        - (Future) Auto-recalled memory content

        Returns:
            Message dict with role="user" and <system> wrapper, or None if no content.
        """
        from datetime import datetime

        # Per-persona toggle: ペルソナ設定で OFF なら、リアルタイム情報セクション
        # 自体を一切組み立てず送らない (現在時刻・前回発言時刻・空間情報すべて含む)。
        if not self._is_realtime_info_enabled_for_persona(persona):
            LOGGER.debug(
                "[sea][realtime-context] Skipped: REALTIME_INFO_ENABLED is off for persona %s",
                getattr(persona, "persona_id", None),
            )
            return None

        sections: List[str] = []

        # 1. Current timestamp
        # 仮想クロック (一日シミュレータ) 有効時は仮想時刻を見せる。実時刻を
        # 注入すると、判断プロンプト側の仮想時刻 (saiverse.clock 経由) と head の
        # 「現在時刻」が矛盾し、ペルソナの世界像が実時計に引っ張られる
        # (2026-07-05 実 LLM シム 異常 #3: 09:00 起床なのに時間割が 15:00 始まり)。
        # 実モードでは従来どおり persona.timezone の実時刻 (挙動不変)。
        from saiverse import clock

        if clock.is_virtual():
            now = clock.now()  # naive ローカル (シナリオの仮想時刻)
        else:
            now = datetime.now(persona.timezone)
        weekday_names = ["月", "火", "水", "木", "金", "土", "日"]
        current_time_str = now.strftime(f"%Y年%m月%d日({weekday_names[now.weekday()]}) %H:%M")
        sections.append(f"現在時刻: {current_time_str}")

        # 2. Previous AI response timestamp
        # Find the last assistant/persona message in history with a timestamp
        prev_ai_timestamp = None
        persona_id = getattr(persona, "persona_id", None)
        persona_name = getattr(persona, "persona_name", None)
        for msg in reversed(history_messages):
            role = msg.get("role", "")
            # Check if this is an assistant message or a message from this persona
            if role == "assistant" or (persona_name and msg.get("sender") == persona_name):
                # Try 'created_at' first (SAIMemory format), then 'timestamp' (fallback)
                ts_str = msg.get("created_at") or msg.get("timestamp")
                if ts_str:
                    try:
                        # Handle both ISO format and datetime objects
                        if isinstance(ts_str, datetime):
                            prev_ai_timestamp = ts_str
                        else:
                            prev_ai_timestamp = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                        break
                    except (ValueError, TypeError):
                        pass

        if prev_ai_timestamp:
            # Convert to persona's timezone for display
            if prev_ai_timestamp.tzinfo is not None:
                prev_ai_timestamp = prev_ai_timestamp.astimezone(persona.timezone)
            prev_time_str = prev_ai_timestamp.strftime(f"%Y年%m月%d日({weekday_names[prev_ai_timestamp.weekday()]}) %H:%M")
            sections.append(f"あなたの前回発言: {prev_time_str}")

        # 3. Spatial context (Unity gateway)
        try:
            unity_gateway = getattr(self.manager, "unity_gateway", None)
            if unity_gateway and getattr(unity_gateway, "is_running", False):
                spatial_state = unity_gateway.spatial_state.get(persona_id) if persona_id else None
                if spatial_state:
                    distance = getattr(spatial_state, "distance_to_player", None)
                    is_visible = getattr(spatial_state, "is_visible", None)

                    spatial_lines = []
                    if distance is not None:
                        spatial_lines.append(f"プレイヤーとの距離: {distance:.1f}m")
                    if is_visible is not None:
                        visibility_text = "見える" if is_visible else "見えない"
                        spatial_lines.append(f"プレイヤーの視認: {visibility_text}")

                    if spatial_lines:
                        sections.append("空間情報: " + " / ".join(spatial_lines))
                        LOGGER.debug("[sea][realtime-context] Added spatial info: distance=%.1f, visible=%s", distance, is_visible)
        except Exception as exc:
            LOGGER.debug("[sea][realtime-context] Failed to get spatial context: %s", exc)

        if not sections:
            return None

        # user role + <system> タグ統一形式 (詳細: sea/runtime_llm.py の同様の
        # 自動ラップ箇所のコメント参照)。Gemini 等が messages 中途の system role
        # を受け付けないため、role='user' + <system>...</system> で全プロバイダ
        # 共通の「指示扱いブロック」として送る。system role に直すと Gemini
        # 互換が壊れる — 「system っぽいから system role にしたい」直感は誤り。
        content = "<system>\n## リアルタイム情報\n" + "\n".join(f"- {s}" for s in sections) + "\n</system>"
        return {
            "role": "user",
            "content": content,
            "metadata": {"__realtime_context__": True},  # Mark for identification
        }

    def _choose_playbook(self, kind: str, persona: Any, building_id: str) -> PlaybookSchema:
        """Resolve playbook by kind with DB→disk→fallback.

        kind="user" のみサポート。auto 系 Pulse は meta_playbook 指定で走るのが
        不変条件だが、未指定のままここへ落ちる呼び出しは**存在しうる** —
        run_meta_user 側が pulse_type を見て WARNING を出す (散文の不変条件は
        破られても無音だった実績あり、autonomous_pulse_vehicle.md §D)。

        対ユーザー会話の Pulse は UserConversationTrackHandler が事前に
        対ユーザー Track を running 化 + Track コンテキストを注入してから
        メインラインを起動するため、Playbook は `track_user_conversation`
        (Phase 3 の 1-LLM + Spell 構成) を使う。

        最終フォールバックの `_basic_chat_playbook()` (in-memory) は、
        `track_user_conversation` が DB / disk のどちらにも存在しない
        異常系での保険として残置 (絶対にここに到達しないことを期待)。
        """
        pb = self._load_playbook_for("track_user_conversation", persona, building_id)
        if pb:
            return pb
        return self._basic_chat_playbook()

    def _basic_chat_playbook(self) -> PlaybookSchema:
        return PlaybookSchema(
            name="basic_chat",
            description="No-op fallback for simple conversations handled by meta layer",
            input_schema=[{"name": "input", "description": "User or system input"}],
            nodes=[
                {
                    "id": "noop",
                    "type": "pass",
                    "next": None,
                },
            ],
            start_node="noop",
        )

    # playbook loading helpers -----------------------------------------
    def _load_playbook_for(self, name: str, persona: Any, building_id: str) -> Optional[PlaybookSchema]:
        pb = self._load_playbook_from_db(name, persona, building_id)
        if not pb:
            LOGGER.warning("[sea] playbook '%s' not found in DB (persona=%s building=%s)", name, getattr(persona, "persona_id", None), building_id)
        return pb

    def _visible(self, model: PlaybookModel, persona: Any, building_id: str) -> bool:
        scope = (model.scope or "public").lower()
        if scope == "public":
            return True
        if scope == "personal":
            return model.created_by_persona_id == getattr(persona, "persona_id", None)
        if scope == "building":
            return model.building_id == building_id
        return False

    def _load_playbook_from_db(self, name: str, persona: Any, building_id: str) -> Optional[PlaybookSchema]:
        session_maker = getattr(self.manager, "SessionLocal", None)
        if session_maker is None:
            return None
        try:
            session = session_maker()
        except Exception:
            return None
        try:
            try:
                rec = (
                    session.query(PlaybookModel)
                    .filter(PlaybookModel.name == name)
                    .first()
                )
            except Exception:
                LOGGER.debug("Playbook table not ready; skipping DB load")
                return None
            if not rec or not self._visible(rec, persona, building_id):
                return None
            # dev_only playbooks require developer mode
            if getattr(rec, "dev_only", False):
                dev_mode = False
                if self.manager and hasattr(self.manager, "state"):
                    dev_mode = getattr(self.manager.state, "developer_mode", False)
                if not dev_mode:
                    LOGGER.debug("[sea] playbook '%s' is dev_only but developer mode is off", name)
                    return None
            try:
                data = json.loads(rec.nodes_json)
                pb = PlaybookSchema(**data)
                validate_playbook_graph(pb)
                LOGGER.debug("[sea] Loaded playbook '%s' with %d input_schema params: %s", pb.name, len(pb.input_schema), [p.name for p in pb.input_schema])
                self._debug_playbook(pb, source="db")
                return pb
            except PlaybookValidationError as exc:
                LOGGER.error("[sea] playbook %s failed validation: %s", name, exc)
                return None
            except Exception:
                LOGGER.exception("Failed to parse playbook %s from DB", name)
                return None
        finally:
            session.close()

    # Disk fallbackを無効化（バグ隠し防止のため）
    def _load_playbook_from_disk(self, name: str) -> Optional[PlaybookSchema]:
        return None
