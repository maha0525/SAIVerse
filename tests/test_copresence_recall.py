"""再会の想起は Pulse の頭で「いま同席している相手」から発火する (2026-09-07)。

固定する仕様 (docs/issues/perception_state_pushed_at_event_time.md 直し方 6):

1. Pulse の頭で、その部屋にいま居る相手を想起する。
2. **相手が去った後の Pulse では発火しない** — 今回の欠陥の再現形。移動の瞬間に
   入室ラベルを目印として積んでいた頃は、往復して相手が居なくなった後の Pulse に
   「もう居ない相手との再会」が届いていた (まはーの実機報告)。
3. 再会の門 (直近の文脈に相手が居るなら想起しない) は従来どおり効く。
4. SAIMemory が未 ready の回は静かに見送る (例外にしない)。次の Pulse の頭が
   同じ同席をもう一度確かめる。
5. **同席が続いている間は再発火しない** — 発火の条件が「同席している」という状態
   になったので、隣で黙っている相手 (門を通り続ける相手) に毎 Pulse 想起が積まれ
   うる。「不在から同席へ変わった一回だけ試みる」記憶がそれを止め、相手が退室
   すると再武装される。
6. Pulse の頭の呼び出し元 (sea/runtime.py の run_meta_user) から実際に配線されて
   いて、位置は**建物発言の取り込みの後**・知覚の消費の前。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from sea.head_pipeline.integration import reset_copresence_recall_memory

ROOM_A = "b_air_room"
ROOM_B = "b_elis_room"
SELF_ID = "air_city_a"
PARTNER = "elis_city_a"


@pytest.fixture(autouse=True)
def _forget_copresence():
    """「試み済み」の記憶はプロセス内で共有される — テスト間の汚染を掃除する。"""
    reset_copresence_recall_memory()
    yield
    reset_copresence_recall_memory()


class _FakeManager:
    def __init__(self, occupants):
        self.occupants = dict(occupants)
        self.personas = {SELF_ID: object(), PARTNER: object()}


class _FakeMemory:
    def __init__(self, ready=True):
        self.ready = ready
        self.pushed = []

    def is_ready(self):
        return self.ready

    def push_perception(self, kind, content, **kwargs):
        self.pushed.append((kind, content))


def _persona(sai_mem, *, gate=True, building_id=ROOM_B):
    return SimpleNamespace(
        persona_id=SELF_ID,
        current_building_id=building_id,
        sai_memory=sai_mem,
        history_manager=SimpleNamespace(
            should_recall_persona=lambda occupant_id, target_kind=None, **kw: gate,
            recall_conversation_with=(
                lambda occupant_id, **kwargs: f"[想起: {occupant_id} との過去の会話]"
            ),
        ),
        id_to_name_map={PARTNER: "エリス"},
    )


def test_copresent_partner_is_recalled():
    from sea.head_pipeline.integration import inject_copresence_recall

    sai_mem = _FakeMemory()
    manager = _FakeManager({ROOM_A: [SELF_ID, PARTNER]})
    inject_copresence_recall(_persona(sai_mem), manager, ROOM_A)
    assert sai_mem.pushed == [
        ("persona_recall", f"[想起: {PARTNER} との過去の会話]"),
    ]


def test_partner_who_left_is_not_recalled():
    """相手と別れた後の Pulse では想起しない (今回の欠陥の再現形)。

    エリスの部屋 → アイフィの部屋 → エリスの部屋、と往復してから Pulse を打つと、
    アイフィの部屋で積んだ「アイフィとの再会」が自室の Pulse に届いていた。
    """
    from sea.head_pipeline.integration import inject_copresence_recall

    sai_mem = _FakeMemory()
    manager = _FakeManager({ROOM_A: [SELF_ID], ROOM_B: [PARTNER]})
    # 本人は ROOM_A に戻っている。ROOM_B に居るエリスは同席者ではない。
    inject_copresence_recall(_persona(sai_mem, building_id=ROOM_A), manager, ROOM_A)
    assert sai_mem.pushed == []


def test_gate_suppresses_the_recall():
    from sea.head_pipeline.integration import inject_copresence_recall

    sai_mem = _FakeMemory()
    manager = _FakeManager({ROOM_A: [SELF_ID, PARTNER]})
    inject_copresence_recall(_persona(sai_mem, gate=False), manager, ROOM_A)
    assert sai_mem.pushed == []


def test_memory_not_ready_is_skipped_quietly():
    from sea.head_pipeline.integration import inject_copresence_recall

    sai_mem = _FakeMemory(ready=False)
    manager = _FakeManager({ROOM_A: [SELF_ID, PARTNER]})
    inject_copresence_recall(_persona(sai_mem), manager, ROOM_A)   # 例外にしない
    assert sai_mem.pushed == []


def test_missing_memory_is_skipped_quietly():
    from sea.head_pipeline.integration import inject_copresence_recall

    persona = _persona(None)
    persona.sai_memory = None
    inject_copresence_recall(persona, _FakeManager({ROOM_A: [SELF_ID, PARTNER]}), ROOM_A)


# ---- 同席が続いている間の再発火の抑止 (2026-09-07 の追加) --------------------


def test_a_staying_partner_is_recalled_only_once():
    """同じ部屋で Pulse を 2 回打っても想起は 1 回だけ。

    発火の条件が「同席している」という状態になったので、門を通り続ける相手
    (隣で黙っている相手) には毎 Pulse 想起が積まれうる。旧実装が「入室」という
    一回きりの出来事に紐づいていた性質を、縁の記憶で復元する。
    """
    from sea.head_pipeline.integration import inject_copresence_recall

    sai_mem = _FakeMemory()
    persona = _persona(sai_mem, building_id=ROOM_A)
    manager = _FakeManager({ROOM_A: [SELF_ID, PARTNER]})

    inject_copresence_recall(persona, manager, ROOM_A)
    inject_copresence_recall(persona, manager, ROOM_A)

    assert len(sai_mem.pushed) == 1


def test_leaving_and_coming_back_rearms_the_recall():
    """相手が退室した Pulse を挟むと、再入室でまた想起する (再武装)。"""
    from sea.head_pipeline.integration import inject_copresence_recall

    sai_mem = _FakeMemory()
    persona = _persona(sai_mem, building_id=ROOM_A)
    together = _FakeManager({ROOM_A: [SELF_ID, PARTNER]})
    alone = _FakeManager({ROOM_A: [SELF_ID], ROOM_B: [PARTNER]})

    inject_copresence_recall(persona, together, ROOM_A)
    inject_copresence_recall(persona, alone, ROOM_A)      # 相手が出ていった回
    inject_copresence_recall(persona, together, ROOM_A)   # 戻ってきた回

    assert len(sai_mem.pushed) == 2


def test_a_gated_newcomer_is_not_retried_next_pulse():
    """門で抑制された相手も「試み済み」— 次の Pulse で再試行しない。

    再会は一度きりの出来事なので、抑制された回を再試行に回すと「同席している間
    じゅう毎 Pulse 門を叩く」形に戻り、門が開いた瞬間に想起が積まれる。
    """
    from sea.head_pipeline.integration import inject_copresence_recall

    sai_mem = _FakeMemory()
    manager = _FakeManager({ROOM_A: [SELF_ID, PARTNER]})
    gate = {"open": False}
    persona = _persona(sai_mem, building_id=ROOM_A)
    persona.history_manager.should_recall_persona = (
        lambda occupant_id, target_kind=None, **kw: gate["open"]
    )

    inject_copresence_recall(persona, manager, ROOM_A)   # 門は閉じている
    gate["open"] = True
    inject_copresence_recall(persona, manager, ROOM_A)   # 門が開いても再試行しない

    assert sai_mem.pushed == []


def test_an_unready_memory_round_does_not_consume_the_attempt():
    """SAIMemory 未 ready の回は試み済みにならず、ready 後の次の Pulse で発火する。"""
    from sea.head_pipeline.integration import inject_copresence_recall

    sai_mem = _FakeMemory(ready=False)
    persona = _persona(sai_mem, building_id=ROOM_A)
    manager = _FakeManager({ROOM_A: [SELF_ID, PARTNER]})

    inject_copresence_recall(persona, manager, ROOM_A)
    assert sai_mem.pushed == []

    sai_mem.ready = True
    inject_copresence_recall(persona, manager, ROOM_A)
    assert len(sai_mem.pushed) == 1


def test_a_failed_push_is_not_retried_next_pulse():
    """push が落ちた回も試み済み (再会は一度きり — 再試行に戻さない)。"""
    from sea.head_pipeline.integration import inject_copresence_recall

    sai_mem = _FakeMemory()
    persona = _persona(sai_mem, building_id=ROOM_A)
    manager = _FakeManager({ROOM_A: [SELF_ID, PARTNER]})

    with patch.object(
        _FakeMemory, "push_perception", side_effect=RuntimeError("buffer down"),
    ):
        inject_copresence_recall(persona, manager, ROOM_A)   # 例外にしない

    inject_copresence_recall(persona, manager, ROOM_A)
    assert sai_mem.pushed == []


def test_the_memory_is_per_persona():
    """縁の記憶はペルソナごと — 一人の想起が他のペルソナの発火を食べない。"""
    from sea.head_pipeline.integration import inject_copresence_recall

    manager = _FakeManager({ROOM_A: [SELF_ID, PARTNER]})

    air_mem = _FakeMemory()
    inject_copresence_recall(_persona(air_mem, building_id=ROOM_A), manager, ROOM_A)

    elis_mem = _FakeMemory()
    elis = _persona(elis_mem, building_id=ROOM_A)
    elis.persona_id = PARTNER
    inject_copresence_recall(elis, manager, ROOM_A)

    assert len(air_mem.pushed) == 1
    assert len(elis_mem.pushed) == 1


# ---- Pulse の頭の配線と順序 --------------------------------------------------


def _run_pulse_head(order):
    """run_meta_user を最小の替え玉で 1 回走らせ、頭の各段の順序を order へ記録する。"""
    from sea.runtime import SEARuntime

    manager = SimpleNamespace(building_histories={"b1": []})
    runtime = SEARuntime(manager)
    sai_mem = SimpleNamespace(
        flush_perception_buffer=lambda **kwargs: order.append("flush"),
    )
    persona = SimpleNamespace(
        persona_name="p", persona_id=SELF_ID, model="m", llm_client=object(),
        history_manager=SimpleNamespace(add_message=Mock()), execution_state={},
        sai_memory=sai_mem, current_building_id="b1",
    )
    playbook = SimpleNamespace(
        name="meta_user/exec", start_node="exec", context_requirements=None,
    )
    runtime._choose_playbook = Mock(return_value=playbook)
    runtime._prepare_context = Mock(return_value=[])
    runtime._compile_with_langgraph = Mock(return_value=["ok"])
    runtime.session_lifecycle.maybe_run_metabolism = Mock()
    runtime.session_lifecycle.maybe_run_emergency_precompaction = Mock(return_value="skip")
    runtime.session_lifecycle.maybe_run_window_refill = Mock(return_value="skip")
    runtime.session_lifecycle.ensure_window_floor = Mock(return_value="skip")

    with patch(
        "saiverse.dynamic_state.DynamicStateManager.maybe_inject_event_messages",
        new=lambda *a, **kw: order.append("detect") or False,
    ), patch(
        "builtin_data.tools.get_building_messages.auto_ingest_building_messages",
        new=lambda *a, **kw: order.append("ingest") or 0,
    ), patch(
        "sea.head_pipeline.inject_copresence_recall",
        new=lambda *a, **kw: order.append("recall"),
    ):
        runtime.run_meta_user(persona=persona, user_input="hello", building_id="b1")


def test_the_pulse_head_recalls_after_ingesting_the_conversation():
    """順序: 差分の検知 → 建物発言の取り込み → 同席の想起 → 知覚の消費。

    取り込みより前に想起を判定すると、直前に喋った相手の発言がまだ履歴に無い
    ため、会話中の相手が「久しぶりの相手」に化けて過去会話 6 件が積まれる。
    """
    order = []
    _run_pulse_head(order)
    assert order == ["detect", "ingest", "recall", "flush"]


def test_a_broken_recall_does_not_stop_the_pulse():
    """想起が落ちても Pulse は続く (各段は互いに独立の best-effort)。"""
    from sea.runtime import SEARuntime

    manager = SimpleNamespace(building_histories={"b1": []})
    runtime = SEARuntime(manager)
    persona = SimpleNamespace(
        persona_name="p", persona_id=SELF_ID, model="m", llm_client=object(),
        history_manager=SimpleNamespace(add_message=Mock()), execution_state={},
        current_building_id="b1",
    )
    playbook = SimpleNamespace(
        name="meta_user/exec", start_node="exec", context_requirements=None,
    )
    runtime._choose_playbook = Mock(return_value=playbook)
    runtime._prepare_context = Mock(return_value=[])
    runtime._compile_with_langgraph = Mock(return_value=["ok"])
    runtime.session_lifecycle.maybe_run_metabolism = Mock()
    runtime.session_lifecycle.maybe_run_emergency_precompaction = Mock(return_value="skip")
    runtime.session_lifecycle.maybe_run_window_refill = Mock(return_value="skip")
    runtime.session_lifecycle.ensure_window_floor = Mock(return_value="skip")

    with patch(
        "saiverse.dynamic_state.DynamicStateManager.maybe_inject_event_messages",
        new=lambda *a, **kw: False,
    ), patch(
        "builtin_data.tools.get_building_messages.auto_ingest_building_messages",
        new=lambda *a, **kw: 0,
    ), patch(
        "sea.head_pipeline.inject_copresence_recall",
        side_effect=RuntimeError("boom"),
    ):
        assert runtime.run_meta_user(
            persona=persona, user_input="hello", building_id="b1",
        ) == ["ok"]
