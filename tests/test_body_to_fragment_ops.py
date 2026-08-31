"""本文 → Fragment 変換の適用層（下見 / 実行 / 取り消し）のテスト。

設計: docs/intent/memopedia_body_to_fragment.md

検証対象:
- 編集来歴の diff から「機構が足した行」を復元できること
  （``generate_diff`` はハンク見出しの直後の行を癒着させるので、その補正込み）
- 下見が①来歴の裏づけ / ②保留 を分けて返すこと
- 実行が「来歴の裏づけ + 明示的に判断された保留行」だけを Fragment にすること
- **判断を渡さなかった保留行が本文に残ること**（機械が代わりに決めない）
- 逐語の検算に落ちたら何も書かずに拒否すること
- 取り消しでページ本文と Fragment が変換前へ完全に戻ること
- Chronicle のページと trunk を対象にしないこと
"""
from __future__ import annotations

import sqlite3

import pytest

from sai_memory.memopedia.body_to_fragment import (
    _added_lines,
    apply_conversion,
    attested_machine_lines,
    list_conversion_runs,
    preview_conversion,
    revert_conversion,
)
from sai_memory.memopedia.storage import (
    create_page,
    generate_diff,
    get_fragments_for_entity,
    get_page,
    init_memopedia_tables,
    record_page_edit,
)


@pytest.fixture()
def conn():
    connection = sqlite3.connect(":memory:")
    init_memopedia_tables(connection)
    yield connection
    connection.close()


def _page_with_extractor_history(conn, title, before, after, *, category="people"):
    """``before`` の本文を持つページを作り、``after`` への機構の追記を来歴へ刻む。"""
    page = create_page(conn, parent_id=None, title=title, content=after, category=category)
    record_page_edit(
        conn,
        page_id=page.id,
        diff_text=generate_diff(
            f"title: {title}\nsummary: \ncontent:\n{before}",
            f"title: {title}\nsummary: \ncontent:\n{after}",
        ),
        edit_type="append",
        edit_source="entity_extractor",
    )
    conn.commit()
    return page


def _apply(conn, decisions=None):
    """下見して指紋を取り、その指紋で実行する（本番の UI と同じ順序）。"""
    preview = preview_conversion(conn, decisions)
    return apply_conversion(
        conn, decisions=decisions, expected_fingerprint=preview["fingerprint"]
    )


# --------------------------------------------------------------------------
# diff から追加行を復元する
# --------------------------------------------------------------------------

def test_added_lines_recovers_the_first_line_after_a_hunk_header():
    """ハンク見出しに癒着した 1 行目を取りこぼさないこと。

    ``storage.generate_diff`` は ``lineterm=""`` のまま ``"".join()`` するので、
    ``@@ ... @@`` の直後の行が見出しと同じ行に並ぶ。補正しないと各ハンクの
    先頭行が丸ごと落ちる。
    """
    diff = generate_diff("content:\n", "content:\n- 追加された最初の行\n- 二行目\n")
    assert "- 追加された最初の行" in _added_lines(diff)
    assert "- 二行目" in _added_lines(diff)


def test_attested_lines_are_collected_per_page(conn):
    page = _page_with_extractor_history(
        conn, "テスト",
        "## 2026-05-15\n- 既にあった行\n",
        "## 2026-05-15\n- 既にあった行\n- 機構が足した行\n",
    )
    attested = attested_machine_lines(conn)
    assert "- 機構が足した行" in attested[page.id]
    assert "- 既にあった行" not in attested[page.id]


def test_non_extractor_edits_are_not_attested(conn):
    """``manual_ui`` は「編集の経路」であって「本文の著者」ではないので数えない。"""
    page = create_page(conn, parent_id=None, title="手入力", content="## 2026-05-15\n- UI から入った行\n", category="people")
    record_page_edit(
        conn, page_id=page.id,
        diff_text=generate_diff("content:\n", "content:\n- UI から入った行\n"),
        edit_type="update", edit_source="manual_ui",
    )
    conn.commit()
    assert attested_machine_lines(conn).get(page.id, {}) == {}


# --------------------------------------------------------------------------
# 下見
# --------------------------------------------------------------------------

