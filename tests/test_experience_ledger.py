"""経験の台帳 (読み側) のテスト — experience_ledger.md §3。

対象:
- sai_memory/experience_ledger.py — 索引 (統計の実測値 / テーマページ掲載 /
  削除・trunk 除外) と動的合成 (fragment 新しい順 / 関与あらすじの履歴 /
  共起エンティティ / 対象外は None)
- api/routes/people/experience_ledger.py — 索引 + 合成ページの 2 本
  (正常系と 404、目的ノード合流)

fixtures は tests/test_slot_close_note.py の流儀 (in-memory memopedia +
main DB は必要なテストだけ)。本番データ (~/.saiverse) には触れない。
"""
from __future__ import annotations

import sqlite3
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.deps import get_manager
from database.models import AI, ActionTrack, Base, City, User
from sai_memory.experience_ledger import build_ledger_index, build_ledger_page
from sai_memory.memopedia.storage import create_fragment, create_page
from sai_memory.purpose_tags import LAYER_SHELVE, add_tag
from sai_memory.theme_pages import create_theme_page

PERSONA_ID = "alice"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn():
    from sai_memory.memopedia.storage import init_memopedia_tables
    from sai_memory.purpose_tags import init_purpose_tags_tables

    c = sqlite3.connect(":memory:", check_same_thread=False)
    init_memopedia_tables(c)
    init_purpose_tags_tables(c)
    yield c
    c.close()


def _make_chronicle_entry(
    conn, *, title: str, start_time: int, end_time: int, short_id: int
) -> str:
    """Chronicle エントリの物理格納 (memopedia_pages category='chronicle') を再現。"""
    page = create_page(
        conn,
        parent_id=None,
        title=title,
        category="chronicle",
        metadata={
            "level": 1,
            "start_time": start_time,
            "end_time": end_time,
            "short_id": short_id,
        },
    )
    return page.id


def _set_fragment_created_at(conn, fragment_id: str, epoch: int) -> None:
    """create_fragment は now() 固定なので、順序検証用に時刻を差し替える。"""
    conn.execute(
        "UPDATE memopedia_fragments SET created_at = ? WHERE id = ?",
        (epoch, fragment_id),
    )
    conn.commit()


@pytest.fixture
def seeded(conn):
    """索引・合成の両方で使う小さな世界。

    - people「まはー」: fragment 3 件 (source_date 3 日分、Chronicle 2 件由来)
    - terms「スタックチャン」: fragment 1 件 (まはーと同じ Chronicle 由来 = 共起)
    - theme「絵を描く」: 経験値ノート 1 件 (chronicle_entry_id なし)
    - plans「空ページ」: fragment ゼロ
    - events「消した出来事」: 削除済み (索引に載らない)
    """
    ch1 = _make_chronicle_entry(
        conn, title="05/25 触覚リサーチ", start_time=1000, end_time=2000, short_id=1
    )
    ch2 = _make_chronicle_entry(
        conn, title="06/13 アライメント調査", start_time=3000, end_time=4000, short_id=2
    )

    maha = create_page(
        conn, parent_id="root_people", title="まはー",
        summary="SAIVerse の作り手", category="people",
    )
    f1 = create_fragment(
        conn, entity_id=maha.id, content="触覚の話をした",
        chronicle_entry_id=ch1, source_date="2026-05-25",
    )
    f2 = create_fragment(
        conn, entity_id=maha.id, content="アライメントの相談",
        chronicle_entry_id=ch2, source_date="2026-06-13",
    )
    f3 = create_fragment(
        conn, entity_id=maha.id, content="誕生日は1月14日",
        chronicle_entry_id=ch2, source_date="2026-06-01",
    )
    _set_fragment_created_at(conn, f1.id, 1000)
    _set_fragment_created_at(conn, f2.id, 3000)
    _set_fragment_created_at(conn, f3.id, 2000)

    stack = create_page(
        conn, parent_id="root_terms", title="スタックチャン",
        summary="小さなロボット", category="terms",
    )
    create_fragment(
        conn, entity_id=stack.id, content="身体性リサーチの対象",
        chronicle_entry_id=ch1, source_date="2026-05-25",
    )

    theme_id = create_theme_page(
        conn, title="絵を描く", member_refs=[], origin="slot_close",
        content="「絵を描く」の経験のページ",
    )
    create_fragment(
        conn, entity_id=theme_id, content="淡い色から塗ると直しやすい",
        source_date="2026-08-03",
    )

    empty = create_page(
        conn, parent_id="root_plans", title="空ページ",
        summary="まだ何もない", category="plans",
    )

    deleted = create_page(
        conn, parent_id="root_events", title="消した出来事", category="events",
    )
    conn.execute(
        "UPDATE memopedia_pages SET is_deleted = 1 WHERE id = ?", (deleted.id,)
    )
    conn.commit()

    return SimpleNamespace(
        maha=maha, stack=stack, theme_id=theme_id, empty=empty,
        deleted=deleted, ch1=ch1, ch2=ch2,
    )


