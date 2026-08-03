"""RSS フィード取り込みバックエンド核のテスト (docs/intent/rss_feed_intake.md)。

対象:
- saiverse/feed_fetch.py: fetch_feed / discover_feed の正規化・条件付き GET・例外種別
- saiverse/feed_manager.py: 保存の重複防止・失敗記録・カーソル配送・STATE_JSON 衛生
- saiverse/feed_presets.py: 三層ローダ

実ネットワークなし: フィード XML / HTML は文字列 fixture、requests は monkeypatch。
DB は in-memory SQLite、SAIMemory adapter は tmp ディレクトリ + DummyEmbedder。
"""
from __future__ import annotations

import ipaddress
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import (
    AI,
    Base,
    Building,
    City,
    FeedItem,
    FeedReadCursor,
    FeedSubscription,
    User,
)
from saiverse import feed_fetch
from saiverse.feed_fetch import FeedFetchError, discover_feed, fetch_feed
from saiverse.feed_manager import FeedManager
from saiverse.observer_manager import ObserverManager

BUILDING_ID = "b1"

# SSRF ガードの DNS 解決を差し替えるための公開アドレス (テストはネットワーク断ち)
PUBLIC_ADDR = ipaddress.ip_address("93.184.216.34")


def _patch_public_dns(testcase: unittest.TestCase) -> None:
    """_resolve_host_addresses を公開 IP 固定にする (実 DNS を引かない)。"""
    patcher = patch.object(
        feed_fetch, "_resolve_host_addresses", return_value=[PUBLIC_ADDR],
    )
    testcase.addCleanup(patcher.stop)
    patcher.start()


# ---------------------------------------------------------------------------
# フィード / HTML fixture
# ---------------------------------------------------------------------------

RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>テストフィード</title>
<link>https://example.com/</link>
<item>
  <title>記事1</title>
  <link>https://example.com/a1</link>
  <guid isPermaLink="false">guid-1</guid>
  <description>&lt;p&gt;概要&lt;b&gt;テキスト&lt;/b&gt;&lt;/p&gt;</description>
  <pubDate>Wed, 01 Jul 2026 09:00:00 GMT</pubDate>
</item>
<item>
  <title>guid なし記事</title>
  <link>https://example.com/a2</link>
  <pubDate>Wed, 01 Jul 2026 10:00:00 GMT</pubDate>
</item>
<item>
  <title>guid もリンクも無い記事</title>
  <pubDate>Wed, 01 Jul 2026 11:00:00 GMT</pubDate>
</item>
</channel></rss>
"""

RSS_XML_TWO_ITEMS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>テストフィード</title>
<link>https://example.com/</link>
<item><title>記事A</title><link>https://example.com/a</link><guid>g-a</guid>
  <description>Aの概要</description>
  <pubDate>Wed, 01 Jul 2026 09:00:00 GMT</pubDate></item>
<item><title>記事B</title><link>https://example.com/b</link><guid>g-b</guid>
  <description>Bの概要</description>
  <pubDate>Wed, 01 Jul 2026 10:00:00 GMT</pubDate></item>
</channel></rss>
"""

ATOM_XML = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atomフィード</title>
  <link href="https://example.org/"/>
  <entry>
    <title>Atom記事</title>
    <id>tag:example.org,2026:1</id>
    <link href="https://example.org/e1"/>
    <summary>Atomの要約テキスト</summary>
    <updated>2026-07-01T09:00:00Z</updated>
  </entry>
</feed>
"""

HTML_WITH_AUTODISCOVERY = """<!doctype html>
<html><head>
<title>サイト</title>
<link rel="alternate" type="application/rss+xml" title="RSS" href="/feed.xml">
<link rel="alternate" type="application/atom+xml" title="Atom"
      href="https://feeds.example.net/site.atom">
