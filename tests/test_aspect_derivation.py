"""認知モデル v0.2: アスペクト (Aspect) の導出と LineFrame 連動の単体テスト。

line_tag_responsibility.md §10。アスペクトは「呼び出し時に1つ指定」される
唯一の供給源で、line_role / scope / model tier を導出する。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sea.pulse_context import (  # noqa: E402
    Aspect,
    LineFrame,
    PulseContext,
    aspect_from_pulse_type,
)


class TestAspectDerivation(unittest.TestCase):
    def test_derivation_table(self):
        # (line_role, scope, model_tier) — §10.2 の表どおり
        self.assertEqual(
            (Aspect.CONVERSATION.line_role, Aspect.CONVERSATION.scope, Aspect.CONVERSATION.model_tier),
            ("main_line", "committed", "standard"),
        )
        self.assertEqual(
            (Aspect.WORKER.line_role, Aspect.WORKER.scope, Aspect.WORKER.model_tier),
            ("sub_line", "volatile", "lightweight"),
        )
        self.assertEqual(
            (Aspect.AUTONOMOUS.line_role, Aspect.AUTONOMOUS.scope, Aspect.AUTONOMOUS.model_tier),
            ("main_line", "committed", "lightweight"),
        )
        self.assertEqual(
            (Aspect.META.line_role, Aspect.META.scope, Aspect.META.model_tier),
            ("meta_judgment", "discardable", "standard"),
        )

    def test_aspect_from_pulse_type(self):
        self.assertEqual(aspect_from_pulse_type("auto"), Aspect.AUTONOMOUS)
        self.assertEqual(aspect_from_pulse_type("meta_judgment"), Aspect.META)
        self.assertEqual(aspect_from_pulse_type("user"), Aspect.CONVERSATION)
        self.assertEqual(aspect_from_pulse_type("schedule"), Aspect.CONVERSATION)
        self.assertEqual(aspect_from_pulse_type(None), Aspect.CONVERSATION)


class TestLineFrameAspect(unittest.TestCase):
    def test_frame_derives_role_and_scope_from_aspect(self):
        frame = LineFrame(aspect=Aspect.WORKER)
        self.assertEqual(frame.role, "sub_line")       # __post_init__ で導出
        self.assertEqual(frame.scope, "volatile")       # property
        self.assertEqual(frame.model_tier, "lightweight")

    def test_aspect_overrides_explicit_role(self):
        # aspect が設定されていれば role 引数は無視されアスペクトから導出される
        frame = LineFrame(role="main_line", aspect=Aspect.WORKER)
        self.assertEqual(frame.role, "sub_line")

    def test_legacy_frame_without_aspect(self):
        frame = LineFrame(role="main_line")
        self.assertIsNone(frame.aspect)
        self.assertEqual(frame.role, "main_line")
        self.assertIsNone(frame.scope)        # legacy: scope 未導出 → _store_memory で committed 既定
        self.assertIsNone(frame.model_tier)


class TestPushLineMetadata(unittest.TestCase):
    def test_push_aspect_exposes_line_role_and_scope(self):
        pc = PulseContext(pulse_id="p1", thread_id="t1")
        pc.push_line(aspect=Aspect.WORKER, track_id="trk")
        meta = pc.current_line_metadata()
        self.assertEqual(meta["line_role"], "sub_line")
        self.assertEqual(meta["scope"], "volatile")
        self.assertEqual(meta["origin_track_id"], "trk")

    def test_autonomous_root_then_worker_subline(self):
        # Pulse-root = AUTONOMOUS (main_line/committed), その下に WORKER サブライン
        pc = PulseContext(pulse_id="p1", thread_id="t1")
        pc.push_line(aspect=Aspect.AUTONOMOUS, track_id="trk")
        root_meta = pc.current_line_metadata()
        self.assertEqual(root_meta["line_role"], "main_line")
        self.assertEqual(root_meta["scope"], "committed")

        sub = pc.push_line(aspect=Aspect.WORKER)
        sub_meta = pc.current_line_metadata()
        self.assertEqual(sub_meta["line_role"], "sub_line")
        self.assertEqual(sub_meta["scope"], "volatile")
        # WORKER サブラインは AUTONOMOUS root を親として track_id を継承
        self.assertEqual(sub.track_id, "trk")
        self.assertIsNotNone(sub.parent_id)

        pc.pop_line()
        # pop 後は root (AUTONOMOUS) に戻る
        self.assertEqual(pc.current_line_metadata()["scope"], "committed")

    def test_legacy_metadata_scope_none(self):
        pc = PulseContext(pulse_id="p1", thread_id="t1")
        pc.push_line(role="main_line")   # legacy: aspect なし
        self.assertIsNone(pc.current_line_metadata()["scope"])

    def test_empty_stack_metadata(self):
        pc = PulseContext(pulse_id="p1", thread_id="t1")
        meta = pc.current_line_metadata()
        self.assertIsNone(meta["line_role"])
        self.assertIsNone(meta["scope"])


class TestMemorizeBoolAcceptance(unittest.TestCase):
    """§10.6 の field 剥がしで memorize が空 dict → ``true`` になるため、
    LLMNodeDef.memorize は bool も受理する必要がある (v0.1 schema は dict 限定で、
    track_user_conversation が DB ロードに失敗→basic_chat フォールバック→無発言の
    リグレッションを起こした)。"""

    def test_llm_node_memorize_accepts_bool(self):
        from sea.playbook_models import LLMNodeDef

        node = LLMNodeDef(id="x", type="llm", action="hi", memorize=True)
        self.assertIs(node.memorize, True)

    def test_llm_node_memorize_accepts_dict(self):
        from sea.playbook_models import LLMNodeDef

        node = LLMNodeDef(id="x", type="llm", action="hi", memorize={"tags": ["creation"]})
        self.assertEqual(node.memorize, {"tags": ["creation"]})

    def test_playbook_schema_roundtrips_memorize_true(self):
        # 剥がし後の典型形 (memorize: true, line_role/scope なし) が PlaybookSchema を通る
        from sea.playbook_models import PlaybookSchema

        data = {
            "name": "t",
            "description": "test",
            "input_schema": [],
            "nodes": [
                {"id": "main", "type": "llm", "action": "reply", "speak": True,
                 "memorize": True, "next": None},
            ],
            "start_node": "main",
        }
        pb = PlaybookSchema(**data)
        self.assertIs(pb.nodes[0].memorize, True)


if __name__ == "__main__":
    unittest.main()
