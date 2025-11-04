"""Google Gemini client implementation."""
from __future__ import annotations

import json
import logging
import mimetypes
import os
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from google import genai
from google.genai import types

from .gemini_utils import build_gemini_clients

from media_utils import iter_image_media, load_image_bytes_for_llm
from media_summary import ensure_image_summary
from tools import GEMINI_TOOLS_SPEC, TOOL_REGISTRY
from tools.defs import parse_tool_result
from llm_router import route

from .base import LLMClient, raw_logger
from .utils import content_to_text, is_truthy_flag, merge_reasoning_strings

GEMINI_SAFETY_CONFIG = [
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        threshold=types.HarmBlockThreshold.BLOCK_NONE,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        threshold=types.HarmBlockThreshold.BLOCK_NONE,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
        threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH,
    ),
]

GROUNDING_TOOL = types.Tool(google_search=types.GoogleSearch())


def merge_tools_for_gemini(request_tools: Optional[List[types.Tool]]) -> List[types.Tool]:
    """Ensure grounding tool is included when no custom functions are declared."""
    request_tools = request_tools or GEMINI_TOOLS_SPEC
    has_functions = any(tool.function_declarations for tool in request_tools)
    if has_functions:
        return request_tools
    return [GROUNDING_TOOL] + request_tools


