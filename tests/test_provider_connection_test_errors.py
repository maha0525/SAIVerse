"""Connection-test diagnostics never reflect upstream bodies or exception text.

Both HTTP entry points use synthetic provider registries and a fake HTTP
transport. No credentials, persona state, or real provider connections are used.
"""
import logging
import socket

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import providers
from saiverse import provider_configs, provider_security

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

    def run(
        handler, *, protocol="openai_compat", base_url=BASE_URL, api_key_env=API_KEY_ENV,
        source=provider_configs.SOURCE_USER_DATA,
    ):
        cfg = {
            "id": "synthetic",
            "display_name": "Synthetic provider",
            "source": source,
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
    *[(status, "リダイレクト応答を受け取りました。自動追跡はしません。接続先 URL と http/https の設定を確認してください。")
      for status in (300, 301, 302, 307, 308, 399)],
    (400, "リクエストが拒否されました。リクエストまたは認証の形式を確認してください。"),
    (401, "認証に失敗しました。API キーの設定を確認してください。"),
    (403, "アクセスが拒否されました。API キーの権限とプロバイダの利用条件を確認してください。"),
    (404, "接続先が見つかりません。base_url のパスと /v1 の有無を確認してください。"),
    (405, "HTTP メソッドが許可されていません。プロトコルの設定を確認してください。"),
    (408, "正常な応答を受け取れませんでした。URL とプロトコルの設定を確認してください。"),
    (429, "利用制限に達しました。時間をおいて再試行するか、利用枠を確認してください。"),
    *[(status, "プロバイダ側でエラーが発生しました。時間をおいて再試行してください。")
      for status in (500, 502, 503, 599)],
])
def test_http_failures_return_safe_status_diagnostics(probe, caplog, protocol, status, diagnostic):
    run, requests = probe

    def reflect_credentials(req):
        body = f"{req.headers.get('authorization', SECRET)} {UPSTREAM_SECRET} {req.url}"
        return httpx.Response(
            status,
            text=body,
            headers={
                "x-upstream-diagnostic": UPSTREAM_SECRET,
                "location": f"https://redirect.example.invalid/{SECRET}",
            },
            extensions={"reason_phrase": UPSTREAM_SECRET.encode()},
        )

    response = run(reflect_credentials, protocol=protocol)
    assert_safe(response, caplog)
    data = response.json()
    assert data["success"] is False
    assert data["status_code"] == status
    assert data["error"] == f"HTTP {status}: {diagnostic}"
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
    (ValueError, "予期しないエラー"),
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


@pytest.mark.parametrize("api_key_env", [None, API_KEY_ENV])
@pytest.mark.parametrize(("base_url", "diagnostic"), [
    (f"https://provider.example.invalid:{URL_MARKER}/{SECRET}",
     "Provider base_url port must be an integer between 0 and 65535"),
    (f"https://provider.example.invalid:65536/{SECRET}",
     "Provider base_url port must be an integer between 0 and 65535"),
    (f"https://[{SECRET}]/{URL_MARKER}", "Provider base_url must be a valid HTTP(S) URL"),
    (f"https://user:{SECRET}@provider.example.invalid\uff1a443/{URL_MARKER}",
     "Provider base_url must be a valid HTTP(S) URL"),
    (f"https://user:{SECRET}@provider.example.invalid/{URL_MARKER}",
     "Provider base_url must not contain credentials, query, or fragment"),
    (f"https://provider.example.invalid/{URL_MARKER}?key={SECRET}",
     "Provider base_url must not contain credentials, query, or fragment"),
    (f"https://provider.example.invalid/{URL_MARKER}#{SECRET}",
     "Provider base_url must not contain credentials, query, or fragment"),
    (f"http://provider.example.invalid/{URL_MARKER}",
     "Plain HTTP provider URLs require loopback or an explicit allowed host"),
    (f"https://10.0.0.1/{URL_MARKER}",
     "Provider host resolves to a non-public address (10.0.0.1); "
     "add the host to SAIVERSE_PROVIDER_ALLOWED_HOSTS to permit it"),
])
def test_destination_rejection_is_safe_and_sends_nothing(probe, caplog, api_key_env, base_url, diagnostic):
    run, requests = probe
    response = run(
        lambda _: pytest.fail("Rejected URL reached HTTP transport"),
        base_url=base_url, api_key_env=api_key_env,
    )
    assert_safe(response, caplog)
    assert response.json()["success"] is False
    assert response.json()["error"] == diagnostic
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


@pytest.mark.parametrize("api_key_env", [None, API_KEY_ENV])
def test_dns_rejection_preserves_local_diagnostic_without_resolver_text(probe, caplog, monkeypatch, api_key_env):
    run, requests = probe

    def fail_resolution(*_args, **_kwargs):
        raise socket.gaierror(f"{SECRET} {UPSTREAM_SECRET} {BASE_URL}")

    monkeypatch.setattr(provider_security.socket, "getaddrinfo", fail_resolution)
    response = run(lambda _: pytest.fail("Rejected host reached HTTP transport"), api_key_env=api_key_env)
    assert_safe(response, caplog)
    assert response.json()["success"] is False
    assert response.json()["error"] == "Provider host could not be resolved: provider.example.invalid"
    assert requests == []


def test_credential_policy_preserves_local_diagnostic_without_key_values(probe, caplog, monkeypatch):
    run, requests = probe
    monkeypatch.setenv("OPENAI_API_KEY", UPSTREAM_SECRET)
    response = run(
        lambda _: pytest.fail("Rejected credential reached HTTP transport"),
        api_key_env="OPENAI_API_KEY", source=provider_configs.SOURCE_EXPANSION,
    )
    assert_safe(response, caplog)
    assert response.json()["success"] is False
    assert response.json()["error"] == (
        "Provider 'synthetic' was not configured by the owner (source=expansion); "
        f"it must use {API_KEY_ENV} instead of OPENAI_API_KEY"
    )
    assert requests == []


def test_shared_credential_preserves_local_diagnostic_without_key_values(probe, caplog):
    run, requests = probe
    provider_configs.PROVIDER_CONFIGS["other"] = {"api_key_env": API_KEY_ENV.lower()}
    response = run(
        lambda _: pytest.fail("Shared credential reached HTTP transport"),
        source=provider_configs.SOURCE_EXPANSION,
    )
    assert_safe(response, caplog)
    assert response.json()["success"] is False
    assert response.json()["error"] == (
        "Provider 'synthetic' was not configured by the owner (source=expansion); "
        f"{API_KEY_ENV} is already read by 'other' — rename this provider so its credential is its own"
    )
    assert requests == []
