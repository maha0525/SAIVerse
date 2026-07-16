"""BeatGate (Beat ロック + 実行台帳の関所) のテスト。

docs/intent/beat_execution_context.md §2.2/§3.4 / execution_ledger.md §2.2-2.3:

1. 直列化: 同一 persona の Beat は同時に走らない。別 persona は並行。
2. RLock 再入: 子ライン / Metabolism の同一スレッド再入はデッドロックせず、
   関所 (pending flush) は最外周の 1 回だけ。
3. 関所 fail-closed: flush が False / 例外なら BeatGateClosedError で Beat を
   開始しない。ロックは解放済み (leak しない)。
4. boundary: 解放の隙間で待機 Beat が挟まる / cancelled token で
   ExecutionCancelledException (再取得しない) / ネスト中・非保持スレッドは no-op。
5. PulseController 統合: BeatGateClosedError は user で re-raise、
   auto / meta_judgment で [] (実行未開始 = 副作用ゼロ)。
6. _run_spell_loop の周間 cancel 評価点と Beat 境界 (boundary) の呼び出し。
"""
from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from typing import Any, List, Optional
from unittest.mock import patch

import pytest

from sea.beat_gate import BeatGate, BeatGateClosedError, hold_beat
from sea.cancellation import CancellationToken, ExecutionCancelledException


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeLedger:
    """flush_pending_for_persona の呼び出しを記録し、設定した結果を返す。"""

    def __init__(self, default: bool = True):
        self.calls: List[Any] = []
        self.default = default

    def flush_pending_for_persona(self, persona_id):
        self.calls.append(persona_id)
        return self.default


def _gate(ledger: Optional[Any] = None) -> BeatGate:
    return BeatGate(SimpleNamespace(execution_ledger=ledger))


# ---------------------------------------------------------------------------
# 1. 直列化
# ---------------------------------------------------------------------------


def test_hold_serializes_same_persona_and_allows_other_personas():
    gate = _gate()
    order: List[str] = []
    meta_entered = threading.Event()
    other_entered = threading.Event()
    release_other = threading.Event()

    def meta_beat():
        with gate.hold("p1", purpose="meta_judgment"):
            order.append("meta")
            meta_entered.set()

    def other_persona_beat():
        with gate.hold("p2", purpose="auto"):
            other_entered.set()
            release_other.wait(2)

    t_meta = threading.Thread(target=meta_beat, daemon=True)
    t_other = threading.Thread(target=other_persona_beat, daemon=True)
    with gate.hold("p1", purpose="user"):
        order.append("main")
        t_other.start()
        # 別 persona (p2) は p1 の保持中でも並行して入れる
        assert other_entered.wait(2)
        t_meta.start()
        # 同 persona (p1) の META Beat はブロックされる
        assert not meta_entered.wait(0.3)
    # main Beat の解放後に META Beat が入る
    assert meta_entered.wait(2)
    release_other.set()
    t_meta.join(2)
    t_other.join(2)
    assert order == ["main", "meta"]


# ---------------------------------------------------------------------------
# 2. RLock 再入 + 関所は最外周の 1 回だけ
# ---------------------------------------------------------------------------


def test_reentrant_hold_does_not_deadlock_and_flushes_once():
    ledger = FakeLedger()
    gate = _gate(ledger)
    with gate.hold("p1", purpose="user"):
        with gate.hold("p1", purpose="run_playbook_child"):
            with gate.hold("p1", purpose="metabolism"):
                assert gate.held_depth("p1") == 3
        assert gate.held_depth("p1") == 1
    assert gate.held_depth("p1") == 0
    # 関所 flush は最外周の取得時の 1 回だけ (子ライン再入では走らない)
    assert ledger.calls == ["p1"]


# ---------------------------------------------------------------------------
# 3. 関所 fail-closed
# ---------------------------------------------------------------------------


