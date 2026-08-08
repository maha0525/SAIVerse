"""tell スペル (builtin_data/tools/tell.py) のテスト。

autonomous_pulse_vehicle.md §B の契約:

- 宛先の検証 (user / all / 同室ペルソナのみ。不在の相手には理由文を返し LLM を呼ばない)
- 会話中のユーザーへの tell は no-op + 教育文 (返答との二重発話の防止)
- 言葉は CONVERSATION aspect の 1 Beat (標準モデル側) が書く
- 投函は _emit_say (Building 履歴 + UI + TTS) + 本人の記憶 (_store_memory)、
  metadata に宛先 (tell_target) が残る
- 会話エピソード・Track・タイムアウトには一切触らない (このテストの fake には
  そもそもその口が無い = 呼べば AttributeError で落ちる)
"""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import patch

from sea.pulse_context import Aspect, PulseContext
from tools.context import persona_context

PERSONA_ID = "p1"
BUILDING = "cafe"


class FakeLLMClient:
    def __init__(self, responses: List[Any]):
        self.responses = list(responses)
        self.calls: List[List[Dict[str, Any]]] = []

    def generate(self, messages, tools=None, temperature=None, **kwargs):
        self.calls.append(list(messages))
        return self.responses.pop(0)

    def consume_usage(self):
        return None


class FakeRuntime:
    def __init__(self, client: FakeLLMClient):
        self.llm_client = client
        self.emitted: List[Dict[str, Any]] = []
        self.stored: List[Dict[str, Any]] = []
        self.flushed = False
        self.selected_aspects: List[Any] = []
        self._pulse_contexts: Dict[str, PulseContext] = {}

    def _get_or_create_pulse_context(self, pulse_id: str) -> PulseContext:
        self._pulse_contexts.setdefault(pulse_id, PulseContext(pulse_id=pulse_id))
        return self._pulse_contexts[pulse_id]

    def _prepare_context(self, persona, building_id, user_input, **kwargs):
        return [{"role": "system", "content": "head"}]

    def select_llm_client(self, node_def, persona, execution_context=None, state=None, **kw):
        pulse_ctx = state.get("_pulse_context") if state else None
        frame = pulse_ctx.current_line() if pulse_ctx is not None else None
        self.selected_aspects.append(getattr(frame, "aspect", None))
        return self.llm_client, execution_context.model_key

    def _default_temperature(self, persona):
        return None

    def _get_cache_kwargs(self, persona_id=None):
        return {}

    def _dump_llm_io(self, playbook_name, node_id, persona, messages, text):
        pass

    def _emit_say(self, persona, building_id, text, pulse_id=None, metadata=None):
        self.emitted.append({
            "building_id": building_id, "text": text, "metadata": metadata,
        })
        return {"message_id": "m1"}

    def _store_memory(self, persona, text, **kwargs):
        self.stored.append({"text": text, **kwargs})

    def _flush_pulse_logs(self, persona, pulse_ctx):
        self.flushed = True


def _make_env(responses: List[Any], occupants: List[str] | None = None):
    client = FakeLLMClient(responses)
    runtime = FakeRuntime(client)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, persona_name="アリス", current_building_id=BUILDING,
    )
    other = SimpleNamespace(
        persona_id="p2", persona_name="ベル", current_building_id=BUILDING,
    )
    manager = SimpleNamespace(
        personas={PERSONA_ID: persona, "p2": other},
        occupants={BUILDING: occupants if occupants is not None else [PERSONA_ID, "p2"]},
        sea_runtime=runtime,
    )
    return manager, runtime, client


@contextmanager
def _ctx(manager):
    with persona_context(PERSONA_ID, "/tmp/p1", manager=manager):
        yield


def _fake_exec_ctx():
    ctx = SimpleNamespace(model_key="std-model")
    ctx.with_model = lambda m: SimpleNamespace(model_key=m, with_model=ctx.with_model)
    return ctx