<link rel="stylesheet" href="/style.css">
</head><body>hello</body></html>
"""

HTML_WITHOUT_FEED = "<!doctype html><html><head><title>素のページ</title></head><body>no feed</body></html>"


class FakeResponse:
    def __init__(
        self,
        *,
        status_code=200,
        content=b"",
        headers=None,
        url="",
    ):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}
        self.url = url

    @property
    def text(self):
        return self.content.decode("utf-8", errors="replace")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i + chunk_size]

    def close(self):
        pass


def _rss_response(xml=RSS_XML, **kwargs):
    headers = {"Content-Type": "application/rss+xml; charset=utf-8"}
    headers.update(kwargs.pop("headers", {}))
    return FakeResponse(content=xml.encode("utf-8"), headers=headers, **kwargs)


# ---------------------------------------------------------------------------
# fetch_feed
# ---------------------------------------------------------------------------

class FetchFeedTest(unittest.TestCase):
    def setUp(self):
        _patch_public_dns(self)

    def _patch_get(self, func):
        patcher = patch.object(feed_fetch.requests, "get", side_effect=func)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def test_rss_normalization(self):
        self._patch_get(lambda url, **kw: _rss_response(
            headers={"ETag": '"e1"', "Last-Modified": "Wed, 01 Jul 2026 09:00:00 GMT"},
        ))
        result = fetch_feed("https://example.com/feed.xml")
        self.assertFalse(result.not_modified)
        self.assertEqual(result.title, "テストフィード")
        self.assertEqual(result.etag, '"e1"')
        self.assertEqual(result.last_modified, "Wed, 01 Jul 2026 09:00:00 GMT")
        self.assertEqual(len(result.entries), 3)
        e = result.entries[0]
        self.assertEqual(e.guid, "guid-1")
        self.assertEqual(e.title, "記事1")
        self.assertEqual(e.link, "https://example.com/a1")
        # HTML タグは落ち、本文テキストはそのまま残る (転載のみ)
        self.assertNotIn("<", e.summary)
        self.assertIn("概要", e.summary)
        self.assertIn("テキスト", e.summary)
        self.assertEqual(
            e.published, datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc),
        )

    def test_guid_fallbacks(self):
        self._patch_get(lambda url, **kw: _rss_response())
        result = fetch_feed("https://example.com/feed.xml")
        # guid 欠落 → link 代替
        self.assertEqual(result.entries[1].guid, "https://example.com/a2")
        # guid も link も欠落 → title+published のハッシュ代替 (決定的)
        self.assertTrue(result.entries[2].guid.startswith("hash:"))
        result2 = fetch_feed("https://example.com/feed.xml")
        self.assertEqual(result.entries[2].guid, result2.entries[2].guid)

    def test_atom_normalization(self):
        self._patch_get(lambda url, **kw: FakeResponse(
            content=ATOM_XML.encode("utf-8"),
            headers={"Content-Type": "application/atom+xml"},
        ))
        result = fetch_feed("https://example.org/atom.xml")
        self.assertEqual(result.title, "Atomフィード")
        self.assertEqual(len(result.entries), 1)
        e = result.entries[0]
        self.assertEqual(e.guid, "tag:example.org,2026:1")
        self.assertEqual(e.title, "Atom記事")
        self.assertEqual(e.link, "https://example.org/e1")
        self.assertEqual(e.summary, "Atomの要約テキスト")
        self.assertEqual(
            e.published, datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc),
        )

    def test_conditional_get_headers_and_304(self):
        captured = {}

        def fake_get(url, headers=None, **kw):
            captured.update(headers or {})
            return FakeResponse(status_code=304)

        self._patch_get(fake_get)
        result = fetch_feed(
            "https://example.com/feed.xml",
            etag='"e1"',
            last_modified="Wed, 01 Jul 2026 09:00:00 GMT",
        )
        self.assertTrue(result.not_modified)
        self.assertEqual(result.entries, [])
        self.assertEqual(captured.get("If-None-Match"), '"e1"')
        self.assertEqual(
            captured.get("If-Modified-Since"), "Wed, 01 Jul 2026 09:00:00 GMT",
        )
        # 変化なしのときは渡した ETag/Last-Modified を保持する
        self.assertEqual(result.etag, '"e1"')

    def test_timeout_error_kind(self):
        self._patch_get(
            lambda url, **kw: (_ for _ in ()).throw(requests.exceptions.Timeout())
        )
        with self.assertRaises(FeedFetchError) as ctx:
            fetch_feed("https://example.com/feed.xml")
        self.assertEqual(ctx.exception.kind, "timeout")

    def test_expired_deadline_raises_timeout_before_dns(self):
        """K4: 期限切れ deadline では SSRF 検査の DNS 解決 (getaddrinfo) より
        前に timeout を上げる — DNS にもネットワークにも到達しない。残余
        (期限内に始まった単発の遅い getaddrinfo) の受容記録は _checked_get
        docstring 参照。"""
        with patch.object(feed_fetch, "_resolve_host_addresses") as dns, \
             patch.object(feed_fetch.requests, "get") as get:
            with self.assertRaises(FeedFetchError) as ctx:
                fetch_feed(
                    "https://example.com/feed.xml",
                    deadline=time.monotonic() - 1,
                )
        self.assertEqual(ctx.exception.kind, "timeout")
        dns.assert_not_called()
        get.assert_not_called()

    def test_network_error_kind(self):
        self._patch_get(
            lambda url, **kw: (_ for _ in ()).throw(
                requests.exceptions.ConnectionError("boom")
            )
        )
        with self.assertRaises(FeedFetchError) as ctx:
            fetch_feed("https://example.com/feed.xml")
        self.assertEqual(ctx.exception.kind, "network")

    def test_http_error_kind(self):
        self._patch_get(lambda url, **kw: FakeResponse(status_code=500))
        with self.assertRaises(FeedFetchError) as ctx:
            fetch_feed("https://example.com/feed.xml")
        self.assertEqual(ctx.exception.kind, "http_error")

    def test_not_a_feed_kind(self):
        self._patch_get(lambda url, **kw: FakeResponse(
            content=HTML_WITHOUT_FEED.encode("utf-8"),
            headers={"Content-Type": "text/html"},
        ))
        with self.assertRaises(FeedFetchError) as ctx:
            fetch_feed("https://example.com/")
        self.assertEqual(ctx.exception.kind, "not_a_feed")

    def test_bozo_empty_is_not_a_feed(self):
        """N3 (九巡目): bozo (解析エラー) かつ記事ゼロの破損応答は成功に
        しない — 成功にすると破損応答の ETag/Last-Modified が保存され、
        以後の条件付き GET が 304 を返し続けて記事を取りこぼす。"""
        # 途中で切れた XML: フィードタイトルまでは読めるが bozo=1・entries=0
        truncated = '<?xml version="1.0"?><rss version="2.0"><channel>' \
            '<title>壊れたフィード</title></channel></rss'
        self._patch_get(lambda url, **kw: FakeResponse(
            content=truncated.encode("utf-8"),
            headers={"Content-Type": "application/rss+xml"},
        ))
        with self.assertRaises(FeedFetchError) as ctx:
            fetch_feed("https://example.com/feed.xml")
        self.assertEqual(ctx.exception.kind, "not_a_feed")

    def test_bozo_with_entries_succeeds(self):
        """N3: bozo でも記事が取れていれば方言差として許容 (feedparser が
        軽微な仕様違反を回収するケース)。"""
        # 未定義実体 &nbsp; で bozo=1 になるが、記事は 1 件回収される
        sloppy = '<?xml version="1.0"?><rss version="2.0"><channel>' \
            '<title>ゆるいフィード</title>' \
            '<item><title>a&nbsp;b</title><guid>g1</guid></item>' \
            '</channel></rss>'
        self._patch_get(lambda url, **kw: FakeResponse(
            content=sloppy.encode("utf-8"),
            headers={"Content-Type": "application/rss+xml"},
        ))
        result = fetch_feed("https://example.com/feed.xml")
        self.assertEqual(len(result.entries), 1)
        self.assertEqual(result.entries[0].guid, "g1")

    def test_valid_empty_feed_succeeds(self):
        """N3: 正当な空フィード (bozo なし・記事ゼロ) は従来どおり成功。"""
        empty = '<?xml version="1.0"?><rss version="2.0"><channel>' \
            '<title>空のフィード</title><link>https://example.com/</link>' \
            '</channel></rss>'
        self._patch_get(lambda url, **kw: FakeResponse(
            content=empty.encode("utf-8"),
            headers={"Content-Type": "application/rss+xml"},
        ))
        result = fetch_feed("https://example.com/feed.xml")
        self.assertEqual(result.title, "空のフィード")
        self.assertEqual(result.entries, [])

    def test_malformed_entry_link_neutralized(self):
        """N2 (九巡目): urlparse が ValueError を投げる悪性 link を持つ記事が
        あっても取得は成功し、当該記事の link だけが空になる。"""
        bad_link_xml = '<?xml version="1.0"?><rss version="2.0"><channel>' \
            '<title>t</title>' \
            '<item><title>悪性リンク記事</title><guid>g-bad</guid>' \
            '<link>http://[bad</link></item>' \
            '</channel></rss>'
        self._patch_get(lambda url, **kw: FakeResponse(
            content=bad_link_xml.encode("utf-8"),
            headers={"Content-Type": "application/rss+xml"},
        ))
        result = fetch_feed("https://example.com/feed.xml")
        self.assertEqual(len(result.entries), 1)
        self.assertEqual(result.entries[0].link, "")
        self.assertEqual(result.entries[0].title, "悪性リンク記事")

    def test_parser_exception_mapped_to_not_a_feed(self):
        """N2: feedparser の予期しない例外は生例外でなく FeedFetchError
        (kind="not_a_feed") として表明する — API 層を 500 にしない。"""
        self._patch_get(lambda url, **kw: _rss_response())
        with patch.object(
            feed_fetch.feedparser, "parse",
            side_effect=RuntimeError("parser crashed"),
        ):
            with self.assertRaises(FeedFetchError) as ctx:
                fetch_feed("https://example.com/feed.xml")
        self.assertEqual(ctx.exception.kind, "not_a_feed")

    def test_overlong_guid_replaced_with_deterministic_hash(self):
        """N5 (九巡目): 512 文字超の guid は sha256 ハッシュ (hash: 形式) に
        置換される。置換は決定論なので再取得でも同一 guid になり重複判定が
        機能する。"""
        long_guid = "g" * 1_000_000
        xml = (
            '<?xml version="1.0"?><rss version="2.0"><channel>'
            '<title>t</title>'
            f'<item><title>長大guid記事</title><guid>{long_guid}</guid></item>'
            '</channel></rss>'
        )
        self._patch_get(lambda url, **kw: FakeResponse(
            content=xml.encode("utf-8"),
            headers={"Content-Type": "application/rss+xml"},
        ))
        result = fetch_feed("https://example.com/feed.xml")
        guid = result.entries[0].guid
        self.assertTrue(guid.startswith("hash:"))
        self.assertLessEqual(len(guid), 512)  # DB 列 GUID の宣言長に収まる
        result2 = fetch_feed("https://example.com/feed.xml")
        self.assertEqual(guid, result2.entries[0].guid)  # 決定論

    def test_response_size_limit_too_large(self):
        os.environ["SAIVERSE_FEED_MAX_BYTES"] = "1024"
        self.addCleanup(lambda: os.environ.pop("SAIVERSE_FEED_MAX_BYTES", None))
        self._patch_get(lambda url, **kw: FakeResponse(
            content=b"x" * 4096,
            headers={"Content-Type": "application/rss+xml"},
        ))
        with self.assertRaises(FeedFetchError) as ctx:
            fetch_feed("https://example.com/feed.xml")
        self.assertEqual(ctx.exception.kind, "too_large")

    def test_entry_count_and_field_length_limits(self):
        long_title = "あ" * 600
        long_summary = "い" * 6000
        items = "".join(
            f"<item><title>{long_title}</title><guid>g{i}</guid>"
            f"<description>{long_summary}</description></item>"
            for i in range(250)
        )
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f"<rss version=\"2.0\"><channel><title>T</title>{items}</channel></rss>"
        )
        self._patch_get(lambda url, **kw: _rss_response(xml))
        result = fetch_feed("https://example.com/feed.xml")
        self.assertEqual(len(result.entries), 200)  # 件数上限
        self.assertEqual(len(result.entries[0].title), 500)
        self.assertEqual(len(result.entries[0].summary), 5000)

    def test_redirect_followed_with_recheck(self):
        """公開 URL → 公開 URL のリダイレクトは手動追跡で通る。"""

        def fake_get(url, **kw):
            if url == "https://example.com/feed.xml":
                return FakeResponse(
                    status_code=302,
                    headers={"Location": "https://example.com/real.xml"},
                )
            return _rss_response()

        self._patch_get(fake_get)
        result = fetch_feed("https://example.com/feed.xml")
        self.assertEqual(result.title, "テストフィード")

    def test_size_limit_reached_without_eof_aborts_immediately(self):
        """G5: 累計が上限に達したら EOF を待たず即打ち切り + close する
        (EOF を返さないストリーム相手に次の chunk を待たない)。"""

        class EndlessResponse(FakeResponse):
            def __init__(self):
                super().__init__(
                    headers={"Content-Type": "application/rss+xml"},
                )
                self.closed = False

            def iter_content(self, chunk_size):
                yield b"x" * 1024
                raise AssertionError("上限到達後に次の chunk を読みに来た")

            def close(self):
                self.closed = True

        os.environ["SAIVERSE_FEED_MAX_BYTES"] = "1024"
        self.addCleanup(lambda: os.environ.pop("SAIVERSE_FEED_MAX_BYTES", None))
        resp = EndlessResponse()
        self._patch_get(lambda url, **kw: resp)
        with self.assertRaises(FeedFetchError) as ctx:
            fetch_feed("https://example.com/feed.xml")
        self.assertEqual(ctx.exception.kind, "too_large")
        self.assertTrue(resp.closed)

    def test_uppercase_scheme_normalized(self):
        """H3 回帰: HTTPS://EXAMPLE.COM/... は scheme だけ小文字化して取得が
        走る (https://HTTPS://... という壊れ URL にしない)。ホスト部の大文字は
        変えない — アドレス検査は _ensure_url_allowed / requests 側が処理する。"""
        captured = {}

        def fake_get(url, **kw):
            captured["url"] = url
            return _rss_response()

        self._patch_get(fake_get)
        result = fetch_feed("HTTPS://EXAMPLE.COM/feed.xml")
        self.assertEqual(captured["url"], "https://EXAMPLE.COM/feed.xml")
        self.assertEqual(result.title, "テストフィード")

    def test_schemeless_url_gets_https(self):
        """無 scheme (example.com/feed) には https:// を前置する (従来動作維持)。"""
        captured = {}

        def fake_get(url, **kw):
            captured["url"] = url
            return _rss_response()

        self._patch_get(fake_get)
        fetch_feed("example.com/feed.xml")
        self.assertEqual(captured["url"], "https://example.com/feed.xml")

    def test_invalid_ipv6_url_is_invalid_url(self):
        """I4: 不正な IPv6 ブラケットの URL は生 ValueError を漏らさず
        FeedFetchError(kind=invalid_url) にする (API 層を 500 にしない)。"""
        with patch.object(
            feed_fetch.requests, "get",
            side_effect=AssertionError("requests.get must not be called"),
        ):
            with self.assertRaises(FeedFetchError) as ctx:
                fetch_feed("https://[::1/feed")
        self.assertEqual(ctx.exception.kind, "invalid_url")

    def test_invalid_port_is_invalid_url(self):
        """I4: port が数値でない / 範囲外の URL も invalid_url (500 にしない)。"""
        for bad in ("https://example.com:99999/feed", "https://example.com:abc/feed"):
            with self.assertRaises(FeedFetchError) as ctx:
                fetch_feed(bad)
            self.assertEqual(ctx.exception.kind, "invalid_url", bad)

    def test_explicit_non_http_scheme_forbidden(self):
        """I4: ftp:// 等の明示 scheme には https:// を前置せず forbidden_url で
        即拒否する (https://ftp://... という壊れ URL への再構成を防ぐ)。"""
        for bad in (
            "ftp://example.com/feed",
            "file:///etc/passwd",
            "FTP://example.com/feed",
        ):
            with self.assertRaises(FeedFetchError) as ctx:
                fetch_feed(bad)
            self.assertEqual(ctx.exception.kind, "forbidden_url", bad)

    def test_user_agent_matches_web_fetch_helpers(self):
        """UA 定数は tools/web_fetch_helpers.py と同値を保つ (import で共有しない
        代わりの機械検査)。"""
        os.environ["SAIVERSE_SKIP_TOOL_IMPORTS"] = "1"
        try:
            from tools.web_fetch_helpers import USER_AGENT as canonical
        finally:
            os.environ.pop("SAIVERSE_SKIP_TOOL_IMPORTS", None)
        self.assertEqual(feed_fetch.USER_AGENT, canonical)


# ---------------------------------------------------------------------------
# SAIVERSE_FEED_FETCH_TIMEOUT の安全 parse (_fetch_timeout)
# ---------------------------------------------------------------------------

class FetchTimeoutEnvTest(unittest.TestCase):
    """S4 (十四巡目): timeout env の不正値 (非数値・空・0・負数) は WARNING +
    既定 15 秒へ fallback する — module-level parse だと import 自体が落ちる
    ため関数化されている。"""

    def _with_env(self, value: str) -> None:
        os.environ["SAIVERSE_FEED_FETCH_TIMEOUT"] = value
        self.addCleanup(
            lambda: os.environ.pop("SAIVERSE_FEED_FETCH_TIMEOUT", None)
        )

    def test_unset_uses_default(self):
        os.environ.pop("SAIVERSE_FEED_FETCH_TIMEOUT", None)
        self.assertEqual(feed_fetch._fetch_timeout(), 15)

    def test_valid_value_used(self):
        self._with_env("30")
        self.assertEqual(feed_fetch._fetch_timeout(), 30)

    def test_non_numeric_falls_back_with_warning(self):
        self._with_env("abc")
        with self.assertLogs("saiverse.feed_fetch", level="WARNING"):
            self.assertEqual(feed_fetch._fetch_timeout(), 15)

    def test_empty_falls_back_with_warning(self):
        self._with_env("")
        with self.assertLogs("saiverse.feed_fetch", level="WARNING"):
            self.assertEqual(feed_fetch._fetch_timeout(), 15)

    def test_zero_falls_back_with_warning(self):
        self._with_env("0")
        with self.assertLogs("saiverse.feed_fetch", level="WARNING"):
            self.assertEqual(feed_fetch._fetch_timeout(), 15)

    def test_negative_falls_back_with_warning(self):
        self._with_env("-5")
        with self.assertLogs("saiverse.feed_fetch", level="WARNING"):
            self.assertEqual(feed_fetch._fetch_timeout(), 15)


# ---------------------------------------------------------------------------
# SSRF ガード (_ensure_url_allowed)
# ---------------------------------------------------------------------------

class UrlGuardTest(unittest.TestCase):
    def test_loopback_ip_rejected(self):
        with self.assertRaises(FeedFetchError) as ctx:
            feed_fetch._ensure_url_allowed("http://127.0.0.1/feed")
        self.assertEqual(ctx.exception.kind, "forbidden_url")

    def test_private_ip_rejected(self):
        for host in ("10.0.0.5", "192.168.1.1", "169.254.0.1"):
            with self.assertRaises(FeedFetchError) as ctx:
                feed_fetch._ensure_url_allowed(f"http://{host}/feed")
            self.assertEqual(ctx.exception.kind, "forbidden_url")

    def test_cgn_shared_ip_rejected(self):
        """N1 (九巡目): CGN 共有帯 100.64.0.0/10 は is_private=False のまま
        非グローバル — 個別フラグの否定列挙ではすり抜けるため、判定の軸は
        「全アドレスが is_global」であること。"""
        for host in ("100.64.0.1", "100.100.100.200"):
            with self.assertRaises(FeedFetchError) as ctx:
                feed_fetch._ensure_url_allowed(f"http://{host}/feed")
            self.assertEqual(ctx.exception.kind, "forbidden_url")

    def test_hostname_resolving_to_cgn_rejected(self):
        """N1 (九巡目): DNS 解決経由で CGN 帯に着地するホスト名も弾く。"""
        with patch.object(
            feed_fetch, "_resolve_host_addresses",
            return_value=[ipaddress.ip_address("100.100.100.200")],
        ):
            with self.assertRaises(FeedFetchError) as ctx:
                feed_fetch._ensure_url_allowed("https://evil.example.com/feed")
            self.assertEqual(ctx.exception.kind, "forbidden_url")

    def test_userinfo_rejected(self):
        with self.assertRaises(FeedFetchError) as ctx:
            feed_fetch._ensure_url_allowed("https://user:pw@example.com/feed")
        self.assertEqual(ctx.exception.kind, "forbidden_url")

    def test_hostname_resolving_to_private_rejected(self):
        with patch.object(
            feed_fetch, "_resolve_host_addresses",
            return_value=[ipaddress.ip_address("10.0.0.5")],
        ):
            with self.assertRaises(FeedFetchError) as ctx:
                feed_fetch._ensure_url_allowed("https://evil.example.com/feed")
            self.assertEqual(ctx.exception.kind, "forbidden_url")

    def test_public_address_allowed(self):
        with patch.object(
            feed_fetch, "_resolve_host_addresses", return_value=[PUBLIC_ADDR],
        ):
            feed_fetch._ensure_url_allowed("https://example.com/feed")  # 例外なし

    def test_env_allows_private(self):
        os.environ["SAIVERSE_FEED_ALLOW_PRIVATE"] = "1"
        self.addCleanup(lambda: os.environ.pop("SAIVERSE_FEED_ALLOW_PRIVATE", None))
        feed_fetch._ensure_url_allowed("http://127.0.0.1/feed")  # 例外なし

    def test_fetch_feed_rejects_loopback_before_network(self):
        with patch.object(
            feed_fetch.requests, "get",
            side_effect=AssertionError("requests.get must not be called"),
        ):
            with self.assertRaises(FeedFetchError) as ctx:
                fetch_feed("http://127.0.0.1:8000/feed")
        self.assertEqual(ctx.exception.kind, "forbidden_url")

    def test_redirect_to_private_rejected(self):
        """公開 URL → 127.0.0.1 へのリダイレクトは hop 再検査で弾く。"""
        _patch_public_dns(self)

        def fake_get(url, **kw):
            return FakeResponse(
                status_code=302,
                headers={"Location": "http://127.0.0.1/admin"},
            )

        with patch.object(feed_fetch.requests, "get", side_effect=fake_get):
            with self.assertRaises(FeedFetchError) as ctx:
                fetch_feed("https://example.com/feed.xml")
        self.assertEqual(ctx.exception.kind, "forbidden_url")

    def test_redirect_to_malformed_location_maps_to_invalid_url(self):
        """O1 (十巡目): リダイレクト先 (Location) が urlparse 不能な URL
        (不正な IPv6 ブラケット) でも生 ValueError を漏らさず invalid_url に
        写像する — 漏れると API 層が 500 になる。"""
        _patch_public_dns(self)

        def fake_get(url, **kw):
            return FakeResponse(
                status_code=301,
                headers={"Location": "http://[bad"},
            )

        with patch.object(feed_fetch.requests, "get", side_effect=fake_get):
            with self.assertRaises(FeedFetchError) as ctx:
                fetch_feed("https://example.com/feed.xml")
        self.assertEqual(ctx.exception.kind, "invalid_url")


# ---------------------------------------------------------------------------
# discover_feed
# ---------------------------------------------------------------------------

class DiscoverFeedTest(unittest.TestCase):
    def setUp(self):
        _patch_public_dns(self)

    def _patch_routes(self, routes):
        """url → FakeResponse の辞書で requests.get を差し替える。"""

        def fake_get(url, **kw):
            resp = routes.get(url)
            if resp is None:
                return FakeResponse(status_code=404)
            return resp

        patcher = patch.object(feed_fetch.requests, "get", side_effect=fake_get)
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_html_autodiscovery(self):
        self._patch_routes({
            "https://example.com": FakeResponse(
                content=HTML_WITH_AUTODISCOVERY.encode("utf-8"),
                headers={"Content-Type": "text/html"},
                url="https://example.com",
            ),
        })
        found = discover_feed("https://example.com")
        urls = [f.url for f in found]
        # 相対 href はサイト起点で解決、絶対 href (別ホストの自己宣言) はそのまま
        self.assertEqual(urls, [
            "https://example.com/feed.xml",
            "https://feeds.example.net/site.atom",
        ])
        self.assertEqual(found[0].source, "autodiscovery")
        self.assertEqual(found[0].title, "RSS")

    def test_autodiscovery_title_capped(self):
        """I3: HTML 宣言のフィードタイトル (外部入力) は 200 文字に切り詰めて
        返す (購読タイトルの入口上限と同じ値)。"""
        huge = "t" * 500
        html = HTML_WITH_AUTODISCOVERY.replace('title="RSS"', f'title="{huge}"')
        self._patch_routes({
            "https://example.com": FakeResponse(
                content=html.encode("utf-8"),
                headers={"Content-Type": "text/html"},
                url="https://example.com",
            ),
        })
        found = discover_feed("https://example.com")
        self.assertEqual(len(found[0].title), 200)
        self.assertEqual(found[0].title, "t" * 200)

    def test_common_path_fallback(self):
        self._patch_routes({
            "https://example.com": FakeResponse(
                content=HTML_WITHOUT_FEED.encode("utf-8"),
                headers={"Content-Type": "text/html"},
                url="https://example.com",
            ),
            "https://example.com/feed": _rss_response(),
        })
        found = discover_feed("https://example.com")
        self.assertEqual([f.url for f in found], ["https://example.com/feed"])
        self.assertEqual(found[0].source, "common_path")

    def test_nothing_found(self):
        self._patch_routes({
            "https://example.com": FakeResponse(
                content=HTML_WITHOUT_FEED.encode("utf-8"),
                headers={"Content-Type": "text/html"},
                url="https://example.com",
            ),
        })
        self.assertEqual(discover_feed("https://example.com"), [])

    def test_site_url_is_already_a_feed(self):
        self._patch_routes({
            "https://example.com/feed.xml": _rss_response(),
        })
        found = discover_feed("https://example.com/feed.xml")
        self.assertEqual([f.url for f in found], ["https://example.com/feed.xml"])

    def test_site_fetch_failure_raises(self):
        patcher = patch.object(
            feed_fetch.requests, "get",
            side_effect=requests.exceptions.ConnectionError("down"),
        )
        self.addCleanup(patcher.stop)
        patcher.start()
        with self.assertRaises(FeedFetchError):
            discover_feed("https://example.com")

    def test_common_path_probe_budget_exhaustion_raises_timeout(self):
        """K3: 定番パス探索の途中で時間予算が切れたら、候補固有の失敗と
        混同して「候補なし」(→ API 422 = フィード未提供の誤診) に畳まず、
        timeout をそのまま上げる (→ API 502 系)。"""
        clock = {"now": 0.0}

        class BudgetEatingResponse(FakeResponse):
            """本文を返し切った直後に予算を使い切ったことにする — サイト取得
            は成功し、定番パス probe の入口で期限切れになる並びの再現。"""

            def iter_content(self, chunk_size):
                yield self.content
                clock["now"] = 100.0

        probe_urls = []

        def fake_get(url, **kw):
            if url == "https://example.com":
                return BudgetEatingResponse(
                    content=HTML_WITHOUT_FEED.encode("utf-8"),
                    headers={"Content-Type": "text/html"},
                    url="https://example.com",
                )
            probe_urls.append(url)
            return FakeResponse(status_code=404)

        get_patcher = patch.object(
            feed_fetch.requests, "get", side_effect=fake_get,
        )
        self.addCleanup(get_patcher.stop)
        get_patcher.start()
        clock_patcher = patch.object(
            feed_fetch.time, "monotonic", side_effect=lambda: clock["now"],
        )
        self.addCleanup(clock_patcher.stop)
        clock_patcher.start()

        with self.assertRaises(FeedFetchError) as ctx:
            discover_feed("https://example.com", deadline=50.0)
        self.assertEqual(ctx.exception.kind, "timeout")
        # 期限切れ後の probe はネットワークに到達していない (K4 の前段検査)
        self.assertEqual(probe_urls, [])

    def test_probe_stream_read_timeout_raises_timeout(self):
        """O2 (十巡目): 定番パス probe の本文ストリーミング中に requests 層の
        ReadTimeout が出たら「この候補はフィードでない」(False) に畳まず
        timeout を上げる — K3 (予算切れを候補なしの 422 に化けさせない) と
        同じ理由の requests 層版。"""

        class StallingResponse(FakeResponse):
            def iter_content(self, chunk_size):
                raise requests.exceptions.ReadTimeout("stream stalled")

        def fake_get(url, **kw):
            if url == "https://example.com":
                return FakeResponse(
                    content=HTML_WITHOUT_FEED.encode("utf-8"),
                    headers={"Content-Type": "text/html"},
                    url="https://example.com",
                )
            return StallingResponse(
                headers={"Content-Type": "application/rss+xml"},
            )

        patcher = patch.object(feed_fetch.requests, "get", side_effect=fake_get)
        self.addCleanup(patcher.stop)
        patcher.start()

        with self.assertRaises(FeedFetchError) as ctx:
            discover_feed("https://example.com")
        self.assertEqual(ctx.exception.kind, "timeout")


# ---------------------------------------------------------------------------
# FeedManager (DB + 配送)
# ---------------------------------------------------------------------------

class DummyEmbedder:
    def __init__(self, model=None, **kwargs) -> None:
        self.model_name = model

    def embed(self, texts, **kwargs):
        return [[0.0] * 3 for _ in texts]


def _make_fake_manager(db_url: str | None = None):
    """テスト用の fake manager。既定は in-memory (StaticPool = 単一接続共有)。

    スレッド並走テストは file DB の URL を渡す — StaticPool の単一接続を
    複数スレッドの transaction が共有すると interleave して壊れるため。
    """
    if db_url is None:
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    else:
        engine = create_engine(
            db_url, connect_args={"check_same_thread": False},
        )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
        db.flush()
        city = City(USERID=1, CITYNAME="test_city", UI_PORT=3001, API_PORT=8001)
        db.add(city)
        db.flush()
        db.add(Building(
            CITYID=city.CITYID, BUILDINGID=BUILDING_ID, BUILDINGNAME="lounge",
        ))
        db.add(AI(AIID="tester", HOME_CITYID=city.CITYID, AINAME="Tester"))
        db.commit()
        city_id = city.CITYID
    finally:
        db.close()
    fake = SimpleNamespace(
        SessionLocal=Session,
        occupants={},
        personas={},
        event_scheduler=None,
        city_id=city_id,  # City 所有権境界 (city_feed_fixture_ids) が参照する
    )
    fake.observer_manager = ObserverManager(fake)
    return engine, fake


class FeedManagerFetchTest(unittest.TestCase):
    def setUp(self):
        _patch_public_dns(self)
        self.engine, self.fake = _make_fake_manager()
        self.addCleanup(self.engine.dispose)
        self.fm = FeedManager(self.fake)
        fixture = self.fm.create_feed_fixture(BUILDING_ID, "新聞スタンド", "テスト用")
        self.fixture_id = fixture.FIXTURE_ID
        sub = self.fm.add_subscription(
            self.fixture_id, "https://example.com/feed.xml", title="テストフィード",
        )
        self.sub_id = sub.SUBSCRIPTION_ID

    def _patch_get(self, func):
        patcher = patch.object(feed_fetch.requests, "get", side_effect=func)
        self.addCleanup(patcher.stop)
        patcher.start()

    def _get_sub(self):
        db = self.fake.SessionLocal()
        try:
            return db.query(FeedSubscription).filter(
                FeedSubscription.SUBSCRIPTION_ID == self.sub_id
            ).first()
        finally:
            db.close()

    def _item_count(self):
        db = self.fake.SessionLocal()
        try:
            return db.query(FeedItem).filter(
                FeedItem.SUBSCRIPTION_ID == self.sub_id
            ).count()
        finally:
            db.close()

    def test_new_items_saved_without_duplicates(self):
        self._patch_get(lambda url, **kw: _rss_response(
            RSS_XML_TWO_ITEMS, headers={"ETag": '"e1"'},
        ))
        self.assertEqual(self.fm._fetch_one(self.sub_id), 2)
        self.assertEqual(self._item_count(), 2)
        # 同じ guid の再取得は保存しない (UniqueConstraint + 事前照合)
        self.assertEqual(self.fm._fetch_one(self.sub_id), 0)
        self.assertEqual(self._item_count(), 2)
        sub = self._get_sub()
        self.assertEqual(sub.ETAG, '"e1"')
        self.assertIsNotNone(sub.LAST_OK_AT)
        self.assertEqual(sub.CONSECUTIVE_FAILURES, 0)
        self.assertIsNone(sub.LAST_ERROR)

    def test_failure_counting_and_recovery(self):
        self._patch_get(
            lambda url, **kw: (_ for _ in ()).throw(
                requests.exceptions.ConnectionError("down")
            )
        )
        self.fm._fetch_one(self.sub_id)
        self.fm._fetch_one(self.sub_id)
        sub = self._get_sub()
        self.assertEqual(sub.CONSECUTIVE_FAILURES, 2)
        self.assertIsNotNone(sub.LAST_ERROR)
        self.assertEqual(self._item_count(), 0)  # 失敗を成功と偽装しない

        self._patch_get(lambda url, **kw: _rss_response(RSS_XML_TWO_ITEMS))
        self.fm._fetch_one(self.sub_id)
        sub = self._get_sub()
        self.assertEqual(sub.CONSECUTIVE_FAILURES, 0)
        self.assertIsNone(sub.LAST_ERROR)

    def test_unexpected_exception_isolated_per_subscription(self):
        """N2 (九巡目): FeedFetchError 以外の想定外例外を出す購読が 1 本
        あってもサイクルは死なない — 購読単位で隔離して失敗を記録し、
        他の購読の取得は続く。"""
        sub2 = self.fm.add_subscription(
            self.fixture_id, "https://example.org/feed2.xml", title="第二フィード",
        )

        def fake_fetch(url, **kw):
            if "example.com" in url:
                raise RuntimeError("simulated parser crash")
            return feed_fetch.FeedFetchResult(
                url=url,
                title="第二フィード",
                entries=[feed_fetch.FeedEntry(
                    guid="g-survivor", title="生き残り記事", summary="",
                    link="", published=None,
                )],
            )

        patcher = patch("saiverse.feed_manager.fetch_feed", side_effect=fake_fetch)
        self.addCleanup(patcher.stop)
        patcher.start()
        self.fm._fetch_all()  # 例外は漏れない
        # 悪性購読には失敗が記録される (成功と偽装しない — 不変条件 2)
        sub1 = self._get_sub()
        self.assertEqual(sub1.CONSECUTIVE_FAILURES, 1)
        self.assertIn("予期しないエラー", sub1.LAST_ERROR)
        # 健全な購読の取得は続いた
        db = self.fake.SessionLocal()
        try:
            count2 = db.query(FeedItem).filter(
                FeedItem.SUBSCRIPTION_ID == sub2.SUBSCRIPTION_ID
            ).count()
        finally:
            db.close()
        self.assertEqual(count2, 1)

    def test_cycle_budget_exceeded_stops_fetch_but_delivery_runs(self):
        """AA3 (二十三巡目): サイクル全体の壁時計予算
        (SAIVERSE_FEED_CYCLE_BUDGET_SEC) を超えたら残り購読の取得を打ち切り、
        残数を WARNING で表明する。打ち切っても表示更新・配送・剪定は実行
        される — 取得済みぶんを届ける。"""
        for i in range(2):
            self.fm.add_subscription(
                self.fixture_id, f"https://example.org/f{i}.xml",
            )
        fetched = []
        clock = {"t": 0.0}

        def fake_fetch_one(sub_id):
            fetched.append(sub_id)
            clock["t"] += 100.0  # 遅い購読 (1 本 100 秒) の偽装
            return 0

        with patch.object(self.fm, "_fetch_one", side_effect=fake_fetch_one), \
             patch(
                 "saiverse.feed_manager.time.monotonic",
                 side_effect=lambda: clock["t"],
             ), \
             patch.object(self.fm, "_update_all_fixture_displays") as update_m, \
             patch.object(self.fm, "deliver_new_items") as deliver_m, \
             patch.object(self.fm, "_prune_old_items") as prune_m, \
             patch.dict(os.environ, {"SAIVERSE_FEED_CYCLE_BUDGET_SEC": "150"}):
            with self.assertLogs(
                "saiverse.feed_manager", level="WARNING",
            ) as logs:
                self.fm._fetch_cycle_worker()
        # 予算 150 秒: 1 本目 (経過 0)・2 本目 (経過 100) は取得、
        # 3 本目は経過 200 >= 150 で打ち切り
        self.assertEqual(len(fetched), 2)
        self.assertTrue(
            any("budget exceeded" in m for m in logs.output), logs.output,
        )
        # 打ち切っても表示更新・配送・剪定は実行される
        update_m.assert_called_once()
        deliver_m.assert_called_once()
        prune_m.assert_called_once()

    def test_fetch_order_rotates_between_cycles(self):
        """AA3 公平性: 購読の処理開始位置がサイクルごとに 1 つずつ進む —
        予算打ち切りは処理順の後方を削るため、開始位置が固定だと後方の購読
        が慢性的に取得されない。"""
        for i in range(2):
            self.fm.add_subscription(
                self.fixture_id, f"https://example.org/f{i}.xml",
            )
        db = self.fake.SessionLocal()
        try:
            all_ids = sorted(
                r[0]
                for r in db.query(FeedSubscription.SUBSCRIPTION_ID).all()
            )
        finally:
            db.close()
        orders = []

        def fake_fetch_one(sub_id):
            orders[-1].append(sub_id)
            return 0

        with patch.object(self.fm, "_fetch_one", side_effect=fake_fetch_one):
            for _ in range(3):
                orders.append([])
                self.fm._fetch_all()
        self.assertEqual(orders[0], all_ids)  # 初回は整列順そのまま
        self.assertEqual(orders[1], all_ids[1:] + all_ids[:1])
        self.assertEqual(orders[2], all_ids[2:] + all_ids[:2])

    def test_million_char_guid_saved_short_and_deduped(self):
        """N5 (九巡目): 100 万文字 guid の記事はハッシュ guid で保存され、
        再取得しても重複しない (置換が決定論のため)。"""
        long_guid = "z" * 1_000_000
        xml = (
            '<?xml version="1.0"?><rss version="2.0"><channel>'
            '<title>テストフィード</title>'
            f'<item><title>長大guid記事</title><guid>{long_guid}</guid></item>'
            '</channel></rss>'
        )
        self._patch_get(lambda url, **kw: FakeResponse(
            content=xml.encode("utf-8"),
            headers={"Content-Type": "application/rss+xml"},
        ))
        self.assertEqual(self.fm._fetch_one(self.sub_id), 1)
        self.assertEqual(self.fm._fetch_one(self.sub_id), 0)  # 再取得は重複しない
        self.assertEqual(self._item_count(), 1)
        db = self.fake.SessionLocal()
        try:
            item = db.query(FeedItem).filter(
                FeedItem.SUBSCRIPTION_ID == self.sub_id
            ).first()
            self.assertTrue(item.GUID.startswith("hash:"))
            self.assertLessEqual(len(item.GUID), 512)
        finally:
            db.close()

    def test_fetch_one_discards_result_when_stopping(self):
        """I2: 停止要求後の _fetch_one は取得が済んでいても commit しない —
        stop() 後の残留 worker がデータ表 (購読状態・記事) に書かない。"""

        def get_and_stop(url, **kw):
            # ネットワーク取得の最中に stop() が入った状況を偽装
            self.fm._stop_event.set()
            return _rss_response(RSS_XML_TWO_ITEMS, headers={"ETag": '"e1"'})

        self._patch_get(get_and_stop)
        self.assertEqual(self.fm._fetch_one(self.sub_id), 0)
        self.assertEqual(self._item_count(), 0)
        sub = self._get_sub()
        self.assertIsNone(sub.LAST_OK_AT)
        self.assertIsNone(sub.ETAG)

    def test_fetch_one_skips_failure_record_when_stopping(self):
        """I2: 停止要求後は失敗記録 (CONSECUTIVE_FAILURES / LAST_ERROR) も
        書かない — 次回サイクルの取得で正直に付き直る。"""

        def fail_and_stop(url, **kw):
            self.fm._stop_event.set()
            raise requests.exceptions.ConnectionError("down")

        self._patch_get(fail_and_stop)
        self.assertEqual(self.fm._fetch_one(self.sub_id), 0)
        sub = self._get_sub()
        self.assertEqual(sub.CONSECUTIVE_FAILURES or 0, 0)
        self.assertIsNone(sub.LAST_ERROR)

    def test_subscription_title_capped_at_entry(self):
        """I3: ユーザー指定タイトルは入口 (add_subscription) で 200 文字に
        切り詰めて保存する。"""
        sub = self.fm.add_subscription(
            self.fixture_id, "https://example.com/huge.xml", title="た" * 1000,
        )
        self.assertEqual(sub.TITLE, "た" * 200)

    def test_feed_declared_title_capped(self):
        """I3: フィード宣言タイトルの自動採用 (_fetch_one) も 200 文字まで。"""
        sub = self.fm.add_subscription(
            self.fixture_id, "https://example.com/untitled.xml",
        )
        xml = RSS_XML_TWO_ITEMS.replace("テストフィード", "巨" * 1000)
        self._patch_get(lambda url, **kw: _rss_response(xml))
        self.fm._fetch_one(sub.SUBSCRIPTION_ID)
        db = self.fake.SessionLocal()
        try:
            stored = db.query(FeedSubscription).filter(
                FeedSubscription.SUBSCRIPTION_ID == sub.SUBSCRIPTION_ID
            ).first()
            self.assertEqual(stored.TITLE, "巨" * 200)
        finally:
            db.close()

    def test_not_modified_marks_ok_without_items(self):
        self._patch_get(lambda url, **kw: FakeResponse(status_code=304))
        self.assertEqual(self.fm._fetch_one(self.sub_id), 0)
        sub = self._get_sub()
        self.assertIsNotNone(sub.LAST_OK_AT)
        self.assertEqual(sub.CONSECUTIVE_FAILURES, 0)
        self.assertEqual(self._item_count(), 0)

    def test_state_json_has_no_internal_bookkeeping(self):
        self._patch_get(lambda url, **kw: _rss_response(
            RSS_XML_TWO_ITEMS, headers={"ETag": '"secret-etag"'},
        ))
        self.fm._fetch_one(self.sub_id)
        self.fm.update_fixture_display(self.fixture_id)
        db = self.fake.SessionLocal()
        try:
            from database.models import Fixture
            fixture = db.query(Fixture).filter(
                Fixture.FIXTURE_ID == self.fixture_id
            ).first()
            state_json = fixture.STATE_JSON or ""
        finally:
            db.close()
        state = json.loads(state_json)["feed_stand"]
        # ペルソナに見せてよい表示情報だけ。latest は最新が先頭
        # (published desc — 記事B 10:00 > 記事A 09:00。二十巡目 V2)
        self.assertEqual(state["subscriptions"], ["テストフィード"])
        self.assertEqual(state["latest"], ["記事B", "記事A"])
        # 内部帳簿 (購読 URL / ETag) は漏れない
        self.assertNotIn("example.com", state_json)
        self.assertNotIn("secret-etag", state_json)
        self.assertNotIn("etag", state_json.lower())

    def test_state_json_latest_titles_are_truncated(self):
        """STATE_JSON の latest headlines は各 100 文字に制限される (F6)。"""
        db = self.fake.SessionLocal()
        try:
            db.add(FeedItem(
                SUBSCRIPTION_ID=self.sub_id,
                GUID="long-title",
                TITLE="長" * 300,
                SUMMARY="",
                LINK="",
            ))
            db.commit()
        finally:
            db.close()
        self.fm.update_fixture_display(self.fixture_id)
        db = self.fake.SessionLocal()
        try:
            from database.models import Fixture
            fixture = db.query(Fixture).filter(
                Fixture.FIXTURE_ID == self.fixture_id
            ).first()
            state = json.loads(fixture.STATE_JSON)["feed_stand"]
        finally:
            db.close()
        self.assertLessEqual(len(state["latest"]), 5)
        for title in state["latest"]:
            self.assertLessEqual(len(title), 100)

    def test_state_json_latest_ordering_matches_delivery_and_items_api(self):
        """二十巡目 V2: latest 5 件の選択・並びは配送の選択・items API と同一
        (published desc [NULL 末尾], id desc = 最新が先頭)。newest-first
        フィードの初回取り込み (若い id ほど新しい) で 6 件以上あるとき、
        id desc 選択では実際の最新記事 (id=1) が 5 件から漏れていた。"""
        db = self.fake.SessionLocal()
        try:
            # newest-first: id=1 が published 最新、id=6 が最古
            for i in range(1, 7):
                db.add(FeedItem(
                    SUBSCRIPTION_ID=self.sub_id,
                    GUID=f"g-{i}",
                    TITLE=f"記事{i}",
                    SUMMARY="",
                    LINK="",
                    PUBLISHED_AT=datetime(2026, 8, 3, 12 - i, 0),
                ))
            db.commit()
        finally:
            db.close()
        self.fm.update_fixture_display(self.fixture_id)
        db = self.fake.SessionLocal()
        try:
            from database.models import Fixture
            fixture = db.query(Fixture).filter(
                Fixture.FIXTURE_ID == self.fixture_id
            ).first()
            state = json.loads(fixture.STATE_JSON)["feed_stand"]
        finally:
            db.close()
        # 最新タイトルが先頭。id desc 選択なら [記事6..記事2] で
        # 最新の記事1 が漏れていた
        self.assertEqual(
            state["latest"], ["記事1", "記事2", "記事3", "記事4", "記事5"],
        )

    def test_add_subscription_same_url_returns_existing(self):
        """同一 Fixture への同一 URL (正規化後) は get-or-create (F7)。"""
        again = self.fm.add_subscription(
            self.fixture_id, "HTTPS://EXAMPLE.COM/feed.xml/", title="別名",
        )
        self.assertEqual(again.SUBSCRIPTION_ID, self.sub_id)
        db = self.fake.SessionLocal()
        try:
            self.assertEqual(db.query(FeedSubscription).count(), 1)
        finally:
            db.close()

    def test_add_subscription_concurrent_insert_converges(self):
        """G4: 事前検索と INSERT の間に別リクエストが同じ購読を先に作った場合
        (同時 POST)、IntegrityError を握って既存行に収束する (500 にしない)。"""
        real = self.fm._find_existing_subscription
        calls = {"n": 0}

        def racy_find(db, fixture_id, url):
            calls["n"] += 1
            if calls["n"] == 1:
                # 検索時点では相手の INSERT がまだ見えなかったことにする
                return None
            return real(db, fixture_id, url)

        with patch.object(
            self.fm, "_find_existing_subscription", side_effect=racy_find,
        ):
            again = self.fm.add_subscription(
                self.fixture_id, "https://example.com/feed.xml", title="競合",
            )
        self.assertEqual(again.SUBSCRIPTION_ID, self.sub_id)
        self.assertEqual(calls["n"], 2)  # rollback 後に再検索している
        db = self.fake.SessionLocal()
        try:
            self.assertEqual(db.query(FeedSubscription).count(), 1)
        finally:
            db.close()

    def test_normalize_url_strips_default_ports(self):
        """G8: 既定ポート (http の :80 / https の :443) の明示は同一 URL と
        みなして除去。非既定ポートと query の並びは保持する。"""
        norm = FeedManager._normalize_feed_url
        self.assertEqual(
            norm("https://Example.com:443/feed.xml"),
            "https://example.com/feed.xml",
        )
        self.assertEqual(
            norm("http://example.com:80/feed"), "http://example.com/feed",
        )
        # scheme に対応しないポート・非既定ポートは識別情報として保持
        self.assertEqual(
            norm("https://example.com:8443/feed"),
            "https://example.com:8443/feed",
        )
        self.assertEqual(
            norm("http://example.com:443/feed"), "http://example.com:443/feed",
        )
        # query は保持し、並び替えもしない (順序が意味を持つ場合がある)
        self.assertEqual(
            norm("https://example.com/f?b=2&a=1"),
            "https://example.com/f?b=2&a=1",
        )

    def test_add_subscription_default_port_url_returns_existing(self):
        """G8: :443 明示の URL は既存の (ポートなし) 購読へ収束する。"""
        again = self.fm.add_subscription(
            self.fixture_id, "https://example.com:443/feed.xml",
        )
        self.assertEqual(again.SUBSCRIPTION_ID, self.sub_id)

    def test_add_subscription_rejects_non_feed_fixture(self):
        """feed_stand 以外の Fixture への購読追加は拒否 (F8)。"""
        from database.models import Fixture
        db = self.fake.SessionLocal()
        try:
            db.add(Fixture(
                FIXTURE_ID="obj-1", BUILDING_ID=BUILDING_ID,
                NAME="ただの置物", TYPE="object",
            ))
            db.commit()
        finally:
            db.close()
        with self.assertRaises(ValueError):
            self.fm.add_subscription("obj-1", "https://example.com/feed.xml")

    def test_add_subscription_rejects_overlong_url(self):
        """O4 (十巡目): 正規化後 512 字 (DB 宣言 String(512)) を超える URL は
        保存前に ValueError で拒否 — SQLite は VARCHAR 長を強制しないため、
        検査しないと宣言超過の行がそのまま入る。"""
        overlong = "https://example.com/" + "a" * 512
        with self.assertRaises(ValueError) as ctx:
            self.fm.add_subscription(self.fixture_id, overlong)
        self.assertIn("長すぎます", str(ctx.exception))
        db = self.fake.SessionLocal()
        try:
            self.assertEqual(db.query(FeedSubscription).count(), 1)  # setUp の 1 本のみ
        finally:
            db.close()

    def test_subscription_limit_new_rejected_existing_url_ok(self):
        """U3 (十五巡目): 新規購読は上限 (SAIVERSE_FEED_MAX_SUBSCRIPTIONS_
        PER_FIXTURE) で FeedSubscriptionLimitError。既存 URL の再追加
        (get-or-create) は上限到達後も既存行を返して成功する。"""
        from saiverse.feed_manager import FeedSubscriptionLimitError
        with patch.dict(os.environ, {
            "SAIVERSE_FEED_MAX_SUBSCRIPTIONS_PER_FIXTURE": "1",
        }):
            # setUp の 1 本で既に上限 — 新規 URL は拒否
            with self.assertRaises(FeedSubscriptionLimitError) as ctx:
                self.fm.add_subscription(
                    self.fixture_id, "https://example.com/other.xml",
                )
            self.assertIn("上限", str(ctx.exception))
            # 既存 URL の再追加は get-or-create で成功 (新規作成しないので数えない)
            sub = self.fm.add_subscription(
                self.fixture_id, "https://example.com/feed.xml",
            )
            self.assertEqual(sub.SUBSCRIPTION_ID, self.sub_id)
        db = self.fake.SessionLocal()
        try:
            self.assertEqual(db.query(FeedSubscription).count(), 1)
        finally:
            db.close()

    def test_subscription_limit_env_invalid_falls_back_to_default(self):
        """U3: 0 以下・非数値の env は不正として既定 (10) へ。"""
        from saiverse.feed_manager import DEFAULT_MAX_SUBSCRIPTIONS_PER_FIXTURE
        for bad in ("0", "-3", "abc"):
            with patch.dict(os.environ, {
                "SAIVERSE_FEED_MAX_SUBSCRIPTIONS_PER_FIXTURE": bad,
            }):
                self.assertEqual(
                    FeedManager._read_max_subscriptions_env(),
                    DEFAULT_MAX_SUBSCRIPTIONS_PER_FIXTURE,
                    msg=f"env value: {bad!r}",
                )

    def test_overlong_site_url_dropped_not_saved(self):
        """O4 (十巡目): site_url は外部由来 (フィードの channel link) の表示用
        メタデータ — 512 字超はエラーにせず破棄して購読自体は成立させる
        (切り詰めは別資源を指す壊れリンクになるのでしない)。"""
        sub = self.fm.add_subscription(
            self.fixture_id,
            "https://example.com/other.xml",
            site_url="https://example.com/" + "s" * 600,
        )
        self.assertIsNone(sub.SITE_URL)

    def test_fetch_now_returns_none_while_running(self):
        """取得サイクル実行中の fetch_now は None (起動しない) (F9)。"""
        self.assertTrue(self.fm._fetch_lock.acquire(blocking=False))
        try:
            self.assertIsNone(self.fm.fetch_now())
        finally:
            self.fm._fetch_lock.release()

    def test_start_registers_periodic_with_immediate_first_fire(self):
        calls = []
        self.fake.event_scheduler = SimpleNamespace(
            schedule_periodic=lambda **kw: calls.append(kw),
            cancel=lambda key: None,
        )
        fm = FeedManager(self.fake)
        self.assertEqual(calls, [])  # __init__ は構築のみ (背景処理を始めない)
        fm.start()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["key"], "feeds:fetch")
        self.assertTrue(calls[0]["first_fire_immediate"])

    def test_remove_subscription_deletes_items_and_cursors(self):
        self._patch_get(lambda url, **kw: _rss_response(RSS_XML_TWO_ITEMS))
        self.fm._fetch_one(self.sub_id)
        db = self.fake.SessionLocal()
        try:
            db.add(FeedReadCursor(
                PERSONA_ID="tester", SUBSCRIPTION_ID=self.sub_id, LAST_ITEM_ID=1,
            ))
            db.commit()
        finally:
            db.close()
        self.assertTrue(self.fm.remove_subscription(self.sub_id))
        db = self.fake.SessionLocal()
        try:
            self.assertEqual(db.query(FeedSubscription).count(), 0)
            self.assertEqual(db.query(FeedItem).count(), 0)
            self.assertEqual(db.query(FeedReadCursor).count(), 0)
        finally:
            db.close()
        self.assertFalse(self.fm.remove_subscription(self.sub_id))

    def test_fetch_all_skips_subscription_of_non_feed_fixture(self):
        """L1: 取得列挙はフィード施設 (TYPE="feed_stand") の購読に限る —
        Fixture の TYPE が書き換わって孤児化した購読は取得もネットワークも
        走らない (不活性で無害)。配送クエリと同じ join フィルタ。"""
        from database.models import Fixture
        calls = []
        with patch.object(
            self.fm, "_fetch_one", side_effect=lambda sid: calls.append(sid),
        ):
            self.fm._fetch_all()
        self.assertEqual(calls, [self.sub_id])  # 通常は列挙される

        db = self.fake.SessionLocal()
        try:
            fixture = db.query(Fixture).filter(
                Fixture.FIXTURE_ID == self.fixture_id
            ).first()
            fixture.TYPE = "object"
            db.commit()
        finally:
            db.close()
        calls.clear()
        with patch.object(
            self.fm, "_fetch_one", side_effect=lambda sid: calls.append(sid),
        ):
            self.fm._fetch_all()
        self.assertEqual(calls, [])  # 型変更後は列挙から外れる

    def _seed_plain_items(self, count):
        db = self.fake.SessionLocal()
        try:
            for i in range(1, count + 1):
                db.add(FeedItem(
                    SUBSCRIPTION_ID=self.sub_id, GUID=f"g-{i}",
                    TITLE=f"記事{i}", SUMMARY="", LINK="",
                ))
            db.commit()
        finally:
            db.close()

    def test_prune_old_items_keeps_newest(self):
        """L4: 取得サイクル末尾の剪定 — 購読ごとに KEEP 件より古い行 (id 降順)
        を削除する。取得サイクル経由で走らせ、配線ごと検査する。"""
        os.environ["SAIVERSE_FEED_ITEM_KEEP"] = "3"
        self.addCleanup(lambda: os.environ.pop("SAIVERSE_FEED_ITEM_KEEP", None))
        self._seed_plain_items(5)
        with patch.object(self.fm, "_fetch_all"):  # ネットワーク段は素通し
            self.fm._fetch_cycle_worker()
        db = self.fake.SessionLocal()
        try:
            remaining = (
                db.query(FeedItem)
                .filter(FeedItem.SUBSCRIPTION_ID == self.sub_id)
                .order_by(FeedItem.id)
                .all()
            )
            self.assertEqual(
                [it.GUID for it in remaining], ["g-3", "g-4", "g-5"],
            )
        finally:
            db.close()

    def test_prune_disabled_when_keep_is_zero(self):
        """L4: SAIVERSE_FEED_ITEM_KEEP=0 (以下) は「剪定しない」の明示指定。"""
        os.environ["SAIVERSE_FEED_ITEM_KEEP"] = "0"
        self.addCleanup(lambda: os.environ.pop("SAIVERSE_FEED_ITEM_KEEP", None))
        self._seed_plain_items(5)
        self.assertEqual(self.fm._prune_old_items(), 0)
        self.assertEqual(self._item_count(), 5)

    def test_prune_keep_floor_is_delivery_push_size(self):
        """P2 (十一巡目): KEEP < 配送 N 件の端な設定では、実効 keep を N まで
        引き上げる — 「最新 N 件は必ず剪定を生き残る」がタイムライン意味論
        (剪定は未配送記事を消さない) の成立条件のため。"""
        os.environ["SAIVERSE_FEED_ITEM_KEEP"] = "1"
        os.environ["SAIVERSE_FEED_MAX_ITEMS_PER_PUSH"] = "3"
        self.addCleanup(lambda: os.environ.pop("SAIVERSE_FEED_ITEM_KEEP", None))
        self.addCleanup(
            lambda: os.environ.pop("SAIVERSE_FEED_MAX_ITEMS_PER_PUSH", None)
        )
        self._seed_plain_items(5)
        self.assertEqual(self.fm._prune_old_items(), 2)
        db = self.fake.SessionLocal()
        try:
            remaining = (
                db.query(FeedItem)
                .filter(FeedItem.SUBSCRIPTION_ID == self.sub_id)
                .order_by(FeedItem.id)
                .all()
            )
            # KEEP=1 でも配送 N=3 件ぶんは残る
            self.assertEqual(
                [it.GUID for it in remaining], ["g-3", "g-4", "g-5"],
            )
        finally:
            db.close()


