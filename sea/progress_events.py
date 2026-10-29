"""チャット画面の「いま何をしているか」へ送る進み具合 (status イベントの ``phase``)。

設計の正典: docs/intent/chat_progress_display.md

画面の文言はフロント (frontend/src/lib/chatProgress.ts) だけが持つ。ここが送るのは
段階の名前と、文言に差し込む固有名 (接続先の表示名・スペルの表示名) だけ。
Playbook やノードの名前は利用者には意味が無いので、段階の表示には使わない
(各ノードが出す ``"<playbook> / <node>"`` の status は開発用の足跡として残り、
フロントはそれでは表示を変えない)。

進み具合の表示は返事そのものではない。送れなくても返事の生成は止めない。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable, Dict, Optional

LOGGER = logging.getLogger(__name__)

#: 返事に使う文脈 (履歴・記憶・head) を組み立てて、LLM に送るデータを作っている。
PHASE_BUILDING_REQUEST = "building_request"
#: LLM に送り終え、最初の応答を待っている。
PHASE_AWAITING_RESPONSE = "awaiting_response"
#: ペルソナが唱えたスペルを実行している。
PHASE_RUNNING_TOOL = "running_tool"

# 表示名の末尾の補足 ("LM Studio (local)" の "(local)" など) と、「サーバー」と
# 重なる語。画面では「<名前>サーバー」と続けるので落とす。
_TRAILING_NOTE = re.compile(r"\s*[（(][^()（）]*[)）]\s*$")
_TRAILING_SERVER = re.compile(r"\s+server$", re.IGNORECASE)


def provider_label(model_key: Optional[str]) -> Optional[str]:
    """model の接続先を、画面の「<名前>サーバー」に入れる短い名前で返す。

    分からない (model の設定が無い・接続先の定義が無い) ときは None。画面は
    「AI サーバー」と出す。
    """
    if not model_key:
        return None
    try:
        from saiverse.model_configs import get_provider_for_model
        from saiverse.provider_configs import get_provider

        provider_id = get_provider_for_model(model_key)
        provider = get_provider(provider_id) if provider_id else None
        name = (provider or {}).get("display_name") or ""
    except Exception:
        LOGGER.debug("[progress] could not resolve provider for model=%s", model_key, exc_info=True)
        return None
    name = _TRAILING_SERVER.sub("", _TRAILING_NOTE.sub("", str(name).strip())).strip()
    return name or None


def emit_progress(
    event_callback: Optional[Callable[[Dict[str, Any]], None]],
    phase: str,
    *,
    persona: Any = None,
    **params: Any,
) -> None:
    """段階を画面へ知らせる。値の無い引数は載せない。"""
    if not event_callback:
        return
    event: Dict[str, Any] = {"type": "status", "phase": phase}
    persona_id = getattr(persona, "persona_id", None)
    if persona_id:
        # 送り手 (manager/runtime.py の _enrich_event) が persona_name を足す
        event["persona_id"] = persona_id
    event.update({key: value for key, value in params.items() if value})
    try:
        event_callback(event)
    except Exception:
        LOGGER.warning("[progress] failed to emit phase=%s", phase, exc_info=True)


def emit_awaiting_response(
    event_callback: Optional[Callable[[Dict[str, Any]], None]],
    llm_client: Any,
    persona: Any = None,
) -> None:
    """LLM へ送る直前に呼ぶ。接続先は実際に送る client の model から引く。"""
    if not event_callback:
        return
    emit_progress(
        event_callback,
        PHASE_AWAITING_RESPONSE,
        persona=persona,
        provider=provider_label(getattr(llm_client, "config_key", None)),
    )
