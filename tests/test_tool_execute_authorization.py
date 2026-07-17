from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

from sea.pulse_context import Aspect, PulseContext
from tools import _wrap_with_authorization_gate
from tools.context import persona_context
from tools.core import ToolSchema


def _schema(name: str, **kwargs) -> ToolSchema:
    return ToolSchema(
        name=name,
        description="test",
        parameters={"type": "object", "properties": {}},
        result_type="string",
        **kwargs,
    )


def _pulse(aspect: Aspect) -> PulseContext:
    pulse = PulseContext(pulse_id="p1")
    pulse.push_line(aspect=aspect)
    return pulse


def test_mode_gate_applies_at_registered_callable_boundary() -> None:
    calls: list[dict] = []

    def raw(**kwargs):
        calls.append(kwargs)
        return "executed"

    wrapped = _wrap_with_authorization_gate(
        "track_create",
        _schema("track_create", spell=True),
        raw,
    )
    with persona_context(
        "persona-1",
        Path.cwd(),
        pulse_context=_pulse(Aspect.WORKER),
    ):
        result = wrapped(name="forbidden")

    assert "分身モード" in result
    assert calls == []


def test_disabled_addon_native_tool_is_denied_even_if_still_registered() -> None:
    called = False

    def raw(**_kwargs):
        nonlocal called
        called = True
        return "executed"

    wrapped = _wrap_with_authorization_gate(
        "addon_tool",
        _schema("addon_tool", addon_name="sample-addon"),
        raw,
    )
    with patch("saiverse.addon_config.is_addon_enabled", return_value=False):
        result = wrapped()

    assert "無効化されているアドオン" in result
    assert called is False


def test_availability_check_is_rechecked_at_execution() -> None:
    called = False

    def raw(**_kwargs):
        nonlocal called
        called = True
        return "executed"

    wrapped = _wrap_with_authorization_gate(
        "oauth_tool",
        _schema("oauth_tool", availability_check=lambda persona_id: False),
        raw,
    )
    with persona_context("persona-1", Path.cwd()):
        result = wrapped()

    assert "現在のペルソナでは利用できません" in result
    assert called is False


def test_async_tools_use_the_same_execute_time_gate() -> None:
    called = False

    async def raw(**_kwargs):
        nonlocal called
        called = True
        return "executed"

    wrapped = _wrap_with_authorization_gate(
        "purpose_close",
        _schema("purpose_close", spell=True),
        raw,
    )

    async def run() -> str:
        with persona_context(
            "persona-1",
            Path.cwd(),
            pulse_context=_pulse(Aspect.META),
        ):
            return await wrapped()

    result = asyncio.run(run())
    assert "自律制御モード" in result
    assert called is False


def test_composite_action_empty_allowlist_denies_arbitrary_tool() -> None:
    from saiverse.composite_actions import validate_action

    data = {
        "id": "unsafe",
        "display_name": "unsafe",
        "steps": [
            {
                "type": "parallel",
                "calls": [{"tool": "arbitrary_tool", "args": {}}],
            }
        ],
    }

    try:
        validate_action(data, allowed_tools=[])
    except ValueError as exc:
        assert "このアドオンで使用できません" in str(exc)
    else:
        raise AssertionError("empty allowlist must deny every tool")


def test_composite_action_does_not_unwrap_common_authorization_gate() -> None:
    from saiverse.composite_actions import _make_call_awaitable
    from tools import TOOL_REGISTRY

    called = False

    def raw(**_kwargs):
        nonlocal called
        called = True
        return "executed"

    name = "purpose_close"
    previous = TOOL_REGISTRY.get(name)
    TOOL_REGISTRY[name] = _wrap_with_authorization_gate(
        name,
        _schema(name, spell=True),
        raw,
    )

    async def run() -> str:
        with persona_context(
            "persona-1",
            Path.cwd(),
            pulse_context=_pulse(Aspect.META),
        ):
            return await _make_call_awaitable(None, set(), name, {})

    try:
        result = asyncio.run(run())
    finally:
        if previous is None:
            TOOL_REGISTRY.pop(name, None)
        else:
            TOOL_REGISTRY[name] = previous

    assert "自律制御モード" in result
    assert called is False