# ---------------------------------------------------------------------------
# 索引
# ---------------------------------------------------------------------------


class TestLedgerIndex:
    def test_stats_are_measured_values(self, conn, seeded):
        index = build_ledger_index(conn)
        by_id = {row["page_id"]: row for row in index}

        maha = by_id[seeded.maha.id]
        assert maha["stats"]["fragment_count"] == 3
        assert maha["stats"]["first_date"] == "2026-05-25"
        assert maha["stats"]["last_date"] == "2026-06-13"
        assert maha["stats"]["chronicle_count"] == 2
        assert maha["summary"] == "SAIVerse の作り手"

        stack = by_id[seeded.stack.id]
        assert stack["stats"]["fragment_count"] == 1
        assert stack["stats"]["chronicle_count"] == 1

    def test_theme_page_is_listed(self, conn, seeded):
        index = build_ledger_index(conn)
        by_id = {row["page_id"]: row for row in index}
        theme = by_id[seeded.theme_id]
        assert theme["category"] == "theme"
        assert theme["title"] == "絵を描く"
        # 経験値ノートは chronicle_entry_id を持たない → あらすじ数 0 が正直な値
        assert theme["stats"]["fragment_count"] == 1
        assert theme["stats"]["chronicle_count"] == 0

    def test_empty_shelf_is_listed_with_zero_stats(self, conn, seeded):
        index = build_ledger_index(conn)
        by_id = {row["page_id"]: row for row in index}
        empty = by_id[seeded.empty.id]
        assert empty["stats"]["fragment_count"] == 0
        assert empty["stats"]["first_date"] is None
        assert empty["stats"]["last_date"] is None

    def test_deleted_and_trunk_pages_are_excluded(self, conn, seeded):
        index = build_ledger_index(conn)
        ids = {row["page_id"] for row in index}
        assert seeded.deleted.id not in ids
        # カテゴリルート (trunk) は棚でない
        assert "root_people" not in ids
        assert "root_theme" not in ids
        # Chronicle は台帳カテゴリ外
        assert seeded.ch1 not in ids

    def test_category_grouping_order(self, conn, seeded):
        index = build_ledger_index(conn)
        categories = [row["category"] for row in index]
        # CATEGORY_DEFS の order どおり: people → terms → plans → theme
        assert categories == sorted(
            categories,
            key=lambda c: ["people", "terms", "plans", "events", "theme"].index(c),
        )


# ---------------------------------------------------------------------------
# 動的合成
# ---------------------------------------------------------------------------


