"""スペル不使用モード (``AI.SPELL_ENABLED=false``) の回帰テスト。

無効にしたペルソナのシステムプロンプトから「スペルがあるから意味を持つ文章」が
全部消えること、実行側の最後の穴 (pre_spells) が塞がっていることを固定する。

設計: docs/intent/spell_disabled_mode.md
"""
from __future__ import annotations

import asyncio
import gc
import json
import logging
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import sea.runtime_llm as runtime_llm
from sea.head_pipeline import LineHeadInput
from sea.head_pipeline.sections.autonomy_modes import (
    _AUTONOMY_MODES_TEXT,
    _SPEECH_ONLY_TEXT,
    AutonomyModesSection,
)
from sea.head_pipeline.sections.available_playbooks import (
    AvailablePlaybooksSection,
    AvailablePlaybooksSnapshot,
    PlaybookEntry,
)
from sea.head_pipeline.sections.common_prompt import CommonPromptSection
from sea.head_pipeline.sections.desk import (
    DeskPageItem,
    DeskSection,
    DeskSnapshot,
)
from sea.head_pipeline.sections.memopedia_index import (
    MemopediaIndexSection,
    MemopediaIndexSnapshot,
)
from sea.head_pipeline.spell_gate import (
    apply_spell_markers,
    resolve_spell_enabled,
    resolve_spell_enabled_for,
)

# ---------------------------------------------------------------------------
# fixtures (tests/test_head_pipeline_spell_list.py と同じ隔離 DB の作り)
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_manager(request):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from database.models import AI, Base, City, User

    tmpdir = tempfile.TemporaryDirectory()
    db_path = Path(tmpdir.name) / "spell_disabled_test.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, future=True)

    db = SessionLocal()
    try:
        db.add(User(USERID=1, USERNAME="t", PASSWORD="x"))
        db.commit()
        db.add(City(CITYID=1, USERID=1, CITY_SLUG="c", UI_PORT=3000, API_PORT=8000))
        db.commit()
        db.add(AI(
            AIID="air", HOME_CITYID=1, AINAME="Air",
            DEFAULT_MODEL="claude-opus-4-7", SPELL_ENABLED=1,
        ))
        db.commit()
    finally:
        db.close()

    class FakeManager:
        pass

    manager = FakeManager()
    manager.SessionLocal = SessionLocal

    def _set_spell_enabled(value: bool) -> None:
        session = SessionLocal()
        try:
            session.query(AI).filter_by(AIID="air").update(
                {"SPELL_ENABLED": 1 if value else 0}
            )
            session.commit()
        finally:
            session.close()

    manager.set_spell_enabled = _set_spell_enabled

    def _cleanup():
        engine.dispose()
        gc.collect()
        try:
            tmpdir.cleanup()
        except PermissionError:
            pass

    request.addfinalizer(_cleanup)
    return manager


@pytest.fixture
def ctx(isolated_manager):
    return LineHeadInput(
        persona_id="air", line_id="main", line_role="main_line",
        model_key="claude-opus-4-7", current_building_id="b_lobby",
        manager=isolated_manager,
    )


# ---------------------------------------------------------------------------
# 1. 条件マーカーの処理
# ---------------------------------------------------------------------------

_SAMPLE = "\n".join([
    "前置き",
    "{if_spell_enabled}",
    "スペルの説明",
    "{end_if_spell_enabled}",
    "{if_spell_disabled}",
    "スペル無しの言い換え",
    "{end_if_spell_disabled}",
    "後置き",
])


def test_markers_keep_the_enabled_block_when_spell_is_enabled():
    out = apply_spell_markers(_SAMPLE, True)
    assert out == "前置き\nスペルの説明\n後置き"


def test_markers_keep_the_disabled_block_when_spell_is_disabled():
    out = apply_spell_markers(_SAMPLE, False)
    assert out == "前置き\nスペル無しの言い換え\n後置き"


def test_template_without_markers_is_untouched():
    """ユーザー上書きテンプレートは挙動が一切変わらない (文字列として同一)。"""
    template = "## 私だけのプロンプト\n自由に書いた文章。\n"
    assert apply_spell_markers(template, True) == template
    assert apply_spell_markers(template, False) == template


def test_marker_must_be_the_whole_line():
    """行の途中に現れた同じ文字列は本文 (マーカーとして働かない)。"""
    template = "説明の中で {if_spell_enabled} と書いた行\n次の行"
    assert apply_spell_markers(template, False) == template


def test_marker_line_may_be_indented():
    template = "A\n   {if_spell_enabled}   \nB\n\t{end_if_spell_enabled}\nC"
    assert apply_spell_markers(template, False) == "A\nC"
    assert apply_spell_markers(template, True) == "A\nB\nC"


def test_disabled_only_block_alone():
    """無効側ブロック単独 — 有効時に丸ごと消え、無効時に中身だけ残る。"""
    template = "A\n{if_spell_disabled}\nB\n{end_if_spell_disabled}\nC"
    assert apply_spell_markers(template, True) == "A\nC"
    assert apply_spell_markers(template, False) == "A\nB\nC"


