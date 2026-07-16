"""Tools package for SAIVerse.

Autodiscovers tools from:
  - user_data/<project>/tools/  (project-based, priority)
  - builtin_data/tools/

Supports both:
  - Direct .py files with schema() function
  - Subdirectories with schema.py file (for git-cloned tool repos)
"""
import importlib.util
import inspect
import logging
import os
import pkgutil
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from tools.core import ToolSchema
from tools.adapters import openai as oa, gemini as gm

LOGGER = logging.getLogger(__name__)

TOOL_REGISTRY: Dict[str, Callable] = {}
OPENAI_TOOLS_SPEC: List[Dict[str, Any]] = []
GEMINI_TOOLS_SPEC: List[Any] = []
TOOL_SCHEMAS: List[ToolSchema] = []
SPELL_TOOL_NAMES: set[str] = set()  # Tools available as spells (invoked via /spell in text)
SPELL_TOOL_SCHEMAS: Dict[str, ToolSchema] = {}  # Spell tool schemas for system prompt generation


def _wrap_with_building_gate(name: str, building_ids: List[str], func: Callable) -> Callable:
    """Wrap a tool callable to enforce the ``building_ids`` execute-time gate.

    Applied at registration time so native and MCP tools share the same gate
    semantics. The gate consults the active persona's ``current_building_id``
    via ``tools.context`` and returns an error string when the persona is
    outside the allowed buildings. Detail: docs/intent/stackchan_vessel.md
    A-3-c.
    """
    if inspect.iscoroutinefunction(func):
        async def _async_gated(**kwargs: Any) -> Any:
            from tools.mcp_client import check_building_gate
            gate_error = check_building_gate(name, building_ids)
            if gate_error is not None:
                return gate_error
            return await func(**kwargs)

        _async_gated.__wrapped__ = func  # type: ignore[attr-defined]
        _async_gated._saiverse_building_gate = True  # type: ignore[attr-defined]
        return _async_gated

    def _sync_gated(**kwargs: Any) -> Any:
        from tools.mcp_client import check_building_gate
        gate_error = check_building_gate(name, building_ids)
        if gate_error is not None:
            return gate_error
        return func(**kwargs)

    _sync_gated.__wrapped__ = func  # type: ignore[attr-defined]
    _sync_gated._saiverse_building_gate = True  # type: ignore[attr-defined]
    return _sync_gated


def _tool_authorization_error(name: str, schema: ToolSchema) -> Optional[str]:
    """Return an execute-time denial message, or ``None`` when authorized.

    This is the final common gate immediately in front of every registered
    native/MCP callable.  Prompt visibility is advisory; callers such as
    Playbook TOOL/TOOL_CALL nodes, realtime spells, and admin integrations must
    not be able to bypass the same policy by reaching ``TOOL_REGISTRY`` through
    a different route.
    """
    from tools.context import get_active_persona_id, get_active_pulse_context

    persona_id = get_active_persona_id()

    addon_name = getattr(schema, "addon_name", None)
    if addon_name:
        try:
            from saiverse.addon_config import is_addon_enabled

            if not is_addon_enabled(addon_name):
                return (
                    f"ツール '{name}' は無効化されているアドオン "
                    f"'{addon_name}' に属するため実行できません。"
                )
        except Exception:
            LOGGER.exception(
                "Tool authorization failed while checking addon state: tool=%s addon=%s",
                name,
                addon_name,
            )
            return f"ツール '{name}' のアドオン有効状態を確認できないため実行を拒否しました。"

    availability_check = getattr(schema, "availability_check", None)
    if availability_check is not None:
        try:
            if not availability_check(persona_id):
                return f"ツール '{name}' は現在のペルソナでは利用できません。"
        except Exception:
            LOGGER.exception(
                "Tool authorization availability_check failed: tool=%s persona=%s",
                name,
                persona_id,
            )
            return f"ツール '{name}' の利用条件を確認できないため実行を拒否しました。"

    pulse_context = get_active_pulse_context()
    active_line = pulse_context.current_line() if pulse_context is not None else None
    active_aspect = getattr(active_line, "aspect", None)
    from sea.mode_spell_permissions import check_spell_permission

    return check_spell_permission(name, active_aspect)


