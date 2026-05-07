"""Test for _resolve_response_schema_source helper.

Verifies that 'spell:<name>' source strings resolve to the corresponding Spell's
input schema from SPELL_TOOL_SCHEMAS, and that malformed / unknown sources
return None gracefully.
"""
from __future__ import annotations

from unittest.mock import patch

from sea.runtime_llm import _resolve_response_schema_source
from tools.core import ToolSchema


def _make_schema(name: str, params: dict) -> ToolSchema:
    return ToolSchema(
        name=name,
        description=f"Test spell {name}",
        parameters=params,
        result_type="string",
        spell=True,
    )


def test_resolves_spell_input_schema() -> None:
    schema = _make_schema(
        "test_spell",
        {
            "type": "object",
            "properties": {
                "arg1": {"type": "string"},
                "arg2": {"type": "integer"},
            },
            "required": ["arg1"],
        },
    )
    with patch("sea.runtime_llm.SPELL_TOOL_SCHEMAS", {"test_spell": schema}):
        result = _resolve_response_schema_source("spell:test_spell")

    assert result == schema.parameters
    assert result["properties"]["arg1"]["type"] == "string"


def test_unknown_spell_returns_none() -> None:
    with patch("sea.runtime_llm.SPELL_TOOL_SCHEMAS", {}):
        result = _resolve_response_schema_source("spell:nonexistent")

    assert result is None


def test_empty_spell_name_returns_none() -> None:
    assert _resolve_response_schema_source("spell:") is None
    assert _resolve_response_schema_source("spell:  ") is None


def test_unrecognized_form_returns_none() -> None:
    assert _resolve_response_schema_source("not_a_known_form") is None
    assert _resolve_response_schema_source("playbook:some_pb") is None


def test_invalid_input_returns_none() -> None:
    assert _resolve_response_schema_source("") is None
    assert _resolve_response_schema_source("   ") is None
    # Non-string would raise TypeError in real call sites; the helper itself
    # uses isinstance check, so it's defensive
    assert _resolve_response_schema_source(None) is None  # type: ignore[arg-type]


def test_spell_with_malformed_parameters_returns_none() -> None:
    """If a Spell's parameters is not a dict, helper logs and returns None."""
    schema = ToolSchema(
        name="bad_schema_spell",
        description="Schema is malformed",
        parameters="not a dict",  # type: ignore[arg-type]
        result_type="string",
        spell=True,
    )
    with patch("sea.runtime_llm.SPELL_TOOL_SCHEMAS", {"bad_schema_spell": schema}):
        result = _resolve_response_schema_source("spell:bad_schema_spell")

    assert result is None
