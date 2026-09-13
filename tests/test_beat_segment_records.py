"""Pulse の各 Beat が、それぞれ 1 件の記録として自分の部屋に残ることの回帰。

設計: docs/issues/pulse_beats_merge_into_single_record.md

2026-09-12 以前、スペルが走った Pulse は全ラウンドの本文を 1 本に結合して
1 つの部屋に 1 件だけ保存していた。そのため Pulse の途中でペルソナが移動すると、
移動後の発言まで元の部屋に残って居残った同席者に読まれた (ストリーミング経路) か、
移動前の発言まで移動先へ持って行かれた (非ストリーミング経路) 。ここで固定するのは
その反対の形 — **1 Beat = 1 回の LLM 生成 = 1 件の記録**で、記録先は
「その Beat の生成が始まった時点の部屋」。
"""
import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from sea import runtime_llm
from sea.runtime_emitters import SpeakFinalizeResult

SPELL_NAME = "note_add"
SPELL_LINE = f"/spell name='{SPELL_NAME}' args={{}}"


# ---------------------------------------------------------------------------
# フェイク
# ---------------------------------------------------------------------------


class SpellLoopRuntime:
    """_run_spell_loop / _emit_beat_segments が触る SEARuntime の最小フェイク。

    ``_effective_building_id`` は実物 (sea/runtime.py) と同じく occupants を
    走査する — スペルが在室者を書き換えたら現在地も変わる、という本物の連動を
    テストの中でも成立させるため。
    """

    def __init__(self, occupants: Optional[Dict[str, List[str]]] = None):
        self.stored: List[str] = []
        self.said: List[Dict[str, Any]] = []
        self.speak_starts: List[str] = []
        self.finalized: List[Dict[str, Any]] = []
        self.sub_speaks: List[Dict[str, Any]] = []
        self._msg_seq = 0
        self.manager = SimpleNamespace(occupants=occupants or {"b1": ["p1"]})
        self.session_lifecycle = SimpleNamespace(
            touch_anchor_after_llm_call=lambda persona, usage, anchor_id=None: None,
        )

    # --- 現在地 -----------------------------------------------------------
    def _effective_building_id(self, persona, fallback):
        pid = getattr(persona, "persona_id", None)
        if pid:
            for bid, occ in self.manager.occupants.items():
                if pid in occ:
                    return bid
        return fallback

    # --- 記録 -------------------------------------------------------------
    def _store_memory(self, persona, text, **kwargs):
        self.stored.append(text)
        return "mem-1" if kwargs.get("return_message_id") else True

    def _emit_say(self, persona, building_id, text, pulse_id=None, metadata=None,
                  event_callback=None, occupants_snapshot=None):
        self._msg_seq += 1
        msg_id = f"say-{self._msg_seq}"
        self.said.append({
            "building_id": building_id, "text": text, "metadata": metadata,
            "message_id": msg_id, "occupants_snapshot": occupants_snapshot,
        })
        return {"message_id": msg_id, "content": text}

    # --- 下書き行 (Pipeline Streaming) -------------------------------------
    def _emit_speak_start(self, persona, building_id, pulse_id=None):
        self._msg_seq += 1
        msg_id = f"draft-{self._msg_seq}"
        self.speak_starts.append(building_id)
        return msg_id

    def _emit_sub_speak(self, persona, building_id, message_id, sub_text, sub_seq,
                        pulse_id=None):
        self.sub_speaks.append({
            "building_id": building_id, "message_id": message_id,
            "text": sub_text, "sub_seq": sub_seq,
        })

    def _emit_speak_finalize(self, persona, building_id, message_id, text,
                             pulse_id=None, extra_metadata=None,
                             final_sub_seq=None, final_voice_text=None):
        self.finalized.append({
            "building_id": building_id, "message_id": message_id, "text": text,
            "extra_metadata": extra_metadata, "final_sub_seq": final_sub_seq,
        })
        return SpeakFinalizeResult(
            status="saved",
            building_msg={"message_id": message_id, "content": text},
        )

    # --- その他 -----------------------------------------------------------
    def _default_temperature(self, persona):
        return None

    def _get_cache_kwargs(self, persona_id=None):
        return {}

    def _dump_llm_io(self, *args, **kwargs):
        return None

    def _accumulate_usage(self, *args, **kwargs):
        return None


class ScriptedClient:
    """retry 応答をスクリプト順に返す mock LLM クライアント (全文一括)。"""

    def __init__(self, responses: List[Any]):
        self.responses = list(responses)

    def generate(self, messages, tools=None, temperature=None, **kwargs):
        if not self.responses:
            raise AssertionError("ScriptedClient: no scripted responses left")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def consume_usage(self):
        return None


