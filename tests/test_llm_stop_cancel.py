"""Tests for immediate HTTP abort & no-retry on user_stop / server_shutdown."""
from __future__ import annotations

import http.server
import socketserver
import threading
import time
from typing import Callable

import httpx
import pytest
from google import genai
from google.genai import types

from llm_clients.cancel_transport import (
    CancellableHTTPTransport,
    cancellable_sleep,
    install_cancellable_transport,
    install_cancellable_transport2,
)
from saiverse.gemini_clients import _attach_cancellable_hooks
from sea.cancellation import (
    CancellationToken,
    ExecutionCancelledException,
    bind_llm_cancellation_token,
)


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _start_server(handler_cls: type[http.server.BaseHTTPRequestHandler]):
    server = _ThreadedHTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}"


def test_stop_aborts_blocked_response_headers_immediately():
    """応答ヘッダ待ちでブロック中でも user_stop で即座に抜ける。"""
    release = threading.Event()
    entered = threading.Event()

    class HangingHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            entered.set()
            release.wait(timeout=15.0)

        def log_message(self, format, *args):
            pass

    server, base_url = _start_server(HangingHandler)
    token = CancellationToken()
    try:
        with httpx.Client(transport=CancellableHTTPTransport(), timeout=30.0) as client:
            def _stop_soon():
                assert entered.wait(timeout=5.0)
                time.sleep(0.05)
                token.cancel(interrupted_by="user_stop")

            stopper = threading.Thread(target=_stop_soon, daemon=True)
            stopper.start()
            t0 = time.monotonic()
            with pytest.raises(ExecutionCancelledException) as exc_info:
                with bind_llm_cancellation_token(token):
                    with client.stream("POST", f"{base_url}/hang", content=b"hello"):
                        pass
            elapsed = time.monotonic() - t0
            assert elapsed < 2.0
            assert exc_info.value.interrupted_by == "user_stop"
    finally:
        release.set()
        server.shutdown()
        server.server_close()


def test_stop_aborts_mid_stream_between_chunks():
    """1チャンク受信後に固まったストリームでも user_stop で即座に抜ける。"""
    release = threading.Event()
    got_first = threading.Event()

    class SlowStreamHandler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            first = b"data: hello\n\n"
            self.wfile.write(f"{len(first):x}\r\n".encode() + first + b"\r\n")
            self.wfile.flush()
            release.wait(timeout=15.0)

        def log_message(self, format, *args):
            pass

    server, base_url = _start_server(SlowStreamHandler)
    token = CancellationToken()
    try:
        with httpx.Client(transport=CancellableHTTPTransport(), timeout=30.0) as client:
            def _stop_after_first():
                assert got_first.wait(timeout=5.0)
                time.sleep(0.05)
                token.cancel(interrupted_by="user_stop")

            stopper = threading.Thread(target=_stop_after_first, daemon=True)
            stopper.start()
            seen: list[str] = []
            t0 = time.monotonic()
            with pytest.raises(ExecutionCancelledException) as exc_info:
                with bind_llm_cancellation_token(token):
                    with client.stream("POST", f"{base_url}/stream", content=b"hi") as resp:
                        for line in resp.iter_lines():
                            if line:
                                seen.append(line)
                                got_first.set()
            elapsed = time.monotonic() - t0
            assert seen == ["data: hello"]
            assert elapsed < 2.0
            assert exc_info.value.interrupted_by == "user_stop"
    finally:
        release.set()
        server.shutdown()
        server.server_close()


