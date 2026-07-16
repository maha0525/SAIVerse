"""MCP client manager for dynamically registering external tools.

This module manages MCP (Model Context Protocol) server connections and
registers the tools they expose into SAIVerse's TOOL_REGISTRY.

Design overview (see ``docs/intent/mcp_addon_integration.md``):

- Each MCP server is identified internally by an **instance_key**:
  ``"{qualified_server_name}:global"`` for global scope,
  ``"{qualified_server_name}:persona:{persona_id}"`` for per_persona scope.
- ``qualified_server_name`` is already addon-prefixed by ``tools.mcp_config``
  (e.g. ``saiverse-elyth-addon__elyth`` for addon-declared servers).
- Each instance tracks a **refcount** (the set of referrers that currently
  use it). When refcount hits zero, the instance is shut down.
- ``scope: "global"`` servers start at initialization time (refcount starts
  at 1, attributed to the config source — addon / user_data / builtin).
- ``scope: "per_persona"`` servers defer startup to the first tool call from
  that persona (implemented in Phase 2c; currently skipped with a warning).
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import re
import threading
import time
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from tools.core import ToolSchema
# Bind get_active_persona_id at module-load time. Lazy-importing this inside
# the per_persona MCP wrapper used to race with saiverse-voice-tts's
# GPT-SoVITS loader (which temporarily removes 'tools.*' from sys.modules
# while inserting its own tools/ dir onto sys.path), causing
# ModuleNotFoundError in the middle of a spell call. See
# memory/project_tts_import_pollution.md.
from tools.context import get_active_manager, get_active_persona_id

LOGGER = logging.getLogger(__name__)

_DEFAULT_TOOL_TIMEOUT_SECONDS = 120
_DEFAULT_STARTUP_TIMEOUT_SECONDS = 30.0


def check_building_gate(
    tool_name: str,
    building_ids: Optional[List[str]],
) -> Optional[str]:
    """Execute-time gate for ``building_ids``-restricted tools.

    Returns an error message string when the active persona is outside the
    allowed buildings; returns None when the tool may proceed. Shared by
    native tools (wrapped in ``tools.__init__._add_registered_tool``) and
    MCP tools (wrapped in ``_make_mcp_tool_wrapper``) so the gate semantics
    live at a single point. Detail: docs/intent/stackchan_vessel.md A-3-c
    """
    if not building_ids:
        return None
    persona_id_for_gate = get_active_persona_id()
    active_manager = get_active_manager()
    current_building_id: Optional[str] = None
    if active_manager is not None and persona_id_for_gate:
        persona_obj = (
            getattr(active_manager, "all_personas", {}).get(persona_id_for_gate)
            or getattr(active_manager, "personas", {}).get(persona_id_for_gate)
        )
        current_building_id = (
            getattr(persona_obj, "current_building_id", None)
            if persona_obj is not None else None
        )
    if current_building_id is None or current_building_id not in building_ids:
        LOGGER.info(
            "tool execute-time gate: blocked %s persona=%s building=%s allowed=%s",
            tool_name, persona_id_for_gate, current_building_id,
            sorted(str(b) for b in building_ids),
        )
        return (
            f"Error: ツール '{tool_name}' は現在の Building "
            f"({current_building_id or '不明'}) からは実行できません。"
            f"許可されている Building: {', '.join(str(b) for b in building_ids)}"
        )
    return None
_loop: Optional[asyncio.AbstractEventLoop] = None
_loop_thread: Optional[threading.Thread] = None

# --- Error categories (see docs/intent/mcp_addon_integration.md §F) -------

ERROR_CATEGORY_RUNTIME_MISSING = "runtime_missing"
ERROR_CATEGORY_MISSING_CONFIG = "missing_config"
ERROR_CATEGORY_AUTH_FAILED = "auth_failed"
ERROR_CATEGORY_COMMAND_ERROR = "command_error"
ERROR_CATEGORY_NETWORK = "network"
ERROR_CATEGORY_PROCESS_CRASH = "process_crash"
ERROR_CATEGORY_UNKNOWN = "unknown"

_CATEGORY_JP = {
    ERROR_CATEGORY_RUNTIME_MISSING: "必要なランタイム（npx/uvx/python 等）が見つかりません",
    ERROR_CATEGORY_MISSING_CONFIG: "必須の設定値が未設定です",
    ERROR_CATEGORY_AUTH_FAILED: "サーバー側の認証に失敗しました",
    ERROR_CATEGORY_COMMAND_ERROR: "サーバーの起動コマンドエラー",
    ERROR_CATEGORY_NETWORK: "ネットワークエラー",
    ERROR_CATEGORY_PROCESS_CRASH: "サーバープロセスが異常終了しました",
    ERROR_CATEGORY_UNKNOWN: "不明なエラー",
}

# Exponential backoff bounds (seconds). Failed instances are not auto-retried
# until the deadline; callers may force-retry via reconnect/manual_stop.
_STARTUP_BACKOFF_INITIAL = 2.0
_STARTUP_BACKOFF_MAX = 60.0

_PLACEHOLDER_RE = re.compile(r"\$\{([^}]+)\}")


def _classify_error(exc: Exception) -> str:
    """Heuristically categorize an MCP startup exception."""
    exc_str = str(exc).lower()

    if isinstance(exc, FileNotFoundError):
        return ERROR_CATEGORY_RUNTIME_MISSING
    if "command not found" in exc_str or "no such file" in exc_str:
        return ERROR_CATEGORY_RUNTIME_MISSING
    if "401" in exc_str or "unauthorized" in exc_str or "forbidden" in exc_str:
        return ERROR_CATEGORY_AUTH_FAILED
    if "403" in exc_str and "rate" not in exc_str:
        return ERROR_CATEGORY_AUTH_FAILED
    if "timeout" in exc_str or "timed out" in exc_str:
        return ERROR_CATEGORY_NETWORK
    if "connection" in exc_str or "dns" in exc_str or "name resolution" in exc_str:
        return ERROR_CATEGORY_NETWORK
    if "e404" in exc_str or "could not find package" in exc_str or "not found" in exc_str:
        return ERROR_CATEGORY_COMMAND_ERROR
    if "exit" in exc_str or "terminated" in exc_str or "signal" in exc_str:
        return ERROR_CATEGORY_PROCESS_CRASH
    return ERROR_CATEGORY_UNKNOWN


def _find_unresolved_placeholders(resolved_config: Any) -> List[str]:
    """Walk a resolved config and collect any remaining ``${...}`` strings.

    Used to detect missing-config failures before attempting to spawn a
    subprocess (e.g. ``${persona.addon.x.y}`` with no AddonPersonaConfig).
    """
    found: List[str] = []

    def _walk(value: Any) -> None:
        if isinstance(value, str):
            for match in _PLACEHOLDER_RE.finditer(value):
                found.append(match.group(0))
        elif isinstance(value, list):
            for item in value:
                _walk(item)
        elif isinstance(value, dict):
            for inner in value.values():
                _walk(inner)

    _walk(resolved_config)
    return found


def _build_user_error_message(
    qualified_name: str,
    addon_name: Optional[str],
    category: str,
    detail: str,
) -> str:
    """Build the user-facing (UI/log) error message."""
    source = f"{addon_name} アドオン由来の" if addon_name else ""
    category_desc = _CATEGORY_JP.get(category, category)
    return (
        f"{source}{qualified_name} MCPサーバーの起動に失敗しました"
        f"（{category_desc}）。アドオンの導入および設定が正常に完了しているか"
        f"確認してください。解決しない場合はアドオン制作者に問い合わせてください。"
        f"（詳細: {detail}）"
    )


def _build_persona_error_message(
    qualified_name: str,
    tool_name: str,
    category: str,
    detail: Optional[str] = None,
) -> str:
    """Build the short persona/LLM-facing error message.

    Kept brief so the LLM recognizes the tool as unavailable and picks an
    alternative action rather than retrying in a loop. ``detail`` (if given)
    is appended so the LLM and the user have at least one concrete clue
    about what went wrong — "不明なエラー" alone is unhelpful.
    """
    category_desc = _CATEGORY_JP.get(category, category)
    base = (
        f"ツール '{qualified_name}__{tool_name}' は現在利用できません"
        f"（原因: {category_desc}）。"
    )
    if detail:
        base = base + f" 詳細: {detail}"
    return base


def _normalize_spell_config(raw_value: Any) -> Dict[str, Dict[str, Any]]:
    """Normalize spell tool declarations into ``tool_name -> options``."""
    result: Dict[str, Dict[str, Any]] = {}
    if isinstance(raw_value, list):
        for entry in raw_value:
            if isinstance(entry, str):
                result[entry] = {}
            elif isinstance(entry, dict):
                tool_name = entry.get("name")
                if isinstance(tool_name, str) and tool_name:
                    result[tool_name] = {
                        key: value for key, value in entry.items()
                        if key != "name"
                    }
    elif isinstance(raw_value, dict):
        for tool_name, options in raw_value.items():
            if isinstance(tool_name, str) and tool_name:
                if options is True or options is None:
                    result[tool_name] = {}
                elif isinstance(options, str):
                    result[tool_name] = {"display_name": options}
                elif isinstance(options, dict):
                    result[tool_name] = dict(options)
    return result


def _strip_jsonschema_meta_keys(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Remove top-level JSON Schema metadata keys that our ToolSchema
    pydantic model rejects as ``extra_forbidden``.

    MCP servers commonly include ``$schema`` (draft URI) and sometimes
    ``$id``/``$defs``. These are valid JSON Schema but not meaningful for
    SAIVerse's runtime tool invocation, so we drop them at the top level.
    Nested ``$ref``/``$defs`` inside ``properties`` are preserved because
    they can be load-bearing for referential schemas.
    """
    return {k: v for k, v in schema.items() if not k.startswith("$")}