def _wrap_with_authorization_gate(name: str, schema: ToolSchema, func: Callable) -> Callable:
    """Wrap a registered callable with the common execute-time policy gate."""
    if inspect.iscoroutinefunction(func):
        async def _async_authorized(**kwargs: Any) -> Any:
            denial = _tool_authorization_error(name, schema)
            if denial is not None:
                LOGGER.warning("Tool execution denied: tool=%s reason=%s", name, denial)
                return denial
            return await func(**kwargs)

        _async_authorized.__wrapped__ = func  # type: ignore[attr-defined]
        _async_authorized._saiverse_authorization_gate = True  # type: ignore[attr-defined]
        return _async_authorized

    def _sync_authorized(**kwargs: Any) -> Any:
        denial = _tool_authorization_error(name, schema)
        if denial is not None:
            LOGGER.warning("Tool execution denied: tool=%s reason=%s", name, denial)
            return denial
        return func(**kwargs)

    _sync_authorized.__wrapped__ = func  # type: ignore[attr-defined]
    _sync_authorized._saiverse_authorization_gate = True  # type: ignore[attr-defined]
    return _sync_authorized


def _wrap_registered_callable(name: str, schema: ToolSchema, func: Callable) -> Callable:
    """Apply every execute-time policy shared by native tools and aliases."""
    wrapped = _wrap_with_authorization_gate(name, schema, func)
    schema_building_ids = getattr(schema, "building_ids", None)
    if schema_building_ids:
        wrapped = _wrap_with_building_gate(name, list(schema_building_ids), wrapped)
    return wrapped


def _add_registered_tool(name: str, schema: ToolSchema, func: Callable) -> None:
    """Register a tool into all in-memory registries."""
    func = _wrap_registered_callable(name, schema, func)
    # Build the provider function-calling specs defensively. A single tool
    # whose JSON schema a provider SDK rejects (e.g. an MCP tool whose schema
    # uses a construct google-genai forbids) must NOT abort registration of
    # every other tool on the same server. We skip only the offending provider
    # spec and keep the tool callable + visible as a spell. Without this guard,
    # one bad schema silently dropped all later tools on the server (this is
    # how the Stack-chan LED spells went missing).
    try:
        openai_spec = oa.to_openai(schema)
    except Exception:
        LOGGER.warning(
            "to_openai failed for tool '%s'; OpenAI function spec skipped", name, exc_info=True
        )
        openai_spec = None
    try:
        gemini_spec = gm.to_gemini(schema)
    except Exception:
        LOGGER.warning(
            "to_gemini failed for tool '%s'; Gemini function spec skipped", name, exc_info=True
        )
        gemini_spec = None
    TOOL_REGISTRY[name] = func
    if openai_spec is not None:
        OPENAI_TOOLS_SPEC.append(openai_spec)
    if gemini_spec is not None:
        GEMINI_TOOLS_SPEC.append(gemini_spec)
    TOOL_SCHEMAS.append(schema)
    if getattr(schema, "spell", False):
        SPELL_TOOL_NAMES.add(name)
        SPELL_TOOL_SCHEMAS[name] = schema


def _remove_registered_tool(name: str) -> None:
    """Remove a tool from all in-memory registries."""
    TOOL_REGISTRY.pop(name, None)
    OPENAI_TOOLS_SPEC[:] = [
        spec for spec in OPENAI_TOOLS_SPEC
        if spec.get("function", {}).get("name") != name
    ]
    GEMINI_TOOLS_SPEC[:] = [
        spec for spec in GEMINI_TOOLS_SPEC
        if not any(getattr(decl, "name", None) == name for decl in getattr(spec, "function_declarations", []) or [])
    ]
    TOOL_SCHEMAS[:] = [schema for schema in TOOL_SCHEMAS if schema.name != name]
    SPELL_TOOL_NAMES.discard(name)
    SPELL_TOOL_SCHEMAS.pop(name, None)


