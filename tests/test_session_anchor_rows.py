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
import os
import threading
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


# ---------------------------------------------------------------------------
# arasuji_levels.md §13 (2026-07-29): 起点の TTL 失効は温度情報のみ —
# 提示範囲 (ウィンドウ) の所属を変えない
# ---------------------------------------------------------------------------


def test_resolve_returns_expired_self_anchor(session_factory):
    """TTL がとうに切れていても、自 model の行が起点として返る (§13 裁定1)。"""
    lc = _make_lifecycle(session_factory)
    stale = _now() - timedelta(days=3)
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "a1", "updated_at": stale.isoformat(), "ttl_seconds": 300,
    })
    persona = SimpleNamespace(persona_id=PERSONA_ID, model="model-a")
    assert lc.resolve_metabolism_anchor(persona) == ("a1", "self")


def test_resolve_borrows_most_recent_other_model_row(session_factory):
    """自 model の行が無ければ、直近更新の他 model の起点を借りる (温度不問)。"""
    lc = _make_lifecycle(session_factory)
    old = _now() - timedelta(days=7)
    newer = _now() - timedelta(days=1)
    lc.upsert_anchor_entry(PERSONA_ID, "model-b", {
        "anchor_id": "b1", "updated_at": old.isoformat(), "ttl_seconds": 300,
    })
    lc.upsert_anchor_entry(PERSONA_ID, "model-c", {
        "anchor_id": "c1", "updated_at": newer.isoformat(), "ttl_seconds": 300,
    })
    persona = SimpleNamespace(persona_id=PERSONA_ID, model="model-a")
    assert lc.resolve_metabolism_anchor(persona) == ("c1", "other")


def test_resolve_without_rows_is_bootstrap(session_factory):
    """起点行が一つも無い (新規ペルソナ / 修復直後) だけが minimal になる。"""
    lc = _make_lifecycle(session_factory)
    persona = SimpleNamespace(persona_id=PERSONA_ID, model="model-a")
    assert lc.resolve_metabolism_anchor(persona) == (None, "minimal")


# ---------------------------------------------------------------------------
# §13 裁定4: 手動畳み (run_manual_compaction) — 範囲規則は自動と同一
# ---------------------------------------------------------------------------


def _window(anchor_id, presented):
    from sea.session_window import SessionWindow
    return SessionWindow(
        anchor_id=anchor_id, raw=list(presented), presented=list(presented), folds=[],
    )


def _perception_block(chars, at=100):
    """送信直前に差し込まれる知覚ブロック (保存行ではないので id が無い)。

    水位を測る側は保存行だけでなくこれも数える (2026-09-02 まはー裁定 —
    docs/issues/context_accounting_excludes_injected_rows.md)。
    """
    from sea.eviction_plan import CONSUMED_PERCEPTION_KEY
    return {
        "role": "user",
        "content": "p" * chars,
        "created_at": at,
        "metadata": {
            "tags": ["internal", "event_message", "perception"],
            CONSUMED_PERCEPTION_KEY: True,
        },
    }


def _chronicle_on(lc):
    """手動畳みテスト用: Chronicle 生成の門 (ペルソナ設定) を開ける。

    かつては env ENABLE_MEMORY_WEAVE_CONTEXT との二段だったが、2026-09-01 に
    env 側を撤去した (.env に行が無いアップグレード組で記憶の整理が全停止した
    実害)。門はペルソナ設定 AI.CHRONICLE_ENABLED だけ — テスト DB に AI 行が
    無ければ既定 True になるが、前提を明示するために patch してから回す。
    """
    return patch.object(lc, "is_chronicle_enabled_for_persona", return_value=True)


def _chronicle_off(lc):
    """Chronicle 無効 (ペルソナ設定 OFF) — 唯一の「無効」の作り方。"""
    return patch.object(lc, "is_chronicle_enabled_for_persona", return_value=False)


def test_manual_compaction_runs_metabolism_with_force(session_factory):
    """残す量超過なら run_metabolism を chronicle_force=True で一度だけ呼ぶ。"""
    from sea.eviction_plan import Watermarks
    lc = _make_lifecycle(session_factory)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a", current_building_id="room",
    )
    msgs = [{"id": f"m{i}", "content": "x" * 1000} for i in range(10)]
    with _chronicle_on(lc), \
            patch.object(lc, "get_metabolism_watermarks",
                         return_value=Watermarks(low=1000, target=2000, high=4000)), \
            patch.object(lc, "get_presented_window", return_value=_window("m0", msgs)), \
            patch.object(lc, "run_metabolism", return_value="ok") as run:
        assert lc.run_manual_compaction(persona) == "ok"
    run.assert_called_once()
    assert run.call_args.kwargs.get("chronicle_force") is True
    # 材料 U 未満の端数 fold (小粒) を許すのは非常経路だけ — 手動入口は不可。
    assert not run.call_args.kwargs.get("close_undersized_tail")
    assert run.call_args.kwargs.get("stop_when_disabled") is True
    assert run.call_args.kwargs.get("model_key") == "model-a"


def test_manual_compaction_propagates_failure_and_cancel(session_factory):
    """編纂の失敗/キャンセルを成功に偽装しない (Codex 2026-07-29 指摘の根治)。

    run_metabolism の "failed" / "deferred" はそのまま呼び出し元へ返り、
    "nothing" (畳める範囲なし) だけが "noop" に写る。cancellation_token は
    そのまま run_metabolism へ渡る。
    """
    from sea.cancellation import CancellationToken
    from sea.eviction_plan import Watermarks
    lc = _make_lifecycle(session_factory)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a", current_building_id="room",
    )
    msgs = [{"id": f"m{i}", "content": "x" * 1000} for i in range(10)]
    token = CancellationToken()
    for inner, expected in [
        ("failed", "failed"), ("deferred", "deferred"),
        ("deferred_sluice_unseen", "deferred_sluice_unseen"), ("nothing", "noop"),
    ]:
        with _chronicle_on(lc), \
                patch.object(lc, "get_metabolism_watermarks",
                             return_value=Watermarks(low=1000, target=2000, high=4000)), \
                patch.object(lc, "get_presented_window", return_value=_window("m0", msgs)), \
                patch.object(lc, "run_metabolism", return_value=inner) as run:
            assert lc.run_manual_compaction(
                persona, cancellation_token=token,
            ) == expected
        assert run.call_args.kwargs.get("cancellation_token") is token


def test_manual_compaction_noop_at_or_below_target(session_factory):
    """残す量以下なら畳まない — 手動でも直近の生ログには手を付けない。"""
    from sea.eviction_plan import Watermarks
    lc = _make_lifecycle(session_factory)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a", current_building_id="room",
    )
    msgs = [{"id": "m0", "content": "x" * 100}]
    with _chronicle_on(lc), \
            patch.object(lc, "get_metabolism_watermarks",
                         return_value=Watermarks(low=1000, target=2000, high=4000)), \
            patch.object(lc, "get_presented_window", return_value=_window("m0", msgs)), \
            patch.object(lc, "run_metabolism") as run:
        assert lc.run_manual_compaction(persona) == "noop"
    run.assert_not_called()


def test_manual_compaction_gate_counts_rows_only(session_factory, caplog):
    """手動整理の門は**会話の行だけ**を残す量と比べる (2026-09-03 まはー裁定)。

    残す量の主語は「会話の行の量」。走行側 (plan_eviction) の保護範囲も行だけで
    測るので、行が残す量以下の窓は門を通しても計画が空で "nothing" に終わる —
    門で "noop" を返すのが一貫した答え (門と本走行が別の答えを出さない)。
    知覚ぶんで合計が上限を超えている窓は、畳めるものが無い旨を 1 度だけ
    WARNING に出す (docs/issues/protection_quota_consumed_by_perception_blocks.md)。
    """
    import logging

    from sea.eviction_plan import Watermarks
    lc = _make_lifecycle(session_factory)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a", current_building_id="room",
    )
    msgs = [{"id": "m0", "content": "x" * 1500}]
    wm = Watermarks(low=1000, target=2000, high=4000)
    with _chronicle_on(lc), \
            patch.object(lc, "get_metabolism_watermarks", return_value=wm), \
            patch.object(lc, "get_presented_window",
                         return_value=_window("m0", msgs)), \
            patch.object(lc, "run_metabolism", return_value="ok") as run:
        # 保存行 1,500 字 <= 残す量 2,000 → 門前払い。
        assert lc.run_manual_compaction(persona) == "noop"
        # 知覚 1,000 字を足しても行は 1,500 字のまま → やはり門前払い
        # (合計 2,500 は上限 4,000 以下なので警告も出ない)。
        with caplog.at_level(logging.WARNING, logger="sea.session_lifecycle"), \
                patch.object(lc, "perception_blocks_for",
                             return_value=[_perception_block(1000)]):
            assert lc.run_manual_compaction(persona) == "noop"
            assert not [r for r in caplog.records if "perception blocks" in r.getMessage()]
        # 知覚 3,000 字で合計 4,500 > 上限 → 畳めるものが無い旨を 1 度だけ警告。
        with caplog.at_level(logging.WARNING, logger="sea.session_lifecycle"), \
                patch.object(lc, "perception_blocks_for",
                             return_value=[_perception_block(3000)]):
            assert lc.run_manual_compaction(persona) == "noop"
            assert lc.run_manual_compaction(persona) == "noop"
            warned = [r for r in caplog.records if "perception blocks" in r.getMessage()]
            assert len(warned) == 1
    run.assert_not_called()


