"""生成の段階の信号 (v0.3 リリース門 H / I / J) の検証。

- H: ``emit_speak_finalize`` は三値の結果を返し、保存できた回だけ
  保存完了イベント (``speak_persisted``) が流れる。
  設計: docs/issues/stream_completion_is_not_proof_of_persistence.md
- I: ``client_message_id`` で発言の保存結果を問い合わせる読み取りの口。
  「分からない」を潰さない。
  設計: docs/issues/unknown_send_outcome_has_no_recovery_path.md
- J: retry の門番 — 対象の発言より後にペルソナの発言行があれば拒否。
  判定は I と同じ一比較を共有する。
  設計: docs/issues/retry_api_has_no_server_side_eligibility_check.md
"""
from __future__ import annotations

import gc
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.building_messages import (
    assistant_reply_exists_after,
    insert_building_message,
    lookup_client_message_outcome,
)
from database.models import Base
from sea import runtime_llm
from sea.runtime_emitters import RuntimeEmitters, SpeakFinalizeResult


# ---------------------------------------------------------------------------
# H: emit_speak_finalize の三値の結果
# ---------------------------------------------------------------------------


def _emitters() -> RuntimeEmitters:
    manager = SimpleNamespace(
        occupants={},
        user_presence_status="offline",
        gateway_handle_ai_replies=MagicMock(),
    )
    return RuntimeEmitters(runtime=SimpleNamespace(manager=manager))


def _persona_with(update_result) -> SimpleNamespace:
    history = MagicMock()
    history.update_building_message.return_value = update_result
    history.add_to_persona_only.return_value = ("skipped", None)
    return SimpleNamespace(persona_id="p1", history_manager=history)


def test_finalize_reports_saved_with_the_row_id() -> None:
    persona = _persona_with({
        "message_id": "room:5",
        "metadata": {"_streaming_placeholder": False},
    })
    result = _emitters().emit_speak_finalize(persona, "room", "room:5", "こんにちは")

    assert isinstance(result, SpeakFinalizeResult)
    assert result.status == "saved"
    assert result.saved_message_id == "room:5"


def test_finalize_reports_a_missing_placeholder() -> None:
    """対象行なしは「保存失敗」と別の値 — 呼び出し元が二つを混ぜないため。"""
    persona = _persona_with(None)
    result = _emitters().emit_speak_finalize(persona, "room", "room:5", "こんにちは")

    assert result.status == "missing"
    assert result.saved_message_id is None


def test_finalize_reports_a_failure_with_the_exception() -> None:
    """例外は握り潰さず、結果へ写して返す (上へは投げない)。"""
    persona = _persona_with(None)
    persona.history_manager.update_building_message.side_effect = RuntimeError("db down")

    result = _emitters().emit_speak_finalize(persona, "room", "room:5", "こんにちは")

    assert result.status == "failed"
    assert "db down" in (result.error or "")
    assert result.saved_message_id is None


def test_finalize_does_not_call_a_write_that_did_not_land_saved() -> None:
    """更新後の行がまだ下書きの印を付けたままなら、保存できたとは言わない。

    ``update_building_message`` は DB エラーを内側で握って戻るので、
    「呼べた」と「載った」は別 — 読み直した行の姿で検算する。
    """
    persona = _persona_with({
        "message_id": "room:5",
        "metadata": {"_streaming_placeholder": True},
    })
    result = _emitters().emit_speak_finalize(persona, "room", "room:5", "こんにちは")

    assert result.status == "failed"
    assert result.saved_message_id is None


def test_finalize_stays_saved_when_the_persona_record_fails() -> None:
    """status は建物の行の永続化の成否だけで決める (2026-08-29 裁定)。

    ペルソナ履歴の追加で落ちても saved を覆さない — 信号の契約は「建物履歴の
    行に本文が入った」で、続きの印・retry の門番が見るのはそこ。副作用の失敗で
    failed に降格すると、行は入っているのに「保存されていない」と報告する
    ことになり、受け手の見る事実と食い違う (Codex #3 の裁定版)。
    """
    persona = _persona_with({
        "message_id": "room:5",
        "content": "こんにちは",
        "metadata": {"_streaming_placeholder": False},
    })
    persona.history_manager.add_to_persona_only.side_effect = RuntimeError("log broken")

    result = _emitters().emit_speak_finalize(persona, "room", "room:5", "こんにちは")

    assert result.status == "saved"
    assert result.saved_message_id == "room:5"


