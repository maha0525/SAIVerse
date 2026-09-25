"""Cached Head Architecture: Section 実装群。

各 Section はこのパッケージ内に 1 ファイルずつ。``register_default_sections`` で
プロセスの default registry に一括登録する想定。

退役 (2026-09-06, docs/intent/room_state_packages.md):
- ``VisualContextSection`` — head の部屋の描画。部屋の様子の置き場は知覚 (tail)
  一つになった。
- ``BuildingItemsSection`` — アイテム差分ラベル (「追加されました」)。滞在中の
  パッケージ照合 (sea/head_pipeline/integration._detect_room_state_changes) に
  一本化。

復帰 (2026-09-25, docs/issues/inventory_and_appearance_dropped_from_context.md):
上の退役で、旧 ``VisualContextSection`` だけが運んでいた「自分の外見」と
「インベントリ」、旧 ``BuildingItemsSection`` が出していたインベントリの差分通知が
どこからも届かなくなっていた。``SelfViewSection`` がこの二つを運ぶ (部屋の描画は
戻さない)。
"""
from sea.head_pipeline.sections.autonomy_modes import AutonomyModesSection
from sea.head_pipeline.sections.available_playbooks import AvailablePlaybooksSection
from sea.head_pipeline.sections.building import BuildingSection
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
from sea.head_pipeline.sections.self_view import SelfViewSection
from sea.head_pipeline.sections.spell_list import SpellListSection


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
    # 自分の外見とインベントリ (独立した user メッセージ、_compose_messages が置く)
    registry.register(SelfViewSection())
    # dynamic_state Section 群 (head 描画なし、差分通知のみ)
    registry.register(BuildingOccupantsSection())
    registry.register(MemopediaIndexSection())
    registry.register(ChronicleIndexSection())


__all__ = [
    "AutonomyModesSection",
    "AvailablePlaybooksSection",
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
    "SelfViewSection",
    "SpellListSection",
    "register_default_sections",
]
