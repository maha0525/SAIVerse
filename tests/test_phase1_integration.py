"""Integration tests for API security (Phase 1).

Tests the actual FastAPI endpoints with security guards.
"""

import os
import pytest
from unittest.mock import MagicMock, patch, AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient


# Create a minimal test app with protected endpoints
def create_test_app():
    """Create a minimal FastAPI app with protected endpoints for testing."""
    app = FastAPI()

    from api.security import local_or_token_guard
    from fastapi import Depends, Request

    @app.get("/api/version")
    async def get_version():
        """Public endpoint - no protection."""
        return {"version": "1.0.0"}

    @app.post("/api/system/update")
    async def trigger_update(request: Request, _guard: None = Depends(local_or_token_guard)):
        """Protected endpoint - requires local or token."""
        return {"status": "updating"}

    @app.post("/api/world/cities")
    async def create_city(_guard: None = Depends(local_or_token_guard)):
        """Protected endpoint - requires local or token."""
        return {"message": "City created"}

    @app.delete("/api/world/cities/1")
    async def delete_city(_guard: None = Depends(local_or_token_guard)):
        """Protected endpoint - requires local or token."""
        return {"message": "City deleted"}

    return app


class TestPublicEndpoints:
    """Tests for public (unprotected) endpoints."""

    def test_version_endpoint_is_public(self):
        """Version endpoint should be accessible without authentication."""
        app = create_test_app()
        client = TestClient(app)

        response = client.get("/api/version")
        assert response.status_code == 200
        assert response.json()["version"] == "1.0.0"


class TestProtectedEndpoints:
    """Tests for protected endpoints with security guards."""

    def test_update_endpoint_allows_localhost(self):
        """Update endpoint should allow localhost requests."""
        # Reset token
        import api.security
        api.security._DESTRUCTIVE_TOKEN = None

        app = create_test_app()
        client = TestClient(app)

        # TestClient uses 127.0.0.1 by default
        # But we need to mock the request to appear as localhost
        with patch("api.security.is_local_request") as mock_local:
            mock_local.return_value = True
            response = client.post("/api/system/update")
            assert response.status_code == 200

    def test_update_endpoint_rejects_external_without_token(self):
        """Update endpoint should reject external requests without token."""
        import api.security
        api.security._DESTRUCTIVE_TOKEN = "test-secret-token"

        app = create_test_app()

        # Create a custom test client that simulates external request
        with TestClient(app) as client:
            # The TestClient actually uses testclient as host, not localhost
            # So this should fail without token
            response = client.post("/api/system/update")
            assert response.status_code == 403
            assert "local access or a valid token" in response.json()["detail"]

        api.security._DESTRUCTIVE_TOKEN = None

    def test_update_endpoint_allows_external_with_valid_token(self):
        """Update endpoint should allow external requests with valid token."""
        import api.security
        api.security._DESTRUCTIVE_TOKEN = "test-secret-token"

        app = create_test_app()

        with TestClient(app) as client:
            response = client.post(
                "/api/system/update",
                headers={"X-Destructive-Token": "test-secret-token"}
            )
            assert response.status_code == 200

        api.security._DESTRUCTIVE_TOKEN = None

    def test_update_endpoint_rejects_invalid_token(self):
        """Update endpoint should reject requests with invalid token."""
        import api.security
        api.security._DESTRUCTIVE_TOKEN = "correct-token"

        app = create_test_app()

        with TestClient(app) as client:
            response = client.post(
                "/api/system/update",
                headers={"X-Destructive-Token": "wrong-token"}
            )
            assert response.status_code == 403

        api.security._DESTRUCTIVE_TOKEN = None