def test_finalize_stays_saved_when_gateway_delivery_fails() -> None:
    """gateway 配信の失敗も同じ — ERROR ログに残すが saved は覆さない。"""
    emitters = _emitters()
    emitters.runtime.manager.gateway_handle_ai_replies.side_effect = RuntimeError("gw down")
    persona = _persona_with({
        "message_id": "room:5",
        "content": "こんにちは",
        "metadata": {"_streaming_placeholder": False},
    })

    result = emitters.emit_speak_finalize(persona, "room", "room:5", "こんにちは")

    assert result.status == "saved"


# ---------------------------------------------------------------------------
# H: 保存完了イベントは保存できた回だけ流れる
# ---------------------------------------------------------------------------


def _runtime_returning(result: SpeakFinalizeResult) -> SimpleNamespace:
    return SimpleNamespace(_emit_speak_finalize=MagicMock(return_value=result))


def _finalize(runtime, text: str, events: list) -> None:
    runtime_llm._finalize_speak_with_signal(
        runtime, SimpleNamespace(persona_id="p1"), "room", "room:5", text,
        pulse_id="pl", extra_metadata=None, final_sub_seq=1,
        event_callback=events.append,
    )


def test_the_persistence_signal_is_emitted_after_a_successful_save() -> None:
    events: list = []
    runtime = _runtime_returning(SpeakFinalizeResult(
        status="saved",
        building_msg={"message_id": "room:5", "content": "こんにちは"},
    ))

    _finalize(runtime, "こんにちは", events)

    assert events == [{
        "type": "speak_persisted",
        "message_id": "room:5",
        "persona_id": "p1",
        "pulse_id": "pl",
    }]


def test_no_signal_when_the_save_failed() -> None:
    events: list = []
    runtime = _runtime_returning(SpeakFinalizeResult(
        status="failed", error="boom",
    ))

    _finalize(runtime, "こんにちは", events)

    assert events == []


def test_no_signal_when_the_placeholder_was_missing() -> None:
    events: list = []
    runtime = _runtime_returning(SpeakFinalizeResult(status="missing"))

    _finalize(runtime, "こんにちは", events)

    assert events == []


def test_no_signal_for_an_empty_finalize() -> None:
    """空のまま確定した回 (下書き行を閉じただけ) は発言が生まれていない。

    ここで信号を流すと、続きが一言も生まれていないのに続きの生成の印が
    降り、続きを取る手段が消える。
    """
    events: list = []
    runtime = _runtime_returning(SpeakFinalizeResult(
        status="saved", building_msg={"message_id": "room:5", "content": ""},
    ))

    _finalize(runtime, "", events)

    assert events == []


def test_the_signal_judges_the_saved_content_not_the_raw_text() -> None:
    """発火条件は「実際に保存された本文」の非空 (Codex #6)。

    渡したテキストは非空でも、正規化 (心内文の除去等) の後に空で行が確定
    した回は発言ではない — 共有判定 (retry の門番) も空行を発言に数えない
    ので、ここで信号を流すと受け手と判定がずれる。
    """
    events: list = []
    runtime = _runtime_returning(SpeakFinalizeResult(
        status="saved", building_msg={"message_id": "room:5", "content": ""},
    ))

    # 生テキストは非空 (心内文だけの応答を模す) — それでも信号は流れない。
    _finalize(runtime, "<in_heart>言わないでおこう</in_heart>", events)

    assert events == []


def test_finalize_normalizes_before_saving_and_the_row_carries_the_content() -> None:
    """心内文だけの応答は、建物の行に空の本文で確定される (実物の正規化)。

    update_building_message に渡る content が除去後に空であること = 上の
    「保存本文が空なら信号なし」と合わせて、心内文だけの応答で信号が
    流れない実経路の検算。
    """
    persona = _persona_with({
        "message_id": "room:5",
        "content": "",
        "metadata": {"_streaming_placeholder": False},
    })
    _emitters().emit_speak_finalize(
        persona, "room", "room:5", "<in_heart>言わないでおこう</in_heart>",
    )

    kwargs = persona.history_manager.update_building_message.call_args.kwargs
    assert kwargs["content"] == ""


