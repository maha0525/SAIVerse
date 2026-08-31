from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from manager.runtime import RuntimeService


def _runtime(responders=None) -> RuntimeService:
    service = RuntimeService.__new__(RuntimeService)
    service.state = SimpleNamespace(
        user_id="user_1",
        user_current_building_id="room",
    )
    service.SessionLocal = MagicMock(name="SessionLocal")
    service.personas = {}
    service.occupants = {}
    service.building_map = {}
    service._canonical_building_id = lambda building_id: building_id
    service._build_responding_personas = lambda building_id: list(responders or [])
    service._save_modified_buildings = lambda: None
    service.manager = SimpleNamespace(
        _active_stop_events={},
        _active_sse_callbacks={},
        pulse_dispatcher=MagicMock(),
        cancel_active_generation=MagicMock(),
    )
    return service


def _events(lines) -> list[dict]:
    return [json.loads(line) for line in lines if line.strip()]


def test_stream_does_not_dispatch_when_user_insert_fails() -> None:
    persona = SimpleNamespace(persona_id="p1")
    service = _runtime([persona])

    with patch("database.building_messages.insert_building_message_with_location_guard", return_value=None):
        events = _events(
            service.handle_user_input_stream(
                "hello",
                building_id="room",
                client_message_id="cmd-1",
            )
        )

    assert any(event.get("error_code") == "persistence_failed" for event in events)
    service.manager.pulse_dispatcher.dispatch_user_utterance.assert_not_called()


def test_stream_persists_user_message_even_when_room_is_empty() -> None:
    service = _runtime([])
    saved = {
        "message_id": "room:1",
        "client_message_id": "cmd-1",
        "_was_inserted": True,
    }

    with patch(
        "database.building_messages.insert_building_message_with_location_guard",
        return_value=saved,
    ) as insert:
        events = _events(
            service.handle_user_input_stream(
                "hello",
                building_id="room",
                client_message_id="cmd-1",
            )
        )

    assert any(event.get("message_id") == "room:1" for event in events)
    assert insert.call_count == 1
    inserted_entry = insert.call_args.args[2]
    assert inserted_entry["content"] == "hello"
    assert inserted_entry["heard_by"] == ["user_1"]


def test_duplicate_command_returns_canonical_id_without_redispatch() -> None:
    persona = SimpleNamespace(persona_id="p1")
    service = _runtime([persona])
    existing = {
        "message_id": "room:7",
        "client_message_id": "cmd-1",
        "_was_inserted": False,
    }

    with patch(
        "database.building_messages.insert_building_message_with_location_guard",
        return_value=existing,
    ):
        events = _events(
            service.handle_user_input_stream(
                "hello",
                building_id="room",
                client_message_id="cmd-1",
            )
        )

    duplicate = next(event for event in events if event["type"] == "duplicate_command")
    assert duplicate["message_id"] == "room:7"
    service.manager.pulse_dispatcher.dispatch_user_utterance.assert_not_called()


def test_new_durable_command_dispatches_once() -> None:
    persona = SimpleNamespace(persona_id="p1")
    service = _runtime([persona])
    saved = {"message_id": "room:1", "_was_inserted": True}

    with patch(
        "database.building_messages.insert_building_message_with_location_guard",
        return_value=saved,
    ):
        list(
            service.handle_user_input_stream(
                "hello",
                building_id="room",
                client_message_id="cmd-1",
            )
        )

    service.manager.pulse_dispatcher.dispatch_user_utterance.assert_called_once()


def test_non_streaming_insert_failure_is_terminal() -> None:
    persona = SimpleNamespace(persona_id="p1")
    service = _runtime([persona])

    with patch("database.building_messages.insert_building_message_with_location_guard", return_value=None):
        replies = service.handle_user_input("hello")

    assert "処理を開始しませんでした" in replies[0]
    service.manager.pulse_dispatcher.dispatch_user_utterance.assert_not_called()


