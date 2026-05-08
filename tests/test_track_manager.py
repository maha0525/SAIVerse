"""TrackManager unit tests (Phase B-1).

In-memory SQLite で完結する純粋ロジックテスト。実機環境を必要としない。
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import AI, Base, City, User
from saiverse.track_manager import (
    InvalidTrackStateError,
    PersistentTrackError,
    STATUS_ABORTED,
    STATUS_ALERT,
    STATUS_COMPLETED,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_UNSTARTED,
    STATUS_WAITING,
    TrackManager,
    TrackNotFoundError,
)


@pytest.fixture
def session_factory():
    """In-memory SQLite session factory.

    StaticPool + check_same_thread=False により、thread 跨ぎでも同一 :memory: DB
    を共有できる (Phase 4-e の waiting timeout テストでは EventScheduler の
    dispatch thread から DB アクセスがあるため)。
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    yield Session
    engine.dispose()


@pytest.fixture
def persona(session_factory):
    """Create minimal user/city/AI for FK satisfaction."""
    db = session_factory()
    try:
        db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
        db.flush()
        city = City(USERID=1, CITYNAME="test_city", UI_PORT=3001, API_PORT=8001)
        db.add(city)
        db.flush()
        db.add(AI(AIID="alice", HOME_CITYID=city.CITYID, AINAME="Alice"))
        db.add(AI(AIID="bob", HOME_CITYID=city.CITYID, AINAME="Bob"))
        db.commit()
    finally:
        db.close()
    return "alice"


@pytest.fixture
def tm(session_factory):
    return TrackManager(session_factory=session_factory)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def test_create_returns_track_id_with_unstarted_status(tm, persona):
    track_id = tm.create(persona, "autonomous")
    track = tm.get(track_id)
    assert track.track_id == track_id
    assert track.persona_id == persona
    assert track.status == STATUS_UNSTARTED
    assert track.is_persistent is False
    assert track.output_target == "none"


def test_create_persistent_track(tm, persona):
    track_id = tm.create(
        persona, "social",
        title="交流",
        is_persistent=True,
        output_target="building:current",
    )
    track = tm.get(track_id)
    assert track.is_persistent is True
    assert track.output_target == "building:current"


def test_create_requires_persona_and_type(tm):
    with pytest.raises(ValueError):
        tm.create("", "autonomous")
    with pytest.raises(ValueError):
        tm.create("alice", "")


def test_get_raises_when_not_found(tm):
    with pytest.raises(TrackNotFoundError):
        tm.get("nonexistent")


def test_list_filters_by_status(tm, persona):
    t1 = tm.create(persona, "autonomous")
    t2 = tm.create(persona, "autonomous")
    tm.activate(t1)

    running = tm.list_for_persona(persona, statuses=[STATUS_RUNNING])
    unstarted = tm.list_for_persona(persona, statuses=[STATUS_UNSTARTED])

    assert {t.track_id for t in running} == {t1}
    assert {t.track_id for t in unstarted} == {t2}


def test_list_excludes_forgotten_by_default(tm, persona):
    t1 = tm.create(persona, "autonomous")
    t2 = tm.create(persona, "autonomous")
    tm.forget(t2)

    visible = tm.list_for_persona(persona)
    full = tm.list_for_persona(persona, include_forgotten=True)

    assert {t.track_id for t in visible} == {t1}
    assert {t.track_id for t in full} == {t1, t2}


def test_get_running_returns_none_when_no_active(tm, persona):
    assert tm.get_running(persona) is None
    track_id = tm.create(persona, "autonomous")
    assert tm.get_running(persona) is None
    tm.activate(track_id)
    running = tm.get_running(persona)
    assert running is not None
    assert running.track_id == track_id


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------

def test_activate_pushes_existing_running_to_pending(tm, persona):
    t1 = tm.create(persona, "autonomous")
    t2 = tm.create(persona, "autonomous")
    tm.activate(t1)
    assert tm.get(t1).status == STATUS_RUNNING

    tm.activate(t2)

    assert tm.get(t1).status == STATUS_PENDING
    assert tm.get(t2).status == STATUS_RUNNING
    # Only one running per persona
    running = tm.list_for_persona(persona, statuses=[STATUS_RUNNING])
    assert len(running) == 1


