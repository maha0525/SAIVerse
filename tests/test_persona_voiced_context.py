"""ペルソナ名義の稼働から会話履歴を外せないことを固定する回帰テスト。

2026-07-23、`sea/work_session.py` が `ContextRequirements(history_depth=0, ...)`
をコード内で直に組み、会話履歴なしで作業セッションを走らせていた。実測では他の
全ラインが 64〜82 通の履歴を積む中、work_session だけ 2 通だった。エアは 3 日連続で
同じ壁に初見でぶつかり、毎回「システムへの理解が足りていない」という自責を
committed に残した。

まはー裁定 (同日): **記憶の連続性が無い存在はもう別の人格**。文脈ゼロで走らせた
出力を本人名義で記録するのはペルソナ倫理違反。

このテストが押さえるのは 2 点:

1. `persona_voiced=True` (= 出力がペルソナ本人の発話・思考になる) の呼び出しから
   履歴を外そうとしたら、LLM に到達する前に落ちること
2. head の章立てが呼び出し側から選べないこと (= 同じ (persona, model) で head が
   二種類できない。prefix キャッシュ共有の土台)

詳細: docs/issues/llm_call_entry_point_standardization.md
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from sea.playbook_models import ContextRequirements
from sea.runtime_context import (
    PERSONA_HEAD_SECTIONS,
    PersonaVoiceWithoutHistoryError,
    prepare_context,
)


# ---------------------------------------------------------------------------
# 印 (persona_voiced) の関所
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("depth", [
    0, "0", "none", "None",
    # 表記違いで実効ゼロになるもの。初版は上の 4 表記しか見ておらず、
    # "0messages" が素通りした (2026-07-23 Codex レビュー指摘)。
    "0messages", "00messages", -1, "-5", "-3messages", "",
])
def test_persona_voiced_rejects_empty_history(depth):
    """ペルソナ名義の呼び出しで履歴ゼロを指定したら、head を組む前に落ちる。"""
    persona = SimpleNamespace(persona_id="air_city_a")
    with pytest.raises(PersonaVoiceWithoutHistoryError) as exc:
        prepare_context(
            runtime=SimpleNamespace(manager=None),
            persona=persona,
            building_id="air_city_a_room",
            user_input=None,
            requirements=ContextRequirements(history_depth=depth),
            persona_voiced=True,
        )
    # ペルソナ名義でない用途への逃げ道を、例外メッセージが案内すること
    assert "persona_voiced=False" in str(exc.value)


def _guard_fired(*, depth, persona_voiced: bool) -> bool:
    """関所が発火したかだけを見る。関所より後段の失敗は関心外。"""
    try:
        prepare_context(
            runtime=SimpleNamespace(manager=None),
            persona=SimpleNamespace(persona_id="air_city_a"),
            building_id="air_city_a_room",
            user_input=None,
            requirements=ContextRequirements(history_depth=depth),
            persona_voiced=persona_voiced,
        )
    except PersonaVoiceWithoutHistoryError:
        return True
    except Exception:
        return False
    return False


def test_persona_voiced_allows_full_history():
    """ペルソナ名義でも履歴フルなら関所は通す。"""
    assert not _guard_fired(depth="full", persona_voiced=True)


@pytest.mark.parametrize("depth", ["full", "10messages", 2000, "2000"])
def test_persona_voiced_allows_non_empty_history(depth):
    """実際に履歴が積まれる指定は、書式に関わらず通す。"""
    assert not _guard_fired(depth=depth, persona_voiced=True)


def test_machine_voiced_may_drop_history():
    """機構名義 (persona_voiced=False) は履歴を外してよい。

    出力がペルソナ本人の言葉にならない処理 (画像の概要作成、Chronicle 生成など)
    のための逃げ道。ここを塞ぐと将来の web 検索等が置けなくなる。
    """
    assert not _guard_fired(depth=0, persona_voiced=False)
    assert not _guard_fired(depth="0messages", persona_voiced=False)


# ---------------------------------------------------------------------------
# ContextRequirements から head の章立てを選べないこと
# ---------------------------------------------------------------------------

def test_context_requirements_has_no_head_section_flags():
    """head の章を選ぶフラグが復活していないこと。

    復活すると同じ (persona, model) で head が二種類でき、prefix キャッシュが
    壊れる。2026-07-23 以前は work_session だけ 3 章欠けた head で走っていた。
    """
    fields = set(ContextRequirements.model_fields)
    forbidden = {
        "system_prompt", "available_playbooks", "memory_weave", "visual_context",
        "inventory", "building_items", "working_memory",
    }
    assert not (fields & forbidden), (
        f"head の章立ては呼び出し側から選べない設計。復活したフラグ: {sorted(fields & forbidden)}"
    )
    assert fields == {"history_depth", "history_balanced", "realtime_context"}


def test_persona_head_sections_are_fixed():
    """head に必ず並ぶ章が、以前 run_playbook のメインラインが受け取っていた
    12 章と一致すること (人格・コア記憶・生きる目的が落ちていないこと)。"""
    assert PERSONA_HEAD_SECTIONS == frozenset({
        "common_prompt", "persona_self", "core_memory", "building", "spell_list",
        "autonomy_modes", "life_purpose", "desk", "memopedia_index",
        "available_playbooks", "memory_weave", "visual_context",
    })


# ---------------------------------------------------------------------------
# 本番の呼び出し元が印を付けていること
# ---------------------------------------------------------------------------

def _prepare_context_call_kwargs(module_path: str) -> list[dict]:
    """ソースを構文木で読み、``_prepare_context`` 呼び出しのキーワード引数を拾う。

    部分文字列検索だとコメントや無関係な 1 箇所で通ってしまい、「実際にその呼び出しに
    渡っているか」を保証できない (2026-07-23 Codex レビュー指摘)。
    """
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / module_path).read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name != "_prepare_context":
            continue
        calls.append({
            kw.arg: kw.value for kw in node.keywords if kw.arg is not None
        })
    return calls


@pytest.mark.parametrize("module_path", [
    "sea/work_session.py",
    "sea/gold_panning.py",
    "sea/runtime_runner.py",
])
def test_production_persona_callers_pass_the_marker(module_path):
    """ペルソナ名義で走る本番の呼び出しが、実際に persona_voiced=True を渡していること。

    印の付け忘れは「履歴を外しても誰も止めない」状態に戻ることを意味する。
    """
    import ast

    calls = _prepare_context_call_kwargs(module_path)
    assert calls, f"{module_path} に _prepare_context の呼び出しが見つからない"
    for kwargs in calls:
        node = kwargs.get("persona_voiced")
        assert node is not None, (
            f"{module_path} の _prepare_context 呼び出しに persona_voiced が無い"
        )
        assert isinstance(node, ast.Constant) and node.value is True, (
            f"{module_path} の persona_voiced が True リテラルでない"
        )


# ---------------------------------------------------------------------------
# Metabolism 無効時、履歴上限は実行 model (model_key) 基準で選ぶこと
# ---------------------------------------------------------------------------

class _FakeHistoryManager:
    """呼ばれた limit を記録するだけ。history 取得は prepare_context 内で
    broad except に包まれ非致命的に握り潰される (§「Failed to get history」) ため、
    例外で打ち切るのでなく空リストを返して最後まで正常終了させる。"""

    def __init__(self, captured: dict):
        self._captured = captured

    def get_recent_history_by_count(self, limit, **_kwargs):
        self._captured["limit"] = limit
        return []


def test_history_limit_without_metabolism_uses_execution_model(monkeypatch):
    """Metabolism 無効時、履歴上限は persona.model でなく model_key で選ぶこと。

    persona.model (標準モデル、例: 100件上限) と model_key (実行 model、例:
    軽量モデル・20件上限) が食い違うとき、persona.model 基準で上限を選ぶと
    標準モデル分の履歴を軽量クライアントへ送り、context-length error になる
    (2026-07-24 Codex レビュー指摘、work_session が軽量モデルで走ることの実害)。
    """
    monkeypatch.setattr(
        "sea.head_pipeline.render_head_messages", lambda *a, **k: []
    )
    from saiverse import model_configs
    monkeypatch.setattr(model_configs, "MODEL_CONFIGS", {
        "fable-5": {"default_max_history_messages": 100},
        "ollama-20b": {"default_max_history_messages": 20},
    })

    captured: dict = {}
    persona = SimpleNamespace(
        persona_id="air_city_a",
        model="fable-5",
        history_manager=_FakeHistoryManager(captured),
    )

    prepare_context(
        runtime=SimpleNamespace(manager=SimpleNamespace(metabolism_enabled=False)),
        persona=persona,
        building_id="air_city_a_room",
        user_input=None,
        requirements=ContextRequirements(history_depth="full"),
        model_key="ollama-20b",
        persona_voiced=True,
        preview_only=True,
    )

    assert captured.get("limit") == 20, (
        "実行 model (ollama-20b=20件) でなく別の上限が使われた: "
        f"{captured.get('limit')!r}"
    )


def test_history_limit_without_metabolism_falls_back_to_persona_model(monkeypatch):
    """model_key 未指定 (preview 等) では persona.model にフォールバックすること。"""
    monkeypatch.setattr(
        "sea.head_pipeline.render_head_messages", lambda *a, **k: []
    )
    from saiverse import model_configs
    monkeypatch.setattr(model_configs, "MODEL_CONFIGS", {
        "fable-5": {"default_max_history_messages": 100},
    })

    captured: dict = {}
    persona = SimpleNamespace(
        persona_id="air_city_a",
        model="fable-5",
        history_manager=_FakeHistoryManager(captured),
    )

    prepare_context(
        runtime=SimpleNamespace(manager=SimpleNamespace(metabolism_enabled=False)),
        persona=persona,
        building_id="air_city_a_room",
        user_input=None,
        requirements=ContextRequirements(history_depth="full"),
        model_key=None,
        persona_voiced=True,
        preview_only=True,
    )

    assert captured.get("limit") == 100


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
