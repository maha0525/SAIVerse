"""アラームの「受理」記帳と「実行完了」を別々に観測する (FLOW-24)。

何を確かめるものか
------------------
ScheduleManager は、アラームの発火を PulseController へ渡したときの結果を
6 種類に分類する (``saiverse/schedule_manager.py:1042-1073``)。そのうち
``action == "queued"`` (ペルソナが塞がっていて待ち行列へ入った) と
``runtime_outcome == "cancelled"`` (走行中に中断された) を **accepted (前進)**
として扱い、実行が完走した ``executed`` と同じ精算をする
(``saiverse/schedule_manager.py:783-820``)。

その精算は、oneshot アラームの場合 ``COMPLETED = True`` を書き、実行台帳へ
applied → completed を刻み、次回発火の再登録まで行う
(``saiverse/schedule_manager.py:1237-1268``)。

根拠として明記されているのは「schedule は on_blocked="wait" — queued /
cancelled は queue に残っていて消えないため」。本スクリプトは、その前提が
成り立たない 2 つの経路を隔離環境で実際に走らせ、
**「受理」の記帳 (DB の COMPLETED / 台帳の状態)** と
**「実行完了」の回数 (run_meta_user が実際に呼ばれた回数)** を別々に数える。

  経路 A: 待ち行列の上限超過 (``sea/pulse_controller.py:420-430``)
          — 上限 10 件を超えると **いちばん古い要求が捨てられる**。
  経路 B: 終了処理 (``sea/pulse_controller.py:604-635``)
          — 終了処理中は待ち行列を繰り上げない。残った要求は破棄される。
  経路 C: 走行中のアラームを利用者の発話が中断した場合
          — ``cancelled`` も accepted なので、中断した瞬間に COMPLETED=True。
            その後に走る復帰実行の顛末は誰も読まない。

どう実行するか
--------------
リポジトリルートから::

    .venv/Scripts/python.exe \
      docs/audits/2026-09-10_normal_behavior_remaining_evidence/repro/b12_alarm_accepted_vs_executed.py

隔離: ``SAIVERSE_HOME`` を一時ディレクトリへ向け、DB は in-memory SQLite、
ペルソナは合成 (``alice``)、LLM は呼ばない (``run_meta_user`` を差し替えた
フェイクが待つだけ)。本番データには一切触れない。

何が観測されたか (2026-09-10 実行、HEAD=7d7214be の作業ツリー)
--------------------------------------------------------------
経路 A (待ち行列の上限超過):
    仕掛けたアラーム            12 件
    DB の COMPLETED=True        12 件
    台帳 (execution ledger) completed  12 件
    実際に run_meta_user が走った回数  10 回
    → **2 件のアラームが「実行済み」と記帳されたまま一度も鳴らなかった。**
      利用者・ペルソナへの通知は無く、残るのは backend.log の
      "Queue limit (10) exceeded ... Dropping oldest request." の
      ERROR 1 行だけ。再試行も来ない (oneshot は COMPLETED=True で終わり)。

経路 B (終了処理):
    仕掛けたアラーム            3 件
    DB の COMPLETED=True        3 件
    実際に run_meta_user が走った回数  0 回 (走行中の会話 1 本を除く)
    → **3 件とも「実行済み」と記帳されたまま破棄された。**
      次の起動でも再発火しない (COMPLETED=True のため再登録されない)。

経路 C (中断 → 復帰実行の失敗):
    DB の COMPLETED=True        1 件 / 1 件中 (中断した瞬間に前進)
    台帳                        completed
    復帰実行                    RuntimeError で失敗
    再試行の予約                なし
    → **アラームの仕事は一度も完走していないのに「実行済み」で閉じた。**
      復帰実行の顛末を ScheduleManager へ返す経路が無いため、失敗が
      台帳にも予約にも現れない。
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT))

# 本番データに触れないよう、import より前に SAIVERSE_HOME を隔離する。
_TMP_HOME = tempfile.mkdtemp(prefix="b12_saiverse_home_")
os.environ["SAIVERSE_HOME"] = _TMP_HOME
os.environ.setdefault("SAIVERSE_USER_DATA_DIR", str(Path(_TMP_HOME) / "user_data"))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from database.models import AI, Base, City, PersonaSchedule, User  # noqa: E402
from saiverse.event_scheduler import EventScheduler  # noqa: E402
from saiverse.execution_ledger import ExecutionLedger  # noqa: E402
from saiverse.pulse_dispatcher import PulseDispatcher  # noqa: E402
from saiverse.schedule_manager import (  # noqa: E402
    SCHEDULE_DISPATCH_LEDGER_KIND,
    ScheduleManager,
    _schedule_key,
)
from sea.cancellation import ExecutionCancelledException  # noqa: E402
from sea.pulse_controller import (  # noqa: E402
    QUEUE_LIMIT,
    ExecutionRequest,
    PulseController,
)

PERSONA_ID = "alice"


class FakeRuntime:
    """LLM を呼ばない SEARuntime の代役。

    ``run_meta_user`` は「呼ばれた回数」を数え、``block`` が立っている間だけ
    待つ。取り消しが刻まれたら本物と同じ ``ExecutionCancelledException`` を
    送出する (PulseController が runtime_outcome="cancelled" を記入する経路)。
    """

    def __init__(self) -> None:
        self.manager: Any = None
        self.calls: List[Dict[str, Any]] = []
        self.release = threading.Event()
        self.entered = threading.Event()

    def run_meta_user(self, **kwargs: Any) -> List[str]:
        self.calls.append(kwargs)
        self.entered.set()
        token = kwargs.get("cancellation_token")
        while not self.release.is_set():
            if token is not None and token.is_cancelled():
                raise ExecutionCancelledException(
                    message="cancelled", interrupted_by=token.interrupted_by,
                )
            time.sleep(0.01)
        return ["ok"]

    def schedule_calls(self) -> List[Dict[str, Any]]:
        return [c for c in self.calls if c.get("pulse_type") == "schedule"]


class ScriptedRuntime:
    """呼び出し回数ごとに振る舞いを変えられる SEARuntime の代役 (経路 C 用)。"""

    def __init__(self, handler) -> None:
        self.manager: Any = None
        self.calls: List[Dict[str, Any]] = []
        self.handler = handler
        self.entered = threading.Event()

    def run_meta_user(self, **kwargs: Any) -> List[str]:
        index = len(self.calls)
        self.calls.append(kwargs)
        self.entered.set()
        return self.handler(index, kwargs)

    def schedule_calls(self) -> List[Dict[str, Any]]:
        return [c for c in self.calls if c.get("pulse_type") == "schedule"]


def _make_env(runtime=None):
    """in-memory DB + 実 PulseController / PulseDispatcher / ScheduleManager。"""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    db = session_factory()
    try:
        db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
        db.flush()
        city = City(USERID=1, CITY_SLUG="test_city", UI_PORT=3001, API_PORT=8001)
        db.add(city)
        db.flush()
        db.add(AI(AIID=PERSONA_ID, HOME_CITYID=city.CITYID, AINAME="Alice"))
        db.commit()
    finally:
        db.close()

    persona = SimpleNamespace(
        persona_id=PERSONA_ID,
        current_building_id="alice_room",
        _save_session_metadata=lambda: None,
    )
    runtime = runtime if runtime is not None else FakeRuntime()
    controller = PulseController(runtime)

    manager = SimpleNamespace(
        SessionLocal=session_factory,
        all_personas={PERSONA_ID: persona},
        personas={PERSONA_ID: persona},
        event_scheduler=EventScheduler(),  # start() しない (手動発火)
        execution_ledger=ExecutionLedger(session_factory=session_factory),
        pulse_controller=controller,
        _save_modified_buildings=lambda: None,
    )
    manager.pulse_dispatcher = PulseDispatcher(manager)
    runtime.manager = manager

    sm = ScheduleManager(saiverse_manager=manager)
    return SimpleNamespace(
        manager=manager, sm=sm, runtime=runtime, controller=controller,
        session_factory=session_factory, engine=engine,
    )


def _add_oneshot(session_factory) -> int:
    db = session_factory()
    try:
        row = PersonaSchedule(
            PERSONA_ID=PERSONA_ID,
            SCHEDULE_TYPE="oneshot",
            META_PLAYBOOK="track_user_conversation",
            ENABLED=True,
            SCHEDULED_DATETIME=datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(minutes=1),
            COMPLETED=False,
        )
        db.add(row)
        db.commit()
        return row.SCHEDULE_ID
    finally:
        db.close()


def _fire(env, schedule_id: int) -> None:
    """登録済みの予約の callback を手動で呼ぶ (発火スレッドの直列実行と等価)。"""
    entry = env.manager.event_scheduler._entries_by_key.get(
        _schedule_key(schedule_id)
    )
    assert entry is not None, f"no reservation for schedule {schedule_id}"
    entry.callback()


def _completed_count(env, ids: List[int]) -> int:
    db = env.session_factory()
    try:
        return (
            db.query(PersonaSchedule)
            .filter(
                PersonaSchedule.SCHEDULE_ID.in_(ids),
                PersonaSchedule.COMPLETED.is_(True),
            )
            .count()
        )
    finally:
        db.close()


def _ledger_states(env) -> Dict[str, int]:
    """実行台帳の状態ごとの件数 (schedule 発火の kind のみ)。"""
    from database.models import ExecutionLedgerEntry

    db = env.session_factory()
    try:
        rows = (
            db.query(ExecutionLedgerEntry)
            .filter(ExecutionLedgerEntry.KIND == SCHEDULE_DISPATCH_LEDGER_KIND)
            .all()
        )
        out: Dict[str, int] = {}
        for row in rows:
            out[row.STATUS] = out.get(row.STATUS, 0) + 1
        return out
    finally:
        db.close()


def _occupy_persona(env) -> ExecutionRequest:
    """利用者との会話 1 本でペルソナを塞ぐ (priority USER なので中断されない)。"""
    busy = ExecutionRequest(
        type="user",
        persona_id=PERSONA_ID,
        building_id="alice_room",
        user_input="こんにちは",
    )
    threading.Thread(
        target=lambda: env.controller.submit(busy), daemon=True
    ).start()
    assert env.runtime.entered.wait(5.0), "occupying request did not start"
    return busy


def case_a_queue_overflow() -> None:
    """経路 A: 待ち行列の上限を超えて、古い要求が捨てられる。"""
    print("=" * 72)
    print(f"経路 A: 待ち行列の上限超過 (QUEUE_LIMIT={QUEUE_LIMIT})")
    print("=" * 72)
    env = _make_env()
    _occupy_persona(env)

    total = QUEUE_LIMIT + 2
    ids: List[int] = []
    for _ in range(total):
        sid = _add_oneshot(env.session_factory)
        ids.append(sid)
        env.sm.register_schedule(sid)
        _fire(env, sid)

    queued_now = len(env.controller._queues.get(PERSONA_ID, []))
    print(f"  仕掛けたアラーム              : {total} 件")
    print(f"  待ち行列に残っている要求      : {queued_now} 件")
    print(f"  DB の COMPLETED=True          : {_completed_count(env, ids)} 件")
    print(f"  実行台帳の状態                : {_ledger_states(env)}")

    # 会話を終わらせ、待ち行列を最後まで流す。
    env.runtime.release.set()
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if (
            not env.controller._queues.get(PERSONA_ID)
            and PERSONA_ID not in env.controller._current
        ):
            break
        time.sleep(0.05)

    executed = len(env.runtime.schedule_calls())
    print(f"  実際に走ったアラーム (run_meta_user 呼び出し) : {executed} 回")
    print(f"  記帳と実行の差                : {total - executed} 件")
    print()
    assert _completed_count(env, ids) == total, "全件が COMPLETED になっていない"
    assert executed < total, "上限超過の破棄が起きなかった"
    env.engine.dispose()


def case_b_shutdown_discard() -> None:
    """経路 B: 終了処理で待ち行列の要求が破棄される。"""
    print("=" * 72)
    print("経路 B: 終了処理での破棄")
    print("=" * 72)
    env = _make_env()
    _occupy_persona(env)

    total = 3
    ids: List[int] = []
    for _ in range(total):
        sid = _add_oneshot(env.session_factory)
        ids.append(sid)
        env.sm.register_schedule(sid)
        _fire(env, sid)

    print(f"  仕掛けたアラーム              : {total} 件")
    print(f"  待ち行列に残っている要求      : "
          f"{len(env.controller._queues.get(PERSONA_ID, []))} 件")
    print(f"  DB の COMPLETED=True          : {_completed_count(env, ids)} 件")
    print(f"  実行台帳の状態                : {_ledger_states(env)}")

    settled = env.controller.shutdown(timeout=5.0)
    print(f"  shutdown() の戻り値           : {settled}")
    time.sleep(0.3)

    executed = len(env.runtime.schedule_calls())
    leftover = len(env.controller._queues.get(PERSONA_ID, []))
    print(f"  実際に走ったアラーム          : {executed} 回")
    print(f"  破棄されたまま残る待ち要求    : {leftover} 件")
    print(f"  記帳と実行の差                : {total - executed} 件")
    print()
    assert _completed_count(env, ids) == total, "全件が COMPLETED になっていない"
    assert executed == 0, "終了処理中にアラームが走ってしまった"
    env.engine.dispose()


def case_c_cancelled_then_failed_resumption() -> None:
    """経路 C: 走り出したアラームを利用者が中断 → 「受理」で前進 → 復帰実行が失敗。

    ``runtime_outcome == "cancelled"`` も accepted に分類される
    (``saiverse/schedule_manager.py:1058``)。ここで oneshot は
    ``COMPLETED = True`` になり、台帳も completed で閉じる。その後に走る
    **復帰実行 (resumption)** の顛末は誰も読まないので、復帰が失敗しても
    再試行は来ない。受理の記帳と実行の顛末が分かれる最短の経路。
    """
    print("=" * 72)
    print("経路 C: 中断で「受理」→ 復帰実行が失敗しても再試行が来ない")
    print("=" * 72)

    resumption_error = threading.Event()

    def handler(index: int, kwargs: Dict[str, Any]) -> List[str]:
        token = kwargs.get("cancellation_token")
        if kwargs.get("pulse_type") == "schedule":
            user_input = kwargs.get("user_input") or ""
            if "[前回の処理が中断されました]" in user_input:
                # 復帰実行: 生成が失敗する (プロバイダ落ち等を模す)
                resumption_error.set()
                raise RuntimeError("resumed generation failed")
            # 初回のアラーム実行: 中断されるまで待つ
            while True:
                if token is not None and token.is_cancelled():
                    raise ExecutionCancelledException(
                        message="cancelled", interrupted_by=token.interrupted_by,
                    )
                time.sleep(0.01)
        return ["ok"]

    env = _make_env(ScriptedRuntime(handler))

    sid = _add_oneshot(env.session_factory)
    env.sm.register_schedule(sid)
    threading.Thread(target=lambda: _fire(env, sid), daemon=True).start()
    assert env.runtime.entered.wait(5.0), "alarm did not start"
    time.sleep(0.1)

    # 利用者が話しかける → 優先度 USER が走行中のアラームを中断する
    env.controller.submit(ExecutionRequest(
        type="user", persona_id=PERSONA_ID,
        building_id="alice_room", user_input="ねえ",
    ))

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if resumption_error.is_set() and PERSONA_ID not in env.controller._current:
            break
        time.sleep(0.05)

    entry = env.manager.event_scheduler._entries_by_key.get(_schedule_key(sid))
    print(f"  DB の COMPLETED=True          : {_completed_count(env, [sid])} 件 / 1 件中")
    print(f"  実行台帳の状態                : {_ledger_states(env)}")
    print(f"  復帰実行が失敗したか          : {resumption_error.is_set()}")
    print(f"  再試行の予約                  : {'あり' if entry else 'なし'}")
    print()
    assert _completed_count(env, [sid]) == 1, "中断で COMPLETED にならなかった"
    assert resumption_error.is_set(), "復帰実行が走らなかった"
    assert entry is None, "再試行の予約が作られている"
    env.engine.dispose()


def main() -> None:
    case_a_queue_overflow()
    case_b_shutdown_discard()
    case_c_cancelled_then_failed_resumption()
    print("観測完了。COMPLETED=True の件数と、実際に走った回数の差が要点。")


if __name__ == "__main__":
    main()
