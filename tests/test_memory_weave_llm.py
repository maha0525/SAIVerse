"""memory_weave_llm — weave 系ジョブの LLM 解決チェーンの回帰テスト。

背景 (2026-07-18): 編纂 (curation_ops) が独自解決で存在しない
``persona.LIGHTWEIGHT_MODEL`` を getattr し、ペルソナ設定が常に素通りして
グローバル既定モデルへ貫通していた。このテスト群はその再発を三方向から塞ぐ:

1. 解決チェーン (persona → env → builtin) の順序が正しいこと
2. 属性が欠けたオブジェクトを黙って fallback せず WARNING すること
3. PersonaCore の属性名契約 (文字列で参照される属性が実在すること) —
   属性のリネームでこのテストが先に割れる
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from saiverse.memory_weave_llm import (
    SOURCE_BUILTIN,
    SOURCE_ENV,
    SOURCE_PERSONA,
    resolve_memory_weave_model,
)


class TestResolveMemoryWeaveModel(unittest.TestCase):
    def test_persona_setting_wins(self):
        persona = SimpleNamespace(memory_weave_model="persona-model")
        with patch.dict("os.environ", {"MEMORY_WEAVE_MODEL": "env-model"}):
            model, source = resolve_memory_weave_model(persona)
        self.assertEqual(model, "persona-model")
        self.assertEqual(source, SOURCE_PERSONA)

    def test_env_fallback_when_persona_unset(self):
        persona = SimpleNamespace(memory_weave_model=None)
        with patch.dict("os.environ", {"MEMORY_WEAVE_MODEL": "env-model"}):
            model, source = resolve_memory_weave_model(persona)
        self.assertEqual(model, "env-model")
        self.assertEqual(source, SOURCE_ENV)

    def test_builtin_fallback_when_nothing_set(self):
        from saiverse.model_defaults import BUILTIN_DEFAULT_LITE_MODEL

        persona = SimpleNamespace(memory_weave_model=None)
        with patch.dict("os.environ", {}, clear=False):
            import os
            env_backup = os.environ.pop("MEMORY_WEAVE_MODEL", None)
            try:
                model, source = resolve_memory_weave_model(persona)
            finally:
                if env_backup is not None:
                    os.environ["MEMORY_WEAVE_MODEL"] = env_backup
        self.assertEqual(model, BUILTIN_DEFAULT_LITE_MODEL)
        self.assertEqual(source, SOURCE_BUILTIN)

    def test_missing_attribute_warns_instead_of_silent_none(self):
        """属性名の変更・不完全スタブは黙って fallback せず WARNING で叫ぶ。"""
        persona = SimpleNamespace()  # memory_weave_model を持たない
        with self.assertLogs("saiverse.memory_weave_llm", level="WARNING") as logs:
            resolve_memory_weave_model(persona)
        self.assertTrue(
            any("memory_weave_model" in line for line in logs.output),
            f"WARNING に欠落属性名が含まれること: {logs.output}",
        )

    def test_none_persona_warns(self):
        with self.assertLogs("saiverse.memory_weave_llm", level="WARNING"):
            model, source = resolve_memory_weave_model(None)
        self.assertIn(source, (SOURCE_ENV, SOURCE_BUILTIN))


class TestPersonaCoreAttributeContract(unittest.TestCase):
    """PersonaCore の属性名契約 — 文字列で参照される属性が実在すること。

    getattr(persona, "…", None) 系の読み手はスペルミスしても黙って None に
    なるため、参照される名前がコンストラクタ引数として実在することを固定する。
    リネーム時はこのテストが先に割れ、読み手の追従を強制する。
    """

    def test_model_setting_params_exist(self):
        import inspect

        from persona.core import PersonaCore

        params = inspect.signature(PersonaCore.__init__).parameters
        for name in ("model", "lightweight_model", "memory_weave_model"):
            self.assertIn(
                name, params,
                f"PersonaCore.__init__ に {name!r} 引数が存在すること "
                "(リネーム時は memory_weave_llm 等の読み手も追従が必要)",
            )


class TestCurationUsesWeaveChain(unittest.TestCase):
    """編纂 (run_pending_plans) の LLM がペルソナ渡しで weave 解決を通ること。"""

    def test_split_plan_resolves_llm_via_weave_chain(self):
        import sqlite3

        from sai_memory.curation_ops import enqueue_plan, init_curation_tables, run_pending_plans
        from sai_memory.memopedia.storage import init_memopedia_tables

        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys=ON")
        init_memopedia_tables(conn)
        init_curation_tables(conn)
        # split 対象ページ (root の下に 1 枚)
        import time as _time
        now = int(_time.time())
        conn.execute(
            "INSERT INTO memopedia_pages (id, title, category, content, summary,"
            " parent_id, short_id, created_at, updated_at)"
            " VALUES ('p1', '対象', 'people', '本文', '', NULL, 1, ?, ?)",
            (now, now),
        )
        conn.commit()
        enqueue_plan(
            conn, kind="split", op_id="split:memopedia:1", refs=["memopedia:1"],
        )

        class _FakeAdapter:
            def __init__(self, c):
                self.conn = c

            def append_persona_message(self, msg):
                pass

        persona_obj = SimpleNamespace(
            sai_memory=_FakeAdapter(conn),
            memory_weave_model="weave-model-under-test",
            persona_id="alice",
        )
        manager = SimpleNamespace(personas={"alice": persona_obj}, SessionLocal=None)

        with patch(
            "saiverse.memory_weave_llm.get_memory_weave_client",
            side_effect=RuntimeError("stop here (resolution verified)"),
        ) as resolver:
            run_pending_plans(manager, "alice")

        resolver.assert_called_once()
        args, kwargs = resolver.call_args
        self.assertIs(args[0], persona_obj)
        self.assertEqual(kwargs.get("purpose"), "curation")


if __name__ == "__main__":
    unittest.main()
