"""tell スペル (builtin_data/tools/tell.py) のテスト。

autonomous_behavior_v3.md §9-4 決着 (引数式) と autonomous_pulse_vehicle.md §B の契約:

- 引数式: ``tell(target, message)`` — 唱えた言葉がそのまま届く。別建ての LLM
  呼び出しは無い (このテストの fake runtime にはそもそも LLM の口が無い)
- 投函は唱えた Pulse の内側の仕事: 唱えた Pulse の pulse_id で ``_emit_say``
  (Building 履歴 + UI + TTS) し、metadata に宛先 (tell_target) が残る。
  本人の記憶 (SAIMemory) には別の行を書かない (fake runtime に ``_store_memory``
  の口が無い = 呼べば AttributeError で落ちる)
- 空の message は拒否、軽量文脈 (分身モード) からは投函しない
- 宛先の検証 (user / all / 同室ペルソナのみ。不在の相手には理由文を返す)
- 会話中のユーザーへの tell は no-op + 教育文 (返答との二重発話の防止)。
  会話中か確認できないときも見送る (fail-closed)
- 履歴の保存失敗・投函後の例外は正直に返す
- Beat ロックは取り直さない (親 Beat の内側で走る) — 別スレッドから取ると
  永久ブロックする
- 会話・タイムアウトには一切触らない
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
PULSE_ID = "pulse-parent"


class FakeRuntime:
    """tell が触ってよい runtime の口だけを持つ fake。

    ``_store_memory`` / LLM client / ``_prepare_context`` は持たせない — tell が
    呼べば AttributeError で落ち、二重の記憶書き込みや別建ての生成の再発を
    テストが拾う。
    """

    def __init__(self):
        self.emitted: List[Dict[str, Any]] = []
        # 実装と同じ戻り値の型: emit_say は building message dict。**保存できた
        # 印は message_id** (DB 採番)。DB insert が失敗しても HistoryManager は
        # 渡した dict をそのまま返すため、message_id の無い truthy な dict が返る。
        self.emit_result: Any = {"role": "assistant", "message_id": "b1:1"}

    def _emit_say(self, persona, building_id, text, pulse_id=None, metadata=None,
                  event_callback=None, occupants_snapshot=None):
        self.emitted.append({
            "building_id": building_id, "text": text, "metadata": metadata,
            "pulse_id": pulse_id, "event_callback": event_callback,
        })
        return self.emit_result


def _make_env(occupants: List[str] | None = None):
    runtime = FakeRuntime()
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
    return manager, runtime


def _pulse(aspect: Aspect | None = Aspect.AUTONOMOUS) -> PulseContext:
    """唱えた側の Pulse。aspect=None は legacy フレーム。"""
    pulse_ctx = PulseContext(pulse_id=PULSE_ID)
    pulse_ctx.push_line(aspect=aspect)
    return pulse_ctx


@contextmanager
def _ctx(manager, pulse_ctx: Any = "default", event_callback=None):
    if pulse_ctx == "default":
        pulse_ctx = _pulse()
    with persona_context(
        PERSONA_ID, "/tmp/p1", manager=manager,
        pulse_context=pulse_ctx, event_callback=event_callback,
    ):
        yield


def _conversation_state(state):
    """会話中かの三値 (True / False / None=不明) を差し替える。

    fake manager では会話状態の読み取りが成立しないので、tell の分岐を
    見るテストはここで明示的に状態を与える。
    """
    return patch("saiverse.day_plan.get_user_conversation_state", return_value=state)


def test_tell_user_delivers_the_written_words_in_the_casting_pulse():
    from builtin_data.tools.tell import tell

    manager, runtime = _make_env()
    pulse_ctx = _pulse(Aspect.AUTONOMOUS)
    with _ctx(manager, pulse_ctx), _conversation_state(False):
        result = tell(target="user", message="  まはー、面白い記事を見つけたよ。 ")

    assert "ユーザー" in result and "声をかけました" in result
    # 投函: Building 履歴へ 1 通。書いた言葉がそのまま (前後の空白だけ落として) 届く
    assert len(runtime.emitted) == 1
    sent = runtime.emitted[0]
    assert sent["building_id"] == BUILDING
    assert sent["text"] == "まはー、面白い記事を見つけたよ。"
    # 宛先が metadata に残る (gist は廃止)
    assert sent["metadata"] == {"tell_target": "user"}
    # 唱えた Pulse の pulse_id で投函する (自前の Pulse を作らない)
    assert sent["pulse_id"] == PULSE_ID
    # 唱えた Pulse の監査記録に何を言ったかが積まれる
    spoken = [e for e in pulse_ctx.logs if e.node_id == "tell_speech"]
    assert [e.content for e in spoken] == ["まはー、面白い記事を見つけたよ。"]
    # ラインを積み増していない (唱えた側のラインのまま)
    assert pulse_ctx.current_line().aspect is Aspect.AUTONOMOUS


def test_tell_from_conversation_line_is_allowed():
    """会話 (メインモード) の別の相手への一言も標準文脈なので通る。"""
    from builtin_data.tools.tell import tell

    manager, runtime = _make_env()
    with _ctx(manager, _pulse(Aspect.CONVERSATION)):
        result = tell(target="ベル", message="ベル、ちょっといい？")

    assert "声をかけました" in result
    assert runtime.emitted[0]["metadata"]["tell_target"] == "p2"


def test_tell_forwards_the_event_callback_for_the_persistence_signal():
    """tell も assistant 発言を建物へ保存する経路の一つ (Codex #1)。

    親 Beat の persona_context が contextvar で運ぶ event_callback を
    ``_emit_say`` へ渡す — 保存が成功した回に保存完了イベント
    (``speak_persisted``) を流すのは emit_say 内の共通の口で、ここで渡し
    忘れるとこの経路だけ信号が欠ける。
    """
    from builtin_data.tools.tell import tell

    manager, runtime = _make_env()
    events: List[Dict[str, Any]] = []
    cb = events.append
    with _ctx(manager, event_callback=cb), _conversation_state(False):
        tell(target="user", message="まはー、聞こえる？")

    assert runtime.emitted[0]["event_callback"] is cb


@pytest.mark.parametrize("message", ["", "   ", "\n\t "])
def test_tell_rejects_an_empty_message(message):
    from builtin_data.tools.tell import tell

    manager, runtime = _make_env()
    with _ctx(manager), _conversation_state(False):
        result = tell(target="user", message=message)

    assert "伝える言葉が空です" in result
    assert runtime.emitted == []


def test_tell_is_refused_from_a_lightweight_line():
    """分身モード (WORKER = 軽量 tier) からは投函しない (標準文脈限定)。

    引数式では唱えた言葉がそのまま本人の声として届くので、軽量モデルが本人の
    声でユーザーに喋る事故を構造で塞ぐ。
    """
    from builtin_data.tools.tell import tell

    manager, runtime = _make_env()
    pulse_ctx = _pulse(Aspect.CONVERSATION)
    pulse_ctx.push_line(aspect=Aspect.WORKER)  # run_playbook のサブライン
    with _ctx(manager, pulse_ctx), _conversation_state(False):
        result = tell(target="user", message="まはー、聞いて。")

    assert "分身モード" in result
    assert "本体の時間に唱えてください" in result
    assert runtime.emitted == []
    assert not [e for e in pulse_ctx.logs if e.node_id == "tell_speech"]


@pytest.mark.parametrize("pulse_ctx", [None, "legacy"])
def test_tell_without_aspect_is_treated_as_standard(pulse_ctx):
    """aspect の無い経路 (Pulse 外 / legacy フレーム) は標準扱い。

    ``tier_without_aspect`` が state の無いときに標準を返すのと同じ向き。
    """
    from builtin_data.tools.tell import tell

    manager, runtime = _make_env()
    ctx = _pulse(None) if pulse_ctx == "legacy" else None
    with _ctx(manager, ctx), _conversation_state(False):
        result = tell(target="user", message="まはー、聞いて。")

    assert "声をかけました" in result
    assert len(runtime.emitted) == 1
    assert runtime.emitted[0]["pulse_id"] == (PULSE_ID if ctx is not None else None)


def test_tell_unknown_target_returns_reason_without_emitting():
    from builtin_data.tools.tell import tell

    manager, runtime = _make_env()
    with _ctx(manager):
        result = tell(target="どこかの誰か", message="こんにちは")

    assert "この場所にいません" in result
    assert "ベル" in result  # 声をかけられる相手の提示
    assert runtime.emitted == []


def test_tell_user_during_conversation_is_noop_with_guidance():
    from builtin_data.tools.tell import tell

    manager, runtime = _make_env()
    with _ctx(manager), _conversation_state(True):
        result = tell(target="user", message="まはー、聞いて。")

    assert "会話の最中" in result
    assert "返答" in result
    assert runtime.emitted == []


def test_tell_user_is_withheld_when_conversation_state_is_unknown():
    """会話中か読めないときは発声しない (fail-closed)。

    二重発話は届いた後では取り消せない。見送りは次の機会に唱え直せる。
    """
    from builtin_data.tools.tell import tell

    manager, runtime = _make_env()
    with _ctx(manager), _conversation_state(None):
        result = tell(target="user", message="まはー、聞いて。")

    assert "確認できませんでした" in result
    assert "見送" in result
    assert runtime.emitted == []


def test_tell_all_during_conversation_is_allowed():
    """会話中でも user 以外 (all / 同席ペルソナ) への一言は塞がない。"""
    from builtin_data.tools.tell import tell

    manager, runtime = _make_env()
    with _ctx(manager), _conversation_state(True):
        result = tell(target="all", message="みんな、聞いて。")

    assert "声をかけました" in result
    assert runtime.emitted[0]["metadata"]["tell_target"] == "all"


@pytest.mark.parametrize("emit_result", [
    None,                                        # 保存前に例外
    {},                                          # 採番された行が返らなかった場合
    {"role": "assistant", "content": "まはー、聞いて。"},  # DB insert 失敗 = 渡した dict がそのまま返る
])
def test_tell_reports_history_failure_without_claiming_silence(emit_result):
    """履歴に残らなくても「言ってしまった」— 出た事実を伏せない。

    `_emit_say` は履歴保存に失敗しても gateway (Discord 等) へは送る
    ため、戻り値で分かるのは「この場の記録に残ったか」だけ。実装で最も起き
    やすい失敗形は 3 番目 — dict は返るが message_id が無い。truthy かどうかで
    判定すると、この形が丸ごと成功に化ける。
    """
    from builtin_data.tools.tell import tell

    manager, runtime = _make_env()
    runtime.emit_result = emit_result
    with _ctx(manager), _conversation_state(False):
        result = tell(target="user", message="まはー、聞いて。")

    assert "履歴に残せませんでした" in result
    # 届いたとも届いていないとも断定しない (外への配送は別経路で、宛先が
    # 繋がっていない構成では no-op になる)
    assert "確認できません" in result
    assert "慎重に決めてください" in result


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


def test_tell_after_delivery_failure_does_not_claim_nothing_happened():
    """投函後に転んだら「届いた + 記録で失敗」と返す (逆向きの嘘を防ぐ)。

    声は取り消せないので、「声をかけられませんでした」と返すと、届いた話を
    ペルソナがもう一度しに行く。
    """
    from builtin_data.tools.tell import tell

    manager, runtime = _make_env()

    class _ExplodingPulse(PulseContext):
        def append(self, entry):
            raise RuntimeError("pulse log exploded after the voice went out")

    pulse_ctx = _ExplodingPulse(pulse_id=PULSE_ID)
    pulse_ctx.push_line(aspect=Aspect.AUTONOMOUS)
    with _ctx(manager, pulse_ctx), _conversation_state(False):
        result = tell(target="user", message="まはー、聞いて。")

    assert "声を出したあと" in result
    assert "声をかけられませんでした" not in result
    assert len(runtime.emitted) == 1


def test_tell_does_not_retake_the_beat_lock_from_another_thread():
    """親 Beat を保持したまま別スレッドから唱えても固まらない。

    スペルは必ず親 Beat (会話 Pulse / ティック) の内側で唱えられ、しかも
    同期ツールは executor スレッドで実行される (sea/runtime_llm.py の
    ``run_in_executor(None, _run)``)。RLock の再入は取得したスレッドでしか
    効かないため、tell が自分で Beat ロックを取り直すと「親スレッドは結果待ち・
    ツールスレッドはロック待ち」で永久に固まる (Codex レビュー 2026-08-08
    critical の回帰テスト)。
    """
    from sea.beat_gate import BeatGate

    manager, runtime = _make_env()
    manager.beat_gate = BeatGate(manager)
    box: Dict[str, Any] = {}

    def _worker():
        # 実運用のスペル実行と同じ形 — 別スレッドで persona_context を張り直す。
        from builtin_data.tools.tell import tell

        with _ctx(manager):
            box["result"] = tell(target="user", message="まはー、ちょっといい？")

    with _conversation_state(False):
        with manager.beat_gate.hold(PERSONA_ID, purpose="parent_beat"):
            worker = threading.Thread(target=_worker, daemon=True)
            worker.start()
            worker.join(timeout=10)
            still_blocked = worker.is_alive()

    assert not still_blocked, "tell が親 Beat のロック待ちで固まった (デッドロック回帰)"
    assert "声をかけました" in box["result"]
    assert len(runtime.emitted) == 1


def test_tell_schema_takes_the_words_as_an_argument():
    """ペルソナに見える形: target と message の二つ。gist は廃止。"""
    from builtin_data.tools.tell import schema

    s = schema()
    props = s.parameters["properties"]
    assert set(props) == {"target", "message"}
    assert set(s.parameters["required"]) == {"target", "message"}
    assert "as-is" in s.description
