"""唱えごとの行をまたいでも、その前後の文はちゃんと声になる。

`_consume_pipeline_stream` は LLM のストリームを消費しながら、文の区切りごとに
音声 (sub-speak) を送り出す。2026-09-13 まで、この関数は ``/spell`` 行を 1 つ
見つけた時点で以降の送出を全部止めていた。Beat 分割より前は「行より後ろは全部
``<user_only>`` に包まれて音声から外れる」が正しかったが、Beat 分割後の契約 1
では **行の後ろに書かれた文は可視の本文** になる — 実機で、唱えごとの後ろに
書かれた長い朝の挨拶が画面には出たのに一音も声にならなかった
(docs/issues/pulse_beats_merge_into_single_record.md)。

ここで固定するのは一つ: **音声から外れるのは唱えごとの行そのものだけ**。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from sea.runtime_llm import _consume_pipeline_stream


def _consume(chunks, *, msg_id="msg-1"):
    """``chunks`` を 1 ストリームとして流し、声になった断片の列を返す。"""
    runtime = MagicMock()
    runtime._effective_building_id.return_value = "b1"
    persona = SimpleNamespace(persona_id="p1")

    text, sub_seq, spell_detected, cancelled = asyncio.run(
        _consume_pipeline_stream(
            iter(chunks),
            runtime=runtime,
            persona=persona,
            building_id="b1",
            node_def=SimpleNamespace(id="llm"),
            state={},
            pipeline_msg_id=msg_id,
            sub_seq_start=0,
            cancellation_token=None,
            event_callback=None,
            emit_building_id="b1",
        ),
    )
    voiced = [call.args[3] for call in runtime._emit_sub_speak.call_args_list]
    return SimpleNamespace(
        voiced=voiced,
        text=text,
        sub_seq=sub_seq,
        spell_detected=spell_detected,
        cancelled=cancelled,
    )


def test_the_sentences_around_a_spell_line_are_both_voiced():
    """(a) 行の前の文も後ろの文も声になり、行そのものは声にならない。"""
    result = _consume([
        "おはよう。\n",
        "/spell name='look_around' args={}\n",
        "今日はいい天気だね。",
    ])
    assert result.voiced == ["おはよう。", "今日はいい天気だね。"]
    assert result.spell_detected is True
    # 唱えごとの行の中身が、どの断片にも混ざっていない
    assert not any("/spell" in fragment for fragment in result.voiced)


def test_text_after_a_spell_line_is_flushed_without_punctuation():
    """行の後ろが文の区切りで終わらなくても、終端の flush で声になる。

    実機で無音になった形そのもの — 止めていた頃は、この残りが誰にも渡らずに
    消えていた。
    """
    result = _consume([
        "/spell name='look_around' args={}\n",
        "声にしたい残りの一言",
    ])
    assert result.voiced == ["声にしたい残りの一言"]


def test_a_spell_line_split_across_chunks_is_still_skipped():
    """(b) 行の途中で chunk が切れても、行は最後まで飛ばす。"""
    result = _consume([
        "こんにちは。\n/spell name='look_around' ar",
        'gs={"radius": 1}\n続きの文です。',
    ])
    assert result.voiced == ["こんにちは。", "続きの文です。"]
    assert not any("args=" in fragment for fragment in result.voiced)


def test_two_spell_lines_are_both_skipped():
    """(c) 唱えごとが 2 本あっても、飛ばすのは 2 本とも行だけ。"""
    result = _consume([
        "まず調べる。\n",
        "/spell name='look_around' args={}\n",
        "次はこっち。\n",
        "/quick_spell name='rest' args={}\n",
        "終わり。",
    ])
    assert result.voiced == ["まず調べる。", "次はこっち。", "終わり。"]


def test_a_spell_line_cut_off_mid_stream_is_never_voiced():
    """(d) 行の途中でストリームが終わった回は、その不完全な行を声にしない。"""
    result = _consume([
        "前の文。\n",
        '/spell name=\'look_around\' args={"radius":',
    ])
    assert result.voiced == ["前の文。"]
    assert not any("radius" in fragment for fragment in result.voiced)


def test_a_stream_without_spells_behaves_as_before():
    """(e) 唱えごとの無い回の挙動は変わらない。"""
    result = _consume(["こんにちは。", "元気にしてた？", "また話そう"])
    assert result.voiced == ["こんにちは。", "元気にしてた？", "また話そう"]
    assert result.spell_detected is False
    assert result.sub_seq == 3
    assert result.text == "こんにちは。元気にしてた？また話そう"


def test_a_spell_with_raw_newlines_in_its_args_is_skipped_whole():
    """args の中に生の改行が入って行が千切れた回も、唱えごと全体を飛ばす。

    救済パース (`_rescue_multiline_args`) が唱えごととして読む範囲と、音声から
    外れる範囲を揃える — ずれると args の続きが読み上げられる。
    """
    result = _consume([
        "書いておくね。\n",
        '/spell name=\'document_create\' args={"body": "1 行目\n',
        '2 行目"}\n',
        "書けたよ。",
    ])
    assert result.voiced == ["書いておくね。", "書けたよ。"]
    assert not any("行目" in fragment for fragment in result.voiced)
