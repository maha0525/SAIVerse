from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api.routes import config


class _FakeQuery:
    def __init__(self, playbook_exists: bool = True) -> None:
        self._model = None
        self._playbook_exists = playbook_exists

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        model_name = getattr(self._model, "__name__", "")
        if model_name == "Playbook":
            return object() if self._playbook_exists else None
        if model_name == "UserSettings":
            return SimpleNamespace(SELECTED_META_PLAYBOOK=None)
        return None


class _FakeSession:
    def __init__(self, playbook_exists: bool = True) -> None:
        self._playbook_exists = playbook_exists

    def query(self, model):
        query = _FakeQuery(self._playbook_exists)
        query._model = model
        return query

    def add(self, _obj):
        return None

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


class _FakeManager:
    def __init__(self) -> None:
        self.state = SimpleNamespace(current_playbook=None, playbook_params={})


def _patch_session_local(monkeypatch: pytest.MonkeyPatch, *, playbook_exists: bool) -> None:
    import database.session

    monkeypatch.setattr(
        database.session,
        "SessionLocal",
        lambda: _FakeSession(playbook_exists=playbook_exists),
    )


def test_set_playbook_meta_user_manual_rejects_unknown_selected_playbook(monkeypatch: pytest.MonkeyPatch):
    _patch_session_local(monkeypatch, playbook_exists=False)
    manager = _FakeManager()

    with pytest.raises(HTTPException) as exc_info:
        config.set_playbook(
            config.PlaybookOverrideRequest(
                playbook="meta_user_manual",
                playbook_params={"selected_playbook": "表示ラベル"},
            ),
            manager=manager,
        )

    assert exc_info.value.status_code == 400
    assert "Playbook ID" in exc_info.value.detail


def test_set_playbook_meta_user_manual_allows_empty_selected_playbook(monkeypatch: pytest.MonkeyPatch):
    _patch_session_local(monkeypatch, playbook_exists=False)
    manager = _FakeManager()

    resp = config.set_playbook(
        config.PlaybookOverrideRequest(
            playbook="meta_user_manual",
            playbook_params={"selected_playbook": ""},
        ),
        manager=manager,
    )

    assert resp["success"] is True
    assert resp["playbook"] == "meta_user_manual"


def test_set_playbook_meta_user_manual_accepts_existing_selected_playbook(monkeypatch: pytest.MonkeyPatch):
    _patch_session_local(monkeypatch, playbook_exists=True)
    manager = _FakeManager()

    resp = config.set_playbook(
        config.PlaybookOverrideRequest(
            playbook="meta_user_manual",
            playbook_params={"selected_playbook": "deep_research"},
        ),
        manager=manager,
    )

    assert resp["success"] is True
    assert resp["playbook_params"]["selected_playbook"] == "deep_research"