def test_unclosed_marker_keeps_everything(caplog):
    """閉じ忘れは fail-safe — 中身は全部残し、マーカー行だけ落として警告。"""
    template = "A\n{if_spell_enabled}\nB\nC"
    with caplog.at_level(logging.WARNING):
        out = apply_spell_markers(template, False)
    assert out == "A\nB\nC"
    assert any("unbalanced" in rec.message for rec in caplog.records)


def test_stray_close_marker_keeps_everything(caplog):
    template = "A\n{end_if_spell_enabled}\nB"
    with caplog.at_level(logging.WARNING):
        out = apply_spell_markers(template, False)
    assert out == "A\nB"
    assert any("unbalanced" in rec.message for rec in caplog.records)


def test_nested_markers_keep_everything(caplog):
    template = "\n".join([
        "A",
        "{if_spell_enabled}",
        "B",
        "{if_spell_enabled}",
        "C",
        "{end_if_spell_enabled}",
        "D",
        "{end_if_spell_enabled}",
        "E",
    ])
    with caplog.at_level(logging.WARNING):
        out = apply_spell_markers(template, False)
    assert out == "A\nB\nC\nD\nE"
    assert any("unbalanced" in rec.message for rec in caplog.records)


def test_crossed_markers_keep_everything(caplog):
    """有効側の開きを無効側の閉じで閉じる形 (交差) も fail-safe。"""
    template = "A\n{if_spell_enabled}\nB\n{end_if_spell_disabled}\nC"
    with caplog.at_level(logging.WARNING):
        out = apply_spell_markers(template, False)
    assert out == "A\nB\nC"
    assert any("unbalanced" in rec.message for rec in caplog.records)


def test_unclosed_disabled_marker_keeps_everything(caplog):
    """fail-safe は無効側マーカーにも効く。"""
    template = "A\n{if_spell_disabled}\nB\nC"
    with caplog.at_level(logging.WARNING):
        out = apply_spell_markers(template, True)
    assert out == "A\nB\nC"
    assert any("unbalanced" in rec.message for rec in caplog.records)


def test_marker_on_the_first_line_works_with_a_utf8_bom(caplog):
    """BOM 付きで保存されたテンプレートの 1 行目もマーカーとして働く。

    BOM は ``str.strip()`` で落ちないので、素朴に書くと 1 行目が本文扱いになり、
    その先の閉じが「閉じだけ」と判定されて fail-safe (全文残し) に落ちる
    — 無効のペルソナにスペル前提の文章が残る、いちばん危ない失敗の形。
    """
    template = "\ufeff{if_spell_enabled}\nスペルの説明\n{end_if_spell_enabled}\n後置き"

    with caplog.at_level(logging.WARNING):
        disabled = apply_spell_markers(template, False)
    assert disabled == "後置き"
    assert not any("unbalanced" in rec.message for rec in caplog.records)

    assert apply_spell_markers(template, True) == "スペルの説明\n後置き"


def test_a_bom_on_a_body_line_is_left_untouched():
    """本文行の BOM は 1 文字も削らない (落とすのはマーカー行の判定だけ)。"""
    template = "\ufeff前置き\n{if_spell_enabled}\nスペルの説明\n{end_if_spell_enabled}"
    assert apply_spell_markers(template, False) == "\ufeff前置き"


# ---------------------------------------------------------------------------
# 2. builtin common.txt を実際に読んで両モードの展開結果を検査
# ---------------------------------------------------------------------------


def _builtin_common_txt() -> str:
    from saiverse.data_paths import BUILTIN_DATA_DIR, PROMPTS_DIR

    path = BUILTIN_DATA_DIR / PROMPTS_DIR / "common.txt"
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "forbidden",
    ["スペル", "/spell", "Playbook", "アイテム", "saiverse://", "building_move"],
)
def test_disabled_common_prompt_has_no_spell_wording(forbidden):
    out = apply_spell_markers(_builtin_common_txt(), False)
    assert forbidden not in out


@pytest.mark.parametrize(
    "kept", ["パルスシステム", "ふと浮かんだ記憶", "でっち上げ"],
)
def test_disabled_common_prompt_keeps_the_spell_free_truths(kept):
    """スペルが無くても真である文章は無効側にも残る。"""
    out = apply_spell_markers(_builtin_common_txt(), False)
    assert kept in out


def test_enabled_common_prompt_has_no_leftover_markers():
    out = apply_spell_markers(_builtin_common_txt(), True)
    for marker in (
        "{if_spell_enabled}", "{end_if_spell_enabled}",
        "{if_spell_disabled}", "{end_if_spell_disabled}",
    ):
        assert marker not in out
    # 有効側は現状どおり全部載る
    assert "## 能力（スペルとPlaybook）" in out
    assert "## SAIVerse URI" in out
    assert "## アイテムシステム" in out
    assert "建物間の移動は `building_move` Playbookで行います。" in out
    assert "ふと浮かんだ記憶" in out


