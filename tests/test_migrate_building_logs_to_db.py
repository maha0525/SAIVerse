"""scripts/migrate_building_logs_to_db.py + saiverse/legacy_log_import.py の振る舞いテスト。

- 一時 saiverse_home + 一時 SQLite で取り込み動作を確認
- 全件保存 (重複 seq があっても落とさない)
- legacy_seq / legacy_message_id の保存
- AddonMessageMetadata 一括 UPDATE
- 冪等性 (2 回実行で重複 INSERT しない、 building 単位 skip)
- 0 バイト / JSON 異常 / 構造異常 のスキップ
- 隔離マーカー (log.json.corrupted_*) が残っていても現物が健全なら取り込む
  (2026-08-16 テスタロッサの部屋の取り込み漏れの再発防止)
- 取り込み痕跡なしで通常経路の行がある building は skip (順序保護)
- 起動時の検算 scan_legacy_log_deficits の判定
"""
from __future__ import annotations

import gc
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import Base, BuildingMessage, AddonMessageMetadata


class MigrateBuildingLogsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._home_tmp = tempfile.TemporaryDirectory(prefix="saiverse_home_")
        self.home_path = Path(self._home_tmp.name)
        self.addCleanup(self._home_tmp.cleanup)

        self._db_tmp = tempfile.TemporaryDirectory(prefix="saiverse_db_")
        self.addCleanup(self._cleanup_db_dir)
        self.db_path = Path(self._db_tmp.name) / "saiverse.db"

        self.engine = create_engine(f"sqlite:///{self.db_path}")
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)
        self.addCleanup(self.engine.dispose)

        self.script_module = importlib.import_module("scripts.migrate_building_logs_to_db")
        importlib.reload(self.script_module)
        self._patches = [
            patch.object(self.script_module, "SessionLocal", self.SessionLocal),
            patch.object(self.script_module, "get_saiverse_home", lambda: self.home_path),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(patch.stopall)

    def _cleanup_db_dir(self) -> None:
        try:
            self.engine.dispose()
        except Exception:
            pass
        gc.collect()
        try:
            self._db_tmp.cleanup()
        except PermissionError:
            pass

    def _make_building_log(self, city: str, building_id: str, messages: list) -> Path:
        b_dir = self.home_path / "cities" / city / "buildings" / building_id
        b_dir.mkdir(parents=True, exist_ok=True)
        path = b_dir / "log.json"
        path.write_text(json.dumps(messages, ensure_ascii=False), encoding="utf-8")
        return path

    def _run_main(self, argv: list) -> int:
        old_argv = sys.argv
        sys.argv = ["migrate_building_logs_to_db.py", *argv]
        try:
            return self.script_module.main()
        finally:
            sys.argv = old_argv

    def _all_db_messages(self):
        db = self.SessionLocal()
        try:
            return db.query(BuildingMessage).order_by(
                BuildingMessage.building_id, BuildingMessage.seq
            ).all()
        finally:
            db.close()

    # ------------------------------------------------------------------
    # 基本: 全件保存 + 連番採番 + legacy_* 保存
    # ------------------------------------------------------------------

    def test_imports_all_messages_with_renumbered_seq(self) -> None:
        self._make_building_log(
            "city_a",
            "room1",
            [
                {"role": "user", "content": "hi", "seq": 100, "message_id": "room1:100",
                 "timestamp": "2026-05-20T10:00:00", "heard_by": ["a"]},
                {"role": "assistant", "content": "yo", "seq": 101, "message_id": "room1:101",
                 "persona_id": "air", "timestamp": "2026-05-20T10:00:01", "heard_by": ["a"]},
            ],
        )
        self._run_main([])
        rows = self._all_db_messages()
        self.assertEqual(len(rows), 2)
        # 新 seq は 1, 2 で連番採番
        self.assertEqual(rows[0].seq, 1)
        self.assertEqual(rows[1].seq, 2)
        self.assertEqual(rows[0].message_id, "room1:1")
        self.assertEqual(rows[1].message_id, "room1:2")
        # legacy_* に元情報
        self.assertEqual(rows[0].legacy_seq, 100)
        self.assertEqual(rows[1].legacy_seq, 101)
        self.assertEqual(rows[0].legacy_message_id, "room1:100")
        self.assertEqual(rows[1].legacy_message_id, "room1:101")

    def test_duplicate_seq_preserved_as_separate_rows(self) -> None:
        """同じ seq の行が複数あっても全件 INSERT。 重複 skip は無し。"""
        self._make_building_log(
            "city_a",
            "room1",
            [
                {"role": "host", "content": "<div>moved</div>", "seq": 1,
                 "message_id": "room1:1", "heard_by": []},
                {"role": "user", "content": "こんにちは", "seq": 1,
                 "message_id": "room1:1", "timestamp": "2026-05-20T10:00:00",
                 "heard_by": []},
            ],
        )
        self._run_main([])
        rows = self._all_db_messages()
        self.assertEqual(len(rows), 2)
        # 両方とも残る (新 seq=1, 2)
        contents = {r.content for r in rows}
        self.assertIn("<div>moved</div>", contents)
        self.assertIn("こんにちは", contents)
        # legacy_message_id は両方とも "room1:1"
        self.assertEqual(rows[0].legacy_message_id, "room1:1")
        self.assertEqual(rows[1].legacy_message_id, "room1:1")

    # ------------------------------------------------------------------
    # AddonMessageMetadata 一括 UPDATE
    # ------------------------------------------------------------------

    def test_addon_metadata_message_id_remapped(self) -> None:
        # 旧 message_id を持つ AddonMessageMetadata を先に作る
        db = self.SessionLocal()
        try:
            db.add(AddonMessageMetadata(
                message_id="room1:100", addon_name="voice-tts",
                key="audio_path", value="/path/to/audio.ogg",
            ))
            db.commit()
        finally:
            db.close()

        # log.json: 旧 seq=100 のメッセージ。 新 seq は 1 になる
        self._make_building_log(
            "city_a",
            "room1",
            [
                {"role": "assistant", "content": "yo", "seq": 100,
                 "message_id": "room1:100", "persona_id": "air",
                 "timestamp": "2026-05-20T10:00:00", "heard_by": []},
            ],
        )
        self._run_main([])

        # AddonMessageMetadata.message_id が新 ID にリマップされている
        db = self.SessionLocal()
        try:
            rows = db.query(AddonMessageMetadata).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].message_id, "room1:1")
            self.assertEqual(rows[0].value, "/path/to/audio.ogg")
        finally:
            db.close()

    def test_addon_metadata_duplicate_legacy_maps_to_first_new_id(self) -> None:
        """重複 legacy_message_id があった場合、 AddonMessageMetadata は最初の新 ID にマップ。"""
        db = self.SessionLocal()
        try:
            db.add(AddonMessageMetadata(
                message_id="room1:5", addon_name="voice-tts",
                key="audio_path", value="/legacy.ogg",
            ))
            db.commit()
        finally:
            db.close()

        self._make_building_log(
            "city_a",
            "room1",
            [
                # 同じ legacy message_id を持つ行が 2 つ (旧 race 痕跡)
                {"role": "host", "content": "evt", "seq": 5,
                 "message_id": "room1:5", "heard_by": []},
                {"role": "assistant", "content": "reply", "seq": 5,
                 "message_id": "room1:5", "persona_id": "air",
                 "timestamp": "2026-05-20T10:00:00", "heard_by": []},
            ],
        )
        self._run_main([])
        # 新 seq=1 が最初に出会った行 → AddonMessageMetadata は room1:1 へ
        db = self.SessionLocal()
        try:
            rows = db.query(AddonMessageMetadata).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].message_id, "room1:1")
        finally:
            db.close()

    # ------------------------------------------------------------------
    # 冪等性
    # ------------------------------------------------------------------

    def test_idempotent_on_rerun_skips_already_migrated_building(self) -> None:
        self._make_building_log(
            "city_a",
            "room1",
            [
                {"role": "user", "content": "hi", "seq": 1, "message_id": "room1:1",
                 "timestamp": "2026-05-20T10:00:00", "heard_by": []},
            ],
        )
        self._run_main([])
        self._run_main([])  # 2 回目
        rows = self._all_db_messages()
        self.assertEqual(len(rows), 1)

    def test_dry_run_does_not_insert(self) -> None:
        self._make_building_log(
            "city_a",
            "room1",
            [
                {"role": "user", "content": "hi", "seq": 1, "message_id": "room1:1",
                 "timestamp": "2026-05-20T10:00:00", "heard_by": []},
            ],
        )
        self._run_main(["--dry-run"])
        self.assertEqual(len(self._all_db_messages()), 0)

    # ------------------------------------------------------------------
    # エッジケース: 不正ファイル / 隔離
    # ------------------------------------------------------------------

    def test_zero_byte_log_skipped(self) -> None:
        b_dir = self.home_path / "cities" / "city_a" / "buildings" / "broken"
        b_dir.mkdir(parents=True, exist_ok=True)
        (b_dir / "log.json").write_text("", encoding="utf-8")
        rc = self._run_main([])
        self.assertEqual(rc, 0)
        self.assertEqual(len(self._all_db_messages()), 0)

    def test_invalid_json_skipped(self) -> None:
        b_dir = self.home_path / "cities" / "city_a" / "buildings" / "broken"
        b_dir.mkdir(parents=True, exist_ok=True)
        (b_dir / "log.json").write_text("{not json", encoding="utf-8")
        rc = self._run_main([])
        self.assertEqual(rc, 0)
        self.assertEqual(len(self._all_db_messages()), 0)

    def test_non_list_root_skipped(self) -> None:
        b_dir = self.home_path / "cities" / "city_a" / "buildings" / "broken"
        b_dir.mkdir(parents=True, exist_ok=True)
        (b_dir / "log.json").write_text('{"role": "user"}', encoding="utf-8")
        rc = self._run_main([])
        self.assertEqual(rc, 0)
        self.assertEqual(len(self._all_db_messages()), 0)

    def test_corrupted_marker_does_not_block_import(self) -> None:
        """隔離マーカー (log.json.corrupted_*) が残っていても、現物の log.json が
        健全なら取り込む。マーカーは事故時の退避物で、修復後の現物の健全性に
        ついて何も語らない — マーカーの有無でスキップした結果、修復済みの部屋の
        全履歴が移行から漏れた (2026-08-16 テスタロッサの部屋)。"""
        path = self._make_building_log(
            "city_a",
            "room_q",
            [{"role": "user", "content": "hi", "seq": 1, "message_id": "room_q:1",
              "timestamp": "2026-05-20T10:00:00", "heard_by": []}],
        )
        (path.parent / "log.json.corrupted_20260519_120000").write_text("rescued", encoding="utf-8")
        rc = self._run_main([])
        self.assertEqual(rc, 0)
        rows = self._all_db_messages()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].content, "hi")

    def test_live_rows_without_import_trace_skipped(self) -> None:
        """取り込み痕跡 (legacy_seq) が無いのに通常経路の行がある building は、
        過去ログを後付けすると順序が壊れるため取り込まない。"""
        db = self.SessionLocal()
        try:
            db.add(BuildingMessage(
                building_id="room_live", seq=1, role="user", content="live msg",
                timestamp="2026-06-01T10:00:00", heard_by="[]", ingested_by="[]",
                message_id="room_live:1",
            ))
            db.commit()
        finally:
            db.close()

        self._make_building_log(
            "city_a", "room_live",
            [{"role": "user", "content": "old msg", "seq": 1, "message_id": "room_live:1",
              "timestamp": "2026-05-01T10:00:00", "heard_by": []}],
        )
        self._run_main([])
        rows = self._all_db_messages()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].content, "live msg")
        self.assertIsNone(rows[0].legacy_seq)

    def test_messages_without_seq_still_imported_with_null_legacy_seq(self) -> None:
        """seq なしの行も全件取り込む (新規連番採番)。 legacy_seq は NULL。"""
        self._make_building_log(
            "city_a",
            "room1",
            [
                {"role": "host", "content": "<note>upload</note>",
                 "timestamp": "2026-05-20T10:00:00"},
                {"role": "user", "content": "hi", "seq": 1, "message_id": "room1:1",
                 "timestamp": "2026-05-20T10:00:01", "heard_by": []},
            ],
        )
        self._run_main([])
        rows = self._all_db_messages()
        self.assertEqual(len(rows), 2)
        # 最初の行 (seq なし) は legacy_seq=NULL、 新 seq=1
        self.assertEqual(rows[0].seq, 1)
        self.assertIsNone(rows[0].legacy_seq)
        self.assertIsNone(rows[0].legacy_message_id)
        # 2 つ目は legacy_seq=1, 新 seq=2
        self.assertEqual(rows[1].seq, 2)
        self.assertEqual(rows[1].legacy_seq, 1)

    def test_addon_metadata_two_phase_handles_swapped_message_ids(self) -> None:
        """二段階 UPDATE: 旧 seq=A と 旧 seq=B が new で互いの値に置き換わるケース。

        旧 log.json:
          - seq=10 行 → 新 seq=1 (= "room:1")
          - seq=11 行 → 新 seq=2 (= "room:2")
        AddonMessageMetadata:
          - id=A: message_id="room:10" → 新 "room:1" に動かしたい
          - id=B: message_id="room:11" → 新 "room:2" に動かしたい
        逐次 UPDATE では衝突しないが、 もし旧 log で逆順 (seq=11 行が先、 seq=10 行が後)
        だと: 旧 seq=11 → 新 1、 旧 seq=10 → 新 2 となる。 これでも問題なく完了することを確認。
        """
        db = self.SessionLocal()
        try:
            db.add(AddonMessageMetadata(
                message_id="room1:10", addon_name="voice", key="audio_path", value="v_old_10",
            ))
            db.add(AddonMessageMetadata(
                message_id="room1:11", addon_name="voice", key="audio_path", value="v_old_11",
            ))
            db.commit()
        finally:
            db.close()

        # log.json: 順序は seq=11 → seq=10 (逆順)。 新採番では順に 1, 2
        self._make_building_log(
            "city_a", "room1",
            [
                {"role": "assistant", "content": "x", "seq": 11, "message_id": "room1:11",
                 "persona_id": "p", "timestamp": "2026-05-20T10:00:00", "heard_by": []},
                {"role": "assistant", "content": "y", "seq": 10, "message_id": "room1:10",
                 "persona_id": "p", "timestamp": "2026-05-20T10:00:01", "heard_by": []},
            ],
        )
        self._run_main([])

        # AddonMessageMetadata: 旧 11 → 新 1、 旧 10 → 新 2
        db = self.SessionLocal()
        try:
            rows = db.query(AddonMessageMetadata).order_by(AddonMessageMetadata.value).all()
            self.assertEqual(len(rows), 2)
            by_value = {r.value: r.message_id for r in rows}
            self.assertEqual(by_value["v_old_10"], "room1:2")
            self.assertEqual(by_value["v_old_11"], "room1:1")
        finally:
            db.close()

    def test_addon_metadata_conflict_with_non_migrating_row_skipped(self) -> None:
        """target に migration 対象外の既存行があれば、 migration 対象を捨てて既存を尊重する。

        既存:
          - id=A: message_id="room1:1", addon=x, key=y  (= migration 対象外, 元から存在)
          - id=B: message_id="room1:旧A", addon=x, key=y  (= migration 対象、 → "room1:1" に動かしたい)
        log.json: 1 行だけ (seq=1, message_id="room1:旧A") → 新 message_id="room1:1"
        結果: id=A は不変、 id=B は削除される (= 既存を尊重)
        """
        db = self.SessionLocal()
        try:
            db.add(AddonMessageMetadata(
                id=100,
                message_id="room1:1", addon_name="x", key="y", value="existing",
            ))
            db.add(AddonMessageMetadata(
                id=200,
                message_id="room1:旧A", addon_name="x", key="y", value="to_be_dropped",
            ))
            db.commit()
        finally:
            db.close()

        self._make_building_log(
            "city_a", "room1",
            [
                {"role": "user", "content": "hi", "seq": 99, "message_id": "room1:旧A",
                 "timestamp": "2026-05-20T10:00:00", "heard_by": []},
            ],
        )
        self._run_main([])

        db = self.SessionLocal()
        try:
            rows = db.query(AddonMessageMetadata).order_by(AddonMessageMetadata.id).all()
            # id=100 (既存 room1:1) は残る、 id=200 (migration 対象) は衝突で削除
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].id, 100)
            self.assertEqual(rows[0].value, "existing")
        finally:
            db.close()

    def test_rerun_after_building_messages_already_imported_still_updates_metadata(self) -> None:
        """再実行時 (= 全 building が既取り込みで skip) でも、 既存 building_messages から
        legacy → new マッピングを再構築して AddonMessageMetadata UPDATE が走る。
        """
        self._make_building_log(
            "city_a", "room1",
            [
                {"role": "assistant", "content": "x", "seq": 50, "message_id": "room1:50",
                 "persona_id": "p", "timestamp": "2026-05-20T10:00:00", "heard_by": []},
            ],
        )
        # 1 回目: building_messages 取り込み、 AddonMessageMetadata はまだない
        self._run_main([])

        # AddonMessageMetadata を後付けで足す (旧 message_id を持つ)
        db = self.SessionLocal()
        try:
            db.add(AddonMessageMetadata(
                message_id="room1:50", addon_name="v", key="audio", value="late",
            ))
            db.commit()
        finally:
            db.close()

        # 2 回目: building_messages は skip されるが map 再構築 → AddonMessageMetadata UPDATE
        self._run_main([])
        db = self.SessionLocal()
        try:
            rows = db.query(AddonMessageMetadata).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].message_id, "room1:1")  # 新採番=1
        finally:
            db.close()

    def test_city_filter(self) -> None:
        self._make_building_log(
            "city_a", "room1",
            [{"role": "user", "content": "a", "seq": 1, "message_id": "room1:1",
              "timestamp": "2026-05-20T10:00:00", "heard_by": []}],
        )
        self._make_building_log(
            "city_b", "room2",
            [{"role": "user", "content": "b", "seq": 1, "message_id": "room2:1",
              "timestamp": "2026-05-20T10:00:00", "heard_by": []}],
        )
        self._run_main(["--city", "city_a"])
        rows = self._all_db_messages()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].content, "a")

    # ------------------------------------------------------------------
    # 取り込み時の cursor 前進 (過去ログを「未読の新着」に化けさせない)
    # ------------------------------------------------------------------

    def test_import_advances_existing_cursor_rows(self) -> None:
        """cursor=0 の既存行がある部屋へ取り込むと、cursor が取り込み末尾へ進む。
        進めないと次の Pulse が取り込んだ全過去ログを未読として消化しに行く。"""
        from database.models import PersonaPulseCursor
        db = self.SessionLocal()
        try:
            db.add(PersonaPulseCursor(
                PERSONA_ID="p1", BUILDING_ID="room1", CURSOR_SEQ=0, ENTRY_MARKER_SEQ=0,
            ))
            db.commit()
        finally:
            db.close()

        self._make_building_log(
            "city_a", "room1",
            [
                {"role": "user", "content": "a", "seq": 1, "message_id": "room1:1",
                 "timestamp": "2026-05-20T10:00:00", "heard_by": []},
                {"role": "user", "content": "b", "seq": 2, "message_id": "room1:2",
                 "timestamp": "2026-05-20T10:00:01", "heard_by": []},
            ],
        )
        self._run_main([])

        db = self.SessionLocal()
        try:
            row = db.query(PersonaPulseCursor).filter_by(
                PERSONA_ID="p1", BUILDING_ID="room1"
            ).one()
            self.assertEqual(row.CURSOR_SEQ, 2)
            self.assertEqual(row.ENTRY_MARKER_SEQ, 2)
        finally:
            db.close()

    def test_import_does_not_create_cursor_rows(self) -> None:
        """cursor 行が無い環境 (リリース版ユーザーの経路) では行を作らない —
        位置は後続の conscious_log 取り込みが決める。"""
        from database.models import PersonaPulseCursor
        self._make_building_log(
            "city_a", "room1",
            [{"role": "user", "content": "a", "seq": 1, "message_id": "room1:1",
              "timestamp": "2026-05-20T10:00:00", "heard_by": []}],
        )
        self._run_main([])
        db = self.SessionLocal()
        try:
            self.assertEqual(db.query(PersonaPulseCursor).count(), 0)
        finally:
            db.close()

    # ------------------------------------------------------------------
    # 起動時の検算: scan_legacy_log_deficits
    # ------------------------------------------------------------------

    def _scan(self, building_ids: list) -> list:
        from saiverse.legacy_log_import import scan_legacy_log_deficits
        db = self.SessionLocal()
        try:
            return scan_legacy_log_deficits(db, self.home_path, "city_a", building_ids)
        finally:
            db.close()

    def test_scan_reports_not_imported_building(self) -> None:
        self._make_building_log(
            "city_a", "room1",
            [{"role": "user", "content": "hi", "seq": 1, "message_id": "room1:1",
              "timestamp": "2026-05-20T10:00:00", "heard_by": []}],
        )
        deficits = self._scan(["room1"])
        self.assertEqual(len(deficits), 1)
        self.assertEqual(deficits[0]["building_id"], "room1")
        self.assertEqual(deficits[0]["kind"], "not_imported")
        self.assertEqual(deficits[0]["file_entries"], 1)

    def test_scan_clean_after_import(self) -> None:
        self._make_building_log(
            "city_a", "room1",
            [{"role": "user", "content": "hi", "seq": 1, "message_id": "room1:1",
              "timestamp": "2026-05-20T10:00:00", "heard_by": []}],
        )
        self._run_main([])
        self.assertEqual(self._scan(["room1"]), [])

    def test_scan_reports_live_rows_only(self) -> None:
        """ファイル時代のメッセージ ID が DB のどの行にも無い部屋 (saiverse_navi 型)。"""
        db = self.SessionLocal()
        try:
            db.add(BuildingMessage(
                building_id="room1", seq=1, role="user", content="live",
                timestamp="2026-06-01T10:00:00", heard_by="[]", ingested_by="[]",
                message_id="room1:1",
            ))
            db.commit()
        finally:
            db.close()
        self._make_building_log(
            "city_a", "room1",
            [{"role": "user", "content": "old", "seq": 7, "message_id": "room1:old7",
              "timestamp": "2026-05-01T10:00:00", "heard_by": []}],
        )
        deficits = self._scan(["room1"])
        self.assertEqual(len(deficits), 1)
        self.assertEqual(deficits[0]["kind"], "live_rows_only")
        self.assertEqual(deficits[0]["missing"], 1)

    def test_scan_silent_for_dual_written_entries(self) -> None:
        """dual-write 期の環境: ファイル末尾の行が「通常経路の行」として DB に
        居る場合、件数は食い違っても欠けではない — 警告を出さない
        (本番データで 6 部屋の偽陽性を出した実測からの回帰固定)。"""
        db = self.SessionLocal()
        try:
            db.add(BuildingMessage(
                building_id="room1", seq=1, role="user", content="dual",
                timestamp="2026-06-01T10:00:00", heard_by="[]", ingested_by="[]",
                message_id="room1:1",
            ))
            db.commit()
        finally:
            db.close()
        self._make_building_log(
            "city_a", "room1",
            [{"role": "user", "content": "dual", "seq": 1, "message_id": "room1:1",
              "timestamp": "2026-06-01T10:00:00", "heard_by": []}],
        )
        self.assertEqual(self._scan(["room1"]), [])

    def test_scan_reports_partial_when_imported_room_misses_entries(self) -> None:
        """取り込み痕跡はあるが、ファイルにだけ居るメッセージが残っている部屋。"""
        self._make_building_log(
            "city_a", "room1",
            [{"role": "user", "content": "a", "seq": 1, "message_id": "room1:1",
              "timestamp": "2026-05-20T10:00:00", "heard_by": []}],
        )
        self._run_main([])
        # 取り込み後、ファイルにだけ存在する行を追加 (DB に無い ID)
        self._make_building_log(
            "city_a", "room1",
            [
                {"role": "user", "content": "a", "seq": 1, "message_id": "room1:1",
                 "timestamp": "2026-05-20T10:00:00", "heard_by": []},
                {"role": "user", "content": "lost", "seq": 9, "message_id": "room1:lost9",
                 "timestamp": "2026-05-20T10:00:01", "heard_by": []},
            ],
        )
        deficits = self._scan(["room1"])
        self.assertEqual(len(deficits), 1)
        self.assertEqual(deficits[0]["kind"], "partial")
        self.assertEqual(deficits[0]["missing"], 1)

    def test_scan_reports_unreadable_file(self) -> None:
        b_dir = self.home_path / "cities" / "city_a" / "buildings" / "room1"
        b_dir.mkdir(parents=True, exist_ok=True)
        (b_dir / "log.json").write_text("{broken", encoding="utf-8")
        deficits = self._scan(["room1"])
        self.assertEqual(len(deficits), 1)
        self.assertEqual(deficits[0]["kind"], "unreadable")

    def test_scan_silent_for_no_id_entries_when_db_has_rows(self) -> None:
        """message_id を持たないファイル行 (旧形式 host イベント) は、DB に行が
        あれば dual-write 済みとみなして欠け扱いしない (本番 2 部屋で偽陽性を
        出した実測からの回帰固定)。"""
        db = self.SessionLocal()
        try:
            db.add(BuildingMessage(
                building_id="room1", seq=1, role="host", content="moved",
                timestamp="", heard_by="[]", ingested_by="[]",
                message_id="room1:1",
            ))
            db.commit()
        finally:
            db.close()
        self._make_building_log(
            "city_a", "room1",
            [{"role": "host", "content": "moved", "heard_by": []}],  # message_id なし
        )
        self.assertEqual(self._scan(["room1"]), [])

    def test_scan_counts_no_id_entries_when_db_is_empty(self) -> None:
        """DB に行が 1 つも無い部屋では、ID 無しのファイル行も欠けと数える
        (ファイルが唯一の写し)。"""
        self._make_building_log(
            "city_a", "room1",
            [{"role": "host", "content": "moved", "heard_by": []}],
        )
        deficits = self._scan(["room1"])
        self.assertEqual(len(deficits), 1)
        self.assertEqual(deficits[0]["kind"], "not_imported")
        self.assertEqual(deficits[0]["missing"], 1)

    def test_scan_silent_for_building_without_legacy_file(self) -> None:
        """Phase 2+3 以降に作られた部屋 (log.json 無し) は検算の対象外。"""
        self.assertEqual(self._scan(["new_room"]), [])

    def test_scan_silent_for_empty_legacy_file(self) -> None:
        self._make_building_log("city_a", "room1", [])
        self.assertEqual(self._scan(["room1"]), [])


if __name__ == "__main__":
    unittest.main()