# ---------------------------------------------------------------------------
# 出口 3: 発言は受け取ったのに、画面へ何も出ないまま終わる回を捕まえる
#
# 個々の失敗経路を数え上げるのではなく、結果を作る場所 (backend_worker の
# finally) で一度だけ検査する形を固定する。入口を数え切る守り方は必ず漏れる。
# 設計: docs/issues/user_utterance_path_failure_inventory.md
# ---------------------------------------------------------------------------


def _no_response(events) -> list[dict]:
    return [e for e in events if e.get("error_code") == "no_response"]


def _no_responder(events) -> list[dict]:
    return [e for e in events if e.get("error_code") == "no_responder"]


def test_an_empty_room_says_so_instead_of_ending_in_silence() -> None:
    """応答できるペルソナが一人もいない部屋。発言は残るが返事は生まれない。

    ⚠️ この検査はもともと汎用の ``no_response`` を期待していた。実機検証
    (2026-08-26) で、その札のまま画面に出すと「発言の『再送』から応答をもう
    一度求められます」という**果たせない約束**になると分かったため、専用の
    ``no_responder`` へ変えた。テストの名前 (「部屋が空だとそう言う」) の方が
    最初から正しく、検査だけが汎用の札を仕様として固定していた。
    """
    service = _runtime([])
    saved = {"message_id": "room:1", "_was_inserted": True}

    with patch(
        "database.building_messages.insert_building_message_with_location_guard",
        return_value=saved,
    ):
        events = _events(
            service.handle_user_input_stream(
                "hello", building_id="room", client_message_id="cmd-1",
            )
        )

    assert len(_no_responder(events)) == 1
    assert _no_response(events) == []
    assert "応答できる相手がいません" in _no_responder(events)[0]["content"]


def test_a_swallowed_dispatch_failure_still_reaches_the_user() -> None:
    """受け口が何も出さずに終えた回も、同じ一箇所の検査が拾う。"""
    persona = SimpleNamespace(persona_id="p1")
    service = _runtime([persona])
    saved = {"message_id": "room:1", "_was_inserted": True}
    # 応答も、エラーも、何ひとつイベントを出さずに戻る受け口
    service.manager.pulse_dispatcher.dispatch_user_utterance = MagicMock(
        return_value=None,
    )

    with patch(
        "database.building_messages.insert_building_message_with_location_guard",
        return_value=saved,
    ):
        events = _events(
            service.handle_user_input_stream(
                "hello", building_id="room", client_message_id="cmd-1",
            )
        )

    assert len(_no_response(events)) == 1


def test_a_reported_failure_is_not_reported_twice() -> None:
    """既にエラーが出た回に no_response を重ねない (二重の説明にしない)。"""
    persona = SimpleNamespace(persona_id="p1")
    service = _runtime([persona])
    saved = {"message_id": "room:1", "_was_inserted": True}
    service.manager.pulse_dispatcher.dispatch_user_utterance = MagicMock(
        side_effect=RuntimeError("boom"),
    )

    with patch(
        "database.building_messages.insert_building_message_with_location_guard",
        return_value=saved,
    ):
        events = _events(
            service.handle_user_input_stream(
                "hello", building_id="room", client_message_id="cmd-1",
            )
        )

    assert [e for e in events if e.get("error_code") == "unknown"]
    assert _no_response(events) == []


def test_a_persistence_failure_is_not_reported_twice() -> None:
    """早期 return の経路も同じ — 既に理由を伝えているので重ねない。"""
    persona = SimpleNamespace(persona_id="p1")
    service = _runtime([persona])

    with patch(
        "database.building_messages.insert_building_message_with_location_guard",
        return_value=None,
    ):
        events = _events(
            service.handle_user_input_stream(
                "hello", building_id="room", client_message_id="cmd-1",
            )
        )

    assert [e for e in events if e.get("error_code") == "persistence_failed"]
    assert _no_response(events) == []


