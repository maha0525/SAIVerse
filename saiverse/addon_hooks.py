"""アドオン向けサーバー側 hook ディスパッチャ。

本体内部イベント (現状は ``persona_speak`` のみ) を、宣言的に登録された
アドオンの Python 関数へ通知する。

設計 / 不変条件は ``docs/intent/addon_speak_hooks.md`` を参照。

主要な不変条件:

1. **本体スレッドに干渉しない**
   ハンドラは ``ThreadPoolExecutor`` (max_workers=4) に submit して
   fire-and-forget。発火元 (発話処理スレッド) は即座に次へ進む。

2. **ハンドラ例外は隔離する**
   ハンドラが投げた例外は WARNING ログに記録し、他のハンドラに伝播しない。
   1 つのアドオンの不具合が他アドオン・本体を巻き込まない。

3. **複数ハンドラの順序は保証しない**
   並列 submit。順序が必要な処理は 1 ハンドラ内で順次実行すること。

使い方 (本体側):

    from saiverse.addon_hooks import dispatch_hook
    dispatch_hook(
        "persona_speak",
        persona_id="air_city_a",
        building_id="b1",
        text_raw="こんにちは <in_heart>...</in_heart>",
        text_for_voice="こんにちは",
        message_id="msg-123",
        pulse_id="p-456",
        source="speak",
        metadata={"tags": ["conversation"]},
    )

使い方 (アドオン側、``addon.json`` 経由で自動登録):

    # expansion_data/<addon>/speak_hook.py
    def on_persona_speak(persona_id, text_for_voice, message_id, **kwargs):
        # 重い処理は禁止 — 自前で Queue / Thread に投入すること
        my_queue.put((persona_id, text_for_voice, message_id))
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List

LOGGER = logging.getLogger(__name__)

# Phase 1 で許可するイベント名。新規イベント追加時はここに登録する。
KNOWN_EVENTS: frozenset = frozenset({"persona_speak"})

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="addon-hook")
_handlers: Dict[str, List[Callable[..., Any]]] = {}
_lock = threading.Lock()


def register_hook(event: str, handler: Callable[..., Any]) -> None:
    """イベントへハンドラを登録する。

    同一ハンドラを 2 回登録すると 2 回呼ばれる (重複排除しない)。
    アドオンライフサイクル (有効化/無効化) は addon_loader 側で管理する。
    """
    if event not in KNOWN_EVENTS:
        LOGGER.warning(
            "addon_hooks: unknown event %r registered (handler=%s.%s). "
            "Allowed events: %s",
            event,
            getattr(handler, "__module__", "?"),
            getattr(handler, "__name__", "?"),
            sorted(KNOWN_EVENTS),
        )
    with _lock:
        _handlers.setdefault(event, []).append(handler)
    LOGGER.info(
        "addon_hooks: registered handler %s.%s for event %r",
        getattr(handler, "__module__", "?"),
        getattr(handler, "__name__", "?"),
        event,
    )


def unregister_hook(event: str, handler: Callable[..., Any]) -> bool:
    """イベントからハンドラを 1 件解除する。

    Returns:
        解除に成功すれば True、見つからなければ False。
    """
    with _lock:
        handlers = _handlers.get(event)
        if not handlers:
            return False
        try:
            handlers.remove(handler)
        except ValueError:
            return False
        if not handlers:
            _handlers.pop(event, None)
    LOGGER.info(
        "addon_hooks: unregistered handler %s.%s for event %r",
        getattr(handler, "__module__", "?"),
        getattr(handler, "__name__", "?"),
        event,
    )
    return True


def dispatch_hook(event: str, **payload: Any) -> None:
    """登録済みのハンドラ全てへ並列 submit する。

    ハンドラはバックグラウンドスレッドで実行され、本関数は即座に return する。
    ハンドラ例外は ``_safe_invoke`` で握り潰される。
    """
    with _lock:
        handlers = list(_handlers.get(event, ()))
    if not handlers:
        return
    for handler in handlers:
        try:
            _executor.submit(_safe_invoke, handler, payload)
        except RuntimeError:
            # Executor がシャットダウン済み (プロセス終了時等)。
            # ログだけ残して捨てる。
            LOGGER.warning(
                "addon_hooks: executor shutdown, dropping event=%r handler=%s.%s",
                event,
                getattr(handler, "__module__", "?"),
                getattr(handler, "__name__", "?"),
            )


def _safe_invoke(handler: Callable[..., Any], payload: Dict[str, Any]) -> None:
    """ハンドラ呼び出しを例外保護でラップする。"""
    try:
        handler(**payload)
    except Exception:
        LOGGER.warning(
            "addon_hooks: handler failed: %s.%s",
            getattr(handler, "__module__", "?"),
            getattr(handler, "__name__", "?"),
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Test / introspection helpers
# ---------------------------------------------------------------------------

def _registered_handlers(event: str) -> List[Callable[..., Any]]:
    """テスト用: 登録済みハンドラのスナップショットを返す。"""
    with _lock:
        return list(_handlers.get(event, ()))


def _clear_all_handlers() -> None:
    """テスト用: 全ハンドラを解除する。"""
    with _lock:
        _handlers.clear()