class FeedDeliveryTest(unittest.TestCase):
    """カーソル配送 (deliver_new_items) を実 SAIMemory adapter で検証する。"""

    PERSONA_ID = "tester"

    def setUp(self):
        self.engine, self.fake = _make_fake_manager()
        self.addCleanup(self.engine.dispose)
        self.fm = FeedManager(self.fake)
        fixture = self.fm.create_feed_fixture(BUILDING_ID, "新聞スタンド")
        self.fixture_id = fixture.FIXTURE_ID
        sub = self.fm.add_subscription(
            self.fixture_id, "https://example.com/feed.xml", title="テストフィード",
        )
        self.sub_id = sub.SUBSCRIPTION_ID

        # 実 SAIMemory adapter (tmp ディレクトリ + DummyEmbedder)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._cleanup_tmp)
        persona_path = Path(self._tmp.name) / "personas" / self.PERSONA_ID
        persona_path.mkdir(parents=True, exist_ok=True)
        os.environ["SAIMEMORY_MEMORY"] = "1"
        self.addCleanup(lambda: os.environ.pop("SAIMEMORY_MEMORY", None))
        patcher = patch("saiverse_memory.adapter.Embedder", DummyEmbedder)
        self.addCleanup(patcher.stop)
        patcher.start()
        from saiverse_memory import SAIMemoryAdapter
        self.adapter = SAIMemoryAdapter(
            self.PERSONA_ID, persona_dir=persona_path, resource_id=self.PERSONA_ID,
        )
        self.addCleanup(self.adapter.close)

        persona = SimpleNamespace(
            sai_memory=self.adapter, current_building_id=BUILDING_ID,
        )
        self.fake.personas = {self.PERSONA_ID: persona}
        self.fake.occupants = {BUILDING_ID: [self.PERSONA_ID, "user_someone"]}

    def _cleanup_tmp(self):
        import gc
        gc.collect()
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _seed_items(self, count):
        db = self.fake.SessionLocal()
        try:
            for i in range(1, count + 1):
                db.add(FeedItem(
                    SUBSCRIPTION_ID=self.sub_id,
                    GUID=f"g-{i}",
                    TITLE=f"記事{i}",
                    SUMMARY=f"概要{i}",
                    LINK=f"https://example.com/a{i}",
                    PUBLISHED_AT=datetime(2026, 7, 1, 9, i),
                ))
            db.commit()
        finally:
            db.close()

    def _pending(self):
        from sai_memory.perception_buffer import list_pending
        return list_pending(self.adapter.conn)

    def _cursor(self):
        db = self.fake.SessionLocal()
        try:
            return db.query(FeedReadCursor).filter(
                FeedReadCursor.PERSONA_ID == self.PERSONA_ID,
                FeedReadCursor.SUBSCRIPTION_ID == self.sub_id,
            ).first()
        finally:
            db.close()

    def test_delivery_selects_newest_and_cursor_covers_all_candidates(self):
        """候補のうち最も新しい N 件を時系列昇順で投入し、カーソルは候補全体の
        末尾へ前進する (古い候補は正直にスキップ — intent §13)。"""
        self._seed_items(5)
        os.environ["SAIVERSE_FEED_MAX_ITEMS_PER_PUSH"] = "3"
        self.addCleanup(
            lambda: os.environ.pop("SAIVERSE_FEED_MAX_ITEMS_PER_PUSH", None)
        )

        # 最も新しい 3 件 (記事3,4,5) が時系列昇順で届く。記事1,2 はスキップ
        self.assertEqual(self.fm.deliver_new_items(), 3)
        pending = self._pending()
        self.assertEqual(len(pending), 3)
        self.assertTrue(all(it.kind == "feed" for it in pending))
        self.assertIn("『テストフィード』の新着記事: 記事3", pending[0].content)
        self.assertIn("概要3", pending[0].content)
        self.assertIn("(リンク: https://example.com/a3)", pending[0].content)
        self.assertIn("記事4", pending[1].content)
        self.assertIn("記事5", pending[2].content)
        # カーソルは候補全体 (1〜5) の max(id)
        self.assertEqual(self._cursor().LAST_ITEM_ID, 5)

        # 2 回目: 新着なし → 何も届かない (スキップ分が蒸し返されない)
        self.assertEqual(self.fm.deliver_new_items(), 0)
        self.assertEqual(len(self._pending()), 3)

    def test_newest_first_feed_initial_delivery(self):
        """F1 回帰: 「id 昇順 = 新しい → 古い」(新着順フィードの初回取り込み)
        でも最新 N 件が届き、カーソルは全候補の末尾へ前進する。"""
        db = self.fake.SessionLocal()
        try:
            # id が若いほど新しい (published が新しい) 並びで 5 件保存
            for i in range(1, 6):
                db.add(FeedItem(
                    SUBSCRIPTION_ID=self.sub_id,
                    GUID=f"g-{i}",
                    TITLE=f"記事{i}",
                    SUMMARY=f"概要{i}",
                    LINK=f"https://example.com/a{i}",
                    PUBLISHED_AT=datetime(2026, 7, 1, 9, 60 - i * 10),
                ))
            db.commit()
        finally:
            db.close()
        os.environ["SAIVERSE_FEED_MAX_ITEMS_PER_PUSH"] = "3"
        self.addCleanup(
            lambda: os.environ.pop("SAIVERSE_FEED_MAX_ITEMS_PER_PUSH", None)
        )

        # 最新 3 件 = 記事1 (9:50), 記事2 (9:40), 記事3 (9:30)。
        # 提示は時系列昇順: 記事3 → 記事2 → 記事1
        self.assertEqual(self.fm.deliver_new_items(), 3)
        pending = self._pending()
        self.assertEqual(len(pending), 3)
        self.assertIn("記事3", pending[0].content)
        self.assertIn("記事2", pending[1].content)
        self.assertIn("記事1", pending[2].content)
        # カーソルは全候補の max(id) — 最新記事 (id=1) が永久に漏れない
        self.assertEqual(self._cursor().LAST_ITEM_ID, 5)

        # 次 tick: 新着ゼロなら何も届かない
        self.assertEqual(self.fm.deliver_new_items(), 0)
        self.assertEqual(len(self._pending()), 3)

    def test_prune_of_max_id_row_does_not_orphan_future_items(self):
        """二十巡目 V1 回帰: 剪定 (published 順) が最大 id の行を消した後の
        新規 INSERT が旧 id を再利用しない (AUTOINCREMENT で単調継続) —
        再利用されるとカーソル (id > LAST_ITEM_ID) から新着が永久に漏れる。"""
        os.environ["SAIVERSE_FEED_MAX_ITEMS_PER_PUSH"] = "2"
        os.environ["SAIVERSE_FEED_ITEM_KEEP"] = "2"
        self.addCleanup(
            lambda: os.environ.pop("SAIVERSE_FEED_MAX_ITEMS_PER_PUSH", None)
        )
        self.addCleanup(
            lambda: os.environ.pop("SAIVERSE_FEED_ITEM_KEEP", None)
        )
        db = self.fake.SessionLocal()
        try:
            # newest-first 初回取り込み: 若い id ほど published が新しい
            for i in range(1, 4):
                db.add(FeedItem(
                    SUBSCRIPTION_ID=self.sub_id,
                    GUID=f"g-{i}",
                    TITLE=f"記事{i}",
                    SUMMARY="",
                    LINK="",
                    PUBLISHED_AT=datetime(2026, 8, 3, 12 - i, 0),
                ))
            db.commit()
        finally:
            db.close()
        # 配送: 最新 2 件 (記事1,2)。カーソルは候補全体の max(id) = 3
        self.assertEqual(self.fm.deliver_new_items(), 2)
        self.assertEqual(self._cursor().LAST_ITEM_ID, 3)
        # 剪定は published 順で記事1,2 を残す = 最大 id (3) の行が消える
        self.assertEqual(self.fm._prune_old_items(), 1)
        # 新着 1 件 — AUTOINCREMENT により id=4 (3 の再利用ではない)
        db = self.fake.SessionLocal()
        try:
            db.add(FeedItem(
                SUBSCRIPTION_ID=self.sub_id,
                GUID="g-new",
                TITLE="新着記事",
                SUMMARY="",
                LINK="",
                PUBLISHED_AT=datetime(2026, 8, 3, 13, 0),
            ))
            db.commit()
            new_id = db.query(FeedItem.id).filter(
                FeedItem.GUID == "g-new"
            ).scalar()
        finally:
            db.close()
        self.assertEqual(new_id, 4)
        # 配送が新着を拾う (id=3 を再利用していたらカーソル 3 に隠れて
        # 永久に届かなかった)
        self.assertEqual(self.fm.deliver_new_items(), 1)
        self.assertIn("新着記事", self._pending()[-1].content)
        self.assertEqual(self._cursor().LAST_ITEM_ID, 4)

    def test_subscription_deleted_mid_delivery_skips_commit_and_push(self):
        """K6: 配送処理中 (候補選定後・カーソル commit 前) に購読が削除される
        と、カーソル commit も知覚投入も行われない — 削除済み購読名義の知覚
        投入と、道連れ削除済みカーソルの復活 INSERT を防ぐ。残余窓の受容は
        _deliver_subscription_to_persona docstring「購読削除との並走」参照。"""
        self._seed_items(2)
        outer = self

        class DeletingPersona:
            """current_building_id の参照 (カーソル commit 直前の現在地
            再確認) をフックし、その瞬間に購読を削除する — 現在地確認と
            生存確認の間に削除が並走する並びを決定的に再現する。"""

            def __init__(self, adapter):
                self.sai_memory = adapter

            @property
            def current_building_id(self):
                outer.fm.remove_subscription(outer.sub_id)
                return BUILDING_ID

        self.fake.personas = {self.PERSONA_ID: DeletingPersona(self.adapter)}
        self.assertEqual(self.fm.deliver_new_items(), 0)
        self.assertEqual(len(self._pending()), 0)  # 知覚投入なし
        self.assertIsNone(self._cursor())  # カーソルも復活していない

    def test_db_error_isolated_per_subscription(self):
        """S1 (十四巡目): 配送 1 件の DB 例外 (スナップショット競合等) は
        購読単位で隔離 (WARNING + その購読の書き込み全スキップ) — cycle
        全体の例外経路に漏らさず、他の購読への配送は続く。"""
        from sqlalchemy.exc import OperationalError
        sub2 = self.fm.add_subscription(
            self.fixture_id, "https://example.org/feed2.xml", title="第二フィード",
        )
        db = self.fake.SessionLocal()
        try:
            db.add(FeedItem(
                SUBSCRIPTION_ID=self.sub_id, GUID="g-crash", TITLE="壊れる方",
            ))
            db.add(FeedItem(
                SUBSCRIPTION_ID=sub2.SUBSCRIPTION_ID, GUID="g-alive",
                TITLE="生き残る方",
            ))
            db.commit()
        finally:
            db.close()

        real = self.fm._deliver_subscription_to_persona

        def flaky(**kwargs):
            if kwargs["subscription_id"] == self.sub_id:
                raise OperationalError(
                    "UPDATE feed_read_cursor ...", {},
                    Exception("simulated snapshot conflict"),
                )
            return real(**kwargs)

        with patch.object(
            self.fm, "_deliver_subscription_to_persona", side_effect=flaky,
        ):
            delivered = self.fm.deliver_new_items()  # 例外は漏れてこない
        self.assertEqual(delivered, 1)
        pending = self._pending()
        self.assertEqual(len(pending), 1)
        self.assertIn("生き残る方", pending[0].content)

    def test_idempotent_guard_prevents_duplicate_push(self):
        self._seed_items(2)
        self.assertEqual(self.fm.deliver_new_items(), 2)
        self.assertEqual(len(self._pending()), 2)

        # カーソルが何らかの理由で巻き戻っても、未消費バッファに同じ記事が
        # あれば再投入しない (metadata の冪等キー照合)
        db = self.fake.SessionLocal()
        try:
            cursor = db.query(FeedReadCursor).filter(
                FeedReadCursor.PERSONA_ID == self.PERSONA_ID,
            ).first()
            cursor.LAST_ITEM_ID = 0
            db.commit()
        finally:
            db.close()
        self.assertEqual(self.fm.deliver_new_items(), 0)
        self.assertEqual(len(self._pending()), 2)

    def test_idempotent_marker_with_special_char_guid(self):
        """F2 回帰: ダブルクォート/バックスラッシュ/LIKE ワイルドカード入りの
        GUID でも冪等 marker (sha256 hex) が機能する。"""
        db = self.fake.SessionLocal()
        try:
            db.add(FeedItem(
                SUBSCRIPTION_ID=self.sub_id,
                GUID='gu"id\\with%special_chars',
                TITLE="特殊記事",
                SUMMARY="",
                LINK="",
                PUBLISHED_AT=datetime(2026, 7, 1, 9, 0),
            ))
            db.commit()
        finally:
            db.close()
        self.assertEqual(self.fm.deliver_new_items(), 1)
        self.assertEqual(len(self._pending()), 1)

        # カーソルを巻き戻しても再投入されない
        db = self.fake.SessionLocal()
        try:
            cursor = db.query(FeedReadCursor).filter(
                FeedReadCursor.PERSONA_ID == self.PERSONA_ID,
            ).first()
            cursor.LAST_ITEM_ID = 0
            db.commit()
        finally:
            db.close()
        self.assertEqual(self.fm.deliver_new_items(), 0)
        self.assertEqual(len(self._pending()), 1)

    def test_cursor_advances_even_when_push_fails(self):
        """F2: カーソル前進が知覚投入より先に耐久化される — 投入失敗時は
        当該分が欠落する (重複より欠落に倒す)。"""
        self._seed_items(2)
        with patch.object(
            self.adapter, "push_perception", side_effect=RuntimeError("boom"),
        ):
            self.assertEqual(self.fm.deliver_new_items(), 0)
        self.assertEqual(len(self._pending()), 0)  # 投入は失敗
        self.assertEqual(self._cursor().LAST_ITEM_ID, 2)  # カーソルは前進済み

    def test_pending_limit_blocks_delivery_and_cursor(self):
        """F5: 未消費の feed 知覚が上限以上なら投入もカーソル前進もしない。"""
        for i in range(10):  # 既定上限 (SAIVERSE_FEED_MAX_PENDING=10) まで積む
            self.adapter.push_perception(kind="feed", content=f"既存の知覚{i}")
        self._seed_items(2)
        self.assertEqual(self.fm.deliver_new_items(), 0)
        self.assertIsNone(self._cursor())  # カーソルは前進しない
        self.assertEqual(len(self._pending()), 10)

    def test_pending_budget_shared_across_subscriptions(self):
        """G2: pending 上限は購読横断のペルソナ単位共有予算 — 残り枠 1 なら
        購読が 2 本あっても合計 1 件しか投入されない。予算を使い切った後の
        購読はカーソルも据え置き。"""
        sub2 = self.fm.add_subscription(
            self.fixture_id, "https://example.com/feed2.xml", title="第二フィード",
        )
        # 既定上限 10 に対し 9 件積む → 残り枠 1
        for i in range(9):
            self.adapter.push_perception(kind="feed", content=f"既存の知覚{i}")
        self._seed_items(3)  # 購読 1 に 3 件
        db = self.fake.SessionLocal()
        try:
            for i in range(1, 4):  # 購読 2 にも 3 件
                db.add(FeedItem(
                    SUBSCRIPTION_ID=sub2.SUBSCRIPTION_ID,
                    GUID=f"h-{i}",
                    TITLE=f"第二記事{i}",
                    SUMMARY="",
                    LINK="",
                    PUBLISHED_AT=datetime(2026, 7, 1, 10, i),
                ))
            db.commit()
        finally:
            db.close()

        self.assertEqual(self.fm.deliver_new_items(), 1)
        self.assertEqual(len(self._pending()), 10)
        # 予算切れで見送った購読はカーソルも動かない (次の機会に届く)
        db = self.fake.SessionLocal()
        try:
            self.assertEqual(db.query(FeedReadCursor).count(), 1)
        finally:
            db.close()

    def test_zero_max_items_disables_delivery(self):
        """G3: SAIVERSE_FEED_MAX_ITEMS_PER_PUSH=0 は配送無効 — 記事が
        積まれても知覚投入もカーソル前進もしない。"""
        os.environ["SAIVERSE_FEED_MAX_ITEMS_PER_PUSH"] = "0"
        self.addCleanup(
            lambda: os.environ.pop("SAIVERSE_FEED_MAX_ITEMS_PER_PUSH", None)
        )
        self._seed_items(3)
        self.assertEqual(self.fm.deliver_new_items(), 0)
        self.assertIsNone(self._cursor())
        self.assertEqual(len(self._pending()), 0)

    def test_null_published_presented_last(self):
        """G6: 日付なし (PUBLISHED_AT=NULL) 記事は提示の末尾 — 日付あり記事を
        時系列昇順で先に並べ、いつのものか不明な記事を最後に置く。"""
        db = self.fake.SessionLocal()
        try:
            db.add(FeedItem(
                SUBSCRIPTION_ID=self.sub_id, GUID="d-1", TITLE="日付あり早い",
                SUMMARY="", LINK="", PUBLISHED_AT=datetime(2026, 7, 1, 9, 10),
            ))
            db.add(FeedItem(
                SUBSCRIPTION_ID=self.sub_id, GUID="n-1", TITLE="日付なし記事",
                SUMMARY="", LINK="", PUBLISHED_AT=None,
            ))
            db.add(FeedItem(
                SUBSCRIPTION_ID=self.sub_id, GUID="d-2", TITLE="日付あり遅い",
                SUMMARY="", LINK="", PUBLISHED_AT=datetime(2026, 7, 1, 9, 20),
            ))
            db.commit()
        finally:
            db.close()
        self.assertEqual(self.fm.deliver_new_items(), 3)
        pending = self._pending()
        self.assertIn("日付あり早い", pending[0].content)
        self.assertIn("日付あり遅い", pending[1].content)
        self.assertIn("日付なし記事", pending[2].content)

    def test_content_is_framed_as_external_reprint(self):
        """F6: 知覚 content の冒頭に転載の枠づけ一行が付く。"""
        self._seed_items(1)
        self.assertEqual(self.fm.deliver_new_items(), 1)
        pending = self._pending()
        self.assertTrue(
            pending[0].content.startswith("(外部サイトの記事の転載)\n")
        )

    def test_departed_persona_is_skipped(self):
        """F10: occupants に残っていても現在地が違うペルソナには配送しない。"""
        self._seed_items(2)
        self.fake.personas[self.PERSONA_ID].current_building_id = "elsewhere"
        self.assertEqual(self.fm.deliver_new_items(), 0)
        self.assertIsNone(self._cursor())
        self.assertEqual(len(self._pending()), 0)

    def test_move_during_delivery_stops_remaining_subscriptions(self):
        """J5: 配送サイクルの途中で移動したペルソナには、以降の購読の配送も
        カーソル前進もしない — 現在地の確認はカーソル commit 直前で行う。"""
        sub2 = self.fm.add_subscription(
            self.fixture_id, "https://example.com/feed2.xml", title="第二フィード",
        )
        self._seed_items(1)  # 購読 1 に 1 件
        db = self.fake.SessionLocal()
        try:
            db.add(FeedItem(
                SUBSCRIPTION_ID=sub2.SUBSCRIPTION_ID, GUID="h-1",
                TITLE="第二記事", SUMMARY="", LINK="",
                PUBLISHED_AT=datetime(2026, 7, 1, 10, 0),
            ))
            db.commit()
        finally:
            db.close()

        persona = self.fake.personas[self.PERSONA_ID]
        real_push = self.adapter.push_perception

        def push_and_move(**kwargs):
            result = real_push(**kwargs)
            persona.current_building_id = "elsewhere"  # 最初の投入直後に移動
            return result

        with patch.object(
            self.adapter, "push_perception", side_effect=push_and_move,
        ):
            delivered = self.fm.deliver_new_items()

        # 先に処理された購読 1 本だけ届き、以降の購読は投入もカーソルも無し
        self.assertEqual(delivered, 1)
        self.assertEqual(len(self._pending()), 1)
        db = self.fake.SessionLocal()
        try:
            self.assertEqual(db.query(FeedReadCursor).count(), 1)
        finally:
            db.close()

    def test_adapter_not_ready_skips_without_cursor_advance(self):
        self._seed_items(2)
        not_ready = SimpleNamespace(is_ready=lambda: False)
        self.fake.personas = {
            self.PERSONA_ID: SimpleNamespace(
                sai_memory=not_ready, current_building_id=BUILDING_ID,
            ),
        }
        self.assertEqual(self.fm.deliver_new_items(), 0)
        self.assertIsNone(self._cursor())  # カーソルは前進しない

    def test_cursor_is_per_persona(self):
        """既読カーソルはペルソナの持ち物 (intent 不変条件 3)。"""
        self._seed_items(1)
        self.assertEqual(self.fm.deliver_new_items(), 1)
        # 同じ Building の別ペルソナには別カーソルで独立に配送される
        other = SimpleNamespace(sai_memory=self.adapter)  # バッファ共有だが記事は別 guid 照合
        db = self.fake.SessionLocal()
        try:
            rows = db.query(FeedReadCursor).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].PERSONA_ID, self.PERSONA_ID)
        finally:
            db.close()
        del other

    def test_feed_kind_has_header(self):
        from sai_memory.perception_buffer import format_perception_message
        self._seed_items(1)
        self.fm.deliver_new_items()
        text = format_perception_message(self._pending())
        self.assertIn("[フィード]", text)


