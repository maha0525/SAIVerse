"""Tests for API security guards (Phase 1)."""

import os
import pytest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.testclient import TestClient

from api.security import (
    is_local_request,
    require_local_or_token,
    get_destructive_token,
    DestructiveActionGuard,
    local_or_token_guard,
)


class TestIsLocalRequest:
    """Tests for is_local_request function."""

    def test_returns_true_for_127_0_0_1(self):
        """Localhost 127.0.0.1 should be recognized as local."""
        request = MagicMock(spec=Request)
        request.client = MagicMock()
        request.client.host = "127.0.0.1"
        assert is_local_request(request) is True

    def test_returns_true_for_localhost_ipv6(self):
        """IPv6 localhost ::1 should be recognized as local."""
        request = MagicMock(spec=Request)
        request.client = MagicMock()
        request.client.host = "::1"
        assert is_local_request(request) is True

    def test_returns_false_for_external_ip(self):
        """External IPs should not be recognized as local."""
        request = MagicMock(spec=Request)
        request.client = MagicMock()
        request.client.host = "192.168.1.1"
        assert is_local_request(request) is False

    def test_returns_false_for_no_client(self):
        """Requests without client info should not be considered local."""
        request = MagicMock(spec=Request)
        request.client = None
        assert is_local_request(request) is False


class TestGetDestructiveToken:
    """Tests for get_destructive_token function."""

    def test_returns_env_token_if_set(self):
        """Should return token from environment variable if set."""
        with patch.dict(os.environ, {"SAIVERSE_DESTRUCTIVE_TOKEN": "test-token-123"}):
            # Reset the cached token
            import api.security
            api.security._DESTRUCTIVE_TOKEN = None
            token = get_destructive_token()
            assert token == "test-token-123"
            # Cleanup
            api.security._DESTRUCTIVE_TOKEN = None

    def test_generates_random_token_if_not_set(self):
        """Should generate a random token if env var is not set."""
        with patch.dict(os.environ, {}, clear=True):
            import api.security
            api.security._DESTRUCTIVE_TOKEN = None
            token1 = get_destructive_token()
            token2 = get_destructive_token()
            # Should return same token on subsequent calls
            assert token1 == token2
            # Should be a valid hex string
            assert len(token1) == 32  # token_hex(16) produces 32 chars
            # Cleanup
            api.security._DESTRUCTIVE_TOKEN = None


class TestRequireLocalOrToken:
    """Tests for require_local_or_token function."""

    def test_allows_localhost_without_token(self):
        """Localhost requests should be allowed without token."""
        request = MagicMock(spec=Request)
        request.client = MagicMock()
        request.client.host = "127.0.0.1"
        # Should not raise
        require_local_or_token(request, None)

    def test_allows_remote_with_valid_token(self):
        """Remote requests with valid token should be allowed."""
        import api.security
        api.security._DESTRUCTIVE_TOKEN = "test-token"
        request = MagicMock(spec=Request)
        request.client = MagicMock()
        request.client.host = "192.168.1.1"
        # Should not raise
        require_local_or_token(request, "test-token")
        # Cleanup
        api.security._DESTRUCTIVE_TOKEN = None

    def test_rejects_remote_without_token(self):
        """Remote requests without token should be rejected."""
        import api.security
        api.security._DESTRUCTIVE_TOKEN = "test-token"
        request = MagicMock(spec=Request)
        request.client = MagicMock()
        request.client.host = "192.168.1.1"
        with pytest.raises(HTTPException) as exc_info:
            require_local_or_token(request, None)
        assert exc_info.value.status_code == 403
        # Cleanup
        api.security._DESTRUCTIVE_TOKEN = None

    def test_rejects_remote_with_invalid_token(self):
        """Remote requests with invalid token should be rejected."""
        import api.security
        api.security._DESTRUCTIVE_TOKEN = "correct-token"
        request = MagicMock(spec=Request)
        request.client = MagicMock()
        request.client.host = "192.168.1.1"
        with pytest.raises(HTTPException) as exc_info:
            require_local_or_token(request, "wrong-token")
        assert exc_info.value.status_code == 403
        # Cleanup
        api.security._DESTRUCTIVE_TOKEN = None


class TestDestructiveActionGuard:
    """Tests for DestructiveActionGuard class."""

    def test_guard_allows_localhost_by_default(self):
        """Guard should allow localhost requests by default."""
        guard = DestructiveActionGuard(require_token=False)
        request = MagicMock(spec=Request)
        request.client = MagicMock()
        request.client.host = "127.0.0.1"
        request.headers = {}

        # Should not raise
        import asyncio
        asyncio.run(guard(request))

    def test_guard_rejects_remote_without_token(self):
        """Guard should reject remote requests without token."""
        import api.security
        api.security._DESTRUCTIVE_TOKEN = "secret-token"

        guard = DestructiveActionGuard(require_token=False)
        request = MagicMock(spec=Request)
        request.client = MagicMock()
        request.client.host = "10.0.0.1"
        request.headers = {}

        with pytest.raises(HTTPException) as exc_info:
            import asyncio
            asyncio.run(guard(request))
        assert exc_info.value.status_code == 403

        # Cleanup
        api.security._DESTRUCTIVE_TOKEN = None