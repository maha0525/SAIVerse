"""モード (aspect) 別スペル権限ゲートのテスト。

mode_spell_permissions.md §5 の権限マトリクスを検証する。
"""
import unittest

from sea.mode_spell_permissions import check_spell_permission
from sea.pulse_context import Aspect


class TestSpellPermissionMatrix(unittest.TestCase):
    def test_task_control_allowed_in_autonomous_and_conversation(self):
        # P2c-4a: 旧 task_* は撤去済み。ゲート対象は purpose_* 動詞 (旧 task_* 後継)。
        # 2026-08-21: purpose_seed / purpose_adopt はスペルごと退役した。
        for sp in ("purpose_close", "purpose_step", "purpose_decompose"):
            self.assertIsNone(check_spell_permission(sp, Aspect.AUTONOMOUS))
            self.assertIsNone(check_spell_permission(sp, Aspect.CONVERSATION))

    def test_task_control_blocked_in_meta_and_worker(self):
        for sp in ("purpose_close", "purpose_step", "purpose_decompose"):
            self.assertIsNotNone(check_spell_permission(sp, Aspect.META))
            self.assertIsNotNone(check_spell_permission(sp, Aspect.WORKER))

    def test_retired_track_spells_are_no_longer_gated(self):
        # Track 操作スペル 7 種は 2026-08-21 に機構ごと退役した
        # (track_retirement.md §7.2 ④群)。ゲート表からも消える。
        for sp in ("track_create", "track_activate", "track_pause",
                   "track_complete", "track_abort", "track_parameter_set"):
            for asp in Aspect:
                self.assertIsNone(check_spell_permission(sp, asp))

    def test_read_and_generic_spells_unrestricted(self):
        for sp in ("get_task_summary", "recall", "memopedia_note"):
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
            "document_search",
        )
        for sp in document_spells:
            self.assertIsNone(check_spell_permission(sp, Aspect.WORKER))
            for asp in Aspect:
                self.assertIsNone(check_spell_permission(sp, asp))

    def test_none_aspect_never_restricts(self):
        # legacy frame / aspect 不明時は制限しない。
        self.assertIsNone(check_spell_permission("purpose_close", None))

    def test_block_message_uses_mode_display_name(self):
        msg = check_spell_permission("purpose_close", Aspect.WORKER)
        self.assertIsNotNone(msg)
        self.assertIn("purpose_close", msg)
        self.assertIn("分身モード", msg)
        self.assertIn("実行できません", msg)


if __name__ == "__main__":
    unittest.main()