def test_preview_separates_confirmed_from_pending(conn):
    _page_with_extractor_history(
        conn, "アイフィ",
        "## 2026-05-15\n- 来歴に無い行\n",
        "## 2026-05-15\n- 来歴に無い行\n- 機構が足した行\n",
    )
    preview = preview_conversion(conn)

    assert preview["confirmed_count"] == 1
    assert preview["pending_count"] == 1
    # UI の「全 N ページ中」の分母 — 対象外ページも含めた生存ページ数
    assert preview["total_page_count"] == 1
    assert preview["is_safe"] is True
    assert preview["conservation"]["before_lines"] == preview["conservation"]["after_lines"]

    pending = preview["pending_pages"]
    assert len(pending) == 1
    lines = [line for block in pending[0]["blocks"] for line in block["lines"]]
    roles = {line["content"]: line["role"] for line in lines}
    assert roles["機構が足した行"] == "fragment"
    assert roles["来歴に無い行"] == "pending"


def test_preview_skips_chronicle_pages_and_trunks(conn):
    create_page(
        conn, parent_id=None, title="時間の地図", category="chronicle",
        is_trunk=True, content="## 2026-05-15\n- 幹の行\n",
    )
    create_page(
        conn, parent_id=None, title="ある日の記録", category="chronicle",
        content="## 2026-05-15\n- 物語の行\n",
    )
    preview = preview_conversion(conn)
    assert preview["page_count"] == 0
    assert preview["pending_count"] == 0
    # 対象外の Chronicle ページも分母には数えられる（ユーザーから見れば存在する
    # ページ）が、trunk はカテゴリの器なので数えない
    assert preview["total_page_count"] == 1


# --------------------------------------------------------------------------
# 実行
# --------------------------------------------------------------------------

def test_apply_converts_attested_and_leaves_pending_in_body(conn):
    page = _page_with_extractor_history(
        conn, "アイフィ",
        "## 2026-05-15\n- 来歴に無い行\n",
        "## 2026-05-15\n- 来歴に無い行\n- 機構が足した行\n",
    )
    result = _apply(conn)

    assert result["fragment_count"] == 1
    assert result["confirmed_count"] == 1
    assert result["decided_count"] == 0
    assert result["pending_left"] == 1

    fragments = get_fragments_for_entity(conn, page.id)
    assert [f.content for f in fragments] == ["機構が足した行"]
    assert fragments[0].source_date == "2026-05-15"
    assert fragments[0].chronicle_entry_id is None

    body = get_page(conn, page.id).content
    assert "- 来歴に無い行" in body
    assert "## 2026-05-15" in body
    assert "機構が足した行" not in body


def test_apply_honours_explicit_decisions(conn):
    page = _page_with_extractor_history(
        conn, "アイフィ",
        "## 2026-05-15\n- 来歴に無い行\n",
        "## 2026-05-15\n- 来歴に無い行\n- 機構が足した行\n",
    )
    preview = preview_conversion(conn)
    pending = [
        line for p in preview["pending_pages"] for b in p["blocks"] for line in b["lines"]
        if line["role"] == "pending"
    ]
    decisions = {page.id: {str(pending[0]["line_no"]): "fragment"}}

    result = apply_conversion(
        conn, decisions=decisions, expected_fingerprint=preview["fingerprint"]
    )
    assert result["fragment_count"] == 2
    assert result["decided_count"] == 1
    assert result["pending_left"] == 0
    # 全部出ていったので日付見出しごと本文が空になる
    assert get_page(conn, page.id).content == ""


def test_apply_refuses_when_conservation_fails(conn, monkeypatch):
    """逐語の検算に落ちたら何も書かずに止まる。"""
    _page_with_extractor_history(
        conn, "アイフィ",
        "## 2026-05-15\n",
        "## 2026-05-15\n- 機構が足した行\n",
    )
    import sai_memory.memopedia.body_to_fragment as mod

    real_preview = mod.preview_conversion

    def broken(conn_, decisions=None):
        preview = real_preview(conn_, decisions)
        preview["is_safe"] = False
        preview["conservation"]["lost"] = ["- 消えた行"]
        return preview

    monkeypatch.setattr(mod, "preview_conversion", broken)
    with pytest.raises(ValueError, match="逐語の検算"):
        _apply(conn)

    assert conn.execute("SELECT COUNT(*) FROM memopedia_fragments").fetchone()[0] == 0


# --------------------------------------------------------------------------
# 取り消し
# --------------------------------------------------------------------------

def test_revert_restores_body_and_removes_fragments(conn):
    page = _page_with_extractor_history(
        conn, "アイフィ",
        "## 2026-05-15\n- 来歴に無い行\n",
        "## 2026-05-15\n- 来歴に無い行\n- 機構が足した行\n",
    )
    before_body = get_page(conn, page.id).content

    result = _apply(conn)
    assert get_page(conn, page.id).content != before_body

    runs = list_conversion_runs(conn)
    assert [r["run_id"] for r in runs] == [result["run_id"]]

    revert = revert_conversion(conn, result["run_id"])
    assert revert["restored_pages"] == 1
    assert revert["deleted_fragments"] == 1

    assert get_page(conn, page.id).content == before_body
    assert get_fragments_for_entity(conn, page.id) == []
    assert list_conversion_runs(conn) == []


