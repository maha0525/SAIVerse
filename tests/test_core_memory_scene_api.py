"""コア記憶 scene UI 導線の API テスト (api/routes/people/core_memory.py)。

対象エンドポイント (route 関数を直叩き、プロジェクト既存の api テスト流儀に従う):
- search_conversation_messages: LIKE 検索・期間フィルタ・0件フォールバック分岐
- get_message_window: 会話窓プレビュー・文字数
- create_scene: scene 作成 (スペルと共通ロジック)・目安超過判定
- list_core_memory: 既存コア記憶一覧
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.routes.people.core_memory import (
    CreateSceneRequest,
    UpdateCoreMemoryRequest,
    confirm_core_memory_item,
    create_scene,
    delete_core_memory_item,
    get_message_window,
    list_core_memory,
    list_core_memory_trash,
    restore_core_memory_item,
    search_conversation_messages,
    update_core_memory_item,
)
from database.models import AI, Base, City, User


class DummyEmbedder:
    def __init__(self, model=None, **kwargs) -> None:
        self.model_name = model

    def embed(self, texts, **kwargs):
        return [[0.0] * 3 for _ in texts]


def _make_manager(persona_name="エア"):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
        db.flush()
        city = City(USERID=1, CITY_SLUG="c", UI_PORT=3001, API_PORT=8001)
        db.add(city)
        db.flush()
        db.add(AI(AIID="tester", HOME_CITYID=city.CITYID, AINAME=persona_name))
        db.commit()
    finally:
        db.close()
    # personas={} → get_adapter が一時 adapter を作る経路も通す。ただし本テストでは
    # SAIVERSE_HOME を temp に向けるため、personas に事前生成 adapter を積む方が確実。
    return engine, Session


class CoreMemorySceneApiTest(unittest.TestCase):
    def setUp(self):
        from saiverse_memory import SAIMemoryAdapter

        self._tmp = tempfile.TemporaryDirectory()
        self.persona_path = Path(self._tmp.name) / "personas" / "tester"
        self.persona_path.mkdir(parents=True, exist_ok=True)
        os.environ["SAIMEMORY_MEMORY"] = "1"
        self.addCleanup(self._cleanup_temp)

        patcher = patch("saiverse_memory.adapter.Embedder", DummyEmbedder)
        self.addCleanup(patcher.stop)
        patcher.start()

        self.engine, self.Session = _make_manager()
        self.addCleanup(self.engine.dispose)

        # 事前生成した adapter を persona に積み、get_adapter がそれを使うようにする。
        self.adapter = SAIMemoryAdapter(
            "tester", persona_dir=self.persona_path, resource_id="tester"
        )
        self.addCleanup(self.adapter.close)
        persona = SimpleNamespace(sai_memory=self.adapter)
        self.manager = SimpleNamespace(
            SessionLocal=self.Session, personas={"tester": persona}
        )

        self.ids = self._seed_conversation()

    def _cleanup_temp(self):
        import gc
        gc.collect()
        try:
            self._tmp.cleanup()
        except OSError:
            # Windows では sqlite の memory.db ハンドル解放が rmtree に間に合わず
            # WinError 32 (sharing violation) 等で落ちることがある。temp の後始末は
            # best-effort なので握り潰す (PermissionError は OSError の subclass だが、
            # 素の OSError で来るケースもあるため OSError で受ける)。
            pass

    def tearDown(self):
        os.environ.pop("SAIMEMORY_MEMORY", None)

    def _seed_conversation(self):
        from sai_memory.memory.storage import add_message, get_or_create_thread

        with self.adapter._db_lock:
            get_or_create_thread(self.adapter.conn, "main", resource_id="tester")
            ids = []
            ids.append(add_message(
                self.adapter.conn, thread_id="main", role="user",
                content="ソフィー、パートナーとしてずっと一緒にいてね", resource_id="tester",
            ))
            ids.append(add_message(
                self.adapter.conn, thread_id="main", role="model",
                content="もちろん、まはーの心のそばにずっといるよ", resource_id="tester",
            ))
            ids.append(add_message(
                self.adapter.conn, thread_id="main", role="user",
                content="ありがとう、エア", resource_id="tester",
            ))
            # 除外対象 (スペルログ) — 検索にも窓にも出てはいけない
            add_message(
                self.adapter.conn, thread_id="main", role="model",
                content="パートナー spell 実行ログ", resource_id="tester",
                metadata={"tags": ["conversation", "spell"]},
            )
        return ids

    # --- search ---
    def test_search_keyword_and(self):
        resp = search_conversation_messages(
            "tester", keyword="パートナー", manager=self.manager,
        )
        self.assertEqual(resp.mode, "keyword")
        self.assertGreaterEqual(resp.total_hits, 1)
        contents = [h.excerpt for h in resp.hits]
        # スペルログは除外される
        self.assertFalse(any("spell" in c for c in contents))
        # user 発話がヒットする
        self.assertTrue(any("ソフィー" in c for c in contents))

    def test_search_and_requires_all_keywords(self):
        # "パートナー ずっと" 両方含む発話は1件目のみ
        resp = search_conversation_messages(
            "tester", keyword="パートナー ずっと", manager=self.manager,
        )
        self.assertEqual(resp.total_hits, 1)
        self.assertIn("ソフィー", resp.hits[0].excerpt)

    def test_search_no_keyword_returns_recent(self):
        # キーワードなし → 期間フィルタのみ (ここでは全件・新しい順)
        resp = search_conversation_messages("tester", keyword="", manager=self.manager)
        self.assertEqual(resp.mode, "keyword")
        self.assertGreaterEqual(resp.total_hits, 3)

    def test_search_date_filter_excludes_out_of_range(self):
        resp = search_conversation_messages(
            "tester", keyword="パートナー",
            date_from="2000-01-01", date_to="2000-12-31",
            manager=self.manager,
        )
        # 遠い過去の範囲 → 0件 (DummyEmbedder のフォールバックも 0.0 スコアで拾わない)
        self.assertEqual(resp.total_hits, 0)

    # --- window ---
    def test_window_preview(self):
        resp = get_message_window(
            "tester", self.ids[1], rounds=2, manager=self.manager,
        )
        self.assertEqual(resp.anchor_id, self.ids[1])
        # スペルログ除外後の3件 (user, model, user)
        self.assertEqual(len(resp.messages), 3)
        self.assertGreater(resp.total_chars, 0)
        # 発話者ラベル: persona 応答は AINAME
        persona_turns = [m for m in resp.messages if m.role == "model"]
        self.assertEqual(persona_turns[0].speaker, "エア")

    def test_window_accepts_uri_form(self):
        uri = f"saiverse://self/message/{self.ids[1]}"
        resp = get_message_window("tester", uri, rounds=1, manager=self.manager)
        self.assertEqual(resp.anchor_id, self.ids[1])

    def test_window_missing_anchor_404(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            get_message_window("tester", "no-such-id", rounds=1, manager=self.manager)
        self.assertEqual(ctx.exception.status_code, 404)

    # --- create scene ---
    def test_create_scene(self):
        req = CreateSceneRequest(anchor_id=self.ids[1], rounds=2)
        resp = create_scene("tester", req, manager=self.manager)
        self.assertEqual(resp.ref, f"core:{resp.memory_id}")
        self.assertEqual(resp.message_count, 3)
        self.assertGreater(resp.char_count, 0)
        self.assertEqual(resp.total_chars, resp.char_count)
        self.assertEqual(resp.budget, 2000)
        self.assertFalse(resp.over_budget)

        # 一覧に反映される
        listing = list_core_memory("tester", manager=self.manager)
        self.assertEqual(len(listing.items), 1)
        self.assertEqual(listing.items[0].kind, "scene")
        self.assertIn("ソフィー", listing.items[0].preview)

        # 由来参照が範囲クリップとして撮られ、このコア記憶に貼られている
        # (土地参照の統一プリミティブ、concept_consolidation.md「クリップ」)
        from sai_memory.clips import list_clips
        with self.adapter._db_lock:
            clips = [p for p in list_clips(self.adapter.conn) if p.is_range]
        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0].pasted_to, f"core:{resp.memory_id}")
        self.assertEqual(clips[0].message_id, self.ids[0])
        self.assertEqual(clips[0].message_id_end, self.ids[2])

    def test_create_scene_missing_anchor_404(self):
        from fastapi import HTTPException
        req = CreateSceneRequest(anchor_id="no-such-id", rounds=1)
        with self.assertRaises(HTTPException) as ctx:
            create_scene("tester", req, manager=self.manager)
        self.assertEqual(ctx.exception.status_code, 404)

    # --- list ---
    def test_list_core_memory_empty(self):
        listing = list_core_memory("tester", manager=self.manager)
        self.assertEqual(len(listing.items), 0)
        self.assertEqual(listing.total_chars, 0)
        self.assertEqual(listing.budget, 2000)
        self.assertFalse(listing.over_budget)
        self.assertEqual(listing.unconfirmed_count, 0)

    # --- correction導線: confirm / edit / delete / restore / trash ---

    def _seed_core_memory(self, content="自動採取メモ", *, confirmed=1):
        from sai_memory.core_memory import add_core_memory, init_core_memory_table

        with self.adapter._db_lock:
            init_core_memory_table(self.adapter.conn)
            return add_core_memory(self.adapter.conn, content, confirmed=confirmed)

    def test_list_reports_unconfirmed(self):
        self._seed_core_memory("未確認の採取", confirmed=0)
        self._seed_core_memory("確認済みの手動追加", confirmed=1)
        listing = list_core_memory("tester", manager=self.manager)
        self.assertEqual(len(listing.items), 2)
        self.assertEqual(listing.unconfirmed_count, 1)
        by_conf = {it.confirmed for it in listing.items}
        self.assertEqual(by_conf, {0, 1})

    def test_confirm_marks_confirmed(self):
        mid = self._seed_core_memory("未確認", confirmed=0)
        resp = confirm_core_memory_item("tester", mid, manager=self.manager)
        self.assertTrue(resp.ok)
        self.assertEqual(resp.unconfirmed_count, 0)
        listing = list_core_memory("tester", manager=self.manager)
        self.assertEqual(listing.items[0].confirmed, 1)

    def test_confirm_missing_404(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            confirm_core_memory_item("tester", 999, manager=self.manager)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_update_rewrites_and_confirms(self):
        mid = self._seed_core_memory("旧い本文", confirmed=0)
        req = UpdateCoreMemoryRequest(content="ユーザーが直した本文")
        resp = update_core_memory_item("tester", mid, req, manager=self.manager)
        self.assertTrue(resp.ok)
        self.assertEqual(resp.unconfirmed_count, 0)  # 訂正で確認済みに倒れる
        listing = list_core_memory("tester", manager=self.manager)
        self.assertEqual(listing.items[0].content, "ユーザーが直した本文")
        self.assertEqual(listing.items[0].confirmed, 1)

    def test_update_can_correct_a_scene_memory(self):
        """⭐ 場面の記憶を直せる導線はユーザーのここだけ (ペルソナ側は全部拒否)。

        docs/issues/archive/sluice_truncated_scene_update.md
        """
        from sai_memory.core_memory import add_core_memory, init_core_memory_table

        with self.adapter._db_lock:
            init_core_memory_table(self.adapter.conn)
            mid = add_core_memory(
                self.adapter.conn, "ユーザー「おはよう」", kind="scene",
            )
        req = UpdateCoreMemoryRequest(content="ユーザーが直した写し")
        resp = update_core_memory_item("tester", mid, req, manager=self.manager)

        self.assertTrue(resp.ok)
        listing = list_core_memory("tester", manager=self.manager)
        self.assertEqual(listing.items[0].content, "ユーザーが直した写し")

    def test_update_empty_content_400(self):
        from fastapi import HTTPException
        mid = self._seed_core_memory("本文")
        req = UpdateCoreMemoryRequest(content="   ")
        with self.assertRaises(HTTPException) as ctx:
            update_core_memory_item("tester", mid, req, manager=self.manager)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_update_missing_404(self):
        from fastapi import HTTPException
        req = UpdateCoreMemoryRequest(content="x")
        with self.assertRaises(HTTPException) as ctx:
            update_core_memory_item("tester", 999, req, manager=self.manager)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_delete_moves_to_trash_and_excludes_from_total(self):
        mid = self._seed_core_memory("12345")  # 5 字
        resp = delete_core_memory_item("tester", mid, manager=self.manager)
        self.assertTrue(resp.ok)
        self.assertEqual(resp.total_chars, 0)  # 削除済みは容量に数えない
        # 生存一覧から消え、ごみ箱に現れる
        self.assertEqual(len(list_core_memory("tester", manager=self.manager).items), 0)
        trash = list_core_memory_trash("tester", manager=self.manager)
        self.assertEqual([it.id for it in trash.items], [mid])
        self.assertIsNotNone(trash.items[0].deleted_at)

    def test_delete_missing_404(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            delete_core_memory_item("tester", 999, manager=self.manager)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_double_delete_404(self):
        from fastapi import HTTPException
        mid = self._seed_core_memory("x")
        delete_core_memory_item("tester", mid, manager=self.manager)
        with self.assertRaises(HTTPException) as ctx:
            delete_core_memory_item("tester", mid, manager=self.manager)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_restore_brings_back(self):
        mid = self._seed_core_memory("復元対象")
        delete_core_memory_item("tester", mid, manager=self.manager)
        resp = restore_core_memory_item("tester", mid, manager=self.manager)
        self.assertTrue(resp.ok)
        self.assertEqual([it.id for it in list_core_memory("tester", manager=self.manager).items], [mid])
        self.assertEqual(len(list_core_memory_trash("tester", manager=self.manager).items), 0)

    def test_restore_not_in_trash_404(self):
        from fastapi import HTTPException
        mid = self._seed_core_memory("生存中")  # 削除していない
        with self.assertRaises(HTTPException) as ctx:
            restore_core_memory_item("tester", mid, manager=self.manager)
        self.assertEqual(ctx.exception.status_code, 404)

    # --- 仮想センサー (ユーザー訂正→ペルソナへ event_message 通知) ---

    def _correction_notices(self):
        """挿入された訂正通知 (event_message) を新しい順に返す。"""
        import json
        with self.adapter._db_lock:
            rows = self.adapter.conn.execute(
                "SELECT content, role, line_role, scope, metadata FROM messages "
                "WHERE content LIKE ? ORDER BY created_at DESC, rowid DESC",
                ("%[コア記憶の更新通知]%",),
            ).fetchall()
        out = []
        for content, role, line_role, scope, metadata in rows:
            tags = []
            if metadata:
                try:
                    tags = (json.loads(metadata) or {}).get("tags", [])
                except (ValueError, TypeError):
                    tags = []
            out.append({
                "content": content, "role": role, "line_role": line_role,
                "scope": scope, "tags": tags,
            })
        return out

    def _pending_perceptions(self):
        """知覚バッファの未消費項目 (発生順) を返す。"""
        from sai_memory.perception_buffer import list_pending
        with self.adapter._db_lock:
            return list_pending(self.adapter.conn)

    def _consumed_perceptions(self):
        """知覚台帳の消費済み項目 (消費時刻 → 発生順) を返す。"""
        from sai_memory.perception_buffer import list_consumed_since
        with self.adapter._db_lock:
            return list_consumed_since(self.adapter.conn, 0)

    def _count_flush_messages(self, needle: str) -> int:
        with self.adapter._db_lock:
            row = self.adapter.conn.execute(
                "SELECT COUNT(*) FROM messages WHERE content LIKE ?",
                (f"%{needle}%",),
            ).fetchone()
        return int(row[0])

    # --- 知覚バッファ経由: 訂正は push → Beat 頭の消費 (flush) で消費印 (W14) ---

    def test_edit_pushes_to_buffer_then_flush_marks_consumed(self):
        mid = self._seed_core_memory("旧い本文", confirmed=0)
        req = UpdateCoreMemoryRequest(content="ユーザーが直した新しい本文")
        update_core_memory_item("tester", mid, req, manager=self.manager)

        # 訂正時点ではまだ知覚されない (知覚バッファに溜まるだけ)。
        self.assertEqual(self._correction_notices(), [])
        pending = self._pending_perceptions()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].kind, "core_memory_correction")
        self.assertEqual(pending[0].reduce_key, f"core:{mid}")
        self.assertIn("ユーザーが直した新しい本文", pending[0].content)

        # 消費 (Beat 頭) = 台帳の消費印。messages に event_message 行は作らない
        # (perception_buffer.md §10.1/§10.2 — 提示は時刻順マージが担う)。
        payload = self.adapter.flush_perception_buffer_payload()
        self.assertIsNotNone(payload)
        self.assertIn("ユーザーが直した新しい本文", payload["content"])
        self.assertIn(f"core:{mid}", payload["content"])
        self.assertTrue(payload["content"].startswith("<system>"))
        self.assertEqual(self._correction_notices(), [])   # 行を作らない
        self.assertEqual(self._pending_perceptions(), [])
        consumed = self._consumed_perceptions()
        self.assertEqual(len(consumed), 1)
        self.assertIsNotNone(consumed[0].consumed_at)

    def test_delete_flush_carries_removed_content(self):
        mid = self._seed_core_memory("消される秘密の内容")
        delete_core_memory_item("tester", mid, manager=self.manager)
        payload = self.adapter.flush_perception_buffer_payload()
        self.assertIsNotNone(payload)
        # 削除で head から消えるので、失われた内容を消費本文に載せる
        self.assertIn("消される秘密の内容", payload["content"])
        self.assertIn("削除", payload["content"])

    def test_same_memory_ops_reduce_to_latest(self):
        # 同一コア記憶への複数操作は未消費の間に最新へ集約される (C2)。
        mid = self._seed_core_memory("復元される内容")
        delete_core_memory_item("tester", mid, manager=self.manager)
        restore_core_memory_item("tester", mid, manager=self.manager)
        # バッファには 2 件溜まっているが、reduce_key が同じなので本文は最新 1 件のみ。
        self.assertEqual(len(self._pending_perceptions()), 2)
        payload = self.adapter.flush_perception_buffer_payload()
        self.assertIsNotNone(payload)
        self.assertIn("復元", payload["content"])
        self.assertNotIn("削除しました", payload["content"])
        # reduce で畳まれた分も消費印は打たれる (相殺は未消費の間だけ)。
        self.assertEqual(len(self._consumed_perceptions()), 2)

    def test_flush_empty_buffer_is_noop(self):
        self.assertFalse(self.adapter.flush_perception_buffer())
        self.assertEqual(self._correction_notices(), [])
        self.assertEqual(self._consumed_perceptions(), [])

    def test_flush_payload_returns_message_body_and_media(self):
        # Beat 頭のラウンド途中消費 (sea/runtime_llm.py の spell ループ) 用:
        # 消費した合成メッセージ本体と media が返り、作業中 messages への差し込みと
        # 以後の提示マージが同じ内容になる。
        media = [{"path": "/img/x.png", "mime_type": "image/png", "role": "image"}]
        self.adapter.push_perception("surroundings", "移動先の様子テスト", media=media)
        payload = self.adapter.flush_perception_buffer_payload()
        self.assertIsNotNone(payload)
        self.assertIn("移動先の様子テスト", payload["content"])
        self.assertTrue(payload["content"].startswith("<system>"))
        self.assertEqual(payload["media"], media)
        self.assertEqual(self._pending_perceptions(), [])
        # messages には何も書かれない (§10.1)。
        self.assertEqual(self._count_flush_messages("移動先の様子テスト"), 0)
        # 空バッファでは None (bool ラッパーは False)
        self.assertIsNone(self.adapter.flush_perception_buffer_payload())
        self.assertFalse(self.adapter.flush_perception_buffer())

    def test_flush_records_batch_with_rendered_text(self):
        # 消費は 1 バッチ = 提示の 1 ブロック (§10.2/§10.3)。pulse_id と確定
        # 文面 (reduce → format の結果) はバッチ行が持ち、項目はバッチ id で
        # まとまる (秒精度時刻からグループを再構成しない)。
        from sai_memory.perception_buffer import list_unannexed_batches
        self.adapter.push_perception("world_state", "グループ検証 A")
        self.adapter.push_perception("world_state", "グループ検証 B")
        self.assertTrue(
            self.adapter.flush_perception_buffer(pulse_id="pulse-xyz")
        )
        with self.adapter._db_lock:
            batches = list_unannexed_batches(self.adapter.conn)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0].pulse_id, "pulse-xyz")
        self.assertIn("グループ検証 A", batches[0].rendered_text)
        self.assertIn("グループ検証 B", batches[0].rendered_text)
        # manager を渡していないので episode は NULL。
        self.assertIsNone(batches[0].episode_id)
        consumed = self._consumed_perceptions()
        self.assertEqual(len(consumed), 2)
        self.assertTrue(
            all(it.consumed_batch_id == batches[0].id for it in consumed)
        )

    def test_confirm_does_not_push(self):
        mid = self._seed_core_memory("未確認", confirmed=0)
        confirm_core_memory_item("tester", mid, manager=self.manager)
        # confirm は内容不変なので知覚バッファにも積まれない。
        self.assertEqual(self._pending_perceptions(), [])
        self.adapter.flush_perception_buffer()
        self.assertEqual(self._correction_notices(), [])

    # ------------------------------------------------------------------
    # SEA 監査 S5 (W14 改訂): 消費印 (UPDATE) が打てなければ pending 保持
    # ------------------------------------------------------------------

    def test_flush_keeps_pending_when_batch_tx_fails(self):
        self.adapter.push_perception("world_state", "S5検証: 世界が変わった")
        with patch(
            "sai_memory.perception_buffer.create_consumption_batch",
            side_effect=RuntimeError("db down"),
        ):
            self.assertFalse(self.adapter.flush_perception_buffer())
        # 消費不成立 = pending 無傷・消費印なし (知覚を落とさない)。
        self.assertEqual(len(self._pending_perceptions()), 1)
        self.assertEqual(self._consumed_perceptions(), [])
        # 障害が解ければ次 Beat の flush で一度だけ消費される。
        self.assertTrue(self.adapter.flush_perception_buffer())
        self.assertEqual(self._pending_perceptions(), [])
        self.assertEqual(len(self._consumed_perceptions()), 1)
        # 再 flush しても二重にならない (消費済みは対象外)。
        self.assertFalse(self.adapter.flush_perception_buffer())
        self.assertEqual(len(self._consumed_perceptions()), 1)

    # ------------------------------------------------------------------
    # C6 退役 (§10.7): 消費が単一 tx になり、二度書きが構造的に不可能なこと
    # ------------------------------------------------------------------

    def test_consumption_is_single_transaction_no_double_write(self):
        # 旧 C6 は「行を書く → 削除」の二段 commit の隙間を照合で塞いでいた。
        # 新経路は消費 = バッチ確定の単一 tx だけなので、「書けたのに残った」
        # 中間状態が存在しない: 消費済み行の再消費は tx ごと拒否され、
        # messages への書き込み自体が無い。
        from sai_memory.perception_buffer import (
            create_consumption_batch,
            list_unannexed_batches,
        )
        self.adapter.push_perception("world_state", "単一tx検証")
        self.assertTrue(self.adapter.flush_perception_buffer())
        consumed = self._consumed_perceptions()
        self.assertEqual(len(consumed), 1)
        first_stamp = consumed[0].consumed_at

        # 再 flush は no-op — バッチは増えない。
        self.assertFalse(self.adapter.flush_perception_buffer())
        consumed = self._consumed_perceptions()
        self.assertEqual(len(consumed), 1)
        self.assertEqual(consumed[0].consumed_at, first_stamp)
        with self.adapter._db_lock:
            self.assertEqual(len(list_unannexed_batches(self.adapter.conn)), 1)

        # 消費済み id での再消費は tx ごと拒否される。
        with self.adapter._db_lock:
            with self.assertRaises(ValueError):
                create_consumption_batch(
                    self.adapter.conn, [consumed[0].id],
                    consumed_at=9999999999, rendered_text="x",
                )
            self.assertEqual(len(list_unannexed_batches(self.adapter.conn)), 1)
        # messages には最初から何も書かれていない。
        self.assertEqual(self._count_flush_messages("単一tx検証"), 0)

    def test_flush_after_failed_flush_still_delivers_new_perceptions(self):
        # 失敗回の残りが新しい知覚を巻き添えにしない。
        self.adapter.push_perception("world_state", "中断分: 未消費のまま残る知覚")
        with patch(
            "sai_memory.perception_buffer.create_consumption_batch",
            side_effect=RuntimeError("db down"),
        ):
            self.assertFalse(self.adapter.flush_perception_buffer())
        self.adapter.push_perception("world_state", "新着分: 後から届いた知覚")
        self.assertEqual(len(self._pending_perceptions()), 2)

        payload = self.adapter.flush_perception_buffer_payload()
        self.assertIsNotNone(payload)
        self.assertIn("中断分", payload["content"])
        self.assertIn("新着分", payload["content"])
        self.assertEqual(self._pending_perceptions(), [])
        self.assertEqual(len(self._consumed_perceptions()), 2)


if __name__ == "__main__":
    unittest.main()
