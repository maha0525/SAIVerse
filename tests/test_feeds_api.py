"""フィード API (api/routes/feeds.py) の HTTP テスト。

docs/intent/rss_feed_intake.md の API 層: プリセット一覧 / フィード施設の
作成・一覧 / 購読の追加 (直フィード・自動発見・候補提示・0件) / 削除 /
手動取得 / 記事一覧 (透明性 UI)。

- ネットワークは fetch_feed / discover_feed の patch で完全に断つ
- DB は隔離した file sqlite (TestClient はワーカースレッドでルートを実行する
  ため :memory: でなく check_same_thread=False の file 共有 —
  test_life_settings_api.py と同じ流儀)
- manager は SimpleNamespace + 実物の ObserverManager / FeedManager
  (どちらも構築のみで背景処理を始めない)
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.deps import get_manager
from database.models import (
    Base,
    Building,
    City,
    FeedItem,
    FeedSubscription,
    Fixture,
    User,
)
from saiverse import feed_presets
from saiverse.feed_fetch import (
    DiscoveredFeed,
    FeedEntry,
    FeedFetchError,
    FeedFetchResult,
)
from saiverse.feed_manager import FeedManager
from saiverse.observer_manager import ObserverManager

BUILDING_ID = "bldg-1"

TEST_PRESETS = {
    "tech_news": {
        "id": "tech_news",
        "name": "技術ニュース",
        "description": "技術系の読み物",
        "fixture_name": "技術ニューススタンド",
        "fixture_description": "技術記事のフィードが届く施設",
        "feeds": [
            {"url": "https://example.com/tech.rss", "title": "Tech Feed"},
            {"url": "https://example.org/dev.atom", "title": "Dev Feed"},
        ],
    },
}


def _feed_result(url: str, *, title: str = "Example Feed", entries=None) -> FeedFetchResult:
    return FeedFetchResult(
        url=url,
        title=title,
        site_url="https://example.com/",
        entries=entries or [],
    )


def _entry(guid: str, *, title: str = "記事", published: datetime | None = None) -> FeedEntry:
    return FeedEntry(
        guid=guid,
        title=title,
        summary=f"{title} の概要",
        link=f"https://example.com/articles/{guid}",
        published=published,
    )


class FeedsApiTestBase(unittest.TestCase):
    """共通 setUp (隔離 DB + 実 City/Building + TestClient)。テストは持たない。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._cleanup_temp)
        db_file = Path(self._tmp.name) / "saiverse_test.db"
        self.engine = create_engine(
            f"sqlite:///{db_file}", connect_args={"check_same_thread": False}
        )
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        # City 所有権境界 (city_feed_fixture_ids) が Building.CITYID を join
        # するため、City / Building は実 DB 行として作る
        db = self.Session()
        try:
            db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
            db.flush()
            city = City(USERID=1, CITYNAME="test_city", UI_PORT=3001, API_PORT=8001)
            db.add(city)
            db.flush()
            db.add(Building(
                CITYID=city.CITYID, BUILDINGID=BUILDING_ID, BUILDINGNAME="図書館",
            ))
            db.commit()
            self.city_id = city.CITYID
        finally:
            db.close()

        self.manager = SimpleNamespace(
            SessionLocal=self.Session,
            buildings=[SimpleNamespace(building_id=BUILDING_ID, name="図書館")],
            city_id=self.city_id,
        )
        self.manager.observer_manager = ObserverManager(self.manager)
        self.manager.feed_manager = FeedManager(self.manager)

        presets_patcher = patch.object(feed_presets, "FEED_PRESETS", TEST_PRESETS)
        presets_patcher.start()
        self.addCleanup(presets_patcher.stop)

        from api.routes import feeds as feeds_route

        app = FastAPI()
        app.include_router(feeds_route.router, prefix="/api/feeds")
        app.dependency_overrides[get_manager] = lambda: self.manager
        self.client = TestClient(app)

    def _cleanup_temp(self):
        import gc
        gc.collect()
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass  # Windows: sqlite ハンドル解放待ちの既知事情

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _create_custom_fixture(self, name: str = "新聞スタンド") -> str:
        resp = self.client.post(
            "/api/feeds/fixtures",
            json={"building_id": BUILDING_ID, "name": name},
        )
        self.assertEqual(resp.status_code, 200)
        return resp.json()["fixture_id"]

    def _subscription_rows(self):
        db = self.Session()
        try:
            return db.query(FeedSubscription).all()
        finally:
            db.close()


