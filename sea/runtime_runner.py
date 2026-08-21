from __future__ import annotations

import logging
import uuid
from typing import Any, Callable, Dict, List, Optional

from sea.message_stamp import record_presented_message_ids
from sea.playbook_models import PlaybookSchema

LOGGER = logging.getLogger(__name__)


def run_playbook(
    runtime: Any,
    playbook: PlaybookSchema,
    persona: Any,
    building_id: str,
    user_input: Optional[str],
    auto_mode: bool,
    record_history: bool = True,
    parent_state: Optional[Dict[str, Any]] = None,
    event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    cancellation_token: Optional[Any] = None,
    pulse_type: Optional[str] = None,
    initial_params: Optional[Dict[str, Any]] = None,
    isolate_pulse_context: bool = False,
    line: str = "main",
    pulse_line_aspect: Optional[Any] = None,  # sea.pulse_context.Aspect
    pre_spells: Optional[List[str]] = None,
) -> List[str]:
    if cancellation_token:
        cancellation_token.raise_if_cancelled()

    parent = parent_state or {}

    if initial_params:
        LOGGER.debug("[sea] _run_playbook received args: %s", list(initial_params.keys()))
        # Store args for compile_with_langgraph to resolve via input_schema
        parent["_args"] = dict(initial_params)

    # UI-triggered pre-spells: forwarded into initial_state so lg_llm_node
    # can execute them before the first LLM call. See nested_subline_spell.md §13.
    if pre_spells:
        parent["_pre_spells"] = list(pre_spells)
        LOGGER.info("[sea] _run_playbook received pre_spells: %s", pre_spells)
    LOGGER.debug("[sea] _run_playbook called for %s, parent_state keys: %s", playbook.name, list(parent.keys()) if parent else "(none)")
    if "_pulse_id" in parent:
        pulse_id = str(parent["_pulse_id"])
    else:
        pulse_id = str(uuid.uuid4())

    parent_chain = parent.get("_playbook_chain", "")
    if parent_chain:
        current_chain = f"{parent_chain} > {playbook.name}"
    else:
        current_chain = playbook.name

    parent["_playbook_chain"] = current_chain

    if cancellation_token:
        parent["_cancellation_token"] = cancellation_token

    def wrapped_event_callback(event: Dict[str, Any]) -> None:
        if event_callback:
            if event.get("type") == "status":
                node = event.get("node", "")
                event["content"] = f"{current_chain} / {node}"
                event["playbook_chain"] = current_chain
            event_callback(event)

    if hasattr(persona, "execution_state"):
        persona.execution_state["playbook"] = playbook.name
        persona.execution_state["node"] = playbook.start_node
        persona.execution_state["status"] = "running"

    # ライン分岐: line="sub" の場合、_prepare_context (SAIMemory 再構築) を bypass し、
    # 親 state["_messages"] のコピーをそのまま base_messages とする。
    # これによりサブラインは「呼び出し時点の親メインラインの会話履歴」を引き継ぐ。
    # See: docs/intent/persona_action_tracks.md (v0.9 サブライン分岐の messages コピー仕様)
    context_warnings: List[Dict[str, Any]] = []
    # 親の messages が**空でない**ときだけ分岐を引き継ぐ。空リストは「親がいない」
    # (spell loop 外からの直接呼び出し) のサインで、そのまま採用すると文脈ゼロの
    # まま LLM を走らせ、その出力をペルソナ名義で記録することになる — work_session
    # の history_depth=0 と同型の事故 (2026-07-23 Codex レビュー指摘)。
    # 親がいない場合は下の else に落として通常のペルソナ文脈を組ませる
    # (サブラインの軽量モデル強制はそちらでも維持する)。
    if line == "sub" and parent.get("_messages"):
        parent_messages = parent.get("_messages") or []
        base_messages = list(parent_messages)  # コピー (参照共有しない)
        LOGGER.info(
            "[sea][run-playbook] %s: line='sub', forking parent _messages (%d messages) instead of "
            "calling _prepare_context. Lightweight model will be used.",
            playbook.name, len(base_messages),
        )
        # サブラインで動かすときは軽量モデルを強制 (LLM ノードの model_type 指定を上書き)
        parent["_force_lightweight_model"] = True
    else:
        if line == "sub":
            # 親のいないサブライン。文脈は下で通常どおり組むが、サブラインである
            # 以上モデルの tier は軽量のままにする (分岐先の挙動を親の有無で
            # 変えない)。
            parent["_force_lightweight_model"] = True
            LOGGER.info(
                "[sea][run-playbook] %s: line='sub' but no parent messages — "
                "building a normal persona context instead of running context-free",
                playbook.name,
            )
        # Playbook が context_requirements を指定していなければ何も渡さない。
        # 「指定なし」の意味は prepare_context 側 (ContextRequirements のフィールド
        # 既定 = 履歴フル + 固定 head) の一箇所だけで決まる。2026-07-23 以前は
        # ここに _FULL_CONTEXT_REQUIREMENTS という第二の既定があり、フィールド既定
        # と値が食い違っていた (docs/issues/llm_call_entry_point_standardization.md)。
        effective_requirements = playbook.context_requirements
        LOGGER.info(
            "[sea][run-playbook] %s: calling _prepare_context with history_depth=%s, pulse_id=%s, "
            "requirements=%s",
            playbook.name,
            effective_requirements.history_depth if effective_requirements else "default",
            pulse_id,
            "playbook-defined" if playbook.context_requirements else "default",
        )
        # persona._pending_auto_recall_text は _prepare_context (実体は
        # _maybe_inject_auto_recall) が「今回このメッセージ列に注入した」場合のみ
        # 立てる一時属性。呼び出し前に一旦クリアしておき、今回の呼び出しで注入が
        # 起きなければ古い値を引きずらないようにする (state 経由で say/speak
        # ノードまで運ぶ設計は reasoning の _reasoning_text と同じ、docs/intent/
        # memory_architecture_v2.md §4.5)。
        persona._pending_auto_recall_text = None
        # head はこの Pulse を実行する model の Session (persona, model) に向けて
        # render する (beat_execution_context.md §3.1)。実行 model は LLM ノードが
        # resolve_execution_context で解決する値と同じ導出をここで先取りする —
        # フレームはまだ push されていないため legacy フォールバック
        # (_pulse_type=='auto' / _force_lightweight_model → lightweight) を通すが、
        # これは Pulse-root aspect の tier (AUTONOMOUS=lightweight /
        # CONVERSATION・META=standard) と一致する。
        from sea.pulse_context import resolve_execution_context
        _ec_probe_state = dict(parent)
        if pulse_type is not None:
            _ec_probe_state.setdefault("_pulse_type", pulse_type)
        _prepared_model_key = resolve_execution_context(
            persona, None, state=_ec_probe_state,
        ).model_key
        # call-local anchor (beat_execution_context.md §3.2): _prepare_context が
        # 今回の prefix に採用した anchor を out-param で受け取り、state 経由で
        # LLM 成功後の touch_anchor_after_llm_call(anchor_id=...) まで運ぶ。
        _context_meta: Dict[str, Any] = {}
        base_messages = runtime._prepare_context(
            persona,
            building_id,
            user_input,
            effective_requirements,
            pulse_id=pulse_id,
            warnings=context_warnings,
            event_callback=wrapped_event_callback,
            cancellation_token=cancellation_token,
            pulse_type=pulse_type,
            model_key=_prepared_model_key,
            context_meta=_context_meta,
            # メインラインの Playbook はペルソナ本人の発話・思考になる。
            persona_voiced=True,
        )
        parent["_prefix_anchor_id"] = _context_meta.get("prefix_anchor_id")
        # 前駆刻印の材料 (sea/message_stamp.py): 今回の生成が実際に見た履歴の
        # ID 列。末尾が「この生成が見ていた最後のメッセージ」になる。
        record_presented_message_ids(parent, _context_meta)
        LOGGER.info("[sea][run-playbook] %s: _prepare_context returned %d messages", playbook.name, len(base_messages))
        _auto_recall_text = getattr(persona, "_pending_auto_recall_text", None)
        if _auto_recall_text:
            parent["_auto_recall_text"] = _auto_recall_text
            LOGGER.debug(
                "[sea][run-playbook] %s: carrying auto_recall text (%d chars) into state",
                playbook.name, len(_auto_recall_text),
            )
    conversation_msgs = list(base_messages)

    for warn in context_warnings:
        if event_callback:
            wrapped_event_callback(warn)

    compiled_ok = runtime._compile_with_langgraph(
        playbook,
        persona,
        building_id,
        user_input,
        auto_mode,
        conversation_msgs,
        pulse_id,
        parent_state=parent,
        event_callback=wrapped_event_callback,
        cancellation_token=cancellation_token,
        pulse_type=pulse_type,
        isolate_pulse_context=isolate_pulse_context,
        pulse_line_aspect=pulse_line_aspect,
        line=line,
    )
    if compiled_ok is None:
        LOGGER.error(
            "LangGraph compilation failed for playbook '%s'. This indicates a configuration or dependency issue.",
            playbook.name,
        )
        if hasattr(persona, "execution_state"):
            persona.execution_state["playbook"] = None
            persona.execution_state["node"] = None
            persona.execution_state["status"] = "idle"
        return []

    return compiled_ok
