"""会話履歴ファイル(log.json)の安全性テスト。

このテストは複数の連鎖的データロス事故を防ぐ:

1. **アトミック書き込み**: ``_save_building_histories`` が ``tempfile +
   os.replace`` で書き込むこと。プロセスが書き込み中に強制終了されても
   既存のlog.jsonが部分的な内容で破損しないことを保証する。

2. **破損ファイルの退避と隔離**: ``_init_building_histories`` がJSONデコード
   失敗・0バイト・配列以外などの異常ファイルを ``log.json.corrupted_<ts>``
   に退避し、``quarantined_buildings`` に登録、``building_histories`` には
   キーを入れない。これにより以降のsaveで「空配列で上書き」が起きない。

3. **modified_buildings 駆動のsave**: 引数なし呼び出しを廃止し、明示的に
   変更があったビルディングだけ書き込む。in-memory dict にキーが無い
   ビルディングのファイルは絶対に触らない。

4. **起動時バックアップスナップショット**: 正常ロード時に
   ``log.json.backup_<ts>.bak`` を作成、最新N個まで保持。

歴史的経緯: 2026-04-26 に上記の不具合が連鎖し、24個のビルディングログが
``[]`` で上書きされる事件が発生した。
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from manager.history import HistoryMixin, list_log_backups, create_log_backup_snapshot
from manager.initialization import InitializationMixin


class _SaveStub(HistoryMixin):
    """HistoryMixin の _save_building_histories を呼ぶための最小スタブ。"""

    def __init__(self, building_memory_paths, building_histories, quarantined_buildings=None):
        self.building_memory_paths = building_memory_paths
        self.building_histories = building_histories
        self.quarantined_buildings = quarantined_buildings if quarantined_buildings is not None else {}
        self.modified_buildings = set()


class _InitStub(InitializationMixin):
    """InitializationMixin の _init_building_histories を呼ぶための最小スタブ。"""

    def __init__(self, building_memory_paths):
        self.building_memory_paths = building_memory_paths
        self.startup_alerts = []
        self.quarantined_buildings = {}


class AtomicSaveTests(unittest.TestCase):
    """_save_building_histories のアトミック書き込み + 安全な対象選別の検証。"""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _make_stub(self, building_ids, quarantined=None):
        paths = {bid: self.tmp_dir / bid / "log.json" for bid in building_ids}
        histories = {bid: [{"role": "user", "content": f"hello {bid}"}] for bid in building_ids}
        return _SaveStub(paths, histories, quarantined_buildings=quarantined), paths

    def test_save_writes_correct_content(self):
        stub, paths = self._make_stub(["b1", "b2"])
        stub._save_building_histories(["b1", "b2"])
        for bid, path in paths.items():
            self.assertTrue(path.exists(), f"{path} should exist")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data, [{"role": "user", "content": f"hello {bid}"}])

    def test_save_overwrites_existing_file_via_replace(self):
        stub, paths = self._make_stub(["b1"])
        path = paths["b1"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('[{"role":"old","content":"old"}]', encoding="utf-8")
        stub._save_building_histories(["b1"])
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data, [{"role": "user", "content": "hello b1"}])

    def test_save_failure_preserves_original(self):
        """os.replace 失敗時、既存ファイルは元の内容のまま残る + tmp掃除。"""
        stub, paths = self._make_stub(["b1"])
        path = paths["b1"]
        path.parent.mkdir(parents=True, exist_ok=True)
        original = '[{"role":"original","content":"safe"}]'
        path.write_text(original, encoding="utf-8")

        with patch("manager.history.os.replace", side_effect=OSError("simulated failure")):
            with self.assertRaises(OSError):
                stub._save_building_histories(["b1"])

        self.assertEqual(path.read_text(encoding="utf-8"), original)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        self.assertFalse(tmp_path.exists())

    def test_save_skips_quarantined_buildings(self):
        """隔離中ビルディングは書き込み対象から除外される。"""
        stub, paths = self._make_stub(
            ["b1", "b2"],
            quarantined={"b2": {"reason": "corrupted"}},
        )
        path_b2 = paths["b2"]
        path_b2.parent.mkdir(parents=True, exist_ok=True)
        path_b2.write_text("garbage that we must not overwrite", encoding="utf-8")

        stub._save_building_histories(["b1", "b2"])

        self.assertTrue(paths["b1"].exists())
        # b2 は隔離中なので元の内容のまま (上書きされていない)
        self.assertEqual(path_b2.read_text(encoding="utf-8"), "garbage that we must not overwrite")

    def test_save_skips_missing_keys(self):
        """in-memory dict にキーが無いビルディングは絶対に上書きしない。

        旧コードの ``.get(b_id, [])`` フォールバックは、キーが無ければ
        ディスクの正本を [] で上書きしていた (24件同時消失事故の主因)。
        新コードはキーが無ければ書き込みをskip。ディスクの正本を守る。
        """
        paths = {"b1": self.tmp_dir / "b1" / "log.json", "b2": self.tmp_dir / "b2" / "log.json"}
        histories = {"b1": [{"role": "user", "content": "hello b1"}]}  # b2 のキーは無い
        stub = _SaveStub(paths, histories)

        # b2 のディスクには絶対に触ってほしくないデータを置いておく
        paths["b2"].parent.mkdir(parents=True, exist_ok=True)
        paths["b2"].write_text(
            '[{"role":"sacred","content":"please do not destroy"}]', encoding="utf-8",
        )
        b2_before = paths["b2"].read_text(encoding="utf-8")

        stub._save_building_histories(["b1", "b2"])

        self.assertTrue(paths["b1"].exists())
        # b2 は変更なし
        self.assertEqual(paths["b2"].read_text(encoding="utf-8"), b2_before)

    def test_save_unknown_building_id_ignored(self):
        stub, paths = self._make_stub(["b1"])
        stub._save_building_histories(["nonexistent"])
        self.assertFalse(paths["b1"].exists())  # b1 も触れない (要求されてない)

    def test_save_empty_building_ids_is_noop(self):
        stub, paths = self._make_stub(["b1"])
        stub._save_building_histories([])
        self.assertFalse(paths["b1"].exists())

    def test_save_modified_buildings_drains_set(self):
        """_save_modified_buildings がセットを空にすることの検証。"""
        stub, paths = self._make_stub(["b1", "b2"])
        stub.modified_buildings.update({"b1", "b2"})
        stub._save_modified_buildings()
        self.assertTrue(paths["b1"].exists())
        self.assertTrue(paths["b2"].exists())
        self.assertEqual(stub.modified_buildings, set())


class FiveStateLoadTests(unittest.TestCase):
    """_init_building_histories の5状態 (不在/0バイト/空配列/正常/破損) の検証。

    各状態は別々の動作を取る:
      - 不在: dictにキーを入れる ([])
      - 0バイト: 退避＋隔離。dictにキー入れない
      - 空配列: dictにキーを入れる ([])
      - 正常: dictにロード結果を入れる
      - 破損: 退避＋隔離。dictにキー入れない
      - 配列以外 (dict/null等): 退避＋隔離。dictにキー入れない
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _path_for(self, bid):
        return self.tmp_dir / bid / "log.json"

    def _stub_with(self, building_ids):
        paths = {bid: self._path_for(bid) for bid in building_ids}
        return _InitStub(paths), paths

    # State 1: 不在
    def test_missing_file_starts_empty_no_alert(self):
        stub, _ = self._stub_with(["b_new"])
        stub._init_building_histories()
        self.assertEqual(stub.building_histories["b_new"], [])
        self.assertEqual(stub.startup_alerts, [])
        self.assertEqual(stub.quarantined_buildings, {})

    # State 2: 0バイト
    def test_zero_byte_file_quarantined(self):
        stub, paths = self._stub_with(["b1"])
        path = paths["b1"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")  # 0バイト

        stub._init_building_histories()

        self.assertNotIn("b1", stub.building_histories, "quarantined building must NOT have key in histories")
        self.assertIn("b1", stub.quarantined_buildings)
        self.assertEqual(stub.quarantined_buildings["b1"]["reason"], "zero_byte")
        # 元ファイルは退避された
        self.assertFalse(path.exists())
        backups = list(path.parent.glob("log.json.corrupted_*"))
        self.assertEqual(len(backups), 1)

    # State 3: 空配列
    def test_empty_array_loads_normally(self):
        stub, paths = self._stub_with(["b1"])
        path = paths["b1"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[]", encoding="utf-8")

        stub._init_building_histories()

        self.assertEqual(stub.building_histories["b1"], [])
        self.assertEqual(stub.startup_alerts, [])
        self.assertEqual(stub.quarantined_buildings, {})

    # State 4: 正常配列
    def test_valid_array_loads_normally(self):
        stub, paths = self._stub_with(["b1"])
        path = paths["b1"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('[{"role":"user","content":"hi"}]', encoding="utf-8")

        stub._init_building_histories()
        self.assertEqual(stub.building_histories["b1"], [{"role": "user", "content": "hi"}])
        self.assertEqual(stub.quarantined_buildings, {})

    # State 5: 破損
    def test_corrupted_file_quarantined(self):
        stub, paths = self._stub_with(["b1"])
        path = paths["b1"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not valid json", encoding="utf-8")

        stub._init_building_histories()

        # 隔離: dictキー無し、quarantineに登録、startup_alertにcritical
        self.assertNotIn("b1", stub.building_histories)
        self.assertIn("b1", stub.quarantined_buildings)
        info = stub.quarantined_buildings["b1"]
        self.assertEqual(info["reason"], "corrupted")
        self.assertEqual(info["building_id"], "b1")
        self.assertIsNotNone(info["corrupted_path"])
        self.assertEqual(len(stub.startup_alerts), 1)
        self.assertEqual(stub.startup_alerts[0]["level"], "critical")

    # State 6: 配列以外の構造 (dict, null など)
    def test_invalid_structure_quarantined(self):
        stub, paths = self._stub_with(["b1"])
        path = paths["b1"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"not": "an array"}', encoding="utf-8")

        stub._init_building_histories()

        self.assertNotIn("b1", stub.building_histories)
        self.assertIn("b1", stub.quarantined_buildings)
        self.assertEqual(stub.quarantined_buildings["b1"]["reason"], "invalid_structure")

    def test_mixed_state_buildings(self):
        """複数ビルディングの混在状態 — 隔離は独立に行われ、正常は影響受けない。"""
        stub, paths = self._stub_with(["b_ok", "b_corrupted", "b_zero", "b_missing"])
        # b_ok: 正常
        paths["b_ok"].parent.mkdir(parents=True, exist_ok=True)
        paths["b_ok"].write_text('[{"role":"u","content":"x"}]', encoding="utf-8")
        # b_corrupted: 破損
        paths["b_corrupted"].parent.mkdir(parents=True, exist_ok=True)
        paths["b_corrupted"].write_text("garbage", encoding="utf-8")
        # b_zero: 0バイト
        paths["b_zero"].parent.mkdir(parents=True, exist_ok=True)
        paths["b_zero"].write_text("", encoding="utf-8")
        # b_missing: ファイル無し (何もしない)

        stub._init_building_histories()

        self.assertEqual(stub.building_histories["b_ok"], [{"role": "u", "content": "x"}])
        self.assertEqual(stub.building_histories["b_missing"], [])
        self.assertNotIn("b_corrupted", stub.building_histories)
        self.assertNotIn("b_zero", stub.building_histories)
        self.assertEqual(set(stub.quarantined_buildings.keys()), {"b_corrupted", "b_zero"})
        self.assertEqual(len(stub.startup_alerts), 2)

    def test_rescue_failure_still_quarantines(self):
        """退避リネーム失敗でも隔離は成立 (システム自動上書きは止める)。"""
        stub, paths = self._stub_with(["b1"])
        path = paths["b1"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("garbage", encoding="utf-8")

        with patch("pathlib.Path.rename", side_effect=OSError("permission denied")):
            stub._init_building_histories()

        # 退避失敗でも隔離されている (これがないとシャットダウン時に上書きされる)
        self.assertIn("b1", stub.quarantined_buildings)
        self.assertNotIn("b1", stub.building_histories)
        info = stub.quarantined_buildings["b1"]
        self.assertIsNone(info["corrupted_path"])
        self.assertEqual(info["rescue_error"], "permission denied")
        # 別系統のアラート ("退避失敗" を含む)
        self.assertIn("退避失敗", stub.startup_alerts[0]["title"])


class BackupSnapshotTests(unittest.TestCase):
    """起動時バックアップスナップショット機構の検証。"""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_successful_load_creates_backup_snapshot(self):
        log_path = self.tmp_dir / "b1" / "log.json"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text('[{"role":"u","content":"x"}]', encoding="utf-8")

        stub = _InitStub({"b1": log_path})
        stub._init_building_histories()

        backups = list_log_backups(log_path)
        self.assertEqual(len(backups), 1)
        # バックアップの中身は元と一致
        self.assertEqual(backups[0].read_text(encoding="utf-8"), '[{"role":"u","content":"x"}]')

    def test_quarantined_does_not_create_backup(self):
        """隔離されたファイルからはバックアップを作らない。"""
        log_path = self.tmp_dir / "b1" / "log.json"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("garbage", encoding="utf-8")

        stub = _InitStub({"b1": log_path})
        stub._init_building_histories()

        backups = list_log_backups(log_path)
        self.assertEqual(len(backups), 0)

    def test_backup_rotation_keeps_last_n(self):
        """古いバックアップはローテーションで削除される。"""
        log_path = self.tmp_dir / "b1" / "log.json"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("[]", encoding="utf-8")

        # SAIVERSE_BUILDING_LOG_BACKUP_KEEP=3 で6個作って3個残ることを確認
        with patch.dict("os.environ", {"SAIVERSE_BUILDING_LOG_BACKUP_KEEP": "3"}):
            for i in range(6):
                ts = f"20260426_12000{i}"
                create_log_backup_snapshot(log_path, ts)

        backups = list_log_backups(log_path)
        self.assertEqual(len(backups), 3)
        # 新しいものが残っているはず (timestamp順で降順)
        kept_names = sorted([p.name for p in backups], reverse=True)
        self.assertEqual(kept_names[0], "log.json.backup_20260426_120005.bak")
        self.assertEqual(kept_names[2], "log.json.backup_20260426_120003.bak")

    def test_list_backups_excludes_corrupted(self):
        """list_log_backups は .corrupted_* を含まない (バックアップとは別物)。"""
        log_path = self.tmp_dir / "b1" / "log.json"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # バックアップ
        (log_path.parent / "log.json.backup_20260426_120000.bak").write_text("[]", encoding="utf-8")
        # 破損退避
        (log_path.parent / "log.json.corrupted_20260426_130000").write_text("garbage", encoding="utf-8")

        backups = list_log_backups(log_path)
        self.assertEqual(len(backups), 1)
        self.assertIn("backup_", backups[0].name)


class IntegrationCorruptionLoopTests(unittest.TestCase):
    """init→quarantine→save の一連の流れの統合検証。

    旧コードの不具合 (破損→空フォールバック→空で上書き) が完全に塞がれて
    いることを、init+save の組み合わせで証明する。
    """

    def test_quarantined_building_save_does_not_touch_disk(self):
        """隔離後、save呼び出しでも .corrupted_* もlog.jsonも触らない。"""
        with TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            path = tmp_dir / "b1" / "log.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{corrupted", encoding="utf-8")

            init_stub = _InitStub({"b1": path})
            init_stub._init_building_histories()

            # 退避済み、quarantineに登録、dictにキー無し
            backups = list(path.parent.glob("log.json.corrupted_*"))
            self.assertEqual(len(backups), 1)
            original_corrupted_content = backups[0].read_text(encoding="utf-8")
            self.assertNotIn("b1", init_stub.building_histories)
            self.assertIn("b1", init_stub.quarantined_buildings)

            # save は隔離されたビルディングを完全にskip
            save_stub = _SaveStub(
                {"b1": path},
                init_stub.building_histories,  # 共有参照
                quarantined_buildings=init_stub.quarantined_buildings,
            )
            save_stub._save_building_histories(["b1"])

            # 退避ファイルは無傷
            self.assertEqual(backups[0].read_text(encoding="utf-8"), original_corrupted_content)
            # log.json も新規作成されない (隔離中)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
