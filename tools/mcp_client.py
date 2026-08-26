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
- ``scope: "per_persona"`` servers are owned by the persona whose key opens
  them (§I). Nothing is connected at boot: the persona's own connection is
  opened at its Pulse head and its tool list is re-asked at every Beat head
  (``refresh_persona_tools``). The tool names that persona may see are
  recorded per (server, persona) in ``_persona_tool_names`` — a presentation
  filter derived from that live connection, never a substitute for it.
- Config placeholders (``${persona.addon.x.y}`` etc.) are resolved per
  instance, and the boundary check that no unresolved placeholder ever
  reaches an MCP server lives in :meth:`MCPServerConnection.connect` — the
  single point where config values leave this process.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import logging
import re
import threading
import time
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

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
# 同じ instance の起動 / 停止が進行中で今は使えない、という**一時的な状態**。
# 故障ではないので失敗記録 (_failed_instances) には載せない — backoff を焚くと
# 「混み合っただけ」で次の Pulse まで使えなくなる。
ERROR_CATEGORY_BUSY = "busy"
ERROR_CATEGORY_UNKNOWN = "unknown"

# --- Reconnect outcomes ---------------------------------------------------
#
# 「繋ぎ直した」「繋ごうとして失敗した」「繋ぎ直す相手がいなかった」は別の
# 結果である。 bool ひとつに潰すと最後のものが失敗の顔で表に出るが、
# per_persona のサーバーは起動時に接続を張らないので、**接続が無いのが常態**。
# 2026-08-25、再起動直後に再接続ボタンを押しても無反応に見えた件の根 —
# 「対象なし」が「失敗」と同じ False になり、その False を API が HTTP 200 に
# 包み、画面が HTTP ステータスしか見ていなかった (三段重ね)。
RECONNECT_RECONNECTED = "reconnected"
RECONNECT_FAILED = "failed"
RECONNECT_NO_INSTANCES = "no_instances"

_CATEGORY_JP = {
    ERROR_CATEGORY_RUNTIME_MISSING: "必要なランタイム（npx/uvx/python 等）が見つかりません",
    ERROR_CATEGORY_MISSING_CONFIG: "必須の設定値が未設定です",
    ERROR_CATEGORY_AUTH_FAILED: "サーバー側の認証に失敗しました",
    ERROR_CATEGORY_COMMAND_ERROR: "サーバーの起動コマンドエラー",
    ERROR_CATEGORY_NETWORK: "ネットワークエラー",
    ERROR_CATEGORY_PROCESS_CRASH: "サーバープロセスが異常終了しました",
    ERROR_CATEGORY_BUSY: "サーバーの起動・停止処理が進行中です",
    ERROR_CATEGORY_UNKNOWN: "不明なエラー",
}

# Exponential backoff bounds (seconds). Failed instances are not auto-retried
# until the deadline; callers may force-retry via reconnect/manual_stop.
_STARTUP_BACKOFF_INITIAL = 2.0
_STARTUP_BACKOFF_MAX = 60.0

_PLACEHOLDER_RE = re.compile(r"\$\{([^}]+)\}")


class MCPUnresolvedConfigError(RuntimeError):
    """接続しようとした config に未解決の ``${...}`` が残っていた。

    未解決のまま繋ぐと、``${persona.addon.x.y}`` という文字列そのものが
    remote MCP の Authorization ヘッダーや subprocess の env に載って外部へ
    出る。検査は「値が外へ出る場所」= :meth:`MCPServerConnection.connect` の
    直前に 1 箇所だけ置き (Codex レビュー 2026-08-10)、そこから本例外を投げる。
    起動側の入口で数え上げる形にすると、入口を一つ見落とすたびに漏れる
    (実際に reconnect 経路が漏れていた)。
    """

    def __init__(self, server_name: str, placeholders: List[str]) -> None:
        self.server_name = server_name
        self.placeholders = sorted(set(placeholders))
        super().__init__(
            "未解決のプレースホルダー: " + ", ".join(self.placeholders)
        )


class MCPInstanceBusyError(RuntimeError):
    """同じ instance_key が起動中 / 停止中で、今この操作はできない。

    MCP ループは単一スレッドだが接続の確立と切断には await があるため、その隙に
    別経路 (Pulse 頭の取得・再接続・遅延起動・アドオン無効化) が同じ instance を
    触りうる。待たずに正直に断ることで、二重起動と「旧接続の後片付けが新接続に
    適用される」事故を防ぐ (Codex レビュー 3 巡目)。per_persona は次の Pulse 頭が
    張り直すため、断られた回の実害は「その Pulse はツール無し」だけ。
    """

    def __init__(self, instance_key: str, reason: str) -> None:
        self.instance_key = instance_key
        self.reason = reason
        super().__init__(
            f"MCP instance '{instance_key}' is busy: {reason}"
        )


def _classify_error(exc: Exception) -> str:
    """Heuristically categorize an MCP startup exception."""
    if isinstance(exc, MCPUnresolvedConfigError):
        return ERROR_CATEGORY_MISSING_CONFIG
    if isinstance(exc, MCPInstanceBusyError):
        return ERROR_CATEGORY_BUSY
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


def _coerce_json_bool(
    raw: Dict[str, Any],
    key: str,
    absent_default: bool,
    server_name: str,
    where: str,
) -> bool:
    """Read one boolean out of an ``mcp_servers.json`` object.

    Absent → ``absent_default`` (what the declaration exists to express).
    Present but not a JSON boolean → ``False``, the closed side. Plain
    ``bool()`` would read the string ``"false"`` as ``True`` and open a tool
    the author meant to keep shut; **a typo must never widen access**.

    Every visibility / reachability flag read out of a server definition goes
    through here, so the ``bool("false")`` hole cannot reappear in one branch
    while being closed in another.
    """
    if key not in raw:
        return absent_default
    value = raw[key]
    if isinstance(value, bool):
        return value
    LOGGER.warning(
        "MCP server '%s': %s.%s must be true or false; got %r — "
        "treating it as false",
        server_name,
        where,
        key,
        value,
    )
    return False


