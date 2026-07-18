"""episode_read スペル (builtin_data/tools/episode_read.py) のテスト (W1 Chunk C / D10)。

- 正常系: episode ヘッダ (参照 / kind / 表題 / 期間) + 原本全文 (切り詰めなし)
- episode 不在 / 原本 0 件は丁寧語でその旨を返す
- schema: spell=True / episode 引数 required
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import Base
from saiverse import clock
from saiverse import episodes as ep_mod
from tool_loader import load_builtin_tool

PERSONA_ID = "alice"
BASE_TIME = datetime(2026, 7, 4, 10, 0, 0)


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    yield Session
    engine.dispose()


@pytest.fixture(autouse=True)
def _virtual_clock():
    clock.enable_virtual(BASE_TIME)
    yield
    clock.disable_virtual()


class TranscriptAdapter:
    """get_messages_by_origin_episode の canned 応答スタブ。"""

    def __init__(self):
        self.rows: List[Dict[str, Any]] = []
        self.queries: List[str] = []

    def get_messages_by_origin_episode(self, episode_ref):
        self.queries.append(episode_ref)
        return list(self.rows)


@pytest.fixture
def manager(session_factory):
    persona = SimpleNamespace(
        persona_id=PERSONA_ID,
        persona_name="Alice",
        current_building_id="alice_room",
        sai_memory=TranscriptAdapter(),
    )
    return SimpleNamespace(
        SessionLocal=session_factory,
        personas={PERSONA_ID: persona},
    )


@pytest.fixture
def tool_mod():
    return load_builtin_tool("episode_read")


def _ctx(manager, tmp_path):
    from tools.context import persona_context
    return persona_context(PERSONA_ID, tmp_path, manager=manager)


def _make_episode(manager, *, title="共有文の下書きを書く", close=True):
    ep = ep_mod.open_episode(
        manager, PERSONA_ID, ep_mod.KIND_WORK_SESSION,
        building_id="workshop", meta={"title": title},
    )
    if close:
        ep_mod.close_episode(manager, PERSONA_ID, ep["episode_ref"])
    return ep["episode_ref"]


def test_episode_read_renders_header_and_full_transcript(manager, tool_mod, tmp_path):
    episode_ref = _make_episode(manager)
    adapter = manager.personas[PERSONA_ID].sai_memory
    long_text = "長い本文。" * 500  # 2500 文字 — 切り詰めなしの検証
    adapter.rows = [
        {"role": "assistant",
         "content": "本文を書く。\n/spell name='document_create' args={\"title\": \"下書き\"}",
         "created_at": int(BASE_TIME.timestamp())},
        {"role": "system",
         "content": "文書「下書き」を作成しました。",
         "created_at": int(BASE_TIME.timestamp()) + 60},
        {"role": "assistant",
         "content": long_text,
         "created_at": int(BASE_TIME.timestamp()) + 120},
    ]

    with _ctx(manager, tmp_path):
        text = tool_mod.episode_read(episode=episode_ref)

    # ヘッダ: 参照 / kind 表示名 / 表題 / 期間
    assert f"出来事 {episode_ref}（作業セッション）" in text
    assert "表題: 共有文の下書きを書く" in text
    assert "期間: 2026-07-04 10:00 〜 2026-07-04 10:00" in text
    # 原本: 発話者 + 内容 (スペル行・結果も読める)
    assert "Alice:" in text
    assert "システム:" in text
    assert "/spell name='document_create'" in text
    assert "文書「下書き」を作成しました。" in text
    # 全文 (切り詰めなし)
    assert long_text in text
    assert adapter.queries == [episode_ref]


def test_episode_read_episode_not_found(manager, tool_mod, tmp_path):
    with _ctx(manager, tmp_path):
        text = tool_mod.episode_read(episode="episode:999")
    assert "episode:999" in text
    assert "見つかりませんでした" in text


def test_episode_read_empty_transcript(manager, tool_mod, tmp_path):
    episode_ref = _make_episode(manager)
    with _ctx(manager, tmp_path):
        text = tool_mod.episode_read(episode=episode_ref)
    assert f"出来事 {episode_ref}" in text
    assert "この出来事の記録は残っていません。" in text


def test_episode_read_empty_argument(manager, tool_mod, tmp_path):
    with _ctx(manager, tmp_path):
        text = tool_mod.episode_read(episode="")
    assert "episode:N の形式" in text


def test_episode_read_adapter_without_reader(manager, tool_mod, tmp_path):
    """読み口の無い adapter (旧スタブ) でも丁寧語で取得不能を返す。"""
    episode_ref = _make_episode(manager)
    manager.personas[PERSONA_ID].sai_memory = SimpleNamespace()  # 読み口なし
    with _ctx(manager, tmp_path):
        text = tool_mod.episode_read(episode=episode_ref)
    assert "取得できませんでした" in text


def test_episode_read_schema_is_spell(tool_mod):
    schema = tool_mod.schema()
    assert schema.name == "episode_read"
    assert schema.spell is True
    assert schema.spell_visible is True
    assert schema.parameters["required"] == ["episode"]
    assert schema.result_type == "string"
