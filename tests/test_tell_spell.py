"""tell スペル (builtin_data/tools/tell.py) のテスト。

autonomous_pulse_vehicle.md §B の契約:

- 宛先の検証 (user / all / 同室ペルソナのみ。不在の相手には理由文を返し LLM を呼ばない)
- 会話中のユーザーへの tell は no-op + 教育文 (返答との二重発話の防止)。
  会話中か確認できないときも見送る (fail-closed)
- 言葉は CONVERSATION aspect の 1 Beat (標準モデル側) が書く
- 投函は _emit_say (Building 履歴 + UI + TTS) + 本人の記憶 (_store_memory)、
  metadata に宛先 (tell_target) が残る。どちらの失敗も正直に返す
- Beat ロックは取り直さない (親 Beat の内側で走る) — 別スレッドから取ると
  永久ブロックする
- 会話エピソード・Track・タイムアウトには一切触らない (このテストの fake には
  そもそもその口が無い = 呼べば AttributeError で落ちる)
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from sea.pulse_context import Aspect, PulseContext
from tools.context import persona_context

PERSONA_ID = "p1"
BUILDING = "cafe"


class FakeLLMClient:
    def __init__(self, responses: List[Any]):
        self.responses = list(responses)
        self.calls: List[List[Dict[str, Any]]] = []

    def generate(self, messages, tools=None, temperature=None, **kwargs):
        self.calls.append(list(messages))
        return self.responses.pop(0)

    def consume_usage(self):
        return None


class FakeRuntime:
    def __init__(self, client: FakeLLMClient):
        self.llm_client = client
        self.emitted: List[Dict[str, Any]] = []
        self.stored: List[Dict[str, Any]] = []
        # 実装と同じ戻り値の型:
        # - emit_say は building message dict。**保存できた印は message_id**
        #   (DB 採番)。DB insert が失敗しても HistoryManager は渡した dict を
        #   そのまま返すため、message_id の無い truthy な dict が返る。
        # - _store_memory(return_message_id=True) は挿入 id (失敗で "")。
        self.emit_result: Any = {"role": "assistant", "message_id": "b1:1"}
        self.store_result_id: Any = "mem-1"
        self.flushed = False
        self.selected_aspects: List[Any] = []
        self._pulse_contexts: Dict[str, PulseContext] = {}

    def _get_or_create_pulse_context(self, pulse_id: str) -> PulseContext:
        self._pulse_contexts.setdefault(pulse_id, PulseContext(pulse_id=pulse_id))
        return self._pulse_contexts[pulse_id]

    def _prepare_context(self, persona, building_id, user_input, **kwargs):
        return [{"role": "system", "content": "head"}]

    def select_llm_client(self, node_def, persona, execution_context=None, state=None, **kw):
        pulse_ctx = state.get("_pulse_context") if state else None
        frame = pulse_ctx.current_line() if pulse_ctx is not None else None
        self.selected_aspects.append(getattr(frame, "aspect", None))
        return self.llm_client, execution_context.model_key

    def _default_temperature(self, persona):
        return None

    def _get_cache_kwargs(self, persona_id=None):
        return {}

    def _dump_llm_io(self, playbook_name, node_id, persona, messages, text):
        pass

    def _emit_say(self, persona, building_id, text, pulse_id=None, metadata=None):
        self.emitted.append({
            "building_id": building_id, "text": text, "metadata": metadata,
        })
        return self.emit_result

    def _store_memory(self, persona, text, **kwargs):
        self.stored.append({"text": text, **kwargs})
        if kwargs.get("return_message_id"):
            return self.store_result_id
        return bool(self.store_result_id)

    def _flush_pulse_logs(self, persona, pulse_ctx):
        self.flushed = True


def _make_env(responses: List[Any], occupants: List[str] | None = None):
    client = FakeLLMClient(responses)
    runtime = FakeRuntime(client)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, persona_name="アリス", current_building_id=BUILDING,
    )
    other = SimpleNamespace(
        persona_id="p2", persona_name="ベル", current_building_id=BUILDING,
    )
    manager = SimpleNamespace(
        personas={PERSONA_ID: persona, "p2": other},
        occupants={BUILDING: occupants if occupants is not None else [PERSONA_ID, "p2"]},
        sea_runtime=runtime,
    )
    return manager, runtime, client


@contextmanager
def _ctx(manager):
    with persona_context(PERSONA_ID, "/tmp/p1", manager=manager):
        yield


def _fake_exec_ctx():
    ctx = SimpleNamespace(model_key="std-model")
    ctx.with_model = lambda m: SimpleNamespace(model_key=m, with_model=ctx.with_model)
    return ctx


def _patched_resolve(runtime):
    """resolve_execution_context を差し替え、呼び出し時の active aspect を記録する。"""
    def fake_resolve(persona, pulse_ctx):
        frame = pulse_ctx.current_line()
        runtime.selected_aspects.append(getattr(frame, "aspect", None))
        return _fake_exec_ctx()
    return patch("sea.pulse_context.resolve_execution_context", side_effect=fake_resolve)


def _conversation_state(state):
    """会話中かの三値 (True / False / None=不明) を差し替える。

    fake manager では出来事の読み取りが成立しないので、tell の分岐を
    見るテストはここで明示的に状態を与える。
    """
    return patch("saiverse.day_plan.get_user_conversation_state", return_value=state)


def test_tell_user_composes_on_conversation_aspect_and_delivers():
    from builtin_data.tools.tell import tell

    manager, runtime, client = _make_env(["まはー、面白い記事を見つけたよ。"])
    with _ctx(manager), _patched_resolve(runtime), _conversation_state(False):
        result = tell(target="user", gist="記事の共有")

    assert "ユーザー" in result and "声をかけました" in result
    # 言葉を書く Beat は CONVERSATION aspect で解決された (→ 標準モデル)
    assert Aspect.CONVERSATION in runtime.selected_aspects
    # 投函: Building 履歴へ 1 通、宛先が metadata に残る
    assert len(runtime.emitted) == 1
    assert runtime.emitted[0]["building_id"] == BUILDING
    assert runtime.emitted[0]["metadata"]["tell_target"] == "user"
    assert runtime.emitted[0]["metadata"]["tell_gist"] == "記事の共有"
    assert "面白い記事" in runtime.emitted[0]["text"]
    # 本人の記憶にも発話として残る
    assert len(runtime.stored) == 1
    assert runtime.stored[0]["metadata"]["tell_target"] == "user"
    # 指示 (directive) に宛先と gist が入っている
    directive = client.calls[0][-1]["content"]
    assert "ユーザー" in directive
    assert "記事の共有" in directive
    # ライン frame は後始末され、pulse ログは flush 済み
    assert runtime.flushed


def test_tell_persona_by_name_resolves_to_id():
    from builtin_data.tools.tell import tell

    manager, runtime, client = _make_env(["ベル、ちょっといい？"])
    with _ctx(manager), _patched_resolve(runtime):
        result = tell(target="ベル")

    assert "ベル" in result and "声をかけました" in result
    assert runtime.emitted[0]["metadata"]["tell_target"] == "p2"
    assert "tell_gist" not in runtime.emitted[0]["metadata"]


def test_tell_unknown_target_returns_reason_without_llm():
    from builtin_data.tools.tell import tell

    manager, runtime, client = _make_env(["(呼ばれないはず)"])
    with _ctx(manager), _patched_resolve(runtime):
        result = tell(target="どこかの誰か")

    assert "この場所にいません" in result
    assert "ベル" in result  # 声をかけられる相手の提示
    assert client.calls == []
    assert runtime.emitted == []


def test_tell_user_during_conversation_is_noop_with_guidance():
    from builtin_data.tools.tell import tell

    manager, runtime, client = _make_env(["(呼ばれないはず)"])
    with _ctx(manager), _patched_resolve(runtime), _conversation_state(True):
        result = tell(target="user")

    assert "会話の最中" in result
    assert "返答" in result
    assert client.calls == []
    assert runtime.emitted == []


def test_tell_user_is_withheld_when_conversation_state_is_unknown():
    """会話中か読めないときは発声しない (fail-closed)。

    二重発話は届いた後では取り消せない。見送りは次の機会に唱え直せる。
    """
    from builtin_data.tools.tell import tell

    manager, runtime, client = _make_env(["(呼ばれないはず)"])
    with _ctx(manager), _patched_resolve(runtime), _conversation_state(None):
        result = tell(target="user")

    assert "確認できませんでした" in result
    assert "見送" in result
    assert client.calls == []
    assert runtime.emitted == []
    assert runtime.stored == []


def test_tell_all_during_conversation_is_allowed():
    """会話中でも user 以外 (all / 同席ペルソナ) への一言は塞がない。"""
    from builtin_data.tools.tell import tell

    manager, runtime, client = _make_env(["みんな、聞いて。"])
    with _ctx(manager), _patched_resolve(runtime), _conversation_state(True):
        result = tell(target="all")

    assert "声をかけました" in result
    assert runtime.emitted[0]["metadata"]["tell_target"] == "all"


def test_tell_empty_generation_does_not_emit():
    from builtin_data.tools.tell import tell

    manager, runtime, client = _make_env(["   "])
    with _ctx(manager), _patched_resolve(runtime), _conversation_state(False):
        result = tell(target="user")

    assert "言葉が出てきませんでした" in result
    assert runtime.emitted == []
    assert runtime.stored == []


@pytest.mark.parametrize("emit_result", [
    None,                                        # 隔離建物 / 保存前に例外
    {},                                          # add_to_building_only の隔離返り値
    {"role": "assistant", "content": "まはー、聞いて。"},  # DB insert 失敗 = 渡した dict がそのまま返る
])
def test_tell_reports_history_failure_without_claiming_silence(emit_result):
    """履歴に残らなくても「言ってしまった」— 出た事実を伏せない。

    `_emit_say` は履歴保存に失敗しても gateway (Discord 等) と Unity へは送る
    ため、戻り値で分かるのは「この場の記録に残ったか」だけ。実装で最も起き
    やすい失敗形は 3 番目 — dict は返るが message_id が無い。truthy かどうかで
    判定すると、この形が丸ごと成功に化ける。
    """
    from builtin_data.tools.tell import tell

    manager, runtime, client = _make_env(["まはー、聞いて。"])
    runtime.emit_result = emit_result
    with _ctx(manager), _patched_resolve(runtime), _conversation_state(False):
        result = tell(target="user")

    assert "履歴に残せませんでした" in result
    # 届いたとも届いていないとも断定しない (外への配送は別経路で、宛先が
    # 繋がっていない構成では no-op になる)
    assert "確認できません" in result
    assert "慎重に決めてください" in result
    # 記憶には残す — 自分が言ったことを知らないと同じ話を二度する
    assert len(runtime.stored) == 1


def test_building_history_save_failure_returns_dict_without_message_id():
    """上のテストが前提にしている HistoryManager の契約を固定する。

    DB セッションが無い (= insert できない) とき ``add_to_building_only`` は
    ``for_insert`` をそのまま返す。返り値の truthy 性は保存の証拠にならず、
    message_id (DB 採番) の有無だけが証拠になる。
    """
    from persona.history_manager import HistoryManager

    hm = HistoryManager(
        persona_id=PERSONA_ID,
        persona_log_path=Path("/tmp/p1/log.json"),
        building_memory_paths={},
        db_session_factory=None,
    )
    saved = hm.add_to_building_only(BUILDING, {"role": "assistant", "content": "hi"})

    assert saved  # truthy — だが保存されていない
    assert "message_id" not in saved


def test_tell_reports_partial_when_memory_store_fails():
    """声は届いたが記憶に残らなかった場合、届いた事実も残らなかった事実も伝える。

    ``_store_memory`` の既定 bool は例外が無ければ True になり、SAIMemory
    adapter の静かな挿入失敗を拾えない。tell は挿入 id を要求する。
    """
    from builtin_data.tools.tell import tell

    manager, runtime, client = _make_env(["まはー、聞いて。"])
    runtime.store_result_id = ""  # adapter が None を返した (静かな挿入失敗)
    with _ctx(manager), _patched_resolve(runtime), _conversation_state(False):
        result = tell(target="user")

    assert "声は届きました" in result
    assert "記憶には残せませんでした" in result
    assert len(runtime.emitted) == 1
    # id を要求していること自体が契約 (bool 戻り値では失敗を検出できない)
    assert runtime.stored[0]["return_message_id"] is True


def test_tell_after_delivery_failure_does_not_claim_nothing_happened():
    """投函後に転んだら「届いた + 記録で失敗」と返す (逆向きの嘘を防ぐ)。

    声は取り消せないので、「声をかけられませんでした」と返すと、届いた話を
    ペルソナがもう一度しに行く。
    """
    from builtin_data.tools.tell import tell

    manager, runtime, client = _make_env(["まはー、聞いて。"])

    def _boom(persona, text, **kwargs):
        runtime.stored.append({"text": text, **kwargs})
        raise RuntimeError("SAIMemory exploded after the voice went out")

    runtime._store_memory = _boom
    with _ctx(manager), _patched_resolve(runtime), _conversation_state(False):
        result = tell(target="user")

    assert "声を出したあと" in result
    assert "声をかけられませんでした" not in result
    assert len(runtime.emitted) == 1
    # 何を言ったかの監査記録は記憶の保存より先に積む (例外でも欠けない)
    spoken = [e for e in runtime._pulse_contexts.values() for e in e.logs
              if e.node_id == "tell_speech"]
    assert spoken and spoken[0].content == "まはー、聞いて。"


def test_tell_does_not_retake_the_beat_lock_from_another_thread():
    """親 Beat を保持したまま別スレッドから唱えても固まらない。

    スペルは必ず親 Beat (会話 Pulse / セッション) の内側で唱えられ、しかも
    同期ツールは executor スレッドで実行される (sea/runtime_llm.py の
    ``run_in_executor(None, _run)`` / work_session._run_coro_sync)。RLock の
    再入は取得したスレッドでしか効かないため、tell が自分で Beat ロックを
    取り直すと「親スレッドは結果待ち・ツールスレッドはロック待ち」で永久に
    固まる (Codex レビュー 2026-08-08 critical の回帰テスト)。
    """
    from sea.beat_gate import BeatGate

    manager, runtime, client = _make_env(["まはー、ちょっといい？"])
    manager.beat_gate = BeatGate(manager)
    box: Dict[str, Any] = {}

    def _worker():
        # 実運用のスペル実行と同じ形 — 別スレッドで persona_context を張り直す。
        from builtin_data.tools.tell import tell

        with persona_context(PERSONA_ID, "/tmp/p1", manager=manager):
            box["result"] = tell(target="user")

    with _patched_resolve(runtime), _conversation_state(False):
        with manager.beat_gate.hold(PERSONA_ID, purpose="parent_beat"):
            worker = threading.Thread(target=_worker, daemon=True)
            worker.start()
            worker.join(timeout=10)
            still_blocked = worker.is_alive()

    assert not still_blocked, "tell が親 Beat のロック待ちで固まった (デッドロック回帰)"
    assert "声をかけました" in box["result"]
    assert len(runtime.emitted) == 1
