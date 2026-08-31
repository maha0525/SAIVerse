"""Cached Head Architecture: Section 実装群。

各 Section はこのパッケージ内に 1 ファイルずつ。``register_default_sections`` で
プロセスの default registry に一括登録する想定。
"""
from sea.head_pipeline.sections.autonomy_modes import AutonomyModesSection
from sea.head_pipeline.sections.available_playbooks import AvailablePlaybooksSection
from sea.head_pipeline.sections.building import BuildingSection
from sea.head_pipeline.sections.building_items import BuildingItemsSection
from sea.head_pipeline.sections.building_occupants import BuildingOccupantsSection
from sea.head_pipeline.sections.chronicle_index import ChronicleIndexSection
from sea.head_pipeline.sections.common_prompt import CommonPromptSection
from sea.head_pipeline.sections.core_memory import CoreMemorySection
from sea.head_pipeline.sections.desk import DeskSection
from sea.head_pipeline.sections.facilities import FacilitiesSection
from sea.head_pipeline.sections.memopedia_index import MemopediaIndexSection
from sea.head_pipeline.sections.memory_weave import MemoryWeaveSection
from sea.head_pipeline.sections.persona_self import PersonaSelfSection
from sea.head_pipeline.sections.self_image import SelfImageSection
from sea.head_pipeline.sections.spell_list import SpellListSection
from sea.head_pipeline.sections.visual_context import VisualContextSection


def register_default_sections(registry) -> None:
    """default registry に標準 Section 群を登録するヘルパ。

    startup 時に 1 回呼ぶ想定。order は各 Section の order property に従う。
    """
    registry.register(CommonPromptSection())
    registry.register(PersonaSelfSection())
    registry.register(CoreMemorySection())
    registry.register(BuildingSection())
    registry.register(FacilitiesSection())
    registry.register(AvailablePlaybooksSection())
    registry.register(AutonomyModesSection())
    registry.register(SelfImageSection())
    registry.register(SpellListSection())
    registry.register(MemoryWeaveSection())
    registry.register(DeskSection())
    registry.register(VisualContextSection())
    # Phase 3: dynamic_state Section 群 (head 描画なし、差分通知のみ)
    registry.register(BuildingItemsSection())
    registry.register(BuildingOccupantsSection())
    registry.register(MemopediaIndexSection())
    registry.register(ChronicleIndexSection())


__all__ = [
    "AutonomyModesSection",
    "AvailablePlaybooksSection",
    "BuildingItemsSection",
    "BuildingOccupantsSection",
    "BuildingSection",
    "ChronicleIndexSection",
    "CommonPromptSection",
    "CoreMemorySection",
    "DeskSection",
    "FacilitiesSection",
    "MemopediaIndexSection",
    "MemoryWeaveSection",
    "PersonaSelfSection",
    "SelfImageSection",
    "SpellListSection",
    "VisualContextSection",
    "register_default_sections",
]
