"""刺激の ID の義務化と受領記録 (v0.4 段 1-3) のテスト。

docs/issues/on_event_judgment_has_no_idempotency_key.md の「決まったこと」:

- 外から届く刺激 (現象の封筒 TriggerEvent) は供給源が発行する永続 ID を必ず持つ
- 同じ刺激の再配送では、ペルソナは何も起動しない (直接応対の経路でも判断の経路でも)
- ID の無い刺激は受け取り口で代理採番せず、ERROR を出して落とす
- 受領記録は保持期間を過ぎた行だけを個別に消す (古さは実時間で判定する)

本番のペルソナ・LLM には触れない — in-memory SQLite とフェイクの manager だけ。
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from builtin_data.phenomena.inject_persona_event import inject_persona_event
from database.models import Base, StimulusReceipt
from phenomena.triggers import TriggerEvent, TriggerType
from saiverse import autonomy_wiring as wiring
from saiverse import stimulus_receipt as receipt

PERSONA_ID = "alice"


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


class _FakeDispatcher:
    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def dispatch_phenomenon_event(self, **kwargs):
        self.calls.append(kwargs)
        return True


def _manager(session_factory, *, autonomy: bool):
    persona = SimpleNamespace(
        persona_id=PERSONA_ID,
        current_building_id="alice_room",
        autonomy_enabled=autonomy,
    )
    recorded: List[Dict[str, Any]] = []
    manager = SimpleNamespace(
        SessionLocal=session_factory,
        personas={PERSONA_ID: persona},
        all_personas={PERSONA_ID: persona},
        pulse_dispatcher=_FakeDispatcher(),
        record_persona_event=lambda **kw: recorded.append(kw),
    )
    return manager, recorded


def _receipt_rows(session_factory) -> List[tuple]:
    db = session_factory()
    try:
        return [
            (r.PERSONA_ID, r.STIMULUS_ID, r.RECEIVED_AT)
            for r in db.query(StimulusReceipt).order_by(StimulusReceipt.RECEIPT_ID)
        ]
    finally:
        db.close()


# ---------------------------------------------------------------------------
# ① 同じ刺激の二度目の配送は何も起動しない
# ---------------------------------------------------------------------------


def test_同じ刺激IDの二度目の配送は直接応対の経路で何も起動しない(session_factory):
    manager, recorded = _manager(session_factory, autonomy=False)

    first = inject_persona_event(
        PERSONA_ID, "メンションが届いた", _manager=manager, _stimulus_id="x:mention:1",
    )
    second = inject_persona_event(
        PERSONA_ID, "メンションが届いた", _manager=manager, _stimulus_id="x:mention:1",
    )

    assert first == "ok"
    assert second == "ok: duplicate stimulus"
    # 応対は一度だけ、イベントの記録 (ペルソナが読む pending event) も一度だけ
    assert len(manager.pulse_dispatcher.calls) == 1
    assert len(recorded) == 1
    # 応対の metadata に刺激の ID が載る
    assert manager.pulse_dispatcher.calls[0]["metadata"]["stimulus_id"] == "x:mention:1"


def test_同じ刺激IDの二度目の配送は判断の経路で何も起動しない(
    session_factory, monkeypatch,
):
    manager, recorded = _manager(session_factory, autonomy=True)
    monkeypatch.setattr(
        "saiverse.day_plan.is_in_user_conversation", lambda *a, **k: False,
    )
    fired: List[Dict[str, Any]] = []

    def _fake_fire(mgr, pid, kind, context=None, **kw):
        fired.append(context)
        return {
            "submitted": True,
            "applied_events": [{"extras": ["reaction=engage_now"]}],
        }

    monkeypatch.setattr(wiring, "fire_judgment_point", _fake_fire)

    for _ in range(2):
        inject_persona_event(
            PERSONA_ID, "掲示板の告知", _manager=manager, _stimulus_id="board:7",
        )

    assert len(fired) == 1
    # 判断の文脈に刺激の ID が同乗する (on_event の冪等キーの元)
    assert fired[0]["stimulus_id"] == "board:7"
    assert len(manager.pulse_dispatcher.calls) == 1
    assert len(recorded) == 1


def test_meta_playbookを明示した直接応対の経路も再配送で起動しない(session_factory):
    manager, recorded = _manager(session_factory, autonomy=True)
    for _ in range(2):
        inject_persona_event(
            PERSONA_ID, "センサー反応", meta_playbook="sensor_handler",
            _manager=manager, _stimulus_id="switchbot:evt:3",
        )
    assert len(manager.pulse_dispatcher.calls) == 1
    assert len(recorded) == 1


def test_受領記録はペルソナごとなので別のペルソナには同じ刺激が届く(session_factory):
    assert receipt.claim_stimulus(
        SimpleNamespace(SessionLocal=session_factory), "alice", "feed:1",
    ) == receipt.CLAIM_ACCEPTED
    assert receipt.claim_stimulus(
        SimpleNamespace(SessionLocal=session_factory), "bob", "feed:1",
    ) == receipt.CLAIM_ACCEPTED
    assert receipt.claim_stimulus(
        SimpleNamespace(SessionLocal=session_factory), "alice", "feed:1",
    ) == receipt.CLAIM_DUPLICATE


# ---------------------------------------------------------------------------
# ② ID の無い刺激は ERROR で落とされる
# ---------------------------------------------------------------------------


def test_刺激IDの無い現象の発火はERRORで落とされ何も起動しない(session_factory, caplog):
    manager, recorded = _manager(session_factory, autonomy=False)
    with caplog.at_level("ERROR"):
        result = inject_persona_event(PERSONA_ID, "メンション", _manager=manager)
    assert result == "error: missing stimulus_id"
    assert manager.pulse_dispatcher.calls == []
    assert recorded == []
    assert _receipt_rows(session_factory) == []
    assert any("stimulus_id" in r.message for r in caplog.records)


def test_封筒は刺激IDなしでは作れない():
    with pytest.raises(TypeError):
        TriggerEvent(type=TriggerType.EXTERNAL_WEBHOOK, data={})  # type: ignore[call-arg]


def test_IDの無い封筒を返すアドオンの出来事はERRORで落とされる(caplog):
    """古い実装のアドオン (空の ID を返す) を IntegrationManager が fail-closed で落とす。"""
    from saiverse.integration_manager import IntegrationManager
    from saiverse.integrations.base import BaseIntegration

    good = TriggerEvent(
        type=TriggerType.EXTERNAL_WEBHOOK, data={"source": "a"}, stimulus_id="hook:1",
    )
    bad = TriggerEvent(
        type=TriggerType.EXTERNAL_WEBHOOK, data={"source": "b"}, stimulus_id="",
    )

    class _Legacy(BaseIntegration):
        name = "legacy"

        def poll(self, manager):
            return [bad, good]

    emitted: List[TriggerEvent] = []
    manager = SimpleNamespace(
        phenomenon_manager=SimpleNamespace(emit=emitted.append),
    )
    im = IntegrationManager.__new__(IntegrationManager)
    im.manager = manager
    im._last_poll = {}
    with caplog.at_level("ERROR", logger="saiverse.integration_manager"):
        im._poll_integration(_Legacy())

    assert emitted == [good]
    assert any("without a stimulus_id" in r.message for r in caplog.records)


def test_現象マネージャは封筒の刺激IDを予約引数で現象まで運ぶ(monkeypatch):
    """ルールの引数マッピングに書かれていなくても _stimulus_id は必ず届く。"""
    from phenomena import manager as pm_mod

    seen: Dict[str, Any] = {}

    def _phenomenon(**kwargs):
        seen.update(kwargs)
        return "ok"

    monkeypatch.setitem(pm_mod.PHENOMENON_REGISTRY, "capture", _phenomenon)
    rule = SimpleNamespace(
        PHENOMENON_NAME="capture", RULE_ID=1, ARGUMENT_MAPPING_JSON=None,
        CONDITION_JSON=None,
    )
    pm = pm_mod.PhenomenonManager(lambda: None, async_execution=False)
    monkeypatch.setattr(pm, "_find_matching_rules", lambda event: [rule])

    pm.emit(TriggerEvent(
        type=TriggerType.EXTERNAL_WEBHOOK, data={}, stimulus_id="hook:42",
    ))
    assert seen["_stimulus_id"] == "hook:42"

    # 非同期キューの組にも載る
    pm_async = pm_mod.PhenomenonManager(lambda: None, async_execution=True)
    monkeypatch.setattr(pm_async, "_find_matching_rules", lambda event: [rule])
    pm_async.emit(TriggerEvent(
        type=TriggerType.EXTERNAL_WEBHOOK, data={}, stimulus_id="hook:43",
    ))
    name, args, rule_id, stimulus_id = pm_async._execution_queue.get_nowait()
    assert (name, rule_id, stimulus_id) == ("capture", 1, "hook:43")


# ---------------------------------------------------------------------------
# ④ 受領記録の掃除は保持期間を過ぎた行だけを消す
# ---------------------------------------------------------------------------


def test_受領記録の掃除は保持期間を過ぎた行だけを消し新しい行は残る(
    session_factory, monkeypatch,
):
    manager = SimpleNamespace(SessionLocal=session_factory)
    base = 1_800_000_000
    now = {"t": base}
    # 古さの判定は実時間 (time.time) — clock.now() の仮想時刻ではない
    monkeypatch.setattr(receipt.time, "time", lambda: now["t"])

    receipt.claim_stimulus(manager, PERSONA_ID, "old")
    now["t"] = base + receipt.RETENTION_SECONDS - 60
    receipt.claim_stimulus(manager, PERSONA_ID, "recent")

    # "old" の受領から保持期間 + 1 秒後。"recent" はまだ保持期間内
    now["t"] = base + receipt.RETENTION_SECONDS + 1
    assert receipt.claim_stimulus(manager, PERSONA_ID, "new") == receipt.CLAIM_ACCEPTED

    ids = [row[1] for row in _receipt_rows(session_factory)]
    assert ids == ["recent", "new"]
    # 保持期間内の行は照合に効き続ける
    assert receipt.claim_stimulus(manager, PERSONA_ID, "recent") == receipt.CLAIM_DUPLICATE
    # 期限切れで消えた ID は新しい刺激として受け付けられる
    assert receipt.claim_stimulus(manager, PERSONA_ID, "old") == receipt.CLAIM_ACCEPTED


def test_受領時刻は仮想クロックではなく実時間で刻まれる(session_factory, monkeypatch):
    from datetime import datetime

    from saiverse import clock

    monkeypatch.setattr(receipt.time, "time", lambda: 1_900_000_000.7)
    clock.enable_virtual(datetime(2030, 1, 1, 0, 0, 0))
    try:
        receipt.claim_stimulus(SimpleNamespace(SessionLocal=session_factory), PERSONA_ID, "s")
    finally:
        clock.disable_virtual()
    assert _receipt_rows(session_factory) == [(PERSONA_ID, "s", 1_900_000_000)]
