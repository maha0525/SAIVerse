"""手動生成ジョブの保留理由の写像 (docs/issues/archive/metabolism_deferral_mislabeled_as_window_claim.md 従)。

run_manual_compaction の戻り値がユーザー向けの error_code / 文面へどう写るかを
固定する。特に "deferred" (別入口との claim 競合) と "deferred_sluice_unseen"
(スルースが読めていない範囲による退場見送り) を混ぜない — 混ぜると「別の整理が
処理中」という嘘の案内になる (2026-08-24 実機で エリス の手動生成が踏んだ)。
"""

from types import SimpleNamespace

from api.routes.people import arasuji


def _run_with_status(status: str) -> dict:
    """run_manual_compaction が ``status`` を返す構成で worker を回し job を返す。"""
    job_id = arasuji._create_job("p1")
    lifecycle = SimpleNamespace(
        run_manual_compaction=lambda persona, cancellation_token=None: status,
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
    assert "別の整理" in job["error"]


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
    assert "別の整理" not in job["error"]


def test_ok_completes():
    job = _run_with_status("ok")
    assert job["status"] == "completed"
    assert job["error"] is None


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
