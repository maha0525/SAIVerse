"""P4-b 命名（テーマ立て）の単体テスト。

検証スコープ:
1. ``detect_naming_candidates`` — クラスタ検知ロジック（≥3件、既存除外、max-1）
2. ``build_day_close_schema`` — candidates あり/なし で naming_reviews field の有無
3. ``_apply_naming_reviews`` — verdict=name → create_theme_page、name 欠落 → 警告、
   skip → 何もしない、冪等（同タイトル二度目は既存 id を返す）
4. ``create_theme_page`` — 新規作成・冪等チェック

all mocked — LLM コールなし、DB は sqlite3 インメモリ。
"""
from __future__ import annotations

import json
import sqlite3
import unittest
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_mem_conn() -> sqlite3.Connection:
    """インメモリ memory.db に Memopedia テーブルを用意して返す。"""
    from sai_memory.memopedia import init_memopedia_tables
    conn = sqlite3.connect(":memory:")
    init_memopedia_tables(conn)
    return conn


def _make_fake_manager(tasks: List[Dict[str, Any]], mem_conn: Optional[sqlite3.Connection] = None):
    """PersonaTaskManager.list_tasks の戻り値を固定した fake manager を返す。"""

    class FakeSessionLocal:
        pass

    manager = MagicMock()
    manager.SessionLocal = FakeSessionLocal

    # personas[persona_id].sai_memory.conn
    if mem_conn is not None:
        fake_sai_mem = MagicMock()
        fake_sai_mem.conn = mem_conn
        fake_persona = MagicMock()
        fake_persona.sai_memory = fake_sai_mem
        manager.personas = {"test_persona": fake_persona}
    else:
        manager.personas = {}

    return manager


def _make_task(short_id: int, desire_type: Optional[str], stage: str) -> Dict[str, Any]:
    """_task_to_dict が返す最小限の dict を模倣。"""
    ref = f"task:{short_id}"
    return {
        "id": f"id-{short_id}",
        "short_id": short_id,
        "task_ref": ref,
        "persona_id": "test_persona",
        "desire_type": desire_type,
        "stage": stage,
        "title": f"タスク{short_id}",
    }


# ---------------------------------------------------------------------------
# 1. detect_naming_candidates
# ---------------------------------------------------------------------------

class DetectNamingCandidatesTest(unittest.TestCase):
    """detect_naming_candidates の検知ロジックを検証する。"""

    def _call(self, tasks_completed, tasks_dormant, mem_conn=None):
        from saiverse.curation import detect_naming_candidates

        manager = _make_fake_manager(tasks_completed + tasks_dormant, mem_conn)

        # detect_naming_candidates 内で import される PersonaTaskManager を
        # パッチする（関数スコープの動的 import なので本モジュール側を patch）
        with patch(
            "saiverse.persona_task_manager.PersonaTaskManager"
        ) as MockPTM:
            instance = MockPTM.return_value

            def _list_tasks(persona_id, *, stage, include_steps):
                from saiverse.persona_task_manager import STAGE_COMPLETED, STAGE_DORMANT
                if stage == STAGE_COMPLETED:
                    return tasks_completed
                if stage == STAGE_DORMANT:
                    return tasks_dormant
                return []

            instance.list_tasks.side_effect = _list_tasks

            return detect_naming_candidates(manager, "test_persona")

    def test_returns_candidate_when_cluster_meets_min(self):
        tasks = [_make_task(i, "創作", "completed") for i in range(1, 5)]
        result = self._call(tasks, [])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["kind"], "naming")
        self.assertIn("創作", result[0]["cluster_id"])
        self.assertEqual(len(result[0]["member_refs"]), 4)

    def test_no_candidate_when_below_min(self):
        # NAMING_CLUSTER_MIN=3 → 2件ではダメ
        tasks = [_make_task(i, "勉強", "completed") for i in range(1, 3)]
        result = self._call(tasks, [])
        self.assertEqual(result, [])

    def test_excludes_tasks_without_desire_type(self):
        # desire_type=None は対象外
        tasks = [_make_task(i, None, "completed") for i in range(1, 5)]
        result = self._call(tasks, [])
        self.assertEqual(result, [])

    def test_excludes_already_themed_refs(self):
        """既にテーマページの member_refs に含まれている task:N は除外される。"""
        mem_conn = _make_mem_conn()
        # テーマページを作って task:1〜task:3 を member_refs に刻む
        from sai_memory.theme_pages import create_theme_page, ensure_root_theme
        ensure_root_theme(mem_conn)
        create_theme_page(
            mem_conn,
            title="既存テーマ",
            member_refs=["task:1", "task:2", "task:3"],
        )
        mem_conn.commit()

        tasks = [_make_task(i, "趣味", "completed") for i in range(1, 5)]
        result = self._call(tasks, [], mem_conn=mem_conn)
        # task:1〜3 は除外 → task:4 の 1 件だけ残り、MIN 未達でゼロ
        self.assertEqual(result, [])

    def test_max_candidates_cap(self):
        """NAMING_MAX_CANDIDATES=1 — 複数クラスタがあっても 1 件しか返さない。"""
        tasks_a = [_make_task(i, "音楽", "completed") for i in range(1, 5)]
        tasks_b = [_make_task(i + 10, "料理", "completed") for i in range(1, 5)]
        result = self._call(tasks_a + tasks_b, [])
        self.assertEqual(len(result), 1)

    def test_includes_dormant_tasks(self):
        dormant = [_make_task(i, "旅行", "dormant") for i in range(1, 5)]
        result = self._call([], dormant)
        self.assertEqual(len(result), 1)