def test_an_answered_utterance_carries_no_no_response() -> None:
    """普通に喋れた回に余計な断りを出さない。"""
    persona = SimpleNamespace(persona_id="p1")
    service = _runtime([persona])
    saved = {"message_id": "room:1", "_was_inserted": True}

    def _speak(**kwargs):
        service.manager._active_sse_callbacks["room"]({
            "type": "say", "content": "hello back", "persona_id": "p1",
        })

    service.manager.pulse_dispatcher.dispatch_user_utterance = MagicMock(
        side_effect=_speak,
    )

    with patch(
        "database.building_messages.insert_building_message_with_location_guard",
        return_value=saved,
    ):
        events = _events(
            service.handle_user_input_stream(
                "hello", building_id="room", client_message_id="cmd-1",
            )
        )

    assert any(e["type"] == "say" for e in events)
    assert _no_response(events) == []


def test_progress_events_alone_do_not_count_as_an_answer() -> None:
    """考えている様子だけ出して黙って終わるのは、ユーザーから見れば無言。"""
    persona = SimpleNamespace(persona_id="p1")
    service = _runtime([persona])
    saved = {"message_id": "room:1", "_was_inserted": True}

    def _only_think(**kwargs):
        cb = service.manager._active_sse_callbacks["room"]
        cb({"type": "think", "content": "うーん", "persona_id": "p1"})
        cb({"type": "activity", "action": "tool", "name": "search"})

    service.manager.pulse_dispatcher.dispatch_user_utterance = MagicMock(
        side_effect=_only_think,
    )

    with patch(
        "database.building_messages.insert_building_message_with_location_guard",
        return_value=saved,
    ):
        events = _events(
            service.handle_user_input_stream(
                "hello", building_id="room", client_message_id="cmd-1",
            )
        )

    assert len(_no_response(events)) == 1


# --- 2026-08-26 ローカルレビューの消し込み ------------------------------------
# 芯は一つ: 「試みた」を「成功した」と同じ扱いにしない。続きが一言も生まれ
# なかった回に印を降ろすと二度と押せなくなり、履歴が読めなかった回を「無い」
# と言うと送り直しで同じ発言が二度載る。


def _interrupted_message(persona_id: str = "p1") -> dict:
    return {
        "message_id": "room:7",
        "role": "assistant",
        "persona_id": persona_id,
        "content": "途中まで",
        "metadata": {"_interrupted": True},
    }


def test_continue_keeps_the_mark_when_nothing_was_spoken() -> None:
    """続きが一言も生まれなかった回は、中断の印を降ろさない。"""
    history_manager = MagicMock()
    persona = SimpleNamespace(persona_id="p1", history_manager=history_manager)
    service = _runtime([persona])
    service.personas = {"p1": persona}
    service.manager.get_building_history = lambda building_id: [_interrupted_message()]

    def _silent_pulse(building_id, target, user_input, spoke=None,
                      on_saved=None, pre_generation_check=None):
        # 何も喋らないまま error だけ出して終わる Pulse。保存の信号を見て
        # いないので on_saved は呼ばない (実物のワーカーと同じ契約)。
        yield json.dumps({"type": "error", "error_code": "unknown"}) + "\n"

    service._stream_persona_pulse = _silent_pulse
    events = _events(service.continue_persona_message_stream("room:7"))

    assert any(event.get("error_code") == "unknown" for event in events)
    history_manager.update_building_message.assert_not_called()


def test_continue_clears_the_mark_once_the_persona_actually_spoke() -> None:
    """続きが生まれた回は、これまでどおり印を降ろす。"""
    history_manager = MagicMock()
    persona = SimpleNamespace(persona_id="p1", history_manager=history_manager)
    service = _runtime([persona])
    service.personas = {"p1": persona}
    service.manager.get_building_history = lambda building_id: [_interrupted_message()]

    def _speaking_pulse(building_id, target, user_input, spoke=None,
                        on_saved=None, pre_generation_check=None):
        if spoke is not None:
            spoke["value"] = True
        yield json.dumps({"type": "say", "content": "続きです"}) + "\n"
        # 保存の信号を見た回は、ストリームの終端までに on_saved を呼ぶ
        # (実物のワーカーと同じ契約)。
        if on_saved is not None:
            on_saved()

    service._stream_persona_pulse = _speaking_pulse
    _events(service.continue_persona_message_stream("room:7"))

    history_manager.update_building_message.assert_called_once()
    _, kwargs = history_manager.update_building_message.call_args
    assert kwargs["metadata"]["_interrupted"] is False


