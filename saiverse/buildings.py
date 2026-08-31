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
        auto_interval_sec: int = 10,
        extra_prompt_files: Optional[list[str]] = None,
        physical_vessel_id: Optional[str] = None,
        region_id: Optional[str] = None,
        facility_roles: Optional[list[str]] = None,
    ):
        self.building_id = building_id
        self.name = name
        self.capacity = capacity
        self.base_system_instruction = system_instruction or ""
        self.system_instruction = self.base_system_instruction
        self.entry_prompt = entry_prompt
        self.auto_prompt = auto_prompt
        self.description = description # Added this to accept description from DB
        self.run_entry_llm = run_entry_llm
        self.run_auto_llm = run_auto_llm
        self.auto_interval_sec = auto_interval_sec
        self.item_ids: list[str] = []
        self.extra_prompt_files: list[str] = extra_prompt_files or []
        # 物理機体 (Stack-chan 等) を表す Vessel Building の場合に非NULL。
        # 詳細: docs/intent/stackchan_vessel.md
        self.physical_vessel_id: Optional[str] = physical_vessel_id
        # 所属する Region / SubRegion の ID。NULL なら無所属 (従来どおりの Building)。
        self.region_id: Optional[str] = region_id
        # 公共施設のロールタグ (自律行動 v2 §6.1)。語彙・解決は saiverse/facility_map.py。
        # 空リスト = ロールなし (私室・通常 Building)。
        self.facility_roles: list[str] = facility_roles or []