# ---------------------------------------------------------------------------
# SAIVerseManager.shutdown との結線
# ---------------------------------------------------------------------------

class ManagerShutdownTest(unittest.TestCase):
    def test_shutdown_stops_feed_manager_before_persona_teardown(self):
        """G1: shutdown 経路で FeedManager.stop が呼ばれ、しかも persona 保存
        (teardown) より前であること。shutdown 本体を偽マネージャで実行する。"""
        import threading
        from saiverse.saiverse_manager import SAIVerseManager

        order: list = []
        fake = SimpleNamespace(
            state=SimpleNamespace(user_presence_status="offline"),
            sds_stop_event=threading.Event(),
            db_polling_stop_event=threading.Event(),
            event_scheduler=None,
            conversation_managers={},
            feed_manager=SimpleNamespace(stop=lambda: order.append("feed_stop")),
            personas={
                "p1": SimpleNamespace(
                    _save_session_metadata=lambda: order.append("persona_save"),
                ),
            },
            city_id="c1",
            city_name="test_city",
            _emit_trigger=lambda *a, **kw: None,
            _save_modified_buildings=lambda: None,
        )
        SAIVerseManager.shutdown(fake)
        self.assertIn("feed_stop", order)
        self.assertIn("persona_save", order)
        self.assertLess(order.index("feed_stop"), order.index("persona_save"))


# ---------------------------------------------------------------------------
# ライフサイクル (H1: stop 後の worker 起動・書き込みを封じる)
# ---------------------------------------------------------------------------

class FeedManagerLifecycleTest(unittest.TestCase):
    """stop() 後は新しい worker が起動せず、実行中の worker も配送前に止まる。"""

    def setUp(self):
        self.engine, self.fake = _make_fake_manager()
        self.addCleanup(self.engine.dispose)
        self.fm = FeedManager(self.fake)

    def test_safe_tick_after_stop_does_not_start_worker(self):
        """停止後に届いた周期 callback は worker を起動しない (no-op)。"""
        self.fm.stop()
        with patch.object(self.fm, "_start_worker") as start_worker:
            self.fm._safe_tick()
        start_worker.assert_not_called()

    def test_fetch_now_after_stop_returns_none(self):
        """停止後の手動取得は起動せず None (API 層は 409)。"""
        self.fm.stop()
        self.assertIsNone(self.fm.fetch_now())

    def test_start_after_stop_is_noop(self):
        """I2: stopped からの start() は再登録しない no-op (FeedManager は
        SAIVerseManager と同寿命の単回使用)。再 start を許すと stop() の
        join 待ち中に _stop_event が clear され旧 worker が続行する。"""
        calls = []
        self.fake.event_scheduler = SimpleNamespace(
            schedule_periodic=lambda **kw: calls.append(kw),
            cancel=lambda key: None,
        )
        self.fm.stop()
        self.fm.start()
        self.assertEqual(calls, [])  # 周期登録が走っていない
        self.assertTrue(self.fm._stop_event.is_set())  # 中断要求が消えていない

    def test_double_start_registers_once(self):
        """start() の二重呼び出しは 2 回目が no-op (周期登録は 1 回だけ)。"""
        calls = []
        self.fake.event_scheduler = SimpleNamespace(
            schedule_periodic=lambda **kw: calls.append(kw),
            cancel=lambda key: None,
        )
        self.fm.start()
        self.fm.start()
        self.assertEqual(len(calls), 1)

    def test_stop_during_fetch_skips_display_and_delivery(self):
        """遅い fetch の最中に stop() → fetch 完了後の表示更新・配送は走らない
        (停止後の DB / 知覚バッファ書き込みを防ぐ)。"""
        fetch_started = threading.Event()
        release_fetch = threading.Event()
        calls = []

        def slow_fetch_all():
            fetch_started.set()
            release_fetch.wait(timeout=10)

        with patch.object(self.fm, "_fetch_all", side_effect=slow_fetch_all), \
             patch.object(
                 self.fm, "_update_all_fixture_displays",
                 side_effect=lambda: calls.append("display"),
             ), \
             patch.object(
                 self.fm, "deliver_new_items",
                 side_effect=lambda: calls.append("deliver"),
             ):
            worker = self.fm.fetch_now()
            self.assertIsNotNone(worker)
            self.assertTrue(fetch_started.wait(timeout=10))
            # stop() は worker の join まで行うので別スレッドから呼び、
            # 中断要求が立ったのを確認してから fetch を進める
            stopper = threading.Thread(target=self.fm.stop, daemon=True)
            stopper.start()
            self.assertTrue(self.fm._stop_event.wait(timeout=10))
            release_fetch.set()
            worker.join(timeout=10)
            stopper.join(timeout=15)
        self.assertFalse(worker.is_alive())
        self.assertEqual(calls, [])  # 表示更新も配送も呼ばれていない


# ---------------------------------------------------------------------------
# Fixture.STATE_JSON の書き手直列化 (H2)
# ---------------------------------------------------------------------------

class FixtureStateJsonMergeTest(unittest.TestCase):
    """STATE_JSON の 2 つの書き手 (record_metrics / update_fixture_display) が
    互いのキーを消さない — json_set の単文 UPDATE (update_fixture_state_keys)
    によるキー単位の原子更新 (十五巡目 U1 で lock + 楽観 CAS から置換)。"""

    OBSERVER_ID = "obs-1"

    def setUp(self):
        self.engine, self.fake = _make_fake_manager()
        self.addCleanup(self.engine.dispose)
        self.fm = FeedManager(self.fake)
        fixture = self.fm.create_feed_fixture(BUILDING_ID, "新聞スタンド")
        self.fixture_id = fixture.FIXTURE_ID
        self.fm.add_subscription(
            self.fixture_id, "https://example.com/feed.xml", title="テストフィード",
        )
        from database.models import ObserverConfig
        db = self.fake.SessionLocal()
        try:
            db.add(ObserverConfig(
                OBSERVER_ID=self.OBSERVER_ID, FIXTURE_ID=self.fixture_id,
                ENABLED=True, EXEC_KIND="push",
            ))
            db.commit()
        finally:
            db.close()

    def _state(self):
        from database.models import Fixture
        db = self.fake.SessionLocal()
        try:
            fixture = db.query(Fixture).filter(
                Fixture.FIXTURE_ID == self.fixture_id
            ).first()
            return json.loads(fixture.STATE_JSON) if fixture.STATE_JSON else {}
        finally:
            db.close()

    def test_alternating_writers_keep_each_others_keys(self):
        om = self.fake.observer_manager
        self.fm.update_fixture_display(self.fixture_id)
        om.record_metrics(self.OBSERVER_ID, {"temperature": {"value_num": 21.5}})
        state = self._state()
        self.assertIn("feed_stand", state)  # metrics 書き込みで消えない
        self.assertIn("temperature", state)
        # もう一巡交互に書いても、他方のキーが生き残る
        self.fm.update_fixture_display(self.fixture_id)
        om.record_metrics(self.OBSERVER_ID, {"temperature": {"value_num": 22.0}})
        state = self._state()
        self.assertEqual(state["temperature"]["value_num"], 22.0)
        self.assertEqual(state["feed_stand"]["subscriptions"], ["テストフィード"])

    def test_record_metrics_disabled_observer_writes_nothing(self):
        """無効化済み observer への record_metrics は履歴 (observer_metrics)
        も STATE_JSON も書かない — 設定確認と書き込みは同一 transaction
        (旧 lock 方式の「lock 待ち中の無効化」テストの後継)。"""
        from database.models import ObserverConfig, ObserverMetric
        db = self.fake.SessionLocal()
        try:
            cfg = db.query(ObserverConfig).filter(
                ObserverConfig.OBSERVER_ID == self.OBSERVER_ID
            ).first()
            cfg.ENABLED = False
            db.commit()
        finally:
            db.close()
        recorded = self.fake.observer_manager.record_metrics(
            self.OBSERVER_ID, {"temperature": {"value_num": 9.9}},
        )
        self.assertEqual(recorded, [])
        self.assertNotIn("temperature", self._state())
        db = self.fake.SessionLocal()
        try:
            self.assertEqual(db.query(ObserverMetric).count(), 0)
        finally:
            db.close()

    def test_reserved_metric_name_feed_stand_rejected(self):
        """U2 (十五巡目): metric 名 "feed_stand" は STATE_JSON トップレベルの
        予約キー (フィード表示の書き手の領域) — WARNING + skip で拒否され、
        履歴にも書かれず、フィード表示は無傷。同じ呼び出しの正当な metric は
        通常どおり記録される。"""
        from database.models import ObserverMetric
        self.fm.update_fixture_display(self.fixture_id)
        recorded = self.fake.observer_manager.record_metrics(
            self.OBSERVER_ID,
            {
                "feed_stand": {"value_text": "偽装表示"},
                "temperature": {"value_num": 21.5},
            },
        )
        self.assertEqual(len(recorded), 1)
        state = self._state()
        # フィード表示が metric に上書きされていない
        self.assertEqual(state["feed_stand"]["subscriptions"], ["テストフィード"])
        self.assertEqual(state["temperature"]["value_num"], 21.5)
        db = self.fake.SessionLocal()
        try:
            names = [m.METRIC_NAME for m in db.query(ObserverMetric).all()]
        finally:
            db.close()
        self.assertEqual(names, ["temperature"])

    def test_unsafe_metric_name_rejected_before_json_path(self):
        """U1 (十五巡目): json_set の JSON パスに安全に埋め込めない metric 名
        ($ / . / " / 空白等) は WARNING + skip — パス注入で他キーを触る口を
        名前の関所で塞ぐ。同じ呼び出しの正当な metric は通る。"""
        from database.models import ObserverMetric
        recorded = self.fake.observer_manager.record_metrics(
            self.OBSERVER_ID,
            {
                "$.hijack": {"value_num": 1.0},
                'a"b': {"value_num": 2.0},
                "dotted.name": {"value_num": 3.0},
                "ok_metric-1": {"value_num": 4.0},
            },
        )
        self.assertEqual(len(recorded), 1)
        state = self._state()
        self.assertEqual(list(state.keys()), ["ok_metric-1"])
        self.assertEqual(state["ok_metric-1"]["value_num"], 4.0)
        db = self.fake.SessionLocal()
        try:
            names = [m.METRIC_NAME for m in db.query(ObserverMetric).all()]
        finally:
            db.close()
        self.assertEqual(names, ["ok_metric-1"])

    def test_corrupt_state_json_rebuilt_as_object(self):
        """既存の STATE_JSON が壊れた JSON / 非オブジェクトでも json_set 更新は
        エラーにせず '{}' から作り直す (壊れ値の自己修復 — json_set は壊れた
        JSON を渡すと文ごと失敗するため、土台の CASE ガードが要る)。"""
        from database.models import Fixture
        for corrupt in ("これはJSONではない", "[1, 2, 3]"):
            db = self.fake.SessionLocal()
            try:
                fx = db.query(Fixture).filter(
                    Fixture.FIXTURE_ID == self.fixture_id
                ).first()
                fx.STATE_JSON = corrupt
                db.commit()
            finally:
                db.close()
            self.fake.observer_manager.record_metrics(
                self.OBSERVER_ID, {"temperature": {"value_num": 7.0}},
            )
            state = self._state()
            self.assertEqual(
                state["temperature"]["value_num"], 7.0,
                msg=f"corrupt value: {corrupt!r}",
            )

    def test_update_fixture_state_keys_missing_row_returns_false(self):
        """対象行が無ければ False (静かに skip — 旧 CAS ヘルパと同じ契約)。"""
        from database.models import Fixture
        from saiverse.observer_manager import update_fixture_state_keys
        db = self.fake.SessionLocal()
        try:
            ok = update_fixture_state_keys(
                db, (Fixture.FIXTURE_ID == "no-such-fixture",),
                {"temperature": {"value_num": 1.0}}, context="test missing",
            )
            db.commit()
        finally:
            db.close()
        self.assertFalse(ok)

    def test_update_fixture_state_keys_rejects_unsafe_key(self):
        """update_fixture_state_keys 自身も不正キーを ValueError で拒む —
        呼び出し側の検証をすり抜けた場合の最終関所 (パス注入の構造的封じ)。"""
        from database.models import Fixture
        from saiverse.observer_manager import update_fixture_state_keys
        db = self.fake.SessionLocal()
        try:
            with self.assertRaises(ValueError):
                update_fixture_state_keys(
                    db, (Fixture.FIXTURE_ID == self.fixture_id,),
                    {'$."x"': 1}, context="test unsafe key",
                )
        finally:
            db.close()

    def test_concurrent_writers_keep_both_keys(self):
        """新方式の意味論 (十五巡目 U1): lock なしの並行書き込みでも json_set
        の単文 UPDATE はキー単位に原子的で、両者のキーが共存する — 旧
        lock 直列化 / CAS 衝突リトライテストの後継。file DB + 実スレッドで
        プロセス内並走を実機再現する (単文更新はプロセス跨ぎも同じ文が守る)。"""
        from database.models import Fixture, ObserverConfig
        tmp = tempfile.TemporaryDirectory()

        def _cleanup_tmp():
            import gc
            gc.collect()
            try:
                tmp.cleanup()
            except PermissionError:
                pass  # Windows: sqlite ハンドル解放待ちの既知事情
        self.addCleanup(_cleanup_tmp)
        db_file = Path(tmp.name) / "concurrent.db"
        engine, fake = _make_fake_manager(f"sqlite:///{db_file}")
        self.addCleanup(engine.dispose)
        fm = FeedManager(fake)
        fixture = fm.create_feed_fixture(BUILDING_ID, "並走スタンド")
        fixture_id = fixture.FIXTURE_ID
        fm.add_subscription(
            fixture_id, "https://example.com/feed.xml", title="並走フィード",
        )
        db = fake.SessionLocal()
        try:
            db.add(ObserverConfig(
                OBSERVER_ID="obs-c", FIXTURE_ID=fixture_id,
                ENABLED=True, EXEC_KIND="push",
            ))
            db.commit()
        finally:
            db.close()

        errors: list = []

        def metrics_writer():
            try:
                for i in range(10):
                    fake.observer_manager.record_metrics(
                        "obs-c", {"temperature": {"value_num": float(i)}},
                    )
            except Exception as exc:  # noqa: BLE001 — スレッド内の失敗を主スレッドへ運ぶ
                errors.append(exc)

        def display_writer():
            try:
                for _ in range(10):
                    fm.update_fixture_display(fixture_id)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=metrics_writer, daemon=True),
            threading.Thread(target=display_writer, daemon=True),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertEqual(errors, [])
        db = fake.SessionLocal()
        try:
            fx = db.query(Fixture).filter(
                Fixture.FIXTURE_ID == fixture_id
            ).first()
            state = json.loads(fx.STATE_JSON)
        finally:
            db.close()
        # 両者の最終書き込みが互いを消していない
        self.assertEqual(state["temperature"]["value_num"], 9.0)
        self.assertEqual(state["feed_stand"]["subscriptions"], ["並走フィード"])

    def test_create_fixture_upsert_without_state_json_keeps_existing(self):
        """J2: 既存 fixture への upsert (state_json 省略 = None) で既存の
        STATE_JSON (他の書き手の feed_stand / metrics キー) が消えない —
        省略は「変更しない」の upsert 意味論。他フィールドは従来どおり更新。"""
        from database.models import Fixture
        self.fm.update_fixture_display(self.fixture_id)
        self.assertIn("feed_stand", self._state())

        self.fake.observer_manager.create_fixture(
            self.fixture_id, BUILDING_ID, "改名スタンド",
            fixture_type="feed_stand", description="説明も更新",
        )
        state = self._state()
        self.assertIn("feed_stand", state)  # 表示情報が None で消えていない
        db = self.fake.SessionLocal()
        try:
            fixture = db.query(Fixture).filter(
                Fixture.FIXTURE_ID == self.fixture_id
            ).first()
            self.assertEqual(fixture.NAME, "改名スタンド")
            self.assertEqual(fixture.DESCRIPTION, "説明も更新")
        finally:
            db.close()

    def test_create_fixture_upsert_with_explicit_state_json_overwrites(self):
        """J2: state_json を明示指定した upsert は従来どおり全置換する。"""
        self.fm.update_fixture_display(self.fixture_id)
        self.assertIn("feed_stand", self._state())

        self.fake.observer_manager.create_fixture(
            self.fixture_id, BUILDING_ID, "新聞スタンド",
            fixture_type="feed_stand",
            state_json=json.dumps({"x": 1}),
        )
        self.assertEqual(self._state(), {"x": 1})


