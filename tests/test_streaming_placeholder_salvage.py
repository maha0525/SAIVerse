"""返事が途中で止まった回 — 発言を消さず、最後の発言に「続きの生成」を出す。

設計: docs/intent/reply_stop_exit.md (2026-09-25 再設計)。

二つの役割を分けて固定する。

1. **止まりかけた生成は保存だけ** (lg_llm_node の出口 `_save_draft_on_beat_death`
   ほか): 下書き行は本文の器で、確定して初めて中身が入る。Beat が例外で死ぬと
   確定が走らず、喋った内容がどこにも残らない (2026-05-19〜08-26 の 3 ヶ月で
   32 件)。**lg_llm_node の node() は、未確定の下書き行を残して終わらない**。
   ただし「言い切っていない」印と中断の通告はここでは置かない。
2. **後始末は返事の実行につき一回** (sea/reply_stop_exit.py の
   ``settle_reply_stop`` — 本物は run_meta_user が呼ぶ): 「このペルソナが最後に
   保存した発言」の記録を見て、その発言に印を付け、その部屋に通告を一枚置き、
   エラー札 / 知らせに案内の材料を載せる。

node() を直接回すテストは、返事の一番外側の代わりに ``_settle_reply`` を呼んで
後始末まで通す。
"""
from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from llm_clients.exceptions import LLMError, ModelUnavailableError
from sea import runtime_llm
from sea.cancellation import ExecutionCancelledException
from sea.reply_stop_exit import interruption_notice_text, settle_reply_stop
from sea.runtime_emitters import (
    SAVED_FORM_COMPLETE,
    SAVED_FORM_CUT,
    SAVED_FORM_SPELL_RESULTS,
    SAVED_FORM_SPELL_UNFINISHED,
    forget_saved_utterances,
    last_saved_utterance,
    SpeakFinalizeResult,
    notify_speak_persisted,
)
from sea.runtime_llm import INTERRUPTED_METADATA_KEY, _save_cut_utterance


@pytest.fixture(autouse=True)
def _fresh_saved_utterance_record():
    """「最後に保存した発言」の記録はプロセス内で共有 — テストごとに空にする。"""
    forget_saved_utterances()
    yield
    forget_saved_utterances()


def _marking_history_manager() -> MagicMock:
    """建物の行の印付けが通る history_manager (更新後の行を dict で返す)。"""
    hm = MagicMock()

    def _update(building_id, message_id, *, content=None, metadata=None):
        return {"message_id": message_id, "metadata": dict(metadata or {})}

    hm.update_building_message.side_effect = _update

    # 通告の書き込みは DB 採番の message_id つきの行を返す (本物の
    # add_to_building_only は失敗しても id 無しの dict を返すので、置けたかの
    # 判定は id の有無で行われる)。
    _notice_seq = {"n": 0}

    def _add(building_id, msg, *, heard_by=None):
        _notice_seq["n"] += 1
        return {**msg, "message_id": f"{building_id}:notice{_notice_seq['n']}"}

    hm.add_to_building_only.side_effect = _add
    return hm


def _settle_reply(runtime, persona, *, events=None, exc=None, token=None,
                  reply_building_id="b1", started_at=0.0):
    """返事の一番外側 (run_meta_user) の代わりに、後始末を一回だけ通す。"""
    return settle_reply_stop(
        runtime, persona,
        reply_building_id=reply_building_id,
        started_at=started_at,
        exc=exc,
        cancellation_token=token,
        event_callback=(events.append if events is not None else None),
    )


def _notices(persona) -> list:
    """建物の記録へ置かれた中断の通告 (host 名義の行) の (部屋, 本文) の列。"""
    return [
        (c.args[0], c.args[1]["content"])
        for c in persona.history_manager.add_to_building_only.call_args_list
        if isinstance(c.args[1], dict) and c.args[1].get("role") == "host"
    ]


def _marked(persona) -> list:
    """「言い切っていない」印を付けた (部屋, 発言 id) の列。"""
    return [
        (c.args[0], c.args[1])
        for c in persona.history_manager.update_building_message.call_args_list
        if (c.kwargs.get("metadata") or {}).get(INTERRUPTED_METADATA_KEY)
    ]


# ---------------------------------------------------------------------------
# 通告の本文 — 4 分類 × 原因 2 通り (2026-09-25 まはー裁定の文面)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("form,body", [
    (SAVED_FORM_CUT, "ここで発言が中断されました"),
    (SAVED_FORM_SPELL_UNFINISHED,
     "発言の後に唱えたスペルは、実行が終わる前に中断されました。"
     "結果は届いておらず、どこまで実行されたかは不明です"),
    (SAVED_FORM_SPELL_RESULTS,
     "スペルの結果を受け取った後、続きの発言の前に中断されました"),
    (SAVED_FORM_COMPLETE, "この発言の後、続きが始まる前に中断されました"),
])
def test_the_notice_text_for_each_form_and_cause(form, body):
    assert interruption_notice_text(form, by_user=False) == f"({body})"
    assert interruption_notice_text(form, by_user=True) == f"(ユーザーの操作により、{body})"


def test_the_existing_user_stop_wording_is_kept():
    """① のユーザー停止は既存の文字列そのまま (記憶に既に入っている文面と揃える)。"""
    assert interruption_notice_text(SAVED_FORM_CUT, by_user=True) == (
        "(ユーザーの操作により、ここで発言が中断されました)"
    )


def test_an_unknown_form_falls_back_to_the_general_notice():
    """形が分からないときは、証明できる一般の受け皿 (④) に倒す。"""
    assert interruption_notice_text(None, by_user=False) == (
        "(この発言の後、続きが始まる前に中断されました)"
    )


def test_the_unfinished_spell_notice_never_claims_it_did_not_run():
    """② は「実行されていない」と断定しない — 副作用がどこまで効いたかは機構にも
    分からず、断定するとペルソナが唱え直して二重に効く。"""
    text = interruption_notice_text(SAVED_FORM_SPELL_UNFINISHED, by_user=False)
    assert "不明" in text
    assert "実行されていません" not in text
    assert "実行されませんでした" not in text


# ---------------------------------------------------------------------------
# _save_cut_utterance 単体 — 止まった生成の保存は、保存だけ
# ---------------------------------------------------------------------------

def _save(occupants=("1", "p1"), **overrides):
    runtime = MagicMock()
    runtime.manager.occupants = {"b1": list(occupants)}
    runtime._emit_speak_finalize.return_value = SpeakFinalizeResult(
        status="saved",
        building_msg={"message_id": "m1", "content": overrides.get("text", "言いかけた本文")},
    )
    persona = SimpleNamespace(persona_id="p1", history_manager=_marking_history_manager())
    state = {}
    events: list = []
    params = dict(
        runtime=runtime,
        persona=persona,
        state=state,
        playbook=SimpleNamespace(name="pb"),
        event_callback=events.append,
        building_id="b1",
        msg_id="m1",
        sub_seq=0,
        text="言いかけた本文",
    )
    params.update(overrides)
    seq = _save_cut_utterance(**params)
    return runtime, persona, state, events, seq


def test_saving_a_cut_utterance_places_no_mark_and_no_notice():
    """保存は保存だけ — 建物の行の印と通告は、返事の後始末が一回だけ置く。"""
    runtime, persona, state, events, seq = _save()
    runtime._emit_speak_finalize.assert_called_once()
    call = runtime._emit_speak_finalize.call_args
    assert call.args[3] == "言いかけた本文"
    assert call.kwargs["extra_metadata"] is None
    assert seq == 1
    # 通告・画面への印は置かない
    persona.history_manager.add_to_building_only.assert_not_called()
    assert not [e for e in events if e.get("interrupted")]
    # 記憶には途中で切れた本文として、印つきで書く
    runtime._store_memory.assert_called_once()
    assert runtime._store_memory.call_args.kwargs["metadata"] == {
        INTERRUPTED_METADATA_KEY: True,
    }
    assert state["_beat_memorized"] is True
    assert state[INTERRUPTED_METADATA_KEY] is True


def test_saving_records_the_form_and_that_the_talk_stopped():
    """保存できた発言に、形と「この後で話が止まった」を記録へ書き足す。"""
    _save()
    record = last_saved_utterance("p1")
    assert record is not None
    assert (record.message_id, record.building_id) == ("m1", "b1")
    assert record.form == SAVED_FORM_CUT
    assert record.stopped is True


def test_a_complete_utterance_is_memorized_without_the_cut_mark():
    """言い切ってから止まった本文は、記憶に「言い切っていない」印を付けない。"""
    runtime, _, state, _, _ = _save(form=SAVED_FORM_COMPLETE)
    assert runtime._store_memory.call_args.kwargs["metadata"] is None
    assert INTERRUPTED_METADATA_KEY not in state
    assert last_saved_utterance("p1").form == SAVED_FORM_COMPLETE


def test_an_empty_memory_text_skips_the_memory_write():
    """周の頭で記憶に書いた本文は重ねない (memory_text="")。"""
    runtime, _, _, _, _ = _save(form=SAVED_FORM_SPELL_UNFINISHED, memory_text="")
    runtime._emit_speak_finalize.assert_called_once()
    runtime._store_memory.assert_not_called()


def test_an_empty_body_saves_quietly():
    """一言も出ないうちに死んだ回は、空文字で確定するだけで何も記録しない。"""
    runtime, persona, state, events, _ = _save(text="")
    runtime._emit_speak_finalize.assert_called_once()
    assert runtime._emit_speak_finalize.call_args.args[3] == ""
    runtime._store_memory.assert_not_called()
    assert events == []
    assert INTERRUPTED_METADATA_KEY not in state
    assert "_beat_memorized" not in state
    assert last_saved_utterance("p1") is None


def test_a_failed_save_records_nothing():
    """保存に失敗した行は、最後の発言として記録しない (見えない行に印を付けない)。"""
    runtime = MagicMock()
    runtime._emit_speak_finalize.return_value = SpeakFinalizeResult(
        status="failed", error="db down",
    )
    persona = SimpleNamespace(persona_id="p1", history_manager=_marking_history_manager())
    _save_cut_utterance(
        runtime=runtime, persona=persona, state={},
        playbook=SimpleNamespace(name="pb"), event_callback=None,
        building_id="b1", msg_id="m1", sub_seq=0, text="言いかけ",
    )
    assert last_saved_utterance("p1") is None


# ---------------------------------------------------------------------------
# settle_reply_stop 単体 — 返事につき一回、最後の発言に印と通告を一つずつ
# ---------------------------------------------------------------------------

def _reply_world(occupants=None, building_names=None):
    runtime = MagicMock()
    runtime.manager.occupants = occupants or {"b1": ["1"], "b2": ["2"]}
    runtime.manager.building_map = {
        bid: SimpleNamespace(name=name)
        for bid, name in (building_names or {"b1": "居間", "b2": "書斎"}).items()
    }
    persona = SimpleNamespace(persona_id="p1", history_manager=_marking_history_manager())
    return runtime, persona


def _saved(persona, message_id, building_id="b1", content="発言"):
    notify_speak_persisted(
        None, {"message_id": message_id, "content": content}, persona, "pl",
        building_id=building_id,
    )


def test_an_error_marks_the_last_saved_utterance_and_carries_the_guidance():
    runtime, persona = _reply_world()
    _saved(persona, "m1")
    _saved(persona, "m2")
    exc = LLMError("boom")

    record = _settle_reply(runtime, persona, exc=exc)

    assert record.message_id == "m2"
    assert _marked(persona) == [("b1", "m2")]
    assert _notices(persona) == [("b1", "(この発言の後、続きが始まる前に中断されました)")]
    event = exc.to_dict()
    assert event["interrupted_message_id"] == "m2"
    # 返事の部屋と同じ部屋なので、部屋の案内は載らない
    assert "interrupted_building_id" not in event
    assert "interrupted_building_name" not in event


def test_an_utterance_in_another_room_carries_the_room_guidance():
    """スペルで移った先の部屋の発言で止まった回 — 部屋の id と表示名を添える。"""
    runtime, persona = _reply_world()
    _saved(persona, "m1", building_id="b1")
    _saved(persona, "m2", building_id="b2")
    exc = LLMError("boom")

    _settle_reply(runtime, persona, exc=exc, reply_building_id="b1")

    assert _marked(persona) == [("b2", "m2")]
    assert _notices(persona)[0][0] == "b2"
    event = exc.to_dict()
    assert event["interrupted_message_id"] == "m2"
    assert event["interrupted_building_id"] == "b2"
    assert event["interrupted_building_name"] == "書斎"


def test_the_notice_reaches_every_occupant_including_the_speaker():
    """通告の heard_by は在室者全員。在室者リストに発話者本人が欠けていても補う。"""
    runtime, persona = _reply_world(occupants={"b1": ["1"]})
    _saved(persona, "m1")
    _settle_reply(runtime, persona, exc=LLMError("boom"))
    call = persona.history_manager.add_to_building_only.call_args
    assert call.kwargs["heard_by"] == ["1", "p1"]


def test_an_utterance_saved_before_the_reply_started_is_not_marked():
    """時間窓 — 返事の実行の開始時刻より前に保存された発言 (別の実行の発言) には
    印も通告も付けない。"""
    runtime, persona = _reply_world()
    _saved(persona, "old")
    started = time.monotonic() + 1.0
    exc = LLMError("boom")

    assert _settle_reply(runtime, persona, exc=exc, started_at=started) is None
    assert _marked(persona) == []
    assert _notices(persona) == []
    assert "interrupted_message_id" not in exc.to_dict()


def test_nothing_saved_in_the_reply_means_no_mark_and_no_notice():
    """一文字も生まれていない回は、印も通告も置かない (エラー札は「再送」のまま)。"""
    runtime, persona = _reply_world()
    exc = LLMError("boom")
    assert _settle_reply(runtime, persona, exc=exc) is None
    assert _notices(persona) == []
    assert "interrupted_message_id" not in exc.to_dict()


def test_a_clean_finish_is_not_settled():
    """例外なしで閉じ、話が止まった書き足しも無い回は、何もしない。"""
    runtime, persona = _reply_world()
    _saved(persona, "m1")
    assert _settle_reply(runtime, persona, exc=None) is None
    assert _marked(persona) == []
    assert _notices(persona) == []


def test_the_settle_runs_once_per_utterance():
    """印と通告は一回の中断に一つずつ — 二度呼ばれても二枚目を作らない。"""
    runtime, persona = _reply_world()
    _saved(persona, "m1")
    assert _settle_reply(runtime, persona, exc=LLMError("a")) is not None
    assert _settle_reply(runtime, persona, exc=LLMError("b")) is None
    assert len(_notices(persona)) == 1
    assert len(_marked(persona)) == 1


def test_a_failed_mark_places_no_notice():
    """印が付かなかった回は通告も置かない (印と通告は必ず対)。"""
    runtime, persona = _reply_world()
    persona.history_manager.update_building_message.side_effect = None
    persona.history_manager.update_building_message.return_value = None
    _saved(persona, "m1")
    exc = LLMError("boom")

    _settle_reply(runtime, persona, exc=exc)

    assert _notices(persona) == []
    assert "interrupted_message_id" not in exc.to_dict()


