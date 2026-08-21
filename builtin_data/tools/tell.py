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
- **親 Beat の内側で走る**: スペルとして唱えられる以上、関所も Beat ロックも
  親 (会話 Pulse / セッション) が済ませている。ここで ``hold_beat`` を取り
  直してはいけない — 冗長なだけでなく、executor スレッドへ逃げた spell
  ループから取ると永久ブロックする (:func:`tell` 内のコメント参照)。
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
    # 会話中か確認できなかったときは発声を見送る (fail-closed): 二重発話は
    # ユーザーに届いてしまえば取り消せないが、見送りは次の機会に唱え直せる。
    if target_norm == TARGET_USER:
        try:
            from saiverse.day_plan import get_user_conversation_state
            conversation_state = get_user_conversation_state(manager, persona_id)
        except Exception:
            LOGGER.warning("[tell] conversation check failed", exc_info=True)
            conversation_state = None
        if conversation_state is None:
            return (
                "いまユーザーと会話中かどうかを確認できませんでした。行き違いで"
                "二重に話しかけないよう、今回は見送ります。時間をおいて試せます。"
            )
        if conversation_state:
            return (
                "ユーザーとはいま会話の最中です。伝えたいことは、"
                "返答にそのまま書けば届きます。"
            )

    runtime = getattr(manager, "sea_runtime", None)
    if runtime is None:
        return "声を出す仕組み (runtime) が利用できません。"

    from sea.message_stamp import record_presented_message_ids
    from sea.pulse_context import Aspect, PulseLogEntry, resolve_execution_context
    from sea.work_session import _extract_text, _record_llm_usage

    pulse_id = str(uuid.uuid4())
    pulse_ctx = runtime._get_or_create_pulse_context(pulse_id)
    pulse_ctx.push_line(aspect=Aspect.CONVERSATION)
    # 投函が済んだ後の失敗を「何も起きなかった」と報告しないための印。
    # 声は取り消せないので、届いた後のエラーは「届いた + 記録で失敗」と返す。
    delivered = False
    try:
        # CONVERSATION フレームが active な状態で解決 → 標準モデルが導出される
        # (aspect → tier の既存規則。ここが B 案の「構造で鉄則を守る」本体)。
        execution_context = resolve_execution_context(persona, pulse_ctx)

        # ---- Beat ロックは取らない (beat_execution_context.md §2.2/§3.4) ----
        # スペルは定義上つねに親 Beat (会話 Pulse / 作業・暮らしセッション) の
        # 内側で唱えられる。関所も直列化も親が済ませており、子ラインは別 Beat
        # ではなく親 Beat の一部 — ここで取り直すのは設計上も冗長。
        # さらに実害がある: 同期スペルは常に executor スレッドで実行される
        # (sea/runtime_llm.py の ``run_in_executor(None, _run)``。作業・暮らし
        # セッションではその手前の spell ループ自体も別スレッドへ逃げる —
        # sea/work_session._run_coro_sync)。RLock の再入は取得したスレッドで
        # しか効かないため、別スレッドから取り直すと「親スレッドは結果待ち・
        # ツールスレッドはロック待ち」で永久に固まる (Codex レビュー
        # 2026-08-08 critical)。知覚消費も最外 Beat の頭が担う。
        _context_meta: Dict[str, Any] = {}
        messages = list(runtime._prepare_context(
            persona, building_id, None, pulse_id=pulse_id,
            model_key=execution_context.model_key,
            context_meta=_context_meta,
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
        # 前駆刻印の材料 (sea/message_stamp.py): この発話が実際に見た履歴の
        # ID 列。末尾がこの生成の前駆になる。
        record_presented_message_ids(state, _context_meta)
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
        # 投函: Building 履歴 + UI + TTS/Unity (既存の発話経路がそのまま効く)。
        # **ここを呼んだ時点で「言ってしまった」**— `_emit_say` は履歴の保存に
        # 失敗しても gateway (Discord 等) と Unity へは送る (sea/runtime_emitters.py
        # の emit_say: gateway 送信と unity 通知は insert の成否を見ない)。
        # したがって戻り値から分かるのは「届いたか」ではなく **「この場の記録に
        # 残ったか」**だけ。記録の成否によらず本人の記憶には残す — 自分が言った
        # ことを知らないまま次を喋ると、同じ話を二度することになる。
        emitted = runtime._emit_say(
            persona, building_id, text, pulse_id=pulse_id, metadata=dict(tell_meta),
        )
        delivered = True
        # 記録に残った印は **message_id の有無**。dict が返ったこと自体は証拠に
        # ならない — `HistoryManager.add_to_building_only` は DB insert が失敗
        # しても渡した dict をそのまま返す (`building_msg or for_insert`)。
        # message_id は DB 採番なので insert が通ったときにしか付かない。既存の
        # 消費者 (sea/runtime.py の say イベント / sea/runtime_llm.py の
        # `_last_message_id`) も同じ印で判定している。
        emitted_id = emitted.get("message_id") if isinstance(emitted, dict) else None
        if not emitted_id:
            LOGGER.error(
                "[tell] utterance went out but was not persisted to the building "
                "history (persona=%s building=%s target=%s)",
                persona_id, building_id, target_norm,
            )
        # Pulse ログ (監査記録) は記憶の保存より先に積む — 記憶の保存が例外を
        # 投げると、後ろに置いた append は実行されず「何を言ったか」の記録だけが
        # 欠ける (指示文だけが残る)。
        pulse_ctx.append(PulseLogEntry(
            role="assistant", content=text,
            node_id="tell_speech", playbook_name=_PLAYBOOK_NAME,
        ))
        # 本人の記憶に発話として残す (CONVERSATION フレーム下 = committed)。
        # ``return_message_id=True`` で受ける — 既定の bool 戻り値は例外が
        # 出なければ True になり、SAIMemory adapter が None を返す静かな
        # 挿入失敗 (sea/runtime.py `_store_memory` の insert 経路) を成功と
        # 取り違える。id が返ったことだけが行の存在の証拠。
        stored_id = runtime._store_memory(
            persona, text, role="assistant",
            pulse_id=pulse_id, metadata=dict(tell_meta),
            playbook_name=_PLAYBOOK_NAME, pulse_context=pulse_ctx,
            return_message_id=True,
            beat_state=state,
        )
        LOGGER.info(
            "[tell] spoken: persona=%s building=%s target=%s len=%d "
            "history=%s memory=%s",
            persona_id, building_id, target_norm, len(text),
            bool(emitted_id), bool(stored_id),
        )
        if not emitted_id:
            # 記録に残らなかった = 相手に届いたかどうかも確かめられない。
            # 外への配送 (gateway / Unity) は履歴と別経路で走るうえ、宛先が
            # 繋がっていない構成では黙って no-op になる — 「届いた」とも
            # 「届いていない」とも言えない。断定せず、判断の材料だけ返す。
            return (
                f"「{target_display}」への声は、この場の履歴に残せませんでした。"
                "相手に届いたかどうかも確認できません。もう一度言うと二重に"
                "聞こえるおそれがあるので、繰り返すかは慎重に決めてください。"
            )
        if not stored_id:
            # 記録には残ったが本人の記憶に入らなかった。次の文脈に出てこない
            # ので、繰り返さないよう本人に伝える。
            LOGGER.error(
                "[tell] spoken but not stored in SAIMemory "
                "(persona=%s target=%s)", persona_id, target_norm,
            )
            return (
                f"「{target_display}」に声は届きましたが、自分の記憶には"
                "残せませんでした。同じ話を繰り返さないよう気をつけてください。"
            )
        return f"「{target_display}」に声をかけました。"
    except Exception:
        LOGGER.exception("[tell] failed (persona=%s target=%s)", persona_id, target)
        if delivered:
            # 投函の後で転んだ。声はもう出したあとなので「かけられません
            # でした」は嘘になる (届いた話をもう一度しに行かせてしまう)。
            return (
                f"「{target_display}」へ声を出したあと、記録の途中で内部エラーが"
                "起きました。届いているかもしれないので、繰り返すかは慎重に"
                "決めてください。"
            )
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