def test_enabled_common_prompt_is_the_template_minus_markers_and_disabled_blocks():
    """有効側 1 バイト不変の恒常化 (docs/intent/spell_disabled_mode.md §4-2 / §7-1)。

    有効側の展開結果は「テンプレートからマーカー行と無効側ブロックを機械的に
    取り除いたもの」と厳密に一致する。この等式が成り立つ限り、common.txt への
    変更が**マーカー行と無効側ブロックの挿入だけ**であれば、スペル有効の
    ペルソナのプロンプトは 1 バイトも変わらない (= キャッシュが切れない)。
    """
    raw = _builtin_common_txt()
    expected: list[str] = []
    in_disabled_block = False
    for line in raw.split("\n"):
        stripped = line.strip()
        if stripped == "{if_spell_disabled}":
            in_disabled_block = True
            continue
        if stripped == "{end_if_spell_disabled}":
            in_disabled_block = False
            continue
        if in_disabled_block:
            continue
        if stripped in ("{if_spell_enabled}", "{end_if_spell_enabled}"):
            continue
        expected.append(line)
    assert apply_spell_markers(raw, True) == "\n".join(expected)


def test_disabled_common_prompt_keeps_the_memory_heading_and_replacement():
    """「## 記憶」の見出しは共通。無効側ブロックの導入文が入れ替わりで載る。"""
    out = apply_spell_markers(_builtin_common_txt(), False)
    assert "## 記憶" in out
    assert "あなたを形づくるのは、あなたの過去すべての体験です。" in out
    assert "思い出せないときは、思い出せないと正直に伝えてください。" in out


# ---------------------------------------------------------------------------
# 3. CommonPromptSection.capture が gate を capture 時に焼き込む
# ---------------------------------------------------------------------------


def _persona_with_template(template: str) -> SimpleNamespace:
    return SimpleNamespace(
        common_prompt=template,
        persona_name="エア", persona_id="air", current_city_id="city_a",
        persona_system_instruction="", linked_user_name="まはー", buildings={},
    )


def test_common_prompt_capture_applies_the_gate(ctx, isolated_manager):
    section = CommonPromptSection()
    ctx.persona = _persona_with_template(
        "こんにちは {current_persona_name}\n"
        "{if_spell_enabled}\nスペルが使えます\n{end_if_spell_enabled}\n"
        "またね"
    )

    snapshot = section.capture(ctx)
    assert snapshot.text == "こんにちは エア\nスペルが使えます\nまたね"

    isolated_manager.set_spell_enabled(False)
    snapshot = section.capture(ctx)
    assert snapshot.text == "こんにちは エア\nまたね"


def test_common_prompt_markers_are_resolved_before_placeholders(ctx):
    """差し込まれるペルソナの文章がマーカーとして働かないこと。

    順序が逆だと、システムプロンプトに ``{if_spell_enabled}`` と書いた利用者が
    common.txt の条件分岐を乗っ取れてしまう。
    """
    section = CommonPromptSection()
    persona = _persona_with_template(
        "{current_persona_system_instruction}\n本文"
    )
    persona.persona_system_instruction = "{if_spell_enabled}"
    ctx.persona = persona

    snapshot = section.capture(ctx)
    assert snapshot.text == "{if_spell_enabled}\n本文"


# ---------------------------------------------------------------------------
# 4. AvailablePlaybooksSection
# ---------------------------------------------------------------------------


def test_available_playbooks_capture_skips_the_tool_when_spell_disabled(
    ctx, isolated_manager, monkeypatch,
):
    import tools as tools_module

    def _boom(**kwargs):
        raise AssertionError("list_available_playbooks must not be called")

    monkeypatch.setitem(
        tools_module.TOOL_REGISTRY, "list_available_playbooks", _boom,
    )
    isolated_manager.set_spell_enabled(False)

    snapshot = AvailablePlaybooksSection().capture(ctx)
    assert snapshot.entries == ()
    assert snapshot.spell_enabled is False
    assert AvailablePlaybooksSection().render(snapshot) is None


def test_available_playbooks_capture_reads_the_tool_when_spell_enabled(
    ctx, monkeypatch,
):
    import tools as tools_module

    monkeypatch.setitem(
        tools_module.TOOL_REGISTRY, "list_available_playbooks",
        lambda **kwargs: json.dumps([{"name": "pb_a", "description": "説明"}]),
    )

    snapshot = AvailablePlaybooksSection().capture(ctx)
    assert snapshot.spell_enabled is True
    assert [e.name for e in snapshot.entries] == ["pb_a"]


def test_available_playbooks_gate_flip_emits_no_notifications():
    """機構ごとの有効/無効は SpellListSection のラベルが一手に担う (§4-6)。"""
    section = AvailablePlaybooksSection()
    old = AvailablePlaybooksSnapshot(
        entries=(PlaybookEntry(name="pb_a", description=""),), spell_enabled=True,
    )
    new = AvailablePlaybooksSnapshot(entries=(), spell_enabled=False)
    assert section.diff_to_notifications(old, new) == []
    assert section.diff_to_notifications(new, old) == []


