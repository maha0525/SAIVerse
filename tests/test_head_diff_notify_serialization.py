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

B の前進の DB 処理 (性能の退行の修正):

i. 一回の配送で複数の Section を進めても、DB にだけある組の前進は store の
   呼び出し一回、メモリ上の組の保存はモデルごとに一回 (Section 数に比例しない)。
j. 全モデルの組がメモリにあるとき、DB にだけある組の前進は commit しない。

同 issue ケース 5 (再起動後、DB から読み込まれる前の撮り直しが、未配送の変化の
B を現在値で上書きして知らせを消す) の固定する仕様:

k. 読み込み前に dispatch_event (Metabolism) が撮り直しても、DB に残っていた
   未配送の変化は次の差分検知で届く。
l. capture_for_event がメモリに組の無いまま capture_all へ落ちる経路も同じで、
   落ちるときに pipeline のロック (self._lock) を握っていない。
m. DB に行が本当に無いモデルは従来どおり B = 現在値で初期化し、初回に全内容を
   「変わった」と知らせない。
n. capture_all は撮影から保存までをペルソナの通知ロックの内側で行い、別スレッドが
   通知ロックを握っている間 (B を進めている途中) は撮らずに待つ。
o. 読み込みが例外を投げたら、B を初期化して DB を上書きせずに例外を返す。

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
    EventType,
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

    # 差分検知は advance_last_notified_many を、ツール側の知らせは単数版
    # (→ many へ委譲) を呼ぶ。many を壊せば両方の前進が落ちる。
    monkeypatch.setattr(pipeline, "advance_last_notified_many", broken_advance)

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
    assert store.save_notified_sections_for_other_models(
        PERSONA_ID, {"core_memory": value}, exclude_model_keys={MODEL_A},
    ) is False
    # 除外した組 (メモリ上の組) は触らない / 壊れた行は残す / 他は進める
    assert _stored_notified(session_factory, MODEL_A)["core_memory"] == {"text": "初期値"}
    assert _raw_notified(session_factory, MODEL_B) == "[]"
    assert _stored_notified(session_factory, MODEL_C)["core_memory"] == value

    # 未登録の Section しか無ければ何も書かない
    raw_c = _raw_notified(session_factory, MODEL_C)
    assert store.save_notified_sections_for_other_models(
        PERSONA_ID, {"no_such_section": value}, exclude_model_keys=set(),
    ) is False
    assert _raw_notified(session_factory, MODEL_C) == raw_c

    # 壊れた行が無ければ True
    _set_raw_notified(session_factory, MODEL_B, "{}")
    assert store.save_notified_sections_for_other_models(
        PERSONA_ID, {"core_memory": value}, exclude_model_keys=set(),
    ) is True
    assert _stored_notified(session_factory, MODEL_B) == {"core_memory": value}

    # 未登録の Section が混ざっても、登録済みの Section は書く (戻り値は False)
    newer = {"text": "もっと新しい値"}
    assert store.save_notified_sections_for_other_models(
        PERSONA_ID, {"no_such_section": value, "core_memory": newer},
        exclude_model_keys=set(),
    ) is False
    assert _stored_notified(session_factory, MODEL_B) == {"core_memory": newer}
    assert "no_such_section" not in _stored_notified(session_factory, MODEL_C)


def test_store_advance_for_other_models_skips_unserializable_section(session_factory):
    """serialize に失敗した Section だけを飛ばし、残りの Section は書く。"""

    class _BrokenSerialize(_MutableSection):
        def serialize_snapshot(self, snapshot):
            raise ValueError("serialize failed")

    core = _MutableSection("core_memory", "初期値")
    broken = _BrokenSerialize("desk", "机の初期値")
    store, before, _restart = _stored_pipelines(session_factory, core, broken)
    before.capture_all(_ctx(MODEL_B))
    # 撮影時の保存でも desk は serialize できずに省かれている (optional Section)
    assert "desk" not in json.loads(_raw_notified(session_factory, MODEL_B))

    assert store.save_notified_sections_for_other_models(
        PERSONA_ID,
        {"core_memory": {"text": "届いた値"}, "desk": {"text": "書けない値"}},
        exclude_model_keys=set(),
    ) is False
    raw_after = json.loads(_raw_notified(session_factory, MODEL_B))
    assert json.loads(raw_after["core_memory"]) == {"text": "届いた値"}
    # 書けなかった Section は書かれていない
    assert "desk" not in raw_after


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


# ---------------------------------------------------------------------------
# B の前進の DB 処理は配送一回につき一回 (Section 数に比例しない)
# ---------------------------------------------------------------------------


