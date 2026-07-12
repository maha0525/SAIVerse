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
    TrackManager,
    TrackNotFoundError,
)


@pytest.fixture
def session_factory():
    """In-memory SQLite session factory.

    StaticPool + check_same_thread=False により、thread 跨ぎでも同一 :memory: DB
    を共有できる (wait_response timeout 等の EventScheduler dispatch thread から
    DB アクセスがあるため)。
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


def test_wait_response_timeout_schedules_on_activate(session_factory, persona):
    """activate() で provider が wait_response を返したらタイマー予約が入る。"""
    from saiverse.event_scheduler import EventScheduler
    scheduler = EventScheduler()
    scheduler.start()
    try:
        # provider: 60 分 + last_msg=None (= 即時タイムアウト無し、now+60min)
        provider_calls = []
        def provider(track):
            provider_calls.append(track.track_id)
            return (60, None)

        tm_with_sched = TrackManager(
            session_factory=session_factory,
            event_scheduler=scheduler,
            wait_response_timeout_provider=provider,
        )
        t = tm_with_sched.create(persona, "user_conversation", is_persistent=True)
        tm_with_sched.activate(t)
        assert provider_calls == [t]
        assert scheduler.has_key(f"wait_response_timeout:{t}")
    finally:
        scheduler.stop()


def test_wait_response_timeout_provider_returns_none_skips(session_factory, persona):
    """provider が None を返したらタイマー予約しない (= wait_response 対象外 Track)。"""
    from saiverse.event_scheduler import EventScheduler
    scheduler = EventScheduler()
    scheduler.start()
    try:
        def provider(track):
            return None  # 対象外

        tm_with_sched = TrackManager(
            session_factory=session_factory,
            event_scheduler=scheduler,
            wait_response_timeout_provider=provider,
        )
        t = tm_with_sched.create(persona, "autonomous")
        tm_with_sched.activate(t)
        assert not scheduler.has_key(f"wait_response_timeout:{t}")
    finally:
        scheduler.stop()


def test_wait_response_timeout_canceled_on_pause(session_factory, persona):
    """pause で wait_response タイマーが解除される (= レース防止)。"""
    from saiverse.event_scheduler import EventScheduler
    scheduler = EventScheduler()
    scheduler.start()
    try:
        def provider(track):
            return (60, None)

        tm_with_sched = TrackManager(
            session_factory=session_factory,
            event_scheduler=scheduler,
            wait_response_timeout_provider=provider,
        )
        t = tm_with_sched.create(persona, "user_conversation", is_persistent=True)
        tm_with_sched.activate(t)
        assert scheduler.has_key(f"wait_response_timeout:{t}")
        tm_with_sched.pause(t)
        assert not scheduler.has_key(f"wait_response_timeout:{t}")
    finally:
        scheduler.stop()


def test_wait_response_timeout_canceled_on_other_activate(session_factory, persona):
    """別 Track の activate で押し出された Track の wait_response タイマーも解除される。"""
    from saiverse.event_scheduler import EventScheduler
    scheduler = EventScheduler()
    scheduler.start()
    try:
        def provider(track):
            # user_conversation だけ wait_response 対象に
            if track.track_type == "user_conversation":
                return (60, None)
            return None

        tm_with_sched = TrackManager(
            session_factory=session_factory,
            event_scheduler=scheduler,
            wait_response_timeout_provider=provider,
        )
        t1 = tm_with_sched.create(persona, "user_conversation", is_persistent=True)
        t2 = tm_with_sched.create(persona, "autonomous")
        tm_with_sched.activate(t1)
        assert scheduler.has_key(f"wait_response_timeout:{t1}")
        # t2 を activate → t1 が pending に押し出される + タイマー解除
        tm_with_sched.activate(t2)
        assert not scheduler.has_key(f"wait_response_timeout:{t1}")
    finally:
        scheduler.stop()


def test_wait_response_timeout_fires_callback_without_pausing(session_factory, persona):
    """タイムアウト発火で callback は呼ばれるが、Track は running のまま動かない。

    life.md §7 案 Y (2026-07-13): Track の状態遷移は時間経過では起きない。
    「いま」の真実は開いているエピソードが持つため、wait_response タイムアウトの
    仕事は callback 起動 (会話出来事の close / メタ判断) のみに縮退した。

    activate 時の schedule は `base_time < now` フォールバック (2026-05-10) で
    `now + minutes` にずらされるため即時 fire しない。代わりに `_handle_…` を
    直接叩いて再評価ロジック (idle_for >= threshold) を通す。
    """
    from datetime import datetime as _dt, timedelta as _td
    from saiverse.event_scheduler import EventScheduler

    scheduler = EventScheduler()
    scheduler.start()
    callback_calls = []

    def callback(persona_id, track_id):
        callback_calls.append((persona_id, track_id))

    # base_time=2分前 + minutes=1 → 再評価時に idle_for >= 1min が成立 → callback
    def provider(track):
        return (1, _dt.now() - _td(minutes=2))

    try:
        tm_with_sched = TrackManager(
            session_factory=session_factory,
            event_scheduler=scheduler,
            wait_response_timeout_provider=provider,
            wait_response_timeout_callback=callback,
        )
        t = tm_with_sched.create(persona, "user_conversation", is_persistent=True)
        tm_with_sched.activate(t)
        # 再評価ルートを直接走らせる (schedule 側は now+1min まで動かない)
        tm_with_sched._handle_wait_response_timeout(t, persona)
        assert callback_calls == [(persona, t)]
        # Track は running のまま (時間経過は目的を動かさない — 不変条件 4)
        assert tm_with_sched.get(t).status == STATUS_RUNNING
    finally:
        scheduler.stop()


def test_wait_response_timeout_reschedules_when_not_idle_enough(session_factory, persona):
    """idle 期間が閾値に満たないと再スケジュールされ、callback は呼ばれない。"""
    import time as _time
    from saiverse.event_scheduler import EventScheduler

    scheduler = EventScheduler()
    scheduler.start()
    callback_calls = []

    def callback(persona_id, track_id):
        callback_calls.append((persona_id, track_id))

    # base_time=直前 + minutes=60 → fire_at は約 60 分後 → そもそも fire しない。
    # ただし発火直後に時間が極短いまま再呼び出しされる事故を再現するため、
    # minutes=1 + base_time が発火時に「ちょうど 1 分前」になるよう操作する。
    # 簡略化: provider は base_time=now() を毎回返し、minutes=1 で fire_at=now+1min。
    # 1.5 秒待っても callback は来ない (まだ 1 分経っていない)。
    from datetime import datetime as _dt
    def provider(track):
        return (1, _dt.now())

    try:
        tm_with_sched = TrackManager(
            session_factory=session_factory,
            event_scheduler=scheduler,
            wait_response_timeout_provider=provider,
            wait_response_timeout_callback=callback,
        )
        t = tm_with_sched.create(persona, "user_conversation", is_persistent=True)
        tm_with_sched.activate(t)
        _time.sleep(1.0)
        # 1 秒では 1 分閾値に達していない → callback 未呼び出し
        assert callback_calls == []
        # Track は依然 running
        assert tm_with_sched.get(t).status == STATUS_RUNNING
    finally:
        scheduler.stop()


def test_ensure_wait_response_timeout_reestablishes_on_running(session_factory, persona):
    """ensure_wait_response_timeout() が running Track にタイマーを張り直す (C)。

    タイマーは activate 時にしか張られず EventScheduler はインメモリのため
    再起動で失われる。起動時相当に running Track が存在する状態でタイマーが
    無いところから、ensure_wait_response_timeout で再確立されることを確認する。
    """
    from saiverse.event_scheduler import EventScheduler
    scheduler = EventScheduler()
    scheduler.start()
    try:
        def provider(track):
            return (60, None)

        tm_with_sched = TrackManager(
            session_factory=session_factory,
            event_scheduler=scheduler,
            wait_response_timeout_provider=provider,
        )
        t = tm_with_sched.create(persona, "user_conversation", is_persistent=True)
        tm_with_sched.activate(t)
        # 「再起動でタイマーが失われた」状態を再現
        scheduler.cancel(f"wait_response_timeout:{t}")
        assert not scheduler.has_key(f"wait_response_timeout:{t}")
        # 再確立
        tm_with_sched.ensure_wait_response_timeout(persona)
        assert scheduler.has_key(f"wait_response_timeout:{t}")
    finally:
        scheduler.stop()


def test_ensure_wait_response_timeout_skips_when_provider_none(session_factory, persona):
    """provider が None (= Idle / 対象外) を返すなら再確立しても予約は入らない (A+C)。"""
    from saiverse.event_scheduler import EventScheduler
    scheduler = EventScheduler()
    scheduler.start()
    try:
        # activate 時は予約させ、その後 provider を「対象外」に切り替えて
        # ensure 時に None を返すようにする (Active→Idle 相当)。
        active = {"on": True}

        def provider(track):
            return (60, None) if active["on"] else None

        tm_with_sched = TrackManager(
            session_factory=session_factory,
            event_scheduler=scheduler,
            wait_response_timeout_provider=provider,
        )
        t = tm_with_sched.create(persona, "user_conversation", is_persistent=True)
        tm_with_sched.activate(t)
        scheduler.cancel(f"wait_response_timeout:{t}")
        active["on"] = False  # 以後 provider は None (Idle ゲート相当)
        tm_with_sched.ensure_wait_response_timeout(persona)
        assert not scheduler.has_key(f"wait_response_timeout:{t}")
    finally:
        scheduler.stop()


def test_ensure_wait_response_timeout_noop_without_running(session_factory, persona):
    """running Track が無ければ ensure_wait_response_timeout は no-op (例外を出さない)。"""
    from saiverse.event_scheduler import EventScheduler
    scheduler = EventScheduler()
    scheduler.start()
    try:
        def provider(track):
            return (60, None)

        tm_with_sched = TrackManager(
            session_factory=session_factory,
            event_scheduler=scheduler,
            wait_response_timeout_provider=provider,
        )
        # 未 activate (unstarted) のみ存在 → running は無い
        tm_with_sched.create(persona, "user_conversation", is_persistent=True)
        tm_with_sched.ensure_wait_response_timeout(persona)  # 例外なく no-op
        assert scheduler.pending_count() == 0
    finally:
        scheduler.stop()


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


def test_persistent_track_can_pause(tm, persona):
    t = tm.create(persona, "social", is_persistent=True)
    tm.activate(t)
    tm.pause(t)
    assert tm.get(t).status == STATUS_PENDING


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
        ("complete", ("nope",)),
        ("abort", ("nope",)),
        ("set_alert", ("nope",)),
        ("forget", ("nope",)),
        ("recall", ("nope",)),
    ]:
        op = getattr(tm, op_name)
        with pytest.raises(TrackNotFoundError):
            op(*args)


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
