from pathlib import Path

from .. import Building


def load() -> Building:
    base = Path(__file__).resolve().parent
    sys_prompt = (base / 'system_prompt.txt').read_text(encoding='utf-8')
    auto_prompt = (base / 'auto_prompt.txt').read_text(encoding='utf-8')
    return Building(
        building_id='const_test_room',
        name='恒常性テスト',
        system_instruction=sys_prompt,
        entry_prompt=auto_prompt,
        auto_prompt=auto_prompt,
        capacity=1,
        run_entry_llm=True,
        run_auto_llm=True,
        auto_interval_sec=60,
    )
