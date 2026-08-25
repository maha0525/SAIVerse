"""Tests for spell misfire feedback (unknown spell → error returned to persona).

Covers the two user-visible building blocks of the misfire path:

- ``_build_spell_user_only_block(success=False)`` renders the failure (×)
  variant with the ``spellResultError`` class so the frontend styles a
  misfired spell distinctly from a successful one.
- ``_build_unknown_spell_error`` produces a corrective hint: when the unknown
  spell name is actually a router_callable Playbook, it guides the persona to
  ``run_playbook``; otherwise it offers the *close* spell names only (listing
  every registered spell buried the correction and burned context — see the
  2026-08-25 Elyth key-deletion run).

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
# _build_unknown_spell_error: 候補は「近い名前」だけ — 全列挙はしない
#
# 2026-08-25、Elyth の API キーを消した直後にペルソナが消えたスペルを呼び、
# 登録済み 127 個が丸ごと並んだメッセージが返った。訂正すべき一点が埋もれる上、
# 取り違えのたびにペルソナの文脈をそれだけ食う。
# ---------------------------------------------------------------------------

# 実世界の縮図: アドオン名の長い共通接頭辞 + 同名ツールの重複
_REGISTERED = {
    "memory_write",
    "memory_read",
    "memory_delete",
    "document_search",
    "document_create",
    "saiverse-stackchan-addon__set_mouth",
    "saiverse-stackchan-addon__stackchan__set_mouth",
    "saiverse-stackchan-addon__stackchan__get_device_info",
    "saiverse-x-addon__x_post_tweet",
    "saiverse-x-addon__x_get_user",
}


def _unknown(spell_name: str) -> str:
    with patch("sea.runtime_llm.SPELL_TOOL_NAMES", _REGISTERED), \
            patch.dict("sea.runtime_llm.TOOL_REGISTRY", {}, clear=True):
        return _build_unknown_spell_error(spell_name, _persona(), "bld_1")


def test_typo_gets_the_intended_spell_as_a_candidate() -> None:
    msg = _unknown("memory_wirte")
    assert "memory_write" in msg
    # 無関係なアドオンのスペルは混ざらない
    assert "saiverse-stackchan-addon" not in msg


def test_vanished_spell_offers_no_bogus_candidate() -> None:
    """鍵を消して消滅した Elyth のスペル名 — 近い名前は本当に無い。

    ここで無理に候補を出すと、ペルソナは無関係なスペルへ誘導される。
    """
    msg = _unknown("saiverse-elyth-addon__elyth__get_information")
    assert "存在しません" in msg
    assert "名前が近いスペル" not in msg
    # 案内先だけは示す
    assert "addon_spell_help" in msg


def test_does_not_dump_the_whole_registry() -> None:
    msg = _unknown("saiverse-elyth-addon__elyth__create_post")
    for name in _REGISTERED:
        assert name not in msg


def test_bare_name_with_ambiguous_owner_lists_both_owners() -> None:
    """アドオン名を落とした呼び出しで、同名ツールが複数あるケース。

    候補が一意なら canonicalize_spell_name が救うので、ここまで落ちてくるのは
    曖昧な時だけ。どちらを指したいのか選べるよう、両方を出す。
    """
    msg = _unknown("set_mouth")
    assert "saiverse-stackchan-addon__set_mouth" in msg
    assert "saiverse-stackchan-addon__stackchan__set_mouth" in msg


def test_hint_points_at_where_the_full_list_lives() -> None:
    msg = _unknown("totally_made_up_name")
    assert "addon_spell_help" in msg


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