def _normalize_spell_default(raw_value: Any, server_name: str) -> Optional[Dict[str, bool]]:
    """Normalize a server's ``spell_tools_default`` declaration.

    **Absent (``None``) keeps the historical allowlist behaviour**: a tool the
    server exposes but ``spell_tools`` does not name gets ``spell=False`` and
    stays unreachable from a persona. Addons that use ``spell_tools`` to *hide*
    raw tools behind native wrappers (saiverse-stackchan-addon) depend on this,
    so the default must never flip globally.

    **Present opts one server into following its own tool list**: tools that
    appear later become callable without anyone editing JSON. That is the whole
    point of writing the key, so ``spell`` defaults to ``True``; ``visible``
    defaults to ``False`` so a service shipping tools cannot grow every
    persona's head. Writing the key is the single opt-in gate — for a server
    that might add dangerous tools, simply omit it.

    Detail: docs/intent/mcp_addon_integration.md §H.
    """
    if raw_value is None:
        return None
    if raw_value is True:
        return {"spell": True, "visible": False}
    if not isinstance(raw_value, dict):
        LOGGER.warning(
            "MCP server '%s': 'spell_tools_default' must be an object or true; "
            "ignoring %s",
            server_name,
            type(raw_value).__name__,
        )
        return None
    return {
        "spell": _coerce_json_bool(
            raw_value, "spell", True, server_name, "spell_tools_default"
        ),
        "visible": _coerce_json_bool(
            raw_value, "visible", False, server_name, "spell_tools_default"
        ),
    }


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
    spell_default: Optional[Dict[str, bool]] = None,
) -> ToolSchema:
    description = getattr(tool_def, "description", "") or ""
    parameters = getattr(tool_def, "inputSchema", None)
    if not isinstance(parameters, dict):
        parameters = {"type": "object", "properties": {}}
    else:
        parameters = _strip_jsonschema_meta_keys(parameters)

    declared = tool_name in spell_config
    spell_options = spell_config.get(tool_name, {})
    if declared:
        # An explicitly listed tool keeps its historical meaning: it is a
        # spell, and it is visible unless the entry says otherwise.
        spell_enabled = True
        default_visible = True
    else:
        # ``is True`` rather than ``bool()``: spell_default arrives normalized
        # from _normalize_spell_default, but a caller passing a raw dict must
        # not be able to re-open the ``bool("false") == True`` hole.
        defaults = spell_default or {}
        spell_enabled = defaults.get("spell") is True
        default_visible = defaults.get("visible") is True

    display_name = spell_options.get("display_name") or spell_options.get("spell_display_name") or ""
    spell_visible = _coerce_json_bool(
        spell_options,
        "visible",
        default_visible,
        qualified_server_name,
        f"spell_tools[{tool_name}]",
    )
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
        spell=spell_enabled,
        spell_display_name=str(display_name) if display_name else "",
        spell_visible=spell_visible,
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


# Mirrors the MCP SDK's own remote defaults. Kept as literals instead of
# importing ``mcp.shared._httpx_utils``: that module is private, and the v2
# line reorganizes it. Coupling to a private path would turn an SDK upgrade
# into an ImportError at connect time; drifting a timeout by a few seconds is
# a far softer failure.
_REMOTE_CONNECT_TIMEOUT_SECONDS = 30.0
_REMOTE_SSE_READ_TIMEOUT_SECONDS = 300.0


