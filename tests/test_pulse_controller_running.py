"""PulseController の「いま走っている処理」の記録と、停止ボタンの API の回帰テスト。

docs/intent/stop_button.md の保障 1・2:

- 新しい発言に割り込まれて裏で走り続けている古い request も、本当に終わる
  (``_execute_unlocked`` の finally を通る) まで記録に残る。
- 停止ボタンは、表示している建物にいるペルソナの走っている request すべてに
  "user_stop" を刻む (古い request を含む)。
- メタ判断は記録に載らない。
- ``GET /api/chat/running`` と ``POST /api/chat/stop`` (building_id 付き) が
  この記録だけを参照する。
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.deps import get_manager
from api.routes import chat
from sea.cancellation import ExecutionCancelledException
from sea.pulse_controller import PulseController
from saiverse.saiverse_manager import SAIVerseManager


def _make_controller() -> PulseController:
    return PulseController(sea_runtime=SimpleNamespace(manager=None))


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "条件が満たされなかった"
        time.sleep(0.01)


class _BlockingExecution:
    """request ごとに ``release[pulse_id]`` が立つまで走り続ける _do_execute の代役。

    固まったツールを待つ Beat の代わり: 割り込み ("user") の取り消しが来ても
    抜けず (ツールの完了を待つ)、"user_stop" が来たら抜ける。
    """

    def __init__(self) -> None:
        self.started: dict[str, threading.Event] = {}
        self.release: dict[str, threading.Event] = {}
        self.lock = threading.Lock()

    def events(self, pulse_id: str):
        with self.lock:
            return (
                self.started.setdefault(pulse_id, threading.Event()),
                self.release.setdefault(pulse_id, threading.Event()),
            )

    def __call__(self, request):
        started, release = self.events(request.pulse_id)
        started.set()
        deadline = time.monotonic() + 10
        while not release.is_set():
            assert time.monotonic() < deadline
            token = request.cancellation_token
            if token.is_cancelled() and token.interrupted_by == "user_stop":
                raise ExecutionCancelledException("stopped", interrupted_by="user_stop")
            time.sleep(0.01)
        return ["ok"]


def _submit_in_thread(controller: PulseController, text: str):
    holder = {}

    def run():
        holder["result"] = controller.submit_user(
            persona_id="p1", building_id="b1", user_input=text,
        )

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, holder


def test_superseded_request_stays_recorded_until_it_finishes():
    controller = _make_controller()
    execution = _BlockingExecution()
    controller._do_execute = execution

    first_thread, _ = _submit_in_thread(controller, "一つ目")
    _wait_until(lambda: controller.has_running("p1"))
    first = controller.running_requests("p1")[0]
    execution.events(first.pulse_id)[0].wait(5)

    second_thread, _ = _submit_in_thread(controller, "二つ目")
    _wait_until(lambda: len(controller.running_requests("p1")) == 2)
    second = controller._current["p1"]
    assert second is not first
    # 古い request は割り込みの印を受けたが、まだ走っていて記録に残る
    assert first.cancellation_token.interrupted_by == "user"
    assert controller.running_requests("p1") == [first, second]

    # 古い方が終わると記録から消え、新しい方だけが残る
    execution.events(first.pulse_id)[1].set()
    first_thread.join(5)
    assert controller.running_requests("p1") == [second]

    execution.events(second.pulse_id)[1].set()
    second_thread.join(5)
    assert not controller.has_running("p1")


def test_cancel_running_reaches_both_superseded_and_current_requests():
    controller = _make_controller()
    execution = _BlockingExecution()
    controller._do_execute = execution

    first_thread, _ = _submit_in_thread(controller, "一つ目")
    _wait_until(lambda: controller.has_running("p1"))
    first = controller.running_requests("p1")[0]
    execution.events(first.pulse_id)[0].wait(5)
    second_thread, _ = _submit_in_thread(controller, "二つ目")
    _wait_until(lambda: len(controller.running_requests("p1")) == 2)
    second = controller._current["p1"]
    execution.events(second.pulse_id)[0].wait(5)

    assert controller.cancel_running(["p1", "someone_else"], "user_stop") == 2
    # 割り込みの "user" は停止ボタンの "user_stop" で上書きされる
    assert first.cancellation_token.interrupted_by == "user_stop"
    assert second.cancellation_token.interrupted_by == "user_stop"

    first_thread.join(5)
    second_thread.join(5)
    assert not first_thread.is_alive() and not second_thread.is_alive()
    assert not controller.has_running("p1")
    assert first.runtime_outcome == "cancelled"
    assert second.runtime_outcome == "cancelled"


def test_meta_judgment_is_not_recorded_as_running():
    controller = _make_controller()
    seen = {}

    def fake_do_execute(request):
        seen["running"] = controller.has_running(request.persona_id)
        return []

    controller._do_execute = fake_do_execute
    controller.submit_meta_judgment(persona_id="p1", building_id="b1", meta_playbook="x")
    assert seen["running"] is False


def test_running_persona_ids_keeps_the_given_order():
    controller = _make_controller()
    execution = _BlockingExecution()
    controller._do_execute = execution
    thread, _ = _submit_in_thread(controller, "こんにちは")
    _wait_until(lambda: controller.has_running("p1"))
    assert controller.running_persona_ids(["p0", "p1", "p2"]) == ["p1"]
    controller.cancel_running(["p1"], "user_stop")
    thread.join(5)
    assert controller.running_persona_ids(["p1"]) == []


# ---------------------------------------------------------------------------
# SAIVerseManager.cancel_active_generation と API
# ---------------------------------------------------------------------------


def _fake_manager(controller: PulseController):
    """cancel_active_generation / running_persona_ids_in_building の実物を、
    最小の属性だけを持つ manager の上で呼ぶ。"""
    manager = SimpleNamespace(
        state=SimpleNamespace(user_current_building_id="b_user"),
        occupants={"b_user": ["p_user"], "b_view": ["p1"]},
        pulse_controller=controller,
        _active_stop_events={},
    )
    manager.cancel_active_generation = (
        lambda building_id=None: SAIVerseManager.cancel_active_generation(manager, building_id)
    )
    manager.running_persona_ids_in_building = (
        lambda building_id: SAIVerseManager.running_persona_ids_in_building(manager, building_id)
    )
    return manager


def _client(manager) -> TestClient:
    app = FastAPI()
    app.include_router(chat.router, prefix="/api/chat")
    app.dependency_overrides[get_manager] = lambda: manager
    return TestClient(app)


def test_running_endpoint_and_stop_with_displayed_building():
    controller = _make_controller()
    execution = _BlockingExecution()
    controller._do_execute = execution
    manager = _fake_manager(controller)
    thread, _ = _submit_in_thread(controller, "こんにちは")  # p1 は b_view にいる
    _wait_until(lambda: controller.has_running("p1"))
    request = controller.running_requests("p1")[0]

    with _client(manager) as client:
        res = client.get("/api/chat/running", params={"building_id": "b_view"})
        assert res.status_code == 200
        assert res.json() == {"running": True, "persona_ids": ["p1"]}
        res = client.get("/api/chat/running", params={"building_id": "b_user"})
        assert res.json() == {"running": False, "persona_ids": []}

        # building_id を省くと、ユーザーの今いる建物 (b_user) が対象 — p1 は止まらない
        res = client.post("/api/chat/stop")
        assert res.json() == {"cancelled": False}
        assert not request.cancellation_token.is_cancelled()

        # 表示している建物を渡すと、その建物のペルソナが止まる
        res = client.post("/api/chat/stop", json={"building_id": "b_view"})
        assert res.json() == {"cancelled": True}
        assert request.cancellation_token.interrupted_by == "user_stop"

    thread.join(5)
    assert not controller.has_running("p1")


def test_stop_sets_stop_event_of_the_displayed_building():
    controller = _make_controller()
    manager = _fake_manager(controller)
    stop_event = threading.Event()
    manager._active_stop_events["b_view"] = stop_event
    assert manager.cancel_active_generation(building_id="b_view") is True
    assert stop_event.is_set()
