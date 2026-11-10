"""Payload-free DEBUG timings for Gemini calls and individual HTTP attempts."""
from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Callable, Iterator

import httpx

LOGGER = logging.getLogger(__name__)


@dataclass
class _Call:
    operation: str
    parent_id: str
    call_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started: float = field(default_factory=lambda: perf_counter())

    def mark(self, stage: str) -> None:
        LOGGER.debug(
            "[gemini_timing] call_id=%s parent_id=%s operation=%s stage=%s elapsed_ms=%.3f",
            self.call_id, self.parent_id, self.operation, stage,
            (perf_counter() - self.started) * 1000,
        )


_CURRENT: ContextVar[_Call | None] = ContextVar("gemini_timing_call", default=None)


def mark_gemini_stage(stage: str) -> None:
    call = _CURRENT.get()
    if call is not None:
        call.mark(stage)


def bind_gemini_timing(callback: Callable[[], Any]) -> Callable[[], Any]:
    """Carry only this diagnostic context into a background thread."""
    call = _CURRENT.get()
    if call is None:
        return callback

    def run() -> Any:
        token = _CURRENT.set(call)
        try:
            return callback()
        finally:
            _CURRENT.reset(token)

    return run


@contextmanager
def gemini_timing_scope(operation: str) -> Iterator[None]:
    """For synchronous work only; never hold this scope across a generator yield."""
    if not LOGGER.isEnabledFor(logging.DEBUG):
        yield
        return
    parent = _CURRENT.get()
    call = _Call(operation, parent.call_id if parent else "-")
    token = _CURRENT.set(call)
    call.mark("call_started")
    try:
        yield
    except BaseException:
        call.mark("call_failed")
        raise
    else:
        call.mark("call_completed")
    finally:
        _CURRENT.reset(token)


def timed_gemini_iterator(iterator: Iterator[Any], operation: str) -> Iterator[Any]:
    """Activate the call only while advancing/closing, restoring before each yield."""
    if not LOGGER.isEnabledFor(logging.DEBUG):
        yield from iterator
        return
    parent = _CURRENT.get()
    call = _Call(operation, parent.call_id if parent else "-")
    call.mark("call_started")
    outcome = "call_closed"
    try:
        while True:
            token = _CURRENT.set(call)
            try:
                item = next(iterator)
            except StopIteration:
                outcome = "call_completed"
                break
            finally:
                _CURRENT.reset(token)
            yield item
    except GeneratorExit:
        raise
    except BaseException:
        outcome = "call_failed"
        raise
    finally:
        token = _CURRENT.set(call)
        try:
            close = getattr(iterator, "close", None)
            if close is not None:
                close()
        finally:
            _CURRENT.reset(token)
            call.mark(outcome)


_TRACE_STAGES = {
    "connection.connect_tcp": "connect",
    "connection.start_tls": "tls",
    "http11.send_request_headers": "headers_send",
    "http11.send_request_body": "body_send",
    "http11.receive_response_headers": "response_headers",
    "http2.send_request_headers": "headers_send",
    "http2.send_request_body": "body_send",
    "http2.receive_response_headers": "response_headers",
}


class _HttpTiming:
    def __init__(self, request: httpx.Request, previous: Any) -> None:
        self.previous = previous
        self.request_id = uuid.uuid4().hex
        self.call = _CURRENT.get()
        self.started = perf_counter()
        self.phase_starts: dict[str, float] = {}
        path = request.url.path
        if path.endswith(":streamGenerateContent"):
            self.operation = "generate_stream"
        elif path.endswith(":generateContent"):
            self.operation = "generate"
        elif path.rstrip("/").endswith("/cachedContents") and request.method == "POST":
            self.operation = "cache_create"
        else:
            self.operation = "other"
        length = request.headers.get("content-length", "")
        self.body_bytes = int(length) if length.isascii() and length.isdecimal() and len(length) < 20 else -1
        self.emit("request_ready", self.started)

    def emit(self, stage: str, now: float, duration_ms: float = 0, error_type: str = "-") -> None:
        LOGGER.debug(
            "[gemini_http] call_id=%s request_id=%s operation=%s stage=%s "
            "elapsed_ms=%.3f call_elapsed_ms=%.3f duration_ms=%.3f body_bytes=%d error_type=%s",
            self.call.call_id if self.call else "-", self.request_id, self.operation, stage,
            (now - self.started) * 1000,
            (now - self.call.started) * 1000 if self.call else -1,
            duration_ms, self.body_bytes, error_type,
        )

    def __call__(self, event: str, info: dict[str, Any]) -> None:
        base, _, suffix = event.rpartition(".")
        phase = _TRACE_STAGES.get(base)
        if phase is not None and suffix in {"started", "complete", "failed"}:
            now = perf_counter()
            if suffix == "started":
                self.phase_starts[phase] = now
            duration = (now - self.phase_starts.get(phase, now)) * 1000
            error = info.get("exception") if suffix == "failed" else None
            self.emit(f"{phase}_{suffix}", now, duration, type(error).__name__ if error else "-")
        if self.previous is not None:
            self.previous(event, info)


def trace_gemini_request(request: httpx.Request) -> None:
    """HTTPX request hook: serialized bytes are ready; do not inspect their content."""
    if not LOGGER.isEnabledFor(logging.DEBUG):
        return
    previous = request.extensions.get("trace")
    # Redirects/reused Request instances may carry our old hook. Replace it,
    # preserving only the pre-existing external callback (no duplicate records).
    if isinstance(previous, _HttpTiming):
        previous = previous.previous
    request.extensions["trace"] = _HttpTiming(request, previous)
