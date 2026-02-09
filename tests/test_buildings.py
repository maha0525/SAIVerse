"""Tests for buildings.py — Building data class."""
import unittest

from buildings import Building


class TestBuilding(unittest.TestCase):
    def test_init_required_params(self):
        b = Building("room-1", "テスト部屋")
        self.assertEqual(b.building_id, "room-1")
        self.assertEqual(b.name, "テスト部屋")

    def test_default_values(self):
        b = Building("room-1", "テスト部屋")
        self.assertEqual(b.capacity, 1)
        self.assertEqual(b.base_system_instruction, "")
        self.assertEqual(b.system_instruction, "")
        self.assertIsNone(b.entry_prompt)
        self.assertIsNone(b.auto_prompt)
        self.assertEqual(b.description, "")
        self.assertTrue(b.run_entry_llm)
        self.assertTrue(b.run_auto_llm)
        self.assertEqual(b.auto_interval_sec, 10)
        self.assertEqual(b.item_ids, [])
        self.assertEqual(b.extra_prompt_files, [])

    def test_custom_values(self):
        b = Building(
            "room-2",
            "カスタム部屋",
            capacity=5,
            system_instruction="指示文",
            entry_prompt="入室プロンプト",
            auto_prompt="自動プロンプト",
            description="説明",
            run_entry_llm=False,
            run_auto_llm=False,
            auto_interval_sec=30,
            extra_prompt_files=["file1.txt"],
        )
        self.assertEqual(b.capacity, 5)
        self.assertEqual(b.base_system_instruction, "指示文")
        self.assertEqual(b.system_instruction, "指示文")
        self.assertEqual(b.entry_prompt, "入室プロンプト")
        self.assertEqual(b.auto_prompt, "自動プロンプト")
        self.assertEqual(b.description, "説明")
        self.assertFalse(b.run_entry_llm)
        self.assertFalse(b.run_auto_llm)
        self.assertEqual(b.auto_interval_sec, 30)
        self.assertEqual(b.extra_prompt_files, ["file1.txt"])

    def test_empty_system_instruction_normalised(self):
        b = Building("room-3", "部屋", system_instruction="")
        self.assertEqual(b.base_system_instruction, "")


if __name__ == "__main__":
    unittest.main()