def test_retry_does_not_ask_for_a_resend_when_the_history_cannot_be_read() -> None:
    """履歴が読めなかった回に「もう一度送ってください」と言わない。

    言うと送り直しで同じ発言が二度載る — この口が防ごうとしているものそのもの。
    """
    service = _runtime([])

    def _boom(building_id):
        raise RuntimeError("db is down")

    service.manager.get_building_history = _boom
    events = _events(service.retry_user_message_stream("room:7"))

    assert [event.get("error_code") for event in events] == ["history_unavailable"]
    assert not any("もう一度送ってください" in (e.get("content") or "") for e in events)


def test_retry_still_reports_a_genuinely_missing_message() -> None:
    """本当に記録が無い回は、これまでどおり送り直しを勧める。"""
    service = _runtime([])
    service.manager.get_building_history = lambda building_id: []
    events = _events(service.retry_user_message_stream("room:7"))

    assert [event.get("error_code") for event in events] == ["message_not_found"]


def test_continue_separates_an_unreadable_history_from_a_missing_message() -> None:
    """continue 側も同じ区別を持つ (片方だけ直すと隣を忘れた形が残る)。"""
    service = _runtime([])

    def _boom(building_id):
        raise RuntimeError("db is down")

    service.manager.get_building_history = _boom
    unreadable = _events(service.continue_persona_message_stream("room:7"))

    service.manager.get_building_history = lambda building_id: []
    missing = _events(service.continue_persona_message_stream("room:7"))

    assert [e.get("error_code") for e in unreadable] == ["history_unavailable"]
    assert [e.get("error_code") for e in missing] == ["message_not_found"]


def test_a_silent_room_with_someone_in_it_keeps_the_generic_wording() -> None:
    """相手が居るのに返事が生まれなかった回は、やり直す余地があるので別の札。"""
    persona = SimpleNamespace(persona_id="p1")
    service = _runtime([persona])
    saved = {"message_id": "room:1", "_was_inserted": True}
    service.manager.pulse_dispatcher.dispatch_user_utterance = MagicMock(
        return_value=None,
    )

    with patch(
        "database.building_messages.insert_building_message_with_location_guard",
        return_value=saved,
    ):
        events = _events(
            service.handle_user_input_stream(
                "hello", building_id="room", client_message_id="cmd-1",
            )
        )

    assert len(_no_response(events)) == 1
    assert _no_responder(events) == []


def test_retry_uses_the_same_label_for_the_same_fact() -> None:
    """同じ事実 (相手が居ない) には、通常送信と再送で同じ札を使う。

    札が経路ごとに違うと、画面の側が「やり直しても無駄か」を経路ごとに
    判断する羽目になる。
    """
    service = _runtime([])
    service.manager.get_building_history = lambda building_id: [
        {"message_id": "room:7", "role": "user", "content": "hello"},
    ]
    events = _events(service.retry_user_message_stream("room:7"))

    assert [event.get("error_code") for event in events] == ["no_responder"]