def _register_multiple_tools(module: Any, addon_name: Optional[str] = None) -> bool:
    """Register multiple tools from a module with schemas() function.

    This supports tool packages that export multiple tools from a single module,
    such as user_data/discord/tools/schema.py.

    addon_name: Loader 側で expansion_data/<addon>/tools/ から推定した値。
    スキーマ側で明示してなければここで注入する。
    """
    try:
        tool_schemas: List[ToolSchema] = module.schemas()
        registered = False
        for meta in tool_schemas:
            impl: Callable = getattr(module, meta.name, None)
            if not impl or not callable(impl):
                LOGGER.warning("Tool '%s' has schema but no implementation function", meta.name)
                continue

            # Skip if already registered (user_data takes priority)
            if meta.name in TOOL_REGISTRY:
                LOGGER.debug("Tool '%s' already registered, skipping", meta.name)
                continue

            if addon_name and not getattr(meta, "addon_name", None):
                meta.addon_name = addon_name

            _add_registered_tool(meta.name, meta, impl)
            registered = True
            LOGGER.debug("Registered tool '%s' from schemas() (spell=%s addon=%s)",
                         meta.name, getattr(meta, "spell", False), meta.addon_name)

        return registered
    except Exception as e:
        LOGGER.warning("Failed to register tools from schemas(): %s", e)
        return False


def _register_tool(module: Any, addon_name: Optional[str] = None) -> bool:
    """Register a tool from a module if it has schema() or schemas() function.

    addon_name: Loader 側で expansion_data/<addon>/tools/ から推定した値。
    スキーマ側で明示してなければここで注入する。
    """
    # Multiple tools support: schemas() takes priority
    if hasattr(module, "schemas") and callable(module.schemas):
        return _register_multiple_tools(module, addon_name=addon_name)

    # Single tool: schema() function
    if not hasattr(module, "schema") or not callable(module.schema):
        return False

    try:
        meta: ToolSchema = module.schema()
        impl: Callable = getattr(module, meta.name, None)
        if not impl or not callable(impl):
            LOGGER.warning("Tool '%s' has schema but no implementation function", meta.name)
            return False

        # Skip if already registered (user_data takes priority)
        if meta.name in TOOL_REGISTRY:
            LOGGER.debug("Tool '%s' already registered, skipping", meta.name)
            return False

        if addon_name and not getattr(meta, "addon_name", None):
            meta.addon_name = addon_name

        _add_registered_tool(meta.name, meta, impl)

        # Handle aliases
        alias = getattr(module, "ALIASES", None)
        if isinstance(alias, dict):
            for alt_name, alt_impl_name in alias.items():
                function_ref = getattr(module, alt_impl_name, None)
                if callable(function_ref):
                    TOOL_REGISTRY[alt_name] = _wrap_registered_callable(
                        alt_name,
                        meta,
                        function_ref,
                    )
        
        return True
    except Exception as e:
        LOGGER.warning("Failed to register tool from module: %s", e)
        return False


def _load_module_from_path(module_name: str, file_path: Path) -> Any:
    """Dynamically load a Python module from a file path.
    
    For subdirectory modules (schema.py), the parent directory is temporarily
    added to sys.path to allow local imports (e.g., from .helper import ...).

    NOTE: parent_dir は sys.path に **永続追加** する。schema() 内で隣接ライブラリ
    (例: `from x_lib.credentials import ...`) を呼ぶケースがあるため、
    exec_module 完了後の register 段階でも sys.path にパスが残っている必要がある。
    以前は finally で削除していたが、それだと register 時の動的 import が失敗していた。
    アドオンごとに tools/ ディレクトリは一意なので衝突リスクは低い。
    """
    import sys

    parent_dir = str(file_path.parent)
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)

    spec = importlib.util.spec_from_file_location(
        module_name,
        file_path,
        submodule_search_locations=[parent_dir]
    )
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module  # Register module for relative imports
    spec.loader.exec_module(module)
    return module


def _infer_addon_name(tools_path: Path) -> Optional[str]:
    """tools_path が ``expansion_data/<addon>/tools/`` の形なら ``<addon>`` を返す。

    user_data/builtin_data 配下や legacy `tools/defs/` の場合は None。
    親ディレクトリ名を addon 識別子として使うことで、ツール側のコードを変えずに
    addon 所属を runtime に伝えられる。
    """
    from saiverse.data_paths import EXPANSION_DATA_DIR
    try:
        if tools_path.parent.parent.resolve() == EXPANSION_DATA_DIR.resolve():
            return tools_path.parent.name
    except Exception:
        pass
    return None