class ScriptedStreamClient:
    """retry 応答をスクリプト順に chunk 列で返す mock (ストリーミング経路)。"""

    def __init__(self, responses: List[str]):
        self.responses = list(responses)

    def generate_stream(self, messages, tools=None, temperature=None, **kwargs):
        if not self.responses:
            raise AssertionError("ScriptedStreamClient: no scripted responses left")
        return iter([self.responses.pop(0)])

    def consume_usage(self):
        return None


def _ok_spell(on_call=None, result: str = "やりました"):
    async def fake(tool_name, tool_args, persona, state, playbook_name,
                   event_callback, messages=None):
        if on_call is not None:
            on_call()
        return (result, None, True)
    return fake


def _run_loop(runtime, client, text, fake_spell, *, streaming_state=None,
              initial_building_id=None, event_callback=None, state=None):
    persona = SimpleNamespace(persona_id="p1")
    if state is None:
        state = {"_pulse_id": "pulse-1", "_pulse_context": None,
                 "_cancellation_token": None}
    node_def = SimpleNamespace(id="llm", memorize=None, speak=True)
    playbook = SimpleNamespace(name="test_playbook")
    with patch.object(runtime_llm, "SPELL_TOOL_NAMES", {SPELL_NAME}), \
         patch.object(runtime_llm, "_run_spell_tool_async", new=fake_spell):
        return asyncio.run(runtime_llm._run_spell_loop(
            text=text,
            spell_enabled=True,
            llm_client=client,
            runtime=runtime,
            persona=persona,
            building_id="b1",
            state=state,
            messages=[],
            playbook=playbook,
            event_callback=event_callback,
            node_def=node_def,
            pipeline_streaming_state=streaming_state,
            initial_building_id=initial_building_id,
            initial_llm_usage={"model": "m", "input_tokens": 1},
        )), persona


# ---------------------------------------------------------------------------
# 1. 同じ部屋のまま — 2 ラウンドで 3 件
# ---------------------------------------------------------------------------


def test_two_spell_rounds_leave_three_records_in_order():
    """2 ラウンドのスペルループ = 3 Beat = 3 件の記録 (順序・部屋・内容)。"""
    runtime = SpellLoopRuntime()
    client = ScriptedClient([f"二回目だ。\n{SPELL_LINE}", "終わりました。"])
    result, persona = _run_loop(
        runtime, client, f"一回目だ。\n{SPELL_LINE}", _ok_spell(),
    )

    assert result.loop_count == 2
    assert len(result.segments) == 3
    assert result.final_continuation == "終わりました。"
    assert [seg.building_id for seg in result.segments] == ["b1", "b1", "b1"]
    assert result.segments[0].text.startswith("一回目だ。")
    assert result.segments[1].text.startswith("二回目だ。")
    assert result.segments[2].text == "終わりました。"
    # スペルの結果ブロックは、そのスペルを唱えた周の記録にだけ入る
    assert "やりました" in result.segments[0].text
    assert "やりました" in result.segments[1].text
    assert "やりました" not in result.segments[2].text

    # 記録は Beat ごとに 1 件ずつ、同じ順序で建物へ出る
    events: List[Dict[str, Any]] = []
    runtime_llm._emit_beat_segments(
        runtime, persona, {}, result.segments,
        pulse_id="pulse-1",
        event_callback=events.append,
        final_metadata_factory=lambda usage: {"tags": ["conversation"], "final": True},
    )
    assert len(runtime.said) == 3
    assert [row["building_id"] for row in runtime.said] == ["b1", "b1", "b1"]
    assert [row["text"] for row in runtime.said] == [
        seg.text for seg in result.segments
    ]
    # 締めの Beat だけが全部入りのメタデータを持つ (契約 4)
    assert runtime.said[-1]["metadata"]["final"] is True
    assert "final" not in runtime.said[0]["metadata"]
    assert runtime.said[0]["metadata"]["llm_usage"] == {"model": "m", "input_tokens": 1}
    say_events = [e for e in events if e["type"] == "say"]
    assert [e["building_id"] for e in say_events] == ["b1", "b1", "b1"]


# ---------------------------------------------------------------------------
# 2. 移動を挟む — Beat 2 以降は移動先の部屋へ
# ---------------------------------------------------------------------------


