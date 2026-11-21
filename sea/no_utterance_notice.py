"""エラーで発言が生まれなかった回を、部屋の記録に一行置く。

設計: docs/intent/no_utterance_error_notice.md

- 本文はペルソナに読ませる文で、失敗したという事実だけを書く
  (「(エラーにより、{名前}は発言できませんでした)」)。エラーの種類は書かない
  (まはーが決めたこと 3)。ユーザー自身の操作が原因のサーバーの停止だけは、
  原因を本文に含める (問い 2)。
- 原因の詳細 (エラーの種類・利用者向けの文・プロバイダーの返したエラーの原文・
  モデル名) は、行の ``metadata`` の :data:`NO_UTTERANCE_DETAIL_KEY` に置く。
  ``__`` 始まりの鍵はサーバーだけが書ける領域 (docs/intent/system_notice_delivery.md
  不変条件 4) で、取り込み (builtin_data/tools/get_building_messages.py) は本文
  しか記憶へ写さない。画面はチャット履歴の API (api/routes/chat.py の
  ``ChatMessage.failure_detail``) 経由でこれを見せる。
- 行は部屋に居合わせた全員に届ける (問い 0)。書き手は sea/host_notice.py。
- 書く部屋が決まらない失敗 (ペルソナが居ない・現在地が無い) は、部屋の記録の
  代わりに画面右下の閉じるまで残る知らせ (saiverse/runtime_alerts.py) に出す
  (問い 3) — :func:`post_unrunnable_alert`。

実行が始まった後の確認は ``sea/pulse_controller.py`` の ``_execute_unlocked`` に
一つだけ置く。例外を出さずに終わる二つの失敗 (再試行しても応答が空だった /
指定された Playbook が見つからなかった) は ``runtime_outcome`` を変えず
(schedule の台帳の分類を動かさないため)、:func:`note_no_utterance_cause` で
その実行の取り消しの札 (``CancellationToken``) に結びつけて記録する。札は
request ごとに一枚で、Playbook の state (``_cancellation_token``) を通って LLM
ノードまで届くので、実行を取り違えない。
"""
from __future__ import annotations

import logging
import threading
import weakref
from typing import Any, Dict, Optional

from sea.host_notice import record_host_notice

LOGGER = logging.getLogger(__name__)

#: 行の metadata に置く、詳細の鍵 (サーバーだけが書ける ``__`` 始まり)。
NO_UTTERANCE_DETAIL_KEY = "__no_utterance_error__"

#: 詳細に残すエラーの原文の上限 (プロバイダーの返す本文は長いことがある)。
_TECHNICAL_DETAIL_LIMIT = 2000

#: 詳細の error_code — 例外の型から取れない、この機構が自分で見分ける失敗。
CODE_EMPTY_RESPONSE = "empty_response"
CODE_PLAYBOOK_NOT_FOUND = "playbook_not_found"
CODE_BEAT_GATE_CLOSED = "beat_gate_closed"
CODE_WINDOW_FLOOR_UNMET = "window_floor_unmet"
CODE_QUEUE_OVERFLOW = "queue_overflow"
CODE_SERVER_SHUTDOWN = "server_shutdown"


def error_notice_text(persona_name: str) -> str:
    """エラーで発言できなかった回の本文。"""
    return f"(エラーにより、{persona_name}は発言できませんでした)"


def shutdown_notice_text(persona_name: str) -> str:
    """サーバーの停止で発言できなかった回の本文。"""
    return f"(サーバーの停止により、{persona_name}は発言できませんでした)"


def _clip(text: str) -> str:
    if len(text) <= _TECHNICAL_DETAIL_LIMIT:
        return text
    return text[:_TECHNICAL_DETAIL_LIMIT] + "…"


