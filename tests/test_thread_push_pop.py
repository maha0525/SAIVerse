"""thread の push/pop 化 (統合工事 §6-6a、SEA 監査 S4 根治) の検証。

intent: docs/intent/beat_execution_context.md 不変条件6「thread は実行の属性」。

- PulseContext.push_thread / pop_thread が persona の active_state.json を
  切替・復元する (①)
- Stelis 区間内の例外で end ノード不達でも、graph 実行の finally
  (unwind_threads) が親 thread へ復元する (②)
- 入れ子 push は各実行の入口深さまでしか巻き戻さない (③)
- push 状態でプロセスが死んだ場合、pulse_scoped_parent マーカーから
  ペルソナ登録経路の adapter 初期化が親へ復元する (④)
- thread_switch (恒久切替) はマーカーを消す — クラッシュ復旧が恒久選択を
  打ち消さない (⑤)
- PulseContext.thread_id (生成時固定の死に値) 廃止後、pulse_logs の thread
  記帳は flush 時点の adapter 現在値 (⑥)
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sea.pulse_context import PulseContext, PulseLogEntry


class _AdapterFixture(unittest.TestCase):
    """実 SAIMemoryAdapter (memory 無効 = ファイル操作のみ) を temp dir に作る土台。

    SAIMEMORY_MEMORY=0 にすると adapter は conn を作らず即 return するが、
    active_state.json の読み書き (set_active_thread / get_current_thread /
    pulse_scoped_parent) と孤児復旧はすべて生きている — thread push/pop の
    検証にはこれで十分で、embedder のモックが要らない。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.persona_dir = Path(self._tmp.name) / "personas" / "tester"
        self.persona_dir.mkdir(parents=True, exist_ok=True)
        self._old_env = os.environ.get("SAIMEMORY_MEMORY")
        os.environ["SAIMEMORY_MEMORY"] = "0"
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        if self._old_env is None:
            os.environ.pop("SAIMEMORY_MEMORY", None)
        else:
            os.environ["SAIMEMORY_MEMORY"] = self._old_env

    def _adapter(self, recover: bool = False):
        from saiverse_memory import SAIMemoryAdapter
        return SAIMemoryAdapter(
            "tester",
            persona_dir=self.persona_dir,
            resource_id="tester",
            recover_orphaned_thread=recover,
        )

    @property
    def state_file(self) -> Path:
        return self.persona_dir / "active_state.json"

    def _state(self) -> dict:
        return json.loads(self.state_file.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# ① push/pop で active_state.json が切替 → 復元される
# ---------------------------------------------------------------------------


class PushPopActiveStateTest(_AdapterFixture):
    def test_push_switches_and_pop_restores(self) -> None:
        adapter = self._adapter()
        adapter.set_active_thread("tester:main")
        ctx = PulseContext(pulse_id="p1")

        parent = ctx.push_thread(adapter, "tester:child")
        self.assertEqual(parent, "tester:main")
        self.assertEqual(adapter.get_current_thread(), "tester:child")
        self.assertEqual(ctx.thread_stack_depth(), 1)
        data = self._state()
        self.assertEqual(data["active_thread_id"], "child")
        # クラッシュ復旧マーカー (最外周の親) が書かれている
        self.assertEqual(data["pulse_scoped_parent"], "main")

        restored = ctx.pop_thread(adapter)
        self.assertEqual(restored, "tester:main")
        self.assertEqual(adapter.get_current_thread(), "tester:main")
        self.assertEqual(ctx.thread_stack_depth(), 0)
        # 復元後はマーカーが消えている
        self.assertNotIn("pulse_scoped_parent", self._state())

    def test_pop_on_empty_stack_is_noop(self) -> None:
        adapter = self._adapter()
        adapter.set_active_thread("tester:main")
        ctx = PulseContext(pulse_id="p1")
        self.assertIsNone(ctx.pop_thread(adapter))
        self.assertEqual(adapter.get_current_thread(), "tester:main")

    def test_push_without_active_state_uses_default_thread(self) -> None:
        # active_state.json が無い状態からの push: 親は既定 thread に解決される
        adapter = self._adapter()
        ctx = PulseContext(pulse_id="p1")
        parent = ctx.push_thread(adapter, "tester:child")
        self.assertEqual(parent, "tester:__persona__")
        restored = ctx.pop_thread(adapter)
        self.assertEqual(restored, "tester:__persona__")
        self.assertEqual(adapter.get_current_thread(), "tester:__persona__")


# ---------------------------------------------------------------------------
# ② Stelis 区間内の例外 → graph の finally が親へ復元 (end ノード不達シナリオ)
# ---------------------------------------------------------------------------


class GraphFinallyRestoreTest(_AdapterFixture):
    def test_exception_mid_stelis_restores_parent_thread(self) -> None:
        import sea.runtime_graph as rg
        from llm_clients.exceptions import LLMError
        from sea.runtime import SEARuntime

        adapter = self._adapter()
        adapter.set_active_thread("tester:main")

        runtime = SEARuntime(SimpleNamespace(building_histories={}))
        runtime._is_spell_enabled_for_persona = lambda p: False
        persona = SimpleNamespace(
            persona_id="tester",
            sai_memory=adapter,
            execution_state={},
        )
        playbook = SimpleNamespace(
            name="pb", start_node="n1", input_schema=[], output_schema=[],
            report_template=None,
        )

        def fake_compile(playbook, **factories):
            async def compiled(initial_state, config):
                # Stelis start 相当: 子 thread へ push した直後に例外
                # (stelis_end ノードには到達しない)
                ctx = initial_state["_pulse_context"]
                ctx.push_thread(adapter, "tester:stelis-child")
                assert adapter.get_current_thread() == "tester:stelis-child"
                raise RuntimeError("boom before stelis_end")
            return compiled

        with patch.object(rg, "compile_playbook", fake_compile):
            with self.assertRaises(LLMError):
                rg.compile_with_langgraph(
                    runtime, playbook, persona, "b1", None, False, [], "pulse-x",
                )

        # finally の unwind_threads が親 thread へ復元し、マーカーも消えている
        self.assertEqual(adapter.get_current_thread(), "tester:main")
        self.assertNotIn("pulse_scoped_parent", self._state())


# ---------------------------------------------------------------------------
# ③ 入れ子 push の unwind 順序 (各実行は自分の入口深さまでしか巻き戻さない)
# ---------------------------------------------------------------------------


class NestedUnwindTest(_AdapterFixture):
    def test_nested_unwind_respects_entry_depth(self) -> None:
        adapter = self._adapter()
        adapter.set_active_thread("tester:main")
        ctx = PulseContext(pulse_id="p1")

        ctx.push_thread(adapter, "tester:c1")
        entry_depth = ctx.thread_stack_depth()  # 入れ子実行の入口 (=1)
        ctx.push_thread(adapter, "tester:c2")
        # マーカーは常に最外周の親を指す
        self.assertEqual(self._state()["pulse_scoped_parent"], "main")

        # 入れ子実行の finally: 自分の push (c2) だけ巻き戻し、親の push は残す
        popped = ctx.unwind_threads(adapter, entry_depth)
        self.assertEqual(popped, 1)
        self.assertEqual(adapter.get_current_thread(), "tester:c1")
        self.assertEqual(ctx.thread_stack_depth(), 1)
        self.assertEqual(self._state()["pulse_scoped_parent"], "main")

        # 最外周の finally: 残りを巻き戻して原点へ
        popped = ctx.unwind_threads(adapter, 0)
        self.assertEqual(popped, 1)
        self.assertEqual(adapter.get_current_thread(), "tester:main")
        self.assertEqual(ctx.thread_stack_depth(), 0)
        self.assertNotIn("pulse_scoped_parent", self._state())

    def test_unwind_at_entry_depth_is_noop(self) -> None:
        adapter = self._adapter()
        adapter.set_active_thread("tester:main")
        ctx = PulseContext(pulse_id="p1")
        ctx.push_thread(adapter, "tester:c1")
        ctx.pop_thread(adapter)  # 正常系: ノード自身が pop 済み
        self.assertEqual(ctx.unwind_threads(adapter, 0), 0)
        self.assertEqual(adapter.get_current_thread(), "tester:main")


# ---------------------------------------------------------------------------
# ④ クラッシュ孤児の復旧 (pulse_scoped_parent が残った状態での adapter 初期化)
# ---------------------------------------------------------------------------


class OrphanRecoveryTest(_AdapterFixture):
    def _write_orphaned_state(self) -> None:
        self.state_file.write_text(
            json.dumps({
                "active_thread_id": "stelis-child",
                "updated_at": "2026-07-17T00:00:00",
                "pulse_scoped_parent": "main",
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def test_registration_init_recovers_orphaned_thread(self) -> None:
        self._write_orphaned_state()
        adapter = self._adapter(recover=True)
        self.assertEqual(adapter.get_current_thread(), "tester:main")
        data = self._state()
        self.assertEqual(data["active_thread_id"], "main")
        self.assertNotIn("pulse_scoped_parent", data)

    def test_throwaway_adapter_does_not_recover(self) -> None:
        # ツール/API の使い捨て adapter (recover_orphaned_thread=False) は
        # 走行中の Stelis を誤って巻き戻さない
        self._write_orphaned_state()
        adapter = self._adapter(recover=False)
        self.assertEqual(adapter.get_current_thread(), "tester:stelis-child")
        self.assertEqual(self._state()["pulse_scoped_parent"], "main")

    def test_recovery_without_marker_is_noop(self) -> None:
        self.state_file.write_text(
            json.dumps({"active_thread_id": "main"}), encoding="utf-8",
        )
        adapter = self._adapter(recover=True)
        self.assertEqual(adapter.get_current_thread(), "tester:main")


# ---------------------------------------------------------------------------
# ⑤ thread_switch (恒久切替) との相互作用
# ---------------------------------------------------------------------------


class PermanentSwitchInteractionTest(_AdapterFixture):
    def test_permanent_switch_clears_marker_but_pop_still_restores(self) -> None:
        adapter = self._adapter()
        adapter.set_active_thread("tester:main")
        ctx = PulseContext(pulse_id="p1")
        ctx.push_thread(adapter, "tester:stelis-child")
        self.assertEqual(self._state()["pulse_scoped_parent"], "main")

        # Stelis 中の恒久切替 (thread_switch ツールの _write_active_state 相当):
        # マーカーは消える = クラッシュ復旧が恒久選択を「Stelis 前の親」へ
        # 巻き戻さない (§6-6a 項目5 の裁定)
        from tool_loader import load_builtin_tool
        thread_switch = load_builtin_tool("thread_switch")
        thread_switch._write_active_state(
            self.state_file, "permanent", "2026-07-17T00:00:00+00:00",
        )
        data = self._state()
        self.assertEqual(data["active_thread_id"], "permanent")
        self.assertNotIn("pulse_scoped_parent", data)

        # プロセス内の Pulse 終端復元は従来どおりスタック記録の親へ戻る
        # (Stelis end の既存挙動維持)
        restored = ctx.pop_thread(adapter)
        self.assertEqual(restored, "tester:main")
        self.assertEqual(adapter.get_current_thread(), "tester:main")


# ---------------------------------------------------------------------------
# ⑥ PulseContext.thread_id 廃止後の pulse_logs thread 記帳
# ---------------------------------------------------------------------------


class FlushThreadBookkeepingTest(unittest.TestCase):
    def test_pulse_context_has_no_thread_id_field(self) -> None:
        ctx = PulseContext(pulse_id="p1")
        self.assertFalse(hasattr(ctx, "thread_id"))

    def test_flush_records_flush_time_thread(self) -> None:
        from sea.runtime import SEARuntime

        runtime = SEARuntime(SimpleNamespace(building_histories={}))
        recorded: list = []

        def _append_pulse_log(**kwargs):
            recorded.append(kwargs)
            return "log-id"

        fake_adapter = SimpleNamespace(
            is_ready=lambda: True,
            get_current_thread=lambda: "tester:current-at-flush",
            append_pulse_log=_append_pulse_log,
        )
        persona = SimpleNamespace(persona_id="tester", sai_memory=fake_adapter)
        ctx = PulseContext(pulse_id="p-flush")
        ctx.append(PulseLogEntry(role="assistant", content="hello"))
        ctx.append(PulseLogEntry(role="user", content="world"))

        runtime._flush_pulse_logs(persona, ctx)

        self.assertEqual(len(recorded), 2)
        for entry in recorded:
            # 記帳は flush 時点の adapter 現在値 (生成時固定値の持ち回りは廃止)
            self.assertEqual(entry["thread_id"], "tester:current-at-flush")


if __name__ == "__main__":
    unittest.main()
