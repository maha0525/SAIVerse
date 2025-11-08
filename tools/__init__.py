import importlib
import pkgutil
from pathlib import Path
from typing import Dict, Callable, List, Any
from tools.defs import ToolSchema
from tools.adapters import openai as oa, gemini as gm

TOOL_REGISTRY: Dict[str, Callable] = {}
OPENAI_TOOLS_SPEC: List[Dict[str, Any]] = []
GEMINI_TOOLS_SPEC: List[Any] = []
TOOL_SCHEMAS: List[ToolSchema] = []

for modinfo in pkgutil.iter_modules([Path(__file__).parent / "defs"]):
    module = importlib.import_module(f"tools.defs.{modinfo.name}")
    if hasattr(module, "schema") and callable(module.schema):
        meta: ToolSchema = module.schema()
        impl: Callable = getattr(module, meta.name)
        TOOL_REGISTRY[meta.name] = impl
        OPENAI_TOOLS_SPEC.append(oa.to_openai(meta))
        GEMINI_TOOLS_SPEC.append(gm.to_gemini(meta))
        TOOL_SCHEMAS.append(meta)

        alias = getattr(module, "ALIASES", None)
        if isinstance(alias, dict):
            for alt_name, alt_impl_name in alias.items():
                function_name = getattr(module, alt_impl_name, None)
                if callable(function_name):
                    TOOL_REGISTRY[alt_name] = function_name