def test_manual_compaction_unavailable_without_anchor(session_factory):
    """起点行が無い (ブートストラップ前) は畳みを定義できず unavailable。"""
    from sea.eviction_plan import Watermarks
    lc = _make_lifecycle(session_factory)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a", current_building_id="room",
    )
    with _chronicle_on(lc), \
            patch.object(lc, "get_metabolism_watermarks",
                         return_value=Watermarks(low=1000, target=2000, high=4000)), \
            patch.object(lc, "get_presented_window", return_value=_window(None, [])), \
            patch.object(lc, "run_metabolism") as run:
        assert lc.run_manual_compaction(persona) == "unavailable"
    run.assert_not_called()


def test_manual_compaction_disabled_does_not_fold(session_factory):
    """Chronicle 無効 (persona トグル OFF) は畳まず "disabled"。

    手動入口の同意文は「Chronicle に畳む」— 編纂なしの退場 (忘却) を黙って
    実行しない (Codex 再レビュー 2026-07-29)。

    2026-09-01: 旧 (a) 「weave env が無い / OFF」の枝は消えた — env
    ENABLE_MEMORY_WEAVE_CONTEXT を撤去し、門はペルソナ設定だけになったため。
    """
    from sea.eviction_plan import Watermarks
    lc = _make_lifecycle(session_factory)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a", current_building_id="room",
    )
    msgs = [{"id": f"m{i}", "content": "x" * 1000} for i in range(10)]

    with _chronicle_off(lc), \
            patch.object(lc, "get_metabolism_watermarks",
                         return_value=Watermarks(low=1000, target=2000, high=4000)), \
            patch.object(lc, "get_presented_window", return_value=_window("m0", msgs)), \
            patch.object(lc, "run_metabolism") as run:
        assert lc.run_manual_compaction(persona) == "disabled"
    run.assert_not_called()


def test_manual_compaction_runs_without_any_env_gate(session_factory):
    """env が一切設定されていなくても畳みは走る (2026-09-01 撤去の回帰止め)。

    .env に ENABLE_MEMORY_WEAVE_CONTEXT の行が無いアップグレード組で記憶の整理が
    全停止した実害の再発防止。環境を空にしても "disabled" に落ちない。
    """
    from sea.eviction_plan import Watermarks
    lc = _make_lifecycle(session_factory)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a", current_building_id="room",
    )
    msgs = [{"id": f"m{i}", "content": "x" * 1000} for i in range(10)]

    with patch.dict(os.environ, {}, clear=True), \
            patch.object(lc, "get_metabolism_watermarks",
                         return_value=Watermarks(low=1000, target=2000, high=4000)), \
            patch.object(lc, "get_presented_window", return_value=_window("m0", msgs)), \
            patch.object(lc, "run_metabolism", return_value="ok") as run:
        assert lc.run_manual_compaction(persona) == "ok"
    run.assert_called_once()


def _count_head_rebuilds():
    """手動入口が発火した head 再構築 (on_metabolism) の回数を数える patch。"""
    calls = []
    return calls, patch(
        "saiverse.dynamic_state.DynamicStateManager.on_metabolism",
        lambda persona, manager, model_key=None: calls.append(model_key),
    )


def test_manual_compaction_rebuilds_head_when_nothing_was_folded(session_factory):
    """畳めなかった結果でも head 再構築を 1 回だけ発火する。

    手動入口の契約 (2026-09-01): ボタンを押した以上、畳みが起きなくても設定
    トグル (Memopedia 索引の常時表示など) の変更がコンテキストへ反映される。
    早期 return の全ての出口 (noop / unavailable / disabled) と run_metabolism
    経由の失敗系が、どれも 1 回に揃うことを固定する。
    """
    from sea.eviction_plan import Watermarks
    lc = _make_lifecycle(session_factory)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a", current_building_id="room",
    )
    big = [{"id": f"m{i}", "content": "x" * 1000} for i in range(10)]
    small = [{"id": "m0", "content": "x" * 100}]
    marks = Watermarks(low=1000, target=2000, high=4000)

    # (presented, run_metabolism の戻り値, 期待 status)
    cases = [
        (small, None, "noop"),                     # 残す量以下 (早期 return)
        (None, None, "unavailable"),               # 起点行なし (早期 return)
        (big, "failed", "failed"),                 # 編纂失敗
        (big, "deferred", "deferred"),             # claim 競合 / キャンセル
        (big, "nothing", "noop"),                  # 畳める範囲なし
    ]
    for presented, inner, expected in cases:
        window = _window("m0", presented) if presented else _window(None, [])
        calls, counting = _count_head_rebuilds()
        with counting, _chronicle_on(lc), \
                patch.object(lc, "get_metabolism_watermarks", return_value=marks), \
                patch.object(lc, "get_presented_window", return_value=window), \
                patch.object(lc, "run_metabolism", return_value=inner):
            assert lc.run_manual_compaction(persona) == expected
        assert len(calls) == 1, f"{expected}: {len(calls)} rebuild(s)"
        # 再構築するのは畳みを試みた model の (persona, model) snapshot
        assert calls[0] == "model-a"

    # Chronicle 無効 (ペルソナ設定 OFF) も手動入口なので 1 回
    calls, counting = _count_head_rebuilds()
    with counting, _chronicle_off(lc):
        assert lc.run_manual_compaction(persona) == "disabled"
    assert len(calls) == 1


def test_manual_compaction_does_not_rebuild_head_twice_on_success(session_factory):
    """"ok" では外側から発火しない — 畳み本体が既に発火しているため。

    二重の capture_all (全セクション再構築 + スナップショット永続化) を避ける
    (Codex 指摘 2026-09-01)。ここでは run_metabolism を stub しているので内部
    発火も起きず、合計 0 回になる = 外側が上乗せしていないことの検査。

    契約のもう半分「畳み本体は成功時に内部で 1 回発火する」は
    tests/test_metabolism_two_layer.py の
    test_run_metabolism_dispatches_with_advancing_model が実経路で固定して
    いる — 本テストと対で「成功時は合計ちょうど 1 回」になる。
    """
    from sea.eviction_plan import Watermarks
    lc = _make_lifecycle(session_factory)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a", current_building_id="room",
    )
    msgs = [{"id": f"m{i}", "content": "x" * 1000} for i in range(10)]
    calls, counting = _count_head_rebuilds()
    with counting, _chronicle_on(lc), \
            patch.object(lc, "get_metabolism_watermarks",
                         return_value=Watermarks(low=1000, target=2000, high=4000)), \
            patch.object(lc, "get_presented_window", return_value=_window("m0", msgs)), \
            patch.object(lc, "run_metabolism", return_value="ok"):
        assert lc.run_manual_compaction(persona) == "ok"
    assert calls == []