def test_activate_does_not_affect_other_personas(tm, persona, session_factory):
    t_alice = tm.create("alice", "autonomous")
    t_bob = tm.create("bob", "autonomous")
    tm.activate(t_alice)
    tm.activate(t_bob)

    assert tm.get(t_alice).status == STATUS_RUNNING
    assert tm.get(t_bob).status == STATUS_RUNNING


def test_activate_rejects_terminal(tm, persona):
    t = tm.create(persona, "autonomous")
    tm.activate(t)
    tm.complete(t)
    with pytest.raises(InvalidTrackStateError):
        tm.activate(t)


def test_pause(tm, persona):
    t = tm.create(persona, "autonomous")
    tm.activate(t)
    tm.pause(t)
    assert tm.get(t).status == STATUS_PENDING


def test_pause_rejects_unstarted(tm, persona):
    t = tm.create(persona, "autonomous")
    with pytest.raises(InvalidTrackStateError):
        tm.pause(t)


def test_wait_sets_waiting_fields(tm, persona):
    t = tm.create(persona, "autonomous")
    tm.activate(t)
    tm.wait(t, waiting_for='{"type":"user_response"}', timeout_seconds=600)
    track = tm.get(t)
    assert track.status == STATUS_WAITING
    assert track.waiting_for == '{"type":"user_response"}'
    assert track.waiting_timeout_at is not None


def test_wait_without_timeout_keeps_null(tm, persona):
    t = tm.create(persona, "autonomous")
    tm.activate(t)
    tm.wait(t, waiting_for='{"type":"user_response"}')
    assert tm.get(t).waiting_timeout_at is None


def test_resume_from_wait_activate(tm, persona):
    t = tm.create(persona, "autonomous")
    tm.activate(t)
    tm.wait(t, waiting_for='{"x":1}')
    tm.resume_from_wait(t, "activate")
    assert tm.get(t).status == STATUS_RUNNING


def test_resume_from_wait_pause_clears_waiting_fields(tm, persona):
    t = tm.create(persona, "autonomous")
    tm.activate(t)
    tm.wait(t, waiting_for='{"x":1}', timeout_seconds=60)
    tm.resume_from_wait(t, "pause")
    track = tm.get(t)
    assert track.status == STATUS_PENDING
    assert track.waiting_for is None
    assert track.waiting_timeout_at is None


def test_resume_from_wait_abort(tm, persona):
    t = tm.create(persona, "autonomous")
    tm.activate(t)
    tm.wait(t, waiting_for='{"x":1}')
    tm.resume_from_wait(t, "abort")
    track = tm.get(t)
    assert track.status == STATUS_ABORTED
    assert track.aborted_at is not None


def test_wait_with_event_scheduler_pushes_timeout(session_factory, persona):
    """Phase 4-e: wait() で waiting_timeout_at が EventScheduler に push される。"""
    from saiverse.event_scheduler import EventScheduler
    scheduler = EventScheduler()
    scheduler.start()
    try:
        tm_with_sched = TrackManager(
            session_factory=session_factory, event_scheduler=scheduler,
        )
        t = tm_with_sched.create(persona, "autonomous")
        tm_with_sched.activate(t)
        tm_with_sched.wait(t, waiting_for='{"type":"user_response"}', timeout_seconds=600)
        # EventScheduler に予約が登録されている
        assert scheduler.has_key(f"wait_timeout:{t}")
    finally:
        scheduler.stop()


def test_wait_no_event_scheduler_skips_push(tm, persona):
    """event_scheduler=None なら wait() は push しない (互換性、tools 等のケース)。"""
    t = tm.create(persona, "autonomous")
    tm.activate(t)
    # 例外なく wait できる
    tm.wait(t, waiting_for='{"type":"user_response"}', timeout_seconds=60)
    assert tm.get(t).status == STATUS_WAITING


def test_resume_from_wait_cancels_timeout_schedule(session_factory, persona):
    """resume_from_wait (pause/abort) で EventScheduler の予約がキャンセルされる。"""
    from saiverse.event_scheduler import EventScheduler
    scheduler = EventScheduler()
    scheduler.start()
    try:
        tm_with_sched = TrackManager(
            session_factory=session_factory, event_scheduler=scheduler,
        )
        t = tm_with_sched.create(persona, "autonomous")
        tm_with_sched.activate(t)
        tm_with_sched.wait(t, waiting_for='{"x":1}', timeout_seconds=60)
        assert scheduler.has_key(f"wait_timeout:{t}")
        tm_with_sched.resume_from_wait(t, "pause")
        assert not scheduler.has_key(f"wait_timeout:{t}")
    finally:
        scheduler.stop()


