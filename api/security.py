"""Security utilities for API protection.

This module provides guards to protect destructive API endpoints from
unauthorized access, especially when SAIVerse is exposed via Tailscale
or other network interfaces.
"""

import logging
import os
import secrets
from functools import wraps
from typing import Callable, Optional

from fastapi import HTTPException, Request

LOGGER = logging.getLogger(__name__)

# Token for destructive operations (set via environment variable)
# If not set, a random token is generated on startup (requires UI confirmation)
_DESTRUCTIVE_TOKEN: Optional[str] = None


def get_destructive_token() -> str:
    """Get or generate the destructive operation token.

    Returns the token from SAIVERSE_DESTRUCTIVE_TOKEN env var,
    or generates a random one if not set.
    """
    global _DESTRUCTIVE_TOKEN
    if _DESTRUCTIVE_TOKEN is None:
        env_token = os.environ.get("SAIVERSE_DESTRUCTIVE_TOKEN")
        if env_token:
            _DESTRUCTIVE_TOKEN = env_token
        else:
            _DESTRUCTIVE_TOKEN = secrets.token_hex(16)
            LOGGER.info(
                "Generated destructive token (set SAIVERSE_DESTRUCTIVE_TOKEN env var to customize): %s",
                _DESTRUCTIVE_TOKEN[:8] + "..."
            )
    return _DESTRUCTIVE_TOKEN


def is_local_request(request: Request) -> bool:
    """Check if the request originates from localhost.

    Returns True if the request comes from localhost (127.0.0.1, ::1).
    """
    client_host = request.client.host if request.client else None
    if client_host is None:
        return False

    # Check for localhost variants
    local_hosts = {"127.0.0.1", "::1", "localhost"}
    return client_host in local_hosts


def require_local_or_token(request: Request, provided_token: Optional[str] = None) -> None:
    """Require either local origin or valid token.

    Raises HTTPException(403) if neither condition is met.

    Args:
        request: FastAPI request object
        provided_token: Token provided by the client (optional)
    """
    # Allow if request is from localhost
    if is_local_request(request):
        return

    # Check token for remote requests
    if provided_token:
        expected_token = get_destructive_token()
        if secrets.compare_digest(provided_token, expected_token):
            return

    # Neither condition met - reject
    LOGGER.warning(
        "Rejected destructive request from %s (local=%s, has_token=%s)",
        request.client.host if request.client else "unknown",
        is_local_request(request),
        provided_token is not None
    )
    raise HTTPException(
        status_code=403,
        detail="This operation requires local access or a valid token. "
               "Set SAIVERSE_DESTRUCTIVE_TOKEN env var and include it in the X-Destructive-Token header."
    )


class DestructiveActionGuard:
    """Dependency class for protecting destructive endpoints."""

    def __init__(self, require_token: bool = False):
        """Initialize the guard.

        Args:
            require_token: If True, always require token even for localhost.
                          If False (default), localhost is always allowed.
        """
        self.require_token = require_token

    async def __call__(self, request: Request) -> None:
        """Check access permission."""
        if self.require_token:
            # Token required - check header
            provided_token = request.headers.get("X-Destructive-Token")
            expected_token = get_destructive_token()
            if not provided_token or not secrets.compare_digest(provided_token, expected_token):
                raise HTTPException(
                    status_code=403,
                    detail="This operation requires a valid token in X-Destructive-Token header."
                )
        else:
            # Localhost or token
            provided_token = request.headers.get("X-Destructive-Token")
            require_local_or_token(request, provided_token)


# Pre-configured guard instances
local_or_token_guard = DestructiveActionGuard(require_token=False)
token_required_guard = DestructiveActionGuard(require_token=True)