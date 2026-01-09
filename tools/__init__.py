"""Tools package for SAIVerse.

Autodiscovers tools from:
  - user_data/tools/    (priority)
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


def _register_tool(module: Any) -> bool:
    """Register a tool from a module if it has schema() function."""
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
    """Dynamically load a Python module from a file path."""
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _autodiscover_tools() -> None:
    """Discover and register tools from user_data and builtin_data directories."""
    # Import here to avoid circular imports at module load time
    from data_paths import get_data_paths, TOOLS_DIR
    
    registered_names: set[str] = set()
    
    # Get tool directories (user_data first for priority)
    tool_dirs = get_data_paths(TOOLS_DIR)
    
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
