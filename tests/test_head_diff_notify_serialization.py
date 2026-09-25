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

同 issue ケース 4 (再起動後、メモリに読み込まれていないモデルの組の B が古いまま
残り、そのモデルが読み込まれたときに同じ変化を再配送する) の固定する仕様:

d. B を進めるとき、DB にだけある別モデルの行の B もその Section だけ進む
   (後でそのモデルが読み込まれても再配送しない)。
e. DB 上で進めるのは届けた Section だけで、行の他の Section の B は保たれる。
f. LAST_NOTIFIED_JSON が壊れた行は飛ばし、配送も他の行の前進も止めない。
g. load_from_store はペルソナの通知ロックの内側で読み込む (B を進めている
   途中の組を読み込めない)。
h. 既にメモリにある組を load_from_store が DB の値で上書きしない。

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

from database.models import Base, ExecutionOutboxItem, SessionHeadSnapshot
from saiverse.execution_ledger import ExecutionLedger
from sea.head_pipeline import (
    HeadPipeline,
    HeadSectionRegistry,
    LineHeadInput,
    LineHeadSnapshotStore,
    NotificationLabel,
    RenderedSection,
    inject_diff_notifications,
)
from sea.head_pipeline.notify import notify_head_mutation

PERSONA_ID = "tester"
MODEL_A = "model-standard"
MODEL_B = "model-other"
MODEL_C = "model-third"
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


# ---------------------------------------------------------------------------
# ケース 4: 再起動後、メモリに読み込まれていないモデルの組
# ---------------------------------------------------------------------------


def _stored_pipelines(session_factory, *sections):
    """同じ DB を使う「再起動前」と「再起動後」の pipeline を作る。

    返す ``(store, before, restart)``: ``before`` で撮った組は DB に保存され、
    ``restart`` はメモリが空の新しい pipeline (再起動の再現)。
    """
    registry = HeadSectionRegistry()
    for section in sections:
        registry.register(section)
    store = LineHeadSnapshotStore(session_factory=session_factory, registry=registry)
    before = HeadPipeline(registry=registry, store=store)
    restart = HeadPipeline(registry=registry, store=store)
    return store, before, restart


def _stored_notified(session_factory, model_key: str) -> dict:
    """DB の行の LAST_NOTIFIED_JSON を Section 名 → 復元値の dict で返す。"""
    db = session_factory()
    try:
        row = db.query(SessionHeadSnapshot).filter_by(
            PERSONA_ID=PERSONA_ID, MODEL_KEY=model_key,
        ).one()
        raw = json.loads(row.LAST_NOTIFIED_JSON)
    finally:
        db.close()
    return {name: json.loads(value) for name, value in raw.items()}


def _set_raw_notified(session_factory, model_key: str, raw: str) -> None:
    db = session_factory()
    try:
        row = db.query(SessionHeadSnapshot).filter_by(
            PERSONA_ID=PERSONA_ID, MODEL_KEY=model_key,
        ).one()
        row.LAST_NOTIFIED_JSON = raw
        db.commit()
    finally:
        db.close()


def _raw_notified(session_factory, model_key: str) -> str:
    db = session_factory()
    try:
        return db.query(SessionHeadSnapshot).filter_by(
            PERSONA_ID=PERSONA_ID, MODEL_KEY=model_key,
        ).one().LAST_NOTIFIED_JSON
    finally:
        db.close()


def _detect(persona, manager, pipeline, model_key, **kwargs):
    return inject_diff_notifications(
        persona, manager, BUILDING, pipeline=pipeline, model_key=model_key,
        detect_room=False, **kwargs,
    )


