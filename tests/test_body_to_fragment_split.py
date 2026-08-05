"""本文 → Fragment 変換の判定層 (`split_page_body`) のテスト。

設計: docs/intent/memopedia_body_to_fragment.md

判定は三段 (まはー裁定 2026-08-05):

1. 編集来歴に ``entity_extractor`` の追記として現れる行 → 確証あり、Fragment
2. 来歴に無いが記法が機構と同じ行 → **保留** (pending)
3. 保留行は 1 行ずつユーザーが「Fragment にする / 本文に残す」を決める

``attested`` は集合ではなく **行 → 回数** の辞書。機構が足した回数ぶんだけを
確証ありにするため (同じ文字列を人が複製した 2 つ目まで自動変換しない)。

検証対象:
- 来歴が裏づけた行だけが自動で Fragment になること
- 裏づけの無い行が**勝手に Fragment にならない**こと (保留に落ちる)
- 保留行の既定は「本文に残す」で、決定を渡したときだけ動くこと
- 記法から外れる行 (地の文・入れ子・折り返し・コードフェンス内) は本文に残ること
- **本文は原文の行をそのまま連結すること** (改行コードも空行もいじらない)
"""
from __future__ import annotations

from sai_memory.memopedia.body_to_fragment import split_page_body


def attest(*lines: str) -> dict:
    """テスト用: 各行を 1 回ずつ裏づけた来歴。"""
    out: dict = {}
    for line in lines:
        out[line] = out.get(line, 0) + 1
    return out


# --------------------------------------------------------------------------
# 三段の骨格
# --------------------------------------------------------------------------

def test_history_attested_lines_become_fragments():
    content = (
        "## 2025-01-11\n"
        "- 名前の由来は、AIの「アイ」から構成されている。\n"
        "- 深い青と三日月のイヤリングを身につけることになった。\n"
    )
    split = split_page_body(content, attest(
        "- 名前の由来は、AIの「アイ」から構成されている。",
        "- 深い青と三日月のイヤリングを身につけることになった。",
    ))

    assert [f.content for f in split.fragments] == [
        "名前の由来は、AIの「アイ」から構成されている。",
        "深い青と三日月のイヤリングを身につけることになった。",
    ]
    assert all(f.evidence == "history" for f in split.fragments)
    assert split.pending == []
    assert split.render_body() == ""


def test_unattested_lines_go_to_pending_not_fragments():
    """⭐ 来歴に無い行は Fragment にならず保留に落ちる。

    記法だけを根拠に機械が変換すると、機構と同じ記法で書かれた本人の記述を
    黙って取り込んでしまう。**機械は決めない** —— 判断はユーザーへ渡す。
    """
    content = (
        "## 2026-05-15\n"
        "- 機構が抽出した知識\n"
        "- 同じ記法だが来歴に無い行\n"
    )
    split = split_page_body(content, attest("- 機構が抽出した知識"))

    assert [f.content for f in split.fragments] == ["機構が抽出した知識"]
    assert [f.content for f in split.pending] == ["同じ記法だが来歴に無い行"]
    assert all(f.evidence == "notation" for f in split.pending)


def test_no_history_means_everything_is_pending():
    """来歴を渡さなければ全行が保留。来歴が無いことを『機構が書いた』と読み替えない。"""
    split = split_page_body("## 2026-05-15\n- 何かの記述\n- もう一つ\n")
    assert split.fragments == []
    assert len(split.pending) == 2


def test_ambiguous_duplicate_lines_all_fall_back_to_pending():
    """⭐ 本文の重複が来歴の回数より多いなら、1 つも確証にしない。

    どれが機構の行か決められない。回数を上から順に消費すると、人が先に書いた
    行を機構由来と誤認する（そして日付まで間違った Fragment ができる）——
    Codex 指摘 2026-08-05。決められないものは全部ユーザーへ渡す。
    """
    content = (
        "## 2026-05-15\n"
        "- 同じ内容の行\n"
        "\n"
        "## 2026-05-20\n"
        "- 同じ内容の行\n"
    )
    split = split_page_body(content, attest("- 同じ内容の行"))  # 機構は 1 回だけ足した

    assert split.fragments == []
    assert [f.line_no for f in split.pending] == [2, 5]
    assert "ambiguous_duplicate" in [m.kind for m in split.marks]


def test_duplicates_fully_covered_by_history_are_all_attested():
    """来歴が本文の重複数を満たしていれば、どれも機構の行として扱える。"""
    content = (
        "## 2026-05-15\n"
        "- 同じ内容の行\n"
        "\n"
        "## 2026-05-20\n"
        "- 同じ内容の行\n"
    )
    split = split_page_body(content, attest("- 同じ内容の行", "- 同じ内容の行"))

    assert [f.line_no for f in split.fragments] == [2, 5]
    assert split.pending == []
    assert [m.kind for m in split.marks] == []


