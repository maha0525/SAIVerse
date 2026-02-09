import unittest
from unittest.mock import patch, MagicMock, mock_open
import json
from pathlib import Path
from datetime import datetime

# テスト対象のモジュールをインポート
from persona.history_manager import HistoryManager

class TestHistoryManager(unittest.TestCase):
    def assertMessagesMatch(self, actual, expected):
        self.assertEqual(
            [(m.get("role"), m.get("content")) for m in actual],
            [(m.get("role"), m.get("content")) for m in expected],
        )

    def setUp(self):
        # 各テストメソッドの実行前に呼ばれるセットアップ
        self.persona_id = "test_persona"
        self.persona_log_path = Path("/mock/saiverse_home/personas/test_persona/log.json")
        self.building_memory_paths = {
            "user_room": Path("/mock/saiverse_home/buildings/user_room/log.json"),
            "deep_think_room": Path("/mock/saiverse_home/buildings/deep_think_room/log.json"),
        }
        self.initial_persona_history = []
        self.initial_building_histories = {
            "user_room": [],
            "deep_think_room": [],
        }

        # Pathオブジェクトのメソッドをモック化
        self.mock_path_exists = patch('pathlib.Path.exists').start()
        self.mock_path_read_text = patch('pathlib.Path.read_text').start()
        self.mock_path_write_text = patch('pathlib.Path.write_text').start()
        self.mock_path_mkdir = patch('pathlib.Path.mkdir').start()
        self.mock_path_glob = patch('pathlib.Path.glob').start()
        self.mock_path_stat = patch('pathlib.Path.stat').start()

        # デフォルトのモックの振る舞いを設定
        self.mock_path_exists.return_value = True # ファイルは存在すると仮定
        self.mock_path_read_text.return_value = "[]" # ファイル内容は空のJSONリストと仮定
        self.mock_path_glob.return_value = [] # globは空リストを返す
        self.mock_path_stat.return_value.st_size = 0 # ファイルサイズは0と仮定

        self.history_manager = HistoryManager(
            persona_id=self.persona_id,
            persona_log_path=self.persona_log_path,
            building_memory_paths=self.building_memory_paths,
            initial_persona_history=self.initial_persona_history,
            initial_building_histories=self.initial_building_histories
        )

    def tearDown(self):
        # 各テストメソッドの実行後に呼ばれるクリーンアップ
        patch.stopall()

    def test_initialization(self):
        self.assertEqual(self.history_manager.persona_id, "test_persona")
        self.assertEqual(self.history_manager.persona_log_path, self.persona_log_path)
        self.assertEqual(self.history_manager.building_memory_paths, self.building_memory_paths)
        self.assertEqual(self.history_manager.messages, [])
        self.assertEqual(self.history_manager.building_histories, {
            "user_room": [],
            "deep_think_room": [],
        })

    def test_add_message(self):
        msg1 = {"role": "user", "content": "Hello"}
        msg2 = {"role": "assistant", "content": "Hi there", "persona_id": "test_persona"}

        self.history_manager.add_message(msg1, "user_room", heard_by=["user"])
        self.assertEqual(len(self.history_manager.messages), 1)
        self.assertEqual(self.history_manager.messages[0]["content"], "Hello")
        building_entry_1 = self.history_manager.building_histories["user_room"][0]
        self.assertEqual(building_entry_1["content"], "Hello")
        self.assertIn("message_id", building_entry_1)
        self.assertIn("seq", building_entry_1)
        self.assertEqual(building_entry_1["heard_by"], ["user"])

        self.history_manager.add_message(msg2, "user_room", heard_by=["user", "assistant"])
        self.assertEqual(len(self.history_manager.messages), 2)
        self.assertEqual(self.history_manager.messages[1]["content"], "Hi there")
        building_entry_2 = self.history_manager.building_histories["user_room"][1]
        self.assertEqual(building_entry_2["content"], "Hi there")
        self.assertGreater(building_entry_2["seq"], building_entry_1["seq"])
        self.assertIn("message_id", building_entry_2)
        self.assertEqual(building_entry_2["heard_by"], ["assistant", "user"])

        # persona_idが自動で付与されるか確認
        msg3 = {"role": "assistant", "content": "Auto ID"}
        self.history_manager.add_message(msg3, "user_room")
        self.assertEqual(self.history_manager.messages[2]["persona_id"], "test_persona")

    def test_add_message_trimming_persona_history(self):
        # 2MBを超えるメッセージを追加してトリミングをテスト
        # 1MBのメッセージを3つ追加 -> 合計3MBとなり、2MB制限を超える
        long_msg = {"role": "user", "content": "a" * (1024 * 1024)}
        self.history_manager.add_message(long_msg, "user_room") # 1MB
        self.history_manager.add_message(long_msg, "user_room") # 2MB
        self.history_manager.add_message(long_msg, "user_room") # 3MB -> 最初のメッセージが削除される

        # 2MB制限を超過したため、メッセージがトリミングされ、old_logに書き込まれたことを確認
        # 厳密な残るメッセージ数は、JSONエンコードのオーバーヘッドによって変動するため、
        # 2MB以下になっていることと、old_logへの書き込みが行われたことを確認する
        self.assertLessEqual(len(json.dumps(self.history_manager.messages, ensure_ascii=False).encode("utf-8")), 2000 * 1024)
        self.mock_path_mkdir.assert_called_with(parents=True, exist_ok=True)
        self.mock_path_write_text.assert_called() # old_logへの書き込み

    def test_add_message_trimming_building_history(self):
        # 2MBを超えるメッセージを追加してビルディング履歴のトリミングをテスト
        # 1MBのメッセージを3つ追加 -> 合計3MBとなり、2MB制限を超える
        long_msg = {"role": "user", "content": "b" * (1024 * 1024)}
        self.history_manager.add_message(long_msg, "deep_think_room") # 1MB
        self.history_manager.add_message(long_msg, "deep_think_room") # 2MB
        self.history_manager.add_message(long_msg, "deep_think_room") # 3MB -> 最初のメッセージが削除される

        # 2MB制限を超過したため、メッセージがトリミングされ、old_logに書き込まれたことを確認
        self.assertLessEqual(len(json.dumps(self.history_manager.building_histories["deep_think_room"], ensure_ascii=False).encode("utf-8")), 2000 * 1024)
        self.mock_path_mkdir.assert_called_with(parents=True, exist_ok=True)
        self.mock_path_write_text.assert_called() # old_logへの書き込み

    def test_add_to_building_only(self):
        msg = {"role": "system", "content": "Building specific"}
        self.history_manager.add_to_building_only("user_room", msg, heard_by=["observer"])
        self.assertEqual(len(self.history_manager.building_histories["user_room"]), 1)
        entry = self.history_manager.building_histories["user_room"][0]
        self.assertEqual(entry["content"], "Building specific")
        self.assertEqual(entry["heard_by"], ["observer"])
        self.assertIn("message_id", entry)
        self.assertIn("seq", entry)
        self.assertEqual(self.history_manager.messages, []) # persona history should be unchanged

    def test_add_to_persona_only(self):
        msg = {"role": "system", "content": "Persona specific"}
        self.history_manager.add_to_persona_only(msg)
        self.assertEqual(len(self.history_manager.messages), 1)
        stored = self.history_manager.messages[0]
        self.assertEqual(stored["role"], msg["role"])
        self.assertEqual(stored["content"], msg["content"])
        self.assertIn("timestamp", stored)
        self.assertEqual(self.history_manager.building_histories["user_room"], []) # building history should be unchanged

    def test_get_recent_history(self):
        msgs = [
            {"role": "user", "content": "1"},
            {"role": "assistant", "content": "22"},
            {"role": "user", "content": "333"},
            {"role": "assistant", "content": "4444"},
        ]
        for msg in msgs:
            self.history_manager.add_message(msg, "user_room")

        # 全て取得
        recent = self.history_manager.get_recent_history(100)
        self.assertMessagesMatch(recent, msgs)
        for item in recent:
            self.assertIn("timestamp", item)
        self.assertTrue(all(item.get("role") != "assistant" or item.get("persona_id") == self.persona_id for item in recent))

        # 制限付きで取得
        recent = self.history_manager.get_recent_history(7) # "333" + "4444" = 7文字
        self.assertMessagesMatch(recent, [msgs[2], msgs[3]])

        recent = self.history_manager.get_recent_history(3)
        self.assertEqual(recent, []) # "4444" (4文字) が制限を超えるため、何も返されない

        recent = self.history_manager.get_recent_history(4) # "4444" = 4文字
        self.assertMessagesMatch(recent, [msgs[3]])

        recent = self.history_manager.get_recent_history(6)
        self.assertMessagesMatch(recent, [msgs[3]]) # "4444" (4文字) のみ

        recent = self.history_manager.get_recent_history(10) # "1" + "22" + "333" + "4444" = 10文字
        self.assertMessagesMatch(recent, msgs)

        recent = self.history_manager.get_recent_history(0)
        self.assertEqual(recent, [])

    def test_save_all(self):
        msg1 = {"role": "user", "content": "Save test 1"}
        msg2 = {"role": "assistant", "content": "Save test 2"}
        self.history_manager.add_message(msg1, "user_room")
        self.history_manager.add_message(msg2, "deep_think_room")

        self.history_manager.save_all()

        # persona_log_pathへの書き込みが呼ばれたことを確認
        self.mock_path_write_text.assert_any_call(
            json.dumps(self.history_manager.messages, ensure_ascii=False),
            encoding="utf-8"
        )
        # building_memory_pathsへの書き込みがそれぞれ呼ばれたことを確認
        self.mock_path_write_text.assert_any_call(
            json.dumps(self.history_manager.building_histories["user_room"], ensure_ascii=False),
            encoding="utf-8"
        )
        self.mock_path_write_text.assert_any_call(
            json.dumps(self.history_manager.building_histories["deep_think_room"], ensure_ascii=False),
            encoding="utf-8"
        )
        # ディレクトリ作成が呼ばれたことを確認
        self.mock_path_mkdir.assert_called_with(parents=True, exist_ok=True)

if __name__ == '__main__':
    unittest.main()
