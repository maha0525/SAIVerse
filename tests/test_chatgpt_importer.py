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

from tools.chatgpt_importer import ChatGPTExport
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


if __name__ == "__main__":
    unittest.main()
