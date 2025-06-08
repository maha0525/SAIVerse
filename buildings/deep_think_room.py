from pathlib import Path

from . import Building


def load() -> Building:
    base = Path(__file__).resolve().parent
    sys_prompt = (base.parent / 'system_prompts' / 'deep_think_room.txt').read_text(encoding='utf-8')
    auto_prompt = (base / 'deep_think_room_prompt.txt').read_text(encoding='utf-8')
    return Building(
        building_id='deep_think_room',
        name='思索の部屋',
        system_instruction=sys_prompt,
        auto_prompt=auto_prompt,
    )
