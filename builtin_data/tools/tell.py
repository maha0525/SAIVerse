"""tell: 宛先を決めて、この場で声をかける (発声スペル)。

自律行動中にユーザーへ届く発話の唯一の経路 (autonomous_pulse_vehicle.md §B)。

設計の芯:

- **宛先明示** (speak でなく tell): 「誰に伝える価値があるか」を毎回問わせる
  含意が乱発を構造的に防ぐ。宛先は metadata に残り、将来の通知・未読バッジ・
  ペルソナ間配送の器になる。
- **引数式** (autonomous_behavior_v3.md §9-4 決着): 唱える側が言葉そのものを
  ``message`` に書き、それがそのまま届く (メールと同型)。2026-10-10 まで tell は
  要旨 (gist) だけを受けて別建ての標準モデル 1 呼び出しに言葉を書かせていた —
  「唱える側が軽量かもしれない」前提の産物で、ティックが標準モデル・メイン
  ラインになった v3 でその前提は消えた。唱える側がそのまま本人の声。
- **標準文脈限定**: 軽量文脈 (分身モード = WORKER サブライン等) から唱えられたら
  投函しない。引数式では唱えた側の言葉がそのまま届くので、開けておくと軽量
  モデルが本人の声でユーザーに喋ってしまう。aspect の無い legacy 経路は標準扱い
  (sea/pulse_context.py の ``tier_without_aspect`` と同じ向き)。
- **唱えた Pulse の内側の仕事**: 投函は唱えた返事 (Pulse) の pulse_id で行い、
  自前の Pulse・ライン・文脈は作らない。本人の記憶 (SAIMemory) にも別の行を
  書かない — 言葉は唱えた本文のスペル行として既に本人の記憶に残っている。
- **世界の状態に触らない**: 会話を開かない・無応答タイムアウトを装填しない。
  返事はユーザーの通常入力が既存機構で会話を開く (「喋る」と「会話が始まる」は
  別の出来事)。
- **親 Beat の内側で走る**: スペルとして唱えられる以上、関所も Beat ロックも
  親 (会話 Pulse / ティック) が済ませている。ここで ``hold_beat`` を取り
  直してはいけない — 冗長なだけでなく、executor スレッドで走る同期スペルから
  取ると永久ブロックする (:func:`tell` 内のコメント参照)。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple, Union

from tools.context import (
    get_active_manager,
    get_active_persona_id,
    get_active_pulse_context,
    get_event_callback,
)
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


def _lightweight_refusal(pulse_ctx: Any) -> Optional[str]:
    """いまのラインが軽量 tier なら断りの文を、標準なら None を返す。

    tier はアクティブなラインの aspect から引く (sea/pulse_context.py の
    ``LineFrame.model_tier``)。Pulse の外 / ラインの無い実行 / aspect の無い
    legacy フレームは標準扱い — ``tier_without_aspect`` が state の無いときに
    標準を返すのと同じ向き。軽量で走る経路 (サブライン・分離したサブラインを
    含む) は sea/runtime_graph.py が必ず WORKER フレームを積むので、ここで
    フレームの有無を tier の代わりに読んでも食い違わない。

    身分を **確かめられなかった** とき (current_line が例外を投げた) は発声を
    見送る (fail-closed)。会話中確認と同じ理屈: 声は取り消せないが、見送りは
    次の機会に唱え直せる。
    """
    frame = None
    if pulse_ctx is not None:
        try:
            frame = pulse_ctx.current_line()
        except Exception:
            LOGGER.warning(
                "[tell] current_line failed; refusing to speak (fail-closed)",
                exc_info=True,
            )
            return (
                "いまどのモードで動いているかを確認できませんでした。この声は"
                "あなた本人の声としてそのまま相手に届くので、確かめられないまま"
                "には出せません。今回は見送ります。時間をおいて試せます。"
            )
    aspect = getattr(frame, "aspect", None)
    if aspect is None or aspect.model_tier != "lightweight":
        return None
    return (
        f"tellスペルは現在のモード（{aspect.mode_display_name}）では実行できません。"
        "この声はあなた本人の声としてそのまま相手に届くので、分身の時間からは"
        "唱えられません。本体の時間に唱えてください。"
    )


def _failure(text: str) -> Tuple[str, Dict[str, Any]]:
    """拒否・失敗の返却形。``meta.error is True`` がツールの論理的失敗の印。

    スペル経路 (sea/runtime_llm.py の ``_run_spell_tool_async`` →
    ``tools.core.parse_tool_result``) は ``(str, dict)`` の dict を結果の
    metadata として運び、失敗判定 (``_is_failed_spell_record`` / quick_spell の
    終端判定) は ``success=False`` か ``meta.error is True`` しか見ない。素の
    文字列で断ると「成功」に数えられ、一回で閉じる Beat (ティック) では失敗の
    知覚が本人へ届かない (Codex 敵対レビュー 2026-10-10 medium)。
    """
    return text, {"error": True}


def tell(target: str, message: str = "") -> Union[str, Tuple[str, Dict[str, Any]]]:
    """宛先を決めて声をかける。``message`` に書いた言葉がそのまま届く。

    戻り値: 届いて記録にも残った回だけ素の文字列。拒否・失敗 (投函後に記録へ
    残らなかった回を含む) は ``(文字列, {"error": True})`` — :func:`_failure`。
    """
    manager = get_active_manager()
    persona_id = get_active_persona_id()
    if manager is None or not persona_id:
        raise RuntimeError(
            "Active persona/manager context is not set. Use tools.context.persona_context()."
        )
    persona = (getattr(manager, "personas", {}) or {}).get(persona_id)
    if persona is None:
        raise RuntimeError(f"persona '{persona_id}' not found on manager")

    # この一言は唱えた返事 (Pulse) の中の仕事。pulse_id もラインもそこから引く。
    pulse_ctx = get_active_pulse_context()
    refusal = _lightweight_refusal(pulse_ctx)
    if refusal is not None:
        LOGGER.info("[tell] refused from a lightweight line (persona=%s)", persona_id)
        return _failure(refusal)

    text = (message or "").strip()
    if not text:
        return _failure(
            "伝える言葉が空です。届けたい言葉を message にそのまま書いてください。"
        )

    building_id = getattr(persona, "current_building_id", None)
    if not building_id:
        return _failure("いまはどの場所にもいないため、声をかけられません。")

    target_norm, target_display = _resolve_target(
        manager, persona_id, building_id, target,
    )
    if target_norm is None:
        return _failure(target_display)  # 理由文

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
            return _failure(
                "いまユーザーと会話中かどうかを確認できませんでした。行き違いで"
                "二重に話しかけないよう、今回は見送ります。時間をおいて試せます。"
            )
        if conversation_state:
            return _failure(
                "ユーザーとはいま会話の最中です。伝えたいことは、"
                "返答にそのまま書けば届きます。"
            )

    runtime = getattr(manager, "sea_runtime", None)
    if runtime is None:
        return _failure("声を出す仕組み (runtime) が利用できません。")

    pulse_id = getattr(pulse_ctx, "pulse_id", None) or None

    # ---- Beat ロックは取らない (beat_execution_context.md §2.2/§3.4) ----
    # スペルは定義上つねに親 Beat (会話 Pulse / ティック) の内側で唱えられる。
    # 関所も直列化も親が済ませており、この投函は親 Beat の一部 — ここで取り直す
    # のは設計上も冗長。
    # さらに実害がある: 同期スペルは常に executor スレッドで実行される
    # (sea/runtime_llm.py の ``run_in_executor(None, _run)``)。RLock の再入は
    # 取得したスレッドでしか効かないため、別スレッドから取り直すと「親スレッドは
    # 結果待ち・ツールスレッドはロック待ち」で永久に固まる (Codex レビュー
    # 2026-08-08 critical)。知覚消費も最外 Beat の頭が担う。

    # 投函の後の失敗を「何も起きなかった」と報告しないための印。
    # 声は取り消せないので、届いた後のエラーは「届いた + 記録で失敗」と返す。
    delivered = False
    try:
        tell_meta = {"tell_target": target_norm}
        # 投函: Building 履歴 + UI + TTS (既存の発話経路がそのまま効く)。
        # **ここを呼んだ時点で「言ってしまった」**— `_emit_say` は履歴の保存に
        # 失敗しても gateway (Discord 等) へは送る (sea/runtime_emitters.py
        # の emit_say: gateway 送信は insert の成否を見ない)。
        # したがって戻り値から分かるのは「届いたか」ではなく **「この場の記録に
        # 残ったか」**だけ。
        # event_callback: 親 Beat の persona_context が contextvar で運んで
        # くる。渡しておくと、建物への保存が成功した回だけ保存完了イベント
        # (speak_persisted) が流れる (発火は emit_say 内の共通の口 —
        # この tell も assistant 発言を保存する経路の一つ)。
        emitted = runtime._emit_say(
            persona, building_id, text, pulse_id=pulse_id, metadata=dict(tell_meta),
            event_callback=get_event_callback(),
        )
        delivered = True
        # 記録に残った印は **message_id の有無**。dict が返ったこと自体は証拠に
        # ならない — `HistoryManager.add_to_building_only` は DB insert が失敗
        # しても渡した dict をそのまま返す (`building_msg or for_insert`)。
        # message_id は DB 採番なので insert が通ったときにしか付かない。既存の
        # 消費者 (sea/runtime.py の say イベント / sea/runtime_llm.py の
        # `_last_message_id`) も同じ印で判定している。
        emitted_id = emitted.get("message_id") if isinstance(emitted, dict) else None
        if pulse_ctx is not None:
            # 唱えた Pulse の監査記録 (pulse_logs) に、何を誰に言ったかを積む。
            # flush は Pulse の終わりに親が行う。
            from sea.pulse_context import PulseLogEntry

            pulse_ctx.append(PulseLogEntry(
                role="assistant", content=text,
                node_id="tell_speech", playbook_name=_PLAYBOOK_NAME,
            ))
        LOGGER.info(
            "[tell] spoken: persona=%s building=%s target=%s pulse=%s len=%d history=%s",
            persona_id, building_id, target_norm, pulse_id, len(text), bool(emitted_id),
        )
        if not emitted_id:
            LOGGER.error(
                "[tell] utterance went out but was not persisted to the building "
                "history (persona=%s building=%s target=%s)",
                persona_id, building_id, target_norm,
            )
            # 記録に残らなかった = 相手に届いたかどうかも確かめられない。
            # 外への配送 (gateway) は履歴と別経路で走るうえ、宛先が
            # 繋がっていない構成では黙って no-op になる — 「届いた」とも
            # 「届いていない」とも言えない。断定せず、判断の材料だけ返す。
            # 失敗の印 (error) を付ける — 一回で閉じる Beat (ティック) は結果を
            # 続きの生成に回さないので、印が無いとこの文面は本人に届かず、
            # 二重発話を避ける判断の材料ごと消える。
            return _failure(
                f"「{target_display}」への声は、この場の履歴に残せませんでした。"
                "相手に届いたかどうかも確認できません。もう一度言うと二重に"
                "聞こえるおそれがあるので、繰り返すかは慎重に決めてください。"
            )
        return f"「{target_display}」に声をかけました。"
    except Exception:
        LOGGER.exception("[tell] failed (persona=%s target=%s)", persona_id, target)
        if delivered:
            # 投函の後で転んだ。声はもう出したあとなので「かけられません
            # でした」は嘘になる (届いた話をもう一度しに行かせてしまう)。
            return _failure(
                f"「{target_display}」へ声を出したあと、記録の途中で内部エラーが"
                "起きました。届いているかもしれないので、繰り返すかは慎重に"
                "決めてください。"
            )
        return _failure("声をかけられませんでした (内部エラー)。時間をおいて試せます。")


def schema() -> ToolSchema:
    return ToolSchema(
        name="tell",
        description=(
            "Say something out loud to someone here, in your own voice. Write the "
            "exact words you want to say in 'message' — they are delivered as-is, "
            "as your own speech; nothing is rewritten or composed for you. Specify "
            "who it is for: 'user' (the user), 'all' (everyone in this place), or "
            "the name of a persona in the same place. This does not start a "
            "conversation — if the target replies, a conversation begins naturally. "
            "While you are already talking with the user, just write it in your "
            "reply instead."
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
                "message": {
                    "type": "string",
                    "description": (
                        "The exact words to say, written in your own voice. "
                        "Delivered to the target exactly as written."
                    ),
                },
            },
            "required": ["target", "message"],
        },
        result_type="string",
        spell=True,
        # 自律行動が v0.3.0 で未出荷のため、通常会話での無駄撃ちを避けて UI と
        # head のスペル一覧から隠す (2026-09-01 裁定)。ペルソナからの実行は可能。
        # 復帰条件: 自律行動の出荷時に spell_visible=True へ戻す。
        spell_visible=False,
        spell_display_name="声をかける",
    )