def test_manual_compaction_checked_reports_stale_head(session_factory):
    """head の weave が撮り直されていなければ head_rebuilt=False で返す。

    §15 読み戻しと同じ指紋比較 — dispatch が成功しても capture_all は section の
    capture 例外で既存オブジェクトを使い回す (stale-but-real) ので、identity が
    変わらなければ「組み直せていない」。畳み自体の status は巻き込まない。
    """
    from sea.eviction_plan import Watermarks
    lc = _make_lifecycle(session_factory)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a", current_building_id="room",
    )
    msgs = [{"id": f"m{i}", "content": "x" * 1000} for i in range(10)]
    marks = Watermarks(low=1000, target=2000, high=4000)
    stale_weave = object()

    def _run(snapshots):
        calls, counting = _count_head_rebuilds()
        with counting, _chronicle_on(lc), \
                patch.object(lc, "get_metabolism_watermarks", return_value=marks), \
                patch.object(lc, "get_presented_window", return_value=_window("m0", msgs)), \
                patch.object(lc, "run_metabolism", return_value="failed"), \
                patch.object(lc, "_head_weave_snapshot", side_effect=snapshots):
            return lc.run_manual_compaction_checked(persona)

    # 前後で同じオブジェクト = 使い回し → 組み直せていない
    status, head_rebuilt = _run([stale_weave, stale_weave])
    assert status == "failed"
    assert head_rebuilt is False

    # 別オブジェクトに入れ替わっていれば組み直せている
    status, head_rebuilt = _run([object(), object()])
    assert status == "failed"
    assert head_rebuilt is True

    # weave が元々無い環境 (head 未初期化 / weave 無効) は失敗扱いにしない
    status, head_rebuilt = _run([None, None])
    assert head_rebuilt is True


def _run_checked_with_pipeline(lc, persona, pipeline_factory):
    """head pipeline を差し替えて run_manual_compaction_checked を回す。

    指紋を撮る経路 (_head_weave_snapshot) を本物のまま通したいので、
    パッチするのは pipeline の取得口だけ。畳みは noop で終わらせる。
    """
    from sea.eviction_plan import Watermarks
    calls, counting = _count_head_rebuilds()
    with counting, _chronicle_on(lc), \
            patch("sea.head_pipeline.get_default_pipeline", pipeline_factory), \
            patch.object(lc, "get_metabolism_watermarks",
                         return_value=Watermarks(low=1000, target=2000, high=4000)), \
            patch.object(lc, "get_presented_window", return_value=_window("m0", [])):
        return lc.run_manual_compaction_checked(persona)


def test_head_inspection_failure_is_reported_not_swallowed(session_factory):
    """指紋の検査自体が失敗したら head_rebuilt=False (= 画面へ警告を出す側)。

    検査の失敗を「weave が無い」と同じ None に潰すと、stale な head が
    「組み直せました」の顔で通り、知らせるための機構が黙る (Codex 指摘
    2026-09-01)。分からないときは黙らない。
    """
    lc = _make_lifecycle(session_factory)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a", current_building_id="room",
    )

    def _boom():
        raise RuntimeError("head pipeline unavailable")

    status, head_rebuilt = _run_checked_with_pipeline(lc, persona, _boom)
    assert status == "noop"
    assert head_rebuilt is False


def test_absent_weave_is_not_treated_as_failure(session_factory):
    """weave が正当に無い環境 (head 未初期化 / weave 無効) は警告を出さない。

    残りようが無いものを「組み直せていない」と数えると、常態が異常の顔で
    画面に出る。検査失敗 (上のテスト) と区別できていることの対。
    """
    lc = _make_lifecycle(session_factory)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a", current_building_id="room",
    )
    empty_pipeline = SimpleNamespace(get_snapshot=lambda persona_id, model_key: None)

    status, head_rebuilt = _run_checked_with_pipeline(
        lc, persona, lambda: empty_pipeline,
    )
    assert status == "noop"
    assert head_rebuilt is True


def test_manual_compaction_checked_survives_dispatch_exception(session_factory):
    """発火が例外で落ちても、判定は head_rebuilt=False に倒れるだけで畳みは返る。"""
    from sea.eviction_plan import Watermarks
    lc = _make_lifecycle(session_factory)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a", current_building_id="room",
    )
    stale_weave = object()

    def _boom(persona, manager, model_key=None):
        raise RuntimeError("head pipeline down")

    with patch("saiverse.dynamic_state.DynamicStateManager.on_metabolism", _boom), \
            _chronicle_on(lc), \
            patch.object(lc, "get_metabolism_watermarks",
                         return_value=Watermarks(low=1000, target=2000, high=4000)), \
            patch.object(lc, "get_presented_window", return_value=_window("m0", [])), \
            patch.object(lc, "_head_weave_snapshot", return_value=stale_weave):
        status, head_rebuilt = lc.run_manual_compaction_checked(persona)

    assert status == "noop"          # 畳みの結果は例外に巻き込まれない
    assert head_rebuilt is False


def test_run_metabolism_manual_stops_when_disabled_under_lock(session_factory):
    """ロック内の再判定 (TOCTOU, Codex 三巡 2026-07-29): 入口の事前判定の後に
    Chronicle が OFF へ反転しても、手動 (stop_when_disabled=True) は編纂なしの
    退場へ進まず "disabled" で止まる。自動・§14 経路 (False) は従来どおり進む。"""
    from sea.eviction_plan import Watermarks
    lc = _make_lifecycle(session_factory)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a", current_building_id="room",
    )
    wm = Watermarks(low=1000, target=2000, high=4000)

    # 手動: disabled なら提示ウィンドウの取り直しにすら進まない
    with _chronicle_off(lc), \
            patch.object(lc, "get_presented_window") as gw:
        status = lc._run_metabolism_locked(
            persona, "room", _window("m0", []), wm,
            chronicle_force=True, stop_when_disabled=True,
        )
    assert status == "disabled"
    gw.assert_not_called()

    # 自動: disabled でも早期 return しない (空ウィンドウまで進み "nothing")
    with _chronicle_off(lc), \
            patch.object(lc, "get_presented_window", return_value=_window(None, [])):
        status = lc._run_metabolism_locked(
            persona, "room", _window(None, []), wm, chronicle_force=False,
        )
    assert status == "nothing"

    # §14 経路 (chronicle_force=True + stop_when_disabled=False): disabled でも
    # 止まらない — 非常畳み・先回り畳みは Chronicle 無効の persona も前進で救う。
    with _chronicle_off(lc), \
            patch.object(lc, "get_presented_window", return_value=_window(None, [])):
        status = lc._run_metabolism_locked(
            persona, "room", _window(None, []), wm, chronicle_force=True,
        )
    assert status == "nothing"


def test_run_metabolism_forwards_close_undersized_tail_to_the_plan(session_factory):
    """run_metabolism の close_undersized_tail が退場計画 (plan_eviction) まで届く。

    U 判定が材料字数になった (2026-08-29 裁定) ため、「生は巨大だが材料が薄い」
    期間は通常計画が fold を閉じられない。非常畳み (§14-3) だけがこのフラグで
    材料 U 未満の端数を閉じて前進する — 配管が切れると非常畳みでも閉じられなく
    なるので、フラグが計画まで届くことを固定する。
    """
    from sea.eviction_plan import EvictionPlan, Watermarks

    lc = _make_lifecycle(session_factory)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a", current_building_id="room",
    )
    wm = Watermarks(low=1000, target=2000, high=4000)
    msgs = [{"id": f"m{i}", "content": "x" * 1000} for i in range(4)]

    with _chronicle_off(lc), \
            patch.object(lc, "get_presented_window", return_value=_window("m0", msgs)), \
            patch("sea.session_lifecycle.plan_eviction",
                  return_value=EvictionPlan()) as plan:
        status = lc._run_metabolism_locked(
            persona, "room", _window("m0", msgs), wm,
            chronicle_force=True, close_undersized_tail=True,
        )
    assert status == "nothing"  # 空計画で早期終了 — 配管の観測だけが目的
    assert plan.call_args.kwargs.get("close_undersized_tail") is True

    # 既定 (自動・手動・先回り経路) は False のまま計画へ渡る。
    with _chronicle_off(lc), \
            patch.object(lc, "get_presented_window", return_value=_window("m0", msgs)), \
            patch("sea.session_lifecycle.plan_eviction",
                  return_value=EvictionPlan()) as plan:
        lc._run_metabolism_locked(persona, "room", _window("m0", msgs), wm)
    assert plan.call_args.kwargs.get("close_undersized_tail") is False


# ---------------------------------------------------------------------------
# arasuji_levels.md §14 (2026-07-29): 冷えたウィンドウの保守
# ---------------------------------------------------------------------------


