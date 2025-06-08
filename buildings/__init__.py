from dataclasses import dataclass
from pathlib import Path

__all__ = ["Building"]

@dataclass
class Building:
    building_id: str
    name: str
    system_instruction: str
    auto_prompt: str
    memory: list | None = None
    memory_path: Path | None = None