def test_a_failed_notice_withdraws_the_mark_and_gives_no_guidance():
    """通告が置けなかった回は印を取り下げ、案内も出さない (印と通告は必ず対)。

    印だけの行は「続きの生成」が押せるのに、会話の末尾に通告が無く、続きの
    プロンプトがモデル発話で終わって Gemini 系が必ず拒む — 対の片割れだけを
    残すくらいなら、エラー札だけの世界 (既知の割り切り) に倒す。
    """
    runtime, persona = _reply_world()
    persona.history_manager.add_to_building_only.side_effect = RuntimeError("db busy")
    _saved(persona, "m1")
    exc = LLMError("boom")

    _settle_reply(runtime, persona, exc=exc)

    # 印は一度付いた後、取り下げられている (最後の更新が False)
    updates = [
        (c.kwargs.get("metadata") or {}).get(INTERRUPTED_METADATA_KEY)
        for c in persona.history_manager.update_building_message.call_args_list
    ]
    assert updates == [True, False]
    # 案内は出ない — 押せないボタンを案内しない
    assert "interrupted_message_id" not in exc.to_dict()


def test_a_notice_write_that_returns_no_id_withdraws_the_mark_too():
    """通告の書き込みが例外を出さずに失敗した回 (DB 挿入が再試行の後に失敗し、
    add_to_building_only が id の無い dict を返した) も、置けなかった扱い。

    例外が出なかったことは置けた証拠にならない — 置けたかは DB 採番の
    message_id の有無で決める (builtin_data/tools/tell.py と同じ裁定)。
    """
    runtime, persona = _reply_world()
    persona.history_manager.add_to_building_only.side_effect = (
        lambda building_id, msg, *, heard_by=None: {**msg, "heard_by": heard_by}
    )
    _saved(persona, "m1")
    exc = LLMError("boom")

    assert _settle_reply(runtime, persona, exc=exc) is None

    updates = [
        (c.kwargs.get("metadata") or {}).get(INTERRUPTED_METADATA_KEY)
        for c in persona.history_manager.update_building_message.call_args_list
    ]
    assert updates == [True, False]
    assert "interrupted_message_id" not in exc.to_dict()


def test_the_notice_write_reports_success_only_with_a_db_numbered_row():
    """_record_interruption_notice 単体 — 戻り値は message_id の有無で決まる。"""
    runtime, persona = _reply_world()
    assert runtime_llm._record_interruption_notice(
        runtime, persona, "b1", content="(通告)", msg_id="m1",
    ) is True
    persona.history_manager.add_to_building_only.side_effect = None
    persona.history_manager.add_to_building_only.return_value = {"role": "host"}
    assert runtime_llm._record_interruption_notice(
        runtime, persona, "b1", content="(通告)", msg_id="m1",
    ) is False
    # 隔離中の部屋 (空の dict) も置けなかった扱い
    persona.history_manager.add_to_building_only.return_value = {}
    assert runtime_llm._record_interruption_notice(
        runtime, persona, "b1", content="(通告)", msg_id="m1",
    ) is False


def test_a_user_stop_is_read_from_the_wrapped_cancellation():
    """LLM ノードは取り消しを LLMError に包み直す — 連鎖をたどって原因を読む。"""
    runtime, persona = _reply_world()
    _saved(persona, "m1")
    cancel = ExecutionCancelledException("stopped", interrupted_by="user")
    wrapped = LLMError("LLM node failed", original_error=cancel)

    _settle_reply(runtime, persona, exc=wrapped)

    assert _notices(persona)[0][1].startswith("(ユーザーの操作により、")


def test_a_schedule_preemption_read_from_the_chain_is_not_a_user_stop():
    runtime, persona = _reply_world()
    _saved(persona, "m1")
    cancel = ExecutionCancelledException("preempted", interrupted_by="schedule")

    _settle_reply(runtime, persona, exc=LLMError("x", original_error=cancel))

    assert "ユーザーの操作" not in _notices(persona)[0][1]


def test_a_quiet_stop_reads_the_cause_from_the_token():
    """例外なしで閉じた停止 (スペル無効のペルソナ) は、取り消しの札から原因を読む。"""
    runtime, persona = _reply_world()
    _saved(persona, "m1")
    runtime_llm.note_saved_utterance("p1", message_id="m1", form=SAVED_FORM_CUT, stopped=True)
    token = SimpleNamespace(is_cancelled=lambda: True, interrupted_by="user_stop")

    _settle_reply(runtime, persona, exc=None, token=token)

    assert _notices(persona) == [
        ("b1", "(ユーザーの操作により、ここで発言が中断されました)"),
    ]


def test_a_stream_cut_emits_the_info_notice_with_the_guidance():
    """サーバーが切った回はエラー札ではなく情報の知らせのまま、案内の材料を載せる。"""
    runtime, persona = _reply_world()
    _saved(persona, "m1", building_id="b2")
    runtime_llm.note_saved_utterance(
        "p1", message_id="m1", form=SAVED_FORM_CUT, stopped=True,
        detail={"stream_error": {"code": 500, "message": "internal error"}},
    )
    events: list = []

    _settle_reply(runtime, persona, events=events, exc=None, reply_building_id="b1")

    infos = [e for e in events if e.get("type") == "info"]
    assert len(infos) == 1
    assert infos[0]["content"].startswith("メッセージの生成が途中で終了しました。")
    assert "ℹ️" not in infos[0]["content"]
    assert infos[0]["interrupted_message_id"] == "m1"
    assert infos[0]["interrupted_building_id"] == "b2"
    assert infos[0]["interrupted_building_name"] == "書斎"
    assert _notices(persona) == [("b2", "(ここで発言が中断されました)")]


def test_the_error_event_carries_no_guidance_fields_by_default():
    event = LLMError("boom").to_dict()
    for key in ("interrupted_message_id", "interrupted_building_id",
                "interrupted_building_name"):
        assert key not in event


# ---------------------------------------------------------------------------
# node() 全体 — Beat の出口の不変条件
# ---------------------------------------------------------------------------

class _FakeStreamClient:
    """generate_stream だけを持つ最小の LLM クライアント。

    ``call_exc`` は呼び出しの瞬間に、``iter_exc`` は chunk を流し終えた後に
    例外を投げる。どちらも「LLM 呼び出し中に Beat が死ぬ」形の再現用。
    """

    config_key = None

    def __init__(self, chunks=None, call_exc=None, iter_exc=None):
        self._chunks = list(chunks or [])
        self._call_exc = call_exc
        self._iter_exc = iter_exc

    def generate_stream(self, messages, tools=(), temperature=None, **kwargs):
        if self._call_exc is not None:
            raise self._call_exc

        def _gen():
            yield from self._chunks
            if self._iter_exc is not None:
                raise self._iter_exc

        return _gen()

    def consume_usage(self):
        return None

    def consume_thought_signature(self):
        return None


class _CancelDuringStream:
    """1 chunk 目を通した後に取り消しへ倒れる cancellation token。

    node() 冒頭の ``raise_if_cancelled`` は素通しし、ストリーム消費中の
    ``is_cancelled`` 判定が 2 回目から True になる — 「ストリームの途中で
    停止ボタンが押された」形の再現用。
    """

    interrupted_by = "user"

    def __init__(self):
        self._checks = 0

    def raise_if_cancelled(self):
        return None

    def is_cancelled(self):
        self._checks += 1
        return self._checks > 1


def _node_def():
    return SimpleNamespace(
        id="llm",
        speak=True,
        action=None,
        available_tools=None,
        response_schema=None,
        response_schema_source=None,
        output_key=None,
        output_keys=None,
        metadata_key=None,
        memorize=None,
        important=False,
        label=None,
    )


def _saving_finalize(runtime):
    """確定の結果を、渡された行 id と本文で返す (実物の SpeakFinalizeResult の形)。

    テストが ``return_value`` に結果を置いた回はそれを返す (保存失敗の再現など)。
    """
    def _finalize(persona, building_id, message_id, text, **kwargs):
        configured = runtime._emit_speak_finalize.return_value
        if isinstance(configured, SpeakFinalizeResult):
            return configured
        return SpeakFinalizeResult(
            status="saved",
            building_msg={"message_id": message_id, "content": text},
        )
    return _finalize


def _recording_emit_say(runtime):
    """直接の建物書き込み (_emit_say) の偽物。実物と同じく保存完了の共通の口を通す
    — 通さないと「最後に保存した発言」の記録が書かれない。テストが
    ``return_value`` に行を置いた回はそれを返す (採番の無い行の再現など)。"""
    counter = {"n": 0}

    def _emit_say(persona, building_id, text, pulse_id=None, metadata=None,
                  event_callback=None, occupants_snapshot=None):
        counter["n"] += 1
        configured = runtime._emit_say.return_value
        if isinstance(configured, dict):
            bmsg = dict(configured)
        else:
            bmsg = {"message_id": f"say-{counter['n']}", "content": text}
        notify_speak_persisted(
            event_callback, bmsg, persona, pulse_id, building_id=building_id,
        )
        return bmsg
    return _emit_say


def _build_node(monkeypatch, *, client, spell_loop, node_def=None, persona_id="p1"):
    runtime = MagicMock()
    runtime.manager.occupants = {"b1": ["1"]}
    runtime.manager.building_map = {
        "b1": SimpleNamespace(name="居間"), "b2": SimpleNamespace(name="書斎"),
    }
    runtime._effective_building_id.return_value = "b1"
    runtime._emit_speak_start.return_value = "msg-1"
    # 確定は三値の結果を返す (sea/runtime_emitters.py の SpeakFinalizeResult)。
    # 素の MagicMock を返すと status が "saved" と一致せず、呼び出し元が
    # 「確定できなかった」側 (= salvage 続行) に倒れてテストの前提が崩れる。
    # 渡された行 id と本文をそのまま返す — 「最後に保存した発言」の記録が
    # 実際に確定した行を指すように。
    runtime._emit_speak_finalize.side_effect = _saving_finalize(runtime)
    runtime._emit_say.side_effect = _recording_emit_say(runtime)
    runtime._default_temperature.return_value = 0.7
    runtime._get_cache_kwargs.return_value = {}
    runtime._store_memory.return_value = "mem-1"
    runtime.select_llm_client.return_value = (client, "model-a")

    monkeypatch.setattr(
        runtime_llm, "resolve_execution_context",
        lambda persona, pulse_context, state=None: SimpleNamespace(model_key="model-a"),
    )
    monkeypatch.setattr(runtime_llm, "_is_llm_streaming_enabled", lambda: True)
    monkeypatch.setattr(runtime_llm, "_record_llm_usage", lambda *a, **k: None)
    monkeypatch.setattr(runtime_llm, "_consume_reasoning", lambda *a, **k: ("", None))
    monkeypatch.setattr(runtime_llm, "_finalize_beat", lambda *a, **k: None)
    monkeypatch.setattr(runtime_llm, "_run_spell_loop", spell_loop)

    # 「最後に保存した発言」の記録はペルソナ単位なので、既定で persona_id を
    # 持たせる (返事の後始末まで通すため)。
    persona = SimpleNamespace(
        persona_id=persona_id, persona_name="p",
        history_manager=_marking_history_manager(),
    )
    events: list = []
    node = runtime_llm.lg_llm_node(
        runtime, node_def or _node_def(), persona, "b1", SimpleNamespace(name="pb"),
        events.append,
    )
    return runtime, persona, node, events


def _run_reply(node, state, runtime, persona, events, *, reply_building_id="b1"):
    """node() を返事の一番外側 (run_meta_user) と同じ形で回す。

    例外で閉じたら後始末を例外つきで、例外なしで閉じたら例外なしで、一回だけ
    通す。例外はそのまま投げ直す (エラー札の案内の材料は例外に載っている)。
    """
    # 返事の実行の一番外側 (sea/runtime.py) と同じ単調時計。
    started = time.monotonic()
    try:
        result = asyncio.run(node(state))
    except Exception as exc:
        _settle_reply(
            runtime, persona, events=events, exc=exc,
            token=state.get("_cancellation_token"),
            reply_building_id=reply_building_id, started_at=started,
        )
        raise
    _settle_reply(
        runtime, persona, events=events, exc=None,
        token=state.get("_cancellation_token"),
        reply_building_id=reply_building_id, started_at=started,
    )
    return result


async def _spell_loop_raising(exc):
    raise exc


def test_a_stream_call_that_dies_leaves_no_empty_record(monkeypatch):
    """LLM 呼び出しが即死した回 — 本文ゼロの下書き行は確定せず、取り下げる。

    2026-09-13 以前はここで空文字の確定を打っていた。本文の無い記録が建物と
    ペルソナのログに永続し、下書きの印も倒れて孤児掃除の網から外れる —
    締めの Beat で塞いだ H-1 と同じ形だったので、Beat の出口でも同じ判定を
    通す (docs/issues/pulse_beats_merge_into_single_record.md の N-1)。
    """
    client = _FakeStreamClient(call_exc=RuntimeError("api down"))

    async def _unused_spell_loop(**kwargs):  # pragma: no cover - 到達しない
        raise AssertionError("spell loop must not run")

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_unused_spell_loop,
    )
    runtime._withdraw_speak_placeholder.return_value = True
    with pytest.raises(LLMError):
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    runtime._emit_speak_finalize.assert_not_called()
    runtime._withdraw_speak_placeholder.assert_called_once()
    assert runtime._withdraw_speak_placeholder.call_args.args[1] == "b1"
    assert runtime._withdraw_speak_placeholder.call_args.args[2] == "msg-1"
    # 本文が無いので、記憶にも建物の通告にも何も書かない
    runtime._store_memory.assert_not_called()
    persona.history_manager.add_to_building_only.assert_not_called()


def test_a_stop_before_the_first_token_leaves_no_empty_record(monkeypatch):
    """ストリームが一語も返さないうちの停止 — 空の記録を残さず行を取り下げる。

    ストリーム中の停止を受け止める経路 (`cancelled_during_stream`) も、
    Beat の出口と締めの Beat と同じ判定を通す (同族の三箇所目)。
    """
    # 最初の chunk を受け取る前に停止が立つ (chunk は捨てられ、本文は空)。
    client = _FakeStreamClient(chunks=["これから喋るはずだった。"])

    async def _no_spells(**kwargs):
        return runtime_llm.SpellLoopResult(
            segments=[], final_continuation=kwargs["text"], loop_count=0,
        )

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_no_spells,
    )
    runtime._withdraw_speak_placeholder.return_value = True

    class _CancelImmediately:
        interrupted_by = "user"

        def raise_if_cancelled(self):
            return None

        def is_cancelled(self):
            return True

    asyncio.run(node({
        "_messages": [], "_pulse_id": "pl-1",
        "_cancellation_token": _CancelImmediately(),
    }))

    runtime._emit_speak_finalize.assert_not_called()
    runtime._withdraw_speak_placeholder.assert_called_once()
    assert runtime._withdraw_speak_placeholder.call_args.args[1] == "b1"
    assert runtime._withdraw_speak_placeholder.call_args.args[2] == "msg-1"
    persona.history_manager.add_to_building_only.assert_not_called()


