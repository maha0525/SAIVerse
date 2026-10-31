"""Real SDK/HTTPX/httpcore over an in-memory network; no live API or personas."""
from __future__ import annotations

import json
import logging
import re
import threading
from contextvars import ContextVar

import httpcore
import httpx
import pytest
import tenacity
from google import genai
from google.genai import types

from saiverse import gemini_timing as timing
from saiverse.gemini_clients import build_gemini_clients


class Clock:
    now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class WireStream(httpcore.MockStream):
    def __init__(self, response, clock, writes, fail_read=False):
        super().__init__([response])
        self.clock = clock
        self.writes = writes
        self.fail_read = fail_read

    def write(self, buffer, timeout=None):
        self.writes.append(buffer)
        if buffer.startswith(b'{'):
            self.clock.advance(.125)

    def read(self, max_bytes, timeout=None):
        self.clock.advance(.750)
        if self.fail_read:
            raise httpcore.ReadTimeout('SECRET exception payload')
        return super().read(max_bytes, timeout)


class WireBackend(httpcore.MockBackend):
    def __init__(self, responses, clock, *, fail_read=False):
        self.responses = iter(responses)
        self.clock = clock
        self.writes = []
        self.fail_read = fail_read

    def connect_tcp(self, host, port, **kwargs):
        self.clock.advance(.025)
        return WireStream(next(self.responses), self.clock, self.writes, self.fail_read)


def wire_response(body, *, status=200, sse=False):
    data = body.encode()
    return (
        f'HTTP/1.1 {status} Status\r\n'
        f'Content-Type: {"text/event-stream" if sse else "application/json"}\r\n'
        f'Content-Length: {len(data)}\r\nConnection: close\r\n\r\n'
    ).encode() + data


def output_json():
    return json.dumps({'candidates': [{'content': {'role': 'model', 'parts': [{'text': 'ok'}]},
                                      'finishReason': 'STOP'}]})


def records(caplog, prefix='[gemini_http]'):
    return [dict(re.findall(r'(\w+)=([^ ]+)', r.getMessage()))
            for r in caplog.records if r.name == timing.__name__ and r.getMessage().startswith(prefix)]


@pytest.fixture
def sdk_wire(monkeypatch):
    clients = []
    original_send = httpx.HTTPTransport.handle_request

    def memory_network_only(transport, request):
        if not isinstance(transport._pool._network_backend, WireBackend):
            raise AssertionError('Timing tests must use the in-memory network backend')
        return original_send(transport, request)

    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', memory_network_only)

    def create(responses, clock, *, fail_read=False, attempts=1):
        monkeypatch.setenv('GEMINI_API_KEY', 'SECRET-api-key')
        monkeypatch.delenv('GEMINI_FREE_API_KEY', raising=False)
        monkeypatch.setattr(timing, 'perf_counter', clock)
        _, client, _ = build_gemini_clients(_genai_module=genai)
        # Retain the actual builder's hook and actual SDK/HTTPX/httpcore stack.
        backend = WireBackend(responses, clock, fail_read=fail_read)
        client._api_client._httpx_client._mounts.clear()
        client._api_client._httpx_client._transport._pool = httpcore.ConnectionPool(network_backend=backend)
        client._api_client._retry.stop = tenacity.stop_after_attempt(attempts)
        client._api_client._retry.wait = tenacity.wait_none()
        clients.append(client)
        return client, backend

    yield create
    for client in clients:
        client.close()


