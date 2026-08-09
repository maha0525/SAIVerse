"""ライフビュー API のスペル行パース (api/routes/people/activity.py) のテスト。

この関数は runtime のパーサ (sea/runtime_llm.py `_parse_spell_lines`) へ委譲する。
委譲先の戻り値 ``ParsedSpell`` にフィールドが増えたとき、位置アンパックのままだと
ValueError になり、呼び出し元の包括 except が握って**スペル詳細が黙って消える**
— quick フィールド追加 (2026-08-09 quick_spell) で実際に退行した。ここでは
「委譲先の形が変わってもライフビューが壊れない」ことを両形式で押さえる。
"""
from api.routes.people.activity import _parse_spell_lines


def test_parses_canonical_spell_line():
    parsed = _parse_spell_lines(
        """考えた結果、こうする。
/spell name='track_activate' args={"track_id": "t:3"}"""
    )
    assert parsed == [{"name": "track_activate", "args": {"track_id": "t:3"}}]


def test_parses_quick_spell_line():
    """/quick_spell (終端宣言) も同じ形で拾える — 動詞の違いは表示に出さない。"""
    parsed = _parse_spell_lines("/quick_spell note_add text='メモ'")
    assert len(parsed) == 1
    assert parsed[0]["name"] == "note_add"
    assert parsed[0]["args"] == {"text": "メモ"}


def test_parses_multiple_lines_in_order():
    parsed = _parse_spell_lines(
        """/spell name='track_pause' args={"track_id": "t:1"}
つぎはこれ。
/spell name='track_activate' args={"track_id": "t:2"}"""
    )
    assert [p["name"] for p in parsed] == ["track_pause", "track_activate"]


def test_returns_empty_for_text_without_spells():
    assert _parse_spell_lines("今日はこのまま続ける。") == []


def test_result_keys_are_exactly_name_and_args():
    """呼び出し元 (spells_emitted) が読む形を固定する。"""
    parsed = _parse_spell_lines("/spell name='track_abort' args={\"track_id\": \"t:9\"}")
    assert set(parsed[0]) == {"name", "args"}
