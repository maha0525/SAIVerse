from typing import Optional

class Building:
    """Represents a building within a city."""
    def __init__(
        self,
        building_id: str,
        name: str,
        capacity: int = 1,
        system_instruction: str = "",
        entry_prompt: Optional[str] = None,
        auto_prompt: Optional[str] = None,
        description: str = "", # Added this to accept description from DB
        run_entry_llm: bool = True,
        run_auto_llm: bool = True,
        auto_interval_sec: int = 0,
    ):
        self.building_id = building_id
        self.name = name
        self.capacity = capacity
        self.system_instruction = system_instruction
        self.entry_prompt = entry_prompt
        self.auto_prompt = auto_prompt
        self.description = description # Added this to accept description from DB
        self.run_entry_llm = run_entry_llm
        self.run_auto_llm = run_auto_llm
        self.auto_interval_sec = auto_interval_sec