def test_a_refused_withdrawal_falls_back_to_confirming_the_row(monkeypatch):
    """取り下げを断られた回は空文字でも確定する — 未確定の行を残さない。"""
    client = _FakeStreamClient(call_exc=RuntimeError("api down"))

    async def _unused_spell_loop(**kwargs):  # pragma: no cover - 到達しない
        raise AssertionError("spell loop must not run")

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_unused_spell_loop,
    )
    runtime._withdraw_speak_placeholder.return_value = False
    with pytest.raises(LLMError):
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    runtime._emit_speak_finalize.assert_called_once()
    args = runtime._emit_speak_finalize.call_args.args
    assert args[2] == "msg-1"
    assert args[3] == ""


def test_a_spell_loop_death_confirms_the_draft_row_with_the_spoken_text(monkeypatch):
    """喋り終えた後に Beat が死んだ回 — 出口は本文つきで保存するだけ。

    ストリームは受け切っているので、本文は言い切った発言 (④)。印と通告は
    返事の後始末が一回だけ置き、エラー札にその発言の id が載る。
    """
    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _spell_loop(**kwargs):
        raise RuntimeError("boom after generation")

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_spell_loop,
    )
    with pytest.raises(LLMError) as excinfo:
        _run_reply(node, {"_messages": [], "_pulse_id": "pl-1"}, runtime, persona, events)

    runtime._emit_speak_finalize.assert_called_once()
    call = runtime._emit_speak_finalize.call_args
    assert call.args[2] == "msg-1"
    assert call.args[3] == "こんにちは。"
    # 建物の行の印は後始末が付ける (確定の時点では載せない)
    assert call.kwargs["extra_metadata"] is None
    # sub-speak が 1 番まで出た後なので、final は 2 番 (連番の衝突なし)
    assert call.kwargs["final_sub_seq"] == 2
    # 言い切った本文なので、記憶には「言い切っていない」印を付けない
    runtime._store_memory.assert_called_once()
    assert runtime._store_memory.call_args.kwargs["metadata"] is None

    assert _marked(persona) == [("b1", "msg-1")]
    assert _notices(persona) == [("b1", "(この発言の後、続きが始まる前に中断されました)")]
    assert excinfo.value.to_dict()["interrupted_message_id"] == "msg-1"


def test_a_late_stop_still_reads_as_a_user_interruption(monkeypatch):
    """生成し終えた直後の停止 (2026-08-26 実機の形) — 通告はユーザーの操作の文面。"""
    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _spell_loop(**kwargs):
        raise ExecutionCancelledException("stopped", interrupted_by="user")

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_spell_loop,
    )
    with pytest.raises(LLMError):
        _run_reply(node, {"_messages": [], "_pulse_id": "pl-1"}, runtime, persona, events)

    runtime._emit_speak_finalize.assert_called_once()
    assert runtime._emit_speak_finalize.call_args.args[3] == "こんにちは。"
    assert _notices(persona) == [
        ("b1", "(ユーザーの操作により、この発言の後、続きが始まる前に中断されました)"),
    ]


def test_a_schedule_preemption_is_not_reported_as_a_user_stop(monkeypatch):
    """schedule の割り込みで切られた回 — 通告を「ユーザーの操作により」と書かない。

    取り消しの例外型はユーザー停止と同じなので、型で判定すると機構起点の中断に
    ユーザー起点の通告が捏造される。判定は取り消しに刻まれた原因
    (`interrupted_by`) から行う (2026-08-27 Codex 指摘の固定)。
    """
    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _spell_loop(**kwargs):
        raise ExecutionCancelledException("preempted", interrupted_by="schedule")

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_spell_loop,
    )
    with pytest.raises(LLMError):
        _run_reply(node, {"_messages": [], "_pulse_id": "pl-1"}, runtime, persona, events)

    notices = _notices(persona)
    assert len(notices) == 1
    assert "ユーザーの操作" not in notices[0][1]


def test_a_server_shutdown_is_not_reported_as_a_user_stop(monkeypatch):
    """サーバー終了 (Ctrl+C / SIGTERM) で切られた回 — 取り消しには
    "server_shutdown" が刻まれる (PulseController.shutdown)。ユーザー起点では
    ないので、通告は原因を書かない非ユーザー形になる。"""
    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _spell_loop(**kwargs):
        raise ExecutionCancelledException("shutdown", interrupted_by="server_shutdown")

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_spell_loop,
    )
    with pytest.raises(LLMError):
        _run_reply(node, {"_messages": [], "_pulse_id": "pl-1"}, runtime, persona, events)

    assert _notices(persona) == [("b1", "(この発言の後、続きが始まる前に中断されました)")]


def test_a_keyboard_interrupt_propagates_without_salvage_side_effects(monkeypatch):
    """KeyboardInterrupt はインタープリタ終了の道筋 — DB 書き込みの副作用を
    足さず、そのまま伝播させる (捕捉を CancelledError に絞った境界の固定)。"""
    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _spell_loop(**kwargs):
        raise KeyboardInterrupt()

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_spell_loop,
    )
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    runtime._emit_speak_finalize.assert_not_called()
    persona.history_manager.add_to_building_only.assert_not_called()


def test_a_task_cancellation_confirms_the_draft_row_and_propagates(monkeypatch):
    """asyncio.CancelledError (サーバー停止等) は Exception に捕まらないが、
    下書き行は同じく確定させてから、そのままの型で伝播する。"""
    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _spell_loop(**kwargs):
        raise asyncio.CancelledError()

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_spell_loop,
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    runtime._emit_speak_finalize.assert_called_once()
    assert runtime._emit_speak_finalize.call_args.args[3] == "こんにちは。"


def test_a_mid_stream_death_confirms_the_draft_row_with_the_partial_text(monkeypatch):
    """chunk を流している最中に client が例外を投げた回。

    返り値の tuple は届かないが、途中経過の器 (`_stream_progress`) 経由で
    受信済みの部分文が回収され、下書き行はその本文で確定する。final の連番も
    発火済みの sub-speak (seq=1) と衝突しない (2026-08-27 Codex 指摘の固定)。
    途中で切れた本文なので、後始末の通告は ① になる。
    """
    client = _FakeStreamClient(chunks=["こんにちは。"], iter_exc=RuntimeError("died mid-stream"))

    async def _unused_spell_loop(**kwargs):  # pragma: no cover - 到達しない
        raise AssertionError("spell loop must not run")

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_unused_spell_loop,
    )
    with pytest.raises(LLMError):
        _run_reply(node, {"_messages": [], "_pulse_id": "pl-1"}, runtime, persona, events)

    runtime._emit_speak_finalize.assert_called_once()
    call = runtime._emit_speak_finalize.call_args
    assert call.args[2] == "msg-1"
    assert call.args[3] == "こんにちは。"
    assert call.kwargs["final_sub_seq"] == 2
    assert call.kwargs["extra_metadata"] is None
    assert runtime._store_memory.call_args.kwargs["metadata"] == {
        INTERRUPTED_METADATA_KEY: True,
    }
    assert _marked(persona) == [("b1", "msg-1")]
    assert _notices(persona) == [("b1", "(ここで発言が中断されました)")]


def test_a_stop_during_the_stream_settles_inside_and_not_again_at_the_exit(monkeypatch):
    """ストリーム途中の停止 — try 塊の中で保存した後、Beat の出口が二重確定しない。

    この経路だけは保存の後もコードが続く (spell round の頭で取り消しが
    見つかって例外で抜けるまで)。確定済みの印 (`pipeline_finalized`) が
    出口の保存を止めることを、実物の `_run_spell_loop` ごと通して固定する。
    印と通告は返事の後始末が一回だけ置く。
    """
    client = _FakeStreamClient(chunks=["こんにちは。", "続きの文。"])
    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=runtime_llm._run_spell_loop,
    )
    state = {
        "_messages": [],
        "_pulse_id": "pl-1",
        "_cancellation_token": _CancelDuringStream(),
        # spell を有効にして、実物の spell loop が round 頭の取り消し検査で
        # ExecutionCancelledException を投げる経路を通す (無効だと入り口で
        # 即 return し、Beat は例外なしで静かに閉じる — それはそれで正しい)。
        "_spell_enabled": True,
        "_realtime_spells_executed": True,
    }
    with pytest.raises(LLMError):
        _run_reply(node, state, runtime, persona, events)

    # 確定は 1 回だけ (途中停止の保存が行い、出口は印を見て手を出さない)
    runtime._emit_speak_finalize.assert_called_once()
    call = runtime._emit_speak_finalize.call_args
    assert call.args[3] == "こんにちは。"
    assert call.kwargs["extra_metadata"] is None
    runtime._store_memory.assert_called_once()
    # 印と通告も 1 回だけで、文面はユーザーの操作 (途中で切れた本文 = ①)
    assert _marked(persona) == [("b1", "msg-1")]
    assert _notices(persona) == [
        ("b1", "(ユーザーの操作により、ここで発言が中断されました)"),
    ]


def test_an_exception_after_the_finalize_does_not_settle_twice(monkeypatch):
    """通常確定の後で Beat が死んだ回 — 出口の後始末は確定済みの印を見て手を出さない。

    印 (`pipeline_finalized = True`) を消すとこのテストが落ちる。確定の後に
    もう一度 settle が走ると、確定済みの本文が古い値で上書きされ、中断の通告と
    記憶書き込みが捏造される。
    """
    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _no_spells(**kwargs):
        return runtime_llm.SpellLoopResult(
            segments=[], final_continuation=kwargs["text"], loop_count=0,
        )

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_no_spells,
    )
    # 確定 (branch 3) の後に通る _dump_llm_io で Beat を落とす
    runtime._dump_llm_io.side_effect = RuntimeError("boom after finalize")

    with pytest.raises(LLMError):
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    runtime._emit_speak_finalize.assert_called_once()
    extra = runtime._emit_speak_finalize.call_args.kwargs.get("extra_metadata") or {}
    assert INTERRUPTED_METADATA_KEY not in extra
    persona.history_manager.add_to_building_only.assert_not_called()
    runtime._store_memory.assert_not_called()


def test_a_failed_finalize_keeps_the_exit_cleanup_armed(monkeypatch):
    """確定が「保存できなかった」を返した回は、確定済みの印を立てない (Codex #2)。

    かつては確定を**呼んだだけ**で印を立てていた — 保存に失敗していても
    Beat 死亡時の救済 (この出口の後始末) が「もう確定した」と誤認し、下書き行が
    空のまま永久に残った。saved のときだけ印を立てれば、その後 Beat が死んだ
    回に救済がもう一度確定を試みる。
    """
    from sea.runtime_emitters import SpeakFinalizeResult

    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _no_spells(**kwargs):
        return runtime_llm.SpellLoopResult(
            segments=[], final_continuation=kwargs["text"], loop_count=0,
        )

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_no_spells,
    )
    runtime._emit_speak_finalize.return_value = SpeakFinalizeResult(
        status="failed", error="db down",
    )
    # 確定 (失敗) の後に通る _dump_llm_io で Beat を落とす
    runtime._dump_llm_io.side_effect = RuntimeError("boom after finalize")

    with pytest.raises(LLMError):
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    # 1 回目 = 通常確定 (失敗)、2 回目 = 出口の後始末による再試行。
    assert runtime._emit_speak_finalize.call_count == 2


def test_a_missing_draft_row_makes_the_exit_cleanup_a_no_op(monkeypatch):
    """下書き行をそもそも作れなかった回 — 出口の後始末は何もしない。"""
    client = _FakeStreamClient(call_exc=RuntimeError("api down"))

    async def _unused_spell_loop(**kwargs):  # pragma: no cover - 到達しない
        raise AssertionError("spell loop must not run")

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_unused_spell_loop,
    )
    runtime._emit_speak_start.return_value = None

    with pytest.raises(LLMError):
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    runtime._emit_speak_finalize.assert_not_called()
    persona.history_manager.add_to_building_only.assert_not_called()


def test_the_fallback_emit_carries_the_event_callback_for_the_signal(monkeypatch):
    """下書き行を作れなかった回の fallback (_emit_say 直書き) も、保存完了
    イベントの口 (event_callback) を運ぶ (Codex #1)。

    信号の発火は emit_say 内の共通の口が行うので、ここで渡し忘れると
    この救済経路で保存された発言だけ信号が欠ける。
    """
    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _no_spells(**kwargs):
        return runtime_llm.SpellLoopResult(
            segments=[], final_continuation=kwargs["text"], loop_count=0,
        )

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_no_spells,
    )
    # 下書き行の発番に失敗 → 正常完了時に _emit_say fallback が走る。
    runtime._emit_speak_start.return_value = None

    asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    runtime._emit_speak_finalize.assert_not_called()
    runtime._emit_say.assert_called_once()
    assert runtime._emit_say.call_args.kwargs["event_callback"] is not None


def test_a_failing_cleanup_does_not_replace_the_original_exception(monkeypatch):
    """出口の後始末が自分で失敗しても、Beat を落とした元の例外がそのまま届く。"""
    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _spell_loop(**kwargs):
        raise ValueError("the original failure")

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_spell_loop,
    )

    def _save_raises(**kwargs):
        raise RuntimeError("cleanup itself failed")

    monkeypatch.setattr(runtime_llm, "_save_cut_utterance", _save_raises)

    with pytest.raises(LLMError) as excinfo:
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    assert isinstance(excinfo.value.original_error, ValueError)


def test_a_normal_completion_finalizes_exactly_once(monkeypatch):
    """正常完了の回に出口の後始末が二重確定しないこと (確定済みの印の検算)。"""
    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _no_spells(**kwargs):
        return runtime_llm.SpellLoopResult(
            segments=[], final_continuation=kwargs["text"], loop_count=0,
        )

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_no_spells,
    )
    state = {"_messages": [], "_pulse_id": "pl-1"}
    result = asyncio.run(node(state))

    assert result is state
    runtime._emit_speak_finalize.assert_called_once()
    call = runtime._emit_speak_finalize.call_args
    assert call.args[3] == "こんにちは。"
    extra = call.kwargs.get("extra_metadata") or {}
    assert INTERRUPTED_METADATA_KEY not in extra
    # 中断の通告も記憶書き込みも走らない
    persona.history_manager.add_to_building_only.assert_not_called()
    runtime._store_memory.assert_not_called()


# ---------------------------------------------------------------------------
# Beat の出口の記憶補填 (`_backfill_memory_on_beat_death`) — 確定は済んだのに
# memorize (`_finalize_beat`) が走る前に Beat が死ぬと、建物の記録には全文が
# あるのに本人の記憶には何も残らない。下書き行の後始末は「確定済み」でスキップ
# するので、記憶の欠けは補填が塞ぐ。
# ---------------------------------------------------------------------------

def _memorize_node_def():
    node_def = _node_def()
    node_def.memorize = True
    return node_def