# ---------------------------------------------------------------------------
# feed_presets (三層ローダ)
# ---------------------------------------------------------------------------

class FeedPresetsTest(unittest.TestCase):
    def tearDown(self):
        # グローバルキャッシュをディスクの実状態に戻す
        from saiverse import feed_presets
        feed_presets.reload_presets()

    def test_builtin_preset_loads(self):
        from saiverse import feed_presets
        presets = feed_presets.load_presets()
        self.assertIn("tech_news_stand", presets)
        preset = presets["tech_news_stand"]
        self.assertTrue(preset.get("builtin"))
        self.assertEqual(preset["name"], "技術ニューススタンド")
        # ラインナップ (件数・銘柄) はキュレーションで変わるデータなので
        # 構造だけを検査する (件数の焼き込みはプリセット更新のたびに壊れる)
        self.assertGreaterEqual(len(preset["feeds"]), 1)
        for feed in preset["feeds"]:
            self.assertIn("url", feed)
            self.assertIn("title", feed)
            self.assertTrue(feed["url"].startswith("https://"))

    def test_user_data_overrides_builtin(self):
        from saiverse import data_paths, feed_presets
        with tempfile.TemporaryDirectory() as tmp:
            user_dir = Path(tmp)
            (user_dir / "feeds").mkdir(parents=True)
            (user_dir / "feeds" / "tech_news_stand.json").write_text(
                json.dumps({
                    "id": "tech_news_stand",
                    "name": "上書きスタンド",
                    "feeds": [],
                }),
                encoding="utf-8",
            )
            with patch.object(data_paths, "USER_DATA_DIR", user_dir):
                presets = feed_presets.reload_presets()
        preset = presets["tech_news_stand"]
        self.assertEqual(preset["name"], "上書きスタンド")
        self.assertFalse(preset.get("builtin", False))

    def test_get_preset_missing_returns_none(self):
        from saiverse import feed_presets
        self.assertIsNone(feed_presets.get_preset("no_such_preset"))

    def test_malformed_structure_files_skipped(self):
        """O3 (十巡目): JSON 構文は通るが構造が想定外のファイル (トップレベル
        配列 / feeds に null 混入) は、消費側 (API・プリセット施設作成) が
        dict 前提で触って 500 になる前にローダで弾く。弾くのはそのファイル
        だけで、他のプリセット (builtin 含む) は巻き込まない。"""
        from saiverse import data_paths, feed_presets
        with tempfile.TemporaryDirectory() as tmp:
            user_dir = Path(tmp)
            feeds_dir = user_dir / "feeds"
            feeds_dir.mkdir(parents=True)
            (feeds_dir / "top_level_array.json").write_text(
                json.dumps([{"id": "top_level_array", "name": "配列"}]),
                encoding="utf-8",
            )
            (feeds_dir / "null_feed_entry.json").write_text(
                json.dumps({
                    "id": "null_feed_entry",
                    "name": "null入り",
                    "feeds": [None, {"url": "https://example.com/a.rss"}],
                }),
                encoding="utf-8",
            )
            (feeds_dir / "good.json").write_text(
                json.dumps({
                    "id": "good",
                    "name": "正常",
                    "feeds": [{"url": "https://example.com/ok.rss"}],
                }),
                encoding="utf-8",
            )
            with patch.object(data_paths, "USER_DATA_DIR", user_dir):
                presets = feed_presets.reload_presets()
        self.assertNotIn("top_level_array", presets)
        self.assertNotIn("null_feed_entry", presets)
        # 正常なユーザープリセットと builtin は生きている
        self.assertIn("good", presets)
        self.assertIn("tech_news_stand", presets)
        # title 欠落は "" に正規化される (消費側の .strip() 等が安全に通る)
        self.assertEqual(presets["good"]["feeds"][0]["title"], "")


class CreateFixtureFromPresetTest(unittest.TestCase):
    def setUp(self):
        self.engine, self.fake = _make_fake_manager()
        self.addCleanup(self.engine.dispose)
        self.fm = FeedManager(self.fake)

    def test_create_from_builtin_preset(self):
        fixture = self.fm.create_fixture_from_preset(BUILDING_ID, "tech_news_stand")
        self.assertEqual(fixture.TYPE, "feed_stand")
        self.assertEqual(fixture.NAME, "技術ニューススタンド")
        subs = self.fm.list_subscriptions(fixture.FIXTURE_ID)
        # 件数はプリセットの中身 (キュレーションで変わる) に一致することだけ検査
        from saiverse import feed_presets
        preset = feed_presets.get_preset("tech_news_stand")
        self.assertEqual(len(subs), len(preset["feeds"]))
        self.assertGreaterEqual(len(subs), 1)
        self.assertEqual(
            {s.TITLE for s in subs},
            {f["title"] for f in preset["feeds"]},
        )

    def test_unknown_preset_raises(self):
        with self.assertRaises(ValueError):
            self.fm.create_fixture_from_preset(BUILDING_ID, "no_such_preset")

    def _fixture_count(self):
        from database.models import Fixture
        db = self.fake.SessionLocal()
        try:
            return db.query(Fixture).count()
        finally:
            db.close()

    def _subscription_count(self):
        db = self.fake.SessionLocal()
        try:
            return db.query(FeedSubscription).count()
        finally:
            db.close()

    def test_invalid_url_rejects_whole_preset(self):
        """J3: プリセットに 1 本でも不正 URL があれば全体を ValueError で拒否
        — 施設を作る前に全 URL を検査するため、Fixture も購読も作られない。"""
        from saiverse import feed_presets
        bad_preset = {
            "id": "bad_preset",
            "name": "壊れたプリセット",
            "feeds": [
                {"url": "https://example.com/ok.rss", "title": "OK"},
                {"url": "ftp://example.com/feed", "title": "不正 scheme"},
            ],
        }
        with patch.object(
            feed_presets, "FEED_PRESETS", {"bad_preset": bad_preset},
        ):
            with self.assertRaises(ValueError):
                self.fm.create_fixture_from_preset(BUILDING_ID, "bad_preset")
        self.assertEqual(self._fixture_count(), 0)
        self.assertEqual(self._subscription_count(), 0)

    def test_overlong_url_rejects_whole_preset(self):
        """O4 (十巡目): 正規化後 512 字 (DB 宣言長) を超える URL もプリセット
        事前検証で全体拒否 — 施設を作る前に判定するので部分状態は残らない。"""
        from saiverse import feed_presets
        long_preset = {
            "id": "long_preset",
            "name": "長いURL入り",
            "feeds": [
                {"url": "https://example.com/ok.rss", "title": "OK"},
                {"url": "https://example.com/" + "a" * 512, "title": "長すぎ"},
            ],
        }
        with patch.object(
            feed_presets, "FEED_PRESETS", {"long_preset": long_preset},
        ):
            with self.assertRaises(ValueError) as ctx:
                self.fm.create_fixture_from_preset(BUILDING_ID, "long_preset")
        self.assertIn("長すぎる", str(ctx.exception))
        self.assertEqual(self._fixture_count(), 0)
        self.assertEqual(self._subscription_count(), 0)

    def test_preset_exceeding_subscription_limit_rejected_whole(self):
        """U3 (十五巡目): プリセットの購読数が上限
        (SAIVERSE_FEED_MAX_SUBSCRIPTIONS_PER_FIXTURE) を超えたら、施設を作る
        前に全か無かで拒否する — 部分状態を残さない。"""
        from saiverse import feed_presets
        from saiverse.feed_manager import FeedSubscriptionLimitError
        preset = {
            "id": "many",
            "name": "多すぎ",
            "feeds": [
                {"url": f"https://example.com/{i}.rss", "title": f"F{i}"}
                for i in range(3)
            ],
        }
        with patch.object(feed_presets, "FEED_PRESETS", {"many": preset}), \
             patch.dict(os.environ, {
                 "SAIVERSE_FEED_MAX_SUBSCRIPTIONS_PER_FIXTURE": "2",
             }):
            with self.assertRaises(FeedSubscriptionLimitError) as ctx:
                self.fm.create_fixture_from_preset(BUILDING_ID, "many")
        self.assertIn("上限", str(ctx.exception))
        self.assertEqual(self._fixture_count(), 0)
        self.assertEqual(self._subscription_count(), 0)
        # 上限ちょうどなら通る
        with patch.object(feed_presets, "FEED_PRESETS", {"many": preset}), \
             patch.dict(os.environ, {
                 "SAIVERSE_FEED_MAX_SUBSCRIPTIONS_PER_FIXTURE": "3",
             }):
            self.fm.create_fixture_from_preset(BUILDING_ID, "many")
        self.assertEqual(self._fixture_count(), 1)
        self.assertEqual(self._subscription_count(), 3)

    def test_mid_failure_leaves_no_partial_state(self):
        """J3: 購読作成の途中で失敗したら単一 transaction ごと rollback —
        「施設だけある」「購読が半分だけある」部分状態を残さない。"""
        from saiverse import feed_presets
        preset = {
            "id": "p",
            "name": "二本立て",
            "feeds": [
                {"url": "https://example.com/a.rss", "title": "A"},
                {"url": "https://example.com/b.rss", "title": "B"},
            ],
        }
        real = self.fm._add_subscription_in_session
        calls = []

        def failing(db, fixture_id, feed_url, **kwargs):
            calls.append(feed_url)
            if len(calls) == 2:
                raise RuntimeError("boom")  # 2 本目で失敗
            return real(db, fixture_id, feed_url, **kwargs)

        with patch.object(feed_presets, "FEED_PRESETS", {"p": preset}), \
             patch.object(
                 self.fm, "_add_subscription_in_session", side_effect=failing,
             ):
            with self.assertRaises(RuntimeError):
                self.fm.create_fixture_from_preset(BUILDING_ID, "p")
        self.assertEqual(len(calls), 2)  # 1 本目は成功していた
        self.assertEqual(self._fixture_count(), 0)
        self.assertEqual(self._subscription_count(), 0)


# ---------------------------------------------------------------------------
# migration (一意 index の致命化)
# ---------------------------------------------------------------------------

class FeedMigrationIndexTest(unittest.TestCase):
    """L2/L5: 一意 index の作成前に重複行を決定論で削除修復する。

    重複を例外で表明して migration を止めると Web UI が起動できず、
    「UI から削除して再実行」の案内自体が実行不能になる (七巡目裁定)。
    記事・カーソルは再取得で再生する消耗データなので削除修復が安全。
    修復後の index 作成失敗は引き続き例外 (握り潰さない)。
    """

    def _make_engine(self):
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.addCleanup(engine.dispose)
        return engine

    @staticmethod
    def _create_old_form_tables(conn):
        """UNIQUE 制約なしの旧形 3 テーブル (開発期 DB の再現)。

        feed_item / feed_read_cursor に AUTOINCREMENT が無いのも忠実な再現 —
        sqlite_autoincrement 導入 (二十巡目 V1) 前の SQLAlchemy はどちらも
        素の INTEGER PRIMARY KEY で作っていた。これにより本 class の各テストは
        「重複修復 → feed_item の AUTOINCREMENT 再構築 → 一意 index 補修」の
        実経路全体を通る。
        """
        from sqlalchemy import text
        conn.execute(text(
            'CREATE TABLE feed_subscription ('
            '"SUBSCRIPTION_ID" VARCHAR(36) PRIMARY KEY, '
            '"FIXTURE_ID" VARCHAR(36) NOT NULL, '
            '"FEED_URL" VARCHAR(512) NOT NULL, '
            '"SITE_URL" VARCHAR(512), '
            '"TITLE" VARCHAR(255) NOT NULL DEFAULT \'\', '
            '"ENABLED" BOOLEAN NOT NULL DEFAULT 1, '
            '"ETAG" VARCHAR(255), '
            '"LAST_MODIFIED" VARCHAR(255), '
            '"LAST_OK_AT" DATETIME, '
            '"LAST_ERROR" VARCHAR(512), '
            '"CONSECUTIVE_FAILURES" INTEGER NOT NULL DEFAULT 0, '
            '"CREATED_AT" DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, '
            '"UPDATED_AT" DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)'
        ))
        conn.execute(text(
            'CREATE TABLE feed_item ('
            'id INTEGER PRIMARY KEY, '
            '"SUBSCRIPTION_ID" VARCHAR(36) NOT NULL, '
            '"GUID" VARCHAR(512) NOT NULL, '
            '"TITLE" TEXT NOT NULL DEFAULT \'\', '
            '"SUMMARY" TEXT NOT NULL DEFAULT \'\', '
            '"LINK" VARCHAR(512) NOT NULL DEFAULT \'\', '
            '"PUBLISHED_AT" DATETIME, '
            '"FETCHED_AT" DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)'
        ))
        conn.execute(text(
            'CREATE TABLE feed_read_cursor ('
            'id INTEGER PRIMARY KEY, '
            '"PERSONA_ID" VARCHAR(255) NOT NULL, '
            '"SUBSCRIPTION_ID" VARCHAR(36) NOT NULL, '
            '"LAST_ITEM_ID" INTEGER NOT NULL DEFAULT 0, '
            '"UPDATED_AT" DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)'
        ))

    @staticmethod
    def _create_nullable_old_form_tables(conn):
        """NOT NULL も DEFAULT も無い、さらに古い nullable 形の 3 テーブル
        (二十二巡目 Z2/Z3 の再現用)。現行モデルで NOT NULL の列に NULL が
        残っている野生 DB を再現する。"""
        from sqlalchemy import text
        conn.execute(text(
            'CREATE TABLE feed_subscription ('
            '"SUBSCRIPTION_ID" VARCHAR(36) PRIMARY KEY, '
            '"FIXTURE_ID" VARCHAR(36), '
            '"FEED_URL" VARCHAR(512), '
            '"SITE_URL" VARCHAR(512), '
            '"TITLE" VARCHAR(255), '
            '"ENABLED" BOOLEAN, '
            '"ETAG" VARCHAR(255), '
            '"LAST_MODIFIED" VARCHAR(255), '
            '"LAST_OK_AT" DATETIME, '
            '"LAST_ERROR" VARCHAR(512), '
            '"CONSECUTIVE_FAILURES" INTEGER, '
            '"CREATED_AT" DATETIME, '
            '"UPDATED_AT" DATETIME)'
        ))
        conn.execute(text(
            'CREATE TABLE feed_item ('
            'id INTEGER PRIMARY KEY, '
            '"SUBSCRIPTION_ID" VARCHAR(36), '
            '"GUID" VARCHAR(512), '
            '"TITLE" TEXT, '
            '"SUMMARY" TEXT, '
            '"LINK" VARCHAR(512), '
            '"PUBLISHED_AT" DATETIME, '
            '"FETCHED_AT" DATETIME)'
        ))
        conn.execute(text(
            'CREATE TABLE feed_read_cursor ('
            'id INTEGER PRIMARY KEY, '
            '"PERSONA_ID" VARCHAR(255), '
            '"SUBSCRIPTION_ID" VARCHAR(36), '
            '"LAST_ITEM_ID" INTEGER, '
            '"UPDATED_AT" DATETIME)'
        ))

    def test_duplicate_subscriptions_repaired_then_index_created(self):
        """L2: 重複購読 2 組入りの旧形 DB — 最古 (rowid 最小) が残り、後発と
        その従属行 (記事・カーソル) が同一 transaction で消え、index が立つ。"""
        from sqlalchemy import inspect, text
        from database.migrate import _ensure_feed_tables
        engine = self._make_engine()
        with engine.begin() as conn:
            self._create_old_form_tables(conn)
            conn.execute(text(
                'INSERT INTO feed_subscription '
                '("SUBSCRIPTION_ID", "FIXTURE_ID", "FEED_URL") VALUES '
                "('s1', 'f1', 'https://example.com/feed'), "
                "('s2', 'f1', 'https://example.com/feed'), "
                "('s3', 'f2', 'https://example.org/feed'), "
                "('s4', 'f2', 'https://example.org/feed')"
            ))
            # 従属行: 最古 s1 の記事は残り、後発 s2/s4 の記事・カーソルは道連れ
            conn.execute(text(
                'INSERT INTO feed_item ("SUBSCRIPTION_ID", "GUID") VALUES '
                "('s1', 'g-keep'), ('s2', 'g-drop'), ('s4', 'g-drop2')"
            ))
            conn.execute(text(
                'INSERT INTO feed_read_cursor '
                '("PERSONA_ID", "SUBSCRIPTION_ID", "LAST_ITEM_ID") VALUES '
                "('p1', 's1', 1), ('p1', 's2', 2)"
            ))
        _ensure_feed_tables(engine)  # 例外にならない (修復して続行)
        insp = inspect(engine)
        idx_names = {i["name"] for i in insp.get_indexes("feed_subscription")}
        self.assertIn("uq_feed_sub_fixture_url", idx_names)
        with engine.connect() as conn:
            subs = {
                r[0] for r in conn.execute(text(
                    'SELECT "SUBSCRIPTION_ID" FROM feed_subscription'
                ))
            }
            self.assertEqual(subs, {"s1", "s3"})  # 各組の最古だけが残る
            item_subs = [
                r[0] for r in conn.execute(text(
                    'SELECT "SUBSCRIPTION_ID" FROM feed_item'
                ))
            ]
            self.assertEqual(item_subs, ["s1"])  # 後発の従属記事は消える
            cursor_subs = [
                r[0] for r in conn.execute(text(
                    'SELECT "SUBSCRIPTION_ID" FROM feed_read_cursor'
                ))
            ]
            self.assertEqual(cursor_subs, ["s1"])  # 後発の従属カーソルも消える

    def test_old_form_item_and_cursor_duplicates_converge(self):
        """L5: 制約なし旧形テーブルの feed_item / feed_read_cursor も一意
        index へ収束する — 記事は rowid 最小、カーソルは既読が最も進んだ行
        (LAST_ITEM_ID 最大) を残す。"""
        from sqlalchemy import inspect, text
        from database.migrate import _ensure_feed_tables
        engine = self._make_engine()
        with engine.begin() as conn:
            self._create_old_form_tables(conn)
            conn.execute(text(
                'INSERT INTO feed_subscription '
                '("SUBSCRIPTION_ID", "FIXTURE_ID", "FEED_URL") VALUES '
                "('s1', 'f1', 'https://example.com/feed')"
            ))
            conn.execute(text(
                'INSERT INTO feed_item (id, "SUBSCRIPTION_ID", "GUID", "TITLE") '
                "VALUES (1, 's1', 'g-1', '先'), (2, 's1', 'g-1', '後'), "
                "(3, 's1', 'g-2', '別')"
            ))
            conn.execute(text(
                'INSERT INTO feed_read_cursor '
                '("PERSONA_ID", "SUBSCRIPTION_ID", "LAST_ITEM_ID") VALUES '
                "('p1', 's1', 3), ('p1', 's1', 7), ('p2', 's1', 1)"
            ))
        _ensure_feed_tables(engine)
        insp = inspect(engine)
        item_idx = {i["name"] for i in insp.get_indexes("feed_item")}
        cursor_idx = {i["name"] for i in insp.get_indexes("feed_read_cursor")}
        self.assertIn("uq_feed_item_sub_guid", item_idx)
        self.assertIn("uq_feed_cursor_persona_sub", cursor_idx)
        with engine.connect() as conn:
            items = conn.execute(text(
                'SELECT id, "GUID" FROM feed_item ORDER BY id'
            )).fetchall()
            # 同一 (sub, guid) は rowid 最小 (id=1) を残す
            self.assertEqual([tuple(r) for r in items], [(1, "g-1"), (3, "g-2")])
            cursors = conn.execute(text(
                'SELECT "PERSONA_ID", "LAST_ITEM_ID" FROM feed_read_cursor '
                'ORDER BY "PERSONA_ID"'
            )).fetchall()
            # p1 は既読が進んだ方 (7) を残す — 巻き戻すと重複配送になる
            self.assertEqual([tuple(r) for r in cursors], [("p1", 7), ("p2", 1)])

    def test_clean_db_creates_index(self):
        from sqlalchemy import inspect
        from database.migrate import _ensure_feed_tables
        engine = self._make_engine()
        _ensure_feed_tables(engine)
        insp = inspect(engine)
        sub_idx = {i["name"] for i in insp.get_indexes("feed_subscription")}
        item_idx = {i["name"] for i in insp.get_indexes("feed_item")}
        cursor_idx = {i["name"] for i in insp.get_indexes("feed_read_cursor")}
        self.assertIn("uq_feed_sub_fixture_url", sub_idx)
        self.assertIn("uq_feed_item_sub_guid", item_idx)
        self.assertIn("uq_feed_cursor_persona_sub", cursor_idx)

    def _feed_item_create_sql(self, engine):
        from sqlalchemy import text
        with engine.connect() as conn:
            return conn.execute(text(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'feed_item'"
            )).scalar() or ""

    def test_feed_item_without_autoincrement_rebuilt_preserving_rows(self):
        """二十巡目 V1: AUTOINCREMENT 無しの旧形 feed_item は再構築で単調性を
        獲得する — 行と id を保全し、最大 id の行を消しても次の INSERT が
        その id を再利用しない (再利用されると配送カーソル id > LAST_ITEM_ID
        から新着が永久に漏れる)。"""
        from sqlalchemy import inspect, text
        from database.migrate import _ensure_feed_tables
        engine = self._make_engine()
        with engine.begin() as conn:
            self._create_old_form_tables(conn)
            conn.execute(text(
                'INSERT INTO feed_subscription '
                '("SUBSCRIPTION_ID", "FIXTURE_ID", "FEED_URL") VALUES '
                "('s1', 'f1', 'https://example.com/feed')"
            ))
            conn.execute(text(
                'INSERT INTO feed_item '
                '(id, "SUBSCRIPTION_ID", "GUID", "TITLE", "PUBLISHED_AT") '
                "VALUES (1, 's1', 'g-1', '新しい記事', '2026-08-03 12:00:00'), "
                "(5, 's1', 'g-5', '古い記事', '2026-08-01 12:00:00')"
            ))
            # 旧形には AUTOINCREMENT が無い (再構築が発動する前提の確認)
            self.assertNotIn(
                "AUTOINCREMENT",
                (conn.execute(text(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'feed_item'"
                )).scalar() or "").upper(),
            )
        _ensure_feed_tables(engine)
        # CREATE 文に AUTOINCREMENT が付いた
        self.assertIn("AUTOINCREMENT", self._feed_item_create_sql(engine).upper())
        # 再実行は no-op (冪等)
        _ensure_feed_tables(engine)
        with engine.connect() as conn:
            rows = conn.execute(text(
                'SELECT id, "GUID", "TITLE" FROM feed_item ORDER BY id'
            )).fetchall()
        # 行と id は保全される
        self.assertEqual(
            [tuple(r) for r in rows],
            [(1, "g-1", "新しい記事"), (5, "g-5", "古い記事")],
        )
        # index も新テーブル側に立っている
        insp = inspect(engine)
        item_idx = {i["name"] for i in insp.get_indexes("feed_item")}
        self.assertIn("idx_feed_item_sub", item_idx)
        self.assertIn("uq_feed_item_sub_guid", item_idx)
        # 単調性: 最大 id (5) の行を剪定相当で消して新規 INSERT →
        # id は 6 (旧形なら 5 を再利用していた)
        with engine.begin() as conn:
            conn.execute(text('DELETE FROM feed_item WHERE id = 5'))
            conn.execute(text(
                'INSERT INTO feed_item '
                '("SUBSCRIPTION_ID", "GUID", "TITLE", "SUMMARY", "LINK") '
                "VALUES ('s1', 'g-new', '新着', '', '')"
            ))
        with engine.connect() as conn:
            new_id = conn.execute(text(
                'SELECT id FROM feed_item WHERE "GUID" = \'g-new\''
            )).scalar()
        self.assertEqual(new_id, 6)

    def test_rebuild_skips_null_key_rows(self):
        """AA1 (二十三巡目): 必須キー (SUBSCRIPTION_ID / GUID) が NULL の行が
        混ざった非 AUTOINCREMENT 旧表でも起動時再構築は成功し、当該行だけが
        WARNING (件数表明) 付きで落ちる — 素通しすると新テーブルの NOT NULL
        制約でコピー INSERT が失敗し、migration 全体が rollback して毎起動
        同じ地点で失敗する起動不能になる。起動時 backfill は既定値のある列
        しか埋めないため、必須キーの NULL はここへ必ず届きうる。"""
        from sqlalchemy import text
        from database.migrate import _ensure_feed_tables
        engine = self._make_engine()
        with engine.begin() as conn:
            self._create_nullable_old_form_tables(conn)
            conn.execute(text(
                'INSERT INTO feed_subscription '
                '("SUBSCRIPTION_ID", "FIXTURE_ID", "FEED_URL") VALUES '
                "('s1', 'f1', 'https://example.com/feed')"
            ))
            conn.execute(text(
                'INSERT INTO feed_item (id, "SUBSCRIPTION_ID", "GUID") VALUES '
                "(1, 's1', 'g-1'), "  # 健全 → 残る
                "(2, NULL, 'g-2'), "  # SUBSCRIPTION_ID NULL → 落ちる
                "(3, 's1', NULL)"     # GUID NULL → 落ちる
            ))
        with self.assertLogs(level="WARNING") as logs:
            _ensure_feed_tables(engine)
        self.assertTrue(
            any("コピー対象から除外" in m for m in logs.output), logs.output,
        )
        # 再構築自体は成功して AUTOINCREMENT を獲得している
        self.assertIn(
            "AUTOINCREMENT", self._feed_item_create_sql(engine).upper(),
        )
        with engine.connect() as conn:
            rows = conn.execute(text(
                'SELECT id, "SUBSCRIPTION_ID", "GUID" FROM feed_item'
            )).fetchall()
        # キー NULL の 2 行だけが落ち、健全な行は id ごと保全される
        self.assertEqual([tuple(r) for r in rows], [(1, "s1", "g-1")])

    def test_rebuild_inherits_sequence_high_water_from_cursor(self):
        """Y1 (二十一巡目): 配送済みカーソル=100・現存最大 id=90 の旧形 DB を
        再構築 → 新規 INSERT は 101 以上で採番され、配送 (id > LAST_ITEM_ID)
        が新着を拾う。sqlite_sequence を現存行だけから作り直すと 90 に戻り、
        新着がカーソル未満の id (91〜100) で採番されて永久に漏れる。"""
        from sqlalchemy import text
        from database.migrate import _ensure_feed_tables
        engine = self._make_engine()
        with engine.begin() as conn:
            self._create_old_form_tables(conn)
            conn.execute(text(
                'INSERT INTO feed_subscription '
                '("SUBSCRIPTION_ID", "FIXTURE_ID", "FEED_URL") VALUES '
                "('s1', 'f1', 'https://example.com/feed')"
            ))
            # 剪定済みの高 id (91〜100) は現存しない — 残るのは 3 と 90 のみ
            conn.execute(text(
                'INSERT INTO feed_item (id, "SUBSCRIPTION_ID", "GUID") VALUES '
                "(3, 's1', 'g-3'), (90, 's1', 'g-90')"
            ))
            conn.execute(text(
                'INSERT INTO feed_read_cursor '
                '("PERSONA_ID", "SUBSCRIPTION_ID", "LAST_ITEM_ID") VALUES '
                "('p1', 's1', 100)"
            ))
        _ensure_feed_tables(engine)
        with engine.begin() as conn:
            conn.execute(text(
                'INSERT INTO feed_item '
                '("SUBSCRIPTION_ID", "GUID", "TITLE", "SUMMARY", "LINK") '
                "VALUES ('s1', 'g-new', '新着', '', '')"
            ))
        with engine.connect() as conn:
            # 既存行と id は保全されている
            ids = [r[0] for r in conn.execute(text(
                'SELECT id FROM feed_item ORDER BY id'
            ))]
            new_id = conn.execute(text(
                'SELECT id FROM feed_item WHERE "GUID" = \'g-new\''
            )).scalar()
            cursor = conn.execute(text(
                'SELECT "LAST_ITEM_ID" FROM feed_read_cursor'
            )).scalar()
        self.assertEqual(ids, [3, 90, 101])
        self.assertEqual(new_id, 101)
        # 配送条件 id > LAST_ITEM_ID を満たす = 新着が配送から漏れない
        self.assertGreater(new_id, cursor)

    def test_rebuild_of_empty_table_inherits_cursor_high_water(self):
        """Y1 (二十一巡目): 記事が全て剪定済み (空表) でもカーソルの高水位を
        継承する — 素の再構築では採番が 1 から再開し、非再利用が崩れる。"""
        from sqlalchemy import text
        from database.migrate import _ensure_feed_tables
        engine = self._make_engine()
        with engine.begin() as conn:
            self._create_old_form_tables(conn)
            conn.execute(text(
                'INSERT INTO feed_subscription '
                '("SUBSCRIPTION_ID", "FIXTURE_ID", "FEED_URL") VALUES '
                "('s1', 'f1', 'https://example.com/feed')"
            ))
            conn.execute(text(
                'INSERT INTO feed_read_cursor '
                '("PERSONA_ID", "SUBSCRIPTION_ID", "LAST_ITEM_ID") VALUES '
                "('p1', 's1', 100)"
            ))
        _ensure_feed_tables(engine)
        with engine.begin() as conn:
            conn.execute(text(
                'INSERT INTO feed_item '
                '("SUBSCRIPTION_ID", "GUID", "TITLE", "SUMMARY", "LINK") '
                "VALUES ('s1', 'g-new', '新着', '', '')"
            ))
        with engine.connect() as conn:
            new_id = conn.execute(text(
                'SELECT id FROM feed_item WHERE "GUID" = \'g-new\''
            )).scalar()
        self.assertEqual(new_id, 101)

    @staticmethod
    def _create_old_form_tables_missing_item_columns(conn):
        """SUMMARY / LINK / FETCHED_AT 列の無いさらに古い形の feed_item を持つ
        3 テーブル (Y2 の再現用)。"""
        from sqlalchemy import text
        FeedMigrationIndexTest._create_old_form_tables(conn)
        conn.execute(text("DROP TABLE feed_item"))
        conn.execute(text(
            'CREATE TABLE feed_item ('
            'id INTEGER PRIMARY KEY, '
            '"SUBSCRIPTION_ID" VARCHAR(36) NOT NULL, '
            '"GUID" VARCHAR(512) NOT NULL, '
            '"TITLE" TEXT NOT NULL DEFAULT \'\', '
            '"PUBLISHED_AT" DATETIME)'
        ))

    def test_missing_not_null_columns_backfilled_in_one_shot(self):
        """Y2 (二十一巡目): 現行の NOT NULL 列 (SUMMARY / LINK / FETCHED_AT) が
        欠落した旧形 feed_item (既存行あり) からの収束が一発で成功する —
        列補修の ALTER (nullable) 後、再構築のコピーが NULL をモデル既定値で
        backfill する。"""
        from sqlalchemy import inspect, text
        from database.migrate import _ensure_feed_tables
        engine = self._make_engine()
        with engine.begin() as conn:
            self._create_old_form_tables_missing_item_columns(conn)
            conn.execute(text(
                'INSERT INTO feed_subscription '
                '("SUBSCRIPTION_ID", "FIXTURE_ID", "FEED_URL") VALUES '
                "('s1', 'f1', 'https://example.com/feed')"
            ))
            conn.execute(text(
                'INSERT INTO feed_item (id, "SUBSCRIPTION_ID", "GUID", "TITLE") '
                "VALUES (1, 's1', 'g-1', '記事1'), (2, 's1', 'g-2', '記事2')"
            ))
        _ensure_feed_tables(engine)  # 一発で成功する (固定化しない)
        with engine.connect() as conn:
            rows = conn.execute(text(
                'SELECT id, "TITLE", "SUMMARY", "LINK", "FETCHED_AT" '
                'FROM feed_item ORDER BY id'
            )).fetchall()
        # 行保全 + NULL が既定値 (SUMMARY/LINK = '', FETCHED_AT = 現在時刻)
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            [(r[0], r[1], r[2], r[3]) for r in rows],
            [(1, "記事1", "", ""), (2, "記事2", "", "")],
        )
        for r in rows:
            self.assertIsNotNone(r[4])  # FETCHED_AT が CURRENT_TIMESTAMP で埋まる
        # AUTOINCREMENT 再構築・index 補修も同時に完了している
        self.assertIn("AUTOINCREMENT", self._feed_item_create_sql(engine).upper())
        item_idx = {i["name"] for i in inspect(engine).get_indexes("feed_item")}
        self.assertIn("uq_feed_item_sub_guid", item_idx)

    def test_mid_failure_rolls_back_column_alter_no_fixation(self):
        """Y2 (二十一巡目): 列補修より後の工程で失敗しても ALTER が独立 commit
        で残らない (全工程が単一 transaction で rollback される) — 残ると
        次回起動が「NULL 入りの既存列」で同じ失敗を繰り返す固定化になる。
        再実行 (人工失敗の解除後) で最初から収束できることも確認する。"""
        from sqlalchemy import inspect, text
        from database.migrate import _ensure_feed_tables
        engine = self._make_engine()
        with engine.begin() as conn:
            self._create_old_form_tables_missing_item_columns(conn)
            conn.execute(text(
                'INSERT INTO feed_subscription '
                '("SUBSCRIPTION_ID", "FIXTURE_ID", "FEED_URL") VALUES '
                "('s1', 'f1', 'https://example.com/feed')"
            ))
            conn.execute(text(
                'INSERT INTO feed_item (id, "SUBSCRIPTION_ID", "GUID") '
                "VALUES (1, 's1', 'g-1')"
            ))
        # 列補修 (ALTER) の後・再構築の途中で人工的に失敗させる
        with patch(
            "database.migrate._rebuild_feed_item_with_autoincrement",
            side_effect=RuntimeError("injected failure"),
        ):
            with self.assertRaises(RuntimeError):
                _ensure_feed_tables(engine)
        # ALTER も rollback されている — 独立 commit で残ると固定化の温床
        cols = {c["name"] for c in inspect(engine).get_columns("feed_item")}
        self.assertNotIn("FETCHED_AT", cols)
        self.assertNotIn("SUMMARY", cols)
        # 失敗原因の解消後 (= 人工失敗なし) の再実行で最初から収束できる
        _ensure_feed_tables(engine)
        with engine.connect() as conn:
            row = conn.execute(text(
                'SELECT "SUMMARY", "FETCHED_AT" FROM feed_item WHERE id = 1'
            )).fetchone()
        self.assertEqual(row[0], "")
        self.assertIsNotNone(row[1])


