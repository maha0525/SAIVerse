from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class MemopediaRuntimeConfig:
    persona_id: str
    db_path: Path


def prepare_script_runtime() -> None:
    load_dotenv()
    os.environ["SAIVERSE_SKIP_TOOL_IMPORTS"] = "1"


def resolve_persona_db_path(persona_id: str) -> Path:
    return Path(os.getenv("SAIVERSE_HOME") or Path.home() / ".saiverse") / "personas" / persona_id / "memory.db"


def load_runtime_config(persona_id: str) -> MemopediaRuntimeConfig:
    return MemopediaRuntimeConfig(persona_id=persona_id, db_path=resolve_persona_db_path(persona_id))


def load_prompt(name: str) -> str:
    from saiverse.data_paths import load_prompt as dp_load_prompt

    return dp_load_prompt(name)