class GeminiClient(LLMClient):
    """Client for Google Gemini API."""

    def __init__(
        self,
        model: str,
        config: Optional[Dict[str, Any]] | None = None,
        supports_images: bool = True,
    ) -> None:
        super().__init__(supports_images=supports_images)
        self.free_client, self.paid_client, self.client = build_gemini_clients()
        self.model = model
        cfg = config or {}
        include_thoughts = cfg.get("include_thoughts")
        if include_thoughts is None:
            include_thoughts = "2.5" in (model or "").lower()
        try:
            self._thinking_config = types.ThinkingConfig(include_thoughts=True) if include_thoughts else None
        except Exception:
            self._thinking_config = None

    @staticmethod
    def _schema_from_json(js: Optional[Dict[str, Any]]) -> Optional[types.Schema]:
        if js is None:
            return None

        def _to_schema(node: Any) -> types.Schema:
            if not isinstance(node, dict):
                return types.Schema(type=types.Type.OBJECT)

            kwargs: Dict[str, Any] = {}
            value_type = node.get("type")
            if isinstance(value_type, list):
                if len(value_type) == 1:
                    value_type = value_type[0]
                else:
                    kwargs["any_of"] = [_to_schema({**node, "type": t}) for t in value_type]
                    value_type = None
            if isinstance(value_type, str):
                kwargs["type"] = {
                    "string": types.Type.STRING,
                    "number": types.Type.NUMBER,
                    "integer": types.Type.INTEGER,
                    "boolean": types.Type.BOOLEAN,
                    "array": types.Type.ARRAY,
                    "object": types.Type.OBJECT,
                    "null": types.Type.NULL,
                }.get(value_type, types.Type.TYPE_UNSPECIFIED)

            if "description" in node:
                kwargs["description"] = node["description"]
            if "enum" in node and isinstance(node["enum"], list):
                kwargs["enum"] = node["enum"]
            if "const" in node:
                kwargs["enum"] = [node["const"]]

            if "properties" in node and isinstance(node["properties"], dict):
                props = {k: _to_schema(v) for k, v in node["properties"].items()}
                kwargs["properties"] = props
                kwargs["property_ordering"] = list(node["properties"].keys())
            if "required" in node and isinstance(node["required"], list):
                kwargs["required"] = node["required"]

            if "items" in node:
                kwargs["items"] = _to_schema(node["items"])

            if "anyOf" in node and isinstance(node["anyOf"], list):
                kwargs["any_of"] = [_to_schema(sub) for sub in node["anyOf"]]

            if "oneOf" in node and isinstance(node["oneOf"], list):
                kwargs["any_of"] = [_to_schema(sub) for sub in node["oneOf"]]

            if "allOf" in node and isinstance(node["allOf"], list):
                kwargs["all_of"] = [_to_schema(sub) for sub in node["allOf"]]

            return types.Schema(**kwargs)

        return _to_schema(js)

    @staticmethod
    def _is_rate_limit_error(err: Exception) -> bool:
        msg = str(err).lower()
        return (
            "rate" in msg
            or "429" in msg
            or "quota" in msg
            or "503" in msg
            or "unavailable" in msg
            or "overload" in msg
        )

    def _convert_messages(
        self,
        msgs: List[Dict[str, str] | types.Content],
    ) -> Tuple[str, List[types.Content]]:
        system_lines: List[str] = []
        contents: List[types.Content] = []
        attachment_limit_env = os.getenv("SAIVERSE_GEMINI_ATTACHMENT_LIMIT")
        max_image_embeds: Optional[int] = None
        if attachment_limit_env is not None:
            try:
                max_image_embeds = int(attachment_limit_env.strip())
            except ValueError:
                logging.warning(
                    "Invalid SAIVERSE_GEMINI_ATTACHMENT_LIMIT=%s; ignoring",
                    attachment_limit_env,
                )
                max_image_embeds = None
            else:
                if max_image_embeds < 0:
                    max_image_embeds = 0
        logging.debug(
            "[gemini] attachment limit=%s",
            "∞" if max_image_embeds is None else max_image_embeds,
        )

        attachment_cache: Dict[int, List[Dict[str, Any]]] = {}
        if self.supports_images:
            for idx, message in enumerate(msgs):
                if isinstance(message, dict):
                    media_items = iter_image_media(message.get("metadata"))
                    if media_items:
                        attachment_cache[idx] = media_items
        allowed_attachment_keys: Optional[Set[Tuple[int, int]]] = None
        if max_image_embeds is not None and attachment_cache:
            ordered: List[Tuple[int, int]] = []
            for msg_idx in sorted(attachment_cache.keys(), reverse=True):
                items = attachment_cache[msg_idx]
                for att_idx in range(len(items)):
                    ordered.append((msg_idx, att_idx))
            if max_image_embeds == 0:
                allowed_attachment_keys = set()
            else:
                allowed_attachment_keys = set(ordered[:max_image_embeds])
        elif max_image_embeds is not None:
            allowed_attachment_keys = set()

        for idx, message in enumerate(msgs):
            if isinstance(message, types.Content):
                contents.append(message)
                continue

            role = message.get("role", "")
            if role == "system":
                system_lines.append(message.get("content", "") or "")
                continue

            if "tool_calls" in message:
                for fn_call in message["tool_calls"]:
                    contents.append(
                        types.Content(role="model", parts=[types.Part(function_call=fn_call)])
                    )
                continue

            text = content_to_text(message.get("content", "")) or ""
            text_content = text
            g_role = "user" if role == "user" else "model"
            attachments = attachment_cache.get(idx, []) if self.supports_images else []
            if attachments and self.supports_images:
                selected_attachments: List[Dict[str, Any]] = []
                skipped_attachments: List[Dict[str, Any]] = []
                attachment_records: List[Tuple[int, Dict[str, Any], Optional[str]]] = []
                for att_idx, attachment in enumerate(attachments):
                    summary_text = ensure_image_summary(attachment["path"], attachment["mime_type"])
                    key = (idx, att_idx)
                    if allowed_attachment_keys is not None and key not in allowed_attachment_keys:
                        attachment_records.append((att_idx, attachment, summary_text))
                        skipped_attachments.append(attachment)
                        continue
                    attachment_records.append((att_idx, attachment, summary_text))
                    selected_attachments.append(attachment)
                logging.debug(
                    "[gemini] embedding %d image attachment(s) for role=%s (skipped=%d)",
                    len(selected_attachments),
                    role,
                    len(skipped_attachments),
                )
                parts: List[types.Part] = []
                if skipped_attachments:
                    for att_idx, attachment, summary_text in attachment_records:
                        if attachment not in skipped_attachments:
                            continue
                        summary = summary_text or "(要約を取得できませんでした)"
                        note = f"[画像参照のみ: {attachment['uri']}] {summary}"
                        text_content = f"{text_content}\n{note}" if text_content else note
                if text_content:
                    parts.append(types.Part(text=text_content))
                for attachment in selected_attachments:
                    data, effective_mime = load_image_bytes_for_llm(
                        attachment["path"], attachment["mime_type"]
                    )
                    if not data or not effective_mime:
                        logging.warning(
                            "Failed to load image for Gemini payload: %s", attachment["uri"]
                        )
                        continue
                    parts.append(types.Part.from_bytes(data=data, mime_type=effective_mime))
                if not parts:
                    parts.append(types.Part(text=""))
                contents.append(types.Content(parts=parts, role=g_role))
            else:
                if attachments:
                    logging.debug(
                        "[gemini] image attachments present but not embedded (supports_images=%s)",
                        self.supports_images,
                    )
                    for attachment in attachments:
                        summary = ensure_image_summary(attachment["path"], attachment["mime_type"])
                        summary_note = summary or "(要約を取得できませんでした)"
                        note = f"[画像: {attachment['uri']}] {summary_note}"
                        text_content = f"{text_content}\n{note}" if text_content else note
                contents.append(types.Content(parts=[types.Part(text=text_content)], role=g_role))

        return "\n".join(system_lines), contents

    def _last_user(self, messages: List[Any]) -> str:
        for message in reversed(messages):
            if isinstance(message, dict) and message.get("role") == "user":
                return content_to_text(message.get("content", ""))
            if isinstance(message, types.Content) and message.role == "user":
                if message.parts and message.parts[0].text:
                    return message.parts[0].text
        return ""

    def _call_with_client(
        self,
        client: genai.Client,
        messages: List[Any],
        tools_spec: Optional[list],
        tool_cfg: Optional[types.ToolConfig],
    ):
        sys_msg, contents = self._convert_messages(messages)
        cfg_kwargs: Dict[str, Any] = {
            "system_instruction": sys_msg,
            "safety_settings": GEMINI_SAFETY_CONFIG,
        }
        if tools_spec:
            cfg_kwargs["tools"] = merge_tools_for_gemini(tools_spec)
            cfg_kwargs["tool_config"] = tool_cfg
        if self._thinking_config is not None:
            cfg_kwargs["thinking_config"] = self._thinking_config
        return client.models.generate_content(
            model=self.model,
            contents=contents,
            config=types.GenerateContentConfig(**cfg_kwargs),
        )

    def generate(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[list] | None = None,
        history_snippets: Optional[List[str]] | None = None,
        response_schema: Optional[Dict[str, Any]] = None,
        *,
        temperature: float | None = None,
    ) -> str:
        default_tools = GEMINI_TOOLS_SPEC if tools is None else tools
        if response_schema is not None and tools is None:
            tools_spec: List[Any] = []
        else:
            tools_spec = default_tools
        use_tools = bool(tools_spec)
        history_snippets = history_snippets or []
        self._store_reasoning([])

        if use_tools:
            decision = route(self._last_user(messages), tools_spec)
            logging.info(
                "Router decision:\n%s",
                json.dumps(decision, indent=2, ensure_ascii=False),
            )

            tool_list = merge_tools_for_gemini(tools_spec)
            if decision["call"] == "yes":
                fc_cfg = types.FunctionCallingConfig(
                    mode="ANY",
                    allowedFunctionNames=[decision["tool"]],
                )
            else:
                fc_cfg = types.FunctionCallingConfig(mode="AUTO")
            tool_cfg = types.ToolConfig(functionCallingConfig=fc_cfg)
        else:
            tool_cfg = None
            tool_list = []

        snippets: List[str] = history_snippets

        def _call(client: genai.Client, model_id: str):
            snippets: List[str] = list(history_snippets)
            for _ in range(10):
                sys_msg, contents = self._convert_messages(messages)
                cfg_kwargs = {
                    "system_instruction": sys_msg,
                    "safety_settings": GEMINI_SAFETY_CONFIG,
                }
                if temperature is not None:
                    cfg_kwargs["temperature"] = temperature
                if use_tools:
                    cfg_kwargs["tools"] = merge_tools_for_gemini(tools_spec)
                    cfg_kwargs["tool_config"] = tool_cfg
                if self._thinking_config is not None:
                    cfg_kwargs["thinking_config"] = self._thinking_config
                if response_schema:
                    cfg_kwargs["response_mime_type"] = "application/json"
                    schema_obj = self._schema_from_json(response_schema)
                    if schema_obj is not None:
                        cfg_kwargs["response_schema"] = schema_obj

                resp = client.models.generate_content(
                    model=model_id,
                    contents=contents,
                    config=types.GenerateContentConfig(**cfg_kwargs),
                )
                raw_logger.debug("Gemini raw:\n%s", resp)

                if not resp.candidates:
                    continue
                candidate = resp.candidates[0]
                if not candidate.content or not candidate.content.parts:
                    continue

                text, reasoning_entries = self._separate_parts(candidate.content.parts)
                if not text and not candidate.function_call:
                    continue

                fcall = getattr(candidate, "function_call", None)
                fcall_name = getattr(fcall, "name", None)
                if not fcall_name or not isinstance(fcall_name, str):
                    self._store_reasoning(reasoning_entries)
                    prefix = "\n".join(snippets)
                    return prefix + ("\n" if prefix and text else "") + text

                # tool call branch
                fn = TOOL_REGISTRY.get(fcall_name)
                if fn is None:
                    logging.warning("Unknown tool '%s' from Gemini; abort", fcall_name)
                    return ""

                try:
                    result_text, snippet, file_path, metadata = parse_tool_result(
                        fn(**(getattr(fcall, "args", {}) or {}))
                    )
                    if snippet:
                        snippets.append(snippet)
                    result = result_text
                except Exception:
                    logging.exception("Tool '%s' execution failed", fcall_name)
                    return "エラー: ツール実行に失敗しました。"

                if metadata:
                    self._store_attachment(metadata)

                messages.extend(
                    [
                        types.Content(role="model", parts=[types.Part(function_call=fcall)]),
                        types.Content(
                            role="tool",
                            parts=[
                                types.Part(
                                    function_response=types.FunctionResponse(
                                        name=fcall_name,
                                        response={"result": result},
                                    )
                                )
                            ],
                        ),
                    ]
                )
                if file_path:
                    with open(file_path, "rb") as file_handler:
                        img_bytes = file_handler.read()
                    mime = mimetypes.guess_type(file_path)[0] or "image/png"
                    messages[-1].parts.append(types.Part.from_bytes(data=img_bytes, mime_type=mime))
                snippets = list(history_snippets)
            return "ツール呼び出しが 10 回を超えました。"

        active_client = self.client
        model_id = self.model
        try:
            result = _call(active_client, model_id)
            if result:
                return result
        except Exception as exc:
            if active_client is self.free_client and self.paid_client and self._is_rate_limit_error(exc):
                logging.info("Retrying with paid Gemini API key due to rate limit")
                active_client = self.paid_client
                result = _call(active_client, model_id)
                if result:
                    return result
            logging.exception("Gemini call failed")
            return "エラーが発生しました。"
        return "エラーが発生しました。"

    def _separate_parts(self, parts: List[Any]) -> Tuple[str, List[Dict[str, str]]]:
        reasoning_entries: List[Dict[str, str]] = []
        text_segments: List[str] = []
        counter = 1
        for part in parts or []:
            if part is None:
                continue
            is_thought = is_truthy_flag(getattr(part, "thought", None))
            text = getattr(part, "text", None)
            if not text:
                continue
            if is_thought:
                reasoning_entries.append({"title": f"Thought {counter}", "text": text.strip()})
                counter += 1
            else:
                text_segments.append(text)
        return "".join(text_segments), reasoning_entries

    def generate_stream(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[list] | None = None,
        history_snippets: Optional[List[str]] | None = None,
        response_schema: Optional[Dict[str, Any]] = None,
        *,
        temperature: float | None = None,
    ) -> Iterator[str]:
        tools_spec = GEMINI_TOOLS_SPEC if tools is None else tools
        use_tools = bool(tools_spec)
        history_snippets = history_snippets or []
        self._store_reasoning([])
        if response_schema:
            logging.warning("Structured streaming output is not yet supported for GeminiClient; ignoring response_schema.")
        reasoning_chunks: List[str] = []

        if use_tools:
            decision = route(self._last_user(messages), tools_spec)
            logging.info(
                "Router decision:\n%s",
                json.dumps(decision, indent=2, ensure_ascii=False),
            )
            tool_cfg = types.ToolConfig(
                functionCallingConfig=(
                    types.FunctionCallingConfig(
                        mode="ANY",
                        allowedFunctionNames=[decision["tool"]],
                    )
                    if decision["call"] == "yes"
                    else types.FunctionCallingConfig(mode="AUTO")
                )
            )
        else:
            tool_cfg = None

        active_client = self.client
        try:
            stream = self._start_stream(active_client, messages, tools_spec, tool_cfg, use_tools, temperature)
        except Exception as exc:
            if active_client is self.free_client and self.paid_client and self._is_rate_limit_error(exc):
                logging.info("Retrying with paid Gemini API key due to rate limit")
                active_client = self.paid_client
                stream = self._start_stream(active_client, messages, tools_spec, tool_cfg, use_tools, temperature)
            else:
                logging.exception("Gemini call failed")
                yield "エラーが発生しました。"
                return

        fcall: Optional[types.FunctionCall] = None
        prefix_yielded = False
        seen_stream_texts: Dict[int, str] = {}
        thought_seen: Dict[int, str] = {}

        for chunk in stream:
            raw_logger.debug("Gemini stream chunk:\n%s", chunk)
            if not chunk.candidates:
                continue
            candidate = chunk.candidates[0]
            raw_logger.debug("Gemini stream candidate:\n%s", candidate)
            if not candidate.content or not candidate.content.parts:
                continue
            candidate_index = getattr(candidate, "index", 0)
            for part_idx, part in enumerate(candidate.content.parts):
                if getattr(part, "function_call", None) and fcall is None:
                    raw_logger.debug("Gemini function_call (part %s): %s", part_idx, part.function_call)
                    fcall = part.function_call
                elif is_truthy_flag(getattr(part, "thought", None)):
                    text_val = getattr(part, "text", None) or ""
                    if text_val:
                        previous = thought_seen.get(candidate_index, "")
                        if text_val.startswith(previous):
                            delta = text_val[len(previous) :]
                            if delta:
                                reasoning_chunks.append(delta)
                                thought_seen[candidate_index] = previous + delta
                        else:
                            reasoning_chunks.append(text_val)
                            thought_seen[candidate_index] = previous + text_val

            combined_text = "".join(
                getattr(part, "text", None) or ""
                for part in candidate.content.parts
                if getattr(part, "text", None) and not is_truthy_flag(getattr(part, "thought", None))
            )
            if not combined_text:
                continue

            previous_text = seen_stream_texts.get(candidate_index, "")
            new_text = (
                combined_text[len(previous_text) :]
                if combined_text.startswith(previous_text)
                else combined_text
            )
            if not new_text:
                continue
            raw_logger.debug("Gemini text delta: %s", new_text)
            if not prefix_yielded and history_snippets:
                yield "\n".join(history_snippets) + "\n"
                prefix_yielded = True
            yield new_text
            seen_stream_texts[candidate_index] = combined_text

        if fcall is None:
            self._store_reasoning(merge_reasoning_strings(reasoning_chunks))
            return

        fn = TOOL_REGISTRY.get(fcall.name)
        if fn is None:
            logging.warning("Unknown tool '%s' from Gemini; abort", fcall.name)
            return

        try:
            result_text, snippet, file_path, metadata = parse_tool_result(fn(**fcall.args))
            if snippet:
                history_snippets.append(snippet)
            result = result_text
        except Exception:
            logging.exception("Tool '%s' execution failed", fcall.name)
            yield "エラー: ツール実行に失敗しました。"
            return

        logging.info("Gemini tool '%s' executed -> %s", fcall.name, result)

        parts = [types.Part(function_call=fcall)]
        file_parts = [
            types.Part(
                function_response=types.FunctionResponse(
                    name=fcall.name,
                    response={"result": result},
                )
            )
        ]
        if file_path:
            with open(file_path, "rb") as file_handler:
                img_bytes = file_handler.read()
            mime = mimetypes.guess_type(file_path)[0] or "image/png"
            file_parts.append(types.Part.from_bytes(data=img_bytes, mime_type=mime))
        if metadata:
            self._store_attachment(metadata)

        messages.extend([
            types.Content(role="model", parts=parts),
            types.Content(role="tool", parts=file_parts),
        ])

        yield from self._stream_follow_up(
            active_client,
            messages,
            tools_spec,
            history_snippets,
            reasoning_chunks,
            result,
            temperature,
        )

    def _start_stream(
        self,
        client: genai.Client,
        messages: List[Any],
        tools_spec: Optional[list],
        tool_cfg: Optional[types.ToolConfig],
        use_tools: bool,
        temperature: float | None,
    ):
        sys_msg, contents = self._convert_messages(messages)
        cfg_kwargs: Dict[str, Any] = {
            "system_instruction": sys_msg,
            "safety_settings": GEMINI_SAFETY_CONFIG,
        }
        if use_tools:
            cfg_kwargs["tools"] = merge_tools_for_gemini(tools_spec)
            cfg_kwargs["tool_config"] = tool_cfg
        if self._thinking_config is not None:
            cfg_kwargs["thinking_config"] = self._thinking_config
        if temperature is not None:
            cfg_kwargs["temperature"] = temperature
        return client.models.generate_content_stream(
            model=self.model,
            contents=contents,
            config=types.GenerateContentConfig(**cfg_kwargs),
        )

    def _stream_follow_up(
        self,
        client: genai.Client,
        messages: List[Any],
        tools_spec: Optional[list],
        history_snippets: List[str],
        reasoning_chunks: List[str],
        fallback_result: str,
        temperature: float | None,
    ) -> Iterator[str]:
        tool_cfg_none = types.ToolConfig(functionCallingConfig=types.FunctionCallingConfig(mode="NONE"))
        sys_msg, contents = self._convert_messages(messages)
        cfg_kwargs: Dict[str, Any] = {
            "system_instruction": sys_msg,
            "safety_settings": GEMINI_SAFETY_CONFIG,
            "tools": merge_tools_for_gemini(tools_spec or []),
            "tool_config": tool_cfg_none,
        }
        if self._thinking_config is not None:
            cfg_kwargs["thinking_config"] = self._thinking_config
        if temperature is not None:
            cfg_kwargs["temperature"] = temperature
        stream = client.models.generate_content_stream(
            model=self.model,
            contents=contents,
            config=types.GenerateContentConfig(**cfg_kwargs),
        )

        prefix_yielded = False
        seen_stream_texts: Dict[int, str] = {}
        thought_seen: Dict[int, str] = {}
        yielded = False

        for chunk in stream:
            raw_logger.debug("Gemini stream2 chunk:\n%s", chunk)
            if not chunk.candidates:
                continue
            candidate = chunk.candidates[0]
            raw_logger.debug("Gemini stream2 candidate:\n%s", candidate)
            if not candidate.content or not candidate.content.parts:
                continue
            candidate_index = getattr(candidate, "index", 0)
            for part in candidate.content.parts:
                if is_truthy_flag(getattr(part, "thought", None)):
                    text_val = getattr(part, "text", None) or ""
                    if text_val:
                        prev = thought_seen.get(candidate_index, "")
                        if text_val.startswith(prev):
                            delta = text_val[len(prev) :]
                            if delta:
                                reasoning_chunks.append(delta)
                                thought_seen[candidate_index] = prev + delta
                        else:
                            reasoning_chunks.append(text_val)
                            thought_seen[candidate_index] = prev + text_val
            combined_text = "".join(
                getattr(part, "text", None) or ""
                for part in candidate.content.parts
                if getattr(part, "text", None) and not is_truthy_flag(getattr(part, "thought", None))
            )
            if not combined_text:
                continue

            previous_text = seen_stream_texts.get(candidate_index, "")
            new_text = (
                combined_text[len(previous_text) :]
                if combined_text.startswith(previous_text)
                else combined_text
            )
            if not new_text:
                continue
            raw_logger.debug("Gemini text2 delta: %s", new_text)
            if not prefix_yielded and history_snippets:
                yield "\n".join(history_snippets) + "\n"
                prefix_yielded = True
            yield new_text
            seen_stream_texts[candidate_index] = combined_text
            yielded = True

        if not yielded:
            if history_snippets:
                yield "\n".join(history_snippets) + "\n" + fallback_result
            else:
                yield fallback_result
        self._store_reasoning(merge_reasoning_strings(reasoning_chunks))


__all__ = [
    "GeminiClient",
    "GEMINI_SAFETY_CONFIG",
    "merge_tools_for_gemini",
    "GROUNDING_TOOL",
    "genai",
]
