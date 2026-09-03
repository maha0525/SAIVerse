"""取り込み元の「題名なし」の既定値を、スレッド名として保存しないことの固定。

chatgpt_importer は "(untitled)"、chatlog_exporter_importer は "Untitled" を、
題名の無い会話に入れる。大文字小文字や括弧の違いで素通りすると、スレッド一覧に
ダミーの題名が並ぶ。
"""
from __future__ import annotations

import pytest

from api.routes.people.import_chatlog import _is_placeholder_title


@pytest.mark.parametrize(
    "title",
    [None, "", "   ", "Untitled", "untitled", "UNTITLED", "(untitled)", "(Untitled)", " (untitled) "],
)
def test_placeholder_titles_are_not_saved(title):
    assert _is_placeholder_title(title) is True


@pytest.mark.parametrize("title", ["猫ロボットの話", "Untitled draft", "unt", "会話 (untitled) の続き"])
def test_real_titles_are_saved(title):
    assert _is_placeholder_title(title) is False