def test_gate_closed_raises_and_releases_lock():
    ledger = FakeLedger(default=False)
    gate = _gate(ledger)
    with pytest.raises(BeatGateClosedError):
        with gate.hold("p1", purpose="user"):
            pytest.fail("gate closed の Beat 本体に入ってはいけない")
    assert gate.held_depth("p1") == 0

    # ロックは解放済み: 別スレッドが即取得できる (check_gate=False で関所を回避)
    acquired = threading.Event()

    def other():
        with gate.hold("p1", purpose="probe", check_gate=False):
            acquired.set()

    t = threading.Thread(target=other, daemon=True)
    t.start()
    assert acquired.wait(2)
    t.join(2)


def test_gate_flush_exception_is_fail_closed():
    class BoomLedger:
        def flush_pending_for_persona(self, persona_id):
            raise RuntimeError("db down")

    gate = _gate(BoomLedger())
    with pytest.raises(BeatGateClosedError):
        with gate.hold("p1", purpose="user"):
            pytest.fail("flush 例外でも Beat 本体に入ってはいけない")
    assert gate.held_depth("p1") == 0


def test_hold_without_ledger_skips_gate():
    # manager に execution_ledger が無い (テストシーム) → 取得のみで通る
    gate = BeatGate(SimpleNamespace())
    with gate.hold("p1", purpose="user"):
        assert gate.held_depth("p1") == 1


def test_hold_beat_returns_noop_without_gate_or_persona():
    # manager に beat_gate が無い / persona_id 不明 → nullcontext (既存テスト互換)
    with hold_beat(SimpleNamespace(), "p1", purpose="user"):
        pass
    with hold_beat(None, "p1", purpose="user"):
        pass
    with hold_beat(SimpleNamespace(beat_gate=_gate()), None, purpose="user"):
        pass


# ---------------------------------------------------------------------------
# 4. boundary
# ---------------------------------------------------------------------------


def test_boundary_lets_waiting_beat_interleave():
    ledger = FakeLedger()
    gate = _gate(ledger)
    interleaved = threading.Event()

    def waiter():
        with gate.hold("p1", purpose="meta_judgment"):
            interleaved.set()

    class GapToken:
        """boundary の解放の隙間 (cancel 評価点) で待機 Beat の完了を待つ。"""

        interrupted_by = None

        def is_cancelled(self):
            # boundary が release した後に呼ばれる — ここで waiter が挟まる
            assert interleaved.wait(2), "解放の隙間で待機 Beat が入れなかった"
            return False

    t = threading.Thread(target=waiter, daemon=True)
    with gate.hold("p1", purpose="user"):
        t.start()
        time.sleep(0.1)  # waiter が acquire でブロックするまで待つ
        assert not interleaved.is_set()
        gate.boundary("p1", GapToken())
        # boundary から戻った = 再取得済み。待機 Beat は隙間で完了している
        assert interleaved.is_set()
        assert gate.held_depth("p1") == 1
    t.join(2)
    # 関所: 最外周 hold で 1 回 + waiter の hold で 1 回 + boundary 再取得で 1 回
    assert ledger.calls.count("p1") == 3


def test_boundary_cancelled_raises_and_does_not_reacquire():
    gate = _gate()
    token = CancellationToken()
    token.cancel(interrupted_by="user")

    with pytest.raises(ExecutionCancelledException) as ei:
        with gate.hold("p1", purpose="auto"):
            gate.boundary("p1", token)
            pytest.fail("cancelled token で boundary は raise するはず")
    assert ei.value.interrupted_by == "user"
    # 再取得していない (hold の finally も二重解放しない)
    assert gate.held_depth("p1") == 0

    acquired = threading.Event()

    def other():
        with gate.hold("p1", purpose="user"):
            acquired.set()

    t = threading.Thread(target=other, daemon=True)
    t.start()
    assert acquired.wait(2)
    t.join(2)


def test_boundary_gate_closed_releases_and_propagates():
    # boundary 再取得時の関所が False → BeatGateClosedError、ロックは解放済み
    ledger = FakeLedger()
    gate = _gate(ledger)
    with pytest.raises(BeatGateClosedError):
        with gate.hold("p1", purpose="user"):
            ledger.default = False  # Beat 中に pending が発生し配送不能になった状況
            gate.boundary("p1", None)
    assert gate.held_depth("p1") == 0


