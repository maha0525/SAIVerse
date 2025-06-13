from dataclasses import dataclass
from pathlib import Path

__all__ = ["Building"]

@dataclass
class Building:
    building_id: str
    name: str
    system_instruction: str
    entry_prompt: str
    auto_prompt: str
    run_entry_llm: bool = False
    run_auto_llm: bool = False
    memory: list | None = None
    memory_path: Path | None = None
