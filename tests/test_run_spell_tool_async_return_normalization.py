"""Regression tests for ``_run_spell_tool_async`` return-value normalization.

native tool が返す各種戻り値型が ``_run_spell_tool_async`` で正しく
``(content, metadata)`` に展開されるかを検証する。

特に 4-tuple ``(text, ToolResult, file_path, metadata)`` (see.py 等の
multimodal tool で使われる形) が tuple repr 文字列化されずに content と
metadata に分離される regression を担保する。

詳細: docs/issues/native_tool_return_4tuple_bug.md
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from sea.runtime_llm import _run_spell_tool_async
from tools.core import ToolResult


def _make_state() -> dict:
    return {
        "_persona_obj": SimpleNamespace(
            persona_id="test_persona",
            persona_log_path=None,
            manager_ref=None,
        ),
        "_pulse_context": None,
    }


def _run(coro):
    # Use asyncio.run (fresh loop per call, closed cleanly) rather than
    # get_event_loop().run_until_complete: the latter depends on a "current"
    # event loop existing, which breaks under Python 3.10+ when an earlier
    # test in the same process closed the loop via asyncio.run (current loop
    # reset to None → "no current event loop"). asyncio.run has no such
    # cross-test ordering dependency.
    return asyncio.run(coro)


def _invoke(tool_func) -> tuple[str, dict | None]:
    """Helper: register tool_func in TOOL_REGISTRY and run the spell."""
    with patch.dict("sea.runtime_llm.TOOL_REGISTRY", {"_test_tool": tool_func}, clear=False):
        return _run(_run_spell_tool_async(
            tool_name="_test_tool",
            tool_args={},
            persona=None,
            state=_make_state(),
            playbook_name="test_playbook",
            event_callback=None,
        ))


def test_str_return() -> None:
    """str 戻り値は (str, None) になる。"""
    result_str, result_meta = _invoke(lambda: "plain text result")
    assert result_str == "plain text result"
    assert result_meta is None


def test_str_dict_tuple_return() -> None:
    """(str, dict) 戻り値は dict が metadata に乗る。"""
    media = [{"type": "image", "path": "/tmp/foo.jpg"}]
    result_str, result_meta = _invoke(lambda: ("画像を生成した", {"media": media}))
    assert result_str == "画像を生成した"
    assert result_meta == {"media": media}


def test_four_tuple_return_unpacks_correctly() -> None:
    """4-tuple (text, ToolResult, file_path, metadata) が正しく展開される。

    これが本 regression test のメイン。 旧実装では tuple 全体が str() 化
    されて ``"('text', ToolResult(...), '/path', {...})"`` のような repr
    文字列が LLM に渡っていた。
    """
    media = [{"type": "image", "path": "/saiverse/media/img.jpg"}]
    snippet = "![見えた光景](/saiverse/media/img.jpg)"

    def tool():
        return (
            "目の前の光景を見た。",
            ToolResult(history_snippet=snippet),
            "/saiverse/media/img.jpg",
            {"media": media},
        )

    result_str, result_meta = _invoke(tool)

    # content だけが str に乗る (tuple repr ではなく)
    assert result_str == "目の前の光景を見た。"
    assert "ToolResult" not in result_str
    assert "history_snippet" not in result_str

    # metadata は 4 番目の dict をそのまま受け取る
    assert result_meta == {"media": media}


def test_three_tuple_return_no_metadata() -> None:
    """3-tuple (text, ToolResult, file_path) は metadata=None になる。"""
    def tool():
        return ("結果", ToolResult(history_snippet="snip"), "/tmp/x.txt")

    result_str, result_meta = _invoke(tool)
    assert result_str == "結果"
    assert result_meta is None


def test_tool_result_only_return() -> None:
    """ToolResult 単体戻り値は content 空文字 + metadata None になる。"""
    result_str, result_meta = _invoke(lambda: ToolResult(history_snippet="snip only"))
    assert result_str == ""
    assert result_meta is None


def test_unknown_tool_returns_error_string() -> None:
    """TOOL_REGISTRY に存在しない tool は error string を返す。"""
    with patch.dict("sea.runtime_llm.TOOL_REGISTRY", {}, clear=True):
        result_str, result_meta = _run(_run_spell_tool_async(
            tool_name="_does_not_exist",
            tool_args={},
            persona=None,
            state=_make_state(),
            playbook_name="test_playbook",
            event_callback=None,
        ))
    assert "not found in registry" in result_str
    assert result_meta is None


def test_tool_exception_returns_error_string() -> None:
    """tool が raise した時は error string を返し、 metadata=None。"""
    def tool():
        raise RuntimeError("boom")

    result_str, result_meta = _invoke(tool)
    assert "Spell error" in result_str
    assert "RuntimeError" in result_str
    assert "boom" in result_str
    assert result_meta is None
