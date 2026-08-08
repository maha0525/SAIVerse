"""tell: 宛先を決めて、この場で声をかける (発声スペル)。

自律行動中にユーザーへ届く発話の唯一の経路 (autonomous_pulse_vehicle.md §B)。

設計の芯:

- **宛先明示** (speak でなく tell): 「誰に伝える価値があるか」を毎回問わせる
  含意が乱発を構造的に防ぐ。宛先は metadata に残り、将来の通知・未読バッジ・
  ペルソナ間配送の器になる。
- **1 Beat の分業**: 唱える側 (軽量モデルでありうる) は「いま・誰に・何を」の
  判断だけを担う。実際の言葉は、このツールが起動する 1 Beat — CONVERSATION
  aspect の実行文脈 = 標準モデルの 1 呼び出し — が書く。モデル階層は aspect
  導出の既存規則で決まるため、「ユーザーに届く声は標準モデル」の鉄則が
  禁止ルールなしで構造的に成立する。
- **世界の状態に触らない**: 会話エピソードを開かない・Track に触らない・
  無応答タイムアウトを装填しない。返事はユーザーの通常入力が既存機構で
  会話を開く (「喋る」と「会話が始まる」は別の出来事)。
"""
from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace
from typing import Any, Dict, Optional, Tuple

from tools.context import get_active_manager, get_active_persona_id
from tools.core import ToolSchema

LOGGER = logging.getLogger(__name__)

TARGET_USER = "user"
TARGET_ALL = "all"

_PLAYBOOK_NAME = "tell"


def _resolve_target(
    manager: Any, persona_id: str, building_id: str, target: str,
) -> Tuple[Optional[str], str]:
    """target を検証し、(正規化 target, 表示名) を返す。不正なら (None, 理由)。

    正規化 target は 'user' / 'all' / 同室ペルソナの persona_id のいずれか。
    """
    raw = (target or "").strip()
    if not raw:
        return None, "target が空です。user / all / 同じ場所にいるペルソナ名を指定してください。"
    if raw.lower() == TARGET_USER:
        return TARGET_USER, "ユーザー"
    if raw.lower() == TARGET_ALL:
        return TARGET_ALL, "この場にいるみんな"
    occupants = list((getattr(manager, "occupants", {}) or {}).get(building_id, []))
    personas = getattr(manager, "personas", {}) or {}
    for oid in occupants:
        if oid == persona_id:
            continue
        p = personas.get(oid)
        name = getattr(p, "persona_name", None) if p is not None else None
        if raw == oid or (name and raw == name):
            return oid, str(name or oid)
    return None, (
        f"「{raw}」はこの場所にいません。声をかけられる相手: user / all"
        + "".join(
            f" / {getattr(personas.get(oid), 'persona_name', oid)}"
            for oid in occupants if oid != persona_id
        )
    )


def _build_directive(target_display: str, gist: str) -> str:
    """言葉を書く 1 Beat への指示 (user role + <system> の統一形式)。"""
    gist_line = f"伝えたいことのメモ: {gist}\n" if gist else ""
    return (
        "<system>\n"
        f"あなたはいま、この場で「{target_display}」に向けて声をかけようとしています。\n"
        f"{gist_line}"
        "相手に届けたい言葉だけを、普段のあなたの声で書いてください。\n"
        "スペルは使えません。\n"
        "</system>"
    )