def test_boundary_is_noop_when_nested():
    ledger = FakeLedger()
    gate = _gate(ledger)
    token = CancellationToken()
    token.cancel(interrupted_by="user")
    with gate.hold("p1", purpose="user"):
        with gate.hold("p1", purpose="child"):
            # depth>1: 子ラインは親の直列域の一部 — 何もしない (cancel も評価しない)
            gate.boundary("p1", token)
            assert gate.held_depth("p1") == 2
    # boundary の関所は走っていない (最外周 hold の 1 回だけ)
    assert ledger.calls == ["p1"]


def test_boundary_is_noop_on_non_owner_thread():
    gate = _gate()
    errors: List[BaseException] = []
    with gate.hold("p1", purpose="user"):

        def other():
            try:
                # 非保持スレッド (depth==0) → no-op (release 例外を出さない)
                gate.boundary("p1", None)
            except BaseException as exc:  # noqa: BLE001 — テストの検分用
                errors.append(exc)

        t = threading.Thread(target=other, daemon=True)
        t.start()
        t.join(2)
        assert errors == []
        assert gate.held_depth("p1") == 1


# ---------------------------------------------------------------------------
# 5. PulseController 統合
# ---------------------------------------------------------------------------


def _make_controller(exc: BaseException):
    from sea.pulse_controller import PulseController

    persona = SimpleNamespace(persona_id="p1")

    def run_meta_user(**kwargs):
        raise exc

    sea_runtime = SimpleNamespace(
        manager=SimpleNamespace(all_personas={"p1": persona}),
        run_meta_user=run_meta_user,
    )
    return PulseController(sea_runtime)


def test_pulse_controller_reraises_gate_closed_for_user():
    pc = _make_controller(BeatGateClosedError("p1", "user"))
    with pytest.raises(BeatGateClosedError):
        pc.submit_user("p1", "b1", "こんにちは")


def test_pulse_controller_returns_empty_for_auto_gate_closed():
    pc = _make_controller(BeatGateClosedError("p1", "auto"))
    result = pc.submit_auto("p1", "b1", meta_playbook="track_autonomous")
    assert result == []


def test_pulse_controller_meta_lane_returns_empty_on_gate_closed():
    pc = _make_controller(BeatGateClosedError("p1", "meta_judgment"))
    result = pc.submit_meta_judgment(
        "p1", "b1", meta_playbook="meta_judgment_running",
    )
    assert result == []


# ---------------------------------------------------------------------------
# 6. _run_spell_loop: 周間 cancel 評価点 + Beat 境界
# ---------------------------------------------------------------------------

SPELL_NAME = "make_doc"
SPELL_TEXT = f"やるぞ。\n/spell name='{SPELL_NAME}' args={{}}"


class SpellLoopRuntime:
    """_run_spell_loop が触る SEARuntime の最小フェイク (test_work_session と同型)。"""

    def __init__(self, manager: Optional[Any] = None):
        self.stored: List[str] = []
        self.manager = manager if manager is not None else SimpleNamespace()
        self.session_lifecycle = SimpleNamespace(
            touch_anchor_after_llm_call=lambda persona, usage: None,
        )

    def _store_memory(self, persona, text, **kwargs):
        self.stored.append(text)
        return "msg-1" if kwargs.get("return_message_id") else True

    def _default_temperature(self, persona):
        return None

    def _get_cache_kwargs(self, persona_id=None):
        return {}

    def _dump_llm_io(self, *args, **kwargs):
        return None

    def _accumulate_usage(self, *args, **kwargs):
        return None


class ScriptedClient:
    """スクリプト化された retry 応答を順に返す mock LLM クライアント。"""

    def __init__(self, responses: List[str]):
        self.responses = list(responses)

    def generate(self, messages, tools=None, temperature=None, **kwargs):
        if not self.responses:
            raise AssertionError("ScriptedClient: no scripted responses left")
        return self.responses.pop(0)

    def consume_usage(self):
        return None


