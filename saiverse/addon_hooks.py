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
   並列 submit。 同じ event を複数ハンドラに配る順序、 異なるハンドラ間の
   完了順は未定義。

4. **同一 ``order_key`` の dispatch は FIFO で 1 ハンドラへ流す**
   ``dispatch_hook(..., order_key=K)`` で呼ぶと、 同じ ``K`` の dispatch は
   登録ハンドラごとに登録順で 1 つずつ実行される (= 1 ハンドラ目の前回が
   完了するまで次の dispatch のそのハンドラ呼び出しは走らない)。 異なる
   ``order_key`` の dispatch は独立に並列実行されるので、 他 message を
   ブロックしない。 Pipeline Streaming sub_seq の順序保証 (= voice_tts
   intent doc 不変条件 2) はこの機構に依存する。 ``order_key=None`` (デフォ
   ルト) なら従来の並列挙動。

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
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

LOGGER = logging.getLogger(__name__)

# Phase 1 で許可するイベント名。新規イベント追加時はここに登録する。
#
# - ``persona_speak``: ペルソナ発話時。 voice-tts / stackchan vessel など、
#   発話を物理出力に橋渡しするアドオン向け。
# - ``persona_entered_building`` / ``persona_exited_building``: ペルソナの
#   建物入退室時 (= AI 移動)。 Vessel Building に憑依したタイミングで
#   avatar セットを動的ロードし、 退室時に物理身体を非表示状態に戻す等。
#   両イベントとも payload: ``persona_id``, ``building_id``, ``from_building_id``。
#   入室時の ``building_id`` は入った先、 退室時の ``building_id`` は出た元
#   (= ペルソナの居場所が「どこに居たか / どこに居るようになったか」と読める向き)。
KNOWN_EVENTS: frozenset = frozenset(
    {
        "persona_speak",
        "persona_entered_building",
        "persona_exited_building",
    }
)

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="addon-hook")
_handlers: Dict[str, List[Callable[..., Any]]] = {}
_lock = threading.Lock()

# 同一 ``order_key`` の dispatch を FIFO で直列化するための「次に走らせる
# べきタスクが待ってる Future」 を (event, order_key, handler) ごとに記憶する。
# 新しい dispatch が来た時、 ここから 「直前タスクの Future」 を取って、
# それを待ってから自分のハンドラ呼び出しを走らせる Future を新規 submit
# する。 これにより同 key 内の登録順 = 実行順が保証される。 ハンドラ間は
# 独立に直列化され、 異なる key も独立に並列。
#
# leak 抑制: 古いエントリは新しい Future で上書きされるので、 アクティブな
# key 数だけ常に dict サイズが保たれる。 message_id が大量に増え続ける状況
# (例: 長時間稼働で 10 万 message) でも、 dict のエントリはハンドラ数 ×
# active key 数で抑えられる (完了した Future への参照は他から保持されてない
# ので GC される)。 完了した Future のエントリは ``_on_chain_done`` が消す。
_chain_state: Dict[Tuple[str, str, int], Future] = {}
_chain_lock = threading.Lock()


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


def dispatch_hook(
    event: str,
    *,
    order_key: Optional[str] = None,
    **payload: Any,
) -> None:
    """登録済みのハンドラへ submit する。

    ハンドラはバックグラウンドスレッドで実行され、本関数は即座に return する。
    ハンドラ例外は ``_safe_invoke`` で握り潰される。

    ``order_key`` が指定された場合、 同じ ``order_key`` の以前の dispatch が
    そのハンドラに対して完了するまで待ってから自身のハンドラ呼び出しを走らせる
    (= ハンドラごとに FIFO 直列化)。 異なる ``order_key`` の dispatch は独立
    に並列。 ``order_key=None`` なら従来の並列挙動。

    Pipeline Streaming の sub_seq 順序保証 (= 同 message_id への sub-speak
    が emit 順で voice-tts addon に届く) に使う。 ``order_key=message_id``。
    """
    with _lock:
        handlers = list(_handlers.get(event, ()))
    if not handlers:
        return
    for handler in handlers:
        try:
            if order_key is None:
                _executor.submit(_safe_invoke, handler, payload)
            else:
                _submit_chained(event, order_key, handler, payload)
        except RuntimeError:
            # Executor がシャットダウン済み (プロセス終了時等)。
            # ログだけ残して捨てる。
            LOGGER.warning(
                "addon_hooks: executor shutdown, dropping event=%r handler=%s.%s",
                event,
                getattr(handler, "__module__", "?"),
                getattr(handler, "__name__", "?"),
            )


def _submit_chained(
    event: str,
    order_key: str,
    handler: Callable[..., Any],
    payload: Dict[str, Any],
) -> None:
    """同 ``(event, order_key, handler)`` 内で前回完了を待ってから走る Future
    を ``_executor`` に submit して、 _chain_state[key] を新しい Future に
    差し替える。 「直前 Future の完了 → 自分の handler 実行」 が 1 Future の
    中で連結されているので、 提出順 = 実行開始順が保証される。
    """
    chain_key = (event, order_key, id(handler))
    with _chain_lock:
        prev_future = _chain_state.get(chain_key)
        new_future = _executor.submit(
            _chained_invoke, handler, payload, prev_future,
        )
        _chain_state[chain_key] = new_future
    # add_done_callback は _chain_lock の外で呼ぶこと。 Future が既に完了して
    # いると callback はこのスレッドで即座に同期実行され、 _on_chain_done が
    # _chain_lock を取りに行く。 ロックの中で登録すると、 submit した仕事が
    # ここに到達する前に終わった並びで自分自身を待つ (2026-08-23 にフル
    # スイートで実際に踏んだ自己デッドロック)。
    new_future.add_done_callback(
        lambda fut, k=chain_key: _on_chain_done(k, fut)
    )


def _chained_invoke(
    handler: Callable[..., Any],
    payload: Dict[str, Any],
    prev_future: Optional[Future],
) -> None:
    """直前 Future の完了を待ってから handler を実行する。

    直前で例外が出ても自身の実行は妨げない (= 順序保証は維持するが、 前回の
    失敗で次の発話が消えると困るので吸収して進める)。
    """
    if prev_future is not None:
        try:
            prev_future.result()
        except Exception:
            pass  # 前回の失敗は _safe_invoke 側で既にログ済
    _safe_invoke(handler, payload)


def _on_chain_done(chain_key: Tuple[str, str, int], future: Future) -> None:
    """Future 完了時、 ``_chain_state[chain_key]`` がまだ自分を指していれば
    エントリを削除して dict leak を防ぐ。 既に新しい Future に差し替わって
    いれば noop (= 後続 dispatch の chain を壊さない)。
    """
    with _chain_lock:
        if _chain_state.get(chain_key) is future:
            del _chain_state[chain_key]


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