def test_a_move_in_round_one_sends_the_later_beats_to_the_new_room():
    """周の中で移動したら、次の Beat の記録は移動先の部屋に落ちる (契約 1・2)。"""
    runtime = SpellLoopRuntime(occupants={"b1": ["p1", "other"], "b2": []})

    def _move():
        runtime.manager.occupants["b1"].remove("p1")
        runtime.manager.occupants["b2"].append("p1")

    client = ScriptedClient(["自室は静かだ。"])
    result, persona = _run_loop(
        runtime, client, f"部屋に戻るね。\n{SPELL_LINE}", _ok_spell(on_call=_move),
    )

    assert result.loop_count == 1
    assert len(result.segments) == 2
    # Beat 1 は「生成が始まった部屋」= 移動前
    assert result.segments[0].building_id == "b1"
    assert "部屋に戻るね。" in result.segments[0].text
    # Beat 2 は移動後の部屋
    assert result.segments[1].building_id == "b2"
    assert result.segments[1].text == "自室は静かだ。"

    runtime_llm._emit_beat_segments(
        runtime, persona, {}, result.segments,
        pulse_id="pulse-1", event_callback=None,
        final_metadata_factory=lambda usage: {"tags": ["conversation"]},
    )
    assert [row["building_id"] for row in runtime.said] == ["b1", "b2"]


def test_a_record_is_heard_by_the_occupants_of_its_own_room():
    """記録の heard_by は、その記録が落ちた部屋の在室者から作られる (契約 3)。

    Beat 2 が移動先の部屋に落ちる (上のテスト) ことと合わせて、「元の部屋に
    居残った同席者が移動後の発言を読む」形が構造として起きなくなる。
    """
    from sea.runtime_emitters import RuntimeEmitters

    history = MagicMock()
    history.add_to_building_only.return_value = {"message_id": "m1", "content": "やあ"}
    persona = SimpleNamespace(persona_id="p1", history_manager=history)
    runtime = SimpleNamespace(
        manager=SimpleNamespace(
            occupants={"b1": ["p1", "elis"], "b2": ["mira"]},
            user_presence_status="offline",
            gateway_handle_ai_replies=lambda *a, **k: None,
        ),
    )
    emitters = RuntimeEmitters(runtime)

    emitters.emit_say(persona, "b2", "やあ")
    heard_by = history.add_to_building_only.call_args.kwargs["heard_by"]
    assert sorted(heard_by) == ["mira", "p1"]
    assert "elis" not in heard_by

    # 在室者の写しを渡した回は、いまの在室表ではなく写しから作る
    emitters.emit_say(persona, "b2", "やあ", occupants_snapshot=["p1", "aify"])
    heard_by2 = history.add_to_building_only.call_args.kwargs["heard_by"]
    assert sorted(heard_by2) == ["aify", "p1"]
    assert "mira" not in heard_by2


def test_each_beat_carries_the_occupants_of_the_moment_it_started():
    """記録が後からまとめて書かれても、聞いた人は Beat 開始時点の在室者 (契約 3)。

    ストリーミングを使わない経路は全 Beat をループ完了後に書く。書くときに
    在室表を引くと、移動を挟んだ Pulse の Beat 1 にまで「移動後の在室者」が
    載って、元の部屋に居た同席者が聞いた人から抜ける。
    """
    runtime = SpellLoopRuntime(occupants={"b1": ["p1", "elis"], "b2": ["mira"]})

    def _move():
        runtime.manager.occupants["b1"].remove("p1")
        runtime.manager.occupants["b2"].append("p1")

    client = ScriptedClient(["自室は静かだ。"])
    result, persona = _run_loop(
        runtime, client, f"部屋に戻るね。\n{SPELL_LINE}", _ok_spell(on_call=_move),
    )

    assert result.segments[0].occupants == ["p1", "elis"]
    assert result.segments[1].occupants == ["mira", "p1"]

    runtime_llm._emit_beat_segments(
        runtime, persona, {}, result.segments,
        pulse_id="pulse-1", event_callback=None,
        final_metadata_factory=lambda usage: {"tags": ["conversation"]},
    )
    assert [row["occupants_snapshot"] for row in runtime.said] == [
        ["p1", "elis"], ["mira", "p1"],
    ]


def test_nothing_to_write_still_consumes_the_closing_metadata():
    """1 件も書かない回でも、締めのメタデータの組み立ては 1 回走らせる。

    `_build_say_metadata` は自動想起の本文を state から pop で取り出す。
    呼ばないと消費されず、次のノードの発言に前の Beat の想起が付いて流れる。
    """
    runtime = SpellLoopRuntime()
    persona = SimpleNamespace(persona_id="p1")
    calls: List[Any] = []
    wrote = runtime_llm._emit_beat_segments(
        runtime, persona, {},
        [runtime_llm.BeatSegment(text="   ", building_id="b1")],
        pulse_id="pulse-1", event_callback=None,
        final_metadata_factory=lambda usage: calls.append(usage) or {},
    )
    assert wrote is False
    assert runtime.said == []
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 3. ストリーミング経路 — 下書き行は作った部屋に確定する
# ---------------------------------------------------------------------------


