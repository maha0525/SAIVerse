from dataclasses import dataclass

__all__ = ["Building"]

@dataclass
class Building:
    building_id: str
    name: str
    system_instruction: str
    auto_prompt: str