def test_revert_keeps_fragments_made_outside_the_run(conn):
    """他の経路で作られた Fragment を巻き添えにしない。"""
    from sai_memory.memopedia.storage import create_fragment

    page = _page_with_extractor_history(
        conn, "アイフィ",
        "## 2026-05-15\n",
        "## 2026-05-15\n- 機構が足した行\n",
    )
    keeper = create_fragment(
        conn, entity_id=page.id, content="変換とは無関係の Fragment",
        chronicle_entry_id="chr-1", source_date="2026-07-01",
    )

    result = _apply(conn)
    revert_conversion(conn, result["run_id"])

    remaining = get_fragments_for_entity(conn, page.id)
    assert [f.id for f in remaining] == [keeper.id]


def test_revert_unknown_run_raises(conn):
    with pytest.raises(ValueError, match="見つかりません"):
        revert_conversion(conn, "存在しない")


def test_revert_refuses_when_the_page_was_edited_after_the_conversion(conn):
    """⭐ 変換後に別経路が書き換えたページは、既定で取り消しを拒む。

    変換前へ戻すとその編集も消える。何を失うかを示して判断を仰ぐ
    (Codex 指摘 2026-08-05)。
    """
    page = _page_with_extractor_history(
        conn, "アイフィ",
        "## 2026-05-15\n",
        "## 2026-05-15\n- 機構が足した行\n",
    )
    result = _apply(conn)

    # 変換のあとで別経路がこのページを書き換えた（本文が実際に変わる）
    conn.execute(
        "UPDATE memopedia_pages SET content = ? WHERE id = ?",
        ("あとから手で書き直した本文\n", page.id),
    )
    record_page_edit(
        conn, page_id=page.id,
        diff_text="dummy", edit_type="update", edit_source="manual_ui",
    )
    conn.commit()

    with pytest.raises(ValueError, match="取り消しを中止しました"):
        revert_conversion(conn, result["run_id"])

    # 拒否したなら何も壊していないこと（Fragment も実行記録も残る）
    assert get_fragments_for_entity(conn, page.id) != []
    assert [r["run_id"] for r in list_conversion_runs(conn)] == [result["run_id"]]

    # 承知のうえの強制指定でだけ戻す
    revert = revert_conversion(conn, result["run_id"], force=True)
    assert [o["title"] for o in revert["overwritten_pages"]] == ["アイフィ"]
    assert revert["overwritten_pages"][0]["edit_count"] == 1
    assert get_fragments_for_entity(conn, page.id) == []


def test_revert_reports_nothing_when_untouched(conn):
    _page_with_extractor_history(
        conn, "アイフィ",
        "## 2026-05-15\n",
        "## 2026-05-15\n- 機構が足した行\n",
    )
    result = _apply(conn)
    assert revert_conversion(conn, result["run_id"])["overwritten_pages"] == []


def test_conservation_tolerates_bullet_spacing_variation(conn):
    """``-  二つスペース`` のような書きぶりの揺れで検算が落ちないこと。

    本文に残る行は原文のまま、Fragment 側は中身だけ。同じ記述を同じキーで
    数えないと、揺れが「行が消えた・現れた」として検算に出てしまう。
    """
    _page_with_extractor_history(
        conn, "アイフィ",
        "## 2026-05-15\n",
        "## 2026-05-15\n-   空白が多い行\n",
    )
    preview = preview_conversion(conn)
    assert preview["is_safe"] is True
    assert preview["conservation"]["lost"] == []
    assert preview["conservation"]["gained"] == []
    _apply(conn)


# --------------------------------------------------------------------------
# Codex レビュー (2026-08-05) で見つかった穴の回帰
# --------------------------------------------------------------------------