def _streaming_state(msg_id: str, building_id: str) -> Dict[str, Any]:
    return {
        "msg_id": msg_id,
        "building_id": building_id,
        "sub_seq": 0,
        "finalized": False,
        "placeholder_round": 1,
        "cancellation_token": None,
    }


def test_the_draft_row_is_confirmed_in_the_room_where_it_was_created():
    """下書き行の確定は作成時の部屋に対して行う — 移動後に引き直さない。

    引き直すと移動先に存在しない行を更新しに行って空振りし、本文の入らない行が
    残る (2026-06-11 Region RPG 実機)。Beat 単位に割ってもこの不変条件は同じ。
    """
    runtime = SpellLoopRuntime(occupants={"b1": ["p1"], "b2": []})

    def _move():
        runtime.manager.occupants["b1"].remove("p1")
        runtime.manager.occupants["b2"].append("p1")

    client = ScriptedStreamClient(["自室は静かだ。"])
    st = _streaming_state("draft-0", "b1")
    result, _persona = _run_loop(
        runtime, client, f"部屋に戻るね。\n{SPELL_LINE}", _ok_spell(on_call=_move),
        streaming_state=st, initial_building_id="b1",
    )

    # Beat 1 の確定先は、移動が済んだ後でも作成時の部屋
    assert len(runtime.finalized) == 1
    assert runtime.finalized[0]["building_id"] == "b1"
    assert runtime.finalized[0]["message_id"] == "draft-0"
    assert "部屋に戻るね。" in runtime.finalized[0]["text"]
    # Beat 2 の下書き行は移動先の部屋に新しく作る
    assert runtime.speak_starts == ["b2"]
    assert st["building_id"] == "b2"
    assert st["msg_id"] != "draft-0"
    # 連番は新しい行で 0 から積み直す
    assert st["placeholder_round"] == 2
    # 締めの Beat はループの中では確定させない (呼び出し元が全部入りで確定する)
    assert result.segments[-1].emitted is False
    assert result.segments[-1].building_id == "b2"


def test_a_quick_spell_terminal_round_opens_no_empty_draft_row():
    """/quick_spell 終端では次の生成が無いので、空の下書き行を作らない。"""
    runtime = SpellLoopRuntime()
    client = ScriptedStreamClient([])  # 再呼び出しに到達しないはず
    st = _streaming_state("draft-0", "b1")
    result, _persona = _run_loop(
        runtime, client,
        f"やっておくね。\n/quick_spell name='{SPELL_NAME}' args={{}}",
        _ok_spell(), streaming_state=st, initial_building_id="b1",
    )

    assert result.loop_count == 1
    assert result.final_continuation == ""
    # 新しい下書き行は作らない / ループの中では確定もしない
    assert runtime.speak_starts == []
    assert runtime.finalized == []
    assert st["msg_id"] == "draft-0"
    # 終端の周の本文は、締めの Beat として呼び出し元に渡る (空ではない)
    assert len(result.segments) == 1
    assert result.segments[0].emitted is False
    assert result.segments[0].text.strip()


def test_a_quick_spell_terminal_round_emits_no_empty_message_without_streaming():
    """非ストリーミング経路でも、終端の周は空メッセージを作らない。"""
    runtime = SpellLoopRuntime()
    client = ScriptedClient([])
    result, persona = _run_loop(
        runtime, client,
        f"やっておくね。\n/quick_spell name='{SPELL_NAME}' args={{}}",
        _ok_spell(),
    )
    runtime_llm._emit_beat_segments(
        runtime, persona, {}, result.segments,
        pulse_id="pulse-1", event_callback=None,
        final_metadata_factory=lambda usage: {"tags": ["conversation"]},
    )
    assert len(runtime.said) == 1
    assert runtime.said[0]["text"].strip()


# ---------------------------------------------------------------------------
# 4. 途中で落ちても、確定済みの Beat は残る
# ---------------------------------------------------------------------------