def test_a_death_after_finalize_but_before_memorize_backfills_the_memory(
    monkeypatch, caplog,
):
    """確定後・memorize 前の例外死 — 本人の記憶が本文つきで補填される。"""
    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _no_spells(**kwargs):
        return runtime_llm.SpellLoopResult(
            segments=[], final_continuation=kwargs["text"], loop_count=0,
        )

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_no_spells,
        node_def=_memorize_node_def(),
    )
    # 確定 (branch 3) の後、_finalize_beat の手前で通る _dump_llm_io で落とす
    runtime._dump_llm_io.side_effect = RuntimeError("boom before memorize")

    with pytest.raises(LLMError), caplog.at_level(logging.INFO, logger="sea.runtime_llm"):
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    # 補填の成功は INFO の印を残す — 実地での発火をログの grep で数えるための
    # 目印 (2026-08-29 まはー承認)。文言を変えるときは監視の grep も一緒に。
    assert any(
        "memory backfill on beat death succeeded" in rec.message
        for rec in caplog.records
    )

    # 確定は通常経路の 1 回だけ (出口の後始末は確定済みの印を見て手を出さない)
    runtime._emit_speak_finalize.assert_called_once()
    # 記憶は補填の 1 回だけ、本文つき — memorize=True の組み立て (tags=[]) で書く
    runtime._store_memory.assert_called_once()
    call = runtime._store_memory.call_args
    assert call.args[1] == "こんにちは。"
    assert call.kwargs["tags"] == []
    assert call.kwargs["playbook_name"] == "pb"
    # 言い切った発言なので、中断の通告は書かれず、印も載らない
    persona.history_manager.add_to_building_only.assert_not_called()
    assert INTERRUPTED_METADATA_KEY not in (call.kwargs.get("metadata") or {})


def test_a_beat_already_marked_memorized_is_not_written_again(monkeypatch):
    """「もう記憶に書かれた」の印が立った後の死 — 補填は追加で書かない。"""
    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _no_spells(**kwargs):
        return runtime_llm.SpellLoopResult(
            segments=[], final_continuation=kwargs["text"], loop_count=0,
        )

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_no_spells,
        node_def=_memorize_node_def(),
    )

    def _mark_and_die(node_def_arg, text_arg, state_arg):
        state_arg["_beat_memorized"] = True
        raise RuntimeError("boom after the memory write")

    runtime._process_structured_output.side_effect = _mark_and_die

    with pytest.raises(LLMError):
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    runtime._store_memory.assert_not_called()


def test_a_settled_interruption_is_not_memorized_twice_by_the_backfill(monkeypatch):
    """停止の後始末 (settle) が記憶を書いた回の死 — 補填が重ねて書かない。"""
    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _spell_loop(**kwargs):
        raise RuntimeError("boom after generation")

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_spell_loop,
        node_def=_memorize_node_def(),
    )
    with pytest.raises(LLMError):
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    # 出口の保存が 1 回書き、補填は印を見て手を出さない。ストリームを受け
    # 切った本文 (言い切った発言) なので、記憶に「言い切っていない」印は無い。
    runtime._store_memory.assert_called_once()
    assert runtime._store_memory.call_args.kwargs["metadata"] is None


def test_a_death_inside_finalize_beat_still_backfills_the_memory(monkeypatch):
    """`_finalize_beat` は except 節の外で呼ばれる — その内部 (memorize の手前の
    組み立て) で死んだ回も「確定後の隙間」で、補填が要る。呼び出しを包む
    try が受け止めて補填してから、元の例外をそのまま通す。"""
    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _no_spells(**kwargs):
        return runtime_llm.SpellLoopResult(
            segments=[], final_continuation=kwargs["text"], loop_count=0,
        )

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_no_spells,
        node_def=_memorize_node_def(),
    )

    def _die_before_memorize(*args, **kwargs):
        raise RuntimeError("boom inside finalize beat")

    monkeypatch.setattr(runtime_llm, "_finalize_beat", _die_before_memorize)

    with pytest.raises(RuntimeError):
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    runtime._store_memory.assert_called_once()
    assert runtime._store_memory.call_args.args[1] == "こんにちは。"


def test_a_death_inside_finalize_beat_after_memorize_is_not_written_again(monkeypatch):
    """`_finalize_beat` が memorize を終えてから死んだ回 — 印が立っているので
    補填は黙り、同じ本文が二重に記憶へ入らない。"""
    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _no_spells(**kwargs):
        return runtime_llm.SpellLoopResult(
            segments=[], final_continuation=kwargs["text"], loop_count=0,
        )

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_no_spells,
        node_def=_memorize_node_def(),
    )

    def _memorize_then_die(runtime_arg, beat):
        beat.state["_beat_memorized"] = True
        raise RuntimeError("boom after the memorize inside finalize beat")

    monkeypatch.setattr(runtime_llm, "_finalize_beat", _memorize_then_die)

    with pytest.raises(RuntimeError):
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    runtime._store_memory.assert_not_called()


def test_a_stop_with_spells_disabled_memorizes_the_partial_text_only_once(monkeypatch):
    """ストリーム途中の停止 + スペル無効 — 保存した部分文を確定が重ねない。

    スペル無効のペルソナでは `_run_spell_loop` が入り口で即 return するので、
    停止された Beat は例外を出さずに完走し、`_finalize_beat` の memorize に
    到達する (Codex レビュー 2 巡目)。memorize が保存の印 (`_beat_memorized`)
    を見ないと、同じ部分文が同 Beat で二重に記憶へ入る。この 1 本だけは
    `_run_spell_loop` も `_finalize_beat` も実物で通す。

    返事は例外なしで閉じるので、印と通告は「この後で話が止まった」の書き足しを
    見た後始末が置く (取り消しの札から、ユーザーの停止と読む)。
    """
    client = _FakeStreamClient(chunks=["こんにちは。", "続きの文。"])

    real_finalize_beat = runtime_llm._finalize_beat
    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=runtime_llm._run_spell_loop,
        node_def=_memorize_node_def(),
    )
    # fixture が patch した _finalize_beat を実物へ戻す — 確定の memorize 節が
    # 印を読むところまで含めて本物の経路を固定する。
    monkeypatch.setattr(runtime_llm, "_finalize_beat", real_finalize_beat)
    # MagicMock の戻り値 (truthy) が structured output 扱いにならないように倒す。
    runtime._process_structured_output.return_value = False

    token = _CancelDuringStream()
    state = {
        "_messages": [],
        "_pulse_id": "pl-1",
        "_cancellation_token": token,
        "_spell_enabled": False,
    }
    # 例外なしで完走する (spell loop はスペル無効の入り口で即 return し、
    # round 頭の取り消し検査に到達しない)
    result = _run_reply(node, state, runtime, persona, events)
    assert result is state

    # 確定は保存の 1 回だけ (通常確定は pipeline_finalized を見てスキップ)
    runtime._emit_speak_finalize.assert_called_once()
    call = runtime._emit_speak_finalize.call_args
    assert call.args[3] == "こんにちは。"
    assert call.kwargs["extra_metadata"] is None
    # 記憶も保存の 1 回だけ、中断の印つき — ここが二重だった (二重保存の固定)
    runtime._store_memory.assert_called_once()
    assert runtime._store_memory.call_args.kwargs["metadata"] == {
        INTERRUPTED_METADATA_KEY: True,
    }
    # 印と通告は後始末が 1 回、ユーザーの操作の文面で
    assert _marked(persona) == [("b1", "msg-1")]
    assert _notices(persona) == [
        ("b1", "(ユーザーの操作により、ここで発言が中断されました)"),
    ]


# ---------------------------------------------------------------------------
# 締めの Beat が本文を生まなかった回 — 空の記録を残さない
# (docs/issues/pulse_beats_merge_into_single_record.md の H-1)
#
# スペルループは次の周の生成の直前に新しい下書き行を開ける。その生成が例外で
# 落ちるか空応答だと、締めの本文が無いまま行だけが残る。空文字で確定すると
# 「本文の無い発言」が建物とペルソナのログに永続し、下書きの印も倒れて孤児
# 掃除の網から外れる。
# ---------------------------------------------------------------------------

SPELL_NAME = "note_add"
SPELL_LINE = f"/spell name='{SPELL_NAME}' args={{}}"


class _ScriptedStreamClient:
    """呼び出しごとに別の応答を返す streaming クライアント。

    要素は「その回に流す chunk の列」か、``generate_stream`` の瞬間に投げる
    例外。列の末尾に例外を置くと、chunk を流し終えた後にストリームが死ぬ。
    """

    config_key = None

    def __init__(self, calls):
        self._calls = list(calls)

    def generate_stream(self, messages, tools=(), temperature=None, **kwargs):
        assert self._calls, "_ScriptedStreamClient: no scripted calls left"
        nxt = self._calls.pop(0)
        if isinstance(nxt, Exception):
            raise nxt

        def _gen():
            for item in nxt:
                if isinstance(item, Exception):
                    raise item
                yield item

        return _gen()

    def consume_usage(self):
        return None

    def consume_thought_signature(self):
        return None

    def consume_stream_error(self):
        return None


class _CancelAtCheck:
    """``is_cancelled`` の N 回目から取り消しへ倒れる token。"""

    interrupted_by = "auto"

    def __init__(self, at: int):
        self._at = at
        self._checks = 0

    def raise_if_cancelled(self):
        return None

    def is_cancelled(self):
        self._checks += 1
        return self._checks >= self._at


async def _ok_spell(tool_name, tool_args, persona, state, playbook_name,
                    event_callback, messages=None):
    return ("やりました", None, True)


def _build_spell_node(monkeypatch, *, calls, node_def=None):
    """実物のスペルループを通す node と、その周辺のフェイク一式。"""
    client = _ScriptedStreamClient(calls)
    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=runtime_llm._run_spell_loop,
        node_def=node_def,
    )
    # Beat ごとに別の下書き行が発番される (2 周目は新しい部屋の新しい行)。
    runtime._emit_speak_start.side_effect = ["msg-1", "msg-2", "msg-3"]
    monkeypatch.setattr(runtime_llm, "SPELL_TOOL_NAMES", {SPELL_NAME})
    monkeypatch.setattr(runtime_llm, "_run_spell_tool_async", _ok_spell)
    return runtime, persona, node, events


def _spell_state(**extra):
    state = {
        "_messages": [],
        "_pulse_id": "pl-1",
        "_spell_enabled": True,
        "_realtime_spells_executed": True,
    }
    state.update(extra)
    return state


def _withdrawal_marks_the_event_log(runtime, events) -> list:
    """取り下げを呼んだ瞬間のイベント数を記録し、取り下げは成功させる。

    取り消し (``streaming_discard``) は Beat 1 の確定でも流れるので、
    「取り下げ由来のものが流れたか」は種類と部屋だけでは区別できない。
    呼び出しの瞬間に線を引いて、その後ろに来たものだけを数える。
    """
    marks: list = []

    def _withdraw(persona, building_id, message_id):
        marks.append(len(events))
        return True

    runtime._withdraw_speak_placeholder.side_effect = _withdraw
    return marks


def _discards_after(events, index: int) -> list:
    return [
        e for e in events[index:]
        if e.get("type") == "streaming_discard"
    ]


def test_a_closing_beat_that_dies_leaves_no_empty_record(monkeypatch):
    """再呼び出しが例外で落ちた回 — 空の下書き行は確定せず、取り下げる。

    LLMError に包まれていない例外でも、続きの生成の失敗として返事を止める
    (スペル内部の致命エラーとして黙って閉じない)。取り下げは Beat の出口が行う。
    """
    runtime, persona, node, events = _build_spell_node(
        monkeypatch,
        calls=[[f"やるね。\n{SPELL_LINE}"], RuntimeError("api down")],
    )
    marks = _withdrawal_marks_the_event_log(runtime, events)

    with pytest.raises(LLMError) as excinfo:
        asyncio.run(node(_spell_state()))
    assert isinstance(excinfo.value.original_error, RuntimeError)

    # 確定は Beat 1 の 1 回だけ。空文字での確定は起きない。
    runtime._emit_speak_finalize.assert_called_once()
    assert "やるね。" in runtime._emit_speak_finalize.call_args.args[3]
    # 締めの Beat のために開けた行は取り下げられる (行を作った部屋に対して)
    runtime._withdraw_speak_placeholder.assert_called_once()
    assert runtime._withdraw_speak_placeholder.call_args.args[1] == "b1"
    assert runtime._withdraw_speak_placeholder.call_args.args[2] == "msg-2"
    # 宙吊りの吹き出しを消す取り消しは、**取り下げの後に** 流れたものであること。
    # Beat 1 の確定も取り消しを流すので、種類だけを数えると取り下げ由来が
    # 一つも流れていなくてもこの検査は通ってしまう。
    assert marks, "取り下げが呼ばれていない"
    after = _discards_after(events, marks[0])
    assert after, "取り下げた行の吹き出しを消す取り消しが流れていない"
    assert after[0]["pulse_id"] == "pl-1"
    assert after[0]["node_id"] == "llm"
    assert after[0]["building_id"] == "b1"
    # 取り下げた行の後に say は流れない (消したはずの発言が画面に残らない)
    assert not [e for e in events[marks[0]:] if e.get("type") == "say"]


def test_a_closing_beat_that_returns_nothing_leaves_no_empty_record(monkeypatch):
    """再呼び出しが空応答だった回も同じ — 空の記録を残さない。"""
    runtime, persona, node, events = _build_spell_node(
        monkeypatch, calls=[[f"やるね。\n{SPELL_LINE}"], []],
    )
    runtime._withdraw_speak_placeholder.return_value = True

    asyncio.run(node(_spell_state()))

    runtime._emit_speak_finalize.assert_called_once()
    assert "やるね。" in runtime._emit_speak_finalize.call_args.args[3]
    runtime._withdraw_speak_placeholder.assert_called_once()
    assert runtime._withdraw_speak_placeholder.call_args.args[1] == "b1"
    assert runtime._withdraw_speak_placeholder.call_args.args[2] == "msg-2"


def test_a_withdrawal_after_a_move_targets_the_room_that_holds_the_row(monkeypatch):
    """移動を挟んだ回の取り下げ先は、その行を作った部屋 (= 移動先)。

    2 周目の下書き行は移動後の部屋に作られる。取り下げが元の部屋を指すと、
    移動先に残った空の行は消えないまま、元の部屋に対して空振りの削除を投げる。
    """
    runtime, persona, node, events = _build_spell_node(
        monkeypatch, calls=[[f"移るね。\n{SPELL_LINE}"], []],
    )
    # スペルの実行でペルソナが b2 へ移る (現在地は引くたびに最新を返す)
    rooms = ["b1"]
    runtime._effective_building_id.side_effect = (
        lambda _persona, _fallback: rooms[-1]
    )

    async def _moving_spell(tool_name, tool_args, persona_, state, playbook_name,
                            event_callback, messages=None):
        rooms.append("b2")
        return ("移動しました", None, True)

    monkeypatch.setattr(runtime_llm, "_run_spell_tool_async", _moving_spell)
    runtime._withdraw_speak_placeholder.return_value = True

    asyncio.run(node(_spell_state()))

    # Beat 1 の確定先は作成時の部屋 (移動後に引き直さない)
    runtime._emit_speak_finalize.assert_called_once()
    assert runtime._emit_speak_finalize.call_args.args[1] == "b1"
    assert runtime._emit_speak_finalize.call_args.args[2] == "msg-1"
    # Beat 2 の下書き行は移動先の部屋に作られる
    assert runtime._emit_speak_start.call_args.args[1] == "b2"
    # 取り下げも、その行がある移動先の部屋に対して行う
    runtime._withdraw_speak_placeholder.assert_called_once()
    assert runtime._withdraw_speak_placeholder.call_args.args[1] == "b2"
    assert runtime._withdraw_speak_placeholder.call_args.args[2] == "msg-2"