def _tool_schema_from_mcp(
    namespaced_name: str,
    qualified_server_name: str,
    tool_name: str,
    tool_def: Any,
    spell_config: Dict[str, Dict[str, Any]],
) -> ToolSchema:
    description = getattr(tool_def, "description", "") or ""
    parameters = getattr(tool_def, "inputSchema", None)
    if not isinstance(parameters, dict):
        parameters = {"type": "object", "properties": {}}
    else:
        parameters = _strip_jsonschema_meta_keys(parameters)

    spell_options = spell_config.get(tool_name, {})
    display_name = spell_options.get("display_name") or spell_options.get("spell_display_name") or ""
    spell_visible = spell_options.get("visible", True)
    raw_building_ids = spell_options.get("building_ids")
    building_ids: Optional[List[str]] = None
    if isinstance(raw_building_ids, list):
        building_ids = [str(bid) for bid in raw_building_ids if bid]
        if not building_ids:
            building_ids = None

    return ToolSchema(
        name=namespaced_name,
        description=f"[MCP:{qualified_server_name}] {description}".strip(),
        parameters=parameters,
        result_type="string",
        spell=tool_name in spell_config,
        spell_display_name=str(display_name) if display_name else "",
        spell_visible=bool(spell_visible),
        building_ids=building_ids,
    )


def _format_tool_result(result: Any) -> str:
    content = getattr(result, "content", None) or []
    if not content:
        return "(no content)"

    rendered: List[str] = []
    for item in content:
        text = getattr(item, "text", None)
        if text:
            rendered.append(str(text))
            continue
        uri = getattr(item, "uri", None)
        if uri:
            rendered.append(f"[resource: {uri}]")
            continue
        data = getattr(item, "data", None)
        if data is not None:
            rendered.append(f"[binary: {len(data)} bytes]")
            continue
        rendered.append(str(item))
    return "\n".join(rendered)


class MCPServerConnection:
    """Manage one MCP server connection.

    ``server_name`` here is the qualified (potentially addon-prefixed) name
    used for logging; it does not affect the underlying MCP protocol.
    """

    def __init__(
        self,
        server_name: str,
        config: Dict[str, Any],
        instance_key: Optional[str] = None,
    ) -> None:
        self.server_name = server_name
        self.config = config
        # Full instance key (``<server>:global`` / ``:persona:<id>`` /
        # ``:instance:<id>``). Used to give each named instance its own
        # subprocess errlog so concurrent subprocesses from one server
        # template (e.g. one gateway per vessel) don't interleave / clobber
        # into a single shared file. ``None`` keeps the legacy per-server path.
        self.instance_key = instance_key
        self.session: Any = None
        self.tools: List[Any] = []
        self._connected = False
        # 管理側 (``_shutdown_instance``) が意図的に停止した接続かどうか。
        # ``call_tool`` の失敗時 auto-reconnect が、 既に stop された instance を
        # 接続オブジェクト単位で蘇生し、 port を掴んだ孤児 subprocess を生む事故
        # を防ぐ (in-flight のツールコールと stop がレースする経路。 stackchan の
        # per-vessel gateway 停止で顕在化した)。stop 時に True を立て、 except
        # 分岐で再接続せず素通しで raise させる。
        self._closed = False
        # 接続の async context (transport / ClientSession) は単一の長命
        # 「所有タスク」(_owner_task) の中で開いて同じタスクの中で閉じる。
        # anyio のキャンセルスコープはタスク束縛のため、別タスクから閉じると
        # "exit cancel scope in a different task" を起こし、最悪 detached な
        # async generator 片付けタスクが _deliver_cancellation で無限スピン
        # して GIL を焼く (docs/issues/mcp_cancel_scope_spin_gil_starvation.md)。
        self._owner_task: Optional[asyncio.Task] = None
        self._shutdown_event: Optional[asyncio.Event] = None
        self._ready_future: Optional[asyncio.Future] = None

    @property
    def connected(self) -> bool:
        return self._connected and self.session is not None

    @property
    def transport_type(self) -> str:
        if "command" in self.config:
            return "stdio"
        return str(self.config.get("transport", "streamable_http"))

    async def connect(self) -> None:
        if self.connected:
            return
        # 明示 stop 後に (reconnect_server 等で) 意図的に繋ぎ直す場合は復活を許す。
        self._closed = False

        try:
            startup_timeout = float(
                self.config.get(
                    "startup_timeout", _DEFAULT_STARTUP_TIMEOUT_SECONDS
                )
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"MCP server '{self.server_name}' startup_timeout must be a number"
            ) from exc
        if startup_timeout <= 0:
            raise ValueError(
                f"MCP server '{self.server_name}' startup_timeout must be positive"
            )

        loop = asyncio.get_running_loop()
        self._shutdown_event = asyncio.Event()
        self._ready_future = loop.create_future()
        # 接続の async context はこのタスクではなく専用の所有タスクの中で
        # 開閉する。connect() は所有タスクが「接続完了」か「失敗」を通知
        # するまで待つだけ (実害のある作業は所有タスク側でやる)。
        self._owner_task = loop.create_task(self._run_connection())
        try:
            await asyncio.wait_for(
                asyncio.shield(self._ready_future),
                timeout=startup_timeout,
            )
        except asyncio.TimeoutError as exc:
            LOGGER.error(
                "MCP server '%s' did not become ready within %.1fs; aborting startup",
                self.server_name,
                startup_timeout,
            )
            # Cancelling the owner is safe: its transport context is unwound by
            # the same owner task, preserving the anyio cancel-scope invariant.
            task = self._owner_task
            if task is not None and not task.done():
                task.cancel()
            if self._ready_future is not None and not self._ready_future.done():
                self._ready_future.cancel()
            await self.disconnect()
            raise TimeoutError(
                f"MCP server '{self.server_name}' startup timed out after "
                f"{startup_timeout:.1f}s"
            ) from exc

    async def _run_connection(self) -> None:
        """所有タスク本体: transport/session を開き、shutdown 要求まで保持する。

        ``async with`` の開始も終了もこの単一タスクの中で完結するため、anyio
        のキャンセルスコープがクロスタスクで抜かれることがない
        (docs/issues/mcp_cancel_scope_spin_gil_starvation.md)。
        """
        ready = self._ready_future
        shutdown = self._shutdown_event
        try:
            async with AsyncExitStack() as stack:
                if self.transport_type == "stdio":
                    await self._connect_stdio(stack)
                elif self.transport_type == "sse":
                    await self._connect_sse(stack)
                else:
                    await self._connect_streamable_http(stack)

                if self.session is None:
                    raise RuntimeError("MCP session was not created")

                init_result = await self.session.initialize()
                self._connected = True
                LOGGER.info(
                    "MCP server '%s' initialized (protocol=%s, server=%s)",
                    self.server_name,
                    getattr(init_result, "protocolVersion", "unknown"),
                    getattr(getattr(init_result, "serverInfo", None), "name", "unknown"),
                )
                await self._discover_tools()

                if ready is not None and not ready.done():
                    ready.set_result(None)

                # shutdown 要求 (disconnect) が来るまで context を保持。
                if shutdown is not None:
                    await shutdown.wait()
        except Exception as exc:
            if ready is not None and not ready.done():
                ready.set_exception(exc)
            else:
                LOGGER.warning(
                    "MCP server '%s' connection task ended with error: %s",
                    self.server_name, exc,
                )
        finally:
            self._connected = False
            self.tools = []
            self.session = None
            if ready is not None and not ready.done():
                ready.set_exception(
                    RuntimeError(
                        f"MCP server '{self.server_name}' connection task "
                        "ended before becoming ready"
                    )
                )

    def _open_subprocess_errlog(self, stack: AsyncExitStack):
        """Open a per-session, per-server log file for subprocess stderr.

        Resolves the session log directory from the root logger's FileHandler
        (= the same dir backend.log lives in) so the new file co-locates with
        the rest of the SAIVerse session output. Falls back to the system temp
        dir if no FileHandler is configured (e.g. test runs).

        The handle is registered with the exit_stack so it closes when the
        MCPServerConnection is torn down. ``line buffered`` so the subprocess
        log shows up promptly without explicit flush.
        """
        import logging
        import tempfile

        log_dir: Optional[Path] = None
        for handler in logging.getLogger().handlers:
            if isinstance(handler, logging.FileHandler):
                log_dir = Path(handler.baseFilename).parent
                break

        if log_dir is None:
            log_dir = Path(tempfile.gettempdir())
        log_dir.mkdir(parents=True, exist_ok=True)

        # Sanitize server_name for filesystem
        safe_name = self.server_name.replace("/", "_").replace("\\", "_")
        # Append the instance scope so named instances (and personas) get a
        # dedicated file. Global scope keeps the bare per-server path so
        # existing single-instance setups are unaffected. Without this, several
        # concurrent subprocesses spawned from one server template (e.g. one
        # stackchan gateway per vessel) all open the SAME path, and only the
        # first instance's stderr survives — the rest are invisible for
        # debugging (per-vessel "Gateway started" / vision_url / device logs).
        suffix = ""
        instance_key = self.instance_key
        if instance_key and instance_key.startswith(self.server_name + ":"):
            scope = instance_key[len(self.server_name) + 1:]
            if scope and scope != "global":
                safe_scope = (
                    scope.replace(":", "_").replace("/", "_").replace("\\", "_")
                )
                suffix = f"_{safe_scope}"
        path = log_dir / f"mcp_subprocess_{safe_name}{suffix}.log"
        handle = open(path, "a", encoding="utf-8", buffering=1)
        stack.callback(handle.close)
        return handle

    async def _connect_stdio(self, stack: AsyncExitStack) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        server_params = StdioServerParameters(
            command=self.config["command"],
            args=self.config.get("args", []),
            env=self.config.get("env"),
        )

        # Capture subprocess stderr to a per-session log file. mcp SDK's
        # stdio_client defaults errlog to sys.stderr which only goes to
        # the parent console (= SAIVerse runner), so subprocess-internal
        # logs ("Gateway started", "ESP32 hello", env diagnostics, etc.)
        # are invisible to anyone reading backend.log. We forward to a
        # sibling file in the same session log dir.
        errlog_handle = self._open_subprocess_errlog(stack)

        LOGGER.info(
            "MCP subprocess starting: server=%s command=%s arg_count=%d "
            "env_keys=%s errlog=%s",
            self.server_name,
            server_params.command,
            len(server_params.args or []),
            sorted((server_params.env or {}).keys()),
            getattr(errlog_handle, "name", "<unknown>"),
        )
        read_stream, write_stream = await stack.enter_async_context(
            stdio_client(server_params, errlog=errlog_handle)
        )
        self.session = await stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )

    async def _connect_sse(self, stack: AsyncExitStack) -> None:
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        read_stream, write_stream = await stack.enter_async_context(
            sse_client(self.config["url"])
        )
        self.session = await stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )

    async def _connect_streamable_http(self, stack: AsyncExitStack) -> None:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        transport = await stack.enter_async_context(
            streamablehttp_client(self.config["url"])
        )
        read_stream, write_stream = transport[0], transport[1]
        self.session = await stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )

    async def _discover_tools(self) -> None:
        result = await self.session.list_tools()
        self.tools = list(getattr(result, "tools", []) or [])
        LOGGER.info(
            "MCP server '%s': discovered %d tool(s): %s",
            self.server_name,
            len(self.tools),
            [getattr(tool, "name", "?") for tool in self.tools],
        )

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        if not self.connected:
            raise ConnectionError(f"MCP server '{self.server_name}' is not connected")

        timeout_seconds = int(self.config.get("timeout", _DEFAULT_TOOL_TIMEOUT_SECONDS))
        try:
            result = await self.session.call_tool(
                name=tool_name,
                arguments=arguments,
                read_timeout_seconds=timedelta(seconds=timeout_seconds),
            )
        except Exception as exc:
            LOGGER.warning(
                "MCP tool call '%s__%s' failed after dispatch (%s). "
                "The result is uncertain; refusing automatic replay.",
                self.server_name,
                tool_name,
                exc,
            )
            if not self._closed:
                await self.disconnect()
            raise

        if getattr(result, "isError", False):
            LOGGER.warning("MCP tool '%s__%s' returned an error", self.server_name, tool_name)
        return _format_tool_result(result)

    async def disconnect(self) -> None:
        self._connected = False
        self.tools = []
        self.session = None

        # 所有タスクに shutdown を合図し、所有タスク自身が (= context を開いた
        # のと同じタスクが) async with を巻き戻すのを待つ。ここで直接 aclose
        # しないことで「別タスクでキャンセルスコープを抜く」事象を構造的に排除する。
        if self._shutdown_event is not None:
            self._shutdown_event.set()
        task = self._owner_task
        self._owner_task = None
        if task is not None and not task.done():
            try:
                # transport の __aexit__ が死んだ subprocess 等で詰まる可能性に
                # 備えてタイムアウト。超過時は cancel する (cancel による巻き戻し
                # も所有タスク内で走るのでクロスタスクにはならない)。
                await asyncio.wait_for(asyncio.shield(task), timeout=15.0)
            except asyncio.TimeoutError:
                LOGGER.warning(
                    "MCP server '%s' owner task did not finish within 15s; cancelling.",
                    self.server_name,
                )
                task.cancel()
            except asyncio.CancelledError:
                # Startup timeout deliberately cancels the owner task after
                # asking it to unwind its own transport context.
                pass
            except Exception as exc:
                LOGGER.debug(
                    "MCP server '%s' owner task ended with: %s", self.server_name, exc,
                )
        self._shutdown_event = None
        self._ready_future = None