@pytest.mark.parametrize('stream', [False, True])
def test_real_sdk_separates_body_send_from_wait_without_payloads(sdk_wire, caplog, stream):
    caplog.set_level(logging.DEBUG, logger=timing.__name__)
    clock = Clock()
    body = f'data: {output_json()}\n\n' if stream else output_json()
    client, backend = sdk_wire([wire_response(body, sse=stream)], clock)
    contents = types.Content(role='user', parts=[
        types.Part(text='SECRET prompt'),
        types.Part.from_bytes(data=b'SECRET_AUDIO_BYTES' * 8192, mime_type='audio/wav'),
    ])
    with timing.gemini_timing_scope('generate_stream' if stream else 'generate'):
        clock.advance(.200)
        timing.mark_gemini_stage('contents_ready')
        if stream:
            result = list(client.models.generate_content_stream(model='gemini-test', contents=contents))
            assert result[0].text == 'ok'
        else:
            assert client.models.generate_content(model='gemini-test', contents=contents).text == 'ok'
    events = records(caplog)
    by_stage = {e['stage']: e for e in events}
    assert float(by_stage['body_send_complete']['duration_ms']) == pytest.approx(125)
    assert float(by_stage['response_headers_complete']['duration_ms']) == pytest.approx(750)
    assert float(by_stage['request_ready']['call_elapsed_ms']) == pytest.approx(200)
    assert len({e['request_id'] for e in events}) == 1
    assert len({e['call_id'] for e in events}) == 1
    body_bytes = next(b for b in backend.writes if b.startswith(b'{'))
    assert int(events[0]['body_bytes']) == len(body_bytes)
    assert b'inlineData' in body_bytes and len(body_bytes) > 180_000
    assert [e['stage'] for e in events].index('body_send_complete') < [e['stage'] for e in events].index('response_headers_complete')
    diagnostic = '\n'.join(r.getMessage() for r in caplog.records if r.name == timing.__name__)
    assert 'SECRET' not in diagnostic
    assert 'https://' not in diagnostic
    assert 'gemini-test' not in diagnostic


def test_cache_create_and_sdk_retry_have_separate_http_attempts(sdk_wire, caplog):
    caplog.set_level(logging.DEBUG, logger=timing.__name__)
    clock = Clock()
    error = json.dumps({'error': {'code': 503, 'message': 'SECRET server error', 'status': 'UNAVAILABLE'}})
    cache = json.dumps({'name': 'cachedContents/synthetic', 'usageMetadata': {'totalTokenCount': 1234}})
    client, _ = sdk_wire([wire_response(error, status=503), wire_response(cache)], clock, attempts=2)
    with timing.gemini_timing_scope('cache_create_sync'):
        result = client.caches.create(model='gemini-test', config={'contents': 'SECRET cache prompt'})
    assert result.name == 'cachedContents/synthetic'
    events = records(caplog)
    starts = [e for e in events if e['stage'] == 'request_ready']
    assert len(starts) == 2
    assert starts[0]['request_id'] != starts[1]['request_id']
    assert starts[0]['call_id'] == starts[1]['call_id']
    assert {e['operation'] for e in events} == {'cache_create'}
    assert all('SECRET' not in r.getMessage() for r in caplog.records if r.name == timing.__name__)


def test_transport_failure_logs_type_without_exception_text(sdk_wire, caplog):
    caplog.set_level(logging.DEBUG, logger=timing.__name__)
    client, _ = sdk_wire([b''], Clock(), fail_read=True)
    with pytest.raises(httpx.ReadTimeout), timing.gemini_timing_scope('generate'):
        client.models.generate_content(model='gemini-test', contents='SECRET')
    failures = [e for e in records(caplog) if e['stage'].endswith('_failed')]
    assert failures[-1]['stage'] == 'response_headers_failed'
    assert failures[-1]['error_type'] == 'ReadTimeout'
    assert all('SECRET' not in r.getMessage() for r in caplog.records if r.name == timing.__name__)


def test_info_does_not_add_trace_or_diagnostics(sdk_wire, caplog):
    caplog.set_level(logging.INFO, logger=timing.__name__)
    client, _ = sdk_wire([wire_response(output_json())], Clock())
    with timing.gemini_timing_scope('generate'):
        assert client.models.generate_content(model='gemini-test', contents='SECRET').text == 'ok'
    assert records(caplog) == []
    assert records(caplog, '[gemini_timing]') == []


def test_stream_context_restores_before_yield_and_closes_inner_generator(caplog):
    caplog.set_level(logging.DEBUG, logger=timing.__name__)
    closed = []

    def source():
        timing.mark_gemini_stage('first_text')
        try:
            yield 'ok'
        finally:
            closed.append(timing._CURRENT.get().call_id)

    iterator = timing.timed_gemini_iterator(source(), 'generate_stream')
    assert next(iterator) == 'ok'
    assert timing._CURRENT.get() is None
    iterator.close()
    assert len(closed) == 1
    assert records(caplog, '[gemini_timing]')[-1]['stage'] == 'call_closed'
    assert timing._CURRENT.get() is None