def test_waiting_timeout_fires_alert(session_factory, persona):
    """timeout 到達時に alert observer が呼ばれる (Intent: 自動遷移せず通知)。"""
    import time as _time
    from datetime import datetime as _dt
    from saiverse.event_scheduler import EventScheduler
    scheduler = EventScheduler()
    scheduler.start()
    received = []

    def observer(persona_id, track_id, context):
        received.append((persona_id, track_id, context))

    try:
        tm_with_sched = TrackManager(
            session_factory=session_factory, event_scheduler=scheduler,
        )
        tm_with_sched.add_alert_observer(observer)
        t = tm_with_sched.create(persona, "autonomous")
        tm_with_sched.activate(t)
        # 0 秒タイムアウト → 即時 fire (datetime.now() がそのまま fire_at)
        # ただし wait() の中で now+0 が計算されるため、fire は即時走る
        tm_with_sched.wait(t, waiting_for='{"x":1}', timeout_seconds=0)
        # callback 実行を待つ
        deadline = _time.time() + 2.0
        while _time.time() < deadline and not received:
            _time.sleep(0.05)
        assert len(received) == 1
        pid, tid, ctx = received[0]
        assert pid == persona
        assert tid == t
        assert ctx["trigger"] == "waiting_timeout"
        assert ctx["waiting_for"] == '{"x":1}'
        # Track 状態は依然 waiting (Intent: 自動遷移しない、メタ判断に委ねる)
        assert tm_with_sched.get(t).status == STATUS_WAITING
    finally:
        scheduler.stop()


def test_waiting_timeout_no_fire_after_resume(session_factory, persona):
    """waiting 解除後に timeout が fire されても alert 通知が走らない。"""
    import time as _time
    from saiverse.event_scheduler import EventScheduler
    scheduler = EventScheduler()
    scheduler.start()
    received = []

    def observer(persona_id, track_id, context):
        received.append((persona_id, track_id, context))

    try:
        tm_with_sched = TrackManager(
            session_factory=session_factory, event_scheduler=scheduler,
        )
        tm_with_sched.add_alert_observer(observer)
        t = tm_with_sched.create(persona, "autonomous")
        tm_with_sched.activate(t)
        # 1 秒タイムアウトで予約 → 直後に解除 (dispatch thread が起きる前に cancel)
        tm_with_sched.wait(t, waiting_for='{"x":1}', timeout_seconds=1)
        tm_with_sched.resume_from_wait(t, "pause")
        # 1.5 秒待っても alert は来ない (cancel されたため)
        _time.sleep(1.5)
        assert received == []
    finally:
        scheduler.stop()


def test_resume_from_wait_invalid_mode(tm, persona):
    t = tm.create(persona, "autonomous")
    tm.activate(t)
    tm.wait(t, waiting_for='{"x":1}')
    with pytest.raises(ValueError):
        tm.resume_from_wait(t, "explode")


def test_resume_from_wait_requires_waiting_status(tm, persona):
    t = tm.create(persona, "autonomous")
    tm.activate(t)  # running, not waiting
    with pytest.raises(InvalidTrackStateError):
        tm.resume_from_wait(t, "activate")


def test_complete_sets_timestamp(tm, persona):
    t = tm.create(persona, "autonomous")
    tm.activate(t)
    tm.complete(t)
    track = tm.get(t)
    assert track.status == STATUS_COMPLETED
    assert track.completed_at is not None


def test_complete_rejects_non_running(tm, persona):
    t = tm.create(persona, "autonomous")
    with pytest.raises(InvalidTrackStateError):
        tm.complete(t)


def test_abort_from_pending(tm, persona):
    t = tm.create(persona, "autonomous")
    tm.activate(t)
    tm.pause(t)
    tm.abort(t)
    assert tm.get(t).status == STATUS_ABORTED


def test_abort_clears_waiting_fields(tm, persona):
    t = tm.create(persona, "autonomous")
    tm.activate(t)
    tm.wait(t, waiting_for='{"x":1}', timeout_seconds=60)
    tm.abort(t)
    track = tm.get(t)
    assert track.status == STATUS_ABORTED
    assert track.waiting_for is None
    assert track.waiting_timeout_at is None


