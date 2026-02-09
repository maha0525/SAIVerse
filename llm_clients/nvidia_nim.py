"""NVIDIA NIM client - OpenAI-compatible but with custom structured output."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from .openai import OpenAIClient


class NvidiaNIMClient(OpenAIClient):
    """
    Client for NVIDIA NIM APIs.

    Nvidia NIM is OpenAI-compatible for most operations, but Mistral models
    don't support guided_json or response_format for structured output.

    Workaround for structured output:
    - Define the output schema as a "dummy tool"
    - Force the model to call that tool via tool_choice
    - Extract the tool arguments as the structured output
    """

    def __init__(
        self,
        model: str,
        *,
        supports_images: bool = False,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key_env: Optional[str] = None,
        request_kwargs: Optional[Dict[str, Any]] = None,
        max_image_bytes: Optional[int] = None,
        convert_system_to_user: bool = False,
    ) -> None:
        # Nvidia NIM doesn't need structured_output_backend parameter
        super().__init__(
            model=model,
            supports_images=supports_images,
            api_key=api_key,
            base_url=base_url,
            api_key_env=api_key_env,
            request_kwargs=request_kwargs,
            max_image_bytes=max_image_bytes,
            convert_system_to_user=convert_system_to_user,
            structured_output_backend=None,  # Not used for NIM
        )
        self._nim_base_url = base_url or "https://integrate.api.nvidia.com/v1"
        self._nim_api_key = api_key
        # Get API key from environment if not provided directly
        if not self._nim_api_key and api_key_env:
            import os
            self._nim_api_key = os.environ.get(api_key_env)

    def _create_nim_structured_output_via_tool(
        self,
        messages: List[Dict[str, Any]],
        response_schema: Dict[str, Any],
        temperature: Optional[float],
        max_retries: int = 2,
    ) -> str:
        """
        Get structured output from Nvidia NIM using forced function calling.

        Since Mistral models on Nvidia NIM don't support guided_json or response_format
        for structured output, we use a workaround:
        1. Define the output schema as a "dummy tool"
        2. Force the model to call that tool via tool_choice
        3. Extract the tool arguments as the structured output

        Args:
            messages: The messages to send to the model.
            response_schema: The JSON schema for the expected output.
            temperature: The temperature for generation.
            max_retries: Maximum number of retry attempts for transient errors (default: 2).

        Returns the JSON string of the structured output.
        """
        import httpx
        import time

        url = f"{self._nim_base_url}/chat/completions"

        # Create a dummy tool from the response schema
        dummy_tool = {
            "type": "function",
            "function": {
                "name": "_structured_output",
                "description": "Output the response in the required structured format. You MUST call this function with the appropriate arguments.",
                "parameters": self._add_additional_properties(response_schema),
            }
        }

        # Build request body
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "n": 1,
            "tools": [dummy_tool],
            "tool_choice": {
                "type": "function",
                "function": {"name": "_structured_output"}
            },
        }

        # Add temperature from request_kwargs or parameter
        if temperature is not None:
            body["temperature"] = temperature
        elif "temperature" in self._request_kwargs:
            body["temperature"] = self._request_kwargs["temperature"]

        # Add top_p if present
        if "top_p" in self._request_kwargs:
            body["top_p"] = self._request_kwargs["top_p"]

        # Add max_tokens if present
        if "max_tokens" in self._request_kwargs:
            body["max_tokens"] = self._request_kwargs["max_tokens"]

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._nim_api_key}",
        }

        logging.info("Using forced function calling for structured output (tool: _structured_output)")
        logging.debug("NIM structured output schema: %s", dummy_tool["function"]["parameters"])

        # Retry logic for transient errors (timeouts, connection errors, 5xx)
        last_exception: Optional[Exception] = None
        for attempt in range(max_retries + 1):
            try:
                with httpx.Client(timeout=120.0) as client:
                    response = client.post(url, json=body, headers=headers)
                    response.raise_for_status()
                    resp_json = response.json()
                break  # Success, exit retry loop
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                last_exception = e
                if attempt < max_retries:
                    wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s
                    logging.warning(
                        "NIM structured output request failed (attempt %d/%d): %s. Retrying in %ds...",
                        attempt + 1, max_retries + 1, type(e).__name__, wait_time
                    )
                    time.sleep(wait_time)
                else:
                    logging.error(
                        "NIM structured output request failed after %d attempts: %s",
                        max_retries + 1, e
                    )
                    raise
            except httpx.HTTPStatusError as e:
                last_exception = e
                logging.error(
                    "NIM structured output HTTP %d response body: %s",
                    e.response.status_code, e.response.text
                )
                # Retry on 5xx server errors
                if e.response.status_code >= 500 and attempt < max_retries:
                    wait_time = 2 ** attempt
                    logging.warning(
                        "NIM structured output request failed with %d (attempt %d/%d). Retrying in %ds...",
                        e.response.status_code, attempt + 1, max_retries + 1, wait_time
                    )
                    time.sleep(wait_time)
                else:
                    raise

        from .base import get_llm_logger
        get_llm_logger().debug("Nvidia NIM raw:\n%s", json.dumps(resp_json, indent=2, ensure_ascii=False))

        # Extract tool call arguments
        choices = resp_json.get("choices", [])
        if not choices:
            logging.error("Nvidia NIM returned no choices")
            raise ValueError("No choices in response")

        message = choices[0].get("message", {})
        tool_calls = message.get("tool_calls", [])

        if not tool_calls:
            # Fallback: if no tool call, try to get content
            content = message.get("content", "")
            if content:
                logging.warning("No tool call in response, falling back to content")
                return content
            logging.error("No tool calls and no content in response")
            raise ValueError("No tool calls in response")

        # Get the arguments from the first tool call
        first_call = tool_calls[0]
        function_data = first_call.get("function", {})
        arguments = function_data.get("arguments", "{}")

        logging.debug("Extracted structured output: %s", arguments)
        return arguments

    def generate(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[list] | None = None,
        history_snippets: Optional[List[str]] | None = None,
        response_schema: Optional[Dict[str, Any]] = None,
        *,
        temperature: float | None = None,
        **_: Any,
    ) -> str:
        """
        Generate response using Nvidia NIM.

        For structured output, uses guided_json in extra_body instead of response_format.
        """
        from tools import OPENAI_TOOLS_SPEC, TOOL_REGISTRY
        from tools.core import parse_tool_result
        from .openai import _prepare_openai_messages

        default_tools = OPENAI_TOOLS_SPEC if tools is None else tools
        if response_schema is not None and tools is None:
            tools_spec: List[Dict[str, Any]] | list = []
        else:
            tools_spec = default_tools
        use_tools = bool(tools_spec)
        snippets: List[str] = list(history_snippets or [])
        self._store_reasoning([])

        if response_schema and use_tools:
            logging.warning(
                "response_schema specified alongside tools; structured output is ignored for tool runs."
            )
            response_schema = None

        # For non-tool calls with response_schema, use forced function calling
        # to get structured output (workaround for Mistral models on Nvidia NIM)
        if not use_tools and response_schema:
            try:
                prepared_messages = _prepare_openai_messages(
                    messages, self.supports_images, self.max_image_bytes, self.convert_system_to_user
                )
                # Use forced function calling to get structured output
                text_body = self._create_nim_structured_output_via_tool(
                    messages=prepared_messages,
                    response_schema=response_schema,
                    temperature=temperature,
                )
            except Exception:
                logging.exception("Nvidia NIM structured output call failed")
                raise RuntimeError("NVIDIA NIM API call failed")

            self._store_reasoning([])
            # Check for empty response (structured output should have content)
            if not text_body.strip():
                logging.error(
                    "[nvidia_nim] Empty structured output response. "
                    "Model returned empty content."
                )
                raise RuntimeError("Nvidia NIM returned empty structured output response")
            if snippets:
                prefix = "\n".join(snippets)
                return prefix + ("\n" if text_body and prefix else "") + text_body
            return text_body

        # For tool calls or non-structured output, use parent OpenAI implementation
        return super().generate(
            messages=messages,
            tools=tools,
            history_snippets=history_snippets,
            response_schema=response_schema,
            temperature=temperature,
        )