class TestLedgerPage:
    def test_fragments_newest_first(self, conn, seeded):
        page = build_ledger_page(conn, seeded.maha.id)
        assert page is not None
        contents = [f["content"] for f in page["fragments"]]
        assert contents == ["アライメントの相談", "誕生日は1月14日", "触覚の話をした"]

    def test_involvement_history(self, conn, seeded):
        page = build_ledger_page(conn, seeded.maha.id)
        entries = page["involvement"]["entries"]
        # 新しい順 (end_time desc): ch2 → ch1
        assert [e["entry_id"] for e in entries] == [seeded.ch2, seeded.ch1]
        assert entries[0]["title"] == "06/13 アライメント調査"
        assert entries[0]["start_time"] == 3000
        assert entries[0]["end_time"] == 4000
        assert entries[0]["short_id"] == 2
        assert page["involvement"]["unresolved_count"] == 0

    def test_involvement_unresolved_entry_counted(self, conn, seeded):
        create_fragment(
            conn, entity_id=seeded.maha.id, content="宙に浮いた記録",
            chronicle_entry_id="ghost-entry", source_date="2026-07-01",
        )
        page = build_ledger_page(conn, seeded.maha.id)
        assert page["involvement"]["unresolved_count"] == 1
        # 解決できた 2 件はそのまま載る
        assert len(page["involvement"]["entries"]) == 2

    def test_related_cooccurring_pages(self, conn, seeded):
        page = build_ledger_page(conn, seeded.maha.id)
        related = page["related"]
        assert [r["page_id"] for r in related] == [seeded.stack.id]
        assert related[0]["shared_count"] == 1
        # 逆方向でも共起が引ける
        page2 = build_ledger_page(conn, seeded.stack.id)
        assert [r["page_id"] for r in page2["related"]] == [seeded.maha.id]

    def test_theme_page_synthesis_is_honest(self, conn, seeded):
        page = build_ledger_page(conn, seeded.theme_id)
        assert page["fragments"][0]["content"] == "淡い色から塗ると直しやすい"
        # 経験値ノートは Chronicle への辺を持たない → 履歴・共起は空 (正直な現状)
        assert page["involvement"]["entries"] == []
        assert page["related"] == []

    def test_out_of_scope_pages_return_none(self, conn, seeded):
        assert build_ledger_page(conn, "no-such-page") is None
        assert build_ledger_page(conn, seeded.deleted.id) is None
        assert build_ledger_page(conn, "root_people") is None  # trunk
        assert build_ledger_page(conn, seeded.ch1) is None  # chronicle

    def test_short_ref_resolution(self, conn, seeded):
        by_uuid = build_ledger_page(conn, seeded.maha.id)
        by_short = build_ledger_page(conn, f"memopedia:{by_uuid['page']['short_id']}")
        assert by_short is not None
        assert by_short["page"]["page_id"] == seeded.maha.id


# ---------------------------------------------------------------------------
# API (TestClient)
# ---------------------------------------------------------------------------


class StubMemoryAdapter:
    def __init__(self, conn):
        self.conn = conn
        self._db_lock = threading.RLock()

    def is_ready(self):
        return True


@pytest.fixture
def client(conn, seeded):
    """薄い manager (main DB なし) の TestClient。目的ノードはフェイルオープンで空。"""
    from api.routes.people import experience_ledger as route

    manager = SimpleNamespace(
        personas={
            PERSONA_ID: SimpleNamespace(sai_memory=StubMemoryAdapter(conn))
        },
    )
    app = FastAPI()
    app.include_router(route.router, prefix="/api/people")
    app.dependency_overrides[get_manager] = lambda: manager
    return TestClient(app)


class TestLedgerApi:
    def test_index_endpoint(self, client, seeded):
        res = client.get(f"/api/people/{PERSONA_ID}/experience-ledger")
        assert res.status_code == 200
        data = res.json()
        keys = [c["key"] for c in data["categories"]]
        assert keys == ["people", "terms", "plans", "theme"]
        labels = {c["key"]: c["label"] for c in data["categories"]}
        assert labels["theme"] == "テーマ"
        people_pages = next(
            c for c in data["categories"] if c["key"] == "people"
        )["pages"]
        assert people_pages[0]["stats"]["fragment_count"] == 3
        # main DB の無い薄い manager → 目的ノードは空 (索引本体は独立して返る)
        assert data["purposes"] == []

    def test_page_endpoint(self, client, seeded):
        res = client.get(
            f"/api/people/{PERSONA_ID}/experience-ledger/{seeded.maha.id}"
        )
        assert res.status_code == 200
        data = res.json()
        assert data["page"]["title"] == "まはー"
        assert len(data["fragments"]) == 3
        assert len(data["involvement"]["entries"]) == 2

    def test_page_endpoint_404(self, client):
        res = client.get(
            f"/api/people/{PERSONA_ID}/experience-ledger/no-such-page"
        )
        assert res.status_code == 404

    def test_unknown_persona_404(self, client):
        res = client.get("/api/people/nobody/experience-ledger")
        assert res.status_code == 404