def _make_instance_key(
    qualified_server_name: str,
    persona_id: Optional[str] = None,
    instance_id: Optional[str] = None,
) -> str:
    """Build the internal instance key.

    - Global scope   : ``"<qualified_name>:global"``
    - Per-persona    : ``"<qualified_name>:persona:<persona_id>"``
    - Named instance : ``"<qualified_name>:instance:<instance_id>"``
      (設計 G, docs/intent/mcp_addon_integration.md — 同一 server 定義から
      設定違いで複数インスタンスを動的起動する場合に使う)

    ``instance_id`` takes precedence over ``persona_id`` when both are given.
    """
    if instance_id is not None:
        return f"{qualified_server_name}:instance:{instance_id}"
    if persona_id is None:
        return f"{qualified_server_name}:global"
    return f"{qualified_server_name}:persona:{persona_id}"


def _referrer_from_meta(addon_name: Optional[str], source_path: Optional[str]) -> str:
    """Attribute a config source to a referrer tag used in the refcount set.

    - ``addon:<addon_name>`` when declared under expansion_data/<addon_name>/
    - ``builtin``             when declared under builtin_data/
    - ``user_data``           otherwise (user_data/ and sub-projects)
    """
    if addon_name:
        return f"addon:{addon_name}"
    if source_path and "builtin_data" in str(source_path):
        return "builtin"
    return "user_data"