def test_a_closing_beat_that_dies_midway_keeps_what_it_said(monkeypatch):
    """再呼び出しが途中まで流してから死んだ回 — 部分文を途中で切れた本文として保存する。

    画面と音声には既に流れた言葉なので、取り下げでは消えてしまう。LLMError に
    包まれていない例外でも続きの生成の失敗として返事を止め (スペル内部の致命
    エラーとして黙って閉じない — スペル無しの同じ切断と同じエラー札になる)、
    Beat の出口が部分文を途中で切れた本文として保存し、後始末がそれに印と
    通告を置いてエラー札に案内を載せる。
    """
    runtime, persona, node, events = _build_spell_node(
        monkeypatch,
        calls=[
            [f"やるね。\n{SPELL_LINE}"],
            ["途中まで喋った", RuntimeError("died mid-stream")],
        ],
    )
    runtime._withdraw_speak_placeholder.return_value = True

    with pytest.raises(LLMError) as excinfo:
        _run_reply(node, _spell_state(), runtime, persona, events)
    assert isinstance(excinfo.value.original_error, RuntimeError)
    assert excinfo.value.to_dict()["interrupted_message_id"] == "msg-2"

    # 行は取り下げない — 本文があるので確定側で救う
    runtime._withdraw_speak_placeholder.assert_not_called()
    assert runtime._emit_speak_finalize.call_count == 2
    second = runtime._emit_speak_finalize.call_args_list[1]
    assert second.args[2] == "msg-2"
    assert second.args[3] == "途中まで喋った"
    assert second.kwargs["extra_metadata"] is None
    # 印と中断の通告は、その部分文が流れた部屋の、その発言に
    assert _marked(persona) == [("b1", "msg-2")]
    assert _notices(persona) == [("b1", "(ここで発言が中断されました)")]


def test_a_refused_withdrawal_still_confirms_the_row(monkeypatch):
    """取り下げが断られた回は確定へ落ちる — 未確定の下書き行を残さない。"""
    runtime, persona, node, events = _build_spell_node(
        monkeypatch, calls=[[f"やるね。\n{SPELL_LINE}"], []],
    )
    runtime._withdraw_speak_placeholder.return_value = False

    asyncio.run(node(_spell_state()))

    assert runtime._emit_speak_finalize.call_count == 2
    assert runtime._emit_speak_finalize.call_args_list[1].args[2] == "msg-2"


def test_a_death_in_the_second_beat_salvages_from_the_spell_side_progress(monkeypatch):
    """2 周目以降の下書き行の回収元は、スペルループ側の器。

    閉包の `text` は最初の周の本文で、それは**別の行として確定済み**。
    `placeholder_round > 1` の分岐がここを取り違えると、確定済みの本文が
    2 周目の行にも書かれて二重になる。
    """
    runtime, persona, node, events = _build_spell_node(
        monkeypatch, calls=[[f"やるね。\n{SPELL_LINE}"], ["二周目の言葉。"]],
    )
    # 取り消しの評価点: 1 = 1 周目の chunk、2 = 周の頭、3 = 2 周目の chunk、
    # 4 = 次の周の頭 (ここで倒れて Beat の出口へ抜ける)。
    state = _spell_state(_cancellation_token=_CancelAtCheck(4))

    with pytest.raises(LLMError):
        asyncio.run(node(state))

    assert runtime._emit_speak_finalize.call_count == 2
    second = runtime._emit_speak_finalize.call_args_list[1]
    assert second.args[2] == "msg-2"
    # 2 周目の器の本文で確定する (1 周目の本文を持ち込まない)
    assert second.args[3] == "二周目の言葉。"
    assert "やるね。" not in second.args[3]
    assert second.kwargs["extra_metadata"] is None
    # 連番も 2 周目の器の値から進める (0 から積み直した行なので)
    assert second.kwargs["final_sub_seq"] == 2


def test_a_beat_that_dies_before_its_first_token_leaves_no_empty_record(monkeypatch):
    """再呼び出しの最初の一語が届く前に停止された回 — 空の記録を残さない。

    2 周目の下書き行は開いたが、その周のストリームが一語も返さないうちに
    ユーザーが止めた。ここで空文字のまま確定すると、本文の無い記録が建物と
    ペルソナのログに永続する (締めの Beat の H-1 と同じ形)。判定と取り下げは
    正常復帰側と同じ関数を通す。
    """
    runtime, persona, node, events = _build_spell_node(
        monkeypatch, calls=[[f"やるね。\n{SPELL_LINE}"], []],
    )
    marks = _withdrawal_marks_the_event_log(runtime, events)
    # 取り消しの評価点: 1 = 1 周目の chunk、2 = 周の頭、2 周目のストリームは
    # 一語も流さないので chunk の評価は無く、3 = 次の周の頭 (ここで倒れる)。
    state = _spell_state(_cancellation_token=_CancelAtCheck(3))

    with pytest.raises(LLMError):
        asyncio.run(node(state))

    # 確定は Beat 1 の 1 回だけ — 空文字での確定は起きない
    assert runtime._emit_speak_finalize.call_count == 1
    assert "やるね。" in runtime._emit_speak_finalize.call_args.args[3]
    # 2 周目の空の行は取り下げられる (行を作った部屋に対して)
    runtime._withdraw_speak_placeholder.assert_called_once()
    assert runtime._withdraw_speak_placeholder.call_args.args[1] == "b1"
    assert runtime._withdraw_speak_placeholder.call_args.args[2] == "msg-2"
    # 本文が無いので、記憶にも中断の通告にも何も書かない
    persona.history_manager.add_to_building_only.assert_not_called()
    # 宙吊りの吹き出しを消す取り消しが、取り下げの後に流れる
    assert marks, "取り下げが呼ばれていない"
    after = _discards_after(events, marks[0])
    assert after, "取り下げた行の吹き出しを消す取り消しが流れていない"
    assert after[0]["building_id"] == "b1"


def test_a_dying_beat_that_said_something_is_still_confirmed(monkeypatch):
    """一語でも流れた回は取り下げない — 画面と音声に出た言葉を消さない。"""
    runtime, persona, node, events = _build_spell_node(
        monkeypatch, calls=[[f"やるね。\n{SPELL_LINE}"], ["二周目の言葉。"]],
    )
    runtime._withdraw_speak_placeholder.return_value = True
    state = _spell_state(_cancellation_token=_CancelAtCheck(4))

    with pytest.raises(LLMError):
        asyncio.run(node(state))

    runtime._withdraw_speak_placeholder.assert_not_called()
    assert runtime._emit_speak_finalize.call_count == 2
    assert runtime._emit_speak_finalize.call_args_list[1].args[3] == "二周目の言葉。"


def test_a_refused_withdrawal_on_beat_death_still_confirms_the_row(monkeypatch):
    """取り下げを断られた回は従来の確定へ落ちる — 未確定の行を残さない。"""
    runtime, persona, node, events = _build_spell_node(
        monkeypatch, calls=[[f"やるね。\n{SPELL_LINE}"], []],
    )
    runtime._withdraw_speak_placeholder.return_value = False
    state = _spell_state(_cancellation_token=_CancelAtCheck(3))

    with pytest.raises(LLMError):
        asyncio.run(node(state))

    assert runtime._emit_speak_finalize.call_count == 2
    second = runtime._emit_speak_finalize.call_args_list[1]
    assert second.args[2] == "msg-2"
    assert second.args[3] == ""


def test_a_salvaged_beat_is_not_confirmed_again_at_the_exit(monkeypatch):
    """中間 Beat の確定が失敗して建物へ退避した回 — 出口が同じ本文を重ねない。

    退避に成功したら器の印 (`finalized`) を立てる。立てないと、退避の直後
    (次の下書き行を開ける前) に Beat が死んだとき、出口の後始末が「まだ確定
    していない行」を見つけて同じ本文でもう一度確定し、記憶にも書く — 建物に
    2 件、記憶に 2 件の同じ発言が残る (三巡目レビュー N-3)。
    """
    from sea.runtime_emitters import SpeakFinalizeResult

    runtime, persona, node, events = _build_spell_node(
        monkeypatch, calls=[[f"やるね。\n{SPELL_LINE}"], ["終わりました。"]],
        node_def=_memorize_node_def(),
    )
    # 中間 Beat の確定が「対象行なし」で返る = 本文は建物へ直接退避される
    runtime._emit_speak_finalize.return_value = SpeakFinalizeResult(
        status="missing", building_msg=None,
    )

    # 退避の直後、次の下書き行を開ける前に Beat を殺す。Beat 境界の直後に通る
    # ツール一覧の取り直しがちょうどこの窓にある。
    def _die_right_after_the_salvage(*args, **kwargs):
        raise ExecutionCancelledException(
            message="stopped right after the salvage", interrupted_by="user",
        )

    monkeypatch.setattr(
        runtime_llm, "refresh_mcp_tools_at_head", _die_right_after_the_salvage,
    )
    runtime._withdraw_speak_placeholder.return_value = True

    with pytest.raises(LLMError):
        asyncio.run(node(_spell_state()))

    # 確定の試みは退避の 1 回だけ (出口は器の印を見て手を出さない)
    assert runtime._emit_speak_finalize.call_count == 1
    assert runtime._emit_speak_finalize.call_args.args[2] == "msg-1"
    # 退避済みの行を取り下げにも行かない
    runtime._withdraw_speak_placeholder.assert_not_called()
    # 建物への退避は 1 件だけ
    assert runtime._emit_say.call_count == 1
    assert "やるね。" in runtime._emit_say.call_args.args[2]
    # 記憶にもこの周の本文は 1 回だけ (出口の後始末が重ねて書かない)
    said_again = [
        c for c in runtime._store_memory.call_args_list
        if isinstance(c.args[1], str) and "やるね。" in c.args[1]
    ]
    assert len(said_again) == 1
    # 中断の通告 (settle の副産物) も出ない
    persona.history_manager.add_to_building_only.assert_not_called()


def test_a_node_without_memorize_gets_no_backfill(monkeypatch):
    """memorize 設定の無いノード — 記憶に書かない設計の発言を補填で書かない。"""
    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _no_spells(**kwargs):
        return runtime_llm.SpellLoopResult(
            segments=[], final_continuation=kwargs["text"], loop_count=0,
        )

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_no_spells,
    )
    runtime._dump_llm_io.side_effect = RuntimeError("boom before memorize")

    with pytest.raises(LLMError):
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    runtime._store_memory.assert_not_called()


# ---------------------------------------------------------------------------
# 建物へ本文を書く口は下書き行の確定だけではない — `_emit_say_and_capture` の
# 直接書き込み (非ストリーミング / fallback / tool streaming) の後に死んだ回も
# 「建物には本文があるのに記憶が無い」形で、補填の対象 (2026-08-27 Codex 指摘)。
# ---------------------------------------------------------------------------

class _FakeSyncClient:
    """generate だけを持つ最小の LLM クライアント (非ストリーミング経路用)。"""

    config_key = None

    def __init__(self, reply):
        self._reply = reply

    def generate(self, messages, tools=(), temperature=None,
                 response_schema=None, **kwargs):
        return self._reply

    def consume_usage(self):
        return None

    def consume_thought_signature(self):
        return None


def test_a_death_after_a_non_streaming_say_backfills_the_memory(monkeypatch):
    """非ストリーミング (同期) 経路 — say の後の例外死でも記憶が補填される。

    この経路は下書き行を作らない (pipeline_finalized が立たない) ので、
    say 直書きの印 (`beat_said`) が補填の発火条件に要る。"""
    client = _FakeSyncClient("こんにちは。")

    async def _no_spells(**kwargs):
        return runtime_llm.SpellLoopResult(
            segments=[], final_continuation=kwargs["text"], loop_count=0,
        )

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_no_spells,
        node_def=_memorize_node_def(),
    )
    monkeypatch.setattr(runtime_llm, "_is_llm_streaming_enabled", lambda: False)
    # say (branch: 非ストリーミング) の後、_finalize_beat の手前で通る
    # _dump_llm_io で落とす
    runtime._dump_llm_io.side_effect = RuntimeError("boom after the sync say")

    with pytest.raises(LLMError):
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    # 下書き行は無いので、確定は呼ばれない
    runtime._emit_speak_finalize.assert_not_called()
    # 補填が say した本文で 1 回書く
    runtime._store_memory.assert_called_once()
    assert runtime._store_memory.call_args.args[1] == "こんにちは。"


def test_a_death_after_the_fallback_say_backfills_the_memory(monkeypatch):
    """下書き行の発番に失敗した回の fallback say — その後の例外死でも補填される。"""
    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _no_spells(**kwargs):
        return runtime_llm.SpellLoopResult(
            segments=[], final_continuation=kwargs["text"], loop_count=0,
        )

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_no_spells,
        node_def=_memorize_node_def(),
    )
    runtime._emit_speak_start.return_value = None
    runtime._dump_llm_io.side_effect = RuntimeError("boom after the fallback say")

    with pytest.raises(LLMError):
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    # 下書き行を作れなかったので、確定は無い — 本文は fallback の _emit_say で
    # 建物に入り、記憶は補填が埋める
    runtime._emit_speak_finalize.assert_not_called()
    runtime._store_memory.assert_called_once()
    assert runtime._store_memory.call_args.args[1] == "こんにちは。"


def test_an_important_only_backfill_writes_the_same_shape_as_the_dual_write(monkeypatch):
    """important のみのノードの補填 — 通常の dual-write と同一の引数集合で書く。

    `_store_beat_memory` 経由だと pulse_context / paired_action_text / scope /
    line_role / spell_origin_id が付き、同じノードの発言が死に方で違う形になる
    (2026-08-27 Codex 指摘の固定)。"""
    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _no_spells(**kwargs):
        return runtime_llm.SpellLoopResult(
            segments=[], final_continuation=kwargs["text"], loop_count=0,
        )

    node_def = _node_def()
    node_def.memorize = None
    node_def.important = True
    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_no_spells, node_def=node_def,
    )
    # 確定 (branch 3) の後、_finalize_beat の手前で通る _dump_llm_io で落とす
    runtime._dump_llm_io.side_effect = RuntimeError("boom before the dual-write")

    state = {"_messages": [], "_pulse_id": "pl-1"}
    with pytest.raises(LLMError):
        asyncio.run(node(state))

    runtime._store_memory.assert_called_once()
    call = runtime._store_memory.call_args
    assert call.args[1] == "こんにちは。"
    assert call.kwargs["tags"] == ["conversation"]
    assert call.kwargs["playbook_name"] == "pb"
    # dual-write に無い引数は補填でも渡さない
    for absent in ("paired_action_text", "spell_origin_id", "spell_seq",
                   "scope", "line_role", "pulse_context"):
        assert absent not in call.kwargs, absent
    # 書けた回は「もう記憶に書かれた」の印が立つ
    assert state["_beat_memorized"] is True


# ---------------------------------------------------------------------------
# サーバーがストリームを途中で切った回 — 停止ボタン・Beat 死亡と同じ通告を書く
# (docs/issues/server_cut_stream_writes_no_interruption_notice.md、2026-09-13)。
#
# この経路だけ通告が無いと、会話の末尾がペルソナ本人の途中発言のままになる。
# 続きの生成のプロンプト末尾がモデル発話になり、プリフィルを受け付けない
# Gemini 3.x はそこで生成ごと拒否する。
# ---------------------------------------------------------------------------