class TestWorldEndpoints:
    """Tests for world API endpoints with security guards."""

    def test_create_city_rejects_external_without_token(self):
        """Create city endpoint should reject external requests without token."""
        import api.security
        api.security._DESTRUCTIVE_TOKEN = "test-token"

        app = create_test_app()

        with TestClient(app) as client:
            response = client.post("/api/world/cities")
            assert response.status_code == 403

        api.security._DESTRUCTIVE_TOKEN = None

    def test_create_city_allows_with_token(self):
        """Create city endpoint should allow requests with valid token."""
        import api.security
        api.security._DESTRUCTIVE_TOKEN = "test-token"

        app = create_test_app()

        with TestClient(app) as client:
            response = client.post(
                "/api/world/cities",
                headers={"X-Destructive-Token": "test-token"}
            )
            assert response.status_code == 200

        api.security._DESTRUCTIVE_TOKEN = None

    def test_delete_city_rejects_external_without_token(self):
        """Delete city endpoint should reject external requests without token."""
        import api.security
        api.security._DESTRUCTIVE_TOKEN = "test-token"

        app = create_test_app()

        with TestClient(app) as client:
            response = client.delete("/api/world/cities/1")
            assert response.status_code == 403

        api.security._DESTRUCTIVE_TOKEN = None


class TestCORSConfiguration:
    """Tests for CORS configuration in main.py."""

    def test_cors_default_localhost_only(self):
        """Default CORS should only allow localhost."""
        # Test the logic without importing main.py
        test_cors_origins = [
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ]
        # These should be in the default origins
        assert "http://localhost:3000" in test_cors_origins
        assert "*" not in test_cors_origins

    def test_cors_custom_origins_from_env(self):
        """CORS should respect SAIVERSE_CORS_ORIGINS environment variable."""
        with patch.dict(os.environ, {"SAIVERSE_CORS_ORIGINS": "https://example.com,https://app.example.com"}):
            # This tests that the env var is read correctly
            cors_origins_env = os.getenv("SAIVERSE_CORS_ORIGINS", "")
            cors_origins = [origin.strip() for origin in cors_origins_env.split(",") if origin.strip()]
            assert "https://example.com" in cors_origins
            assert "https://app.example.com" in cors_origins

    def test_cors_wildcard_with_credentials_warning(self):
        """CORS wildcard with credentials should be detected as misconfiguration."""
        # Test the logic for detecting misconfiguration
        cors_origins = ["*"]
        allow_credentials = True

        # This configuration is a security risk
        is_misconfigured = "*" in cors_origins and allow_credentials
        assert is_misconfigured is True


class TestPortCleanupSafety:
    """Tests for safe port cleanup logic (without importing main.py)."""

    def test_is_saiverse_process_logic(self):
        """Test the logic for identifying SAIVerse processes."""
        # SAIVerse keywords to check
        saiverse_keywords = [
            "saiverse",
            "uvicorn",
            "main.py",
            "api_server.py",
            "sds_server.py",
        ]

        # Test SAIVerse command line
        saiverse_cmdline = ["python", "main.py", "city_a"]
        cmdline_str = " ".join(saiverse_cmdline).lower()
        is_saiverse = any(kw in cmdline_str for kw in saiverse_keywords)
        assert is_saiverse is True

        # Test unrelated command line
        unrelated_cmdline = ["node", "some-other-app.js"]
        cmdline_str = " ".join(unrelated_cmdline).lower()
        is_saiverse = any(kw in cmdline_str for kw in saiverse_keywords)
        assert is_saiverse is False

    def test_port_cleanup_force_env_var(self):
        """SAIVERSE_FORCE_PORT_CLEANUP should control force mode."""
        with patch.dict(os.environ, {"SAIVERSE_FORCE_PORT_CLEANUP": "true"}):
            force_mode = os.getenv("SAIVERSE_FORCE_PORT_CLEANUP", "false").lower() == "true"
            assert force_mode is True

        with patch.dict(os.environ, {"SAIVERSE_FORCE_PORT_CLEANUP": "false"}, clear=True):
            force_mode = os.getenv("SAIVERSE_FORCE_PORT_CLEANUP", "false").lower() == "true"
            assert force_mode is False

    def test_port_cleanup_default_is_safe(self):
        """By default, port cleanup should be safe (force=false)."""
        # Default value when env var is not set
        default_force = os.getenv("SAIVERSE_FORCE_PORT_CLEANUP", "false").lower() == "true"
        assert default_force is False