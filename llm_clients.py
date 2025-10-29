import logging
import os
import json
from typing import Any, Dict, List, Iterator, Tuple, Optional
import os

import requests
import openai
from openai import OpenAI
from google import genai
from google.genai import types
from google.genai.types import FunctionResponse

from dotenv import load_dotenv
import mimetypes
import base64

from media_utils import iter_image_media, load_image_bytes_for_llm

from tools import TOOL_REGISTRY, OPENAI_TOOLS_SPEC, GEMINI_TOOLS_SPEC
from tools.defs import parse_tool_result
from llm_router import route
from model_configs import get_model_config

load_dotenv()

# --- Raw LLM response logger -----------------------------------------------
RAW_LOG_FILE = os.getenv("SAIVERSE_RAW_LLM_LOG", "raw_llm_responses.txt")

raw_logger = logging.getLogger("saiverse.llm.raw")   # 既存設定の子ロガー
raw_logger.setLevel(logging.DEBUG)

# すでに同じファイルハンドラが付いていないかチェック
if not any(isinstance(h, logging.FileHandler) and h.baseFilename == os.path.abspath(RAW_LOG_FILE)
           for h in raw_logger.handlers):
    fh = logging.FileHandler(RAW_LOG_FILE, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    raw_logger.addHandler(fh)

# --- Constants ---
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
def _merge_tools_for_gemini(request_tools: list[types.Tool] | None) -> list[types.Tool]:
    """google_search は function_declarations が無いときだけ混ぜる"""
    request_tools = request_tools or GEMINI_TOOLS_SPEC
    has_functions = any(t.function_declarations for t in request_tools)
    if has_functions:
        return request_tools          # ← カスタム関数のみ
    return [GROUNDING_TOOL] + request_tools  # ← 検索だけ or 他ツールだけ


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: List[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("text") or part.get("content")
            if text:
                texts.append(str(text))
        return "".join(texts)
    return ""


def _prepare_openai_messages(messages: List[Any], supports_images: bool) -> List[Any]:
    prepared: List[Any] = []
    for msg in messages:
        if not isinstance(msg, dict):
            prepared.append(msg)
            continue

        metadata = msg.get("metadata")
        attachments = iter_image_media(metadata)
        if not attachments:
            prepared.append(msg.copy())
            continue

        text = _content_to_text(msg.get("content"))
        if supports_images:
            parts: List[Dict[str, Any]] = []
            if text:
                parts.append({"type": "text", "text": text})
            for att in attachments:
                data, effective_mime = load_image_bytes_for_llm(att["path"], att["mime_type"])
                if data and effective_mime:
                    b64 = base64.b64encode(data).decode("ascii")
                    parts.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{effective_mime};base64,{b64}"}
                    })
                else:
                    parts.append({"type": "text", "text": f"[画像: {att['uri']}]"})
            new_msg = msg.copy()
            new_msg["content"] = parts if parts else text
            prepared.append(new_msg)
        else:
            note_lines: List[str] = []
            if text:
                note_lines.append(text)
            for att in attachments:
                note_lines.append(f"[画像: {att['uri']}]")
            new_msg = msg.copy()
            new_msg["content"] = "\n".join(note_lines)
            prepared.append(new_msg)
    return prepared