def test_abort_rejects_already_terminal(tm, persona):
    t = tm.create(persona, "autonomous")
    tm.activate(t)
    tm.complete(t)
    with pytest.raises(InvalidTrackStateError):
        tm.abort(t)


def test_set_alert_from_pending(tm, persona):
    t = tm.create(persona, "autonomous")
    tm.activate(t)
    tm.pause(t)
    tm.set_alert(t)
    assert tm.get(t).status == STATUS_ALERT


def test_set_alert_no_op_when_running(tm, persona):
    t = tm.create(persona, "autonomous")
    tm.activate(t)
    tm.set_alert(t)
    # running stays running (no-op)
    assert tm.get(t).status == STATUS_RUNNING


def test_set_alert_rejects_terminal(tm, persona):
    t = tm.create(persona, "autonomous")
    tm.activate(t)
    tm.complete(t)
    with pytest.raises(InvalidTrackStateError):
        tm.set_alert(t)


# ---------------------------------------------------------------------------
# Persistent track constraints
# ---------------------------------------------------------------------------

def test_persistent_track_cannot_complete(tm, persona):
    t = tm.create(persona, "social", is_persistent=True)
    tm.activate(t)
    with pytest.raises(PersistentTrackError):
        tm.complete(t)


def test_persistent_track_cannot_abort(tm, persona):
    t = tm.create(persona, "social", is_persistent=True)
    tm.activate(t)
    with pytest.raises(PersistentTrackError):
        tm.abort(t)


def test_persistent_track_cannot_abort_from_wait(tm, persona):
    t = tm.create(persona, "social", is_persistent=True)
    tm.activate(t)
    tm.wait(t, waiting_for='{"x":1}')
    with pytest.raises(PersistentTrackError):
        tm.resume_from_wait(t, "abort")


def test_persistent_track_can_pause(tm, persona):
    t = tm.create(persona, "social", is_persistent=True)
    tm.activate(t)
    tm.pause(t)
    assert tm.get(t).status == STATUS_PENDING


def test_persistent_track_can_wait(tm, persona):
    t = tm.create(persona, "social", is_persistent=True)
    tm.activate(t)
    tm.wait(t, waiting_for='{"x":1}')
    assert tm.get(t).status == STATUS_WAITING


# ---------------------------------------------------------------------------
# Forgetting
# ---------------------------------------------------------------------------

def test_forget_and_recall(tm, persona):
    t = tm.create(persona, "autonomous")
    tm.forget(t)
    assert tm.get(t).is_forgotten is True
    tm.recall(t)
    assert tm.get(t).is_forgotten is False


def test_forget_does_not_affect_status(tm, persona):
    t = tm.create(persona, "autonomous")
    tm.activate(t)
    tm.forget(t)
    track = tm.get(t)
    assert track.status == STATUS_RUNNING
    assert track.is_forgotten is True


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

def test_operations_on_unknown_track_raise(tm):
    for op_name, args in [
        ("activate", ("nope",)),
        ("pause", ("nope",)),
        ("wait", ("nope", '{"x":1}')),
        ("complete", ("nope",)),
        ("abort", ("nope",)),
        ("set_alert", ("nope",)),
        ("forget", ("nope",)),
        ("recall", ("nope",)),
    ]:
        op = getattr(tm, op_name)
        with pytest.raises(TrackNotFoundError):
            op(*args)


def test_wait_requires_waiting_for(tm, persona):
    t = tm.create(persona, "autonomous")
    tm.activate(t)
    with pytest.raises(ValueError):
        tm.wait(t, waiting_for="")


# ---------------------------------------------------------------------------
# Alert observer mechanism (Phase C-1)
# ---------------------------------------------------------------------------

def test_set_alert_notifies_observer(tm, persona):
    """alert への実遷移時に observer が呼ばれる。"""
    calls = []
    tm.add_alert_observer(
        lambda pid, tid, ctx: calls.append((pid, tid, ctx))
    )
    t = tm.create(persona, "autonomous", title="test track")
    tm.activate(t)
    tm.pause(t)  # running -> pending
    tm.set_alert(t, context={"trigger": "test"})
    assert len(calls) == 1
    assert calls[0][0] == persona
    assert calls[0][1] == t
    # Phase 2.6: context は元値 + Track 識別情報で enrich される
    ctx = calls[0][2]
    assert ctx["trigger"] == "test"
    assert ctx["target_track_title"] == "test track"
    assert ctx["target_track_type"] == "autonomous"
    assert "target_already_running" not in ctx  # 通常の遷移ではフラグなし