def test_background_inherits_only_timing_and_keeps_own_call_id(caplog):
    caplog.set_level(logging.DEBUG, logger=timing.__name__)
    unrelated = ContextVar('unrelated-test-context', default='clean')
    observed = []
    gate = threading.Event()

    def create_cache():
        gate.wait(5)
        with timing.gemini_timing_scope('cache_create_bg'):
            observed.append(unrelated.get())

    with timing.gemini_timing_scope('generate_stream'):
        token = unrelated.set('SECRET')
        thread = threading.Thread(target=timing.bind_gemini_timing(create_cache))
        thread.start()
        unrelated.reset(token)
    gate.set()
    thread.join(5)
    assert not thread.is_alive()
    calls = [e for e in records(caplog, '[gemini_timing]') if e['stage'] == 'call_started']
    assert calls[1]['parent_id'] == calls[0]['call_id']
    assert calls[1]['call_id'] != calls[0]['call_id']
    assert observed == ['clean']


def test_existing_trace_is_preserved_and_urls_are_not_logged(caplog):
    caplog.set_level(logging.DEBUG, logger=timing.__name__)
    observed = []
    request = httpx.Request('POST', 'https://SECRET:password@example.test/private?key=SECRET',
                            content=b'SECRET', extensions={'trace': lambda name, info: observed.append(name)})
    timing.trace_gemini_request(request)
    first = request.extensions['trace']
    timing.trace_gemini_request(request)
    second = request.extensions['trace']
    second('http2.send_request_body.complete', {})
    assert observed == ['http2.send_request_body.complete']
    assert first.request_id != second.request_id
    assert all('SECRET' not in r.getMessage() for r in caplog.records if r.name == timing.__name__)


@pytest.mark.parametrize('stream', [False, True])
def test_application_entrypoint_links_preparation_http_and_first_text(sdk_wire, caplog, monkeypatch, stream):
    from llm_clients import gemini

    caplog.set_level(logging.DEBUG, logger=timing.__name__)
    # The application's non-schema generate() also consumes the streaming SDK
    # endpoint. tools=[] selects the same no-router path as SEA conversation.
    body = f'data: {output_json()}\n\n'
    sdk, _ = sdk_wire([wire_response(body, sse=True)], Clock())
    monkeypatch.setattr(gemini, 'build_gemini_clients', lambda **kwargs: (None, sdk, sdk))
    monkeypatch.setattr(gemini.GeminiClient, '_AUTO_CACHE_ENABLED', False)
    monkeypatch.delenv('SAIVERSE_DISABLE_GEMINI_STREAMING', raising=False)
    client = gemini.GeminiClient('gemini-2.5-flash')
    messages = [{'role': 'user', 'content': 'SECRET prompt'}]
    result = ''.join(client.generate_stream(messages, tools=[])) if stream else client.generate(messages, tools=[])
    assert result == 'ok'
    calls = records(caplog, '[gemini_timing]')
    assert {r['stage'] for r in calls} >= {'call_started', 'contents_ready', 'call_completed'}
    assert {r['stage'] for r in calls} >= {'first_chunk', 'first_text'}
    assert len({r['call_id'] for r in calls + records(caplog)}) == 1
    assert timing._CURRENT.get() is None


def test_auto_cache_controller_links_child_http_call(sdk_wire, caplog):
    from llm_clients.gemini_cache import GeminiCacheController

    caplog.set_level(logging.DEBUG, logger=timing.__name__)
    cache = json.dumps({'name': 'cachedContents/synthetic', 'usageMetadata': {'totalTokenCount': 1234}})
    sdk, _ = sdk_wire([wire_response(cache)], Clock())
    with timing.gemini_timing_scope('generate'):
        result = GeminiCacheController().ensure(sdk, 'gemini-test', 'SECRET ' * 2000, 300)
    assert result.name == 'cachedContents/synthetic'
    calls = [r for r in records(caplog, '[gemini_timing]') if r['stage'] == 'call_started']
    assert calls[1]['operation'] == 'cache_create_auto'
    assert calls[1]['parent_id'] == calls[0]['call_id']
    assert {r['call_id'] for r in records(caplog)} == {calls[1]['call_id']}