def _patched_resolve(runtime):
    """resolve_execution_context を差し替え、呼び出し時の active aspect を記録する。"""
    def fake_resolve(persona, pulse_ctx):
        frame = pulse_ctx.current_line()
        runtime.selected_aspects.append(getattr(frame, "aspect", None))
        return _fake_exec_ctx()
    return patch("sea.pulse_context.resolve_execution_context", side_effect=fake_resolve)


def test_tell_user_composes_on_conversation_aspect_and_delivers():
    from builtin_data.tools.tell import tell

    manager, runtime, client = _make_env(["まはー、面白い記事を見つけたよ。"])
    with _ctx(manager), _patched_resolve(runtime):
        result = tell(target="user", gist="記事の共有")

    assert "ユーザー" in result and "声をかけました" in result
    # 言葉を書く Beat は CONVERSATION aspect で解決された (→ 標準モデル)
    assert Aspect.CONVERSATION in runtime.selected_aspects
    # 投函: Building 履歴へ 1 通、宛先が metadata に残る
    assert len(runtime.emitted) == 1
    assert runtime.emitted[0]["building_id"] == BUILDING
    assert runtime.emitted[0]["metadata"]["tell_target"] == "user"
    assert runtime.emitted[0]["metadata"]["tell_gist"] == "記事の共有"
    assert "面白い記事" in runtime.emitted[0]["text"]
    # 本人の記憶にも発話として残る
    assert len(runtime.stored) == 1
    assert runtime.stored[0]["metadata"]["tell_target"] == "user"
    # 指示 (directive) に宛先と gist が入っている
    directive = client.calls[0][-1]["content"]
    assert "ユーザー" in directive
    assert "記事の共有" in directive
    # ライン frame は後始末され、pulse ログは flush 済み
    assert runtime.flushed


def test_tell_persona_by_name_resolves_to_id():
    from builtin_data.tools.tell import tell

    manager, runtime, client = _make_env(["ベル、ちょっといい？"])
    with _ctx(manager), _patched_resolve(runtime):
        result = tell(target="ベル")

    assert "ベル" in result and "声をかけました" in result
    assert runtime.emitted[0]["metadata"]["tell_target"] == "p2"
    assert "tell_gist" not in runtime.emitted[0]["metadata"]


def test_tell_unknown_target_returns_reason_without_llm():
    from builtin_data.tools.tell import tell

    manager, runtime, client = _make_env(["(呼ばれないはず)"])
    with _ctx(manager), _patched_resolve(runtime):
        result = tell(target="どこかの誰か")

    assert "この場所にいません" in result
    assert "ベル" in result  # 声をかけられる相手の提示
    assert client.calls == []
    assert runtime.emitted == []


def test_tell_user_during_conversation_is_noop_with_guidance():
    from builtin_data.tools.tell import tell

    manager, runtime, client = _make_env(["(呼ばれないはず)"])
    with _ctx(manager), _patched_resolve(runtime), patch(
        "saiverse.day_plan.is_in_user_conversation", return_value=True,
    ):
        result = tell(target="user")

    assert "会話の最中" in result
    assert "返答" in result
    assert client.calls == []
    assert runtime.emitted == []


def test_tell_all_during_conversation_is_allowed():
    """会話中でも user 以外 (all / 同席ペルソナ) への一言は塞がない。"""
    from builtin_data.tools.tell import tell

    manager, runtime, client = _make_env(["みんな、聞いて。"])
    with _ctx(manager), _patched_resolve(runtime), patch(
        "saiverse.day_plan.is_in_user_conversation", return_value=True,
    ):
        result = tell(target="all")

    assert "声をかけました" in result
    assert runtime.emitted[0]["metadata"]["tell_target"] == "all"


def test_tell_empty_generation_does_not_emit():
    from builtin_data.tools.tell import tell

    manager, runtime, client = _make_env(["   "])
    with _ctx(manager), _patched_resolve(runtime):
        result = tell(target="user")

    assert "言葉が出てきませんでした" in result
    assert runtime.emitted == []
    assert runtime.stored == []
