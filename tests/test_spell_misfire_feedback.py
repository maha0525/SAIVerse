"""Tests for spell misfire feedback (unknown spell → error returned to persona).

Covers the two user-visible building blocks of the misfire path:

- ``_build_spell_user_only_block(success=False)`` renders the failure (×)
  variant with the ``spellResultError`` class so the frontend styles a
  misfired spell distinctly from a successful one.
- ``_build_unknown_spell_error`` produces a corrective hint: when the unknown
  spell name is actually a router_callable Playbook, it guides the persona to
  ``run_playbook``; otherwise it lists the registered spells.

詳細: docs/issues/spell_misfire_user_feedback.md
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from sea.runtime_llm import (
    _build_malformed_args_error,
    _build_spell_user_only_block,
    _build_unknown_spell_error,
)


def _persona() -> SimpleNamespace:
    return SimpleNamespace(persona_id="test_persona")


# ---------------------------------------------------------------------------
# _build_spell_user_only_block: success (star) vs failure (×)
# ---------------------------------------------------------------------------

def test_success_block_uses_star_icon() -> None:
    block = _build_spell_user_only_block(
        "calculator", {"expression": "1+1"}, "電卓", "2", success=True
    )
    assert 'class="spellResult"' in block
    assert "spellResultError" not in block
    # star path
    assert "M12 2L15.09" in block


def test_success_block_isolates_fenced_markdown_from_disclosure_tags() -> None:
    result = "```\n<system>notice</system>\n```"
    block = _build_spell_user_only_block(
        "messagelog_get_around", {}, "特定時刻のログ取得", result,
        success=True,
    )

    # A fenced result must start and end on lines independent from the raw
    # HTML wrapper.  Otherwise Markdown reads ```</details> as a new opening
    # fence whose info string is </details>, swallowing the continuation into
    # the disclosure (production regression: aifi room, 2026-07-22 00:49:32).
    assert "</summary>\n\n```\n" in block
    assert "\n```\n\n</details>\n</user_only>\n" in block
    assert "```</details>" not in block


def test_failure_block_uses_cross_icon_and_error_class() -> None:
    block = _build_spell_user_only_block(
        "web_research", {"query": "x"}, "web_research",
        "「web_research」は Playbook です", success=False,
    )
    assert "spellResultError" in block
    # cross (×) path, not the star
    assert "M18 6L6 18" in block
    assert "M12 2L15.09" not in block
    # the error/hint text is shown to the user
    assert "Playbook" in block


def test_failure_block_renders_even_with_empty_result() -> None:
    # Failure blocks must always render the disclosure (the user needs to see
    # that something misfired), unlike success blocks which collapse when empty.
    block = _build_spell_user_only_block(
        "bogus", {}, "bogus", "", success=False
    )
    assert "spellResultError" in block


# ---------------------------------------------------------------------------
# _build_unknown_spell_error: playbook-name guidance vs spell listing
# ---------------------------------------------------------------------------

def _fake_list_playbooks(names: list[str]):
    import json

    def _func(persona_id=None, building_id=None):
        return json.dumps([{"name": n, "description": ""} for n in names])

    return _func


def test_unknown_name_matching_playbook_guides_to_run_playbook() -> None:
    fake = _fake_list_playbooks(["web_research", "memory_recall"])
    with patch.dict(
        "sea.runtime_llm.TOOL_REGISTRY",
        {"list_available_playbooks": fake},
        clear=False,
    ):
        msg = _build_unknown_spell_error("web_research", _persona(), "bld_1")
    assert "run_playbook" in msg
    assert "web_research" in msg
    # the canonical corrective invocation form is shown
    assert "name='run_playbook'" in msg


def test_unknown_name_not_a_playbook_lists_spells() -> None:
    fake = _fake_list_playbooks(["web_research"])
    with patch.dict(
        "sea.runtime_llm.TOOL_REGISTRY",
        {"list_available_playbooks": fake},
        clear=False,
    ):
        msg = _build_unknown_spell_error("totally_made_up", _persona(), "bld_1")
    assert "存在しません" in msg
    # not mistaken for a playbook
    assert "totally_made_up" in msg


def test_unknown_spell_error_degrades_without_playbook_tool() -> None:
    # list_available_playbooks missing → still returns a usable message.
    with patch.dict(
        "sea.runtime_llm.TOOL_REGISTRY", {}, clear=True
    ):
        msg = _build_unknown_spell_error("web_research", _persona(), "bld_1")
    assert "存在しません" in msg


# ---------------------------------------------------------------------------
# _build_malformed_args_error: args parse 失敗の差し戻し文面
# (2026-07-05 実 LLM シム 異常 #2 の回帰防止)
# ---------------------------------------------------------------------------

def test_malformed_args_error_contains_hint_and_preview() -> None:
    msg = _build_malformed_args_error(
        "document_create", '{"content": "急性膵炎に関する基本事項'
    )
    # 何が起きたか (実行されていないこと) が明示される
    assert "実行されませんでした" in msg
    # どの発動のことか分かる (スペル名 + 受け取った args の先頭)
    assert "document_create" in msg
    assert "急性膵炎" in msg
    # 正しい書式 (改行のエスケープ) への誘導と再試行の指示
    assert "\\n" in msg
    assert "もう一度" in msg


def test_malformed_args_error_truncates_long_preview() -> None:
    long_args = '{"content": "' + "あ" * 500
    msg = _build_malformed_args_error("document_create", long_args)
    assert "あ" * 500 not in msg  # 全文は貼らない (先頭部分のみ)
    assert "…" in msg