def test_apply_is_atomic_across_pages(conn, monkeypatch):
    """⭐ 途中で失敗したら 1 ページも変わっていないこと。

    ページごとに確定していくと、失敗した変換の残骸が本文と Fragment に散る。
    """
    pages = [
        _page_with_extractor_history(
            conn, f"ページ{i}",
            "## 2026-05-15\n",
            f"## 2026-05-15\n- 機構が足した行{i}\n",
        )
        for i in range(3)
    ]
    before = {p.id: get_page(conn, p.id).content for p in pages}

    import sai_memory.memopedia.body_to_fragment as mod

    real_split = mod.split_page_body
    seen = {"n": 0}

    def exploding(content, attested=None):
        seen["n"] += 1
        if seen["n"] > 5:  # 下見のぶんを通し、実行の 2 ページ目で落とす
            raise RuntimeError("途中で失敗")
        return real_split(content, attested)

    monkeypatch.setattr(mod, "split_page_body", exploding)
    with pytest.raises(RuntimeError):
        _apply(conn)

    assert conn.execute("SELECT COUNT(*) FROM memopedia_fragments").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM memopedia_body_conversion").fetchone()[0] == 0
    for page in pages:
        assert get_page(conn, page.id).content == before[page.id]


def test_apply_refuses_when_the_body_changed_since_the_preview(conn):
    """⭐ 下見のあとで本文が変われば、選んだ行番号が別の行を指しうる。"""
    page = _page_with_extractor_history(
        conn, "アイフィ",
        "## 2026-05-15\n- 保留の行\n",
        "## 2026-05-15\n- 保留の行\n- 機構が足した行\n",
    )
    preview = preview_conversion(conn)
    stale = preview["fingerprint"]

    conn.execute(
        "UPDATE memopedia_pages SET content = ? WHERE id = ?",
        ("## 2026-05-15\n- 割り込みで入った行\n- 保留の行\n- 機構が足した行\n", page.id),
    )
    conn.commit()

    with pytest.raises(ValueError, match="本文か編集来歴が変わりました"):
        apply_conversion(conn, decisions={page.id: {"2": "fragment"}}, expected_fingerprint=stale)


def test_apply_requires_a_fingerprint_when_decisions_are_given(conn):
    page = _page_with_extractor_history(
        conn, "アイフィ",
        "## 2026-05-15\n- 保留の行\n",
        "## 2026-05-15\n- 保留の行\n- 機構が足した行\n",
    )
    with pytest.raises(ValueError, match="指紋が要ります"):
        apply_conversion(conn, decisions={page.id: {"2": "fragment"}}, expected_fingerprint="")


def test_pages_without_machine_history_are_out_of_scope(conn):
    """⭐ 対象は機構が本文へ書き足した実績のあるページだけ。

    カテゴリの一覧で線を引くと、新しいカテゴリが増えるたびに漏れる。
    """
    create_page(
        conn, parent_id=None, title="手書きのページ", category="core",
        content="## 2026-05-15\n- 誰かが手で書いた行\n",
    )
    preview = preview_conversion(conn)
    assert preview["page_count"] == 0
    assert preview["pending_count"] == 0


def test_added_lines_does_not_mangle_content_containing_diff_headers(conn):
    """⭐ diff の構造見出しは行頭でだけ剥がす。

    diff 全体へ文字列置換をかけると、本文に同じ文字列が含まれていたときに
    中身を壊し、機構の行なのに確証から外れる。
    """
    line = "- ログに --- before と +++ after が出ていた"
    diff = generate_diff("content:\n", f"content:\n{line}\n")
    assert line in _added_lines(diff)


def test_revert_refuses_instead_of_losing_memory_when_a_snapshot_is_missing(conn):
    """⭐ 復元できないページがあるなら、何も消さずに中止する。

    Fragment を先に消してから「復元できないページは飛ばす」と、本文は変換後の
    まま・Fragment は消滅・戻す手がかりも消滅、という記憶が失われた状態が
    「成功」として残る (Codex 指摘 2026-08-05)。
    """
    page = _page_with_extractor_history(
        conn, "アイフィ",
        "## 2026-05-15\n",
        "## 2026-05-15\n- 機構が足した行\n",
    )
    result = _apply(conn)
    converted_body = get_page(conn, page.id).content

    # 変換のスナップショットが失われた状況を作る
    conn.execute(
        "DELETE FROM memopedia_page_edit_history WHERE edit_source = ?",
        (f"body_to_fragment:{result['run_id']}",),
    )
    conn.commit()

    with pytest.raises(ValueError, match="復元できないページ"):
        revert_conversion(conn, result["run_id"])

    # 何も失われていないこと
    assert get_fragments_for_entity(conn, page.id) != []
    assert [r["run_id"] for r in list_conversion_runs(conn)] == [result["run_id"]]
    assert get_page(conn, page.id).content == converted_body