class FullRewriteMigrationFeedDedupeTest(unittest.TestCase):
    """M1 (八巡目) → P1 (十一巡目): 破壊的差分による全書換 migration が、
    重複フィード行入りの旧 DB でも起動不能にならない。

    全書換 (migrate_database_in_place) は unique 制約付きの新テーブルを作って
    から旧行をコピーするため、重複を残したままだと INSERT が IntegrityError →
    migration 全体がロールバックする。コピーの SELECT 自体を「勝者行のみ」の
    決定論フィルタ (_feed_copy_filter_select) にしてこれを塞ぐ。規則は起動時
    修復 (_repair_duplicate_feed_rows) と同一。ソース = バックアップには一切
    書かない — 後続の移行失敗時にロールバックが「無傷の元」を復元できること
    がバックアップの存在意義 (十一巡目 P1 で、ソース側を書き換える旧方式から
    置き換え)。
    """

    @staticmethod
    def _find_backup(tmpdir):
        """migrate_database_in_place が残す `.bak` ファイル (唯一のはず)。"""
        backups = [f for f in os.listdir(tmpdir) if f.endswith(".bak")]
        assert len(backups) == 1, backups
        return os.path.join(tmpdir, backups[0])

    def test_full_rewrite_succeeds_with_duplicate_feed_rows(self):
        from sqlalchemy import inspect as sa_inspect, text
        from database.migrate import (
            migrate_database_in_place,
            try_additive_migration,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "saiverse.db")
            engine = create_engine(f"sqlite:///{db_path}")
            try:
                Base.metadata.create_all(engine)
                with engine.begin() as conn:
                    # feed 3 テーブルを UNIQUE 制約なしの旧形へ差し替えて
                    # 重複行を仕込む (開発期 DB の再現)
                    conn.execute(text("DROP TABLE feed_read_cursor"))
                    conn.execute(text("DROP TABLE feed_item"))
                    conn.execute(text("DROP TABLE feed_subscription"))
                    FeedMigrationIndexTest._create_old_form_tables(conn)
                    conn.execute(text(
                        'INSERT INTO feed_subscription '
                        '("SUBSCRIPTION_ID", "FIXTURE_ID", "FEED_URL") VALUES '
                        "('s1', 'f1', 'https://example.com/feed'), "
                        "('s2', 'f1', 'https://example.com/feed')"
                    ))
                    conn.execute(text(
                        'INSERT INTO feed_item '
                        '(id, "SUBSCRIPTION_ID", "GUID") VALUES '
                        "(1, 's1', 'g-1'), (2, 's1', 'g-1'), (3, 's2', 'g-x')"
                    ))
                    conn.execute(text(
                        'INSERT INTO feed_read_cursor '
                        '("PERSONA_ID", "SUBSCRIPTION_ID", "LAST_ITEM_ID") '
                        "VALUES ('p1', 's1', 3), ('p1', 's1', 7)"
                    ))
                    # 別テーブルの破壊的差分 (モデルに無い列) — これが
                    # 全書換パスへ落とす引き金
                    conn.execute(text(
                        'ALTER TABLE "AI" ADD COLUMN "LEGACY_JUNK" TEXT'
                    ))
            finally:
                engine.dispose()

            # 破壊的差分なので追加系では解消できない = 実運用では全書換が選ばれる
            self.assertFalse(try_additive_migration(db_path))

            # 全書換が IntegrityError にならず、除外は WARNING で表明される
            with self.assertLogs(level="WARNING") as logs:
                migrate_database_in_place(db_path)
            self.assertTrue(
                any("コピー対象から除外" in m for m in logs.output),
                logs.output,
            )

            engine = create_engine(f"sqlite:///{db_path}")
            try:
                with engine.connect() as conn:
                    subs = [r[0] for r in conn.execute(text(
                        'SELECT "SUBSCRIPTION_ID" FROM feed_subscription'
                    ))]
                    # 購読は最古 (rowid 最小) の s1 だけが残る
                    self.assertEqual(subs, ["s1"])
                    items = [tuple(r) for r in conn.execute(text(
                        'SELECT id, "GUID" FROM feed_item ORDER BY id'
                    ))]
                    # 同一 (sub, guid) は rowid 最小、後発購読 s2 の記事は道連れ
                    self.assertEqual(items, [(1, "g-1")])
                    cursors = [tuple(r) for r in conn.execute(text(
                        'SELECT "PERSONA_ID", "LAST_ITEM_ID" '
                        'FROM feed_read_cursor'
                    ))]
                    # カーソルは既読が最も進んだ行 (LAST_ITEM_ID 最大) を残す
                    self.assertEqual(cursors, [("p1", 7)])
                    # 破壊的差分そのものも全書換で解消されている
                    ai_cols = {
                        c["name"] for c in sa_inspect(engine).get_columns("AI")
                    }
                    self.assertNotIn("LEGACY_JUNK", ai_cols)
            finally:
                engine.dispose()

            # ソース = バックアップは無傷 (重複行がそのまま残る) — コピー時
            # フィルタはソースに書かないことの検証 (十一巡目 P1)
            backup_engine = create_engine(
                f"sqlite:///{self._find_backup(tmpdir)}"
            )
            try:
                with backup_engine.connect() as conn:
                    subs = [r[0] for r in conn.execute(text(
                        'SELECT "SUBSCRIPTION_ID" FROM feed_subscription '
                        'ORDER BY "SUBSCRIPTION_ID"'
                    ))]
                    self.assertEqual(subs, ["s1", "s2"])
                    items = [tuple(r) for r in conn.execute(text(
                        'SELECT id, "GUID" FROM feed_item ORDER BY id'
                    ))]
                    self.assertEqual(
                        items, [(1, "g-1"), (2, "g-1"), (3, "g-x")],
                    )
                    cursors = [tuple(r) for r in conn.execute(text(
                        'SELECT "PERSONA_ID", "LAST_ITEM_ID" '
                        'FROM feed_read_cursor ORDER BY "LAST_ITEM_ID"'
                    ))]
                    self.assertEqual(cursors, [("p1", 3), ("p1", 7)])
            finally:
                backup_engine.dispose()

    def test_late_failure_rolls_back_to_pristine_backup(self):
        """P1 (十一巡目): フィード行の間引き後、移行の後半で失敗しても、
        ロールバックで復元される元 DB は無傷 (重複行が全行残っている)。
        旧方式 (ソース側 dedupe) ではバックアップ自体が書き換わっていたため、
        復元しても「無傷の元」に戻れなかった。"""
        from sqlalchemy import text
        from database.migrate import migrate_database_in_place
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "saiverse.db")
            engine = create_engine(f"sqlite:///{db_path}")
            try:
                Base.metadata.create_all(engine)
                with engine.begin() as conn:
                    conn.execute(text("DROP TABLE feed_read_cursor"))
                    conn.execute(text("DROP TABLE feed_item"))
                    conn.execute(text("DROP TABLE feed_subscription"))
                    FeedMigrationIndexTest._create_old_form_tables(conn)
                    conn.execute(text(
                        'INSERT INTO feed_subscription '
                        '("SUBSCRIPTION_ID", "FIXTURE_ID", "FEED_URL") VALUES '
                        "('s1', 'f1', 'https://example.com/feed'), "
                        "('s2', 'f1', 'https://example.com/feed')"
                    ))
                    conn.execute(text(
                        'INSERT INTO feed_item '
                        '(id, "SUBSCRIPTION_ID", "GUID") VALUES '
                        "(1, 's1', 'g-1'), (2, 's1', 'g-1'), (3, 's2', 'g-x')"
                    ))
                    conn.execute(text(
                        'INSERT INTO feed_read_cursor '
                        '("PERSONA_ID", "SUBSCRIPTION_ID", "LAST_ITEM_ID") '
                        "VALUES ('p1', 's1', 3), ('p1', 's1', 7)"
                    ))
            finally:
                engine.dispose()

            # テーブルコピー (フィード行の間引き) より後の工程で人工的に失敗
            # させる → ロールバック経路に入る
            with patch(
                "database.migrate._backfill_item_short_ids",
                side_effect=RuntimeError("injected failure"),
            ):
                with self.assertRaises(RuntimeError):
                    migrate_database_in_place(db_path)

            # 復元された DB に全行が残っている (バックアップが無傷だった証拠)
            engine = create_engine(f"sqlite:///{db_path}")
            try:
                with engine.connect() as conn:
                    subs = [r[0] for r in conn.execute(text(
                        'SELECT "SUBSCRIPTION_ID" FROM feed_subscription '
                        'ORDER BY "SUBSCRIPTION_ID"'
                    ))]
                    self.assertEqual(subs, ["s1", "s2"])
                    items = [tuple(r) for r in conn.execute(text(
                        'SELECT id, "GUID" FROM feed_item ORDER BY id'
                    ))]
                    self.assertEqual(
                        items, [(1, "g-1"), (2, "g-1"), (3, "g-x")],
                    )
                    cursors = [tuple(r) for r in conn.execute(text(
                        'SELECT "PERSONA_ID", "LAST_ITEM_ID" '
                        'FROM feed_read_cursor ORDER BY "LAST_ITEM_ID"'
                    ))]
                    self.assertEqual(cursors, [("p1", 3), ("p1", 7)])
            finally:
                engine.dispose()

    def test_full_rewrite_succeeds_with_subscription_table_only(self):
        """N4 (九巡目): フィード表が部分的にしか無いソース (購読表のみ) でも
        存在する表のコピーが間引かれ、全書換が IntegrityError にならない —
        「3 表は常に一緒」を前提にすると、部分スキーマの野生 DB でフィルタが
        存在しない表を参照してコピーが失敗する。"""
        from sqlalchemy import text
        from database.migrate import (
            migrate_database_in_place,
            try_additive_migration,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "saiverse.db")
            engine = create_engine(f"sqlite:///{db_path}")
            try:
                Base.metadata.create_all(engine)
                with engine.begin() as conn:
                    conn.execute(text("DROP TABLE feed_read_cursor"))
                    conn.execute(text("DROP TABLE feed_item"))
                    conn.execute(text("DROP TABLE feed_subscription"))
                    FeedMigrationIndexTest._create_old_form_tables(conn)
                    # 購読表だけ残す (記事・カーソル表の無い部分スキーマ)
                    conn.execute(text("DROP TABLE feed_read_cursor"))
                    conn.execute(text("DROP TABLE feed_item"))
                    conn.execute(text(
                        'INSERT INTO feed_subscription '
                        '("SUBSCRIPTION_ID", "FIXTURE_ID", "FEED_URL") VALUES '
                        "('s1', 'f1', 'https://example.com/feed'), "
                        "('s2', 'f1', 'https://example.com/feed')"
                    ))
                    # 破壊的差分 — 全書換パスへ落とす引き金
                    conn.execute(text(
                        'ALTER TABLE "AI" ADD COLUMN "LEGACY_JUNK" TEXT'
                    ))
            finally:
                engine.dispose()

            self.assertFalse(try_additive_migration(db_path))
            with self.assertLogs(level="WARNING") as logs:
                migrate_database_in_place(db_path)
            self.assertTrue(
                any("コピー対象から除外" in m for m in logs.output),
                logs.output,
            )

            engine = create_engine(f"sqlite:///{db_path}")
            try:
                with engine.connect() as conn:
                    subs = [r[0] for r in conn.execute(text(
                        'SELECT "SUBSCRIPTION_ID" FROM feed_subscription'
                    ))]
                    # 最古 (rowid 最小) の s1 だけが残る
                    self.assertEqual(subs, ["s1"])
            finally:
                engine.dispose()

    @staticmethod
    def _prepare_old_form_db(db_path, setup, create_tables=None):
        """旧形フィード 3 テーブル + 破壊的差分 (LEGACY_JUNK) 入りの DB を作る。
        setup(conn) がテーブルの削除・行の投入を行う。create_tables で旧形の
        変種 (nullable 形など) に差し替えられる。"""
        from sqlalchemy import text
        engine = create_engine(f"sqlite:///{db_path}")
        try:
            Base.metadata.create_all(engine)
            with engine.begin() as conn:
                conn.execute(text("DROP TABLE feed_read_cursor"))
                conn.execute(text("DROP TABLE feed_item"))
                conn.execute(text("DROP TABLE feed_subscription"))
                (create_tables
                 or FeedMigrationIndexTest._create_old_form_tables)(conn)
                setup(conn)
                # 破壊的差分 — 全書換パスへ落とす引き金
                conn.execute(text(
                    'ALTER TABLE "AI" ADD COLUMN "LEGACY_JUNK" TEXT'
                ))
        finally:
            engine.dispose()

    def _assert_child_tables_after_rewrite(self, db_path, items, cursors):
        from sqlalchemy import text
        engine = create_engine(f"sqlite:///{db_path}")
        try:
            with engine.connect() as conn:
                got_items = [tuple(r) for r in conn.execute(text(
                    'SELECT id, "SUBSCRIPTION_ID", "GUID" FROM feed_item '
                    'ORDER BY id'
                ))]
                self.assertEqual(got_items, items)
                got_cursors = [tuple(r) for r in conn.execute(text(
                    'SELECT "PERSONA_ID", "SUBSCRIPTION_ID", "LAST_ITEM_ID" '
                    'FROM feed_read_cursor ORDER BY "SUBSCRIPTION_ID"'
                ))]
                self.assertEqual(got_cursors, cursors)
        finally:
            engine.dispose()

    def test_full_rewrite_child_tables_without_parent_table_copy_nothing(self):
        """Q1 (十二巡目) 態様①: 親表 (feed_subscription) の無い部分スキーマ
        では子表 (feed_item / feed_read_cursor) の行を一切コピーしない —
        親が存在しえない以上、全行が孤児確定。旧述語 (敗者除外の NOT IN) は
        親表なしで述語ごと外れ、孤児行を素通ししていた。"""
        from sqlalchemy import text
        from database.migrate import migrate_database_in_place
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "saiverse.db")

            def setup(conn):
                conn.execute(text("DROP TABLE feed_subscription"))
                conn.execute(text(
                    'INSERT INTO feed_item (id, "SUBSCRIPTION_ID", "GUID") '
                    "VALUES (1, 'ghost', 'g-1'), (2, 'ghost2', 'g-2')"
                ))
                conn.execute(text(
                    'INSERT INTO feed_read_cursor '
                    '("PERSONA_ID", "SUBSCRIPTION_ID", "LAST_ITEM_ID") '
                    "VALUES ('p1', 'ghost', 3)"
                ))

            self._prepare_old_form_db(db_path, setup)
            with self.assertLogs(level="WARNING") as logs:
                migrate_database_in_place(db_path)
            self.assertTrue(
                any("コピー対象から除外" in m for m in logs.output),
                logs.output,
            )
            self._assert_child_tables_after_rewrite(db_path, [], [])

    def test_full_rewrite_child_rows_with_empty_parent_table_copy_nothing(self):
        """Q1 態様②: 親表が空 (購読ゼロ) でも子行はコピーされない —
        旧述語は NOT IN (空集合) = 常に真で全子行を通していた。"""
        from sqlalchemy import text
        from database.migrate import migrate_database_in_place
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "saiverse.db")

            def setup(conn):
                # feed_subscription は旧形のまま空。子行だけ入れる
                conn.execute(text(
                    'INSERT INTO feed_item (id, "SUBSCRIPTION_ID", "GUID") '
                    "VALUES (1, 'ghost', 'g-1')"
                ))
                conn.execute(text(
                    'INSERT INTO feed_read_cursor '
                    '("PERSONA_ID", "SUBSCRIPTION_ID", "LAST_ITEM_ID") '
                    "VALUES ('p1', 'ghost', 3)"
                ))

            self._prepare_old_form_db(db_path, setup)
            with self.assertLogs(level="WARNING") as logs:
                migrate_database_in_place(db_path)
            self.assertTrue(
                any("コピー対象から除外" in m for m in logs.output),
                logs.output,
            )
            self._assert_child_tables_after_rewrite(db_path, [], [])

    def test_full_rewrite_orphan_child_rows_excluded(self):
        """Q1 態様③: 該当親の無い子行が混在する場合、勝者購読に親を持つ
        子行だけがコピーされる (孤児は除外、親ありは従来どおり通る)。"""
        from sqlalchemy import text
        from database.migrate import migrate_database_in_place
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "saiverse.db")

            def setup(conn):
                conn.execute(text(
                    'INSERT INTO feed_subscription '
                    '("SUBSCRIPTION_ID", "FIXTURE_ID", "FEED_URL") VALUES '
                    "('s1', 'f1', 'https://example.com/feed')"
                ))
                conn.execute(text(
                    'INSERT INTO feed_item (id, "SUBSCRIPTION_ID", "GUID") '
                    "VALUES (1, 's1', 'g-1'), (2, 'ghost', 'g-2')"
                ))
                conn.execute(text(
                    'INSERT INTO feed_read_cursor '
                    '("PERSONA_ID", "SUBSCRIPTION_ID", "LAST_ITEM_ID") '
                    "VALUES ('p1', 's1', 5), ('p1', 'ghost', 9)"
                ))

            self._prepare_old_form_db(db_path, setup)
            with self.assertLogs(level="WARNING") as logs:
                migrate_database_in_place(db_path)
            self.assertTrue(
                any("コピー対象から除外" in m for m in logs.output),
                logs.output,
            )
            self._assert_child_tables_after_rewrite(
                db_path,
                items=[(1, "s1", "g-1")],
                cursors=[("p1", "s1", 5)],
            )

    def test_full_rewrite_inherits_sequence_high_water_from_cursor(self):
        """Y1 (二十一巡目): 全書換 migration でも採番の高水位をカーソルから
        継承する — 配送済みカーソル=100・現存最大 id=90 の旧形 DB を全書換
        → 新規 INSERT は 101 で採番され、配送 (id > LAST_ITEM_ID) が新着を
        拾う。コピーだけでは sqlite_sequence が現存行の最大 id (90) までしか
        進まず、新着 91〜100 がカーソル未満で永久に漏れる。"""
        from sqlalchemy import text
        from database.migrate import migrate_database_in_place
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "saiverse.db")

            def setup(conn):
                conn.execute(text(
                    'INSERT INTO feed_subscription '
                    '("SUBSCRIPTION_ID", "FIXTURE_ID", "FEED_URL") VALUES '
                    "('s1', 'f1', 'https://example.com/feed')"
                ))
                # 剪定済みの高 id (91〜100) は現存しない
                conn.execute(text(
                    'INSERT INTO feed_item (id, "SUBSCRIPTION_ID", "GUID") '
                    "VALUES (3, 's1', 'g-3'), (90, 's1', 'g-90')"
                ))
                conn.execute(text(
                    'INSERT INTO feed_read_cursor '
                    '("PERSONA_ID", "SUBSCRIPTION_ID", "LAST_ITEM_ID") '
                    "VALUES ('p1', 's1', 100)"
                ))

            self._prepare_old_form_db(db_path, setup)
            migrate_database_in_place(db_path)

            engine = create_engine(f"sqlite:///{db_path}")
            try:
                with engine.begin() as conn:
                    conn.execute(text(
                        'INSERT INTO feed_item '
                        '("SUBSCRIPTION_ID", "GUID", "TITLE", "SUMMARY", '
                        '"LINK") '
                        "VALUES ('s1', 'g-new', '新着', '', '')"
                    ))
                with engine.connect() as conn:
                    new_id = conn.execute(text(
                        'SELECT id FROM feed_item WHERE "GUID" = \'g-new\''
                    )).scalar()
                    cursor = conn.execute(text(
                        'SELECT "LAST_ITEM_ID" FROM feed_read_cursor'
                    )).scalar()
                self.assertEqual(new_id, 101)
                # 配送条件 id > LAST_ITEM_ID を満たす = 新着が配送から漏れない
                self.assertGreater(new_id, cursor)
            finally:
                engine.dispose()

    def test_full_rewrite_partial_schema_missing_key_empty_copies_nothing(self):
        """Z1 (二十二巡目) 態様①: フィルタのキー列 (GUID) を欠く部分スキーマ
        の feed_item が空なら、存在しない列を参照する SQL を生成せず 0 行
        コピー (WARNING) で続行する — 検査なしではフィルタ SQL が
        "no such column" になり、migration 全体が rollback して起動不能に
        なる。健全な表 (購読) のコピーは通常どおり行われる。"""
        from sqlalchemy import text
        from database.migrate import migrate_database_in_place
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "saiverse.db")

            def setup(conn):
                conn.execute(text("DROP TABLE feed_item"))
                conn.execute(text(
                    'CREATE TABLE feed_item ('
                    'id INTEGER PRIMARY KEY, '
                    '"SUBSCRIPTION_ID" VARCHAR(36) NOT NULL, '
                    '"TITLE" TEXT)'
                ))
                conn.execute(text(
                    'INSERT INTO feed_subscription '
                    '("SUBSCRIPTION_ID", "FIXTURE_ID", "FEED_URL") VALUES '
                    "('s1', 'f1', 'https://example.com/feed')"
                ))

            self._prepare_old_form_db(db_path, setup)
            with self.assertLogs(level="WARNING") as logs:
                migrate_database_in_place(db_path)
            self.assertTrue(
                any("部分スキーマ" in m for m in logs.output), logs.output
            )
            engine = create_engine(f"sqlite:///{db_path}")
            try:
                with engine.connect() as conn:
                    n_items = conn.execute(text(
                        'SELECT COUNT(*) FROM feed_item'
                    )).scalar()
                    subs = [r[0] for r in conn.execute(text(
                        'SELECT "SUBSCRIPTION_ID" FROM feed_subscription'
                    ))]
                self.assertEqual(n_items, 0)
                self.assertEqual(subs, ["s1"])
            finally:
                engine.dispose()

    def test_full_rewrite_partial_schema_missing_key_with_rows_fails(self):
        """Z1 態様②: キー列 (GUID) を欠く部分スキーマの feed_item に行がある
        場合は「修復不能な部分スキーマ」として明示的な移行エラーで止まる —
        重複・孤児の判定ができない行を黙って捨ても通しもしない。元 DB は
        ロールバックで無傷 (部分スキーマのまま) に戻る。"""
        from sqlalchemy import text
        from database.migrate import migrate_database_in_place
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "saiverse.db")

            def setup(conn):
                conn.execute(text("DROP TABLE feed_item"))
                conn.execute(text(
                    'CREATE TABLE feed_item ('
                    'id INTEGER PRIMARY KEY, '
                    '"SUBSCRIPTION_ID" VARCHAR(36) NOT NULL, '
                    '"TITLE" TEXT)'
                ))
                conn.execute(text(
                    'INSERT INTO feed_item ("SUBSCRIPTION_ID", "TITLE") '
                    "VALUES ('s1', '記事')"
                ))

            self._prepare_old_form_db(db_path, setup)
            with self.assertRaises(RuntimeError) as cm:
                migrate_database_in_place(db_path)
            # 手当の指針を含む明示エラー (外側の rollback 例外に連鎖する)
            self.assertIn("部分スキーマ", str(cm.exception.__cause__))
            engine = create_engine(f"sqlite:///{db_path}")
            try:
                with engine.connect() as conn:
                    cols = {r[1] for r in conn.execute(text(
                        "PRAGMA table_info('feed_item')"
                    ))}
                    n_items = conn.execute(text(
                        'SELECT COUNT(*) FROM feed_item'
                    )).scalar()
                self.assertNotIn("GUID", cols)  # 元の部分スキーマのまま復元
                self.assertEqual(n_items, 1)
            finally:
                engine.dispose()

    def test_full_rewrite_item_table_without_id_empty_copies_nothing(self):
        """AA2 (二十三巡目) 態様①: id 列の無い feed_item は必須コピーキー
        欠落の部分スキーマ — 空なら 0 行コピー (WARNING) で続行する。id 抜き
        でコピーすると INSERT の自動採番で id が振り直され、配送カーソルの
        座標系が壊れるため、id はフィルタ SQL に現れなくても必須キー扱い。"""
        from sqlalchemy import text
        from database.migrate import migrate_database_in_place
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "saiverse.db")

            def setup(conn):
                conn.execute(text("DROP TABLE feed_item"))
                conn.execute(text(
                    'CREATE TABLE feed_item ('
                    '"SUBSCRIPTION_ID" VARCHAR(36) NOT NULL, '
                    '"GUID" VARCHAR(512) NOT NULL, '
                    '"TITLE" TEXT)'
                ))
                conn.execute(text(
                    'INSERT INTO feed_subscription '
                    '("SUBSCRIPTION_ID", "FIXTURE_ID", "FEED_URL") VALUES '
                    "('s1', 'f1', 'https://example.com/feed')"
                ))

            self._prepare_old_form_db(db_path, setup)
            with self.assertLogs(level="WARNING") as logs:
                migrate_database_in_place(db_path)
            self.assertTrue(
                any("部分スキーマ" in m for m in logs.output), logs.output
            )
            engine = create_engine(f"sqlite:///{db_path}")
            try:
                with engine.connect() as conn:
                    n_items = conn.execute(text(
                        'SELECT COUNT(*) FROM feed_item'
                    )).scalar()
                    subs = [r[0] for r in conn.execute(text(
                        'SELECT "SUBSCRIPTION_ID" FROM feed_subscription'
                    ))]
                self.assertEqual(n_items, 0)
                self.assertEqual(subs, ["s1"])  # 健全な表のコピーは通常どおり
            finally:
                engine.dispose()

    def test_full_rewrite_item_table_without_id_with_rows_fails(self):
        """AA2 態様②: id 列の無い feed_item に行がある場合は指針つきの明示
        エラーで止まる — id を欠いたままコピーすると自動採番の振り直しで
        配送カーソルの座標を復元できない。決定的 ID 移送は実装しない
        (id 列の無い表はこのコード系譜から生まれ得ず、防衛は明示停止で
        足りる)。元 DB はロールバックで無傷に戻る。"""
        from sqlalchemy import text
        from database.migrate import migrate_database_in_place
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "saiverse.db")

            def setup(conn):
                conn.execute(text("DROP TABLE feed_item"))
                conn.execute(text(
                    'CREATE TABLE feed_item ('
                    '"SUBSCRIPTION_ID" VARCHAR(36) NOT NULL, '
                    '"GUID" VARCHAR(512) NOT NULL, '
                    '"TITLE" TEXT)'
                ))
                conn.execute(text(
                    'INSERT INTO feed_item ("SUBSCRIPTION_ID", "GUID") '
                    "VALUES ('s1', 'g-1')"
                ))

            self._prepare_old_form_db(db_path, setup)
            with self.assertRaises(RuntimeError) as cm:
                migrate_database_in_place(db_path)
            # 停止理由 (カーソル座標の復元不能) を指針として含む明示エラー
            self.assertIn("配送カーソル", str(cm.exception.__cause__))
            self.assertIn("id", str(cm.exception.__cause__))
            engine = create_engine(f"sqlite:///{db_path}")
            try:
                with engine.connect() as conn:
                    cols = {r[1] for r in conn.execute(text(
                        "PRAGMA table_info('feed_item')"
                    ))}
                    n_items = conn.execute(text(
                        'SELECT COUNT(*) FROM feed_item'
                    )).scalar()
                self.assertNotIn("id", cols)  # 元の部分スキーマのまま復元
                self.assertEqual(n_items, 1)
            finally:
                engine.dispose()

    def test_full_rewrite_backfills_nulls_and_skips_null_key_rows(self):
        """Z2 (二十二巡目): nullable の旧形に残る NULL — 既定値のある列
        (TITLE / ENABLED / FETCHED_AT / LAST_ITEM_ID 等) は backfill されて
        コピーされ、既定値の無い参照キー (FEED_URL / GUID / SUBSCRIPTION_ID)
        が NULL の行はスキップされる (キー NULL 購読の子行も道連れ)。
        素通しだと新スキーマの NOT NULL で migration 全体が落ちる。"""
        from sqlalchemy import text
        from database.migrate import migrate_database_in_place
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "saiverse.db")

            def setup(conn):
                conn.execute(text(
                    'INSERT INTO feed_subscription '
                    '("SUBSCRIPTION_ID", "FIXTURE_ID", "FEED_URL", "TITLE", '
                    '"ENABLED", "CONSECUTIVE_FAILURES") VALUES '
                    "('s1', 'f1', 'https://example.com/feed', "
                    "NULL, NULL, NULL), "
                    "('s2', 'f2', NULL, 't', 1, 0)"  # FEED_URL NULL → スキップ
                ))
                conn.execute(text(
                    'INSERT INTO feed_item '
                    '(id, "SUBSCRIPTION_ID", "GUID", "TITLE", "FETCHED_AT") '
                    "VALUES "
                    "(1, 's1', 'g-1', NULL, NULL), "  # backfill されてコピー
                    "(2, 's1', NULL, 't2', NULL), "   # GUID NULL → スキップ
                    "(3, 's2', 'g-3', 't3', NULL)"    # 親スキップの道連れ
                ))
                conn.execute(text(
                    'INSERT INTO feed_read_cursor '
                    '("PERSONA_ID", "SUBSCRIPTION_ID", "LAST_ITEM_ID") VALUES '
                    "('p1', 's1', NULL), "  # 重複の敗者 (COALESCE → 0 < 5)
                    "('p1', 's1', 5), "     # 勝者
                    "('p2', NULL, 3)"       # SUBSCRIPTION_ID NULL → スキップ
                ))

            self._prepare_old_form_db(
                db_path, setup,
                create_tables=(
                    FeedMigrationIndexTest._create_nullable_old_form_tables
                ),
            )
            with self.assertLogs(level="WARNING") as logs:
                migrate_database_in_place(db_path)
            self.assertTrue(
                any("コピー対象から除外" in m for m in logs.output),
                logs.output,
            )
            engine = create_engine(f"sqlite:///{db_path}")
            try:
                with engine.connect() as conn:
                    subs = [tuple(r) for r in conn.execute(text(
                        'SELECT "SUBSCRIPTION_ID", "TITLE", "ENABLED", '
                        '"CONSECUTIVE_FAILURES", "CREATED_AT" '
                        'FROM feed_subscription'
                    ))]
                    items = [tuple(r) for r in conn.execute(text(
                        'SELECT id, "TITLE", "SUMMARY", "LINK", "FETCHED_AT" '
                        'FROM feed_item'
                    ))]
                    cursors = [tuple(r) for r in conn.execute(text(
                        'SELECT "PERSONA_ID", "LAST_ITEM_ID" '
                        'FROM feed_read_cursor'
                    ))]
                # s2 (FEED_URL NULL) はスキップ、s1 は既定値で backfill
                self.assertEqual(len(subs), 1)
                self.assertEqual(subs[0][0], "s1")
                self.assertEqual(subs[0][1], "")   # TITLE
                self.assertEqual(subs[0][2], 1)    # ENABLED
                self.assertEqual(subs[0][3], 0)    # CONSECUTIVE_FAILURES
                self.assertIsNotNone(subs[0][4])   # CREATED_AT
                # GUID NULL (id=2) と親スキップの道連れ (id=3) は消え、
                # id=1 は既定値 backfill 込みで残る
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0][0], 1)
                self.assertEqual(items[0][1:4], ("", "", ""))
                self.assertIsNotNone(items[0][4])  # FETCHED_AT
                # キー NULL のカーソルは消え、重複は既読が進んだ方が残る
                self.assertEqual(cursors, [("p1", 5)])
            finally:
                engine.dispose()

    def test_null_residue_backfilled_on_startup_then_full_rewrite_succeeds(
        self,
    ):
        """Z3 (二十二巡目): AUTOINCREMENT 化済みの feed_item は再構築
        (COALESCE backfill 込み) がもう走らないため、過去の補修が残した
        NULL は起動時の backfill UPDATE で埋める — 放置すると後日の全書換の
        コピーで NOT NULL 違反として表面化する。NULL の LAST_ITEM_ID が
        重複カーソル修復の大小比較を壊さない (backfill が修復より先に走る)
        ことも確認する。"""
        from sqlalchemy import text
        from database.migrate import (
            ensure_feed_tables,
            migrate_database_in_place,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "saiverse.db")
            engine = create_engine(f"sqlite:///{db_path}")
            try:
                Base.metadata.create_all(engine)
                with engine.begin() as conn:
                    conn.execute(text("DROP TABLE feed_read_cursor"))
                    conn.execute(text("DROP TABLE feed_item"))
                    conn.execute(text("DROP TABLE feed_subscription"))
                    FeedMigrationIndexTest._create_nullable_old_form_tables(
                        conn
                    )
                    # feed_item だけ AUTOINCREMENT 済み + nullable drift の形
                    # (旧版補修の通過痕) に差し替える → 再構築は走らない
                    conn.execute(text("DROP TABLE feed_item"))
                    conn.execute(text(
                        'CREATE TABLE feed_item ('
                        'id INTEGER PRIMARY KEY AUTOINCREMENT, '
                        '"SUBSCRIPTION_ID" VARCHAR(36) NOT NULL, '
                        '"GUID" VARCHAR(512) NOT NULL, '
                        '"TITLE" TEXT, '
                        '"SUMMARY" TEXT, '
                        '"LINK" VARCHAR(512), '
                        '"PUBLISHED_AT" DATETIME, '
                        '"FETCHED_AT" DATETIME)'
                    ))
                    conn.execute(text(
                        'INSERT INTO feed_subscription '
                        '("SUBSCRIPTION_ID", "FIXTURE_ID", "FEED_URL") VALUES '
                        "('s1', 'f1', 'https://example.com/feed')"
                    ))
                    conn.execute(text(
                        'INSERT INTO feed_item '
                        '(id, "SUBSCRIPTION_ID", "GUID", "TITLE", '
                        '"FETCHED_AT") '
                        "VALUES (1, 's1', 'g-1', NULL, NULL)"
                    ))
                    conn.execute(text(
                        'INSERT INTO feed_read_cursor '
                        '("PERSONA_ID", "SUBSCRIPTION_ID", "LAST_ITEM_ID") '
                        "VALUES ('p1', 's1', NULL), ('p1', 's1', 5)"
                    ))
            finally:
                engine.dispose()

            # 起動時の軽量シンクが NULL を埋める (黙って直さず WARNING)
            with self.assertLogs(level="WARNING") as logs:
                ensure_feed_tables(db_path)
            self.assertTrue(
                any("NULL をモデル既定値で埋めました" in m
                    for m in logs.output),
                logs.output,
            )
            engine = create_engine(f"sqlite:///{db_path}")
            try:
                with engine.connect() as conn:
                    create_sql = (conn.execute(text(
                        "SELECT sql FROM sqlite_master "
                        "WHERE type = 'table' AND name = 'feed_item'"
                    )).scalar() or "")
                    row = conn.execute(text(
                        'SELECT "TITLE", "SUMMARY", "LINK", "FETCHED_AT" '
                        'FROM feed_item WHERE id = 1'
                    )).fetchone()
                    cursors = [tuple(r) for r in conn.execute(text(
                        'SELECT "PERSONA_ID", "LAST_ITEM_ID" '
                        'FROM feed_read_cursor'
                    ))]
                # AUTOINCREMENT 済み = 再構築なしのまま NULL が埋まっている
                self.assertIn("AUTOINCREMENT", create_sql.upper())
                self.assertEqual(tuple(row[:3]), ("", "", ""))
                self.assertIsNotNone(row[3])
                # NULL → 0 に埋まった敗者は消え、既読が進んだ方が残る
                self.assertEqual(cursors, [("p1", 5)])
                # 破壊的差分を仕込み、後日の全書換に進む
                with engine.begin() as conn:
                    conn.execute(text(
                        'ALTER TABLE "AI" ADD COLUMN "LEGACY_JUNK" TEXT'
                    ))
            finally:
                engine.dispose()
            # NULL 残存が解消済みなので全書換も NOT NULL 違反にならない
            migrate_database_in_place(db_path)
            engine = create_engine(f"sqlite:///{db_path}")
            try:
                with engine.connect() as conn:
                    n_items = conn.execute(text(
                        'SELECT COUNT(*) FROM feed_item'
                    )).scalar()
                self.assertEqual(n_items, 1)
            finally:
                engine.dispose()


