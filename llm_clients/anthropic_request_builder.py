"""Anthropic request building helpers."""
from __future__ import annotations

import base64
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from saiverse.media_utils import iter_image_media, load_image_bytes_for_llm

from .utils import (
    compute_allowed_attachment_keys,
    content_to_text,
    image_summary_note,
    parse_attachment_limit,
)


def _make_cache_control(cache_ttl: str = "5m") -> Dict[str, Any]:
    return {"type": "ephemeral", "ttl": cache_ttl}


def _prepare_schema_for_native_output(schema: Dict[str, Any]) -> Dict[str, Any]:
    import copy

    schema = copy.deepcopy(schema)

    def _fix_object(obj: Dict[str, Any]) -> None:
        if not isinstance(obj, dict):
            return
        if obj.get("type") == "object":
            obj.setdefault("additionalProperties", False)
            for prop in (obj.get("properties") or {}).values():
                _fix_object(prop)
        elif obj.get("type") == "array" and isinstance(obj.get("items"), dict):
            _fix_object(obj["items"])
        for key in ("anyOf", "allOf"):
            if key in obj:
                for item in obj[key]:
                    _fix_object(item)

    _fix_object(schema)
    return schema


def _prepare_anthropic_system(
    messages: List[Dict[str, Any]],
    enable_cache: bool = True,
    cache_ttl: str = "5m",
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    system_blocks: List[Dict[str, Any]] = []
    remaining: List[Dict[str, Any]] = []

    for msg in messages:
        if not isinstance(msg, dict):
            remaining.append(msg)
            continue
        role = msg.get("role", "")
        if isinstance(role, str) and role.lower() == "host":
            role = "system"
        if role == "system":
            content = content_to_text(msg.get("content", ""))
            if content:
                system_blocks.append({"type": "text", "text": content})
        else:
            remaining.append(msg)

    if enable_cache and system_blocks:
        system_blocks[-1]["cache_control"] = _make_cache_control(cache_ttl)

    return system_blocks, remaining


def _collect_attachment_state(
    messages: List[Dict[str, Any]],
) -> Tuple[Dict[int, List[Dict[str, Any]]], Set[int], Optional[int], Optional[Set[Tuple[int, int]]]]:
    max_image_embeds = parse_attachment_limit("ANTHROPIC")
    attachment_cache: Dict[int, List[Dict[str, Any]]] = {}
    skip_summary_indices: Set[int] = set()
    exempt_indices: Set[int] = set()

    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        metadata = msg.get("metadata")
        if isinstance(metadata, dict):
            if metadata.get("__skip_image_summary__"):
                skip_summary_indices.add(idx)
            if metadata.get("__visual_context__"):
                exempt_indices.add(idx)
        media_items = iter_image_media(metadata) if metadata else []
        if media_items:
            attachment_cache[idx] = list(media_items)

    allowed_attachment_keys = compute_allowed_attachment_keys(attachment_cache, max_image_embeds, exempt_indices)
    return attachment_cache, skip_summary_indices, max_image_embeds, allowed_attachment_keys


def _build_anthropic_content_blocks(
    role: str,
    content: Any,
    metadata: Any,
    attachments: List[Dict[str, Any]],
    skip_summary: bool,
    supports_images: bool,
    max_image_bytes: Optional[int],
    allowed_attachment_keys: Optional[Set[Tuple[int, int]]],
    message_index: int,
) -> Tuple[List[Dict[str, Any]], bool]:
    content_blocks: List[Dict[str, Any]] = []
    text = content_to_text(content)
    if text:
        content_blocks.append({"type": "text", "text": text})

    is_dynamic = isinstance(metadata, dict) and bool(metadata.get("__realtime_context__"))

    for att_idx, att in enumerate(attachments):
        should_embed = supports_images and role == "user" and (
            allowed_attachment_keys is None or (message_index, att_idx) in allowed_attachment_keys
        )
        if should_embed:
            data, effective_mime = load_image_bytes_for_llm(att["path"], att["mime_type"], max_bytes=max_image_bytes)
            if data and effective_mime:
                b64 = base64.b64encode(data).decode("ascii")
                content_blocks.append(
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": effective_mime, "data": b64},
                    }
                )
                continue
            logging.warning("Image file not found or unreadable: %s", att.get("uri") or att.get("path"))

        note = image_summary_note(
            att["path"],
            att["mime_type"],
            att.get("uri", att.get("path", "unknown")),
            skip_summary=skip_summary,
        )
        content_blocks.append({"type": "text", "text": note})

    return content_blocks, is_dynamic


def _apply_anthropic_cache_breakpoint(
    prepared: List[Dict[str, Any]],
    first_dynamic_index: Optional[int],
    enable_cache: bool,
    cache_ttl: str,
) -> None:
    if not enable_cache or not prepared:
        return
    if first_dynamic_index is not None and first_dynamic_index > 0:
        cache_target_index = first_dynamic_index - 1
        logging.debug("[anthropic] Placing cache breakpoint before dynamic content at index %d", cache_target_index)
    else:
        cache_target_index = len(prepared) - 2 if len(prepared) >= 2 else len(prepared) - 1

    if cache_target_index >= 0:
        target_msg = prepared[cache_target_index]
        if target_msg.get("content") and isinstance(target_msg["content"], list):
            target_msg["content"][-1]["cache_control"] = _make_cache_control(cache_ttl)