def test_available_playbooks_ordinary_diff_still_works():
    section = AvailablePlaybooksSection()
    old = AvailablePlaybooksSnapshot(entries=())
    new = AvailablePlaybooksSnapshot(
        entries=(PlaybookEntry(name="pb_a", description=""),),
    )
    labels = section.diff_to_notifications(old, new)
    assert [label.kind for label in labels] == ["playbook_added"]


def test_available_playbooks_old_payload_is_enabled():
    section = AvailablePlaybooksSection()
    stored = json.dumps({"entries": [{"name": "pb_a", "description": ""}]})
    restored = section.deserialize_snapshot(stored)
    assert restored.spell_enabled is True
    assert section.render(restored) is not None


def test_available_playbooks_roundtrip_keeps_the_flag():
    section = AvailablePlaybooksSection()
    snap = AvailablePlaybooksSnapshot(entries=(), spell_enabled=False)
    assert section.deserialize_snapshot(section.serialize_snapshot(snap)) == snap


# ---------------------------------------------------------------------------
# 4-b. get_system_prompt ツール (head pipeline を通らない、もう一本のプロンプト経路)
# ---------------------------------------------------------------------------


def _tool_persona() -> SimpleNamespace:
    return SimpleNamespace(
        persona_id="air", persona_name="エア",
        persona_system_instruction="本文", common_prompt=None,
        current_building_id="b_lobby", current_city_id="city_a",
        buildings={}, linked_user_name="まはー",
    )


def _build_system_prompt(manager, **kwargs) -> str:
    from builtin_data.tools.get_system_prompt import get_system_prompt
    from tools.context import persona_context

    manager.all_personas = {"air": _tool_persona()}
    with persona_context("air", Path("."), manager=manager):
        return get_system_prompt(**kwargs)


def test_get_system_prompt_skips_the_playbook_list_when_spell_disabled(
    isolated_manager, monkeypatch,
):
    """一覧を取りに行くことすらしない。

    Playbook は ``run_playbook`` スペルからしか実行できないので、無効の
    ペルソナに名前を見せても「呼べない手札」になる。
    """
    import tools as tools_module

    calls: list = []
    monkeypatch.setitem(
        tools_module.TOOL_REGISTRY, "list_available_playbooks",
        lambda **kwargs: calls.append(kwargs) or json.dumps(
            [{"name": "pb_a", "description": "説明"}]
        ),
    )
    isolated_manager.set_spell_enabled(False)

    out = _build_system_prompt(isolated_manager, include_available_playbooks=True)
    assert calls == []
    assert "利用可能なPlaybook" not in out
    assert "run_playbook" not in out
    assert "pb_a" not in out


def test_get_system_prompt_lists_the_playbooks_when_spell_enabled(
    isolated_manager, monkeypatch,
):
    import tools as tools_module

    monkeypatch.setitem(
        tools_module.TOOL_REGISTRY, "list_available_playbooks",
        lambda **kwargs: json.dumps([{"name": "pb_a", "description": "説明"}]),
    )

    out = _build_system_prompt(isolated_manager, include_available_playbooks=True)
    assert "## 利用可能なPlaybook" in out
    assert "**pb_a**: 説明" in out


# ---------------------------------------------------------------------------
# 5. AutonomyModesSection
# ---------------------------------------------------------------------------


def test_autonomy_modes_falls_back_to_the_speech_only_text(ctx, isolated_manager):
    """無効時は「## モード」の代わりに、発言の届き先だけを述べた短い定数。"""
    isolated_manager.set_spell_enabled(False)
    section = AutonomyModesSection()
    snapshot = section.capture(ctx)
    assert snapshot.spell_enabled is False

    rendered = section.render(snapshot)
    assert rendered is not None
    assert rendered.text == _SPEECH_ONLY_TEXT
    assert rendered.text.startswith("## 発言の扱い")
    # モードの語彙も分身モードも出てこない
    assert "## モード" not in rendered.text
    assert "分身" not in rendered.text
    assert "run_playbook" not in rendered.text
    assert "スペル" not in rendered.text
    # 発言の届き先という、スペルと無関係な事実は残る
    assert "ユーザーの見るUIに表示されます" in rendered.text
    assert "同一Buildingにいる他のペルソナにも発言内容が知覚されます" in rendered.text


def test_autonomy_modes_renders_when_spell_enabled(ctx):
    section = AutonomyModesSection()
    snapshot = section.capture(ctx)
    rendered = section.render(snapshot)
    assert rendered is not None
    assert rendered.text == _AUTONOMY_MODES_TEXT


def test_autonomy_modes_old_payload_is_enabled():
    """欄を持たない旧 payload は有効扱い (文言はコード側の現在値)。"""
    section = AutonomyModesSection()
    restored = section.deserialize_snapshot(json.dumps({"text": "旧い文言"}))
    assert restored.spell_enabled is True
    assert restored.text == _AUTONOMY_MODES_TEXT
    assert section.render(restored) is not None


def test_autonomy_modes_roundtrip_keeps_the_flag(ctx, isolated_manager):
    isolated_manager.set_spell_enabled(False)
    section = AutonomyModesSection()
    snap = section.capture(ctx)
    restored = section.deserialize_snapshot(section.serialize_snapshot(snap))
    assert restored.spell_enabled is False
    assert section.render(restored).text == _SPEECH_ONLY_TEXT


