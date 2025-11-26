"""Anthropic Claude client via OpenAI-compatible endpoint."""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import openai

from .openai import OpenAIClient


class AnthropicClient(OpenAIClient):
    """Anthropic Claude via OpenAI-compatible endpoint."""

    def __init__(
        self,
        model: str = "claude-sonnet-4-5",
        config: Optional[Dict[str, Any]] | None = None,
        supports_images: bool = False,
    ) -> None:
        api_key = os.getenv("CLAUDE_API_KEY")
        if not api_key:
            raise RuntimeError("CLAUDE_API_KEY environment variable is not set.")

        base_url = os.getenv("ANTHROPIC_OPENAI_BASE_URL", "https://api.anthropic.com/v1/")
        if not base_url.endswith("/"):
            base_url = f"{base_url}/"

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

        logging.warning(
            "Anthropic request rejected due to missing thinking blocks; disabling thinking payload and retrying without it."
        )
        return True

    def _create_completion(self, **kwargs: Any):
        try:
            return self.client.chat.completions.create(**kwargs)
        except openai.BadRequestError as err:
            if self._disable_thinking_if_needed(err):
                return self.client.chat.completions.create(**kwargs)
            raise


__all__ = ["AnthropicClient"]