# ---------------------------------------------------------------------------
# I / J: 「その発言より後にペルソナの発言行があるか」の共有判定 (実 DB)
# ---------------------------------------------------------------------------


@pytest.fixture()
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    yield factory
    engine.dispose()
    gc.collect()


def _insert(factory, building_id, role, content, *, persona_id=None,
            client_message_id=None, metadata=None):
    msg = {"role": role, "content": content}
    if persona_id:
        msg["persona_id"] = persona_id
    if client_message_id:
        msg["client_message_id"] = client_message_id
    if metadata:
        msg["metadata"] = metadata
    saved = insert_building_message(factory, building_id, msg)
    assert saved is not None
    return saved


def test_a_later_assistant_row_counts_as_a_reply(session_factory) -> None:
    user = _insert(session_factory, "room", "user", "こんにちは")
    _insert(session_factory, "room", "assistant", "やあ", persona_id="p1")

    assert assistant_reply_exists_after(
        session_factory, "room", user["message_id"],
    ) == ("found", True)


def test_no_later_assistant_row_means_no_reply(session_factory) -> None:
    _insert(session_factory, "room", "assistant", "先の発言", persona_id="p1")
    user = _insert(session_factory, "room", "user", "こんにちは")

    assert assistant_reply_exists_after(
        session_factory, "room", user["message_id"],
    ) == ("found", False)


def test_an_empty_assistant_row_is_not_a_reply(session_factory) -> None:
    """content が空の行 (ストリーミングの下書き) はまだ発言ではない。"""
    user = _insert(session_factory, "room", "user", "こんにちは")
    _insert(
        session_factory, "room", "assistant", "", persona_id="p1",
        metadata={"_streaming_placeholder": True},
    )

    assert assistant_reply_exists_after(
        session_factory, "room", user["message_id"],
    ) == ("found", False)


def test_a_reply_in_another_building_does_not_count(session_factory) -> None:
    user = _insert(session_factory, "room", "user", "こんにちは")
    _insert(session_factory, "other", "assistant", "別の部屋の話", persona_id="p1")

    assert assistant_reply_exists_after(
        session_factory, "room", user["message_id"],
    ) == ("found", False)


def test_a_missing_message_is_not_the_same_as_unreadable(session_factory) -> None:
    assert assistant_reply_exists_after(
        session_factory, "room", "room:404",
    ) == ("not_found", False)
    assert assistant_reply_exists_after(None, "room", "room:1") == (
        "unavailable", False,
    )


def test_lookup_by_client_message_id_returns_the_three_values(session_factory) -> None:
    user = _insert(
        session_factory, "room", "user", "こんにちは", client_message_id="cmid-1",
    )

    found = lookup_client_message_outcome(session_factory, "cmid-1")
    assert found == {
        "status": "found",
        "message_id": user["message_id"],
        "has_reply": False,
    }

    _insert(session_factory, "room", "assistant", "やあ", persona_id="p1")
    replied = lookup_client_message_outcome(session_factory, "cmid-1")
    assert replied["has_reply"] is True

    assert lookup_client_message_outcome(session_factory, "cmid-404") == {
        "status": "not_found",
    }
    # 照会できない回は「分からない」— 「残っていない」と混ぜない。
    assert lookup_client_message_outcome(None, "cmid-1") == {"status": "unknown"}


# ---------------------------------------------------------------------------
# I: /chat/message-outcome の口 / J: /chat/retry の門番 (route 関数を直接呼ぶ)
# ---------------------------------------------------------------------------


def _manager(session_factory, building_id="room"):
    return SimpleNamespace(
        state=SimpleNamespace(user_current_building_id=building_id),
        SessionLocal=session_factory,
        retry_user_message_stream=MagicMock(
            return_value=iter(['{"type": "say"}\n']),
        ),
    )