def test_genai_sdk_stop_does_not_retry_after_abort():
    """google-genai の tenacity リトライ (attempts=5) が停止後に一切走らない。"""
    release = threading.Event()
    entered = threading.Event()
    hit_count = 0
    hit_lock = threading.Lock()

    class HangingGenAIHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            nonlocal hit_count
            length = int(self.headers.get("Content-Length", "0"))
            if length:
                self.rfile.read(length)
            with hit_lock:
                hit_count += 1
            entered.set()
            release.wait(timeout=15.0)

        def log_message(self, format, *args):
            pass

    server, base_url = _start_server(HangingGenAIHandler)
    token = CancellationToken()
    try:
        http_options = types.HttpOptions(
            base_url=base_url,
            timeout=30_000,
            retry_options=types.HttpRetryOptions(
                attempts=5,
                initial_delay=0.1,
                max_delay=1.0,
                http_status_codes=[408, 429, 500, 502, 503, 504],
            ),
            client_args={"transport": CancellableHTTPTransport()},
        )
        client = genai.Client(api_key="test-key", http_options=http_options)
        _attach_cancellable_hooks(client)

        def _stop_soon():
            assert entered.wait(timeout=5.0)
            time.sleep(0.05)
            token.cancel(interrupted_by="user_stop")

        stopper = threading.Thread(target=_stop_soon, daemon=True)
        stopper.start()
        t0 = time.monotonic()
        with pytest.raises(ExecutionCancelledException) as exc_info:
            with bind_llm_cancellation_token(token):
                for _chunk in client.models.generate_content_stream(
                    model="gemini-2.5-flash",
                    contents="hello",
                ):
                    pass
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0
        assert exc_info.value.interrupted_by == "user_stop"
        assert hit_count == 1
    finally:
        release.set()
        server.shutdown()
        server.server_close()


def test_genai_sdk_stop_aborts_retry_backoff_sleep_immediately():
    """503 後のリトライ待機 (sleep) 中に停止ボタンを押すと即座に抜け、再送しない。"""
    first_503_sent = threading.Event()
    hit_count = 0
    hit_lock = threading.Lock()

    class Always503Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            nonlocal hit_count
            length = int(self.headers.get("Content-Length", "0"))
            if length:
                self.rfile.read(length)
            with hit_lock:
                hit_count += 1
            body = b'{"error": {"code": 503, "message": "overloaded", "status": "UNAVAILABLE"}}'
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            first_503_sent.set()

        def log_message(self, format, *args):
            pass

    server, base_url = _start_server(Always503Handler)
    token = CancellationToken()
    try:
        http_options = types.HttpOptions(
            base_url=base_url,
            timeout=10_000,
            retry_options=types.HttpRetryOptions(
                attempts=5,
                initial_delay=30.0,
                max_delay=30.0,
                http_status_codes=[503],
            ),
            client_args={"transport": CancellableHTTPTransport()},
        )
        client = genai.Client(api_key="test-key", http_options=http_options)
        _attach_cancellable_hooks(client)

        def _stop_during_sleep():
            assert first_503_sent.wait(timeout=5.0)
            time.sleep(0.1)
            token.cancel(interrupted_by="user_stop")

        stopper = threading.Thread(target=_stop_during_sleep, daemon=True)
        stopper.start()
        t0 = time.monotonic()
        with pytest.raises(ExecutionCancelledException) as exc_info:
            with bind_llm_cancellation_token(token):
                for _chunk in client.models.generate_content_stream(
                    model="gemini-2.5-flash",
                    contents="hello",
                ):
                    pass
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0
        assert exc_info.value.interrupted_by == "user_stop"
        assert hit_count == 1
    finally:
        server.shutdown()
        server.server_close()


def test_stop_only_aborts_target_request_not_sibling_thread():
    """共有 httpx.Client 上でスレッド A を止めてもスレッド B の通信は成功する。"""
    release_a = threading.Event()
    entered_a = threading.Event()

    class DualHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path == "/hang":
                entered_a.set()
                release_a.wait(timeout=15.0)
                return
            time.sleep(0.3)
            body = b"ok-from-b"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server, base_url = _start_server(DualHandler)
    token_a = CancellationToken()
    token_b = CancellationToken()
    results: dict[str, object] = {}

    def _run(name: str, path: str, token: CancellationToken, client: httpx.Client, on_exc: Callable[[Exception], None] | None = None):
        try:
            with bind_llm_cancellation_token(token):
                resp = client.post(f"{base_url}{path}", content=b"x")
                results[name] = resp.text
        except Exception as exc:  # noqa: BLE001
            results[name] = exc
            if on_exc:
                on_exc(exc)

    try:
        with httpx.Client(transport=CancellableHTTPTransport(), timeout=10.0) as shared_client:
            ta = threading.Thread(target=_run, args=("a", "/hang", token_a, shared_client))
            tb = threading.Thread(target=_run, args=("b", "/ok", token_b, shared_client))
            ta.start()
            tb.start()
            assert entered_a.wait(timeout=5.0)
            token_a.cancel(interrupted_by="user_stop")
            ta.join(timeout=3.0)
            tb.join(timeout=3.0)
            assert not ta.is_alive()
            assert not tb.is_alive()
            assert isinstance(results.get("a"), ExecutionCancelledException)
            assert results.get("b") == "ok-from-b"
    finally:
        release_a.set()
        server.shutdown()
        server.server_close()


