"""POST /api/config/model の直列化 + 世代ガード (client_id ごと) のテスト。

abort されたモデル変更要求はブラウザー側の fetch が切れるだけで、サーバー側の
適用は走り切る。古い要求が遅れて到着して同じクライアントの新しい選択を
上書きしないよう、選択世代 (seq) を client_id ごとに管理して古い要求を 409 で
拒否する (Codex 指摘 2026-07-30)。世代をサーバー全体で一本にすると、カウンターが
マウントごとに 0 へ戻るクライアントの再読込・別タブが恒久的に 409 になる
(同 5 巡目指摘) — client_id の名前空間がそれを防ぐ。
docs/issues/chat_options_metabolism_section_redesign.md
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import api.routes.config as config_module
from api.routes.config import UpdateModelRequest, set_model


def _fake_manager():
    manager = SimpleNamespace(model=None, model_parameter_overrides={})

    def _set_model(model, parameters=None):
        manager.model = model

    manager.set_model = _set_model
    return manager


@pytest.fixture(autouse=True)
def _reset_seqs(monkeypatch):
    monkeypatch.setattr(config_module, "_model_change_seqs", {})


def test_newer_seq_applies():
    manager = _fake_manager()
    set_model(UpdateModelRequest(model="model-a", seq=1, client_id="c1"), manager)
    set_model(UpdateModelRequest(model="model-b", seq=2, client_id="c1"), manager)
    assert manager.model == "model-b"


def test_stale_seq_is_rejected_and_not_applied():
    """新しい選択 (seq=2) の後に古い要求 (seq=1) が遅延到着 → 409、適用しない。"""
    manager = _fake_manager()
    set_model(UpdateModelRequest(model="model-b", seq=2, client_id="c1"), manager)
    with pytest.raises(HTTPException) as exc:
        set_model(UpdateModelRequest(model="model-a", seq=1, client_id="c1"), manager)
    assert exc.value.status_code == 409
    assert manager.model == "model-b"


def test_remounted_client_starts_fresh():
    """再読込 (新しい client_id・seq=1 から) は前の高い seq に阻まれないこと。"""
    manager = _fake_manager()
    set_model(UpdateModelRequest(model="model-a", seq=5, client_id="old"), manager)
    set_model(UpdateModelRequest(model="model-b", seq=1, client_id="new"), manager)
    assert manager.model == "model-b"


def test_clients_do_not_block_each_other():
    """別タブ (別 client_id) の高い seq が他クライアントの操作を拒否させない。"""
    manager = _fake_manager()
    set_model(UpdateModelRequest(model="model-a", seq=9, client_id="tab1"), manager)
    set_model(UpdateModelRequest(model="model-b", seq=1, client_id="tab2"), manager)
    set_model(UpdateModelRequest(model="model-c", seq=10, client_id="tab1"), manager)
    assert manager.model == "model-c"


def test_seqless_request_keeps_backward_compat():
    """seq / client_id 省略 (他の呼び出し元) は従来どおり無条件適用。"""
    manager = _fake_manager()
    set_model(UpdateModelRequest(model="model-b", seq=2, client_id="c1"), manager)
    set_model(UpdateModelRequest(model="model-c"), manager)
    assert manager.model == "model-c"


def test_ledger_cap_keeps_fresh_entries_and_their_guarantees():
    """上限到達でも新しい項目は刈られず、既存クライアントの順序保証が生きること。

    全消しにすると「c1/seq=2 適用 → 上限で台帳消去 → 遅着した c1/seq=1 が
    再受理されて巻き戻る」— 防止対象そのものが復活する (Codex 指摘 2026-07-30)。
    """
    manager = _fake_manager()
    set_model(UpdateModelRequest(model="model-b", seq=2, client_id="c1"), manager)
    for i in range(config_module._MODEL_CHANGE_SEQS_MAX + 5):
        set_model(
            UpdateModelRequest(model="model-a", seq=1, client_id=f"filler{i}"), manager,
        )
    # 上限を跨いだ後でも、c1 の古い要求 (seq=1) は拒否される
    with pytest.raises(HTTPException) as exc:
        set_model(UpdateModelRequest(model="model-x", seq=1, client_id="c1"), manager)
    assert exc.value.status_code == 409
    assert manager.model == "model-a"


def test_ledger_cap_evicts_only_expired_entries():
    """刈ってよいのは安全期間 (TTL) を過ぎた項目だけ。"""
    manager = _fake_manager()
    set_model(UpdateModelRequest(model="model-b", seq=2, client_id="c1"), manager)
    # c1 を「10 分以上前に使われた」ことにして、他で上限を満たす
    seq_val, _touched = config_module._model_change_seqs["c1"]
    config_module._model_change_seqs["c1"] = (
        seq_val,
        _touched - config_module._MODEL_CHANGE_SEQ_TTL_SECONDS - 1,
    )
    for i in range(config_module._MODEL_CHANGE_SEQS_MAX):
        set_model(
            UpdateModelRequest(model="model-a", seq=1, client_id=f"filler{i}"), manager,
        )
    # 期限切れの c1 だけが刈られ、新しい filler 群は残る
    assert "c1" not in config_module._model_change_seqs
    assert len(config_module._model_change_seqs) >= config_module._MODEL_CHANGE_SEQS_MAX
