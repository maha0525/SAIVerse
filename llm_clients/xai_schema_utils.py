"""Utility to dynamically convert JSON Schema dicts to Pydantic models.

Used by XAIClient to leverage xai_sdk's ``chat.parse()`` for structured output.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Literal, Optional, Type, get_args

from pydantic import BaseModel, Field, create_model

_log = logging.getLogger(__name__)

# Counter to ensure unique model names for nested objects
_model_counter = 0


def _next_model_name(prefix: str) -> str:
    global _model_counter
    _model_counter += 1
    return f"{prefix}_{_model_counter}"


def _json_type_to_annotation(
    prop_schema: Dict[str, Any],
    field_name: str = "",
) -> type:
    """Map a JSON Schema property definition to a Python type annotation.

    Supports: string, integer/int, number/float, boolean/bool, array, object,
    and string+enum → Literal.
    """
    type_name = prop_schema.get("type", "string")

    # Normalize aliases
    if type_name == "int":
        type_name = "integer"
    elif type_name == "float":
        type_name = "number"
    elif type_name == "bool":
        type_name = "boolean"

    # string with enum → Literal
    if type_name == "string" and "enum" in prop_schema:
        enum_values = tuple(prop_schema["enum"])
        if not enum_values:
            return str
        return Literal[enum_values]  # type: ignore[valid-type]

    if type_name == "string":
        return str
    if type_name == "integer":
        return int
    if type_name == "number":
        return float
    if type_name == "boolean":
        return bool

    if type_name == "array":
        items_schema = prop_schema.get("items", {})
        item_type = _json_type_to_annotation(items_schema, f"{field_name}_item")
        return List[item_type]  # type: ignore[valid-type]

    if type_name == "object":
        nested_name = _next_model_name(field_name.capitalize() or "Nested")
        return json_schema_to_pydantic(prop_schema, model_name=nested_name)

    # Fallback for unrecognised types
    _log.warning("Unknown JSON Schema type '%s' for field '%s'; using Any", type_name, field_name)
    return Any


def json_schema_to_pydantic(
    schema: Dict[str, Any],
    model_name: str = "DynamicModel",
) -> Type[BaseModel]:
    """Convert a JSON Schema dict into a dynamically-created Pydantic model.

    Args:
        schema: A JSON Schema object (must have ``type: "object"`` and ``properties``).
        model_name: The class name for the generated model.

    Returns:
        A Pydantic ``BaseModel`` subclass matching the schema.

    Example::

        schema = {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["search", "read"]},
                "query": {"type": "string"},
            },
            "required": ["action", "query"],
        }
        Model = json_schema_to_pydantic(schema)
        instance = Model(action="search", query="hello")
    """
    properties = schema.get("properties", {})
    required_fields = set(schema.get("required", []))

    field_definitions: Dict[str, Any] = {}
    for prop_name, prop_schema in properties.items():
        annotation = _json_type_to_annotation(prop_schema, prop_name)
        description = prop_schema.get("description", "")
        default_value = prop_schema.get("default")

        if prop_name in required_fields:
            # Required field: no default → use ... (Ellipsis)
            if default_value is not None:
                field_definitions[prop_name] = (
                    annotation,
                    Field(default=default_value, description=description),
                )
            else:
                field_definitions[prop_name] = (
                    annotation,
                    Field(description=description),
                )
        else:
            # Optional field
            if default_value is not None:
                field_definitions[prop_name] = (
                    Optional[annotation],  # type: ignore[valid-type]
                    Field(default=default_value, description=description),
                )
            else:
                field_definitions[prop_name] = (
                    Optional[annotation],  # type: ignore[valid-type]
                    Field(default=None, description=description),
                )

    model = create_model(model_name, **field_definitions)  # type: ignore[call-overload]
    return model


__all__ = ["json_schema_to_pydantic"]
