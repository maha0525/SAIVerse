"""手動生成ジョブの保留理由の写像 (docs/issues/archive/metabolism_deferral_mislabeled_as_window_claim.md 従)。

run_manual_compaction の戻り値がユーザー向けの error_code / 文面へどう写るかを
固定する。特に "deferred" (別入口との claim 競合) と "deferred_sluice_unseen"
(スルースが読めていない範囲による退場見送り) を混ぜない — 混ぜると「別のあらすじ
処理が処理中」という嘘の案内になる (2026-08-24 実機で エリス の手動生成が踏んだ)。
"""

import asyncio
from types import SimpleNamespace

from api.routes.people import arasuji


def _run_with_status(status: str, *, head_rebuilt: bool = True) -> dict:
    """run_manual_compaction_checked が ``status`` を返す構成で worker を回し job を返す。"""
    job_id = arasuji._create_job("p1")
    lifecycle = SimpleNamespace(
        run_manual_compaction_checked=(
            lambda persona, cancellation_token=None: (status, head_rebuilt)
        ),
        ensure_recall_embeddings=lambda persona: None,
    )
    manager = SimpleNamespace(
        personas={"p1": SimpleNamespace(persona_id="p1")},
        sea_runtime=SimpleNamespace(session_lifecycle=lifecycle),
    )
    arasuji._run_chronicle_generation(
        job_id, "p1", max_messages=0, model_name=None, with_memopedia=False,
        manager=manager,
    )
    job = arasuji._get_job(job_id)
    assert job is not None
    return job


def test_deferred_maps_to_window_claimed():
    """claim 競合の保留は従来どおり window_claimed (文面も従来のまま)。"""
    job = _run_with_status("deferred")
    assert job["status"] == "failed"
    assert job["error_code"] == "window_claimed"
    assert "別のあらすじ処理" in job["error"]


def test_deferred_sluice_unseen_maps_to_sluice_unseen():
    """スルースが読めていない範囲による退場見送りは sluice_unseen — claim 競合と混ぜない。

    読めていない範囲は末尾の新着とは限らない (冷えた起点の前進で窓の頭側が
    漏れる並びが実機の初出) ので、文面が「新しい会話」と断定しないことも固定する。
    """
    job = _run_with_status("deferred_sluice_unseen")
    assert job["status"] == "failed"
    assert job["error_code"] == "sluice_unseen"
    assert "読めていない範囲" in job["error"]
    assert "新しい会話" not in job["error"]
    assert "別のあらすじ処理" not in job["error"]


def test_ok_completes():
    job = _run_with_status("ok")
    assert job["status"] == "completed"
    assert job["error"] is None
    assert job["warning"] is None


def test_head_rebuild_failure_warns_but_stays_completed():
    """head を組み直せなかったら completed のまま warning を添える。

    畳み自体は成功しているので失敗扱いにしない。救済 (再試行) はせず、
    知らせるだけ (2026-09-01 まはー裁定)。
    """
    job = _run_with_status("ok", head_rebuilt=False)
    assert job["status"] == "completed"
    assert job["error"] is None
    assert job["warning"] == arasuji._HEAD_REBUILD_WARNING


def test_noop_completes_without_dispatching_head_rebuild(monkeypatch):
    """ルートは head 再構築を発火しない — 責務は run_manual_compaction 側。

    「畳めなくても設定トグルを反映する」保証そのものは
    tests/test_session_anchor_rows.py の
    test_manual_compaction_rebuilds_head_when_nothing_was_folded が固定する。
    ここで見るのは「ルートが上乗せしない」こと — 呼び出し元ごとに発火を
    書くと、"ok" のとき畳み本体の発火と二重になる (Codex 指摘 2026-09-01)。
    """
    from saiverse.dynamic_state import DynamicStateManager

    calls = []
    monkeypatch.setattr(
        DynamicStateManager, "on_metabolism",
        staticmethod(lambda persona, manager, model_key=None: calls.append(persona)),
    )

    job = _run_with_status("noop")
    assert job["status"] == "completed"
    assert calls == []


# ---------------------------------------------------------------------------
# 走行中ジョブへの再接続 (GET /arasuji/generate/latest)
#
# ジョブ ID は開始した画面の state にしか無いので、モーダルを閉じたり、ペルソナ
# メニューから開始したりすると手掛かりが消える。「進捗はあらすじタブで確認
# できます」という画面の案内を成立させるための引き直し口。
# ---------------------------------------------------------------------------


def _latest(persona_id: str):
    return asyncio.run(arasuji.get_latest_arasuji_generation(persona_id))


def test_latest_returns_none_without_any_job():
    """1 件も無いのはエラーではない — 404 ではなく null。"""
    assert _latest("persona_with_no_job") is None


def test_latest_returns_the_newest_job_of_that_persona():
    older = arasuji._create_job("latest_p")
    newer = arasuji._create_job("latest_p")
    # created_at は time.time() 由来で、速いマシンでは同値になりうる。
    # 「より新しい方」を選ぶ規則そのものを見たいので明示的に差をつける。
    arasuji._update_job(older, created_at=100.0, status="completed")
    arasuji._update_job(newer, created_at=200.0, status="running",
                        message="まとめています...")

    result = _latest("latest_p")
    assert result is not None
    assert result.job_id == newer
    assert result.status == "running"
    assert result.message == "まとめています..."


def test_latest_does_not_leak_other_personas_jobs():
    """別ペルソナのジョブは返さない (台帳はプロセス内で共有されている)。"""
    arasuji._create_job("latest_other")
    assert _latest("latest_absent") is None


def test_latest_carries_warning_of_a_finished_job():
    """終了済みジョブも返す — 後から見に来たユーザーが結果と警告を読めるように。"""
    job_id = arasuji._create_job("latest_done")
    arasuji._update_job(
        job_id, status="completed", message="あらすじにまとめました",
        warning=arasuji._HEAD_REBUILD_WARNING,
    )
    result = _latest("latest_done")
    assert result is not None
    assert result.status == "completed"
    assert result.warning == arasuji._HEAD_REBUILD_WARNING