class FeedsApiTest(FeedsApiTestBase):
    # ------------------------------------------------------------------
    # GET /presets
    # ------------------------------------------------------------------

    def test_presets_listing(self):
        resp = self.client.get("/api/feeds/presets")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(len(body), 1)
        preset = body[0]
        self.assertEqual(preset["id"], "tech_news")
        self.assertEqual(preset["name"], "技術ニュース")
        self.assertEqual(preset["description"], "技術系の読み物")
        self.assertEqual(preset["feed_count"], 2)
        self.assertEqual(preset["feed_titles"], ["Tech Feed", "Dev Feed"])

    # ------------------------------------------------------------------
    # POST /fixtures
    # ------------------------------------------------------------------

    def test_create_fixture_from_preset(self):
        resp = self.client.post(
            "/api/feeds/fixtures",
            json={"building_id": BUILDING_ID, "preset_id": "tech_news"},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["name"], "技術ニューススタンド")
        self.assertEqual(body["building_id"], BUILDING_ID)

        list_resp = self.client.get("/api/feeds/fixtures")
        self.assertEqual(list_resp.status_code, 200)
        fixtures = list_resp.json()
        self.assertEqual(len(fixtures), 1)
        fixture = fixtures[0]
        self.assertEqual(fixture["fixture_id"], body["fixture_id"])
        self.assertEqual(fixture["building_name"], "図書館")
        self.assertEqual(len(fixture["subscriptions"]), 2)
        urls = {s["feed_url"] for s in fixture["subscriptions"]}
        self.assertEqual(
            urls, {"https://example.com/tech.rss", "https://example.org/dev.atom"}
        )
        # 健康状態のフィールドが隠されず返る
        for sub in fixture["subscriptions"]:
            self.assertIn("last_ok_at", sub)
            self.assertIn("last_error", sub)
            self.assertEqual(sub["consecutive_failures"], 0)

    def test_create_fixture_custom(self):
        fixture_id = self._create_custom_fixture("科学の掲示板")
        fixtures = self.client.get("/api/feeds/fixtures").json()
        self.assertEqual(len(fixtures), 1)
        self.assertEqual(fixtures[0]["fixture_id"], fixture_id)
        self.assertEqual(fixtures[0]["name"], "科学の掲示板")
        self.assertEqual(fixtures[0]["subscriptions"], [])

    def test_create_fixture_unknown_building_404(self):
        resp = self.client.post(
            "/api/feeds/fixtures",
            json={"building_id": "nowhere", "preset_id": "tech_news"},
        )
        self.assertEqual(resp.status_code, 404)

    def test_create_fixture_unknown_preset_404(self):
        resp = self.client.post(
            "/api/feeds/fixtures",
            json={"building_id": BUILDING_ID, "preset_id": "no_such_preset"},
        )
        self.assertEqual(resp.status_code, 404)

    def test_create_fixture_without_preset_or_name_422(self):
        resp = self.client.post(
            "/api/feeds/fixtures", json={"building_id": BUILDING_ID}
        )
        self.assertEqual(resp.status_code, 422)

    def test_create_fixture_from_preset_invalid_url_422_no_partial_state(self):
        """J3: プリセット内に 1 本でも不正 URL があれば 422 で全体を拒否 —
        全か無か (feed_manager 側の単一 transaction) で、Fixture も購読も
        作られない。"""
        bad = {
            "bad_preset": {
                "id": "bad_preset",
                "name": "壊れたプリセット",
                "feeds": [
                    {"url": "https://example.com/ok.rss", "title": "OK"},
                    {"url": "ftp://example.com/feed", "title": "不正 scheme"},
                ],
            },
        }
        with patch.object(feed_presets, "FEED_PRESETS", bad):
            resp = self.client.post(
                "/api/feeds/fixtures",
                json={"building_id": BUILDING_ID, "preset_id": "bad_preset"},
            )
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(self._subscription_rows(), [])
        db = self.Session()
        try:
            self.assertEqual(db.query(Fixture).count(), 0)
        finally:
            db.close()

    def test_create_fixture_from_preset_display_failure_still_succeeds(self):
        """K1: 本体 (施設 + 購読) の commit 成功後に表示更新が失敗しても
        500 にしない — 表示は次の取得サイクルで自己修復する派生物。"""
        with patch.object(
            FeedManager, "update_fixture_display",
            side_effect=RuntimeError("display boom"),
        ):
            resp = self.client.post(
                "/api/feeds/fixtures",
                json={"building_id": BUILDING_ID, "preset_id": "tech_news"},
            )
        self.assertEqual(resp.status_code, 200)
        # 施設・購読は残っている
        self.assertEqual(len(self._subscription_rows()), 2)
        db = self.Session()
        try:
            self.assertEqual(db.query(Fixture).count(), 1)
        finally:
            db.close()

    def test_create_fixture_from_preset_duplicate_urls_collapse_to_one(self):
        """K2: プリセット内の正規化後重複 URL は事前排除され、購読は最初の
        1 本だけ作られる (重複 INSERT の一意制約違反 = 500 にならない)。"""
        dup = {
            "dup_preset": {
                "id": "dup_preset",
                "name": "重複プリセット",
                "feeds": [
                    {"url": "https://example.com/tech.rss", "title": "A"},
                    # 表記ゆれ (大文字ホスト + 末尾スラッシュ) — 正規化後は同一
                    {"url": "https://EXAMPLE.com/tech.rss/", "title": "B"},
                ],
            },
        }
        with patch.object(feed_presets, "FEED_PRESETS", dup):
            resp = self.client.post(
                "/api/feeds/fixtures",
                json={"building_id": BUILDING_ID, "preset_id": "dup_preset"},
            )
        self.assertEqual(resp.status_code, 200)
        rows = self._subscription_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].TITLE, "A")  # 最初の 1 本を採用

    # ------------------------------------------------------------------
    # POST /subscriptions — 直フィード
    # ------------------------------------------------------------------

    def test_add_subscription_direct_feed(self):
        fixture_id = self._create_custom_fixture()
        url = "https://example.com/feed.rss"
        with patch(
            "saiverse.feed_fetch.fetch_feed",
            return_value=_feed_result(url, title="Example Feed"),
        ):
            resp = self.client.post(
                "/api/feeds/subscriptions",
                json={"fixture_id": fixture_id, "url": url},
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "subscribed")
        # タイトル未指定 → フィード自身の宣言タイトルが転載される
        self.assertEqual(body["subscription"]["title"], "Example Feed")
        self.assertEqual(body["subscription"]["feed_url"], url)

        rows = self._subscription_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].FEED_URL, url)
        self.assertEqual(rows[0].FIXTURE_ID, fixture_id)

    def test_add_subscription_shares_one_deadline_across_stages(self):
        """J6: 購読追加はリクエスト入口で 1 つの時間予算 (deadline) を作り、
        fetch → discover → fetch の全段に同じ値を渡す — 各段 60 秒 × 3 で
        最大約 180 秒 worker を占有しない。"""
        fixture_id = self._create_custom_fixture()
        deadlines = []

        def fake_fetch(url, **kwargs):
            deadlines.append(kwargs.get("deadline"))
            if len(deadlines) == 1:
                raise FeedFetchError("フィードではない", kind="not_a_feed")
            return _feed_result(url)

        def fake_discover(url, **kwargs):
            deadlines.append(kwargs.get("deadline"))
            return [DiscoveredFeed(url="https://example.com/feed.xml", title="t")]

        with patch("saiverse.feed_fetch.fetch_feed", side_effect=fake_fetch), \
             patch("saiverse.feed_fetch.discover_feed", side_effect=fake_discover):
            resp = self.client.post(
                "/api/feeds/subscriptions",
                json={"fixture_id": fixture_id, "url": "https://example.com/"},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(deadlines), 3)
        self.assertTrue(all(d is not None for d in deadlines))
        self.assertEqual(len(set(deadlines)), 1)  # 全段が同じ予算を共有

    def test_add_subscription_invalid_ipv6_url_422(self):
        """I4: 不正な IPv6 ブラケットの URL は 500 でなく 422 (invalid_url)。
        実物の fetch_feed が URL 整形段階で拒否するため、ネットワークには
        触れない (patch 不要)。"""
        fixture_id = self._create_custom_fixture()
        resp = self.client.post(
            "/api/feeds/subscriptions",
            json={"fixture_id": fixture_id, "url": "https://[::1/feed"},
        )
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(self._subscription_rows(), [])

    def test_add_subscription_ftp_scheme_422(self):
        """I4: ftp:// は https:// 前置で再構成せず 422 (forbidden_url)。"""
        fixture_id = self._create_custom_fixture()
        resp = self.client.post(
            "/api/feeds/subscriptions",
            json={"fixture_id": fixture_id, "url": "ftp://example.com/feed"},
        )
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(self._subscription_rows(), [])

    def test_add_subscription_redirect_to_malformed_location_422(self):
        """O1 (十巡目): リダイレクト先 (Location) が urlparse 不能な URL でも
        500 にしない — feed_fetch が invalid_url に写像して 422 で返る。
        requests 層だけ偽装し、リダイレクト解決の実経路を通す。"""
        import ipaddress

        from saiverse import feed_fetch as feed_fetch_module

        fixture_id = self._create_custom_fixture()

        class _RedirectResp:
            status_code = 301
            headers = {"Location": "http://[bad"}
            url = ""

            def iter_content(self, chunk_size):
                return iter(())

            def close(self):
                pass

        with patch.object(
            feed_fetch_module, "_resolve_host_addresses",
            return_value=[ipaddress.ip_address("93.184.216.34")],
        ), patch.object(
            feed_fetch_module.requests, "get", return_value=_RedirectResp(),
        ):
            resp = self.client.post(
                "/api/feeds/subscriptions",
                json={"fixture_id": fixture_id, "url": "https://example.com/feed.xml"},
            )
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(self._subscription_rows(), [])

    def test_add_subscription_overlong_url_422_without_fetch(self):
        """O4 (十巡目): DB 宣言長 (String(512)) を超える URL は入口で 422 —
        外部取得には出ない (fetch_feed が呼ばれたら AssertionError で落ちる)。"""
        fixture_id = self._create_custom_fixture()
        overlong = "https://example.com/" + "a" * 493  # 全体で 513 文字
        self.assertEqual(len(overlong), 513)
        with patch(
            "saiverse.feed_fetch.fetch_feed",
            side_effect=AssertionError("fetch_feed must not be called"),
        ):
            resp = self.client.post(
                "/api/feeds/subscriptions",
                json={"fixture_id": fixture_id, "url": overlong},
            )
        self.assertEqual(resp.status_code, 422)
        self.assertIn("長すぎます", resp.json()["detail"])
        self.assertEqual(self._subscription_rows(), [])

    def test_add_subscription_huge_channel_link_site_url_dropped(self):
        """O4 (十巡目): フィード自身が宣言する channel link (外部由来の
        site_url) が 512 字超なら、購読は成立させたまま site_url だけ破棄する。"""
        fixture_id = self._create_custom_fixture()
        url = "https://example.com/feed.rss"
        result = FeedFetchResult(
            url=url,
            title="Example Feed",
            site_url="https://example.com/" + "x" * 600,
        )
        with patch("saiverse.feed_fetch.fetch_feed", return_value=result):
            resp = self.client.post(
                "/api/feeds/subscriptions",
                json={"fixture_id": fixture_id, "url": url},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()["subscription"]["site_url"])
        rows = self._subscription_rows()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].SITE_URL)

    def test_add_subscription_same_url_repost_returns_existing(self):
        """F7: 同一 URL の再 POST は新規購読を作らず既存を返す (get-or-create)。"""
        fixture_id = self._create_custom_fixture()
        url = "https://example.com/feed.rss"
        with patch(
            "saiverse.feed_fetch.fetch_feed",
            return_value=_feed_result(url, title="Example Feed"),
        ):
            first = self.client.post(
                "/api/feeds/subscriptions",
                json={"fixture_id": fixture_id, "url": url},
            )
            # 表記ゆれ (大文字ホスト・末尾スラッシュ) も同一購読に正規化される
            second = self.client.post(
                "/api/feeds/subscriptions",
                json={"fixture_id": fixture_id, "url": "https://EXAMPLE.com/feed.rss/"},
            )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(
            first.json()["subscription"]["subscription_id"],
            second.json()["subscription"]["subscription_id"],
        )
        self.assertEqual(len(self._subscription_rows()), 1)

    def test_add_subscription_limit_reached_422(self):
        """U3 (十五巡目): 施設の購読数が上限 (SAIVERSE_FEED_MAX_SUBSCRIPTIONS_
        PER_FIXTURE) に達していたら、新規 URL の購読追加は 422。"""
        fixture_id = self._create_custom_fixture()
        with patch.dict("os.environ", {
            "SAIVERSE_FEED_MAX_SUBSCRIPTIONS_PER_FIXTURE": "1",
        }), patch(
            "saiverse.feed_fetch.fetch_feed",
            side_effect=lambda url, **kw: _feed_result(url),
        ):
            first = self.client.post(
                "/api/feeds/subscriptions",
                json={"fixture_id": fixture_id, "url": "https://example.com/a.rss"},
            )
            second = self.client.post(
                "/api/feeds/subscriptions",
                json={"fixture_id": fixture_id, "url": "https://example.com/b.rss"},
            )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 422)
        self.assertIn("上限", second.json()["detail"])
        self.assertEqual(len(self._subscription_rows()), 1)

    def test_add_subscription_existing_url_repost_succeeds_at_limit(self):
        """U3: 上限到達後でも既存 URL の再 POST は get-or-create で既存購読を
        返して成功する (上限は新規作成時のみ数える)。"""
        fixture_id = self._create_custom_fixture()
        url = "https://example.com/a.rss"
        with patch.dict("os.environ", {
            "SAIVERSE_FEED_MAX_SUBSCRIPTIONS_PER_FIXTURE": "1",
        }), patch(
            "saiverse.feed_fetch.fetch_feed",
            side_effect=lambda u, **kw: _feed_result(u),
        ):
            first = self.client.post(
                "/api/feeds/subscriptions",
                json={"fixture_id": fixture_id, "url": url},
            )
            second = self.client.post(
                "/api/feeds/subscriptions",
                json={"fixture_id": fixture_id, "url": url},
            )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["status"], "subscribed")
        self.assertEqual(
            first.json()["subscription"]["subscription_id"],
            second.json()["subscription"]["subscription_id"],
        )
        self.assertEqual(len(self._subscription_rows()), 1)

    def test_create_fixture_from_preset_exceeding_limit_422(self):
        """U3: プリセットの購読数 (tech_news = 2 本) が上限 (1) を超えるなら
        422 で全か無か — 施設も購読も作られない。"""
        with patch.dict("os.environ", {
            "SAIVERSE_FEED_MAX_SUBSCRIPTIONS_PER_FIXTURE": "1",
        }):
            resp = self.client.post(
                "/api/feeds/fixtures",
                json={"building_id": BUILDING_ID, "preset_id": "tech_news"},
            )
        self.assertEqual(resp.status_code, 422)
        self.assertIn("上限", resp.json()["detail"])
        db = self.Session()
        try:
            self.assertEqual(db.query(Fixture).count(), 0)
        finally:
            db.close()
        self.assertEqual(self._subscription_rows(), [])

    def test_add_subscription_uppercase_scheme_url(self):
        """H3 回帰 (API 経由): 大文字スキーム URL (HTTPS://) でも購読追加が通る。
        fetch_feed は patch せず requests 層だけ偽装し、_normalize_url の実経路
        (scheme だけ小文字化) を通す。ホストの小文字化は保存側の正規化
        (_normalize_feed_url) の仕事。"""
        import ipaddress

        from saiverse import feed_fetch as feed_fetch_module

        fixture_id = self._create_custom_fixture()
        rss = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<rss version="2.0"><channel><title>Upper Feed</title>'
            "<item><title>A</title><guid>u-1</guid></item></channel></rss>"
        ).encode("utf-8")
        captured = {}

        class _FakeResp:
            status_code = 200
            headers = {"Content-Type": "application/rss+xml"}
            url = ""

            def iter_content(self, chunk_size):
                yield rss

            def close(self):
                pass

        def fake_get(url, **kw):
            captured["url"] = url
            return _FakeResp()

        with patch.object(
            feed_fetch_module, "_resolve_host_addresses",
            return_value=[ipaddress.ip_address("93.184.216.34")],
        ), patch.object(feed_fetch_module.requests, "get", side_effect=fake_get):
            resp = self.client.post(
                "/api/feeds/subscriptions",
                json={
                    "fixture_id": fixture_id,
                    "url": "HTTPS://EXAMPLE.COM/feed.xml",
                },
            )
        self.assertEqual(resp.status_code, 200)
        # 取得は scheme だけ小文字化した URL で走る (https://HTTPS://... にしない)
        self.assertEqual(captured["url"], "https://EXAMPLE.COM/feed.xml")
        # 保存はホストも小文字化した正規形
        self.assertEqual(
            resp.json()["subscription"]["feed_url"],
            "https://example.com/feed.xml",
        )

    def test_add_subscription_to_non_feed_fixture_404(self):
        """F8: feed_stand 以外の Fixture への購読追加は 404 (fetch も走らない)。"""
        db = self.Session()
        try:
            db.add(Fixture(
                FIXTURE_ID="obj-1", BUILDING_ID=BUILDING_ID,
                NAME="ただの置物", TYPE="object",
            ))
            db.commit()
        finally:
            db.close()
        with patch(
            "saiverse.feed_fetch.fetch_feed",
            side_effect=AssertionError("fetch_feed must not be called"),
        ):
            resp = self.client.post(
                "/api/feeds/subscriptions",
                json={"fixture_id": "obj-1", "url": "https://example.com/feed"},
            )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self._subscription_rows(), [])

    def test_add_subscription_unknown_fixture_404_without_fetch(self):
        # fixture の存在確認が fetch より先 — 呼ばれたら AssertionError で落ちる
        with patch(
            "saiverse.feed_fetch.fetch_feed",
            side_effect=AssertionError("fetch_feed must not be called"),
        ):
            resp = self.client.post(
                "/api/feeds/subscriptions",
                json={"fixture_id": "no-such-fixture", "url": "https://example.com/"},
            )
        self.assertEqual(resp.status_code, 404)

    # ------------------------------------------------------------------
    # POST /subscriptions — 自動発見
    # ------------------------------------------------------------------

    def test_add_subscription_autodiscovery_single_candidate(self):
        fixture_id = self._create_custom_fixture()
        site_url = "https://example.com/blog"
        feed_url = "https://example.com/blog/feed.xml"

        def fake_fetch(url, **kwargs):
            if url == site_url:
                raise FeedFetchError("not a feed", kind="not_a_feed")
            return _feed_result(url, title="Blog Feed")

        with patch("saiverse.feed_fetch.fetch_feed", side_effect=fake_fetch), patch(
            "saiverse.feed_fetch.discover_feed",
            return_value=[DiscoveredFeed(url=feed_url, title="Blog Feed")],
        ):
            resp = self.client.post(
                "/api/feeds/subscriptions",
                json={"fixture_id": fixture_id, "url": site_url},
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "subscribed")
        self.assertEqual(body["subscription"]["feed_url"], feed_url)
        # 元のサイト URL は site_url として残る
        self.assertEqual(body["subscription"]["site_url"], site_url)

    def test_add_subscription_multiple_candidates_returned_for_choice(self):
        fixture_id = self._create_custom_fixture()
        cands = [
            DiscoveredFeed(url="https://example.com/rss", title="全記事"),
            DiscoveredFeed(url="https://example.com/news.rss", title="ニュースのみ"),
        ]
        with patch(
            "saiverse.feed_fetch.fetch_feed",
            side_effect=FeedFetchError("not a feed", kind="not_a_feed"),
        ), patch("saiverse.feed_fetch.discover_feed", return_value=cands):
            resp = self.client.post(
                "/api/feeds/subscriptions",
                json={"fixture_id": fixture_id, "url": "https://example.com/"},
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "candidates")
        self.assertEqual(
            [c["url"] for c in body["candidates"]],
            ["https://example.com/rss", "https://example.com/news.rss"],
        )
        # 候補提示の段階では購読は作られない
        self.assertEqual(self._subscription_rows(), [])

    def test_add_subscription_no_candidates_422(self):
        fixture_id = self._create_custom_fixture()
        with patch(
            "saiverse.feed_fetch.fetch_feed",
            side_effect=FeedFetchError("not a feed", kind="not_a_feed"),
        ), patch("saiverse.feed_fetch.discover_feed", return_value=[]):
            resp = self.client.post(
                "/api/feeds/subscriptions",
                json={"fixture_id": fixture_id, "url": "https://example.com/"},
            )
        self.assertEqual(resp.status_code, 422)
        self.assertIn("フィード", resp.json()["detail"])
        self.assertEqual(self._subscription_rows(), [])

    def test_add_subscription_network_error_502(self):
        fixture_id = self._create_custom_fixture()
        with patch(
            "saiverse.feed_fetch.fetch_feed",
            side_effect=FeedFetchError("接続できません", kind="network"),
        ):
            resp = self.client.post(
                "/api/feeds/subscriptions",
                json={"fixture_id": fixture_id, "url": "https://example.com/feed"},
            )
        self.assertEqual(resp.status_code, 502)

    def test_add_subscription_discovery_timeout_502(self):
        """K3: 自動発見中の時間予算切れ (kind="timeout") は「候補なし」の
        422 (= フィード未提供の誤診) でなく 502 で表面化する。discover_feed が
        timeout を候補なしへ畳まない側は test_feed_intake の K3 テストが持つ。"""
        fixture_id = self._create_custom_fixture()
        with patch(
            "saiverse.feed_fetch.fetch_feed",
            side_effect=FeedFetchError("フィードではない", kind="not_a_feed"),
        ), patch(
            "saiverse.feed_fetch.discover_feed",
            side_effect=FeedFetchError(
                "タイムアウト: 取得全体の時間予算を使い切りました。",
                kind="timeout",
            ),
        ):
            resp = self.client.post(
                "/api/feeds/subscriptions",
                json={"fixture_id": fixture_id, "url": "https://example.com/"},
            )
        self.assertEqual(resp.status_code, 502)

    def test_add_subscription_display_failure_still_succeeds(self):
        """K1: 購読の commit 成功後に表示更新が失敗しても 500 にしない。"""
        fixture_id = self._create_custom_fixture()
        url = "https://example.com/feed.rss"
        with patch(
            "saiverse.feed_fetch.fetch_feed", return_value=_feed_result(url),
        ), patch.object(
            FeedManager, "update_fixture_display",
            side_effect=RuntimeError("display boom"),
        ):
            resp = self.client.post(
                "/api/feeds/subscriptions",
                json={"fixture_id": fixture_id, "url": url},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(self._subscription_rows()), 1)

    def test_add_subscription_concurrency_limit_429(self):
        """L3: 購読追加のネットワーク区間は同時 2 本まで — 枠が塞がっている
        間の POST は待たせず 429 で即返し、fetch にも DB にも進まない。"""
        from api.routes import feeds as feeds_route
        fixture_id = self._create_custom_fixture()
        # 枠 (BoundedSemaphore(2)) を人工的に使い切る
        self.assertTrue(feeds_route._SUBSCRIBE_CONCURRENCY.acquire(blocking=False))
        self.assertTrue(feeds_route._SUBSCRIBE_CONCURRENCY.acquire(blocking=False))
        try:
            with patch(
                "saiverse.feed_fetch.fetch_feed",
                side_effect=AssertionError("枠が無いのに fetch が走った"),
            ):
                resp = self.client.post(
                    "/api/feeds/subscriptions",
                    json={
                        "fixture_id": fixture_id,
                        "url": "https://example.com/feed.rss",
                    },
                )
        finally:
            feeds_route._SUBSCRIBE_CONCURRENCY.release()
            feeds_route._SUBSCRIBE_CONCURRENCY.release()
        self.assertEqual(resp.status_code, 429)
        self.assertIn("混み合って", resp.json()["detail"])
        self.assertEqual(self._subscription_rows(), [])

        # 枠が空けば通常どおり通る (429 が居座らない)
        url = "https://example.com/feed.rss"
        with patch(
            "saiverse.feed_fetch.fetch_feed", return_value=_feed_result(url),
        ):
            resp = self.client.post(
                "/api/feeds/subscriptions",
                json={"fixture_id": fixture_id, "url": url},
            )
        self.assertEqual(resp.status_code, 200)

    # ------------------------------------------------------------------
    # DELETE /subscriptions/{id}
    # ------------------------------------------------------------------

    def test_remove_subscription(self):
        fixture_id = self._create_custom_fixture()
        url = "https://example.com/feed.rss"
        with patch(
            "saiverse.feed_fetch.fetch_feed", return_value=_feed_result(url)
        ):
            sub_id = self.client.post(
                "/api/feeds/subscriptions",
                json={"fixture_id": fixture_id, "url": url},
            ).json()["subscription"]["subscription_id"]

        resp = self.client.delete(f"/api/feeds/subscriptions/{sub_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._subscription_rows(), [])

    def test_remove_subscription_not_found_404(self):
        resp = self.client.delete("/api/feeds/subscriptions/no-such-id")
        self.assertEqual(resp.status_code, 404)

    def test_remove_subscription_display_failure_still_succeeds(self):
        """K1: 削除の commit 成功後に表示更新が失敗しても 500 にしない。"""
        fixture_id = self._create_custom_fixture()
        url = "https://example.com/feed.rss"
        with patch(
            "saiverse.feed_fetch.fetch_feed", return_value=_feed_result(url)
        ):
            sub_id = self.client.post(
                "/api/feeds/subscriptions",
                json={"fixture_id": fixture_id, "url": url},
            ).json()["subscription"]["subscription_id"]

        with patch.object(
            FeedManager, "update_fixture_display",
            side_effect=RuntimeError("display boom"),
        ):
            resp = self.client.delete(f"/api/feeds/subscriptions/{sub_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._subscription_rows(), [])  # 削除は成立している

    # ------------------------------------------------------------------
    # POST /fetch — 手動の全取得 (実 FeedManager の取得サイクルを回す)
    # ------------------------------------------------------------------

    def test_fetch_endpoint_runs_fetch_cycle(self):
        fixture_id = self._create_custom_fixture()
        url = "https://example.com/feed.rss"
        with patch(
            "saiverse.feed_fetch.fetch_feed", return_value=_feed_result(url)
        ):
            self.client.post(
                "/api/feeds/subscriptions",
                json={"fixture_id": fixture_id, "url": url},
            )

        # fetch_now が返す worker スレッドを捕まえて完了を待てるようにする
        threads = []
        orig_fetch_now = self.manager.feed_manager.fetch_now

        def capturing_fetch_now():
            thread = orig_fetch_now()
            threads.append(thread)
            return thread

        self.manager.feed_manager.fetch_now = capturing_fetch_now

        entries = [
            _entry("guid-1", title="記事その1",
                   published=datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)),
            _entry("guid-2", title="記事その2"),
        ]
        # 取得サイクル側の fetch_feed は feed_manager モジュールに束縛されている
        with patch(
            "saiverse.feed_manager.fetch_feed",
            return_value=_feed_result(url, entries=entries),
        ):
            resp = self.client.post("/api/feeds/fetch")
            self.assertEqual(resp.status_code, 202)
            for thread in threads:
                thread.join(timeout=10)

        db = self.Session()
        try:
            items = db.query(FeedItem).all()
            self.assertEqual({i.GUID for i in items}, {"guid-1", "guid-2"})
            # 施設の表示 (STATE_JSON) にも最新記事タイトルが載る
            fixture = db.query(Fixture).filter(
                Fixture.FIXTURE_ID == fixture_id
            ).first()
            self.assertIn("記事その1", fixture.STATE_JSON or "")
        finally:
            db.close()

    def test_fetch_endpoint_busy_409(self):
        """F9: 取得サイクル実行中の POST /fetch は 409。"""
        fm = self.manager.feed_manager
        self.assertTrue(fm._fetch_lock.acquire(blocking=False))
        try:
            resp = self.client.post("/api/feeds/fetch")
        finally:
            fm._fetch_lock.release()
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["detail"], "取得処理が既に実行中です")

    # ------------------------------------------------------------------
    # GET /items — 記事一覧 (透明性 UI)
    # ------------------------------------------------------------------

    def _seed_items(self, fixture_id: str) -> str:
        """購読 1 本と記事 3 件を直接 DB に入れる。"""
        db = self.Session()
        try:
            sub = FeedSubscription(
                SUBSCRIPTION_ID="sub-1",
                FIXTURE_ID=fixture_id,
                FEED_URL="https://example.com/feed.rss",
                TITLE="Example Feed",
            )
            db.add(sub)
            db.add(FeedItem(
                SUBSCRIPTION_ID="sub-1", GUID="old", TITLE="古い記事",
                SUMMARY="昔の話", LINK="https://example.com/old",
                PUBLISHED_AT=datetime(2026, 7, 1, 0, 0),
            ))
            db.add(FeedItem(
                SUBSCRIPTION_ID="sub-1", GUID="new", TITLE="新しい記事",
                SUMMARY="今日の話", LINK="https://example.com/new",
                PUBLISHED_AT=datetime(2026, 8, 1, 0, 0),
            ))
            db.add(FeedItem(
                SUBSCRIPTION_ID="sub-1", GUID="undated", TITLE="日付なしの記事",
                SUMMARY="", LINK="https://example.com/undated",
                PUBLISHED_AT=None,
            ))
            db.commit()
            return "sub-1"
        finally:
            db.close()

    def test_items_listing_newest_first(self):
        fixture_id = self._create_custom_fixture()
        self._seed_items(fixture_id)

        resp = self.client.get(f"/api/feeds/items?fixture_id={fixture_id}")
        self.assertEqual(resp.status_code, 200)
        items = resp.json()["items"]
        self.assertEqual(len(items), 3)
        # PUBLISHED_AT desc (SQLite では NULL は最後)
        self.assertEqual(
            [i["title"] for i in items], ["新しい記事", "古い記事", "日付なしの記事"]
        )
        self.assertEqual(items[0]["subscription_title"], "Example Feed")
        self.assertEqual(items[0]["link"], "https://example.com/new")
        # naive UTC が UTC 明示で返る (フロントの時刻誤解釈防止)
        self.assertTrue(items[0]["published_at"].endswith("+00:00"))
        self.assertIsNone(items[2]["published_at"])

    def test_items_limit(self):
        fixture_id = self._create_custom_fixture()
        self._seed_items(fixture_id)
        resp = self.client.get(f"/api/feeds/items?fixture_id={fixture_id}&limit=1")
        self.assertEqual(resp.status_code, 200)
        items = resp.json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "新しい記事")

    def test_items_unknown_fixture_404(self):
        resp = self.client.get("/api/feeds/items?fixture_id=no-such-fixture")
        self.assertEqual(resp.status_code, 404)


