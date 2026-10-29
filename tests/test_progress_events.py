"""チャット画面の進み具合 (status イベントの phase) の送り手。

画面の文言はフロント (frontend/scripts/test-chat-progress.cjs) が検査する。
ここは「何を送るか」と「送れなくても返事を止めない」だけを見る。
"""
from types import SimpleNamespace

import pytest

from saiverse import model_configs, provider_configs
from sea import progress_events
from sea.progress_events import (
    PHASE_AWAITING_RESPONSE,
    PHASE_BUILDING_REQUEST,
    emit_awaiting_response,
    emit_progress,
    provider_label,
)


@pytest.fixture
def providers(monkeypatch):
    table = {
        "gpt": "openai",
        "local": "lmstudio",
        "cpp": "llama_cpp_server",
        "orphan": "missing_provider",
    }
    defs = {
        "openai": {"display_name": "OpenAI"},
        "lmstudio": {"display_name": "LM Studio (local)"},
        "llama_cpp_server": {"display_name": "llama.cpp Server (local)"},
    }
    monkeypatch.setattr(model_configs, "get_provider_for_model", lambda m: table.get(m))
    monkeypatch.setattr(provider_configs, "get_provider", lambda pid: defs.get(pid))


def test_provider_label_is_the_short_server_name(providers):
    assert provider_label("gpt") == "OpenAI"
    # 画面は「<名前>サーバー」と続けるので、補足と重なる語を落とす
    assert provider_label("local") == "LM Studio"
    assert provider_label("cpp") == "llama.cpp"


def test_provider_label_unknown_is_none(providers):
    assert provider_label(None) is None
    assert provider_label("unregistered") is None
    assert provider_label("orphan") is None


def test_emit_progress_names_the_persona_and_drops_empty_params():
    sent = []
    persona = SimpleNamespace(persona_id="p1")
    emit_progress(sent.append, PHASE_BUILDING_REQUEST, persona=persona, provider=None, tool="")
    assert sent == [{"type": "status", "phase": PHASE_BUILDING_REQUEST, "persona_id": "p1"}]


def test_emit_awaiting_response_uses_the_client_actually_sending(providers):
    sent = []
    emit_awaiting_response(sent.append, SimpleNamespace(config_key="gpt"))
    assert sent == [{"type": "status", "phase": PHASE_AWAITING_RESPONSE, "provider": "OpenAI"}]


def test_no_callback_is_a_no_op():
    emit_progress(None, PHASE_BUILDING_REQUEST)
    emit_awaiting_response(None, SimpleNamespace(config_key="gpt"))


def test_a_broken_display_never_stops_the_reply(caplog):
    def broken(_event):
        raise RuntimeError("queue closed")

    with caplog.at_level("WARNING", logger=progress_events.__name__):
        emit_progress(broken, PHASE_AWAITING_RESPONSE)
    assert "failed to emit phase=awaiting_response" in caplog.text
