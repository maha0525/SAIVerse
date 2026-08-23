"""モード (aspect) 別スペル権限ゲートのテスト。

mode_spell_permissions.md §5 の権限マトリクスを検証する。
"""
import unittest
from unittest import mock

from sea import mode_spell_permissions as msp
from sea.mode_spell_permissions import check_spell_permission
from sea.pulse_context import Aspect


class TestSpellPermissionMatrix(unittest.TestCase):
    def test_task_control_table_is_empty_after_purpose_tree_retirement(self):
        # 2026-08-23: 目的の木が手帳に後を譲って退役し、最後まで残っていた
        # purpose_decompose / purpose_step / purpose_close も削除された
        # (purpose_tree_vs_pocketbook_succession.md 裁定 A・一段目)。
        # ゲート表は空になり、どのモードでも素通しになる。
        self.assertEqual(msp.TASK_CONTROL_SPELLS, frozenset())
        for sp in ("purpose_close", "purpose_step", "purpose_decompose"):
            for asp in Aspect:
                self.assertIsNone(check_spell_permission(sp, asp))

    def test_gate_still_blocks_a_listed_spell_in_meta_and_worker(self):
        # 表が空でも機構は生きている — 表に載った名前は META / WORKER で断られ、
        # CONVERSATION / AUTONOMOUS では通る (§5 マトリクス)。
        with mock.patch.object(
            msp, "TASK_CONTROL_SPELLS", frozenset({"gated_spell"})
        ):
            self.assertIsNotNone(check_spell_permission("gated_spell", Aspect.META))
            self.assertIsNotNone(check_spell_permission("gated_spell", Aspect.WORKER))
            self.assertIsNone(
                check_spell_permission("gated_spell", Aspect.CONVERSATION)
            )
            self.assertIsNone(check_spell_permission("gated_spell", Aspect.AUTONOMOUS))

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
        with mock.patch.object(
            msp, "TASK_CONTROL_SPELLS", frozenset({"gated_spell"})
        ):
            self.assertIsNone(check_spell_permission("gated_spell", None))

    def test_block_message_uses_mode_display_name(self):
        with mock.patch.object(
            msp, "TASK_CONTROL_SPELLS", frozenset({"gated_spell"})
        ):
            msg = check_spell_permission("gated_spell", Aspect.WORKER)
        self.assertIsNotNone(msg)
        self.assertIn("gated_spell", msg)
        self.assertIn("分身モード", msg)
        self.assertIn("実行できません", msg)


if __name__ == "__main__":
    unittest.main()