# ---------------------------------------------------------------------------
# 6. MemopediaIndexSection
# ---------------------------------------------------------------------------


def _toc_snapshot(spell_enabled: bool) -> MemopediaIndexSnapshot:
    return MemopediaIndexSnapshot(
        captured_at=0.0, pages=(), index_enabled=True,
        toc_markdown="\n### 人物（1 ページ）\n- まはー [memopedia:3]: 相棒",
        spell_enabled=spell_enabled,
    )


def test_memopedia_index_drops_only_the_memory_read_sentence():
    section = MemopediaIndexSection()
    enabled = section.render(_toc_snapshot(True)).text
    disabled = section.render(_toc_snapshot(False)).text

    assert "memory_read" in enabled
    assert "memory_read" not in disabled
    # 目次そのもの (自動想起の手掛かり) は残る
    assert "## 記憶の目次（Memopedia Index）" in disabled
    assert "まはー [memopedia:3]: 相棒" in disabled
    assert "[OPEN] は机に開いているページ、★ は重要ページです。" in disabled


def test_memopedia_index_old_payload_is_enabled():
    section = MemopediaIndexSection()
    stored = json.dumps({
        "captured_at": 1.0, "pages": [],
        "index_enabled": True, "toc_markdown": "\n- ページ",
    })
    restored = section.deserialize_snapshot(stored)
    assert restored.spell_enabled is True
    assert "memory_read" in section.render(restored).text


def test_memopedia_index_roundtrip_keeps_the_flag():
    section = MemopediaIndexSection()
    snap = _toc_snapshot(False)
    assert section.deserialize_snapshot(section.serialize_snapshot(snap)) == snap


# ---------------------------------------------------------------------------
# 6-b. DeskSection (机は UI からも開けるので、ページは残して案内文だけ差し替える)
# ---------------------------------------------------------------------------


def _desk_snapshot(spell_enabled: bool) -> DeskSnapshot:
    return DeskSnapshot(
        pages=(DeskPageItem(ref="memopedia:3", text="### まはー\n相棒。"),),
        spell_enabled=spell_enabled,
    )


def test_desk_keeps_the_pages_and_drops_only_the_open_close_guidance():
    section = DeskSection()
    enabled = section.render(_desk_snapshot(True)).text
    disabled = section.render(_desk_snapshot(False)).text

    assert "memory_open" in enabled and "memory_close" in enabled
    assert "memory_open" not in disabled and "memory_close" not in disabled
    # 見出しとページ本文はそのまま残る (中身はペルソナの記憶 — 没収しない)
    for text in (enabled, disabled):
        assert "## 机に開いているページ" in text
        assert "### まはー\n相棒。" in text
    # 「溢れたら自動で棚へ戻る」はスペルと無関係なので無効側にも残る
    assert "長く触っていないページから自動的に棚へ戻ります。" in disabled


def test_desk_old_payload_is_enabled():
    section = DeskSection()
    stored = json.dumps({"pages": [{"ref": "memopedia:3", "text": "本文"}]})
    restored = section.deserialize_snapshot(stored)
    assert restored.spell_enabled is True
    assert "memory_open" in section.render(restored).text


def test_desk_roundtrip_keeps_the_flag():
    section = DeskSection()
    snap = _desk_snapshot(False)
    assert section.deserialize_snapshot(section.serialize_snapshot(snap)) == snap


# ---------------------------------------------------------------------------
# 6-c. CommonPromptSection.diff — 比べるのは展開済みテキストだけ
# ---------------------------------------------------------------------------


def test_common_prompt_diff_ignores_a_fingerprint_only_change():
    """文章が同一バイトなら、テンプレートのファイルが変わっても通知しない。

    マーカー行を挿入するリリースがまさにこの形 — snapshot 全体で比べていると、
    有効ペルソナ全員に「前提情報が更新されました」が飛ぶ。
    """
    from sea.head_pipeline.sections.common_prompt import CommonPromptSnapshot

    section = CommonPromptSection()
    old = CommonPromptSnapshot(text="同じ本文", template_fingerprint="a" * 64)
    new = CommonPromptSnapshot(text="同じ本文", template_fingerprint="b" * 64)
    assert section.diff_to_notifications(old, new) == []


def test_common_prompt_diff_still_reports_a_real_text_change():
    from sea.head_pipeline.sections.common_prompt import CommonPromptSnapshot

    section = CommonPromptSection()
    old = CommonPromptSnapshot(text="前の本文", template_fingerprint="a" * 64)
    new = CommonPromptSnapshot(text="後の本文", template_fingerprint="a" * 64)
    labels = section.diff_to_notifications(old, new)
    assert [label.kind for label in labels] == ["common_prompt_changed"]


# ---------------------------------------------------------------------------
# 7. 共有ヘルパー本体
# ---------------------------------------------------------------------------


