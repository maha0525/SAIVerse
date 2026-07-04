"""モード (aspect) 別スペル権限ゲートのテスト。

mode_spell_permissions.md §5 の権限マトリクスを検証する。
"""
import unittest

from sea.mode_spell_permissions import check_spell_permission
from sea.pulse_context import Aspect


class TestSpellPermissionMatrix(unittest.TestCase):
    def test_track_control_allowed_in_meta_and_conversation(self):
        for sp in ("track_create", "track_complete", "track_abort", "track_pause"):
            self.assertIsNone(check_spell_permission(sp, Aspect.CONVERSATION))
            self.assertIsNone(check_spell_permission(sp, Aspect.META))

    def test_track_control_blocked_in_autonomous_and_worker(self):
        for sp in ("track_create", "track_complete", "track_activate", "track_parameter_set"):
            self.assertIsNotNone(check_spell_permission(sp, Aspect.AUTONOMOUS))
            self.assertIsNotNone(check_spell_permission(sp, Aspect.WORKER))

    def test_task_control_allowed_in_autonomous_and_conversation(self):
        for sp in ("task_add", "task_done", "task_update_step", "task_decompose"):
            self.assertIsNone(check_spell_permission(sp, Aspect.AUTONOMOUS))
            self.assertIsNone(check_spell_permission(sp, Aspect.CONVERSATION))

    def test_task_control_blocked_in_meta_and_worker(self):
        # 厳密版: META は大目的 (Track) のみ、Task (小目標) には触らない。
        for sp in ("task_add", "task_done", "task_update_step", "task_decompose"):
            self.assertIsNotNone(check_spell_permission(sp, Aspect.META))
            self.assertIsNotNone(check_spell_permission(sp, Aspect.WORKER))

    def test_life_purpose_set_allowed_in_meta_and_conversation(self):
        # 自己定義は Track 操作と同じ = META / CONVERSATION のみ。
        self.assertIsNone(check_spell_permission("life_purpose_set", Aspect.META))
        self.assertIsNone(check_spell_permission("life_purpose_set", Aspect.CONVERSATION))

    def test_life_purpose_set_blocked_in_autonomous_and_worker(self):
        # AUTONOMOUS (軽量モデル) が生きる目的を勝手に書き換えるのを防ぐ。
        self.assertIsNotNone(check_spell_permission("life_purpose_set", Aspect.AUTONOMOUS))
        self.assertIsNotNone(check_spell_permission("life_purpose_set", Aspect.WORKER))

    def test_read_and_generic_spells_unrestricted(self):
        for sp in ("track_list", "get_task_summary", "recall", "memopedia_note"):
            for asp in Aspect:
                self.assertIsNone(check_spell_permission(sp, asp))

    def test_document_spells_unrestricted_including_worker(self):
        # 生産手段の document スペルは汎用扱い = ゲート対象外 (全モード許可)。
        # 特に分身モード (WORKER) の作業セッションがアーティファクト生成に
        # 使えることが要 (autonomous_behavior_v2.md §2.2 / §11)。
        document_spells = (
            "document_create",
            "document_read",
            "document_edit",
            "document_append_content",
            "document_search",
        )
        for sp in document_spells:
            self.assertIsNone(check_spell_permission(sp, Aspect.WORKER))
            for asp in Aspect:
                self.assertIsNone(check_spell_permission(sp, asp))

    def test_none_aspect_never_restricts(self):
        # legacy frame / aspect 不明時は制限しない。
        self.assertIsNone(check_spell_permission("track_create", None))
        self.assertIsNone(check_spell_permission("task_add", None))

    def test_block_message_uses_mode_display_name(self):
        msg = check_spell_permission("track_create", Aspect.AUTONOMOUS)
        self.assertIsNotNone(msg)
        self.assertIn("track_create", msg)
        self.assertIn("自律作業モード", msg)
        self.assertIn("実行できません", msg)


if __name__ == "__main__":
    unittest.main()
