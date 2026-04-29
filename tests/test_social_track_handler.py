"""SocialTrackHandler unit tests (Phase B-X).

Handler の責務:
- ensure_track: 既存の social Track があれば返す、なければ作成
- 初期状態は unstarted (即 activate しない)
- 永続 Track として作成 (is_persistent=True, output_target=building:current)
- 冪等性: 何度呼んでも複数作られない
- ペルソナ間で混ざらない
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import AI, Base, City, User
from saiverse.track_handlers import SocialTrackHandler
from saiverse.track_handlers.social_track_handler import (
    SOCIAL_TRACK_OUTPUT_TARGET,
    SOCIAL_TRACK_TITLE,
    SOCIAL_TRACK_TYPE,
)
from saiverse.track_manager import (
    STATUS_RUNNING,
    STATUS_UNSTARTED,
    TrackManager,
)


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    yield Session
    engine.dispose()


@pytest.fixture
def db_personas(session_factory):
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
    return ["alice", "bob"]


@pytest.fixture
def tm(session_factory):
    return TrackManager(session_factory=session_factory)


@pytest.fixture
def handler(tm):
    return SocialTrackHandler(track_manager=tm)


# ---------------------------------------------------------------------------
# 初回作成
# ---------------------------------------------------------------------------

def test_first_call_creates_unstarted_persistent_track(handler, db_personas):
    """初回呼び出しで unstarted の永続 social Track が作られる。"""
    track = handler.ensure_track("alice")
    assert track.track_type == SOCIAL_TRACK_TYPE
    assert track.title == SOCIAL_TRACK_TITLE
    assert track.is_persistent is True
    assert track.output_target == SOCIAL_TRACK_OUTPUT_TARGET
    # 即 activate しない (対ユーザー Track と競合させないため)
    assert track.status == STATUS_UNSTARTED


def test_track_belongs_to_correct_persona(handler, db_personas):
    track = handler.ensure_track("alice")
    assert track.persona_id == "alice"


# ---------------------------------------------------------------------------
# 冪等性
# ---------------------------------------------------------------------------

def test_second_call_returns_existing_track(handler, db_personas):
    """同じペルソナで 2 回呼んでも同じ Track が返る (新規作成しない)。"""
    t1 = handler.ensure_track("alice")
    t2 = handler.ensure_track("alice")
    assert t1.track_id == t2.track_id


def test_multiple_calls_dont_create_duplicates(handler, tm, db_personas):
    """5 回呼んでも social Track は 1 つだけ。"""
    for _ in range(5):
        handler.ensure_track("alice")
    tracks = [
        t for t in tm.list_for_persona("alice")
        if t.track_type == SOCIAL_TRACK_TYPE
    ]
    assert len(tracks) == 1


def test_existing_track_is_not_modified(handler, tm, db_personas):
    """既に存在する social Track の状態を勝手に変更しない。"""
    t1 = handler.ensure_track("alice")
    # 後から activate されたとする
    tm.activate(t1.track_id)
    assert tm.get(t1.track_id).status == STATUS_RUNNING
    # 再度 ensure 呼んでも status を unstarted に戻したりしない
    t2 = handler.ensure_track("alice")
    assert t2.track_id == t1.track_id
    assert tm.get(t1.track_id).status == STATUS_RUNNING


# ---------------------------------------------------------------------------
# ペルソナ間の独立性
# ---------------------------------------------------------------------------

def test_different_personas_get_separate_tracks(handler, db_personas):
    t_alice = handler.ensure_track("alice")
    t_bob = handler.ensure_track("bob")
    assert t_alice.track_id != t_bob.track_id
    assert t_alice.persona_id == "alice"
    assert t_bob.persona_id == "bob"


def test_alice_existing_does_not_block_bob_creation(handler, tm, db_personas):
    """alice の Track があっても bob 用は別途作られる。"""
    handler.ensure_track("alice")
    handler.ensure_track("bob")
    alice_tracks = [
        t for t in tm.list_for_persona("alice")
        if t.track_type == SOCIAL_TRACK_TYPE
    ]
    bob_tracks = [
        t for t in tm.list_for_persona("bob")
        if t.track_type == SOCIAL_TRACK_TYPE
    ]
    assert len(alice_tracks) == 1
    assert len(bob_tracks) == 1


# ---------------------------------------------------------------------------
# 他 Track 種別との独立性
# ---------------------------------------------------------------------------

def test_other_track_types_do_not_count_as_social(handler, tm, db_personas):
    """対ユーザー Track 等が既にあっても、social Track は別途作られる。"""
    # 別種別の Track を先に作っておく
    tm.create("alice", track_type="user_conversation", is_persistent=True)
    tm.create("alice", track_type="autonomous")

    t = handler.ensure_track("alice")
    assert t.track_type == SOCIAL_TRACK_TYPE

    # social Track は新規に 1 個できているはず
    social_tracks = [
        x for x in tm.list_for_persona("alice")
        if x.track_type == SOCIAL_TRACK_TYPE
    ]
    assert len(social_tracks) == 1
