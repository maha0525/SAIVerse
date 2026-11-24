"""停止ボタン / 終了処理で待機中の HTTP 通信とリトライを即座に断つための部品。

docs/intent/stop_button.md の「LLM サーバーからの応答待ち・リトライ待機中も
停止で即座に抜ける」契約を実現する。

- ``CancellableHTTPTransport``: ``httpx.HTTPTransport`` のネットワーク層
  (``httpcore.SyncBackend`` / ``NetworkStream``) を薄く包み、現在の文脈に
  紐づく ``CancellationToken`` に ``user_stop`` / ``server_shutdown`` が
  届いた瞬間に、そのリクエストのソケットだけを shutdown + close して
  ブロック中の ``recv`` / ``send`` / TLS ハンドシェイクを即座に解除する。
- 中断時は ``httpx.ReadError`` 等ではなく ``ExecutionCancelledException`` を
  投げるため、SDK 内部の ``tenacity`` リトライ (``_HTTPX_TRANSIENT_EXC`` 判定)
  にもクライアント側のリトライにも絶対に捕まらず、以降のリトライが走らない。
- ``cancellable_sleep``: ``tenacity`` やクライアント側のリトライ待機
  (``time.sleep``) を置き換え、待機中に停止が届いたら即座に抜ける。
"""
from __future__ import annotations

import logging
import socket
import ssl
import time
from typing import Any, Optional

import httpcore
import httpx

from sea.cancellation import (
    current_llm_cancellation_token,
    current_llm_stop_reason,
    raise_if_llm_stopped,
)

LOGGER = logging.getLogger(__name__)


def _abort_stream(inner: httpcore.NetworkStream) -> None:
    """待機中のソケットだけを即座に閉じる (他スレッドの接続には触れない)。"""
    sock = None
    try:
        sock = inner.get_extra_info("socket")
    except Exception:  # noqa: BLE001
        sock = None

    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass
        return

    try:
        inner.close()
    except Exception:  # noqa: BLE001
        pass


