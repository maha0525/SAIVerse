"""_queue_for_resumption の実行契約コピーの回帰テスト (2026-07-31 Codex 六・七巡目)。

中断された Pulse の復帰 request の扱いは目的で分ける:

- **args = 純粋な入力データ → コピーする**。落とすと schedule / phenomenon 発の
  Pulse (inject_persona_event の playbook_args 等) が元と異なる入力で再開される。
- **pre_spells = 実行前アクション (副作用) → コピーしない**。中断が起きる時点で
  ほぼ実行済みで、載せると割り込みのたびメール送信・画像生成等が再実行される。
"""
from __future__ import annotations

from types import SimpleNamespace

from sea.pulse_controller import ExecutionRequest, PulseController


def test_queue_for_resumption_copies_inputs_but_not_side_effect_actions():
    queues = {}
    stub = SimpleNamespace(_get_queue=lambda pid: queues.setdefault(pid, []))
    request = ExecutionRequest(
        type="schedule",
        persona_id="alice",
        building_id="alice_room",
        user_input="<system>x</system>",
        metadata={"source": "external_event"},
        meta_playbook="track_user_conversation",
        args={"trigger_author_name": "X"},
        pre_spells=['/run_playbook(name="generate_image_playbook")'],
    )

    PulseController._queue_for_resumption(stub, request)

    resumed = queues["alice"][0]
    assert resumed.is_resumption is True
    assert resumed.args == {"trigger_author_name": "X"}
    assert resumed.pre_spells is None  # 副作用アクションは復帰で再実行しない
    assert resumed.meta_playbook == "track_user_conversation"
    assert resumed.metadata == {"source": "external_event"}