def test_resolve_spell_enabled_reads_the_db(ctx, isolated_manager):
    assert resolve_spell_enabled(ctx) is True
    isolated_manager.set_spell_enabled(False)
    assert resolve_spell_enabled(ctx) is False


def test_resolve_spell_enabled_falls_back_to_false_without_a_manager():
    assert resolve_spell_enabled(None) is False
    assert resolve_spell_enabled(
        LineHeadInput(persona_id="air", model_key="m")
    ) is False


def test_resolve_spell_enabled_falls_back_to_false_when_the_session_cannot_be_made(
    caplog,
):
    """セッションを作るところで転んでも例外は漏らさない。

    この関数は ``get_system_prompt`` が頭で呼ぶので、例外を漏らすとプロンプトの
    取得そのものが新たに落ちる — 「DB を引けないときは False に倒す」という
    約束には、セッションが作れない場合も含まれる (敵対レビュー 2026-09-14)。
    """
    def _boom():
        raise RuntimeError("database is gone")

    manager = SimpleNamespace(SessionLocal=_boom)

    with caplog.at_level(logging.WARNING):
        assert resolve_spell_enabled_for(manager, "air") is False
    assert any("SPELL_ENABLED" in rec.message for rec in caplog.records)

    ctx = LineHeadInput(persona_id="air", model_key="m", manager=manager)
    assert resolve_spell_enabled(ctx) is False


# ---------------------------------------------------------------------------
# 8. pre_spells の実行 gate (tests/test_spell_auto_mode_w10.py と同じ流儀)
# ---------------------------------------------------------------------------


class _Boom(Exception):
    """node 実行の status イベントで node を止める番兵。"""


def _run_llm_node(state: dict, monkeypatch) -> tuple[list, list]:
    """lg_llm_node を「ノード実行の status」まで走らせ、呼び出しとイベントを返す。"""
    calls: list = []
    events: list = []

    async def _recorder(*args, **kwargs):
        calls.append(args)

    monkeypatch.setattr(runtime_llm, "_execute_pre_spells", _recorder)
    monkeypatch.setattr(runtime_llm, "_execute_realtime_spells", AsyncMock())

    def _callback(event):
        events.append(event)
        if event.get("node") == "llm":   # ノード実行の status = ここで止める
            raise _Boom()

    # persona_id=None で node_with_persona_context の wrap を素通しする。
    persona = SimpleNamespace(persona_id=None, persona_name="p")
    playbook = SimpleNamespace(name="pb")
    node_def = SimpleNamespace(id="llm")
    node = runtime_llm.lg_llm_node(
        MagicMock(), node_def, persona, "b1", playbook, _callback,
    )

    with pytest.raises(_Boom):
        asyncio.run(node(state))
    return calls, events


def test_pre_spells_are_skipped_when_spell_disabled(monkeypatch):
    state = {
        "_spell_enabled": False,
        "_pre_spells": ["/spell name='memory_search' args={'q': 'x'}"],
        "_messages": [],
    }
    calls, events = _run_llm_node(state, monkeypatch)

    assert calls == []
    assert state["_pre_spells_executed"] is True
    # 黙って握り潰さない — ユーザーにスキップを見せる
    assert any(
        e.get("type") == "status"
        and e.get("content") == "スペル不使用モードのため、指定されたツールの実行をスキップしました"
        for e in events
    )


def test_pre_spells_run_when_spell_enabled(monkeypatch):
    state = {
        "_spell_enabled": True,
        "_pre_spells": ["/spell name='memory_search' args={'q': 'x'}"],
        "_messages": [],
    }
    calls, events = _run_llm_node(state, monkeypatch)

    assert len(calls) == 1
    assert state["_pre_spells_executed"] is True
    assert not any(
        e.get("content") == "スペル不使用モードのため、指定されたツールの実行をスキップしました"
        for e in events
    )


# ---------------------------------------------------------------------------
# 9. 切り替えイベント (SPELL_TOGGLED) の宛先 — gate を持つ section が一斉に撮り直される
# ---------------------------------------------------------------------------

#: 切り替えで撮り直す section (docs/intent/spell_disabled_mode.md §4-2 / §4-5)。
_GATED_SECTIONS = {
    "common_prompt",
    "available_playbooks",
    "autonomy_modes",
    "spell_list",
    "desk",
    "memopedia_index",
}


def _default_registry():
    from sea.head_pipeline.registry import HeadSectionRegistry
    from sea.head_pipeline.sections import register_default_sections

    registry = HeadSectionRegistry()
    register_default_sections(registry)
    return registry


def test_spell_toggled_refreshes_exactly_the_gated_sections():
    """切り替えの宛先は gate を持つ 6 つだけ (多くても少なくてもいけない)。

    足りなければ「一部だけ新しいモードの中途半端なプロンプト先頭部」ができ、
    余計に足すと、スペルと関係ない section まで切り替えのたびに撮り直される。
    """
    from sea.head_pipeline.types import EventType

    registry = _default_registry()
    names = {
        s.name for s in registry.sections_for_event(EventType.SPELL_TOGGLED)
    }
    assert names == _GATED_SECTIONS