def _prepare_anthropic_messages(
    messages: List[Dict[str, Any]],
    supports_images: bool = False,
    max_image_bytes: Optional[int] = None,
    enable_cache: bool = True,
    cache_ttl: str = "5m",
) -> List[Dict[str, Any]]:
    attachment_cache, skip_summary_indices, max_image_embeds, allowed_attachment_keys = _collect_attachment_state(messages)
    logging.debug(
        "[anthropic] attachment limit=%s, cached=%d msgs with images",
        "∞" if max_image_embeds is None else max_image_embeds,
        len(attachment_cache),
    )

    prepared: List[Dict[str, Any]] = []
    first_dynamic_index: Optional[int] = None

    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        if role == "system":
            continue
        if role == "host":
            role = "user"

        content_blocks, is_dynamic = _build_anthropic_content_blocks(
            role=role,
            content=msg.get("content", ""),
            metadata=msg.get("metadata"),
            attachments=attachment_cache.get(i, []),
            skip_summary=(i in skip_summary_indices),
            supports_images=supports_images,
            max_image_bytes=max_image_bytes,
            allowed_attachment_keys=allowed_attachment_keys,
            message_index=i,
        )
        if not content_blocks:
            continue

        prepared_index = len(prepared)
        prepared.append({"role": role, "content": content_blocks})
        if is_dynamic and first_dynamic_index is None:
            first_dynamic_index = prepared_index

    _apply_anthropic_cache_breakpoint(prepared, first_dynamic_index, enable_cache, cache_ttl)
    return prepared


def _prepare_anthropic_tools(
    tools: List[Dict[str, Any]],
    enable_cache: bool = True,
    cache_ttl: str = "5m",
) -> List[Dict[str, Any]]:
    anthropic_tools: List[Dict[str, Any]] = []

    for tool in tools:
        if tool.get("type") == "function":
            func = tool.get("function", {})
            anthropic_tools.append(
                {
                    "name": func.get("name", ""),
                    "description": func.get("description", ""),
                    "input_schema": func.get("parameters", {}),
                }
            )
        else:
            anthropic_tools.append(tool)

    if enable_cache and anthropic_tools:
        anthropic_tools[-1]["cache_control"] = _make_cache_control(cache_ttl)

    return anthropic_tools


def build_request_params(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Any]],
    response_schema: Optional[Dict[str, Any]],
    temperature: Optional[float],
    enable_cache: bool,
    cache_ttl: str,
    model: str,
    max_tokens: int,
    extra_params: Dict[str, Any],
    thinking_config: Optional[Dict[str, Any]],
    thinking_effort: Optional[str],
    supports_images: bool,
    max_image_bytes: Optional[int],
) -> Dict[str, Any]:
    system_blocks, remaining_messages = _prepare_anthropic_system(messages, enable_cache=enable_cache, cache_ttl=cache_ttl)
    prepared_messages = _prepare_anthropic_messages(
        remaining_messages,
        supports_images=supports_images,
        max_image_bytes=max_image_bytes,
        enable_cache=enable_cache,
        cache_ttl=cache_ttl,
    )

    use_tools = bool(tools)
    use_native_structured_output = False
    request_params: Dict[str, Any] = {"model": model, "max_tokens": max_tokens, "messages": prepared_messages}

    if system_blocks:
        request_params["system"] = system_blocks

    if temperature is not None:
        request_params["temperature"] = temperature
    elif "temperature" in extra_params:
        request_params["temperature"] = extra_params["temperature"]

    for param in ("top_p", "top_k"):
        if param in extra_params:
            request_params[param] = extra_params[param]

    if thinking_config:
        request_params["thinking"] = thinking_config

    if use_tools and tools:
        request_params["tools"] = _prepare_anthropic_tools(tools, enable_cache=enable_cache, cache_ttl=cache_ttl)

    output_config: Dict[str, Any] = {}
    if thinking_effort:
        output_config["effort"] = thinking_effort

    if response_schema and not use_tools:
        if thinking_config:
            output_config["format"] = {
                "type": "json_schema",
                "schema": _prepare_schema_for_native_output(response_schema),
            }
            use_native_structured_output = True
        else:
            schema_name = response_schema.get("title", "structured_output")
            request_params["tools"] = [{
                "name": schema_name,
                "description": "Generate structured output according to the schema",
                "input_schema": response_schema,
            }]
            request_params["tool_choice"] = {"type": "tool", "name": schema_name}

    if output_config:
        request_params["output_config"] = output_config

    return {
        "request_params": request_params,
        "use_tools": use_tools,
        "use_native_structured_output": use_native_structured_output,
    }
