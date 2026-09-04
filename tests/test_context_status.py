"""context-status endpoint (提示コンテキストの現在量と水位、read-only) のテスト。

docs/issues/chat_options_metabolism_section_redesign.md — チャットオプションの
状態表示が「次に話しかけた時に実際に送られる量」(§15 読み戻し込み) を返すこと。
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api.routes.people.context_status import get_context_status
from sea.eviction_plan import CONSUMED_PERCEPTION_KEY, Watermarks

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


def _perception(chars, at=0):
    """送信直前に差し込まれる知覚ブロック (保存行ではないので id を持たない)。"""
    return {
        "role": "user",
        "content": "p" * chars,
        "created_at": at,
        "metadata": {
            "tags": ["internal", "event_message", "perception"],
            CONSUMED_PERCEPTION_KEY: True,
        },
    }


def _lifecycle(
    *,
    watermarks=Watermarks(target=2000, high=4000),
    refill_plan=None,
    anchor_id="m1",
    presented=None,
    planning_presented=None,
    refold_ranges=0,
    perceptions=None,
):
    """context-status が触る SessionLifecycle の顔だけの偽物。

    ``planning_presented`` は preview_planning_window (本走行と同じ窓の正規化を
    書き込みなしで再現する下見) が返す提示 — 省略時は素の窓をそのまま返す
    (正規化で何も変わらない窓)。``refold_ranges`` は §15-3 印戻しで digest 表示
    へ戻る区間数。``perceptions`` は送信直前に差し込まれる知覚ブロックの列
    (本物の SessionLifecycle は runtime_context の組成関数から得る)。
    """
    def _preview_planning_window(persona, model_key, window, wm, **_kw):
        if planning_presented is None:
            return window, refold_ranges
        return SimpleNamespace(presented=list(planning_presented)), refold_ranges

    def _presented_with_perceptions(persona, rows, aid=None, **_kw):
        return list(rows) + list(perceptions or [])

    return SimpleNamespace(
        get_metabolism_watermarks=(
            lambda persona, model_key, persona_id=None: watermarks
        ),
        preview_refilled_history=lambda persona, model_key, **_kw: refill_plan,
        resolve_metabolism_anchor=(
            lambda persona, model_key=None, persist_advance=True: (anchor_id, "own")
        ),
        get_presented_window=lambda persona, model_key, aid: SimpleNamespace(
            presented=list(presented or []),
        ),
        preview_planning_window=_preview_planning_window,
        presented_with_perceptions=_presented_with_perceptions,
        window_floor_applied_at=lambda persona_id, model_key: None,
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
    # 読み戻し経路でも「畳めるか」は測る (残す量以下なので畳めない)。
    assert status["fold_ready"] is False
    assert status["fold_shortfall_chars"] == status["fold_unit_chars"]


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
    # 提示を測れないときは「畳めるか」も測れない — None のまま。
    assert status["fold_ready"] is None
    assert status["fold_shortfall_chars"] is None


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


def test_fold_ready_comes_from_the_real_eviction_plan(monkeypatch):
    """「いま畳めるか」は生の超過と U の比較ではなく、実行時と同じ退場計画
    (plan_eviction の dry 呼び) から出す (2026-08-29 裁定: U 判定は材料字数)。

    材料が U に達する並びでは fold_ready=True / shortfall=0。
    """
    monkeypatch.setenv("SAIVERSE_CHRONICLE_BAND_BUDGET", "4000")
    persona = SimpleNamespace(persona_id=PERSONA_ID, model="model-a")
    # 6 × 1,000 字: 残す量 2,000 → 保護は末尾 2 件、候補の材料 4,000 = U で閉じる。
    lc = _lifecycle(presented=[_msg(f"m{i}", 1000) for i in range(6)])
    status = get_context_status(PERSONA_ID, _manager(persona=persona, lifecycle=lc))
    assert status["fold_unit_chars"] == 4000
    assert status["fold_ready"] is True
    assert status["fold_shortfall_chars"] == 0


def test_fold_ready_false_when_material_is_thin(monkeypatch):
    """生の超過が U を超えていても、材料 (機構行を圧縮した後の字数) が U 未満なら
    fold_ready=False と「あと材料何字」を返す — 8/24 の「押せたのに何も起きない」
    を材料判定の下で再発させないための欄。生比較の旧判定ではこの並びを
    「畳める」と誤答する。
    """
    from sai_memory.arasuji.generator import material_len

    monkeypatch.setenv("SAIVERSE_CHRONICLE_BAND_BUDGET", "4000")
    persona = SimpleNamespace(persona_id=PERSONA_ID, model="model-a")
    spell = {
        "id": "s0",
        "content": "x" * 20_000,
        "metadata": {"tags": ["spell"]},
    }
    presented = [spell, _msg("m0", 1000), _msg("m1", 1000),
                 _msg("k0", 1000), _msg("k1", 1000)]
    lc = _lifecycle(presented=presented)
    status = get_context_status(PERSONA_ID, _manager(persona=persona, lifecycle=lc))
    # 生では残す量 2,000 を 22,000 字超えているが、候補の材料は一行 + 2,000 字。
    assert status["presented_chars"] == 24_000
    assert status["fold_ready"] is False
    expected_material = material_len("x" * 20_000, ("spell",)) + 2_000
    assert status["fold_shortfall_chars"] == 4000 - expected_material


def test_fold_ready_is_measured_on_the_normalized_planning_window(monkeypatch):
    """下見は本走行と同じ窓の正規化 (preview_planning_window — 恒久欠落 fold の
    除外 → §15-3 印戻し) を通した提示で計画を立てる (Codex 指摘 2026-08-29)。

    素の窓では畳める並びでも、正規化後 (印戻しで残す量以下) なら本走行は
    退場計画を立てない — fold_ready も False でなければ、下見だけが「畳める」
    と言う食い違い (8/24 の「押せたのに何も起きない」の再発口) になる。
    """
    monkeypatch.setenv("SAIVERSE_CHRONICLE_BAND_BUDGET", "4000")
    persona = SimpleNamespace(persona_id=PERSONA_ID, model="model-a")
    # 素の窓は 6,000 字 (畳める並び)、正規化後は 1,000 字 (残す量以下)。
    lc = _lifecycle(
        presented=[_msg(f"m{i}", 1000) for i in range(6)],
        planning_presented=[_msg("m5", 1000)],
    )
    status = get_context_status(PERSONA_ID, _manager(persona=persona, lifecycle=lc))
    # 表示する送信量は素の窓 (いま実際に送られる量) のまま。
    assert status["presented_chars"] == 6000
    # 「畳めるか」は正規化後の窓の答え。
    assert status["fold_ready"] is False


def test_fold_ready_true_when_refold_alone_reduces_the_window(monkeypatch):
    """§15-3 印戻しだけで提示が減る窓 (raw-view fold あり) は、退場計画が空でも
    fold_ready=True — 本走行 (run_manual_compaction) は印戻しを実行して "ok" を
    返すので、押せないと嘘になる。"""
    monkeypatch.setenv("SAIVERSE_CHRONICLE_BAND_BUDGET", "4000")
    persona = SimpleNamespace(persona_id=PERSONA_ID, model="model-a")
    lc = _lifecycle(
        presented=[_msg(f"m{i}", 1000) for i in range(6)],
        planning_presented=[_msg("m5", 1000)],   # 印戻し後は残す量以下
        refold_ranges=2,
    )
    status = get_context_status(PERSONA_ID, _manager(persona=persona, lifecycle=lc))
    assert status["fold_ready"] is True
    assert status["fold_shortfall_chars"] == 0


def test_presented_chars_is_stored_plus_injected_perceptions():
    """表示する送信量は「実際に送る中身」の合計 — 保存行だけではない。

    2026-09-02 まはー裁定 (docs/issues/context_accounting_excludes_injected_rows.md):
    送信直前に差し込まれる知覚 (部屋の様子) は勘定から漏れていて、本番エリスで
    勘定 149,856 字に対し実送信 209,031 字という乖離を出していた。内訳も返す。
    """
    persona = SimpleNamespace(persona_id=PERSONA_ID, model="model-a")
    lc = _lifecycle(
        presented=[_msg("m1", 1000)],
        perceptions=[_perception(4000)],
    )
    status = get_context_status(PERSONA_ID, _manager(persona=persona, lifecycle=lc))
    assert status["stored_chars"] == 1000
    assert status["injected_perception_chars"] == 4000
    assert status["presented_chars"] == 5000


def test_refill_path_also_counts_injected_perceptions():
    """§15 読み戻し経路の表示も同じ物差し (合計) で出す。"""
    persona = SimpleNamespace(persona_id=PERSONA_ID, model="model-a")
    plan = {"presented": [_msg("m1", 700)], "new_anchor_id": "m1"}
    lc = _lifecycle(refill_plan=plan, perceptions=[_perception(300)])
    status = get_context_status(PERSONA_ID, _manager(persona=persona, lifecycle=lc))
    assert status["refill_applied"] is True
    assert status["stored_chars"] == 700
    assert status["injected_perception_chars"] == 300
    assert status["presented_chars"] == 1000


def test_perception_lookup_failure_is_flagged_not_shown_as_zero():
    """知覚一覧の内部失敗を「知覚 0 字 (正常)」として表示しない。

    透明性の画面なので、失敗は measurement_failed に落とす (raise_on_error 経由。
    Codex 指摘 2026-09-02)。門・発火・送信の各経路は fail-open のままで、
    fail-closed にするのはこの表示だけ。
    """
    persona = SimpleNamespace(persona_id=PERSONA_ID, model="model-a")
    lc = _lifecycle(presented=[_msg("m1", 1000)])

    def _boom(persona, rows, aid=None, **_kw):
        raise RuntimeError("perception listing failed")

    lc.presented_with_perceptions = _boom
    status = get_context_status(PERSONA_ID, _manager(persona=persona, lifecycle=lc))
    assert status["measurement_failed"] is True
    assert status["presented_chars"] is None
    assert status["injected_perception_chars"] is None


def test_fold_readiness_protection_ignores_the_perception_weight(monkeypatch):
    """下見の退場計画でも、残す量の保護は**会話の行だけ**で測る (2026-09-03 裁定)。

    末尾に巨大な知覚ブロックが乗っても保護範囲は縮まない — 縮むと本走行が
    会話を畳みすぎる (docs/issues/protection_quota_consumed_by_perception_blocks.md)。
    下見は本走行と同じ純関数を呼ぶので答えも同じ: 候補の材料 1,200 字 < U で
    閉じない。合計はそのまま表示に出る (透明性)。
    """
    monkeypatch.setenv("SAIVERSE_CHRONICLE_BAND_BUDGET", "2000")
    persona = SimpleNamespace(persona_id=PERSONA_ID, model="model-a")
    rows = [_msg(f"m{i}", 600) for i in range(6)]  # 保存行は合計 3,600 字

    without = _lifecycle(presented=rows)
    plain = get_context_status(
        PERSONA_ID, _manager(persona=persona, lifecycle=without),
    )
    assert plain["presented_chars"] == 3600
    assert plain["window_rows_chars"] == 3600
    assert plain["fold_ready"] is False

    lc = _lifecycle(presented=rows, perceptions=[_perception(3000, at=10**10)])
    status = get_context_status(PERSONA_ID, _manager(persona=persona, lifecycle=lc))
    assert status["presented_chars"] == 6600
    assert status["window_rows_chars"] == 3600
    assert status["fold_ready"] is False


def test_perception_over_budget_is_reported_when_nothing_is_evictable():
    """合計が上限を超えているのに会話の行が残す量以下 = 畳めるものが無い。

    この状態は会話ではなく知覚の供給が予算を超えている。整理は空振り
    (LLM なし) で終わるので、画面にその事実を出す欄 ``perception_over_budget``
    と、残す量の契約が見ている量 ``window_rows_chars`` を返す。
    """
    persona = SimpleNamespace(persona_id=PERSONA_ID, model="model-a")
    wm = Watermarks(target=2000, high=4000)

    # 行 1,000 ≤ 残す量、合計 5,000 > 上限 → 知覚が予算超過。
    lc = _lifecycle(watermarks=wm, presented=[_msg("m1", 1000)],
                    perceptions=[_perception(4000)])
    status = get_context_status(PERSONA_ID, _manager(persona=persona, lifecycle=lc))
    assert status["stored_chars"] == 1000
    assert status["window_rows_chars"] == 1000
    assert status["presented_chars"] == 5000
    assert status["perception_over_budget"] is True
    assert status["fold_ready"] is False

    # 行 3,000 > 残す量、合計 6,000 > 上限 → 畳めるものがある = 予算超過の主は会話。
    lc = _lifecycle(watermarks=wm, presented=[_msg(f"m{i}", 1000) for i in range(3)],
                    perceptions=[_perception(3000)])
    status = get_context_status(PERSONA_ID, _manager(persona=persona, lifecycle=lc))
    assert status["window_rows_chars"] == 3000
    assert status["perception_over_budget"] is False

    # 合計が上限以下なら、行が残す量以下でも予算超過ではない。
    lc = _lifecycle(watermarks=wm, presented=[_msg("m1", 1000)],
                    perceptions=[_perception(2000)])
    status = get_context_status(PERSONA_ID, _manager(persona=persona, lifecycle=lc))
    assert status["presented_chars"] == 3000
    assert status["perception_over_budget"] is False

    # 上限なし (文字数では発火しない model) では常に False。
    lc = _lifecycle(watermarks=Watermarks(target=2000, high=None),
                    presented=[_msg("m1", 1000)], perceptions=[_perception(9000)])
    status = get_context_status(PERSONA_ID, _manager(persona=persona, lifecycle=lc))
    assert status["perception_over_budget"] is False


def test_window_rows_chars_on_the_refill_path():
    """§15 読み戻し経路でも、残す量の契約が見る量 (行だけ) を返す。"""
    persona = SimpleNamespace(persona_id=PERSONA_ID, model="model-a")
    plan = {"presented": [_msg("m1", 700)], "new_anchor_id": "m1"}
    lc = _lifecycle(refill_plan=plan, perceptions=[_perception(300)])
    status = get_context_status(PERSONA_ID, _manager(persona=persona, lifecycle=lc))
    assert status["window_rows_chars"] == 700
    assert status["presented_chars"] == 1000
    assert status["perception_over_budget"] is False


def test_unmeasured_status_leaves_the_new_fields_null():
    """測れないとき (起点なし) は新しい欄も None のまま — 0 や False で偽らない。"""
    persona = SimpleNamespace(persona_id=PERSONA_ID, model="model-a")
    lc = _lifecycle(anchor_id=None)
    status = get_context_status(PERSONA_ID, _manager(persona=persona, lifecycle=lc))
    assert status["window_rows_chars"] is None
    assert status["perception_over_budget"] is None


def test_watermark_resolution_failure_is_500():
    """水位解決の失敗を「水位を持たないモデル」(正常) に偽装しない。"""
    persona = SimpleNamespace(persona_id=PERSONA_ID, model="model-a")
    lc = _lifecycle()

    def _boom(persona, model_key, persona_id=None):
        raise RuntimeError("config broken")

    lc.get_metabolism_watermarks = _boom
    with pytest.raises(HTTPException) as exc:
        get_context_status(PERSONA_ID, _manager(persona=persona, lifecycle=lc))
    assert exc.value.status_code == 500


def test_window_floor_applied_at_passes_through():
    """最終防衛ラインの最終発火時刻 (無ければ None) をそのまま返す
    (docs/issues/window_floor_and_refill_redesign.md 設計 0 — 発火は上流の
    読み戻しの失敗の印なので画面に出す)。"""
    persona = SimpleNamespace(persona_id=PERSONA_ID, model="model-a")
    lc = _lifecycle(presented=[_msg("m1", 2500)])
    status = get_context_status(PERSONA_ID, _manager(persona=persona, lifecycle=lc))
    assert status["window_floor_applied_at"] is None
    lc.window_floor_applied_at = lambda persona_id, model_key: "2026-09-04T01:23:45"
    status = get_context_status(PERSONA_ID, _manager(persona=persona, lifecycle=lc))
    assert status["window_floor_applied_at"] == "2026-09-04T01:23:45"


def test_perception_over_budget_uses_one_window_for_total_and_rows():
    """f288f003 の Codex レビュー残 #2: 旗の合計と行は同じ窓 (計画窓 + 知覚)
    から取る。いまの窓 (印戻し前) は上限超えでも、計画窓の合計が上限以下なら
    旗は立たない — どちらの窓が数字を出したかで旗が裏返らない。"""
    persona = SimpleNamespace(persona_id=PERSONA_ID, model="model-a")
    wm = Watermarks(target=2000, high=4000)
    current = [_msg(f"m{i}", 1000) for i in range(5)]  # いまの窓: 行 5,000
    planning = [_msg("folded:m0", 300), _msg("m4", 1000)]  # 印戻し後: 行 1,300

    # いまの窓の合計 6,500 > 上限、計画窓の合計 2,800 ≤ 上限 → 旗なし
    # (旧: いまの窓の合計 × 計画窓の行 で True になっていた)
    lc = _lifecycle(watermarks=wm, presented=current, planning_presented=planning,
                    refold_ranges=1, perceptions=[_perception(1500)])
    status = get_context_status(PERSONA_ID, _manager(persona=persona, lifecycle=lc))
    assert status["presented_chars"] == 6500  # 実際に送る合計 (透明性) はそのまま
    assert status["stored_chars"] == 5000
    assert status["window_rows_chars"] == 1300
    assert status["perception_over_budget"] is False

    # 計画窓の合計 4,300 > 上限、計画窓の行 1,300 ≤ 残す量 → 旗あり
    # (いまの窓の行 5,000 で判定すると False になる = 窓を混ぜると裏返る)
    lc = _lifecycle(watermarks=wm, presented=current, planning_presented=planning,
                    refold_ranges=1, perceptions=[_perception(3000)])
    status = get_context_status(PERSONA_ID, _manager(persona=persona, lifecycle=lc))
    assert status["window_rows_chars"] == 1300
    assert status["perception_over_budget"] is True
