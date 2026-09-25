"""変化の知らせ (head の差分通知) の並行実行で二重に積まれないことのテスト。

issue: docs/issues/head_diff_notification_duplicate_delivery.md ケース 1。

ペルソナ P の Pulse 頭の検知と、別ペルソナ Q の入室処理 (移動の配送ハンドラ
の中で P の検知に入る) が同時に走ると、両方が同じ古い B (last_notified) から
同じ変化を見つけて outbox に二行積んでいた。修正はペルソナ単位の通知ロック
(:meth:`HeadPipeline.notify_lock_for`) の内側で「検出 → 積む → B 前進」を
一続きにし、即時配送はロックを離してから行う。

固定する仕様:

a. 同じ変化を二スレッドが同時に検知しても、outbox には 1 行しか積まれない。
b. 即時配送 (flush_pending_for_persona) は通知ロックの外で呼ばれ、失われて
   いない。台帳への登録は deliver=False (ロックの中では配らない)。
c. ツール成功時の知らせ (notify_head_mutation) が先に B を進めたら、並行する
   差分検知はその変化を見つけない。

並行テストは StaticPool の in-memory SQLite だと二スレッドが一つの接続を共有して
台帳の transaction が壊れるので、tmp_path のファイル SQLite を使う。本番の
~/.saiverse には触れない。
"""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from typing import Any, List

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

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
BUILDING = "b_lobby"

#: スレッドの待ち合わせの上限 (秒)。正常なら一瞬で抜ける。
WAIT_TIMEOUT = 10.0


class _MutableSection:
    """live 値を差し替えられる最小 section (tests/test_head_mutation_notify.py と同形)。"""

    order = 100
    refresh_on_events = frozenset()

    def __init__(self, name: str = "core_memory", text: str = "初期値"):
        self.name = name
        self.live_text = text

    def capture(self, ctx):
        return {"text": self.live_text}

    def render(self, snapshot):
        if not snapshot:
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
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'ledger.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
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
    return SimpleNamespace(
        SessionLocal=session_factory,
        personas={PERSONA_ID: persona},
        execution_ledger=ledger,
    )


def _ctx(model_key: str = MODEL_A) -> LineHeadInput:
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


def _hold_first_begin_execution(ledger: ExecutionLedger, monkeypatch):
    """ledger.begin_execution の一回目の呼び出しだけを止める。

    戻り値 ``(entered, release)``: 一回目が止まった瞬間に ``entered`` が立ち、
    テストが ``release`` を立てると先へ進む。二回目以降は素通し。止めるのは
    差分検出 (または撮影) の後・台帳登録の途中 — 修正前のコードで二本目の処理が
    同じ古い B から同じ変化を見つけられる位置。
    """
    entered = threading.Event()
    release = threading.Event()
    calls = {"n": 0}
    calls_lock = threading.Lock()
    original = ledger.begin_execution

    def held(*args, **kwargs):
        with calls_lock:
            calls["n"] += 1
            first = calls["n"] == 1
        if first:
            entered.set()
            assert release.wait(WAIT_TIMEOUT), "test never released the first call"
        return original(*args, **kwargs)

    monkeypatch.setattr(ledger, "begin_execution", held)
    return entered, release


class _ContentionProbe:
    """通知ロックを包み、二本目がロック待ちに入った瞬間を観測する。

    ``notify_lock_for`` が返すロックの取得を、まず非待機で試みる。取れなければ
    ``contended`` を立ててから待つ。修正後は二本目が必ずここで待たされるので
    ``contended`` が立ち、修正前 (並べない) は二本目がロック待ちせずに最後まで
    走り切る。どちらも時間ではなく観測で判定できる (固定 sleep に頼らない)。
    """

    def __init__(self, pipeline: HeadPipeline, monkeypatch):
        self.contended = threading.Event()
        original = pipeline.notify_lock_for
        probe = self

        class _ProbedLock:
            def __init__(self, lock):
                self._lock = lock

            def __enter__(self):
                if not self._lock.acquire(blocking=False):
                    probe.contended.set()
                    self._lock.acquire()
                return self

            def __exit__(self, *exc):
                self._lock.release()
                return False

            def _is_owned(self):
                return self._lock._is_owned()

        monkeypatch.setattr(
            pipeline, "notify_lock_for",
            lambda persona_id: _ProbedLock(original(persona_id)),
        )

    def wait_second(self, second: threading.Thread) -> None:
        """二本目が「ロック待ちに入る」か「走り切る」まで待つ。"""
        deadline = WAIT_TIMEOUT
        while deadline > 0 and not self.contended.is_set() and second.is_alive():
            self.contended.wait(0.01)
            deadline -= 0.01
        assert self.contended.is_set() or not second.is_alive(), (
            "second thread neither contended the notify lock nor finished"
        )


