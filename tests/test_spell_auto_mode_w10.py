"""W10 (柱8) Spell 監査残の回帰テスト — auto_mode の正直化と realtime spell の gate。

Spell 監査 (2026-07-15) の残 finding 2 点を固定する:

1. auto_mode の正直化: リポジトリ全体で auto_mode=True を渡す箇所が存在せず、
   自律 Pulse でもスペル実行が「ユーザー起動」として下流 (確認ダイアログの
   自動承認 / Playbook の auto フィルタ) に伝わっていた。修正後は
   run_meta_user が pulse_type から導出し、state["_auto_mode"] で子まで運ぶ。
2. realtime spell の SPELL_ENABLED gate: persona の SPELL_ENABLED=false でも
   binding された realtime spell が毎 Pulse 自動実行されていた。修正後は
   state["_spell_enabled"] が False なら実行しない。
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

import sea.runtime_llm as runtime_llm
from sea.runtime import SEARuntime
from sea.runtime_engine import RuntimeEngine
from sea.runtime_graph import compile_with_langgraph
from tools.context import get_auto_mode, is_user_configured_invocation


# ---------------------------------------------------------------------------
# 1. run_meta_user: pulse_type → auto_mode の導出
# ---------------------------------------------------------------------------

def _runtime_and_persona() -> tuple[SEARuntime, SimpleNamespace]:
    manager = SimpleNamespace(building_histories={"b1": []})
    runtime = SEARuntime(manager)
    persona = SimpleNamespace(
        persona_name="p",
        persona_id="pid",
        model="m",
        llm_client=object(),
        history_manager=SimpleNamespace(add_message=Mock()),
        execution_state={},
    )
    return runtime, persona


@pytest.mark.parametrize(
    "pulse_type,expected_auto_mode",
    [
        ("user", False),
        ("schedule", True),
        ("auto", True),
    ],
)
def test_run_meta_user_derives_auto_mode_from_pulse_type(pulse_type, expected_auto_mode):
    """user Pulse だけが auto_mode=False。schedule / auto は True で渡る。"""
    runtime, persona = _runtime_and_persona()
    playbook = SimpleNamespace(name="meta/exec", start_node="exec", context_requirements=None)

    runtime._choose_playbook = Mock(return_value=playbook)
    runtime._load_playbook_for = Mock(return_value=playbook)
    runtime._run_playbook = Mock(return_value=[])
    runtime.session_lifecycle = MagicMock()

    runtime.run_meta_user(
        persona, "hello", "b1",
        meta_playbook="meta/exec",
        pulse_type=pulse_type,
    )

    assert runtime._run_playbook.call_count == 1
    kwargs = runtime._run_playbook.call_args.kwargs
    assert kwargs["auto_mode"] is expected_auto_mode
    assert kwargs["pulse_type"] == pulse_type


# ---------------------------------------------------------------------------
# 2. compile_with_langgraph: state["_auto_mode"] の運搬 (単調 OR)
# ---------------------------------------------------------------------------

def _capture_initial_state(monkeypatch) -> dict:
    """compile_playbook を差し替え、graph 実行に渡る initial_state を捕獲する。"""
    captured: dict = {}

    def _fake_compile(*args, **kwargs):
        async def _fake_graph(initial_state, config):
            captured.update(initial_state)
            return initial_state
        return _fake_graph

    monkeypatch.setattr("sea.runtime_graph.compile_playbook", _fake_compile)
    return captured


def _minimal_playbook() -> SimpleNamespace:
    return SimpleNamespace(
        name="pb", start_node="n0", context_requirements=None,
        input_schema=[], output_schema=None,
    )


@pytest.mark.parametrize(
    "auto_mode,parent_auto,expected",
    [
        (True, False, True),    # 根元が auto
        (False, True, True),    # 親 Pulse が auto なら子も auto (単調)
        (False, False, False),  # user のまま
    ],
)
def test_initial_state_carries_auto_mode(monkeypatch, auto_mode, parent_auto, expected):
    captured = _capture_initial_state(monkeypatch)
    runtime = MagicMock()
    persona = SimpleNamespace(execution_state={})

    compile_with_langgraph(
        runtime,
        _minimal_playbook(),
        persona,
        "b1",
        None,
        auto_mode,
        base_messages=[],
        pulse_id="pulse-test",
        parent_state={"_auto_mode": parent_auto},
    )

    assert captured["_auto_mode"] is expected


# ---------------------------------------------------------------------------
# 3. スペル実行が state["_auto_mode"] を persona_context へ渡す
# ---------------------------------------------------------------------------

def test_spell_executor_passes_state_auto_mode_to_persona_context(monkeypatch):
    """自律 Pulse (_auto_mode=True) のスペルから get_auto_mode() が True で見える。

    これが False だと、確認ダイアログ (tools/confirmation.py) が誰も見ていない
    UI へ確認を出してブロックし、auto フィルタ (list_available_playbooks) が
    user 専用 Playbook を自律 Pulse に見せる。
    """
    seen: dict = {}

    def _fake_spell() -> str:
        seen["auto_mode"] = get_auto_mode()
        return "ok"

    monkeypatch.setitem(runtime_llm.TOOL_REGISTRY, "w10_fake_spell", _fake_spell)

    persona = SimpleNamespace(persona_id="pid", persona_log_path=None, manager_ref=None)
    state = {"_auto_mode": True, "_persona_obj": persona}

    result_text, _meta, ok = asyncio.run(
        runtime_llm._run_spell_tool_async(
            "w10_fake_spell", {}, persona, state, "pb", None, messages=[],
        )
    )

    assert ok is True
    assert result_text == "ok"
    assert seen["auto_mode"] is True


def test_pre_spell_execution_marks_the_invocation_as_user_configured(monkeypatch):
    """pre_spells 経路のスペルからは「ユーザーが書いた起動」として見える。

    accessor を patch するテストは配線の欠落を見逃すので、実行経路
    (_execute_pre_spells → _run_spell_tool_async → persona_context) を
    そのまま通して contextvar の値を観測する。
    """
    seen: dict = {}

    def _fake_spell() -> str:
        seen["user_configured"] = is_user_configured_invocation()
        return "ok"

    monkeypatch.setitem(runtime_llm.TOOL_REGISTRY, "w10_fake_spell", _fake_spell)
    # pre_spells は「スペルとして唱えられる名前か」を SPELL_TOOL_NAMES で検査する。
    # グローバルの集合を直接 add すると他テストへ漏れるので、束縛ごと差し替える。
    monkeypatch.setattr(
        runtime_llm, "SPELL_TOOL_NAMES",
        set(runtime_llm.SPELL_TOOL_NAMES) | {"w10_fake_spell"},
    )

    persona = SimpleNamespace(persona_id="pid", persona_log_path=None, manager_ref=None)
    state = {"_persona_obj": persona, "_messages": [], "_auto_mode": False}
    playbook = SimpleNamespace(name="pb")

    asyncio.run(
        runtime_llm._execute_pre_spells(
            ["/spell name='w10_fake_spell' args={}"],
            MagicMock(), persona, "b1", state, playbook, None,
        )
    )

    assert seen["user_configured"] is True


def test_pre_spell_with_llm_decided_args_is_not_user_configured(monkeypatch):
    """引数を LLM が決めた pre_spell は「ユーザーが名指しした起動」ではない。

    引数省略形 (``/spell name='run_playbook'``) は spell_args_decider が引数を
    決める。``run_playbook`` の場合それは **どの Playbook を起こすか** なので、
    ユーザー指定として承認すると、ユーザーが選んだ覚えのない Playbook が
    無確認で走る。
    """
    seen: dict = {}

    def _fake_spell(**kwargs) -> str:
        seen["user_configured"] = is_user_configured_invocation()
        return "ok"

    monkeypatch.setitem(runtime_llm.TOOL_REGISTRY, "w10_fake_spell", _fake_spell)
    monkeypatch.setattr(
        runtime_llm, "SPELL_TOOL_NAMES",
        set(runtime_llm.SPELL_TOOL_NAMES) | {"w10_fake_spell"},
    )

    async def _fake_decider(*args, **kwargs):
        return {"target": "chosen_by_llm"}

    monkeypatch.setattr(runtime_llm, "_decide_spell_args_via_playbook", _fake_decider)

    persona = SimpleNamespace(persona_id="pid", persona_log_path=None, manager_ref=None)
    state = {"_persona_obj": persona, "_messages": [], "_auto_mode": False}
    playbook = SimpleNamespace(name="pb")

    asyncio.run(
        runtime_llm._execute_pre_spells(
            ["/spell name='w10_fake_spell'"],
            MagicMock(), persona, "b1", state, playbook, None,
        )
    )

    assert seen["user_configured"] is False


def test_persona_spoken_spell_is_not_user_configured(monkeypatch):
    """ペルソナが唱えたスペルは「ユーザーが書いた起動」ではない。

    同じ Pulse の中でも、承認されているのはユーザーが指定した起動だけ。
    """
    seen: dict = {}

    def _fake_spell() -> str:
        seen["user_configured"] = is_user_configured_invocation()
        return "ok"

    monkeypatch.setitem(runtime_llm.TOOL_REGISTRY, "w10_fake_spell", _fake_spell)

    persona = SimpleNamespace(persona_id="pid", persona_log_path=None, manager_ref=None)
    state = {"_persona_obj": persona, "_auto_mode": False}

    asyncio.run(
        runtime_llm._run_spell_tool_async(
            "w10_fake_spell", {}, persona, state, "pb", None, messages=[],
        )
    )

    assert seen["user_configured"] is False


def test_spell_executor_defaults_to_user_mode_without_flag(monkeypatch):
    """_auto_mode が無い state (レガシー/直接呼び出し) は user 扱いに倒す。"""
    seen: dict = {}

    def _fake_spell() -> str:
        seen["auto_mode"] = get_auto_mode()
        return "ok"

    monkeypatch.setitem(runtime_llm.TOOL_REGISTRY, "w10_fake_spell", _fake_spell)

    persona = SimpleNamespace(persona_id="pid", persona_log_path=None, manager_ref=None)
    state = {"_persona_obj": persona}

    asyncio.run(
        runtime_llm._run_spell_tool_async(
            "w10_fake_spell", {}, persona, state, "pb", None, messages=[],
        )
    )

    assert seen["auto_mode"] is False


# ---------------------------------------------------------------------------
# 4. realtime spell は SPELL_ENABLED=false で実行されない
# ---------------------------------------------------------------------------

class _Boom(Exception):
    """realtime gate 通過直後 (status イベント送出) で node を止める番兵。"""


def _llm_node_state(spell_enabled: bool) -> dict:
    return {
        "_spell_enabled": spell_enabled,
        "_messages": [],
    }


def _run_llm_node_until_status(state: dict, monkeypatch) -> list:
    """lg_llm_node を realtime ブロック直後の status イベントまで走らせる。

    event_callback が _Boom を投げて LLM 呼び出し前に必ず停止するので、
    realtime spell の実行有無だけを観測できる。
    """
    calls: list = []

    async def _recorder(*args, **kwargs):
        calls.append(args)

    monkeypatch.setattr(runtime_llm, "_execute_realtime_spells", _recorder)

    def _raising_callback(event):
        raise _Boom()

    # persona_id=None で node_with_persona_context の wrap を素通しし、
    # persona_context 依存なしで node 本体だけを走らせる。
    persona = SimpleNamespace(persona_id=None, persona_name="p")
    playbook = SimpleNamespace(name="pb")
    node_def = SimpleNamespace(id="llm")
    node = runtime_llm.lg_llm_node(
        MagicMock(), node_def, persona, "b1", playbook, _raising_callback,
    )

    with pytest.raises(_Boom):
        asyncio.run(node(state))
    return calls


def test_realtime_spells_skipped_when_spell_disabled(monkeypatch):
    state = _llm_node_state(spell_enabled=False)
    calls = _run_llm_node_until_status(state, monkeypatch)

    assert calls == []
    assert "_realtime_spells_executed" not in state


def test_realtime_spells_run_when_spell_enabled(monkeypatch):
    state = _llm_node_state(spell_enabled=True)
    calls = _run_llm_node_until_status(state, monkeypatch)

    assert len(calls) == 1
    assert state["_realtime_spells_executed"] is True


# ---------------------------------------------------------------------------
# 5. EXEC ノードの ask_every_time 許可判定 (user / auto / schedule)
#
# 判定の順序そのものが仕様。「schedule は設定行為が事前承認」→「ユーザー不在
# なら確認できない」→「確認ダイアログ」の順で、①と②が逆になると schedule の
# 自動化が静かに skip される (2026-08-16 W10 レビュー F1 の回帰)。
# ---------------------------------------------------------------------------

def _exec_engine(permission: str = "ask_every_time", dialog_response: str = "allow"):
    """ask_every_time な sub-playbook を持つ exec ノードと、その runtime mock。

    許可判定は本物 (``SEARuntime.decide_playbook_permission``) を通す。/run_playbook
    スペルと共有する正典なので、fake を挟むと「EXEC が正典に従うか」を検証できない。
    """
    runtime = MagicMock()
    runtime.decide_playbook_permission = (
        lambda *args, **kwargs: SEARuntime.decide_playbook_permission(runtime, *args, **kwargs)
    )
    runtime._load_playbook_for = Mock(return_value=SimpleNamespace(name="risky_pb"))
    runtime._get_playbook_permission = Mock(return_value=permission)
    runtime._request_playbook_permission = Mock(return_value=dialog_response)
    runtime._effective_building_id = Mock(return_value="b1")
    runtime._run_playbook = Mock(return_value=[])
    runtime._notify_persona_permission_result = Mock()
    runtime._append_tool_result_message = Mock()

    manager = SimpleNamespace(city_id=1)
    engine = RuntimeEngine(runtime, manager, Mock(), {})
    return runtime, engine


def _run_exec_node(engine, *, factory_auto_mode: bool, state: dict):
    node_def = SimpleNamespace(
        id="exec", playbook_source="selected_playbook", args_source="selected_args",
        args=None, execution="inline",
    )
    playbook = SimpleNamespace(name="meta_exec_speak", display_name=None)
    persona = SimpleNamespace(persona_id="pid", persona_name="p")
    # event_callback は確認ダイアログの宛先。user Pulse の分岐でこれが無いと
    # 「聞く先が無いので拒否」になるため、UI の居る Pulse を模して渡す。
    node = engine.lg_exec_node(
        node_def, playbook, persona, "b1", factory_auto_mode, [], Mock(),
    )
    asyncio.run(node(state))


@pytest.mark.parametrize(
    "pulse_type,auto_mode,expect_dialog,expect_executed",
    [
        # user Pulse: 確認ダイアログを出し、許可されたら実行する
        ("user", False, True, True),
        # 自律 Pulse: 誰も見ていない UI へ確認を出さず、skip する
        ("auto", True, False, False),
        # schedule Pulse: EXEC が起こす Playbook 名はユーザーが名指ししたもの
        # ではない (router / 呼び出し側が積んだ値) ので事前承認は名乗らない。
        # ユーザーが設定画面で選んだ起動は pre_spells 経路を通る。
        ("schedule", True, False, False),
    ],
)
def test_exec_ask_every_time_by_pulse_type(pulse_type, auto_mode, expect_dialog, expect_executed):
    runtime, engine = _exec_engine()
    state = {
        "selected_playbook": "risky_pb",
        "_pulse_type": pulse_type,
        "_auto_mode": auto_mode,
    }

    _run_exec_node(engine, factory_auto_mode=auto_mode, state=state)

    assert runtime._request_playbook_permission.called is expect_dialog
    assert runtime._run_playbook.called is expect_executed
    # 実行されなかったケースはペルソナへ理由が返っている
    assert runtime._notify_persona_permission_result.called is not expect_executed


def test_exec_ask_every_time_user_denial_blocks_execution():
    """user Pulse で拒否されたら実行しない (事前承認の順序変更で緩まないこと)。"""
    runtime, engine = _exec_engine(dialog_response="deny")
    state = {"selected_playbook": "risky_pb", "_pulse_type": "user", "_auto_mode": False}

    _run_exec_node(engine, factory_auto_mode=False, state=state)

    assert runtime._request_playbook_permission.called is True
    assert runtime._run_playbook.called is False


def test_exec_reads_auto_mode_from_state_not_factory_argument():
    """factory が capture した引数より state の実効値が優先される (F2)。

    親 Pulse が auto でも、子 Playbook の compile が auto_mode=False で
    呼ばれる経路がありうる。判定に使うのは state 側の実効値。
    """
    runtime, engine = _exec_engine()
    state = {"selected_playbook": "risky_pb", "_pulse_type": "auto", "_auto_mode": True}

    _run_exec_node(engine, factory_auto_mode=False, state=state)

    assert runtime._request_playbook_permission.called is False
    assert runtime._run_playbook.called is False


def test_call_playbook_forwards_the_pulse_event_channel(monkeypatch):
    """`call_playbook` は呼び出し元 Pulse の UI チャネルを子へ渡す。

    渡さないと、子の EXEC ノードが ask_every_time の Playbook を起こすときに
    確認ダイアログの宛先を失い、`_request_playbook_permission` が「チャネル
    無し = deny」で即拒否する。ユーザーが見ている会話でも「毎回確認」の
    Playbook が黙って拒否される。
    """
    from builtin_data.tools import call_playbook as call_playbook_mod

    sea_runtime = MagicMock()
    sea_runtime._load_playbook_for = Mock(return_value=SimpleNamespace(name="meta_exec_speak"))
    sea_runtime._run_playbook = Mock(return_value=["done"])
    persona = SimpleNamespace(persona_id="pid", current_building_id="b1")
    manager = SimpleNamespace(sea_runtime=sea_runtime, personas={"pid": persona})
    sentinel_callback = Mock()

    monkeypatch.setattr(call_playbook_mod, "get_active_persona_id", lambda: "pid")
    monkeypatch.setattr(call_playbook_mod, "get_active_manager", lambda: manager)
    monkeypatch.setattr(call_playbook_mod, "get_auto_mode", lambda: False)
    monkeypatch.setattr(call_playbook_mod, "get_event_callback", lambda: sentinel_callback)

    call_playbook_mod.call_playbook("risky_pb")

    kwargs = sea_runtime._run_playbook.call_args.kwargs
    assert kwargs["event_callback"] is sentinel_callback


def test_exec_passes_effective_auto_mode_to_sub_playbook():
    """auto の親から起動された子 Playbook にも auto_mode=True が渡る (F2)。"""
    runtime, engine = _exec_engine(permission="auto_allow")
    state = {"selected_playbook": "risky_pb", "_pulse_type": "auto", "_auto_mode": True}

    _run_exec_node(engine, factory_auto_mode=False, state=state)

    assert runtime._run_playbook.called is True
    # _run_playbook(sub_pb, persona, building_id, sub_input, auto_mode, ...)
    assert runtime._run_playbook.call_args.args[4] is True