def test_fingerprint_covers_the_edit_history_not_just_the_body(conn):
    """⭐ 本文が同じでも来歴が変われば判定が変わる。指紋はそれも見る。"""
    page = _page_with_extractor_history(
        conn, "アイフィ",
        "## 2026-05-15\n- 保留の行\n",
        "## 2026-05-15\n- 保留の行\n- 機構が足した行\n",
    )
    before = preview_conversion(conn)["fingerprint"]

    # 本文はそのまま、来歴だけ増やす（保留だった行が確証あり側へ回る）
    record_page_edit(
        conn, page_id=page.id,
        diff_text=generate_diff("content:\n", "content:\n- 保留の行\n"),
        edit_type="append", edit_source="entity_extractor",
    )
    conn.commit()

    assert preview_conversion(conn)["fingerprint"] != before
    with pytest.raises(ValueError, match="本文か編集来歴が変わりました"):
        apply_conversion(conn, expected_fingerprint=before)


def test_reconversion_does_not_reattest_text_the_user_retyped(conn):
    """⭐ 一度 Fragment へ移した記述を、ユーザーが本文へ書き直したら保留にする。

    来歴の回数は累計なので、すでに外へ出した分を差し引かないと、書き直した行が
    また機構由来と判定される (Codex 指摘 2026-08-05)。
    """
    page = _page_with_extractor_history(
        conn, "アイフィ",
        "## 2026-05-15\n",
        "## 2026-05-15\n- 機構が足した行\n",
    )
    _apply(conn)
    assert [f.content for f in get_fragments_for_entity(conn, page.id)] == ["機構が足した行"]

    # ユーザーが同じ記述を本文へ書き直した
    conn.execute(
        "UPDATE memopedia_pages SET content = ? WHERE id = ?",
        ("## 2026-05-20\n- 機構が足した行\n", page.id),
    )
    conn.commit()

    preview = preview_conversion(conn)
    assert preview["confirmed_count"] == 0
    assert preview["pending_count"] == 1


def test_revert_refuses_when_a_created_fragment_was_edited(conn):
    """⭐ 変換後に中身を編集された Fragment は、黙って消さない。

    Fragment の編集はページの編集来歴に残らないので、来歴だけを見ていると
    「後続の編集は無い」と判断して編集内容ごと消してしまう。
    """
    page = _page_with_extractor_history(
        conn, "アイフィ",
        "## 2026-05-15\n",
        "## 2026-05-15\n- 機構が足した行\n",
    )
    result = _apply(conn)
    frag = get_fragments_for_entity(conn, page.id)[0]

    conn.execute(
        "UPDATE memopedia_fragments SET content = ? WHERE id = ?",
        ("あとから手で直した内容", frag.id),
    )
    conn.commit()

    with pytest.raises(ValueError, match="Fragment に編集"):
        revert_conversion(conn, result["run_id"])
    assert get_fragments_for_entity(conn, page.id) != []

    revert = revert_conversion(conn, result["run_id"], force=True)
    assert revert["deleted_fragments"] == 1


def test_manual_fragments_are_not_counted_as_already_moved(conn):
    """⭐ 手で作った Fragment を「機構が移した分」として差し引かない。

    現存する Fragment の本文を数えると、同じ文字列の手動 Fragment が
    機構の移動分として差し引かれ、本来変換すべき行が保留に落ちる。
    """
    from sai_memory.memopedia.storage import create_fragment

    page = _page_with_extractor_history(
        conn, "アイフィ",
        "## 2026-05-15\n",
        "## 2026-05-15\n- 機構が足した行\n",
    )
    create_fragment(conn, entity_id=page.id, content="機構が足した行")

    preview = preview_conversion(conn)
    assert preview["confirmed_count"] == 1
    assert preview["pending_count"] == 0


def test_force_revert_refuses_when_a_later_run_touched_the_same_page(conn):
    """⭐ 後続の変換がある実行は force でも戻さない。

    本文だけ古い姿へ戻ると、後続 run の Fragment と実行記録が孤立して
    同じ記述が二重になる (Codex 指摘 2026-08-05)。
    """
    page = _page_with_extractor_history(
        conn, "アイフィ",
        "## 2026-05-15\n- 保留の行\n",
        "## 2026-05-15\n- 保留の行\n- 機構が足した行\n",
    )
    first = _apply(conn)

    preview = preview_conversion(conn)
    pending = [
        line for p in preview["pending_pages"] for b in p["blocks"] for line in b["lines"]
        if line["role"] == "pending"
    ]
    second = apply_conversion(
        conn,
        decisions={page.id: {str(pending[0]["line_no"]): "fragment"}},
        expected_fingerprint=preview["fingerprint"],
    )
    assert second["run_id"] != first["run_id"]

    with pytest.raises(ValueError, match="別の変換が同じページを触っています"):
        revert_conversion(conn, first["run_id"], force=True)


