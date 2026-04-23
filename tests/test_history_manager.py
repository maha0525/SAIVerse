import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from persona.history_manager import HistoryManager


class TestHistoryManager(unittest.TestCase):
    def assertMessagesMatch(self, actual, expected):
        self.assertEqual(
            [(m.get("role"), m.get("content")) for m in actual],
            [(m.get("role"), m.get("content")) for m in expected],
        )

    def setUp(self):
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

        self.mock_path_exists = patch("pathlib.Path.exists").start()
        self.mock_path_read_text = patch("pathlib.Path.read_text").start()
        self.mock_path_write_text = patch("pathlib.Path.write_text").start()
        self.mock_path_mkdir = patch("pathlib.Path.mkdir").start()
        self.mock_path_glob = patch("pathlib.Path.glob").start()
        self.mock_path_stat = patch("pathlib.Path.stat").start()

        self.mock_path_exists.return_value = True
        self.mock_path_read_text.return_value = "[]"
        self.mock_path_glob.return_value = []
        self.mock_path_stat.return_value.st_size = 0
        self.history_manager = HistoryManager(
            persona_id=self.persona_id,
            persona_log_path=self.persona_log_path,
            building_memory_paths=self.building_memory_paths,
            initial_persona_history=self.initial_persona_history,
            initial_building_histories=self.initial_building_histories,
        )

    def tearDown(self):
        patch.stopall()

    def test_initialization(self):
        self.assertEqual(self.history_manager.persona_id, "test_persona")
        self.assertEqual(self.history_manager.persona_log_path, self.persona_log_path)
        self.assertEqual(self.history_manager.building_memory_paths, self.building_memory_paths)
        self.assertEqual(self.history_manager.messages, [])
        self.assertEqual(
            self.history_manager.building_histories,
            {"user_room": [], "deep_think_room": []},
        )

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

        msg3 = {"role": "assistant", "content": "Auto ID"}
        self.history_manager.add_message(msg3, "user_room")
        self.assertEqual(self.history_manager.messages[2]["persona_id"], "test_persona")

    def test_add_message_trimming_persona_history(self):
        long_msg = {"role": "user", "content": "a" * (1024 * 1024)}
        self.history_manager.add_message(long_msg, "user_room")
        self.history_manager.add_message(long_msg, "user_room")
        self.history_manager.add_message(long_msg, "user_room")
        self.assertLessEqual(
            len(json.dumps(self.history_manager.messages, ensure_ascii=False).encode("utf-8")),
            2000 * 1024,
        )
        self.mock_path_mkdir.assert_called_with(parents=True, exist_ok=True)
        self.mock_path_write_text.assert_called()

    def test_add_message_trimming_building_history(self):
        long_msg = {"role": "user", "content": "b" * (1024 * 1024)}
        self.history_manager.add_message(long_msg, "deep_think_room")
        self.history_manager.add_message(long_msg, "deep_think_room")
        self.history_manager.add_message(long_msg, "deep_think_room")
        self.assertLessEqual(
            len(
                json.dumps(
                    self.history_manager.building_histories["deep_think_room"],
                    ensure_ascii=False,
                ).encode("utf-8")
            ),
            2000 * 1024,
        )
        self.mock_path_mkdir.assert_called_with(parents=True, exist_ok=True)
        self.mock_path_write_text.assert_called()

    def test_add_to_building_only(self):
        msg = {"role": "system", "content": "Building specific"}
        self.history_manager.add_to_building_only("user_room", msg, heard_by=["observer"])
        self.assertEqual(len(self.history_manager.building_histories["user_room"]), 1)
        entry = self.history_manager.building_histories["user_room"][0]
        self.assertEqual(entry["content"], "Building specific")
        self.assertEqual(entry["heard_by"], ["observer"])
        self.assertIn("message_id", entry)
        self.assertIn("seq", entry)
        self.assertEqual(self.history_manager.messages, [])

    def test_add_to_building_only_with_unknown_building_id_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.history_manager.add_to_building_only(
                "unknown_room",
                {"role": "assistant", "content": "invalid"},
            )

    def test_add_to_persona_only(self):
        msg = {"role": "system", "content": "Persona specific"}
        self.history_manager.add_to_persona_only(msg)
        self.assertEqual(len(self.history_manager.messages), 1)
        stored = self.history_manager.messages[0]
        self.assertEqual(stored["role"], msg["role"])
        self.assertEqual(stored["content"], msg["content"])
        self.assertIn("timestamp", stored)
        self.assertEqual(self.history_manager.building_histories["user_room"], [])

    def test_get_recent_history(self):
        msgs = [
            {"role": "user", "content": "1"},
            {"role": "assistant", "content": "22"},
            {"role": "user", "content": "333"},
            {"role": "assistant", "content": "4444"},
        ]
        for msg in msgs:
            self.history_manager.add_message(msg, "user_room")

        recent = self.history_manager.get_recent_history(100)
        self.assertMessagesMatch(recent, msgs)
        for item in recent:
            self.assertIn("timestamp", item)
        self.assertTrue(
            all(
                item.get("role") != "assistant" or item.get("persona_id") == self.persona_id
                for item in recent
            )
        )

        recent = self.history_manager.get_recent_history(7)
        self.assertMessagesMatch(recent, [msgs[2], msgs[3]])

        recent = self.history_manager.get_recent_history(3)
        self.assertEqual(recent, [])

        recent = self.history_manager.get_recent_history(4)
        self.assertMessagesMatch(recent, [msgs[3]])

        recent = self.history_manager.get_recent_history(6)
        self.assertMessagesMatch(recent, [msgs[3]])

        recent = self.history_manager.get_recent_history(10)
        self.assertMessagesMatch(recent, msgs)

        recent = self.history_manager.get_recent_history(0)
        self.assertEqual(recent, [])

    def test_save_all(self):
        msg1 = {"role": "user", "content": "Save test 1"}
        msg2 = {"role": "assistant", "content": "Save test 2"}
        self.history_manager.add_message(msg1, "user_room")
        self.history_manager.add_message(msg2, "deep_think_room")

        self.history_manager.save_all()

        self.mock_path_write_text.assert_any_call(
            json.dumps(self.history_manager.messages, ensure_ascii=False),
            encoding="utf-8",
        )
        self.mock_path_write_text.assert_any_call(
            json.dumps(self.history_manager.building_histories["user_room"], ensure_ascii=False),
            encoding="utf-8",
        )
        self.mock_path_write_text.assert_any_call(
            json.dumps(self.history_manager.building_histories["deep_think_room"], ensure_ascii=False),
            encoding="utf-8",
        )
        self.mock_path_mkdir.assert_called_with(parents=True, exist_ok=True)

    def test_get_recent_entrant_events_only_returns_ai_enter_events(self):
        self.history_manager.building_histories["user_room"] = [
            {
                "role": "host",
                "content": "leave",
                "metadata": {
                    "event": {
                        "type": "occupancy",
                        "action": "leave",
                        "entity_id": "persona_leave",
                        "entity_type": "ai",
                        "event_key": "leave-1",
                    }
                },
            },
            {
                "role": "host",
                "content": "user enter",
                "metadata": {
                    "event": {
                        "type": "occupancy",
                        "action": "enter",
                        "entity_id": "user_1",
                        "entity_type": "user",
                        "event_key": "user-enter-1",
                    }
                },
            },
            {
                "role": "host",
                "content": "ai enter",
                "metadata": {
                    "event": {
                        "type": "occupancy",
                        "action": "enter",
                        "entity_id": "persona_enter",
                        "entity_type": "ai",
                        "event_key": "enter-1",
                    }
                },
            },
        ]

        entrants = self.history_manager.get_recent_entrant_events("user_room")

        self.assertEqual(
            entrants,
            [{"entity_id": "persona_enter", "event_key": "enter-1"}],
        )

    def test_marked_entrant_event_is_not_recalled_twice(self):
        self.history_manager.building_histories["user_room"] = [
            {
                "role": "host",
                "content": "ai enter",
                "metadata": {
                    "event": {
                        "type": "occupancy",
                        "action": "enter",
                        "entity_id": "persona_enter",
                        "entity_type": "ai",
                        "event_key": "enter-1",
                        "recalled_by": [],
                    }
                },
            }
        ]

        self.assertTrue(
            self.history_manager.should_recall_persona(
                "persona_enter",
                building_id="user_room",
                event_key="enter-1",
            )
        )
        self.assertTrue(self.history_manager.mark_entrant_event_recalled("user_room", "enter-1"))
        self.assertFalse(
            self.history_manager.should_recall_persona(
                "persona_enter",
                building_id="user_room",
                event_key="enter-1",
            )
        )

    def test_should_not_recall_when_target_already_in_recent_context(self):
        self.history_manager.add_to_persona_only(
            {
                "role": "assistant",
                "content": "recent message",
                "persona_id": "persona_enter",
            }
        )

        self.assertFalse(self.history_manager.should_recall_persona("persona_enter"))

    @patch("sai_memory.memopedia.storage.create_page")
    @patch("sai_memory.memopedia.storage.get_page")
    @patch("sai_memory.memopedia.storage.get_page_by_title")
    @patch("sai_memory.memopedia.storage.get_page_by_persona_id")
    def test_ensure_persona_page_uses_persona_id_suffix_when_title_conflicts(
        self,
        mock_get_page_by_persona_id,
        mock_get_page_by_title,
        mock_get_page,
        mock_create_page,
    ):
        memory_adapter = MagicMock()
        memory_adapter.is_ready.return_value = True
        memory_adapter.conn = object()
        self.history_manager.set_memory_adapter(memory_adapter)

        mock_get_page_by_persona_id.return_value = None
        mock_get_page_by_title.return_value = MagicMock(id="existing-title-page")
        mock_get_page.return_value = MagicMock(id="root_people")
        mock_create_page.return_value = MagicMock(id="new-page")

        result = self.history_manager.ensure_persona_page("persona_x", "同名")

        self.assertTrue(result)
        mock_create_page.assert_called_once()
        self.assertEqual(
            mock_create_page.call_args.kwargs["title"],
            "同名 (persona_x)",
        )


if __name__ == "__main__":
    unittest.main()