def test_spell_toggled_is_added_to_the_existing_refresh_declarations():
    """既存の宣言は残したまま (和集合) — アドオン着脱・プロンプト編集も効き続ける。"""
    from sea.head_pipeline.types import EventType

    registry = _default_registry()
    by_name = {s.name: s for s in registry.all_sections()}
    assert by_name["spell_list"].refresh_on_events == frozenset({
        EventType.ADDON_LOADED,
        EventType.ADDON_UNLOADED,
        EventType.SPELL_TOGGLED,
    })
    assert by_name["available_playbooks"].refresh_on_events == frozenset({
        EventType.ADDON_LOADED,
        EventType.ADDON_UNLOADED,
        EventType.SPELL_TOGGLED,
    })
    assert by_name["common_prompt"].refresh_on_events == frozenset({
        EventType.SYSTEM_PROMPT_EDITED,
        EventType.SPELL_TOGGLED,
    })


def test_sections_without_a_spell_gate_are_not_refreshed_by_the_toggle():
    """gate を持たない section は切り替えで撮り直さない (キャッシュを無駄に捨てない)。"""
    from sea.head_pipeline.types import EventType

    registry = _default_registry()
    by_name = {s.name: s for s in registry.all_sections()}
    for name in ("persona_self", "core_memory", "building", "facilities",
                 "self_image", "memory_weave", "building_occupants",
                 "chronicle_index"):
        assert EventType.SPELL_TOGGLED not in by_name[name].refresh_on_events, name


# ---------------------------------------------------------------------------
# 10. 保存した時点で発火する (値が変わった保存だけ)
# ---------------------------------------------------------------------------

_ADMIN_AI_ID = "air_city_a"


