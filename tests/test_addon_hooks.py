"""Tests for ``saiverse.addon_hooks``.

検証観点:
- 登録したハンドラが ``dispatch_hook`` で呼ばれる
- ハンドラ例外が他のハンドラを巻き込まない (隔離)
- 複数ハンドラが (順序非保証で) すべて呼ばれる
- ``unregister_hook`` でハンドラが解除される
- ``KNOWN_EVENTS`` 外のイベント名でも warning 付きで登録できる
"""
from __future__ import annotations

import threading
import time
import unittest
from typing import Any, Dict, List

from saiverse import addon_hooks


class AddonHooksTests(unittest.TestCase):
    def setUp(self) -> None:
        addon_hooks._clear_all_handlers()

    def tearDown(self) -> None:
        addon_hooks._clear_all_handlers()

    # ------------------------------------------------------------------
    # 基本動作
    # ------------------------------------------------------------------

    def test_register_and_dispatch_calls_handler(self) -> None:
        called: Dict[str, Any] = {}
        done = threading.Event()

        def handler(**payload: Any) -> None:
            called.update(payload)
            done.set()

        addon_hooks.register_hook("persona_speak", handler)
        addon_hooks.dispatch_hook(
            "persona_speak",
            persona_id="air",
            text_for_voice="hi",
            message_id="m1",
        )

        self.assertTrue(done.wait(timeout=2.0), "handler not called within 2s")
        self.assertEqual(called["persona_id"], "air")
        self.assertEqual(called["text_for_voice"], "hi")
        self.assertEqual(called["message_id"], "m1")

    def test_dispatch_with_no_handlers_is_noop(self) -> None:
        # 例外が出ないことを確認
        addon_hooks.dispatch_hook("persona_speak", foo="bar")

    def test_unregister_removes_handler(self) -> None:
        calls: List[Dict[str, Any]] = []
        done = threading.Event()

        def handler(**payload: Any) -> None:
            calls.append(payload)
            done.set()

        addon_hooks.register_hook("persona_speak", handler)
        self.assertTrue(addon_hooks.unregister_hook("persona_speak", handler))

        addon_hooks.dispatch_hook("persona_speak", x=1)
        # 解除済みなので呼ばれない
        self.assertFalse(done.wait(timeout=0.3))
        self.assertEqual(calls, [])

    def test_unregister_unknown_handler_returns_false(self) -> None:
        def handler(**_payload: Any) -> None:
            pass

        # 未登録 → False
        self.assertFalse(addon_hooks.unregister_hook("persona_speak", handler))

        addon_hooks.register_hook("persona_speak", handler)
        addon_hooks.unregister_hook("persona_speak", handler)
        # 二度目の解除 → False
        self.assertFalse(addon_hooks.unregister_hook("persona_speak", handler))

    # ------------------------------------------------------------------
    # 隔離 / 並列実行
    # ------------------------------------------------------------------

    def test_handler_exception_does_not_break_other_handlers(self) -> None:
        bad_called = threading.Event()
        good_called = threading.Event()

        def bad_handler(**_payload: Any) -> None:
            bad_called.set()
            raise RuntimeError("intentional failure")

        def good_handler(**_payload: Any) -> None:
            good_called.set()

        addon_hooks.register_hook("persona_speak", bad_handler)
        addon_hooks.register_hook("persona_speak", good_handler)

        addon_hooks.dispatch_hook("persona_speak", x=1)

        self.assertTrue(bad_called.wait(timeout=2.0))
        self.assertTrue(
            good_called.wait(timeout=2.0),
            "good handler must run despite bad handler raising",
        )

    def test_multiple_handlers_all_called(self) -> None:
        n_handlers = 5
        latch_calls: List[str] = []
        latch_lock = threading.Lock()
        all_done = threading.Event()

        def make_handler(label: str):
            def _handler(**_payload: Any) -> None:
                with latch_lock:
                    latch_calls.append(label)
                    if len(latch_calls) >= n_handlers:
                        all_done.set()
            return _handler

        for i in range(n_handlers):
            addon_hooks.register_hook("persona_speak", make_handler(f"h{i}"))

        addon_hooks.dispatch_hook("persona_speak", x=1)

        self.assertTrue(all_done.wait(timeout=2.0))
        self.assertEqual(sorted(latch_calls), [f"h{i}" for i in range(n_handlers)])

    def test_dispatch_does_not_block_caller(self) -> None:
        """重いハンドラがあっても dispatch_hook は即座に return する。"""
        slow_finished = threading.Event()

        def slow_handler(**_payload: Any) -> None:
            time.sleep(0.5)
            slow_finished.set()

        addon_hooks.register_hook("persona_speak", slow_handler)

        t0 = time.monotonic()
        addon_hooks.dispatch_hook("persona_speak", x=1)
        elapsed = time.monotonic() - t0

        # dispatch_hook は ThreadPoolExecutor に submit するだけなので
        # ハンドラの 0.5 秒 sleep を待たず即座に return する
        self.assertLess(elapsed, 0.2, f"dispatch took {elapsed:.3f}s, expected <0.2s")
        self.assertTrue(slow_finished.wait(timeout=2.0))

    # ------------------------------------------------------------------
    # 不明イベント
    # ------------------------------------------------------------------

    def test_unknown_event_still_registers(self) -> None:
        """KNOWN_EVENTS 外のイベント名でも (warning 付きで) 登録できる。"""
        called = threading.Event()

        def handler(**_payload: Any) -> None:
            called.set()

        # warning ログが出るが登録は成功する想定
        addon_hooks.register_hook("future_event", handler)
        addon_hooks.dispatch_hook("future_event", x=1)

        self.assertTrue(called.wait(timeout=2.0))


if __name__ == "__main__":
    unittest.main()
