"""出来事 (Episode) の読み取り口が旧データに対してどう答えるかの配線テスト。

⚠ このファイルは元々「出来事の開閉を実経路へ接続する」配線テストだった。
その配線は束 6c (2026-08-22、autonomous_behavior_v3.md §7) で全部退役した —
「エピソードという専用の記録行は持たない」の裁定で、開閉の書き手 (会話シム
経路 / run_work_session / day_plan._fire_slot / SEARuntime._store_memory の
origin_episode 刻印) が消え、旧エピソードの情報は台帳・Chronicle が持つように
なった (始まりと終わりはどこにも記録しない — 2026-08-23 裁定)。以下の節はその
書き手ごと削除した:

- 会話の出来事 (day_scenario の EpisodeSimUserEventDriver、occurrence_id の
  Building 共有) — ドライバごと退役
- 作業セッションの出来事 (run_work_session の open→close、meta 書式契約、
  digest_ref、出自 = 開いている slot 出来事)
- コマの出来事 (_fire_slot の open→close、presence スタブ、skip)
- 層0タグ (_store_memory が metadata.origin_episode を付ける/付けない)

残したのは**読み取り口**だけ (``episodes`` テーブルは読み取り専用の残置として
残る — v3 §9-8 ①)。fixture の行は ORM で直接挿入する: 書き手はもう居ないので、
旧世代が書き残した行を読む状況をそのまま作るのが唯一正しい再現になる。

- get_open_episode + per-persona キャッシュ (初回 DB 読み・以降キャッシュヒット)
- get_open_non_conversation_episode (「別の活動中か」の判定集合)

DB は in-memory SQLite (StaticPool)。
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import AI, Base, City, Episode, User
from saiverse import clock, episodes

PERSONA_ID = "alice"
BUILDING = "alice_room"


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


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


@pytest.fixture(autouse=True)
def _reset_clock():
    yield
    clock.disable_virtual()


def _seed_personas(session_factory, persona_ids: List[str]) -> None:
    db = session_factory()
    try:
        db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
        db.flush()
        city = City(USERID=1, CITY_SLUG="test_city", UI_PORT=3001, API_PORT=8001)
        db.add(city)
        db.flush()
        for pid in persona_ids:
            db.add(AI(AIID=pid, HOME_CITYID=city.CITYID, AINAME=pid.title()))
        db.commit()
    finally:
        db.close()


def _make_manager(session_factory, persona_ids: Optional[List[str]] = None,
                  building: str = BUILDING) -> SimpleNamespace:
    persona_ids = persona_ids or [PERSONA_ID]
    _seed_personas(session_factory, persona_ids)
    personas = {
        pid: SimpleNamespace(
            persona_id=pid,
            persona_name=pid.title(),
            current_building_id=building,
            private_room_id=building,
            sai_memory=SimpleNamespace(
                get_current_thread=lambda pid=pid: f"{pid}:persona_main",
            ),
        )
        for pid in persona_ids
    }
    return SimpleNamespace(SessionLocal=session_factory, personas=personas)


#: ペルソナごとの SHORT_ID 採番 (episode:N の N)。書き手が退役したので
#: fixture 側で「ペルソナ内連番」の規則を再現する。
_SHORT_IDS: Dict[str, int] = {}


@pytest.fixture(autouse=True)
def _reset_short_ids():
    _SHORT_IDS.clear()
    yield
    _SHORT_IDS.clear()


def _insert_episode(
    session_factory,
    persona_id: str,
    kind: str,
    *,
    status: str = episodes.STATUS_OPEN,
    started_at: int = 1_000,
    ended_at: Optional[int] = None,
    building_id: Optional[str] = BUILDING,
    participants: Optional[List[str]] = None,
    origin_ref: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """旧世代が書き残した episodes 行を 1 件作る (書き込み API の代わり)。"""
    short_id = _SHORT_IDS.get(persona_id, 0) + 1
    _SHORT_IDS[persona_id] = short_id
    episode_id = str(uuid.uuid4())
    db = session_factory()
    try:
        db.add(Episode(
            EPISODE_ID=episode_id,
            PERSONA_ID=persona_id,
            SHORT_ID=short_id,
            KIND=kind,
            STARTED_AT=started_at,
            ENDED_AT=ended_at,
            BUILDING_ID=building_id,
            PARTICIPANTS_JSON=(
                json.dumps(participants, ensure_ascii=False)
                if participants is not None else None
            ),
            ORIGIN_REF=origin_ref,
            STATUS=status,
            META_JSON=(
                json.dumps(meta, ensure_ascii=False) if meta is not None else None
            ),
        ))
        db.commit()
    finally:
        db.close()
    return {
        "episode_id": episode_id,
        "short_id": short_id,
        "episode_ref": f"episode:{short_id}",
    }


# ---------------------------------------------------------------------------
# 0. get_open_episode + キャッシュ
# ---------------------------------------------------------------------------


def test_get_open_episode_none_when_empty(session_factory):
    manager = _make_manager(session_factory)
    assert episodes.get_open_episode(manager, PERSONA_ID) is None
    # kind 指定 (非キャッシュ経路) も None
    assert episodes.get_open_episode(
        manager, PERSONA_ID, kind=episodes.KIND_CONVERSATION) is None


def test_get_open_episode_returns_last_opened(session_factory):
    """open が複数残っていれば SHORT_ID 最大 (最後に開かれた 1 件) を返す。"""
    manager = _make_manager(session_factory)
    _insert_episode(session_factory, PERSONA_ID, episodes.KIND_SLOT)
    newest = _insert_episode(session_factory, PERSONA_ID, episodes.KIND_WORK_SESSION)
    assert episodes.get_open_episode(
        manager, PERSONA_ID)["episode_id"] == newest["episode_id"]


def test_get_open_episode_kind_filter_selects_by_kind(session_factory):
    """kind 指定は「最後に開いた 1 件」ではなくその種類の最新を返す。"""
    manager = _make_manager(session_factory)
    slot = _insert_episode(session_factory, PERSONA_ID, episodes.KIND_SLOT)
    _insert_episode(session_factory, PERSONA_ID, episodes.KIND_WORK_SESSION)
    assert episodes.get_open_episode(
        manager, PERSONA_ID, kind=episodes.KIND_SLOT)["episode_id"] == slot["episode_id"]


def test_get_open_episode_ignores_closed_rows(session_factory):
    manager = _make_manager(session_factory)
    _insert_episode(
        session_factory, PERSONA_ID, episodes.KIND_SLOT,
        status=episodes.STATUS_CLOSED, ended_at=2_000,
    )
    assert episodes.get_open_episode(manager, PERSONA_ID) is None


def test_get_open_episode_reads_db_on_fresh_manager(session_factory):
    """プロセス再起動 (= 新 manager、キャッシュ空) でも DB から復元される。"""
    manager = _make_manager(session_factory)
    ep = _insert_episode(session_factory, PERSONA_ID, episodes.KIND_CONVERSATION)
    assert episodes.get_open_episode(
        manager, PERSONA_ID)["episode_id"] == ep["episode_id"]

    fresh_manager = SimpleNamespace(SessionLocal=session_factory)
    restored = episodes.get_open_episode(fresh_manager, PERSONA_ID)
    assert restored is not None
    assert restored["episode_id"] == ep["episode_id"]


def test_get_open_episode_uses_cache_without_db(session_factory):
    """2 回目以降の読み出しは DB を引かない (キャッシュヒット)。"""
    manager = _make_manager(session_factory)
    _insert_episode(session_factory, PERSONA_ID, episodes.KIND_SLOT)
    episodes.get_open_episode(manager, PERSONA_ID)  # キャッシュ形成

    def _boom():
        raise AssertionError("DB should not be hit on cache hit")

    manager.SessionLocal = _boom
    result = episodes.get_open_episode(manager, PERSONA_ID)
    assert result is not None


# ---------------------------------------------------------------------------
# 0-b. get_open_non_conversation_episode (「別の活動中か」の判定集合)
# ---------------------------------------------------------------------------


def test_open_non_conversation_none_when_only_conversation(session_factory):
    """開いているのが会話だけなら「別の活動」は無い。"""
    manager = _make_manager(session_factory)
    _insert_episode(session_factory, PERSONA_ID, episodes.KIND_CONVERSATION)
    assert episodes.get_open_non_conversation_episode(manager, PERSONA_ID) is None


@pytest.mark.parametrize("conversation_first", [True, False])
def test_open_non_conversation_is_order_independent(session_factory, conversation_first):
    """会話と作業が同時に開いていても、**開いた順に依らず**作業が見える。

    回帰 (2026-08-14 Codex 指摘 F2): 「最後に開いた 1 件」を見る読み方だと、
    会話が後に開いた並びで作業が隠れ、仲裁するかどうかが順序で変わっていた。
    """
    manager = _make_manager(session_factory)
    if conversation_first:
        _insert_episode(session_factory, PERSONA_ID, episodes.KIND_CONVERSATION)
        work = _insert_episode(session_factory, PERSONA_ID, episodes.KIND_WORK_SESSION)
    else:
        work = _insert_episode(session_factory, PERSONA_ID, episodes.KIND_WORK_SESSION)
        _insert_episode(session_factory, PERSONA_ID, episodes.KIND_CONVERSATION)

    found = episodes.get_open_non_conversation_episode(manager, PERSONA_ID)
    assert found is not None
    assert found["episode_id"] == work["episode_id"]


def test_open_non_conversation_ignores_stale_open_cache(session_factory):
    """判定は DB を直に引く — 層0タグ用の open キャッシュに引きずられない。

    ``get_open_episode(kind=None)`` のキャッシュは manager 単位に居座るので、
    stale な会話 dict が残ることがある。その場合でも「別の活動中か」の答えは
    DB の実態で決まる。
    """
    manager = _make_manager(session_factory)
    work = _insert_episode(session_factory, PERSONA_ID, episodes.KIND_WORK_SESSION)
    episodes._cache_set_open(
        manager, PERSONA_ID,
        {"episode_id": "stale", "kind": episodes.KIND_CONVERSATION},
    )

    found = episodes.get_open_non_conversation_episode(manager, PERSONA_ID)
    assert found is not None
    assert found["episode_id"] == work["episode_id"]


def test_open_non_conversation_ignores_closed_rows(session_factory):
    """閉じた作業は「別の活動」ではない。"""
    manager = _make_manager(session_factory)
    _insert_episode(
        session_factory, PERSONA_ID, episodes.KIND_WORK_SESSION,
        status=episodes.STATUS_CLOSED, ended_at=2_000,
    )
    assert episodes.get_open_non_conversation_episode(manager, PERSONA_ID) is None
