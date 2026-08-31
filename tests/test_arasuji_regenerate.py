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

        def _fake_generate(conn, messages, model_name, persona_id=None,
                           extra_items=None):
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

        def _fake_generate(conn, messages, model_name, persona_id=None,
                           extra_items=None):
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

    def _annex_batch_to_old_entry(self, text="知覚テキスト", at=10):
        """知覚バッチを 1 件作り、旧 entry の材料 (付記印) として消費済みにする。"""
        from sai_memory.perception_buffer import (
            create_consumption_batch,
            init_perception_buffer_table,
            mark_batches_annexed,
            push_perception,
        )
        conn = self.adapter.conn
        init_perception_buffer_table(conn)
        item_id = push_perception(conn, "world_state", text)
        batch_id = create_consumption_batch(
            conn, [item_id], consumed_at=at, rendered_text=text,
        )
        mark_batches_annexed(conn, [batch_id], self.old_entry.id)
        conn.commit()
        return batch_id

    def test_failed_stamp_repoint_aborts_the_swap(self):
        """印の付け替えに失敗したら swap を中止する (fail-open の撤去)。

        旧挙動は warning だけ出して旧 entry の削除へ進んでいた — 削除の
        unmark で印が提示へ戻り、同じバッチの内容が新 entry の材料に居るのに
        次の編纂へも供給される (二重供給)。中止後は (a) 旧 entry 生存
        (b) 新 entry なし (c) 印は旧 entry 宛てのまま、で旧状態を維持する。
        """
        from sai_memory.arasuji.storage import (
            create_entry,
            get_entry,
            regenerate_entry,
        )
        from sai_memory.perception_buffer import list_batches_annexed_to
        conn = self.adapter.conn
        batch_id = self._annex_batch_to_old_entry()

        created_ids = []

        def _fake_generate(conn, messages, model_name, persona_id=None,
                           extra_items=None):
            e = create_entry(
                conn, level=1, content="新あらすじ",
                source_ids=[m.id for m in messages],
                start_time=1, end_time=2,
                source_count=len(messages), message_count=len(messages),
            )
            created_ids.append(e.id)
            return e

        def _fail_reassign(conn, old_id, new_id):
            raise RuntimeError("stamp repoint down")

        with patch(
            "scripts.arasuji.build_arasuji_core.regenerate_entry_from_messages",
            _fake_generate,
        ), patch(
            "sai_memory.perception_buffer.reassign_batches_annexed",
            _fail_reassign,
        ):
            result = regenerate_entry(conn, self.old_entry.id)

        self.assertIsNone(result)
        # (a) 旧 entry 生存
        self.assertIsNotNone(get_entry(conn, self.old_entry.id))
        # (b) 新 entry (replacement) は取り下げられて残っていない
        self.assertEqual(len(created_ids), 1)
        self.assertIsNone(get_entry(conn, created_ids[0]))
        # (c) 印は旧 entry 宛てのまま (提示に戻っていない = 宙吊りも二重もない)
        annexed = list_batches_annexed_to(conn, self.old_entry.id)
        self.assertEqual([b.id for b in annexed], [batch_id])

    def test_partially_repointed_stamps_are_returned_to_the_old_entry(self):
        """部分成功 (一部の印だけ新 id へ移って commit 済み) でも印を宙に浮かせない。

        中止経路の保険 (逆向きの付け替え) が、新 id を指してしまった印を
        旧 entry 宛てへ戻すことを固定する。
        """
        from sai_memory.arasuji.storage import (
            create_entry,
            get_entry,
            regenerate_entry,
        )
        from sai_memory.perception_buffer import (
            list_batches_annexed_to,
            reassign_batches_annexed as real_reassign,
        )
        conn = self.adapter.conn
        batch_a = self._annex_batch_to_old_entry(text="知覚 A", at=10)
        batch_b = self._annex_batch_to_old_entry(text="知覚 B", at=20)

        def _fake_generate(conn, messages, model_name, persona_id=None,
                           extra_items=None):
            return create_entry(
                conn, level=1, content="新あらすじ",
                source_ids=[m.id for m in messages],
                start_time=1, end_time=2,
                source_count=len(messages), message_count=len(messages),
            )

        calls = []

        def _partial_reassign(conn_, old_id, new_id):
            calls.append((old_id, new_id))
            if len(calls) == 1:
                # 部分成功: 2 件中 1 件だけ新 id へ移し、即 commit して
                # rollback で戻らない状態を作る (件数 1 != 2 で中止になる)
                conn_.execute(
                    "UPDATE perception_batches SET annexed_entry_id = ? "
                    "WHERE id = ?",
                    (new_id, batch_a),
                )
                conn_.commit()
                return 1
            # 2 回目以降 (保険の逆向き付け替え) は本物に委譲
            return real_reassign(conn_, old_id, new_id)

        with patch(
            "scripts.arasuji.build_arasuji_core.regenerate_entry_from_messages",
            _fake_generate,
        ), patch(
            "sai_memory.perception_buffer.reassign_batches_annexed",
            _partial_reassign,
        ):
            result = regenerate_entry(conn, self.old_entry.id)

        self.assertIsNone(result)
        self.assertIsNotNone(get_entry(conn, self.old_entry.id))
        # 印は 2 件とも旧 entry 宛てへ戻っている (宙吊り・新 id 残留なし)
        annexed = list_batches_annexed_to(conn, self.old_entry.id)
        self.assertEqual(sorted(b.id for b in annexed), sorted([batch_a, batch_b]))

    def _insert_fragment(self, fragment_id, entry_id):
        self.adapter.conn.execute(
            "INSERT INTO memopedia_fragments "
            "(id, content, entity_id, chronicle_entry_id, vividness, "
            "source_date, created_at) "
            "VALUES (?, '知識', 'root_chronicle', ?, 1.0, NULL, 1)",
            (fragment_id, entry_id),
        )
        self.adapter.conn.commit()

    def _fragment_owner(self, fragment_id):
        row = self.adapter.conn.execute(
            "SELECT chronicle_entry_id FROM memopedia_fragments WHERE id = ?",
            (fragment_id,),
        ).fetchone()
        return row[0] if row else None

    def test_fragments_are_repointed_on_successful_swap(self):
        """[2026-08-31 裁定] 旧 entry の Fragment は消さず新 entry へ付け替える。"""
        from sai_memory.arasuji.storage import create_entry, regenerate_entry
        self._insert_fragment("frag-r", self.old_entry.id)

        def _fake_generate(conn, messages, model_name, persona_id=None,
                           extra_items=None):
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
        self.assertEqual(self._fragment_owner("frag-r"), new_entry.id)

    def test_fragment_repoint_lock_failure_keeps_old_entry(self):
        """[Codex 三巡 F3] Fragment 付け替えは旧削除より前の可逆フェーズ —
        ロック等で失敗したら旧 entry を無傷のまま新を取り下げる (旧削除後の
        raise で旧新両方を失う並びを作らない)。"""
        import sqlite3 as _sqlite3

        from sai_memory.arasuji.storage import (
            create_entry,
            get_entry,
            regenerate_entry,
        )
        self._insert_fragment("frag-l", self.old_entry.id)
        created = []

        def _fake_generate(conn_, messages, model_name, persona_id=None,
                           extra_items=None):
            e = create_entry(
                conn_, level=1, content="新あらすじ",
                source_ids=[m.id for m in messages],
                start_time=1, end_time=2,
                source_count=len(messages), message_count=len(messages),
            )
            created.append(e.id)
            return e

        class _LockOnFragments:
            """memopedia_fragments に触る SQL だけロック例外を返す conn 代理。"""

            def __init__(self, real):
                self._real = real

            def execute(self, sql, *a, **k):
                if "memopedia_fragments" in sql:
                    raise _sqlite3.OperationalError("database is locked")
                return self._real.execute(sql, *a, **k)

            def __getattr__(self, name):
                return getattr(self._real, name)

        with patch(
            "scripts.arasuji.build_arasuji_core.regenerate_entry_from_messages",
            _fake_generate,
        ):
            result = regenerate_entry(
                _LockOnFragments(self.adapter.conn), self.old_entry.id,
            )
        self.assertIsNone(result)
        # 旧 entry は無傷・新 (replacement) は取り下げ・Fragment は旧のまま
        self.assertIsNotNone(get_entry(self.adapter.conn, self.old_entry.id))
        self.assertEqual(len(created), 1)
        self.assertIsNone(get_entry(self.adapter.conn, created[0]))
        self.assertEqual(self._fragment_owner("frag-l"), self.old_entry.id)

    def test_fragment_commit_failure_rolls_back_the_pending_repoint(self):
        """[Codex 十二巡 Q2] Fragment 付け替えの ``commit()`` が失敗したら、
        未確定の UPDATE を明示的に巻き戻してから取り下げる。

        巻き戻さずに ``_withdraw_replacement`` (内部で commit する) へ進むと、
        未確定だった付け替えが**取り下げの commit に相乗りして確定**し、
        Fragment が削除済みの replacement を指したまま UI から消える。
        """
        import sqlite3 as _sqlite3

        from sai_memory.arasuji.storage import (
            create_entry,
            get_entry,
            regenerate_entry,
        )
        self._insert_fragment("frag-c", self.old_entry.id)
        created = []

        def _fake_generate(conn_, messages, model_name, persona_id=None,
                           extra_items=None):
            e = create_entry(
                conn_, level=1, content="新あらすじ",
                source_ids=[m.id for m in messages],
                start_time=1, end_time=2,
                source_count=len(messages), message_count=len(messages),
            )
            created.append(e.id)
            return e

        class _FailFragmentCommit:
            """Fragment の付け替え UPDATE は通し、その commit だけ 1 回失敗させる。"""

            def __init__(self, real):
                self._real = real
                self._armed = False
                self.fired = False

            def execute(self, sql, *a, **k):
                if (
                    "UPDATE memopedia_fragments" in sql
                    and "WHERE chronicle_entry_id = ?" in sql
                    and not self.fired
                ):
                    self._armed = True
                return self._real.execute(sql, *a, **k)

            def commit(self):
                if self._armed:
                    self._armed = False
                    self.fired = True
                    raise _sqlite3.OperationalError("database is locked")
                return self._real.commit()

            def __getattr__(self, name):
                return getattr(self._real, name)

        proxy = _FailFragmentCommit(self.adapter.conn)
        with patch(
            "scripts.arasuji.build_arasuji_core.regenerate_entry_from_messages",
            _fake_generate,
        ):
            result = regenerate_entry(proxy, self.old_entry.id)

        self.assertIsNone(result)
        self.assertTrue(proxy.fired)
        # 旧 entry は無傷・replacement は撤去済み・Fragment は旧 entry のまま
        self.assertIsNotNone(get_entry(self.adapter.conn, self.old_entry.id))
        self.assertEqual(len(created), 1)
        self.assertIsNone(get_entry(self.adapter.conn, created[0]))
        self.assertEqual(self._fragment_owner("frag-c"), self.old_entry.id)

    def test_non_operational_db_error_on_fragments_still_withdraws(self):
        """[Codex 十二巡 Q2] OperationalError 以外の DB 例外 (IntegrityError 等)
        でも取り下げ経路を通る。

        捕捉が OperationalError だけだと、他の ``sqlite3.DatabaseError`` は
        handler の外へ逃げて撤去自体が行われず、旧と replacement の二重被覆が
        残る。
        """
        import sqlite3 as _sqlite3

        from sai_memory.arasuji.storage import (
            create_entry,
            get_entry,
            regenerate_entry,
        )
        self._insert_fragment("frag-i", self.old_entry.id)
        created = []

        def _fake_generate(conn_, messages, model_name, persona_id=None,
                           extra_items=None):
            e = create_entry(
                conn_, level=1, content="新あらすじ",
                source_ids=[m.id for m in messages],
                start_time=1, end_time=2,
                source_count=len(messages), message_count=len(messages),
            )
            created.append(e.id)
            return e

        class _IntegrityOnFragmentRepoint:
            def __init__(self, real):
                self._real = real

            def execute(self, sql, *a, **k):
                if (
                    "UPDATE memopedia_fragments" in sql
                    and "WHERE chronicle_entry_id = ?" in sql
                ):
                    raise _sqlite3.IntegrityError("constraint failed")
                return self._real.execute(sql, *a, **k)

            def __getattr__(self, name):
                return getattr(self._real, name)

        with patch(
            "scripts.arasuji.build_arasuji_core.regenerate_entry_from_messages",
            _fake_generate,
        ):
            result = regenerate_entry(
                _IntegrityOnFragmentRepoint(self.adapter.conn),
                self.old_entry.id,
            )

        self.assertIsNone(result)
        self.assertIsNotNone(get_entry(self.adapter.conn, self.old_entry.id))
        self.assertEqual(len(created), 1)
        self.assertIsNone(get_entry(self.adapter.conn, created[0]))
        self.assertEqual(self._fragment_owner("frag-i"), self.old_entry.id)

    def test_swap_keeps_single_child_parent_alive_and_consistent(self):
        """[Codex 六巡 J5] 一人っ子の親を持つエントリの正常 swap で、親は
        途中の引き直しに解体されず、帳簿が最終形 (新しい子) に揃う。"""
        from sai_memory.arasuji.storage import (
            create_entry,
            get_entry,
            mark_consolidated,
            regenerate_entry,
        )
        conn = self.adapter.conn
        p = create_entry(
            conn, level=2, content="P", source_ids=[self.old_entry.id],
            start_time=1, end_time=2, source_count=1,
            message_count=self.old_entry.message_count,
        )
        mark_consolidated(conn, [self.old_entry.id], p.id)

        def _fake_generate(conn_, messages, model_name, persona_id=None,
                           extra_items=None):
            return create_entry(
                conn_, level=1, content="新あらすじ",
                source_ids=[m.id for m in messages],
                start_time=1, end_time=2,
                source_count=len(messages), message_count=len(messages),
            )

        with patch(
            "scripts.arasuji.build_arasuji_core.regenerate_entry_from_messages",
            _fake_generate,
        ):
            new_entry = regenerate_entry(conn, self.old_entry.id)
        self.assertIsNotNone(new_entry)
        p_after = get_entry(conn, p.id)
        self.assertIsNotNone(p_after)  # 一人っ子でも親は生きている
        self.assertEqual(p_after.source_ids, [new_entry.id])
        self.assertEqual(p_after.source_count, 1)
        self.assertEqual(p_after.message_count, new_entry.message_count)
        self.assertEqual(get_entry(conn, new_entry.id).parent_id, p.id)

    def test_fragment_failure_preserves_batch_annexation(self):
        """[Codex 四巡 G3-a] 順序は Fragment → バッチ。Fragment 更新の失敗で
        取り下げても、バッチの旧帰属は一切動いていない (NULL 落ちしない)。"""
        import sqlite3 as _sqlite3

        from sai_memory.arasuji.storage import (
            create_entry,
            get_entry,
            regenerate_entry,
        )
        from sai_memory.perception_buffer import list_batches_annexed_to
        batch_id = self._annex_batch_to_old_entry()
        self._insert_fragment("frag-b", self.old_entry.id)

        def _fake_generate(conn_, messages, model_name, persona_id=None,
                           extra_items=None):
            return create_entry(
                conn_, level=1, content="新あらすじ",
                source_ids=[m.id for m in messages],
                start_time=1, end_time=2,
                source_count=len(messages), message_count=len(messages),
            )

        class _LockOnFragments:
            def __init__(self, real):
                self._real = real

            def execute(self, sql, *a, **k):
                if "memopedia_fragments" in sql:
                    raise _sqlite3.OperationalError("database is locked")
                return self._real.execute(sql, *a, **k)

            def __getattr__(self, name):
                return getattr(self._real, name)

        with patch(
            "scripts.arasuji.build_arasuji_core.regenerate_entry_from_messages",
            _fake_generate,
        ):
            result = regenerate_entry(
                _LockOnFragments(self.adapter.conn), self.old_entry.id,
            )
        self.assertIsNone(result)
        self.assertIsNotNone(get_entry(self.adapter.conn, self.old_entry.id))
        # バッチの帰属は旧 entry のまま (NULL 落ちも新 id 残留もない)
        annexed = list_batches_annexed_to(self.adapter.conn, self.old_entry.id)
        self.assertEqual([b.id for b in annexed], [batch_id])
        self.assertEqual(self._fragment_owner("frag-b"), self.old_entry.id)

    def test_delete_failure_reverts_stamps_and_fragments(self):
        """[Codex 四巡 G3-b] 印と Fragment を動かした後の失敗 (旧削除の失敗) は、
        取り下げの unmark (NULL 落ち) より前に両方を旧へ明示的に戻す。"""
        from sai_memory.arasuji import storage as arasuji_storage
        from sai_memory.arasuji.storage import (
            create_entry,
            get_entry,
            regenerate_entry,
        )
        from sai_memory.perception_buffer import list_batches_annexed_to
        batch_id = self._annex_batch_to_old_entry()
        self._insert_fragment("frag-d", self.old_entry.id)
        created = []

        def _fake_generate(conn_, messages, model_name, persona_id=None,
                           extra_items=None):
            e = create_entry(
                conn_, level=1, content="新あらすじ",
                source_ids=[m.id for m in messages],
                start_time=1, end_time=2,
                source_count=len(messages), message_count=len(messages),
            )
            created.append(e.id)
            return e

        real_delete = arasuji_storage.delete_entry_and_update_parent

        def _failing_delete(conn_, eid, *, refresh_ancestors=True):
            if eid == self.old_entry.id:
                raise RuntimeError("delete down")
            return real_delete(conn_, eid, refresh_ancestors=refresh_ancestors)

        with patch(
            "scripts.arasuji.build_arasuji_core.regenerate_entry_from_messages",
            _fake_generate,
        ), patch(
            "sai_memory.arasuji.storage.delete_entry_and_update_parent",
            _failing_delete,
        ):
            with self.assertRaises(RuntimeError):
                regenerate_entry(self.adapter.conn, self.old_entry.id)
        # 旧 entry 無傷・新は取り下げ・バッチと Fragment は旧へ戻っている
        self.assertIsNotNone(get_entry(self.adapter.conn, self.old_entry.id))
        self.assertEqual(len(created), 1)
        self.assertIsNone(get_entry(self.adapter.conn, created[0]))
        annexed = list_batches_annexed_to(self.adapter.conn, self.old_entry.id)
        self.assertEqual([b.id for b in annexed], [batch_id])
        self.assertEqual(self._fragment_owner("frag-d"), self.old_entry.id)

    def test_concurrent_content_edit_during_generation_is_not_overwritten(self):
        """LLM 生成中にユーザーが本文を編集していたら、再生成は競合として失敗し、
        編集を LLM 出力で潰さない (Codex レビュー 2026-07-27)。"""
        from sai_memory.arasuji.storage import (
            create_entry,
            get_entry,
            regenerate_entry,
            update_entry_content,
        )

        def _fake_generate(conn, messages, model_name, persona_id=None,
                           extra_items=None):
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