def test_fingerprint_changes_when_the_conversion_ledger_changes(conn):
    """台帳の状態も判定に効くので、変われば指紋も変わる。"""
    _page_with_extractor_history(
        conn, "アイフィ",
        "## 2026-05-15\n",
        "## 2026-05-15\n- 機構が足した行\n",
    )
    before = preview_conversion(conn)["fingerprint"]
    _apply(conn)
    assert preview_conversion(conn)["fingerprint"] != before


def test_revert_refuses_when_a_created_fragment_disappeared(conn):
    """変換で作った Fragment が消えていたら、黙って進めない。"""
    page = _page_with_extractor_history(
        conn, "アイフィ",
        "## 2026-05-15\n",
        "## 2026-05-15\n- 機構が足した行\n",
    )
    result = _apply(conn)
    frag = get_fragments_for_entity(conn, page.id)[0]
    conn.execute("DELETE FROM memopedia_fragments WHERE id = ?", (frag.id,))
    conn.commit()

    with pytest.raises(ValueError, match="Fragment に編集"):
        revert_conversion(conn, result["run_id"])


def test_the_latest_run_can_still_be_reverted_even_within_the_same_second(conn):
    """⭐ 新しい方から取り消す、という正しい手順を塞がないこと。

    「後続」の判定を converted_at (秒単位) でやると、同じ秒に入った先行 run が
    自分より後だと誤判定され、どちらも取り消せなくなる。
    """
    page = _page_with_extractor_history(
        conn, "アイフィ",
        "## 2026-05-15\n- 保留の行\n",
        "## 2026-05-15\n- 保留の行\n- 機構が足した行\n",
    )
    _apply(conn)

    preview = preview_conversion(conn)
    pending = [
        line for p in preview["pending_pages"] for b in p["blocks"] for line in b["lines"]
        if line["role"] == "pending"
    ]
    second = apply_conversion(
        conn,
        decisions={page.id: {str(pending[0]["line_no"]): "fragment"}},
        expected_fingerprint=preview["fingerprint"],
    )

    # 新しい方は取り消せる
    revert = revert_conversion(conn, second["run_id"])
    assert revert["restored_pages"] == 1


def test_revert_detects_a_body_change_that_left_no_edit_history(conn):
    """⭐ 来歴に残らない書き換えも競合として捕まえる。

    Memopedia.update_page はページ更新と来歴を別々に commit するので、その隙間で
    止まれば「本文は変わったのに来歴が無い」状態が成立する。他所の記帳の仕方に
    取り消しの安全を預けない (Codex 指摘 2026-08-05)。
    """
    page = _page_with_extractor_history(
        conn, "アイフィ",
        "## 2026-05-15\n",
        "## 2026-05-15\n- 機構が足した行\n",
    )
    result = _apply(conn)

    # 来歴を一切残さずに本文だけ書き換える
    conn.execute(
        "UPDATE memopedia_pages SET content = ? WHERE id = ?",
        ("来歴に残らない書き換え\n", page.id),
    )
    conn.commit()

    with pytest.raises(ValueError, match="取り消しを中止しました"):
        revert_conversion(conn, result["run_id"])


def test_revert_refuses_when_only_the_title_or_summary_changed(conn):
    """⭐ 取り消しが戻す値は本文だけではない。タイトル・要約の編集も競合。

    本文の指紋だけを見ていると、変換後にタイトルを直した編集を見落として
    黙って戻してしまう (Codex 指摘 2026-08-05)。
    """
    page = _page_with_extractor_history(
        conn, "アイフィ",
        "## 2026-05-15\n",
        "## 2026-05-15\n- 機構が足した行\n",
    )
    result = _apply(conn)

    conn.execute(
        "UPDATE memopedia_pages SET summary = ? WHERE id = ?",
        ("あとから付けた要約", page.id),
    )
    conn.commit()

    with pytest.raises(ValueError, match="取り消しを中止しました"):
        revert_conversion(conn, result["run_id"])


def test_revert_clears_soft_delete(conn):
    """⭐ 取り消しは閉架も解く。変換対象は必ず現役なので、戻すとは現役へ戻すこと。"""
    page = _page_with_extractor_history(
        conn, "アイフィ",
        "## 2026-05-15\n",
        "## 2026-05-15\n- 抽出された行\n",
    )
    result = _apply(conn)
    conn.execute("UPDATE memopedia_pages SET is_deleted = 1 WHERE id = ?", (page.id,))
    conn.commit()

    revert_conversion(conn, result["run_id"], force=True)
    row = conn.execute(
        "SELECT is_deleted, content FROM memopedia_pages WHERE id = ?", (page.id,)
    ).fetchone()
    assert row[0] == 0
    assert "- 抽出された行" in row[1]


