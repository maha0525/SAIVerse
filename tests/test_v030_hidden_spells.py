"""v0.3.0 で UI のスペル一覧から隠したスペルの検証 (2026-09-01 裁定)。

- `tell` (声をかける): 自律行動が未出荷なので、通常会話で無駄に唱えられるのを避ける。
- `observer_read` (オブザーバー観測値取得): オブザーバー機能自体がまだ使えない。

どちらも退場ではなく **非表示** — `spell=True` のままペルソナからは実行でき、
head のスペル一覧と、スペルを一覧して選ばせる UI の口には出さない。出荷時に
`spell_visible=True` へ戻す。

UI の口は二つあるので両方を押さえる (片方だけ塞ぐと、もう片方から選べてしまう):

- `/api/people/spells` — アラーム管理の「使用するスペル」など
- `/api/people/realtime-spell-catalog` — ペルソナ設定・建物設定のリアルタイムスペル
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from builtin_data.tools.observer_read import schema as observer_read_schema
from builtin_data.tools.tell import schema as tell_schema

HIDDEN_SCHEMAS = [tell_schema, observer_read_schema]


@pytest.mark.parametrize("schema_fn", HIDDEN_SCHEMAS)
def test_hidden_spell_stays_executable_but_invisible(schema_fn):
    schema = schema_fn()
    # 実行可否は変えない (ペルソナからは唱えられる)
    assert schema.spell is True
    # UI とプロンプトのスペル一覧には出さない
    assert schema.spell_visible is False


VISIBLE_NAME = "episode_read"


def _registry_with_hidden_and_one_visible():
    """非表示 2 件 + 可視 1 件のレジストリ。

    可視の 1 件を混ぜておくのは、「全部消えている」のと「非表示だけ落ちている」
    のを区別するため — 一覧が空になるバグでも通ってしまう検査にしない。
    """
    from builtin_data.tools.episode_read import schema as visible_schema

    registry = {fn().name: fn() for fn in HIDDEN_SCHEMAS}
    registry[VISIBLE_NAME] = visible_schema()
    return registry


def test_api_spell_list_excludes_the_hidden_spells():
    """/api/people/spells の応答に tell / observer_read が出ないこと。"""
    from api.routes.people.summon import list_available_spells

    with patch("tools.SPELL_TOOL_SCHEMAS", _registry_with_hidden_and_one_visible()):
        results = list_available_spells(manager=None)

    names = {r["name"] for r in results}
    assert names == {VISIBLE_NAME}


def test_realtime_spell_catalog_excludes_the_hidden_spells():
    """リアルタイムスペルの選択肢にも tell / observer_read が出ないこと。"""
    from api.routes.people.realtime_spell import get_spell_catalog

    with patch("tools.SPELL_TOOL_SCHEMAS", _registry_with_hidden_and_one_visible()):
        results = get_spell_catalog()

    names = {r["name"] for r in results}
    assert names == {VISIBLE_NAME}
