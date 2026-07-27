"""regenerate_entry の generate-then-swap (生成が先・削除が後) のテスト。

docs/issues/chronicle_eviction_applier_veto_deadlock.md (Codex レビュー 2026-07-27):
旧実装は「削除 → LLM 生成」の順だったため、LLM 呼び出しの間ずっと
「この範囲を覆うエントリが存在しない」空白が開いていた。

- 生成失敗でエントリを永久に失う (削除だけが残る)
- 空白中に Metabolism が走ると、圧縮区間の記録が「あらすじ恒久欠落」と
  誤判定されて捨てられる (_drop_dead_folds)

生成を先に行えば、外から見える空白は無い。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

PERSONA_ID = "regen-tester"


class DummyEmbedder:
    def __init__(self, model=None, **kwargs) -> None:
        self.model_name = model

    def embed(self, texts, **kwargs):
        return [[0.0] * 3 for _ in texts]


class RegenerateSwapTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        persona_path = Path(self._tmp.name) / "personas" / PERSONA_ID
        persona_path.mkdir(parents=True, exist_ok=True)
        os.environ["SAIMEMORY_MEMORY"] = "1"
        self.addCleanup(self._cleanup_temp)

        patcher = patch("saiverse_memory.adapter.Embedder", DummyEmbedder)
        self.addCleanup(patcher.stop)
        patcher.start()

        from saiverse_memory import SAIMemoryAdapter
        self.adapter = SAIMemoryAdapter(
            PERSONA_ID, persona_dir=persona_path, resource_id=PERSONA_ID,
        )
        self.addCleanup(self._close_adapter)

        self.message_ids = []
        for i in range(2):
            mid = self.adapter.append_persona_message({
                "role": "user" if i % 2 == 0 else "assistant",
                "content": f"メッセージ {i}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            self.message_ids.append(mid)

        from sai_memory.arasuji.storage import create_entry, init_arasuji_tables
        init_arasuji_tables(self.adapter.conn)
        self.old_entry = create_entry(
            self.adapter.conn, level=1, content="旧あらすじ",
            source_ids=list(self.message_ids),
            start_time=1, end_time=2,
            source_count=len(self.message_ids),
            message_count=len(self.message_ids),
        )

    def _close_adapter(self):
        try:
            self.adapter.close()
        except Exception:
            pass

    def _cleanup_temp(self):
        import gc
        gc.collect()
        os.environ.pop("SAIMEMORY_MEMORY", None)
        try:
            self._tmp.cleanup()
        except (PermissionError, OSError):
            pass

    def test_failed_generation_keeps_the_old_entry(self):
        """生成失敗 (None) では旧エントリを失わない (旧実装は削除済みで喪失)。"""
        from sai_memory.arasuji.storage import get_entry, regenerate_entry
        with patch(
            "scripts.arasuji.build_arasuji_core.regenerate_entry_from_messages",
            return_value=None,
        ):
            result = regenerate_entry(self.adapter.conn, self.old_entry.id)
        self.assertIsNone(result)
        self.assertIsNotNone(get_entry(self.adapter.conn, self.old_entry.id))

    def test_old_entry_is_alive_during_generation_and_swapped_after(self):
        """LLM 生成の最中も旧エントリは生きている (空白なし)。成功後に差し替わる。"""
        from sai_memory.arasuji.storage import (
            create_entry,
            get_entries_covering_messages,
            get_entry,
            regenerate_entry,
        )
        seen_during_generation = {}

        def _fake_generate(conn, messages, model_name, persona_id=None):
            # 生成中のスナップショット: 旧エントリはまだ存在するか
            seen_during_generation["old_alive"] = (
                get_entry(conn, self.old_entry.id) is not None
            )
            return create_entry(
                conn, level=1, content="新あらすじ",
                source_ids=[m.id for m in messages],
                start_time=1, end_time=2,
                source_count=len(messages), message_count=len(messages),
            )

        with patch(
            "scripts.arasuji.build_arasuji_core.regenerate_entry_from_messages",
            _fake_generate,
        ):
            new_entry = regenerate_entry(self.adapter.conn, self.old_entry.id)

        self.assertIsNotNone(new_entry)
        self.assertTrue(seen_during_generation["old_alive"])
        # 差し替え完了: 旧は消え、新がメッセージ集合を覆う (圧縮区間の予備照会
        # get_entries_covering_messages が引き当てる形)
        self.assertIsNone(get_entry(self.adapter.conn, self.old_entry.id))
        covering = get_entries_covering_messages(self.adapter.conn, self.message_ids)
        self.assertEqual([e.id for e in covering], [new_entry.id])

    def test_concurrent_delete_during_generation_is_not_undone(self):
        """LLM 生成中に旧エントリが並行削除されたら、再生成は競合として失敗し、
        新エントリを残さない (Codex レビュー 2026-07-27)。

        盲目に差し替えると、ユーザーが明示削除した範囲が別 id で復活する —
        削除という並行操作の結果の方を正とする。
        """
        from sai_memory.arasuji.storage import (
            create_entry,
            delete_entry,
            get_entries_covering_messages,
            regenerate_entry,
        )

        def _fake_generate(conn, messages, model_name, persona_id=None):
            # LLM 実行中に、並行の削除 API が旧エントリを消した状況
            delete_entry(conn, self.old_entry.id)
            return create_entry(
                conn, level=1, content="復活してはいけないあらすじ",
                source_ids=[m.id for m in messages],
                start_time=1, end_time=2,
                source_count=len(messages), message_count=len(messages),
            )

        with patch(
            "scripts.arasuji.build_arasuji_core.regenerate_entry_from_messages",
            _fake_generate,
        ):
            result = regenerate_entry(self.adapter.conn, self.old_entry.id)

        self.assertIsNone(result)
        # 削除された範囲は削除されたまま — どのエントリも復活していない
        self.assertEqual(
            get_entries_covering_messages(self.adapter.conn, self.message_ids), [],
        )

    def test_concurrent_content_edit_during_generation_is_not_overwritten(self):
        """LLM 生成中にユーザーが本文を編集していたら、再生成は競合として失敗し、
        編集を LLM 出力で潰さない (Codex レビュー 2026-07-27)。"""
        from sai_memory.arasuji.storage import (
            create_entry,
            get_entry,
            regenerate_entry,
            update_entry_content,
        )

        def _fake_generate(conn, messages, model_name, persona_id=None):
            # LLM 実行中に、並行の編集 API が本文を更新した状況
            update_entry_content(conn, self.old_entry.id, "ユーザーが直した本文")
            return create_entry(
                conn, level=1, content="LLM の出力",
                source_ids=[m.id for m in messages],
                start_time=1, end_time=2,
                source_count=len(messages), message_count=len(messages),
            )

        with patch(
            "scripts.arasuji.build_arasuji_core.regenerate_entry_from_messages",
            _fake_generate,
        ):
            result = regenerate_entry(self.adapter.conn, self.old_entry.id)

        self.assertIsNone(result)
        # 編集された本文が生きている。新行 (LLM の出力) は残っていない
        survivor = get_entry(self.adapter.conn, self.old_entry.id)
        self.assertEqual(survivor.content, "ユーザーが直した本文")
        from sai_memory.arasuji.storage import get_entries_covering_messages
        covering = get_entries_covering_messages(self.adapter.conn, self.message_ids)
        self.assertEqual([e.id for e in covering], [self.old_entry.id])


if __name__ == "__main__":
    unittest.main()
