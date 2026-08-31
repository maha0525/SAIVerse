"""Shared JSON Schema normalization for OpenAI-style strict structured output.

OpenAI's `response_format=json_schema` (and the equivalent Responses API
`text.format.type=json_schema`) is strict about the JSON Schema it accepts:

* type names must be the JSON Schema spec values (`integer`, `number`, `boolean`),
  not Python-flavored aliases (`int`, `float`, `bool`).
* every `object` node must declare `additionalProperties: false`.
* every `object` node's `required` must list every key in `properties`.

SAIVerse playbooks and tool schemas are written more loosely (e.g. `"type": "int"`),
so we normalize before sending. The same rules apply to the `parameters` block
inside Responses API `tools` entries — both routes go through this helper.

The original logic lived as `OpenAIClient._add_additional_properties`. This
module is the canonical implementation; OpenAICodexClient calls it for both
response_schema and tool parameters.
"""
from __future__ import annotations

import copy
from typing import Any, Dict


_TYPE_ALIASES = {
    "int": "integer",
    "bool": "boolean",
    "float": "number",
}


def normalize_schema_for_strict_json_output(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Return a deep-copied schema that satisfies OpenAI strict mode."""
    schema = copy.deepcopy(schema)
    return _process(schema)


def _process(node: Any) -> Any:
    if isinstance(node, dict):
        # Normalize type aliases (int → integer, bool → boolean, float → number)
        type_value = node.get("type")
        if isinstance(type_value, str) and type_value in _TYPE_ALIASES:
            node["type"] = _TYPE_ALIASES[type_value]
        elif isinstance(type_value, list):
            node["type"] = [
                _TYPE_ALIASES.get(t, t) if isinstance(t, str) else t
                for t in type_value
            ]

        # Object-level invariants
        is_object = node.get("type") == "object" or (
            isinstance(node.get("type"), list) and "object" in node["type"]
        )
        if is_object:
            if "additionalProperties" not in node:
                node["additionalProperties"] = False
            if "properties" in node:
                all_keys = list(node["properties"].keys())
                existing_required = node.get("required") or []
                # Preserve order; dedupe
                merged: list[str] = []
                seen: set[str] = set()
                for key in list(existing_required) + all_keys:
                    if key not in seen:
                        merged.append(key)
                        seen.add(key)
                node["required"] = merged

        return {k: _process(v) for k, v in node.items()}

    if isinstance(node, list):
        return [_process(item) for item in node]

    return node


__all__ = ["normalize_schema_for_strict_json_output"]
