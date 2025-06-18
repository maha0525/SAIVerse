from pathlib import Path

from .. import Building


def load() -> Building:
    base = Path(__file__).resolve().parent
    sys_prompt = (base / 'system_prompt.txt').read_text(encoding='utf-8')
    entry_prompt = (base / 'entry_prompt.txt').read_text(encoding='utf-8')
    return Building(
        building_id='air_room',
        name='エアの部屋',
        system_instruction=sys_prompt,
        entry_prompt=entry_prompt,
        auto_prompt="",
        capacity=1,
        run_entry_llm=False,
        run_auto_llm=False,
        auto_interval_sec=0,
    )