def _continue_service(spoke_value: bool):
    """「続きの生成」を通せる最小の土台と、その履歴モックを返す。

    fake の ``_pulse`` は実物の ``_stream_persona_pulse`` の契約を写す:
    保存の信号を見ていた回は、ストリームの終端までに ``on_saved`` を 1 回
    呼ぶ (実物ではワーカースレッドが呼ぶ)。
    """
    history = MagicMock()
    persona = SimpleNamespace(persona_id="p1", history_manager=history)
    service = _runtime()
    service.personas = {"p1": persona}
    service._find_building_message = lambda building_id, message_id: (
        {
            "role": "assistant",
            "persona_id": "p1",
            "metadata": {"_interrupted": True},
        },
        "ok",
    )

    def _pulse(building_id, target_persona, instruction, spoke=None,
               on_saved=None, pre_generation_check=None):
        if spoke is not None:
            spoke["value"] = spoke_value
        yield "first\n"
        yield "second\n"
        if spoke_value and on_saved is not None:
            on_saved()

    service._stream_persona_pulse = _pulse
    return service, history


def test_continue_clears_the_mark_when_the_stream_finishes() -> None:
    """最後まで配り終えた回は印を降ろし、ボタンを消す。

    降ろすのは continue が ``on_saved`` に渡した閉包 — 対象はこの閉包が
    束縛した元の発言 (message_id) だけで、別の発言の印には触らない。
    """
    service, history = _continue_service(spoke_value=True)

    list(service.continue_persona_message_stream("room:1"))

    history.update_building_message.assert_called_once()
    args, kwargs = history.update_building_message.call_args
    assert args[1] == "room:1"
    assert kwargs["metadata"]["_interrupted"] is False


def _continue_service_with_real_pulse(run_sea_user):
    """実物の ``_stream_persona_pulse`` (= 実物の ``_note_speech`` と実物の
    ワーカー) を通す土台。

    ``_continue_service`` は pulse を差し替えるので、印降ろしの判定材料が
    どのイベントで立つか・誰が印を降ろすかまでは検査できない。こちらは
    manager の受け口 (``run_sea_user``) だけを差し替え、イベントは本物の
    配線を流れる。
    """
    history = MagicMock()
    persona = SimpleNamespace(
        persona_id="p1", history_manager=history,
        persona_name="P1", avatar_image=None,
    )
    service = _runtime()
    service.personas = {"p1": persona}
    service._find_building_message = lambda building_id, message_id: (
        {
            "role": "assistant",
            "persona_id": "p1",
            "metadata": {"_interrupted": True},
        },
        "ok",
    )
    service.manager.run_sea_user = run_sea_user
    return service, history


def _emitting_run_sea_user(events_to_emit):
    """渡されたイベントを Pulse の直通の口へ流すだけの run_sea_user。"""
    def _run_sea_user(target, building_id, user_input,
                      event_callback=None, **kwargs):
        for event in events_to_emit:
            event_callback(dict(event))
    return _run_sea_user