def test_set_alert_no_op_when_running_still_notifies_with_flag(tm, persona):
    """Phase 2.6: running 時は state は no-op だが observer には
    target_already_running=True 付きで通知する。

    自律先制と外部 alert の衝突を観察者が認識できるようにするため。
    """
    calls = []
    tm.add_alert_observer(lambda pid, tid, ctx: calls.append((pid, tid, ctx)))
    t = tm.create(persona, "autonomous", title="auto track")
    tm.activate(t)
    tm.set_alert(t, context={"trigger": "test"})
    assert len(calls) == 1
    pid, tid, ctx = calls[0]
    assert pid == persona
    assert tid == t
    assert ctx["target_already_running"] is True
    assert ctx["target_track_title"] == "auto track"
    assert ctx["target_track_type"] == "autonomous"
    assert ctx["trigger"] == "test"


def test_set_alert_no_op_when_already_alert_does_not_notify(tm, persona):
    """既に alert 状態の場合、二重通知しない。"""
    t = tm.create(persona, "autonomous")
    tm.activate(t)
    tm.pause(t)
    tm.set_alert(t)
    # 1 回目で通知済みのはずなので、observer はそれ以降の追加分だけ見る
    calls = []
    tm.add_alert_observer(lambda *args: calls.append(args))
    tm.set_alert(t)  # 既に alert なので no-op
    assert calls == []


def test_observer_exception_does_not_break_caller(tm, persona):
    """observer の例外は呼び出し元に伝播しない。"""
    def bad_observer(*args):
        raise RuntimeError("boom")
    tm.add_alert_observer(bad_observer)
    t = tm.create(persona, "autonomous")
    tm.activate(t)
    tm.pause(t)
    # 例外が伝播しないことを確認 (raise しなければテスト成功)
    tm.set_alert(t)
    assert tm.get(t).status == STATUS_ALERT


def test_multiple_observers_all_notified(tm, persona):
    """複数 observer 登録時、全員に通知される。"""
    calls_a = []
    calls_b = []
    tm.add_alert_observer(lambda *args: calls_a.append(args))
    tm.add_alert_observer(lambda *args: calls_b.append(args))
    t = tm.create(persona, "autonomous")
    tm.activate(t)
    tm.pause(t)
    tm.set_alert(t)
    assert len(calls_a) == 1
    assert len(calls_b) == 1


def test_remove_alert_observer(tm, persona):
    """remove 後は通知されない。"""
    calls = []
    cb = lambda *args: calls.append(args)  # noqa: E731
    tm.add_alert_observer(cb)
    tm.remove_alert_observer(cb)
    t = tm.create(persona, "autonomous")
    tm.activate(t)
    tm.pause(t)
    tm.set_alert(t)
    assert calls == []


def test_add_same_observer_twice_is_idempotent(tm, persona):
    """同じ observer を二重登録しても 1 回しか呼ばれない。"""
    calls = []
    cb = lambda *args: calls.append(args)  # noqa: E731
    tm.add_alert_observer(cb)
    tm.add_alert_observer(cb)
    t = tm.create(persona, "autonomous")
    tm.activate(t)
    tm.pause(t)
    tm.set_alert(t)
    assert len(calls) == 1


def test_set_alert_default_context_includes_track_metadata(tm, persona):
    """Phase 2.6: context を渡さなくても observer は Track 識別情報を含む dict を受け取る。

    target_track_title / target_track_type が常に乗る (None でなければ)。
    """
    calls = []
    tm.add_alert_observer(lambda pid, tid, ctx: calls.append(ctx))
    t = tm.create(persona, "autonomous", title="auto")
    tm.activate(t)
    tm.pause(t)
    tm.set_alert(t)
    assert len(calls) == 1
    ctx = calls[0]
    assert ctx["target_track_title"] == "auto"
    assert ctx["target_track_type"] == "autonomous"
    assert "target_already_running" not in ctx  # 通常遷移ではフラグなし