def _run_spell_loop_sync(runtime, client, token=None):
    from sea import runtime_llm

    persona = SimpleNamespace(persona_id="p1")
    state = {
        "_pulse_id": "pulse-1",
        "_pulse_context": None,
        "_cancellation_token": token,
    }
    node_def = SimpleNamespace(memorize=None, speak=False)
    playbook = SimpleNamespace(name="test_playbook")
    return asyncio.run(runtime_llm._run_spell_loop(
        text=SPELL_TEXT,
        spell_enabled=True,
        llm_client=client,
        runtime=runtime,
        persona=persona,
        building_id="b1",
        state=state,
        messages=[],
        playbook=playbook,
        event_callback=None,
        node_def=node_def,
    ))


def _spell_patches(fake_spell):
    from sea import runtime_llm

    return (
        patch.object(runtime_llm, "SPELL_TOOL_NAMES", {SPELL_NAME}),
        patch.object(runtime_llm, "_run_spell_tool_async", new=fake_spell),
    )


def test_spell_loop_cancel_between_rounds_raises():
    """round 1 中に cancel された token は、round 2 に入る前に評価されて raise する。"""
    token = CancellationToken()

    async def fake_spell(tool_name, tool_args, persona, state, playbook_name,
                         event_callback, messages=None):
        token.cancel(interrupted_by="user")  # spell 実行中 (round 1) に割り込み
        return ("done", None)

    # retry は spell 入りを返す = ペルソナは round 2 を続けたがっている
    client = ScriptedClient([SPELL_TEXT])
    runtime = SpellLoopRuntime()  # beat_gate 無し → 周頭の cancel 評価点が効く
    p_names, p_exec = _spell_patches(fake_spell)
    with p_names, p_exec:
        with pytest.raises(ExecutionCancelledException) as ei:
            _run_spell_loop_sync(runtime, client, token=token)
    assert ei.value.interrupted_by == "user"
    # round 1 の記録 (judgment + spell 結果) は書かれている (記録済み分は正)
    assert any("/spell" in text for text in runtime.stored)


def test_spell_loop_calls_boundary_between_rounds():
    """spell 結果の記録後・次ラウンド生成前に beat_gate.boundary が呼ばれる。"""
    boundary_calls: List[Any] = []

    class SpyGate:
        def boundary(self, persona_id, cancellation_token=None):
            boundary_calls.append(persona_id)

    async def fake_spell(tool_name, tool_args, persona, state, playbook_name,
                         event_callback, messages=None):
        return ("done", None)

    client = ScriptedClient(["おわり。"])  # retry は spell なし → 自然終了
    runtime = SpellLoopRuntime(manager=SimpleNamespace(beat_gate=SpyGate()))
    p_names, p_exec = _spell_patches(fake_spell)
    with p_names, p_exec:
        merged, continuation, loop_count = _run_spell_loop_sync(runtime, client)
    assert loop_count == 1
    assert continuation == "おわり。"
    # 1 ラウンド = 1 回の Beat 境界
    assert boundary_calls == ["p1"]


def test_spell_loop_boundary_gate_closed_propagates():
    """boundary の BeatGateClosedError は partial 保存へ降格せず伝播する。"""

    class ClosingGate:
        def boundary(self, persona_id, cancellation_token=None):
            raise BeatGateClosedError(persona_id, "boundary")

    async def fake_spell(tool_name, tool_args, persona, state, playbook_name,
                         event_callback, messages=None):
        return ("done", None)

    client = ScriptedClient([])  # 生成に到達しないはず
    runtime = SpellLoopRuntime(manager=SimpleNamespace(beat_gate=ClosingGate()))
    p_names, p_exec = _spell_patches(fake_spell)
    with p_names, p_exec:
        with pytest.raises(BeatGateClosedError):
            _run_spell_loop_sync(runtime, client)