def _run_in_thread(fn, results: dict, key: str) -> threading.Thread:
    def target():
        try:
            results[key] = fn()
        except BaseException as exc:  # pragma: no cover - 失敗の診断用
            results[key] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


# ---------------------------------------------------------------------------
# a. 同時検知で一行しか登録されない
# ---------------------------------------------------------------------------


def test_concurrent_detection_queues_the_change_once(
    pipeline, section, persona, manager, ledger, session_factory, monkeypatch,
):
    pipeline.capture_all(_ctx())
    section.live_text = "Qさんが入室しました"

    entered, release = _hold_first_begin_execution(ledger, monkeypatch)
    probe = _ContentionProbe(pipeline, monkeypatch)

    def detect():
        return inject_diff_notifications(
            persona, manager, BUILDING, pipeline=pipeline, model_key=MODEL_A,
            detect_room=False,
        )

    results: dict = {}
    first = _run_in_thread(detect, results, "first")
    assert entered.wait(WAIT_TIMEOUT), "first detection never reached the ledger"

    # 一本目が台帳登録の途中で止まっている間に、二本目の検知を始める
    # (Pulse 頭の検知と、別ペルソナの入室処理が重なった形)。
    # 修正後は二本目が通知ロックで待たされ、修正前は待たずに走り切る。
    second = _run_in_thread(detect, results, "second")
    probe.wait_second(second)
    release.set()

    first.join(WAIT_TIMEOUT)
    second.join(WAIT_TIMEOUT)
    assert not first.is_alive() and not second.is_alive()
    assert not isinstance(results.get("first"), BaseException), results
    assert not isinstance(results.get("second"), BaseException), results

    rows = _outbox_rows(session_factory)
    assert len(rows) == 1, [json.loads(r.PAYLOAD_JSON)["content"] for r in rows]
    assert "Qさんが入室しました" in json.loads(rows[0].PAYLOAD_JSON)["content"]
    # 積んだのは一本目。二本目は B が進んだ後に比べたので差分を見つけない。
    assert results["first"] is True
    assert results["second"] is False

    # B は前進済み → 次の検知でも積まない
    assert inject_diff_notifications(
        persona, manager, BUILDING, pipeline=pipeline, model_key=MODEL_A,
        detect_room=False,
    ) is False
    assert len(_outbox_rows(session_factory)) == 1


# ---------------------------------------------------------------------------
# b. 配送は通知ロックの外で行われる
# ---------------------------------------------------------------------------


def _record_delivery(ledger: ExecutionLedger, pipeline: HeadPipeline, monkeypatch):
    """flush_pending_for_persona / mark_applied の呼ばれ方を記録する。"""
    record: dict = {
        "flush_lock_owned": [], "mark_applied_deliver": [], "apply_lock_owned": [],
    }
    original_flush = ledger.flush_pending_for_persona
    original_apply = ledger.mark_applied

    def flush(persona_id, *args, **kwargs):
        lock = pipeline.notify_lock_for(PERSONA_ID)
        record["flush_lock_owned"].append((persona_id, lock._is_owned()))
        return original_flush(persona_id, *args, **kwargs)

    def apply(execution_id, **kwargs):
        record["mark_applied_deliver"].append(kwargs.get("deliver", True))
        # 台帳への登録は通知ロックの内側 (検出〜B 前進を一続きにするため)
        record["apply_lock_owned"].append(pipeline.notify_lock_for(PERSONA_ID)._is_owned())
        return original_apply(execution_id, **kwargs)

    monkeypatch.setattr(ledger, "flush_pending_for_persona", flush)
    monkeypatch.setattr(ledger, "mark_applied", apply)
    return record


def test_diff_delivery_runs_outside_the_notify_lock(
    pipeline, section, persona, manager, ledger, session_factory, monkeypatch,
):
    pipeline.capture_all(_ctx())
    section.live_text = "配送はロックの外"
    record = _record_delivery(ledger, pipeline, monkeypatch)

    assert inject_diff_notifications(
        persona, manager, BUILDING, pipeline=pipeline, model_key=MODEL_A,
        detect_room=False,
    ) is True

    # ロックの中では積むだけ (台帳の即時配送を使わない)
    assert record["mark_applied_deliver"] == [False]
    assert record["apply_lock_owned"] == [True]
    # 即時配送は失われていない — ロックを離した後に一回呼ばれている
    assert record["flush_lock_owned"] == [(PERSONA_ID, False)]
    assert len(_outbox_rows(session_factory)) == 1


