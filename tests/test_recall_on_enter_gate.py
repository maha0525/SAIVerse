"""入室想起の「再会の門」と見出しの名前解決の契約テスト (v0.3.9)。

門が配線されていなかったため、ずっと会話している相手にも移動のたびに
「過去会話 6 件 + 相手の Memopedia 個人ページ全文」が積まれ、本番で知覚が
18 万字まで膨らんだ (docs/issues/persona_recall_perception_unbounded.md)。

ここでは繋ぎ実装 (`_inject_persona_recall_on_enter`) と本物の
`HistoryManager.should_recall_persona` / `recall_conversation_with` を繋いだまま
検査する — 門を呼ぶ配線と、見出しに ID 生値ではなく表示名が載ることの両方が
壊れたら落ちるようにするため。
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from persona.history_manager import HistoryManager
from sea.head_pipeline.integration import _inject_persona_recall_on_enter
from sea.head_pipeline.types import NotificationLabel

TARGET = "elis_city_a"


def _enter_label(occupant_id, occupant_kind="persona"):
    return NotificationLabel(
        kind="occupant_entered",
        label=f"{occupant_id} が入室しました",
        metadata={"occupant_id": occupant_id, "occupant_kind": occupant_kind},
    )


class _FakeAdapter:
    """SAIMemory adapter のうち recall が触る面だけの替え玉。"""

    def __init__(self, past_messages):
        self._past_messages = past_messages
        self.conn = object()  # memopedia storage へ渡されるだけ (patch 済み)

    def is_ready(self):
        return True

    def get_messages_with_persona_in_audience(self, target_persona_id, **kwargs):
        return self._past_messages


class RecallGateTests(unittest.TestCase):
    def setUp(self):
        self.pushed = []
        self.past_messages = [
            {"role": "assistant", "content": "また会えたね", "created_at": 1750000000},
        ]
        self.sai_mem = SimpleNamespace(
            push_perception=lambda kind, text: self.pushed.append((kind, text)),
        )

    def _make_persona(self, *, messages, id_to_name_map=None, memopedia_content=None):
        hm = HistoryManager(
            persona_id="air_city_a",
            persona_log_path=Path("/mock/personas/air_city_a/log.json"),
            building_memory_paths={},
            initial_persona_history=list(messages),
            memory_adapter=_FakeAdapter(self.past_messages),
        )
        self.memopedia_page = (
            SimpleNamespace(content=memopedia_content)
            if memopedia_content is not None else None
        )
        return SimpleNamespace(
            history_manager=hm,
            id_to_name_map=dict(id_to_name_map or {}),
        )

    def _run(self, persona, label):
        with patch(
            "sai_memory.memopedia.storage.get_page_by_persona_id",
            return_value=self.memopedia_page,
        ):
            _inject_persona_recall_on_enter(persona, [label], self.sai_mem)

    def test_recent_contact_is_not_recalled(self):
        """直近の文脈に相手が居るなら、再入室しても想起は積まれない。"""
        persona = self._make_persona(messages=[
            {"role": "assistant", "content": "うん", "persona_id": TARGET},
        ])
        self._run(persona, _enter_label(TARGET))
        self.assertEqual(self.pushed, [])

    def test_recent_contact_via_audience_is_not_recalled(self):
        """自分の発言でも、audience に相手が居れば「一緒に居る」と数える。"""
        persona = self._make_persona(messages=[
            {
                "role": "assistant",
                "content": "そうだね",
                "persona_id": "air_city_a",
                "metadata": {"audience": {"personas": [TARGET]}},
            },
        ])
        self._run(persona, _enter_label(TARGET))
        self.assertEqual(self.pushed, [])

    def test_long_absent_contact_is_recalled(self):
        """久しぶりの相手なら従来どおり想起する。"""
        persona = self._make_persona(
            messages=[{"role": "user", "content": "ひとりごと"}],
            id_to_name_map={TARGET: "エリス"},
        )
        self._run(persona, _enter_label(TARGET))
        self.assertEqual(len(self.pushed), 1)
        kind, text = self.pushed[0]
        self.assertEqual(kind, "persona_recall")
        self.assertIn("また会えたね", text)

    def test_heading_uses_display_name(self):
        """見出しは ID 生値ではなく表示名で書く (過去会話・Memopedia の両方)。"""
        persona = self._make_persona(
            messages=[],
            id_to_name_map={"1": "まはー"},
            memopedia_content="まはーについての記録",
        )
        self._run(persona, _enter_label("1", occupant_kind="user"))
        self.assertEqual(len(self.pushed), 1)
        text = self.pushed[0][1]
        self.assertIn("[想起: まはーとの過去の会話]", text)
        self.assertIn("[想起: まはーについてのMemopedia記録]", text)
        self.assertNotIn("[想起: 1との過去の会話]", text)

    def test_unresolvable_id_stays_raw(self):
        """表示名が引けないときだけ ID のままにする。"""
        persona = self._make_persona(messages=[], id_to_name_map={})
        self._run(persona, _enter_label(TARGET))
        self.assertEqual(len(self.pushed), 1)
        self.assertIn(f"[想起: {TARGET}との過去の会話]", self.pushed[0][1])

    def test_gate_failure_falls_back_to_recall(self):
        """門の判定が壊れたら、想起する側に倒す (再会の記憶を黙って失わない)。"""
        def _boom(*args, **kwargs):
            raise RuntimeError("gate broken")

        persona = SimpleNamespace(
            history_manager=SimpleNamespace(
                should_recall_persona=_boom,
                recall_conversation_with=lambda occupant_id, **kwargs: "recall",
            ),
            id_to_name_map={},
        )
        _inject_persona_recall_on_enter(persona, [_enter_label(TARGET)], self.sai_mem)
        self.assertEqual(self.pushed, [("persona_recall", "recall")])


if __name__ == "__main__":
    unittest.main()
