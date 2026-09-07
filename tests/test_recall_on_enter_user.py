"""同席想起 (inject_copresence_recall) の対象種別テスト。

まはー裁定 (2026-07-11): ユーザーも対ペルソナと同様に想起する。
従来はユーザーページの肥大化に打つ手が無く persona 限定だったが、
編纂の分割 (P4-a) が肥大を受けられるようになったため前提が変わった。

種別 (persona / user) は同席者の ID が manager のペルソナ表に居るかで決まる
(2026-09-07 の発火点の移動以前は、入室ラベルの metadata が運んでいた)。
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sea.head_pipeline.integration import (
    inject_copresence_recall,
    reset_copresence_recall_memory,
)

ROOM = "b_air_room"
SELF_ID = "air_city_a"


class _FakeManager:
    def __init__(self, occupants, persona_occupants=()):
        self.occupants = {ROOM: list(occupants)}
        self.personas = {oid: object() for oid in persona_occupants}


class RecallCopresenceKindTests(unittest.TestCase):
    def setUp(self):
        # 「同席の間は一度だけ試みる」記憶はプロセス内で共有される (テスト間の
        # 汚染を防ぐ掃除。記憶の契約そのものは tests/test_copresence_recall.py)。
        reset_copresence_recall_memory()
        self.addCleanup(reset_copresence_recall_memory)
        self.recalled = []
        self.pushed = []

        def _recall(occupant_id, **kwargs):
            self.recalled.append(occupant_id)
            return f"recall:{occupant_id}"

        self.sai_mem = SimpleNamespace(
            is_ready=lambda: True,
            push_perception=lambda kind, text: self.pushed.append((kind, text)),
        )
        self.persona = SimpleNamespace(
            persona_id=SELF_ID,
            history_manager=SimpleNamespace(
                recall_conversation_with=_recall,
                # 再会の門 (2026-09-05): 種別の判定を見るテストなので門は常に開く。
                # 門そのものの契約は tests/test_recall_on_enter_gate.py。
                should_recall_persona=lambda *args, **kwargs: True,
            ),
            sai_memory=self.sai_mem,
            id_to_name_map={},
        )
        self.kinds_seen = []

        def _gate(occupant_id, target_kind=None, **kwargs):
            self.kinds_seen.append((occupant_id, target_kind))
            return True

        self.persona.history_manager.should_recall_persona = _gate

    def test_persona_occupant_is_recalled(self):
        manager = _FakeManager(["elis_city_a"], ["elis_city_a"])
        inject_copresence_recall(self.persona, manager, ROOM)
        self.assertEqual(self.recalled, ["elis_city_a"])
        self.assertEqual(len(self.pushed), 1)
        self.assertEqual(self.kinds_seen, [("elis_city_a", "persona")])

    def test_user_occupant_is_recalled(self):
        """ユーザーも対ペルソナと同様に想起される (まはー裁定 2026-07-11)。"""
        manager = _FakeManager(["1"])       # ペルソナ表に居ない = ユーザー
        inject_copresence_recall(self.persona, manager, ROOM)
        self.assertEqual(self.recalled, ["1"])
        self.assertEqual(len(self.pushed), 1)
        self.assertEqual(self.pushed[0][0], "persona_recall")
        self.assertEqual(self.kinds_seen, [("1", "user")])

    def test_visiting_persona_is_treated_as_a_persona(self):
        """訪問中のペルソナも all_personas 経由でペルソナと判定する。"""
        manager = _FakeManager(["guest"])
        manager.all_personas = {"guest": object()}
        inject_copresence_recall(self.persona, manager, ROOM)
        self.assertEqual(self.kinds_seen, [("guest", "persona")])

    def test_self_is_not_recalled(self):
        manager = _FakeManager([SELF_ID], [SELF_ID])
        inject_copresence_recall(self.persona, manager, ROOM)
        self.assertEqual(self.recalled, [])
        self.assertEqual(self.pushed, [])

    def test_empty_room_recalls_nothing(self):
        inject_copresence_recall(self.persona, _FakeManager([]), ROOM)
        self.assertEqual(self.recalled, [])
        self.assertEqual(self.pushed, [])


if __name__ == "__main__":
    unittest.main()
