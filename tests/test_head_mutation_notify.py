"""head 操作の内容型通知 (統合工事 §6-4) のテスト。

対象: sea/head_pipeline/notify.py / pipeline.advance_last_notified /
flush_diffs(advance=False) / integration.inject_diff_notifications の outbox 化 /
memory 系スペルの成功点結線。

固定する仕様 (beat_execution_context.md §3.3 / issue head_mutation_notification_gap):

1. notify_head_mutation は section の capture→render と**同一の text** を outbox
   payload (target='perception.push', kind='head_mutation') に載せる (別文面禁止)。
2. render が None の section (例: memopedia_index opt-in OFF) は通知しない。
3. (欠番 — 台帳なし環境の直接 push への degrade は 2026-09-28 監査で撤去。
   台帳は SAIVerseManager が無条件に持つ)
4. push 確定後、B (last_notified) は該当 persona の**全 model 行**で前進し、
   backstop flush_diffs が同じ変化を再通知しない。
5. ツール成功点 (memory_write → core_memory) から通知が発火する。
6. S3: inject_diff_notifications の B 前進は outbox 積みの durable 確定後。
   配送予約に失敗したら B は据え置かれ、次回 flush で再検出される。
"""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from typing import Any, List
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import Base, ExecutionOutboxItem
from saiverse.execution_ledger import ExecutionLedger
from sea.head_pipeline import (
    HeadPipeline,
    HeadSectionRegistry,
    LineHeadInput,
    NotificationLabel,
    RenderedSection,
    inject_diff_notifications,
)
from sea.head_pipeline.notify import notify_head_mutation

PERSONA_ID = "tester"
MODEL_A = "model-standard"
MODEL_B = "model-lightweight"
BUILDING = "b_lobby"


class _MutableSection:
    """live 値を差し替えられる最小 section (render / diff を観測できる)。"""

    order = 100
    refresh_on_events = frozenset()

    def __init__(self, name: str = "core_memory", text: str = "初期値"):
        self.name = name
        self.live_text = text
        self.render_none = False

    def capture(self, ctx):
        return {"text": self.live_text}

    def render(self, snapshot):
        if self.render_none or not snapshot:
            return None
        return RenderedSection(text=f"## {self.name}\n{snapshot['text']}")

    def diff_to_notifications(self, old, new):
        if not old or not new:
            return []
        if old.get("text") != new.get("text"):
            return [NotificationLabel(
                kind=f"{self.name}_changed",
                label=f"{self.name} が変わりました: {new['text']}",
            )]
        return []

    def serialize_snapshot(self, snapshot):
        return json.dumps(snapshot or {}, ensure_ascii=False)

    def deserialize_snapshot(self, data):
        return json.loads(data)


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


@pytest.fixture
def ledger(session_factory):
    # handler 未登録 → 配送は pending のまま (outbox 積みだけを観測する)。
    return ExecutionLedger(session_factory=session_factory)


@pytest.fixture
def section():
    return _MutableSection()


@pytest.fixture
def pipeline(section):
    registry = HeadSectionRegistry()
    registry.register(section)
    return HeadPipeline(registry=registry)


@pytest.fixture
def persona():
    return SimpleNamespace(
        persona_id=PERSONA_ID, model=MODEL_A, current_building_id=BUILDING,
    )


@pytest.fixture
def manager(session_factory, ledger, persona):
    mgr = SimpleNamespace(
        SessionLocal=session_factory,
        personas={PERSONA_ID: persona},
        execution_ledger=ledger,
        sea_runtime=None,  # 本番 manager は無条件に持つ (未構築なら None)
    )
    return mgr


def _ctx(model_key: str) -> LineHeadInput:
    return LineHeadInput(
        persona_id=PERSONA_ID, model_key=model_key, current_building_id=BUILDING,
    )