class _CutStreamClient(_FakeStreamClient):
    """chunk を流し切った後に「サーバーが切った」を 1 度だけ申告する。

    ``consume_stream_error`` は消費型 — 2 度目からは None を返す (実物の
    llm_clients と同じ契約)。
    """

    def __init__(self, chunks, error):
        super().__init__(chunks=chunks)
        self._error = error

    def consume_stream_error(self):
        err, self._error = self._error, None
        return err


_CUT = {"code": 500, "message": "internal error", "status": "INTERNAL"}


def _server_cut_node(monkeypatch, *, chunks=("言いかけた本文",)):
    client = _CutStreamClient(chunks=list(chunks), error=dict(_CUT))

    async def _no_spells(**kwargs):
        return runtime_llm.SpellLoopResult(
            segments=[], final_continuation=kwargs["text"], loop_count=0,
        )

    return _build_node(monkeypatch, client=client, spell_loop=_no_spells)


def test_a_server_cut_stream_writes_the_same_interruption_notice(monkeypatch):
    """サーバー切断の回も、停止経路と同じ一枚の通告を建物の記録へ置く。

    文面は原因を書かない一文 (中断させたのはユーザーではない)、役は host、
    ``heard_by`` は在室者 + 本人 — 在室者を渡さないと取り込みが配らず、
    通告は誰の記憶にも届かない。返事は例外なしで閉じるので、置くのは
    後始末 (保存した側の「この後で話が止まった」の書き足しを見て)。
    """
    runtime, persona, node, events = _server_cut_node(monkeypatch)
    _run_reply(node, {"_messages": [], "_pulse_id": "pl-1"}, runtime, persona, events)

    call = persona.history_manager.add_to_building_only.call_args
    assert call is not None, "サーバー切断の回に中断の通告が書かれていない"
    building_id, message = call.args
    assert building_id == "b1"
    assert message["role"] == "host"
    assert message["content"] == "(ここで発言が中断されました)"
    assert call.kwargs["heard_by"] == ["1", "p1"]


def test_a_server_cut_stream_settles_the_partial_text_before_the_notice(
    monkeypatch,
):
    """建物の記録の並びは「途中の発言 → 印 → 通告」。通告を先に置くと、他の
    ペルソナの記憶に「何が中断されたのか分からない一行」だけが残る。"""
    runtime, persona, node, events = _server_cut_node(monkeypatch)

    order: list = []
    _real_finalize = runtime._emit_speak_finalize.side_effect

    def _finalize(*args, **kwargs):
        order.append("finalize")
        return _real_finalize(*args, **kwargs)

    runtime._emit_speak_finalize.side_effect = _finalize
    _real_update = persona.history_manager.update_building_message.side_effect

    def _update(*args, **kwargs):
        order.append("mark")
        return _real_update(*args, **kwargs)

    persona.history_manager.update_building_message.side_effect = _update
    def _notice(building_id, msg, *, heard_by=None):
        order.append("notice")
        return {**msg, "message_id": f"{building_id}:notice"}

    persona.history_manager.add_to_building_only.side_effect = _notice

    _run_reply(node, {"_messages": [], "_pulse_id": "pl-1"}, runtime, persona, events)

    assert order == ["finalize", "mark", "notice"]
    # 確定は部分文つきで、通告と同じ部屋へ
    assert runtime._emit_speak_finalize.call_args.args[1] == "b1"
    assert runtime._emit_speak_finalize.call_args.args[3] == "言いかけた本文"


def test_a_server_cut_stream_notice_does_not_draw_its_own_icon(monkeypatch):
    """画面向けの通知の文面にアイコンを書かない — 画面側が info 種別に
    自前で ℹ️ を描くので、書くと二つ並ぶ (2026-09-13 まはー実機報告)。"""
    runtime, persona, node, events = _server_cut_node(monkeypatch)
    _run_reply(node, {"_messages": [], "_pulse_id": "pl-1"}, runtime, persona, events)

    infos = [e for e in events if e.get("type") == "info"]
    assert len(infos) == 1
    content = infos[0]["content"]
    assert not content.startswith("ℹ️")
    assert "ℹ️" not in content
    assert content.startswith("メッセージの生成が途中で終了しました。")


def test_a_server_cut_stream_marks_the_utterance_as_unfinished(monkeypatch):
    """切れた発言の行に「言い切っていない」印が付き、情報の知らせに「続きの生成」を
    出す発言の id が載る (エラー札ではなく知らせのまま)。"""
    runtime, persona, node, events = _server_cut_node(monkeypatch)
    state = {"_messages": [], "_pulse_id": "pl-1"}
    _run_reply(node, state, runtime, persona, events)

    assert _marked(persona) == [("b1", "msg-1")]
    infos = [e for e in events if e.get("type") == "info"]
    assert infos[0]["interrupted_message_id"] == "msg-1"
    assert "interrupted_building_id" not in infos[0]
    assert not [e for e in events if e.get("type") == "error"]


def test_a_clean_stream_writes_no_interruption_notice(monkeypatch):
    """切られなかった回に通告は出ない (新しい書き込みの入口の裏側)。"""
    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _no_spells(**kwargs):
        return runtime_llm.SpellLoopResult(
            segments=[], final_continuation=kwargs["text"], loop_count=0,
        )

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_no_spells,
    )
    _run_reply(node, {"_messages": [], "_pulse_id": "pl-1"}, runtime, persona, events)

    persona.history_manager.add_to_building_only.assert_not_called()
    assert _marked(persona) == []
    assert [e for e in events if e.get("type") == "info"] == []


def test_a_server_cut_stream_with_a_failed_finalize_writes_no_notice(monkeypatch):
    """部分文の確定が保存に失敗した回は、通告を書かない。

    通告は「途中で終わった発言」の後ろに置く注記なので、その発言が建物の
    記録に載らなかった回に書くと、見えない行の後ろに通告だけが浮く。画面への
    知らせ (info) は保存の成否と無関係に事実なので、こちらは出る。
    """
    runtime, persona, node, events = _server_cut_node(monkeypatch)
    runtime._emit_speak_finalize.return_value = SpeakFinalizeResult(
        status="failed", building_msg=None,
    )
    _run_reply(node, {"_messages": [], "_pulse_id": "pl-1"}, runtime, persona, events)

    persona.history_manager.add_to_building_only.assert_not_called()
    assert [e for e in events if e.get("type") == "info"], (
        "画面への知らせまで消してはいけない"
    )


def test_a_server_cut_fallback_emit_writes_the_notice_where_it_landed(monkeypatch):
    """下書き行を作れなかった救済経路でも、本文が建物へ載れたなら通告を書く。

    宛先は本文を載せたのと同じ部屋 (eff_bid) — 載った部屋と通告の部屋を
    食い違わせない。
    """
    runtime, persona, node, events = _server_cut_node(monkeypatch)
    runtime._emit_speak_start.return_value = None  # 下書き行を作れなかった回
    runtime._emit_say.return_value = {"message_id": "say-1", "content": "言いかけた本文"}
    _run_reply(node, {"_messages": [], "_pulse_id": "pl-1"}, runtime, persona, events)

    assert _marked(persona) == [("b1", "say-1")]
    assert _notices(persona) == [("b1", "(ここで発言が中断されました)")]


def test_a_server_cut_fallback_emit_that_did_not_persist_writes_no_notice(monkeypatch):
    """救済の書き込みが DB に載らなかった回 (message_id なし) は、通告を書かない。

    「書けた」の判定は message_id の有無 — DB 採番なので insert が通った
    ときにしか付かない (builtin_data/tools/tell.py の裁定と同じ)。dict が
    返っただけで通告を書くと、見えない行の後ろに通告だけが浮く。
    """
    runtime, persona, node, events = _server_cut_node(monkeypatch)
    runtime._emit_speak_start.return_value = None
    runtime._emit_say.return_value = {"content": "言いかけた本文"}  # 採番なし
    _run_reply(node, {"_messages": [], "_pulse_id": "pl-1"}, runtime, persona, events)

    persona.history_manager.add_to_building_only.assert_not_called()
    assert [e for e in events if e.get("type") == "info"], (
        "画面への知らせまで消してはいけない"
    )


# ---------------------------------------------------------------------------
# スペルが走った回のストリーム切断 (docs/issues/spell_round_stream_cut_is_not_detected.md)
#
# 切断の申告の消費はスペルループへ引き継がれ、スペル行を含む本文の切断 (次の周が
# 発話を続けるので自己回復する) と、締めの周の切断 (発言が途切れたまま確定する)
# を見分ける。後者にだけ印と通告を付ける。
# ---------------------------------------------------------------------------

class _ScriptedCutStreamClient(_ScriptedStreamClient):
    """呼び出しごとの切断の申告を返す。申告は消費型 (実物の llm_clients と同じ)。"""

    def __init__(self, calls, cuts):
        super().__init__(calls)
        self._cuts = list(cuts)
        self._pending = None

    def generate_stream(self, messages, tools=(), temperature=None, **kwargs):
        self._pending = self._cuts.pop(0) if self._cuts else None
        return super().generate_stream(
            messages, tools=tools, temperature=temperature, **kwargs,
        )

    def consume_stream_error(self):
        err, self._pending = self._pending, None
        return err


def _build_cut_spell_node(monkeypatch, *, calls, cuts):
    """呼び出しごとの chunk 列と、呼び出しごとの切断の申告 (None = 切られていない)。"""
    client = _ScriptedCutStreamClient(calls, cuts)
    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=runtime_llm._run_spell_loop,
    )
    runtime._emit_speak_start.side_effect = ["msg-1", "msg-2", "msg-3"]
    monkeypatch.setattr(runtime_llm, "SPELL_TOOL_NAMES", {SPELL_NAME})
    monkeypatch.setattr(runtime_llm, "_run_spell_tool_async", _ok_spell)
    return runtime, persona, node, events


def test_a_cut_on_a_round_with_a_spell_recovers_without_a_mark(monkeypatch):
    """スペル行を含む本文をサーバーが切った回 — 次の周が発話を続けるので、
    印も通告も付かない (実際にスペル分岐を通す。残留を手で仕込まない)。

    旧実装では、この回の申告が state に残留し、同じ Pulse の次の Beat の
    言い切った発言に偽の印と通告が乗った (2026-09-13 検算)。
    """
    runtime, persona, node, events = _build_cut_spell_node(
        monkeypatch,
        calls=[[f"やるね。\n{SPELL_LINE}"], ["できたよ。"]],
        cuts=[dict(_CUT), None],
    )
    state = _spell_state()
    _run_reply(node, state, runtime, persona, events)

    assert runtime._emit_speak_finalize.call_count == 2
    assert runtime._emit_speak_finalize.call_args_list[1].args[3] == "できたよ。"
    assert _marked(persona) == []
    assert _notices(persona) == []
    assert [e for e in events if e.get("type") == "info"] == []
    # 申告を state に置かない (次の Beat へ残留させない)
    assert "_stream_error" not in state
    assert INTERRUPTED_METADATA_KEY not in state


def test_a_cut_on_the_closing_round_marks_the_cut_utterance(monkeypatch):
    """スペルの後の締めの生成がサーバーに切られた回 — 途切れたまま確定した
    締めの発言に印と通告 (①) が付き、知らせにその発言の id が載る。"""
    runtime, persona, node, events = _build_cut_spell_node(
        monkeypatch,
        calls=[[f"やるね。\n{SPELL_LINE}"], ["できたよ、でも"]],
        cuts=[None, dict(_CUT)],
    )
    _run_reply(node, _spell_state(), runtime, persona, events)

    assert runtime._emit_speak_finalize.call_args_list[1].args[2] == "msg-2"
    assert runtime._emit_speak_finalize.call_args_list[1].args[3] == "できたよ、でも"
    assert _marked(persona) == [("b1", "msg-2")]
    assert _notices(persona) == [("b1", "(ここで発言が中断されました)")]
    infos = [e for e in events if e.get("type") == "info"]
    assert len(infos) == 1
    assert infos[0]["interrupted_message_id"] == "msg-2"


def test_a_closing_round_cut_before_any_word_marks_the_spell_utterance(monkeypatch):
    """締めの生成が一文字も来ないうちに切られた回 — 話はスペルの結果で終わる
    発言の後で止まった。その発言に印と ③ の通告を付ける (結果は行に残っている)。"""
    runtime, persona, node, events = _build_cut_spell_node(
        monkeypatch,
        calls=[[f"やるね。\n{SPELL_LINE}"], []],
        cuts=[None, dict(_CUT)],
    )
    runtime._withdraw_speak_placeholder.return_value = True
    _run_reply(node, _spell_state(), runtime, persona, events)

    # 締めのために開けた行は、一文字も無いので取り下げる
    runtime._withdraw_speak_placeholder.assert_called_once()
    first = runtime._emit_speak_finalize.call_args_list[0]
    assert first.args[2] == "msg-1"
    assert "やりました" in first.args[3]  # 受け取った結果が行にある
    assert _marked(persona) == [("b1", "msg-1")]
    assert _notices(persona) == [
        ("b1", "(スペルの結果を受け取った後、続きの発言の前に中断されました)"),
    ]
    infos = [e for e in events if e.get("type") == "info"]
    assert infos and infos[0]["interrupted_message_id"] == "msg-1"


def test_a_gemini_prompt_block_reaches_the_caller_as_a_safety_filter_error(monkeypatch):
    """Gemini がプロンプトを拒んだ回 — 本物の GeminiClient.generate_stream から
    node() の外まで、SafetyFilterError のまま届く (汎用の LLMError に包まれない)。

    manager/runtime.py の ``except LLMError`` はこの例外の ``to_dict()`` を
    error イベントとして流すので、画面は error_code=safety_filter の札を出せる。
    以前はブロックの chunk (candidates=None) を読み飛ばして空のストリームで
    終わり、理由の無い「返事が生まれませんでした」になっていた (2026-09-24)。
    """
    from llm_clients import gemini as gemini_module
    from llm_clients.exceptions import SafetyFilterError

    monkeypatch.setattr(
        gemini_module, "build_gemini_clients",
        lambda prefer_paid=False: (MagicMock(), None, MagicMock()),
    )
    client = gemini_module.GeminiClient("gemini-3.8-flash")
    block_chunk = SimpleNamespace(
        candidates=None,
        prompt_feedback=SimpleNamespace(block_reason="PROHIBITED_CONTENT"),
        usage_metadata=None,
    )
    monkeypatch.setattr(client, "_start_stream", lambda *a, **k: iter([block_chunk]))

    async def _unused_spell_loop(**kwargs):  # pragma: no cover - 到達しない
        raise AssertionError("spell loop must not run")

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_unused_spell_loop,
    )
    runtime._withdraw_speak_placeholder.return_value = True
    with pytest.raises(SafetyFilterError) as excinfo:
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    event = excinfo.value.to_dict()
    assert event["type"] == "error"
    assert event["error_code"] == "safety_filter"
    assert "PROHIBITED_CONTENT" in event["content"]
    # 本文ゼロなので下書き行は取り下げ、空の記録を残さない
    runtime._withdraw_speak_placeholder.assert_called_once()
    runtime._store_memory.assert_not_called()