# ---------------------------------------------------------------------------
# City 所有権境界 (十二巡目 Q2)
# ---------------------------------------------------------------------------

class _RecordingAdapter:
    """配送の知覚投入を記録するだけの偽 SAIMemory adapter。"""

    def __init__(self):
        self.pushed = []

    def is_ready(self):
        return True

    def count_pending_perceptions(self, kind):
        return 0

    def has_pending_perception_marker(self, key, value):
        return False

    def push_perception(self, **kwargs):
        self.pushed.append(kwargs)


class FeedManagerCityIsolationTest(unittest.TestCase):
    """Q2 (十二巡目): City 所有権境界 — 複数 City の DB で、City A の
    FeedManager が City B のフィード施設・購読を列挙・取得・配送・表示更新
    しない。multi-city (inter-city travel) は凍結中だが、過去データで複数
    City を持つ DB は実在しうるための境界。"""

    OTHER_BUILDING = "b-other"
    OTHER_FIXTURE = "fx-other"
    OTHER_SUB = "sub-other"
    OTHER_URL = "https://other-city.example.com/feed.xml"

    def setUp(self):
        self.engine, self.fake = _make_fake_manager()
        self.addCleanup(self.engine.dispose)
        self.fm = FeedManager(self.fake)

        # 自 City (city_id = fake.city_id) 側の施設と購読
        fixture = self.fm.create_feed_fixture(BUILDING_ID, "自City新聞スタンド")
        self.fixture_id = fixture.FIXTURE_ID
        self.sub_id = self.fm.add_subscription(
            self.fixture_id, "https://example.com/feed.xml", title="自Cityフィード",
        ).SUBSCRIPTION_ID

        # 別 City の Building / フィード施設 / 購読 / 記事を直接 DB へ入れる
        from database.models import Fixture as FixtureModel
        db = self.fake.SessionLocal()
        try:
            other_city = City(
                USERID=1, CITYNAME="other_city", UI_PORT=3002, API_PORT=8002,
            )
            db.add(other_city)
            db.flush()
            self.other_city_id = other_city.CITYID
            db.add(Building(
                CITYID=self.other_city_id, BUILDINGID=self.OTHER_BUILDING,
                BUILDINGNAME="他所",
            ))
            db.add(FixtureModel(
                FIXTURE_ID=self.OTHER_FIXTURE, BUILDING_ID=self.OTHER_BUILDING,
                NAME="別Cityスタンド", TYPE="feed_stand",
            ))
            db.add(FeedSubscription(
                SUBSCRIPTION_ID=self.OTHER_SUB,
                FIXTURE_ID=self.OTHER_FIXTURE,
                FEED_URL=self.OTHER_URL,
                TITLE="別Cityフィード",
            ))
            db.add(FeedItem(
                SUBSCRIPTION_ID=self.OTHER_SUB, GUID="og-1", TITLE="別City記事",
            ))
            db.commit()
        finally:
            db.close()

    def test_list_feed_fixtures_excludes_other_city(self):
        ids = {f.FIXTURE_ID for f in self.fm.list_feed_fixtures()}
        self.assertEqual(ids, {self.fixture_id})

    def test_list_subscriptions_of_other_city_fixture_is_empty(self):
        self.assertEqual(self.fm.list_subscriptions(self.OTHER_FIXTURE), [])
        # 自 City の施設は従来どおり見える
        self.assertEqual(
            [s.SUBSCRIPTION_ID for s in self.fm.list_subscriptions(self.fixture_id)],
            [self.sub_id],
        )

    def test_fetch_all_does_not_fetch_other_city_subscription(self):
        fetched_urls = []

        def fake_fetch(url, **kwargs):
            fetched_urls.append(url)
            from saiverse.feed_fetch import FeedFetchResult
            return FeedFetchResult(url=url, title="t")

        with patch("saiverse.feed_manager.fetch_feed", side_effect=fake_fetch):
            self.fm._fetch_all()
        self.assertEqual(fetched_urls, ["https://example.com/feed.xml"])

    def test_update_displays_do_not_touch_other_city_fixture(self):
        from database.models import Fixture as FixtureModel
        # 全施設の一括更新も、fixture_id 直指定も、別 City には書かない
        self.fm._update_all_fixture_displays()
        self.fm.update_fixture_display(self.OTHER_FIXTURE)
        db = self.fake.SessionLocal()
        try:
            other = db.query(FixtureModel).filter(
                FixtureModel.FIXTURE_ID == self.OTHER_FIXTURE
            ).first()
            self.assertIsNone(other.STATE_JSON)
            mine = db.query(FixtureModel).filter(
                FixtureModel.FIXTURE_ID == self.fixture_id
            ).first()
            self.assertIn("feed_stand", mine.STATE_JSON or "")
        finally:
            db.close()

    def test_deliver_does_not_serve_other_city_building(self):
        adapter = _RecordingAdapter()
        persona = SimpleNamespace(
            sai_memory=adapter, current_building_id=self.OTHER_BUILDING,
        )
        self.fake.personas = {"tester": persona}
        self.fake.occupants = {self.OTHER_BUILDING: ["tester"]}
        self.assertEqual(self.fm.deliver_new_items(), 0)
        self.assertEqual(adapter.pushed, [])
        db = self.fake.SessionLocal()
        try:
            self.assertEqual(db.query(FeedReadCursor).count(), 0)
        finally:
            db.close()

    # ------------------------------------------------------------------
    # R1〜R3 (十三巡目): 最終操作の DB 条件 (事前確認・キャッシュを認可根拠に
    # しない)
    # ------------------------------------------------------------------

    def _other_city_item_guids(self):
        db = self.fake.SessionLocal()
        try:
            return [
                it.GUID
                for it in db.query(FeedItem)
                .filter(FeedItem.SUBSCRIPTION_ID == self.OTHER_SUB)
                .order_by(FeedItem.id)
                .all()
            ]
        finally:
            db.close()

    def _move_own_fixture_to_other_city(self):
        """自 City のフィード施設を別 City の Building へ付け替える (取得中の
        Building 削除 + ID 再利用で所有が変わるシナリオの再現)。"""
        from database.models import Fixture as FixtureModel
        db = self.fake.SessionLocal()
        try:
            fixture = db.query(FixtureModel).filter(
                FixtureModel.FIXTURE_ID == self.fixture_id
            ).first()
            fixture.BUILDING_ID = self.OTHER_BUILDING
            db.commit()
        finally:
            db.close()

    def _own_sub(self):
        db = self.fake.SessionLocal()
        try:
            return db.query(FeedSubscription).filter(
                FeedSubscription.SUBSCRIPTION_ID == self.sub_id
            ).first()
        finally:
            db.close()

    def test_prune_does_not_touch_other_city_items(self):
        """R2 (十三巡目): 剪定は現 City の購読に限る — City A のサイクルが
        City B の記事の件数・内容を変えない。"""
        os.environ["SAIVERSE_FEED_ITEM_KEEP"] = "1"
        os.environ["SAIVERSE_FEED_MAX_ITEMS_PER_PUSH"] = "1"
        self.addCleanup(lambda: os.environ.pop("SAIVERSE_FEED_ITEM_KEEP", None))
        self.addCleanup(
            lambda: os.environ.pop("SAIVERSE_FEED_MAX_ITEMS_PER_PUSH", None)
        )
        db = self.fake.SessionLocal()
        try:
            for i in range(3):
                db.add(FeedItem(
                    SUBSCRIPTION_ID=self.sub_id, GUID=f"mine-{i}",
                    TITLE=f"自City記事{i}",
                ))
                db.add(FeedItem(
                    SUBSCRIPTION_ID=self.OTHER_SUB, GUID=f"other-{i}",
                    TITLE=f"別City記事{i}",
                ))
            db.commit()
        finally:
            db.close()
        before = self._other_city_item_guids()  # setUp の og-1 + other-0..2
        self.fm._prune_old_items()
        # 別 City 側は件数も内容も不変
        self.assertEqual(self._other_city_item_guids(), before)
        # 対照: 自 City 側の剪定自体は生きている (keep=1)
        db = self.fake.SessionLocal()
        try:
            mine = db.query(FeedItem).filter(
                FeedItem.SUBSCRIPTION_ID == self.sub_id
            ).count()
        finally:
            db.close()
        self.assertEqual(mine, 1)

    def test_fetch_result_not_saved_when_ownership_moves_during_fetch(self):
        """R3 (十三巡目): ネットワーク中に施設が別 City 所有へ変わったら、
        保存 transaction の所有権条件が書き込みを止める (記事も購読状態も
        書かない)。"""
        from saiverse.feed_fetch import FeedEntry, FeedFetchResult

        def fake_fetch(url, **kwargs):
            self._move_own_fixture_to_other_city()
            return FeedFetchResult(url=url, title="t", entries=[
                FeedEntry(
                    guid="raced-1", title="取得中に越境", summary="",
                    link="", published=None,
                ),
            ])

        with patch("saiverse.feed_manager.fetch_feed", side_effect=fake_fetch):
            self.assertEqual(self.fm._fetch_one(self.sub_id), 0)
        db = self.fake.SessionLocal()
        try:
            saved = db.query(FeedItem).filter(
                FeedItem.SUBSCRIPTION_ID == self.sub_id
            ).count()
        finally:
            db.close()
        self.assertEqual(saved, 0)
        sub = self._own_sub()
        self.assertIsNone(sub.LAST_OK_AT)
        self.assertIsNone(sub.ETAG)

    def test_fetch_failure_not_recorded_when_ownership_moves_during_fetch(self):
        """R3 (十三巡目): 失敗記録も同じ所有権条件 — 別 City 所有になった
        購読へは CONSECUTIVE_FAILURES / LAST_ERROR を書かない。"""
        def fake_fetch(url, **kwargs):
            self._move_own_fixture_to_other_city()
            raise FeedFetchError("接続に失敗しました", kind="network")

        with patch("saiverse.feed_manager.fetch_feed", side_effect=fake_fetch):
            self.assertEqual(self.fm._fetch_one(self.sub_id), 0)
        sub = self._own_sub()
        self.assertEqual(sub.CONSECUTIVE_FAILURES or 0, 0)
        self.assertIsNone(sub.LAST_ERROR)

    def test_create_feed_fixture_in_other_city_building_rejected(self):
        """R1 (十三巡目): 施設作成は INSERT と同一 transaction の DB 条件で
        「現 City の実在 Building」を検証する — 別 City の Building は
        LookupError で拒否、施設は作られない。"""
        from database.models import Fixture as FixtureModel
        with self.assertRaises(LookupError):
            self.fm.create_feed_fixture(self.OTHER_BUILDING, "越境スタンド")
        db = self.fake.SessionLocal()
        try:
            # setUp の自 City 1 個 + 別 City 1 個のまま増えていない
            self.assertEqual(db.query(FixtureModel).count(), 2)
        finally:
            db.close()

    def test_create_fixture_from_preset_in_other_city_building_rejected(self):
        """R1 (十三巡目): プリセット作成も同じ DB 条件 — 施設も購読も
        作られない (全か無か)。"""
        from database.models import Fixture as FixtureModel
        from saiverse import feed_presets
        preset = {
            "id": "p", "name": "越境プリセット",
            "feeds": [{"url": "https://example.com/a.rss", "title": "A"}],
        }
        with patch.object(feed_presets, "FEED_PRESETS", {"p": preset}):
            with self.assertRaises(LookupError):
                self.fm.create_fixture_from_preset(self.OTHER_BUILDING, "p")
        db = self.fake.SessionLocal()
        try:
            self.assertEqual(db.query(FixtureModel).count(), 2)
            self.assertEqual(db.query(FeedSubscription).count(), 2)
        finally:
            db.close()

    def test_add_subscription_to_other_city_fixture_rejected(self):
        """R1 (十三巡目): 購読追加の Fixture 存在確認は INSERT と同一
        transaction 内で Building JOIN (現 City 所有) を条件に持つ — 別 City
        の Fixture は存在ごと伏せて拒否。"""
        with self.assertRaises(ValueError):
            self.fm.add_subscription(
                self.OTHER_FIXTURE, "https://example.com/new.xml",
            )
        db = self.fake.SessionLocal()
        try:
            count = db.query(FeedSubscription).filter(
                FeedSubscription.FIXTURE_ID == self.OTHER_FIXTURE
            ).count()
        finally:
            db.close()
        self.assertEqual(count, 1)  # setUp の 1 本のまま

    def test_remove_other_city_subscription_returns_false_and_survives(self):
        """R1 (十三巡目): 削除対象の選別は削除と同一 transaction の City 条件
        — 別 City の購読は「該当なし」で行も記事も生き残る。"""
        self.assertFalse(self.fm.remove_subscription(self.OTHER_SUB))
        db = self.fake.SessionLocal()
        try:
            self.assertEqual(
                db.query(FeedSubscription).filter(
                    FeedSubscription.SUBSCRIPTION_ID == self.OTHER_SUB
                ).count(), 1,
            )
        finally:
            db.close()
        self.assertEqual(self._other_city_item_guids(), ["og-1"])

    # ------------------------------------------------------------------
    # S1〜S2 (十四巡目): 書き込み直前・DELETE 文自身の City 条件 (操作の
    # 途中で所有権が移るシナリオ)
    # ------------------------------------------------------------------

    def test_delivery_final_check_blocks_when_ownership_moves(self):
        """S1 (十四巡目): 候補選定後・カーソル commit 直前に施設が別 City の
        Building へ付け替わったら、カーソル commit も知覚投入も起きない —
        live 再検証クエリ自身が City 条件を運ぶ。"""
        db = self.fake.SessionLocal()
        try:
            db.add(FeedItem(
                SUBSCRIPTION_ID=self.sub_id, GUID="mine-1", TITLE="自City記事",
            ))
            db.commit()
        finally:
            db.close()
        adapter = _RecordingAdapter()
        outer = self

        class MovingPersona:
            """現在地確認 (カーソル commit 直前) の瞬間に施設の所有を別 City
            へ移す — 候補選定と live 再検証の間の越境を決定的に再現する。"""

            sai_memory = adapter

            @property
            def current_building_id(self):
                outer._move_own_fixture_to_other_city()
                return BUILDING_ID

        self.fake.personas = {"tester": MovingPersona()}
        self.fake.occupants = {BUILDING_ID: ["tester"]}
        self.assertEqual(self.fm.deliver_new_items(), 0)
        self.assertEqual(adapter.pushed, [])  # 知覚投入なし
        db = self.fake.SessionLocal()
        try:
            self.assertEqual(db.query(FeedReadCursor).count(), 0)  # commit なし
        finally:
            db.close()

    def test_prune_delete_carries_city_condition(self):
        """S2 (十四巡目): 剪定の列挙・境界計算の後、DELETE 実行前に施設の
        所有が別 City へ移ったら行は 1 行も消えない — DELETE 文自身が City
        条件を運ぶ (列挙は認可根拠にしない)。"""
        import saiverse.feed_manager as fm_mod
        os.environ["SAIVERSE_FEED_ITEM_KEEP"] = "1"
        os.environ["SAIVERSE_FEED_MAX_ITEMS_PER_PUSH"] = "1"
        self.addCleanup(lambda: os.environ.pop("SAIVERSE_FEED_ITEM_KEEP", None))
        self.addCleanup(
            lambda: os.environ.pop("SAIVERSE_FEED_MAX_ITEMS_PER_PUSH", None)
        )
        db = self.fake.SessionLocal()
        try:
            for i in range(3):
                db.add(FeedItem(
                    SUBSCRIPTION_ID=self.sub_id, GUID=f"mine-{i}",
                    TITLE=f"自City記事{i}",
                ))
            db.commit()
        finally:
            db.close()

        real = fm_mod.city_feed_fixture_ids
        calls = []

        def moving(db_arg, city_id):
            calls.append(1)
            if len(calls) == 2:  # 1 回目 = 購読列挙、2 回目 = DELETE 条件の構築
                self._move_own_fixture_to_other_city()
            return real(db_arg, city_id)

        with patch.object(fm_mod, "city_feed_fixture_ids", side_effect=moving):
            deleted = self.fm._prune_old_items()
        self.assertEqual(deleted, 0)
        db = self.fake.SessionLocal()
        try:
            count = db.query(FeedItem).filter(
                FeedItem.SUBSCRIPTION_ID == self.sub_id
            ).count()
        finally:
            db.close()
        self.assertEqual(count, 3)  # 所有が移った購読の行は消えていない

    def test_remove_subscription_delete_carries_city_condition(self):
        """S2 (十四巡目): 購読削除も DELETE 文自身が City 条件を運ぶ —
        DELETE 実行直前に所有が別 City へ移った購読は、本体も道連れ
        (記事・カーソル) も消えず False。"""
        import saiverse.feed_manager as fm_mod
        db = self.fake.SessionLocal()
        try:
            db.add(FeedItem(
                SUBSCRIPTION_ID=self.sub_id, GUID="mine-1", TITLE="自City記事",
            ))
            db.add(FeedReadCursor(
                PERSONA_ID="tester", SUBSCRIPTION_ID=self.sub_id, LAST_ITEM_ID=1,
            ))
            db.commit()
        finally:
            db.close()

        real = fm_mod.city_feed_fixture_ids

        def moving(db_arg, city_id):
            self._move_own_fixture_to_other_city()
            return real(db_arg, city_id)

        with patch.object(fm_mod, "city_feed_fixture_ids", side_effect=moving):
            self.assertFalse(self.fm.remove_subscription(self.sub_id))
        db = self.fake.SessionLocal()
        try:
            self.assertEqual(db.query(FeedSubscription).filter(
                FeedSubscription.SUBSCRIPTION_ID == self.sub_id
            ).count(), 1)
            self.assertEqual(db.query(FeedItem).filter(
                FeedItem.SUBSCRIPTION_ID == self.sub_id
            ).count(), 1)
            self.assertEqual(db.query(FeedReadCursor).filter(
                FeedReadCursor.SUBSCRIPTION_ID == self.sub_id
            ).count(), 1)
        finally:
            db.close()


