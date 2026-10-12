"""Embedding モデル変更の起動時警告のテスト。

SAIMemoryAdapter は起動時に、記録された embedding モデルといまのモデルを比べ、
さらに 4 系統のベクトルの長さを標本で確かめて、合わなければ ``embed_model_changed``
を立てる (saiverse_memory/adapter.py。判定そのもののテストは
tests/test_embedding_migration.py)。ペルソナの読み込みが終わったら、manager は
その旗が立っているペルソナを集めて ``embed_model_mismatch`` の警告を積む
(manager/persona.py ``_load_personas_from_db``)。再計算が成功したペルソナは
``clear_embed_model_mismatch`` で警告から外れる。

ここで押さえること:

- adapter は PersonaCore の ``sai_memory`` から読む。旗が立っている人だけが警告に載る
- adapter を持たない (``sai_memory`` が None の) ペルソナは警告の対象にならない
- 再計算を終えたペルソナは旗が下り、警告から外れる。最後の一人が外れたら警告ごと消える
- 前回の再計算が途中で死んで「移行中」の印が残った memory.db は、ベクトルが
  一つも無くても本物の adapter が旗を立て、警告に載る

ペルソナの読み込み (_load_single_persona) はスタブに差し替え、PersonaCore も
SAIMemory も本物は作らない。DB はメモリ上の SQLite。LLM は呼ばない。
"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import AI as AIModel, Base, City as CityModel
from manager.persona import PersonaMixin
from sai_memory.config import EMBED_DIM, EMBED_MODEL_ID, load_settings
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


class _FakeEmbedder:
    """本物のモデルを読まずに adapter を初期化するための偽物。"""

    embedding_size = EMBED_DIM

    def __init__(self, *args, **kwargs) -> None:
        pass

    def embed(self, texts, *, is_query=False):
        return [[0.0] * EMBED_DIM for _ in texts]


def test_unfinished_reembed_mark_warns_even_without_vectors(session_factory, tmp_path):
    from sai_memory.embedding_migration import begin_reembed, has_any_vectors
    from sai_memory.memory.storage import init_db

    conn = init_db(str(tmp_path / "memory.db"), check_same_thread=False)
    try:
        begin_reembed(conn, EMBED_MODEL_ID)
        assert has_any_vectors(conn) is False
    finally:
        conn.close()

    settings = replace(load_settings(), memory_enabled=True)
    with patch("saiverse_memory.adapter.Embedder", _FakeEmbedder):
        adapter = SAIMemoryAdapter("air_city_a", persona_dir=tmp_path, settings=settings)
    try:
        assert adapter.embed_model_changed is True
        svc = _start(session_factory, {"air_city_a": adapter})
        assert [w["persona_ids"] for w in _mismatch_warnings(svc)] == [["air_city_a"]]
    finally:
        adapter.close()


def test_clear_after_reembed_lifts_the_warning(session_factory):
    air = SimpleNamespace(embed_model_changed=True)
    miku = SimpleNamespace(embed_model_changed=True)
    svc = _start(session_factory, {"air_city_a": air, "miku_city_a": miku})
    shared = svc.startup_warnings

    svc.clear_embed_model_mismatch("air_city_a")

    assert air.embed_model_changed is False
    assert miku.embed_model_changed is True
    warnings = _mismatch_warnings(svc)
    assert [w["persona_ids"] for w in warnings] == [["miku_city_a"]]
    assert "air_city_a" not in warnings[0]["message"]

    svc.clear_embed_model_mismatch("miku_city_a")

    assert svc.startup_warnings == []
    # AdminService と共有しているリストを差し替えずに中身だけ変える
    assert svc.startup_warnings is shared
