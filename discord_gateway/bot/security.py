from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class HandshakePayload(BaseModel):
    """Validated handshake payload from local applications."""

    type: Literal["hello"]
    token: str = Field(min_length=8, max_length=512)

    class Config:
        extra = "forbid"


def sanitize_message_content(content: str, *, max_length: int) -> str:
    """Clamp incoming message content to a safe, printable length."""

    text = str(content).replace("\x00", "")
    if len(text) > max_length:
        text = text[:max_length]
    return text
