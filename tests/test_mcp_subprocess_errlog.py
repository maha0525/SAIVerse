"""Regression test for per-instance MCP subprocess errlog paths.

Several subprocesses spawned from one server template (e.g. one stackchan
gateway per vessel via ``register_instance``) used to share a single
``mcp_subprocess_<server>.log`` file. On Windows only the first instance's
stderr survived, so later instances' diagnostics ("Gateway started",
vision_url, ESP32 device connection) were invisible — which blocked
root-causing a per-vessel camera failure.

``MCPServerConnection`` now carries its ``instance_key`` and derives a
dedicated errlog file for named-instance / persona scopes, while keeping the
bare per-server path for the global scope (backward compatible).
"""

import contextlib
import logging
import tempfile
import unittest
from pathlib import Path

from tools.mcp_client import MCPServerConnection


class SubprocessErrlogPathTest(unittest.TestCase):
    SERVER = "saiverse-stackchan-addon__stackchan"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        # Insert a FileHandler at the front so _open_subprocess_errlog resolves
        # the log dir to tmp (mirrors the real session-log resolution) instead
        # of writing probe files into a real session directory.
        self._fh = logging.FileHandler(tmp / "backend.log", encoding="utf-8")
        logging.getLogger().handlers.insert(0, self._fh)

    def tearDown(self) -> None:
        logging.getLogger().removeHandler(self._fh)
        self._fh.close()
        self._tmp.cleanup()

    def _errlog_name(self, instance_key) -> str:
        conn = MCPServerConnection(
            self.SERVER, {"command": "x"}, instance_key=instance_key
        )
        with contextlib.ExitStack() as stack:
            handle = conn._open_subprocess_errlog(stack)
            return Path(handle.name).name

    def test_named_instances_get_distinct_files(self) -> None:
        a = self._errlog_name(f"{self.SERVER}:instance:f0d40b0b")
        b = self._errlog_name(f"{self.SERVER}:instance:076797f8")
        self.assertEqual(
            a, f"mcp_subprocess_{self.SERVER}_instance_f0d40b0b.log"
        )
        self.assertEqual(
            b, f"mcp_subprocess_{self.SERVER}_instance_076797f8.log"
        )
        # Concurrent vessels no longer collide into one shared file.
        self.assertNotEqual(a, b)

    def test_global_and_none_keep_legacy_path(self) -> None:
        legacy = f"mcp_subprocess_{self.SERVER}.log"
        self.assertEqual(self._errlog_name(f"{self.SERVER}:global"), legacy)
        self.assertEqual(self._errlog_name(None), legacy)

    def test_persona_scope_gets_own_file(self) -> None:
        self.assertEqual(
            self._errlog_name(f"{self.SERVER}:persona:aifi_city_a"),
            f"mcp_subprocess_{self.SERVER}_persona_aifi_city_a.log",
        )


if __name__ == "__main__":
    unittest.main()
