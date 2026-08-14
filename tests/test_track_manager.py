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
        city = City(USERID=1, CITY_SLUG="test_city", UI_PORT=3001, API_PORT=8001)
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


def _armed_entry(scheduler, track_id):
    """EventScheduler に登録されている wait_response 予約の実エントリ。"""
    return scheduler._entries_by_key[f"wait_response_timeout:{track_id}"]


def test_ensure_only_if_absent_preserves_the_live_reservation(session_factory, persona):
    """復旧 (only_if_absent=True) は生きている予約を置き換えない — 実物同士の境界検証。

    2026-07-29: 起動時の再確立が、待っている間に通常経路 (ユーザー発話への同期応答 /
    activate) が張った予約を上書きすると、基準時刻が張り直しの瞬間へ丸め直されて
    **会話終了の期限が後退する**。上書き禁止が SAIVerseManager →
    TrackManager → EventScheduler と端まで届いていることを、fake を挟まずに固定する
    (fake だけだと「フラグを渡したこと」しか見えず、TrackManager が通常 schedule を
    呼ぶ・フラグを落とす・別キーを使う回帰を素通りさせる)。
    """
    from saiverse.event_scheduler import EventScheduler

    scheduler = EventScheduler()  # start() しない (登録内容だけ見る)

    def provider(track):
        return (60, None)

    tm = TrackManager(
        session_factory=session_factory,
        event_scheduler=scheduler,
        wait_response_timeout_provider=provider,
    )
    t = tm.create(
        persona, "user_conversation", is_persistent=True,
        initial_status=STATUS_RUNNING,
    )
    original = _armed_entry(scheduler, t)

    tm.ensure_wait_response_timeout(persona, only_if_absent=True)

    assert _armed_entry(scheduler, t) is original, "復旧が生きている予約を置き換えた"
    assert not original.cancelled


def test_ensure_without_only_if_absent_replaces_the_reservation(session_factory, persona):
    """対照: 既定 (only_if_absent 無し) は従来どおり張り直す。

    上のテストが「常に何もしない」実装でも通ってしまわないための対。
    """
    from saiverse.event_scheduler import EventScheduler

    scheduler = EventScheduler()

    def provider(track):
        return (60, None)

    tm = TrackManager(
        session_factory=session_factory,
        event_scheduler=scheduler,
        wait_response_timeout_provider=provider,
    )
    t = tm.create(
        persona, "user_conversation", is_persistent=True,
        initial_status=STATUS_RUNNING,
    )
    original = _armed_entry(scheduler, t)

    tm.ensure_wait_response_timeout(persona)

    assert _armed_entry(scheduler, t) is not original
    assert original.cancelled


def test_recovery_never_uses_the_overwriting_schedule_api(session_factory, persona):
    """復旧経路は上書きする側の入口 (schedule) を使わない。

    上の 2 本は単一スレッドで結果だけを見るため、``has_key`` で確認してから
    ``schedule`` を呼ぶ check-then-act 実装でも通ってしまう (2026-07-29 に実測。
    実装を差し戻しても 78 件全緑だった)。判定と登録が同一ロック区間で行われる
    ことは EventScheduler 側の並行テストが担保するので、ここでは**復旧経路が
    その原子的 API に到達していること**を固定する — 経路が通常 schedule に
    落ちれば、いくら EventScheduler が原子的でも競合窓は開く。
    """
    from saiverse.event_scheduler import EventScheduler

    class _RecordingScheduler(EventScheduler):
        def __init__(self):
            super().__init__()
            self.plain_schedule_keys = []
            self.if_absent_keys = []

        def schedule(self, fire_at, callback, key):
            self.plain_schedule_keys.append(key)
            return super().schedule(fire_at=fire_at, callback=callback, key=key)

        def schedule_if_absent(self, fire_at, callback, key):
            self.if_absent_keys.append(key)
            return super().schedule_if_absent(
                fire_at=fire_at, callback=callback, key=key,
            )

    scheduler = _RecordingScheduler()

    def provider(track):
        return (60, None)

    tm = TrackManager(
        session_factory=session_factory,
        event_scheduler=scheduler,
        wait_response_timeout_provider=provider,
    )
    t = tm.create(
        persona, "user_conversation", is_persistent=True,
        initial_status=STATUS_RUNNING,
    )
    key = f"wait_response_timeout:{t}"
    # create (通常経路) は上書きする側でよい
    assert key in scheduler.plain_schedule_keys
    scheduler.plain_schedule_keys.clear()

    # 予約が失われた状態 = 再起動直後を再現してから復旧させる
    scheduler.cancel(key)
    tm.ensure_wait_response_timeout(persona, only_if_absent=True)

    assert key in scheduler.if_absent_keys, "復旧が原子的 API を使っていない"
    assert key not in scheduler.plain_schedule_keys, "復旧が上書きする側の API を使った"


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


# NOTE: 旧 set_alert (alert 状態への遷移) と alert observer 機構は
# track_retirement.md §7.4 で撤去された。既存 DB の alert 行の互換として
# 「alert からの activate」だけが生きている。


def test_activate_from_legacy_alert_status(tm, persona, session_factory):
    """既存 DB に残る alert 行 (旧 set_alert の遺産) からも activate できる。"""
    from database.models import ActionTrack

    t = tm.create(persona, "autonomous")
    db = session_factory()
    try:
        row = db.query(ActionTrack).filter(ActionTrack.track_id == t).first()
        row.status = STATUS_ALERT
        db.commit()
    finally:
        db.close()
    tm.activate(t)
    assert tm.get(t).status == STATUS_RUNNING


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
        ("forget", ("nope",)),
        ("recall", ("nope",)),
    ]:
        op = getattr(tm, op_name)
        with pytest.raises(TrackNotFoundError):
            op(*args)


# NOTE: 旧 alert observer 機構 (add_alert_observer / _notify_alert) のテスト群は
# 機構の撤去 (track_retirement.md §7.4) と同時に退役した。