# --------------------------------------------------------------------------
# 保留行の決定
# --------------------------------------------------------------------------

def test_pending_defaults_to_staying_in_body():
    content = "## 2026-05-15\n- 保留になる行\n"
    body = split_page_body(content).render_body()
    assert body == content


def test_pending_decided_as_fragment_leaves_the_body():
    content = (
        "## 2026-05-15\n"
        "- 機構と確定している行\n"
        "- 判断待ちの行\n"
    )
    split = split_page_body(content, attest("- 機構と確定している行"))
    pending_line = split.pending[0].line_no

    assert "- 判断待ちの行" in split.render_body()
    assert split.render_body({pending_line: "fragment"}) == ""


def test_date_heading_survives_only_if_something_remains_under_it():
    content = "## 2026-05-15\n- 出ていく行\n- 残る行\n"
    split = split_page_body(content, attest("- 出ていく行"))
    assert split.render_body() == "## 2026-05-15\n- 残る行\n"


def test_taken_lists_confirmed_plus_decided_in_line_order():
    content = "## 2026-05-15\n- 保留\n- 確証\n"
    split = split_page_body(content, attest("- 確証"))
    assert [d.line_no for d in split.taken()] == [3]
    assert [d.line_no for d in split.taken({2: "fragment"})] == [2, 3]


# --------------------------------------------------------------------------
# 逐語性 — 原文の行をそのまま連結する
# --------------------------------------------------------------------------

def test_human_written_page_is_returned_byte_for_byte():
    content = (
        "## 好きなイラストレーター\n"
        "以下のイラストレーターの空気感を好む。\n"
        "- **フカヒレ**（大ファンでサイン会にも行っている）\n"
    )
    split = split_page_body(content)
    assert split.fragments == []
    assert split.pending == []
    assert split.render_body() == content


def test_crlf_and_blank_runs_are_preserved():
    """⭐ 改行コードも空行の連続もいじらない。

    整形を混ぜると「文字を移すだけ」という約束が崩れ、しかも逐語の検算
    (空行と見出しを数えない) では捕まらない (Codex 指摘 2026-08-05)。
    """
    content = "## 2026-05-15\r\n- 機構の行\r\n\r\n\r\n本人の段落\r\n"
    split = split_page_body(content, attest("- 機構の行"))
    body = split.render_body()

    # 抜けるのは機構の行だけ。日付見出しは、同じブロックに本人の段落が残るので
    # 一緒に残る (残った記述が宙に浮かないように)。空行 2 連も CRLF もそのまま。
    assert body == "## 2026-05-15\r\n\r\n\r\n本人の段落\r\n"


def test_trailing_blank_lines_are_not_trimmed():
    content = "本文\n\n\n"
    assert split_page_body(content).render_body() == content


def test_empty_content():
    split = split_page_body("")
    assert split.render_body() == ""
    assert split.fragments == []
    assert not split.changed


# --------------------------------------------------------------------------
# 記法から外れる行 (保留にもならず本文へ)
# --------------------------------------------------------------------------

def test_prose_continuation_keeps_the_bullet_in_body():
    content = (
        "## 2026-05-15\n"
        "- ここから始まって\n"
        "この段落は後から書き足されたもの。\n"
    )
    split = split_page_body(content)
    assert split.fragments == []
    assert split.pending == []
    assert split.render_body() == content


def test_nested_bullet_keeps_its_parent_in_body():
    content = "## 2026-05-15\n- 親の項目\n  - 入れ子の項目\n"
    split = split_page_body(content)
    assert split.pending == []
    assert split.render_body() == content


def test_fenced_code_block_is_never_touched():
    """⭐ コードフェンスの中の箇条書きは記憶ではない (Codex 指摘 2026-08-05)。"""
    content = (
        "## 2026-05-15\n"
        "```\n"
        "- これはコード例であって記憶ではない\n"
        "```\n"
        "- これは機構の行\n"
    )
    split = split_page_body(content, attest(
        "- これはコード例であって記憶ではない", "- これは機構の行",
    ))
    assert [f.content for f in split.fragments] == ["これは機構の行"]
    assert split.pending == []
    assert "- これはコード例であって記憶ではない" in split.render_body()


def test_attested_but_wrapped_line_stays_in_body_with_a_mark():
    content = "## 2026-05-15\n- 来歴にはあるが折り返している\n  続きの行\n"
    split = split_page_body(content, attest("- 来歴にはあるが折り返している"))

    assert split.fragments == []
    assert "attested_wrapped" in [m.kind for m in split.marks]
    assert split.render_body() == content


def test_attested_line_outside_a_date_block_stays_in_body_with_a_mark():
    content = "- 日付ブロックの外にある行\n"
    split = split_page_body(content, attest("- 日付ブロックの外にある行"))

    assert split.fragments == []
    assert "attested_outside_block" in [m.kind for m in split.marks]
    assert split.render_body() == content