def _memory_conn(tmp_path):
    """messages + arasuji_entries を持つ per-persona 記憶 DB (実スキーマ)。"""
    from sai_memory.arasuji import init_arasuji_tables
    from sai_memory.memopedia import init_memopedia_tables
    from sai_memory.memory.storage import init_db

    conn = init_db(str(tmp_path / "memory.db"), check_same_thread=False)
    init_memopedia_tables(conn)
    init_arasuji_tables(conn)
    return conn


def _add_message(conn, msg_id, created_at, line_role="main_line"):
    conn.execute(
        "INSERT INTO messages (id, thread_id, role, content, created_at, "
        "metadata, line_role) VALUES (?, 't-main', 'user', 'hello', ?, NULL, ?)",
        (msg_id, created_at, line_role),
    )
    conn.commit()


def _add_l1_entry(conn, source_ids):
    from sai_memory.arasuji.storage import create_entry
    return create_entry(
        conn, level=1, content="digest", source_ids=list(source_ids),
        source_count=len(source_ids), message_count=len(source_ids),
    )


def _adapter(conn):
    # _db_lock は sluice のパンマーカー読み書き (_load_pan_marker /
    # _save_pan_marker) が使う。機構1 の頭打ち判定がそれを通るので、実物と
    # 同じく再入可能ロックを持たせる。
    return SimpleNamespace(
        conn=conn, is_ready=lambda: True, _db_lock=threading.RLock(),
    )


def _set_pan_marker(persona, last_id):
    """スルースが ``last_id`` まで見た状態にする (永続 + persona 属性)。"""
    from sea.sluice import _save_pan_marker
    _save_pan_marker(persona, last_id)


def test_frontier_anchor_id_derivation(tmp_path):
    """§14-2: 最前線 = 編纂対象なのに未編纂の、正典順で最初のメッセージ。

    一次エントリが無ければ None (編纂の実績なし)、全編纂済みでも None。
    編纂対象外 (sub_line 等) のメッセージは最前線の判定に混ざらない。
    """
    from sai_memory.arasuji.storage import get_frontier_anchor_id

    conn = _memory_conn(tmp_path)
    for i in range(1, 6):
        _add_message(conn, f"m{i}", 1000 + i)

    # 一次エントリが 1 枚も無い → None
    assert get_frontier_anchor_id(conn) is None

    _add_l1_entry(conn, ["m1", "m2"])
    assert get_frontier_anchor_id(conn) == "m3"

    # 編纂対象外のメッセージ (sub_line) は飛ばされる
    conn.execute("UPDATE messages SET line_role='sub_line' WHERE id='m3'")
    conn.commit()
    assert get_frontier_anchor_id(conn) == "m4"

    # 全部畳まれたら None (未編纂の編纂対象が無い)
    _add_l1_entry(conn, ["m4", "m5"])
    assert get_frontier_anchor_id(conn) is None
    conn.close()


def test_compare_message_positions(tmp_path):
    from sai_memory.arasuji.storage import compare_message_positions

    conn = _memory_conn(tmp_path)
    _add_message(conn, "m1", 1001)
    _add_message(conn, "m2", 1002)
    assert compare_message_positions(conn, "m2", "m1") == 1
    assert compare_message_positions(conn, "m1", "m2") == -1
    assert compare_message_positions(conn, "m1", "m1") == 0
    assert compare_message_positions(conn, "m1", "ghost") is None
    conn.close()


def test_resolve_cold_self_anchor_advances_to_frontier(session_factory, tmp_path):
    """§14-2 機構1: 冷え切った自行は最前線まで前進し、行が永続化される。

    前進の書き込みは温度を据え置く (updated_at が古いまま = 冷えたまま) —
    前進はキャッシュの主張ではない。
    """
    lc = _make_lifecycle(session_factory)
    conn = _memory_conn(tmp_path)
    for i in range(1, 5):
        _add_message(conn, f"m{i}", 1000 + i)
    _add_l1_entry(conn, ["m1", "m2"])  # 最前線 = m3

    stale = _now() - timedelta(days=3)
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m1", "updated_at": stale.isoformat(), "ttl_seconds": 300,
    })
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a", sai_memory=_adapter(conn),
    )
    _set_pan_marker(persona, "m4")  # スルースは最前線より先まで見ている
    assert lc.resolve_metabolism_anchor(persona) == ("m3", "frontier")

    row = lc.load_anchor_entry(PERSONA_ID, "model-a")
    assert row["anchor_id"] == "m3"
    assert row["updated_at"] == stale.isoformat()  # 温度据え置き (冷えたまま)

    # 再解決は前進済みの自行をそのまま返す (冪等)
    assert lc.resolve_metabolism_anchor(persona) == ("m3", "self")
    conn.close()


def test_resolve_hot_self_anchor_never_moves(session_factory, tmp_path):
    """生きたキャッシュがある限り、最前線が先にあっても自行は動かない (§13 裁定1)。"""
    lc = _make_lifecycle(session_factory)
    conn = _memory_conn(tmp_path)
    for i in range(1, 5):
        _add_message(conn, f"m{i}", 1000 + i)
    _add_l1_entry(conn, ["m1", "m2"])

    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m1", "updated_at": _now().isoformat(), "ttl_seconds": 3600,
    })
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a", sai_memory=_adapter(conn),
    )
    assert lc.resolve_metabolism_anchor(persona) == ("m1", "self")
    conn.close()


def test_resolve_cold_advance_not_persisted_in_preview(session_factory, tmp_path):
    """preview (persist_advance=False) は本番と同じ位置を返すが、行は触らない。"""
    lc = _make_lifecycle(session_factory)
    conn = _memory_conn(tmp_path)
    for i in range(1, 5):
        _add_message(conn, f"m{i}", 1000 + i)
    _add_l1_entry(conn, ["m1", "m2"])

    stale = _now() - timedelta(days=3)
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m1", "updated_at": stale.isoformat(), "ttl_seconds": 300,
    })
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a", sai_memory=_adapter(conn),
    )
    _set_pan_marker(persona, "m4")
    assert lc.resolve_metabolism_anchor(
        persona, persist_advance=False,
    ) == ("m3", "frontier")
    assert lc.load_anchor_entry(PERSONA_ID, "model-a")["anchor_id"] == "m1"
    conn.close()


def test_resolve_new_model_starts_at_frontier(session_factory, tmp_path):
    """自行なし + Chronicle 実績あり → 最前線から開始。行は書かない (touch 待ち)。"""
    lc = _make_lifecycle(session_factory)
    conn = _memory_conn(tmp_path)
    for i in range(1, 5):
        _add_message(conn, f"m{i}", 1000 + i)
    _add_l1_entry(conn, ["m1", "m2"])

    # 他 model の行が最前線より後ろ (m1) → 最前線 (m3) が勝つ
    lc.upsert_anchor_entry(PERSONA_ID, "model-b", {
        "anchor_id": "m1", "updated_at": _now().isoformat(), "ttl_seconds": 300,
    })
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a", sai_memory=_adapter(conn),
    )
    assert lc.resolve_metabolism_anchor(persona) == ("m3", "frontier")
    assert lc.load_anchor_entry(PERSONA_ID, "model-a") is None  # 行は立てない
    conn.close()


def test_resolve_new_model_borrow_wins_when_ahead_of_frontier(session_factory, tmp_path):
    """借用側が最前線より先 (編纂なしで前進する設計の persona 等) なら借用が正 —
    忘れたはずの生ログを最前線で復活させない。"""
    lc = _make_lifecycle(session_factory)
    conn = _memory_conn(tmp_path)
    for i in range(1, 6):
        _add_message(conn, f"m{i}", 1000 + i)
    _add_l1_entry(conn, ["m1", "m2"])  # 最前線 = m3

    lc.upsert_anchor_entry(PERSONA_ID, "model-b", {
        "anchor_id": "m5", "updated_at": _now().isoformat(), "ttl_seconds": 300,
    })
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a", sai_memory=_adapter(conn),
    )
    assert lc.resolve_metabolism_anchor(persona) == ("m5", "other")
    conn.close()