class MCPClientManager:
    """Manage all configured MCP server instances.

    Keyed state:
      * ``_connections[instance_key]`` — live MCPServerConnection
      * ``_refs[instance_key]``        — set of referrer tags (refcount = len)
      * ``_server_meta[qualified]``    — {scope, source_path, addon_name, raw_config}
      * ``_registered_tools[name]``    — tool metadata keyed by the namespaced
                                         tool name presented to the LLM
    """

    def __init__(self) -> None:
        self._connections: Dict[str, MCPServerConnection] = {}
        self._refs: Dict[str, Set[str]] = {}
        self._server_meta: Dict[str, Dict[str, Any]] = {}
        self._registered_tools: Dict[str, Dict[str, Any]] = {}
        # instance_key -> per-named-instance config context (token/port/...),
        # kept so reconnect / backoff-retry can re-resolve ${instance.*} without
        # the caller re-supplying it. Only populated for ":instance:" keys.
        # See docs/intent/mcp_addon_integration.md 設計 G.
        self._instance_contexts: Dict[str, Dict[str, str]] = {}
        # instance_key -> failure record with backoff deadline
        self._failed_instances: Dict[str, Dict[str, Any]] = {}
        # qualified_name -> subscribers waiting for this server to become ready
        self._on_server_ready: Dict[str, List[Callable[[], None]]] = {}
        # qualified_names currently ready (reset on disconnect, set on connect)
        self._ready_servers: Set[str] = set()

    # -- Startup ---------------------------------------------------------

    async def start_all(self) -> None:
        from tools.mcp_config import load_mcp_configs

        configs = load_mcp_configs()
        if not configs:
            LOGGER.info("MCP: no servers configured")
            return

        try:
            import mcp  # noqa: F401
        except ImportError:
            LOGGER.warning("MCP: 'mcp' package is not installed; skipping MCP initialization")
            return

        for qualified_name, config in configs.items():
            scope = str(config.get("scope", "global")).lower()
            addon_name = config.get("_addon_name")
            source_path = config.get("_source_path")

            self._server_meta[qualified_name] = {
                "scope": scope,
                "source_path": source_path,
                "addon_name": addon_name,
                "raw_config": config,
            }

            referrer = _referrer_from_meta(addon_name, source_path)

            if scope == "global":
                await self._start_global_instance(qualified_name, referrer)
            elif scope == "per_persona":
                # Run one-shot tool discovery so the LLM can see the tools;
                # actual per-persona instances start lazily on first call.
                await self._discover_per_persona_tools(qualified_name)
            elif scope == "instance_template":
                # A template for named instances (設計 G): do NOT auto-start.
                # The meta is registered above; an addon spawns concrete
                # instances on demand via ``register_instance`` (e.g.
                # one gateway per vessel). Tools are reached through the
                # instance connection (``conn.call_tool``) directly, so no
                # global tool discovery is performed here.
                LOGGER.info(
                    "MCP: server '%s' is an instance_template; deferring start "
                    "to dynamic register_instance() calls",
                    qualified_name,
                )
            else:
                LOGGER.warning(
                    "MCP server '%s' has unknown scope '%s'; treating as global",
                    qualified_name,
                    scope,
                )
                await self._start_global_instance(qualified_name, referrer)

    async def _start_global_instance(
        self,
        qualified_name: str,
        referrer: str,
    ) -> Optional[str]:
        """Start a global-scope MCP instance and register its tools.

        Returns the instance_key on success, ``None`` on failure.
        """
        instance_key = _make_instance_key(qualified_name, None)
        try:
            await self._start_instance(instance_key, qualified_name, persona_id=None)
        except Exception as exc:
            LOGGER.warning(
                "MCP: server '%s' failed to connect: %s",
                qualified_name,
                exc,
            )
            return None
        self._add_reference(instance_key, referrer)
        self._fire_server_ready(qualified_name)
        return instance_key

    async def register_instance(
        self,
        qualified_server_name: str,
        instance_id: str,
        context: Dict[str, str],
    ) -> Optional[str]:
        """Dynamically start a **named instance** of an already-loaded server
        definition, injecting per-instance ``${instance.*}`` values.

        Used when an addon wants several independent subprocesses from the
        same server template — e.g. saiverse-stackchan-addon spawning one
        gateway per physical vessel, each on its own port/token (設計 G,
        docs/intent/mcp_addon_integration.md; driving use case in
        docs/intent/stackchan_vessel.md 設計 K).

        The template must already be in ``_server_meta`` (declared in some
        ``mcp_servers.json`` and seen at ``start_all``). ``context`` is stored
        so reconnect / backoff-retry can re-resolve without the caller
        re-supplying it. Idempotent: registering an already-running instance
        refreshes its context and keeps it alive.

        Returns the instance_key on success, ``None`` on start failure.
        """
        meta = self._server_meta.get(qualified_server_name)
        if meta is None:
            raise ValueError(
                f"Unknown MCP server '{qualified_server_name}'; named instances "
                "require a server template declared in mcp_servers.json"
            )

        instance_key = _make_instance_key(
            qualified_server_name, instance_id=instance_id
        )
        referrer = _referrer_from_meta(
            meta.get("addon_name"), meta.get("source_path")
        )
        self._instance_contexts[instance_key] = dict(context)

        existing = self._connections.get(instance_key)
        if existing is not None and existing.connected:
            # Already running — just refresh context + ensure a reference.
            self._add_reference(instance_key, referrer)
            return instance_key

        try:
            await self._start_instance(
                instance_key, qualified_server_name, instance_context=context
            )
        except Exception as exc:
            LOGGER.warning(
                "MCP: named instance '%s' failed to start: %s",
                instance_key,
                exc,
            )
            return None

        self._add_reference(instance_key, referrer)
        self._fire_server_ready(qualified_server_name)
        return instance_key

    async def stop_instance(self, instance_key: str) -> bool:
        """Stop a named instance and forget its context.

        Counterpart to :py:meth:`register_instance` for when a vessel is
        unpaired / deleted. Force-stops regardless of refcount and drops the
        stored ``${instance.*}`` context so it is not silently restarted.
        """
        self._instance_contexts.pop(instance_key, None)
        return await self.manual_stop_instance(instance_key)

    async def _discover_per_persona_tools(self, qualified_name: str) -> None:
        """One-shot tool discovery for a per_persona server.

        Opens a throw-away connection using the env resolved for a single
        concrete persona (tool schemas are shared across all persona
        instances by design), registers the tools in TOOL_REGISTRY, then
        closes the probe connection. Actual tool calls spin up dedicated
        per-persona instances on demand (see the per_persona branch in
        ``_make_mcp_tool_wrapper``).
        """
        meta = self._server_meta.get(qualified_name)
        if meta is None:
            return
        if meta.get("tools_discovered"):
            return

        addon_name = meta.get("addon_name")
        persona_id = self._pick_discovery_persona(addon_name)
        if persona_id is None:
            LOGGER.info(
                "MCP: per_persona server '%s' has no candidate persona for discovery; "
                "tools will appear once a persona is configured",
                qualified_name,
            )
            return

        from tools.mcp_config import resolve_config_placeholders

        raw_config = meta["raw_config"]
        try:
            resolved = resolve_config_placeholders(raw_config, persona_id=persona_id)
        except Exception as exc:
            LOGGER.warning(
                "MCP: placeholder resolution failed during discovery for '%s' (persona=%s): %s",
                qualified_name,
                persona_id,
                exc,
            )
            return

        probe = MCPServerConnection(qualified_name, resolved)
        discovery_key = f"{qualified_name}:discovery:{persona_id}"
        try:
            await probe.connect()
            self._register_tools(probe, qualified_name, discovery_key, persona_id)
            meta["tools_discovered"] = True
            meta["discovery_persona_id"] = persona_id
            LOGGER.info(
                "MCP: per_persona server '%s' discovered %d tool(s) via persona '%s'",
                qualified_name,
                len(probe.tools),
                persona_id,
            )
        except Exception as exc:
            LOGGER.warning(
                "MCP: tool discovery failed for per_persona server '%s' (persona=%s): %s",
                qualified_name,
                persona_id,
                exc,
            )
        finally:
            try:
                await probe.disconnect()
            except Exception as disconnect_exc:
                LOGGER.debug(
                    "MCP: probe disconnect error for '%s': %s",
                    qualified_name,
                    disconnect_exc,
                )

    @staticmethod
    def _pick_discovery_persona(addon_name: Optional[str]) -> Optional[str]:
        """Pick one persona id suitable for per_persona discovery.

        Preference order:
          1. A persona with an AddonPersonaConfig row for ``addon_name`` — they
             are known to have per-persona parameters filled in.
          2. Any persona in the ``ai`` table — usable when the addon relies on
             global params only (no ``${persona.addon.*}`` references in env).
          3. ``None`` if neither is available (discovery will be skipped).
        """
        try:
            from database.models import AI, AddonPersonaConfig
            from database.session import SessionLocal
        except ImportError as exc:
            LOGGER.warning(
                "MCP: DB layer unavailable for discovery persona selection: %s", exc
            )
            return None

        db = SessionLocal()
        try:
            if addon_name:
                row = (
                    db.query(AddonPersonaConfig)
                    .filter(AddonPersonaConfig.addon_name == addon_name)
                    .order_by(AddonPersonaConfig.id.asc())
                    .first()
                )
                if row is not None:
                    return row.persona_id
            ai_row = db.query(AI).order_by(AI.AIID.asc()).first()
            if ai_row is not None:
                return ai_row.AIID
            return None
        except Exception as exc:
            LOGGER.warning("MCP: discovery persona query failed: %s", exc)
            return None
        finally:
            db.close()

    async def _start_instance(
        self,
        instance_key: str,
        qualified_name: str,
        persona_id: Optional[str] = None,
        instance_context: Optional[Dict[str, str]] = None,
    ) -> None:
        """Open a fresh MCPServerConnection and register its tools.

        Raises on connection failure after recording the failure with its
        error category and updating the exponential backoff deadline.
        Caller is responsible for adding a reference (refcount) after a
        successful start.

        ``instance_context`` supplies ``${instance.*}`` values for named
        instances (設計 G). When omitted, it is recovered from
        ``self._instance_contexts`` so backoff-retry / reconnect paths that
        only know the instance_key still resolve correctly.
        """
        from tools.mcp_config import resolve_config_placeholders

        meta = self._server_meta.get(qualified_name)
        if meta is None:
            raise ValueError(f"Unknown MCP server '{qualified_name}'")

        if instance_context is None:
            instance_context = self._instance_contexts.get(instance_key)

        raw_config = meta["raw_config"]
        resolved = resolve_config_placeholders(
            raw_config, persona_id=persona_id, instance_context=instance_context
        )

        unresolved = _find_unresolved_placeholders(resolved)
        if unresolved:
            category = ERROR_CATEGORY_MISSING_CONFIG
            detail = "未解決のプレースホルダー: " + ", ".join(sorted(set(unresolved)))
            user_msg = _build_user_error_message(
                qualified_name, meta.get("addon_name"), category, detail
            )
            self._record_failure(instance_key, category, user_msg)
            LOGGER.error(
                "MCP startup error: instance=%s category=%s msg=%s",
                instance_key,
                category,
                user_msg,
            )
            raise RuntimeError(detail)

        connection = MCPServerConnection(
            qualified_name, resolved, instance_key=instance_key
        )
        try:
            await connection.connect()
        except Exception as exc:
            category = _classify_error(exc)
            user_msg = _build_user_error_message(
                qualified_name, meta.get("addon_name"), category, str(exc)
            )
            self._record_failure(instance_key, category, user_msg, exc)
            LOGGER.error(
                "MCP startup error: instance=%s category=%s msg=%s",
                instance_key,
                category,
                user_msg,
            )
            raise

        self._clear_failure(instance_key)
        self._connections[instance_key] = connection
        self._register_tools(connection, qualified_name, instance_key, persona_id)

    # -- Failure tracking / backoff --------------------------------------

    def _record_failure(
        self,
        instance_key: str,
        category: str,
        user_message: str,
        exc: Optional[Exception] = None,
    ) -> None:
        prev = self._failed_instances.get(instance_key, {})
        attempts = int(prev.get("attempts", 0)) + 1
        delay = min(
            _STARTUP_BACKOFF_INITIAL * (2 ** (attempts - 1)),
            _STARTUP_BACKOFF_MAX,
        )
        self._failed_instances[instance_key] = {
            "attempts": attempts,
            "next_retry_at": time.monotonic() + delay,
            "last_category": category,
            "last_message": user_message,
            "last_exception": repr(exc) if exc else None,
        }

    def _clear_failure(self, instance_key: str) -> None:
        self._failed_instances.pop(instance_key, None)

    def _is_in_backoff(self, instance_key: str) -> bool:
        entry = self._failed_instances.get(instance_key)
        if not entry:
            return False
        return time.monotonic() < float(entry.get("next_retry_at", 0))

    def is_tool_available_for_persona(
        self,
        tool_name: str,
        persona_id: Optional[str],
        building_id: Optional[str] = None,
    ) -> bool:
        """Whether the registered tool can actually be invoked for this persona.

        For non-MCP tools this always returns True (not our concern — the
        caller can use this as a general "is this tool visible" filter).

        For MCP tools, we resolve the server's env placeholders in the
        given persona context and report False if any ``${...}`` remains
        unresolved (missing api_key etc). This is used to hide tools from
        the persona-specific spell list so the LLM does not try to call
        tools that would immediately fail with ``missing_config``.

        ``building_ids`` filter (Phase 4' / A-3-c): when a tool's
        ``spell_tools`` entry lists ``building_ids`` and ``building_id`` is
        provided, only allow the tool when ``building_id`` is in that list.
        Passing ``building_id=None`` skips the filter (= callers without
        building context, e.g. UI summon listing, see all otherwise-visible
        tools). Detail: docs/intent/stackchan_vessel.md A-3-c.

        Policy notes:
          * ``scope=global`` servers resolve without persona context.
            An unresolved placeholder means the tool is broken for
            everyone (e.g. missing AddonConfig / env var) — hide it.
          * ``scope=per_persona`` with ``persona_id=None`` is conservative:
            we cannot evaluate the right context, so we hide the tool.
          * Server not in ``_server_meta`` (shouldn't happen for a
            registered tool, but defensive) → True (fail open).
        """
        meta_info = self._registered_tools.get(tool_name)
        if not meta_info:
            # Native (non-MCP) tool: consult SPELL_TOOL_SCHEMAS so the
            # building_ids filter applies symmetrically. Without this,
            # native tools that declare building_ids in their ToolSchema
            # (e.g. saiverse-stackchan-addon's see / env3) bypass the
            # Phase 4' / A-3-c visibility gate.
            from tools import SPELL_TOOL_SCHEMAS

            schema = SPELL_TOOL_SCHEMAS.get(tool_name)
            if schema is not None and building_id is not None:
                schema_building_ids = getattr(schema, "building_ids", None)
                if schema_building_ids and building_id not in schema_building_ids:
                    return False
            return True

        # building_ids フィルタ: building_id を渡された場合のみ評価。
        # building_id が None なら filter をスキップ (= UI 列挙等の
        # 「現在地が決まらない」呼び出しは visible にする)。
        building_ids = meta_info.get("building_ids")
        if building_ids and building_id is not None:
            if building_id not in building_ids:
                return False

        qualified = meta_info.get("qualified_server_name")
        if not qualified:
            return True
        server_meta = self._server_meta.get(qualified)
        if server_meta is None:
            return True

        from tools.mcp_config import resolve_config_placeholders

        raw_config = server_meta.get("raw_config") or {}
        scope = meta_info.get("scope", "global")
        if scope == "per_persona":
            if persona_id is None:
                return False
            try:
                resolved = resolve_config_placeholders(
                    raw_config, persona_id=persona_id
                )
            except Exception:
                return False
        else:
            try:
                resolved = resolve_config_placeholders(
                    raw_config, persona_id=None
                )
            except Exception:
                return False

        unresolved = _find_unresolved_placeholders(resolved)
        return len(unresolved) == 0

    def get_failed_instances(self) -> List[Dict[str, Any]]:
        """Snapshot of all instances currently in backoff state (for UI)."""
        now = time.monotonic()
        out: List[Dict[str, Any]] = []
        for instance_key, entry in sorted(self._failed_instances.items()):
            next_retry_at = float(entry.get("next_retry_at", 0))
            out.append({
                "instance_key": instance_key,
                "attempts": entry.get("attempts", 0),
                "category": entry.get("last_category"),
                "message": entry.get("last_message"),
                "seconds_until_retry": max(0.0, next_retry_at - now),
                "in_backoff": now < next_retry_at,
            })
        return out

    def _register_tools(
        self,
        connection: MCPServerConnection,
        qualified_name: str,
        instance_key: str,
        persona_id: Optional[str],
    ) -> None:
        from tools import register_external_tool

        spell_config = _normalize_spell_config(connection.config.get("spell_tools"))
        scope = self._server_meta.get(qualified_name, {}).get("scope", "global")

        for tool_def in connection.tools:
            tool_name = getattr(tool_def, "name", None)
            if not tool_name:
                continue
            namespaced_name = f"{qualified_name}__{tool_name}"

            # For global scope, only register once even if called repeatedly.
            # For per_persona, the LLM-visible name is still shared across
            # personas (the wrapper resolves the right instance at call time).
            if namespaced_name in self._registered_tools:
                continue

            schema = _tool_schema_from_mcp(
                namespaced_name,
                qualified_name,
                tool_name,
                tool_def,
                spell_config,
            )
            schema.addon_name = connection.config.get("_addon_name")
            wrapper = _make_mcp_tool_wrapper(self, qualified_name, tool_name, scope)
            if register_external_tool(namespaced_name, schema, wrapper):
                self._registered_tools[namespaced_name] = {
                    "qualified_server_name": qualified_name,
                    "tool_name": tool_name,
                    "scope": scope,
                    "description": schema.description,
                    "spell": schema.spell,
                    "spell_display_name": schema.spell_display_name,
                    "building_ids": schema.building_ids,
                    "source_path": connection.config.get("_source_path"),
                    "addon_name": connection.config.get("_addon_name"),
                    "first_registered_from_instance": instance_key,
                    "first_registered_with_persona": persona_id,
                }

    # -- Shutdown --------------------------------------------------------

    async def shutdown_all(self) -> None:
        from tools import unregister_external_tool

        for name in list(self._registered_tools):
            unregister_external_tool(name)
            self._registered_tools.pop(name, None)

        for instance_key in list(self._connections.keys()):
            await self._shutdown_instance(instance_key, force=True)

        self._connections.clear()
        self._refs.clear()
        self._server_meta.clear()

    async def _shutdown_instance(self, instance_key: str, *, force: bool = False) -> None:
        connection = self._connections.pop(instance_key, None)
        if connection is None:
            return
        # in-flight のツールコールが disconnect のキャンセルで失敗しても、
        # call_tool の auto-reconnect でこの接続を蘇生させない (孤児 subprocess
        # 防止)。disconnect が例外を投げる前に立てておく必要がある。
        connection._closed = True
        if not force:
            await self._unregister_instance_tools(instance_key)
        try:
            await connection.disconnect()
        except Exception as exc:
            LOGGER.debug(
                "MCP: failed to disconnect instance '%s': %s", instance_key, exc
            )
        self._refs.pop(instance_key, None)
        self._instance_contexts.pop(instance_key, None)

    async def _unregister_instance_tools(self, instance_key: str) -> None:
        """Unregister tools tied to a specific instance.

        For global scope, each server has exactly one instance, so all tools
        registered from this server are unregistered. For per_persona, tools
        are shared across persona instances, so we only unregister when no
        persona instance remains (checked by the caller).
        """
        from tools import unregister_external_tool

        qualified_name = self._qualified_from_instance_key(instance_key)
        if not qualified_name:
            return

        # Check if any other instance of the same qualified_name is still running
        for other_key in self._connections:
            if other_key == instance_key:
                continue
            if self._qualified_from_instance_key(other_key) == qualified_name:
                # Another instance is still alive; keep tools registered
                return

        for tool_name, meta in list(self._registered_tools.items()):
            if meta.get("qualified_server_name") != qualified_name:
                continue
            unregister_external_tool(tool_name)
            self._registered_tools.pop(tool_name, None)

    @staticmethod
    def _qualified_from_instance_key(instance_key: str) -> Optional[str]:
        # Format: "<qualified>:global", "<qualified>:persona:<persona_id>",
        # or "<qualified>:instance:<instance_id>" (named instance, 設計 G).
        # Split at the scope suffix. ``qualified`` may itself contain no
        # colons (SAIVerse does not use colons in server_name or addon_name).
        if ":" not in instance_key:
            return None
        if instance_key.endswith(":global"):
            return instance_key.rsplit(":", 1)[0]
        if ":persona:" in instance_key:
            return instance_key.split(":persona:", 1)[0]
        if ":instance:" in instance_key:
            return instance_key.split(":instance:", 1)[0]
        return None

    # -- Reference counting ---------------------------------------------

    def _add_reference(self, instance_key: str, referrer: str) -> None:
        refs = self._refs.setdefault(instance_key, set())
        refs.add(referrer)
        LOGGER.debug(
            "MCP: instance '%s' refcount=%d (+%s)",
            instance_key,
            len(refs),
            referrer,
        )

    async def _remove_reference(self, instance_key: str, referrer: str) -> None:
        refs = self._refs.get(instance_key)
        if not refs:
            return
        refs.discard(referrer)
        LOGGER.debug(
            "MCP: instance '%s' refcount=%d (-%s)",
            instance_key,
            len(refs),
            referrer,
        )
        if not refs:
            await self._shutdown_instance(instance_key)

    # -- Reconnect / manual stop ----------------------------------------

    async def reconnect_server(self, qualified_name: str) -> bool:
        """Reconnect all instances of a given qualified server name.

        Re-loads raw config from the source JSON and re-interpolates against
        the current AddonConfig DB before each reconnect, so AddonConfig
        mutations since the initial start get picked up by the restarted
        subprocess env. Without this, an addon that rotates a token in
        AddonConfig (e.g. stackchan-addon Phase 2' pairing rotates
        ``master_token``) would need a full SAIVerse restart to push the
        new env to the subprocess.

        Crucially, ``_server_meta[name]["raw_config"]`` holds the
        **already-interpolated** result from process-launch time (= old
        values are baked in). Re-running ``resolve_config_placeholders`` on
        it is a no-op because the ``${addon.X.Y}`` markers are gone. The
        only way to pick up DB changes is to re-read the source
        ``mcp_servers.json`` and re-interpolate from scratch.

        OS process env is fixed at spawn time — there is no way to mutate
        an already-running subprocess's env. The only path is kill +
        respawn with a fresh env, which is what disconnect → connect does
        for stdio transport.
        """
        from tools.mcp_config import (
            _interpolate_value,
            _load_config_file,
            resolve_config_placeholders,
        )

        matching = [
            key for key in self._connections
            if self._qualified_from_instance_key(key) == qualified_name
        ]
        if not matching:
            return False

        # Server is going down → drop ready flag so future on_server_ready
        # subscribers won't fire immediately based on stale state.
        self._ready_servers.discard(qualified_name)

        meta = self._server_meta.get(qualified_name)

        # Step 1: re-load raw config from source JSON + interpolate against
        # current AddonConfig DB. Update _server_meta in-place so any
        # subsequent operations also see the fresh values.
        if meta and meta.get("source_path"):
            try:
                source_path = Path(meta["source_path"])
                raw = _load_config_file(source_path)
                old_cfg = meta.get("raw_config") or {}
                original_name = old_cfg.get("_original_server_name")
                # addon-prefixed の場合 raw のキーは original_name、 そう
                # じゃない (user_data / builtin) 場合は qualified_name と
                # 同名のはず
                if original_name and original_name in raw:
                    cfg = raw[original_name]
                else:
                    cfg = raw.get(qualified_name)
                if cfg is not None:
                    cfg_copy = _interpolate_value(cfg)
                    cfg_copy["_source_path"] = str(source_path)
                    addon_name = meta.get("addon_name")
                    if addon_name:
                        cfg_copy["_addon_name"] = addon_name
                        cfg_copy["_original_server_name"] = (
                            original_name or qualified_name
                        )
                    meta["raw_config"] = cfg_copy
                    LOGGER.info(
                        "MCP: re-loaded raw_config for '%s' from %s "
                        "(picked up AddonConfig changes)",
                        qualified_name, source_path,
                    )
            except Exception as exc:
                LOGGER.warning(
                    "MCP: failed to re-load raw_config for '%s' "
                    "(will reconnect with cached values): %s",
                    qualified_name, exc,
                )

        raw_config = meta.get("raw_config") if meta else None

        success = True
        for instance_key in matching:
            connection = self._connections.get(instance_key)
            if connection is None:
                continue

            # Resolve persona-specific placeholders on top of the freshly
            # AddonConfig-interpolated raw_config. On failure fall through
            # to reconnect with the existing config so we at least try a
            # restart, rather than leaving the subprocess stale.
            if raw_config is not None:
                persona_id = self._persona_id_from_instance_key(instance_key)
                instance_context = self._instance_contexts.get(instance_key)
                try:
                    connection.config = resolve_config_placeholders(
                        raw_config,
                        persona_id=persona_id,
                        instance_context=instance_context,
                    )
                except Exception as exc:
                    LOGGER.warning(
                        "MCP: placeholder re-resolution failed for '%s' "
                        "before reconnect (will use cached config): %s",
                        instance_key,
                        exc,
                    )

            await self._unregister_instance_tools(instance_key)
            try:
                await connection.disconnect()
                await connection.connect()
                persona_id = self._persona_id_from_instance_key(instance_key)
                self._register_tools(connection, qualified_name, instance_key, persona_id)
            except Exception as exc:
                LOGGER.warning(
                    "MCP: reconnect failed for instance '%s': %s",
                    instance_key,
                    exc,
                )
                success = False
        if success:
            self._fire_server_ready(qualified_name)
        return success

    # -- Server-ready event hook ----------------------------------------
    # Lets addons (e.g. avatar_loader) trigger work the moment a server
    # finishes connecting, instead of polling on a timer.

    def on_server_ready(
        self,
        qualified_name: str,
        callback: Callable[[], None],
    ) -> None:
        """Register a callback for when the given MCP server is ready.

        If the server is already ready at registration time, the callback
        fires immediately on the caller's thread. Otherwise it is queued and
        fired on the MCP event loop thread once the server connects (or
        reconnects via :py:meth:`reconnect_server`).

        Callbacks should be lightweight and non-blocking — defer real work
        to a worker thread inside the callback if needed.
        """
        if qualified_name in self._ready_servers:
            try:
                callback()
            except Exception:
                LOGGER.exception(
                    "MCP: on_server_ready immediate callback raised for '%s'",
                    qualified_name,
                )
            return
        self._on_server_ready.setdefault(qualified_name, []).append(callback)

    def _fire_server_ready(self, qualified_name: str) -> None:
        """Mark a server ready and invoke any queued callbacks."""
        self._ready_servers.add(qualified_name)
        callbacks = self._on_server_ready.get(qualified_name) or []
        for cb in callbacks:
            try:
                cb()
            except Exception:
                LOGGER.exception(
                    "MCP: on_server_ready callback raised for '%s'",
                    qualified_name,
                )

    async def manual_stop_instance(self, instance_key: str) -> bool:
        """Force-stop a specific instance, ignoring refcount.

        The instance may be restarted on the next tool call (for per_persona)
        or require a process restart (for global).
        """
        if instance_key not in self._connections:
            return False
        await self._shutdown_instance(instance_key)
        return True

    @staticmethod
    def _persona_id_from_instance_key(instance_key: str) -> Optional[str]:
        if ":persona:" not in instance_key:
            return None
        return instance_key.split(":persona:", 1)[1]

    # -- Status / introspection -----------------------------------------

    def get_registered_tool_names(self) -> List[str]:
        return list(self._registered_tools.keys())

    def get_registered_tool_info(self) -> List[Dict[str, Any]]:
        info: List[Dict[str, Any]] = []
        for namespaced_name, meta in sorted(self._registered_tools.items()):
            info.append({
                "name": namespaced_name,
                # Legacy field retained for callers that expect it
                "server_name": meta.get("qualified_server_name"),
                "qualified_server_name": meta.get("qualified_server_name"),
                "tool_name": meta.get("tool_name"),
                "scope": meta.get("scope"),
                "description": meta.get("description"),
                "spell": bool(meta.get("spell")),
                "spell_display_name": meta.get("spell_display_name") or "",
                "source_path": meta.get("source_path"),
                "addon_name": meta.get("addon_name"),
            })
        return info

    def get_server_status(self) -> List[Dict[str, Any]]:
        """Return status entries keyed by instance_key.

        Each entry includes the legacy ``name`` field (= qualified_server_name)
        for backwards compatibility with callers built around the single-
        instance-per-server assumption.
        """
        status: List[Dict[str, Any]] = []
        # Include running instances
        for instance_key, connection in sorted(self._connections.items()):
            qualified = self._qualified_from_instance_key(instance_key) or instance_key
            persona_id = self._persona_id_from_instance_key(instance_key)
            refs = sorted(self._refs.get(instance_key, set()))
            meta = self._server_meta.get(qualified, {})
            status.append({
                "instance_key": instance_key,
                "name": qualified,
                "qualified_server_name": qualified,
                "scope": meta.get("scope", "global"),
                "persona_id": persona_id,
                "transport": connection.transport_type,
                "connected": connection.connected,
                "tool_count": len(connection.tools),
                "tools": [getattr(tool, "name", "?") for tool in connection.tools],
                "refcount": len(refs),
                "referenced_by": refs,
                "addon_name": meta.get("addon_name"),
                "source_path": meta.get("source_path"),
            })
        # Also include configured-but-not-started servers (scope=per_persona in Phase 2a)
        for qualified, meta in sorted(self._server_meta.items()):
            scope = meta.get("scope", "global")
            if scope != "per_persona":
                continue
            has_any_instance = any(
                self._qualified_from_instance_key(k) == qualified
                for k in self._connections
            )
            if has_any_instance:
                continue
            status.append({
                "instance_key": None,
                "name": qualified,
                "qualified_server_name": qualified,
                "scope": scope,
                "persona_id": None,
                "transport": None,
                "connected": False,
                "tool_count": 0,
                "tools": [],
                "refcount": 0,
                "referenced_by": [],
                "addon_name": meta.get("addon_name"),
                "source_path": meta.get("source_path"),
                "note": "per_persona scope: will start on first tool call (Phase 2c pending)",
            })
        return status

    # -- Referrer management (for addon enable/disable events) ----------

    async def add_referrer_for_server(
        self,
        qualified_name: str,
        referrer: str,
        persona_id: Optional[str] = None,
    ) -> Optional[str]:
        """Add a referrer to the appropriate instance_key, starting the
        instance if necessary (global scope) — used when a new source (e.g.
        an addon being enabled) begins using a server.
        """
        meta = self._server_meta.get(qualified_name)
        if meta is None:
            LOGGER.warning(
                "MCP: add_referrer called for unknown server '%s'", qualified_name
            )
            return None
        scope = meta.get("scope", "global")
        instance_key = _make_instance_key(
            qualified_name, persona_id if scope == "per_persona" else None
        )
        if instance_key not in self._connections and scope == "global":
            try:
                await self._start_instance(instance_key, qualified_name, persona_id=None)
            except Exception as exc:
                LOGGER.warning(
                    "MCP: failed to start instance '%s' for referrer '%s': %s",
                    instance_key,
                    referrer,
                    exc,
                )
                return None
        self._add_reference(instance_key, referrer)
        return instance_key

    async def remove_referrer_for_server(
        self,
        qualified_name: str,
        referrer: str,
        persona_id: Optional[str] = None,
    ) -> None:
        """Remove a referrer from the instance, shutting it down if refcount
        reaches zero."""
        meta = self._server_meta.get(qualified_name)
        scope = (meta or {}).get("scope", "global")
        instance_key = _make_instance_key(
            qualified_name, persona_id if scope == "per_persona" else None
        )
        await self._remove_reference(instance_key, referrer)


