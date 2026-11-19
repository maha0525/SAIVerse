"""エラーで発言が生まれなかった回を、部屋の記録に一行残す (docs/intent/no_utterance_error_notice.md)。

固定する契約:

- 実行が発言を一つも残さずに失敗したら、その部屋の記録へ host の行が一行だけ
  置かれる。本文は「(エラーにより、{名前}は発言できませんでした)」で、原因の
  詳細は行の metadata の ``__no_utterance_error__`` にだけ置かれる。
- 行は部屋の在室者全員 + 本人に届く (heard_by)。
- 発言が保存された回・停止ボタンの回・割り込みの回には置かない。サーバーの
  停止の回は停止の文面で置く。
- 例外を出さずに終わる失敗 (再試行しても空だった応答 / 見つからない Playbook) も
  置くが、``runtime_outcome`` は ``completed`` のまま。
- 取り込み (auto_ingest) がペルソナの記憶へ写すのは本文だけで、詳細は写さない。
- やり直しのある経路 (スケジュール・回収された外部イベントへの応答) は、やり直しが
  尽きたときに一度だけ置く。書く部屋が決まらない失敗は画面右下の知らせに出す。

本物の LLM・本番の DB・ペルソナには触れない (書き込み先はすべて代用品)。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from llm_clients.exceptions import (
    ContextLengthExceededError,
    LLMError,
    ModelUnavailableError,
)
from sea.beat_gate import BeatGateClosedError
from sea.cancellation import ExecutionCancelledException
from sea.no_utterance_notice import (
    NO_UTTERANCE_DETAIL_KEY,
    failure_detail,
    note_no_utterance_cause,
)
from sea.pulse_controller import ExecutionRequest, PulseController
from sea.runtime_emitters import forget_saved_utterances, notify_speak_persisted


@pytest.fixture(autouse=True)
def _fresh_saved_utterance_record():
    """「最後に保存した発言」の記録はプロセス内で共有 — テストごとに空にする。"""
    forget_saved_utterances()
    yield
    forget_saved_utterances()


def _history_manager() -> MagicMock:
    hm = MagicMock()
    seq = {"n": 0}

    def _add(building_id, msg, *, heard_by=None):
        seq["n"] += 1
        return {**msg, "message_id": f"{building_id}:{seq['n']}", "heard_by": heard_by}

    hm.add_to_building_only.side_effect = _add
    return hm


def _world(occupants=None):
    persona = SimpleNamespace(
        persona_id="eris", persona_name="エリス", history_manager=_history_manager(),
        current_building_id="room",
    )
    manager = SimpleNamespace(
        occupants=occupants if occupants is not None else {"room": ["eris", "mio"]},
        all_personas={"eris": persona},
    )
    controller = PulseController(sea_runtime=SimpleNamespace(manager=manager))
    return controller, manager, persona


def _host_rows(persona) -> list:
    """部屋の記録へ置かれた host の行 (部屋, 行, heard_by) の列。"""
    return [
        (c.args[0], c.args[1], c.kwargs.get("heard_by"))
        for c in persona.history_manager.add_to_building_only.call_args_list
        if isinstance(c.args[1], dict) and c.args[1].get("role") == "host"
    ]


def _request(type_="schedule", building_id="room") -> ExecutionRequest:
    return ExecutionRequest(
        type=type_, persona_id="eris", building_id=building_id, user_input="x",
    )


# ---------------------------------------------------------------------------
# PulseController._execute_unlocked の確認
# ---------------------------------------------------------------------------


def test_an_llm_error_with_nothing_saved_writes_one_row_to_everyone_in_the_room():
    controller, _manager, persona = _world()
    original = Exception(
        "This model's maximum context length is 128000 tokens. However, your "
        "messages resulted in 159608 tokens."
    )
    err = ContextLengthExceededError("OpenAI failed: context length exceeded", original)
    err.model = "gpt-4o-2024-11-20"

    def _fail(request):
        raise err

    controller._do_execute = _fail
    with pytest.raises(LLMError):
        controller.submit(_request())

    rows = _host_rows(persona)
    assert len(rows) == 1
    building_id, row, heard_by = rows[0]
    assert building_id == "room"
    assert row["content"] == "(エラーにより、エリスは発言できませんでした)"
    # 部屋の全員 + 本人に届く (問い 0)
    assert set(heard_by) == {"eris", "mio"}
    detail = row["metadata"][NO_UTTERANCE_DETAIL_KEY]
    assert detail["error_code"] == "context_length_exceeded"
    assert detail["message"] == ContextLengthExceededError.user_message
    assert "159608 tokens" in detail["technical_detail"]
    assert detail["model"] == "gpt-4o-2024-11-20"
    # 本文には原因を書かない (まはーが決めたこと 3)
    assert "上限" not in row["content"]


def test_the_user_lane_writes_the_row_before_the_error_reaches_the_caller():
    controller, _manager, persona = _world()
    seen_at_raise = []

    def _fail(request):
        raise LLMError("boom")

    controller._do_execute = _fail
    try:
        controller.submit(_request("user"))
    except LLMError:
        seen_at_raise.append(len(_host_rows(persona)))
    assert seen_at_raise == [1]


def test_a_model_unavailable_error_also_writes_a_row():
    controller, _manager, persona = _world()

    def _fail(request):
        raise ModelUnavailableError(
            "missing", role="default_model", reason="missing", model="gone",
            user_message="エリスが選んでいた標準モデル 'gone' は SAIVerse にありません。",
        )

    controller._do_execute = _fail
    with pytest.raises(ModelUnavailableError):
        controller.submit(_request("user"))

    (_bid, row, _heard), = _host_rows(persona)
    detail = row["metadata"][NO_UTTERANCE_DETAIL_KEY]
    assert detail["error_code"] == "model_unavailable"
    assert detail["model"] == "gone"
    assert "gone" not in row["content"]


def test_no_row_when_an_utterance_was_saved_during_the_run():
    controller, _manager, persona = _world()

    def _speak_then_fail(request):
        notify_speak_persisted(
            None, {"message_id": "room:9", "content": "おはよう"}, persona, "pl",
            building_id="room",
        )
        raise LLMError("boom")

    controller._do_execute = _speak_then_fail
    with pytest.raises(LLMError):
        controller.submit(_request())
    assert _host_rows(persona) == []


def test_an_utterance_saved_before_the_run_does_not_hide_the_failure():
    controller, _manager, persona = _world()
    notify_speak_persisted(
        None, {"message_id": "room:1", "content": "前の発言"}, persona, "pl",
        building_id="room",
    )
    time.sleep(0.01)

    def _fail(request):
        raise LLMError("boom")

    controller._do_execute = _fail
    with pytest.raises(LLMError):
        controller.submit(_request())
    assert len(_host_rows(persona)) == 1


@pytest.mark.parametrize("interrupted_by", ["user_stop", "user", "schedule"])
def test_no_row_for_the_stop_button_or_a_preemption(interrupted_by):
    controller, _manager, persona = _world()

    def _cancelled(request):
        request.cancellation_token.cancel(interrupted_by=interrupted_by)
        raise ExecutionCancelledException("stopped", interrupted_by=interrupted_by)

    controller._do_execute = _cancelled
    controller.submit(_request())
    assert _host_rows(persona) == []


def test_no_row_when_the_llm_node_wrapped_a_user_stop_into_an_llm_error():
    """LLM ノードは取り消しを LLMError に包み直す — 連鎖をたどって停止ボタンと読む。"""
    controller, _manager, persona = _world()

    def _wrapped(request):
        cancel = ExecutionCancelledException("stopped", interrupted_by="user_stop")
        raise LLMError("LLM node failed", original_error=cancel)

    controller._do_execute = _wrapped
    with pytest.raises(LLMError):
        controller.submit(_request("user"))
    assert _host_rows(persona) == []


def test_no_row_for_a_request_whose_resumption_copy_was_queued():
    controller, _manager, persona = _world()
    request = _request()
    request.requeued = True

    def _fail(req):
        raise LLMError("boom")

    controller._do_execute = _fail
    controller._current["eris"] = request
    with pytest.raises(LLMError):
        controller._execute_unlocked(request)
    assert _host_rows(persona) == []


def test_a_server_shutdown_writes_the_shutdown_variant():
    controller, _manager, persona = _world()

    def _cancelled(request):
        request.cancellation_token.cancel(interrupted_by="server_shutdown")
        raise ExecutionCancelledException("stopped", interrupted_by="server_shutdown")

    controller._do_execute = _cancelled
    controller.submit(_request())

    (_bid, row, heard_by), = _host_rows(persona)
    assert row["content"] == "(サーバーの停止により、エリスは発言できませんでした)"
    assert set(heard_by) == {"eris", "mio"}
    assert row["metadata"][NO_UTTERANCE_DETAIL_KEY]["error_code"] == "server_shutdown"


def test_a_request_refused_during_shutdown_gets_the_shutdown_variant():
    controller, _manager, persona = _world()
    calls = []
    controller._do_execute = lambda request: calls.append(request) or []
    assert controller.shutdown() is True

    assert controller.submit(_request()) is None
    assert calls == []
    (_bid, row, _heard), = _host_rows(persona)
    assert row["content"].startswith("(サーバーの停止により、")


def test_queued_requests_discarded_at_shutdown_each_get_a_shutdown_row():
    controller, _manager, persona = _world()
    started = threading.Event()

    def _runs_until_cancelled(request):
        started.set()
        deadline = time.monotonic() + 5.0
        while not request.cancellation_token.is_cancelled():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        raise ExecutionCancelledException("x", interrupted_by="server_shutdown")

    controller._do_execute = _runs_until_cancelled
    running = _request()
    worker = threading.Thread(target=lambda: controller.submit(running))
    worker.start()
    assert started.wait(2.0)
    queued = _request()
    assert controller.submit(queued) is None
    assert queued.dispatch_action == "queued"

    assert controller.shutdown(timeout=5.0) is True
    worker.join(2.0)

    contents = [row["content"] for _bid, row, _h in _host_rows(persona)]
    # 走っていた実行の分と、列から捨てた分の二行
    assert contents == ["(サーバーの停止により、エリスは発言できませんでした)"] * 2
    assert controller._get_queue("eris") == []


def test_queue_overflow_writes_an_error_row_for_the_dropped_request():
    from sea.pulse_controller import QUEUE_LIMIT

    controller, _manager, persona = _world()
    controller._current["eris"] = _request()  # 走っている実行の代わり
    for _ in range(QUEUE_LIMIT):
        controller.submit(_request())
    assert _host_rows(persona) == []
    controller.submit(_request())

    (_bid, row, _heard), = _host_rows(persona)
    assert row["content"] == "(エラーにより、エリスは発言できませんでした)"
    assert row["metadata"][NO_UTTERANCE_DETAIL_KEY]["error_code"] == "queue_overflow"


def test_an_empty_response_marker_writes_a_row_and_keeps_the_outcome_completed():
    controller, _manager, persona = _world()

    def _empty(request):
        note_no_utterance_cause(
            request.cancellation_token,
            failure_detail("empty_response", model="m1"),
        )
        return []

    controller._do_execute = _empty
    request = _request()
    controller.submit(request)

    assert request.runtime_outcome == "completed"
    (_bid, row, _heard), = _host_rows(persona)
    assert row["metadata"][NO_UTTERANCE_DETAIL_KEY] == {
        "error_code": "empty_response", "model": "m1",
    }


def test_a_completed_run_without_a_marker_is_an_intended_silence():
    controller, _manager, persona = _world()
    controller._do_execute = lambda request: []
    request = _request()
    controller.submit(request)
    assert request.runtime_outcome == "completed"
    assert _host_rows(persona) == []


def test_a_marker_from_another_run_is_not_picked_up():
    """理由は実行の札ごとに記録される — 別の実行の札の理由は拾わない。"""
    controller, _manager, persona = _world()
    other = _request()
    note_no_utterance_cause(other.cancellation_token, failure_detail("empty_response"))
    controller._do_execute = lambda request: []
    controller.submit(_request())
    assert _host_rows(persona) == []


def test_gate_closed_and_floor_unmet_write_immediately_only_on_the_user_lane():
    from sea.runtime_context import WindowFloorUnmetError

    for make_exc, code in (
        (lambda: BeatGateClosedError("eris", "schedule"), "beat_gate_closed"),
        (lambda: WindowFloorUnmetError("floor"), "window_floor_unmet"),
    ):
        controller, _manager, persona = _world()

        def _fail(request, make_exc=make_exc):
            raise make_exc()

        controller._do_execute = _fail
        controller.submit(_request("schedule"))
        assert _host_rows(persona) == [], code

        try:
            controller.submit(_request("user"))
        except BeatGateClosedError:
            pass
        (_bid, row, _heard), = _host_rows(persona)
        assert row["metadata"][NO_UTTERANCE_DETAIL_KEY]["error_code"] == code


def test_the_queued_thread_logs_an_llm_error_instead_of_dying(caplog):
    controller, _manager, persona = _world()
    release = threading.Event()
    finished = threading.Event()
    calls = []

    def _first_waits_then_queued_fails(request):
        calls.append(request)
        if len(calls) == 1:
            release.wait(2.0)
            return []
        finished.set()
        raise LLMError("queued boom")

    controller._do_execute = _first_waits_then_queued_fails
    first = threading.Thread(target=lambda: controller.submit(_request()))
    first.start()
    deadline = time.monotonic() + 2.0
    while not calls and time.monotonic() < deadline:
        time.sleep(0.01)
    controller.submit(_request())  # 列に入る
    hook_calls = []
    with caplog.at_level(logging.ERROR, logger="sea.pulse_controller"), \
            patch.object(threading, "excepthook", side_effect=hook_calls.append):
        release.set()
        first.join(2.0)
        assert finished.wait(2.0)
        time.sleep(0.2)
    assert hook_calls == []
    assert any("Queued schedule request" in r.getMessage() for r in caplog.records)
    # 部屋の記録への行は _execute_unlocked が置いている
    assert len(_host_rows(persona)) == 1


# ---------------------------------------------------------------------------
# 例外を出さずに終わる失敗 — 実物の runtime の記録
# ---------------------------------------------------------------------------


def test_run_meta_user_records_a_missing_playbook_on_the_runs_token():
    from sea.cancellation import CancellationToken
    from sea.no_utterance_notice import take_no_utterance_cause
    from sea.runtime import SEARuntime

    runtime = SEARuntime(manager_ref=MagicMock())
    runtime.session_lifecycle = MagicMock()
    runtime.session_lifecycle.ensure_window_floor.return_value = "ok"
    runtime._load_playbook_for = lambda *args, **kwargs: None
    persona = SimpleNamespace(persona_id="eris", sai_memory=None, history_manager=MagicMock())
    token = CancellationToken()

    with patch("sea.runtime.resolve_execution_context",
               return_value=SimpleNamespace(model_key="m1")), \
            patch("sea.runtime.refresh_mcp_tools_at_head"), \
            patch("builtin_data.tools.get_building_messages.auto_ingest_building_messages"), \
            patch("sea.head_pipeline.inject_copresence_recall"):
        result = runtime._run_meta_user_with_models(
            persona, "x", "room", MagicMock(),
            meta_playbook="no_such_playbook",
            cancellation_token=token, pulse_type="schedule",
        )

    assert result and "no_such_playbook" in result[0]
    noted = take_no_utterance_cause(token)
    assert noted["error_code"] == "playbook_not_found"
    assert noted["message"] == {
        "$ui": "api.noUtterance.playbookNotFound",
        "params": {"playbook": "no_such_playbook"},
    }


# ---------------------------------------------------------------------------
# 取り込み: 記憶に写るのは本文だけ
# ---------------------------------------------------------------------------


def test_ingestion_carries_only_the_body_not_the_detail():
    from builtin_data.tools.get_building_messages import _transcribe_message

    row = {
        "message_id": "room:5",
        "_building_id": "room",
        "role": "host",
        "content": "(エラーにより、エリスは発言できませんでした)",
        "metadata": {
            NO_UTTERANCE_DETAIL_KEY: failure_detail(
                "safety_filter",
                message="コンテンツが安全性フィルターによりブロックされました。",
                technical_detail="blocked: HARM_CATEGORY_X",
                model="m1",
            ),
        },
        "heard_by": ["mio", "eris"],
    }
    captured = []
    history_manager = MagicMock()
    history_manager.add_to_persona_only.side_effect = (
        lambda entry, memory_first=False: captured.append(entry) or ("synced", "mem1")
    )
    reader = SimpleNamespace(persona_id="mio", buildings={"room": SimpleNamespace(name="居間")})

    outcome, _label, _attempted = _transcribe_message(
        row, persona=reader, manager=None, persona_id="mio", building_id="room",
        history_manager=history_manager, id_to_name_map={}, check_provenance=False,
    )

    assert outcome == "ingested"
    (entry,) = captured
    assert entry["role"] == "system"
    assert entry["content"] == "[居間] (エラーにより、エリスは発言できませんでした)"
    dumped = json.dumps(entry, ensure_ascii=False)
    for leaked in (NO_UTTERANCE_DETAIL_KEY, "safety_filter", "安全性", "HARM_CATEGORY", "m1"):
        assert leaked not in dumped


def test_the_chat_history_api_carries_the_detail_outside_content_and_metadata():
    from api.routes.chat import serialize_history_message

    manager = SimpleNamespace(personas={}, user_display_name="U", state=SimpleNamespace())
    msg = {
        "role": "host",
        "content": "(エラーにより、エリスは発言できませんでした)",
        "metadata": {NO_UTTERANCE_DETAIL_KEY: failure_detail("rate_limit", message="上限", model="m1")},
    }
    out = serialize_history_message(manager, msg, "room:5", building_id="room")
    assert out.failure_detail is not None
    assert out.failure_detail.error_code == "rate_limit"
    assert out.failure_detail.model == "m1"
    assert out.content == msg["content"]

    plain = serialize_history_message(
        manager, {"role": "host", "content": "入室", "metadata": {}}, "room:6",
    )
    assert plain.failure_detail is None


# ---------------------------------------------------------------------------
# 自動でやり直す経路: やり直しが尽きたときに一度だけ
# ---------------------------------------------------------------------------


def _schedule(type_="oneshot"):
    return SimpleNamespace(
        SCHEDULE_ID=7, SCHEDULE_TYPE=type_, PERSONA_ID="eris", DESCRIPTION="朝の挨拶",
    )


def test_schedule_exhaustion_writes_one_row_per_given_up_occurrence():
    from saiverse.schedule_manager import (
        DISPATCH_DETAIL_GATE_CLOSED,
        SCHEDULE_DISPATCH_MAX_ATTEMPTS,
        ScheduleManager,
    )

    _controller, manager, persona = _world()
    manager.event_scheduler = MagicMock()
    sm = ScheduleManager(manager)
    schedule = _schedule()

    # やり直しが残っている間は置かない
    sm._retry_or_give_up(schedule, None, "tok", "occ1", 0, 1, DISPATCH_DETAIL_GATE_CLOSED)
    assert _host_rows(persona) == []
    # 尽きたら一行
    sm._retry_or_give_up(
        schedule, None, "tok", "occ1", 0, SCHEDULE_DISPATCH_MAX_ATTEMPTS,
        DISPATCH_DETAIL_GATE_CLOSED,
    )
    (_bid, row, heard_by), = _host_rows(persona)
    assert row["content"] == "(エラーにより、エリスは発言できませんでした)"
    assert set(heard_by) == {"eris", "mio"}
    assert row["metadata"][NO_UTTERANCE_DETAIL_KEY]["error_code"] == "beat_gate_closed"
    # reconciliation が同じ occurrence を拾い直して再び尽きても二行目は出ない
    sm._retry_or_give_up(
        schedule, None, "tok", "occ1", 0, SCHEDULE_DISPATCH_MAX_ATTEMPTS,
        DISPATCH_DETAIL_GATE_CLOSED,
    )
    assert len(_host_rows(persona)) == 1
    # 別の occurrence はまた一行
    sm._retry_or_give_up(
        schedule, None, "tok", "occ2", 0, SCHEDULE_DISPATCH_MAX_ATTEMPTS,
        DISPATCH_DETAIL_GATE_CLOSED,
    )
    assert len(_host_rows(persona)) == 2


@pytest.mark.parametrize("detail,cause", [
    ("persona not found", "persona_not_found"),
    ("persona has no current_building_id", "no_location"),
])
def test_schedule_exhaustion_without_a_room_posts_a_runtime_alert_once(detail, cause):
    from saiverse.schedule_manager import SCHEDULE_DISPATCH_MAX_ATTEMPTS, ScheduleManager

    _controller, manager, persona = _world()
    manager.event_scheduler = MagicMock()
    sm = ScheduleManager(manager)
    with patch("saiverse.runtime_alerts.post_runtime_alert") as post:
        for _ in range(2):
            sm._retry_or_give_up(
                _schedule(), None, "tok", "occ1", 0, SCHEDULE_DISPATCH_MAX_ATTEMPTS, detail,
            )
    assert post.call_count == 1
    key, level, title, message = post.call_args.args[:4]
    assert key == f"schedule_unrunnable:7:{cause}"
    assert level == "error"
    assert title["$ui"] == "api.runtimeAlerts.scheduleUnrunnable.title"
    assert message["params"] == {"persona": "エリス", "schedule": "朝の挨拶"}
    assert _host_rows(persona) == []


def test_recovered_event_response_exhaustion_writes_one_row():
    from saiverse import autonomy_wiring

    _controller, manager, persona = _world()
    failure = {"cause": "window floor unmet", "building_id": "room"}
    autonomy_wiring._schedule_recovered_response_retry(
        manager, "eris", {}, "exec-1",
        autonomy_wiring.RECOVERED_RESPONSE_MAX_ATTEMPTS + 1, failure=failure,
    )
    autonomy_wiring._schedule_recovered_response_retry(
        manager, "eris", {}, "exec-1",
        autonomy_wiring.RECOVERED_RESPONSE_MAX_ATTEMPTS + 1, failure=failure,
    )
    (_bid, row, _heard), = _host_rows(persona)
    assert row["metadata"][NO_UTTERANCE_DETAIL_KEY]["error_code"] == "window_floor_unmet"


def test_recovered_event_response_records_why_it_could_not_start():
    from saiverse import autonomy_wiring

    manager = SimpleNamespace(pulse_dispatcher=MagicMock(), all_personas={})
    failure: dict = {}
    assert autonomy_wiring._dispatch_recovered_event_response(
        manager, "ghost", {}, failure=failure,
    ) == autonomy_wiring.RECOVERED_DISPATCH_SAFE_FAILURE
    assert failure == {"cause": "persona not found"}


def test_an_external_event_for_a_missing_persona_posts_a_runtime_alert():
    from builtin_data.phenomena.inject_persona_event import inject_persona_event

    manager = SimpleNamespace(
        record_persona_event=MagicMock(), pulse_dispatcher=MagicMock(), all_personas={},
    )
    with patch("saiverse.runtime_alerts.post_runtime_alert") as post:
        result = inject_persona_event("ghost", "mention", _manager=manager)
    assert result.startswith("error:")
    assert post.call_args.args[0] == "event_unrunnable:ghost:persona_not_found"


def test_a_conversation_that_cannot_start_posts_a_runtime_alert():
    from saiverse.user_conversation import _start_main_line_pulse

    persona = SimpleNamespace(persona_id="eris", persona_name="エリス", current_building_id=None)
    manager = SimpleNamespace(personas={"eris": persona}, all_personas={"eris": persona})
    with patch("saiverse.runtime_alerts.post_runtime_alert") as post:
        _start_main_line_pulse(manager, "eris")
    assert post.call_args.args[0] == "conversation_unrunnable:eris:no_location"


# ---------------------------------------------------------------------------
# あわせて直す不具合
# ---------------------------------------------------------------------------


def test_the_user_lane_continues_to_the_second_persona_after_an_llm_error():
    from tests.test_user_utterance_durability import _run, _runtime

    first = SimpleNamespace(persona_id="p1")
    second = SimpleNamespace(persona_id="p2")
    service = _runtime([first, second])
    reached = []

    def _dispatch(**kwargs):
        reached.append(kwargs["persona_id"])
        if kwargs["persona_id"] == "p1":
            raise LLMError("boom", user_message="LLMでエラーが発生しました")
        cb = service.manager._active_sse_callbacks["room"]
        cb({"type": "say", "persona_id": "p2", "content": "こんにちは"})

    service.manager.pulse_dispatcher.dispatch_user_utterance = MagicMock(side_effect=_dispatch)
    events = _run(service)

    assert reached == ["p1", "p2"]
    assert any(e.get("type") == "error" and e.get("error_code") == "llm_error" for e in events)
    assert any(e.get("type") == "say" and e.get("persona_id") == "p2" for e in events)


def test_the_phenomenon_manager_logs_an_error_result_as_a_failure(caplog):
    from phenomena.manager import PhenomenonManager

    pm = PhenomenonManager(session_factory=MagicMock(), async_execution=False)
    with patch.dict("phenomena.manager.PHENOMENON_REGISTRY",
                    {"fake": lambda **_: "error: pulse submission failed"}), \
            caplog.at_level(logging.INFO, logger="phenomena.manager"):
        assert pm._execute_phenomenon("fake", {}) == "error: pulse submission failed"
    messages = [r.getMessage() for r in caplog.records]
    assert any("reported a failure" in m for m in messages)
    assert not any("completed successfully" in m for m in messages)

    caplog.clear()
    with patch.dict("phenomena.manager.PHENOMENON_REGISTRY", {"fake": lambda **_: "ok"}), \
            caplog.at_level(logging.INFO, logger="phenomena.manager"):
        pm._execute_phenomenon("fake", {})
    assert any("completed successfully" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("text", [
    "Error code: 400 - {'error': {'message': \"This model's maximum context length is "
    "128000 tokens. However, your messages resulted in 159608 tokens.\", "
    "'code': 'context_length_exceeded'}}",
    "the request exceeds the available context size, try increasing it",
])
def test_openai_classifies_a_context_overflow(text):
    from llm_clients.openai_errors import convert_to_llm_error

    converted = convert_to_llm_error(Exception(text))
    assert isinstance(converted, ContextLengthExceededError)
    assert "モデル" in converted.user_message


def test_anthropic_classifies_a_context_overflow():
    from llm_clients.anthropic_retry_policy import _convert_to_llm_error

    converted = _convert_to_llm_error(Exception("prompt is too long: 210000 tokens > 200000 maximum"))
    assert isinstance(converted, ContextLengthExceededError)


def test_gemini_classifies_a_context_overflow():
    from llm_clients.gemini import GeminiClient

    err = Exception(
        "400 INVALID_ARGUMENT. The input token count (1200000) exceeds the maximum "
        "number of tokens allowed (1048576)."
    )
    converted = GeminiClient._convert_to_llm_error(SimpleNamespace(), err)
    assert isinstance(converted, ContextLengthExceededError)


def test_xai_classifies_a_context_overflow():
    from llm_clients.xai import _convert_to_llm_error

    err = Exception("This model's maximum prompt length is 131072 but the request contains 140000 tokens.")
    assert isinstance(_convert_to_llm_error(err), ContextLengthExceededError)


def test_an_ordinary_bad_request_is_not_a_context_overflow():
    from llm_clients.openai_errors import convert_to_llm_error

    converted = convert_to_llm_error(Exception("Invalid value for 'temperature'"))
    assert not isinstance(converted, ContextLengthExceededError)


@pytest.mark.parametrize("outcome", ["gate_closed", "floor_unmet"])
def test_a_direct_external_event_that_could_not_start_writes_a_row_immediately(outcome):
    """直接の外部イベントの応答には自動のやり直しが無いので、始められなかった回は
    その場で部屋の記録へ一行置く (問い 1 — やり直しの無い経路はその場で置く)。"""
    from saiverse.pulse_dispatcher import PulseDispatcher

    _controller, manager, persona = _world()
    dispatcher = PulseDispatcher(manager)
    with patch.object(
        dispatcher, "dispatch_schedule_fire",
        return_value={"action": "executed", "runtime_outcome": outcome, "error": "closed"},
    ):
        ok = dispatcher.dispatch_phenomenon_event("eris", "room", "event")

    assert ok is False
    rows = _host_rows(persona)
    assert len(rows) == 1
    building_id, row, heard_by = rows[0]
    assert building_id == "room"
    assert row["content"] == "(エラーにより、エリスは発言できませんでした)"
    assert set(heard_by) == {"eris", "mio"}


def test_a_direct_external_event_that_ran_writes_nothing_from_the_dispatcher():
    from saiverse.pulse_dispatcher import PulseDispatcher

    _controller, manager, persona = _world()
    dispatcher = PulseDispatcher(manager)
    with patch.object(
        dispatcher, "dispatch_schedule_fire",
        return_value={"action": "executed", "runtime_outcome": "completed", "error": None},
    ):
        assert dispatcher.dispatch_phenomenon_event("eris", "room", "event") is True

    assert _host_rows(persona) == []


def test_schedule_exhaustion_retries_the_notice_when_writing_it_failed():
    """行を書けなかった回は「出した」印を外し、拾い直したときにもう一度試す
    (Codex 2 巡目 — 一時的な書き込みの失敗で知らせを永久に失わない)。"""
    from saiverse.schedule_manager import (
        DISPATCH_DETAIL_GATE_CLOSED,
        SCHEDULE_DISPATCH_MAX_ATTEMPTS,
        ScheduleManager,
    )

    _controller, manager, persona = _world()
    manager.event_scheduler = MagicMock()
    sm = ScheduleManager(manager)
    schedule = _schedule()
    with patch(
        "sea.no_utterance_notice.record_no_utterance_notice", side_effect=[False, True],
    ) as record:
        for _ in range(3):
            sm._retry_or_give_up(
                schedule, None, "tok", "occ1", 0, SCHEDULE_DISPATCH_MAX_ATTEMPTS,
                DISPATCH_DETAIL_GATE_CLOSED,
            )
    # 1 回目は失敗 → 2 回目で書けた → 3 回目は出さない
    assert record.call_count == 2


def test_recovered_response_retries_the_notice_when_writing_it_failed():
    from saiverse import autonomy_wiring
    from saiverse.schedule_manager import DISPATCH_DETAIL_GATE_CLOSED

    _controller, manager, _persona = _world()
    autonomy_wiring._RECOVERED_GIVEN_UP_REPORTED.clear()
    failure = {"cause": DISPATCH_DETAIL_GATE_CLOSED, "building_id": "room"}
    with patch(
        "sea.no_utterance_notice.record_no_utterance_notice", side_effect=[False, True],
    ) as record:
        for _ in range(3):
            autonomy_wiring._report_recovered_response_given_up(
                manager, "eris", "exec-1", failure,
            )
    assert record.call_count == 2
    autonomy_wiring._RECOVERED_GIVEN_UP_REPORTED.clear()