def test_resolve_cold_advance_preserves_live_folds(session_factory, tmp_path):
    """§14-2 前進は、新 anchor 以降に生きている圧縮区間 (fold) を消さない
    (Codex 2巡目 2026-07-29)。

    最前線 = 「最初の未編纂メッセージ」なので、未編纂の隙間 (m3) を跨いで先の
    episode (m4-m5) が畳まれた形がありうる。前進の書き込みが汎用 upsert の
    列クリアを踏むと、この fold の生ログが復活してウィンドウが再膨張する。

    仕分けの基準は読み側 prune_folds と同じ「一部でも提示に残る範囲は残す」:
    - 全体が新 anchor より手前の fold → 破棄 (Chronicle エントリは head の枠へ戻る)
    - 末尾が新 anchor 以降の fold → 保持 (新 anchor を跨ぐものも含む)
    """
    from sea.session_window import FoldedRange

    lc = _make_lifecycle(session_factory)
    conn = _memory_conn(tmp_path)
    for i in range(1, 7):
        _add_message(conn, f"m{i}", 1000 + i)
    _add_l1_entry(conn, ["m1", "m2"])
    _add_l1_entry(conn, ["m4", "m5"])  # 隙間 m3 を跨いで先が編纂済み → 最前線 = m3

    stale = _now() - timedelta(days=3)
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m1", "updated_at": stale.isoformat(), "ttl_seconds": 300,
    })
    lc.save_folded_ranges(PERSONA_ID, "model-a", [
        FoldedRange(message_ids=["m1", "m2"]),   # 全体が m3 より手前 → 破棄
        FoldedRange(message_ids=["m2", "m4"]),   # 末尾が m3 以降 (跨ぎ) → 保持
        FoldedRange(message_ids=["m4", "m5"]),   # 全体が m3 以降 → 保持
    ])
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a", sai_memory=_adapter(conn),
    )
    _set_pan_marker(persona, "m6")

    assert lc.resolve_metabolism_anchor(persona) == ("m3", "frontier")

    row = lc.load_anchor_entry(PERSONA_ID, "model-a")
    assert row["anchor_id"] == "m3"
    assert row["updated_at"] == stale.isoformat()  # 温度は据え置き (冷えたまま)
    folds = lc.load_folded_ranges(PERSONA_ID, "model-a")
    assert [f.message_ids for f in folds] == [["m2", "m4"], ["m4", "m5"]]
    conn.close()


def test_resolve_stays_at_self_anchor_when_advance_write_fails(session_factory, tmp_path):
    """§14-2 前進の永続化に失敗したら前進を主張しない (Codex 5巡目 2026-07-30)。

    失敗を握りつぶして frontier を返すと、後続の touch が通常 upsert で frontier
    を書き、anchor 変更の列クリアで行に残った fold が全消えする。旧 anchor に
    留まれば次回の resolve が前進を再試行する。
    """
    lc = _make_lifecycle(session_factory)
    conn = _memory_conn(tmp_path)
    for i in range(1, 5):
        _add_message(conn, f"m{i}", 1000 + i)
    _add_l1_entry(conn, ["m1", "m2"])  # 最前線 = m3

    stale = _now() - timedelta(days=3)
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m1", "updated_at": stale.isoformat(), "ttl_seconds": 300,
    })
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a", sai_memory=_adapter(conn),
    )
    _set_pan_marker(persona, "m4")

    with patch.object(lc, "_advance_anchor_preserving_folds", return_value=False):
        assert lc.resolve_metabolism_anchor(persona) == ("m1", "self")
    assert lc.load_anchor_entry(PERSONA_ID, "model-a")["anchor_id"] == "m1"
    conn.close()


# ---------------------------------------------------------------------------
# 起点はスルースのパンマーカーを越えない (2026-08-23、v3 §13.3 との整合)
# ---------------------------------------------------------------------------


def test_get_next_message_id(tmp_path):
    """正典順で「その次」を引く (パンマーカーの次 = 前進の上限)。"""
    from sai_memory.memory.storage import get_next_message_id

    conn = _memory_conn(tmp_path)
    for i in range(1, 4):
        _add_message(conn, f"m{i}", 1000 + i)
    assert get_next_message_id(conn, "m1") == "m2"
    assert get_next_message_id(conn, "m3") is None  # 最後尾には次が無い
    assert get_next_message_id(conn, "ghost") is None
    conn.close()


def test_cold_advance_is_capped_at_the_sluice_pan_marker(
    session_factory, tmp_path, caplog,
):
    """最前線がマーカーより先でも、起点はマーカーの次までしか進まない。

    Chronicle が覆っているだけでは退場を許さない (v3 §13.3) — スルースが見て
    いない範囲を提示から落とすと、その範囲は本人の目を通らずに消える
    (2026-08-23 実機事故)。
    """
    lc = _make_lifecycle(session_factory)
    conn = _memory_conn(tmp_path)
    for i in range(1, 7):
        _add_message(conn, f"m{i}", 1000 + i)
    _add_l1_entry(conn, ["m1", "m2", "m3", "m4"])  # 最前線 = m5

    stale = _now() - timedelta(days=3)
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m1", "updated_at": stale.isoformat(), "ttl_seconds": 300,
    })
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a", sai_memory=_adapter(conn),
    )
    _set_pan_marker(persona, "m2")  # スルースは m2 までしか見ていない

    with caplog.at_level("INFO", logger="sea.session_lifecycle"):
        assert lc.resolve_metabolism_anchor(persona) == ("m3", "frontier")
    assert "cold anchor advance capped at the sluice pan marker" in caplog.text
    assert lc.load_anchor_entry(PERSONA_ID, "model-a")["anchor_id"] == "m3"
    conn.close()


def test_cold_advance_skipped_without_a_pan_marker(
    session_factory, tmp_path, caplog,
):
    """スルース未走行 (マーカー無し) のペルソナでは前進しない (fail-closed)。"""
    lc = _make_lifecycle(session_factory)
    conn = _memory_conn(tmp_path)
    for i in range(1, 5):
        _add_message(conn, f"m{i}", 1000 + i)
    _add_l1_entry(conn, ["m1", "m2"])  # 最前線 = m3

    stale = _now() - timedelta(days=3)
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m1", "updated_at": stale.isoformat(), "ttl_seconds": 300,
    })
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a", sai_memory=_adapter(conn),
    )

    with caplog.at_level("DEBUG", logger="sea.session_lifecycle"):
        assert lc.resolve_metabolism_anchor(persona) == ("m1", "self")
    assert "cold anchor advance skipped: no sluice pan marker" in caplog.text
    assert lc.load_anchor_entry(PERSONA_ID, "model-a")["anchor_id"] == "m1"
    conn.close()


def test_cold_advance_reaches_frontier_when_the_marker_is_past_it(
    session_factory, tmp_path,
):
    """マーカーが最前線以降なら従来どおり最前線まで進む (機構1 の本来の目的)。

    マーカーがちょうど最前線にある場合も、最前線までの範囲は全部スルースを
    通っているので頭打ちにならない。
    """
    lc = _make_lifecycle(session_factory)
    conn = _memory_conn(tmp_path)
    for i in range(1, 7):
        _add_message(conn, f"m{i}", 1000 + i)
    _add_l1_entry(conn, ["m1", "m2"])  # 最前線 = m3

    stale = _now() - timedelta(days=3)
    for marker in ("m3", "m5"):
        lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
            "anchor_id": "m1", "updated_at": stale.isoformat(), "ttl_seconds": 300,
        })
        persona = SimpleNamespace(
            persona_id=PERSONA_ID, model="model-a", sai_memory=_adapter(conn),
        )
        _set_pan_marker(persona, marker)
        assert lc.resolve_metabolism_anchor(persona) == ("m3", "frontier")
        assert lc.load_anchor_entry(PERSONA_ID, "model-a")["anchor_id"] == "m3"
    conn.close()


def _history_manager(ids, chars=1_000):
    """anchor 以降を返す最小の history_manager (提示ウィンドウの材料)。"""
    payloads = [
        {"id": mid, "content": "x" * chars, "created_at": 1000 + n}
        for n, mid in enumerate(ids, start=1)
    ]

    def get_history_from_anchor(
        anchor, required_line_roles=None, required_scopes=None, pulse_id=None,
    ):
        if anchor not in ids:
            return []
        return [dict(p) for p in payloads[ids.index(anchor):]]

    return SimpleNamespace(get_history_from_anchor=get_history_from_anchor)