def _autodiscover_tools() -> None:
    """Discover and register tools from user_data and builtin_data directories."""
    # Import here to avoid circular imports at module load time
    from saiverse.data_paths import iter_project_subdirs, TOOLS_DIR

    registered_names: set[str] = set()

    # Get tool directories from all projects (user_data/<project>/tools/) + builtin_data/tools/
    tool_dirs = list(iter_project_subdirs(TOOLS_DIR))

    # Also include legacy tools/defs for backwards compatibility during transition
    legacy_defs = Path(__file__).parent / "defs"
    if legacy_defs.exists() and legacy_defs not in tool_dirs:
        tool_dirs.append(legacy_defs)

    for tools_path in tool_dirs:
        if not tools_path.exists():
            continue

        addon_name = _infer_addon_name(tools_path)

        # 1. Direct .py files in the directory
        for modinfo in pkgutil.iter_modules([str(tools_path)]):
            if modinfo.name.startswith("_"):
                continue

            py_file = tools_path / f"{modinfo.name}.py"
            if not py_file.exists():
                continue

            try:
                module = _load_module_from_path(f"tools._loaded.{modinfo.name}", py_file)
                if module and _register_tool(module, addon_name=addon_name):
                    registered_names.add(modinfo.name)
                    LOGGER.debug("Registered tool from %s (addon=%s)", py_file, addon_name)
            except Exception as e:
                LOGGER.warning("Failed to load tool from %s: %s", py_file, e)

        # 2. Subdirectories with schema.py (for git-cloned tool repos),
        #    OR grouping subdirectories that contain .py tool files directly
        #    (e.g. tools/units/env3.py for organizing Unit drivers).
        #
        # Disambiguation rules:
        #   - <subdir>/schema.py exists           → 1 unit per dir (existing)
        #   - <subdir>/__init__.py exists         → Python package / lib
        #                                          modules (skip; helper code
        #                                          imported by sibling tools)
        #   - otherwise                           → grouping dir; load each
        #                                          .py inside as a tool
        for subdir in tools_path.iterdir():
            if not subdir.is_dir() or subdir.name.startswith("_"):
                continue

            schema_file = subdir / "schema.py"
            if schema_file.exists():
                try:
                    module = _load_module_from_path(f"tools._loaded.{subdir.name}", schema_file)
                    if module and _register_tool(module, addon_name=addon_name):
                        registered_names.add(subdir.name)
                        LOGGER.debug("Registered tool from %s (addon=%s)", schema_file, addon_name)
                except Exception as e:
                    LOGGER.warning("Failed to load tool from %s: %s", schema_file, e)
                continue

            # No schema.py — check if this is a Python package (helper lib).
            if (subdir / "__init__.py").exists():
                continue

            # Grouping subdirectory: each .py inside is an independent tool.
            for modinfo in pkgutil.iter_modules([str(subdir)]):
                if modinfo.name.startswith("_"):
                    continue

                py_file = subdir / f"{modinfo.name}.py"
                if not py_file.exists():
                    continue

                try:
                    # uniquify module name so e.g. tools/units/env3.py does
                    # not collide with tools/env3.py if both ever exist.
                    module_qualname = f"tools._loaded.{subdir.name}__{modinfo.name}"
                    module = _load_module_from_path(module_qualname, py_file)
                    if module and _register_tool(module, addon_name=addon_name):
                        registered_names.add(modinfo.name)
                        LOGGER.debug("Registered tool from %s (addon=%s)", py_file, addon_name)
                except Exception as e:
                    LOGGER.warning("Failed to load tool from %s: %s", py_file, e)

    LOGGER.info("Autodiscovered %d tools", len(TOOL_REGISTRY))


def register_external_tool(
    name: str,
    schema: ToolSchema,
    func: Callable,
    *,
    allow_replace: bool = False,
) -> bool:
    """Register an externally provided tool (e.g. MCP) at runtime.

    Returns True when the tool was registered, False when it was skipped.
    """
    existing = TOOL_REGISTRY.get(name)
    if existing is not None and not allow_replace:
        LOGGER.warning("register_external_tool: '%s' already registered, skipping", name)
        return False
    if existing is not None and allow_replace:
        _remove_registered_tool(name)
    _add_registered_tool(name, schema, func)
    LOGGER.info("Registered external tool '%s' (spell=%s)", name, getattr(schema, "spell", False))
    return True


def unregister_external_tool(name: str) -> None:
    """Remove a dynamically registered external tool from all registries."""
    if name not in TOOL_REGISTRY:
        return
    _remove_registered_tool(name)
    LOGGER.info("Unregistered external tool '%s'", name)


if os.getenv("SAIVERSE_SKIP_TOOL_IMPORTS") != "1":
    _autodiscover_tools()