# ---------------------------------------------------------------------------
# 2. build_day_close_schema — naming_reviews field の有無
# ---------------------------------------------------------------------------

class BuildDayCloseSchemaTest(unittest.TestCase):
    """build_day_close_schema の naming_candidates 引数の効果を検証する。

    実際のシグネチャ:
      build_day_close_schema(manager, persona_id, touched_desire_refs, ..., naming_candidates=None)
    manager / persona_id / touched_desire_refs の最低限 mock を渡す。
    """

    def _make_minimal_manager(self):
        """schema 構築に必要な最小限の manager mock。"""
        manager = MagicMock()

        class FakeDB:
            def query(self, *a, **kw):
                return MagicMock(
                    filter=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))),
                    filter_by=MagicMock(return_value=MagicMock(first=MagicMock(return_value=None))),
                )
            def close(self):
                pass

        manager.SessionLocal = lambda: FakeDB()
        return manager

    def _schema(self, naming_candidates=None):
        from saiverse.judgment_points import build_day_close_schema
        manager = self._make_minimal_manager()
        return build_day_close_schema(
            manager,
            "test_persona",
            [],  # touched_desire_refs
            naming_candidates=naming_candidates,
        )

    def test_no_naming_reviews_field_when_no_candidates(self):
        schema = self._schema(naming_candidates=None)
        self.assertNotIn("naming_reviews", schema.get("properties", {}))

    def test_no_naming_reviews_field_when_empty_candidates(self):
        schema = self._schema(naming_candidates=[])
        self.assertNotIn("naming_reviews", schema.get("properties", {}))

    def test_naming_reviews_present_when_candidates_given(self):
        candidates = [
            {"cluster_id": "naming:趣味", "kind": "naming",
             "member_refs": ["task:1", "task:2", "task:3"],
             "line": "[テーマ候補]..."},
        ]
        schema = self._schema(naming_candidates=candidates)
        props = schema.get("properties", {})
        self.assertIn("naming_reviews", props)
        nr = props["naming_reviews"]
        self.assertEqual(nr["type"], "array")
        items = nr["items"]
        # cluster_id は enum で candidates の cluster_id を含む
        enum_vals = items["properties"]["cluster_id"].get("enum", [])
        self.assertIn("naming:趣味", enum_vals)
        # verdict は ["name", "skip"]
        self.assertEqual(
            set(items["properties"]["verdict"]["enum"]),
            {"name", "skip"},
        )


# ---------------------------------------------------------------------------
# 3. _apply_naming_reviews
# ---------------------------------------------------------------------------