def test_diff_delivery_is_skipped_when_nothing_was_queued(
    pipeline, section, persona, manager, ledger, monkeypatch,
):
    pipeline.capture_all(_ctx())  # 変化なし
    record = _record_delivery(ledger, pipeline, monkeypatch)

    assert inject_diff_notifications(
        persona, manager, BUILDING, pipeline=pipeline, model_key=MODEL_A,
        detect_room=False,
    ) is False
    assert record["mark_applied_deliver"] == []
    assert record["flush_lock_owned"] == []


def test_delivery_still_runs_when_b_advance_fails_after_queueing(
    pipeline, section, persona, manager, ledger, session_factory, monkeypatch,
):
    """台帳に積んだ後の B 前進で落ちても、積んだ分の即時配送は飛ばない。

    旧実装 (mark_applied(deliver=True)) は B 前進より先に配っていたので、配送を
    ロックの外へ出したことで「前進の失敗 = 配送も飛ぶ」にならないことを固定する。
    """
    pipeline.capture_all(_ctx())
    record = _record_delivery(ledger, pipeline, monkeypatch)

    def broken_advance(*args, **kwargs):
        raise RuntimeError("advance failed")

    monkeypatch.setattr(pipeline, "advance_last_notified", broken_advance)

    # 差分検知: 例外は呼び出し元へ返るが、配送は済んでいる
    section.live_text = "前進に失敗する変化"
    with pytest.raises(RuntimeError):
        inject_diff_notifications(
            persona, manager, BUILDING, pipeline=pipeline, model_key=MODEL_A,
            detect_room=False,
        )
    assert record["mark_applied_deliver"] == [False]
    assert record["flush_lock_owned"] == [(PERSONA_ID, False)]

    # ツール成功時の知らせ: 例外は握られる (ツールの結果を壊さない) が、配送は済んでいる
    section.live_text = "ツール側も前進に失敗"
    notify_head_mutation(
        persona, manager, BUILDING, "core_memory",
        operation_label="コア記憶を更新しました", pipeline=pipeline,
    )
    assert record["mark_applied_deliver"] == [False, False]
    assert record["flush_lock_owned"] == [(PERSONA_ID, False), (PERSONA_ID, False)]
    assert len(_outbox_rows(session_factory)) == 2


def test_mutation_notice_delivery_runs_outside_the_notify_lock(
    pipeline, section, persona, manager, ledger, session_factory, monkeypatch,
):
    pipeline.capture_all(_ctx())
    section.live_text = "ツールが書いた中身"
    record = _record_delivery(ledger, pipeline, monkeypatch)

    notify_head_mutation(
        persona, manager, BUILDING, "core_memory",
        operation_label="コア記憶を更新しました", pipeline=pipeline,
    )

    assert record["mark_applied_deliver"] == [False]
    assert record["flush_lock_owned"] == [(PERSONA_ID, False)]
    assert len(_outbox_rows(session_factory)) == 1


# ---------------------------------------------------------------------------
# c. ツール成功時の知らせと並行する差分検知
# ---------------------------------------------------------------------------


def test_mutation_notice_and_concurrent_detection_do_not_double_up(
    pipeline, section, persona, manager, ledger, session_factory, monkeypatch,
):
    pipeline.capture_all(_ctx())
    section.live_text = "新しいコア記憶"

    entered, release = _hold_first_begin_execution(ledger, monkeypatch)
    probe = _ContentionProbe(pipeline, monkeypatch)

    results: dict = {}
    tool = _run_in_thread(
        lambda: notify_head_mutation(
            persona, manager, BUILDING, "core_memory",
            operation_label="コア記憶を更新しました", pipeline=pipeline,
        ),
        results, "tool",
    )
    assert entered.wait(WAIT_TIMEOUT), "mutation notice never reached the ledger"

    # ツール側の知らせが撮影を終えて台帳登録の途中で止まっている間に、同じ変化を
    # 差分検知が調べに来る。
    detector = _run_in_thread(
        lambda: inject_diff_notifications(
            persona, manager, BUILDING, pipeline=pipeline, model_key=MODEL_A,
            detect_room=False,
        ),
        results, "detector",
    )
    probe.wait_second(detector)
    release.set()

    tool.join(WAIT_TIMEOUT)
    detector.join(WAIT_TIMEOUT)
    assert not tool.is_alive() and not detector.is_alive()
    assert not isinstance(results.get("detector"), BaseException), results

    rows = _outbox_rows(session_factory)
    kinds = [json.loads(r.PAYLOAD_JSON)["kind"] for r in rows]
    # ツール側の知らせ一通だけ。変化の知らせ (world_state) は重ならない。
    assert kinds == ["head_mutation"]
    assert results["detector"] is False
