from pathlib import Path

from .. import Building


def load() -> Building:
    base = Path(__file__).resolve().parent
    sys_prompt = (base / 'system_prompt.txt').read_text(encoding='utf-8')
    auto_prompt = (base / 'auto_prompt.txt').read_text(encoding='utf-8')
    return Building(
        building_id='deep_think_room',
        name='思索の部屋',
        system_instruction=sys_prompt,
        entry_prompt=auto_prompt,
        auto_prompt=auto_prompt,
        capacity=1,
        run_entry_llm=True,
        run_auto_llm=True,
    )