def _obj_to_dict(obj: Any) -> Any:
    """Best-effort conversion to plain dict for OpenAI/Gemini SDK objects."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump") and callable(getattr(obj, "model_dump")):
        try:
            return obj.model_dump()
        except Exception:
            pass
    if hasattr(obj, "to_dict") and callable(getattr(obj, "to_dict")):
        try:
            return obj.to_dict()
        except Exception:
            pass
    return obj


def _extract_reasoning_from_openai_message(message: Any) -> Tuple[str, List[Dict[str, str]]]:
    """Return (text, reasoning entries) from an OpenAI-style message."""
    msg_dict = _obj_to_dict(message) or {}
    content = msg_dict.get("content")
    reasoning_entries: List[Dict[str, str]] = []
    text_segments: List[str] = []

    def _append_reasoning(text: str, title: Optional[str] = None) -> None:
        text = (text or "").strip()
        if not text:
            return
        reasoning_entries.append({
            "title": title or "",
            "text": text,
        })

    if isinstance(content, list):
        for part in content:
            part_dict = _obj_to_dict(part) or {}
            ptype = part_dict.get("type")
            text = part_dict.get("text") or part_dict.get("content") or ""
            if ptype in {"reasoning", "thinking", "analysis"}:
                _append_reasoning(text, part_dict.get("title"))
            elif ptype in {"output_text", "text", None}:
                text_segments.append(text)
            # ignore other types (e.g. tool_use)
    elif isinstance(content, str):
        text_segments.append(content)

    reasoning_content = msg_dict.get("reasoning_content")
    if isinstance(reasoning_content, str):
        _append_reasoning(reasoning_content)

    if msg_dict.get("reasoning") and isinstance(msg_dict["reasoning"], dict):
        rc = msg_dict["reasoning"].get("content")
        if isinstance(rc, str):
            _append_reasoning(rc)

    final_text = "".join(text_segments)
    return final_text, reasoning_entries


def _merge_reasoning_strings(chunks: List[str]) -> List[Dict[str, str]]:
    """Utility to convert raw reasoning text chunks into structured entries."""
    if not chunks:
        return []
    text = "".join(chunks).strip()
    if not text:
        return []
    return [{"title": "", "text": text}]


def _process_openai_stream_content(content: Any) -> Tuple[str, List[str]]:
    """Return (text_fragment, reasoning_chunks) from a streaming delta content."""
    reasoning_chunks: List[str] = []
    text_fragments: List[str] = []

    if isinstance(content, list):
        for part in content:
            part_dict = _obj_to_dict(part) or {}
            ptype = part_dict.get("type")
            text = part_dict.get("text") or part_dict.get("content") or ""
            if not text:
                continue
            if ptype in {"reasoning", "thinking", "analysis"}:
                reasoning_chunks.append(text)
            else:
                text_fragments.append(text)
    elif isinstance(content, str):
        text_fragments.append(content)

    return "".join(text_fragments), reasoning_chunks


def _extract_reasoning_from_delta(delta: Any) -> List[str]:
    reasoning_chunks: List[str] = []
    delta_dict = _obj_to_dict(delta)
    if not isinstance(delta_dict, dict):
        return reasoning_chunks
    raw_reasoning = delta_dict.get("reasoning")
    if isinstance(raw_reasoning, list):
        for item in raw_reasoning:
            item_dict = _obj_to_dict(item) or {}
            text = item_dict.get("text") or item_dict.get("content") or ""
            if text:
                reasoning_chunks.append(text)
    elif isinstance(raw_reasoning, str):
        reasoning_chunks.append(raw_reasoning)
    return reasoning_chunks


def _is_truthy_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return False


def _extract_gemini_parts(parts: List[Any]) -> Tuple[str, List[Dict[str, str]]]:
    """Separate text and thought summaries from Gemini parts."""
    reasoning_entries: List[Dict[str, str]] = []
    text_segments: List[str] = []
    counter = 1
    for part in parts or []:
        if part is None:
            continue
        is_thought = _is_truthy_flag(getattr(part, "thought", None))
        text = getattr(part, "text", None)
        if not text:
            continue
        if is_thought:
            reasoning_entries.append({
                "title": f"Thought {counter}",
                "text": text.strip(),
            })
            counter += 1
        else:
            text_segments.append(text)
    return "".join(text_segments), reasoning_entries

# --- Base Client ---
class LLMClient:
    """Base class for LLM clients."""

    def __init__(self, supports_images: bool = False) -> None:
        self._latest_reasoning: List[Dict[str, str]] = []
        self._latest_attachments: List[Dict[str, Any]] = []
        self.supports_images = supports_images

    def generate(
        self, messages: List[Dict[str, str]], tools: Optional[list] | None = None
    ) -> str:
        raise NotImplementedError

    def generate_stream(
        self, messages: List[Dict[str, str]], tools: Optional[list] | None = None
    ) -> Iterator[str]:
        raise NotImplementedError

    # --- Reasoning capture helpers ---
    def _store_reasoning(self, entries: List[Dict[str, str]] | None) -> None:
        self._latest_reasoning = entries or []

    def consume_reasoning(self) -> List[Dict[str, str]]:
        entries = self._latest_reasoning
        self._latest_reasoning = []
        return entries

    def _store_attachment(self, metadata: Dict[str, Any]) -> None:
        if not metadata:
            return
        self._latest_attachments.append(metadata)

    def consume_attachments(self) -> List[Dict[str, Any]]:
        attachments = self._latest_attachments
        self._latest_attachments = []
        return attachments

# --- Concrete Clients ---
class OpenAIClient(LLMClient):
    """Client for OpenAI-compatible chat completions API."""

    def __init__(
        self,
        model: str = "gpt-4.1",
        *,
        supports_images: bool = False,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        super().__init__(supports_images=supports_images)
        api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY environment variable is not set.")

        client_kwargs: Dict[str, str] = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url

        self.client = OpenAI(**client_kwargs)
        self.model = model
        self._request_kwargs: Dict[str, Any] = {}

    def _create_completion(self, **kwargs):
        return self.client.chat.completions.create(**kwargs)

    def generate(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[list] | None = None,
        history_snippets: Optional[List[str]] | None = None,
    ) -> str:
        """ツールコールを解決しながら最終テキストを返す（最大 10 回ループ）"""
        tools_spec = OPENAI_TOOLS_SPEC if tools is None else tools
        use_tools = bool(tools_spec)
        snippets: List[str] = list(history_snippets or [])
        self._store_reasoning([])

        if not use_tools:
            try:
                resp = self._create_completion(
                    model=self.model,
                    messages=_prepare_openai_messages(messages, self.supports_images),
                    n=1,
                    **self._request_kwargs,
                )
            except Exception:
                logging.exception("OpenAI call failed")
                return "エラーが発生しました。"

            raw_logger.debug("OpenAI raw:\n%s", resp.model_dump_json(indent=2))
            choice = resp.choices[0]
            text_body, reasoning_entries = _extract_reasoning_from_openai_message(choice.message)
            if not text_body:
                text_body = choice.message.content or ""
            self._store_reasoning(reasoning_entries)
            if snippets:
                prefix = "\n".join(snippets)
                return prefix + ("\n" if text_body and prefix else "") + text_body
            return text_body

        # ── Router 判定（最後の user メッセージだけ渡す） ──
        user_msg = next((m["content"] for m in reversed(messages)
                         if m.get("role") == "user"), "")
        decision = route(user_msg, tools_spec)

        # ログ出力（JSON フォーマットで見やすく）
        try:
            logging.info("Router decision:\n%s", json.dumps(decision, indent=2, ensure_ascii=False))
        except Exception:
            logging.warning("Router decision could not be serialized")

        if decision["call"] == "yes" and decision["tool"]:
            forced_tool_choice: Any = {
                "type": "function",
                "function": {"name": decision["tool"]}
            }
        else:
            forced_tool_choice = "auto"

        try:
            if tools:
                try:
                    logging.info("tools JSON:\n%s", json.dumps(tools, indent=2, ensure_ascii=False))
                except TypeError:
                    logging.info("tools contain non-serializable values")

            for i in range(10):
                tool_choice = forced_tool_choice if i == 0 else "auto"

                resp = self._create_completion(
                    model=self.model,
                    messages=_prepare_openai_messages(messages, self.supports_images),
                    tools=tools_spec,
                    tool_choice=tool_choice,
                    n=1,
                    **self._request_kwargs,
                )
                raw_logger.debug("OpenAI raw:\n%s", resp.model_dump_json(indent=2))

                target_choice = next(
                    (c for c in resp.choices if getattr(c.message, "tool_calls", [])),
                    resp.choices[0]
                )
                tool_calls = getattr(target_choice.message, "tool_calls", [])

                if not isinstance(tool_calls, list) or len(tool_calls) == 0:
                    text_body, reasoning_entries = _extract_reasoning_from_openai_message(target_choice.message)
                    if not text_body:
                        text_body = target_choice.message.content or ""
                    self._store_reasoning(reasoning_entries)
                    prefix = "".join(s + "\n" for s in snippets)
                    return prefix + text_body

                messages.append(target_choice.message)
                for tc in tool_calls:
                    fn = TOOL_REGISTRY.get(tc.function.name)
                    if fn is None:
                        raise RuntimeError(f"Unsupported tool: {tc.function.name}")
                    args = json.loads(tc.function.arguments)
                    result_text, snippet, file_path, metadata = parse_tool_result(fn(**args))
                    if snippet:
                        snippets.append(snippet)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": tc.function.name,
                        "content": result_text,
                    })
                    if file_path:
                        try:
                            with open(file_path, "rb") as f:
                                img_bytes = f.read()
                            b64 = base64.b64encode(img_bytes).decode("ascii")
                            mime = mimetypes.guess_type(file_path)[0] or "image/png"
                            data_url = f"data:{mime};base64,{b64}"
                            messages.append({
                                "role": "assistant",
                                "content": [
                                    {"type": "image_url", "image_url": {"url": data_url}}
                                ],
                            })
                        except Exception:
                            logging.exception("Failed to load file %s", file_path)
                    if metadata:
                        self._store_attachment(metadata)
                continue
            return "ツール呼び出しが 10 回を超えました。"
        except Exception:
            logging.exception("OpenAI call failed")
            return "エラーが発生しました。"

    def generate_stream(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[list] | None = None,
        force_tool_choice: Optional[dict | str] = None,
        history_snippets: Optional[List[str]] | None = None,
    ) -> Iterator[str]:
        """
        ユーザ向けに逐次テキストを yield するストリーム版。
        - force_tool_choice: 初回のみ {"type":"function","function":{"name":..}} か "auto"
        - 再帰呼び出し時はデフォルト None → 自動で "auto"
        """
        tools_spec = OPENAI_TOOLS_SPEC if tools is None else tools
        use_tools = bool(tools_spec)
        history_snippets = list(history_snippets or [])
        self._store_reasoning([])
        reasoning_chunks: List[str] = []

        if not use_tools:
            try:
                resp = self._create_completion(
                    model=self.model,
                    messages=_prepare_openai_messages(messages, self.supports_images),
                    n=1,
                    **self._request_kwargs,
                )
            except Exception:
                logging.exception("OpenAI call failed")
                yield "エラーが発生しました。"
                return

            choice = resp.choices[0]
            text_body, reasoning_entries = _extract_reasoning_from_openai_message(choice.message)
            if not text_body:
                text_body = choice.message.content or ""
            self._store_reasoning(reasoning_entries)
            prefix = "\n".join(history_snippets) + ("\n" if history_snippets and text_body else "")
            if prefix:
                yield prefix
            if text_body:
                yield text_body
            return

        if force_tool_choice is None:
            user_msg = next((m["content"] for m in reversed(messages)
                             if m.get("role") == "user"), "")
            decision = route(user_msg, tools_spec)
            logging.info("Router decision:\n%s", json.dumps(decision, indent=2, ensure_ascii=False))
            if decision["call"] == "yes" and decision["tool"]:
                force_tool_choice = {
                    "type": "function",
                    "function": {"name": decision["tool"]}
                }
            else:
                force_tool_choice = "auto"

        try:
            resp = self._create_completion(
                model=self.model,
                messages=_prepare_openai_messages(messages, self.supports_images),
                tools=tools_spec,
                tool_choice=force_tool_choice,
                stream=True,
                **self._request_kwargs,
            )
        except Exception:
            logging.exception("OpenAI call failed")
            yield "エラーが発生しました。"
            return

        call_buffer: dict[str, dict] = {}
        state = "TEXT"
        prefix_yielded = False

        try:
            current_call_id = None

            for chunk in resp:
                delta = chunk.choices[0].delta

                if delta.tool_calls:
                    state = "TOOL_CALL"
                    for call in delta.tool_calls:
                        tc_id = call.id or current_call_id
                        if tc_id is None:
                            logging.warning("tool_chunk without id; skipping")
                            continue
                        current_call_id = tc_id

                        buf = call_buffer.setdefault(tc_id, {
                            "id": tc_id,
                            "name": "",
                            "arguments": "",
                        })

                        logging.debug("tool_chunk id=%s name=%s args_part=%s",
                                      tc_id, call.function.name or "-", call.function.arguments or "-")

                        if call.function.name:
                            buf["name"] = call.function.name
                        if call.function.arguments:
                            buf["arguments"] += call.function.arguments
                    continue

                if state == "TEXT" and delta.content:
                    text_fragment, reasoning_piece = _process_openai_stream_content(delta.content)
                    if reasoning_piece:
                        reasoning_chunks.extend(reasoning_piece)
                    extra_reasoning = _extract_reasoning_from_delta(delta)
                    if extra_reasoning:
                        reasoning_chunks.extend(extra_reasoning)
                    if not text_fragment:
                        continue
                    if not prefix_yielded and history_snippets:
                        yield "\n".join(history_snippets) + "\n"
                        prefix_yielded = True
                    yield text_fragment
                    continue

                additional_reasoning = _extract_reasoning_from_delta(delta)
                if additional_reasoning:
                    reasoning_chunks.extend(additional_reasoning)

            if not call_buffer:
                self._store_reasoning(_merge_reasoning_strings(reasoning_chunks))
            if call_buffer:
                logging.debug("call_buffer final: %s", json.dumps(call_buffer, indent=2, ensure_ascii=False))
                assistant_call_msg = {"role": "assistant", "content": None, "tool_calls": []}
                for tc in call_buffer.values():
                    name = tc["name"].strip()
                    arg_str = tc["arguments"].strip()

                    if not name:
                        logging.warning("tool_call %s has empty name; skipping", tc["id"])
                        continue
                    if not arg_str:
                        logging.warning("tool_call %s has empty arguments; skipping", tc["id"])
                        continue
                    try:
                        args = json.loads(arg_str)
                    except json.JSONDecodeError:
                        logging.warning("tool_call %s arguments invalid JSON: %s", tc["id"], arg_str)
                        continue

                    fn = TOOL_REGISTRY.get(name)
                    if fn is None:
                        logging.warning("tool_call %s unknown tool '%s'; skipping", tc["id"], name)
                        continue

                    try:
                        result_text, snippet, file_path, metadata = parse_tool_result(fn(**args))
                        logging.info("tool_call %s executed -> %s", tc["id"], result_text)
                        if snippet:
                            history_snippets.append(snippet)
                    except Exception:
                        logging.exception("tool_call %s execution failed", tc["id"])
                        continue

                    assistant_call_msg["tool_calls"].append({
                        "id": tc["id"],
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)}
                    })

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": name,
                        "content": result_text,
                    })
                    if file_path:
                        try:
                            with open(file_path, "rb") as f:
                                img_bytes = f.read()
                            b64 = base64.b64encode(img_bytes).decode("ascii")
                            mime = mimetypes.guess_type(file_path)[0] or "image/png"
                            data_url = f"data:{mime};base64,{b64}"
                            messages.append({
                                "role": "assistant",
                                "content": [
                                    {"type": "image_url", "image_url": {"url": data_url}}
                                ],
                            })
                        except Exception:
                            logging.exception("Failed to load file %s", file_path)
                    if metadata:
                        self._store_attachment(metadata)
                insert_pos = len(messages) - len(call_buffer)
                messages.insert(insert_pos, assistant_call_msg)

                yield from self.generate_stream(
                    messages,
                    tools,
                    force_tool_choice="auto",
                    history_snippets=history_snippets,
                )
                return

        except Exception:
            logging.exception("OpenAI stream call failed")
            yield "エラーが発生しました。"

class AnthropicClient(OpenAIClient):
    """Anthropic Claude via OpenAI-compatible endpoint."""

    def __init__(
        self,
        model: str = "claude-sonnet-4-5",
        config: Optional[Dict[str, Any]] | None = None,
        supports_images: bool = False,
    ):
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY environment variable is not set.")

        base_url = os.getenv("ANTHROPIC_OPENAI_BASE_URL", "https://api.anthropic.com/v1/")
        if not base_url.endswith("/"):
            base_url = base_url + "/"
        super().__init__(model=model, supports_images=supports_images, api_key=api_key, base_url=base_url)

        cfg = config or {}

        def _pick_str(*values: Optional[str]) -> Optional[str]:
            for val in values:
                if val is None:
                    continue
                if isinstance(val, str) and val.strip():
                    return val.strip()
                if not isinstance(val, str):
                    return str(val)
            return None

        def _pick_int(*values: Optional[Any]) -> Optional[int]:
            for val in values:
                if val is None:
                    continue
                try:
                    return int(val)
                except (TypeError, ValueError):
                    continue
            return None

        thinking_payload: Dict[str, Any] = {}
        thinking_type = _pick_str(cfg.get("thinking_type"), os.getenv("ANTHROPIC_THINKING_TYPE"))
        thinking_budget = _pick_int(cfg.get("thinking_budget"), os.getenv("ANTHROPIC_THINKING_BUDGET"))
        thinking_effort = _pick_str(cfg.get("thinking_effort"), os.getenv("ANTHROPIC_THINKING_EFFORT"))

        if thinking_budget is not None and thinking_budget <= 0:
            logging.warning("Anthropic thinking_budget must be positive; ignoring value=%s", thinking_budget)
            thinking_budget = None

        if thinking_type:
            thinking_payload["type"] = thinking_type
        if thinking_budget is not None:
            thinking_payload["budget_tokens"] = thinking_budget
        if thinking_effort:
            thinking_payload["effort"] = thinking_effort

        if thinking_payload:
            thinking_payload.setdefault("type", "enabled")
            extra_body = self._request_kwargs.setdefault("extra_body", {})
            extra_body["thinking"] = thinking_payload

        max_output_tokens = _pick_int(cfg.get("max_output_tokens"), os.getenv("ANTHROPIC_MAX_OUTPUT_TOKENS"))
        if max_output_tokens is not None and max_output_tokens > 0:
            self._request_kwargs["max_output_tokens"] = max_output_tokens

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _disable_thinking_if_needed(self, err: Exception) -> bool:
        """
        Detect Anthropic's thinking-block requirement error and disable the thinking payload.
        Returns True if a retry should be attempted.
        """
        trigger = "Expected `thinking` or `redacted_thinking`"
        message = ""

        if isinstance(err, openai.BadRequestError):
            try:
                response = getattr(err, "response", None)
                if response is not None:
                    data = response.json()
                    message = data.get("error", {}).get("message", "") or ""
            except Exception:
                message = ""
            finally:
                if not message:
                    message = str(err)
        else:
            message = str(err)

        if trigger not in (message or ""):
            return False

        extra_body = self._request_kwargs.get("extra_body")
        if not isinstance(extra_body, dict):
            return False

        if "thinking" not in extra_body:
            return False

        extra_body.pop("thinking", None)
        if not extra_body:
            self._request_kwargs.pop("extra_body", None)

        logging.warning("Anthropic request rejected due to missing thinking blocks; disabling thinking payload and retrying without it.")
        return True

    def _create_completion(self, **kwargs):
        try:
            return self.client.chat.completions.create(**kwargs)
        except openai.BadRequestError as err:
            if self._disable_thinking_if_needed(err):
                return self.client.chat.completions.create(**kwargs)
            raise


class GeminiClient(LLMClient):
    """Client for Google Gemini API."""

    def __init__(
        self,
        model: str,
        config: Optional[Dict[str, Any]] | None = None,
        supports_images: bool = True,
    ):
        super().__init__(supports_images=supports_images)
        free_key = os.getenv("GEMINI_FREE_API_KEY")
        paid_key = os.getenv("GEMINI_API_KEY")
        if not free_key and not paid_key:
            raise RuntimeError(
                "GEMINI_FREE_API_KEY or GEMINI_API_KEY environment variable is not set."
            )
        self.free_client = genai.Client(api_key=free_key) if free_key else None
        self.paid_client = genai.Client(api_key=paid_key) if paid_key else None
        # デフォルトでは無料枠を使用
        self.client = self.free_client or self.paid_client
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

    def _convert_messages(self, msgs: List[Dict[str, str] | types.Content]
                      ) -> Tuple[str, List[types.Content]]:
        system_lines: list[str] = []
        contents: list[types.Content] = []

        for m in msgs:
            # すでに Content オブジェクトならそのまま
            if isinstance(m, types.Content):
                contents.append(m)
                continue

            role = m.get("role", "")
            if role == "system":
                system_lines.append(m.get("content", "") or "")
                continue

            # ----- tool_calls メッセージを検出 -----
            if "tool_calls" in m:
                for fc in m["tool_calls"]:
                    contents.append(
                        types.Content(
                            role="model",
                            parts=[types.Part(function_call=fc)]
                        )
                    )
                continue

            # 通常テキスト
            text = _content_to_text(m.get("content", "")) or ""
            g_role = "user" if role == "user" else "model"
            attachments = iter_image_media(m.get("metadata"))
            if attachments and self.supports_images:
                logging.debug("[gemini] embedding %d image attachment(s) for role=%s", len(attachments), role)
                parts: List[types.Part] = []
                if text:
                    parts.append(types.Part(text=text))
                for att in attachments:
                    data, effective_mime = load_image_bytes_for_llm(att["path"], att["mime_type"])
                    if not data or not effective_mime:
                        logging.warning("Failed to load image for Gemini payload: %s", att["uri"])
                        continue
                    parts.append(types.Part.from_bytes(data=data, mime_type=effective_mime))
                if not parts:
                    parts.append(types.Part(text=""))
                contents.append(types.Content(parts=parts, role=g_role))
            else:
                if attachments:
                    logging.debug("[gemini] image attachments present but not embedded (supports_images=%s)", self.supports_images)
                    for att in attachments:
                        note = f"[画像: {att['uri']}]"
                        text = f"{text}\n{note}" if text else note
                contents.append(types.Content(parts=[types.Part(text=text)], role=g_role))

        return "\n".join(system_lines), contents

    def generate(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[list] | None = None,
        history_snippets: Optional[List[str]] | None = None,
    ) -> str:
        # ------------- 前処理 -------------
        tools_spec = GEMINI_TOOLS_SPEC if tools is None else tools
        use_tools = bool(tools_spec)
        tool_list  = _merge_tools_for_gemini(tools_spec) if use_tools else []
        history_snippets = history_snippets or []
        self._store_reasoning([])

        # 直近 user メッセージ抽出（dict と Content 混在対応）
        def _last_user(msgs) -> str:
            for m in reversed(msgs):
                if isinstance(m, dict) and m.get("role") == "user":
                    return _content_to_text(m.get("content", ""))
                if isinstance(m, types.Content) and m.role == "user":
                    if m.parts and m.parts[0].text:
                        return m.parts[0].text
            return ""

        # ---------- ① Router ----------
        if use_tools:
            decision = route(_last_user(messages), tools_spec)
            logging.info("Router decision:\n%s",
                         json.dumps(decision, indent=2, ensure_ascii=False))

            tool_list = _merge_tools_for_gemini(tools_spec)

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

        def _call(client, model_id: str):
            for _ in range(10):
                sys_msg, contents = self._convert_messages(messages)
                logging.debug("[gemini] system_instruction >>>\n%s", sys_msg)
                try:
                    serialized_contents = json.dumps(
                        [
                            {
                                "role": getattr(c, "role", ""),
                                "parts": [
                                    getattr(part, "text", None)
                                    or getattr(getattr(part, "function_call", None), "name", None)
                                    or getattr(getattr(part, "function_response", None), "name", None)
                                    or getattr(part, "mime_type", None)
                                    for part in getattr(c, "parts", []) or []
                                ],
                            }
                            for c in contents
                        ],
                        ensure_ascii=False,
                    )
                except Exception:
                    serialized_contents = "<unserializable>"
                logging.debug("[gemini] contents_roles >>> %s", serialized_contents)
                config_kwargs = dict(
                    system_instruction=sys_msg,
                    safety_settings=GEMINI_SAFETY_CONFIG,
                )
                if use_tools:
                    config_kwargs["tools"] = _merge_tools_for_gemini(tools_spec)
                    config_kwargs["tool_config"] = tool_cfg
                if self._thinking_config is not None:
                    config_kwargs["thinking_config"] = self._thinking_config
                resp = client.models.generate_content(
                    model=model_id,
                    contents=contents,
                    config=types.GenerateContentConfig(**config_kwargs),
                )
                raw_logger.debug("Gemini raw:\n%s", resp)
                if not resp.candidates:
                    return "（Gemini から応答がありませんでした）"
                cand = resp.candidates[0]
                raw_logger.debug("Gemini candidate:\n%s", cand)

                # ----- どの part に function_call があるか探索 -----
                parts_list = list(getattr(getattr(cand, "content", None), "parts", []) or [])
                fcall_part = next((p for p in parts_list if getattr(p, "function_call", None)), None)
                if fcall_part is None or not use_tools:                       # ★ ツール呼び出しなし
                    prefix = "".join(s + "\n" for s in snippets)
                    text_body, reasoning_entries = _extract_gemini_parts(parts_list)
                    self._store_reasoning(reasoning_entries)
                    return prefix + text_body

                # ----- assistant/tool_calls -----
                messages.append(
                    types.Content(
                        role="model",
                        parts=[types.Part(function_call=fcall_part.function_call)]
                    )
                )

                fc = fcall_part.function_call
                fn = TOOL_REGISTRY.get(fc.name)
                if fn is None:
                    raise RuntimeError(f"Unsupported tool: {fc.name}")
                result_text, snippet, file_path, metadata = parse_tool_result(fn(**fc.args))
                if snippet:
                    snippets.append(snippet)

                parts = [
                    types.Part(
                        function_response=FunctionResponse(
                            name=fc.name,
                            response={"result": result_text}
                        )
                    )
                ]
                if file_path:
                    with open(file_path, "rb") as f:
                        img_bytes = f.read()
                    mime = mimetypes.guess_type(file_path)[0] or "image/png"

                    parts.append(
                        types.Part.from_bytes(           # ← Part を bytes から生成
                            data=img_bytes,
                            mime_type=mime,
                        )
                    )

                messages.append(
                    types.Content(
                        role="tool",
                        parts=parts,
                    )
                )
                if metadata:
                    self._store_attachment(metadata)
            return "ツール呼び出しが 10 回を超えました。"

        active_client = self.client
        # Try current model, then a couple of alternates on 503/overload issues
        model_candidates = []
        for m in [self.model, "gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-flash-8b"]:
            if m and m not in model_candidates:
                model_candidates.append(m)

        last_err: Optional[Exception] = None
        for mid in model_candidates:
            try:
                return _call(active_client, mid)
            except Exception as e:
                last_err = e
                if active_client is self.free_client and self.paid_client and self._is_rate_limit_error(e):
                    logging.info("Retrying with paid Gemini API key due to backend limit/overload")
                    active_client = self.paid_client
                    try:
                        return _call(active_client, mid)
                    except Exception as e2:
                        last_err = e2
                        logging.warning("Paid Gemini also failed for %s: %s", mid, e2)
                else:
                    logging.warning("Gemini model %s failed: %s; trying next candidate", mid, e)
                    continue
        logging.exception("Gemini call failed after candidates: %s", model_candidates, exc_info=last_err)
        return "エラーが発生しました。"

    def generate_stream(
        self,
        messages: List[types.Content | Dict[str, str]],
        tools: Optional[list] | None = None,
        history_snippets: Optional[List[str]] | None = None,
    ) -> Iterator[str]:
        """
        1) Router でツール要否を判定
        2) 必要なら mode=ANY & allowedFunctionNames で強制ツール呼び出し
        3) function_call を受け取りローカルでツールを実行
        4) tool_response を履歴に追加
        5) mode=NONE で “自然文だけ” をストリームしユーザへ返す
        """
        # ------------- 前処理 -------------
        tools_spec = GEMINI_TOOLS_SPEC if tools is None else tools
        use_tools = bool(tools_spec)
        history_snippets = history_snippets or []
        self._store_reasoning([])
        reasoning_chunks: List[str] = []

        # 直近 user メッセージ抽出（dict と Content 混在対応）
        def _last_user(msgs) -> str:
            for m in reversed(msgs):
                if isinstance(m, dict) and m.get("role") == "user":
                    return m.get("content", "") or ""
                if isinstance(m, types.Content) and m.role == "user":
                    if m.parts and m.parts[0].text:
                        return m.parts[0].text
            return ""

        # ---------- ① Router ----------
        if use_tools:
            decision = route(_last_user(messages), tools_spec)
            logging.info("Router decision:\n%s",
                         json.dumps(decision, indent=2, ensure_ascii=False))

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

        def _stream(client):
            sys_msg, contents = self._convert_messages(messages)
            cfg_kwargs = dict(
                system_instruction=sys_msg,
                safety_settings=GEMINI_SAFETY_CONFIG,
            )
            if use_tools:
                cfg_kwargs["tools"] = _merge_tools_for_gemini(tools_spec)
                cfg_kwargs["tool_config"] = tool_cfg
            if self._thinking_config is not None:
                cfg_kwargs["thinking_config"] = self._thinking_config
            return client.models.generate_content_stream(
                model=self.model,
                contents=contents,
                config=types.GenerateContentConfig(**cfg_kwargs),
            )
        active_client = self.client
        try:
            stream = _stream(active_client)
        except Exception as e:
            if active_client is self.free_client and self.paid_client and self._is_rate_limit_error(e):
                logging.info("Retrying with paid Gemini API key due to rate limit")
                active_client = self.paid_client
                stream = _stream(active_client)
            else:
                logging.exception("Gemini call failed")
                yield "エラーが発生しました。"
                return

        fcall: types.FunctionCall | None = None
        prefix_yielded = False
        seen_stream_texts: Dict[int, str] = {}
        thought_seen: Dict[int, str] = {}
        for chunk in stream:
            raw_logger.debug("Gemini stream chunk:\n%s", chunk)
            if not chunk.candidates:                    # keep-alive
                continue
            cand = chunk.candidates[0]
            raw_logger.debug("Gemini stream candidate:\n%s", cand)
            if not cand.content or not cand.content.parts:
                continue
            cand_index = getattr(cand, "index", 0)
            for part_idx, part in enumerate(cand.content.parts):
                if getattr(part, "function_call", None) and fcall is None:
                    raw_logger.debug(
                        "Gemini function_call (part %s): %s", part_idx, part.function_call
                    )
                    fcall = part.function_call         # 後で実行
                elif _is_truthy_flag(getattr(part, "thought", None)):
                    text_val = getattr(part, "text", None) or ""
                    if text_val:
                        prev_thought = thought_seen.get(cand_index, "")
                        if text_val.startswith(prev_thought):
                            delta = text_val[len(prev_thought):]
                            if delta:
                                reasoning_chunks.append(delta)
                                thought_seen[cand_index] = prev_thought + delta
                        else:
                            reasoning_chunks.append(text_val)
                            thought_seen[cand_index] = prev_thought + text_val

            combined_text = "".join(
                getattr(part, "text", None) or ""
                for part in cand.content.parts
                if getattr(part, "text", None) and not _is_truthy_flag(getattr(part, "thought", None))
            )
            if not combined_text:
                continue

            prev_text = seen_stream_texts.get(cand_index, "")
            new_text = (
                combined_text[len(prev_text):]
                if combined_text.startswith(prev_text)
                else combined_text
            )
            if not new_text:
                continue
            raw_logger.debug("Gemini text delta: %s", new_text)
            if not prefix_yielded and history_snippets:
                yield "\n".join(history_snippets) + "\n"
                prefix_yielded = True
            yield new_text
            seen_stream_texts[cand_index] = combined_text
        # ---------- ③ ツール実行 ----------
        if fcall is None:
            self._store_reasoning(_merge_reasoning_strings(reasoning_chunks))
            return                                     # AUTO モードで text だけ返った

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

        # function_call と tool_response を履歴へ
        parts = [
            types.Part(function_call=fcall)
        ]
        file_parts = [
            types.Part(
                function_response=types.FunctionResponse(
                    name=fcall.name,
                    response={"result": result}
                )
            )
        ]
        if file_path:
            with open(file_path, "rb") as f:
                img_bytes = f.read()
            mime = mimetypes.guess_type(file_path)[0] or "image/png"

            file_parts.append(
                types.Part.from_bytes(           # ← Part を bytes から生成
                    data=img_bytes,
                    mime_type=mime,
                )
            )
        if metadata:
            self._store_attachment(metadata)

        messages.extend([
            types.Content(role="model", parts=parts),
            types.Content(role="tool", parts=file_parts),
        ])

        # ---------- ④ mode=NONE で自然文ストリーム ----------
        fc_none = types.FunctionCallingConfig(mode="NONE")
        tool_cfg_none = types.ToolConfig(functionCallingConfig=fc_none)

        sys_msg2, contents2 = self._convert_messages(messages)
        cfg_kwargs2 = dict(
            system_instruction=sys_msg2,
            safety_settings=GEMINI_SAFETY_CONFIG,
            tools=tool_list,
            tool_config=tool_cfg_none,
        )
        if self._thinking_config is not None:
            cfg_kwargs2["thinking_config"] = self._thinking_config
        stream2 = active_client.models.generate_content_stream(
            model=self.model,
            contents=contents2,
            config=types.GenerateContentConfig(**cfg_kwargs2),
        )

        yielded = False
        prefix_yielded2 = False
        seen_stream_texts2: Dict[int, str] = {}
        thought_seen2: Dict[int, str] = {}
        for chunk in stream2:
            raw_logger.debug("Gemini stream2 chunk:\n%s", chunk)
            if not chunk.candidates:
                continue
            cand = chunk.candidates[0]
            raw_logger.debug("Gemini stream2 candidate:\n%s", cand)
            if not cand.content or not cand.content.parts:
                continue
            cand_index = getattr(cand, "index", 0)
            for part in cand.content.parts:
                if _is_truthy_flag(getattr(part, "thought", None)):
                    text_val = getattr(part, "text", None) or ""
                    if text_val:
                        prev = thought_seen2.get(cand_index, "")
                        if text_val.startswith(prev):
                            delta = text_val[len(prev):]
                            if delta:
                                reasoning_chunks.append(delta)
                                thought_seen2[cand_index] = prev + delta
                        else:
                            reasoning_chunks.append(text_val)
                            thought_seen2[cand_index] = prev + text_val
            combined_text = "".join(
                getattr(part, "text", None) or ""
                for part in cand.content.parts
                if getattr(part, "text", None) and not _is_truthy_flag(getattr(part, "thought", None))
            )
            if not combined_text:
                continue

            prev_text = seen_stream_texts2.get(cand_index, "")
            new_text = (
                combined_text[len(prev_text):]
                if combined_text.startswith(prev_text)
                else combined_text
            )
            if not new_text:
                continue
            raw_logger.debug("Gemini text2 delta: %s", new_text)
            if not prefix_yielded2 and history_snippets:
                yield "\n".join(history_snippets) + "\n"
                prefix_yielded2 = True
            yield new_text
            seen_stream_texts2[cand_index] = combined_text
            yielded = True

        # ---------- ⑤ 保険：モデルが無言の場合 ----------
        if not yielded:
            if history_snippets:
                yield "\n".join(history_snippets) + "\n" + result
            else:
                yield result
        self._store_reasoning(_merge_reasoning_strings(reasoning_chunks))

class OllamaClient(LLMClient):
    """Client for Ollama API."""
    def __init__(self, model: str, context_length: int, supports_images: bool = False):
        super().__init__(supports_images=supports_images)
        self.model = model
        self.context_length = context_length
        self.fallback_client: Optional[LLMClient] = None
        # Prefer explicit OLLAMA_BASE_URL, else probe common bases quickly
        base_env = os.getenv("OLLAMA_BASE_URL") or os.getenv("OLLAMA_HOST")
        probed = self._probe_base(base_env)
        if probed is None:
            # No reachable Ollama → try Gemini 1.5 Flash fallback
            try:
                logging.info("No reachable Ollama; falling back to Gemini 1.5 Flash")
                self.fallback_client = GeminiClient("gemini-1.5-flash")
                self.base = ""
                self.url = ""
            except Exception as e:
                logging.warning("Gemini fallback unavailable: %s", e)
                # Last-resort: still set a localhost base to avoid attribute errors
                self.base = "http://127.0.0.1:11434"
                self.url = f"{self.base}/v1/chat/completions"
        else:
            self.base = probed
            # OpenAI 互換 API（推奨）。利用不可なら generate() で /api/chat に自動フォールバック
            self.url = f"{self.base}/v1/chat/completions"

    def _probe_base(self, preferred: Optional[str]) -> Optional[str]:
        """Pick a reachable Ollama base URL with quick connect timeouts.
        Tries: preferred, 127.0.0.1, localhost, host.docker.internal, common docker bridge.
        """
        candidates: list[str] = []
        if preferred:
            # Accept single or comma-separated list
            for part in str(preferred).split(","):
                part = part.strip()
                if part:
                    candidates.append(part)
        candidates += [
            "http://127.0.0.1:11434",
            "http://localhost:11434",
            "http://host.docker.internal:11434",
            "http://172.17.0.1:11434",   # common docker bridge
        ]
        seen = set()
        for base in candidates:
            if base in seen:
                continue
            seen.add(base)
            try:
                # Try v1 then legacy version endpoint with very short connect timeout
                # Use HEAD first; if not supported fall back to GET
                url_v1 = f"{base}/v1/models"
                r = requests.get(url_v1, timeout=(2, 2))
                if r.ok:
                    logging.info("Using Ollama base: %s (v1)", base)
                    return base
            except Exception:
                pass
            try:
                url_legacy = f"{base}/api/version"
                r2 = requests.get(url_legacy, timeout=(2, 2))
                if r2.ok:
                    logging.info("Using Ollama base: %s (legacy)", base)
                    return base
            except Exception:
                continue
        # No responsive endpoint detected
        logging.warning("No responsive Ollama endpoint detected during probe")
        return None

    def generate(self, messages: List[Dict[str, str]], tools: Optional[list] | None = None) -> str:
        payload_v1 = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {"num_ctx": self.context_length},
            # JSON応答を強制（対応バージョンのみ）
            "response_format": {"type": "json_object"},
        }
        # If Ollama is unavailable and Gemini fallback exists, use it
        if self.fallback_client is not None:
            return self.fallback_client.generate(messages, tools)
        try:
            # Fast connect timeout, generous read timeout
            resp = requests.post(self.url, json=payload_v1, timeout=(3, 300))
            resp.raise_for_status()
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            logging.debug("Raw ollama v1 response: %s", content)
            return content
        except Exception:
            logging.exception("Ollama v1 endpoint failed; trying legacy /api/chat")
            # Fallback to legacy /api/chat
            try:
                resp = requests.post(
                    f"{self.base}/api/chat",
                    json={
                        "model": self.model,
                        "messages": messages,
                        "stream": False,
                        "options": {"num_ctx": self.context_length},
                    },
                    timeout=(3, 300),
                )
                resp.raise_for_status()
                data = resp.json()
                content = data.get("message", {}).get("content", "")
                logging.debug("Raw ollama /api/chat response: %s", content)
                return content
            except Exception:
                logging.exception("Ollama legacy /api/chat failed")
                return "エラーが発生しました。"

    def generate_stream(self, messages: List[Dict[str, str]], tools: Optional[list] | None = None) -> Iterator[str]:
        # If Ollama is unavailable and Gemini fallback exists, stream via Gemini
        if self.fallback_client is not None:
            yield from self.fallback_client.generate_stream(messages, tools)
            return
        try:
            resp = requests.post(
                self.url,
                json={
                    "model": self.model,
                    "messages": messages,
                    "stream": True,
                    "options": {"num_ctx": self.context_length}
                },
                timeout=(3, 300),
                stream=True,
            )
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                chunk = line.decode("utf-8")
                if chunk.startswith("data: "):
                    chunk = chunk[len("data: ") :]
                if chunk.strip() == "[DONE]":
                    break
                try:
                    data = json.loads(chunk)
                    delta = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                    yield delta
                except json.JSONDecodeError:
                    logging.warning("Failed to parse stream chunk: %s", chunk)
        except Exception as e:
            logging.exception("Ollama call failed")
            yield "エラーが発生しました。"

# --- Factory ---
def get_llm_client(model: str, provider: str, context_length: int) -> LLMClient:
    """Factory function to get the appropriate LLM client."""
    config = get_model_config(model)
    supports_images_cfg = config.get("supports_images") if isinstance(config, dict) else None
    if supports_images_cfg is None:
        supports_images = provider == "gemini"
    else:
        supports_images = bool(supports_images_cfg)
    if provider == "openai":
        return OpenAIClient(model, supports_images=supports_images)
    elif provider == "anthropic":
        return AnthropicClient(model, config=config, supports_images=supports_images)
    elif provider == "gemini":
        return GeminiClient(model, config=config, supports_images=supports_images)
    else:
        return OllamaClient(model, context_length, supports_images=supports_images)
