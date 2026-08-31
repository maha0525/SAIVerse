"""Tests for ${runtime.lan_ip} placeholder and saiverse.lan_ip helper.

The MCP subprocess env is fixed at spawn time, so a user changing
networks would normally require manually re-entering vision_host /
similar IP-dependent settings via the AddonManager UI. The runtime
placeholder lets mcp_servers.json declare ``"${runtime.lan_ip}"``
directly, which is resolved at each (re)connect to the current outward-
facing IP.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class GetLocalIpTests(unittest.TestCase):
    """``saiverse.lan_ip.get_local_ip`` socket probe behavior."""

    def test_returns_ip_from_getsockname(self):
        from saiverse import lan_ip

        fake_sock = MagicMock()
        fake_sock.getsockname.return_value = ("192.168.0.42", 12345)
        fake_sock.__enter__.return_value = fake_sock

        with patch.object(lan_ip.socket, "socket", return_value=fake_sock):
            result = lan_ip.get_local_ip()

        self.assertEqual(result, "192.168.0.42")
        fake_sock.connect.assert_called_once_with(("8.8.8.8", 80))

    def test_returns_none_on_oserror(self):
        from saiverse import lan_ip

        fake_sock = MagicMock()
        fake_sock.__enter__.return_value = fake_sock
        fake_sock.connect.side_effect = OSError("network unreachable")

        with patch.object(lan_ip.socket, "socket", return_value=fake_sock):
            result = lan_ip.get_local_ip()

        self.assertIsNone(result)

    def test_returns_none_for_invalid_ip(self):
        """0.0.0.0 のような無効値は None に正規化する。"""
        from saiverse import lan_ip

        fake_sock = MagicMock()
        fake_sock.getsockname.return_value = ("0.0.0.0", 0)
        fake_sock.__enter__.return_value = fake_sock

        with patch.object(lan_ip.socket, "socket", return_value=fake_sock):
            result = lan_ip.get_local_ip()

        self.assertIsNone(result)


class RuntimePlaceholderTests(unittest.TestCase):
    """``${runtime.lan_ip}`` placeholder resolves via saiverse.lan_ip."""

    def test_runtime_lan_ip_resolved_in_string(self):
        from tools import mcp_config

        with patch("saiverse.lan_ip.get_local_ip", return_value="192.168.1.50"):
            resolved = mcp_config._interpolate_env(
                "http://${runtime.lan_ip}:8766/capture"
            )

        self.assertEqual(resolved, "http://192.168.1.50:8766/capture")

    def test_runtime_lan_ip_unresolved_kept_intact_on_failure(self):
        """検出失敗時は placeholder をそのまま残す (= 上層で WARN ログを見て
        対処できるよう、 silent に空文字に置換しない)。"""
        from tools import mcp_config

        with patch("saiverse.lan_ip.get_local_ip", return_value=None):
            resolved = mcp_config._interpolate_env(
                "http://${runtime.lan_ip}:8766/capture"
            )

        self.assertEqual(resolved, "http://${runtime.lan_ip}:8766/capture")

    def test_unknown_runtime_key_kept_intact(self):
        from tools import mcp_config

        resolved = mcp_config._interpolate_env("${runtime.bogus_key}")
        self.assertEqual(resolved, "${runtime.bogus_key}")

    def test_runtime_placeholder_in_dict_config(self):
        """resolve_config_placeholders が dict 全体に対して再帰的に適用される。"""
        from tools import mcp_config

        with patch("saiverse.lan_ip.get_local_ip", return_value="10.0.0.5"):
            resolved = mcp_config.resolve_config_placeholders({
                "command": "echo",
                "env": {
                    "VISION_HOST": "${runtime.lan_ip}",
                    "STATIC": "unchanged",
                },
            })

        self.assertEqual(resolved["env"]["VISION_HOST"], "10.0.0.5")
        self.assertEqual(resolved["env"]["STATIC"], "unchanged")


if __name__ == "__main__":
    unittest.main(verbosity=2)
