"""入室想起 (_inject_persona_recall_on_enter) の対象種別テスト。

まはー裁定 (2026-07-11): ユーザーも対ペルソナと同様に想起する。
従来はユーザーページの肥大化に打つ手が無く persona 限定だったが、
編纂の分割 (P4-a) が肥大を受けられるようになったため前提が変わった。
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sea.head_pipeline.integration import _inject_persona_recall_on_enter
from sea.head_pipeline.types import NotificationLabel


def _label(occupant_id, occupant_kind, kind="occupant_entered"):
    return NotificationLabel(
        kind=kind,
        label=f"{occupant_id} が入室",
        metadata={"occupant_id": occupant_id, "occupant_kind": occupant_kind},
    )


class RecallOnEnterKindTests(unittest.TestCase):
    def setUp(self):
        self.recalled = []
        self.pushed = []

        def _recall(occupant_id, **kwargs):
            self.recalled.append(occupant_id)
            return f"recall:{occupant_id}"

        self.persona = SimpleNamespace(
            history_manager=SimpleNamespace(recall_conversation_with=_recall),
        )
        self.sai_mem = SimpleNamespace(
            push_perception=lambda kind, text: self.pushed.append((kind, text)),
        )

    def test_persona_occupant_is_recalled(self):
        _inject_persona_recall_on_enter(
            self.persona, [_label("elis_city_a", "persona")], self.sai_mem,
        )
        self.assertEqual(self.recalled, ["elis_city_a"])
        self.assertEqual(len(self.pushed), 1)

    def test_user_occupant_is_recalled(self):
        """ユーザーも対ペルソナと同様に想起される (まはー裁定 2026-07-11)。"""
        _inject_persona_recall_on_enter(
            self.persona, [_label("1", "user")], self.sai_mem,
        )
        self.assertEqual(self.recalled, ["1"])
        self.assertEqual(len(self.pushed), 1)
        self.assertEqual(self.pushed[0][0], "persona_recall")

    def test_unknown_kind_is_not_recalled(self):
        _inject_persona_recall_on_enter(
            self.persona, [_label("x", "item")], self.sai_mem,
        )
        self.assertEqual(self.recalled, [])
        self.assertEqual(self.pushed, [])

    def test_non_enter_label_is_ignored(self):
        _inject_persona_recall_on_enter(
            self.persona,
            [_label("elis_city_a", "persona", kind="occupant_left")],
            self.sai_mem,
        )
        self.assertEqual(self.recalled, [])


if __name__ == "__main__":
    unittest.main()