def test_deleted_fragment_is_no_longer_counted_as_moved_out(conn):
    """⭐ 消された Fragment は「外に出したまま」ではない。差し引きから外す。

    一般の rollback や手動削除で Fragment が消えたあと、本文に戻った記述を
    移動済みとして数え落とすのを防ぐ (Codex 指摘 2026-08-05)。
    """
    page = _page_with_extractor_history(
        conn, "アイフィ",
        "## 2026-05-15\n",
        "## 2026-05-15\n- 抽出された行\n",
    )
    _apply(conn)
    frag = get_fragments_for_entity(conn, page.id)[0]

    # Fragment を消し、本文にも同じ記述が戻った状況
    conn.execute("DELETE FROM memopedia_fragments WHERE id = ?", (frag.id,))
    conn.execute(
        "UPDATE memopedia_pages SET content = ? WHERE id = ?",
        ("## 2026-05-15\n- 抽出された行\n", page.id),
    )
    conn.commit()

    preview = preview_conversion(conn)
    assert preview["confirmed_count"] == 1  # 差し引かれず、また移せる


def test_full_width_space_line_is_not_swallowed_by_emptying(conn):
    """⭐ 全角空白だけの行は「書かれた空白」。空本文の扱いで消さない。

    str.strip() は U+3000 も空白として落とすので、素直に書くと下見は消失 0 の
    まま実行時にだけ消える (Codex 指摘 2026-08-05)。
    """
    page = _page_with_extractor_history(
        conn, "アイフィ",
        "## 2026-05-15\n",
        "## 2026-05-15\n- 抽出された行\n　\n",
    )
    preview = preview_conversion(conn)
    assert preview["is_safe"] is True

    _apply(conn)
    body = get_page(conn, page.id).content
    assert "　" in body, "全角空白の行が消えた"


def test_reconversion_subtracts_lines_that_had_trailing_spaces(conn):
    """⭐ 末尾空白のある行も、既変換分としてちゃんと差し引く。

    来歴の照合キーは strip 済み、Fragment は逐語。同じ digest で突き合わせると
    末尾空白のある行だけ差し引けず、再変換で二重に移る (Codex 指摘 2026-08-05)。
    """
    page = _page_with_extractor_history(
        conn, "アイフィ",
        "## 2026-05-15\n",
        "## 2026-05-15\n- 末尾に空白がある行  \n",
    )
    _apply(conn)
    assert [f.content for f in get_fragments_for_entity(conn, page.id)] == [
        "末尾に空白がある行  "
    ]

    # ユーザーが同じ記述を本文へ書き直した → 差し引かれて保留になるはず
    conn.execute(
        "UPDATE memopedia_pages SET content = ? WHERE id = ?",
        ("## 2026-05-20\n- 末尾に空白がある行  \n", page.id),
    )
    conn.commit()
    preview = preview_conversion(conn)
    assert preview["confirmed_count"] == 0
    assert preview["pending_count"] == 1


def test_fingerprint_changes_when_a_ledger_fragment_is_deleted(conn):
    """⭐ Fragment が消えると差し引きが変わる。指紋もそれを見る。"""
    page = _page_with_extractor_history(
        conn, "アイフィ",
        "## 2026-05-15\n",
        "## 2026-05-15\n- 抽出された行\n",
    )
    _apply(conn)
    before = preview_conversion(conn)["fingerprint"]

    frag = get_fragments_for_entity(conn, page.id)[0]
    conn.execute("DELETE FROM memopedia_fragments WHERE id = ?", (frag.id,))
    conn.commit()

    assert preview_conversion(conn)["fingerprint"] != before


# --------------------------------------------------------------------------
# 既存 Fragment との照合（移行期の二重書き込み対策、intent §5.4 (a-6)）
# --------------------------------------------------------------------------

def _dual_era_page(conn, *, frag_date="2026-05-15"):
    """移行期の状態を再現: 本文の行と同内容の Fragment が既に併存するページ。"""
    from sai_memory.memopedia.storage import create_fragment

    page = _page_with_extractor_history(
        conn, "アイフィ",
        "## 2026-05-15\n",
        "## 2026-05-15\n- 移行期に二重で書かれた行\n",
    )
    frag = create_fragment(
        conn, entity_id=page.id, content="移行期に二重で書かれた行",
        source_date=frag_date,
    )
    return page, frag


