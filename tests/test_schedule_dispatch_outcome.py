"""ExecutionRequest 観測フィールド + dispatch_schedule_fire 型付き戻り値 (W3 Chunk A)。

docs/handoff/2026-07-20_w3_schedule_ledger_handoff.md D4:
- PulseController.submit() は戻り値契約 (List[str] / None) を変えず、
  request.dispatch_action ("execute"/"queued"/"skipped") と
  request.runtime_outcome ("completed"/"gate_closed"/"cancelled"/"error") を
  観測用に記入する
- PulseDispatcher.dispatch_schedule_fire は握り潰し・None 戻しをやめ、
  {"action", "runtime_outcome", "error"} の型付き dict を返す (例外は再送出
  しない — ScheduleManager が台帳分類に使う)

PulseController のフェイク構成は test_beat_gate.py の _make_controller と同型。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from llm_clients.exceptions import LLMError
from sea.beat_gate import BeatGateClosedError
from sea.cancellation import ExecutionCancelledException
from sea.pulse_controller import ExecutionRequest, PulseController
from saiverse.pulse_dispatcher import PulseDispatcher


def _make_controller(behavior):
    """run_meta_user が behavior() を返す/送出する PulseController を作る。"""
    persona = SimpleNamespace(persona_id="p1")

    def run_meta_user(**kwargs):
        return behavior()

    sea_runtime = SimpleNamespace(
        manager=SimpleNamespace(all_personas={"p1": persona}),
        run_meta_user=run_meta_user,
    )
    return PulseController(sea_runtime)


def _raiser(exc):
    def _behavior():
        raise exc
    return _behavior


def _schedule_request(**overrides):
    kwargs = dict(
        type="schedule",
        persona_id="p1",
        building_id="b1",
        user_input="<system>スケジュールの実行時刻です。</system>",
    )
    kwargs.update(overrides)
    return ExecutionRequest(**kwargs)


# ---------------------------------------------------------------------------
# 1. ExecutionRequest 観測フィールド (submit / _execute_unlocked の記入)
# ---------------------------------------------------------------------------


def test_submit_records_execute_and_completed():
    pc = _make_controller(lambda: ["ok"])
    request = _schedule_request()
    result = pc.submit(request)
    assert result == ["ok"]  # 戻り値契約は不変
    assert request.dispatch_action == "execute"
    assert request.runtime_outcome == "completed"


def test_submit_records_queued_when_busy_with_schedule():
    # schedule は same_priority_policy="first" + on_blocked="wait" なので、
    # 先行 schedule が走行中なら後続は queue され dispatch_action="queued"。
    pc = _make_controller(lambda: ["ok"])
    running = _schedule_request()
    pc._current["p1"] = running  # 走行中の先行 schedule を模す

    queued = _schedule_request()
    result = pc.submit(queued)
    assert result is None  # 戻り値契約は不変 (queued は None)
    assert queued.dispatch_action == "queued"
    assert queued.runtime_outcome is None  # まだ実行されていない
    assert pc._queues["p1"] == [queued]


def test_submit_records_skipped_for_auto_when_busy():
    # auto は on_blocked="skip"。user 走行中の auto submit は skipped。
    pc = _make_controller(lambda: ["ok"])
    pc._current["p1"] = ExecutionRequest(
        type="user", persona_id="p1", building_id="b1", user_input="hi",
    )
    auto = ExecutionRequest(
        type="auto", persona_id="p1", building_id="b1",
        meta_playbook="track_autonomous",
    )
    result = pc.submit(auto)
    assert result is None
    assert auto.dispatch_action == "skipped"
    assert auto.runtime_outcome is None


def test_gate_closed_records_runtime_outcome():
    pc = _make_controller(_raiser(BeatGateClosedError("p1", "schedule")))
    request = _schedule_request()
    result = pc.submit(request)
    assert result == []  # schedule は gate closed を [] で落とす (従来どおり)
    assert request.dispatch_action == "execute"
    assert request.runtime_outcome == "gate_closed"


def test_cancelled_records_runtime_outcome():
    pc = _make_controller(_raiser(
        ExecutionCancelledException(interrupted_by="user")
    ))
    request = _schedule_request()
    # 中断を記憶へ書く機構は 2026-08-26 に撤去した。ここで確かめるのは
    # 「中断は cancelled として記録され、submit は [] を返す」という契約だけ。
    result = pc.submit(request)
    assert result == []
    assert request.runtime_outcome == "cancelled"


def test_generic_error_records_runtime_outcome():
    pc = _make_controller(_raiser(RuntimeError("playbook exploded")))
    request = _schedule_request()
    result = pc.submit(request)
    assert result == []  # 従来どおり例外は [] に潰れる (schedule メインレーン)
    assert request.runtime_outcome == "error"


def test_llm_error_reraises_but_records_outcome():
    pc = _make_controller(_raiser(LLMError("provider down")))
    request = _schedule_request()
    with pytest.raises(LLMError):
        pc.submit(request)
    assert request.dispatch_action == "execute"
    assert request.runtime_outcome == "error"


# ---------------------------------------------------------------------------
# 2. dispatch_schedule_fire の型付き戻り値
# ---------------------------------------------------------------------------


def _dispatch(manager):
    dispatcher = PulseDispatcher(manager)
    return dispatcher.dispatch_schedule_fire(
        persona_id="p1",
        building_id="b1",
        user_input="<system>スケジュールの実行時刻です。</system>",
        metadata={"schedule_id": 1, "schedule_type": "periodic"},
    )


def test_dispatch_returns_unavailable_without_controller():
    outcome = _dispatch(SimpleNamespace(pulse_controller=None))
    assert outcome == {"action": "unavailable", "runtime_outcome": None, "error": None}


def test_dispatch_returns_executed_completed():
    pc = _make_controller(lambda: ["done"])
    outcome = _dispatch(SimpleNamespace(pulse_controller=pc))
    assert outcome == {
        "action": "execute", "runtime_outcome": "completed", "error": None,
    }


def test_dispatch_returns_error_when_submit_raises():
    # LLMError は submit から再送出される — dispatch は握って型付きで返す
    pc = _make_controller(_raiser(LLMError("provider down")))
    outcome = _dispatch(SimpleNamespace(pulse_controller=pc))
    assert outcome["action"] == "execute"  # 受付裁定は済んでいた
    assert outcome["runtime_outcome"] == "error"
    assert "provider down" in outcome["error"]


def test_dispatch_returns_error_before_submit_when_action_unset():
    # submit が受付裁定 (dispatch_action 記入) 前に転ぶスタブ
    class _BrokenController:
        def submit(self, request):
            raise RuntimeError("broken before dispatch decision")

    outcome = _dispatch(SimpleNamespace(pulse_controller=_BrokenController()))
    assert outcome["action"] == "error_before_submit"
    assert outcome["runtime_outcome"] == "error"
    assert "broken before dispatch decision" in outcome["error"]


def test_dispatch_returns_gate_closed_outcome():
    pc = _make_controller(_raiser(BeatGateClosedError("p1", "schedule")))
    outcome = _dispatch(SimpleNamespace(pulse_controller=pc))
    assert outcome == {
        "action": "execute", "runtime_outcome": "gate_closed", "error": None,
    }


def test_dispatch_preserves_completed_when_post_completion_raises():
    """実行完走後の後処理例外 (queue 処理等) が outcome を "error" で上書き
    しない (Codex W3 第七陣 medium) — 上書きすると成功した発火が unknown 扱いに
    なり、oneshot/interval の occurrence が自動再実行禁止で封印される。"""
    class _PostCompletionBoom:
        def submit(self, request):
            request.dispatch_action = "execute"
            request.runtime_outcome = "completed"
            raise RuntimeError("queue drain failed")

    outcome = _dispatch(SimpleNamespace(pulse_controller=_PostCompletionBoom()))
    assert outcome["action"] == "execute"
    assert outcome["runtime_outcome"] == "completed"
    assert "post-completion" in outcome["error"]


# ---------------------------------------------------------------------------
# 最終防衛ライン未達 (WindowFloorUnmetError) — Codex 二巡目 #2
# ---------------------------------------------------------------------------


def test_floor_unmet_records_floor_unmet_outcome_without_a_second_event():
    """run_meta_user が WindowFloorUnmetError を送出したら PulseController は
    "floor_unmet" と記帳して [] を返す (再送出しない)。user への通知は
    run_meta_user が出した一度だけ — controller は二つ目の error を出さない。"""
    from sea.runtime_context import WindowFloorUnmetError

    events = []

    def _behavior():
        # run_meta_user は送出前に一度だけ通知している
        events.append({"type": "error", "error_code": "window_floor_unmet", "content": "x"})
        raise WindowFloorUnmetError("floor unmet")

    pc = _make_controller(_behavior)
    user = ExecutionRequest(
        type="user", persona_id="p1", building_id="b1", user_input="hello",
        event_callback=events.append,
    )
    assert pc.submit(user) == []
    assert user.dispatch_action == "execute"
    assert user.runtime_outcome == "floor_unmet"
    assert len([e for e in events if e.get("type") == "error"]) == 1

    sched = _schedule_request(event_callback=events.append)
    events.clear()
    assert pc.submit(sched) == []
    assert sched.runtime_outcome == "floor_unmet"
    assert len([e for e in events if e.get("type") == "error"]) == 1  # 送出前の分だけ


def test_dispatch_returns_floor_unmet_and_schedule_classifies_it_failed():
    """schedule 経路: dispatch は floor_unmet を型付きで返し、ScheduleManager は
    failed (副作用ゼロ・再試行安全) に分類する — executed でも unknown でもない。"""
    from sea.runtime_context import WindowFloorUnmetError
    from saiverse.schedule_manager import ScheduleManager

    pc = _make_controller(_raiser(WindowFloorUnmetError("floor unmet")))
    dispatcher = PulseDispatcher(SimpleNamespace(pulse_controller=pc))
    outcome = dispatcher.dispatch_schedule_fire(
        persona_id="p1", building_id="b1", user_input="<system>x</system>",
    )
    assert outcome == {
        "action": "execute", "runtime_outcome": "floor_unmet", "error": None,
    }
    assert ScheduleManager._classify_dispatch_outcome(outcome) == (
        "failed", "window floor unmet",
    )
    # inject 系の bool 版も「起動していない」
    assert dispatcher.dispatch_phenomenon_event(
        persona_id="p1", building_id="b1", user_input="<system>x</system>",
    ) is False