def _mcp_http_client_no_redirect(
    headers: Optional[Dict[str, str]] = None,
    timeout: Any = None,
    auth: Any = None,
) -> Any:
    """httpx client for remote MCP that refuses to follow redirects.

    The SDK's own factory hard-codes ``follow_redirects=True``, and httpx only
    strips ``Authorization`` when a redirect crosses to a different origin —
    **any other credential header (``X-API-Key`` and friends) is replayed to
    whatever host the redirect names**. A remote MCP endpoint is a fixed URL,
    so following redirects buys nothing while risking handing a persona's API
    key to a third party.

    Used only when we actually send headers (see
    ``MCPServerConnection._connect_*``); header-less remote servers keep the
    SDK's default factory so their behaviour is unchanged. Mirrors
    ``mcp.shared._httpx_utils.create_mcp_http_client`` in every other respect.
    """
    import httpx

    kwargs: Dict[str, Any] = {"follow_redirects": False}
    if timeout is None:
        kwargs["timeout"] = httpx.Timeout(
            _REMOTE_CONNECT_TIMEOUT_SECONDS, read=_REMOTE_SSE_READ_TIMEOUT_SECONDS
        )
    else:
        kwargs["timeout"] = timeout
    if headers is not None:
        kwargs["headers"] = headers
    if auth is not None:
        kwargs["auth"] = auth
    return httpx.AsyncClient(**kwargs)


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

        # --- 境界の関所: 未解決 placeholder を外へ出さない ---
        # ここが config の値がこのプロセスから出る唯一の場所 (subprocess の env
        # / remote のヘッダー)。起動側・再接続側それぞれの入口で検査する形では、
        # 入口を一つ見落とした時点で漏れる (reconnect_server が実際に素通しだった
        # — Codex レビュー 2026-08-10)。呼び出し元は本例外を通常の接続失敗として
        # 扱えばよい (_classify_error が missing_config に分類する)。
        unresolved = _find_unresolved_placeholders(self.config)
        if unresolved:
            raise MCPUnresolvedConfigError(self.server_name, unresolved)

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

    def _http_headers(self) -> Optional[Dict[str, str]]:
        """Headers to send with remote (SSE / streamable HTTP) connections.

        stdio servers authenticate through ``env`` because they are our own
        subprocess. Remote servers have no such channel — the credential has
        to ride on the HTTP request itself (e.g. Elyth Remote MCP wants
        ``Authorization: Bearer <api key>``). ``self.config`` is already
        placeholder-resolved at this point, so ``${persona.addon.X.api_key}``
        inside a header value arrives here as the real key.

        Returns ``None`` when no headers are declared so we pass the SDK its
        own default rather than an empty dict.
        """
        raw = self.config.get("headers")
        if not isinstance(raw, dict):
            if raw is not None:
                LOGGER.warning(
                    "MCP server '%s': 'headers' must be an object; ignoring %s",
                    self.server_name,
                    type(raw).__name__,
                )
            return None
        headers = {str(key): str(value) for key, value in raw.items()}
        return headers or None

    def _remote_connect_kwargs(self) -> Dict[str, Any]:
        """Shared keyword arguments for the two remote transports.

        Pins a no-redirect httpx client **only when we send headers**: with a
        credential on the wire, following a redirect can hand it to another
        host (see ``_mcp_http_client_no_redirect``). Header-less remote servers
        keep the SDK's default factory, so their behaviour is unchanged.
        """
        headers = self._http_headers()
        kwargs: Dict[str, Any] = {"headers": headers}
        if headers is not None:
            kwargs["httpx_client_factory"] = _mcp_http_client_no_redirect
        return kwargs

    async def _connect_sse(self, stack: AsyncExitStack) -> None:
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        read_stream, write_stream = await stack.enter_async_context(
            sse_client(self.config["url"], **self._remote_connect_kwargs())
        )
        self.session = await stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )

    async def _connect_streamable_http(self, stack: AsyncExitStack) -> None:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        transport = await stack.enter_async_context(
            streamablehttp_client(
                self.config["url"], **self._remote_connect_kwargs()
            )
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
        # (qualified_name, persona_id) -> このペルソナ自身の接続から最後に
        # 取得したツール名集合 (per_persona スコープの「所属」)。一覧の真実は
        # サーバー側にあり、証言できるのは本人の鍵で張った生きた接続だけ
        # (docs/intent/mcp_addon_integration.md §I)。ここに無いペルソナは
        # 「このセッションでまだ一度も取得していない」= 近似判定へフォールバック。
        self._persona_tool_names: Dict[Tuple[str, str], frozenset] = {}
        # 取得が走行中の (qualified_name, persona_id)。sync 側の timeout で
        # 放置された取得と次の Pulse の取得が同じ接続開始を重ねないための
        # ガード (MCP ループ単一スレッド上でのみ触る)。
        self._persona_refresh_inflight: Set[Tuple[str, str]] = set()
        # 起動中 / 停止中の instance_key。MCP ループは単一スレッドだが、接続の
        # 確立と切断には await があるため、その隙に別経路 (Pulse 頭の取得・
        # 再接続・アドオン無効化・全停止) が同じ instance_key を触りうる。
        # ``_connections`` は「今繋がっているもの」しか表せないので、遷移中の
        # instance がどの経路からも見えず、二重起動と孤児 subprocess を生む
        # (Codex レビュー 3 巡目)。遷移中も見えるようにするための状態:
        #   _starting  … 起動処理が走っている (まだ _connections に入っていない)
        #   _stopping  … 停止処理が走っている (もう _connections から出ている)
        #   _stop_requested … 起動中に停止を頼まれた。起動が着地した瞬間に
        #                     自分で畳む (起動を途中でキャンセルはしない)
        self._starting: Set[str] = set()
        self._stopping: Set[str] = set()
        self._stop_requested: Set[str] = set()
        # (qualified_name, persona_id) -> 所属の版番号。意図的な無効化 (接続を
        # 殺した / 鍵が解決できなくなった / アドオン入れ直し) が版を上げ、走行中の
        # 取得は開始時に見た版と違っていたら結果を捨てる。これが無いと、sync 側が
        # timeout で見放した取得が「停止した後」に古い一覧を書き戻してツールを
        # 蘇生させる (fail-open 反転、Codex レビュー 2026-08-10)。
        self._persona_membership_version: Dict[Tuple[str, str], int] = {}
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
                # 起動時のツール登録は行わない (§I, 2026-08-10)。一覧は各ペルソナ
                # 自身の接続から Pulse 頭 / Beat 頭で取得する
                # (refresh_persona_tools)。「どれか 1 人の鍵を借りる」discovery は
                # 候補選びの複雑さと「代表者の鍵失効で全員のツールが消える」
                # 故障モードごと廃止した。
                LOGGER.info(
                    "MCP: per_persona server '%s' registered; tools are "
                    "fetched per-persona at pulse head (no boot discovery)",
                    qualified_name,
                )
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

    def has_per_persona_servers(self) -> bool:
        """per_persona スコープのサーバーが 1 つでも登録されているか (安価な前置き判定)。"""
        return any(
            str(meta.get("scope")) == "per_persona"
            for meta in self._server_meta.values()
        )

    async def refresh_persona_tools(
        self, persona_id: str, *, connect: bool = True,
    ) -> bool:
        """このペルソナの per_persona ツール一覧を、本人の生きた接続から取り直す。

        設計は docs/intent/mcp_addon_integration.md §I。一覧の真実はサーバー側に
        あり、証言できるのは本人の鍵で張った生きた接続だけ — 起動時に代表者の
        鍵を借りて一括登録する discovery は廃止された。

        - ``connect=True`` (Pulse 頭): 鍵が解決できるサーバーへ接続を張り
          (無ければ起動し)、一覧を取得して登録・所属を更新する。
        - ``connect=False`` (Beat 頭): 既に生きている接続の上でだけ一覧を
          聞き直す。新しい接続は張らない — 接続は Pulse 単位、一覧の
          取り直しは Beat 単位。

        Returns:
            いずれかのサーバーでこのペルソナのツール所属が変わったら True。
            呼び出し元はこれを合図に spell_list の検知 (差分 → 知覚バッファ)
            を走らせる。
        """
        if not persona_id:
            return False
        changed = False
        for qualified_name, meta in list(self._server_meta.items()):
            if str(meta.get("scope")) != "per_persona":
                continue
            try:
                if await self._refresh_persona_server_tools(
                    qualified_name, persona_id, meta, connect=connect,
                ):
                    changed = True
            except Exception:
                LOGGER.exception(
                    "MCP: per-persona tool refresh failed for '%s' (persona=%s)",
                    qualified_name,
                    persona_id,
                )
        return changed

    async def _refresh_persona_server_tools(
        self,
        qualified_name: str,
        persona_id: str,
        meta: Dict[str, Any],
        *,
        connect: bool,
    ) -> bool:
        instance_key = _make_instance_key(qualified_name, persona_id)
        membership_key = (qualified_name, persona_id)
        if membership_key in self._persona_refresh_inflight:
            # 前回の取得 (sync 側が timeout で見放したもの等) がまだ MCP ループ
            # 上で走っている。接続開始を重ねると同じ instance_key に接続が
            # 二重生成されるので、この回は見送る。
            LOGGER.debug(
                "MCP: per-persona refresh already in flight for '%s' (persona=%s)",
                qualified_name,
                persona_id,
            )
            return False
        self._persona_refresh_inflight.add(membership_key)
        try:
            return await self._refresh_persona_server_tools_locked(
                qualified_name, persona_id, meta,
                instance_key=instance_key,
                membership_key=membership_key,
                connect=connect,
            )
        finally:
            self._persona_refresh_inflight.discard(membership_key)

    async def _refresh_persona_server_tools_locked(
        self,
        qualified_name: str,
        persona_id: str,
        meta: Dict[str, Any],
        *,
        instance_key: str,
        membership_key: Tuple[str, str],
        connect: bool,
    ) -> bool:
        # 開始時の版。走行中に意図的な無効化 (停止・鍵の消滅・アドオン入れ直し)
        # が入ったら、この取得の結果は「もう古い世界の話」なので書き込まない。
        version_at_start = self._persona_membership_version.get(membership_key, 0)
        connection = self._connections.get(instance_key)
        alive = connection is not None and connection.connected

        if not alive:
            if not connect:
                # Beat 頭は「生きた接続に聞き直す」だけ。接続を作るのは
                # Pulse 頭の仕事 (§I) — ここで張り直すと、未設定ペルソナの
                # resolve (DB 引き) が Beat 頻度で走ってしまう。
                if connection is not None:
                    # 接続オブジェクトはあるのに connected でない = ツール
                    # コールの失敗等でこの Pulse の途中に死んだ接続。証言者が
                    # いない以上、所属は「確認済み・利用不可」に倒す (提示だけ
                    # 残ると、死んだ接続のツールが真実の顔で並び続ける)。次の
                    # Pulse 頭の取得が張り直して復元する。
                    LOGGER.info(
                        "MCP: connection for '%s' (persona=%s) is dead at the "
                        "beat head; dropping its tool membership until the "
                        "next pulse head reconnects",
                        qualified_name,
                        persona_id,
                    )
                    return self._mark_persona_tools_unavailable(membership_key)
                return False

            from tools.mcp_config import resolve_config_placeholders

            raw_config = meta["raw_config"]
            try:
                resolved = resolve_config_placeholders(
                    raw_config, persona_id=persona_id
                )
            except Exception:
                LOGGER.warning(
                    "MCP: placeholder resolution failed for '%s' (persona=%s); "
                    "no tools this pulse",
                    qualified_name,
                    persona_id,
                    exc_info=True,
                )
                return self._mark_persona_tools_unavailable(membership_key)

            if _find_unresolved_placeholders(resolved):
                # 鍵未設定は普通の状態であって故障ではない — 失敗一覧には
                # 載せない (充足状況はアドオン設定画面が示す)。このペルソナに
                # ツールが無いのは正直な状態 (§I 要請 3)。
                return self._mark_persona_tools_unavailable(membership_key)

            if self._is_in_backoff(instance_key):
                return self._mark_persona_tools_unavailable(membership_key)

            try:
                await self._start_instance(
                    instance_key, qualified_name, persona_id=persona_id
                )
            except Exception:
                # _start_instance が失敗記録 + backoff を済ませている
                # (= get_failed_instances / UI に出る)。
                return self._mark_persona_tools_unavailable(membership_key)
            # Lazy-start (wrapper 経路) と同じ自己参照。refcount=0 での停止は
            # addon disable / manual stop 側の remove_reference が担う。
            self._add_reference(instance_key, f"persona:{persona_id}")
            self._clear_failure(instance_key)
            connection = self._connections.get(instance_key)
            if connection is None or not connection.connected:
                return self._mark_persona_tools_unavailable(membership_key)
            # connect() 内の一覧取得 (tools) をそのまま使う。
        else:
            # 生きた接続の上で一覧だけ聞き直す。失敗は「一覧が変わった」証拠
            # ではないので、所属は保って静かに続行する (§I: 失敗 ≠ 消滅)。
            try:
                await connection._discover_tools()
            except Exception:
                LOGGER.warning(
                    "MCP: tools/list refresh failed on live connection '%s' "
                    "(persona=%s); keeping the previous list",
                    qualified_name,
                    persona_id,
                    exc_info=True,
                )
                return False

        # 版の検査は **登録より先**。await の間に無効化 (停止・鍵消失・アドオン
        # 入れ直し) が入っていたら、この取得は「もう古い世界の話」なので所属も
        # 登録もしない。登録を先にやると、一覧は捨てても TOOL_REGISTRY /
        # SPELL_TOOL_SCHEMAS へ足した wrapper は残り、未評価の別ペルソナが
        # config 近似でそれを見てしまう (Codex レビュー 2026-08-10)。
        if self._persona_membership_version.get(membership_key, 0) != version_at_start:
            LOGGER.info(
                "MCP: discarding tool list for '%s' (persona=%s) — membership "
                "was invalidated while the refresh was running",
                qualified_name,
                persona_id,
            )
            return False

        # 新規ツールの wrapper 登録 (登録済みの名前は skip される)。
        self._register_tools(connection, qualified_name, instance_key, persona_id)

        new_names = frozenset(
            str(getattr(tool, "name", ""))
            for tool in connection.tools
            if getattr(tool, "name", None)
        )
        old_names = self._persona_tool_names.get(membership_key)
        self._persona_tool_names[membership_key] = new_names
        if old_names is None:
            # このセッション初取得。ツールが 1 つでもあれば「見える集合が
            # 変わった」ので検知に値する (head 側の近似からの置き換わり検出)。
            return bool(new_names)
        return old_names != new_names

    def presume_persona_tools_unavailable(self, persona_id: str) -> None:
        """このペルソナの「まだ一度も取得していない」所属を空集合へ倒す。

        Pulse 頭の取得を投げる直前に呼ぶ。取得が timeout で見放されたとき、
        所属が未評価 (None) のままだと ``is_tool_available_for_persona`` が
        config 近似 (= 鍵が解決できれば使える顔) へ戻り、一度も繋げていない
        ツールを提示してしまう。§I の「取得できなかった Pulse はツール無し」を
        守るため、取得の結果が出るまでは空集合を置く (fail-closed)。

        版番号は上げない — これは推定であって無効化ではなく、走行中の取得が
        直後に本物の一覧を書き込むのは正しい自己修復だから。既に実績のある
        所属 (空集合含む) には触らない (setdefault)。
        """
        if not persona_id:
            return
        for qualified_name, meta in list(self._server_meta.items()):
            if str(meta.get("scope")) != "per_persona":
                continue
            self._persona_tool_names.setdefault(
                (qualified_name, str(persona_id)), frozenset()
            )

    def _mark_persona_tools_unavailable(
        self, membership_key: Tuple[str, str]
    ) -> bool:
        """所属を「確認済み・利用不可 (空集合)」にする。

        pop で消してはいけない — 消すと is_tool_available_for_persona が
        「まだ一度も取得していない」と同じ config 近似へフォールバックし、
        鍵が解決できる限りツールが使える顔で現れる (fail-open 反転、
        Qwen レビュー 2026-08-10)。§I の「その Pulse はツール無し」を守るには
        『評価した結果、使えない』を空集合として区別する必要がある。
        完全に忘れて近似へ戻すのはアドオン入れ直し (_purge_persona_tools) だけ。

        Returns 変化あり (可視状態が変わりうる) かどうか。
        """
        old = self._persona_tool_names.get(membership_key)
        self._persona_tool_names[membership_key] = frozenset()
        self._bump_membership_version(membership_key)
        # None (未取得 = 近似が True を返していたかもしれない) → 空集合も
        # 可視状態の変化になりうるので変化ありとして返す。空→空だけが不変。
        return old is None or bool(old)

    def _bump_membership_version(self, membership_key: Tuple[str, str]) -> int:
        """所属の版を進める (走行中の取得の書き込みを無効にする)。"""
        version = self._persona_membership_version.get(membership_key, 0) + 1
        self._persona_membership_version[membership_key] = version
        return version

    def _purge_persona_tools(self, qualified_name: str) -> None:
        """qualified server の全ペルソナ所属を白紙に戻す (addon toggle 用)。"""
        for key in [
            k for k in self._persona_tool_names if k[0] == qualified_name
        ]:
            self._persona_tool_names.pop(key, None)
            # 走行中の取得が白紙化を無かったことにしないよう版を進める。
            self._bump_membership_version(key)

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

        遷移中の重なりは :py:class:`MCPInstanceBusyError` で拒む (既に繋がって
        いる場合は何もしない)。接続の確立には await があるため、拒まないと同じ
        instance_key に接続が二重生成され、片方が孤児 subprocess として残る。
        """
        from tools.mcp_config import resolve_config_placeholders

        meta = self._server_meta.get(qualified_name)
        if meta is None:
            raise ValueError(f"Unknown MCP server '{qualified_name}'")

        existing = self._connections.get(instance_key)
        if existing is not None and existing.connected:
            # 別経路が先に張り終えていた (Pulse 頭 と wrapper の遅延起動が
            # 重なる等)。二重に張らない。
            return
        if instance_key in self._starting:
            raise MCPInstanceBusyError(instance_key, "起動処理が進行中です")
        if instance_key in self._stopping:
            # 停止の途中に立て直すと、旧接続の後片付け (参照・所属の無効化) が
            # 新しい接続に適用されてしまう。停止が終わるまで待たず正直に断る —
            # per_persona なら次の Pulse 頭が張り直す。
            raise MCPInstanceBusyError(instance_key, "停止処理が進行中です")

        if instance_context is None:
            instance_context = self._instance_contexts.get(instance_key)

        raw_config = meta["raw_config"]
        resolved = resolve_config_placeholders(
            raw_config, persona_id=persona_id, instance_context=instance_context
        )

        # 未解決 placeholder の検査は connect() の関所が持つ (missing_config に
        # 分類されて下の except 分岐が失敗記録 + backoff を済ませる)。ここで
        # 二重に数えると、検査の条件が二箇所で食い違う余地を作る。
        connection = MCPServerConnection(
            qualified_name, resolved, instance_key=instance_key
        )
        self._starting.add(instance_key)
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
        finally:
            self._starting.discard(instance_key)
            # 旗はここで必ず降ろす。接続が失敗した回に残すと、次に成功した
            # 起動が着地した瞬間に自壊する (要求は「あの起動」に向いたもの)。
            stop_requested = instance_key in self._stop_requested
            self._stop_requested.discard(instance_key)

        if stop_requested:
            # 起動中に停止を頼まれていた (アドオン無効化・手動停止・全停止)。
            # 起動は途中でキャンセルしないので、着地した瞬間に自分で畳む。
            # これをしないと、停止側は _connections を見て「無いから対象外」と
            # 判断しているため、subprocess と wrapper が残り続ける。
            LOGGER.info(
                "MCP: instance '%s' was asked to stop while starting; "
                "shutting it down right after connect", instance_key,
            )
            try:
                await connection.disconnect()
            except Exception as exc:
                LOGGER.debug(
                    "MCP: failed to disconnect just-started instance '%s': %s",
                    instance_key, exc,
                )
            raise MCPInstanceBusyError(
                instance_key, "起動中に停止が要求されました"
            )

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
            # 本人の生きた接続から取得済みの所属があればそれが真実 (§I)。
            # 鍵が解決できても接続できなかった / サーバー側でツールが消えた、
            # を config 近似は表せない。
            membership = self._persona_tool_names.get(
                (qualified, str(persona_id))
            )
            if membership is not None:
                bare_name = meta_info.get("tool_name") or ""
                return bare_name in membership
            # このセッションでまだ一度も取得していない (Pulse 前の UI 列挙等)
            # 場合だけ、従来の「config が解決できるか」で近似する。
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
        spell_default = _normalize_spell_default(
            connection.config.get("spell_tools_default"), qualified_name
        )
        scope = self._server_meta.get(qualified_name, {}).get("scope", "global")
        # Tools this server exposes that no one named in spell_tools, yet which
        # became callable through spell_tools_default. Recorded so the set can
        # be reconstructed after the fact — this is a record, NOT a safeguard:
        # nobody reads logs in time to stop anything. The only gate is whether
        # spell_tools_default is declared for this server at all.
        auto_enabled: List[str] = []

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
                spell_default,
            )
            if schema.spell and tool_name not in spell_config:
                auto_enabled.append(tool_name)
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

        if auto_enabled:
            LOGGER.info(
                "MCP server '%s': %d tool(s) not listed in spell_tools became "
                "callable via spell_tools_default: %s",
                qualified_name,
                len(auto_enabled),
                ", ".join(sorted(auto_enabled)),
            )

    # -- Shutdown --------------------------------------------------------

    async def shutdown_all(self) -> None:
        from tools import unregister_external_tool

        for name in list(self._registered_tools):
            unregister_external_tool(name)
            self._registered_tools.pop(name, None)

        # 起動中の instance も対象に含める — 接続が _connections に入るのは接続
        # 完了後なので、ここで漏らすとプロセス終了後に subprocess が残る
        # (Codex レビュー 3 巡目)。_shutdown_instance が起動中には停止要求を置き、
        # 起動側が着地時に自分で畳む。
        for instance_key in list(self._connections.keys()) + list(self._starting):
            await self._shutdown_instance(instance_key, force=True)

        self._connections.clear()
        self._refs.clear()
        self._server_meta.clear()

    async def _shutdown_instance(
        self, instance_key: str, *, force: bool = False, recoverable: bool = False,
    ) -> None:
        """この instance の生きた接続を落とし、接続から導かれる帳簿を畳む。

        常にやるのは「接続が無くなったら真になること」だけ — 登録簿からツールを
        外し、所属 (派生状態) を無効化する。接続が既に無い instance_key でも
        帳簿の畳みは走る (呼ばれた意味は「この instance は生きていない」であって、
        辞書に居るかではない)。

        **``recoverable`` が畳む理由を運ぶ。** 片付けの範囲は「なぜ畳むのか」で
        変わり、一つの関数で両方を表すと必ずどちらかが壊れる
        (Codex レビュー 2026-08-10 で実際に壊した):

        - ``recoverable=False`` (既定、恒久的な撤去): 参照も落とす。アドオン無効化・
          手動停止・refcount 0 のように「もう在るべきでない」と決まった場合。
        - ``recoverable=True`` (一時的な失敗): 参照を残す。設定の一時的な欠落や
          再接続の失敗で落とした instance は「まだ在るべきもの」なので、
          参照を消すと refcount 0 の invariant を壊したまま復旧させることになり、
          再接続ボタン (:py:meth:`reconnect_server`) からも見えなくなる。

        どちらの場合も ``${instance.*}`` の context は忘れない。context を捨てるのは
        「この instance はもう使わない」と決めた :py:meth:`stop_instance` の責務で、
        そこが明示的に pop している。ここで一緒に捨てると、設定が戻っても名前付き
        instance が再登録なしには復旧できなくなる。

        **起動中の instance にも届く。** 接続が ``_connections`` に入るのは接続完了後
        なので、起動の await 中に停止が来ると「対象に無い」と判断されて subprocess が
        残る。起動中なら停止要求を置いて (``_stop_requested``)、起動が着地した瞬間に
        起動側が自分で畳む (Codex レビュー 3 巡目)。
        """
        if instance_key in self._starting:
            self._stop_requested.add(instance_key)
            LOGGER.info(
                "MCP: stop requested for '%s' while it is still starting; "
                "it will be shut down as soon as the connect lands", instance_key,
            )
        self._stopping.add(instance_key)
        try:
            await self._shutdown_instance_locked(
                instance_key, force=force, recoverable=recoverable,
            )
        finally:
            self._stopping.discard(instance_key)

    async def _shutdown_instance_locked(
        self, instance_key: str, *, force: bool, recoverable: bool,
    ) -> None:
        """:py:meth:`_shutdown_instance` の本体 (``_stopping`` 保持下で実行される)。"""
        connection = self._connections.pop(instance_key, None)
        if connection is not None:
            # in-flight のツールコールが disconnect のキャンセルで失敗しても、
            # call_tool の auto-reconnect でこの接続を蘇生させない (孤児 subprocess
            # 防止)。disconnect が例外を投げる前に立てておく必要がある。
            connection._closed = True
        if not force:
            await self._unregister_instance_tools(instance_key)
        if connection is not None:
            try:
                await connection.disconnect()
            except Exception as exc:
                LOGGER.debug(
                    "MCP: failed to disconnect instance '%s': %s", instance_key, exc
                )
        if not recoverable:
            self._refs.pop(instance_key, None)
        # 所属 (per_persona ツール一覧) は接続から導かれる派生状態なので、
        # 接続を殺すこの関所が道連れで無効化する。残すと「切れた接続の一覧」が
        # 真実の顔で is_tool_available_for_persona から出続ける。次の Pulse 頭の
        # 取得が復元するため、生きた運用では通知のばたつきは起きない
        # (取得 → 検知の順序が Pulse 頭で保証されている)。
        persona_id = self._persona_id_from_instance_key(instance_key)
        if persona_id:
            qualified_name = self._qualified_from_instance_key(instance_key)
            if qualified_name:
                self._mark_persona_tools_unavailable((qualified_name, persona_id))

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

    async def reconnect_server(self, qualified_name: str) -> str:
        """Reconnect all instances of a given qualified server name.

        Returns one of ``RECONNECT_RECONNECTED`` / ``RECONNECT_FAILED`` /
        ``RECONNECT_NO_INSTANCES``. The last one is **not** a failure: a
        per_persona server has no instances until a persona's Pulse head
        opens one, so "nothing to reconnect" is its normal resting state.

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

        # 対象は「今繋がっている instance」+「在るべきなのに落ちている instance」。
        # 後者を入れないと、一度落ちた instance は再接続ボタンから見えなくなる
        # (matching が作れず即 False) — 落とした側が復旧の道を塞ぐ形になる。
        #
        # 在るべきかの根拠は **参照 (_refs) だけ** にする。参照は「まだ誰かが
        # この instance を要求している」= 望まれている状態の記録であり、一時的な
        # 失敗で畳むとき (recoverable) に残すのはまさにこのため。失敗記録
        # (_failed_instances) を根拠に加えると「誰も要求していない instance を
        # 復活させる」道が開き、しかも参照が無いので refcount 0 の live instance
        # ができる (Codex レビュー 3 巡目)。歯止めの条件は「望まれているか」という
        # 目的から導く — 「直近で落ちた」という種類から導くと的を外す。
        matching: List[str] = []
        seen: Set[str] = set()
        for key in list(self._connections) + list(self._refs):
            if key in seen:
                continue
            if self._qualified_from_instance_key(key) != qualified_name:
                continue
            seen.add(key)
            matching.append(key)
        if not matching:
            return RECONNECT_NO_INSTANCES

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
                # 落ちている instance は「繋ぎ直す」のではなく「立て直す」。
                # 設定の再解決も未解決検査も _start_instance → connect() の
                # 関所が済ませる (失敗記録と backoff も同経路)。
                try:
                    await self._start_instance(
                        instance_key,
                        qualified_name,
                        persona_id=self._persona_id_from_instance_key(instance_key),
                    )
                except Exception:
                    success = False
                    continue
                self._clear_failure(instance_key)
                LOGGER.info(
                    "MCP: restarted down instance '%s' via reconnect", instance_key,
                )
                continue

            # Resolve persona-specific placeholders on top of the freshly
            # AddonConfig-interpolated raw_config.
            #
            # 解決に失敗した回は、この instance を触らずに送る (旧 config で
            # 繋ぎ直さない)。再接続の目的は「現在の設定値を subprocess / ヘッダー
            # へ押し込む」ことで、現在の値が分からないなら kill + respawn は
            # 目的を果たさないまま生きている接続を殺すだけになる。今動いている
            # 接続をそのまま残すのが最も無害 (成功扱いにはしない)。
            if raw_config is not None:
                persona_id = self._persona_id_from_instance_key(instance_key)
                instance_context = self._instance_contexts.get(instance_key)
                try:
                    new_config = resolve_config_placeholders(
                        raw_config,
                        persona_id=persona_id,
                        instance_context=instance_context,
                    )
                except Exception as exc:
                    LOGGER.warning(
                        "MCP: placeholder re-resolution failed for '%s'; "
                        "leaving the current connection as-is (not reconnecting "
                        "with stale values): %s",
                        instance_key,
                        exc,
                    )
                    success = False
                    continue
                # 鍵が消された等で解決しきれなかった場合は、旧鍵の入った
                # 現行接続も畳む。「使えなくした」という利用者の意図に対し、
                # 起動時に焼き込まれた旧鍵で喋り続ける接続を残す方が有害
                # (connect() の関所は未解決の送信を止めるが、既存接続は
                # 止めない — 派生状態の所属も _shutdown_instance が畳む)。
                unresolved = _find_unresolved_placeholders(new_config)
                if unresolved:
                    category = ERROR_CATEGORY_MISSING_CONFIG
                    user_msg = _build_user_error_message(
                        qualified_name,
                        meta.get("addon_name") if meta else None,
                        category,
                        "未解決のプレースホルダー: "
                        + ", ".join(sorted(set(unresolved))),
                    )
                    self._record_failure(instance_key, category, user_msg)
                    LOGGER.warning(
                        "MCP: reconnect of '%s' aborted (unresolved config); "
                        "shutting the instance down instead: %s",
                        instance_key,
                        user_msg,
                    )
                    # recoverable: 設定不足は「もう使わない」という決定ではない。
                    # 鍵を入れ直せば per_persona は次の Pulse 頭、global / 名前付きは
                    # 再接続ボタンで戻れる道を残す。
                    await self._shutdown_instance(instance_key, recoverable=True)
                    success = False
                    continue
                connection.config = new_config

            await self._unregister_instance_tools(instance_key)
            try:
                await connection.disconnect()
                await connection.connect()
                persona_id = self._persona_id_from_instance_key(instance_key)
                self._register_tools(connection, qualified_name, instance_key, persona_id)
            except Exception as exc:
                # 再接続に失敗した instance を掴んだままにしない。掴んだままだと
                # (a) ツール wrapper の遅延起動は「_connections にキーがあるか」
                # しか見ないので、切れた接続へ送り続けて必ず失敗し、
                # (b) 失敗記録が付かないので UI の失敗一覧にも出ず、backoff も
                # 効かない。畳めば per_persona は次の Pulse 頭が、名前付き
                # instance は保持された context 付きで遅延起動 / retry が
                # やり直せる (Codex レビュー 2026-08-10 high)。
                # 構造的な統合 (再接続を _start_instance の共通経路へ寄せる) は
                # docs/issues/mcp_remote_connection_recovery_gaps.md の 1。
                category = _classify_error(exc)
                user_msg = _build_user_error_message(
                    qualified_name,
                    meta.get("addon_name") if meta else None,
                    category,
                    str(exc),
                )
                self._record_failure(instance_key, category, user_msg, exc)
                LOGGER.warning(
                    "MCP: reconnect failed for instance '%s' (%s); dropping the "
                    "dead connection so it can be restarted: %s",
                    instance_key,
                    category,
                    exc,
                )
                # recoverable: 参照を残す (「まだ在るべきもの」)。参照まで落とすと
                # refcount 0 の invariant を壊したまま復旧させることになり、
                # 再接続ボタンからも見えなくなる。
                await self._shutdown_instance(instance_key, recoverable=True)
                success = False
        # ready は「このサーバーを今から呼べるか」の合図なので、全 instance の
        # 成功ではなく **1 つでも生きているか** で立てる。複数 instance (ペルソナ
        # ごと / vessel ごと) のうち 1 つが失敗しただけで、生きている instance の
        # 購読者 (avatar_loader 等) を待たせ続ける理由はない。戻り値は従来どおり
        # 「全 instance が繋ぎ直せたか」を意味する (呼び出し元の報告用)。
        any_alive = False
        for instance_key in matching:
            conn = self._connections.get(instance_key)
            if conn is not None and conn.connected:
                any_alive = True
                break
        if any_alive:
            self._fire_server_ready(qualified_name)
        return RECONNECT_RECONNECTED if success else RECONNECT_FAILED

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

        ⚠️ **per_persona instances come back on that persona's next Pulse
        head**, not on the next tool call: the Pulse head opens the connection
        it needs to read the live tool list (§I). While the persona is active,
        a manual stop therefore lasts minutes at most — use *addon disable* to
        keep it down. See
        ``docs/issues/mcp_per_persona_manual_stop_revives.md`` (recorded as
        low priority: the stop button is rarely used and nothing is sent
        externally without a resolvable key).

        global instances stay down until a referrer is re-added (or the
        process restarts); named instances (``:instance:``) can be restarted
        by a tool call or retry because their ``${instance.*}`` context is
        kept — only :py:meth:`stop_instance` forgets it.

        起動中 (まだ接続が登録されていない) instance も止められる: 停止要求を
        置いて、起動が着地した瞬間に畳ませる。
        """
        if instance_key not in self._connections and instance_key not in self._starting:
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
        # Also include per_persona servers that have no instance yet: they are
        # configured but nobody's connection is open (nothing connects at boot —
        # 設計 I). Without this row the UI would show no trace of a configured
        # server at all.
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
                "note": (
                    "per_persona scope: each persona opens its own connection at "
                    "its next Pulse head (nothing is connected at boot)"
                ),
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
        # 生きているかまで見る。キーが残っているだけの死んだ接続 (``call_tool`` が
        # 失敗して ``disconnect()`` した後も ``_connections`` からは消えない) を
        # 「起動済み」と読むと、繋がっていないサーバーを成功として返してしまう。
        # 兄弟の ``register_instance`` は最初からこの判定を持っている。
        existing = self._connections.get(instance_key)
        if (existing is None or not existing.connected) and scope == "global":
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
    For per_persona scope, the active persona is read from ``tools.context``
    and used to resolve (or lazy-start) that persona's own instance —
    execution always goes through the caller's own key, never a borrowed one
    (設計 I). Normally the instance is already open because the Pulse head
    opened it; the lazy start here covers calls outside that path.
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


def refresh_persona_tools_sync(
    persona_id: str, *, connect: bool = True, timeout: float = 15.0,
) -> bool:
    """SEA (同期スレッド) から Pulse/Beat 頭のツール一覧取得を呼ぶための橋。

    MCP 専用ループに coroutine を投げて完了を待つ。設計:
    docs/intent/mcp_addon_integration.md §I (Pulse 頭 connect=True /
    Beat 頭 connect=False)。

    未初期化・per_persona サーバー不在・timeout 超過・例外はすべて
    False (= 変化なし扱い) — 取得の失敗で Pulse を止めない。timeout で
    抜けた場合も coroutine 自体は MCP ループ上で走り続け、完了すれば
    所属は更新される (次の Beat / Pulse の取得が拾う)。

    ``connect=True`` の回は投げる前に「未評価の所属を空集合へ倒す」
    (presume_persona_tools_unavailable)。timeout で結果を受け取れなかった
    ときに config 近似へ戻ってしまうと、一度も繋げていないツールを提示する
    ことになる (§I: 取得できなかった Pulse はツール無し)。
    """
    manager = get_mcp_manager()
    if manager is None or _loop is None or not persona_id:
        return False
    if not manager.has_per_persona_servers():
        return False
    if connect:
        try:
            manager.presume_persona_tools_unavailable(persona_id)
        except Exception:
            LOGGER.exception(
                "MCP: failed to presume per-persona tool unavailability "
                "(persona=%s)", persona_id,
            )
    try:
        current_loop = None
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            pass
        if current_loop is _loop:
            LOGGER.warning(
                "refresh_persona_tools_sync called on the MCP loop itself; "
                "skipping to avoid deadlock",
            )
            return False
        future = asyncio.run_coroutine_threadsafe(
            manager.refresh_persona_tools(persona_id, connect=connect), _loop,
        )
        return bool(future.result(timeout=timeout))
    except concurrent.futures.TimeoutError:
        LOGGER.warning(
            "MCP: per-persona tool refresh timed out after %.1fs "
            "(persona=%s, connect=%s); continuing without the result",
            timeout,
            persona_id,
            connect,
        )
        return False
    except Exception:
        LOGGER.exception(
            "MCP: refresh_persona_tools_sync failed (persona=%s)", persona_id,
        )
        return False


async def initialize_mcp() -> Optional[MCPClientManager]:
    global _manager
    if _manager is not None:
        return _manager

    manager = MCPClientManager()
    await manager.start_all()
    # Return the manager even if no tools were registered yet — per_persona
    # scoped servers register nothing at boot; each persona's tools appear
    # when its own connection is opened at its Pulse head (設計 I).
    _manager = manager
    return _manager


async def shutdown_mcp() -> None:
    global _manager
    if _manager is None:
        return
    await _manager.shutdown_all()
    _manager = None


async def reconnect_mcp_server(server_name: str) -> str:
    """Reconnect one qualified server. Returns a ``RECONNECT_*`` outcome."""
    manager = get_mcp_manager()
    if manager is None:
        return RECONNECT_FAILED
    return await run_on_mcp_loop(manager.reconnect_server(server_name))


async def reconnect_addon_mcp_servers(addon_name: str) -> Dict[str, str]:
    """Reconnect all MCP servers declared by the given addon.

    Used by the addon config update API to push AddonConfig changes
    (e.g. ``vision_host`` after a Wi-Fi switch, ``master_token`` after a
    re-pairing) into the running subprocess env. MCP subprocesses pin
    their env at spawn time, so the only way to apply DB changes is
    kill + respawn via ``reconnect_server``.

    Returns a ``{qualified_name: RECONNECT_* outcome}`` map. Empty dict if the
    addon has no MCP servers or MCP isn't initialized. Note that
    ``RECONNECT_NO_INSTANCES`` is the expected outcome for a per_persona
    server that no persona has opened yet — callers must not report it as a
    failure.
    """
    manager = get_mcp_manager()
    if manager is None:
        return {}
    server_names = [
        qname
        for qname, meta in manager._server_meta.items()
        if meta.get("addon_name") == addon_name
    ]
    results: Dict[str, str] = {}
    for name in server_names:
        try:
            results[name] = await run_on_mcp_loop(manager.reconnect_server(name))
        except Exception as exc:
            LOGGER.warning(
                "MCP: reconnect raised for addon '%s' server '%s': %s",
                addon_name, name, exc,
            )
            results[name] = RECONNECT_FAILED
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
    from tools.mcp_config import (
        _interpolate_value,
        _load_config_file,
        is_server_enabled,
    )

    config_path = EXPANSION_DATA_DIR / addon_name / "mcp_servers.json"
    if not config_path.exists():
        return []

    added: List[str] = []
    raw = _load_config_file(config_path)
    for server_name, cfg in raw.items():
        qualified_name = f"{addon_name}__{server_name}"
        if qualified_name in manager._server_meta:
            continue
        # Same strict-boolean judgement as the startup load — see
        # ``is_server_enabled``. Duplicating the test here once made
        # ``"enabled": "false"`` mean off at boot and on after re-enabling.
        if not is_server_enabled(qualified_name, cfg):
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

    # 個々のサーバーの失敗を集める。ここで握って正常終了すると、呼び出し元の
    # Future は成功として解決し、API は「反映は確定した」と答える — 実際には
    # 接続が残っているのに、画面の一覧はそれを最終形として表示する。
    failures: list[tuple[str, Exception]] = []

    for qualified_name, meta in targets:
        scope = meta.get("scope", "global")
        try:
            if is_enabled:
                if scope == "global":
                    instance_key = await manager.add_referrer_for_server(
                        qualified_name, referrer,
                    )
                    if instance_key is None:
                        # 起動に失敗しても例外ではなく ``None`` が返る (中で捕捉して
                        # いる)。**失敗の伝え方が二通りある**ので、例外だけを集めて
                        # いると、接続も参照も無いのに「有効化済み」と画面に出る。
                        raise RuntimeError(
                            f"could not start or attach MCP server "
                            f"'{qualified_name}'"
                        )
                elif scope == "per_persona":
                    # §I: 再有効化でも一括 discovery は行わない。ツールは各
                    # ペルソナの次の Pulse 頭で本人の鍵により取得される。
                    # 旧セッションの所属記録だけ白紙に戻す。
                    manager._purge_persona_tools(qualified_name)
            else:
                if scope == "global":
                    await manager.remove_referrer_for_server(qualified_name, referrer)
                elif scope == "per_persona":
                    # Stop any running per-persona instances and unregister
                    # tools; forget per-persona tool membership.
                    await _shutdown_all_instances_of_server(manager, qualified_name)
                    manager._purge_persona_tools(qualified_name)
        except Exception as exc:
            LOGGER.warning(
                "MCP: addon toggle failed for '%s' (enabled=%s): %s",
                qualified_name,
                is_enabled,
                exc,
            )
            failures.append((qualified_name, exc))

    failed_names = {name for name, _ in failures}

    if not is_enabled:
        # After shutdown, drop meta entries so the next enable re-reads
        # mcp_servers.json fresh (picks up edits since the last enable).
        for qualified_name, meta in list(manager._server_meta.items()):
            if meta.get("addon_name") != addon_name:
                continue
            if qualified_name in failed_names:
                # 畳めなかったサーバーの記録まで消すと、接続や subprocess が
                # 残ったまま「知らないサーバー」になる。次に有効化したとき
                # JSON から読み直して二重に起動しうるので、記録は残す。
                LOGGER.warning(
                    "MCP: keeping meta entry '%s' — its shutdown failed, so "
                    "forgetting it would leave an untracked connection",
                    qualified_name,
                )
                continue
            manager._server_meta.pop(qualified_name, None)
            LOGGER.debug(
                "MCP: removed meta entry '%s' after addon '%s' disabled",
                qualified_name,
                addon_name,
            )

    if failures:
        # 呼び出し元 (notify_addon_toggled_sync) の except 節がこれを拾い、
        # mcp_settled=False を返す。画面は「まだ確定していない」を知る。
        raise RuntimeError(
            f"addon toggle failed for {len(failures)} of {len(targets)} "
            f"server(s): " + ", ".join(sorted(failed_names))
        )


async def _shutdown_all_instances_of_server(
    manager: MCPClientManager,
    qualified_name: str,
) -> None:
    """Shutdown every instance of a qualified_name server, global or per-persona.

    起動中 (``_starting``) の instance も対象に含める。接続が ``_connections`` に
    入るのは接続完了後なので、アドオン無効化がその await 中に走ると「対象に無い」
    と判断され、無効化したはずのサーバーの subprocess とツール wrapper が残る
    (Codex レビュー 3 巡目)。_shutdown_instance が起動中には停止要求を置き、
    起動側が着地時に自分で畳む。
    """
    targets = [
        key for key in list(manager._connections.keys()) + list(manager._starting)
        if manager._qualified_from_instance_key(key) == qualified_name
    ]
    for instance_key in dict.fromkeys(targets):
        await manager._shutdown_instance(instance_key)


def notify_addon_toggled_sync(
    addon_name: str,
    is_enabled: bool,
    *,
    wait_timeout: Optional[float] = None,
) -> Optional[bool]:
    """Thread-safe hook called from API routes when an addon is toggled.

    Schedules the MCP state update on the dedicated MCP event loop so
    that refcount arithmetic and subprocess lifecycle stay on one thread.
    Safe to call when MCP is not initialized (no-op).

    ``wait_timeout`` を渡すと、その秒数だけ反映の完了を待つ。 呼び出し元が
    「戻った時点で一覧に反映されている」ことを前提にできる必要があるとき
    (無効化の直後に画面が一覧を取り直す) に使う。 待ち切れなかった場合も
    例外にはしない — 反映は続いているので、警告を残して戻る。

    Returns:
        反映が終わったかどうか。待たなかった場合 (``wait_timeout=None``) と
        MCP が動いていない場合は ``None`` — **「分からない」であって成功では
        ない**。呼び出し元はこれを見て「戻った時点で一覧に反映されている」と
        言い切れるかを決める (``False`` なら、少し置いて取り直す等が要る)。
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
    future = asyncio.run_coroutine_threadsafe(
        _apply_addon_toggle(manager, addon_name, is_enabled),
        loop,
    )
    if wait_timeout is None:
        # 待たずに戻る回も、待ち切れた回と**まったく同じ理由で**誰も
        # result() を呼ばない。見届け役を付けないと、この先で失敗しても
        # ログが一行も出ずに消える。下の TimeoutError 側にだけ付けていた
        # のは、同じ危険の隣を忘れた形だった。
        future.add_done_callback(
            lambda f: _log_late_toggle_outcome(
                f, addon_name, is_enabled, waited=False,
            )
        )
        return None
    try:
        future.result(timeout=wait_timeout)
        return True
    except concurrent.futures.TimeoutError:
        # 待ち切れなかっただけで、反映そのものはまだ続いている。ただし
        # この先で失敗して終わる可能性があり、その回は誰も result() を
        # 呼ばないので痕跡なく消える (concurrent.futures.Future は未回収の
        # 例外を自分では報告しない — asyncio.Future と違う)。見届け役を残す。
        future.add_done_callback(
            lambda f: _log_late_toggle_outcome(
                f, addon_name, is_enabled, waited=True,
            )
        )
        LOGGER.warning(
            "notify_addon_toggled_sync: toggle did not settle within %ss "
            "(addon=%s, enabled=%s); it is still being applied",
            wait_timeout,
            addon_name,
            is_enabled,
        )
        return False
    except Exception as exc:
        # 待っている間に反映そのものが失敗した。これは「継続中」ではない —
        # 同じ文面に混ぜると、ログを読んだ人が失敗を進行中として数える。
        LOGGER.warning(
            "notify_addon_toggled_sync: toggle failed (addon=%s, enabled=%s): %s",
            addon_name,
            is_enabled,
            exc,
        )
        return False


def _log_late_toggle_outcome(
    future: "concurrent.futures.Future",
    addon_name: str,
    is_enabled: bool,
    *,
    waited: bool,
) -> None:
    """呼び出し元が結果を回収しないトグルの、その後の顛末を記録する。

    ``waited`` は顛末の出どころの区別 — ``True`` なら待ったが待ち切れなかった
    回、``False`` なら最初から待たずに戻った回 (有効化)。**どちらも「誰も
    result() を呼ばない」点は同じ**で、見届け役が要る理由も同じ。ログの文面
    だけを言い分ける (「待ち切れた後」と書くと、待っていない回の顛末まで
    タイムアウトの結果として読まれる)。
    """
    tail = "after the wait timed out" if waited else "with no one waiting for it"
    try:
        future.result()
    except Exception as exc:
        LOGGER.warning(
            "notify_addon_toggled_sync: toggle failed %s "
            "(addon=%s, enabled=%s): %s",
            tail,
            addon_name,
            is_enabled,
            exc,
        )
    else:
        LOGGER.info(
            "notify_addon_toggled_sync: toggle settled %s "
            "(addon=%s, enabled=%s)",
            tail,
            addon_name,
            is_enabled,
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
