"""Connection-test diagnostics never reflect upstream bodies or exception text.

Both HTTP entry points use synthetic provider registries and a fake HTTP
transport. No credentials, persona state, or real provider connections are used.
"""
import logging

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import providers
from saiverse import provider_configs

SECRET = "dummy-api-key-do-not-reflect"
UPSTREAM_SECRET = "unknown-upstream-secret-do-not-reflect"
URL_MARKER = "private-url-do-not-reflect"
BASE_URL = f"https://provider.example.invalid/{URL_MARKER}"
API_KEY_ENV = "SAIVERSE_PROVIDER_SYNTHETIC_API_KEY"


@pytest.fixture(params=["saved", "inline"])
def probe(request, tmp_path, monkeypatch, mock_provider_network, caplog):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SAIVERSE_HOME", str(tmp_path / "home" / ".saiverse"))
    monkeypatch.setenv("SAIVERSE_USER_DATA_DIR", str(tmp_path / "user_data"))
    monkeypatch.setenv(API_KEY_ENV, SECRET)
    monkeypatch.delenv("SAIVERSE_PROVIDER_ALLOWED_HOSTS", raising=False)
    mock_provider_network("provider.example.invalid")
    monkeypatch.setattr(provider_configs, "PROVIDER_CONFIGS", {})
    caplog.set_level(logging.DEBUG, logger=providers.LOGGER.name)
    app = FastAPI()
    app.include_router(providers.router, prefix="/api/providers")
    real_http_client = httpx.Client
    requests = []

    def run(handler, *, protocol="openai_compat", base_url=BASE_URL, api_key_env=API_KEY_ENV):
        cfg = {
            "id": "synthetic",
            "display_name": "Synthetic provider",
            "source": provider_configs.SOURCE_USER_DATA,
            "protocol": protocol,
            "base_url": base_url,
            "api_key_env": api_key_env,
        }
        provider_configs.PROVIDER_CONFIGS["synthetic"] = cfg

        def handle(req):
            requests.append(req)
            return handler(req)

        def fake_client(**kwargs):
            return real_http_client(transport=httpx.MockTransport(handle), **kwargs)

        monkeypatch.setattr(providers.httpx, "Client", fake_client)
        with TestClient(app) as client:
            if request.param == "saved":
                return client.post("/api/providers/synthetic/test")
            return client.post("/api/providers/test", json={
                "provider_id": "synthetic",
                "protocol": protocol,
                "base_url": base_url,
                "api_key_env": api_key_env,
            })

    return run, requests


def assert_safe(response, caplog):
    assert response.status_code == 200
    # HTTP libraries have their own access logs; this contract covers the
    # connection-test diagnostics, without changing global logger settings.
    records = [record for record in caplog.records if record.name == providers.LOGGER.name]
    for marker in (SECRET, UPSTREAM_SECRET, URL_MARKER):
        assert marker not in response.text
        assert all(marker not in record.getMessage() for record in records)
    assert all(record.exc_info is None for record in records)


@pytest.mark.parametrize("protocol", ["openai_compat", "ollama_compat"])
@pytest.mark.parametrize(("status", "diagnostic"), [
    (301, "URL"), (400, "URL"), (401, "認証"), (403, "権限"),
    (404, "URL"), (429, "利用制限"), (500, "プロバイダ側"),
    (502, "プロバイダ側"), (503, "プロバイダ側"),
])
def test_http_failures_return_safe_status_diagnostics(probe, caplog, protocol, status, diagnostic):
    run, requests = probe

    def reflect_credentials(req):
        body = f"{req.headers.get('authorization', SECRET)} {UPSTREAM_SECRET} {req.url}"
        return httpx.Response(
            status,
            text=body,
            headers={"x-upstream-diagnostic": UPSTREAM_SECRET},
            extensions={"reason_phrase": UPSTREAM_SECRET.encode()},
        )

    response = run(reflect_credentials, protocol=protocol)
    assert_safe(response, caplog)
    data = response.json()
    assert data["success"] is False
    assert data["status_code"] == status
    assert f"HTTP {status}" in data["error"]
    assert diagnostic in data["error"]
    assert data["models"] is None
    assert data["elapsed_ms"] >= 0
    assert len(requests) == 1
    if protocol == "openai_compat":
        assert requests[0].headers["authorization"] == f"Bearer {SECRET}"
        assert requests[0].url.path.endswith("/models")
    else:
        assert "authorization" not in requests[0].headers
        assert requests[0].url.path.endswith("/api/tags")