def test_message_outcome_endpoint_reports_found_and_reply(session_factory) -> None:
    from api.routes.chat import get_message_outcome

    user = _insert(
        session_factory, "room", "user", "こんにちは", client_message_id="cmid-1",
    )
    _insert(session_factory, "room", "assistant", "やあ", persona_id="p1")

    body = get_message_outcome("cmid-1", _manager(session_factory))
    assert body.status == "found"
    assert body.message_id == user["message_id"]
    assert body.has_reply is True


def test_message_outcome_endpoint_does_not_flatten_unknown(session_factory) -> None:
    from api.routes.chat import get_message_outcome

    missing = get_message_outcome("cmid-404", _manager(session_factory))
    assert missing.status == "not_found"

    # SessionLocal を持たない manager = 照会できない。「分からない」のまま返す。
    unknown = get_message_outcome(
        "cmid-1", SimpleNamespace(state=SimpleNamespace(user_current_building_id="room")),
    )
    assert unknown.status == "unknown"


def test_retry_is_refused_after_a_later_assistant_row(session_factory) -> None:
    from api.routes.chat import MessageActionRequest, retry_message

    user = _insert(session_factory, "room", "user", "こんにちは")
    _insert(session_factory, "room", "assistant", "やあ", persona_id="p1")
    manager = _manager(session_factory)

    with pytest.raises(HTTPException) as exc:
        retry_message(MessageActionRequest(message_id=user["message_id"]), manager)

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "already_replied"
    assert "既にペルソナの発言があります" in exc.value.detail["message"]
    manager.retry_user_message_stream.assert_not_called()


def test_retry_proceeds_when_no_reply_exists(session_factory) -> None:
    from api.routes.chat import MessageActionRequest, retry_message

    user = _insert(session_factory, "room", "user", "こんにちは")
    manager = _manager(session_factory)

    response = retry_message(
        MessageActionRequest(message_id=user["message_id"]), manager,
    )

    assert response.status_code == 200


# ---------------------------------------------------------------------------
# H: 保存する全経路が信号を流す (Codex #1) — emit_say / emit_speak / 各配線
# ---------------------------------------------------------------------------


def _history_returning(row) -> MagicMock:
    history = MagicMock()
    history.add_to_building_only.return_value = row
    history.add_to_persona_only.return_value = ("skipped", None)
    return history


def _say_persona(row) -> SimpleNamespace:
    return SimpleNamespace(persona_id="p1", history_manager=_history_returning(row))


def test_emit_say_fires_the_signal_after_a_successful_insert() -> None:
    """直接の建物書き込み (emit_say) も保存完了イベントを流す。

    かつては pipeline streaming の finalize だけが信号を流し、この経路は
    黙っていた — 続きの印の受け手から見ると「保存されたのに信号が来ない」
    穴になる (Codex #1)。
    """
    events: list = []
    persona = _say_persona({"message_id": "room:9", "content": "こんにちは"})

    _emitters().emit_say(
        persona, "room", "こんにちは", pulse_id="pl",
        event_callback=events.append,
    )

    assert [e for e in events if e["type"] == "speak_persisted"] == [{
        "type": "speak_persisted",
        "message_id": "room:9",
        "persona_id": "p1",
        "pulse_id": "pl",
    }]


def test_emit_say_stays_silent_when_the_insert_did_not_land() -> None:
    """message_id が付かない戻り値 = insert は通っていない。信号は流さない。

    HistoryManager は insert 失敗でも渡した dict をそのまま返すため、
    dict の有無では保存を判定できない — 判定は DB 採番の id の有無。
    """
    events: list = []
    persona = _say_persona({"role": "assistant", "content": "こんにちは"})

    _emitters().emit_say(
        persona, "room", "こんにちは", pulse_id="pl",
        event_callback=events.append,
    )

    assert [e for e in events if e["type"] == "speak_persisted"] == []


def test_emit_say_stays_silent_for_an_empty_saved_content() -> None:
    """保存された本文が空の行 (心内文だけの応答) は発言に数えない (Codex #6)。"""
    events: list = []
    persona = _say_persona({"message_id": "room:9", "content": ""})

    _emitters().emit_say(
        persona, "room", "<in_heart>内心</in_heart>", pulse_id="pl",
        event_callback=events.append,
    )

    assert [e for e in events if e["type"] == "speak_persisted"] == []