def _count_b_saves(store: LineHeadSnapshotStore, monkeypatch) -> dict:
    """store の B 保存 (メモリ上の組 / DB にだけある組) の呼び出しを数える。"""
    calls: dict = {"last_notified": [], "other_models": []}
    original_last = store.save_last_notified
    original_other = store.save_notified_sections_for_other_models

    def save_last_notified(persona_id, model_key, notified):
        calls["last_notified"].append(model_key)
        return original_last(persona_id, model_key, notified)

    def save_other(persona_id, notified_values, exclude_model_keys):
        calls["other_models"].append(
            (sorted(notified_values), sorted(exclude_model_keys)),
        )
        return original_other(
            persona_id, notified_values, exclude_model_keys=exclude_model_keys,
        )

    monkeypatch.setattr(store, "save_last_notified", save_last_notified)
    monkeypatch.setattr(store, "save_notified_sections_for_other_models", save_other)
    return calls


def test_advancing_several_sections_touches_the_db_once(
    persona, manager, session_factory, monkeypatch,
):
    """二つの Section が同時に変わった配送一回で、B の保存はモデルごと・店ごとに一回。

    入室の配送ハンドラからの前進は台帳のプロセス全体の配送ロックを握っている
    最中なので、Section ごとに DB を往復すると他ペルソナの配送まで止まる。
    """
    core = _MutableSection("core_memory", "初期値")
    desk = _MutableSection("desk", "机の初期値")
    store, before, restart = _stored_pipelines(session_factory, core, desk)
    before.capture_all(_ctx(MODEL_A))
    before.capture_all(_ctx(MODEL_B))

    # 再起動後、MODEL_A の組だけがメモリにある (MODEL_B は DB にだけある)
    assert restart.load_from_store(PERSONA_ID, MODEL_A) is True
    assert not restart.has_snapshot(PERSONA_ID, MODEL_B)
    calls = _count_b_saves(store, monkeypatch)

    core.live_text = "新しいコア記憶"
    desk.live_text = "新しい机"
    assert _detect(persona, manager, restart, MODEL_A) is True
    contents = [
        json.loads(r.PAYLOAD_JSON)["content"] for r in _outbox_rows(session_factory)
    ]
    assert len(contents) == 2, contents

    # DB にだけある組の前進は一回で、両 Section をまとめて渡している
    assert calls["other_models"] == [(["core_memory", "desk"], [MODEL_A])]
    # メモリ上の組の保存はモデル一つにつき一回
    assert calls["last_notified"] == [MODEL_A]

    # 両 Section の新しい B が DB の MODEL_B の行に入っている
    stored_b = _stored_notified(session_factory, MODEL_B)
    assert stored_b["core_memory"] == {"text": "新しいコア記憶"}
    assert stored_b["desk"] == {"text": "新しい机"}
    stored_a = _stored_notified(session_factory, MODEL_A)
    assert stored_a["core_memory"] == {"text": "新しいコア記憶"}
    assert stored_a["desk"] == {"text": "新しい机"}

    # 後で MODEL_B が読み込まれても、どちらの変化も再配送しない
    assert _detect(persona, manager, restart, MODEL_B) is False
    assert len(_outbox_rows(session_factory)) == 2


def test_no_commit_when_every_model_row_is_in_memory(
    persona, manager, session_factory, monkeypatch,
):
    """全モデルの組がメモリにあるとき、DB にだけある組の前進は commit しない。"""
    core = _MutableSection("core_memory", "初期値")
    desk = _MutableSection("desk", "机の初期値")
    store, before, restart = _stored_pipelines(session_factory, core, desk)
    before.capture_all(_ctx(MODEL_A))
    before.capture_all(_ctx(MODEL_B))
    assert restart.load_from_store(PERSONA_ID, MODEL_A) is True
    assert restart.load_from_store(PERSONA_ID, MODEL_B) is True

    # store が他モデル行の前進の最中に開いた DB セッションの commit を数える
    commits: list[str] = []
    inside_other = {"on": False}
    original_factory = store._session_factory

    def counting_factory():
        db = original_factory()
        if inside_other["on"]:
            original_commit = db.commit

            def commit():
                commits.append("commit")
                return original_commit()

            db.commit = commit
        return db

    monkeypatch.setattr(store, "_session_factory", counting_factory)
    original_other = store.save_notified_sections_for_other_models
    results: list[bool] = []

    def save_other(*args, **kwargs):
        inside_other["on"] = True
        try:
            result = original_other(*args, **kwargs)
        finally:
            inside_other["on"] = False
        results.append(result)
        return result

    monkeypatch.setattr(store, "save_notified_sections_for_other_models", save_other)

    core.live_text = "新しいコア記憶"
    desk.live_text = "新しい机"
    assert _detect(persona, manager, restart, MODEL_A) is True

    # 呼ばれてはいる (除外集合 = 両モデル) が、対象行が 0 件なので commit しない
    assert results == [True]
    assert commits == []
    # メモリ上の組の保存 (save_last_notified) で両モデルの B は DB に入っている
    for model_key in (MODEL_A, MODEL_B):
        assert _stored_notified(session_factory, model_key)["desk"] == {"text": "新しい机"}


