"""Regression: structured-output (dict) content must not be silently dropped.

A memorize node whose upstream produced structured output (a dict) passed that
object straight to the SQLite bind in ``SAIMemoryAdapter._append_message``,
which raised ``type 'dict' is not supported`` — swallowed as a WARNING, so the
memorize was silently lost (observed 2026-07-18, sophie_city_a). The write entry
now normalizes non-str content to text (dict/list → JSON).

See docs/issues/archive/memorize_dict_content_silently_dropped.md.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from saiverse_memory.adapter import _coerce_content_to_text
from sai_memory.memory.storage import get_messages_last


class DummyEmbedder:
    def __init__(self, model=None, **kwargs):
        self.model_name = model

    def embed(self, texts, **kwargs):
        return [[0.0] * 3 for _ in texts]


class CoerceContentToTextTest(unittest.TestCase):
    def test_str_passthrough(self):
        self.assertEqual(_coerce_content_to_text("hi"), "hi")

    def test_dict_becomes_json(self):
        self.assertEqual(_coerce_content_to_text({"a": 1, "b": "x"}), '{"a": 1, "b": "x"}')

    def test_list_becomes_json(self):
        self.assertEqual(_coerce_content_to_text(["a", 1]), '["a", 1]')

    def test_none_becomes_empty(self):
        self.assertEqual(_coerce_content_to_text(None), "")

    def test_other_falls_back_to_str(self):
        self.assertEqual(_coerce_content_to_text(42), "42")


class AppendDictContentTest(unittest.TestCase):
    """A dict content is persisted as JSON instead of failing the bind."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.persona_dir = Path(self._tmp.name) / "personas" / "tester"
        self.persona_dir.mkdir(parents=True, exist_ok=True)
        os.environ["SAIMEMORY_MEMORY"] = "1"
        self.addCleanup(self._cleanup_temp)
        self.addCleanup(os.environ.pop, "SAIMEMORY_MEMORY", None)

        patcher = patch("saiverse_memory.adapter.Embedder", DummyEmbedder)
        self.addCleanup(patcher.stop)
        patcher.start()

        from saiverse_memory import SAIMemoryAdapter

        self.adapter = SAIMemoryAdapter(
            "tester", persona_dir=self.persona_dir, resource_id="tester"
        )
        self.addCleanup(self.adapter.close)

    def _cleanup_temp(self):
        import gc

        gc.collect()
        try:
            self._tmp.cleanup()
        except OSError:
            pass

    def test_dict_content_is_stored_as_json_not_dropped(self):
        payload = {"decision": "continue", "reason": "まだ途中"}
        mid = self.adapter._append_message(
            building_id=None,
            message={"role": "assistant", "content": payload},
        )
        # Before the fix this returned None (dict bind failed → swallowed WARNING).
        self.assertIsNotNone(mid)

        thread_id = self.adapter._thread_id(None)
        rows = get_messages_last(self.adapter.conn, thread_id, limit=5)
        contents = [getattr(r, "content", None) for r in rows]
        self.assertIn('{"decision": "continue", "reason": "まだ途中"}', contents)

    def test_str_content_still_stored_verbatim(self):
        mid = self.adapter._append_message(
            building_id=None,
            message={"role": "assistant", "content": "ふつうの文字列"},
        )
        self.assertIsNotNone(mid)
        thread_id = self.adapter._thread_id(None)
        rows = get_messages_last(self.adapter.conn, thread_id, limit=5)
        self.assertIn("ふつうの文字列", [getattr(r, "content", None) for r in rows])


if __name__ == "__main__":
    unittest.main()
