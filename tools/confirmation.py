"""Generic spell confirmation helper.

Lets any native tool (including addon tools) ask the user to approve a
side-effecting action before it runs. This is the generic replacement for the
X-specific tweet_confirmation mechanism: a single manager-backed request/wait
channel surfaced to the frontend as a ``spell_confirmation`` SSE event.

Flow:
    result = request_spell_confirmation("X投稿の確認", text, editable=True, text=text)
    if not result.approved:
        return "キャンセルされました"
    final = result.edited_text  # user may have edited it

Confirmation is *skipped* (auto-approved) when:
- ``auto_mode`` is active — autonomous pulses are already gated by the
  playbook permission flow, so a second prompt would be redundant.
- there is no event channel / manager — there is no UI to ask, so blocking
  forever would hang the tool. We approve and log a warning.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

from tools.context import get_active_manager, get_auto_mode, get_event_callback

LOGGER = logging.getLogger(__name__)

CONFIRM_TIMEOUT_DEFAULT = 120


@dataclass
class ConfirmationResult:
    """Outcome of a confirmation request.

    ``reason`` is one of: approved | rejected | timeout | auto | no_channel.
    ``edited_text`` carries the (possibly user-edited) text for editable
    confirmations; for non-editable ones it echoes the input ``text``.
    """

    approved: bool
    edited_text: Optional[str] = None
    reason: str = ""


def request_spell_confirmation(
    title: str,
    body: str,
    *,
    editable: bool = False,
    text: Optional[str] = None,
    addon: Optional[str] = None,
    confirm_text: Optional[str] = None,
    max_chars: Optional[int] = None,
    timeout: int = CONFIRM_TIMEOUT_DEFAULT,
) -> ConfirmationResult:
    """Ask the user to confirm a spell action and block until they respond.

    Args:
        title: Short dialog title (e.g. "SwitchBot デバイス操作の確認").
        body: Human-readable description of what will happen.
        editable: If True, the user may edit ``text`` before approving.
        text: Editable payload (only meaningful when ``editable`` is True).
        addon: Originating addon name (for the UI / logging).
        confirm_text: Optional custom label for the approve button.
        timeout: Seconds to wait for a response before treating as cancelled.

    Returns:
        ConfirmationResult.
    """
    if get_auto_mode():
        return ConfirmationResult(approved=True, edited_text=text, reason="auto")

    callback = get_event_callback()
    manager = get_active_manager()
    if not callback or not manager:
        LOGGER.warning(
            "[spell_confirm] no event channel/manager; auto-approving %r", title
        )
        return ConfirmationResult(approved=True, edited_text=text, reason="no_channel")

    request_id = str(uuid.uuid4())
    event = threading.Event()
    manager._pending_spell_confirmations[request_id] = event

    payload: Dict[str, Any] = {
        "type": "spell_confirmation",
        "request_id": request_id,
        "title": title,
        "body": body,
        "editable": editable,
        "addon": addon,
    }
    if confirm_text:
        payload["confirm_text"] = confirm_text
    if max_chars is not None:
        payload["max_chars"] = max_chars
    if editable and text is not None:
        payload["text"] = text

    LOGGER.info(
        "[spell_confirm] requesting confirmation id=%s title=%r addon=%s",
        request_id, title, addon,
    )
    callback(payload)

    responded = event.wait(timeout=timeout)

    # Cleanup regardless of outcome.
    manager._pending_spell_confirmations.pop(request_id, None)
    response = manager._spell_confirmation_responses.pop(request_id, None)

    if not responded or response is None:
        LOGGER.info("[spell_confirm] timed out id=%s", request_id)
        return ConfirmationResult(approved=False, reason="timeout")

    if response == "reject":
        LOGGER.info("[spell_confirm] rejected id=%s", request_id)
        return ConfirmationResult(approved=False, reason="rejected")

    if response.startswith("edit:"):
        LOGGER.info("[spell_confirm] approved with edit id=%s", request_id)
        return ConfirmationResult(
            approved=True, edited_text=response[5:], reason="approved"
        )

    # plain "approve"
    LOGGER.info("[spell_confirm] approved id=%s", request_id)
    return ConfirmationResult(approved=True, edited_text=text, reason="approved")