def test_unloaded_model_row_is_advanced_and_does_not_redeliver(
    section, persona, manager, session_factory,
):
    """d. 再起動後に読み込まれていないモデルの組も B が進み、再配送しない。"""
    _store, before, restart = _stored_pipelines(session_factory, section)
    before.capture_all(_ctx(MODEL_A))
    before.capture_all(_ctx(MODEL_B))

    # 再起動後、標準モデル (MODEL_A) の組だけが読み込まれた状態で変化を届ける
    section.live_text = "Qさんが入室しました"
    assert _detect(persona, manager, restart, MODEL_A) is True
    assert restart.has_snapshot(PERSONA_ID, MODEL_A)
    assert not restart.has_snapshot(PERSONA_ID, MODEL_B)
    assert len(_outbox_rows(session_factory)) == 1
    # MODEL_B が読み込まれる前の、DB にだけある行の B (下で確かめる)
    stored_b_before_load = _stored_notified(session_factory, MODEL_B)

    # 後で MODEL_B で考える (この時点で MODEL_B の組が DB から読み込まれる)。
    # 同じ変化をもう一度見つけて積まない。
    detected_again = _detect(persona, manager, restart, MODEL_B)
    assert restart.has_snapshot(PERSONA_ID, MODEL_B)
    rows = _outbox_rows(session_factory)
    assert len(rows) == 1, [json.loads(r.PAYLOAD_JSON)["content"] for r in rows]
    assert detected_again is False

    # 読み込まれる前から、DB にだけある MODEL_B の行の B は届けた値まで進んでいた
    assert stored_b_before_load["core_memory"] == {"text": "Qさんが入室しました"}


def test_advancing_unloaded_rows_keeps_other_sections(
    persona, manager, session_factory,
):
    """e. DB 上で進めるのは届けた Section だけ。行の他の Section の B は保たれる。"""
    core = _MutableSection("core_memory", "初期値")
    desk = _MutableSection("desk", "机の初期値")
    _store, before, restart = _stored_pipelines(session_factory, core, desk)
    before.capture_all(_ctx(MODEL_A))
    before.capture_all(_ctx(MODEL_B))

    # 両方変えるが、届けるのは core_memory だけ
    core.live_text = "新しいコア記憶"
    desk.live_text = "まだ届けていない机"
    assert _detect(
        persona, manager, restart, MODEL_A, only_sections={"core_memory"},
    ) is True

    stored_b = _stored_notified(session_factory, MODEL_B)
    assert stored_b["core_memory"] == {"text": "新しいコア記憶"}
    assert stored_b["desk"] == {"text": "机の初期値"}

    # MODEL_B が読み込まれて全 Section を調べると、まだ届けていない机の変化
    # だけが見つかる (机の B を巻き込んで進めていない)。
    assert _detect(persona, manager, restart, MODEL_B) is True
    contents = [
        json.loads(r.PAYLOAD_JSON)["content"] for r in _outbox_rows(session_factory)
    ]
    assert len(contents) == 2, contents
    assert "新しいコア記憶" in contents[0]
    assert "まだ届けていない机" in contents[1]


def test_corrupt_unloaded_row_is_skipped_without_blocking_delivery(
    section, persona, manager, session_factory,
):
    """f. 壊れた行は飛ばす。配送も、他の行の前進も止めない。"""
    _store, before, restart = _stored_pipelines(session_factory, section)
    for model_key in (MODEL_A, MODEL_B, MODEL_C):
        before.capture_all(_ctx(model_key))
    _set_raw_notified(session_factory, MODEL_B, "{broken json")

    section.live_text = "壊れた行があっても届く"
    assert _detect(persona, manager, restart, MODEL_A) is True
    assert len(_outbox_rows(session_factory)) == 1

    # 壊れた行は触らずに残し、別の行 (MODEL_C) は進めている
    assert _raw_notified(session_factory, MODEL_B) == "{broken json"
    assert _stored_notified(session_factory, MODEL_C)["core_memory"] == {
        "text": "壊れた行があっても届く",
    }


