"""tools/web_fetch_helpers.py の契約テスト。

read_url_outline はページの全体像を渡すスペルなので、どんなページでも
ページ内の見出しは全部見えなければならない。本文の場所を推測して一部だけを
残すと、この約束より少ない情報を返してしまう (2026-11-13、saiverse.net で
4 枚の <article> カードの 1 枚目だけが本文とされ、97 文字しか返らなかった)。
"""
from __future__ import annotations

import pytest

from tools import web_fetch_helpers as wfh

# saiverse.net (VitePress) と同じ形: <main> が無く、機能紹介のカードが
# 複数の <article> で、その後ろに本文の節が続く。
_CARD_PAGE = """<!doctype html><html><head><title>Cards</title></head><body>
<div class="VPContent">
  <h1>サイトの題</h1>
  <article class="box"><h2>カード1</h2><p>一枚目の説明。</p></article>
  <article class="box"><h2>カード2</h2><p>二枚目の説明。</p></article>
  <h2>本文の節</h2><p>ここが一番読ませたい本文。</p>
</div>
</body></html>"""


class _FakeResponse:
    def __init__(self, text: str, url: str = "https://example.test/"):
        self.text = text
        self.url = url  # requests はリダイレクト後の URL をここに持つ
        self.headers = {"Content-Type": "text/html; charset=utf-8"}
        self.apparent_encoding = "utf-8"
        self.encoding = "utf-8"

    def raise_for_status(self) -> None:
        return None


@pytest.fixture
def card_page(monkeypatch):
    monkeypatch.setattr(
        wfh.requests, "get", lambda *a, **k: _FakeResponse(_CARD_PAGE)
    )
    return wfh.fetch_page("https://example.test/")


def test_fetch_page_keeps_whole_page_when_multiple_articles(card_page):
    md = card_page.markdown
    for text in ("サイトの題", "一枚目の説明", "二枚目の説明", "ここが一番読ませたい本文"):
        assert text in md


def test_outline_lists_every_heading_of_the_page(card_page):
    headings = [e.text for e in wfh.extract_outline(card_page.body_soup)]
    assert headings == ["サイトの題", "カード1", "カード2", "本文の節"]


def test_section_outside_first_article_is_reachable(card_page):
    result = wfh.extract_section_by_heading(card_page.body_soup, "本文の節", 10000)
    assert result is not None
    heading, section_md = result
    assert heading == "本文の節"
    assert "ここが一番読ませたい本文" in section_md


# ブログ記事によくある形: タイトルは <article> 内の <header>、注意書きは <aside>。
_BLOG_PAGE = """<!doctype html><html><head><title>Blog</title></head><body>
<header class="site-header"><a href="/">ブログ名</a><nav><a href="/about">メニュー</a></nav></header>
<article>
  <header class="entry-header"><h1 class="entry-title">記事のタイトル</h1></header>
  <h2>はじめに</h2><p>本文。</p>
  <aside class="note"><p>注意: この手順は管理者権限が必要です。</p></aside>
  <h2>手順</h2><p>本文2。</p>
</article>
<footer>コピーライト</footer>
</body></html>"""


def test_title_in_header_and_note_in_aside_are_kept(monkeypatch):
    monkeypatch.setattr(wfh.requests, "get", lambda *a, **k: _FakeResponse(_BLOG_PAGE))
    page = wfh.fetch_page("https://example.test/post")
    headings = [e.text for e in wfh.extract_outline(page.body_soup)]
    assert headings == ["記事のタイトル", "はじめに", "手順"]
    assert "注意: この手順は管理者権限が必要です。" in page.markdown
    # メニュー (<nav>) とフッターは従来どおり消える
    assert "メニュー" not in page.markdown
    assert "コピーライト" not in page.markdown


_LINK_PAGE = """<html><body>
<p><a href="/guide/why">絶対パス</a> <a href="next.html">相対パス</a>
<a href="https://other.test/x">外部</a> <a href="#sec">ページ内</a>
<a href="mailto:a@example.test">メール</a> <img src="img/pic.png" alt="絵"></p>
</body></html>"""


def test_relative_links_become_full_urls_based_on_the_final_url(monkeypatch):
    # /old に頼んだら /docs/page へリダイレクトされた、という応答
    monkeypatch.setattr(
        wfh.requests, "get",
        lambda *a, **k: _FakeResponse(_LINK_PAGE, url="https://example.test/docs/page"),
    )
    page = wfh.fetch_page("https://example.test/old")
    md = page.markdown
    assert page.url == "https://example.test/docs/page"
    assert "(https://example.test/guide/why)" in md
    assert "(https://example.test/docs/next.html)" in md
    assert "(https://other.test/x)" in md
    assert "(#sec)" in md
    assert "(mailto:a@example.test)" in md
    assert "(https://example.test/docs/img/pic.png)" in md


def test_base_href_takes_priority_for_relative_links(monkeypatch):
    html = '<html><head><base href="https://cdn.example.test/root/"></head>' \
           '<body><a href="a.html">リンク</a></body></html>'
    monkeypatch.setattr(wfh.requests, "get", lambda *a, **k: _FakeResponse(html))
    page = wfh.fetch_page("https://example.test/")
    assert "(https://cdn.example.test/root/a.html)" in page.markdown


def test_broken_link_is_kept_and_does_not_stop_reading(monkeypatch):
    """PR #480 のレビュー指摘: urljoin が ValueError を出す壊れた URL が一つあっても、
    ページは読めて、他のリンクは完全な URL になる。"""
    html = ('<html><body><p>本文はここ。<a href="https://[broken">壊れた</a> '
            '<a href="/ok">正常</a> <img src="//[x/pic.png" alt="壊れた絵"></p></body></html>')
    monkeypatch.setattr(wfh.requests, "get", lambda *a, **k: _FakeResponse(html))
    page = wfh.fetch_page("https://example.test/")
    assert "本文はここ。" in page.markdown
    assert "(https://example.test/ok)" in page.markdown
    assert "(https://[broken)" in page.markdown


def test_broken_base_href_falls_back_to_the_page_url(monkeypatch):
    html = '<html><head><base href="https://[broken/"></head>' \
           '<body><a href="a.html">リンク</a></body></html>'
    monkeypatch.setattr(
        wfh.requests, "get",
        lambda *a, **k: _FakeResponse(html, url="https://example.test/dir/page"),
    )
    page = wfh.fetch_page("https://example.test/dir/page")
    assert "(https://example.test/dir/a.html)" in page.markdown
