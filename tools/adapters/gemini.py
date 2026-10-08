import json
import logging
from typing import Any, Dict, FrozenSet, List, Optional

from google.genai import types
from tools.core import ToolSchema

LOGGER = logging.getLogger(__name__)

# Keywords ``types.Schema`` accepts, by field name and by alias
# (``anyOf``, ``maxItems``, ...). Anything else raises ``extra_forbidden``.
_GEMINI_SCHEMA_KEYS = frozenset(
    key
    for name, field in types.Schema.model_fields.items()
    for key in (name, field.alias)
    if key
)

# Where JSON Schema keeps reusable definitions (pydantic emits ``$defs``).
_DEFS_KEYS = ("$defs", "definitions")


def _format_value(value: Any) -> str:
    # JSON spelling (``null``, ``true``, ``"a"``) — the model writes JSON args.
    return json.dumps(value, ensure_ascii=False)


def _sanitize_schema(
    node: Any,
    dropped: List[str],
    path: str = "",
    defs: Optional[Dict[str, Any]] = None,
    resolving: FrozenSet[str] = frozenset(),
) -> Any:
    """Rewrite one JSON-Schema node into something ``types.Schema`` accepts.

    Tool schemas come from outside SAIVerse (MCP servers, addons) and use full
    JSON Schema, while ``types.Schema`` is a closed subset. Before this, one
    unsupported keyword made ``to_gemini`` raise and the whole tool vanished
    from Gemini personas (e.g. stackchan-mcp's ``enum: [-1, 1]`` and
    ``exclusiveMinimum: 0``). Rules:

    * ``$ref`` into ``$defs``/``definitions`` is inlined (pydantic-generated
      schemas reference their nested models this way).
    * ``oneOf`` -> ``anyOf`` (equivalent for function calling; the model does
      not enforce XOR anyway). Tuple-style ``items: [...]`` -> ``anyOf``.
    * ``type: [T, "null"]`` -> ``T`` + ``nullable``.
    * Constraints Gemini cannot express but the model should still know
      (non-string ``enum``/``const``, ``exclusiveMinimum``/``exclusiveMaximum``)
      are moved into ``description`` as plain text.
    * Any other unknown keyword is dropped and its path recorded in ``dropped``.
    """
    if not isinstance(node, dict):
        return node

    if defs is None:
        defs = {}
        for key in _DEFS_KEYS:
            if isinstance(node.get(key), dict):
                defs.update(node[key])

    def sub(value: Any, where: str) -> Any:
        return _sanitize_schema(value, dropped, where, defs, resolving)

    ref = node.get("$ref")
    if isinstance(ref, str):
        name = ref.rsplit("/", 1)[-1]
        prefix_ok = any(ref == f"#/{k}/{name}" for k in _DEFS_KEYS)
        if prefix_ok and name in defs and name not in resolving:
            # Sibling keywords (e.g. a local description) override the target.
            merged = {**defs[name], **{k: v for k, v in node.items() if k != "$ref"}}
            return _sanitize_schema(merged, dropped, path, defs, resolving | {name})

    out: dict = {}
    notes: List[str] = []
    has_union = isinstance(node.get("anyOf"), list) or isinstance(node.get("oneOf"), list)

    for key, value in node.items():
        here = f"{path}.{key}" if path else key

        if key in _DEFS_KEYS:
            continue  # consumed by $ref inlining above
        if key == "properties" and isinstance(value, dict):
            out[key] = {name: sub(s, f"{here}.{name}") for name, s in value.items()}
        elif key in ("anyOf", "oneOf") and isinstance(value, list):
            if key == "oneOf" and isinstance(node.get("anyOf"), list):
                # Both present: Gemini has one union slot. Decide by keyword,
                # not by JSON key order — the original anyOf always wins.
                dropped.append(here)
                continue
            out["anyOf"] = [sub(s, f"{here}.{i}") for i, s in enumerate(value)]
        elif key == "items" and isinstance(value, list):
            # Tuple validation has no Gemini form; "any of these" keeps the types.
            out[key] = {"anyOf": [sub(s, f"{here}.{i}") for i, s in enumerate(value)]}
        elif key in ("items", "additionalProperties") and isinstance(value, dict):
            out[key] = sub(value, here)
        elif key == "type" and isinstance(value, list):
            real = [t for t in value if t != "null"]
            if "null" in value:
                out["nullable"] = True
            if len(real) == 1:
                out["type"] = real[0]
            elif real and not has_union:
                out["anyOf"] = [{"type": t} for t in real]
            # With several types *and* an explicit anyOf/oneOf, the union
            # already carries the alternatives; dropping ``type`` loses nothing.
        elif key == "enum" and isinstance(value, list):
            if all(isinstance(v, str) for v in value):
                out[key] = value
            else:
                notes.append(
                    "Allowed values: " + ", ".join(_format_value(v) for v in value) + "."
                )
        elif key == "const":
            if isinstance(value, str) and "enum" not in node:
                out["enum"] = [value]
            else:
                notes.append(f"Must be {_format_value(value)}.")
        elif key == "exclusiveMinimum":
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                notes.append(f"Must be greater than {value}.")
            elif value is True and "minimum" in node:
                notes.append(f"Must be greater than {node['minimum']}.")
        elif key == "exclusiveMaximum":
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                notes.append(f"Must be less than {value}.")
            elif value is True and "maximum" in node:
                notes.append(f"Must be less than {node['maximum']}.")
        elif key in _GEMINI_SCHEMA_KEYS:
            out[key] = value
        else:
            dropped.append(here)

    if notes:
        base = out.get("description")
        out["description"] = " ".join(([base] if base else []) + notes)
    return out


def to_gemini(tool: ToolSchema) -> types.Tool:
    dropped: List[str] = []
    parameters = _sanitize_schema(tool.parameters, dropped)
    if dropped:
        LOGGER.debug(
            "to_gemini: dropped schema keywords Gemini cannot represent for tool '%s': %s",
            tool.name, dropped,
        )
    return types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name=tool.name,
                description=tool.description,
                parameters=types.Schema(**parameters),
                response=types.Schema(type=types.Type(tool.result_type.upper())),
            )
        ]
    )
