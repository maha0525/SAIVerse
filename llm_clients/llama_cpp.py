"""llama.cpp client for local GGUF models."""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Iterator, List, Optional

from .base import LLMClient
from .gemini import GeminiClient

logger = logging.getLogger(__name__)


class LlamaCppClient(LLMClient):
    """Client for llama.cpp GGUF models using llama-cpp-python."""

    def __init__(
        self,
        model_path: str,
        context_length: int = 4096,
        n_gpu_layers: int = -1,
        supports_images: bool = False,
        fallback_on_error: bool = True,
    ) -> None:
        """Initialize llama.cpp client.

        Args:
            model_path: Path to GGUF model file (absolute or relative to cwd)
            context_length: Maximum context length (default: 4096)
            n_gpu_layers: Number of layers to offload to GPU (-1 = all, 0 = CPU only)
            supports_images: Whether model supports image inputs
            fallback_on_error: Fall back to Gemini if model loading fails
        """
        super().__init__(supports_images=supports_images)
        self.model_path = model_path
        self.context_length = context_length
        self.n_gpu_layers = n_gpu_layers
        self.fallback_on_error = fallback_on_error
        self._llm: Optional[Any] = None
        self.fallback_client: Optional[LLMClient] = None

        # Temperature and other generation params
        self._temperature: float = 0.7
        self._top_p: float = 0.9
        self._max_tokens: int = 2048

    def _ensure_model_loaded(self) -> bool:
        """Load model on first use. Returns True if successful."""
        if self._llm is not None:
            return True

        try:
            from llama_cpp import Llama

            # Expand ~ and environment variables in path
            expanded_path = os.path.expanduser(os.path.expandvars(self.model_path))

            if not os.path.exists(expanded_path):
                logger.error(
                    "GGUF model file not found: %s (expanded from %s)",
                    expanded_path,
                    self.model_path,
                )
                raise FileNotFoundError(f"Model file not found: {expanded_path}")

            logger.info(
                "Loading GGUF model from %s (n_gpu_layers=%d, n_ctx=%d)",
                expanded_path,
                self.n_gpu_layers,
                self.context_length,
            )

            self._llm = Llama(
                model_path=expanded_path,
                n_ctx=self.context_length,
                n_gpu_layers=self.n_gpu_layers,
                verbose=False,
            )

            logger.info("GGUF model loaded successfully: %s", expanded_path)
            return True

        except ImportError:
            logger.error(
                "llama-cpp-python not installed. Install with: pip install llama-cpp-python"
            )
            if self.fallback_on_error:
                self._setup_fallback()
            return False
        except Exception as exc:
            logger.error("Failed to load GGUF model: %s", exc, exc_info=True)
            if self.fallback_on_error:
                self._setup_fallback()
            return False

    def _setup_fallback(self) -> None:
        """Setup Gemini fallback client."""
        if self.fallback_client is None:
            try:
                logger.info("Setting up Gemini fallback for llama.cpp")
                self.fallback_client = GeminiClient("gemini-2.0-flash")
            except Exception as exc:
                logger.warning("Gemini fallback setup failed: %s", exc)

    def configure_parameters(self, parameters: Dict[str, Any] | None) -> None:
        """Apply model-specific request parameters."""
        if not parameters:
            return
        if "temperature" in parameters:
            self._temperature = float(parameters["temperature"])
        if "top_p" in parameters:
            self._top_p = float(parameters["top_p"])
        if "max_tokens" in parameters:
            self._max_tokens = int(parameters["max_tokens"])

    def generate(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Any] | None = None,
        response_schema: Dict[str, Any] | None = None,
        *,
        temperature: float | None = None,
        **_: Any,
    ) -> str:
        """Generate response using llama.cpp."""
        if not self._ensure_model_loaded():
            if self.fallback_client:
                logger.info("Using Gemini fallback for generation")
                return self.fallback_client.generate(
                    messages, tools, response_schema, temperature=temperature
                )
            return "エラー: モデルの読み込みに失敗しました。"

        try:
            # Use override temperature if provided
            temp = temperature if temperature is not None else self._temperature

            # llama-cpp-python expects OpenAI-style messages
            response = self._llm.create_chat_completion(
                messages=messages,
                temperature=temp,
                top_p=self._top_p,
                max_tokens=self._max_tokens,
                response_format=(
                    {"type": "json_object", "schema": response_schema}
                    if response_schema
                    else None
                ),
            )

            content = response["choices"][0]["message"]["content"]
            logger.debug("llama.cpp response: %s", content[:200])
            return content

        except Exception as exc:
            logger.error("llama.cpp generation failed: %s", exc, exc_info=True)
            if self.fallback_client:
                logger.info("Falling back to Gemini after llama.cpp error")
                return self.fallback_client.generate(
                    messages, tools, response_schema, temperature=temperature
                )
            return "エラーが発生しました。"

    def generate_stream(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Any] | None = None,
        response_schema: Dict[str, Any] | None = None,
        *,
        temperature: float | None = None,
        **_: Any,
    ) -> Iterator[str]:
        """Generate streaming response using llama.cpp."""
        if not self._ensure_model_loaded():
            if self.fallback_client:
                logger.info("Using Gemini fallback for streaming")
                yield from self.fallback_client.generate_stream(
                    messages, tools, response_schema, temperature=temperature
                )
            else:
                yield "エラー: モデルの読み込みに失敗しました。"
            return

        try:
            temp = temperature if temperature is not None else self._temperature

            stream = self._llm.create_chat_completion(
                messages=messages,
                temperature=temp,
                top_p=self._top_p,
                max_tokens=self._max_tokens,
                stream=True,
                response_format=(
                    {"type": "json_object", "schema": response_schema}
                    if response_schema
                    else None
                ),
            )

            for chunk in stream:
                delta = chunk["choices"][0]["delta"]
                if "content" in delta:
                    yield delta["content"]

        except Exception as exc:
            logger.error("llama.cpp streaming failed: %s", exc, exc_info=True)
            if self.fallback_client:
                logger.info("Falling back to Gemini streaming after llama.cpp error")
                yield from self.fallback_client.generate_stream(
                    messages, tools, response_schema, temperature=temperature
                )
            else:
                yield "エラーが発生しました。"

    def generate_with_tool_detection(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Any] | None = None,
        *,
        temperature: float | None = None,
        **_: Any,
    ) -> Dict[str, Any]:
        """Generate response with tool call detection.

        Returns:
            {"type": "text", "content": str} if no tool call
            {"type": "tool_call", "tool_name": str, "tool_args": dict} if tool call detected
        """
        if not self._ensure_model_loaded():
            if self.fallback_client:
                logger.info("Using Gemini fallback for tool detection")
                return self.fallback_client.generate_with_tool_detection(
                    messages, tools, temperature=temperature
                )
            return {"type": "text", "content": "エラー: モデルの読み込みに失敗しました。"}

        try:
            temp = temperature if temperature is not None else self._temperature

            # Convert tools to OpenAI format if needed
            tools_spec = tools or []

            response = self._llm.create_chat_completion(
                messages=messages,
                temperature=temp,
                top_p=self._top_p,
                max_tokens=self._max_tokens,
                tools=tools_spec if tools_spec else None,
            )

            choice = response["choices"][0]
            message = choice["message"]
            content = message.get("content", "") or ""
            tool_calls = message.get("tool_calls", [])

            if tool_calls:
                tc = tool_calls[0]
                func = tc.get("function", {})
                tool_name = func.get("name", "")
                try:
                    tool_args = json.loads(func.get("arguments", "{}"))
                except json.JSONDecodeError:
                    logger.warning(
                        "Tool call arguments invalid JSON: %s", func.get("arguments")
                    )
                    tool_args = {}

                if content.strip():
                    return {
                        "type": "both",
                        "content": content,
                        "tool_name": tool_name,
                        "tool_args": tool_args,
                    }
                else:
                    return {
                        "type": "tool_call",
                        "tool_name": tool_name,
                        "tool_args": tool_args,
                    }
            else:
                return {"type": "text", "content": content}

        except Exception as exc:
            logger.error("llama.cpp tool detection failed: %s", exc, exc_info=True)
            if self.fallback_client:
                logger.info("Falling back to Gemini for tool detection after error")
                return self.fallback_client.generate_with_tool_detection(
                    messages, tools, temperature=temperature
                )
            return {"type": "text", "content": "エラーが発生しました。"}


__all__ = ["LlamaCppClient"]