# ---------------------------------------------------------------------------
# スペルの後の続きの生成 (LLM 呼び出し) が失敗した回 (2026-09-24)
#
# 以前はスペルループの包括 except が LLMError を握り潰し、
# 「[Spell System Error]」の注記を差し込んで途中までの発言だけを保存していた。
# 画面には止まった理由が一切届かなかった。安全性フィルターで最初に塞ぎ、
# 同じ理由が当てはまる LLMError の族 (利用制限・タイムアウト・サーバー
# エラー・空の応答…) へ広げた。不変条件は「普通の返事で同じ失敗が起きた回と
# 同じエラーが画面へ届き、それまでの発言は残る」。
# ---------------------------------------------------------------------------

def _prompt_block() -> Exception:
    from llm_clients.exceptions import SafetyFilterError
    return SafetyFilterError(
        "Gemini blocked the prompt (block_reason=PROHIBITED_CONTENT)",
        user_message="Geminiの安全性フィルターにより、応答がブロックされました（PROHIBITED_CONTENT）",
    )


def _continuation_errors():
    """続きの生成で起きうる LLMError の代表。id は pytest の表示用。

    空の応答 (EmptyResponseError) はここに入れない — スペルの後の沈黙は正常な
    終わり方で、エラーにしない (下の専用テスト)。
    """
    from llm_clients.exceptions import (
        LLMTimeoutError,
        PaymentError,
        RateLimitError,
        ServerError,
    )
    return [
        pytest.param(_prompt_block, id="safety_filter"),
        pytest.param(lambda: RateLimitError("429 Too Many Requests"), id="rate_limit"),
        pytest.param(lambda: LLMTimeoutError("read timed out"), id="timeout"),
        pytest.param(lambda: ServerError("503 Service Unavailable"), id="server_error"),
        pytest.param(lambda: PaymentError("402 Payment Required"), id="payment"),
        pytest.param(lambda: LLMError("provider exploded"), id="llm_error"),
    ]


class _RecordingScriptedStreamClient(_ScriptedStreamClient):
    """渡された messages の参照を控える — 後から注記が積まれたかを見るため。"""

    def __init__(self, calls):
        super().__init__(calls)
        self.seen_messages: list = []

    def generate_stream(self, messages, tools=(), temperature=None, **kwargs):
        self.seen_messages = messages
        return super().generate_stream(messages, tools=tools, temperature=temperature, **kwargs)


class _ScriptedSyncClient:
    """呼び出しごとに別の応答を返す非ストリーミングのクライアント。

    要素は返す本文か、``generate`` の瞬間に投げる例外。
    """

    config_key = None

    def __init__(self, calls):
        self._calls = list(calls)
        self.seen_messages: list = []

    def generate(self, messages, tools=(), temperature=None,
                 response_schema=None, **kwargs):
        assert self._calls, "_ScriptedSyncClient: no scripted calls left"
        self.seen_messages = messages
        nxt = self._calls.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def consume_usage(self):
        return None

    def consume_thought_signature(self):
        return None


def _has_spell_system_error_note(messages) -> bool:
    return any(
        "[Spell System Error]" in str(m.get("content", ""))
        for m in messages if isinstance(m, dict)
    )


@pytest.mark.parametrize("dies", ["at_call", "mid_stream"])
@pytest.mark.parametrize("make_error", _continuation_errors())
def test_a_failed_continuation_after_a_spell_reports_the_error(monkeypatch, make_error, dies):
    """ストリーミング経路 — 前の Beat は確定のまま、続きの空の行は取り下げ、
    理由は元の LLMError のまま (包み直さずに) node() の外へ届く。

    ``dies``: 続きの呼び出しの瞬間に死ぬ回と、ストリームを読み始めてから
    一語も来ないうちに死ぬ回。どちらも続きの生成そのものの失敗。
    """
    err = make_error()
    continuation = err if dies == "at_call" else [err]
    client = _RecordingScriptedStreamClient(
        [[f"やるね。\n{SPELL_LINE}"], continuation],
    )
    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=runtime_llm._run_spell_loop,
    )
    runtime._emit_speak_start.side_effect = ["msg-1", "msg-2", "msg-3"]
    monkeypatch.setattr(runtime_llm, "SPELL_TOOL_NAMES", {SPELL_NAME})
    monkeypatch.setattr(runtime_llm, "_run_spell_tool_async", _ok_spell)
    runtime._withdraw_speak_placeholder.return_value = True

    with pytest.raises(type(err)) as excinfo:
        asyncio.run(node(_spell_state()))

    # 画面へ流れる error イベントは、普通の返事で同じ失敗が起きた回と同じ形
    # (同じオブジェクトがそのまま届く = error_code も文面も同じ)
    assert excinfo.value is err
    assert excinfo.value.to_dict()["error_code"] == err.error_code
    # スペルを唱えた Beat は確定のまま残る (確定は 1 回だけ)
    runtime._emit_speak_finalize.assert_called_once()
    assert runtime._emit_speak_finalize.call_args.args[2] == "msg-1"
    assert "やるね。" in runtime._emit_speak_finalize.call_args.args[3]
    # 失敗した続きのために開けた行は、空の記録として残さず取り下げる
    runtime._withdraw_speak_placeholder.assert_called_once()
    assert runtime._withdraw_speak_placeholder.call_args.args[2] == "msg-2"
    # スペル系の内部エラーではないので、注記も中断の通告も書かない
    assert not _has_spell_system_error_note(client.seen_messages)
    persona.history_manager.add_to_building_only.assert_not_called()


@pytest.mark.parametrize("make_error", _continuation_errors())
def test_the_same_failure_on_an_ordinary_reply_reaches_the_chat_the_same_way(
    monkeypatch, make_error,
):
    """比較の基準 — スペルの無い普通の返事で同じ失敗が起きた回も、元の例外が
    そのまま node() の外へ出る。続きの生成の失敗はこれと同じ形に揃える。"""
    err = make_error()
    client = _RecordingScriptedStreamClient([err])
    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=runtime_llm._run_spell_loop,
    )
    runtime._withdraw_speak_placeholder.return_value = True

    with pytest.raises(type(err)) as excinfo:
        asyncio.run(node(_spell_state()))

    assert excinfo.value is err


@pytest.mark.parametrize("make_error", _continuation_errors())
def test_a_failed_continuation_without_streaming_keeps_the_earlier_beats(monkeypatch, make_error):
    """非ストリーミング経路 — 周の発言は呼び出し元が建物へ書き終えてから、
    元の LLMError が投げられる (ループの中で投げると周の発言が消える)。"""
    err = make_error()
    client = _ScriptedSyncClient([f"やるね。\n{SPELL_LINE}", err])
    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=runtime_llm._run_spell_loop,
    )
    monkeypatch.setattr(runtime_llm, "_is_llm_streaming_enabled", lambda: False)
    monkeypatch.setattr(runtime_llm, "SPELL_TOOL_NAMES", {SPELL_NAME})
    monkeypatch.setattr(runtime_llm, "_run_spell_tool_async", _ok_spell)
    runtime._emit_say.return_value = {"message_id": "say-1", "content": "x"}

    with pytest.raises(type(err)) as excinfo:
        asyncio.run(node(_spell_state()))

    assert excinfo.value is err
    assert excinfo.value.to_dict()["error_code"] == err.error_code
    # スペルの結果を持つ Beat 1 が建物へ書かれている (失敗の前の発言は残る)
    said_texts = [c.args[2] for c in runtime._emit_say.call_args_list]
    assert any("やりました" in t for t in said_texts), said_texts
    # 失敗した続きの分は何も書かない (空の記録を残さない)
    assert all(t.strip() for t in said_texts)
    # 下書き行を使わない経路なので、確定も取り下げも起きない
    runtime._emit_speak_finalize.assert_not_called()
    runtime._withdraw_speak_placeholder.assert_not_called()
    assert not _has_spell_system_error_note(client.seen_messages)


def test_an_empty_continuation_without_streaming_ends_silently(monkeypatch):
    """全文一括のクライアントがスペルの後の空の応答を EmptyResponseError に
    した回も、ストリーミング経路の「空の本文」と同じく黙って終わる。
    受け取り方の違いで、同じ沈黙がエラーの札になってはいけない。"""
    from llm_clients.exceptions import EmptyResponseError

    client = _ScriptedSyncClient(
        [f"やるね。\n{SPELL_LINE}", EmptyResponseError("empty response")],
    )
    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=runtime_llm._run_spell_loop,
    )
    monkeypatch.setattr(runtime_llm, "_is_llm_streaming_enabled", lambda: False)
    monkeypatch.setattr(runtime_llm, "SPELL_TOOL_NAMES", {SPELL_NAME})
    monkeypatch.setattr(runtime_llm, "_run_spell_tool_async", _ok_spell)
    runtime._emit_say.return_value = {"message_id": "say-1", "content": "x"}

    asyncio.run(node(_spell_state()))

    said_texts = [c.args[2] for c in runtime._emit_say.call_args_list]
    assert any("やりました" in t for t in said_texts), said_texts
    assert all(t.strip() for t in said_texts)
    assert not _has_spell_system_error_note(client.seen_messages)


# ---------------------------------------------------------------------------
# スペルの「実行中」に起きた LLMError はスペルの失敗のまま (2026-09-24)
#
# 画面のエラーに変えるのは続きを生成する呼び出しの失敗だけ。スペルのツールが
# 中で LLM を呼んで失敗した回 (サブラインの返事など) はペルソナの返事の失敗では
# ないので、従来どおりスペルの失敗として扱う。型で見分けると両者が混ざる。
# ---------------------------------------------------------------------------

def _rate_limit() -> Exception:
    from llm_clients.exceptions import RateLimitError
    return RateLimitError("429 Too Many Requests")


@pytest.mark.parametrize(
    "make_error",
    [
        pytest.param(_prompt_block, id="safety_filter"),
        pytest.param(_rate_limit, id="rate_limit"),
    ],
)
def test_an_llm_error_that_escapes_spell_execution_stays_a_spell_system_error(
    monkeypatch, make_error,
):
    """スペルの実行から LLMError が漏れてループの包括 except へ届いた回は、
    従来どおり注記を差し込んで途中までの発言を返す (投げない)。"""
    err = make_error()

    async def _spell_raises(*args, **kwargs):
        raise err

    client = _ScriptedSyncClient([f"やるね。\n{SPELL_LINE}"])
    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=runtime_llm._run_spell_loop,
    )
    monkeypatch.setattr(runtime_llm, "_is_llm_streaming_enabled", lambda: False)
    monkeypatch.setattr(runtime_llm, "SPELL_TOOL_NAMES", {SPELL_NAME})
    monkeypatch.setattr(runtime_llm, "_run_spell_tool_async", _spell_raises)
    runtime._emit_say.return_value = {"message_id": "say-1", "content": "x"}

    asyncio.run(node(_spell_state()))

    # 漏れた例外そのものの注記が差し込まれている (投げずに降格した証拠)
    notes = [
        str(m.get("content", "")) for m in client.seen_messages
        if isinstance(m, dict) and "[Spell System Error]" in str(m.get("content", ""))
    ]
    assert notes and type(err).__name__ in notes[0], notes
    # 唱えた発言は途中までの形で建物に残る
    said_texts = [c.args[2] for c in runtime._emit_say.call_args_list]
    assert any("やるね。" in t for t in said_texts), said_texts


def test_a_spell_tool_whose_inner_llm_call_fails_is_a_spell_error(monkeypatch):
    """実物の ``_run_spell_tool_async`` — ツールの中の LLM 呼び出しが
    RateLimitError で落ちても、その周の結果が [Spell Error] になるだけで、
    ペルソナは続きを生成して返事を終える (画面のエラーにはならない)。"""
    from llm_clients.exceptions import RateLimitError

    def _tool_with_inner_llm(**kwargs):
        raise RateLimitError("429 from the sub-line's model")

    client = _ScriptedSyncClient([f"やるね。\n{SPELL_LINE}", "だめだったみたい。"])
    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=runtime_llm._run_spell_loop,
    )
    monkeypatch.setattr(runtime_llm, "_is_llm_streaming_enabled", lambda: False)
    monkeypatch.setattr(runtime_llm, "SPELL_TOOL_NAMES", {SPELL_NAME})
    monkeypatch.setitem(runtime_llm.TOOL_REGISTRY, SPELL_NAME, _tool_with_inner_llm)
    runtime._emit_say.return_value = {"message_id": "say-1", "content": "x"}

    asyncio.run(node(_spell_state()))

    # スペルの失敗として続きの生成に見せている
    assert any(
        "[Spell Error: " in str(m.get("content", "")) and "RateLimitError" in str(m.get("content", ""))
        for m in client.seen_messages if isinstance(m, dict)
    )
    assert not _has_spell_system_error_note(client.seen_messages)
    said_texts = [c.args[2] for c in runtime._emit_say.call_args_list]
    assert any("だめだったみたい。" in t for t in said_texts), said_texts


# ---------------------------------------------------------------------------
# 返事が止まった回の出口 — 止まり方の場面ごとに、行に残るもの・印・通告・案内
# (docs/intent/reply_stop_exit.md)
# ---------------------------------------------------------------------------

TWO_SPELLS = (
    "やるね。\n"
    f"/spell name='{SPELL_NAME}' args={{\"step\": 1}}\n"
    f"/spell name='{SPELL_NAME}' args={{\"step\": 2}}"
)


def _model_gone() -> ModelUnavailableError:
    return ModelUnavailableError(
        "the lightweight model is gone", role="lightweight_model", reason="missing",
    )


def _first_ok_then_model_gone():
    calls = {"n": 0}

    async def _spell(tool_name, tool_args, persona, state, playbook_name,
                     event_callback, messages=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return ("一つ目はやりました", None, True)
        raise _model_gone()

    return _spell


def test_a_stop_during_spell_execution_keeps_the_received_result_and_says_unknown(
    monkeypatch,
):
    """スペル群の一つ目の結果が来てから、二つ目の実行中に止まった回 (②)。

    行には受け取った一つ目の結果が残り、結果の来ていない二つ目は唱えた行だけが
    残る。通告は ② — 「実行されていない」とは断定しない。周の本文は周の頭で
    記憶に書かれているので重ねず、受け取った結果は記憶にも書く。
    """
    client = _ScriptedStreamClient([[TWO_SPELLS]])
    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=runtime_llm._run_spell_loop,
    )
    runtime._emit_speak_start.side_effect = ["msg-1", "msg-2"]
    monkeypatch.setattr(runtime_llm, "SPELL_TOOL_NAMES", {SPELL_NAME})
    monkeypatch.setattr(runtime_llm, "_run_spell_tool_async", _first_ok_then_model_gone())

    with pytest.raises(ModelUnavailableError) as excinfo:
        _run_reply(node, _spell_state(), runtime, persona, events)

    runtime._emit_speak_finalize.assert_called_once()
    call = runtime._emit_speak_finalize.call_args
    assert call.args[2] == "msg-1"
    row = call.args[3]
    assert row.startswith("やるね。")
    assert row.count("<user_only") == 2          # 唱えた行は二つとも残る
    assert row.count("spellResult") == 1         # 結果の折りたたみは一つ目だけ
    assert "一つ目はやりました" in row
    assert '"step": 2' in row
    # 記憶: 周の本文 (assistant) は周の頭の 1 回だけ、受け取った結果は system で
    assistant_rows = [
        c for c in runtime._store_memory.call_args_list
        if c.kwargs.get("role") == "assistant"
    ]
    assert len(assistant_rows) == 1
    system_rows = [
        c.args[1] for c in runtime._store_memory.call_args_list
        if c.kwargs.get("role") == "system"
    ]
    assert system_rows == [f"[Spell Result: {SPELL_NAME}]\n一つ目はやりました"]
    # 印と通告 (②) とエラー札の案内
    assert last_saved_utterance("p1").form == SAVED_FORM_SPELL_UNFINISHED
    assert _marked(persona) == [("b1", "msg-1")]
    (notice_room, notice), = _notices(persona)
    assert notice_room == "b1"
    assert notice == (
        "(発言の後に唱えたスペルは、実行が終わる前に中断されました。"
        "結果は届いておらず、どこまで実行されたかは不明です)"
    )
    assert "実行されていません" not in notice
    assert excinfo.value.to_dict()["interrupted_message_id"] == "msg-1"