# ---------------------------------------------------------------------------
# ケース 5: 再起動後、読み込まれる前の撮り直しが未配送の変化を消さない
# ---------------------------------------------------------------------------


class _RefreshSection(_MutableSection):
    """スペルの切り替えで撮り直される Section (refresh_on_events を持つ)。

    ``on_capture`` を渡すと、capture のたびに呼ぶ (撮影中のロックの観測用)。
    """

    refresh_on_events = frozenset({EventType.SPELL_TOGGLED})

    def __init__(self, name: str = "core_memory", text: str = "初期値"):
        super().__init__(name, text)
        self.on_capture = None

    def capture(self, ctx):
        if self.on_capture is not None:
            self.on_capture()
        return super().capture(ctx)


def _assert_undelivered_change_arrives(
    persona, manager, restart, session_factory, text: str,
) -> None:
    """再起動後の差分検知で、再起動前の未配送の変化が一回だけ届く。"""
    assert _detect(persona, manager, restart, MODEL_A) is True
    contents = [
        json.loads(r.PAYLOAD_JSON)["content"] for r in _outbox_rows(session_factory)
    ]
    assert len(contents) == 1, contents
    assert text in contents[0]
    # 届けた後は B が進んでいるので、もう一度は積まない
    assert _detect(persona, manager, restart, MODEL_A) is False
    assert len(_outbox_rows(session_factory)) == 1


def test_capture_before_load_keeps_the_undelivered_change(
    section, persona, manager, session_factory,
):
    """k. 読み込み前の Metabolism の撮り直しが、未配送の変化の B を上書きしない。"""
    _store, before, restart = _stored_pipelines(session_factory, section)
    before.capture_all(_ctx(MODEL_A))  # DB の B = 初期値

    # 再起動前に変化が起きたが、まだ届けていない (DB の B は古いまま)
    section.live_text = "再起動前に起きた変化"

    # 再起動後、ensure_snapshot (読み込み) より先に Metabolism の撮り直しが来る
    assert not restart.has_snapshot(PERSONA_ID, MODEL_A)
    restart.dispatch_event(_ctx(MODEL_A), EventType.METABOLISM)

    # DB の B は撮り直しで上書きされず、古い値のまま (配送だけが進める)
    assert _stored_notified(session_factory, MODEL_A)["core_memory"] == {"text": "初期値"}
    # 撮り直しで A は今の値になっている
    assert restart.get_snapshot(PERSONA_ID, MODEL_A).sections["core_memory"] == {
        "text": "再起動前に起きた変化",
    }
    _assert_undelivered_change_arrives(
        persona, manager, restart, session_factory, "再起動前に起きた変化",
    )


def test_capture_for_event_fallback_keeps_the_undelivered_change(
    persona, manager, session_factory, monkeypatch,
):
    """l. capture_for_event が capture_all へ落ちる経路も B を保ち、self._lock を握らない。"""
    section = _RefreshSection()
    _store, before, restart = _stored_pipelines(session_factory, section)
    before.capture_all(_ctx(MODEL_A))
    section.live_text = "スペルを切り替える前の変化"

    # capture_all に入った瞬間と、撮影中の pipeline のロックの持ち方を記録する
    entry_lock_owned: list[bool] = []
    capture_lock_owned: list[bool] = []
    original_capture_all = restart.capture_all

    def capture_all(ctx):
        entry_lock_owned.append(restart._lock._is_owned())
        return original_capture_all(ctx)

    monkeypatch.setattr(restart, "capture_all", capture_all)
    section.on_capture = lambda: capture_lock_owned.append(restart._lock._is_owned())

    assert not restart.has_snapshot(PERSONA_ID, MODEL_A)
    snapshot = restart.capture_for_event(_ctx(MODEL_A), EventType.SPELL_TOGGLED)
    section.on_capture = None

    assert snapshot is not None
    assert entry_lock_owned == [False]
    assert capture_lock_owned == [False]
    assert _stored_notified(session_factory, MODEL_A)["core_memory"] == {"text": "初期値"}
    _assert_undelivered_change_arrives(
        persona, manager, restart, session_factory, "スペルを切り替える前の変化",
    )