def _make_mcp_tool_wrapper(
    manager: MCPClientManager,
    qualified_name: str,
    tool_name: str,
    scope: str,
):
    """Build the async callable that SEA/LLM invokes for this MCP tool.

    For global scope, the call is routed to the single global instance.
    For per_persona scope (Phase 2c), the active persona is read from
    ``tools.context`` and used to resolve (or lazy-start) the appropriate
    per-persona instance.
    """
    async def _mcp_tool_wrapper(**kwargs: Any) -> str:
        # building_ids の execute-time gate は ``tools.__init__._add_registered_tool``
        # で施される共通ラッパーが担う (= native / MCP 両方で対称)。詳細は
        # ``check_building_gate`` の docstring と
        # docs/intent/cached_head_architecture.md §7 Phase 4 を参照。
        if scope == "per_persona":
            persona_id = get_active_persona_id()
            if not persona_id:
                LOGGER.warning(
                    "MCP tool '%s__%s' (per_persona) invoked without an active persona",
                    qualified_name,
                    tool_name,
                )
                return (
                    f"Error: MCP tool '{qualified_name}__{tool_name}' is per_persona "
                    "scoped and requires an active persona context, but none was set."
                )

            instance_key = _make_instance_key(qualified_name, persona_id)
            if instance_key not in manager._connections:
                if manager._is_in_backoff(instance_key):
                    entry = manager._failed_instances[instance_key]
                    category = str(entry.get("last_category") or ERROR_CATEGORY_UNKNOWN)
                    detail = str(entry.get("last_message") or entry.get("last_exception") or "")
                    LOGGER.info(
                        "MCP per_persona backoff hit: instance=%s category=%s detail=%s",
                        instance_key,
                        category,
                        detail,
                    )
                    return _build_persona_error_message(
                        qualified_name, tool_name, category, detail=detail,
                    )
                # Launch the instance on the MCP event loop (where stdio
                # subprocess pipes and SSE connections are managed). Calling
                # _start_instance directly from the spell thread's loop would
                # leave stdio clients anchored to the wrong loop and fail
                # silently in some cases.
                LOGGER.info(
                    "MCP per_persona lazy start: instance=%s persona=%s",
                    instance_key,
                    persona_id,
                )
                try:
                    await run_on_mcp_loop(
                        manager._start_instance(
                            instance_key, qualified_name, persona_id=persona_id
                        )
                    )
                except Exception as start_exc:
                    LOGGER.exception(
                        "MCP per_persona lazy start failed: instance=%s persona=%s",
                        instance_key,
                        persona_id,
                    )
                    entry = manager._failed_instances.get(instance_key, {})
                    category = str(
                        entry.get("last_category") or _classify_error(start_exc)
                    )
                    # Prefer the user-facing message recorded by _record_failure;
                    # fall back to the raw exception so the persona/user can see
                    # at least a class name + message.
                    detail = str(
                        entry.get("last_message")
                        or entry.get("last_exception")
                        or f"{type(start_exc).__name__}: {start_exc}"
                    )
                    return _build_persona_error_message(
                        qualified_name, tool_name, category, detail=detail,
                    )
                # Self-reference: the instance stays alive while the persona
                # is actively using it. Shutdown on refcount=0 is handled
                # by explicit remove_reference calls from the caller chain
                # (Phase 3 addon disable event, manual stop, etc.).
                manager._add_reference(instance_key, f"persona:{persona_id}")

            connection = manager._connections[instance_key]
            LOGGER.info(
                "MCP tool call (per_persona): %s__%s persona=%s args=%s",
                qualified_name,
                tool_name,
                persona_id,
                kwargs,
            )
            result = await run_on_mcp_loop(connection.call_tool(tool_name, kwargs))
            preview = result[:200] + "..." if len(result) > 200 else result
            LOGGER.info(
                "MCP tool result (per_persona): %s__%s persona=%s -> %s",
                qualified_name,
                tool_name,
                persona_id,
                preview,
            )
            return result

        # Global scope path
        instance_key = _make_instance_key(qualified_name, None)
        connection = manager._connections.get(instance_key)
        if connection is None:
            return (
                f"Error: MCP server '{qualified_name}' is not running "
                f"(instance_key={instance_key})."
            )
        LOGGER.info(
            "MCP tool call: %s__%s args=%s",
            qualified_name,
            tool_name,
            kwargs,
        )
        result = await run_on_mcp_loop(connection.call_tool(tool_name, kwargs))
        preview = result[:200] + "..." if len(result) > 200 else result
        LOGGER.info(
            "MCP tool result: %s__%s -> %s",
            qualified_name,
            tool_name,
            preview,
        )
        return result

    _mcp_tool_wrapper.__name__ = f"{qualified_name}__{tool_name}"
    _mcp_tool_wrapper.__qualname__ = f"mcp.{qualified_name}.{tool_name}"
    return _mcp_tool_wrapper