@pytest.mark.parametrize(("exception", "diagnostic"), [
    (httpx.ConnectError, "接続失敗"),
    (httpx.ReadTimeout, "タイムアウト"),
    (httpx.RemoteProtocolError, "通信エラー"),
    (httpx.ReadError, "通信エラー"),
    (RuntimeError, "予期しないエラー"),
])
def test_transport_exception_text_is_not_returned_or_logged(probe, caplog, exception, diagnostic):
    run, requests = probe

    def fail(req):
        raise exception(f"{SECRET} {UPSTREAM_SECRET} {req.url}")

    response = run(fail)
    assert_safe(response, caplog)
    assert response.json()["success"] is False
    assert diagnostic in response.json()["error"]
    assert len(requests) == 1


@pytest.mark.parametrize("protocol", ["openai_compat", "ollama_compat"])
def test_invalid_json_does_not_log_parser_exception(probe, caplog, protocol):
    run, _ = probe

    class InvalidJSONResponse(httpx.Response):
        def json(self, **kwargs):
            raise ValueError(f"{SECRET} {UPSTREAM_SECRET} {BASE_URL}")

    response = run(lambda _: InvalidJSONResponse(200), protocol=protocol)
    assert_safe(response, caplog)
    assert response.json()["success"] is True
    assert response.json()["models"] == []
    assert "Failed to parse provider test response" in caplog.text


@pytest.mark.parametrize(("protocol", "listing"), [
    ("openai_compat", {"data": [{"id": "synthetic-model"}, {}, None]}),
    ("ollama_compat", {"models": [{"name": "synthetic-model"}, {}, None]}),
])
def test_success_preserves_model_discovery(probe, caplog, protocol, listing):
    run, _ = probe
    response = run(lambda _: httpx.Response(200, json=listing), protocol=protocol)
    assert_safe(response, caplog)
    assert response.json()["success"] is True
    assert response.json()["status_code"] == 200
    assert response.json()["models"] == ["synthetic-model"]


def test_destination_rejection_is_safe_and_sends_nothing(probe, caplog):
    run, requests = probe
    response = run(
        lambda _: pytest.fail("Rejected URL reached HTTP transport"),
        base_url=f"https://provider.example.invalid:{URL_MARKER}/{SECRET}",
    )
    assert_safe(response, caplog)
    assert response.json()["success"] is False
    assert "設定" in response.json()["error"]
    assert requests == []


def test_unsupported_protocol_is_not_reflected(probe, caplog):
    run, requests = probe
    response = run(
        lambda _: pytest.fail("Unsupported protocol reached HTTP transport"),
        protocol=f"{SECRET} {UPSTREAM_SECRET}",
    )
    assert_safe(response, caplog)
    assert response.json()["success"] is False
    assert "未対応" in response.json()["error"]
    assert requests == []


@pytest.mark.parametrize("listing", [
    {"data": [{"id": {"secret": UPSTREAM_SECRET}}]},
    {"data": [{"id": [UPSTREAM_SECRET]}]},
])
def test_invalid_model_ids_do_not_expose_validation_exception(probe, caplog, listing):
    run, _ = probe
    response = run(lambda _: httpx.Response(200, json=listing))
    assert_safe(response, caplog)
    assert response.json()["success"] is False
    assert "予期しないエラー" in response.json()["error"]


@pytest.mark.parametrize("content", [
    b"not-json",
    f'{{"{UPSTREAM_SECRET}":'.encode(),
    f'["{UPSTREAM_SECRET}"]'.encode(),
])
def test_malformed_listing_keeps_existing_reachable_behavior(probe, caplog, content):
    run, _ = probe
    response = run(lambda _: httpx.Response(200, content=content))
    assert_safe(response, caplog)
    assert response.json()["success"] is True
    assert response.json()["models"] == []