# ---------------------------------------------------------------------------
# API: 目的ノードの合流 (main DB あり)
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


@pytest.fixture
def full_client(conn, seeded, session_factory):
    """main DB (目的ノード) 込みの TestClient。fixtures は test_slot_close_note の流儀。"""
    import uuid

    from saiverse.persona_task_manager import (
        PARENT_TRACK,
        STAGE_CANDIDATE,
        PersonaTaskManager,
    )

    from api.routes.people import experience_ledger as route

    db = session_factory()
    try:
        db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
        db.flush()
        city = City(USERID=1, CITY_SLUG="test_city", UI_PORT=3001, API_PORT=8001)
        db.add(city)
        db.flush()
        db.add(AI(AIID=PERSONA_ID, HOME_CITYID=city.CITYID, AINAME="Alice"))
        db.commit()
    finally:
        db.close()

    # タスクの親になる Track 行。TrackManager は 2026-08-22 (束 6c) に退役したので、
    # 旧データ相当の ActionTrack 行を ORM で直接置く。索引に出るのはタスク側だけで、
    # この行自体は「親が実在する」以上の意味を持たない。
    track_id = str(uuid.uuid4())
    db = session_factory()
    try:
        db.add(ActionTrack(
            track_id=track_id, persona_id=PERSONA_ID, short_id=1,
            title="言葉の標本集", track_type="autonomous", status="running",
        ))
        db.commit()
    finally:
        db.close()

    ptm = PersonaTaskManager(session_factory)
    ptm.create_task(
        persona_id=PERSONA_ID, title="序文の下書き", goal="書き出しを決める",
        parent_kind=PARENT_TRACK, track_id=track_id, auto_activate=False,
    )
    ptm.create_task(
        persona_id=PERSONA_ID, title="雲の写真を集めたい",
        stage=STAGE_CANDIDATE, auto_activate=False,
    )

    manager = SimpleNamespace(
        SessionLocal=session_factory,
        personas={
            PERSONA_ID: SimpleNamespace(sai_memory=StubMemoryAdapter(conn))
        },
    )
    app = FastAPI()
    app.include_router(route.router, prefix="/api/people")
    app.dependency_overrides[get_manager] = lambda: manager
    return TestClient(app)


class TestPurposeRows:
    def test_purposes_join_the_index_with_tag_stats(self, full_client, conn):
        # task:1 (序文の下書き) に帰属タグ 2 件 (別々の出来事から)
        add_tag(
            conn, target_ref="episode:1", purpose_ref="task:1", layer=LAYER_SHELVE
        )
        add_tag(
            conn, target_ref="episode:2", purpose_ref="task:1", layer=LAYER_SHELVE
        )

        res = full_client.get(f"/api/people/{PERSONA_ID}/experience-ledger")
        assert res.status_code == 200
        purposes = res.json()["purposes"]
        by_ref = {p["ref"]: p for p in purposes}

        assert by_ref["task:1"]["title"] == "序文の下書き"
        assert by_ref["task:1"]["kind"] == "task"
        assert by_ref["task:1"]["stats"]["record_count"] == 2
        assert by_ref["task:1"]["stats"]["first_date"] is not None

        # 欲求候補 (kind='desire') と関心 (kind='track') の索引行は 2026-08-21 に
        # 供給源ごと退役した — 索引に残るのは生きたタスクだけ。
        assert "task:2" not in by_ref
        assert {p["kind"] for p in purposes} == {"task"}
