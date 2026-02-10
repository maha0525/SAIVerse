"""
phenomena ― フェノメノン（現象）システム

SAIVerse世界で発生する現象を定義し、トリガーイベントに応じて自動実行する。
ペルソナとは独立してバックグラウンドで動作可能。

Autodiscovers phenomena from:
  - user_data/<project>/phenomena/  (project-based, priority)
  - builtin_data/phenomena/

Supports both:
  - Direct .py files with schema() function
  - Subdirectories with schema.py file (for git-cloned phenomenon repos)
"""
import importlib.util
import logging
import os
import pkgutil
from pathlib import Path
from typing import Any, Callable, Dict, List

from phenomena.core import PhenomenonSchema

LOGGER = logging.getLogger(__name__)

PHENOMENON_REGISTRY: Dict[str, Callable] = {}
PHENOMENON_SCHEMAS: List[PhenomenonSchema] = []


def _register_multiple_phenomena(module: Any) -> bool:
    """Register multiple phenomena from a module with schemas() function.

    This supports phenomenon packages that export multiple phenomena from a single module,
    such as user_data/discord/phenomena/schema.py.
    """
    try:
        phenomenon_schemas: List[PhenomenonSchema] = module.schemas()
        registered = False
        for meta in phenomenon_schemas:
            impl: Callable = getattr(module, meta.name, None)
            if not impl or not callable(impl):
                LOGGER.warning("Phenomenon '%s' has schema but no implementation function", meta.name)
                continue

            # Skip if already registered (user_data takes priority)
            if meta.name in PHENOMENON_REGISTRY:
                LOGGER.debug("Phenomenon '%s' already registered, skipping", meta.name)
                continue

            PHENOMENON_REGISTRY[meta.name] = impl
            PHENOMENON_SCHEMAS.append(meta)
            registered = True
            LOGGER.debug("Registered phenomenon '%s' from schemas()", meta.name)

        return registered
    except Exception as e:
        LOGGER.warning("Failed to register phenomena from schemas(): %s", e)
        return False


def _register_phenomenon(module: Any) -> bool:
    """Register a phenomenon from a module if it has schema() or schemas() function."""
    # Multiple phenomena support: schemas() takes priority
    if hasattr(module, "schemas") and callable(module.schemas):
        return _register_multiple_phenomena(module)

    # Single phenomenon: schema() function
    if not hasattr(module, "schema") or not callable(module.schema):
        return False
    
    try:
        meta: PhenomenonSchema = module.schema()
        impl: Callable = getattr(module, meta.name, None)
        if not impl or not callable(impl):
            LOGGER.warning("Phenomenon '%s' has schema but no implementation function", meta.name)
            return False
        
        # Skip if already registered (user_data takes priority)
        if meta.name in PHENOMENON_REGISTRY:
            LOGGER.debug("Phenomenon '%s' already registered, skipping", meta.name)
            return False
        
        PHENOMENON_REGISTRY[meta.name] = impl
        PHENOMENON_SCHEMAS.append(meta)
        return True
    except Exception as e:
        LOGGER.warning("Failed to register phenomenon from module: %s", e)
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


def _autodiscover_phenomena() -> None:
    """phenomena/defs/ 以下のモジュールを自動的に読み込み、レジストリに登録する"""
    # Import here to avoid circular imports at module load time
    from saiverse.data_paths import iter_project_subdirs, PHENOMENA_DIR

    registered_names: set[str] = set()

    # Get phenomena directories from all projects (user_data/<project>/phenomena/) + builtin_data/phenomena/
    phenomena_dirs = list(iter_project_subdirs(PHENOMENA_DIR))

    # Also include legacy phenomena/defs for backwards compatibility during transition
    legacy_defs = Path(__file__).parent / "defs"
    if legacy_defs.exists() and legacy_defs not in phenomena_dirs:
        phenomena_dirs.append(legacy_defs)
    
    for phenomena_path in phenomena_dirs:
        if not phenomena_path.exists():
            continue
        
        # 1. Direct .py files in the directory
        for modinfo in pkgutil.iter_modules([str(phenomena_path)]):
            if modinfo.name.startswith("_"):
                continue
            
            py_file = phenomena_path / f"{modinfo.name}.py"
            if not py_file.exists():
                continue
            
            try:
                module = _load_module_from_path(f"phenomena._loaded.{modinfo.name}", py_file)
                if module and _register_phenomenon(module):
                    registered_names.add(modinfo.name)
                    LOGGER.debug("Registered phenomenon from %s", py_file)
            except Exception as e:
                LOGGER.warning("Failed to load phenomenon from %s: %s", py_file, e)
        
        # 2. Subdirectories with schema.py (for git-cloned repos)
        for subdir in phenomena_path.iterdir():
            if not subdir.is_dir() or subdir.name.startswith("_"):
                continue
            
            schema_file = subdir / "schema.py"
            if not schema_file.exists():
                continue
            
            try:
                module = _load_module_from_path(f"phenomena._loaded.{subdir.name}", schema_file)
                if module and _register_phenomenon(module):
                    registered_names.add(subdir.name)
                    LOGGER.debug("Registered phenomenon from %s", schema_file)
            except Exception as e:
                LOGGER.warning("Failed to load phenomenon from %s: %s", schema_file, e)
    
    LOGGER.info("Autodiscovered %d phenomena", len(PHENOMENON_REGISTRY))


# 環境変数でスキップ可能
if os.getenv("SAIVERSE_SKIP_PHENOMENA_IMPORTS") != "1":
    _autodiscover_phenomena()
