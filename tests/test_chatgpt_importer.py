from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.utilities.chatgpt_importer import ChatGPTExport
from scripts.import_chatgpt_conversations import resolve_thread_suffix


def _sample_conversation() -> Dict[str, Any]:
    root_id = "root"
    user_id = "user-msg"
    assistant_id = "assistant-msg"

    return {
        "id": "conversation-1",
        "conversation_id": "chatgpt-uuid-1234",
        "title": "Sample Conversation",
        "create_time": 1740000000,
        "update_time": 1740000600,
        "current_node": assistant_id,
        "mapping": {
            root_id: {
                "id": root_id,
                "message": None,
                "parent": None,
                "children": [user_id],
            },
            user_id: {
                "id": user_id,
                "parent": root_id,
                "children": [assistant_id],
                "message": {
                    "author": {"role": "user"},
                    # Milliseconds to ensure normalization works.
                    "create_time": 1740000000123,
                    "content": {"content_type": "text", "parts": ["Hello from user"]},
                },
            },
            assistant_id: {
                "id": assistant_id,
                "parent": user_id,
                "children": [],
                "message": {
                    "author": {"role": "assistant"},
                    # Microseconds to ensure normalization continues to divide.
                    "create_time": 1740000000456789,
                    "content": {"content_type": "text", "parts": ["Hello from assistant"]},
                },
            },
        },
    }


class ChatGPTImporterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.sample_path = Path(self.tmp_dir.name) / "sample_conversations.json"
        payload = [_sample_conversation()]
        self.sample_path.write_text(json.dumps(payload), encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_summaries_and_payloads(self) -> None:
        export = ChatGPTExport(self.sample_path)

        self.assertEqual(len(export.conversations), 1)
        record = export.conversations[0]
        self.assertEqual(record.title, "Sample Conversation")

        summary = record.to_summary_dict()
        self.assertEqual(summary["id"], "conversation-1")
        self.assertEqual(summary["message_count"], 2)
        self.assertTrue(summary["first_user_preview"].startswith("Hello from user"))

        # Ensure timestamps were normalised from ms/us to seconds.
        self.assertIsNotNone(record.create_time)
        self.assertIsInstance(record.messages[0].create_time, datetime)
        self.assertIsInstance(record.messages[1].create_time, datetime)

        user_ts = record.messages[0].create_time
        assistant_ts = record.messages[1].create_time
        self.assertLess(assistant_ts, datetime(4000, 1, 1, tzinfo=timezone.utc))
        self.assertLess(user_ts, datetime(4000, 1, 1, tzinfo=timezone.utc))

        payloads = list(record.iter_memory_payloads(include_roles=["user", "assistant"]))
        self.assertEqual(len(payloads), 2)
        for payload in payloads:
            self.assertIn("timestamp", payload)
            ts = payload["timestamp"]
            self.assertTrue(ts.endswith("Z"), f"timestamp not normalised: {ts}")

    def test_thread_suffix_resolution(self) -> None:
        export = ChatGPTExport(self.sample_path)
        record = export.conversations[0]
        self.assertEqual(resolve_thread_suffix(record, None), record.conversation_id)
        self.assertEqual(resolve_thread_suffix(record, "custom"), "custom")

        fallback = replace(record, conversation_id=None, identifier="alt-id")
        self.assertEqual(resolve_thread_suffix(fallback, None), "alt-id")

    def test_import_writes_the_header_as_a_system_notice(self) -> None:
        """取り込みの見出しは role=system とシステム通知の印 (系統名) で書く。"""
        import argparse
        from unittest.mock import patch

        import scripts.import_chatgpt_conversations as importer
        from database.system_notice_migration import (
            CHATGPT_IMPORT_KIND,
            classify_unmarked_system_message,
        )
        from sai_memory.memory.storage import SYSTEM_NOTICE_KEY, is_system_notice

        written = []

        class _FakeAdapter:
            def __init__(self, persona_id: str) -> None:
                self.persona_id = persona_id

            def is_ready(self) -> bool:
                return True

            def append_persona_message(self, payload, *, thread_suffix=None):
                written.append((thread_suffix, payload))
                return "id"

            def backfill_missing_message_embeddings(self):
                return {"embedded": 0, "total_missing": 0}

            def reembed_stale_messages(self):
                return {"embedded": 0, "total": 0}

            def close(self) -> None:
                pass

        args = argparse.Namespace(
            select=None, select_all=True, preview=40, roles="user,assistant",
            thread_suffix=None, no_header=False, dry_run=False, persona="p1",
            output="json",
        )
        with patch.object(importer, "SAIMemoryAdapter", _FakeAdapter):
            importer.handle_import(ChatGPTExport(self.sample_path), args)

        self.assertEqual(len(written), 3)
        _suffix, header = written[0]
        self.assertEqual(header["role"], "system")
        self.assertEqual(header["metadata"][SYSTEM_NOTICE_KEY], importer.CHATGPT_IMPORT_NOTICE_KIND)
        self.assertTrue(is_system_notice(header))
        self.assertEqual(
            header["content"],
            '[Imported ChatGPT conversation "Sample Conversation" (chatgpt-uuid-1234) '
            "created 2025-02-19T21:20:00Z]",
        )
        # 発言の行には通知の印を付けない
        for _suffix, payload in written[1:]:
            self.assertNotEqual(payload["role"], "system")
            self.assertNotIn(SYSTEM_NOTICE_KEY, payload.get("metadata") or {})
        # 書き手と、印の無い旧い見出しの行を見分ける移行の規則 (版 4) が同じ形と系統名を使う
        self.assertEqual(importer.CHATGPT_IMPORT_NOTICE_KIND, CHATGPT_IMPORT_KIND)
        self.assertEqual(
            classify_unmarked_system_message({"tags": ["conversation"]}, header["content"]),
            CHATGPT_IMPORT_KIND,
        )

    def test_header_with_title_as_identifier_is_still_recognised(self) -> None:
        """会話の id が無く題が id に入る形 (括弧や引用符を含む題) も、移行の規則が同じ形と見る。"""
        from database.system_notice_migration import (
            CHATGPT_IMPORT_KIND,
            classify_unmarked_system_message,
        )
        from scripts.import_chatgpt_conversations import build_header_payload

        record = ChatGPTExport(self.sample_path).conversations[0]
        odd = replace(
            record, title='題 "引用" (括弧)\n二行目', conversation_id=None,
            identifier='題 "引用" (括弧)\n二行目',
        )
        header = build_header_payload(odd)
        self.assertEqual(
            classify_unmarked_system_message({}, header["content"]), CHATGPT_IMPORT_KIND,
        )


if __name__ == "__main__":
    unittest.main()
