"""返事が途中で止まった回の後始末 — 返事の実行につき一回、すべての書き込みの後で。

設計の正典: docs/intent/reply_stop_exit.md

役割は二つに分かれている。

1. **事実の記録** (sea/runtime_emitters.py の ``notify_speak_persisted``): ペルソナの
   発言が建物の行に本文つきで保存されるたび、「このペルソナが最後に保存した
   発言」を上書きする。止まりかけた生成は、まだ保存していない言いかけを自分で
   保存し、その発言の形 (途中で切れた / スペルの実行が終わる前 / スペルの結果を
   受け取った後) を同じ記録に書き足すだけで、印と通告は置かない。
2. **後始末** (この module の :func:`settle_reply_stop`): 返事の実行の一番外側
   (sea/runtime.py の ``run_meta_user``) が、返事が言い切らずに終わったときに
   **一回だけ**呼ぶ。記録にある最後の発言に「言い切っていない」印を付け、その
   発言の部屋に中断の通告を一枚置き、画面への知らせに案内の材料を載せる。

置く者が一人なので、通告の取り下げ・付け替えや二枚目は原理的に起きない。
スペルの周回・サブライン・スペルループの内側はここを呼ばない — 失敗をそのまま
上へ投げるだけ (不変条件 4)。後始末は保存と印と通告だけを行い、LLM を呼ばない
(不変条件 1)。
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from llm_clients.exceptions import LLMError
from sea.cancellation import ExecutionCancelledException
from sea.runtime_emitters import (
    SAVED_FORM_COMPLETE,
    SAVED_FORM_CUT,
    SAVED_FORM_SPELL_RESULTS,
    SAVED_FORM_SPELL_UNFINISHED,
    SavedUtterance,
    claim_saved_utterance_for_settle,
    last_saved_utterance,
)
from sea.runtime_llm import (
    INTERRUPTED_METADATA_KEY,
    _is_user_interruption,
    _record_interruption_notice,
)

LOGGER = logging.getLogger(__name__)

#: 中断の通告の本文 — 後始末が発言の形から選ぶ 4 通り
#: (docs/intent/reply_stop_exit.md §通告の内容、2026-09-25 まはー裁定)。
#: 通告はペルソナに読ませる文なので、機構が確定を知っている事実だけを書き、
#: 不確定は不確定と書く。指示 (「唱え直してください」等) は書かない。
_NOTICE_BODIES: Dict[str, str] = {
    # ① 発言の途中で切られた (言いかけが残る)
    SAVED_FORM_CUT: "ここで発言が中断されました",
    # ② スペルの実行が終わる前に切られた (結果は来ていない)。
    #    「実行されていません」と断定しない — 副作用がどこまで効いたかは
    #    機構自身にも分からないことがあり、断定するとペルソナが安心して
    #    唱え直して二重に効く。
    SAVED_FORM_SPELL_UNFINISHED: (
        "発言の後に唱えたスペルは、実行が終わる前に中断されました。"
        "結果は届いておらず、どこまで実行されたかは不明です"
    ),
    # ③ スペルの結果を受け取った後、それを受けて話す前に切られた。
    #    書けるのは、受け取り済みの結果が必ず行に残っているときだけ。
    SAVED_FORM_SPELL_RESULTS: "スペルの結果を受け取った後、続きの発言の前に中断されました",
    # ④ 言い切った後、続きが始まる前に切られた (一般の受け皿)
    SAVED_FORM_COMPLETE: "この発言の後、続きが始まる前に中断されました",
}

#: ユーザーの停止の回だけ文頭に挟む原因の句 (2026-08-26/27 裁定の出し分けを
#: 継承)。事故 (LLM エラー・サーバー切断・schedule / auto の割り込み) は無印。
_USER_CAUSE = "ユーザーの操作により、"


def interruption_notice_text(form: Optional[str], *, by_user: bool) -> str:
    """発言の形と原因から、中断の通告の一行を組む。

    形が分からないときは一般の受け皿 (④) に倒す — 証明できない方の文面
    (① の「途中で切れた」や ③ の「結果を受け取った」) を推測で書かない。
    """
    body = _NOTICE_BODIES.get(form or "", _NOTICE_BODIES[SAVED_FORM_COMPLETE])
    return f"({_USER_CAUSE if by_user else ''}{body})"


def _stopped_by_user(
    exc: Optional[BaseException], cancellation_token: Any,
) -> bool:
    """止めたのがユーザーかを、取り消しに刻まれた原因から判定する。

    例外の型では判定しない (schedule / auto の割り込みも同じ取り消しの例外で
    届く)。LLM ノードは取り消しを LLMError に包み直すので、例外の連鎖をたどって
    原因の刻まれた取り消しを探す。見つからなければ、返事の取り消しの札を見る
    (例外なしで閉じた回 — スペル無効のペルソナの途中停止など)。
    """
    seen = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ExecutionCancelledException):
            return _is_user_interruption(current.interrupted_by)
        nxt = getattr(current, "original_error", None)
        if not isinstance(nxt, BaseException):
            nxt = current.__cause__ or current.__context__
        current = nxt
    if cancellation_token is not None:
        try:
            if cancellation_token.is_cancelled():
                return _is_user_interruption(
                    getattr(cancellation_token, "interrupted_by", None),
                )
        except Exception:
            LOGGER.debug("[reply_stop] could not read the cancellation token", exc_info=True)
    return False


def _mark_interrupted(persona: Any, record: SavedUtterance) -> bool:
    """建物の行に「言い切っていない」印を付ける。付いたことを確かめて True。

    既に保存済みの発言の**記憶側の行は書き換えない** — エラーの後片付けが記憶を
    書き換える口になるのは危うい (2026-08-25 裁定)。ペルソナに中断を知らせる
    仕事は、記憶の印ではなく通告が担う。
    """
    row = persona.history_manager.update_building_message(
        record.building_id, record.message_id,
        metadata={INTERRUPTED_METADATA_KEY: True},
    )
    if not isinstance(row, dict):
        return False
    return bool((row.get("metadata") or {}).get(INTERRUPTED_METADATA_KEY))


def _unmark_interrupted(persona: Any, record: SavedUtterance) -> None:
    """付けた印を取り下げる (通告が置けなかった回の巻き戻し。最善努力)。

    印だけが残ると「続きの生成」は押せるのに、会話の末尾に通告が無く、続きの
    プロンプトがモデル発話で終わって Gemini 系が拒む (不変条件 2)。取り下げも
    失敗したら、ログに残すことしかできない (印の書き込みが失敗し続ける回と
    同じ、既知の割り切り)。
    """
    try:
        # DB 層は失敗 (ロック競合の再試行切れ) を中で握って戻ってくるので、
        # 例外の有無ではなく、更新後の行を読み直して倒れたことを確かめる
        # (_mark_interrupted と同じ確かめ方)。
        row = persona.history_manager.update_building_message(
            record.building_id, record.message_id,
            metadata={INTERRUPTED_METADATA_KEY: False},
        )
        withdrawn = isinstance(row, dict) and not (
            (row.get("metadata") or {}).get(INTERRUPTED_METADATA_KEY)
        )
    except Exception:
        withdrawn = False
    if not withdrawn:
        LOGGER.error(
            "[reply_stop] the notice failed AND the mark could not be "
            "withdrawn — a mark without a notice remains (msg=%s building=%s)",
            record.message_id, record.building_id,
        )


def _building_name(runtime: Any, building_id: str) -> Optional[str]:
    try:
        building = runtime.manager.building_map.get(building_id)
    except Exception:
        return None
    name = getattr(building, "name", None) if building is not None else None
    return str(name) if name else None


def _guidance(
    runtime: Any, record: SavedUtterance, reply_building_id: Optional[str],
) -> Dict[str, str]:
    """画面への知らせに載せる案内の材料。

    発言の id は常に載せる。その発言が返事の部屋 (ユーザーが発言を送った部屋)
    と違う部屋にある回だけ、部屋の id と表示名を添える — その部屋の画面でしか
    「続きの生成」を押せないので、画面は部屋の名前つきで案内する。
    """
    guidance = {"interrupted_message_id": record.message_id}
    if reply_building_id and record.building_id != reply_building_id:
        guidance["interrupted_building_id"] = record.building_id
        name = _building_name(runtime, record.building_id)
        if name:
            guidance["interrupted_building_name"] = name
    return guidance


def _stream_cut_info_event(
    persona: Any, stream_error: Dict[str, Any], guidance: Dict[str, str],
) -> Dict[str, Any]:
    """サーバーがストリームを切った回の知らせ (エラー札ではなく情報の知らせ)。"""
    event: Dict[str, Any] = {
        "type": "info",
        # 先頭にアイコンを書かない — 画面側が info 種別に自前で ℹ️ を描く
        # ので、書くと二重に並ぶ (2026-09-13 まはー実機報告)。
        "content": (
            "メッセージの生成が途中で終了しました。"
            f"({stream_error.get('code', 504)} "
            f"{stream_error.get('message', '')})".rstrip()
            + "\nここまでの発言はそのまま残ります。"
        ),
        "persona_id": getattr(persona, "persona_id", None),
    }
    event.update(guidance)
    return event


def settle_reply_stop(
    runtime: Any,
    persona: Any,
    *,
    reply_building_id: Optional[str],
    started_at: float,
    exc: Optional[BaseException] = None,
    cancellation_token: Any = None,
    event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Optional[SavedUtterance]:
    """返事が言い切らずに終わった回の後始末。印と通告を置いた発言を返す。

    呼ぶのは返事の実行の一番外側 (``run_meta_user``) だけで、返事につき一回。

    - ``exc`` あり (エラーで閉じた回): 記録にある最後の発言が対象。
    - ``exc`` なし (例外なしで閉じた回): 最後の発言に「この後で話が止まった」
      が書き足されているときだけ働く (サーバーが締めの生成を切った・スペル
      無効のペルソナが途中で止められた等)。話が続いた回は記録ごと置き換わって
      いるので働かない。

    対象は**返事の実行の開始時刻より後に保存された発言だけ** (``started_at``)。
    別の実行の発言を誤って印付けないための時間窓 — 窓の中に別の実行の発言が
    混ざった場合は最後の一件が対象になるが、それは画面でも実際に最後の吹き出し
    であり、「最後に保存した発言に続きを出す」要件から見て誤りではない
    (docs/intent/reply_stop_exit.md §1)。

    エラーで閉じた回は、例外 (:class:`LLMError`) に案内の材料を載せる —
    一番外側が埋めて、そのまま画面へ出す単方向の属性。サーバーが切った回は
    エラー札ではなく情報の知らせを出し、そこに同じ材料を載せる。

    この関数は例外を投げない (後始末の失敗で、返事を止めた元の例外を
    すり替えない)。
    """
    try:
        return _settle(
            runtime, persona,
            reply_building_id=reply_building_id,
            started_at=started_at,
            exc=exc,
            cancellation_token=cancellation_token,
            event_callback=event_callback,
        )
    except Exception:
        LOGGER.exception(
            "[reply_stop] the settle after a stopped reply failed (persona=%s)",
            getattr(persona, "persona_id", None),
        )
        return None


def _settle(
    runtime: Any,
    persona: Any,
    *,
    reply_building_id: Optional[str],
    started_at: float,
    exc: Optional[BaseException],
    cancellation_token: Any,
    event_callback: Optional[Callable[[Dict[str, Any]], None]],
) -> Optional[SavedUtterance]:
    persona_id = getattr(persona, "persona_id", None)
    record = last_saved_utterance(persona_id)
    if record is None or record.saved_at < started_at:
        # この返事の中では何も保存されていない (一文字も生まれていない回は
        # 印も通告も置かない)。
        return None
    if exc is None and not record.stopped:
        return None
    if not claim_saved_utterance_for_settle(persona_id, record.message_id):
        # 別の実行の後始末が、もうこの発言に印と通告を置いた (要件 5)。
        return None

    by_user = _stopped_by_user(exc, cancellation_token)
    stream_error = (record.detail or {}).get("stream_error")

    # 印と通告は必ず対 (不変条件 2)。印が付かなかった回は通告も置かない —
    # 印だけ・通告だけのどちらも、続きの生成を壊す。
    if not _mark_interrupted(persona, record):
        LOGGER.error(
            "[reply_stop] could not mark the last utterance as unfinished "
            "(persona=%s msg=%s building=%s) — no notice, no continue guidance",
            persona_id, record.message_id, record.building_id,
        )
        if exc is None and stream_error and event_callback:
            # 画面への知らせは保存の成否と無関係に事実なので出す。
            _emit(event_callback, _stream_cut_info_event(persona, stream_error, {}))
        return None

    if not _record_interruption_notice(
        runtime, persona, record.building_id,
        content=interruption_notice_text(record.form, by_user=by_user),
        msg_id=record.message_id,
    ):
        # 印と通告は必ず対 (不変条件 2)。通告が置けなかったら印を取り下げ、
        # 案内も出さない — 印だけの行は「続きの生成」が押せるのに Gemini 系で
        # 必ず失敗する。エラー札は案内なしの素の文面で出る (再送は門番が
        # 断るので立てない — 画面側は interrupted_message_id が無い回に
        # landedMessageId で再送を立てるが、それは既存の門番の検査に当たって
        # 断られる。まれな DB 障害の回の割り切り)。
        LOGGER.error(
            "[reply_stop] could not write the interruption notice "
            "(persona=%s msg=%s building=%s) — withdrawing the mark, "
            "no continue guidance",
            persona_id, record.message_id, record.building_id,
        )
        _unmark_interrupted(persona, record)
        if exc is None and stream_error and event_callback:
            _emit(event_callback, _stream_cut_info_event(persona, stream_error, {}))
        return None
    LOGGER.info(
        "[reply_stop] settled a stopped reply (persona=%s msg=%s building=%s "
        "form=%s by_user=%s cause=%s)",
        persona_id, record.message_id, record.building_id, record.form,
        by_user, type(exc).__name__ if exc is not None else "none",
    )

    guidance = _guidance(runtime, record, reply_building_id)
    if isinstance(exc, LLMError):
        exc.interrupted_message_id = guidance.get("interrupted_message_id")
        exc.interrupted_building_id = guidance.get("interrupted_building_id")
        exc.interrupted_building_name = guidance.get("interrupted_building_name")
    elif exc is None and stream_error and event_callback:
        _emit(event_callback, _stream_cut_info_event(persona, stream_error, guidance))
    return record


def _emit(
    event_callback: Callable[[Dict[str, Any]], None], event: Dict[str, Any],
) -> None:
    try:
        event_callback(event)
    except Exception:
        LOGGER.warning("[reply_stop] could not deliver the notice to the UI", exc_info=True)


__all__ = [
    "interruption_notice_text",
    "settle_reply_stop",
]
