"""グローバル設定で標準モデルを変えたとき、動いているペルソナにも反映されること。

グローバル設定の「モデルロール」と「環境」タブは POST /api/admin/env
(api/routes/admin.py の update_env_vars) で保存する。保存が .env と os.environ を
書き換えるだけだと、個別の標準モデルを持たないペルソナは再起動まで古いモデルで
話し続けるのに、モデル設定の警告 (GET /api/config/startup-warnings) はいまの
環境変数を見るので消えてしまう。ここでは、保存のあとで update_default_model が
呼ばれる条件を押さえる:

- 定義のある標準モデルに変えると、書き込みのあとで呼ばれる
- 定義の無い値・空の値・API モデル名 (起動時の引き方では引けない) では呼ばれない
- SAIVERSE_DEFAULT_MODEL を含まない更新では呼ばれない
- 書き込みに失敗したら呼ばれない。反映に失敗しても、保存は成功として返す

本物の .env を書き換えないよう write_env_updates は差し替え、manager は呼び出しを
記録するだけの偽物にする。LLM は呼ばない。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.deps import get_manager
from api.routes import admin
from saiverse import model_configs

DEFINED_KEY = "test-defined-model"
#: DEFINED_KEY の定義が持つ API モデル名。設定キーとしては存在しない。
DEFINED_API_NAME = "vendor/test-defined-api-name"


class _RecordingManager:
    """update_default_model の呼び出しを、書き込みと同じ列に記録する。"""

    def __init__(self, events, *, fail=False):
        self.events = events
        self.fail = fail

    def update_default_model(self, model):
        self.events.append(("update_default_model", model))
        if self.fail:
            raise RuntimeError("boom")


@pytest.fixture
def events(monkeypatch):
    recorded = []
    monkeypatch.setattr(model_configs, "MODEL_CONFIGS", {
        DEFINED_KEY: {"model": DEFINED_API_NAME, "provider": "stub", "context_length": 1000},
    })
    monkeypatch.setattr(
        admin,
        "write_env_updates",
        lambda updates: recorded.append(("write_env_updates", dict(updates))),
    )
    return recorded


def _post(manager, updates):
    app = FastAPI()
    app.include_router(admin.router, prefix="/api/admin")
    app.dependency_overrides[get_manager] = lambda: manager
    return TestClient(app).post("/api/admin/env", json={"updates": updates})


def test_defined_default_model_is_applied_after_the_env_is_written(events):
    response = _post(_RecordingManager(events), {"SAIVERSE_DEFAULT_MODEL": DEFINED_KEY})

    assert response.status_code == 200
    assert events == [
        ("write_env_updates", {"SAIVERSE_DEFAULT_MODEL": DEFINED_KEY}),
        ("update_default_model", DEFINED_KEY),
    ]


@pytest.mark.parametrize("value", [
    pytest.param("gone-default", id="undefined"),
    pytest.param("", id="empty"),
    # 起動時の標準モデルは設定キーの完全一致で引く (manager/initialization.py の
    # _init_model_config)。API モデル名はその引き方では引けない。
    pytest.param(DEFINED_API_NAME, id="api-model-name"),
])
def test_default_model_without_definition_is_not_applied(events, value):
    response = _post(_RecordingManager(events), {"SAIVERSE_DEFAULT_MODEL": value})

    assert response.status_code == 200
    assert events == [("write_env_updates", {"SAIVERSE_DEFAULT_MODEL": value})]


def test_update_without_default_model_is_not_applied(events):
    updates = {
        "SAIVERSE_DEFAULT_LIGHTWEIGHT_MODEL": DEFINED_KEY,
        "MEMORY_WEAVE_MODEL": DEFINED_KEY,
        "SAIVERSE_IMAGE_SUMMARY_MODEL": DEFINED_KEY,
    }

    response = _post(_RecordingManager(events), updates)

    assert response.status_code == 200
    assert events == [("write_env_updates", updates)]


def test_failed_write_is_not_applied(events, monkeypatch):
    def failing_write(updates):
        events.append(("write_env_updates", dict(updates)))
        raise OSError("disk full")

    monkeypatch.setattr(admin, "write_env_updates", failing_write)

    response = _post(_RecordingManager(events), {"SAIVERSE_DEFAULT_MODEL": DEFINED_KEY})

    assert response.status_code == 500
    assert events == [("write_env_updates", {"SAIVERSE_DEFAULT_MODEL": DEFINED_KEY})]


def test_failed_apply_still_reports_the_save_as_done(events):
    """.env と os.environ は書き換え済みなので、保存を失敗として返さない。"""
    response = _post(
        _RecordingManager(events, fail=True), {"SAIVERSE_DEFAULT_MODEL": DEFINED_KEY},
    )

    assert response.status_code == 200
    assert events == [
        ("write_env_updates", {"SAIVERSE_DEFAULT_MODEL": DEFINED_KEY}),
        ("update_default_model", DEFINED_KEY),
    ]


def test_update_default_model_also_updates_the_admin_service_copy():
    """ワールドエディタから作るペルソナは、AdminService が起動時に写した標準モデルを使う
    (manager/admin.py の __init__、manager/persona.py の create_ai)。標準モデルを変えたら
    その写しも揃い、変えた後に作るペルソナが古いモデルで作られない。"""
    from types import SimpleNamespace

    from saiverse.saiverse_manager import SAIVerseManager

    class _Session:
        def close(self):
            pass

    host = SimpleNamespace(
        _base_model="old-model",
        admin=SimpleNamespace(_base_model="old-model"),
        personas={},
        SessionLocal=_Session,
    )

    SAIVerseManager.update_default_model(host, DEFINED_KEY)

    assert host._base_model == DEFINED_KEY
    assert host.admin._base_model == DEFINED_KEY