# ---------------------------------------------------------------------------
# deadline 共有 (呼び出し側が渡す時間予算)
# ---------------------------------------------------------------------------

class DeadlineBudgetTest(unittest.TestCase):
    """J6: fetch_feed / discover_feed の deadline 引数 — 呼び出し側 (購読追加
    API) が直列の複数取得で 1 つの壁時計予算を共有できる。予算を使い切った後の
    呼び出しは HTTP を発行せず即 timeout。"""

    def setUp(self):
        _patch_public_dns(self)

    def _patch_get_counting(self):
        calls = []

        def fake_get(url, **kwargs):
            calls.append(url)
            return _rss_response()

        patcher = patch.object(feed_fetch.requests, "get", side_effect=fake_get)
        self.addCleanup(patcher.stop)
        patcher.start()
        return calls

    def test_fetch_feed_with_exhausted_deadline_times_out_without_network(self):
        calls = self._patch_get_counting()
        with self.assertRaises(FeedFetchError) as ctx:
            fetch_feed(
                "https://example.com/feed.xml", deadline=time.monotonic() - 1.0,
            )
        self.assertEqual(ctx.exception.kind, "timeout")
        self.assertEqual(calls, [])  # 予算切れなら HTTP を発行しない

    def test_discover_feed_with_exhausted_deadline_times_out_without_network(self):
        calls = self._patch_get_counting()
        with self.assertRaises(FeedFetchError) as ctx:
            discover_feed("https://example.com/", deadline=time.monotonic() - 1.0)
        self.assertEqual(ctx.exception.kind, "timeout")
        self.assertEqual(calls, [])

    def test_default_budget_applies_without_deadline(self):
        calls = self._patch_get_counting()
        result = fetch_feed("https://example.com/feed.xml")
        self.assertEqual(len(result.entries), 3)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()


class TestSafeLink:
    """記事リンクの scheme 検証 (外部フィード由来の javascript: 等を供給側で落とす)。"""

    def test_javascript_scheme_dropped(self):
        from saiverse.feed_fetch import _safe_link
        assert _safe_link("javascript:alert(1)") == ""

    def test_data_scheme_dropped(self):
        from saiverse.feed_fetch import _safe_link
        assert _safe_link("data:text/html,<script>1</script>") == ""

    def test_http_and_https_pass(self):
        from saiverse.feed_fetch import _safe_link
        assert _safe_link("https://example.com/a") == "https://example.com/a"
        assert _safe_link("http://example.com/a") == "http://example.com/a"

    def test_empty_and_relative_dropped(self):
        from saiverse.feed_fetch import _safe_link
        assert _safe_link("") == ""
        assert _safe_link("/relative/path") == ""

    def test_unparseable_link_neutralized(self):
        """N2 (九巡目): urlparse が ValueError を投げる悪性リンクは空文字に
        無害化する (例外を漏らしてフィード全体の取得を殺さない)。"""
        from saiverse.feed_fetch import _safe_link
        assert _safe_link("http://[bad") == ""


class SnapshotRetryTest(unittest.TestCase):
    """WAL の snapshot 昇格衝突 (SQLITE_BUSY_SNAPSHOT) の一回再試行 (十六巡目)。"""

    def test_retry_succeeds_on_second_attempt(self):
        from sqlalchemy.exc import OperationalError as OpErr
        from saiverse.observer_manager import run_with_snapshot_retry
        calls = []

        def op():
            calls.append(1)
            if len(calls) == 1:
                raise OpErr("stmt", {}, Exception("database is locked"))
            return "ok"

        self.assertEqual(
            run_with_snapshot_retry(op, context="test"), "ok",
        )
        self.assertEqual(len(calls), 2)

    def test_double_transient_failure_raises_exhausted(self):
        """一時的競合の二連敗は SnapshotRetryExhausted (専用型) になる。"""
        from sqlalchemy.exc import OperationalError as OpErr
        from saiverse.observer_manager import (
            SnapshotRetryExhausted, run_with_snapshot_retry,
        )

        def op():
            raise OpErr("stmt", {}, Exception("database is locked"))

        with self.assertRaises(SnapshotRetryExhausted):
            run_with_snapshot_retry(op, context="test")

    def test_record_metrics_gives_up_quietly_after_double_failure(self):
        """二連敗の record_metrics は例外でなく空リストで見送る (API 500 防止)。"""
        from sqlalchemy.exc import OperationalError as OpErr
        from saiverse import observer_manager as om
        _engine, manager = _make_fake_manager()
        obs = om.ObserverManager(manager)
        fixture = obs.create_fixture("fx-retry", BUILDING_ID, "観測台")
        obs.create_observer("ob-retry", fixture.FIXTURE_ID, exec_kind="push")
        with patch.object(
            om, "update_fixture_state_keys",
            side_effect=OpErr("stmt", {}, Exception("database is locked")),
        ):
            result = obs.record_metrics(
                "ob-retry", {"temperature": {"value_num": 20.5}},
            )
        self.assertEqual(result, [])

    def test_permanent_operational_error_propagates_without_retry(self):
        """恒久障害 (JSON1 不在等) は再試行せず伝播 (十七巡目)。"""
        from sqlalchemy.exc import OperationalError as OpErr
        from saiverse.observer_manager import run_with_snapshot_retry
        calls = []

        def op():
            calls.append(1)
            raise OpErr("stmt", {}, Exception("no such function: json_set"))

        with self.assertRaises(OpErr):
            run_with_snapshot_retry(op, context="test")
        self.assertEqual(len(calls), 1)

    def test_record_metrics_rerun_does_not_duplicate_history(self):
        """再実行の冪等性: 同 (observer, recorded_at) の履歴が重複しない。"""
        from saiverse import observer_manager as om
        from database.models import ObserverMetric as OM
        _engine, manager = _make_fake_manager()
        obs = om.ObserverManager(manager)
        fixture = obs.create_fixture("fx-idem", BUILDING_ID, "観測台")
        obs.create_observer("ob-idem", fixture.FIXTURE_ID, exec_kind="push")
        fixed_at = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
        with patch.object(om, "_utcnow", return_value=fixed_at) if hasattr(om, "_utcnow") else patch.object(
            om, "datetime", wraps=datetime
        ) as dt_mock:
            if hasattr(dt_mock, "now"):
                dt_mock.now.return_value = fixed_at
            obs.record_metrics("ob-idem", {"temperature": {"value_num": 1.0}})
            obs.record_metrics("ob-idem", {"temperature": {"value_num": 1.0}})
        db = manager.SessionLocal()
        try:
            rows = db.query(OM).filter(OM.OBSERVER_ID == "ob-idem").all()
            recorded_ats = {r.RECORDED_AT for r in rows}
            for at in recorded_ats:
                same = [r for r in rows if r.RECORDED_AT == at]
                self.assertEqual(
                    len(same), 1,
                    f"duplicate history rows for recorded_at={at}",
                )
        finally:
            db.close()

    def test_permanent_error_reaches_caller_not_empty_success(self):
        """恒久 OperationalError は record_metrics から伝播 (空成功に化けない)。"""
        from sqlalchemy.exc import OperationalError as OpErr
        from saiverse import observer_manager as om
        _engine, manager = _make_fake_manager()
        obs = om.ObserverManager(manager)
        fixture = obs.create_fixture("fx-perm", BUILDING_ID, "観測台")
        obs.create_observer("ob-perm", fixture.FIXTURE_ID, exec_kind="push")
        with patch.object(
            om, "update_fixture_state_keys",
            side_effect=OpErr("stmt", {}, Exception("no such function: json_set")),
        ):
            with self.assertRaises(OpErr):
                obs.record_metrics("ob-perm", {"temperature": {"value_num": 1.0}})


class PruneOrderingMatchesDeliveryTest(unittest.TestCase):
    """十九巡目: 剪定の「新しい」は配送の選択順位 (published desc) と同一。

    newest-first フィードの初回取り込み (若い id ほど新しい記事) で、
    id 順の剪定は配送がこれから選ぶ最新記事を削ってしまう — 未配送の
    最新が不可逆に消える欠陥の回帰固定。
    """

    def setUp(self):
        self.engine, self.fake = _make_fake_manager()
        from saiverse.feed_manager import FeedManager
        self.fm = FeedManager(self.fake)
        fixture = self.fm.create_feed_fixture(BUILDING_ID, "スタンド")
        db = self.fake.SessionLocal()
        try:
            sub = FeedSubscription(
                SUBSCRIPTION_ID="sub-nf", FIXTURE_ID=fixture.FIXTURE_ID,
                FEED_URL="https://example.com/feed", TITLE="NF", ENABLED=True,
            )
            db.add(sub)
            # newest-first: id=1 が最新 (published が最も新しい)
            base = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
            for i in range(1, 6):
                db.add(FeedItem(
                    SUBSCRIPTION_ID="sub-nf", GUID=f"g-{i}",
                    TITLE=f"記事{i}", SUMMARY="", LINK="",
                    PUBLISHED_AT=base.replace(hour=12 - i),
                ))
            db.commit()
        finally:
            db.close()

    def test_prune_keeps_newest_by_published_not_id(self):
        with patch.dict(os.environ, {"SAIVERSE_FEED_ITEM_KEEP": "3"}):
            self.fm._prune_old_items()
        db = self.fake.SessionLocal()
        try:
            guids = {
                r[0] for r in db.query(FeedItem.GUID).filter(
                    FeedItem.SUBSCRIPTION_ID == "sub-nf"
                ).all()
            }
        finally:
            db.close()
        # published が新しい g-1,g-2,g-3 が残る (id 降順なら g-3..g-5 が残って
        # 未配送の最新 g-1,g-2 が消えていた)
        self.assertEqual(guids, {"g-1", "g-2", "g-3"})