def test_dispatch_spell_toggle_before_load_keeps_the_undelivered_change(
    persona, manager, session_factory,
):
    """l. 本番の入口 (dispatch_event のスペルの切り替え) からも同じく届く。"""
    section = _RefreshSection()
    _store, before, restart = _stored_pipelines(session_factory, section)
    before.capture_all(_ctx(MODEL_A))
    section.live_text = "入口から来た撮り直し"

    restart.dispatch_event(_ctx(MODEL_A), EventType.SPELL_TOGGLED)

    assert _stored_notified(session_factory, MODEL_A)["core_memory"] == {"text": "初期値"}
    _assert_undelivered_change_arrives(
        persona, manager, restart, session_factory, "入口から来た撮り直し",
    )


def test_model_without_a_row_is_initialised_without_notifications(
    section, persona, manager, session_factory,
):
    """m. DB に行が無いモデルは B = 現在値で初期化し、初回に全内容を知らせない。"""
    _store, _before, restart = _stored_pipelines(session_factory, section)
    section.live_text = "初めて撮る内容"

    restart.dispatch_event(_ctx(MODEL_A), EventType.METABOLISM)

    state = restart._states[(PERSONA_ID, MODEL_A)]
    assert state.last_notified_sections["core_memory"] == {"text": "初めて撮る内容"}
    assert _stored_notified(session_factory, MODEL_A)["core_memory"] == {
        "text": "初めて撮る内容",
    }
    assert restart.get_snapshot(PERSONA_ID, MODEL_A).snapshot_version == 1
    assert _detect(persona, manager, restart, MODEL_A) is False
    assert _outbox_rows(session_factory) == []


def test_capture_all_runs_inside_the_notify_lock(session_factory, monkeypatch):
    """n. 撮影中と保存中、現在のスレッドがペルソナの通知ロックを握っている。"""
    section = _RefreshSection()
    store, _before, restart = _stored_pipelines(session_factory, section)

    capture_owned: list[bool] = []
    save_owned: list[bool] = []
    section.on_capture = lambda: capture_owned.append(
        restart.notify_lock_for(PERSONA_ID)._is_owned(),
    )
    original_save = store.save

    def save(snapshot, last_notified_sections):
        save_owned.append(restart.notify_lock_for(PERSONA_ID)._is_owned())
        return original_save(snapshot, last_notified_sections)

    monkeypatch.setattr(store, "save", save)

    restart.capture_all(_ctx(MODEL_A))
    assert capture_owned == [True]
    assert save_owned == [True]
    assert restart.notify_lock_for(PERSONA_ID)._is_owned() is False


def test_capture_all_waits_while_b_is_being_advanced(session_factory, monkeypatch):
    """n. 別スレッドが通知ロックを握っている間 (B を進めている途中)、撮影は待つ。"""
    section = _RefreshSection()
    _store, _before, restart = _stored_pipelines(session_factory, section)
    captured: list[str] = []
    section.on_capture = lambda: captured.append("capture")

    held = restart.notify_lock_for(PERSONA_ID)
    probe = _ContentionProbe(restart, monkeypatch)
    results: dict = {}

    with held:
        capturer = _run_in_thread(
            lambda: restart.capture_all(_ctx(MODEL_C)), results, "capture",
        )
        probe.wait_second(capturer)
        assert probe.contended.is_set()
        assert capturer.is_alive()
        assert captured == []
        assert not restart.has_snapshot(PERSONA_ID, MODEL_C)

    capturer.join(WAIT_TIMEOUT)
    assert not capturer.is_alive()
    assert not isinstance(results.get("capture"), BaseException), results
    assert captured == ["capture"]
    assert restart.has_snapshot(PERSONA_ID, MODEL_C)


def test_capture_all_does_not_overwrite_b_when_the_load_fails(
    section, session_factory, monkeypatch,
):
    """o. 読み込みの例外は返し、DB の B を撮った値で上書きしない。"""
    store, before, restart = _stored_pipelines(session_factory, section)
    before.capture_all(_ctx(MODEL_A))
    section.live_text = "読めなかった間の変化"

    def broken_load(persona_id, model_key):
        raise RuntimeError("db read failed")

    monkeypatch.setattr(store, "load", broken_load)

    with pytest.raises(RuntimeError):
        restart.dispatch_event(_ctx(MODEL_A), EventType.METABOLISM)
    assert not restart.has_snapshot(PERSONA_ID, MODEL_A)
    assert _stored_notified(session_factory, MODEL_A)["core_memory"] == {"text": "初期値"}
    assert restart.notify_lock_for(PERSONA_ID)._is_owned() is False