def test_emit_speak_fires_the_signal_after_a_successful_insert() -> None:
    """SPEAK ノードの経路 (emit_speak) も同じ共通の口で信号を流す。"""
    events: list = []
    persona = _say_persona({"message_id": "room:9", "content": "こんにちは"})

    _emitters().emit_speak(
        persona, "room", "こんにちは", pulse_id="pl",
        event_callback=events.append,
    )

    assert [e for e in events if e["type"] == "speak_persisted"] == [{
        "type": "speak_persisted",
        "message_id": "room:9",
        "persona_id": "p1",
        "pulse_id": "pl",
    }]


def test_emit_say_and_capture_forwards_the_event_callback() -> None:
    """非ストリーミング・tool 経路・placeholder fallback の共通ヘルパは
    event_callback を emit_say まで運ぶ (運ばないとその 4 経路だけ信号が
    欠ける)。"""
    from sea.runtime_llm import _emit_say_and_capture

    runtime = SimpleNamespace(_emit_say=MagicMock(return_value={"message_id": "room:9"}))
    cb = MagicMock()

    _emit_say_and_capture(
        runtime, SimpleNamespace(persona_id="p1"), "room", "こんにちは", {},
        pulse_id="pl", event_callback=cb,
    )

    assert runtime._emit_say.call_args.kwargs["event_callback"] is cb


def test_lg_speak_node_forwards_the_event_callback() -> None:
    """Playbook の SPEAK ノードは emitter へ event_callback を渡す。"""
    from sea.runtime_engine import RuntimeEngine

    speak = MagicMock(return_value={"message_id": "room:9"})
    engine = RuntimeEngine(
        runtime=SimpleNamespace(_effective_building_id=lambda p, b: b),
        manager_ref=SimpleNamespace(),
        llm_selector=MagicMock(),
        emitters={"speak": speak},
    )
    cb = MagicMock()

    engine.lg_speak_node(
        {"last": "こんにちは"}, SimpleNamespace(persona_id="p1"), "room",
        SimpleNamespace(name="pb"), outputs=[], event_callback=cb,
    )

    assert speak.call_args.kwargs["event_callback"] is cb


def test_lg_say_node_emits_the_signal_through_the_real_wiring() -> None:
    """SAY ノード → _emit_say → 共通の口、の実配線で信号が届く。"""
    import asyncio
    from sea.runtime import SEARuntime

    manager = SimpleNamespace(
        building_histories={"room": []},
        occupants={"room": ["p1"]},
        user_presence_status="online",
        gateway_handle_ai_replies=MagicMock(),
        unity_gateway=None,
    )
    runtime = SEARuntime(manager)
    persona = SimpleNamespace(
        persona_id="p1",
        history_manager=SimpleNamespace(
            add_to_building_only=MagicMock(
                return_value={"message_id": "room:9", "content": "こんにちは"},
            ),
        ),
    )
    events: list = []
    node = runtime._lg_say_node(
        SimpleNamespace(id="say", metadata_key=None),
        persona, "room", SimpleNamespace(name="pb"),
        outputs=[], event_callback=events.append,
    )

    asyncio.run(node({"last": "こんにちは", "_pulse_id": "pl"}))

    assert [e for e in events if e["type"] == "speak_persisted"] == [{
        "type": "speak_persisted",
        "message_id": "room:9",
        "persona_id": "p1",
        "pulse_id": "pl",
    }]


# ---------------------------------------------------------------------------
# J: retry 門番の再検査 (Codex #5a) — 生成の直前・Beat ロックの内側
# ---------------------------------------------------------------------------


def test_run_meta_user_stops_at_the_pre_generation_check() -> None:
    """再検査がイベントを返したら、Pulse は開始されずそのイベントだけ流れる。

    検査はロック取得の直後・いかなる副作用よりも前に走る — ここで中止した
    Pulse は知覚の検知・消費も履歴書き込みも行わない (この fake manager は
    それらの口を持たないので、走れば AttributeError で落ちる)。
    """
    from sea.runtime import SEARuntime

    runtime = SEARuntime(SimpleNamespace())
    events: list = []
    blocked = {
        "type": "error", "error_code": "already_replied",
        "content": "この発言の後に既にペルソナの発言があります。",
    }

    result = runtime.run_meta_user(
        SimpleNamespace(persona_id="p1"), "", "room",
        event_callback=events.append,
        pre_generation_check=lambda: blocked,
    )

    assert result == []
    assert events == [blocked]