def failure_detail(
    error_code: str,
    *,
    message: Any = None,
    technical_detail: Optional[str] = None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """行の metadata に置く詳細を組む。

    ``message`` は利用者向けの文。``LLMError`` の ``user_message`` (日本語の文) か、
    この機構の文 (``api.ui_messages.ui_message`` の形 — 画面の言語に合わせて出る)。
    """
    detail: Dict[str, Any] = {"error_code": str(error_code or "unknown")}
    if message:
        detail["message"] = message
    if technical_detail:
        detail["technical_detail"] = _clip(str(technical_detail))
    if model:
        detail["model"] = str(model)
    return detail


def failure_detail_from_exception(exc: BaseException) -> Dict[str, Any]:
    """実行を止めた例外から詳細を組む。

    ``LLMError`` の系統だけが利用者向けの文 (``user_message``) を持つ。エラーの
    原文は、包む前の例外 (``original_error`` — プロバイダーの返したエラー) を優先する。
    """
    from llm_clients.exceptions import LLMError

    if isinstance(exc, LLMError):
        original = getattr(exc, "original_error", None)
        raw = str(original) if original is not None and str(original) else str(exc)
        return failure_detail(
            exc.error_code,
            message=exc.user_message,
            technical_detail=raw,
            model=getattr(exc, "model", None),
        )
    return failure_detail(
        type(exc).__name__,
        technical_detail=str(exc) or type(exc).__name__,
    )


# ---------------------------------------------------------------------------
# 例外を出さずに終わる失敗の記録 — 実行 (取り消しの札) ごと
# ---------------------------------------------------------------------------

_CAUSES: "weakref.WeakKeyDictionary[Any, Dict[str, Any]]" = weakref.WeakKeyDictionary()
_CAUSES_LOCK = threading.Lock()


def note_no_utterance_cause(cancellation_token: Any, detail: Dict[str, Any]) -> None:
    """例外を出さずに終わる失敗を、その実行の札に結びつけて記録する。

    同じ実行で二度目が来ても最初の一件を残す (最初の失敗が発言を止めた原因)。
    札が無い呼び出し (PulseController を経ない直接の呼び出し) は記録しない —
    確認する側 (``_execute_unlocked``) がいない。
    """
    if cancellation_token is None:
        LOGGER.debug("[no_utterance] no cancellation token; cause not recorded: %s", detail)
        return
    try:
        with _CAUSES_LOCK:
            if cancellation_token not in _CAUSES:
                _CAUSES[cancellation_token] = dict(detail)
    except TypeError:
        # 弱参照を持てない札 (テストの代用品など)。記録は諦める。
        LOGGER.debug("[no_utterance] token is not weak-referenceable; cause not recorded")


def take_no_utterance_cause(cancellation_token: Any) -> Optional[Dict[str, Any]]:
    """その実行に記録された失敗を取り出す (取り出したら消える)。"""
    if cancellation_token is None:
        return None
    try:
        with _CAUSES_LOCK:
            return _CAUSES.pop(cancellation_token, None)
    except TypeError:
        return None


# ---------------------------------------------------------------------------
# 書き込み
# ---------------------------------------------------------------------------


def record_no_utterance_notice(
    manager: Any,
    persona: Any,
    building_id: str,
    *,
    shutdown: bool = False,
    detail: Optional[Dict[str, Any]] = None,
) -> bool:
    """発言できなかったことを、その部屋の記録へ一行置く。置けたら True。

    書き込みの失敗は例外にしない (record_host_notice と同じ)。
    """
    if not building_id:
        LOGGER.warning(
            "[no_utterance] no building to record the notice in (persona=%s)",
            getattr(persona, "persona_id", None),
        )
        return False
    persona_id = getattr(persona, "persona_id", None)
    name = getattr(persona, "persona_name", None) or persona_id or "?"
    content = shutdown_notice_text(name) if shutdown else error_notice_text(name)
    if detail is None and shutdown:
        detail = failure_detail(CODE_SERVER_SHUTDOWN)
    metadata = {NO_UTTERANCE_DETAIL_KEY: detail} if detail else None
    written = record_host_notice(
        manager, persona, building_id,
        content=content, metadata=metadata, ref=f"no_utterance:{persona_id}",
    )
    if written:
        LOGGER.info(
            "[no_utterance] recorded that %s could not speak (building=%s cause=%s)",
            persona_id, building_id,
            (detail or {}).get("error_code") if detail else "none",
        )
    return written


# ---------------------------------------------------------------------------
# 書く部屋が決まらない失敗 — 画面右下の閉じるまで残る知らせ (問い 3)
# ---------------------------------------------------------------------------

#: 書く部屋が決まらない原因。
CAUSE_PERSONA_NOT_FOUND = "persona_not_found"
CAUSE_NO_LOCATION = "no_location"
#: 会話を始める関数 (manager.run_sea_user) が無い。user の経路だけ。
CAUSE_NO_RUNNER = "no_runner"

#: 経路ごとの見出しのキー。値を足すときは frontend/src/i18n/messages.json にも
#: 文言を足す。runtime_alerts の行は閉じるまで残るので、キーの名前は後から変えない。
_UNRUNNABLE_TITLES = {
    "schedule": "api.runtimeAlerts.scheduleUnrunnable.title",
    "event": "api.runtimeAlerts.eventUnrunnable.title",
    "conversation": "api.runtimeAlerts.conversationUnrunnable.title",
}
_UNRUNNABLE_MESSAGES = {
    ("schedule", CAUSE_PERSONA_NOT_FOUND): "api.runtimeAlerts.scheduleUnrunnable.personaNotFound",
    ("schedule", CAUSE_NO_LOCATION): "api.runtimeAlerts.scheduleUnrunnable.noLocation",
    ("event", CAUSE_PERSONA_NOT_FOUND): "api.runtimeAlerts.eventUnrunnable.personaNotFound",
    ("event", CAUSE_NO_LOCATION): "api.runtimeAlerts.eventUnrunnable.noLocation",
    ("conversation", CAUSE_PERSONA_NOT_FOUND): "api.runtimeAlerts.conversationUnrunnable.personaNotFound",
    ("conversation", CAUSE_NO_LOCATION): "api.runtimeAlerts.conversationUnrunnable.noLocation",
    ("conversation", CAUSE_NO_RUNNER): "api.runtimeAlerts.conversationUnrunnable.noRunner",
}


def persona_label(manager: Any, persona_id: Optional[str]) -> str:
    """知らせに出すペルソナの呼び名。見つからなければ id そのもの。"""
    if not persona_id:
        return "?"
    try:
        personas = getattr(manager, "all_personas", None) or {}
        persona = personas.get(persona_id)
        name = getattr(persona, "persona_name", None) if persona is not None else None
        if name:
            return str(name)
        names = getattr(manager, "id_to_name_map", None) or {}
        if names.get(persona_id):
            return str(names[persona_id])
    except Exception:
        LOGGER.debug("[no_utterance] could not resolve the persona name", exc_info=True)
    return str(persona_id)


def post_unrunnable_alert(
    scope: str,
    subject_id: Any,
    cause: str,
    *,
    persona: str,
    schedule: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> bool:
    """書く部屋が決まらず、ペルソナを動かせなかったことを画面右下に知らせる。

    ``scope`` は ``"schedule"`` / ``"event"`` / ``"conversation"``。識別キーは
    ``<scope>_unrunnable:<subject_id>:<cause>`` (docs/intent/runtime_alerts.md の
    「何が・どの対象で・どんな原因で」)。同じキーでまだ閉じていない知らせは一枚に
    まとまり、回数が増える。呼ぶ側は、諦めた一回につき一度だけ呼ぶ。

    知らせの失敗で呼び出し元を止めない (例外を投げない)。保存できたら True。
    """
    title_key = _UNRUNNABLE_TITLES.get(scope)
    message_key = _UNRUNNABLE_MESSAGES.get((scope, cause))
    if title_key is None or message_key is None:
        LOGGER.error("[no_utterance] unknown unrunnable alert (scope=%s cause=%s)", scope, cause)
        return False
    try:
        from api.ui_messages import ui_message
        from saiverse.runtime_alerts import post_runtime_alert

        params: Dict[str, Any] = {"persona": persona}
        if schedule is not None:
            params["schedule"] = schedule
        stored = post_runtime_alert(
            f"{scope}_unrunnable:{subject_id}:{cause}",
            "error",
            ui_message(title_key),
            ui_message(message_key, **params),
            details=details,
        )
        return stored is not None
    except Exception:  # noqa: BLE001 — 知らせの失敗で呼び出し元の処理を止めない
        LOGGER.warning(
            "[no_utterance] could not post the unrunnable alert (scope=%s subject=%s cause=%s)",
            scope, subject_id, cause, exc_info=True,
        )
        return False


__all__ = [
    "CAUSE_NO_LOCATION",
    "CAUSE_NO_RUNNER",
    "CAUSE_PERSONA_NOT_FOUND",
    "NO_UTTERANCE_DETAIL_KEY",
    "CODE_BEAT_GATE_CLOSED",
    "CODE_EMPTY_RESPONSE",
    "CODE_PLAYBOOK_NOT_FOUND",
    "CODE_QUEUE_OVERFLOW",
    "CODE_SERVER_SHUTDOWN",
    "CODE_WINDOW_FLOOR_UNMET",
    "error_notice_text",
    "failure_detail",
    "failure_detail_from_exception",
    "note_no_utterance_cause",
    "persona_label",
    "post_unrunnable_alert",
    "record_no_utterance_notice",
    "shutdown_notice_text",
    "take_no_utterance_cause",
]