def test_a_failure_mid_loop_keeps_the_beats_that_were_already_confirmed():
    """ラウンド途中の失敗で、それまでに確定した Beat の記録は消えない。"""
    runtime = SpellLoopRuntime()
    client = ScriptedClient([RuntimeError("api down")])
    result, _persona = _run_loop(
        runtime, client, f"やるぞ。\n{SPELL_LINE}", _ok_spell(),
    )

    assert result.loop_count == 1
    assert len(result.segments) == 1
    assert "やるぞ。" in result.segments[0].text
    assert result.final_continuation == ""


def test_a_streaming_failure_mid_loop_keeps_the_confirmed_beat_in_its_room():
    """ストリーミング経路でも、落ちる前の Beat は自分の部屋に確定済みで残る。"""
    runtime = SpellLoopRuntime()

    class _DyingStreamClient:
        def generate_stream(self, messages, **kwargs):
            raise RuntimeError("api down")

        def consume_usage(self):
            return None

    st = _streaming_state("draft-0", "b1")
    result, _persona = _run_loop(
        runtime, _DyingStreamClient(), f"やるぞ。\n{SPELL_LINE}", _ok_spell(),
        streaming_state=st, initial_building_id="b1",
    )

    assert result.loop_count == 1
    assert len(runtime.finalized) == 1
    assert runtime.finalized[0]["building_id"] == "b1"
    assert "やるぞ。" in runtime.finalized[0]["text"]
    assert result.segments[0].emitted is True


class _UnsavableDraftRuntime(SpellLoopRuntime):
    """下書き行への確定が必ず「対象行なし」で返る runtime。

    行が消えていた / 保存に失敗した回の再現。確定が通らないと、その Beat の
    本文はどこにも入らないまま次の周の行に差し替えられる。
    """

    def _emit_speak_finalize(self, persona, building_id, message_id, text,
                             pulse_id=None, extra_metadata=None,
                             final_sub_seq=None, final_voice_text=None):
        self.finalized.append({
            "building_id": building_id, "message_id": message_id, "text": text,
            "extra_metadata": extra_metadata, "final_sub_seq": final_sub_seq,
        })
        return SpeakFinalizeResult(status="missing", building_msg=None)


def test_a_beat_that_cannot_be_confirmed_still_reaches_the_building():
    """中間 Beat の確定が失敗した回 — 発言は建物へ退避して残る。

    確定できなかった行はそのまま孤児になる (下書きの印が立っているので孤児
    掃除の網には入る) が、退避しないとその Beat の発言が建物から消える。
    """
    runtime = _UnsavableDraftRuntime()
    client = ScriptedStreamClient(["終わりました。"])
    events: List[Dict[str, Any]] = []
    st = _streaming_state("draft-0", "b1")
    result, _persona = _run_loop(
        runtime, client, f"やるぞ。\n{SPELL_LINE}", _ok_spell(),
        streaming_state=st, initial_building_id="b1", event_callback=events.append,
    )

    # 確定は試みたが通らなかった
    assert len(runtime.finalized) == 1
    assert runtime.finalized[0]["message_id"] == "draft-0"
    # 発言は建物へ直接書かれて残る (自分の部屋へ 1 件)
    assert len(runtime.said) == 1
    assert runtime.said[0]["building_id"] == "b1"
    assert "やるぞ。" in runtime.said[0]["text"]
    # 退避済みなので、呼び出し元がもう一度書きに来ない
    assert result.segments[0].emitted is True
    # 画面の吹き出しは 1 つだけ (確定前に流した say を退避で二度流さない)
    say_events = [e for e in events if e["type"] == "say"]
    assert len(say_events) == 1
    assert say_events[0]["message_id"] == "draft-0"


def test_the_memory_backfill_material_carries_no_html():
    """退避書き込みが出口へ渡す本文は、HTML を含まない姿 (assistant_content)。

    セグメントの本文には ``<user_only>`` / spellResult の HTML が入っている。
    それを渡すと、補填がそのまま SAIMemory へ書いて
    docs/issues/spell_html_leak_into_saimemory.md が塞いだ混入が戻る。
    """
    runtime = _UnsavableDraftRuntime()
    # この周の記憶の書き込みが失敗した回だけ、出口へ本文を渡す
    runtime._store_memory = lambda persona, text, **kwargs: None
    client = ScriptedStreamClient(["終わりました。"])
    state = {"_pulse_id": "pulse-1", "_pulse_context": None,
             "_cancellation_token": None}
    st = _streaming_state("draft-0", "b1")
    _result, _persona = _run_loop(
        runtime, client, f"やるぞ。\n{SPELL_LINE}", _ok_spell(),
        streaming_state=st, initial_building_id="b1", state=state,
    )

    handed_over = state[runtime_llm.BEAT_BODY_UNMEMORIZED_KEY]
    assert "やるぞ。" in handed_over
    assert "user_only" not in handed_over
    assert "<" not in handed_over
    # 建物へ書いた本文の方には従来どおり整形済みのブロックが入っている
    assert "user_only" in runtime.said[0]["text"]