async def maybe_await_tool_result(tool_func: Any, *args: Any, **kwargs: Any) -> Any:
    """Execute a tool and await its result when needed."""
    if tool_func is None or not callable(tool_func):
        return None

    if inspect.iscoroutinefunction(tool_func):
        return await tool_func(*args, **kwargs)

    result = tool_func(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


_manager: Optional[MCPClientManager] = None


def get_mcp_manager() -> Optional[MCPClientManager]:
    return _manager


def _ensure_loop_thread() -> asyncio.AbstractEventLoop:
    global _loop, _loop_thread
    if _loop is not None and _loop_thread is not None and _loop_thread.is_alive():
        return _loop

    ready = threading.Event()
    holder: Dict[str, asyncio.AbstractEventLoop] = {}

    def _runner() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        holder["loop"] = loop
        ready.set()
        loop.run_forever()
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()

    thread = threading.Thread(target=_runner, name="SAIVerse-MCP", daemon=True)
    thread.start()
    ready.wait()
    _loop = holder["loop"]
    _loop_thread = thread
    return _loop


async def run_on_mcp_loop(coro: Any) -> Any:
    """Await a coroutine on the dedicated MCP event loop."""
    if _loop is None:
        return await coro
    current_loop = asyncio.get_running_loop()
    if current_loop is _loop:
        return await coro
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return await asyncio.wrap_future(future)


async def initialize_mcp() -> Optional[MCPClientManager]:
    global _manager
    if _manager is not None:
        return _manager

    manager = MCPClientManager()
    await manager.start_all()
    # Return the manager even if no tools were registered yet — per_persona
    # scoped servers register their tools lazily (Phase 2c).
    _manager = manager
    return _manager


async def shutdown_mcp() -> None:
    global _manager
    if _manager is None:
        return
    await _manager.shutdown_all()
    _manager = None


async def reconnect_mcp_server(server_name: str) -> bool:
    manager = get_mcp_manager()
    if manager is None:
        return False
    return await run_on_mcp_loop(manager.reconnect_server(server_name))


async def reconnect_addon_mcp_servers(addon_name: str) -> Dict[str, bool]:
    """Reconnect all MCP servers declared by the given addon.

    Used by the addon config update API to push AddonConfig changes
    (e.g. ``vision_host`` after a Wi-Fi switch, ``master_token`` after a
    re-pairing) into the running subprocess env. MCP subprocesses pin
    their env at spawn time, so the only way to apply DB changes is
    kill + respawn via ``reconnect_server``.

    Returns a ``{qualified_name: success}`` map. Empty dict if the addon
    has no MCP servers or MCP isn't initialized.
    """
    manager = get_mcp_manager()
    if manager is None:
        return {}
    server_names = [
        qname
        for qname, meta in manager._server_meta.items()
        if meta.get("addon_name") == addon_name
    ]
    results: Dict[str, bool] = {}
    for name in server_names:
        try:
            ok = await run_on_mcp_loop(manager.reconnect_server(name))
            results[name] = ok
        except Exception as exc:
            LOGGER.warning(
                "MCP: reconnect raised for addon '%s' server '%s': %s",
                addon_name, name, exc,
            )
            results[name] = False
    return results


def _reload_addon_mcp_config(
    manager: MCPClientManager,
    addon_name: str,
) -> List[str]:
    """Scan ``expansion_data/<addon_name>/mcp_servers.json`` and register
    any newly-declared servers into ``_server_meta``.

    Idempotent: existing entries with the same qualified_name are kept.
    Use this on addon enable so that a freshly-installed addon's MCP
    servers appear without a SAIVerse restart.

    Returns the list of qualified server names newly added.
    """
    from saiverse.data_paths import EXPANSION_DATA_DIR
    from tools.mcp_config import _load_config_file, _interpolate_value

    config_path = EXPANSION_DATA_DIR / addon_name / "mcp_servers.json"
    if not config_path.exists():
        return []

    added: List[str] = []
    raw = _load_config_file(config_path)
    for server_name, cfg in raw.items():
        qualified_name = f"{addon_name}__{server_name}"
        if qualified_name in manager._server_meta:
            continue
        if not cfg.get("enabled", True):
            LOGGER.info(
                "MCP: server '%s' in addon '%s' is marked disabled, skipping",
                qualified_name,
                addon_name,
            )
            continue

        cfg_copy = _interpolate_value(cfg)
        cfg_copy["_source_path"] = str(config_path)
        cfg_copy["_addon_name"] = addon_name
        cfg_copy["_original_server_name"] = server_name

        scope = str(cfg_copy.get("scope", "global")).lower()
        manager._server_meta[qualified_name] = {
            "scope": scope,
            "source_path": str(config_path),
            "addon_name": addon_name,
            "raw_config": cfg_copy,
        }
        added.append(qualified_name)
        LOGGER.info(
            "MCP: hot-loaded server '%s' from addon '%s' (scope=%s)",
            qualified_name,
            addon_name,
            scope,
        )
    return added


async def _apply_addon_toggle(
    manager: MCPClientManager,
    addon_name: str,
    is_enabled: bool,
) -> None:
    """Adjust MCP state to reflect an addon being enabled/disabled.

    Enabled:
      * Hot-load the addon's mcp_servers.json (if any) into _server_meta
        so newly-installed addons don't need a SAIVerse restart.
      * global servers: add referrer, start if not running
      * per_persona servers: re-run tool discovery (idempotent)

    Disabled:
      * global servers: remove referrer (shutdown at refcount=0)
      * per_persona servers: unregister tools, stop all persona instances
      * Remove _server_meta entries tied to this addon so a subsequent
        enable re-reads the JSON (picking up any edits).
    """
    referrer = f"addon:{addon_name}"

    if is_enabled:
        _reload_addon_mcp_config(manager, addon_name)

    targets = [
        (qualified, meta)
        for qualified, meta in list(manager._server_meta.items())
        if meta.get("addon_name") == addon_name
    ]
    if not targets:
        LOGGER.debug(
            "MCP: addon '%s' has no MCP servers registered (nothing to do)",
            addon_name,
        )
        return

    for qualified_name, meta in targets:
        scope = meta.get("scope", "global")
        try:
            if is_enabled:
                if scope == "global":
                    await manager.add_referrer_for_server(qualified_name, referrer)
                elif scope == "per_persona":
                    # Re-run discovery to (re)register tools in TOOL_REGISTRY.
                    # _discover_per_persona_tools is idempotent once tools
                    # are discovered, so we clear the flag first to force it.
                    meta.pop("tools_discovered", None)
                    meta.pop("discovery_persona_id", None)
                    await manager._discover_per_persona_tools(qualified_name)
            else:
                if scope == "global":
                    await manager.remove_referrer_for_server(qualified_name, referrer)
                elif scope == "per_persona":
                    # Stop any running per-persona instances, unregister tools,
                    # clear discovery flag.
                    await _shutdown_all_instances_of_server(manager, qualified_name)
                    meta.pop("tools_discovered", None)
                    meta.pop("discovery_persona_id", None)
        except Exception as exc:
            LOGGER.warning(
                "MCP: addon toggle failed for '%s' (enabled=%s): %s",
                qualified_name,
                is_enabled,
                exc,
            )

    if not is_enabled:
        # After shutdown, drop meta entries so the next enable re-reads
        # mcp_servers.json fresh (picks up edits since the last enable).
        for qualified_name, meta in list(manager._server_meta.items()):
            if meta.get("addon_name") == addon_name:
                manager._server_meta.pop(qualified_name, None)
                LOGGER.debug(
                    "MCP: removed meta entry '%s' after addon '%s' disabled",
                    qualified_name,
                    addon_name,
                )


async def _shutdown_all_instances_of_server(
    manager: MCPClientManager,
    qualified_name: str,
) -> None:
    """Shutdown every instance of a qualified_name server, global or per-persona."""
    targets = [
        key for key in list(manager._connections.keys())
        if manager._qualified_from_instance_key(key) == qualified_name
    ]
    for instance_key in targets:
        await manager._shutdown_instance(instance_key)


def notify_addon_toggled_sync(addon_name: str, is_enabled: bool) -> None:
    """Thread-safe hook called from API routes when an addon is toggled.

    Schedules the MCP state update on the dedicated MCP event loop so
    that refcount arithmetic and subprocess lifecycle stay on one thread.
    Safe to call when MCP is not initialized (no-op).
    """
    manager = get_mcp_manager()
    if manager is None:
        LOGGER.debug(
            "notify_addon_toggled_sync: MCP manager not initialized, skipping "
            "(addon=%s, enabled=%s)",
            addon_name,
            is_enabled,
        )
        return
    loop = _loop
    if loop is None:
        LOGGER.warning(
            "notify_addon_toggled_sync: MCP event loop not available "
            "(addon=%s, enabled=%s)",
            addon_name,
            is_enabled,
        )
        return
    asyncio.run_coroutine_threadsafe(
        _apply_addon_toggle(manager, addon_name, is_enabled),
        loop,
    )


def initialize_mcp_sync() -> Optional[MCPClientManager]:
    loop = _ensure_loop_thread()
    future = asyncio.run_coroutine_threadsafe(initialize_mcp(), loop)
    return future.result()


def shutdown_mcp_sync() -> None:
    global _loop, _loop_thread
    if _loop is None:
        return
    future = asyncio.run_coroutine_threadsafe(shutdown_mcp(), _loop)
    future.result()
    _loop.call_soon_threadsafe(_loop.stop)
    if _loop_thread is not None:
        _loop_thread.join(timeout=5)
    _loop = None
    _loop_thread = None