def test_pulse_controller_forwards_the_pre_generation_check() -> None:
    """再検査は ExecutionRequest に載って実行時 (待機列からの繰り上げ後でも)
    に run_meta_user へ届く。"""
    from sea.pulse_controller import ExecutionRequest, PulseController

    persona = SimpleNamespace(persona_id="p1")
    sea_runtime = SimpleNamespace(
        run_meta_user=MagicMock(return_value=[]),
        manager=SimpleNamespace(all_personas={"p1": persona}),
    )
    controller = PulseController(sea_runtime)

    def check():
        return None

    controller._do_execute(ExecutionRequest(
        type="user", persona_id="p1", building_id="room", user_input="",
        pre_generation_check=check,
    ))

    assert sea_runtime.run_meta_user.call_args.kwargs["pre_generation_check"] is check


def _retry_service(session_factory):
    """retry の再検査の閉包を捕まえる最小の土台。"""
    from manager.runtime import RuntimeService

    service = RuntimeService.__new__(RuntimeService)
    service.state = SimpleNamespace(user_id="u1", user_current_building_id="room")
    service.SessionLocal = session_factory
    service.personas = {}
    responder = SimpleNamespace(persona_id="p1")
    service._build_responding_personas = lambda building_id: [responder]
    service._find_building_message = lambda b, m: ({"role": "user"}, "found")
    captured: dict = {}

    def _run_sea_user(persona, building_id, user_input,
                      event_callback=None, pre_generation_check=None, **kw):
        captured["check"] = pre_generation_check
        if event_callback:
            event_callback({"type": "say", "content": "x", "persona_id": "p1"})

    service.manager = SimpleNamespace(
        _active_stop_events={},
        _active_sse_callbacks={},
        run_sea_user=_run_sea_user,
    )
    return service, captured


def test_retry_recheck_runs_the_shared_judgment_right_before_generation(session_factory) -> None:
    """retry は生成の直前に走る再検査を Pulse へ渡し、その再検査は route の
    門番と同じ共有判定を引く。応答が居たら already_replied、居なければ通す。"""
    user = _insert(session_factory, "room", "user", "こんにちは")
    service, captured = _retry_service(session_factory)

    list(service.retry_user_message_stream(user["message_id"]))

    check = captured["check"]
    assert check is not None

    # まだ応答が無い → 通す。
    assert check() is None

    # 門番の後・生成の前に応答が保存された競合を模す → その場で中止。
    _insert(session_factory, "room", "assistant", "やあ", persona_id="p1")
    blocked = check()
    assert blocked is not None
    assert blocked["error_code"] == "already_replied"


def test_retry_recheck_is_fail_closed_when_the_judgment_is_unavailable(session_factory) -> None:
    """再検査も route と同じく fail-closed — 判定不能で生成を始めない。"""
    user = _insert(session_factory, "room", "user", "こんにちは")
    service, captured = _retry_service(session_factory)
    list(service.retry_user_message_stream(user["message_id"]))

    service.SessionLocal = None
    blocked = captured["check"]()
    assert blocked is not None
    assert blocked["error_code"] == "history_unavailable"


def test_retry_is_refused_when_the_check_is_unavailable(session_factory) -> None:
    """判定不能 (unavailable) は通さない (fail-closed、Codex #5b の裁定版)。

    共有判定の docstring のとおり、読めなかった回に「応答なし」と断定すると
    既に答えた発言へもう一度応答を起こす扉が開く。待たされる側の害は
    「少し待ってもう一度」で回復できるが、二重応答は永続して取り消せない。
    """
    from api.routes.chat import MessageActionRequest, retry_message

    manager = _manager(None)

    with pytest.raises(HTTPException) as exc:
        retry_message(MessageActionRequest(message_id="room:1"), manager)

    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "history_unavailable"
    assert "少し待ってもう一度" in exc.value.detail["message"]
    manager.retry_user_message_stream.assert_not_called()