def test_cancellable_sleep_ignores_non_stop_interrupt_and_wakes_on_stop():
    """普通の割り込み ('user') ではソケット/sleep を即切断せず、user_stop で即起きる。"""
    token = CancellationToken()
    token.cancel(interrupted_by="user")
    assert token.is_cancelled() is True
    assert token.is_stopped() is False

    # user 割り込みだけでは cancellable_sleep はすぐには起きない (0.05s 完走)
    with bind_llm_cancellation_token(token):
        cancellable_sleep(0.05)

    # そのあと user_stop を入れると即座に ExecutionCancelledException
    def _stop():
        time.sleep(0.05)
        token.cancel(interrupted_by="user_stop")

    t = threading.Thread(target=_stop, daemon=True)
    t.start()
    t0 = time.monotonic()
    with pytest.raises(ExecutionCancelledException) as exc_info:
        with bind_llm_cancellation_token(token):
            cancellable_sleep(10.0)
    assert time.monotonic() - t0 < 1.0
    assert exc_info.value.interrupted_by == "user_stop"


def test_httpx2_stop_aborts_blocked_response_headers_immediately():
    """Anthropic 1.x 用の CancellableHTTP2Transport (httpx2) も応答ヘッダ待ちから即座に抜ける。"""
    import httpx2
    from llm_clients.cancel_transport import CancellableHTTP2Transport

    assert CancellableHTTP2Transport is not None
    release = threading.Event()
    entered = threading.Event()

    class HangingHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            entered.set()
            release.wait(timeout=15.0)

        def log_message(self, format, *args):
            pass

    server, base_url = _start_server(HangingHandler)
    token = CancellationToken()
    try:
        with httpx2.Client(transport=CancellableHTTP2Transport(), timeout=30.0) as client:
            def _stop_soon():
                assert entered.wait(timeout=5.0)
                time.sleep(0.05)
                token.cancel(interrupted_by="user_stop")

            stopper = threading.Thread(target=_stop_soon, daemon=True)
            stopper.start()
            t0 = time.monotonic()
            with pytest.raises(ExecutionCancelledException) as exc_info:
                with bind_llm_cancellation_token(token):
                    with client.stream("POST", f"{base_url}/hang", content=b"hello"):
                        pass
            elapsed = time.monotonic() - t0
            assert elapsed < 2.0
            assert exc_info.value.interrupted_by == "user_stop"
    finally:
        release.set()
        server.shutdown()
        server.server_close()


def test_openai_call_with_retry_does_not_retry_on_stop():
    """openai_runtime.call_with_retry は ExecutionCancelledException で即座に抜けリトライしない。"""
    from llm_clients import openai_runtime

    calls = 0

    def _boom():
        nonlocal calls
        calls += 1
        raise ExecutionCancelledException("stopped", interrupted_by="user_stop")

    with pytest.raises(ExecutionCancelledException) as exc_info:
        openai_runtime.call_with_retry(
            _boom,
            context="test",
            max_retries=3,
            initial_backoff=10.0,
            should_retry=lambda _e: True,
        )
    assert calls == 1
    assert exc_info.value.interrupted_by == "user_stop"


def test_provider_timeout_and_retry_defaults_unified():
    """Gemini 4 / Anthropic / OpenAI が 300 秒・リトライ 2 回 (計 3 回) に揃っている。"""
    import json
    from pathlib import Path
    from llm_clients import anthropic as anthropic_mod
    from llm_clients import openai as openai_mod

    assert anthropic_mod.DEFAULT_TIMEOUT_SECONDS == 300.0
    assert anthropic_mod.MAX_RETRIES == 3
    assert openai_mod.DEFAULT_TIMEOUT_SECONDS == 300.0
    assert openai_mod.MAX_RETRIES == 3

    for name in (
        "gemini-4-argon.json",
        "gemini-4-carbon.json",
        "gemini-4-hydrogen.json",
        "gemini-4-hydrogen-paid.json",
        "gemini-4-neon.json",
    ):
        cfg = json.loads((Path("builtin_data/models") / name).read_text(encoding="utf-8"))
        assert cfg["timeout"] == 300, f"{name} timeout should be 300"