def _outbox_rows(session_factory) -> List[Any]:
    db = session_factory()
    try:
        return list(
            db.query(ExecutionOutboxItem)
            .order_by(ExecutionOutboxItem.OUTBOX_ID.asc())
            .all()
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 1. render と同一 text が outbox payload に載る
# ---------------------------------------------------------------------------


def test_notify_puts_render_identical_text_in_outbox(
    pipeline, section, persona, manager, session_factory,
):
    pipeline.capture_all(_ctx(MODEL_A))
    section.live_text = "更新後の中身"

    notify_head_mutation(
        persona, manager, BUILDING, "core_memory",
        operation_label="コア記憶を更新しました (c:1)", pipeline=pipeline,
    )

    rows = _outbox_rows(session_factory)
    assert len(rows) == 1
    assert rows[0].TARGET == "perception.push"
    assert rows[0].PERSONA_ID == PERSONA_ID
    payload = json.loads(rows[0].PAYLOAD_JSON)
    assert payload["kind"] == "head_mutation"
    assert payload["reduce_key"] == "head_mutation:core_memory"
    assert payload["salient"] is False
    assert json.loads(payload["metadata"]) == {"section": "core_memory"}
    # 通知本文 = 操作ラベル + render と寸分たがわぬ断片 (§3.3 不変条件)
    expected_fragment = section.render(section.capture(_ctx(MODEL_A))).text
    assert payload["content"] == f"コア記憶を更新しました (c:1)\n\n{expected_fragment}"

    # 台帳は applied (outbox pending = handler 未登録) — 通知は配達保証の層に乗った
    execution = manager.execution_ledger.get_execution(rows[0].EXECUTION_ID)
    assert execution["kind"] == "head.mutation_notify"
    assert execution["status"] == "applied"


# ---------------------------------------------------------------------------
# 2. rendered None → 通知しない
# ---------------------------------------------------------------------------


def test_notify_skips_when_render_returns_none(
    pipeline, section, persona, manager, session_factory,
):
    pipeline.capture_all(_ctx(MODEL_A))
    section.live_text = "変わった"
    section.render_none = True  # memopedia_index の opt-in OFF 相当

    notify_head_mutation(
        persona, manager, BUILDING, "core_memory",
        operation_label="コア記憶を更新しました", pipeline=pipeline,
    )

    assert _outbox_rows(session_factory) == []


def test_notify_skips_unregistered_section(
    pipeline, persona, manager, session_factory,
):
    notify_head_mutation(
        persona, manager, BUILDING, "no_such_section",
        operation_label="x", pipeline=pipeline,
    )
    assert _outbox_rows(session_factory) == []


# ---------------------------------------------------------------------------
# 3. 台帳の故障でも raise しない
# ---------------------------------------------------------------------------


def test_notify_never_raises_even_when_ledger_broken(pipeline, section, persona):
    class _BrokenLedger:
        def begin_execution(self, *a, **kw):
            raise RuntimeError("db down")

    manager = SimpleNamespace(
        personas={PERSONA_ID: persona}, execution_ledger=_BrokenLedger(),
        sea_runtime=None,
    )
    pipeline.capture_all(_ctx(MODEL_A))
    section.live_text = "失敗しても壊さない"

    # raise しない (ツール本体の結果を壊さない)
    notify_head_mutation(
        persona, manager, BUILDING, "core_memory",
        operation_label="コア記憶を更新しました", pipeline=pipeline,
    )
    # push は確定していないので B は据え置き = backstop が拾える
    labels = pipeline.flush_diffs(_ctx(MODEL_A), all_sections=True)
    assert labels and labels[0].kind == "core_memory_changed"


# ---------------------------------------------------------------------------
# 4. B (last_notified) の全 model 行前進 → 再通知なし
# ---------------------------------------------------------------------------


def test_advance_last_notified_covers_all_model_rows(pipeline, section):
    pipeline.capture_all(_ctx(MODEL_A))
    pipeline.capture_all(_ctx(MODEL_B))

    section.live_text = "全行に届いた変化"
    new_snapshot = section.capture(_ctx(MODEL_A))
    pipeline.advance_last_notified(PERSONA_ID, "core_memory", new_snapshot)

    # 両 model 行とも B が前進済み → 同じ変化は再通知されない
    assert pipeline.flush_diffs(_ctx(MODEL_A), all_sections=True) == []
    assert pipeline.flush_diffs(_ctx(MODEL_B), all_sections=True) == []


def test_notify_advances_all_model_rows(
    pipeline, section, persona, manager, session_factory,
):
    pipeline.capture_all(_ctx(MODEL_A))
    pipeline.capture_all(_ctx(MODEL_B))
    section.live_text = "notify 経由の前進"

    notify_head_mutation(
        persona, manager, BUILDING, "core_memory",
        operation_label="コア記憶を更新しました", pipeline=pipeline,
    )

    assert len(_outbox_rows(session_factory)) == 1
    assert pipeline.flush_diffs(_ctx(MODEL_A), all_sections=True) == []
    assert pipeline.flush_diffs(_ctx(MODEL_B), all_sections=True) == []


def test_flush_diffs_advance_false_returns_detection_and_keeps_b(pipeline, section):
    pipeline.capture_all(_ctx(MODEL_A))
    section.live_text = "検出のみ"

    labels, detected = pipeline.flush_diffs(
        _ctx(MODEL_A), all_sections=True, advance=False,
    )
    assert labels and labels[0].kind == "core_memory_changed"
    assert detected == {"core_memory": {"text": "検出のみ"}}

    # B 据え置き → もう一度検出できる (配送失敗時の再試行が成立する)
    labels2, _ = pipeline.flush_diffs(_ctx(MODEL_A), all_sections=True, advance=False)
    assert labels2 and labels2[0].kind == "core_memory_changed"


# ---------------------------------------------------------------------------
# 5. ツール成功点からの発火 (memory_write → core_memory)
# ---------------------------------------------------------------------------


def _tool_manager(session_factory, ledger, persona):
    return SimpleNamespace(
        SessionLocal=session_factory,
        personas={PERSONA_ID: persona},
        execution_ledger=ledger,
        sea_runtime=None,
    )


def test_memory_write_core_fires_notification(
    session_factory, ledger, tmp_path,
):
    from builtin_data.tools.memory_write import memory_write
    from saiverse import memory_atlas
    from tools.context import persona_context

    section = _MutableSection(name="core_memory", text="コア記憶の中身")
    registry = HeadSectionRegistry()
    registry.register(section)
    pipeline = HeadPipeline(registry=registry)

    adapter = SimpleNamespace(is_ready=lambda: True, _db_lock=threading.Lock())
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model=MODEL_A,
        current_building_id=BUILDING, sai_memory=adapter,
    )
    manager = _tool_manager(session_factory, ledger, persona)

    with patch.object(
        memory_atlas, "write_page",
        return_value="コア記憶 core:1 に書きました（常時開の特殊ページです）。",
    ), patch(
        "sea.head_pipeline.notify.get_default_pipeline", return_value=pipeline,
    ):
        with persona_context(PERSONA_ID, tmp_path, manager=manager):
            result = memory_write(ref="core", content="新しいコア記憶")

    assert "core:1" in result
    rows = _outbox_rows(session_factory)
    assert len(rows) == 1
    payload = json.loads(rows[0].PAYLOAD_JSON)
    assert payload["kind"] == "head_mutation"
    assert json.loads(payload["metadata"]) == {"section": "core_memory"}
    assert "コア記憶の中身" in payload["content"]  # render 断片が同梱される


