"""dispatch_phenomenon_event の型付き成否 (Codex 五巡目 high2) の回帰テスト。

submit が例外を投げなくても Pulse が起動していないケースがある — PulseController
は Beat 関所閉鎖 (BeatGateClosedError) や一般例外を内部で捕捉して空配列で返す。
dispatch_phenomenon_event は受付裁定 (dispatch_action) と実行顛末
(runtime_outcome) を観測してから成否を返さなければ、回収経路の応対復元が
「消えた応対」を成功として記録してしまう。
"""
from __future__ import annotations

from types import SimpleNamespace

from saiverse.pulse_dispatcher import PulseDispatcher


def _dispatcher_with_controller(controller):
    return PulseDispatcher(SimpleNamespace(pulse_controller=controller))


def _call(dispatcher):
    return dispatcher.dispatch_phenomenon_event(
        persona_id="alice",
        building_id="alice_room",
        user_input="<system>x</system>",
    )


class _Controller:
    """submit で request の観測フィールドに顛末を記入する PulseController 面。"""

    def __init__(self, action=None, outcome=None, raises=None):
        self._action = action
        self._outcome = outcome
        self._raises = raises

    def submit(self, request):
        request.dispatch_action = self._action
        request.runtime_outcome = self._outcome
        if self._raises is not None:
            raise self._raises
        return []


def test_completed_pulse_is_success():
    assert _call(_dispatcher_with_controller(
        _Controller(action="execute", outcome="completed")
    )) is True


def test_queued_pulse_is_success():
    """queued は復帰 queue に残って消えない = accepted (D4 と同じ裁定)。"""
    assert _call(_dispatcher_with_controller(
        _Controller(action="queued", outcome=None)
    )) is True


def test_beat_gate_closed_is_failure():
    """関所閉鎖は submit 例外にならず空配列で戻る — Pulse は起動していない。"""
    assert _call(_dispatcher_with_controller(
        _Controller(action="execute", outcome="gate_closed")
    )) is False


def test_swallowed_error_is_failure():
    """PulseController 内部で畳まれた実行時例外 (runtime_outcome=error)。"""
    assert _call(_dispatcher_with_controller(
        _Controller(action="execute", outcome="error")
    )) is False


def test_submit_exception_is_failure():
    assert _call(_dispatcher_with_controller(
        _Controller(action="execute", raises=RuntimeError("boom"))
    )) is False


def test_missing_pulse_controller_is_failure():
    dispatcher = PulseDispatcher(SimpleNamespace())
    assert _call(dispatcher) is False
