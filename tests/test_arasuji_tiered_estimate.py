"""The UI/CLI estimate applies model pricing to each planned request."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from sai_memory.arasuji.estimate import estimate_chronicle_generation_cost
from saiverse.model_configs import calculate_cost


@pytest.mark.parametrize('model,threshold', [('claude-haiku-5.5', 100_000), ('grok-4.7', 200_000)])
@pytest.mark.parametrize('offsets', [(0,), (1,), (-1, 1), (0, 0)])
def test_estimate_uses_each_request_not_total_or_average(monkeypatch, model, threshold, offsets):
    import sai_memory.arasuji.absorption as absorption
    import sai_memory.arasuji.alignment as alignment
    import sai_memory.arasuji.bands as bands
    import sai_memory.arasuji.storage as storage
    import sai_memory.memopedia as memopedia
    import sai_memory.memory.storage as messages

    inputs = [threshold + offset for offset in offsets]
    chunks = [SimpleNamespace(coverage_chars=(tokens - 500) * 3.5, messages=[]) for tokens in inputs]
    plan = SimpleNamespace(chunks=chunks, total_unprocessed=len(chunks), llm_calls=len(chunks))
    monkeypatch.setattr(messages, 'count_messages', lambda conn: len(chunks))
    monkeypatch.setattr(storage, 'get_total_message_count', lambda conn: 0)
    monkeypatch.setattr(storage, 'count_entries_by_level', lambda conn: {})
    monkeypatch.setattr(alignment, 'plan_alignment', lambda *args, **kwargs: plan)
    monkeypatch.setattr(absorption, 'split_plan_for_absorption', lambda *args, **kwargs: (plan, []))
    monkeypatch.setattr(absorption, 'uncovered_tail_zone', lambda *args: ([], None, None))
    monkeypatch.setattr(absorption, 'list_stale_upper_ids', lambda conn: [])
    monkeypatch.setattr(absorption, 'list_sweep_regen_ids', lambda conn: [])
    monkeypatch.setattr(bands, 'plan_band_overflow', lambda *args, **kwargs: 0)
    monkeypatch.setattr(memopedia, 'Memopedia', lambda *args, **kwargs: SimpleNamespace(get_tree_markdown=lambda **kwargs: ''))
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = []
    conn.execute.return_value.fetchone.return_value = (None,)
    estimate = estimate_chronicle_generation_cost(conn, model_name=model, messages_override=[])
    expected = round(sum(calculate_cost(model, tokens, 400) for tokens in inputs), 6)
    assert estimate.estimated_cost_usd == pytest.approx(expected)
    assert estimate.level1_calls == len(inputs)