def tell(target: str, gist: str = "") -> str:
    """宛先を決めて声をかける。言葉は会話の声 (標準モデルの 1 Beat) が書く。"""
    manager = get_active_manager()
    persona_id = get_active_persona_id()
    if manager is None or not persona_id:
        raise RuntimeError(
            "Active persona/manager context is not set. Use tools.context.persona_context()."
        )
    persona = (getattr(manager, "personas", {}) or {}).get(persona_id)
    if persona is None:
        raise RuntimeError(f"persona '{persona_id}' not found on manager")
    building_id = getattr(persona, "current_building_id", None)
    if not building_id:
        return "いまはどの場所にもいないため、声をかけられません。"

    target_norm, target_display = _resolve_target(
        manager, persona_id, building_id, target,
    )
    if target_norm is None:
        return target_display  # 理由文

    # 会話中の相手への tell は実行しない (返答との二重発話の防止)。
    # 会話中でも別の相手 (同席ペルソナ / all) への一言は正当なので許す。
    if target_norm == TARGET_USER:
        try:
            from saiverse.day_plan import is_in_user_conversation
            if is_in_user_conversation(manager, persona_id):
                return (
                    "ユーザーとはいま会話の最中です。伝えたいことは、"
                    "返答にそのまま書けば届きます。"
                )
        except Exception:
            LOGGER.warning("[tell] conversation check failed; continuing", exc_info=True)

    runtime = getattr(manager, "sea_runtime", None)
    if runtime is None:
        return "声を出す仕組み (runtime) が利用できません。"

    from sea.beat_gate import hold_beat
    from sea.pulse_context import Aspect, PulseLogEntry, resolve_execution_context
    from sea.work_session import _extract_text, _record_llm_usage

    pulse_id = str(uuid.uuid4())
    pulse_ctx = runtime._get_or_create_pulse_context(pulse_id)
    pulse_ctx.push_line(aspect=Aspect.CONVERSATION, track_id=None)
    try:
        # CONVERSATION フレームが active な状態で解決 → 標準モデルが導出される
        # (aspect → tier の既存規則。ここが B 案の「構造で鉄則を守る」本体)。
        execution_context = resolve_execution_context(persona, pulse_ctx)
        with hold_beat(manager, persona_id, purpose="tell"):
            # 呼び出し元のセッション Beat の内側から唱えられた場合は再入
            # (子 Beat 扱い)。知覚消費は最外 Beat の頭が担うのでここではしない。
            messages = list(runtime._prepare_context(
                persona, building_id, None, pulse_id=pulse_id,
                model_key=execution_context.model_key,
                persona_voiced=True,
            ))
            directive = _build_directive(target_display, (gist or "").strip())
            messages.append({"role": "user", "content": directive})

            state: Dict[str, Any] = {
                "_pulse_id": pulse_id,
                "_pulse_type": "tell",
                "_pulse_context": pulse_ctx,
                "_execution_context": execution_context,
                "_messages": messages,
            }
            node_def = SimpleNamespace(id="tell_speech", memorize=None, speak=True)
            llm_client, selected_model = runtime.select_llm_client(
                node_def, persona, execution_context=execution_context, state=state,
            )
            if selected_model != execution_context.model_key:
                execution_context = execution_context.with_model(selected_model)
                state["_execution_context"] = execution_context

            pulse_ctx.append(PulseLogEntry(
                role="user", content=directive,
                node_id="tell_directive", playbook_name=_PLAYBOOK_NAME,
            ))
            result = llm_client.generate(
                messages,
                tools=[],
                temperature=runtime._default_temperature(persona),
                **runtime._get_cache_kwargs(persona_id),
            )
            _record_llm_usage(
                runtime, state, llm_client, persona, building_id,
                "llm_tell", playbook_name=_PLAYBOOK_NAME,
            )
            text = _extract_text(result).strip()
            try:
                runtime._dump_llm_io(_PLAYBOOK_NAME, "tell_speech", persona, messages, text)
            except Exception:
                LOGGER.warning("[tell] failed to dump LLM I/O", exc_info=True)
            if not text:
                return "言葉が出てきませんでした (生成が空)。もう一度試せます。"

            tell_meta: Dict[str, Any] = {"tell_target": target_norm}
            if gist and gist.strip():
                tell_meta["tell_gist"] = gist.strip()
            # 投函: Building 履歴 + UI + TTS/Unity (既存の発話経路がそのまま効く)
            runtime._emit_say(
                persona, building_id, text, pulse_id=pulse_id, metadata=dict(tell_meta),
            )
            # 本人の記憶にも発話として残す (CONVERSATION フレーム下 = committed)
            runtime._store_memory(
                persona, text, role="assistant",
                pulse_id=pulse_id, metadata=dict(tell_meta),
                playbook_name=_PLAYBOOK_NAME, pulse_context=pulse_ctx,
            )
            pulse_ctx.append(PulseLogEntry(
                role="assistant", content=text,
                node_id="tell_speech", playbook_name=_PLAYBOOK_NAME,
            ))
        LOGGER.info(
            "[tell] delivered: persona=%s building=%s target=%s len=%d",
            persona_id, building_id, target_norm, len(text),
        )
        return f"「{target_display}」に声をかけました。"
    except Exception:
        LOGGER.exception("[tell] failed (persona=%s target=%s)", persona_id, target)
        return "声をかけられませんでした (内部エラー)。時間をおいて試せます。"
    finally:
        try:
            pulse_ctx.pop_line()
        except Exception:
            LOGGER.warning("[tell] failed to pop line frame", exc_info=True)
        try:
            runtime._flush_pulse_logs(persona, pulse_ctx)
        except Exception:
            LOGGER.warning("[tell] failed to flush pulse logs", exc_info=True)


def schema() -> ToolSchema:
    return ToolSchema(
        name="tell",
        description=(
            "Speak out loud to someone here, in your own voice. Specify who it is "
            "for: 'user' (the user), 'all' (everyone in this place), or the name of "
            "a persona in the same place. The actual words are composed and spoken "
            "as your normal conversational voice; this does not start a conversation "
            "state — if the target replies, a conversation begins naturally."
        ),
        parameters={
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": (
                        "Who to address: 'user' / 'all' / a persona name in the "
                        "same place."
                    ),
                },
                "gist": {
                    "type": "string",
                    "description": (
                        "Optional: a short note of what you want to convey (your "
                        "own memo; the spoken words are composed from it and the "
                        "current context)."
                    ),
                },
            },
            "required": ["target"],
        },
        result_type="string",
        spell=True,
        spell_display_name="声をかける",
    )