def test_manual_compaction_with_failing_sluice_does_not_shrink_the_window(
    session_factory, tmp_path,
):
    """2026-08-23 実機事故の並びの回帰: 手動整理で Chronicle 生成が成功し、その
    直後にスルースが失敗しても、提示ウィンドウの文字数は減らない。

    事故は「スルース自身のプロンプト組成が起点を解決した瞬間に機構1 が発火し、
    いま生成した Chronicle の最前線まで起点が進む」並びで起きた。現行仕様
    (起点の凍結、2026-08-24) ではスルースは起点を解決しない — 呼び出し元が
    実行頭に撮った窓の起点が window_anchor_id で渡り、失敗しても anchor 行と
    提示ウィンドウは実行前のまま残ることを固定する。
    """
    from sea.eviction_plan import Watermarks, message_chars

    lc = _make_lifecycle(session_factory)
    conn = _memory_conn(tmp_path)
    ids = [f"m{i}" for i in range(1, 9)]
    for n, mid in enumerate(ids, start=1):
        _add_message(conn, mid, 1000 + n)
    _add_l1_entry(conn, ["m1", "m2", "m3", "m4", "m5"])  # 最前線 = m6

    stale = _now() - timedelta(days=3)
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m3", "updated_at": stale.isoformat(), "ttl_seconds": 300,
    })
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a", sai_memory=_adapter(conn),
        current_building_id="room", history_manager=_history_manager(ids),
    )
    _set_pan_marker(persona, "m2")  # スルースは m2 まで = 起点 m3 の一つ手前

    lc.ensure_recall_embeddings = lambda p: None
    lc._retry_extraction_backlog = lambda p, **kw: None
    lc.generate_chronicle = lambda p, cb=None, **kw: "ok"

    before = message_chars(lc.get_presented_window(persona, "model-a").presented)

    pinned = []

    def _failing_sluice(lifecycle, persona_, building_id, current_messages,
                        evict_count, event_callback=None, finalize=False,
                        window_anchor_id=None, model_key=None):
        # 新仕様 (起点の凍結): スルースは起点を自分で解決しない — 呼び出し元が
        # 実行頭に撮った窓の起点が window_anchor_id で渡ってくる。ここで失敗
        # しても、その凍結値と anchor 行が動いていないことを下で検算する。
        pinned.append((window_anchor_id, model_key))
        raise RuntimeError("structured output loop")

    with patch.object(lc, "is_chronicle_enabled_for_persona", return_value=True), \
            patch.object(lc, "get_metabolism_watermarks",
                         return_value=Watermarks(low=2_000, target=2_000, high=4_000)), \
            patch.dict(os.environ, {"SAIVERSE_CHRONICLE_BAND_BUDGET": "2500"}), \
            patch("sea.sluice.run_sluice", _failing_sluice):
        status = lc.run_manual_compaction(persona)

    assert status == "failed"                      # 退場は止まった
    assert pinned == [("m3", "model-a")]           # 実行頭の窓の起点と model が凍結で届く
    assert lc.load_anchor_entry(PERSONA_ID, "model-a")["anchor_id"] == "m3"
    after = message_chars(lc.get_presented_window(persona, "model-a").presented)
    assert after == before
    conn.close()


def test_touch_cas_rejects_stale_anchor_write(session_factory):
    """Codex 3巡目 (2026-07-30): Beat ロック外の keepalive touch が anchor 前進の
    後から届いても、行を巻き戻さず・圧縮区間を消さず・温かい行を偽造しない。

    touch は CAS (require_current_anchor_id) で書く — 行の現在の anchor が
    呼び出し時の anchor と一致するときだけ更新される。
    """
    from sea.session_window import FoldedRange

    lc = _make_lifecycle(session_factory)
    stale = _now() - timedelta(days=3)
    # 前進済みの行 (anchor=m3) + 提示に生きている圧縮区間
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m3", "updated_at": stale.isoformat(),
    })
    lc.save_folded_ranges(PERSONA_ID, "model-a", [
        FoldedRange(message_ids=["m4", "m5"]),
    ])
    persona = SimpleNamespace(persona_id=PERSONA_ID, model="model-a")

    # keepalive が LLM 呼び出し前に読んだ古い anchor (m1) の touch が後から届く
    lc.update_anchor_for_model(
        persona, "model-a", "m1", 300, require_current_anchor_id="m1",
    )

    row = lc.load_anchor_entry(PERSONA_ID, "model-a")
    assert row["anchor_id"] == "m3"                # 巻き戻らない
    assert row["updated_at"] == stale.isoformat()  # 温かい行を偽造しない
    folds = lc.load_folded_ranges(PERSONA_ID, "model-a")
    assert [f.message_ids for f in folds] == [["m4", "m5"]]  # fold も無傷

    # 一致する touch は従来どおり通る (updated_at リフレッシュ + TTL 記録)
    lc.update_anchor_for_model(
        persona, "model-a", "m3", 300, require_current_anchor_id="m3",
    )
    row = lc.load_anchor_entry(PERSONA_ID, "model-a")
    assert row["anchor_id"] == "m3"
    assert row["updated_at"] != stale.isoformat()
    assert row["ttl_seconds"] == 300


@patch("saiverse.model_configs.get_cache_config", return_value={"type": "implicit"})
def test_keepalive_touch_is_cas_guarded_and_skips_reservation(_mock_cache, session_factory):
    """keepalive の touch (only_if_anchor_unchanged=True) は stale なら何も書かず、
    見張り予約も上書きしない (Codex 4巡目 2026-07-30 指摘1)。

    stale 完了時刻を起点に予約し直すと、新 anchor の正当な touch が立てた予約が
    後ろへずれ、発火時には現行キャッシュが失効して連鎖ごと止まるため。
    """
    lc = _make_lifecycle(session_factory)
    scheduled = _wire_touch(lc)
    stale = _now() - timedelta(days=3)
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m3", "updated_at": stale.isoformat(),
    })
    persona = _touch_persona(model="model-a")

    lc.touch_anchor_after_llm_call(
        persona, _usage(model="model-a"), anchor_id="m1",
        only_if_anchor_unchanged=True,
    )

    row = lc.load_anchor_entry(PERSONA_ID, "model-a")
    assert row["anchor_id"] == "m3"
    assert row["updated_at"] == stale.isoformat()
    assert scheduled == []  # 見張り予約は更新されない


@patch("saiverse.model_configs.get_cache_config", return_value={"type": "implicit"})
def test_in_beat_touch_establishes_anchor_across_models(_mock_cache, session_factory):
    """Beat 内の touch (既定 = CAS なし) は、実行 model 側に別 anchor の既存行が
    あっても正当に上書きする (Codex 4巡目 2026-07-30 指摘2 の回帰固定)。

    fallback / sub-line では context 組成後に実行 model が変わる — 実際に送った
    prefix の anchor をその model の Session に確立するのが正 (S1: usage.model 記帳)。
    Beat 内の touch は anchor 前進と Beat ロックで直列化済みなので CAS は不要。
    """
    lc = _make_lifecycle(session_factory)
    scheduled = _wire_touch(lc)
    lc.upsert_anchor_entry(PERSONA_ID, "light-model", {
        "anchor_id": "old-anchor", "updated_at": _now().isoformat(),
    })
    persona = _touch_persona(model="std-model")

    lc.touch_anchor_after_llm_call(
        persona, _usage(model="light-model"), anchor_id="new-anchor",
    )

    row = lc.load_anchor_entry(PERSONA_ID, "light-model")
    assert row["anchor_id"] == "new-anchor"
    assert scheduled == ["light-model"]  # 見張り予約も実 model 側に入る


def _cold_ready_lifecycle(session_factory):
    """§14-3/§14-4 テスト用の lifecycle (Metabolism は常時 ON — 2026-07-30 トグル撤去)。"""
    return _make_lifecycle(session_factory)


