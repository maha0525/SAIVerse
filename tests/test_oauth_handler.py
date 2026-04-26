"""Unit tests for saiverse.oauth.handler.

Tests cover PKCE generation, in-memory state lifecycle, build_authorize_url,
exchange_code (mocked httpx), get_valid_token (no-refresh and refresh paths),
get_status, and disconnect.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import AI, Base, City, User


def _make_test_db():
    """Create an in-memory SQLite DB with the schema applied."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


class OAuthHandlerTests(unittest.TestCase):
    """End-to-end tests for the OAuth flow handler."""

    ADDON_NAME = "saiverse-test-addon"
    FLOW_KEY = "test_flow"
    PERSONA_ID = "alice"

    def setUp(self):
        # Tempdir to act as ~/.saiverse and EXPANSION_DATA_DIR
        self._tmp = tempfile.TemporaryDirectory()
        # Register tempdir cleanup with addCleanup so it runs LAST (after engine
        # disposal in tearDown). On Windows, SQLite locks block dir cleanup.
        self.addCleanup(self._tmp.cleanup)

        self._home = Path(self._tmp.name) / ".saiverse"
        self._expansion = Path(self._tmp.name) / "expansion_data"
        os.environ["SAIVERSE_HOME"] = str(self._home)

        # Patch EXPANSION_DATA_DIR (module-level constant in data_paths)
        from saiverse import data_paths
        self._patches = [
            patch.object(data_paths, "EXPANSION_DATA_DIR", self._expansion),
        ]
        for p in self._patches:
            p.start()

        # Create a test addon with oauth_flows declared
        addon_dir = self._expansion / self.ADDON_NAME
        addon_dir.mkdir(parents=True, exist_ok=True)
        (addon_dir / "addon.json").write_text(json.dumps({
            "name": self.ADDON_NAME,
            "version": "0.0.1",
            "params_schema": [
                {"key": "client_id", "label": "Client ID", "type": "text"},
                {"key": "client_secret", "label": "Client Secret", "type": "password"},
            ],
            "oauth_flows": [
                {
                    "key": self.FLOW_KEY,
                    "label": "Test Flow",
                    "provider": "oauth2_pkce",
                    "authorize_url": "https://example.com/oauth/authorize",
                    "token_url": "https://example.com/oauth/token",
                    "scopes": ["read", "write"],
                    "client_id_param": "client_id",
                    "client_secret_param": "client_secret",
                    "result_mapping": {
                        "access_token": "test_access_token",
                        "refresh_token": "test_refresh_token",
                        "expires_at": "test_expires_at",
                    },
                },
            ],
        }), encoding="utf-8")

        # Set up an in-memory SQLite DB and patch SessionLocal
        self._engine = _make_test_db()
        TestSession = sessionmaker(bind=self._engine)

        # Seed required parent rows (City, AI) for AddonPersonaConfig FK
        db = TestSession()
        try:
            db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
            db.flush()
            city = City(USERID=1, CITYNAME="test_city", UI_PORT=3001, API_PORT=8001)
            db.add(city)
            db.flush()
            db.add(AI(AIID=self.PERSONA_ID, HOME_CITYID=city.CITYID, AINAME="Alice"))
            db.commit()
        finally:
            db.close()

        from database import session as session_module
        self._session_patch = patch.object(session_module, "SessionLocal", TestSession)
        self._session_patch.start()

        # Set up grobal AddonConfig with credentials
        from database.models import AddonConfig
        db = TestSession()
        try:
            db.add(AddonConfig(
                addon_name=self.ADDON_NAME,
                is_enabled=True,
                params_json=json.dumps({
                    "client_id": "test_client_id",
                    "client_secret": "test_client_secret",
                }),
            ))
            db.commit()
        finally:
            db.close()

        # Reset oauth handler internal state between tests
        from saiverse.oauth import handler as oauth_handler
        oauth_handler._pending_states.clear()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._session_patch.stop()
        os.environ.pop("SAIVERSE_HOME", None)
        # Dispose engine BEFORE temp dir cleanup so SQLite handles release.
        self._engine.dispose()

    # ------------------------------------------------------------------
    # PKCE helpers
    # ------------------------------------------------------------------

    def test_pkce_challenge_is_s256(self):
        from saiverse.oauth.handler import (
            _code_challenge_from_verifier,
            _generate_code_verifier,
        )
        verifier = _generate_code_verifier()
        self.assertGreaterEqual(len(verifier), 43)
        self.assertLessEqual(len(verifier), 128)
        challenge = _code_challenge_from_verifier(verifier)
        # S256 produces 43-char URL-safe base64 (no padding)
        self.assertEqual(len(challenge), 43)
        self.assertNotIn("=", challenge)

    # ------------------------------------------------------------------
    # build_authorize_url
    # ------------------------------------------------------------------

    def test_build_authorize_url_includes_required_params(self):
        from saiverse.oauth.handler import build_authorize_url, _pending_states

        url = build_authorize_url(
            self.ADDON_NAME, self.FLOW_KEY, self.PERSONA_ID,
            base_url="http://127.0.0.1:8000",
        )

        parsed = urlparse(url)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "example.com")

        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        self.assertEqual(params["response_type"], "code")
        self.assertEqual(params["client_id"], "test_client_id")
        self.assertEqual(
            params["redirect_uri"],
            f"http://127.0.0.1:8000/api/oauth/callback/{self.ADDON_NAME}/{self.FLOW_KEY}",
        )
        self.assertEqual(params["scope"], "read write")
        self.assertEqual(params["code_challenge_method"], "S256")
        self.assertIn("state", params)
        self.assertIn("code_challenge", params)

        # state should be registered in pending
        state = params["state"]
        self.assertIn(state, _pending_states)
        pending = _pending_states[state]
        self.assertEqual(pending.persona_id, self.PERSONA_ID)
        self.assertEqual(pending.addon_name, self.ADDON_NAME)

    def test_build_authorize_url_rejects_unsupported_provider(self):
        from saiverse.oauth.handler import OAuthError, build_authorize_url

        addon_dir = self._expansion / self.ADDON_NAME
        manifest = json.loads((addon_dir / "addon.json").read_text(encoding="utf-8"))
        manifest["oauth_flows"][0]["provider"] = "oauth1"
        (addon_dir / "addon.json").write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaises(OAuthError):
            build_authorize_url(
                self.ADDON_NAME, self.FLOW_KEY, self.PERSONA_ID,
                base_url="http://127.0.0.1:8000",
            )

    def test_build_authorize_url_rejects_missing_client_id(self):
        from database.models import AddonConfig
        from database.session import SessionLocal

        from saiverse.oauth.handler import OAuthError, build_authorize_url

        # Clear client_id from AddonConfig
        db = SessionLocal()
        try:
            row = db.query(AddonConfig).filter(
                AddonConfig.addon_name == self.ADDON_NAME
            ).first()
            row.params_json = json.dumps({})
            db.commit()
        finally:
            db.close()

        with self.assertRaises(OAuthError):
            build_authorize_url(
                self.ADDON_NAME, self.FLOW_KEY, self.PERSONA_ID,
                base_url="http://127.0.0.1:8000",
            )

    def test_unknown_flow_raises(self):
        from saiverse.oauth.handler import (
            OAuthFlowNotFoundError,
            build_authorize_url,
        )
        with self.assertRaises(OAuthFlowNotFoundError):
            build_authorize_url(
                self.ADDON_NAME, "nonexistent", self.PERSONA_ID,
                base_url="http://127.0.0.1:8000",
            )

    # ------------------------------------------------------------------
    # exchange_code
    # ------------------------------------------------------------------

    def _start_flow_and_get_state(self) -> str:
        from saiverse.oauth.handler import build_authorize_url

        url = build_authorize_url(
            self.ADDON_NAME, self.FLOW_KEY, self.PERSONA_ID,
            base_url="http://127.0.0.1:8000",
        )
        return parse_qs(urlparse(url).query)["state"][0]

    def _mock_token_response(self, body: dict, status: int = 200):
        """Build an httpx-compatible mock response."""
        mock_response = MagicMock()
        mock_response.status_code = status
        mock_response.json.return_value = body
        mock_response.text = json.dumps(body)
        return mock_response

    def test_exchange_code_saves_tokens_via_result_mapping(self):
        from saiverse.oauth import handler as oauth_handler
        from saiverse.oauth.handler import _load_persona_params, exchange_code

        state = self._start_flow_and_get_state()

        token_body = {
            "access_token": "access_xyz",
            "refresh_token": "refresh_xyz",
            "expires_in": 3600,
            "token_type": "Bearer",
        }
        mock_resp = self._mock_token_response(token_body)

        with patch.object(oauth_handler.httpx, "Client") as MockClient:
            instance = MockClient.return_value.__enter__.return_value
            instance.post.return_value = mock_resp

            result = exchange_code(
                self.ADDON_NAME, self.FLOW_KEY, "code_abc", state,
            )

        self.assertEqual(result["persona_id"], self.PERSONA_ID)
        saved_keys = set(result["saved_keys"])
        self.assertIn("test_access_token", saved_keys)
        self.assertIn("test_refresh_token", saved_keys)
        self.assertIn("test_expires_at", saved_keys)

        params = _load_persona_params(self.ADDON_NAME, self.PERSONA_ID)
        self.assertEqual(params["test_access_token"], "access_xyz")
        self.assertEqual(params["test_refresh_token"], "refresh_xyz")
        # expires_at should be near time.time() + 3600
        self.assertAlmostEqual(
            params["test_expires_at"], time.time() + 3600, delta=10
        )

    def test_exchange_code_state_is_one_shot(self):
        from saiverse.oauth import handler as oauth_handler
        from saiverse.oauth.handler import OAuthError, exchange_code

        state = self._start_flow_and_get_state()

        mock_resp = self._mock_token_response({
            "access_token": "x", "refresh_token": "y", "expires_in": 60,
        })
        with patch.object(oauth_handler.httpx, "Client") as MockClient:
            instance = MockClient.return_value.__enter__.return_value
            instance.post.return_value = mock_resp
            exchange_code(self.ADDON_NAME, self.FLOW_KEY, "code1", state)

        # Second call with same state must fail
        with self.assertRaises(OAuthError):
            exchange_code(self.ADDON_NAME, self.FLOW_KEY, "code2", state)

    def test_exchange_code_invalid_state_rejected(self):
        from saiverse.oauth.handler import OAuthError, exchange_code
        with self.assertRaises(OAuthError):
            exchange_code(self.ADDON_NAME, self.FLOW_KEY, "code", "bogus_state")

    def test_exchange_code_token_endpoint_failure_raises(self):
        from saiverse.oauth import handler as oauth_handler
        from saiverse.oauth.handler import OAuthError, exchange_code

        state = self._start_flow_and_get_state()

        mock_resp = self._mock_token_response(
            {"error": "invalid_grant"}, status=400,
        )
        with patch.object(oauth_handler.httpx, "Client") as MockClient:
            instance = MockClient.return_value.__enter__.return_value
            instance.post.return_value = mock_resp
            with self.assertRaises(OAuthError):
                exchange_code(self.ADDON_NAME, self.FLOW_KEY, "code", state)

    # ------------------------------------------------------------------
    # get_valid_token (Pull型)
    # ------------------------------------------------------------------

    def test_get_valid_token_returns_existing_when_not_expired(self):
        from saiverse.oauth.handler import (
            _merge_persona_params,
            get_valid_token,
        )

        _merge_persona_params(self.ADDON_NAME, self.PERSONA_ID, {
            "test_access_token": "current_token",
            "test_refresh_token": "current_refresh",
            "test_expires_at": time.time() + 3600,
        })

        token = get_valid_token(self.ADDON_NAME, self.FLOW_KEY, self.PERSONA_ID)
        self.assertEqual(token, "current_token")

    def test_get_valid_token_refreshes_when_expired(self):
        from saiverse.oauth import handler as oauth_handler
        from saiverse.oauth.handler import (
            _load_persona_params,
            _merge_persona_params,
            get_valid_token,
        )

        _merge_persona_params(self.ADDON_NAME, self.PERSONA_ID, {
            "test_access_token": "old_token",
            "test_refresh_token": "old_refresh",
            "test_expires_at": time.time() - 60,  # expired
        })

        new_body = {
            "access_token": "new_token",
            "refresh_token": "new_refresh",
            "expires_in": 3600,
        }
        mock_resp = self._mock_token_response(new_body)
        with patch.object(oauth_handler.httpx, "Client") as MockClient:
            instance = MockClient.return_value.__enter__.return_value
            instance.post.return_value = mock_resp

            token = get_valid_token(
                self.ADDON_NAME, self.FLOW_KEY, self.PERSONA_ID,
            )

        self.assertEqual(token, "new_token")
        params = _load_persona_params(self.ADDON_NAME, self.PERSONA_ID)
        self.assertEqual(params["test_access_token"], "new_token")
        self.assertEqual(params["test_refresh_token"], "new_refresh")

    def test_get_valid_token_returns_none_when_no_token_saved(self):
        from saiverse.oauth.handler import get_valid_token
        token = get_valid_token(self.ADDON_NAME, self.FLOW_KEY, self.PERSONA_ID)
        self.assertIsNone(token)

    def test_get_valid_token_returns_none_when_refresh_fails(self):
        from saiverse.oauth import handler as oauth_handler
        from saiverse.oauth.handler import (
            _merge_persona_params,
            get_valid_token,
        )

        _merge_persona_params(self.ADDON_NAME, self.PERSONA_ID, {
            "test_access_token": "old",
            "test_refresh_token": "bad_refresh",
            "test_expires_at": time.time() - 60,
        })

        mock_resp = self._mock_token_response(
            {"error": "invalid_grant"}, status=400,
        )
        with patch.object(oauth_handler.httpx, "Client") as MockClient:
            instance = MockClient.return_value.__enter__.return_value
            instance.post.return_value = mock_resp

            token = get_valid_token(
                self.ADDON_NAME, self.FLOW_KEY, self.PERSONA_ID,
            )
        self.assertIsNone(token)

    # ------------------------------------------------------------------
    # get_status / disconnect
    # ------------------------------------------------------------------

    def test_get_status_disconnected_initially(self):
        from saiverse.oauth.handler import get_status
        status = get_status(self.ADDON_NAME, self.FLOW_KEY, self.PERSONA_ID)
        self.assertFalse(status["connected"])

    def test_get_status_excludes_token_keys(self):
        from saiverse.oauth.handler import (
            _merge_persona_params,
            get_status,
        )

        _merge_persona_params(self.ADDON_NAME, self.PERSONA_ID, {
            "test_access_token": "secret_xyz",
            "test_refresh_token": "secret_refresh",
            "test_expires_at": time.time() + 3600,
            "username": "alice_handle",
        })

        status = get_status(self.ADDON_NAME, self.FLOW_KEY, self.PERSONA_ID)
        self.assertTrue(status["connected"])
        self.assertNotIn("test_access_token", status["params"])
        self.assertNotIn("test_refresh_token", status["params"])
        self.assertNotIn("test_expires_at", status["params"])
        self.assertEqual(status["params"].get("username"), "alice_handle")

    def test_disconnect_clears_token_keys(self):
        from saiverse.oauth.handler import (
            _load_persona_params,
            _merge_persona_params,
            disconnect,
        )

        _merge_persona_params(self.ADDON_NAME, self.PERSONA_ID, {
            "test_access_token": "x",
            "test_refresh_token": "y",
            "test_expires_at": time.time() + 100,
            "username": "kept",
        })

        disconnect(self.ADDON_NAME, self.FLOW_KEY, self.PERSONA_ID)

        params = _load_persona_params(self.ADDON_NAME, self.PERSONA_ID)
        self.assertNotIn("test_access_token", params)
        self.assertNotIn("test_refresh_token", params)
        self.assertNotIn("test_expires_at", params)
        # Non-token keys preserved (we only delete result_mapping targets)
        self.assertEqual(params.get("username"), "kept")


if __name__ == "__main__":
    unittest.main()
