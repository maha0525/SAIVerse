"""Tools package for SAIVerse.

Autodiscovers tools from:
  - user_data/<project>/tools/  (project-based, priority)
  - builtin_data/tools/

Supports both:
  - Direct .py files with schema() function
  - Subdirectories with schema.py file (for git-cloned tool repos)
"""
import importlib.util
import logging
import os
import pkgutil
from pathlib import Path
from typing import Any, Callable, Dict, List

from tools.defs import ToolSchema
from tools.adapters import openai as oa, gemini as gm

LOGGER = logging.getLogger(__name__)

TOOL_REGISTRY: Dict[str, Callable] = {}
OPENAI_TOOLS_SPEC: List[Dict[str, Any]] = []
GEMINI_TOOLS_SPEC: List[Any] = []
TOOL_SCHEMAS: List[ToolSchema] = []


def _register_multiple_tools(module: Any) -> bool:
    """Register multiple tools from a module with schemas() function.

    This supports tool packages that export multiple tools from a single module,
    such as user_data/discord/tools/schema.py.
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

            TOOL_REGISTRY[meta.name] = impl
            OPENAI_TOOLS_SPEC.append(oa.to_openai(meta))
            GEMINI_TOOLS_SPEC.append(gm.to_gemini(meta))
            TOOL_SCHEMAS.append(meta)
            registered = True
            LOGGER.debug("Registered tool '%s' from schemas()", meta.name)

        return registered
    except Exception as e:
        LOGGER.warning("Failed to register tools from schemas(): %s", e)
        return False


def _register_tool(module: Any) -> bool:
    """Register a tool from a module if it has schema() or schemas() function."""
    # Multiple tools support: schemas() takes priority
    if hasattr(module, "schemas") and callable(module.schemas):
        return _register_multiple_tools(module)

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
        
        TOOL_REGISTRY[meta.name] = impl
        OPENAI_TOOLS_SPEC.append(oa.to_openai(meta))
        GEMINI_TOOLS_SPEC.append(gm.to_gemini(meta))
        TOOL_SCHEMAS.append(meta)
        
        # Handle aliases
        alias = getattr(module, "ALIASES", None)
        if isinstance(alias, dict):
            for alt_name, alt_impl_name in alias.items():
                function_ref = getattr(module, alt_impl_name, None)
                if callable(function_ref):
                    TOOL_REGISTRY[alt_name] = function_ref
        
        return True
    except Exception as e:
        LOGGER.warning("Failed to register tool from module: %s", e)
        return False


def _load_module_from_path(module_name: str, file_path: Path) -> Any:
    """Dynamically load a Python module from a file path.
    
    For subdirectory modules (schema.py), the parent directory is temporarily
    added to sys.path to allow local imports (e.g., from .helper import ...).
    """
    import sys
    
    parent_dir = str(file_path.parent)
    added_to_path = False
    
    # Add parent directory to sys.path for local imports
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
        added_to_path = True
    
    try:
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
    finally:
        # Clean up sys.path if we added to it
        if added_to_path and parent_dir in sys.path:
            sys.path.remove(parent_dir)


def _autodiscover_tools() -> None:
    """Discover and register tools from user_data and builtin_data directories."""
    # Import here to avoid circular imports at module load time
    from data_paths import iter_project_subdirs, TOOLS_DIR

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
        
        # 1. Direct .py files in the directory
        for modinfo in pkgutil.iter_modules([str(tools_path)]):
            if modinfo.name.startswith("_"):
                continue
            
            py_file = tools_path / f"{modinfo.name}.py"
            if not py_file.exists():
                continue
            
            try:
                module = _load_module_from_path(f"tools._loaded.{modinfo.name}", py_file)
                if module and _register_tool(module):
                    registered_names.add(modinfo.name)
                    LOGGER.debug("Registered tool from %s", py_file)
            except Exception as e:
                LOGGER.warning("Failed to load tool from %s: %s", py_file, e)
        
        # 2. Subdirectories with schema.py (for git-cloned tool repos)
        for subdir in tools_path.iterdir():
            if not subdir.is_dir() or subdir.name.startswith("_"):
                continue
            
            schema_file = subdir / "schema.py"
            if not schema_file.exists():
                continue
            
            try:
                module = _load_module_from_path(f"tools._loaded.{subdir.name}", schema_file)
                if module and _register_tool(module):
                    registered_names.add(subdir.name)
                    LOGGER.debug("Registered tool from %s", schema_file)
            except Exception as e:
                LOGGER.warning("Failed to load tool from %s: %s", schema_file, e)
    
    LOGGER.info("Autodiscovered %d tools", len(TOOL_REGISTRY))


if os.getenv("SAIVERSE_SKIP_TOOL_IMPORTS") != "1":
    _autodiscover_tools()