def _wait_until(condition, timeout=5.0) -> bool:
    """ワーカースレッドの後処理を待つ (デッドライン付きポーリング)。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def test_screen_events_alone_do_not_clear_the_interrupted_mark() -> None:
    """画面イベント (say / streaming_chunk) は保存の証拠ではない。

    画面へ流れただけの回に印を降ろすと、保存されていないのにボタンが消え、
    続きを取る手段が失われる。印を動かせるのは保存完了イベントだけ。
    設計: docs/issues/stream_completion_is_not_proof_of_persistence.md
    """
    service, history = _continue_service_with_real_pulse(_emitting_run_sea_user([
        {"type": "streaming_chunk", "content": "続き", "persona_id": "p1", "pulse_id": "x"},
        {"type": "say", "content": "続きです", "persona_id": "p1", "pulse_id": "x"},
    ]))

    list(service.continue_persona_message_stream("room:1"))

    history.update_building_message.assert_not_called()


def test_the_persistence_signal_clears_the_interrupted_mark() -> None:
    """保存完了イベント (``speak_persisted``) を見た回だけ、印を降ろす。"""
    service, history = _continue_service_with_real_pulse(_emitting_run_sea_user([
        {"type": "streaming_chunk", "content": "続き", "persona_id": "p1", "pulse_id": "x"},
        {"type": "speak_persisted", "message_id": "room:9",
         "persona_id": "p1", "pulse_id": "x"},
    ]))

    list(service.continue_persona_message_stream("room:1"))

    history.update_building_message.assert_called_once()
    args, kwargs = history.update_building_message.call_args
    assert args[1] == "room:1"
    assert kwargs["metadata"]["_interrupted"] is False


def test_the_mark_is_cleared_when_the_save_lands_after_the_reader_left() -> None:
    """読み手が切断 → その後に保存の信号 → 印が降りている (Codex #4 の回帰)。

    印を降ろす権威は Pulse を走らせたワーカースレッドにある。読み手側の
    finally に権威を置いた旧配線では、切断の時点で信号がまだ来ていない回に
    印が永久に残り、次の一押しで二つ目の続きが生まれた。
    """
    reader_left = threading.Event()

    def _run_sea_user(target, building_id, user_input,
                      event_callback=None, **kwargs):
        event_callback({"type": "status", "content": "processing",
                        "persona_id": "p1"})
        # 読み手が去るまで生成を続け、去った後に保存が確定する回を模す。
        assert reader_left.wait(timeout=5)
        event_callback({"type": "speak_persisted", "message_id": "room:9",
                        "persona_id": "p1", "pulse_id": "x"})

    service, history = _continue_service_with_real_pulse(_run_sea_user)

    stream = service.continue_persona_message_stream("room:1")
    next(stream)      # 一行だけ受け取って
    stream.close()    # ブラウザを閉じる
    reader_left.set()

    assert _wait_until(lambda: history.update_building_message.called)
    args, kwargs = history.update_building_message.call_args
    assert args[1] == "room:1"
    assert kwargs["metadata"]["_interrupted"] is False


def test_the_mark_stays_when_the_reader_left_and_no_save_was_confirmed() -> None:
    """読み手が去り、保存の信号が最後まで来なかった回は印を残す。

    残した印はもう一度押せるだけで、降ろした印は戻らない — 分からないときは
    ボタンが出続ける側へ倒す。
    """
    reader_left = threading.Event()
    pulse_done = threading.Event()

    def _run_sea_user(target, building_id, user_input,
                      event_callback=None, **kwargs):
        try:
            event_callback({"type": "status", "content": "processing",
                            "persona_id": "p1"})
            assert reader_left.wait(timeout=5)
            # 保存の信号は出さずに終わる (LLM エラー等)。
        finally:
            pulse_done.set()

    service, history = _continue_service_with_real_pulse(_run_sea_user)

    stream = service.continue_persona_message_stream("room:1")
    next(stream)
    stream.close()
    reader_left.set()

    assert pulse_done.wait(timeout=5)
    time.sleep(0.05)  # ワーカーの finally が走り切る猶予
    history.update_building_message.assert_not_called()


def test_a_signal_from_another_pulse_does_not_clear_the_mark() -> None:
    """別試行の信号では印を降ろさない (Codex #4 の照合)。

    continue の ``_enrich_event`` は ``_active_sse_callbacks`` にも登録され、
    そこには**別の Pulse** (会話開始の main_line 等) のイベントも流れ込む。
    保存の信号をそちらで数えると、自分の続きは何も保存していないのに、
    無関係な保存で印が降りてボタンが消える。
    """
    def _run_sea_user(target, building_id, user_input,
                      event_callback=None, **kwargs):
        # 自分の Pulse は信号を出さない。その間に、別の Pulse の保存完了が
        # 建物の受け口 (_active_sse_callbacks) へ流れ込む。
        service.manager._active_sse_callbacks["room"]({
            "type": "speak_persisted", "message_id": "room:77",
            "persona_id": "p1", "pulse_id": "other",
        })

    service, history = _continue_service_with_real_pulse(_run_sea_user)

    list(service.continue_persona_message_stream("room:1"))

    history.update_building_message.assert_not_called()


def test_continue_keeps_the_mark_when_nothing_was_generated() -> None:
    """続きが一言も生まれなかった回は印を残す。

    ここで降ろすとボタンが消え、二度目は「この発言は途中で終わっていないため、
    続きはありません」で拒まれる — 途中で終わっているのに、そう言うことになる。
    """
    service, history = _continue_service(spoke_value=False)

    list(service.continue_persona_message_stream("room:1"))

    history.update_building_message.assert_not_called()


def test_a_stream_that_closed_without_text_is_still_silence() -> None:
    """本文が一文字も出ない回を、ストリームが閉じた合図だけで済ませない。

    LLM が空を返すと ``streaming_chunk`` は一度も出ないので、画面には吹き出し
    すら作られない。それでも ``streaming_complete`` は送られる — これを「届いた」
    に数えると、発言は保存されているのに画面が無言のまま終わる。
    """
    persona = SimpleNamespace(persona_id="p1")
    service = _runtime([persona])
    saved = {"message_id": "room:1", "_was_inserted": True}

    def _empty(**kwargs):
        service.manager._active_sse_callbacks["room"]({
            "type": "streaming_complete", "persona_id": "p1",
        })

    service.manager.pulse_dispatcher.dispatch_user_utterance = MagicMock(
        side_effect=_empty,
    )

    with patch(
        "database.building_messages.insert_building_message_with_location_guard",
        return_value=saved,
    ):
        events = _events(
            service.handle_user_input_stream(
                "hello", building_id="room", client_message_id="cmd-1",
            )
        )

    assert len(_no_response(events)) == 1


def test_cleanup_leaves_a_newer_registration_alone() -> None:
    """先に終わったストリームが、後から始まった側の停止イベントを消さない。

    この registry は建物ごとに一枠しかないので、同じ建物で次のストリームが
    始まると上書きされる。終わった側が無条件に片付けると、後から始まった側の
    「停止」が効かなくなる。
    """
    persona = SimpleNamespace(persona_id="p1")
    service = _runtime([persona])
    saved = {"message_id": "room:1", "_was_inserted": True}
    newer_stop = object()
    newer_callback = object()

    def _speak_then_get_replaced(**kwargs):
        service.manager._active_sse_callbacks["room"]({
            "type": "say", "content": "hello back", "persona_id": "p1",
        })
        # 後から始まったストリームが同じ建物の枠を取る
        service.manager._active_stop_events["room"] = newer_stop
        service.manager._active_sse_callbacks["room"] = newer_callback

    service.manager.pulse_dispatcher.dispatch_user_utterance = MagicMock(
        side_effect=_speak_then_get_replaced,
    )

    with patch(
        "database.building_messages.insert_building_message_with_location_guard",
        return_value=saved,
    ):
        _events(
            service.handle_user_input_stream(
                "hello", building_id="room", client_message_id="cmd-1",
            )
        )

    assert service.manager._active_stop_events.get("room") is newer_stop
    assert service.manager._active_sse_callbacks.get("room") is newer_callback


def _stream_with(events_to_emit, responders=None):
    """指定のイベントだけを流して終わるストリームと、その土台を返す。

    イベントが ``persona_id`` を持っていればそちらを使う (複数のペルソナが並ぶ
    回を組み立てるため)。
    """
    if responders is None:
        responders = [SimpleNamespace(persona_id="p1")]
    service = _runtime(responders)

    def _emit(**kwargs):
        cb = service.manager._active_sse_callbacks["room"]
        for event in events_to_emit:
            cb({"persona_id": "p1", **event})

    service.manager.pulse_dispatcher.dispatch_user_utterance = MagicMock(
        side_effect=_emit,
    )
    return service


def _run(service):
    with patch(
        "database.building_messages.insert_building_message_with_location_guard",
        return_value={"message_id": "room:1", "_was_inserted": True},
    ):
        return _events(
            service.handle_user_input_stream(
                "hello", building_id="room", client_message_id="cmd-1",
            )
        )


def test_a_discarded_bubble_alone_is_still_silence() -> None:
    """出した吹き出しを引っ込めて終わった回は、画面に何も残らない。

    ``streaming_discard`` は「いま出した吹き出しを捨てて整形済みを後で出す」ための
    合図だが、その後 ``say`` が来ない終わり方 (ツール呼び出しだけで終わった回など)
    がある。断片で立てた印をそのままにすると、無言なのに「届いた」と数える。
    """
    service = _stream_with([
        {"type": "streaming_chunk", "content": "途中まで"},
        {"type": "streaming_discard"},
    ])

    assert len(_no_response(_run(service))) == 1


def test_a_discarded_bubble_followed_by_a_say_is_an_answer() -> None:
    """引っ込めた後に整形済みの発言が出れば、それは届いている。"""
    service = _stream_with([
        {"type": "streaming_chunk", "content": "途中まで"},
        {"type": "streaming_discard"},
        {"type": "say", "content": "整形済みの発言"},
    ])

    assert _no_response(_run(service)) == []


def test_blank_chunks_alone_are_silence() -> None:
    """空白だけの断片は、画面には何も出ていないのと同じ。"""
    service = _stream_with([
        {"type": "streaming_chunk", "content": "   "},
        {"type": "streaming_chunk", "content": ""},
    ])

    assert len(_no_response(_run(service))) == 1


def test_one_personas_discard_does_not_erase_anothers_answer() -> None:
    """片方の取り消しが、別のペルソナの届いた発言まで無かったことにしない。

    一度の送信で複数のペルソナが並行して喋ることがある。取り消しを出どころで
    照合しないと、あとから来たツール呼び出しだけのペルソナの取り消しが、既に
    喋った相手の結果まで消して「無言だった」ことにする。
    """
    service = _stream_with(
        [
            {"type": "streaming_chunk", "content": "こんにちは",
             "persona_id": "p1", "pulse_id": "a"},
            {"type": "streaming_chunk", "content": "…",
             "persona_id": "p2", "pulse_id": "b"},
            {"type": "streaming_discard", "persona_id": "p2", "pulse_id": "b"},
        ],
        responders=[
            SimpleNamespace(persona_id="p1"),
            SimpleNamespace(persona_id="p2"),
        ],
    )

    assert _no_response(_run(service)) == []


def test_closing_the_stream_leaves_the_generation_running() -> None:
    """読み手が去っても生成は止めない。

    SAIVerse のペルソナはブラウザが開いているかどうかと無関係に生きている —
    自律行動もするし、時刻から発言も起こす。画面を閉じたことを理由に認知を
    打ち切ると、ユーザーの発言だけが残って返事が生まれない状態を**こちらから
    作る**ことになる。この issue がずっと潰してきた「無言で終わる」そのもの。

    しかも止める手段 (``cancel_active_generation``) は建物にいる全員の実行中の
    要求を取り消すので、画面を閉じただけで無関係な自律行動まで巻き添えになり、
    記録には「ユーザーが止めた」と残る。

    2026-08-26 に一度「画面が閉じたら止める」を入れて撤回した。その規範をここで
    固定する。
    """
    persona = SimpleNamespace(persona_id="p1")
    service = _runtime([persona])
    captured: dict = {}

    def _speak(**kwargs):
        captured["stop_event"] = service.manager._active_stop_events["room"]
        service.manager._active_sse_callbacks["room"]({
            "type": "say", "content": "hello back", "persona_id": "p1",
        })

    service.manager.pulse_dispatcher.dispatch_user_utterance = MagicMock(
        side_effect=_speak,
    )

    with patch(
        "database.building_messages.insert_building_message_with_location_guard",
        return_value={"message_id": "room:1", "_was_inserted": True},
    ):
        stream = service.handle_user_input_stream(
            "hello", building_id="room", client_message_id="cmd-1",
        )
        next(stream)      # 一行だけ受け取って
        stream.close()    # ブラウザを閉じる

    assert not captured["stop_event"].is_set()
    service.manager.cancel_active_generation.assert_not_called()
