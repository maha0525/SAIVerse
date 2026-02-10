"""Tests for SearXNG search tool: rate limiting and retry logic."""

from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock, patch

import requests

from tests.conftest import load_builtin_tool


class TestRateLimiter(unittest.TestCase):
    """Test the _RateLimiter class."""

    def _make_limiter(self, max_calls: int, period: float):
        mod = load_builtin_tool("searxng_search")
        return mod._RateLimiter(max_calls, period)

    def test_acquire_within_limit(self):
        limiter = self._make_limiter(3, 10.0)
        self.assertTrue(limiter.acquire(timeout=0))
        self.assertTrue(limiter.acquire(timeout=0))
        self.assertTrue(limiter.acquire(timeout=0))

    def test_acquire_exceeds_limit(self):
        limiter = self._make_limiter(2, 10.0)
        self.assertTrue(limiter.acquire(timeout=0))
        self.assertTrue(limiter.acquire(timeout=0))
        # Third call should fail immediately with timeout=0
        self.assertFalse(limiter.acquire(timeout=0))

    def test_acquire_releases_after_period(self):
        limiter = self._make_limiter(1, 0.2)
        self.assertTrue(limiter.acquire(timeout=0))
        self.assertFalse(limiter.acquire(timeout=0))
        # Wait for the window to pass
        time.sleep(0.25)
        self.assertTrue(limiter.acquire(timeout=0))

    def test_acquire_blocks_until_available(self):
        limiter = self._make_limiter(1, 0.3)
        self.assertTrue(limiter.acquire(timeout=0))
        start = time.monotonic()
        # Should block and then succeed once slot opens
        self.assertTrue(limiter.acquire(timeout=1.0))
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 0.2)


class TestRetryLogic(unittest.TestCase):
    """Test _execute_search retry behavior."""

    def setUp(self):
        self.mod = load_builtin_tool("searxng_search")

    @patch("requests.get")
    def test_success_no_retry(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"results": [{"title": "test", "url": "http://test.com"}]}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = self.mod._execute_search("http://localhost:8080/search", {"q": "test"})
        self.assertEqual(result["results"][0]["title"], "test")
        self.assertEqual(mock_get.call_count, 1)

    @patch("requests.get")
    def test_connection_error_retries_once(self, mock_get):
        mock_get.side_effect = requests.exceptions.ConnectionError("refused")

        with self.assertRaises(self.mod._SearchError) as ctx:
            self.mod._execute_search("http://localhost:8080/search", {"q": "test"})

        self.assertIn("接続できません", str(ctx.exception))
        # Connection error: initial + 1 retry = 2 attempts
        self.assertEqual(mock_get.call_count, 2)

    @patch("requests.get")
    def test_timeout_retries_with_backoff(self, mock_get):
        mock_get.side_effect = requests.exceptions.Timeout("timed out")

        with self.assertRaises(self.mod._SearchError) as ctx:
            self.mod._execute_search("http://localhost:8080/search", {"q": "test"})

        self.assertIn("タイムアウト", str(ctx.exception))
        # Timeout: initial + 2 retries = 3 attempts
        self.assertEqual(mock_get.call_count, 3)

    @patch("requests.get")
    def test_http_429_retries(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.raise_for_status.side_effect = requests.exceptions.HTTPError(response=mock_resp)
        mock_get.return_value = mock_resp

        with self.assertRaises(self.mod._SearchError) as ctx:
            self.mod._execute_search("http://localhost:8080/search", {"q": "test"})

        self.assertIn("レート制限", str(ctx.exception))
        self.assertEqual(mock_get.call_count, 3)

    @patch("requests.get")
    def test_http_400_no_retry(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.raise_for_status.side_effect = requests.exceptions.HTTPError(response=mock_resp)
        mock_get.return_value = mock_resp

        with self.assertRaises(self.mod._SearchError):
            self.mod._execute_search("http://localhost:8080/search", {"q": "test"})

        # 400 is not retryable, should fail immediately
        self.assertEqual(mock_get.call_count, 1)

    @patch("requests.get")
    def test_json_decode_error_no_retry(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.side_effect = ValueError("bad json")
        mock_get.return_value = mock_resp

        with self.assertRaises(self.mod._SearchError) as ctx:
            self.mod._execute_search("http://localhost:8080/search", {"q": "test"})

        self.assertIn("解釈できませんでした", str(ctx.exception))
        self.assertEqual(mock_get.call_count, 1)

    @patch("requests.get")
    def test_recovery_after_transient_failure(self, mock_get):
        """First call times out, second succeeds."""
        good_resp = MagicMock()
        good_resp.status_code = 200
        good_resp.json.return_value = {"results": [{"title": "ok"}]}
        good_resp.raise_for_status = MagicMock()

        mock_get.side_effect = [
            requests.exceptions.Timeout("timeout"),
            good_resp,
        ]

        result = self.mod._execute_search("http://localhost:8080/search", {"q": "test"})
        self.assertEqual(result["results"][0]["title"], "ok")
        self.assertEqual(mock_get.call_count, 2)


class TestSearxngSearchIntegration(unittest.TestCase):
    """Test the public searxng_search function with rate limiting."""

    def setUp(self):
        self.mod = load_builtin_tool("searxng_search")

    @patch("requests.get")
    def test_rate_limit_returns_error_message(self, mock_get):
        """When rate limit is exhausted, return error message without calling HTTP."""
        # Create a limiter with a very long period so the slot won't free up
        limiter = self.mod._RateLimiter(1, 3600.0)
        limiter.acquire(timeout=0)  # exhaust the single slot

        # Patch both the limiter and the timeout so the test doesn't block
        with patch.object(self.mod, "_rate_limiter", limiter), \
             patch.object(self.mod, "_RATE_LIMIT_PERIOD", 0.1):
            msg, result = self.mod.searxng_search("test query")

        self.assertIn("レート制限", msg)
        self.assertIsNone(result.history_snippet)
        mock_get.assert_not_called()

    @patch("requests.get")
    def test_connection_error_returns_message(self, mock_get):
        """When SearXNG is down, return clear error message."""
        mock_get.side_effect = requests.exceptions.ConnectionError("refused")

        # Use a fresh limiter for the test
        limiter = self.mod._RateLimiter(10, 60.0)
        with patch.object(self.mod, "_rate_limiter", limiter):
            msg, result = self.mod.searxng_search("test query")

        self.assertIn("接続できません", msg)
        self.assertIsNone(result.history_snippet)


if __name__ == "__main__":
    unittest.main()
