"""Chronicle 帯予算の解決順 (2026-09-01 のペルソナ設定追加)。

解決順は **ペルソナ列 `AI.CHRONICLE_CHAR_BUDGET` (非 NULL) > env
`SAIVERSE_CHRONICLE_CHAR_BUDGET` > 既定 20,000 字**。

この順序は 2 つの部品に分かれて実装されている:

- 列の一段だけを解くのが `builtin_data/tools/get_memory_weave_context.py` の
  `_resolve_persona_chronicle_budget` (列が使えなければ sentinel を返す)
- 残りの env → 既定は `sai_memory/arasuji/context.py` の `_resolve_char_budget`

分かれているのは、解決順の全体を二箇所に書かないため。ここでは繋ぎ目 (前者が
sentinel を返したら後者が引き継ぐ) まで通して見る。
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# 本番と同じ import 経路で読む (head の weave section が使う形)。
from builtin_data.tools.get_memory_weave_context import (
    _resolve_persona_chronicle_budget,
)
from database.models import AI, Base, City, User
from sai_memory.arasuji.context import (
    DEFAULT_CHRONICLE_CHAR_BUDGET,
    USE_DEFAULT_BUDGET,
    _resolve_char_budget,
)

PERSONA_ID = "air_city_a"


@pytest.fixture
def manager():
    """`SessionLocal` だけを持つ最小のマネージャ (列の読み出しに使う属性)。"""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    try:
        db.add(User(USERID=1, PASSWORD="x", USERNAME="u"))
        db.flush()
        db.add(City(CITYID=1, USERID=1, CITY_SLUG="city_a", UI_PORT=3000, API_PORT=8000))
        db.flush()
        db.add(AI(AIID=PERSONA_ID, HOME_CITYID=1, AINAME="エア"))
        db.commit()
    finally:
        db.close()
    try:
        yield SimpleNamespace(SessionLocal=SessionLocal)
    finally:
        engine.dispose()


def _set_column(manager, value):
    db = manager.SessionLocal()
    try:
        db.query(AI).filter_by(AIID=PERSONA_ID).first().CHRONICLE_CHAR_BUDGET = value
        db.commit()
    finally:
        db.close()


def _resolve_end_to_end(manager, persona_id=PERSONA_ID):
    """列 → (sentinel なら) env → 既定、の全段を通した実効予算。"""
    from tools.context import persona_context

    with persona_context(persona_id, None, manager):
        first = _resolve_persona_chronicle_budget(
            persona_id, USE_DEFAULT_BUDGET,
        )
    return _resolve_char_budget(first)


# ---------------------------------------------------------------------------
# 三段の解決順
# ---------------------------------------------------------------------------


def test_column_wins_over_env_and_default(manager):
    _set_column(manager, 45_000)
    with patch.dict(os.environ, {"SAIVERSE_CHRONICLE_CHAR_BUDGET": "30000"}):
        assert _resolve_end_to_end(manager) == 45_000


def test_env_is_used_when_the_column_is_null(manager):
    _set_column(manager, None)
    with patch.dict(os.environ, {"SAIVERSE_CHRONICLE_CHAR_BUDGET": "30000"}):
        assert _resolve_end_to_end(manager) == 30_000


def test_builtin_default_when_neither_is_set(manager):
    _set_column(manager, None)
    with patch.dict(os.environ, {}, clear=True):
        assert _resolve_end_to_end(manager) == DEFAULT_CHRONICLE_CHAR_BUDGET
        assert DEFAULT_CHRONICLE_CHAR_BUDGET == 20_000


# ---------------------------------------------------------------------------
# 値の縁 (0 以下・欠落・読めない)
# ---------------------------------------------------------------------------


def test_non_positive_column_falls_back_to_the_default(manager):
    """0 / 負値は「未設定」に倒す。

    UI と manager/admin.py が 0 以下を NULL へ正規化しているので、本来この値は
    列に入らない — 手で書かれた場合の保険。列から「予算制そのものを切る 0」は
    表現できない (CORE_MEMORY_CHAR_BUDGET と同じ流儀)。
    """
    for bad in (0, -1):
        _set_column(manager, bad)
        with patch.dict(os.environ, {}, clear=True):
            assert _resolve_end_to_end(manager) == DEFAULT_CHRONICLE_CHAR_BUDGET


def test_unknown_persona_falls_back(manager):
    _set_column(manager, 45_000)
    with patch.dict(os.environ, {}, clear=True):
        assert _resolve_end_to_end(manager, "no_such_persona") == (
            DEFAULT_CHRONICLE_CHAR_BUDGET
        )


def test_missing_manager_falls_back_without_raising():
    """マネージャが取れない経路 (contextvars 未設定) でも例外にしない。"""
    assert _resolve_persona_chronicle_budget(
        PERSONA_ID, USE_DEFAULT_BUDGET,
    ) == USE_DEFAULT_BUDGET


def test_db_failure_falls_back_without_raising(manager):
    """列を読めなかったら既定へ倒す — weave の組み立てを落とさない。"""
    from tools.context import persona_context

    def _boom():
        raise RuntimeError("db is gone")

    broken = SimpleNamespace(SessionLocal=_boom)
    with persona_context(PERSONA_ID, None, broken):
        assert _resolve_persona_chronicle_budget(
            PERSONA_ID, USE_DEFAULT_BUDGET,
        ) == USE_DEFAULT_BUDGET
