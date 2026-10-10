"""Embedding モデル変更の起動時警告のテスト。

SAIMemoryAdapter は起動時に、記録された embedding モデルといまのモデルを比べ、
違えば ``embed_model_changed`` を立てる (saiverse_memory/adapter.py)。
ペルソナの読み込みが終わったら、manager はその旗が立っているペルソナを集めて
``embed_model_mismatch`` の警告を積む (manager/persona.py ``_load_personas_from_db``)。

ここで押さえること:

- adapter は PersonaCore の ``sai_memory`` から読む。旗が立っている人だけが警告に載る
- adapter を持たない (``sai_memory`` が None の) ペルソナは警告の対象にならない

ペルソナの読み込み (_load_single_persona) はスタブに差し替え、PersonaCore も
SAIMemory も本物は作らない。DB はメモリ上の SQLite。LLM は呼ばない。
"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import AI as AIModel, Base, City as CityModel
from manager.persona import PersonaMixin
from sai_memory.config import load_settings
from saiverse_memory.adapter import SAIMemoryAdapter


class _Manager(PersonaMixin):
    """_load_personas_from_db だけを動かす最小の manager。"""


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    try:
        db.add(CityModel(
            CITYID=1, USERID=1, CITY_SLUG="city_a", UI_PORT=3000, API_PORT=8000,
        ))
        db.commit()
    finally:
        db.close()
    yield factory
    engine.dispose()


def _start(session_factory, adapters):
    """adapters: {persona_id: sai_memory に入れる値} でペルソナを読み込む。"""
    db = session_factory()
    try:
        for pid in adapters:
            db.add(AIModel(AIID=pid, HOME_CITYID=1, AINAME=pid))
        db.commit()
    finally:
        db.close()

    svc = _Manager.__new__(_Manager)
    svc.SessionLocal = session_factory
    svc.city_id = 1
    svc.personas = {}
    svc.startup_warnings = []

    def _load_single_persona(_db, db_ai):
        # PersonaCore と同じく、adapter は sai_memory に持たせる。
        svc.personas[db_ai.AIID] = SimpleNamespace(sai_memory=adapters[db_ai.AIID])

    svc._load_single_persona = _load_single_persona
    svc._load_personas_from_db()
    return svc


def _mismatch_warnings(svc):
    return [w for w in svc.startup_warnings if w.get("source") == "embed_model_mismatch"]


def test_changed_adapter_produces_mismatch_warning(session_factory):
    svc = _start(session_factory, {
        "air_city_a": SimpleNamespace(embed_model_changed=True),
        "miku_city_a": SimpleNamespace(embed_model_changed=False),
    })

    warnings = _mismatch_warnings(svc)
    assert len(warnings) == 1
    assert warnings[0]["persona_ids"] == ["air_city_a"]
    assert "Embeddingモデルが変更されました" in warnings[0]["message"]
    assert "air_city_a" in warnings[0]["message"]
    # 集計の失敗が別の警告として積まれていないこと
    assert [w["source"] for w in svc.startup_warnings] == ["embed_model_mismatch"]


def test_no_warning_when_unchanged_or_adapter_missing(session_factory):
    svc = _start(session_factory, {
        "air_city_a": SimpleNamespace(embed_model_changed=False),
        "miku_city_a": None,
    })

    assert svc.startup_warnings == []


def test_disabled_adapter_does_not_break_startup_check(session_factory, tmp_path):
    """SAIMEMORY_MEMORY=0 の adapter は早期 return するが、旗は持っている。"""
    settings = replace(load_settings(), memory_enabled=False)
    adapter = SAIMemoryAdapter("air_city_a", persona_dir=tmp_path, settings=settings)
    assert adapter.conn is None
    assert adapter.embed_model_changed is False

    svc = _start(session_factory, {"air_city_a": adapter})

    assert svc.startup_warnings == []