def test_memory_write_error_does_not_fire(session_factory, ledger, tmp_path):
    from builtin_data.tools.memory_write import memory_write
    from tools.context import persona_context

    adapter = SimpleNamespace(is_ready=lambda: True, _db_lock=threading.Lock())
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model=MODEL_A,
        current_building_id=BUILDING, sai_memory=adapter,
    )
    manager = _tool_manager(session_factory, ledger, persona)

    with persona_context(PERSONA_ID, tmp_path, manager=manager):
        # ref と title の排他違反 = write_page 手前の Error 返し
        result = memory_write(content="x")

    assert result.startswith("Error")
    assert _outbox_rows(session_factory) == []


# ---------------------------------------------------------------------------
# 6. S3: inject_diff_notifications の B 前進は配送確定後
# ---------------------------------------------------------------------------


def test_diff_notify_b_advances_only_after_durable_queue(
    pipeline, section, persona, session_factory,
):
    class _BrokenLedger:
        def begin_execution(self, *a, **kw):
            raise RuntimeError("db down")

    pipeline.capture_all(_ctx(MODEL_A))
    section.live_text = "配送保証のテスト"

    # 1) 配送予約に失敗 → False + B 据え置き
    broken = SimpleNamespace(execution_ledger=_BrokenLedger(), sea_runtime=None)
    assert inject_diff_notifications(
        persona, broken, BUILDING, pipeline=pipeline, model_key=MODEL_A,
    ) is False
    assert _outbox_rows(session_factory) == []

    # 2) 台帳が復旧 → 同じ差分が再検出されて outbox に載り、B が前進する
    working = SimpleNamespace(
        execution_ledger=ExecutionLedger(session_factory=session_factory),
        sea_runtime=None,
    )
    assert inject_diff_notifications(
        persona, working, BUILDING, pipeline=pipeline, model_key=MODEL_A,
    ) is True
    rows = _outbox_rows(session_factory)
    assert len(rows) == 1
    payload = json.loads(rows[0].PAYLOAD_JSON)
    assert payload["kind"] == "world_state"
    assert "配送保証のテスト" in payload["content"]
    execution = working.execution_ledger.get_execution(rows[0].EXECUTION_ID)
    assert execution["kind"] == "head.diff_notify"

    # 3) B 前進済み → 再通知なし
    assert inject_diff_notifications(
        persona, working, BUILDING, pipeline=pipeline, model_key=MODEL_A,
    ) is False
    assert len(_outbox_rows(session_factory)) == 1