def test_cold_precompaction_status_conditions(session_factory):
    """§14-4 の発火条件: 全行冷え + 中間値超過のときだけ "due"。

    - 1 行でも温かければ "hot" (生きたキャッシュを畳みで壊さない)
    - 中間値 ((target+high)/2) 以下は "cool"
    - Chronicle 生成が無効 (自律確認 OFF 含む) は "skip"
    """
    from sea.eviction_plan import Watermarks

    lc = _cold_ready_lifecycle(session_factory)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a", current_building_id="room",
    )
    stale = _now() - timedelta(days=3)
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m1", "updated_at": stale.isoformat(), "ttl_seconds": 300,
    })
    wm = Watermarks(low=1000, target=2000, high=4000)  # 中間値 = 3000
    big = [{"id": "m1", "content": "x" * 3500}]
    small = [{"id": "m1", "content": "x" * 2500}]

    with patch.object(lc, "is_chronicle_enabled_for_persona", return_value=True), \
            patch.object(lc, "is_autonomous_chronicle_enabled_for_persona", return_value=True), \
            patch.object(lc, "get_metabolism_watermarks", return_value=wm):
        with patch.object(lc, "get_presented_window", return_value=_window("m1", big)):
            assert lc.cold_precompaction_status(persona) == "due"
        with patch.object(lc, "get_presented_window", return_value=_window("m1", small)):
            assert lc.cold_precompaction_status(persona) == "cool"

        # 別 model に温かい行が 1 つでもあれば "hot"
        lc.upsert_anchor_entry(PERSONA_ID, "model-b", {
            "anchor_id": "b1", "updated_at": _now().isoformat(), "ttl_seconds": 3600,
        })
        with patch.object(lc, "get_presented_window", return_value=_window("m1", big)):
            assert lc.cold_precompaction_status(persona) == "hot"

    # 自律確認 OFF の persona は先回りの対象外 (コスト最適化であって回復ではない)
    with patch.object(lc, "is_chronicle_enabled_for_persona", return_value=True), \
            patch.object(lc, "is_autonomous_chronicle_enabled_for_persona", return_value=False):
        assert lc.cold_precompaction_status(persona) == "skip"


def test_cold_precompaction_counts_injected_perceptions(session_factory):
    """先回り畳み (§14-4) の中間値判定も「実際に送る中身」で測る。

    先回りが救おうとしているのは実送信の膨らみなので、保存行だけで測ると、
    知覚ブロックで既に中間値を越えている窓を "cool" と読んで、次の会話の
    非常畳み (§14-3) へ丸ごと送ってしまう (2026-09-02 まはー裁定)。
    """
    from sea.eviction_plan import Watermarks

    lc = _cold_ready_lifecycle(session_factory)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a", current_building_id="room",
    )
    stale = _now() - timedelta(days=3)
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m1", "updated_at": stale.isoformat(), "ttl_seconds": 300,
    })
    wm = Watermarks(low=1000, target=2000, high=4000)  # 中間値 = 3000
    small = [{"id": "m1", "content": "x" * 2500}]

    with patch.object(lc, "is_chronicle_enabled_for_persona", return_value=True), \
            patch.object(lc, "is_autonomous_chronicle_enabled_for_persona",
                         return_value=True), \
            patch.object(lc, "get_metabolism_watermarks", return_value=wm), \
            patch.object(lc, "get_presented_window",
                         return_value=_window("m1", small)):
        # 保存行 2,500 字 <= 中間値 3,000 → 従来どおり "cool"。
        assert lc.cold_precompaction_status(persona) == "cool"
        # 差し込みの知覚 1,000 字を足すと 3,500 字 > 中間値 → "due"。
        with patch.object(lc, "perception_blocks_for",
                          return_value=[_perception_block(1000)]):
            assert lc.cold_precompaction_status(persona) == "due"


def test_run_cold_precompaction_folds_with_force(session_factory):
    """"due" なら run_metabolism を chronicle_force=True (確認ゲート迂回) で呼ぶ。
    stop_when_disabled は渡さない (既定 False) — 手動入口の契約と混ぜない。"""
    from sea.eviction_plan import Watermarks

    lc = _cold_ready_lifecycle(session_factory)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a", current_building_id="room",
    )
    stale = _now() - timedelta(days=3)
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m1", "updated_at": stale.isoformat(), "ttl_seconds": 300,
    })
    wm = Watermarks(low=1000, target=2000, high=4000)
    big = [{"id": "m1", "content": "x" * 3500}]

    with patch.object(lc, "is_chronicle_enabled_for_persona", return_value=True), \
            patch.object(lc, "is_autonomous_chronicle_enabled_for_persona", return_value=True), \
            patch.object(lc, "get_metabolism_watermarks", return_value=wm), \
            patch.object(lc, "get_presented_window", return_value=_window("m1", big)), \
            patch.object(lc, "run_metabolism", return_value="ok") as run:
        assert lc.run_cold_precompaction(persona) == "ok"
    run.assert_called_once()
    assert run.call_args.kwargs.get("chronicle_force") is True
    assert run.call_args.kwargs.get("stop_when_disabled") is not True
    assert run.call_args.kwargs.get("model_key") == "model-a"


def test_run_cold_precompaction_rechecks_under_beat_lock(session_factory):
    """指摘3 (Codex 2巡目 2026-07-29): 発火条件の最終判定は Beat ロックの内側。

    tick 側の事前判定 ("due") からロック取得までの間にユーザー Pulse が完走して
    anchor を touch した状況を、ロック取得時に touch する fake gate で再現する。
    ロック内の再判定が "hot" を見て、温まったキャッシュを畳まず引き返すこと。
    """
    import contextlib as _ctx

    from sea.eviction_plan import Watermarks

    lc = _cold_ready_lifecycle(session_factory)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a", current_building_id="room",
    )
    stale = _now() - timedelta(days=3)
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m1", "updated_at": stale.isoformat(), "ttl_seconds": 300,
    })
    wm = Watermarks(low=1000, target=2000, high=4000)
    big = [{"id": "m1", "content": "x" * 3500}]

    @_ctx.contextmanager
    def _hold(persona_id, purpose=None, check_gate=True):
        # ロック取得と同時に「直前にユーザー Pulse が touch した」状態にする
        lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
            "anchor_id": "m1", "updated_at": _now().isoformat(),
            "ttl_seconds": 3600,
        })
        yield

    lc.manager.beat_gate = SimpleNamespace(hold=_hold)

    with patch.object(lc, "is_chronicle_enabled_for_persona", return_value=True), \
            patch.object(lc, "is_autonomous_chronicle_enabled_for_persona", return_value=True), \
            patch.object(lc, "get_metabolism_watermarks", return_value=wm), \
            patch.object(lc, "get_presented_window", return_value=_window("m1", big)), \
            patch.object(lc, "run_metabolism", return_value="ok") as run:
        # 事前判定の時点では "due" 相当の条件が揃っているが、
        # ロック内の再判定が touch 後の温度を見て "hot" で止まる
        assert lc.run_cold_precompaction(persona) == "hot"
    run.assert_not_called()


def test_run_cold_precompaction_skips_when_only_perception_over_budget(
    session_factory, caplog,
):
    """先回り畳みも「削る先があるか」は会話の行だけを残す量と比べる。

    行 10,000 字 <= 残す量 18,000 字なら退場計画は保護範囲で埋まって空になる
    (残す量の主語は会話の行、2026-09-03 裁定)。合計 40,000 字が中間値も上限
    26,000 字も超えていても、run_metabolism へ進まない — 本体は計画が空と分かる
    前に抽出の滞留 (_retry_extraction_backlog — LLM 課金) を流し、10 分ごとに
    同じ空振りを繰り返す (Codex 指摘 2026-09-03)。"skip" で引き返し、知覚の
    供給が予算超過の警告はペルソナごと 1 度だけ。
    """
    import logging as _logging

    from sea.eviction_plan import Watermarks

    lc = _cold_ready_lifecycle(session_factory)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a", current_building_id="room",
    )
    stale = _now() - timedelta(days=3)
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m1", "updated_at": stale.isoformat(), "ttl_seconds": 300,
    })
    wm = Watermarks(low=9000, target=18000, high=26000)  # 中間値 = 22,000
    rows = [{"id": "m1", "content": "x" * 10000}]

    with patch.object(lc, "is_chronicle_enabled_for_persona", return_value=True), \
            patch.object(lc, "is_autonomous_chronicle_enabled_for_persona",
                         return_value=True), \
            patch.object(lc, "get_metabolism_watermarks", return_value=wm), \
            patch.object(lc, "get_presented_window",
                         return_value=_window("m1", rows)), \
            patch.object(lc, "perception_blocks_for",
                         return_value=[_perception_block(30000)]), \
            patch.object(lc, "_retry_extraction_backlog") as backlog, \
            patch.object(lc, "run_metabolism", return_value="ok") as run:
        # 事前判定は合計 40,000 > 中間値 22,000 で "due" — 本体側で引き返す。
        assert lc.cold_precompaction_status(persona) == "due"
        with caplog.at_level(_logging.WARNING, logger="sea.session_lifecycle"):
            assert lc.run_cold_precompaction(persona) == "skip"
            assert lc.run_cold_precompaction(persona) == "skip"
            warned = [
                r for r in caplog.records
                if r.levelno == _logging.WARNING
                and "perception blocks" in r.getMessage()
            ]
            assert len(warned) == 1
    run.assert_not_called()
    backlog.assert_not_called()


