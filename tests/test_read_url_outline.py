"""read_url_outline スペルの契約テスト。

- 見出しの無い長いページでも、ペルソナに届く量は閾値で抑える (全文は返さない)
- 返さなかった続きは offset で読める
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from tools import web_fetch_helpers as wfh

_ROOT = Path(__file__).resolve().parents[1]


def _load_spell():
    spec = importlib.util.spec_from_file_location(
        "read_url_outline_under_test", _ROOT / "builtin_data/tools/read_url_outline.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.read_url_outline


read_url_outline = _load_spell()


class _FakeResponse:
    def __init__(self, text: str):
        self.text = text
        self.headers = {"Content-Type": "text/html; charset=utf-8"}
        self.apparent_encoding = "utf-8"
        self.encoding = "utf-8"

    def raise_for_status(self) -> None:
        return None


def _serve(monkeypatch, html: str) -> None:
    monkeypatch.setattr(wfh.requests, "get", lambda *a, **k: _FakeResponse(html))


# 見出しの無い長いページ。段落ごとに番号を振って、どこまで届いたかを見分ける。
_NO_HEADING_PAGE = "<html><body>" + "".join(
    f"<p>段落{i:03d}。" + "あ" * 40 + "</p>" for i in range(200)
) + "</body></html>"


def test_long_page_without_headings_returns_only_the_head(monkeypatch):
    _serve(monkeypatch, _NO_HEADING_PAGE)
    message, _ = read_url_outline("https://example.test/", full_threshold=1000)
    assert "段落000" in message
    assert "段落199" not in message
    assert "見出しが無い" in message
    assert 'offset="1000"' in message
    assert len(message) < 1500


def test_offset_continues_from_the_reported_position(monkeypatch):
    _serve(monkeypatch, _NO_HEADING_PAGE)
    first, _ = read_url_outline("https://example.test/", full_threshold=1000)
    second, _ = read_url_outline(
        "https://example.test/", full_threshold=1000, offset="1000"
    )
    md = wfh.fetch_page("https://example.test/").markdown
    assert f"1001〜2000 文字目（全 {len(md)} 文字）" in second
    # 一回目は 1000 文字目まで、二回目はその直後から: 読み飛ばしも重なりも無い
    assert md[:1000] in first
    assert md[:1001] not in first
    assert md[1000:2000] in second


def test_offset_reaches_the_end_of_the_page(monkeypatch):
    _serve(monkeypatch, _NO_HEADING_PAGE)
    total = len(wfh.fetch_page("https://example.test/").markdown)
    last_start = (total - 1) // 1000 * 1000
    message, _ = read_url_outline(
        "https://example.test/", full_threshold=1000, offset=str(last_start)
    )
    assert "段落199" in message
    assert "ページの終わり" in message


def test_offset_past_the_end_is_reported(monkeypatch):
    _serve(monkeypatch, _NO_HEADING_PAGE)
    message, result = read_url_outline("https://example.test/", offset="99999999")
    assert "超えています" in message
    assert result.history_snippet is None


@pytest.mark.parametrize("bad", ["abc", "-5"])
def test_offset_must_be_a_non_negative_number(monkeypatch, bad):
    _serve(monkeypatch, _NO_HEADING_PAGE)
    message, _ = read_url_outline("https://example.test/", offset=bad)
    assert "0 以上の数字" in message


def test_short_page_is_still_returned_whole(monkeypatch):
    _serve(monkeypatch, "<html><body><p>短いページ。</p></body></html>")
    message, _ = read_url_outline("https://example.test/")
    assert "短いページ。" in message
    assert "offset" not in message
