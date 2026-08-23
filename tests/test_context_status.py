"""context-status endpoint (提示コンテキストの現在量と水位、read-only) のテスト。

docs/issues/chat_options_metabolism_section_redesign.md — チャットオプションの
状態表示が「次に話しかけた時に実際に送られる量」(§15 読み戻し込み) を返すこと。
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api.routes.people.context_status import get_context_status
from sea.eviction_plan import Watermarks

PERSONA_ID = "tester"


def _msg(mid, chars):
    return {"id": mid, "content": "x" * chars}


def _manager(persona=None, lifecycle=None, details=None, default_model=None):
    return SimpleNamespace(
        get_ai_details=lambda pid: details if details is not None else (
            {"DEFAULT_MODEL": default_model} if pid == PERSONA_ID else None
        ),
        personas={PERSONA_ID: persona} if persona is not None else {},
        sea_runtime=SimpleNamespace(session_lifecycle=lifecycle),
    )


def _lifecycle(
    *,
    watermarks=Watermarks(low=1000, target=2000, high=4000),
    refill_plan=None,
    anchor_id="m1",
    presented=None,
):
    return SimpleNamespace(
        get_metabolism_watermarks=lambda persona, model_key: watermarks,
        preview_refilled_history=lambda persona, model_key, **_kw: refill_plan,
        resolve_metabolism_anchor=(
            lambda persona, model_key=None, persist_advance=True: (anchor_id, "own")
        ),
        get_presented_window=lambda persona, model_key, aid: SimpleNamespace(
            presented=list(presented or []),
        ),
    )


def test_unknown_persona_404():
    manager = _manager(details=None)
    manager.get_ai_details = lambda pid: None
    with pytest.raises(HTTPException) as exc:
        get_context_status("nobody", manager)
    assert exc.value.status_code == 404


def test_no_model_returns_bare_status():
    manager = _manager(default_model=None)
    status = get_context_status(PERSONA_ID, manager)
    assert status["model"] is None
    assert status["metabolism"] is False
    assert status["presented_chars"] is None


def test_model_without_watermarks_is_not_metabolic():
    """model 定義が水位を持たない (null) = Metabolism を持たない、を表示に写す。"""
    persona = SimpleNamespace(persona_id=PERSONA_ID, model="model-a")
    lc = _lifecycle(watermarks=None)
    status = get_context_status(PERSONA_ID, _manager(persona=persona, lifecycle=lc))
    assert status["model"] == "model-a"
    assert status["metabolism"] is False
    assert status["high_chars"] is None


def test_refill_plan_measures_post_refill_presented():
    """§15 読み戻しが適用されるなら、その適用後の文字数 (プレビューと一致) を返す。"""
    persona = SimpleNamespace(persona_id=PERSONA_ID, model="model-a")
    plan = {"presented": [_msg("m1", 700), _msg("m2", 800)]}
    lc = _lifecycle(refill_plan=plan)
    status = get_context_status(PERSONA_ID, _manager(persona=persona, lifecycle=lc))
    assert status["metabolism"] is True
    assert status["presented_chars"] == 1500
    assert status["refill_applied"] is True
    assert status["target_chars"] == 2000
    assert status["high_chars"] == 4000


def test_no_refill_measures_plain_window():
    persona = SimpleNamespace(persona_id=PERSONA_ID, model="model-a")
    lc = _lifecycle(presented=[_msg("m1", 2500)])
    status = get_context_status(PERSONA_ID, _manager(persona=persona, lifecycle=lc))
    assert status["presented_chars"] == 2500
    assert status["refill_applied"] is False


def test_bootstrap_without_anchor_returns_watermarks_only():
    persona = SimpleNamespace(persona_id=PERSONA_ID, model="model-a")
    lc = _lifecycle(anchor_id=None)
    status = get_context_status(PERSONA_ID, _manager(persona=persona, lifecycle=lc))
    assert status["metabolism"] is True
    assert status["presented_chars"] is None


def test_unloaded_persona_returns_watermarks_only():
    """persona 未ロード (会話中でない) は水位だけ — DEFAULT_MODEL で解決する。"""
    lc = _lifecycle()
    status = get_context_status(
        PERSONA_ID, _manager(lifecycle=lc, default_model="model-b"),
    )
    assert status["model"] == "model-b"
    assert status["metabolism"] is True
    assert status["presented_chars"] is None


def test_measurement_failure_keeps_watermarks_and_is_flagged():
    """計測失敗は水位表示を妨げないが、「起点なし」と区別できる印を立てる。"""
    persona = SimpleNamespace(persona_id=PERSONA_ID, model="model-a")
    lc = _lifecycle()

    def _boom(persona, model_key, **_kw):
        raise RuntimeError("db down")

    lc.preview_refilled_history = _boom
    status = get_context_status(PERSONA_ID, _manager(persona=persona, lifecycle=lc))
    assert status["metabolism"] is True
    assert status["presented_chars"] is None
    assert status["measurement_failed"] is True


def test_bootstrap_is_not_flagged_as_failure():
    persona = SimpleNamespace(persona_id=PERSONA_ID, model="model-a")
    lc = _lifecycle(anchor_id=None)
    status = get_context_status(PERSONA_ID, _manager(persona=persona, lifecycle=lc))
    assert status["measurement_failed"] is False


def test_fold_unit_chars_is_reported_for_the_fold_decision():
    """「畳めるか」の判定に要る U (一次あらすじの標準被覆) を必ず返す。

    整理は残す量より古い側を U ずつ刻んで畳むので、「残す量を超えている」だけ
    では畳めない。UI が実際の条件で実行可否を判定できるよう、水位を持たない
    モデルでも同じ欄を返す (欄の有無で分岐させない)。
    """
    from sai_memory.arasuji.alignment import chronicle_band_budget

    unit = chronicle_band_budget()
    assert unit > 0

    persona = SimpleNamespace(persona_id=PERSONA_ID, model="model-a")
    lc = _lifecycle(presented=[_msg("m1", 2500)])
    status = get_context_status(PERSONA_ID, _manager(persona=persona, lifecycle=lc))
    assert status["fold_unit_chars"] == unit

    no_watermarks = get_context_status(
        PERSONA_ID, _manager(persona=persona, lifecycle=_lifecycle(watermarks=None)),
    )
    assert no_watermarks["metabolism"] is False
    assert no_watermarks["fold_unit_chars"] == unit


def test_watermark_resolution_failure_is_500():
    """水位解決の失敗を「水位を持たないモデル」(正常) に偽装しない。"""
    persona = SimpleNamespace(persona_id=PERSONA_ID, model="model-a")
    lc = _lifecycle()

    def _boom(persona, model_key):
        raise RuntimeError("config broken")

    lc.get_metabolism_watermarks = _boom
    with pytest.raises(HTTPException) as exc:
        get_context_status(PERSONA_ID, _manager(persona=persona, lifecycle=lc))
    assert exc.value.status_code == 500
