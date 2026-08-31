"""サーバー終了時の Pulse 停止 (PulseController.shutdown) の回帰テスト。

サーバー終了 (Ctrl+C / SIGTERM) で走行中の発言生成を「停止ボタンと同じ後始末」で
締めてから死ぬための機構。ここで固定する契約は三つ:

- **走行中の Pulse は "server_shutdown" で取り消され、台帳から消えるまで待つ**。
  台帳から消える = ``_execute_unlocked`` の finally を通過した = Beat の後始末
  (途中本文の確定・記憶書き込み) が完了した、という関係に依存している。
- **shutdown 後は新規 Pulse を受け付けない** (submit は skipped、待機列の
  繰り上げも止まる)。取り消しで空いた席に次の生成が座ると、締めたそばから
  新しい下書き行が生まれる。
- **締切超過は False で返り、残りの終了処理を止めない**。
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from sea.cancellation import ExecutionCancelledException
from sea.pulse_controller import ExecutionRequest, PulseController


def _make_controller() -> PulseController:
    # PulseController は sea_runtime を保持するだけなので、実行を
    # _do_execute の差し替えで塞げば SEARuntime の実物は要らない。
    return PulseController(sea_runtime=SimpleNamespace(manager=None))


def _request(type_: str = "user", persona_id: str = "p1") -> ExecutionRequest:
    return ExecutionRequest(
        type=type_,
        persona_id=persona_id,
        building_id="b1",
        user_input="こんにちは",
    )


def test_shutdown_cancels_the_running_pulse_and_waits_for_its_cleanup():
    """走行中の request が "server_shutdown" で取り消され、実行スレッドが
    ExecutionCancelledException で抜けて台帳から消えるまで待って True が返る。"""
    controller = _make_controller()
    started = threading.Event()
    cleanup_done = threading.Event()  # Beat の後始末 (途中本文の確定等) の代役

    def fake_do_execute(request):
        # 本物の Beat と同じく、取り消しの信号が届くまで走り続け、届いたら
        # 後始末を済ませてから ExecutionCancelledException で抜ける
        # (実物では後始末は runtime_llm の例外経路の中で済む)。
        started.set()
        deadline = time.monotonic() + 5.0
        while not request.cancellation_token.is_cancelled():
            assert time.monotonic() < deadline, "取り消しの信号が届かなかった"
            time.sleep(0.01)
        cleanup_done.set()
        raise ExecutionCancelledException(
            "cancelled", interrupted_by=request.cancellation_token.interrupted_by
        )

    controller._do_execute = fake_do_execute

    request = _request()
    worker = threading.Thread(target=lambda: controller.submit(request))
    worker.start()
    try:
        assert started.wait(2.0), "実行が始まらなかった"

        assert controller.shutdown(timeout=5.0) is True

        # shutdown() が True を返す時点で後始末は済んでいる — 「台帳から
        # 消えた = 後始末完了」の荷重前提をここで契約として固定する。
        assert cleanup_done.is_set(), "後始末の完了前に shutdown が True を返した"
        assert request.cancellation_token.interrupted_by == "server_shutdown"
        assert "p1" not in controller._current
        assert request.runtime_outcome == "cancelled"
    finally:
        worker.join(2.0)
    assert not worker.is_alive()


def test_submit_after_shutdown_is_skipped_without_executing():
    """shutdown() 後の submit は None を返し、実行は起きない。"""
    controller = _make_controller()
    calls = []
    controller._do_execute = lambda request: calls.append(request) or []

    assert controller.shutdown() is True  # 何も走っていないので即 True

    request = _request()
    assert controller.submit(request) is None
    assert request.dispatch_action == "skipped"
    assert calls == []
    assert "p1" not in controller._current


def test_queued_request_is_not_promoted_during_shutdown():
    """待機列に wait ポリシーの request がある状態で走行中を shutdown しても、
    取り消しで空いた席に待機列の次が座らない (終了処理中に新しい生成が
    始まらない)。"""
    controller = _make_controller()
    started = threading.Event()
    executed = []

    def fake_do_execute(request):
        executed.append(request)
        started.set()
        deadline = time.monotonic() + 5.0
        while not request.cancellation_token.is_cancelled():
            assert time.monotonic() < deadline, "取り消しの信号が届かなかった"
            time.sleep(0.01)
        raise ExecutionCancelledException(
            "cancelled", interrupted_by=request.cancellation_token.interrupted_by
        )

    controller._do_execute = fake_do_execute

    # schedule は on_blocked="wait" — 先行 schedule が走行中に来た同種の
    # request は待機列に入る。
    first = _request(type_="schedule")
    worker = threading.Thread(target=lambda: controller.submit(first))
    worker.start()
    try:
        assert started.wait(2.0), "実行が始まらなかった"

        queued = _request(type_="schedule")
        assert controller.submit(queued) is None
        assert queued.dispatch_action == "queued"

        assert controller.shutdown(timeout=5.0) is True
    finally:
        worker.join(2.0)
    assert not worker.is_alive()

    # 実行されたのは先行の 1 本だけ。待機列の request は席に座らない。
    assert executed == [first]
    assert queued.runtime_outcome is None


def test_shutdown_times_out_when_a_pulse_ignores_the_cancellation():
    """取り消しを無視して終わらない request には、締切超過で False が返る
    (残りの終了処理は呼び出し側が続ける)。"""
    controller = _make_controller()
    started = threading.Event()
    release = threading.Event()

    def stubborn_do_execute(request):
        started.set()
        release.wait(10.0)  # 取り消しを見ない — テスト終了時に解放して回収する
        return []

    controller._do_execute = stubborn_do_execute

    request = _request()
    worker = threading.Thread(target=lambda: controller.submit(request))
    worker.start()
    try:
        assert started.wait(2.0), "実行が始まらなかった"

        assert controller.shutdown(timeout=0.3) is False
        # 取り消し自体は刻まれている (無視されただけ)
        assert request.cancellation_token.interrupted_by == "server_shutdown"
        assert "p1" in controller._current
    finally:
        release.set()
        worker.join(2.0)
    assert not worker.is_alive()


def test_a_late_registration_is_cancelled_by_the_wait_loop():
    """受付ガードを通過した後・台帳登録前だったスレッドが、shutdown() の
    一覧取得の後に台帳へ載る (check-then-act の窓)。待機ループの各周回の
    打ち直しがこの遅刻の登録を回収することを固定する。"""
    controller = _make_controller()
    r1_started = threading.Event()
    r1_release = threading.Event()

    def fake_do_execute(request):
        # R1 は取り消しが届いても、テスト側が R2 を挿入し終えるまで台帳に
        # 残る (即抜けると、R2 挿入前に台帳が空になって shutdown が先に
        # True を返し、遅刻の形を作れない)。
        r1_started.set()
        deadline = time.monotonic() + 5.0
        while not request.cancellation_token.is_cancelled():
            assert time.monotonic() < deadline, "取り消しの信号が届かなかった"
            time.sleep(0.01)
        assert r1_release.wait(5.0), "解放の合図が届かなかった"
        raise ExecutionCancelledException(
            "cancelled", interrupted_by=request.cancellation_token.interrupted_by
        )

    controller._do_execute = fake_do_execute

    r1 = _request(persona_id="p1")
    r1_worker = threading.Thread(target=lambda: controller.submit(r1))

    shutdown_result = {}
    shutdown_thread = threading.Thread(
        target=lambda: shutdown_result.setdefault(
            "value", controller.shutdown(timeout=5.0)
        )
    )

    # R2: 未取り消しのトークンのまま台帳へ直接挿入される「遅刻の登録」。
    # worker は取り消しを待ち、finally で自分を台帳から消す
    # (_execute_unlocked の finally の最小の代役)。
    r2 = _request(persona_id="p2")

    def r2_worker_body():
        try:
            deadline = time.monotonic() + 5.0
            while not r2.cancellation_token.is_cancelled():
                assert time.monotonic() < deadline, "打ち直しが届かなかった"
                time.sleep(0.01)
        finally:
            controller._current.pop("p2", None)

    r2_worker = threading.Thread(target=r2_worker_body)

    r1_worker.start()
    try:
        assert r1_started.wait(2.0), "R1 の実行が始まらなかった"

        shutdown_thread.start()
        # shutdown() が一覧取得を終えた目印 = R1 に取り消しが刻まれた
        deadline = time.monotonic() + 2.0
        while not r1.cancellation_token.is_cancelled():
            assert time.monotonic() < deadline, "shutdown が始まらなかった"
            time.sleep(0.01)

        # 一覧取得の後に台帳へ載る遅刻の登録
        controller._current["p2"] = r2
        r2_worker.start()
        r1_release.set()

        shutdown_thread.join(6.0)
        assert shutdown_result.get("value") is True
        assert r2.cancellation_token.interrupted_by == "server_shutdown"
        assert "p2" not in controller._current
    finally:
        r1_release.set()
        r2.cancellation_token.cancel(interrupted_by="server_shutdown")
        r1_worker.join(2.0)
        # 途中の assert で失敗した場合は未 start のことがある — 未 start の
        # スレッドへの join は RuntimeError で元の失敗をすり替えるので避ける
        if r2_worker.ident is not None:
            r2_worker.join(2.0)
        if shutdown_thread.ident is not None:
            shutdown_thread.join(2.0)
    assert not r1_worker.is_alive()
    assert not r2_worker.is_alive()
    assert not shutdown_thread.is_alive()


class _FlagFlipsAfter:
    """最初の ``false_reads`` 回は False、それ以降は True と読める旗もどき。

    _shutting_down の差し替え用 — 「登録前の検査は通過し、登録後の検査で
    旗が見える」というタイミング (shutdown() が検査の合間に始まった形) を
    決定的に再現する。
    """

    def __init__(self, false_reads: int):
        self._reads = 0
        self._false_reads = false_reads

    def __bool__(self):
        self._reads += 1
        return self._reads > self._false_reads


def test_meta_lane_post_registration_check_vacates_the_seat():
    """メタレーンの登録後検査 — submit() の冒頭ガードを通らない直接呼びで、
    登録前の検査 (1 回目の読み) は通過させ、台帳登録後の再検査 (2 回目の
    読み) で旗が見える形にする。自分で席を消して None が返ること。"""
    controller = _make_controller()
    calls = []
    controller._do_execute = lambda request: calls.append(request) or []

    controller._shutting_down = _FlagFlipsAfter(1)

    request = ExecutionRequest(
        type="meta_judgment",
        persona_id="p1",
        building_id="b1",
        meta_playbook="judgment_day_open",
    )
    assert controller._submit_meta_lane(request) is None
    assert request.dispatch_action == "skipped"
    assert "p1" not in controller._current_meta  # 席が残らない
    assert calls == []  # 実行は起きない


def test_process_queue_post_registration_check_vacates_the_seat():
    """_process_queue の登録後検査 — ロック内先頭の検査 (1 回目の読み) は
    通過させ、台帳登録後の再検査 (2 回目の読み) で旗が見える形にする。
    席が残らず、実行スレッドが生まれないこと。"""
    controller = _make_controller()
    calls = []
    controller._do_execute = lambda request: calls.append(request) or []

    queued = _request(type_="schedule")
    controller._get_queue("p1").append(queued)

    controller._shutting_down = _FlagFlipsAfter(1)
    controller._process_queue("p1")

    assert "p1" not in controller._current  # 席が残らない
    assert calls == []  # 実行は起きない
    assert controller._get_queue("p1") == []  # 取り出し済みの request は破棄


def test_manager_stop_all_retries_a_racing_stop_event_listing():
    """stop_event の一覧取得が並行変更で RuntimeError になっても、リトライで
    拾い直し、最終的に pulse_controller.shutdown へ必ず到達すること。"""
    from saiverse.saiverse_manager import SAIVerseManager

    ev = threading.Event()

    class _FlakyEvents:
        """1 回目の values() で RuntimeError を投げ、2 回目から成功する dict もどき。"""

        def __init__(self, events):
            self._events = events
            self._failures = 1

        def values(self):
            if self._failures > 0:
                self._failures -= 1
                raise RuntimeError("dictionary changed size during iteration")
            return list(self._events)

    calls = {}

    def fake_shutdown(timeout):
        calls["timeout"] = timeout
        return True

    fake_manager = SimpleNamespace(
        _active_stop_events=_FlakyEvents([ev]),
        pulse_controller=SimpleNamespace(shutdown=fake_shutdown),
    )

    result = SAIVerseManager.stop_all_active_generations(fake_manager, timeout=2.0)

    assert result is True
    assert ev.is_set()  # リトライ後の一覧でイベントは set された
    assert calls == {"timeout": 2.0}


def test_manager_stop_all_sets_every_stop_event_and_delegates():
    """SAIVerseManager.stop_all_active_generations — 全建物の stop_event を
    立ててから PulseController.shutdown へ委譲する。本体のインスタンス化は
    コストが高いので、メソッドを unbound で fake に適用する。"""
    from saiverse.saiverse_manager import SAIVerseManager

    ev1, ev2 = threading.Event(), threading.Event()
    calls = {}

    def fake_shutdown(timeout):
        # 順序の契約: stop_event が先、shutdown への委譲が後。逆だと
        # shutdown の完了待ちの間に backend_worker が次のペルソナの生成を
        # 始められてしまう。
        calls["events_set_before_delegation"] = ev1.is_set() and ev2.is_set()
        calls["timeout"] = timeout
        return True

    fake_manager = SimpleNamespace(
        _active_stop_events={"b1": ev1, "b2": ev2},
        pulse_controller=SimpleNamespace(shutdown=fake_shutdown),
    )

    result = SAIVerseManager.stop_all_active_generations(fake_manager, timeout=1.5)

    assert result is True
    assert ev1.is_set() and ev2.is_set()
    assert calls["events_set_before_delegation"] is True
    assert calls["timeout"] == 1.5