def test_a_stop_during_spell_execution_without_streaming_writes_the_partial_round(
    monkeypatch,
):
    """ストリーミングを使わない経路の ② — それまでの周と止まった周の途中までを
    建物へ書いてから止まる (以前は周の発言が建物に書かれないまま止まった)。"""
    client = _ScriptedSyncClient([TWO_SPELLS])
    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=runtime_llm._run_spell_loop,
    )
    monkeypatch.setattr(runtime_llm, "_is_llm_streaming_enabled", lambda: False)
    monkeypatch.setattr(runtime_llm, "SPELL_TOOL_NAMES", {SPELL_NAME})
    monkeypatch.setattr(runtime_llm, "_run_spell_tool_async", _first_ok_then_model_gone())

    with pytest.raises(ModelUnavailableError) as excinfo:
        _run_reply(node, _spell_state(), runtime, persona, events)

    said = [c.args[2] for c in runtime._emit_say.call_args_list]
    # 早期の吹き出し (唱える前の文) と、止まった周の途中まで
    assert said[0] == "やるね。"
    assert "一つ目はやりました" in said[-1]
    assert said[-1].count("<user_only") == 2
    assert said[-1].count("spellResult") == 1
    assert _notices(persona) == [(
        "b1",
        "(発言の後に唱えたスペルは、実行が終わる前に中断されました。"
        "結果は届いておらず、どこまで実行されたかは不明です)",
    )]
    assert excinfo.value.to_dict()["interrupted_message_id"] == (
        last_saved_utterance("p1").message_id
    )


def test_a_failed_continuation_marks_the_spell_utterance_with_the_results_notice(
    monkeypatch,
):
    """スペルの結果を受け取った後、続きの生成が一文字も出ないうちに失敗した回 (③)。

    続きのために開けた行は取り下げ、印と通告はスペルの結果で終わる発言に付く。
    エラー札にはその発言の id が載る (「再送」ではなく「続きの生成」へ案内する)。
    """
    from llm_clients.exceptions import ServerError

    err = ServerError("503 Service Unavailable")
    runtime, persona, node, events = _build_spell_node(
        monkeypatch, calls=[[f"やるね。\n{SPELL_LINE}"], err],
    )
    runtime._withdraw_speak_placeholder.return_value = True

    with pytest.raises(ServerError) as excinfo:
        _run_reply(node, _spell_state(), runtime, persona, events)

    assert excinfo.value is err
    runtime._withdraw_speak_placeholder.assert_called_once()
    assert "やりました" in runtime._emit_speak_finalize.call_args_list[0].args[3]
    assert _marked(persona) == [("b1", "msg-1")]
    assert _notices(persona) == [
        ("b1", "(スペルの結果を受け取った後、続きの発言の前に中断されました)"),
    ]
    event = err.to_dict()
    assert event["error_code"] == "server_error"
    assert event["interrupted_message_id"] == "msg-1"
    assert "interrupted_building_id" not in event


def test_a_stop_in_the_room_the_persona_moved_to_names_that_room(monkeypatch):
    """スペルで部屋を移り、移った先の発言の途中で止まった回 — その部屋の行に印と
    通告が付き、エラー札には部屋の id と表示名が載る (返事の部屋では押せない)。"""
    from llm_clients.exceptions import ServerError

    err = ServerError("503 Service Unavailable")
    runtime, persona, node, events = _build_spell_node(
        monkeypatch,
        calls=[[f"移るね。\n{SPELL_LINE}"], ["移った先で話し", err]],
    )
    rooms = ["b1"]
    runtime._effective_building_id.side_effect = lambda _p, _fallback: rooms[-1]

    async def _moving_spell(tool_name, tool_args, persona_, state, playbook_name,
                            event_callback, messages=None):
        rooms.append("b2")
        return ("移動しました", None, True)

    monkeypatch.setattr(runtime_llm, "_run_spell_tool_async", _moving_spell)

    with pytest.raises(ServerError):
        _run_reply(node, _spell_state(), runtime, persona, events, reply_building_id="b1")

    second = runtime._emit_speak_finalize.call_args_list[1]
    assert (second.args[1], second.args[2], second.args[3]) == ("b2", "msg-2", "移った先で話し")
    assert _marked(persona) == [("b2", "msg-2")]
    assert _notices(persona) == [("b2", "(ここで発言が中断されました)")]
    event = err.to_dict()
    assert event["interrupted_message_id"] == "msg-2"
    assert event["interrupted_building_id"] == "b2"
    assert event["interrupted_building_name"] == "書斎"


def test_a_dying_beat_by_itself_places_no_mark_and_no_notice(monkeypatch):
    """Beat の出口 (サブラインの中の Beat も同じ) は保存だけ — 印と通告は返事の
    一番外側の後始末が一回だけ置く。内側で置くと、外側が後から書く発言との
    順序が狂う (前の実装の迷子の原因)。"""
    client = _FakeStreamClient(chunks=["こんにちは。"], iter_exc=RuntimeError("died mid-stream"))

    async def _unused_spell_loop(**kwargs):  # pragma: no cover - 到達しない
        raise AssertionError("spell loop must not run")

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_unused_spell_loop,
    )
    with pytest.raises(LLMError):
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    runtime._emit_speak_finalize.assert_called_once()
    assert _marked(persona) == []
    assert _notices(persona) == []
    assert not [e for e in events if e.get("interrupted")]
    # 流し込みの吹き出しは閉じる (印の無い合図で)。保存の合図で行の id も届く。
    assert [e for e in events if e.get("type") == "streaming_complete"]
    assert [e for e in events if e.get("type") == "speak_persisted"] == [{
        "type": "speak_persisted", "message_id": "msg-1", "persona_id": "p1",
        "pulse_id": "pl-1", "building_id": "b1",
    }]
    # 保存した事実 (形と「この後で話が止まった」) だけが記録に残る
    record = last_saved_utterance("p1")
    assert (record.message_id, record.form, record.stopped) == ("msg-1", SAVED_FORM_CUT, True)


# ---------------------------------------------------------------------------
# 2026-09-25 レビュー消し込み — 回帰テスト
# ---------------------------------------------------------------------------

def test_a_stop_without_a_draft_row_is_not_swallowed(monkeypatch):
    """ストリーミングの口で下書き行が作れなかった回 — ループはストリーミングを
    使わずに走り、止まった理由を例外ではなく戻り値 (stop_error) で返す。

    呼び出し口がそれを投げ直さないと、返事を止める例外 (ここでは使うモデルが
    無い回) が握り潰され、返事が「正常に終わった」ことになる。ほかの三つの口
    (ツールモード・全文一括・作業セッション) と同じく、周の発言を建物へ
    書き終えてから投げる。
    """
    client = _ScriptedStreamClient([[TWO_SPELLS]])
    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=runtime_llm._run_spell_loop,
    )
    runtime._emit_speak_start.return_value = None
    monkeypatch.setattr(runtime_llm, "SPELL_TOOL_NAMES", {SPELL_NAME})
    monkeypatch.setattr(runtime_llm, "_run_spell_tool_async", _first_ok_then_model_gone())

    with pytest.raises(ModelUnavailableError) as excinfo:
        _run_reply(node, _spell_state(), runtime, persona, events)

    # 止まった周の途中まで (受け取った一つ目の結果 + 唱えた二つ目の行) は
    # 建物へ書かれてから投げられている
    said = [c.args[2] for c in runtime._emit_say.call_args_list]
    assert any("一つ目はやりました" in s and s.count("<user_only") == 2 for s in said), said
    record = last_saved_utterance("p1")
    assert record.form == SAVED_FORM_SPELL_UNFINISHED
    assert _notices(persona) == [(
        "b1",
        "(発言の後に唱えたスペルは、実行が終わる前に中断されました。"
        "結果は届いておらず、どこまで実行されたかは不明です)",
    )]
    assert excinfo.value.to_dict()["interrupted_message_id"] == record.message_id


def test_a_raw_stream_cut_after_a_spell_shows_the_same_error_as_without_spells(
    monkeypatch,
):
    """スペルの後の続きのストリームが、LLMError に包まれない例外で切れた回。

    以前は「スペル内部の致命エラー」に飲まれて注記つきで黙って閉じ、画面に
    何も出なかった (隔離検証の spell_then_cut_abort)。スペル無しの同じ切断は
    エラー札が出る — 同じ失敗が場面で違う顔にならないよう、続きの生成の失敗
    として普通の返事の失敗と同じエラー札に至らせる。
    """
    # スペルを一周した後の続きのストリームが、LLMError でない例外で切れた回
    # (先に組む — _build_node は _run_spell_loop を差し替えるので、後に組むと
    # 本物のループを掴めない)
    runtime, persona, node, events = _build_spell_node(
        monkeypatch,
        calls=[[f"やるね。\n{SPELL_LINE}"], RuntimeError("connection reset")],
    )
    runtime._withdraw_speak_placeholder.return_value = True

    with pytest.raises(LLMError) as spell_exc:
        _run_reply(node, _spell_state(), runtime, persona, events)

    # スペル無しの返事で、同じ例外でストリームが切れた回のエラー札
    plain_client = _FakeStreamClient(call_exc=RuntimeError("connection reset"))

    async def _no_spells(**kwargs):  # pragma: no cover - 到達しない
        raise AssertionError("spell loop must not run")

    _, _, plain_node, _ = _build_node(
        monkeypatch, client=plain_client, spell_loop=_no_spells,
    )
    with pytest.raises(LLMError) as plain_exc:
        asyncio.run(plain_node({"_messages": [], "_pulse_id": "pl-1"}))

    plain_event = plain_exc.value.to_dict()
    spell_event = spell_exc.value.to_dict()
    for key in ("type", "error_code", "content", "technical_detail"):
        assert spell_event[key] == plain_event[key], key
    assert isinstance(spell_exc.value.original_error, RuntimeError)
    # 印と通告 (③) はスペルの結果で終わる発言に、エラー札はそこへ案内する
    assert _marked(persona) == [("b1", "msg-1")]
    assert _notices(persona) == [
        ("b1", "(スペルの結果を受け取った後、続きの発言の前に中断されました)"),
    ]
    assert spell_event["interrupted_message_id"] == "msg-1"


def test_a_raw_continuation_failure_without_streaming_is_a_continuation_failure(
    monkeypatch,
):
    """ストリーミングを使わない経路でも同じ — 周の発言を建物へ書いてから、
    包んだ LLMError を投げる (スペル内部の致命エラーの注記で黙って閉じない)。"""
    client = _ScriptedSyncClient([
        f"やるね。\n{SPELL_LINE}", RuntimeError("connection reset"),
    ])
    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=runtime_llm._run_spell_loop,
    )
    monkeypatch.setattr(runtime_llm, "_is_llm_streaming_enabled", lambda: False)
    monkeypatch.setattr(runtime_llm, "SPELL_TOOL_NAMES", {SPELL_NAME})
    monkeypatch.setattr(runtime_llm, "_run_spell_tool_async", _ok_spell)

    with pytest.raises(LLMError) as excinfo:
        _run_reply(node, _spell_state(), runtime, persona, events)

    assert isinstance(excinfo.value.original_error, RuntimeError)
    assert not _has_spell_system_error_note(client.seen_messages)
    said = [c.args[2] for c in runtime._emit_say.call_args_list]
    assert any("やりました" in s for s in said), said
    assert _notices(persona) == [
        ("b1", "(スペルの結果を受け取った後、続きの発言の前に中断されました)"),
    ]


def test_a_stream_cut_note_never_lands_on_an_utterance_from_before_the_reply():
    """確定に失敗した回の切断の書き足しは、この返事で最後に保存できた行が記録の
    最後の発言と一致するときだけ付ける。

    記録の最後の発言がこの返事より前のものだと、返事の後始末は時間窓でそれを
    読み飛ばす — そこへ書き足すと、知らせがどこにも出ずに消える。付けられない
    回は画面への知らせをここで直接出す。
    """
    _, persona = _reply_world()
    _saved(persona, "old")      # この返事より前の発言
    cut = {"code": 500, "message": "internal error"}

    # この返事ではまだ何も保存していない (id が無い)
    events: list = []
    runtime_llm._note_stream_cut(
        persona, cut, message_id=None, reply_last_message_id=None,
        event_callback=events.append,
    )
    assert last_saved_utterance("p1").stopped is False
    assert [e["type"] for e in events] == ["info"]

    # この返事で保存した行が、記録の最後の発言と一致しない
    events = []
    runtime_llm._note_stream_cut(
        persona, cut, message_id=None, reply_last_message_id="mine",
        event_callback=events.append,
    )
    assert last_saved_utterance("p1").stopped is False
    assert [e["type"] for e in events] == ["info"]

    # 一致する回だけ書き足す (知らせは後始末が案内つきで出すのでここでは出さない)
    _saved(persona, "mine")
    events = []
    runtime_llm._note_stream_cut(
        persona, cut, message_id=None, reply_last_message_id="mine",
        event_callback=events.append,
    )
    record = last_saved_utterance("p1")
    assert (record.message_id, record.stopped) == ("mine", True)
    assert record.detail == {"stream_error": cut}
    assert events == []


def test_unexecuted_spells_leave_only_the_cast_line_without_a_result_block(
    monkeypatch,
):
    """止まった周で結果の来ていないスペル・未登録のスペル・引数の壊れたスペルは、
    結果の折りたたみ (<details>) を作らず、唱えた行だけを残す (不変条件 5)。"""
    monkeypatch.setattr(runtime_llm, "SPELL_TOOL_NAMES", {SPELL_NAME})
    text = (
        "やるね。\n"
        f"/spell name='{SPELL_NAME}' args={{\"step\": 1}}\n"
        "/spell name='unknown_spell' args={}\n"
        f"/spell name='{SPELL_NAME}' args={{broken"
    )
    composed = runtime_llm._compose_stopped_round(text, [])

    assert composed["form"] == SAVED_FORM_SPELL_UNFINISHED
    row = composed["text"]
    assert "<details" not in row
    assert "spellResult" not in row
    assert row.count("<user_only") == 3
    assert '"step": 1' in row
    assert "unknown_spell" in row
    assert "args={broken" in row