@pytest.fixture
def admin_with_persona(monkeypatch):
    """`AdminService.update_ai` を、切り替えの発火だけ見える形で組んだもの。

    組み方は tests/test_admin_ai_edit_contract.py の ``_make_admin`` と同じ流儀
    (``__new__`` で update_ai が実際に読む属性だけを注入する)。違いは、発火には
    インメモリのペルソナが要るので ``personas`` を埋めることと、その場合に走る
    モデルの当てはめを差し替えること。

    ``dispatched`` に ``on_spell_toggled`` の呼び出し引数が積まれる。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from database.models import AI, Base, City, User
    from manager.admin import AdminService
    from saiverse import persona_model_selection
    from saiverse.dynamic_state import DynamicStateManager

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    db = SessionLocal()
    try:
        db.add(User(USERID=1, PASSWORD="x", USERNAME="u"))
        db.flush()
        db.add(City(CITYID=1, USERID=1, CITY_SLUG="city_a", UI_PORT=3000, API_PORT=8000))
        db.add(AI(AIID=_ADMIN_AI_ID, HOME_CITYID=1, AINAME="Air", SPELL_ENABLED=True))
        db.commit()
    finally:
        db.close()

    persona = SimpleNamespace(
        persona_id=_ADMIN_AI_ID, persona_name="Air",
        current_building_id="b_lobby",
    )
    manager = SimpleNamespace(SessionLocal=SessionLocal)

    admin = AdminService.__new__(AdminService)
    admin.SessionLocal = SessionLocal
    admin.manager = manager
    admin.personas = {_ADMIN_AI_ID: persona}
    admin.building_map = {}
    admin.state = SimpleNamespace(model=None, city_id=1)
    admin._set_persona_avatar = lambda ai_id, value: None

    monkeypatch.setattr(
        persona_model_selection, "reapply_speaking_models",
        lambda *args, **kwargs: SimpleNamespace(notices=lambda: []),
    )

    dispatched: list = []
    monkeypatch.setattr(
        DynamicStateManager, "on_spell_toggled",
        lambda p, m: dispatched.append((p, m)) or True,
    )

    admin.dispatched = dispatched
    admin.persona = persona
    admin.manager_obj = manager
    try:
        yield admin
    finally:
        engine.dispose()


def _update_ai(admin, **overrides):
    kwargs = dict(
        name="Air", description="desc", system_prompt="prompt", home_city_id=1,
        default_model=None, lightweight_model=None, autonomy_enabled=True,
        avatar_path=None, avatar_upload=None,
    )
    kwargs.update(overrides)
    return admin.update_ai(_ADMIN_AI_ID, **kwargs)


def test_saving_a_changed_spell_mode_dispatches_once(admin_with_persona):
    result = _update_ai(admin_with_persona, spell_enabled=False)

    assert "Error" not in result
    assert len(admin_with_persona.dispatched) == 1
    persona, manager = admin_with_persona.dispatched[0]
    assert persona is admin_with_persona.persona
    assert manager is admin_with_persona.manager_obj


def test_saving_the_same_spell_mode_does_not_dispatch(admin_with_persona):
    """値が変わらない保存では発火しない (キャッシュを無意味に捨てない)。"""
    _update_ai(admin_with_persona, spell_enabled=True)   # 既定と同じ値
    assert admin_with_persona.dispatched == []


def test_saving_other_settings_does_not_dispatch(admin_with_persona):
    """spell_enabled を渡さない保存 (ワールドエディタ経路) では発火しない。"""
    _update_ai(admin_with_persona, name="Air2", realtime_info_enabled=False)
    assert admin_with_persona.dispatched == []


def test_toggling_back_dispatches_again(admin_with_persona):
    _update_ai(admin_with_persona, spell_enabled=False)
    _update_ai(admin_with_persona, spell_enabled=False)   # 二度目は同じ値
    _update_ai(admin_with_persona, spell_enabled=True)

    assert len(admin_with_persona.dispatched) == 2


def test_dispatch_failure_does_not_fail_the_save(admin_with_persona, monkeypatch):
    """届かなくても保存は成功する (反映は次の記憶整理まで待つ)。"""
    from database.models import AI
    from saiverse.dynamic_state import DynamicStateManager

    def _boom(persona, manager):
        raise RuntimeError("head pipeline is down")

    monkeypatch.setattr(DynamicStateManager, "on_spell_toggled", _boom)

    result = _update_ai(admin_with_persona, spell_enabled=False)
    assert "Error" not in result

    session = admin_with_persona.SessionLocal()
    try:
        row = session.query(AI).filter_by(AIID=_ADMIN_AI_ID).first()
        assert bool(row.SPELL_ENABLED) is False
    finally:
        session.close()


def test_dispatch_follows_the_model_reapply(admin_with_persona, monkeypatch):
    """同じ保存でモデルも変えたら、当てはめが済んだ**後**に発火する。

    発火の宛先はその時点の persona のモデルから決まる (on_spell_toggled は
    model_key を渡さない)。当てはめより前に発火すると、旧モデルの head を撮り直して
    新モデルの head には旧モードの説明が残る (敵対レビュー 2026-09-14 二巡目)。
    """
    from saiverse import model_defaults, persona_model_selection
    from saiverse.dynamic_state import DynamicStateManager

    order: list = []

    monkeypatch.setattr(
        model_defaults, "role_model_is_defined", lambda role, value: True,
    )
    monkeypatch.setattr(
        persona_model_selection, "reapply_speaking_models",
        lambda *args, **kwargs: order.append("reapply") or SimpleNamespace(
            notices=lambda: []
        ),
    )
    monkeypatch.setattr(
        DynamicStateManager, "on_spell_toggled",
        lambda p, m: order.append("dispatch") or True,
    )

    result = _update_ai(
        admin_with_persona, spell_enabled=False, default_model="claude-opus-4-7",
    )

    assert "Error" not in result
    assert order == ["reapply", "dispatch"]


def test_dispatch_still_happens_when_the_model_reapply_explodes(
    admin_with_persona, monkeypatch,
):
    """当てはめが例外を投げた回でも、切り替えは head へ届く。

    commit 済みなので DB のモードはもう変わっている。ここで発火を落とすと、同じ値を
    保存し直しても「値が変わっていない」ので再発火せず、次の記憶整理まで旧いままに
    なる (敵対レビュー 2026-09-14)。
    """
    from database.models import AI
    from saiverse import persona_model_selection

    def _boom(*args, **kwargs):
        raise RuntimeError("model reapply exploded")

    monkeypatch.setattr(
        persona_model_selection, "reapply_speaking_models", _boom,
    )

    # update_ai 自体は例外を掴んで Error を返す — 見るのは発火が済んでいること。
    result = _update_ai(admin_with_persona, spell_enabled=False)
    assert "Error" in result
    assert len(admin_with_persona.dispatched) == 1

    session = admin_with_persona.SessionLocal()
    try:
        row = session.query(AI).filter_by(AIID=_ADMIN_AI_ID).first()
        assert bool(row.SPELL_ENABLED) is False
    finally:
        session.close()


def test_a_failure_after_the_dispatch_does_not_dispatch_twice(admin_with_persona):
    """発火の後で転んでも、例外経路の保険が二重に撮り直しを起こさない。"""
    def _boom(ai_id, value):
        raise RuntimeError("avatar write exploded")

    admin_with_persona._set_persona_avatar = _boom

    result = _update_ai(admin_with_persona, spell_enabled=False)
    assert "Error" in result
    assert len(admin_with_persona.dispatched) == 1


def test_no_dispatch_when_the_save_fails_before_the_commit(
    admin_with_persona, monkeypatch,
):
    """commit より前に転んだ回は発火しない (DB は巻き戻り、モードは変わっていない)。"""
    from sqlalchemy.orm import Session

    from database.models import AI

    def _boom(self):
        raise RuntimeError("commit exploded")

    monkeypatch.setattr(Session, "commit", _boom)

    result = _update_ai(admin_with_persona, spell_enabled=False)
    assert "Error" in result
    assert admin_with_persona.dispatched == []

    # 読むだけなので commit の差し替えは戻さなくてよい (close は rollback する)。
    session = admin_with_persona.SessionLocal()
    try:
        row = session.query(AI).filter_by(AIID=_ADMIN_AI_ID).first()
        assert bool(row.SPELL_ENABLED) is True
    finally:
        session.close()