def test_a_confirmed_beat_hands_nothing_to_the_memory_backfill():
    """確定が通った周は、出口の補填へ本文を渡さない (記憶は周が書いている)。"""
    runtime = SpellLoopRuntime()
    client = ScriptedStreamClient(["終わりました。"])
    state = {"_pulse_id": "pulse-1", "_pulse_context": None,
             "_cancellation_token": None}
    st = _streaming_state("draft-0", "b1")
    _run_loop(
        runtime, client, f"やるぞ。\n{SPELL_LINE}", _ok_spell(),
        streaming_state=st, initial_building_id="b1", state=state,
    )
    assert runtime_llm.BEAT_BODY_UNMEMORIZED_KEY not in state


# ---------------------------------------------------------------------------
# 5. 移動イベントの導線
# ---------------------------------------------------------------------------


def test_the_leave_notice_links_to_the_destination_room():
    """退出メッセージの行き先の部屋名は、その部屋へ切り替えるリンクになる。

    ユーザーは移動に自動では付いていかないので、続きの Beat がどこにあるかへの
    導線が要る (docs/issues/pulse_beats_merge_into_single_record.md 契約 8)。
    """
    from datetime import datetime, timezone

    from saiverse.occupancy_manager import OccupancyManager

    om = OccupancyManager.__new__(OccupancyManager)
    om.occupants = {"b1": ["p1", "elis"], "b2": []}
    om.building_map = {
        "b1": SimpleNamespace(name="まはーの部屋"),
        "b2": SimpleNamespace(name="アイフィの自室"),
    }
    om._build_building_info = lambda bid: {}

    events = om._build_occupancy_events(
        "p1", "ai", "アイフィ", "b1", "b2",
        datetime(2026, 9, 12, tzinfo=timezone.utc), move_key="k",
    )
    leave_content = events[0][1]["content"]
    assert '<a href="saiverse://building/b2">アイフィの自室</a>' in leave_content
    assert "へ移動しました" in leave_content
    # 入室側は変えない
    assert "<a href=" not in events[1][1]["content"]


def test_the_leave_notice_encodes_a_destination_id_that_would_split_the_uri():
    """文字種契約より前の部屋 ID は「/」も日本語も取りうる — URI 区切りで割らせない。

    読み手 (frontend の SaiverseLink) は ``saiverse://building/<1 区切り>`` を
    decodeURIComponent で読む。素で入れると「/」入りの ID が途中で切れて別の
    部屋を指す。
    """
    from datetime import datetime, timezone

    from saiverse.occupancy_manager import OccupancyManager

    om = OccupancyManager.__new__(OccupancyManager)
    om.occupants = {"b1": ["p1"]}
    om.building_map = {
        "b1": SimpleNamespace(name="まはーの部屋"),
        "2/へや": SimpleNamespace(name="変な<部屋>"),
    }
    om._build_building_info = lambda bid: {}

    events = om._build_occupancy_events(
        "p1", "ai", "アイフィ", "b1", "2/へや",
        datetime(2026, 9, 12, tzinfo=timezone.utc), move_key="k",
    )
    leave_content = events[0][1]["content"]
    assert "saiverse://building/2%2F%E3%81%B8%E3%82%84" in leave_content
    assert "&lt;部屋&gt;" in leave_content


# ---------------------------------------------------------------------------
# 6. 使用量の札 — 合計は出さず、各吹き出しが自分の分だけを持つ
# ---------------------------------------------------------------------------


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


class _OneChunkStreamClient:
    """1 chunk 流して終わる最小のストリーミングクライアント。"""

    config_key = None

    def generate_stream(self, messages, tools=(), temperature=None, **kwargs):
        return iter(["一回目だ。"])

    def consume_usage(self):
        return None

    def consume_thought_signature(self):
        return None


