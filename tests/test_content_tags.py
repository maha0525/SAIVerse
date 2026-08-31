"""Tests for ``saiverse.content_tags``.

検証観点:
- ``strip_user_only`` が ``<user_only>...</user_only>`` ブロックを中身ごと削除する
- ``alt=...`` 属性付き / 複数ブロック / spell HTML 入りの実ケースで動く
- 空文字 / 該当タグなしテキストはそのまま (no-op)
- ``strip_in_heart`` は単体で動作確認 (既存挙動維持の回帰防止)

これらは voice/TTS 経路 (``emit_speak`` / ``emit_say`` の ``text_for_voice``
hook payload) でスペル HTML 詳細が読み上げられないための保証になる。
"""
from __future__ import annotations

import unittest

from saiverse.content_tags import (
    strip_in_heart,
    strip_user_only,
    wrap_spell_blocks,
)


class StripUserOnlyTests(unittest.TestCase):
    def test_removes_simple_user_only_block(self) -> None:
        text = "before<user_only>secret</user_only>after"
        self.assertEqual(strip_user_only(text), "beforeafter")

    def test_removes_user_only_with_alt_attribute(self) -> None:
        text = 'hello<user_only alt="メモリ想起">spell html</user_only>world'
        self.assertEqual(strip_user_only(text), "helloworld")

    def test_removes_multiple_blocks(self) -> None:
        text = (
            "head"
            '<user_only alt="A">aaa</user_only>'
            "mid"
            '<user_only alt="B">bbb</user_only>'
            "tail"
        )
        self.assertEqual(strip_user_only(text), "headmidtail")

    def test_removes_spell_block_wrapped_in_user_only(self) -> None:
        # wrap_spell_blocks の出力 (= emit_say で building_content にラップされる
        # 実形態) を strip_user_only に通したとき、スペル HTML 詳細が完全に
        # 消えることを確認する。これが今回の修正の本丸。
        spell_html = (
            '<details class="spellBlock">'
            '<summary class="spellSummary">'
            '<span class="spellIcon"><svg width="14" height="14"></svg></span>'
            "<span>メモリ想起</span>"
            "</summary>"
            '<div class="spellContent">'
            '<div class="spellParams"><code>{\'query\': \'foo\'}</code></div>'
            '<div class="spellResultLabel">Result:</div>'
            '<div class="spellResult">recalled content here</div>'
            "</div>"
            "</details>"
        )
        wrapped = wrap_spell_blocks("はじめに\n" + spell_html + "\n続き")
        # wrap_spell_blocks は <user_only alt="..."> でくるむのが期待挙動
        self.assertIn("<user_only", wrapped)
        self.assertIn("メモリ想起", wrapped)
        self.assertIn("recalled content here", wrapped)

        voice_text = strip_user_only(wrapped)
        self.assertNotIn("<user_only", voice_text)
        self.assertNotIn("<details", voice_text)
        self.assertNotIn("メモリ想起", voice_text)
        self.assertNotIn("recalled content here", voice_text)
        self.assertIn("はじめに", voice_text)
        self.assertIn("続き", voice_text)

    def test_no_user_only_tag_passthrough(self) -> None:
        text = "ただの普通のテキスト"
        self.assertEqual(strip_user_only(text), text)

    def test_empty_string_returns_empty(self) -> None:
        self.assertEqual(strip_user_only(""), "")

    def test_none_returns_falsy(self) -> None:
        # 防御的: None が渡っても落ちない
        self.assertFalse(strip_user_only(None))  # type: ignore[arg-type]

    def test_multiline_user_only_block(self) -> None:
        text = (
            "line1\n"
            "<user_only alt=\"X\">\n"
            "  multiline\n"
            "  content\n"
            "</user_only>\n"
            "line2"
        )
        result = strip_user_only(text)
        self.assertNotIn("multiline", result)
        self.assertIn("line1", result)
        self.assertIn("line2", result)


class StripInHeartTests(unittest.TestCase):
    """既存挙動維持確認 (text_for_voice 計算の前段に依存しているため)."""

    def test_removes_in_heart_block(self) -> None:
        text = "外側<in_heart>内側の独白</in_heart>外側続き"
        self.assertEqual(strip_in_heart(text), "外側外側続き")


if __name__ == "__main__":
    unittest.main()