class ApplyNamingReviewsTest(unittest.TestCase):
    """_apply_naming_reviews の挙動を直接テストする。"""

    def _call(self, output, ctx, mem_conn):
        from builtin_data.tools.judgment_finalize import _apply_naming_reviews

        # manager の personas で mem_conn を返す fake
        fake_sai_mem = MagicMock()
        fake_sai_mem.conn = mem_conn
        fake_persona = MagicMock()
        fake_persona.sai_memory = fake_sai_mem
        manager = MagicMock()
        manager.personas = {"test_persona": fake_persona}

        lines: List[str] = []
        warnings: List[str] = []
        applied = _apply_naming_reviews(
            manager, "test_persona", output, ctx, lines, warnings
        )
        return applied, lines, warnings

    def test_verdict_name_creates_theme_page(self):
        mem_conn = _make_mem_conn()
        output = {
            "naming_reviews": [
                {"cluster_id": "naming:音楽", "verdict": "name", "name": "音楽の旅"},
            ]
        }
        ctx = {
            "naming_candidates": [
                {"cluster_id": "naming:音楽", "kind": "naming",
                 "member_refs": ["task:1", "task:2", "task:3"],
                 "line": "..."}
            ]
        }
        applied, lines, warnings = self._call(output, ctx, mem_conn)
        self.assertTrue(applied)
        self.assertFalse(warnings)
        # テーマページが memory.db に存在することを確認
        row = mem_conn.execute(
            "SELECT id, title FROM memopedia_pages "
            "WHERE parent_id='root_theme' AND title='音楽の旅'"
        ).fetchone()
        self.assertIsNotNone(row)
        # lines に成功メッセージが入る
        self.assertTrue(any("音楽の旅" in ln for ln in lines))

    def test_verdict_name_missing_name_field_warns(self):
        mem_conn = _make_mem_conn()
        output = {
            "naming_reviews": [
                {"cluster_id": "naming:音楽", "verdict": "name"},
            ]
        }
        ctx = {
            "naming_candidates": [
                {"cluster_id": "naming:音楽", "kind": "naming",
                 "member_refs": ["task:1", "task:2", "task:3"], "line": "..."}
            ]
        }
        applied, lines, warnings = self._call(output, ctx, mem_conn)
        self.assertFalse(applied)
        self.assertTrue(warnings)  # 警告が出る
        # テーマページは作られない
        row = mem_conn.execute(
            "SELECT id FROM memopedia_pages WHERE parent_id='root_theme'"
        ).fetchone()
        self.assertIsNone(row)

    def test_verdict_skip_does_nothing(self):
        mem_conn = _make_mem_conn()
        output = {
            "naming_reviews": [
                {"cluster_id": "naming:音楽", "verdict": "skip"},
            ]
        }
        ctx = {
            "naming_candidates": [
                {"cluster_id": "naming:音楽", "kind": "naming",
                 "member_refs": ["task:1", "task:2", "task:3"], "line": "..."}
            ]
        }
        applied, lines, warnings = self._call(output, ctx, mem_conn)
        self.assertFalse(applied)
        self.assertFalse(warnings)

    def test_no_naming_reviews_key_is_noop(self):
        mem_conn = _make_mem_conn()
        output = {}
        ctx = {"naming_candidates": []}
        applied, lines, warnings = self._call(output, ctx, mem_conn)
        self.assertFalse(applied)
        self.assertFalse(warnings)


# ---------------------------------------------------------------------------
# 4. create_theme_page — 新規作成・冪等
# ---------------------------------------------------------------------------

class CreateThemePageTest(unittest.TestCase):
    def setUp(self):
        self.conn = _make_mem_conn()
        from sai_memory.theme_pages import ensure_root_theme
        ensure_root_theme(self.conn)

    def test_creates_page_under_root_theme(self):
        from sai_memory.theme_pages import create_theme_page
        page_id = create_theme_page(
            self.conn,
            title="音楽の旅",
            member_refs=["task:1", "task:2", "task:3"],
        )
        self.assertIsNotNone(page_id)
        row = self.conn.execute(
            "SELECT title, parent_id FROM memopedia_pages WHERE id=?",
            (page_id,),
        ).fetchone()
        self.assertEqual(row[0], "音楽の旅")
        self.assertEqual(row[1], "root_theme")

    def test_idempotent_same_title_returns_existing_id(self):
        from sai_memory.theme_pages import create_theme_page
        id1 = create_theme_page(
            self.conn,
            title="音楽の旅",
            member_refs=["task:1", "task:2", "task:3"],
        )
        id2 = create_theme_page(
            self.conn,
            title="音楽の旅",
            member_refs=["task:1", "task:2", "task:3"],
        )
        self.assertEqual(id1, id2)
        # ページが1件だけ存在する
        count = self.conn.execute(
            "SELECT COUNT(*) FROM memopedia_pages "
            "WHERE parent_id='root_theme' AND title='音楽の旅'"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_metadata_contains_origin_and_member_refs(self):
        from sai_memory.theme_pages import create_theme_page
        page_id = create_theme_page(
            self.conn,
            title="料理の哲学",
            member_refs=["task:10", "task:11"],
            origin="naming",
        )
        row = self.conn.execute(
            "SELECT metadata FROM memopedia_pages WHERE id=?",
            (page_id,),
        ).fetchone()
        meta = json.loads(row[0])
        self.assertEqual(meta["origin"], "naming")
        self.assertIn("task:10", meta["member_refs"])
        self.assertIn("task:11", meta["member_refs"])

    def test_edit_history_recorded(self):
        from sai_memory.theme_pages import create_theme_page
        page_id = create_theme_page(
            self.conn,
            title="詩のひとかけら",
            member_refs=["task:5"],
        )
        row = self.conn.execute(
            "SELECT edit_source, edit_type FROM memopedia_page_edit_history "
            "WHERE page_id=?",
            (page_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "naming")   # edit_source
        self.assertEqual(row[1], "create")   # edit_type


if __name__ == "__main__":
    unittest.main()
