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
    def __init__(self, text: str):
        self.text = text
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
