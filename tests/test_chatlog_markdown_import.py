"""Regression tests for Markdown chatlog import (Chrome extension exports).

報告不具合: Claude エクスポーター更新後の新 MD 形式でメッセージが 0 件になる。

変更点:
- 見出しが ## Prompt:/## Response: → ## User:/## Assistant: に変更
- タイムスタンプがブロック引用 ("> M/D/YYYY H:MM:SS") に変更
- 見出し後に空行が挟まるように変更

パーサーは旧・新の両方を吸収する必要がある。
"""
import unittest
from tools.utilities.chatlog_exporter_importer import _parse_markdown_format


OLD_FORMAT = """\
# Old Format Test

**Created:** 4/27/2026 11:48:40
**Updated:** 4/27/2026 13:40:21
**Exported:** 5/1/2026 0:17:58
**Link:** [https://claude.ai/chat/test](https://claude.ai/chat/test)

## Prompt:
2026/4/27 11:48:42

Hello, old format.

## Response:
2026/4/27 11:49:00

Hi there, old format response.

## Prompt:
2026/4/27 11:50:00

Second question.

## Response:
2026/4/27 11:51:00

Second answer.

Powered by Claude Exporter - claudexporter.com
"""

NEW_FORMAT = """\
# New Format Test

**Created:** 5/13/2026 1:15:27
**Updated:** 5/13/2026 1:47:25
**Exported:** 6/3/2026 22:26:20
**Link:** [https://claude.ai/chat/test2](https://claude.ai/chat/test2)

## User:

> 5/13/2026 1:15:28

Hello, new format.

## Assistant:

> 5/13/2026 1:17:08

Hi there, new format response.

## User:

> 5/13/2026 1:20:00

Second question, new style.

## Assistant:

> 5/13/2026 1:22:00

Second answer, new style.

---
Powered by [Claude Exporter](https://www.ai-chat-exporter.net)
"""


class TestMarkdownParseOldFormat(unittest.TestCase):
    def setUp(self):
        self.conv = _parse_markdown_format(OLD_FORMAT)

    def test_message_count(self):
        self.assertEqual(len(self.conv.messages), 4)

    def test_roles(self):
        roles = [m.role for m in self.conv.messages]
        self.assertEqual(roles, ["user", "assistant", "user", "assistant"])

    def test_timestamps_parsed(self):
        for m in self.conv.messages:
            self.assertIsNotNone(m.timestamp, f"Missing timestamp for: {m.content[:30]}")

    def test_content_no_timestamp_leak(self):
        for m in self.conv.messages:
            self.assertNotIn("2026/", m.content)

    def test_source_detected(self):
        self.assertEqual(self.conv.source, "claude")


class TestMarkdownParseNewFormat(unittest.TestCase):
    def setUp(self):
        self.conv = _parse_markdown_format(NEW_FORMAT)

    def test_message_count(self):
        self.assertEqual(len(self.conv.messages), 4)

    def test_roles(self):
        roles = [m.role for m in self.conv.messages]
        self.assertEqual(roles, ["user", "assistant", "user", "assistant"])

    def test_timestamps_parsed(self):
        for m in self.conv.messages:
            self.assertIsNotNone(m.timestamp, f"Missing timestamp for: {m.content[:30]}")

    def test_content_no_timestamp_leak(self):
        for m in self.conv.messages:
            self.assertNotRegex(m.content.strip(), r"^>\s*\d+/\d+/\d+")

    def test_source_detected(self):
        self.assertEqual(self.conv.source, "claude")

    def test_first_message_content_clean(self):
        self.assertTrue(self.conv.messages[0].content.startswith("Hello, new format"))

    def test_footer_stripped(self):
        last = self.conv.messages[-1].content
        self.assertNotIn("Powered by", last)
        self.assertNotIn("ai-chat-exporter", last)


class TestOldFormatFooterStripped(unittest.TestCase):
    def test_footer_stripped(self):
        conv = _parse_markdown_format(OLD_FORMAT)
        last = conv.messages[-1].content
        self.assertNotIn("Powered by", last)
        self.assertNotIn("claudexporter", last)


if __name__ == "__main__":
    unittest.main()