class _CancellableSyncStream(httpcore.NetworkStream):
    """現在の LLM 呼び出しが停止されたら即座にソケットを閉じる NetworkStream。"""

    def __init__(self, inner: httpcore.NetworkStream) -> None:
        self._inner = inner

    def _run_cancellable(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        token = current_llm_cancellation_token()
        if token is None:
            return fn(*args, **kwargs)
        raise_if_llm_stopped()
        unregister = token.register_on_stop(lambda _reason: _abort_stream(self._inner))
        try:
            return fn(*args, **kwargs)
        except Exception:
            # 停止によるソケット切断で OSError / ReadError 等が出た場合は
            # ExecutionCancelledException に正規化して、リトライを確実に止める。
            raise_if_llm_stopped()
            raise
        finally:
            unregister()

    def read(self, max_bytes: int, timeout: Optional[float] = None) -> bytes:
        return self._run_cancellable(self._inner.read, max_bytes, timeout=timeout)

    def write(self, buffer: bytes, timeout: Optional[float] = None) -> None:
        self._run_cancellable(self._inner.write, buffer, timeout=timeout)

    def close(self) -> None:
        self._inner.close()

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> httpcore.NetworkStream:
        tls_stream = self._run_cancellable(
            self._inner.start_tls,
            ssl_context,
            server_hostname=server_hostname,
            timeout=timeout,
        )
        return _CancellableSyncStream(tls_stream)

    def get_extra_info(self, info: str) -> Any:
        return self._inner.get_extra_info(info)


class _CancellableSyncBackend(httpcore.SyncBackend):
    """接続確立時にも停止を確認し、返すストリームをキャンセル対応にする Backend。"""

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: Optional[float] = None,
        local_address: Optional[str] = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        raise_if_llm_stopped()
        stream = super().connect_tcp(
            host,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )
        if current_llm_stop_reason() is not None:
            _abort_stream(stream)
            raise_if_llm_stopped()
        return _CancellableSyncStream(stream)

    def connect_unix_socket(
        self,
        path: str,
        timeout: Optional[float] = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        raise_if_llm_stopped()
        stream = super().connect_unix_socket(
            path,
            timeout=timeout,
            socket_options=socket_options,
        )
        if current_llm_stop_reason() is not None:
            _abort_stream(stream)
            raise_if_llm_stopped()
        return _CancellableSyncStream(stream)


class CancellableHTTPTransport(httpx.HTTPTransport):
    """停止合図に即応する ``httpx.HTTPTransport``。

    同一 ``httpx.Client`` を複数スレッドで共有していても、停止したリクエストの
    ソケットだけを閉じるため他ペルソナの通信には影響しない。
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if hasattr(self._pool, "_network_backend"):
            self._pool._network_backend = _CancellableSyncBackend()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        # リトライの再送も含め、送信前に必ず停止を確認する。
        raise_if_llm_stopped()
        try:
            response = super().handle_request(request)
        except Exception:
            raise_if_llm_stopped()
            raise
        if current_llm_stop_reason() is not None:
            try:
                response.close()
            except Exception:  # noqa: BLE001
                pass
            raise_if_llm_stopped()
        return response


def cancellable_sleep(seconds: float) -> None:
    """リトライ待機用の sleep。停止合図が届いたら即座に ExecutionCancelledException を投げる。"""
    token = current_llm_cancellation_token()
    if token is None:
        time.sleep(seconds)
        return
    if token.wait_for_stop(seconds):
        raise_if_llm_stopped()


try:  # pragma: no cover - httpx2 is installed alongside anthropic 1.x
    import httpcore2
    import httpx2
    _HAS_HTTPX2 = True
except Exception:  # noqa: BLE001
    httpcore2 = None  # type: ignore[assignment]
    httpx2 = None  # type: ignore[assignment]
    _HAS_HTTPX2 = False


if _HAS_HTTPX2:
    class _CancellableSyncStream2(httpcore2.NetworkStream):
        """httpx2 / httpcore2 版のキャンセル対応 NetworkStream。"""

        def __init__(self, inner: httpcore2.NetworkStream) -> None:
            self._inner = inner

        def _run_cancellable(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
            token = current_llm_cancellation_token()
            if token is None:
                return fn(*args, **kwargs)
            raise_if_llm_stopped()
            unregister = token.register_on_stop(lambda _reason: _abort_stream(self._inner))
            try:
                return fn(*args, **kwargs)
            except Exception:
                raise_if_llm_stopped()
                raise
            finally:
                unregister()

        def read(self, max_bytes: int, timeout: Optional[float] = None) -> bytes:
            return self._run_cancellable(self._inner.read, max_bytes, timeout=timeout)

        def write(self, buffer: bytes, timeout: Optional[float] = None) -> None:
            self._run_cancellable(self._inner.write, buffer, timeout=timeout)

        def close(self) -> None:
            self._inner.close()

        def start_tls(
            self,
            ssl_context: ssl.SSLContext,
            server_hostname: Optional[str] = None,
            timeout: Optional[float] = None,
        ) -> httpcore2.NetworkStream:
            tls_stream = self._run_cancellable(
                self._inner.start_tls,
                ssl_context,
                server_hostname=server_hostname,
                timeout=timeout,
            )
            return _CancellableSyncStream2(tls_stream)

        def get_extra_info(self, info: str) -> Any:
            return self._inner.get_extra_info(info)

    class _CancellableSyncBackend2(httpcore2.SyncBackend):
        """httpx2 / httpcore2 版のキャンセル対応 SyncBackend。"""

        def connect_tcp(
            self,
            host: str,
            port: int,
            timeout: Optional[float] = None,
            local_address: Optional[str] = None,
            socket_options: Any = None,
        ) -> httpcore2.NetworkStream:
            raise_if_llm_stopped()
            stream = super().connect_tcp(
                host,
                port,
                timeout=timeout,
                local_address=local_address,
                socket_options=socket_options,
            )
            if current_llm_stop_reason() is not None:
                _abort_stream(stream)
                raise_if_llm_stopped()
            return _CancellableSyncStream2(stream)

        def connect_unix_socket(
            self,
            path: str,
            timeout: Optional[float] = None,
            socket_options: Any = None,
        ) -> httpcore2.NetworkStream:
            raise_if_llm_stopped()
            stream = super().connect_unix_socket(
                path,
                timeout=timeout,
                socket_options=socket_options,
            )
            if current_llm_stop_reason() is not None:
                _abort_stream(stream)
                raise_if_llm_stopped()
            return _CancellableSyncStream2(stream)

    class CancellableHTTP2Transport(httpx2.HTTPTransport):
        """停止合図に即応する ``httpx2.HTTPTransport`` (Anthropic 1.x 用)。"""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            if hasattr(self._pool, "_network_backend"):
                self._pool._network_backend = _CancellableSyncBackend2()

        def handle_request(self, request: httpx2.Request) -> httpx2.Response:
            raise_if_llm_stopped()
            try:
                response = super().handle_request(request)
            except Exception:
                raise_if_llm_stopped()
                raise
            if current_llm_stop_reason() is not None:
                try:
                    response.close()
                except Exception:  # noqa: BLE001
                    pass
                raise_if_llm_stopped()
            return response
else:  # pragma: no cover
    CancellableHTTP2Transport = None  # type: ignore[assignment,misc]


__all__ = [
    "CancellableHTTPTransport",
    "CancellableHTTP2Transport",
    "cancellable_sleep",
]
