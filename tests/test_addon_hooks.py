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

    # ------------------------------------------------------------------
    # order_key (Pipeline Streaming sub_seq 順序保証用)
    # ------------------------------------------------------------------

    def test_order_key_preserves_dispatch_order_within_key(self) -> None:
        """同じ order_key の dispatch は提出順に直列実行される。

        sub_seq 順序保証 (= voice_tts_pipeline_streaming intent doc 不変条件 2)
        の根本機構。 ThreadPoolExecutor の並列 pick-up で順序が崩れると、
        voice-tts addon の `enqueue_tts` が逆順に呼ばれて発話が入れ替わる
        (2026-05-16 観測のチャンク並び替え事故の原因)。
        """
        observed_order: List[int] = []
        observed_lock = threading.Lock()
        all_done = threading.Event()
        n = 20

        def handler(*, seq: int, **_p: Any) -> None:
            # 早く来た方が必ず先に append するため、 sleep で latency を
            # ばらつかせて 「FIFO 直列」 を信頼できる形で検証する。 並列
            # ピックアップだと seq=0 が一番遅く完了する状況を作る。
            time.sleep(0.005 * (n - seq))
            with observed_lock:
                observed_order.append(seq)
                if len(observed_order) >= n:
                    all_done.set()

        addon_hooks.register_hook("persona_speak", handler)

        for i in range(n):
            addon_hooks.dispatch_hook(
                "persona_speak",
                order_key="msg-1",
                seq=i,
            )

        self.assertTrue(all_done.wait(timeout=10.0))
        self.assertEqual(observed_order, list(range(n)))

    def test_order_key_different_keys_run_in_parallel(self) -> None:
        """異なる order_key の dispatch は互いをブロックしない。"""
        start_lock = threading.Lock()
        started: List[str] = []
        release = threading.Event()

        def handler(*, key_label: str, **_p: Any) -> None:
            with start_lock:
                started.append(key_label)
            release.wait(timeout=2.0)

        addon_hooks.register_hook("persona_speak", handler)

        addon_hooks.dispatch_hook("persona_speak", order_key="a", key_label="a")
        addon_hooks.dispatch_hook("persona_speak", order_key="b", key_label="b")

        # a と b は別 key なので並行して start に到達する
        t0 = time.monotonic()
        while time.monotonic() - t0 < 2.0:
            with start_lock:
                if len(started) >= 2:
                    break
            time.sleep(0.01)

        with start_lock:
            self.assertEqual(sorted(started), ["a", "b"])

        release.set()

    def test_order_key_per_handler_chain(self) -> None:
        """同じ order_key + 複数ハンドラ: ハンドラごとに FIFO 直列、 ハンドラ
        間は独立に並行。"""
        observed: List[tuple] = []
        observed_lock = threading.Lock()
        all_done = threading.Event()
        n = 10

        def make_handler(label: str):
            def _h(*, seq: int, **_p: Any) -> None:
                time.sleep(0.003 * (n - seq))
                with observed_lock:
                    observed.append((label, seq))
                    if len(observed) >= n * 2:
                        all_done.set()
            return _h

        h_a = make_handler("a")
        h_b = make_handler("b")
        addon_hooks.register_hook("persona_speak", h_a)
        addon_hooks.register_hook("persona_speak", h_b)

        for i in range(n):
            addon_hooks.dispatch_hook("persona_speak", order_key="m1", seq=i)

        self.assertTrue(all_done.wait(timeout=10.0))

        a_seqs = [seq for label, seq in observed if label == "a"]
        b_seqs = [seq for label, seq in observed if label == "b"]
        self.assertEqual(a_seqs, list(range(n)))
        self.assertEqual(b_seqs, list(range(n)))

    def test_order_key_handler_exception_does_not_stop_chain(self) -> None:
        """直列 chain の途中で例外が出ても、 後続 dispatch は実行される。"""
        observed: List[int] = []
        observed_lock = threading.Lock()
        all_done = threading.Event()

        def handler(*, seq: int, **_p: Any) -> None:
            if seq == 1:
                raise RuntimeError("intentional")
            with observed_lock:
                observed.append(seq)
                if len(observed) >= 4:
                    all_done.set()

        addon_hooks.register_hook("persona_speak", handler)

        for i in range(5):
            addon_hooks.dispatch_hook("persona_speak", order_key="m1", seq=i)

        self.assertTrue(all_done.wait(timeout=5.0))
        # seq=1 は例外で skip、 残り 4 件は順序保たれる
        self.assertEqual(observed, [0, 2, 3, 4])

    def test_order_key_future_completed_before_callback_registration(self) -> None:
        """submit した Future が add_done_callback より先に完了していても固まらない。

        ``Future.add_done_callback`` は Future が既に完了していると callback を
        呼び出し元スレッドで即座に同期実行する。 その callback (_on_chain_done)
        が _chain_lock を取るので、 登録をロックの中で行うと自分自身を待つ
        自己デッドロックになる (2026-08-23 フルスイートで実際に発生)。

        本物のプールでは確率的にしか踏まないので、 submit 時点で同期実行して
        完了済み Future を返す executor に差し替え、 この並びを毎回強制する。
        """
        from concurrent.futures import Future

        class _ImmediateExecutor:
            def submit(self, fn: Any, *args: Any, **kwargs: Any) -> Future:
                fut: Future = Future()
                try:
                    fut.set_result(fn(*args, **kwargs))
                except BaseException as exc:  # noqa: BLE001 - 完了済み Future を返す
                    fut.set_exception(exc)
                return fut

        observed: List[int] = []

        def handler(*, seq: int, **_p: Any) -> None:
            observed.append(seq)

        addon_hooks.register_hook("persona_speak", handler)
        n = 50

        def run() -> None:
            for i in range(n):
                addon_hooks.dispatch_hook("persona_speak", order_key="m1", seq=i)

        original = addon_hooks._executor
        addon_hooks._executor = _ImmediateExecutor()  # type: ignore[assignment]
        try:
            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            worker.join(timeout=5.0)
            self.assertFalse(
                worker.is_alive(),
                "dispatch_hook deadlocked on _chain_lock "
                "(add_done_callback ran _on_chain_done synchronously)",
            )
        finally:
            addon_hooks._executor = original
        self.assertEqual(observed, list(range(n)))
        # 完了済み Future のエントリは _on_chain_done で掃除されている
        self.assertEqual(addon_hooks._chain_state, {})

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