def _build_streaming_node(monkeypatch, *, spell_loop):
    """``lg_llm_node`` を、スペルが走ったストリーミング経路で走らせる器。

    締めの Beat の metadata は node 側 (スペルループの外) で組まれるので、
    ループ単体のテストでは触れない。本物の呼び出し口を通す。
    """
    runtime = MagicMock()
    runtime.manager.occupants = {"b1": ["p1"]}
    runtime._effective_building_id.return_value = "b1"
    runtime._emit_speak_start.return_value = "msg-1"
    runtime._emit_speak_finalize.return_value = SpeakFinalizeResult(
        status="saved",
        building_msg={"message_id": "msg-1", "content": "終わりました。"},
    )
    runtime._default_temperature.return_value = 0.7
    runtime._get_cache_kwargs.return_value = {}
    runtime.select_llm_client.return_value = (_OneChunkStreamClient(), "model-a")

    monkeypatch.setattr(
        runtime_llm, "resolve_execution_context",
        lambda persona, pulse_context, state=None: SimpleNamespace(model_key="model-a"),
    )
    monkeypatch.setattr(runtime_llm, "_is_llm_streaming_enabled", lambda: True)
    monkeypatch.setattr(runtime_llm, "_record_llm_usage", lambda *a, **k: None)
    monkeypatch.setattr(runtime_llm, "_consume_reasoning", lambda *a, **k: ("", None))
    monkeypatch.setattr(runtime_llm, "_finalize_beat", lambda *a, **k: None)
    monkeypatch.setattr(runtime_llm, "_run_spell_loop", spell_loop)

    # persona_id=None で persona_context の wrap を素通しする
    # (tests/test_streaming_placeholder_salvage.py と同じ手)。
    persona = SimpleNamespace(
        persona_id=None, persona_name="p", history_manager=MagicMock(),
    )
    events: List[Dict[str, Any]] = []
    node = runtime_llm.lg_llm_node(
        runtime, _node_def(), persona, "b1", SimpleNamespace(name="pb"),
        events.append,
    )
    return runtime, node, events


def _two_beat_spell_loop(closing_usage: Optional[Dict[str, Any]] = None):
    """中間 Beat 1 件 (確定済み) + 締めの Beat 1 件を返すフェイクのスペルループ。"""

    async def _loop(**kwargs):
        return runtime_llm.SpellLoopResult(
            segments=[
                runtime_llm.BeatSegment(
                    text="一回目だ。", building_id="b1",
                    llm_usage={"model": "m1", "input_tokens": 3}, emitted=True,
                ),
                runtime_llm.BeatSegment(
                    text="終わりました。", building_id="b1",
                    llm_usage=closing_usage, emitted=False,
                ),
            ],
            final_continuation="終わりました。",
            loop_count=1,
        )

    return _loop


def test_the_closing_beat_of_a_spell_pulse_shows_no_pulse_total(monkeypatch):
    """スペルが走った Pulse の締めの Beat に、Pulse 合計の札を載せない。

    吹き出しが Beat ごとに割れた今、締めに合計を付けると前の吹き出しの分を
    含む数字が隣に並んで二重に読める (2026-09-13 まはー観測・同日裁定、
    docs/issues/pulse_beats_merge_into_single_record.md の実機 2)。各吹き出しは
    自分の Beat の分だけを出し、正確な集計は使用量の記帳が別に持つ。
    """
    runtime, node, events = _build_streaming_node(
        monkeypatch,
        spell_loop=_two_beat_spell_loop(
            closing_usage={"model": "m2", "input_tokens": 7},
        ),
    )
    state = {
        "_messages": [],
        "_pulse_id": "pl-1",
        # Pulse 合計は積まれている。載らないのは「積んでいないから」ではなく
        # 「載せないと決めたから」— この前提が崩れるとテストが空振りになる。
        "_pulse_usage_accumulator": {
            "total_input_tokens": 10, "total_output_tokens": 4,
            "total_cost_usd": 0.01, "call_count": 2, "models_used": ["m1", "m2"],
        },
    }
    asyncio.run(node(state))

    assert "llm_usage_total" in runtime_llm._build_say_metadata(dict(state)), (
        "前提が崩れている: この state では合計が載りうるはず"
    )

    runtime._emit_speak_finalize.assert_called_once()
    extra = runtime._emit_speak_finalize.call_args.kwargs["extra_metadata"]
    assert "llm_usage_total" not in extra
    # 締めの吹き出しは自分の Beat の分だけを持つ
    assert extra["llm_usage"] == {"model": "m2", "input_tokens": 7}
    # 画面へ渡す札も同じ (記録と表示で数字が食い違わない)
    say_events = [e for e in events if e["type"] == "say"]
    assert say_events, "締めの Beat の say イベントが流れていない"
    assert "llm_usage_total" not in say_events[-1]["metadata"]


