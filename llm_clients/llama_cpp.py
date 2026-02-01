"""llama.cpp client for local GGUF models."""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Iterator, List, Optional

from .base import LLMClient

logger = logging.getLogger(__name__)


class LlamaCppClient(LLMClient):
    """Client for llama.cpp GGUF models using llama-cpp-python."""

    def __init__(
        self,
        model_path: str,
        context_length: int = 4096,
        n_gpu_layers: int = -1,
        supports_images: bool = False,
    ) -> None:
        """Initialize llama.cpp client.

        Args:
            model_path: Path to GGUF model file (absolute or relative to cwd)
            context_length: Maximum context length (default: 4096)
            n_gpu_layers: Number of layers to offload to GPU (-1 = all, 0 = CPU only)
            supports_images: Whether model supports image inputs
        """
        super().__init__(supports_images=supports_images)
        self.model_path = model_path
        self.context_length = context_length
        self.n_gpu_layers = n_gpu_layers
        self._llm: Optional[Any] = None

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

            # Expand ~ and environment variables in path, then normalize separators
            expanded_path = os.path.normpath(
                os.path.expanduser(os.path.expandvars(self.model_path))
            )

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
            raise RuntimeError("llama-cpp-python not installed")
        except Exception as exc:
            logger.error("Failed to load GGUF model: %s", exc, exc_info=True)
            raise RuntimeError(f"Failed to load GGUF model: {exc}")

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
    ) -> str | Dict[str, Any]:
        """Unified generate method.
        
        Args:
            messages: Conversation messages
            tools: Tool specifications. If provided, returns Dict with tool detection.
                   If None or empty, returns str with text response.
            response_schema: Optional JSON schema for structured output
            temperature: Optional temperature override
            
        Returns:
            str: Text response when tools is None or empty
            Dict: Tool detection result when tools is provided
        """
        self._ensure_model_loaded()
        tools_spec = tools or []
        use_tools = bool(tools_spec)
        temp = temperature if temperature is not None else self._temperature

        try:
            if use_tools:
                # Tool mode: return Dict with tool detection
                response = self._llm.create_chat_completion(
                    messages=messages,
                    temperature=temp,
                    top_p=self._top_p,
                    max_tokens=self._max_tokens,
                    tools=tools_spec,
                )

                # Store usage if available
                usage = response.get("usage")
                if usage:
                    self._store_usage(
                        input_tokens=usage.get("prompt_tokens", 0) or 0,
                        output_tokens=usage.get("completion_tokens", 0) or 0,
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

            # Non-tool mode: return str
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

            # Store usage if available
            usage = response.get("usage")
            if usage:
                self._store_usage(
                    input_tokens=usage.get("prompt_tokens", 0) or 0,
                    output_tokens=usage.get("completion_tokens", 0) or 0,
                )

            content = response["choices"][0]["message"]["content"]
            logger.debug("llama.cpp response: %s", content[:200])
            return content

        except Exception as exc:
            logger.error("llama.cpp generation failed: %s", exc, exc_info=True)
            raise RuntimeError(f"llama.cpp API call failed: {exc}")

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
        self._ensure_model_loaded() 

        # For structured output, use non-streaming to get complete JSON
        if response_schema:
            try:
                temp = temperature if temperature is not None else self._temperature
                response = self._llm.create_chat_completion(
                    messages=messages,
                    temperature=temp,
                    top_p=self._top_p,
                    max_tokens=self._max_tokens,
                    stream=False,
                    response_format={"type": "json_object", "schema": response_schema},
                )
                _ = response["choices"][0]["message"]["content"]  # Structured output captured but not yielded
                yield ""
                return
            except Exception as exc:
                logger.error("llama.cpp structured output failed: %s", exc, exc_info=True)
                raise RuntimeError(f"llama.cpp structured output failed: {exc}")

        # Normal streaming mode (no response_schema)
        try:
            temp = temperature if temperature is not None else self._temperature 

            stream = self._llm.create_chat_completion(
                messages=messages,
                temperature=temp,
                top_p=self._top_p,
                max_tokens=self._max_tokens,
                stream=True,
            )

            for chunk in stream:
                delta = chunk["choices"][0]["delta"]
                if "content" in delta:
                    yield delta["content"]

        except Exception as exc:
            logger.error("llama.cpp streaming failed: %s", exc, exc_info=True)
            raise RuntimeError(f"llama.cpp streaming failed: {exc}")

    def generate_with_tool_detection(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Any] | None = None,
        *,
        temperature: float | None = None,
        **_: Any,
    ) -> Dict[str, Any]:
        """DEPRECATED: Use generate(messages, tools=[...]) instead.
        
        This method is kept for backward compatibility with existing code.
        It simply delegates to generate() with tools specified.
        """
        import warnings
        warnings.warn(
            "generate_with_tool_detection() is deprecated. Use generate(messages, tools=[...]) instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        tools_spec = tools or []
        if not tools_spec:
            result = self.generate(messages, temperature=temperature)
            if isinstance(result, str):
                return {"type": "text", "content": result}
            return result
        return self.generate(messages, tools=tools_spec, temperature=temperature)


__all__ = ["LlamaCppClient"]