def test_store_advance_for_other_models_reports_failures(section, session_factory):
    """f の store 単体: 壊れた行・未登録 Section は False で返り、例外にならない。"""
    store, before, _restart = _stored_pipelines(session_factory, section)
    for model_key in (MODEL_A, MODEL_B, MODEL_C):
        before.capture_all(_ctx(model_key))
    _set_raw_notified(session_factory, MODEL_B, "[]")  # JSON だが dict でない

    value = {"text": "店の値"}
    assert store.save_notified_section_for_other_models(
        PERSONA_ID, "core_memory", value, exclude_model_keys={MODEL_A},
    ) is False
    # 除外した組 (メモリ上の組) は触らない / 壊れた行は残す / 他は進める
    assert _stored_notified(session_factory, MODEL_A)["core_memory"] == {"text": "初期値"}
    assert _raw_notified(session_factory, MODEL_B) == "[]"
    assert _stored_notified(session_factory, MODEL_C)["core_memory"] == value

    # 未登録の Section は何も書かない
    assert store.save_notified_section_for_other_models(
        PERSONA_ID, "no_such_section", value, exclude_model_keys=set(),
    ) is False

    # 壊れた行が無ければ True
    _set_raw_notified(session_factory, MODEL_B, "{}")
    assert store.save_notified_section_for_other_models(
        PERSONA_ID, "core_memory", value, exclude_model_keys=set(),
    ) is True
    assert _stored_notified(session_factory, MODEL_B) == {"core_memory": value}


def test_load_from_store_reads_inside_the_notify_lock(section, session_factory, monkeypatch):
    """g. 読み込みの最中、現在のスレッドがペルソナの通知ロックを握っている。"""
    store, before, restart = _stored_pipelines(session_factory, section)
    before.capture_all(_ctx(MODEL_B))

    owned: list[bool] = []
    original_load = store.load

    def load(persona_id, model_key):
        owned.append(restart.notify_lock_for(persona_id)._is_owned())
        return original_load(persona_id, model_key)

    monkeypatch.setattr(store, "load", load)

    assert restart.load_from_store(PERSONA_ID, MODEL_B) is True
    assert owned == [True]
    # 読み込みが終わればロックは離れている
    assert restart.notify_lock_for(PERSONA_ID)._is_owned() is False


def test_load_from_store_waits_while_b_is_being_advanced(
    section, session_factory, monkeypatch,
):
    """g. 別スレッドが通知ロックを握っている間 (B を進めている途中)、読み込みは待つ。"""
    store, before, restart = _stored_pipelines(session_factory, section)
    before.capture_all(_ctx(MODEL_B))

    loads: list[str] = []
    original_load = store.load

    def load(persona_id, model_key):
        loads.append(model_key)
        return original_load(persona_id, model_key)

    monkeypatch.setattr(store, "load", load)

    held = restart.notify_lock_for(PERSONA_ID)
    probe = _ContentionProbe(restart, monkeypatch)
    results: dict = {}

    with held:  # 配送側が B を進めている途中の形
        loader = _run_in_thread(
            lambda: restart.load_from_store(PERSONA_ID, MODEL_B), results, "load",
        )
        probe.wait_second(loader)
        # 読み込み側は通知ロックで待たされ、まだ DB を読んでいない
        assert probe.contended.is_set()
        assert loader.is_alive()
        assert loads == []
        assert not restart.has_snapshot(PERSONA_ID, MODEL_B)

    loader.join(WAIT_TIMEOUT)
    assert not loader.is_alive()
    assert results["load"] is True
    assert loads == [MODEL_B]
    assert restart.has_snapshot(PERSONA_ID, MODEL_B)


def test_load_from_store_does_not_overwrite_a_loaded_state(
    section, session_factory, monkeypatch,
):
    """h. 二回目の load_from_store は、一回目の後に進めた B を DB の値で巻き戻さない。"""
    store, before, restart = _stored_pipelines(session_factory, section)
    before.capture_all(_ctx(MODEL_A))

    assert restart.load_from_store(PERSONA_ID, MODEL_A) is True
    loaded_snapshot = restart.get_snapshot(PERSONA_ID, MODEL_A)

    # メモリ上の B を進める。DB への保存は失敗させる (DB には古い B が残る)。
    monkeypatch.setattr(store, "save_last_notified", lambda *a, **k: False)
    restart.advance_last_notified(PERSONA_ID, "core_memory", {"text": "進めた値"})
    assert _stored_notified(session_factory, MODEL_A)["core_memory"] == {"text": "初期値"}

    assert restart.load_from_store(PERSONA_ID, MODEL_A) is True
    state = restart._states[(PERSONA_ID, MODEL_A)]
    assert state.last_notified_sections["core_memory"] == {"text": "進めた値"}
    assert restart.get_snapshot(PERSONA_ID, MODEL_A) is loaded_snapshot