def test_emergency_precompaction_skip_below_high(session_factory):
    """高水位以下なら何もしない (通知も編纂も出ない)。"""
    from sea.eviction_plan import Watermarks

    lc = _cold_ready_lifecycle(session_factory)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a", current_building_id="room",
    )
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m1", "updated_at": _now().isoformat(), "ttl_seconds": 3600,
    })
    wm = Watermarks(low=1000, target=2000, high=4000)
    small = [{"id": "m1", "content": "x" * 3000}]
    events = []

    with patch.object(lc, "get_metabolism_watermarks", return_value=wm), \
            patch.object(lc, "get_presented_window", return_value=_window("m1", small)), \
            patch.object(lc, "run_metabolism", return_value="ok") as run:
        assert lc.maybe_run_emergency_precompaction(
            persona, "room", events.append,
        ) == "skip"
    run.assert_not_called()
    assert events == []


def test_emergency_precompaction_counts_injected_perceptions(session_factory):
    """非常畳み (§14-3) の発火判定は「実際に送る中身」で測る。

    この機構の目的は「巨大なコンテキストを送らない」ことそのものなので、
    保存行だけで測ると、知覚ブロックを足した実送信が model の上限を突き抜けて
    いても「上限以下」と読んで応答へ進み、存在意義が壊れる (2026-09-02 裁定)。
    """
    from sea.eviction_plan import Watermarks

    lc = _cold_ready_lifecycle(session_factory)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a", current_building_id="room",
    )
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m1", "updated_at": _now().isoformat(), "ttl_seconds": 3600,
    })
    wm = Watermarks(low=1000, target=2000, high=4000)
    small = [{"id": "m1", "content": "x" * 3000}]
    events = []

    with patch.object(lc, "get_metabolism_watermarks", return_value=wm), \
            patch.object(lc, "get_presented_window",
                         return_value=_window("m1", small)), \
            patch.object(lc, "run_metabolism", return_value="ok") as run:
        # 保存行 3,000 字 <= 上限 4,000 → 従来どおり見送り。
        assert lc.maybe_run_emergency_precompaction(
            persona, "room", events.append,
        ) == "skip"
        run.assert_not_called()
        assert events == []
        # 差し込みの知覚 2,000 字を足すと 5,000 字 > 上限 → 応答前に畳む。
        with patch.object(lc, "perception_blocks_for",
                          return_value=[_perception_block(2000)]):
            assert lc.maybe_run_emergency_precompaction(
                persona, "room", events.append,
            ) == "ok"
    run.assert_called_once()
    assert [e["type"] for e in events] == ["status"]


def test_emergency_precompaction_noop_when_only_perception_over_budget(
    session_factory, caplog,
):
    """知覚の供給だけが予算超過 (合計 > 上限、会話の行 <= 残す量) なら何もしない。

    残す量の主語は会話の行 (2026-09-03 裁定) なので、行 10,000 字 <= 残す量
    18,000 字の窓では退場計画が保護範囲で埋まって必ず空になる。合計 40,000 字
    > 上限 26,000 字だからといって通知を出し・自行を立て・run_metabolism へ
    進むと、毎ターン「整理しています」だけ出て何も畳めない (Codex 指摘
    2026-09-03)。"skip" で引き返し、警告はペルソナごと 1 度だけ。
    """
    import logging as _logging

    from sea.eviction_plan import Watermarks

    lc = _cold_ready_lifecycle(session_factory)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a", current_building_id="room",
    )
    # 自行なし (resolution が frontier/other になり、従来なら行を立てていた経路)
    wm = Watermarks(low=9000, target=18000, high=26000)
    rows = [{"id": "m1", "content": "x" * 10000}]
    events = []

    with patch.object(lc, "get_metabolism_watermarks", return_value=wm), \
            patch.object(lc, "resolve_metabolism_anchor",
                         return_value=("m1", "frontier")), \
            patch.object(lc, "get_presented_window",
                         return_value=_window("m1", rows)), \
            patch.object(lc, "perception_blocks_for",
                         return_value=[_perception_block(30000)]), \
            patch.object(lc, "upsert_anchor_entry") as upsert, \
            patch.object(lc, "run_metabolism", return_value="ok") as run:
        with caplog.at_level(_logging.WARNING, logger="sea.session_lifecycle"):
            assert lc.maybe_run_emergency_precompaction(
                persona, "room", events.append,
            ) == "skip"
            first_warnings = [
                r for r in caplog.records
                if r.levelno == _logging.WARNING
                and "perception blocks" in r.getMessage()
            ]
            assert len(first_warnings) == 1
            caplog.clear()
            # 2 回目 (次のユーザーターン) も "skip" で、警告は再発しない
            assert lc.maybe_run_emergency_precompaction(
                persona, "room", events.append,
            ) == "skip"
            assert not [
                r for r in caplog.records
                if r.levelno == _logging.WARNING
                and "perception blocks" in r.getMessage()
            ]
    run.assert_not_called()
    upsert.assert_not_called()
    assert events == []
    assert lc.load_anchor_entry(PERSONA_ID, "model-a") is None


def test_emergency_precompaction_folds_over_high_with_notice(session_factory):
    """§14-3: 高水位超過なら応答前に畳み、status イベントで通知する
    (同意ダイアログではない)。確認ゲートは chronicle_force=True で迂回。"""
    from sea.eviction_plan import Watermarks

    lc = _cold_ready_lifecycle(session_factory)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a", current_building_id="room",
    )
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m1", "updated_at": _now().isoformat(), "ttl_seconds": 3600,
    })
    wm = Watermarks(low=1000, target=2000, high=4000)
    big = [{"id": "m1", "content": "x" * 5000}]
    events = []

    with patch.object(lc, "get_metabolism_watermarks", return_value=wm), \
            patch.object(lc, "get_presented_window", return_value=_window("m1", big)), \
            patch.object(lc, "run_metabolism", return_value="ok") as run:
        assert lc.maybe_run_emergency_precompaction(
            persona, "room", events.append,
        ) == "ok"
    run.assert_called_once()
    assert run.call_args.kwargs.get("chronicle_force") is True
    assert run.call_args.kwargs.get("stop_when_disabled") is not True
    # 非常経路だけは材料 U 未満の端数 fold を許す (2026-08-29 裁定 —
    # 「生は巨大だが材料が薄い」期間でも前進を保証する)。
    assert run.call_args.kwargs.get("close_undersized_tail") is True
    assert [e["type"] for e in events] == ["status"]


def test_emergency_precompaction_creates_row_for_rowless_model(session_factory, tmp_path):
    """自行の無い model (最前線から始まる初回) で非常畳みが要る場合、畳みの
    適用先となる行を冷えた温度で先に立てる。"""
    from sea.eviction_plan import Watermarks

    lc = _cold_ready_lifecycle(session_factory)
    conn = _memory_conn(tmp_path)
    for i in range(1, 5):
        _add_message(conn, f"m{i}", 1000 + i)
    _add_l1_entry(conn, ["m1", "m2"])  # 最前線 = m3

    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a", current_building_id="room",
        sai_memory=_adapter(conn),
    )
    wm = Watermarks(low=1000, target=2000, high=4000)
    big = [{"id": "m3", "content": "x" * 5000}]

    with patch.object(lc, "get_metabolism_watermarks", return_value=wm), \
            patch.object(lc, "get_presented_window", return_value=_window("m3", big)), \
            patch.object(lc, "run_metabolism", return_value="ok"):
        assert lc.maybe_run_emergency_precompaction(persona, "room") == "ok"

    row = lc.load_anchor_entry(PERSONA_ID, "model-a")
    assert row["anchor_id"] == "m3"
    # 冷えた温度で立つ (温かい行を偽造しない)
    assert not lc._anchor_entry_is_hot(row, "model-a", PERSONA_ID)
    conn.close()
