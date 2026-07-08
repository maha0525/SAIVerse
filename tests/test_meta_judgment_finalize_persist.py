"""meta_judgment_finalize の SAIMemory 書き込み形式のテスト。

確定独白は role='assistant' のペルソナ発話ではなく、``<system>…</system>`` 包みの
role='user' ナレーションとして残す (プロンプト無し assistant 発話の few-shot 汚染回避、
2026-07-07 まはー指摘)。tags は ['meta_judgment'] のまま (event_message は付けない)。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class DummyEmbedder:
    def __init__(self, model=None, **kwargs) -> None:
        self.model_name = model

    def embed(self, texts, **kwargs):
        return [[0.0] * 3 for _ in texts]


class MetaJudgmentFinalizePersistTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.persona_path = Path(self._tmp.name) / "personas" / "tester"
        self.persona_path.mkdir(parents=True, exist_ok=True)
        os.environ["SAIMEMORY_MEMORY"] = "1"
        self.addCleanup(self._cleanup_temp)

        patcher = patch("saiverse_memory.adapter.Embedder", DummyEmbedder)
        self.addCleanup(patcher.stop)
        patcher.start()

        from saiverse_memory import SAIMemoryAdapter
        self.adapter = SAIMemoryAdapter(
            "tester", persona_dir=self.persona_path, resource_id="tester"
        )
        self.addCleanup(self._close_adapter)

    def _close_adapter(self):
        try:
            self.adapter.close()
        except Exception:
            pass

    def _cleanup_temp(self):
        import gc
        gc.collect()
        try:
            self._tmp.cleanup()
        except (PermissionError, OSError):
            pass

    def tearDown(self):
        os.environ.pop("SAIMEMORY_MEMORY", None)

    def _read_record(self):
        row = self.adapter.conn.execute(
            "SELECT role, content, line_role, scope, metadata FROM messages "
            "ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        return row

    def test_finalize_persists_system_wrapped_user_narration(self):
        from builtin_data.tools import meta_judgment_finalize as mod

        # persona lookup 用の manager。SessionLocal を持たせないことで
        # meta_judgment_log DB 書き込み経路をスキップさせる (hasattr False)。
        persona = SimpleNamespace(sai_memory=self.adapter)
        manager = SimpleNamespace(personas={"tester": persona})

        with patch.object(mod, "get_active_persona_id", return_value="tester"), \
                patch.object(mod, "get_active_manager", return_value=manager), \
                patch.object(mod, "get_active_pulse_context", return_value=None):
            # running_active + action='continue' → 発火スペルなし (Track ツール不要)。
            mod.meta_judgment_finalize(
                judgment_output={"monologue": "このまま続けよう", "action": "continue"},
                situation_kind="running_active",
                running_track_id="trk1",
            )

        row = self._read_record()
        self.assertIsNotNone(row)
        role, content, line_role, scope, metadata = row
        # ペルソナ発話ではなく <system> 包みの role='user' ナレーション。
        self.assertEqual(role, "user")
        self.assertEqual(content, "<system>メタ判断の記録:\nこのまま続けよう</system>")
        self.assertEqual(line_role, "meta_judgment")
        # 継続 (発火スペルなし) は discardable。
        self.assertEqual(scope, "discardable")
        # tags は meta_judgment のまま (event_message は付けない)。
        tags = json.loads(metadata)["tags"]
        self.assertEqual(tags, ["meta_judgment"])


if __name__ == "__main__":
    unittest.main()
