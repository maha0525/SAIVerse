"""ExecutionContext — 実行の身分証の解決と LLM client 選択の挙動固定。

intent: docs/intent/beat_execution_context.md §2.1 / §6 移行手順 1。
本段階は「挙動不変の置換」— resolve_execution_context の model_key と
select_llm_client の選択結果が、従来の暗黙推測 (_select_llm_client) と
同一であることをここで固定する。
"""

import dataclasses
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from sea.pulse_context import (
    Aspect,
    ExecutionContext,
    PulseContext,
    default_lightweight_model,
    resolve_execution_context,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _persona(**over):
    base = dict(
        persona_id="p1",
        persona_name="Persona",
        model="standard-model",
        llm_client=object(),
        lightweight_llm_client=object(),
        lightweight_model="lite-model",
        sai_memory=SimpleNamespace(get_current_thread=lambda: "p1:persona_main"),
    )
    base.update(over)
    return SimpleNamespace(**base)


def _pulse_ctx(aspect=None, role="main_line"):
    ctx = PulseContext(pulse_id="pulse-1")
    if aspect is not None:
        ctx.push_line(aspect=aspect)
    else:
        ctx.push_line(role=role)  # legacy frame (aspect なし)
    return ctx


def _runtime():
    from sea.runtime import SEARuntime
    return SEARuntime(SimpleNamespace(building_histories={}))


_NODE = SimpleNamespace(id="test_node", memorize=None, speak=False)


# ---------------------------------------------------------------------------
# resolve_execution_context — aspect → model_key (従来の選択結果と一致)
# ---------------------------------------------------------------------------


class TestResolveByAspect:
    def test_conversation_uses_standard_model(self):
        persona = _persona()
        ctx = _pulse_ctx(Aspect.CONVERSATION)
        ec = resolve_execution_context(persona, ctx)
        assert ec.model_key == "standard-model"
        assert ec.aspect is Aspect.CONVERSATION
        assert ec.persona_id == "p1"
        assert ec.thread_id == "p1:persona_main"
        assert ec.pulse_id == "pulse-1"
        assert ec.line_id == ctx.current_line().line_id
        assert ec.execution_id is None  # 本段階では常に None

    def test_meta_uses_standard_model(self):
        ec = resolve_execution_context(_persona(), _pulse_ctx(Aspect.META))
        assert ec.model_key == "standard-model"

    def test_worker_uses_lightweight_model(self):
        ec = resolve_execution_context(_persona(), _pulse_ctx(Aspect.WORKER))
        assert ec.model_key == "lite-model"

    def test_autonomous_uses_lightweight_model(self):
        ec = resolve_execution_context(_persona(), _pulse_ctx(Aspect.AUTONOMOUS))
        assert ec.model_key == "lite-model"

    def test_lightweight_unset_falls_back_to_env(self, monkeypatch):
        monkeypatch.setenv("SAIVERSE_DEFAULT_LIGHTWEIGHT_MODEL", "env-lite")
        persona = _persona(lightweight_model=None)
        ec = resolve_execution_context(persona, _pulse_ctx(Aspect.WORKER))
        assert ec.model_key == "env-lite"

    def test_lightweight_unset_falls_back_to_builtin(self, monkeypatch):
        monkeypatch.delenv("SAIVERSE_DEFAULT_LIGHTWEIGHT_MODEL", raising=False)
        from saiverse.model_defaults import BUILTIN_DEFAULT_LITE_MODEL
        persona = _persona(lightweight_model=None)
        ec = resolve_execution_context(persona, _pulse_ctx(Aspect.AUTONOMOUS))
        assert ec.model_key == BUILTIN_DEFAULT_LITE_MODEL
        assert ec.model_key == default_lightweight_model()


# ---------------------------------------------------------------------------
# resolve_execution_context — legacy fallback (frame なし / aspect なし frame)
# ---------------------------------------------------------------------------


class TestResolveLegacyFallback:
    def test_no_pulse_context_defaults_to_standard(self):
        ec = resolve_execution_context(_persona(), None)
        assert ec.model_key == "standard-model"
        assert ec.aspect is None
        assert ec.line_id == ""
        assert ec.pulse_id == ""

    def test_no_pulse_context_reads_pulse_id_from_state(self):
        ec = resolve_execution_context(_persona(), None, state={"_pulse_id": "px"})
        assert ec.pulse_id == "px"

    def test_legacy_frame_without_aspect_defaults_to_standard(self):
        ctx = _pulse_ctx(aspect=None)  # role 直指定の legacy frame
        ec = resolve_execution_context(_persona(), ctx, state={})
        assert ec.model_key == "standard-model"
        assert ec.aspect is None
        assert ec.line_id == ctx.current_line().line_id

    def test_legacy_force_lightweight_flag(self):
        ec = resolve_execution_context(
            _persona(), _pulse_ctx(aspect=None),
            state={"_force_lightweight_model": True},
        )
        assert ec.model_key == "lite-model"

    def test_legacy_auto_pulse_type(self):
        ec = resolve_execution_context(
            _persona(), _pulse_ctx(aspect=None), state={"_pulse_type": "auto"},
        )
        assert ec.model_key == "lite-model"

    def test_thread_id_empty_without_adapter(self):
        # PulseContext.thread_id (生成時固定の死に値) は廃止 — adapter が無い
        # 実行では thread_id は空になる (beat_execution_context.md §3.4)。
        persona = _persona(sai_memory=None)
        ctx = _pulse_ctx(Aspect.CONVERSATION)
        ec = resolve_execution_context(persona, ctx)
        assert ec.thread_id == ""


# ---------------------------------------------------------------------------
# ExecutionContext — 不変性と with_model
# ---------------------------------------------------------------------------


class TestExecutionContextValue:
    def _ec(self):
        return ExecutionContext(
            persona_id="p1", thread_id="t1", line_id="l1",
            aspect=Aspect.CONVERSATION, model_key="standard-model",
            pulse_id="pulse-1",
        )

    def test_frozen(self):
        ec = self._ec()
        with pytest.raises(dataclasses.FrozenInstanceError):
            ec.model_key = "other"

    def test_with_model_replaces_only_model_key(self):
        ec = self._ec()
        ec2 = ec.with_model("fallback-model")
        assert ec2 is not ec
        assert ec2.model_key == "fallback-model"
        assert ec.model_key == "standard-model"  # 元は不変
        assert (ec2.persona_id, ec2.thread_id, ec2.line_id, ec2.aspect, ec2.pulse_id, ec2.execution_id) == (
            ec.persona_id, ec.thread_id, ec.line_id, ec.aspect, ec.pulse_id, ec.execution_id,
        )


# ---------------------------------------------------------------------------
# select_llm_client — ExecutionContext 経路が従来の選択結果と一致すること
# ---------------------------------------------------------------------------


class TestSelectLLMClientParity:
    def _select_both(self, persona, ctx, state=None, needs_structured_output=False):
        """新経路 (ec) と legacy 経路 (_select_llm_client) の選択結果を並べて返す。"""
        runtime = _runtime()
        state = dict(state or {})
        state["_pulse_context"] = ctx
        ec = resolve_execution_context(persona, ctx, state=state)
        client, model = runtime.select_llm_client(
            _NODE, persona, execution_context=ec,
            needs_structured_output=needs_structured_output, state=state,
        )
        legacy_client = runtime._select_llm_client(
            _NODE, persona, needs_structured_output=needs_structured_output, state=state,
        )
        return ec, client, model, legacy_client

    def test_conversation_selects_normal_client(self):
        persona = _persona()
        ec, client, model, legacy = self._select_both(persona, _pulse_ctx(Aspect.CONVERSATION))
        assert client is persona.llm_client
        assert legacy is persona.llm_client
        assert model == ec.model_key == "standard-model"

    def test_meta_selects_normal_client(self):
        persona = _persona()
        ec, client, model, legacy = self._select_both(persona, _pulse_ctx(Aspect.META))
        assert client is persona.llm_client is legacy
        assert model == "standard-model"

    def test_worker_selects_lightweight_client(self):
        persona = _persona()
        ec, client, model, legacy = self._select_both(persona, _pulse_ctx(Aspect.WORKER))
        assert client is persona.lightweight_llm_client
        assert legacy is persona.lightweight_llm_client
        assert model == ec.model_key == "lite-model"

    def test_autonomous_selects_lightweight_client(self):
        persona = _persona()
        ec, client, model, legacy = self._select_both(persona, _pulse_ctx(Aspect.AUTONOMOUS))
        assert client is persona.lightweight_llm_client is legacy
        assert model == "lite-model"

    def test_legacy_frame_selects_normal_client(self):
        persona = _persona()
        ec, client, model, legacy = self._select_both(persona, _pulse_ctx(aspect=None))
        assert client is persona.llm_client is legacy
        assert model == "standard-model"

    def test_no_pulse_context_keepalive_style(self):
        """keepalive / sluice 経路: pulse_context なし → standard tier。"""
        persona = _persona()
        runtime = _runtime()
        ec = resolve_execution_context(persona, None)
        client, model = runtime.select_llm_client(_NODE, persona, execution_context=ec)
        assert client is persona.llm_client
        assert model == ec.model_key == "standard-model"

    def test_lightweight_without_client_creates_temp_client(self):
        """lightweight_llm_client 未設定 → 一時 client を lite model 名で作る。"""
        persona = _persona(lightweight_llm_client=None)
        temp_client = object()
        with patch("llm_clients.get_llm_client", return_value=temp_client), \
             patch("saiverse.model_configs.get_context_length", return_value=32768), \
             patch("saiverse.model_configs.get_model_provider", return_value="openai"):
            ec, client, model, legacy = self._select_both(persona, _pulse_ctx(Aspect.WORKER))
        assert client is temp_client
        assert legacy is temp_client
        assert model == ec.model_key == "lite-model"

    def test_lightweight_client_creation_failure_falls_back_to_normal(self):
        """一時 client 作成失敗 → 従来どおり normal client。実 model 名が返る。"""
        persona = _persona(lightweight_llm_client=None)
        with patch("llm_clients.get_llm_client", side_effect=RuntimeError("boom")):
            ec, client, model, legacy = self._select_both(persona, _pulse_ctx(Aspect.WORKER))
        assert client is persona.llm_client is legacy
        assert model == "standard-model"
        # 解決値 (lite-model) と実 model が違う → with_model で差し替えられる
        assert ec.model_key == "lite-model"
        assert ec.with_model(model).model_key == "standard-model"

    def test_structured_output_fallback_returns_actual_model(self):
        """standard が構造化出力非対応 → lite へ fallback、実 model 名が返る。"""
        persona = _persona()
        fb_client = object()
        with patch("saiverse.model_configs.supports_structured_output",
                   side_effect=lambda m: m == "lite-model"), \
             patch("llm_clients.get_llm_client", return_value=fb_client), \
             patch("saiverse.model_configs.get_context_length", return_value=32768), \
             patch("saiverse.model_configs.get_model_provider", return_value="gemini"):
            ec, client, model, legacy = self._select_both(
                persona, _pulse_ctx(Aspect.CONVERSATION), needs_structured_output=True,
            )
        assert client is fb_client
        assert legacy is fb_client
        assert model == "lite-model"
        assert ec.model_key == "standard-model"
        assert ec.with_model(model).model_key == "lite-model"

    def test_raises_llmerror_when_client_unset(self):
        from llm_clients.exceptions import LLMError
        persona = _persona(llm_client=None)
        runtime = _runtime()
        ec = resolve_execution_context(persona, None)
        with pytest.raises(LLMError):
            runtime.select_llm_client(_NODE, persona, execution_context=ec)