def test_openai_client_initializes_with_real_sdk_and_httpx2():
    """openai 3.x の実コンストラクタが httpx2.Client + キャンセル対応で正常初期化する (P1 回帰)。"""
    import httpx2
    from llm_clients.openai import OpenAIClient

    client = OpenAIClient(model="gpt-5", api_key="synthetic-test-key")
    try:
        inner_http = client.client._client
        assert isinstance(inner_http, httpx2.Client)
        assert client.client.max_retries == 0
    finally:
        client.client.close()


def test_install_cancellable_transport_preserves_env_proxies(monkeypatch):
    """HTTPS_PROXY がある環境でもプロキシマウント (HTTPProxy) が維持され、かつキャンセル対応になる (P2 回帰)。"""
    import httpcore
    import httpcore2
    import httpx2
    from llm_clients.cancel_transport import (
        _CancellableSyncBackend,
        _CancellableSyncBackend2,
    )
    from saiverse.gemini_clients import build_gemini_clients

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8888")
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-test-key")
    monkeypatch.delenv("GEMINI_FREE_API_KEY", raising=False)

    with install_cancellable_transport(httpx.Client()) as c1:
        proxy_pools = [
            tr._pool for tr in c1._mounts.values() if isinstance(tr._pool, httpcore.HTTPProxy)
        ]
        assert proxy_pools, "httpx.Client should preserve HTTPS_PROXY HTTPProxy mount"
        assert isinstance(proxy_pools[0]._network_backend, _CancellableSyncBackend)

    with install_cancellable_transport2(httpx2.Client()) as c2:
        proxy_pools2 = [
            tr._pool for tr in c2._mounts.values() if isinstance(tr._pool, httpcore2.HTTPProxy)
        ]
        assert proxy_pools2, "httpx2.Client should preserve HTTPS_PROXY HTTPProxy mount"
        assert isinstance(proxy_pools2[0]._network_backend, _CancellableSyncBackend2)

    _free, _paid, active = build_gemini_clients(prefer_paid=True, timeout_seconds=30.0)
    assert active is not None
    gemini_http = active._api_client._httpx_client
    gemini_proxy_pools = [
        tr._pool for tr in gemini_http._mounts.values() if isinstance(tr._pool, httpcore.HTTPProxy)
    ]
    assert gemini_proxy_pools, "Gemini client should preserve HTTPS_PROXY HTTPProxy mount"
    assert isinstance(gemini_proxy_pools[0]._network_backend, _CancellableSyncBackend)


@pytest.mark.parametrize("use_httpx2", [False, True])
def test_stop_aborts_hanging_tls_handshake_immediately(use_httpx2: bool):
    """TCP 接続後に TLS ハンドシェイクを返さない接続先でも user_stop で即座に抜ける (P2 回帰)。"""
    import socket
    import httpx2

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    accepted = threading.Event()
    release = threading.Event()

    def _accept_and_hang():
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        accepted.set()
        try:
            release.wait(timeout=15.0)
        finally:
            conn.close()

    t_srv = threading.Thread(target=_accept_and_hang, daemon=True)
    t_srv.start()

    token = CancellationToken()

    def _stop_after_tcp():
        assert accepted.wait(timeout=5.0)
        time.sleep(0.05)
        token.cancel(interrupted_by="user_stop")

    t_stop = threading.Thread(target=_stop_after_tcp, daemon=True)
    t_stop.start()

    try:
        if use_httpx2:
            client = install_cancellable_transport2(httpx2.Client(timeout=30.0, trust_env=False))
        else:
            client = install_cancellable_transport(httpx.Client(timeout=30.0, trust_env=False))
        with client:
            t0 = time.monotonic()
            with pytest.raises(ExecutionCancelledException) as exc_info:
                with bind_llm_cancellation_token(token):
                    client.get(f"https://127.0.0.1:{port}/")
            elapsed = time.monotonic() - t0
            assert elapsed < 2.0
            assert exc_info.value.interrupted_by == "user_stop"
    finally:
        release.set()
        srv.close()