def test_date_like_heading_with_extra_text_is_not_a_machine_block():
    content = "## 2026-05-15 まはーとの会話\n- 本人が書いた行\n"
    split = split_page_body(content, attest("- 本人が書いた行"))
    assert split.fragments == []
    assert split.pending == []
    assert split.render_body() == content


def test_source_date_is_verbatim_from_heading():
    split = split_page_body("## 2024-12-30\n- 一番古い記憶\n", attest("- 一番古い記憶"))
    assert split.fragments[0].source_date == "2024-12-30"
    assert split.fragments[0].line_no == 2


def test_fence_is_closed_only_by_the_same_marker_and_length():
    """⭐ backtick フェンスの中の ~~~ や、短い ``` では閉じない。

    単一の真偽値で開閉すると、コード例の箇条書きが記憶として抽出される
    (Codex 指摘 2026-08-05)。
    """
    content = (
        "## 2026-05-15\n"
        "````\n"
        "~~~\n"
        "```\n"
        "- コード例の中の行\n"
        "````\n"
        "- 機構の行\n"
    )
    split = split_page_body(content, attest("- コード例の中の行", "- 機構の行"))

    assert [f.content for f in split.fragments] == ["機構の行"]
    assert "- コード例の中の行" in split.render_body()


def test_unclosed_fence_swallows_the_rest_of_the_page():
    """閉じ忘れたフェンスの後ろは、すべて本文のまま (勝手に取り出さない)。"""
    content = "## 2026-05-15\n```\n- 閉じ忘れたフェンスの中\n"
    split = split_page_body(content, attest("- 閉じ忘れたフェンスの中"))
    assert split.fragments == []
    assert split.pending == []
    assert split.render_body() == content


def test_info_string_does_not_close_a_fence():
    """⭐ ```python は開始行であって閉じ行ではない (Codex 指摘 2026-08-05)。

    prefix 一致で閉じ扱いにすると、その後のコード例の箇条書きが抽出される。
    """
    content = (
        "## 2026-05-15\n"
        "```\n"
        "```python\n"
        "- コード例の中の行\n"
        "```\n"
        "- 機構の行\n"
    )
    split = split_page_body(content, attest("- コード例の中の行", "- 機構の行"))

    assert [f.content for f in split.fragments] == ["機構の行"]
    assert "- コード例の中の行" in split.render_body()


def test_same_text_outside_the_block_counts_toward_ambiguity():
    """除外領域にある同じ記述も数える。候補側だけ数えると来歴の回数に届いてしまう。"""
    content = (
        "- 日付ブロックの外にある同じ記述\n"
        "\n"
        "## 2026-05-15\n"
        "- 日付ブロックの外にある同じ記述\n"
    )
    split = split_page_body(content, attest("- 日付ブロックの外にある同じ記述"))
    assert split.fragments == []
    assert len(split.pending) == 1


def test_indented_continuation_after_a_blank_line_is_still_a_continuation():
    """⭐ 空行を挟んだ字下げ行は、その項目の続き (Codex 指摘 2026-08-05)。"""
    content = (
        "## 2026-05-15\n"
        "- 機構の行に見えるが続きがある\n"
        "\n"
        "    字下げされた続きの段落\n"
    )
    split = split_page_body(content, attest("- 機構の行に見えるが続きがある"))
    assert split.fragments == []
    assert split.render_body() == content


def test_blank_line_then_unindented_paragraph_ends_the_item():
    """空行のあとの字下げなしの段落はリストの外。項目は完結している。"""
    content = (
        "## 2026-05-15\n"
        "- 機構の行\n"
        "\n"
        "本人が書いた段落\n"
    )
    split = split_page_body(content, attest("- 機構の行"))
    assert [f.content for f in split.fragments] == ["機構の行"]


def test_trailing_spaces_in_a_bullet_are_preserved():
    """⭐ 行末の空白 2 つは Markdown で改行の意味を持つ。削らない。

    strip() で落とすと原文と Fragment が食い違うのに、残った本文だけを見る
    逐語の検算は通ってしまう (Codex 指摘 2026-08-05)。
    """
    content = "## 2026-05-15\n- 行末に空白がある行  \n"
    split = split_page_body(content, attest("- 行末に空白がある行"))
    assert [f.content for f in split.fragments] == ["行末に空白がある行  "]


def test_heading_without_a_space_is_not_a_date_block():
    content = "##2026-05-15\n- 本文の行\n"
    split = split_page_body(content, attest("- 本文の行"))
    assert split.fragments == []
    assert split.render_body() == content


def test_impossible_date_is_not_a_date_block():
    content = "## 2026-13-45\n- 本文の行\n"
    split = split_page_body(content, attest("- 本文の行"))
    assert split.fragments == []
    assert split.render_body() == content
