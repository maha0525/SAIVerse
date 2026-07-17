"""Session anchor の (persona, model) 行分離テスト (beat_execution_context.md §3.1)。

SEA 監査 S1/S8 の根治を固定する:

- S8: anchor の永続化が session_anchor テーブルの行単位 upsert になり、
  ある model の更新が他 model の行に触れないこと (旧: AI.METABOLISM_ANCHORS
  単一 JSON の全体 read-modify-write)。
- TTL 延命規則 (生存中は max 維持 / 短い書き込みは非スライド、
  docs/intent/cache_lifecycle_control.md §5.2) が行内の前回値との比較として
  従来どおり働くこと。
- S1: touch_anchor_after_llm_call の記帳先が usage.model (実際に応答した
  model) で解決されること。usage.model が空のときのみ persona.model に
  フォールバックすること。
- backfill (database/migrate.py backfill_session_anchors): 旧 JSON → 行分離、
  元列 NULL 化、再実行冪等、既存行 (新形式) は上書きしない。
- TTL watchdog の予約 key が f"ttl:{persona_id}:{model_key}" で (persona,
  model) ごとに独立に登録・cancel されること。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import AI, Base, SessionAnchor
from saiverse.event_scheduler import EventScheduler
from sea.session_lifecycle import SessionLifecycle

PERSONA_ID = "alice"


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    yield Session
    engine.dispose()


def _make_lifecycle(session_factory, scheduler=None):
    manager = SimpleNamespace(
        SessionLocal=session_factory,
        event_scheduler=scheduler,
        meta_layer=SimpleNamespace(
            _load_judgment_config=lambda persona: {
                "keep_cache_alive": True,
                "cache_threshold_ratio": 0.3,
            }
        ),
        personas={},
    )
    runtime = SimpleNamespace(
        run_cache_keepalive=lambda pid, mk=None: None,
    )
    return SessionLifecycle(runtime, manager)


def _now():
    """秒精度の現在時刻 (DB は epoch 秒で持つため、往復比較用に丸める)。"""
    return datetime.now().replace(microsecond=0)


# ---------------------------------------------------------------------------
# 1. 行単位性 (S8 根治の固定)
# ---------------------------------------------------------------------------


def test_upsert_rows_are_independent(session_factory):
    """2 model の entry が独立の行になり、片方の更新が他方に触れない。"""
    lc = _make_lifecycle(session_factory)
    t0 = _now()
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "a1", "updated_at": t0.isoformat(), "ttl_seconds": 300,
    })
    lc.upsert_anchor_entry(PERSONA_ID, "model-b", {
        "anchor_id": "b1", "updated_at": t0.isoformat(), "ttl_seconds": 3600,
    })

    # model-a だけ更新
    t1 = t0 + timedelta(seconds=10)
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "a2", "updated_at": t1.isoformat(), "ttl_seconds": 300,
    })

    a = lc.load_anchor_entry(PERSONA_ID, "model-a")
    assert a == {"anchor_id": "a2", "updated_at": t1.isoformat(), "ttl_seconds": 300}
    # model-b は無傷
    b = lc.load_anchor_entry(PERSONA_ID, "model-b")
    assert b == {"anchor_id": "b1", "updated_at": t0.isoformat(), "ttl_seconds": 3600}

    entries = lc.load_anchor_entries(PERSONA_ID)
    assert set(entries.keys()) == {"model-a", "model-b"}
    # 互換ビュー load_anchors(persona) も同じ内容を返す
    persona = SimpleNamespace(persona_id=PERSONA_ID)
    assert lc.load_anchors(persona) == entries


def test_load_anchor_entry_missing_returns_none(session_factory):
    lc = _make_lifecycle(session_factory)
    assert lc.load_anchor_entry(PERSONA_ID, "no-such-model") is None
    assert lc.load_anchor_entries(PERSONA_ID) == {}


def test_clear_anchor_entries_removes_all_rows(session_factory):
    lc = _make_lifecycle(session_factory)
    t0 = _now()
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {"anchor_id": "a1", "updated_at": t0.isoformat()})
    lc.upsert_anchor_entry(PERSONA_ID, "model-b", {"anchor_id": "b1", "updated_at": t0.isoformat()})
    lc.clear_anchor_entries(PERSONA_ID)
    assert lc.load_anchor_entries(PERSONA_ID) == {}


# ---------------------------------------------------------------------------
# 2. TTL 延命規則 (行内 prev 比較への移植、挙動不変)
# ---------------------------------------------------------------------------


def test_shorter_write_keeps_max_ttl_and_does_not_slide(session_factory):
    """生存中の 1h キャッシュへの 5m 書き込み: TTL は max 維持、起点は非スライド。"""
    lc = _make_lifecycle(session_factory)
    t0 = _now()
    lc.upsert_anchor_entry(PERSONA_ID, "claude-x", {
        "anchor_id": "a1", "updated_at": t0.isoformat(), "ttl_seconds": 3600,
    })
    t1 = t0 + timedelta(seconds=60)
    lc.upsert_anchor_entry(PERSONA_ID, "claude-x", {
        "anchor_id": "a2", "updated_at": t1.isoformat(), "ttl_seconds": 300,
    })
    entry = lc.load_anchor_entry(PERSONA_ID, "claude-x")
    assert entry["ttl_seconds"] == 3600          # 短縮されない (max 維持)
    assert entry["updated_at"] == t0.isoformat()  # 非スライド (起点を維持)
    assert entry["anchor_id"] == "a2"             # anchor 自体は前進する


def test_equal_or_longer_write_refreshes_window(session_factory):
    """同じか長い TTL の書き込みは updated_at をリフレッシュする (keep-awake)。"""
    lc = _make_lifecycle(session_factory)
    t0 = _now()
    lc.upsert_anchor_entry(PERSONA_ID, "claude-x", {
        "anchor_id": "a1", "updated_at": t0.isoformat(), "ttl_seconds": 300,
    })
    t1 = t0 + timedelta(seconds=60)
    lc.upsert_anchor_entry(PERSONA_ID, "claude-x", {
        "anchor_id": "a2", "updated_at": t1.isoformat(), "ttl_seconds": 3600,
    })
    entry = lc.load_anchor_entry(PERSONA_ID, "claude-x")
    assert entry["ttl_seconds"] == 3600
    assert entry["updated_at"] == t1.isoformat()


def test_write_after_expiry_resets(session_factory):
    """完全失効後の書き込みは新しい TTL / 時刻でリセットされる。"""
    lc = _make_lifecycle(session_factory)
    t0 = _now() - timedelta(hours=3)
    lc.upsert_anchor_entry(PERSONA_ID, "claude-x", {
        "anchor_id": "a1", "updated_at": t0.isoformat(), "ttl_seconds": 3600,
    })
    t1 = _now()  # t0 + 3h > TTL 1h — 失効済み
    lc.upsert_anchor_entry(PERSONA_ID, "claude-x", {
        "anchor_id": "a2", "updated_at": t1.isoformat(), "ttl_seconds": 300,
    })
    entry = lc.load_anchor_entry(PERSONA_ID, "claude-x")
    assert entry["ttl_seconds"] == 300
    assert entry["updated_at"] == t1.isoformat()


def test_write_without_ttl_drops_stored_ttl(session_factory):
    """ttl_seconds 無しの書き込み (metabolism の anchor 前進) は旧挙動どおり
    entry を丸ごと置き換える (前回の ttl_seconds は引き継がない)。"""
    lc = _make_lifecycle(session_factory)
    t0 = _now()
    lc.upsert_anchor_entry(PERSONA_ID, "claude-x", {
        "anchor_id": "a1", "updated_at": t0.isoformat(), "ttl_seconds": 3600,
    })
    t1 = t0 + timedelta(seconds=60)
    lc.upsert_anchor_entry(PERSONA_ID, "claude-x", {
        "anchor_id": "a2", "updated_at": t1.isoformat(),
    })
    entry = lc.load_anchor_entry(PERSONA_ID, "claude-x")
    assert entry == {"anchor_id": "a2", "updated_at": t1.isoformat()}


def test_update_anchor_for_model_delegates_to_row_upsert(session_factory):
    """update_anchor_for_model (既存呼び出し面) が行 upsert に落ちること。"""
    lc = _make_lifecycle(session_factory)
    persona = SimpleNamespace(persona_id=PERSONA_ID)
    lc.update_anchor_for_model(persona, "claude-x", "a1", 300)
    entry = lc.load_anchor_entry(PERSONA_ID, "claude-x")
    assert entry["anchor_id"] == "a1"
    assert entry["ttl_seconds"] == 300


# ---------------------------------------------------------------------------
# 3. 実 model 記帳 (S1 根治の固定)
# ---------------------------------------------------------------------------


def _touch_persona(model="std-model"):
    # anchor は call-local 引数で渡す (§6-5 で persona 属性は廃止)。
    return SimpleNamespace(persona_id=PERSONA_ID, model=model)


def _usage(model="light-model"):
    return SimpleNamespace(
        model=model, input_tokens=100, output_tokens=5,
        cached_tokens=0, cache_write_tokens=0, cache_ttl="",
    )


def _wire_touch(lc):
    lc.get_anchor_validity_seconds = lambda mk, pid=None: 1200
    scheduled = []
    lc.schedule_cache_ttl_pulse = lambda persona, mk, ct: scheduled.append(mk)
    lc.check_token_threshold = lambda persona, mk, usage: None
    return scheduled


@patch("saiverse.model_configs.get_cache_config", return_value={"type": "implicit"})
def test_touch_routes_to_actual_usage_model(_mock_cache, session_factory):
    """usage.model="light-model" / persona.model="std-model" のとき、
    light-model の行だけが touch される (呼んでいない model を動かさない)。"""
    lc = _make_lifecycle(session_factory)
    scheduled = _wire_touch(lc)
    persona = _touch_persona(model="std-model")

    lc.touch_anchor_after_llm_call(persona, _usage(model="light-model"), anchor_id="anchor-1")

    light = lc.load_anchor_entry(PERSONA_ID, "light-model")
    assert light is not None and light["anchor_id"] == "anchor-1"
    assert lc.load_anchor_entry(PERSONA_ID, "std-model") is None
    # 見張り予約も実 model 側に入る
    assert scheduled == ["light-model"]


@patch("saiverse.model_configs.get_cache_config", return_value={"type": "implicit"})
def test_touch_falls_back_to_persona_model_when_usage_model_empty(_mock_cache, session_factory):
    lc = _make_lifecycle(session_factory)
    scheduled = _wire_touch(lc)
    persona = _touch_persona(model="std-model")

    lc.touch_anchor_after_llm_call(persona, _usage(model=""), anchor_id="anchor-1")

    std = lc.load_anchor_entry(PERSONA_ID, "std-model")
    assert std is not None and std["anchor_id"] == "anchor-1"
    assert lc.load_anchor_entry(PERSONA_ID, "light-model") is None
    assert scheduled == ["std-model"]


@patch("saiverse.model_configs.get_cache_config", return_value={"type": "explicit"})
def test_touch_explicit_cache_miss_does_not_write_row(_mock_cache, session_factory):
    """explicit で cache_read=cache_write=0 → touch しない (既存挙動の維持)。"""
    lc = _make_lifecycle(session_factory)
    _wire_touch(lc)
    persona = _touch_persona(model="std-model")

    lc.touch_anchor_after_llm_call(persona, _usage(model="claude-x"), anchor_id="anchor-1")

    assert lc.load_anchor_entries(PERSONA_ID) == {}


# ---------------------------------------------------------------------------
# 4. backfill (METABOLISM_ANCHORS JSON → session_anchor 行)
# ---------------------------------------------------------------------------


def _read_backfill_state(db_path):
    engine = create_engine(f"sqlite:///{db_path}")
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        rows = {
            (r.PERSONA_ID, r.MODEL_KEY): r
            for r in db.query(SessionAnchor).all()
        }
        anchors_col = db.query(AI).filter_by(AIID=PERSONA_ID).first().METABOLISM_ANCHORS
        return {
            "rows": {
                key: (r.ANCHOR_MESSAGE_ID, r.TTL_SECONDS, r.UPDATED_AT)
                for key, r in rows.items()
            },
            "column": anchors_col,
        }
    finally:
        db.close()
        engine.dispose()


def test_backfill_splits_json_and_nulls_column(tmp_path):
    db_path = tmp_path / "saiverse.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    old_json = json.dumps({
        "model-a": {"anchor_id": "a1", "updated_at": "2026-07-01T10:00:00", "ttl_seconds": 300},
        "model-b": {"anchor_id": "b1", "updated_at": "2026-07-01T11:00:00"},
    })
    db.add(AI(AIID=PERSONA_ID, HOME_CITYID=1, AINAME="Alice", METABOLISM_ANCHORS=old_json))
    # 既に新形式の行がある model は上書きしない (新形式が正)
    db.add(SessionAnchor(
        PERSONA_ID=PERSONA_ID, MODEL_KEY="model-a",
        ANCHOR_MESSAGE_ID="new-a", TTL_SECONDS=3600, UPDATED_AT=1750000000,
    ))
    db.commit()
    db.close()
    engine.dispose()

    from database.migrate import backfill_session_anchors
    backfill_session_anchors(str(db_path))

    state = _read_backfill_state(db_path)
    # model-a: 既存の新形式行が勝つ (JSON からの上書きなし)
    assert state["rows"][(PERSONA_ID, "model-a")] == ("new-a", 3600, 1750000000)
    # model-b: JSON から行分離 (ttl 無し → NULL)
    anchor_id, ttl, updated = state["rows"][(PERSONA_ID, "model-b")]
    assert anchor_id == "b1"
    assert ttl is None
    assert updated == int(datetime.fromisoformat("2026-07-01T11:00:00").timestamp())
    # 元列は NULL 化されている
    assert state["column"] is None

    # 再実行しても冪等 (何も変わらない)
    backfill_session_anchors(str(db_path))
    assert _read_backfill_state(db_path) == state


def test_backfill_noop_when_column_already_null(tmp_path):
    db_path = tmp_path / "saiverse.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    db.add(AI(AIID=PERSONA_ID, HOME_CITYID=1, AINAME="Alice", METABOLISM_ANCHORS=None))
    db.commit()
    db.close()
    engine.dispose()

    from database.migrate import backfill_session_anchors
    backfill_session_anchors(str(db_path))

    state = _read_backfill_state(db_path)
    assert state["rows"] == {}
    assert state["column"] is None


# ---------------------------------------------------------------------------
# 5. TTL watchdog の (persona, model) 独立予約
# ---------------------------------------------------------------------------


def test_watchdog_reservations_are_model_scoped(session_factory):
    """(persona, model) 2 予約が独立に登録・cancel される。"""
    scheduler = EventScheduler()  # start() しない (同期検証)
    lc = _make_lifecycle(session_factory, scheduler=scheduler)
    lc.get_anchor_validity_seconds = lambda mk, pid=None: 1200
    persona = SimpleNamespace(persona_id=PERSONA_ID, model="std-model")

    lc.schedule_cache_ttl_pulse(persona, "std-model", "explicit")
    lc.schedule_cache_ttl_pulse(persona, "light-model", "explicit")

    assert scheduler.has_key(f"ttl:{PERSONA_ID}:std-model")
    assert scheduler.has_key(f"ttl:{PERSONA_ID}:light-model")

    # 片方の cancel は他方に触れない
    assert scheduler.cancel(f"ttl:{PERSONA_ID}:std-model") is True
    assert not scheduler.has_key(f"ttl:{PERSONA_ID}:std-model")
    assert scheduler.has_key(f"ttl:{PERSONA_ID}:light-model")


def test_watchdog_callback_carries_model_key(session_factory):
    """予約 callback が run_cache_keepalive(persona_id, model_key) を呼ぶ。"""
    scheduled = {}
    scheduler = SimpleNamespace(
        schedule=lambda fire_at, callback, key: scheduled.update({key: callback}),
        cancel=lambda key: False,
    )
    lc = _make_lifecycle(session_factory, scheduler=scheduler)
    calls = []
    lc.runtime = SimpleNamespace(
        run_cache_keepalive=lambda pid, mk: calls.append((pid, mk)),
    )
    lc.get_anchor_validity_seconds = lambda mk, pid=None: 1200
    persona = SimpleNamespace(persona_id=PERSONA_ID, model="std-model")

    lc.schedule_cache_ttl_pulse(persona, "light-model", "explicit")
    key = f"ttl:{PERSONA_ID}:light-model"
    assert key in scheduled
    scheduled[key]()
    assert calls == [(PERSONA_ID, "light-model")]
