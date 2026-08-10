import json
import os
import shutil
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import uuid

from tools import SPELL_TOOL_NAMES, SPELL_TOOL_SCHEMAS, register_external_tool, unregister_external_tool
from tools.core import ToolSchema
from tools.mcp_client import (
    ERROR_CATEGORY_MISSING_CONFIG,
    MCPClientManager,
    MCPServerConnection,
    _make_instance_key,
    _mcp_http_client_no_redirect,
    _normalize_spell_config,
    _normalize_spell_default,
    _tool_schema_from_mcp,
)
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

    # -- Named instances (設計 G) ---------------------------------------

    def test_resolve_placeholder_instance_context(self) -> None:
        ctx = {"ws_port": "8765", "master_token": "tok-abc"}
        self.assertEqual(
            _resolve_placeholder("instance.ws_port", instance_context=ctx), "8765"
        )
        self.assertEqual(
            _resolve_placeholder("instance.master_token", instance_context=ctx),
            "tok-abc",
        )

    def test_resolve_placeholder_instance_requires_context(self) -> None:
        # Without instance context, an ${instance.*} placeholder stays unresolved.
        self.assertIsNone(_resolve_placeholder("instance.ws_port"))

    def test_missing_instance_context_does_not_warn(self) -> None:
        """インスタンス外の interpolate で WARNING を出さない (DEBUG のみ)。

        名前付きインスタンス構成のテンプレートは非インスタンス文脈でも
        interpolate されるのが正常で、WARNING だと呼ばれるたびに error.log を
        洪水させる (2026-07-19 実測 11,181 行)。
        """
        import logging

        logger = logging.getLogger("tools.mcp_config")
        records = []

        class _Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = _Capture(level=logging.DEBUG)
        logger.addHandler(handler)
        old_level = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            self.assertIsNone(_resolve_placeholder("instance.ws_port"))
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)
        warn_or_above = [r for r in records if r.levelno >= logging.WARNING]
        self.assertEqual(warn_or_above, [])

    def test_resolve_placeholder_instance_missing_key_returns_none(self) -> None:
        self.assertIsNone(
            _resolve_placeholder("instance.nope", instance_context={"ws_port": "1"})
        )

    def test_resolve_config_placeholders_instance_context(self) -> None:
        raw = {
            "env": {
                "STACKCHAN_TOKEN": "${instance.master_token}",
                "WS_PORT": "${instance.ws_port}",
                "VISION_HOST": "${runtime.lan_ip}",
            },
        }
        with mock.patch(
            "tools.mcp_config._resolve_runtime", return_value="192.168.0.10"
        ):
            resolved = resolve_config_placeholders(
                raw,
                instance_context={"master_token": "tok-xyz", "ws_port": "8775"},
            )
        self.assertEqual(resolved["env"]["STACKCHAN_TOKEN"], "tok-xyz")
        self.assertEqual(resolved["env"]["WS_PORT"], "8775")
        self.assertEqual(resolved["env"]["VISION_HOST"], "192.168.0.10")

    def test_make_instance_key_scopes(self) -> None:
        # backward compat
        self.assertEqual(_make_instance_key("s"), "s:global")
        self.assertEqual(_make_instance_key("s", persona_id="p"), "s:persona:p")
        # named instance
        self.assertEqual(
            _make_instance_key("addon__stackchan", instance_id="vessel1"),
            "addon__stackchan:instance:vessel1",
        )
        # instance_id takes precedence over persona_id
        self.assertEqual(
            _make_instance_key("s", persona_id="p", instance_id="v"), "s:instance:v"
        )

    def test_qualified_from_instance_key_named_instance(self) -> None:
        q = MCPClientManager._qualified_from_instance_key
        self.assertEqual(
            q("addon__stackchan:instance:vessel1"), "addon__stackchan"
        )
        self.assertEqual(q("foo:global"), "foo")
        self.assertEqual(q("foo:persona:air_city_a"), "foo")
        self.assertIsNone(q("nocolon"))

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

    # -- spell_tools_default (サーバー側のツール追加への追随) -------------

    @staticmethod
    def _fake_tool(name: str) -> SimpleNamespace:
        return SimpleNamespace(
            name=name,
            description="desc",
            inputSchema={"type": "object", "properties": {}},
        )

    def test_normalize_spell_default_shapes(self) -> None:
        self.assertIsNone(_normalize_spell_default(None, "srv"))
        self.assertEqual(
            _normalize_spell_default(True, "srv"),
            {"spell": True, "visible": False},
        )
        # キーを書く目的が「自動で使えるようにする」ことなので spell 既定は True。
        # visible 既定は False — サービスがツールを増やすたびに head が太らないように。
        self.assertEqual(
            _normalize_spell_default({}, "srv"),
            {"spell": True, "visible": False},
        )
        self.assertEqual(
            _normalize_spell_default({"spell": False}, "srv"),
            {"spell": False, "visible": False},
        )
        self.assertEqual(
            _normalize_spell_default({"spell": True, "visible": True}, "srv"),
            {"spell": True, "visible": True},
        )

    def test_normalize_spell_default_rejects_bad_type(self) -> None:
        self.assertIsNone(_normalize_spell_default("yes", "srv"))
        self.assertIsNone(_normalize_spell_default(["read_file"], "srv"))

    def test_spell_default_rejects_non_boolean_values(self) -> None:
        """誤記が権限を広げないこと。

        素の ``bool()`` は文字列 "false" を True と読むので、閉じたつもりの
        ``{"spell": "false"}`` がツールを開いてしまう。boolean 以外は閉じる側
        (False) に倒す。
        """
        self.assertEqual(
            _normalize_spell_default({"spell": "false"}, "srv"),
            {"spell": False, "visible": False},
        )
        self.assertEqual(
            _normalize_spell_default({"spell": 1, "visible": "yes"}, "srv"),
            {"spell": False, "visible": False},
        )
        # 省略は「宣言の目的どおり」の既定に落ちる (誤記とは区別する)
        self.assertEqual(
            _normalize_spell_default({"visible": True}, "srv"),
            {"spell": True, "visible": True},
        )

    def test_undeclared_tool_is_not_a_spell_without_default(self) -> None:
        """回帰防止: spell_tools_default が無ければ従来どおり spell=False。

        saiverse-stackchan-addon は spell_tools を「生 MCP ツールを隠す」ために
        使っており、既定が反転すると gateway_config_set / i2c_write のような
        管理者向けツールがペルソナへ開く。
        """
        schema = _tool_schema_from_mcp(
            "srv__new_tool",
            "srv",
            "new_tool",
            self._fake_tool("new_tool"),
            {"other_tool": {}},
            None,
        )
        self.assertFalse(schema.spell)

    def test_undeclared_tool_becomes_hidden_spell_with_default(self) -> None:
        schema = _tool_schema_from_mcp(
            "srv__new_tool",
            "srv",
            "new_tool",
            self._fake_tool("new_tool"),
            {"other_tool": {}},
            {"spell": True, "visible": False},
        )
        self.assertTrue(schema.spell)
        self.assertFalse(schema.spell_visible)

    def test_declared_tool_keeps_its_own_settings_under_default(self) -> None:
        """spell_tools に書いたエントリは従来の意味を保つ (visible 既定 True)。"""
        schema = _tool_schema_from_mcp(
            "srv__listed",
            "srv",
            "listed",
            self._fake_tool("listed"),
            {"listed": {"display_name": "一覧のツール"}},
            {"spell": True, "visible": False},
        )
        self.assertTrue(schema.spell)
        self.assertTrue(schema.spell_visible)
        self.assertEqual(schema.spell_display_name, "一覧のツール")

    def test_declared_entry_visible_rejects_non_boolean(self) -> None:
        """spell_tools 側の visible も同じ族 — "false" を True と読ませない。"""
        schema = _tool_schema_from_mcp(
            "srv__t",
            "srv",
            "t",
            self._fake_tool("t"),
            {"t": {"visible": "false"}},
            None,
        )
        self.assertTrue(schema.spell)
        self.assertFalse(schema.spell_visible)

    def test_default_can_disable_spell_for_undeclared_tools(self) -> None:
        schema = _tool_schema_from_mcp(
            "srv__new_tool",
            "srv",
            "new_tool",
            self._fake_tool("new_tool"),
            {},
            {"spell": False, "visible": False},
        )
        self.assertFalse(schema.spell)

    # -- remote transport の認証ヘッダー ---------------------------------

    def test_http_headers_absent_returns_none(self) -> None:
        conn = MCPServerConnection("srv", {"url": "https://example.test/mcp"})
        self.assertIsNone(conn._http_headers())

    def test_http_headers_returns_declared_headers(self) -> None:
        conn = MCPServerConnection(
            "srv",
            {
                "url": "https://example.test/mcp",
                "headers": {"Authorization": "Bearer key-123"},
            },
        )
        self.assertEqual(conn._http_headers(), {"Authorization": "Bearer key-123"})

    def test_http_headers_empty_dict_returns_none(self) -> None:
        conn = MCPServerConnection("srv", {"headers": {}})
        self.assertIsNone(conn._http_headers())

    def test_http_headers_rejects_non_mapping(self) -> None:
        conn = MCPServerConnection("srv", {"headers": "Bearer nope"})
        self.assertIsNone(conn._http_headers())

    def test_remote_kwargs_pin_no_redirect_only_when_headers_are_sent(self) -> None:
        """認証情報を載せるときだけ redirect を止める。

        httpx が cross-origin redirect で落とすのは Authorization だけなので、
        X-API-Key 等は転送先ホストへそのまま送られる。ヘッダーが無ければ漏れる
        秘密がないため、SDK 既定の factory を保つ (既存挙動を変えない)。
        """
        with_headers = MCPServerConnection(
            "srv", {"url": "https://x.test", "headers": {"X-API-Key": "k"}}
        )._remote_connect_kwargs()
        self.assertIs(
            with_headers["httpx_client_factory"], _mcp_http_client_no_redirect
        )

        without = MCPServerConnection(
            "srv", {"url": "https://x.test"}
        )._remote_connect_kwargs()
        self.assertNotIn("httpx_client_factory", without)
        self.assertIsNone(without["headers"])

    def test_no_redirect_client_refuses_redirects(self) -> None:
        client = _mcp_http_client_no_redirect(headers={"X-API-Key": "k"})
        self.assertFalse(client.follow_redirects)

    # -- per_persona ツール一覧のペルソナ単位取得 (§I) --------------------
    #
    # 起動時 discovery (代表者 1 人の鍵で一括登録) は 2026-08-10 に廃止された。
    # 一覧は各ペルソナ自身の接続から Pulse 頭 (connect=True) / Beat 頭
    # (connect=False) で取得する。docs/intent/mcp_addon_integration.md §I。

    @staticmethod
    def _per_persona_meta(**overrides):
        meta = {
            "scope": "per_persona",
            "addon_name": "my-addon",
            "raw_config": {
                "url": "https://x.test",
                "headers": {
                    "Authorization": "Bearer ${persona.addon.my-addon.api_key}"
                },
            },
        }
        meta.update(overrides)
        return meta

    class _FakeLiveConnection:
        """生きている per_persona 接続の代役。tools は差し替え可能。"""

        def __init__(self, tools=None, refresh_error=None):
            from types import SimpleNamespace

            self.config = {"url": "https://x.test"}
            self.session = object()
            self.connect_count = 0
            self._closed = False
            self._connected = True
            self.tools = [
                SimpleNamespace(name=n, description="d", inputSchema={})
                for n in (tools or [])
            ]
            self.refresh_count = 0
            self._refresh_error = refresh_error
            self._next_tools = None

        @property
        def connected(self):
            return self._connected and self.session is not None

        def set_next_tools(self, names):
            from types import SimpleNamespace

            self._next_tools = [
                SimpleNamespace(name=n, description="d", inputSchema={})
                for n in names
            ]

        async def _discover_tools(self):
            self.refresh_count += 1
            if self._refresh_error is not None:
                raise self._refresh_error
            if self._next_tools is not None:
                self.tools = self._next_tools

        async def connect(self):
            self.connect_count += 1
            self._connected = True

        async def disconnect(self):
            self._connected = False

    def test_refresh_skips_connect_when_key_unset(self) -> None:
        """鍵未設定のペルソナでは接続を張らず、失敗としても記録しない。

        未設定は普通の状態であって故障ではない (§I 要請 3)。placeholder の
        literal が外部へ出る経路は「本人の config が解決できたときしか繋がない」
        ことで構造的に存在しない。
        """
        import asyncio

        mgr = MCPClientManager()
        mgr._server_meta["srv"] = self._per_persona_meta()
        with mock.patch(
            "saiverse.addon_config.get_params", return_value={}
        ), mock.patch.object(
            MCPClientManager, "_start_instance", new_callable=mock.AsyncMock
        ) as mock_start:
            asyncio.run(mgr.refresh_persona_tools("air_city_a"))

        mock_start.assert_not_called()
        self.assertEqual(mgr._failed_instances, {})
        # 「評価した結果、使えない」は空集合として記録される (未取得 None とは
        # 区別する — None は config 近似へフォールバックしてしまう)。
        self.assertEqual(
            mgr._persona_tool_names[("srv", "air_city_a")], frozenset()
        )

    def test_refresh_connects_and_grants_membership(self) -> None:
        """鍵が解決できるペルソナは Pulse 頭で接続し、本人の一覧が所属になる。"""
        import asyncio

        mgr = MCPClientManager()
        mgr._server_meta["srv"] = self._per_persona_meta()
        fake = self._FakeLiveConnection(tools=["post", "read"])

        async def _fake_start(self_, instance_key, qualified_name, persona_id=None, **kw):
            mgr._connections[instance_key] = fake

        registered: list[str] = []
        with mock.patch(
            "saiverse.addon_config.get_params",
            return_value={"api_key": "k"},
        ), mock.patch.object(
            MCPClientManager, "_start_instance", new=_fake_start
        ), mock.patch(
            "tools.register_external_tool",
            side_effect=lambda name, schema, fn: registered.append(name) or True,
        ):
            changed = asyncio.run(mgr.refresh_persona_tools("air_city_a"))

        self.assertTrue(changed)
        self.assertEqual(
            mgr._persona_tool_names[("srv", "air_city_a")],
            frozenset({"post", "read"}),
        )
        self.assertIn("srv__post", registered)
        # lazy-start (wrapper 経路) と同じ自己参照が付く
        self.assertIn("persona:air_city_a", mgr._refs.get("srv:persona:air_city_a", set()))

    def test_beat_refresh_does_not_create_connections(self) -> None:
        """Beat 頭 (connect=False) は生きた接続の上でしか聞き直さない。

        接続を張るのは Pulse 頭の仕事。ここで張り直すと未設定ペルソナの
        resolve (DB 引き) が Beat 頻度で走る。
        """
        import asyncio

        mgr = MCPClientManager()
        mgr._server_meta["srv"] = self._per_persona_meta()
        with mock.patch(
            "saiverse.addon_config.get_params"
        ) as mock_params, mock.patch.object(
            MCPClientManager, "_start_instance", new_callable=mock.AsyncMock
        ) as mock_start:
            changed = asyncio.run(
                mgr.refresh_persona_tools("air_city_a", connect=False)
            )

        self.assertFalse(changed)
        mock_start.assert_not_called()
        mock_params.assert_not_called()

    def test_beat_refresh_detects_tool_list_change(self) -> None:
        """ツール呼び出しで一覧が変わるサーバー (モードチェンジ型) の変動を
        同じ Pulse 内の次の Beat で拾える。"""
        import asyncio

        mgr = MCPClientManager()
        mgr._server_meta["srv"] = self._per_persona_meta()
        fake = self._FakeLiveConnection(tools=["post"])
        mgr._connections["srv:persona:air_city_a"] = fake

        with mock.patch("tools.register_external_tool", return_value=True):
            first = asyncio.run(
                mgr.refresh_persona_tools("air_city_a", connect=False)
            )
            fake.set_next_tools(["post", "enter_field"])
            second = asyncio.run(
                mgr.refresh_persona_tools("air_city_a", connect=False)
            )

        self.assertTrue(first)   # 初取得 (近似からの置き換わり) も変化
        self.assertTrue(second)  # モードチェンジの検出
        self.assertEqual(
            mgr._persona_tool_names[("srv", "air_city_a")],
            frozenset({"post", "enter_field"}),
        )

    def test_refresh_failure_keeps_previous_list(self) -> None:
        """生きた接続への聞き直し失敗は「一覧が変わった」証拠ではない (§I)。

        所属を保ち、変化なしとして返す — 取得失敗を『使えなくなりました』へ
        変換してはいけない。
        """
        import asyncio

        mgr = MCPClientManager()
        mgr._server_meta["srv"] = self._per_persona_meta()
        fake = self._FakeLiveConnection(
            tools=["post"], refresh_error=RuntimeError("transient network error")
        )
        mgr._connections["srv:persona:air_city_a"] = fake
        mgr._persona_tool_names[("srv", "air_city_a")] = frozenset({"post"})

        changed = asyncio.run(
            mgr.refresh_persona_tools("air_city_a", connect=False)
        )

        self.assertFalse(changed)
        self.assertEqual(
            mgr._persona_tool_names[("srv", "air_city_a")], frozenset({"post"})
        )

    def test_refresh_drops_membership_when_key_removed(self) -> None:
        """鍵が消えた (解決できなくなった) ペルソナの所属は空になり、変化として返る。"""
        import asyncio

        mgr = MCPClientManager()
        mgr._server_meta["srv"] = self._per_persona_meta()
        mgr._persona_tool_names[("srv", "air_city_a")] = frozenset({"post"})

        with mock.patch(
            "saiverse.addon_config.get_params", return_value={}
        ):
            changed = asyncio.run(mgr.refresh_persona_tools("air_city_a"))

        self.assertTrue(changed)
        self.assertEqual(
            mgr._persona_tool_names[("srv", "air_city_a")], frozenset()
        )

    def test_connect_failure_is_fail_closed(self) -> None:
        """接続失敗したペルソナのツールは「無い」— config 近似で復活しない (§I)。

        所属を pop で消すと近似 (鍵が解決できる = True) へフォールバックして
        fail-open に反転する (Qwen レビュー 2026-08-10)。空集合マークで
        「評価済み・利用不可」を保持する。
        """
        import asyncio

        mgr = MCPClientManager()
        mgr._server_meta["srv"] = self._per_persona_meta()
        mgr._registered_tools["srv__post"] = {
            "qualified_server_name": "srv",
            "tool_name": "post",
            "scope": "per_persona",
            "building_ids": None,
        }

        async def _fail_start(self_, instance_key, qualified_name, persona_id=None, **kw):
            raise RuntimeError("connection refused")

        with mock.patch(
            "saiverse.addon_config.get_params",
            return_value={"api_key": "k"},
        ), mock.patch.object(
            MCPClientManager, "_start_instance", new=_fail_start
        ):
            changed = asyncio.run(mgr.refresh_persona_tools("air_city_a"))
            self.assertTrue(changed)
            # 鍵は解決できるが、接続に失敗した Pulse ではツール無し
            self.assertFalse(
                mgr.is_tool_available_for_persona("srv__post", "air_city_a")
            )

    def test_refresh_skips_while_previous_refresh_in_flight(self) -> None:
        """走行中の取得があるうちは、同じ (サーバー, ペルソナ) の取得を重ねない。

        sync 側が timeout で見放した取得は MCP ループ上で走り続ける。次の
        Pulse の取得が接続開始を重ねると、同じ instance_key に接続が二重生成
        されて片方が孤児になる。
        """
        import asyncio

        mgr = MCPClientManager()
        mgr._server_meta["srv"] = self._per_persona_meta()
        mgr._persona_refresh_inflight.add(("srv", "air_city_a"))

        with mock.patch(
            "saiverse.addon_config.get_params"
        ) as mock_params, mock.patch.object(
            MCPClientManager, "_start_instance", new_callable=mock.AsyncMock
        ) as mock_start:
            changed = asyncio.run(mgr.refresh_persona_tools("air_city_a"))

        self.assertFalse(changed)
        mock_start.assert_not_called()
        mock_params.assert_not_called()
        # ガードは残っている (走行中の取得が finally で自分の分を外す)
        self.assertIn(("srv", "air_city_a"), mgr._persona_refresh_inflight)

    def test_shutdown_instance_drops_membership(self) -> None:
        """接続を殺す関所 (_shutdown_instance) が派生状態の所属を道連れにする。

        残すと「切れた接続の一覧」が真実の顔で is_tool_available_for_persona
        から出続ける (ローカルレビュー 2026-08-10 指摘)。
        """
        import asyncio

        mgr = MCPClientManager()
        mgr._server_meta["srv"] = self._per_persona_meta()
        fake = self._FakeLiveConnection(tools=["post"])

        async def _noop_disconnect():
            pass

        fake.disconnect = _noop_disconnect
        mgr._connections["srv:persona:air_city_a"] = fake
        mgr._persona_tool_names[("srv", "air_city_a")] = frozenset({"post"})

        asyncio.run(mgr._shutdown_instance("srv:persona:air_city_a", force=True))

        self.assertEqual(
            mgr._persona_tool_names[("srv", "air_city_a")], frozenset()
        )

    # -- 未解決 placeholder の関所 (Codex レビュー 2026-08-10) -------------
    #
    # 起動側の入口で数え上げる形は、入口を一つ見落とした時点で漏れる。実際に
    # reconnect_server が素通しで、鍵を消した後の再接続で ${...} の literal が
    # remote のヘッダーに載った。検査は値が外へ出る場所 = connect() に置く。

    def test_connect_refuses_unresolved_placeholders(self) -> None:
        """未解決の ${...} を持つ config では接続そのものを拒む。"""
        import asyncio

        from tools.mcp_client import MCPUnresolvedConfigError, _classify_error

        conn = MCPServerConnection(
            "srv",
            {
                "url": "https://x.test",
                "headers": {
                    "Authorization": "Bearer ${persona.addon.my-addon.api_key}"
                },
            },
        )
        with self.assertRaises(MCPUnresolvedConfigError) as caught:
            asyncio.run(conn.connect())

        self.assertIn("persona.addon.my-addon.api_key", str(caught.exception))
        self.assertFalse(conn.connected)
        # 呼び出し元が「設定不足」として扱えること (UI の失敗一覧の分類)
        self.assertEqual(
            _classify_error(caught.exception), ERROR_CATEGORY_MISSING_CONFIG
        )

    def test_start_instance_reports_missing_config_from_the_guard(self) -> None:
        """入口の事前検査を関所へ寄せても、失敗記録の分類は missing_config のまま。"""
        import asyncio

        mgr = MCPClientManager()
        mgr._server_meta["srv"] = self._per_persona_meta()

        with mock.patch("saiverse.addon_config.get_params", return_value={}):
            with self.assertRaises(Exception):
                asyncio.run(
                    mgr._start_instance(
                        "srv:persona:air_city_a", "srv", persona_id="air_city_a"
                    )
                )

        entry = mgr._failed_instances["srv:persona:air_city_a"]
        self.assertEqual(entry["last_category"], ERROR_CATEGORY_MISSING_CONFIG)
        self.assertIn("persona.addon.my-addon.api_key", entry["last_message"])

    def test_reconnect_shuts_down_instance_when_key_removed(self) -> None:
        """鍵を消した後の再接続は、未解決の値で繋がず instance を畳む。

        旧挙動は再解決した config を検査なしで connect() に渡していたため、
        ``${persona.addon...}`` の literal が remote のヘッダーに載って外部へ
        出た。かつ「起動時に焼き込まれた旧鍵で喋り続ける接続」を残すのも、
        鍵を消した利用者の意図に反する。
        """
        import asyncio

        mgr = MCPClientManager()
        mgr._server_meta["srv"] = self._per_persona_meta()
        fake = self._FakeLiveConnection(tools=["post"])
        mgr._connections["srv:persona:air_city_a"] = fake
        mgr._persona_tool_names[("srv", "air_city_a")] = frozenset({"post"})

        with mock.patch("saiverse.addon_config.get_params", return_value={}):
            ok = asyncio.run(mgr.reconnect_server("srv"))

        self.assertFalse(ok)
        self.assertEqual(fake.connect_count, 0)
        self.assertNotIn("srv:persona:air_city_a", mgr._connections)
        self.assertEqual(
            mgr._persona_tool_names[("srv", "air_city_a")], frozenset()
        )
        self.assertEqual(
            mgr._failed_instances["srv:persona:air_city_a"]["last_category"],
            ERROR_CATEGORY_MISSING_CONFIG,
        )

    def test_beat_refresh_drops_membership_when_connection_is_dead(self) -> None:
        """死んだ接続が残っている Beat 頭では、所属を「利用不可」に倒す。

        ツールコールの失敗で接続は disconnect されるが _connections には残る
        (call_tool の except 分岐)。証言者がいないのに一覧だけ残すと、死んだ
        接続のツールが真実の顔で並び続ける (Codex レビュー 2026-08-10)。
        """
        import asyncio

        mgr = MCPClientManager()
        mgr._server_meta["srv"] = self._per_persona_meta()
        fake = self._FakeLiveConnection(tools=["post"])
        fake._connected = False  # ツールコール失敗で死んだ接続
        mgr._connections["srv:persona:air_city_a"] = fake
        mgr._persona_tool_names[("srv", "air_city_a")] = frozenset({"post"})

        with mock.patch("saiverse.addon_config.get_params") as mock_params:
            changed = asyncio.run(
                mgr.refresh_persona_tools("air_city_a", connect=False)
            )

        self.assertTrue(changed)
        self.assertEqual(
            mgr._persona_tool_names[("srv", "air_city_a")], frozenset()
        )
        # Beat 頭は接続を張り直さない (鍵の再解決もしない) — Pulse 頭の仕事
        mock_params.assert_not_called()
        self.assertEqual(fake.connect_count, 0)

    def test_late_refresh_result_is_discarded_after_invalidation(self) -> None:
        """走行中に無効化された取得は、一覧を書き戻さない (fail-open 反転の防止)。

        sync 側が timeout で見放した取得は MCP ループ上で走り続ける。その間に
        停止や鍵の消滅が入ったのに完了時の一覧を書き込むと、「止めたのにツールが
        復活する」。所属の版番号で、無効化を跨いだ書き込みを捨てる。
        """
        import asyncio

        mgr = MCPClientManager()
        mgr._server_meta["srv"] = self._per_persona_meta()
        fake = self._FakeLiveConnection(tools=["post", "read"])

        async def _start_then_invalidate(
            self_, instance_key, qualified_name, persona_id=None, **kw
        ):
            mgr._connections[instance_key] = fake
            # 接続を張っている間に「停止」が入った状況の再現
            mgr._mark_persona_tools_unavailable(("srv", "air_city_a"))

        with mock.patch(
            "saiverse.addon_config.get_params", return_value={"api_key": "k"},
        ), mock.patch.object(
            MCPClientManager, "_start_instance", new=_start_then_invalidate
        ), mock.patch("tools.register_external_tool", return_value=True):
            changed = asyncio.run(mgr.refresh_persona_tools("air_city_a"))

        self.assertFalse(changed)
        self.assertEqual(
            mgr._persona_tool_names[("srv", "air_city_a")], frozenset()
        )

    def test_presume_unavailable_covers_unevaluated_only(self) -> None:
        """Pulse 頭の取得を投げる前に、未評価の所属だけを空集合へ倒す。

        timeout で結果を受け取れなかったときに config 近似 (鍵が解決できれば
        使える顔) へ戻らないための fail-closed。既に実績のある所属は触らない。
        """
        mgr = MCPClientManager()
        mgr._server_meta["srv"] = self._per_persona_meta()
        mgr._server_meta["glob"] = {
            "scope": "global", "addon_name": None, "raw_config": {},
        }
        mgr._server_meta["srv2"] = self._per_persona_meta()
        mgr._persona_tool_names[("srv2", "air_city_a")] = frozenset({"post"})

        mgr.presume_persona_tools_unavailable("air_city_a")

        self.assertEqual(
            mgr._persona_tool_names[("srv", "air_city_a")], frozenset()
        )
        # 実績のある所属は保つ (推定で本物の一覧を消さない)
        self.assertEqual(
            mgr._persona_tool_names[("srv2", "air_city_a")], frozenset({"post"})
        )
        # global スコープは対象外 (ペルソナ単位の所属を持たない)
        self.assertNotIn(("glob", "air_city_a"), mgr._persona_tool_names)
        # 推定は「無効化」ではないので版は進めない (走行中の取得の結果は有効)
        self.assertEqual(
            mgr._persona_membership_version.get(("srv", "air_city_a"), 0), 0
        )

    def test_reconnect_failure_drops_dead_connection_and_records_it(self) -> None:
        """再接続に失敗した instance を掴んだままにしない。

        掴んだままだと (a) ツール wrapper の遅延起動は「_connections にキーが
        あるか」しか見ないので切れた接続へ送り続け、(b) 失敗記録が無いので UI の
        失敗一覧にも出ず backoff も効かない (Codex レビュー 2026-08-10 high)。
        """
        import asyncio

        mgr = MCPClientManager()
        mgr._server_meta["srv"] = self._per_persona_meta()
        fake = self._FakeLiveConnection(tools=["post"])
        mgr._connections["srv:persona:air_city_a"] = fake
        mgr._refs["srv:persona:air_city_a"] = {"persona:air_city_a"}
        mgr._persona_tool_names[("srv", "air_city_a")] = frozenset({"post"})

        async def _failing_connect():
            fake.connect_count += 1
            raise RuntimeError("connection refused")

        fake.connect = _failing_connect
        ready_fired: list[str] = []
        mgr._on_server_ready["srv"] = [lambda: ready_fired.append("srv")]

        with mock.patch(
            "saiverse.addon_config.get_params",
            return_value={"api_key": "k"},
        ):
            ok = asyncio.run(mgr.reconnect_server("srv"))

        self.assertFalse(ok)
        self.assertEqual(fake.connect_count, 1)
        # 死んだ接続を掴み続けない = 遅延起動 / 次の Pulse 頭がやり直せる
        self.assertNotIn("srv:persona:air_city_a", mgr._connections)
        self.assertEqual(
            mgr._persona_tool_names[("srv", "air_city_a")], frozenset()
        )
        # UI の失敗一覧と backoff に載る
        entry = mgr._failed_instances["srv:persona:air_city_a"]
        self.assertTrue(entry["last_message"])
        self.assertTrue(mgr._is_in_backoff("srv:persona:air_city_a"))
        # 生きた instance が 1 つも無いので ready は立てない
        self.assertEqual(ready_fired, [])

    # -- 遷移中 (起動中 / 停止中) の競合 (Codex レビュー 3 巡目) ------------
    #
    # MCP ループは単一スレッドだが、接続の確立と切断には await がある。その隙に
    # 別経路 (Pulse 頭の取得・再接続・遅延起動・アドオン無効化・全停止) が同じ
    # instance_key を触ると、二重起動と孤児 subprocess が生まれる。

    def test_start_refuses_while_another_start_is_in_flight(self) -> None:
        """起動中の instance_key には二重に接続を張らない。"""
        import asyncio

        from tools.mcp_client import MCPInstanceBusyError

        mgr = MCPClientManager()
        mgr._server_meta["srv"] = self._per_persona_meta()
        key = "srv:persona:air_city_a"
        mgr._starting.add(key)

        with mock.patch(
            "saiverse.addon_config.get_params", return_value={"api_key": "k"},
        ):
            with self.assertRaises(MCPInstanceBusyError):
                asyncio.run(mgr._start_instance(key, "srv", persona_id="air_city_a"))

    def test_start_refuses_while_a_stop_is_in_flight(self) -> None:
        """停止処理の途中に立て直さない。

        旧接続の後片付け (参照・所属の無効化) が新しい接続に適用される事故を防ぐ。
        断られた回の実害は「その Pulse はツール無し」だけで、次の Pulse 頭が張り直す。
        """
        import asyncio

        from tools.mcp_client import MCPInstanceBusyError

        mgr = MCPClientManager()
        mgr._server_meta["srv"] = self._per_persona_meta()
        key = "srv:persona:air_city_a"
        mgr._stopping.add(key)

        with mock.patch(
            "saiverse.addon_config.get_params", return_value={"api_key": "k"},
        ):
            with self.assertRaises(MCPInstanceBusyError):
                asyncio.run(mgr._start_instance(key, "srv", persona_id="air_city_a"))

    def test_start_is_a_noop_when_already_connected(self) -> None:
        """別経路が先に張り終えていたら二重に張らない (Pulse 頭 × 遅延起動)。"""
        import asyncio

        mgr = MCPClientManager()
        mgr._server_meta["srv"] = self._per_persona_meta()
        key = "srv:persona:air_city_a"
        fake = self._FakeLiveConnection(tools=["post"])
        mgr._connections[key] = fake

        with mock.patch("saiverse.addon_config.get_params") as mock_params:
            asyncio.run(mgr._start_instance(key, "srv", persona_id="air_city_a"))

        self.assertIs(mgr._connections[key], fake)
        mock_params.assert_not_called()

    def test_stop_during_start_shuts_the_instance_down_on_landing(self) -> None:
        """起動の await 中に来た停止要求は、起動が着地した瞬間に効く。

        接続が _connections に入るのは接続完了後なので、停止側は「対象に無い」と
        判断してしまう。放置すると無効化・全停止のあとに subprocess とツール
        wrapper が残る (Codex レビュー 3 巡目 high)。
        """
        import asyncio

        from tools.mcp_client import MCPInstanceBusyError

        mgr = MCPClientManager()
        mgr._server_meta["srv"] = self._per_persona_meta()
        key = "srv:persona:air_city_a"
        disconnected: list[str] = []

        class _SlowConnection(self._FakeLiveConnection):
            async def connect(self_inner):
                # 接続の途中にアドオン無効化が走る状況の再現
                await mgr._shutdown_instance(key)
                self_inner._connected = True

            async def disconnect(self_inner):
                disconnected.append(key)
                self_inner._connected = False

        async def _run():
            with mock.patch(
                "saiverse.addon_config.get_params", return_value={"api_key": "k"},
            ), mock.patch(
                "tools.mcp_client.MCPServerConnection",
                side_effect=lambda *a, **kw: _SlowConnection(tools=["post"]),
            ):
                with self.assertRaises(MCPInstanceBusyError):
                    await mgr._start_instance(key, "srv", persona_id="air_city_a")

        asyncio.run(_run())

        # 着地した接続は自分で畳まれ、登録もされない
        self.assertEqual(disconnected, [key])
        self.assertNotIn(key, mgr._connections)
        self.assertEqual(mgr._registered_tools, {})
        self.assertNotIn(key, mgr._stop_requested)

    def test_stop_request_does_not_leak_to_the_next_start(self) -> None:
        """停止要求は「あの起動」に向いたもの。失敗した回に残して次を自壊させない。"""
        import asyncio

        mgr = MCPClientManager()
        mgr._server_meta["srv"] = self._per_persona_meta()
        key = "srv:persona:air_city_a"

        class _FailingConnection(self._FakeLiveConnection):
            async def connect(self_inner):
                # 起動中に停止要求が置かれ、その起動は失敗する
                mgr._stop_requested.add(key)
                raise RuntimeError("connection refused")

        with mock.patch(
            "saiverse.addon_config.get_params", return_value={"api_key": "k"},
        ), mock.patch(
            "tools.mcp_client.MCPServerConnection",
            side_effect=lambda *a, **kw: _FailingConnection(),
        ):
            with self.assertRaises(RuntimeError):
                asyncio.run(mgr._start_instance(key, "srv", persona_id="air_city_a"))

        self.assertNotIn(key, mgr._stop_requested)
        self.assertNotIn(key, mgr._starting)

    def test_reconnect_ignores_unowned_failed_instances(self) -> None:
        """誰も要求していない instance を、失敗記録だけを根拠に復活させない。

        失敗記録を根拠に加えると refcount 0 の live instance ができ、明示的に
        止めた / 一度も所有されたことのない instance まで蘇る (Codex レビュー
        3 巡目 high)。復活の条件は「まだ望まれているか」= 参照があるか。
        """
        import asyncio

        mgr = MCPClientManager()
        mgr._server_meta["srv"] = self._per_persona_meta()
        mgr._failed_instances["srv:persona:air_city_a"] = {
            "attempts": 1, "next_retry_at": 0,
            "last_category": "network", "last_message": "boom",
            "last_exception": None,
        }
        started: list[str] = []

        async def _fake_start(self_, instance_key, qualified_name, persona_id=None, **kw):
            started.append(instance_key)

        with mock.patch.object(
            MCPClientManager, "_start_instance", new=_fake_start
        ):
            ok = asyncio.run(mgr.reconnect_server("srv"))

        self.assertFalse(ok)   # 対象が無いので何もしていない
        self.assertEqual(started, [])

    def test_reconnect_can_restart_a_down_instance(self) -> None:
        """一度落ちた instance も再接続ボタンから立て直せる。

        落とした側が復旧の道を塞ぐと、失敗した instance は「_connections に
        居ない」だけで再接続の対象から消え、global / 名前付き instance は
        プロセス再起動まで戻れなくなる (自分の畳み方が作った裏返し)。
        """
        import asyncio

        mgr = MCPClientManager()
        mgr._server_meta["srv"] = self._per_persona_meta()
        # 一時的な失敗で畳まれた状態: 接続は無いが参照は残っている
        # (= まだ望まれている instance)。加えて失敗記録も付いている。
        mgr._refs["srv:persona:air_city_a"] = {"persona:air_city_a"}
        mgr._failed_instances["srv:persona:air_city_a"] = {
            "attempts": 1, "next_retry_at": 0,
            "last_category": "network", "last_message": "boom",
            "last_exception": None,
        }
        started: list[str] = []

        async def _fake_start(self_, instance_key, qualified_name, persona_id=None, **kw):
            started.append(instance_key)
            mgr._connections[instance_key] = self._FakeLiveConnection(tools=["post"])

        with mock.patch(
            "saiverse.addon_config.get_params",
            return_value={"api_key": "k"},
        ), mock.patch.object(
            MCPClientManager, "_start_instance", new=_fake_start
        ):
            ok = asyncio.run(mgr.reconnect_server("srv"))

        self.assertTrue(ok)
        self.assertEqual(started, ["srv:persona:air_city_a"])
        # 立て直せたので失敗記録は消える (UI の失敗一覧から外れる)
        self.assertNotIn("srv:persona:air_city_a", mgr._failed_instances)

    def test_recoverable_shutdown_keeps_references(self) -> None:
        """一時的な失敗で畳んだ instance は「まだ在るべきもの」として参照を残す。

        参照まで落とすと refcount 0 の invariant を壊したまま復旧させることになり、
        再接続の対象からも消える。恒久的な撤去 (手動停止・アドオン無効化) だけが
        参照を落とす。
        """
        import asyncio

        mgr = MCPClientManager()
        mgr._server_meta["srv"] = self._per_persona_meta()
        key = "srv:persona:air_city_a"

        mgr._connections[key] = self._FakeLiveConnection(tools=["post"])
        mgr._refs[key] = {"persona:air_city_a"}
        asyncio.run(mgr._shutdown_instance(key, recoverable=True))
        self.assertEqual(mgr._refs.get(key), {"persona:air_city_a"})

        mgr._connections[key] = self._FakeLiveConnection(tools=["post"])
        asyncio.run(mgr._shutdown_instance(key))
        self.assertNotIn(key, mgr._refs)

    def test_shutdown_keeps_named_instance_context(self) -> None:
        """設定不足で畳んだ名前付き instance は、context を保って復旧できる。

        ``${instance.*}`` の context を忘れるのは「もう使わない」と決めた
        stop_instance の責務。_shutdown_instance が一緒に捨てると、設定が戻っても
        再登録なしには復旧できなくなる (Codex レビュー 2026-08-10)。
        """
        import asyncio

        mgr = MCPClientManager()
        mgr._server_meta["gw"] = {
            "scope": "instance_template", "addon_name": "vessel-addon",
            "raw_config": {"url": "http://127.0.0.1:${instance.ws_port}"},
        }
        fake = self._FakeLiveConnection(tools=["speak"])
        mgr._connections["gw:instance:v1"] = fake
        mgr._instance_contexts["gw:instance:v1"] = {"ws_port": "9001"}

        asyncio.run(mgr._shutdown_instance("gw:instance:v1"))

        self.assertNotIn("gw:instance:v1", mgr._connections)
        self.assertEqual(
            mgr._instance_contexts["gw:instance:v1"], {"ws_port": "9001"}
        )

        # 明示的な停止 (vessel の解除) だけが context を忘れる
        mgr._connections["gw:instance:v1"] = self._FakeLiveConnection(tools=["speak"])
        asyncio.run(mgr.stop_instance("gw:instance:v1"))
        self.assertNotIn("gw:instance:v1", mgr._instance_contexts)

    def test_invalidated_refresh_does_not_register_tools(self) -> None:
        """走行中に無効化された取得は、登録簿にも触らない。

        版の検査が _register_tools の後だと、一覧は捨てても TOOL_REGISTRY へ
        足した wrapper が残り、未評価の別ペルソナが config 近似でそれを見る
        (Codex レビュー 2026-08-10)。
        """
        import asyncio

        mgr = MCPClientManager()
        mgr._server_meta["srv"] = self._per_persona_meta()
        fake = self._FakeLiveConnection(tools=["post", "read"])

        async def _start_then_invalidate(
            self_, instance_key, qualified_name, persona_id=None, **kw
        ):
            mgr._connections[instance_key] = fake
            mgr._mark_persona_tools_unavailable(("srv", "air_city_a"))

        registered: list[str] = []
        with mock.patch(
            "saiverse.addon_config.get_params", return_value={"api_key": "k"},
        ), mock.patch.object(
            MCPClientManager, "_start_instance", new=_start_then_invalidate
        ), mock.patch(
            "tools.register_external_tool",
            side_effect=lambda name, schema, fn: registered.append(name) or True,
        ):
            changed = asyncio.run(mgr.refresh_persona_tools("air_city_a"))

        self.assertFalse(changed)
        self.assertEqual(registered, [])
        self.assertEqual(mgr._registered_tools, {})

    def test_sync_bridge_timeout_is_fail_closed(self) -> None:
        """取得が timeout で返らなかった Pulse では、ツールを提示しない。

        以前は所属が未評価 (None) のままだったので config 近似 (鍵が解決できる
        = 使える顔) へ戻り、一度も繋げていないツールを提示していた
        (Codex レビュー 2026-08-10 の fail-open)。橋を渡す前に「未評価の所属を
        空集合へ倒す」ことで、結果が来ない Pulse は正直にツール無しになる。
        """
        import asyncio

        import tools.mcp_client as mcp_mod

        mgr = MCPClientManager()
        mgr._server_meta["srv"] = self._per_persona_meta()
        mgr._registered_tools["srv__post"] = {
            "qualified_server_name": "srv",
            "tool_name": "post",
            "scope": "per_persona",
            "building_ids": None,
        }

        async def _slow_refresh(self_, persona_id, *, connect=True):
            await asyncio.sleep(1.0)
            return True

        prev_manager = mcp_mod._manager
        prev_loop = mcp_mod._loop
        prev_thread = mcp_mod._loop_thread
        loop = mcp_mod._ensure_loop_thread()
        mcp_mod._manager = mgr
        try:
            with mock.patch(
                "saiverse.addon_config.get_params",
                return_value={"api_key": "k"},
            ), mock.patch.object(
                MCPClientManager, "refresh_persona_tools", new=_slow_refresh
            ):
                changed = mcp_mod.refresh_persona_tools_sync(
                    "air_city_a", connect=True, timeout=0.05,
                )

            self.assertFalse(changed)
            self.assertEqual(
                mgr._persona_tool_names[("srv", "air_city_a")], frozenset()
            )
            self.assertFalse(
                mgr.is_tool_available_for_persona("srv__post", "air_city_a")
            )
        finally:
            mcp_mod._manager = prev_manager
            loop.call_soon_threadsafe(loop.stop)
            mcp_mod._loop = prev_loop
            mcp_mod._loop_thread = prev_thread

    def test_membership_overrides_config_approximation(self) -> None:
        """所属記録がある間は、config 近似ではなく本人の一覧が真実 (§I)。

        鍵が解決できても、サーバー側で消えたツールは使えない — 近似はそれを
        表せない。
        """
        mgr = MCPClientManager()
        mgr._server_meta["srv"] = self._per_persona_meta()
        mgr._registered_tools["srv__gone"] = {
            "qualified_server_name": "srv",
            "tool_name": "gone",
            "scope": "per_persona",
            "building_ids": None,
        }
        mgr._registered_tools["srv__alive"] = {
            "qualified_server_name": "srv",
            "tool_name": "alive",
            "scope": "per_persona",
            "building_ids": None,
        }
        mgr._persona_tool_names[("srv", "air_city_a")] = frozenset({"alive"})

        with mock.patch(
            "saiverse.addon_config.get_params",
            return_value={"api_key": "k"},
        ):
            self.assertTrue(
                mgr.is_tool_available_for_persona("srv__alive", "air_city_a")
            )
            self.assertFalse(
                mgr.is_tool_available_for_persona("srv__gone", "air_city_a")
            )
            # 所属記録が無いペルソナは従来の config 近似にフォールバック
            self.assertTrue(
                mgr.is_tool_available_for_persona("srv__gone", "sofia_city_a")
            )

    def test_is_server_enabled_is_strict_boolean(self) -> None:
        """誤記は「切ったつもり」を尊重して無効側へ倒す。"""
        from tools.mcp_config import is_server_enabled

        self.assertTrue(is_server_enabled("s", {}))
        self.assertTrue(is_server_enabled("s", {"enabled": True}))
        self.assertFalse(is_server_enabled("s", {"enabled": False}))
        self.assertFalse(is_server_enabled("s", {"enabled": "false"}))
        self.assertFalse(is_server_enabled("s", {"enabled": "true"}))
        self.assertFalse(is_server_enabled("s", {"enabled": 1}))

    def test_enabled_judgement_has_a_single_owner(self) -> None:
        """起動時と addon hot-load が同じ判定を通ること。

        片方だけ厳密化したせいで、同じ定義が boot では無効・再有効化では有効
        という食い違いを作った前科がある。生の truthiness 判定が復活したら
        ここで落とす。
        """
        from pathlib import Path

        repo = Path(__file__).resolve().parents[1]
        cfg_text = (repo / "tools" / "mcp_config.py").read_text(encoding="utf-8")
        # ちょうど 1 箇所 = is_server_enabled の本体だけが読む
        self.assertEqual(
            cfg_text.count('cfg.get("enabled"'),
            1,
            "'enabled' must be read in exactly one place (is_server_enabled)",
        )
        client_text = (repo / "tools" / "mcp_client.py").read_text(encoding="utf-8")
        self.assertNotIn(
            '.get("enabled"',
            client_text,
            "mcp_client must delegate the 'enabled' judgement to is_server_enabled()",
        )

    def test_transport_type_selection(self) -> None:
        self.assertEqual(
            MCPServerConnection("srv", {"command": "npx"}).transport_type, "stdio"
        )
        self.assertEqual(
            MCPServerConnection("srv", {"url": "https://x.test"}).transport_type,
            "streamable_http",
        )
        self.assertEqual(
            MCPServerConnection(
                "srv", {"url": "https://x.test", "transport": "sse"}
            ).transport_type,
            "sse",
        )

    def test_resolve_config_placeholders_reaches_headers(self) -> None:
        """remote MCP の認証情報はヘッダーに載るので、解決がそこまで届く必要がある。"""
        raw = {
            "url": "https://example.test/mcp",
            "headers": {"Authorization": "Bearer ${persona.addon.my-addon.api_key}"},
        }
        with mock.patch(
            "saiverse.addon_config.get_params",
            return_value={"api_key": "secret-key"},
        ):
            resolved = resolve_config_placeholders(raw, persona_id="air_city_a")
        self.assertEqual(resolved["headers"]["Authorization"], "Bearer secret-key")

    def test_unresolved_header_placeholder_is_detected(self) -> None:
        """API キー未入力を missing_config として検出できる (= スペルを隠せる)。"""
        from tools.mcp_client import _find_unresolved_placeholders

        raw = {
            "headers": {"Authorization": "Bearer ${persona.addon.my-addon.api_key}"}
        }
        with mock.patch("saiverse.addon_config.get_params", return_value={}):
            resolved = resolve_config_placeholders(raw, persona_id="air_city_a")
        self.assertTrue(_find_unresolved_placeholders(resolved))

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
