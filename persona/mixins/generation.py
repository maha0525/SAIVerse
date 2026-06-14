"""Messaging and LLM generation helpers shared by persona core.

旧 pre-SEA LLM 生成パス (_generate / _build_messages / handle_user_input 等) は
SEA runtime への移行完了に伴い 2026-06 に撤去済。
モデル切替 (set_model / apply_parameter_overrides) は SEA runtime 経由でも必要。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from saiverse.model_configs import model_supports_images, get_model_parameters


class PersonaGenerationMixin:
    """Model switching and parameter override helpers for PersonaCore."""

    def set_model(
        self,
        model: str,
        context_length: int,
        provider: str,
        parameter_overrides: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.model = model
        self.provider = provider
        self.context_length = context_length
        self.model_supports_images = model_supports_images(model)
        # Invalidate: the lazy property getter will recreate on next access
        self._llm_client = None
        if parameter_overrides:
            self._pending_parameter_overrides = parameter_overrides

    def apply_parameter_overrides(self, overrides: Optional[Dict[str, Any]] = None) -> None:
        if not overrides:
            return
        self._pending_parameter_overrides = overrides
        if getattr(self, "_llm_client", None) is not None:
            allowed = get_model_parameters(self.model)
            filtered = {
                key: value for key, value in overrides.items() if key in allowed
            }
            if not filtered:
                return
            try:
                self._llm_client.configure_parameters(filtered)
            except Exception:
                logging.debug("Failed to apply parameter overrides for %s", self.persona_name, exc_info=True)


__all__ = ["PersonaGenerationMixin"]