def test_apply_reuses_an_existing_identical_fragment(conn):
    """⭐ 同内容の Fragment が既にあれば、新しくは作らず本文から抜くだけ。

    移行期の抽出器は本文への追記と Fragment の作成を同時に行っていた
    (コミット e1866ef)。照合せずに作ると同じ記憶が 2 件になる
    (Codex 指摘 2026-08-06)。
    """
    page, frag = _dual_era_page(conn)

    preview = preview_conversion(conn)
    assert preview["dedup_count"] == 1
    assert preview["fragment_count"] == 0
    assert any(m["kind"] == "already_fragment" for m in preview["marks"])

    result = _apply(conn)
    assert result["dedup_count"] == 1
    assert result["fragment_count"] == 0

    remaining = get_fragments_for_entity(conn, page.id)
    assert [f.id for f in remaining] == [frag.id], "Fragment が二重になった"
    assert get_page(conn, page.id).content == ""


def test_dedup_matches_a_fragment_without_a_date(conn):
    """日付を持たない既存 Fragment も、同内容なら寄せ先になる。"""
    page, frag = _dual_era_page(conn, frag_date=None)
    _apply(conn)
    assert [f.id for f in get_fragments_for_entity(conn, page.id)] == [frag.id]


def test_dedup_does_not_match_a_different_date(conn):
    """日付が食い違う同文は別の観測。寄せずに新しく作る。"""
    page, frag = _dual_era_page(conn, frag_date="2026-07-01")
    result = _apply(conn)
    assert result["dedup_count"] == 0
    assert result["fragment_count"] == 1
    assert len(get_fragments_for_entity(conn, page.id)) == 2


def test_one_existing_fragment_dedups_only_one_of_two_identical_lines(conn):
    """既存 Fragment 1 つが照合に使えるのは 1 回だけ。"""
    from sai_memory.memopedia.storage import create_fragment

    page = _page_with_extractor_history(
        conn, "アイフィ",
        "## 2026-05-15\n",
        "## 2026-05-15\n- 二回観測された行\n- 二回観測された行\n",
    )
    create_fragment(
        conn, entity_id=page.id, content="二回観測された行", source_date="2026-05-15",
    )

    result = _apply(conn)
    assert result["dedup_count"] == 1
    assert result["fragment_count"] == 1
    assert len(get_fragments_for_entity(conn, page.id)) == 2


def test_revert_restores_dedup_lines_without_deleting_the_existing_fragment(conn):
    """⭐ 取り消しは本文だけ戻す。寄せた先の Fragment は変換が作ったものではない。"""
    page, frag = _dual_era_page(conn)
    before_body = get_page(conn, page.id).content

    result = _apply(conn)
    revert = revert_conversion(conn, result["run_id"])
    assert revert["restored_dedup_lines"] == 1
    assert revert["deleted_fragments"] == 0

    assert get_page(conn, page.id).content == before_body
    assert [f.id for f in get_fragments_for_entity(conn, page.id)] == [frag.id]
    assert list_conversion_runs(conn) == []


def test_fingerprint_changes_when_a_matching_fragment_appears(conn):
    """⭐ 下見のあとに同内容の Fragment ができたら、行の行き先が変わる。再下見。"""
    from sai_memory.memopedia.storage import create_fragment

    page = _page_with_extractor_history(
        conn, "アイフィ",
        "## 2026-05-15\n",
        "## 2026-05-15\n- 抽出された行\n",
    )
    preview = preview_conversion(conn)

    create_fragment(
        conn, entity_id=page.id, content="抽出された行", source_date="2026-05-15",
    )
    with pytest.raises(ValueError, match="変わりました"):
        apply_conversion(
            conn, decisions=None, expected_fingerprint=preview["fingerprint"]
        )


def test_dedup_lines_still_count_as_moved_out(conn):
    """⭐ 寄せた行も「外に出した」勘定に入る。書き直しは保留に落ちること。"""
    page, _frag = _dual_era_page(conn)
    _apply(conn)

    # ユーザーが同じ記述を本文へ書き直した
    conn.execute(
        "UPDATE memopedia_pages SET content = ? WHERE id = ?",
        ("## 2026-05-20\n- 移行期に二重で書かれた行\n", page.id),
    )
    conn.commit()

    preview = preview_conversion(conn)
    assert preview["confirmed_count"] == 0
    assert preview["pending_count"] == 1
