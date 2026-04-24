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
from tools.mcp_config import (
    _resolve_placeholder,
    load_mcp_configs,
    resolve_config_placeholders,
)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _make_tmp_data_dirs(tmp: Path) -> tuple[Path, Path, Path]:
    user_data_dir = tmp / "user_data"
    expansion_dir = tmp / "expansion_data"
    builtin_dir = tmp / "builtin_data"
    user_data_dir.mkdir(parents=True, exist_ok=True)
    expansion_dir.mkdir(parents=True, exist_ok=True)
    builtin_dir.mkdir(parents=True, exist_ok=True)
    return user_data_dir, expansion_dir, builtin_dir


class MCPConfigTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temp_root = Path.cwd() / "temp"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.tmp = temp_root / f"mcp-config-{uuid.uuid4().hex}"
        self.tmp.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_load_mcp_configs_respects_priority_and_env(self) -> None:
        user_data_dir, expansion_dir, builtin_dir = _make_tmp_data_dirs(self.tmp)

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

        # user_data is privileged (no prefix) and wins over project/expansion/builtin
        self.assertEqual(configs["shared"]["url"], "http://user.invalid/mcp")
        self.assertEqual(configs["project_only"]["env"]["TOKEN"], "token-123")
        # expansion_data servers are auto-prefixed with addon folder name
        self.assertIn("addon_pack__addon_only", configs)
        self.assertIn("addon_pack__shared", configs)
        self.assertEqual(
            configs["addon_pack__shared"]["url"], "http://addon.invalid/mcp"
        )
        self.assertEqual(configs["addon_pack__addon_only"]["_addon_name"], "addon_pack")
        self.assertEqual(
            configs["addon_pack__addon_only"]["_original_server_name"], "addon_only"
        )
        # builtin servers are privileged (no prefix)
        self.assertIn("builtin_only", configs)
        self.assertNotIn("_addon_name", configs["builtin_only"])
        # disabled servers are filtered out
        self.assertNotIn("disabled", configs)

    def test_addon_server_name_auto_prefix_isolation(self) -> None:
        """user_data と expansion で同じ server_name があってもプレフィックスで共存する。"""
        user_data_dir, expansion_dir, builtin_dir = _make_tmp_data_dirs(self.tmp)

        _write_json(
            user_data_dir / "mcp_servers.json",
            {"mcpServers": {"fs": {"url": "http://user.invalid/mcp"}}},
        )
        _write_json(
            expansion_dir / "addon_a" / "mcp_servers.json",
            {"mcpServers": {"fs": {"url": "http://addon.invalid/mcp"}}},
        )

        with mock.patch("saiverse.data_paths.USER_DATA_DIR", user_data_dir):
            with mock.patch("saiverse.data_paths.EXPANSION_DATA_DIR", expansion_dir):
                with mock.patch("saiverse.data_paths.BUILTIN_DATA_DIR", builtin_dir):
                    configs = load_mcp_configs()

        self.assertIn("fs", configs)  # user_data side
        self.assertIn("addon_a__fs", configs)  # expansion side, prefixed
        self.assertEqual(configs["fs"]["url"], "http://user.invalid/mcp")
        self.assertEqual(configs["addon_a__fs"]["url"], "http://addon.invalid/mcp")

    def test_resolve_placeholder_env_explicit_and_legacy(self) -> None:
        with mock.patch.dict(os.environ, {"MCP_TEST_FOO": "bar"}, clear=False):
            self.assertEqual(_resolve_placeholder("env.MCP_TEST_FOO"), "bar")
            self.assertEqual(_resolve_placeholder("MCP_TEST_FOO"), "bar")

    def test_resolve_placeholder_env_unset_returns_none(self) -> None:
        # Ensure the var is not set for a stable result
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MCP_TEST_UNSET", None)
            self.assertIsNone(_resolve_placeholder("env.MCP_TEST_UNSET"))
            self.assertIsNone(_resolve_placeholder("MCP_TEST_UNSET"))

    def test_resolve_placeholder_addon_calls_get_params_global(self) -> None:
        with mock.patch(
            "saiverse.addon_config.get_params",
            return_value={"api_key": "ADDON_KEY_123"},
        ) as get_params_mock:
            result = _resolve_placeholder("addon.my-addon.api_key")
        self.assertEqual(result, "ADDON_KEY_123")
        get_params_mock.assert_called_once_with("my-addon", persona_id=None)

    def test_resolve_placeholder_persona_addon_calls_get_params_with_persona(self) -> None:
        with mock.patch(
            "saiverse.addon_config.get_params",
            return_value={"api_key": "PERSONA_KEY"},
        ) as get_params_mock:
            result = _resolve_placeholder(
                "persona.addon.my-addon.api_key",
                persona_id="air_city_a",
            )
        self.assertEqual(result, "PERSONA_KEY")
        get_params_mock.assert_called_once_with("my-addon", persona_id="air_city_a")

    def test_resolve_placeholder_persona_addon_requires_persona_context(self) -> None:
        # Without persona_id, should not resolve (returns None)
        self.assertIsNone(_resolve_placeholder("persona.addon.my-addon.api_key"))

    def test_resolve_placeholder_missing_addon_key_returns_none(self) -> None:
        with mock.patch(
            "saiverse.addon_config.get_params",
            return_value={"other_key": "x"},
        ):
            self.assertIsNone(_resolve_placeholder("addon.my-addon.api_key"))

    def test_resolve_placeholder_unknown_format_returns_none(self) -> None:
        self.assertIsNone(_resolve_placeholder("foo.bar.baz.quux.extra"))
        self.assertIsNone(_resolve_placeholder("addon.only_two_parts"))  # only 2 parts with addon prefix

    def test_resolve_config_placeholders_public_api(self) -> None:
        raw = {
            "command": "npx",
            "env": {
                "FOO": "${env.MCP_TEST_FOO}",
                "BAR": "${persona.addon.my-addon.api_key}",
                "BAZ": "literal",
            },
            "args": ["--token", "${env.MCP_TEST_FOO}"],
        }
        with mock.patch.dict(os.environ, {"MCP_TEST_FOO": "foo_value"}, clear=False):
            with mock.patch(
                "saiverse.addon_config.get_params",
                return_value={"api_key": "bar_value"},
            ):
                resolved = resolve_config_placeholders(raw, persona_id="air_city_a")

        self.assertEqual(resolved["env"]["FOO"], "foo_value")
        self.assertEqual(resolved["env"]["BAR"], "bar_value")
        self.assertEqual(resolved["env"]["BAZ"], "literal")
        self.assertEqual(resolved["args"][1], "foo_value")

    def test_resolve_config_placeholders_unresolved_keeps_original(self) -> None:
        """未解決プレースホルダーは原形のまま残る（silent に空文字列にしない）。"""
        raw = {"env": {"X": "${env.MCP_DEFINITELY_UNSET_VAR_XYZ}"}}
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MCP_DEFINITELY_UNSET_VAR_XYZ", None)
            resolved = resolve_config_placeholders(raw)
        self.assertEqual(resolved["env"]["X"], "${env.MCP_DEFINITELY_UNSET_VAR_XYZ}")

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