def test_an_intermediate_beat_carries_its_own_usage(monkeypatch):
    """中間 Beat の記録は、その周の使用量を自分の分として持つ。"""
    runtime = SpellLoopRuntime()
    client = ScriptedStreamClient(["終わりました。"])
    st = _streaming_state("draft-0", "b1")
    state = {"_pulse_id": "pulse-1", "_pulse_context": None,
             "_cancellation_token": None,
             "_pulse_usage_accumulator": {"call_count": 2}}
    _run_loop(
        runtime, client, f"やるぞ。\n{SPELL_LINE}", _ok_spell(),
        streaming_state=st, initial_building_id="b1", state=state,
    )

    assert len(runtime.finalized) == 1
    extra = runtime.finalized[0]["extra_metadata"]
    assert extra["llm_usage"] == {"model": "m", "input_tokens": 1}
    assert "llm_usage_total" not in extra


# ---------------------------------------------------------------------------
# 7. 「ふと浮かんだ記憶」は、最初に確定する Beat に付く
# ---------------------------------------------------------------------------

RECALL = "ふと浮かんだ記憶:\n- [Chronicle] 去年の夏のこと"


def test_the_recall_lands_on_the_first_beat_not_the_closing_one():
    """想起は最初に確定する Beat の記録へ (ストリーミング経路)。

    想起が起きるのは Pulse の文脈を組む時点 = Beat 1 の生成前なので、締めの
    Beat に付くと時系列が逆になる (2026-09-13 まはー観測、実機 4)。最初の
    Beat が state から pop するので、締めの組み立ては空振りする。
    """
    runtime = SpellLoopRuntime()
    client = ScriptedStreamClient(["終わりました。"])
    events: List[Dict[str, Any]] = []
    st = _streaming_state("draft-0", "b1")
    state = {"_pulse_id": "pulse-1", "_pulse_context": None,
             "_cancellation_token": None, "_auto_recall_text": RECALL}
    _run_loop(
        runtime, client, f"やるぞ。\n{SPELL_LINE}", _ok_spell(),
        streaming_state=st, initial_building_id="b1", state=state,
        event_callback=events.append,
    )

    # Beat 1 の記録に載る
    assert runtime.finalized[0]["extra_metadata"]["auto_recall"] == RECALL
    # 締めの Beat には残らない (pop 済みなので組み立てが空振りする)
    assert "_auto_recall_text" not in state
    assert "auto_recall" not in runtime_llm._build_say_metadata(state)
    # 画面にも、Beat 1 を確定させるイベントで届く。Beat の切れ目の取り消しが
    # 生成中の吹き出しごと捨てるので、ここで渡さないと画面から消える。
    say_events = [e for e in events if e["type"] == "say"]
    assert say_events[0]["metadata"]["auto_recall"] == RECALL


def test_the_recall_lands_on_the_first_beat_without_streaming():
    """非ストリーミング経路でも、想起は最初に書かれる Beat の記録へ。"""
    runtime = SpellLoopRuntime()
    persona = SimpleNamespace(persona_id="p1")
    state = {"_auto_recall_text": RECALL}
    segments = [
        runtime_llm.BeatSegment(text="一回目だ。", building_id="b1"),
        runtime_llm.BeatSegment(text="終わりました。", building_id="b1"),
    ]
    runtime_llm._emit_beat_segments(
        runtime, persona, state, segments,
        pulse_id="pulse-1", event_callback=None,
        # 締めのメタデータは本物の組み立てを通す (ここが空振りすることが要点)
        final_metadata_factory=lambda usage: runtime_llm._build_say_metadata(
            state, llm_usage_metadata=usage, include_total=False,
        ),
    )

    assert runtime.said[0]["metadata"]["auto_recall"] == RECALL
    # 締めの組み立ては空振りする (何も残らない回は metadata ごと None になる)
    assert "auto_recall" not in (runtime.said[-1]["metadata"] or {})


def test_a_single_beat_pulse_still_carries_the_recall_on_its_only_record():
    """Beat が 1 つしかない Pulse では、その唯一の記録に想起が付く (従来どおり)。"""
    runtime = SpellLoopRuntime()
    persona = SimpleNamespace(persona_id="p1")
    state = {"_auto_recall_text": RECALL}
    runtime_llm._emit_beat_segments(
        runtime, persona, state,
        [runtime_llm.BeatSegment(text="ただいま。", building_id="b1")],
        pulse_id="pulse-1", event_callback=None,
        final_metadata_factory=lambda usage: runtime_llm._build_say_metadata(
            state, llm_usage_metadata=usage, include_total=False,
        ),
    )

    assert len(runtime.said) == 1
    assert runtime.said[0]["metadata"]["auto_recall"] == RECALL


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
