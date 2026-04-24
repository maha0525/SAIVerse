import json
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock
import uuid

from tools import SPELL_TOOL_NAMES, SPELL_TOOL_SCHEMAS, register_external_tool, unregister_external_tool
from tools.core import ToolSchema
from tools.mcp_client import _normalize_spell_config
from tools.mcp_config import load_mcp_configs


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class MCPConfigTestCase(unittest.TestCase):
    def test_load_mcp_configs_respects_priority_and_env(self) -> None:
        temp_root = Path.cwd() / "temp"
        temp_root.mkdir(parents=True, exist_ok=True)
        tmp = temp_root / f"mcp-config-{uuid.uuid4().hex}"
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            base = tmp
            user_data_dir = base / "user_data"
            expansion_dir = base / "expansion_data"
            builtin_dir = base / "builtin_data"

            _write_json(
                builtin_dir / "mcp_servers.json",
                {
                    "mcpServers": {
                        "shared": {"url": "http://builtin.invalid/mcp"},
                        "builtin_only": {"url": "http://builtin-only.invalid/mcp"},
                    }
                },
            )
            _write_json(
                expansion_dir / "addon_pack" / "mcp_servers.json",
                {
                    "mcpServers": {
                        "shared": {"url": "http://addon.invalid/mcp"},
                        "addon_only": {"url": "http://addon-only.invalid/mcp"},
                    }
                },
            )
            _write_json(
                user_data_dir / "project_alpha" / "mcp_servers.json",
                {
                    "mcpServers": {
                        "shared": {"url": "http://project.invalid/mcp"},
                        "project_only": {"env": {"TOKEN": "${MCP_TEST_TOKEN}"}},
                    }
                },
            )
            _write_json(
                user_data_dir / "mcp_servers.json",
                {
                    "mcpServers": {
                        "shared": {"url": "http://user.invalid/mcp"},
                        "disabled": {"url": "http://disabled.invalid/mcp", "enabled": False},
                    }
                },
            )

            with mock.patch.dict(os.environ, {"MCP_TEST_TOKEN": "token-123"}, clear=False):
                with mock.patch("saiverse.data_paths.USER_DATA_DIR", user_data_dir):
                    with mock.patch("saiverse.data_paths.EXPANSION_DATA_DIR", expansion_dir):
                        with mock.patch("saiverse.data_paths.BUILTIN_DATA_DIR", builtin_dir):
                            configs = load_mcp_configs()

            self.assertEqual(configs["shared"]["url"], "http://user.invalid/mcp")
            self.assertEqual(configs["project_only"]["env"]["TOKEN"], "token-123")
            self.assertIn("addon_only", configs)
            self.assertIn("builtin_only", configs)
            self.assertNotIn("disabled", configs)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_normalize_spell_config_supports_multiple_shapes(self) -> None:
        self.assertEqual(
            _normalize_spell_config(["read_file", {"name": "write_file", "display_name": "書き込み"}]),
            {
                "read_file": {},
                "write_file": {"display_name": "書き込み"},
            },
        )
        self.assertEqual(
            _normalize_spell_config({"read_file": True, "write_file": "保存"}),
            {
                "read_file": {},
                "write_file": {"display_name": "保存"},
            },
        )

    def test_register_external_tool_updates_spell_registry(self) -> None:
        tool_name = "test_mcp_external_spell_tool"
        schema = ToolSchema(
            name=tool_name,
            description="test",
            parameters={"type": "object", "properties": {}},
            result_type="string",
            spell=True,
            spell_display_name="テスト呪文",
        )

        def _tool():
            return "ok"

        try:
            self.assertTrue(register_external_tool(tool_name, schema, _tool))
            self.assertIn(tool_name, SPELL_TOOL_NAMES)
            self.assertEqual(SPELL_TOOL_SCHEMAS[tool_name].spell_display_name, "テスト呪文")
        finally:
            unregister_external_tool(tool_name)

        self.assertNotIn(tool_name, SPELL_TOOL_NAMES)
        self.assertNotIn(tool_name, SPELL_TOOL_SCHEMAS)


if __name__ == "__main__":
    unittest.main()