class GeneratorCoverageCharsTest(unittest.TestCase):
    """generate_level1_arasuji (再生成・CLI 経路) の coverage_chars 保存。

    executor 経路は coverage_chars (材料としての字数 = 圧縮後) を
    extra_metadata に保存するが、generator 経路は保存していなかった —
    後から bands.backfill_coverage が生ログの素の字数で埋め、長さ規則
    (material_chars: 500 字超の機構名義行は決定論の一行に縮む) と食い違う。
    """

    def test_entry_metadata_records_compressed_coverage_chars(self):
        import json
        import sqlite3

        from sai_memory.arasuji.generator import (
            MECHANISM_TEXT_MAX_CHARS,
            generate_level1_arasuji,
            material_chars,
        )
        from sai_memory.arasuji.storage import init_arasuji_tables
        from sai_memory.memory.storage import Message

        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        init_arasuji_tables(conn)

        long_tool_text = "[Spell Result: web_search]\n" + "x" * (
            MECHANISM_TEXT_MAX_CHARS + 100
        )
        messages = [
            Message(
                id="m1", thread_id="main", role="user", content="こんにちは",
                resource_id=None, created_at=100, metadata=None,
            ),
            # 500 字超の機構名義行 — 材料としては決定論の一行に縮む
            Message(
                id="m2", thread_id="main", role="user", content=long_tool_text,
                resource_id=None, created_at=200,
                metadata={"tags": ["handy_tool"]},
            ),
        ]

        class _Client:
            def generate(self, messages, tools):
                return "生成されたあらすじ。"

        entry = generate_level1_arasuji(_Client(), conn, messages)
        self.assertIsNotNone(entry)

        row = conn.execute(
            "SELECT metadata FROM memopedia_pages WHERE id = ?", (entry.id,)
        ).fetchone()
        meta = json.loads(row[0])

        expected = sum(material_chars(m) for m in messages)
        raw_total = sum(len(m.content or "") for m in messages)
        # 圧縮後の合計であること (素の合計字数ではない)
        self.assertEqual(meta["coverage_chars"], expected)
        self.assertLess(expected, raw_total)


if __name__ == "__main__":
    unittest.main()