class FeedsApiCityIsolationTest(FeedsApiTestBase):
    """Q2 (十二巡目): City 所有権境界の API 側 — 複数 City の DB で、City A
    の API から City B のフィード施設・購読が見えない・操作できない。存在は
    404 で漏らさない。multi-city は凍結中だが、過去データで複数 City を持つ
    DB は実在しうるための境界。"""

    OTHER_BUILDING = "bldg-other"
    OTHER_FIXTURE = "fx-other"
    OTHER_SUB = "sub-other"

    def setUp(self):
        super().setUp()
        # 別 City の Building / フィード施設 / 購読 / 記事を直接 DB に用意する
        db = self.Session()
        try:
            other_city = City(
                USERID=1, CITYNAME="other_city", UI_PORT=3002, API_PORT=8002,
            )
            db.add(other_city)
            db.flush()
            db.add(Building(
                CITYID=other_city.CITYID, BUILDINGID=self.OTHER_BUILDING,
                BUILDINGNAME="他所",
            ))
            db.add(Fixture(
                FIXTURE_ID=self.OTHER_FIXTURE, BUILDING_ID=self.OTHER_BUILDING,
                NAME="別Cityスタンド", TYPE="feed_stand",
            ))
            db.add(FeedSubscription(
                SUBSCRIPTION_ID=self.OTHER_SUB,
                FIXTURE_ID=self.OTHER_FIXTURE,
                FEED_URL="https://other-city.example.com/feed.xml",
                TITLE="別Cityフィード",
            ))
            db.add(FeedItem(
                SUBSCRIPTION_ID=self.OTHER_SUB, GUID="og-1", TITLE="別City記事",
            ))
            db.commit()
        finally:
            db.close()

    def test_fixtures_listing_excludes_other_city(self):
        own = self._create_custom_fixture()
        fixtures = self.client.get("/api/feeds/fixtures").json()
        self.assertEqual([f["fixture_id"] for f in fixtures], [own])

    def test_items_of_other_city_fixture_404(self):
        resp = self.client.get(
            f"/api/feeds/items?fixture_id={self.OTHER_FIXTURE}"
        )
        self.assertEqual(resp.status_code, 404)

    def test_add_subscription_to_other_city_fixture_404_without_fetch(self):
        with patch(
            "saiverse.feed_fetch.fetch_feed",
            side_effect=AssertionError("fetch_feed must not be called"),
        ):
            resp = self.client.post(
                "/api/feeds/subscriptions",
                json={
                    "fixture_id": self.OTHER_FIXTURE,
                    "url": "https://example.com/feed.rss",
                },
            )
        self.assertEqual(resp.status_code, 404)
        # 別 City の購読は増えていない (元の 1 本のまま)
        rows = self._subscription_rows()
        self.assertEqual([r.SUBSCRIPTION_ID for r in rows], [self.OTHER_SUB])

    def test_remove_other_city_subscription_404_and_row_survives(self):
        resp = self.client.delete(f"/api/feeds/subscriptions/{self.OTHER_SUB}")
        self.assertEqual(resp.status_code, 404)
        rows = self._subscription_rows()
        self.assertEqual([r.SUBSCRIPTION_ID for r in rows], [self.OTHER_SUB])

    def test_create_fixture_in_other_city_building_404(self):
        resp = self.client.post(
            "/api/feeds/fixtures",
            json={"building_id": self.OTHER_BUILDING, "name": "越境スタンド"},
        )
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
