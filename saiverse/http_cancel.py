"""停止ボタン / 終了処理で待機中の HTTP 通信とリトライを即座に断つための部品。

docs/intent/stop_button.md の「LLM サーバーからの応答待ち・リトライ待機中も
停止で即座に抜ける」契約を実現する。

- ``install_cancellable_transport`` / ``install_cancellable_transport2``:
  既存の ``httpx.Client`` / ``httpx2.Client`` が環境変数 (``HTTP_PROXY`` /
  ``HTTPS_PROXY`` / ``ALL_PROXY`` / ``NO_PROXY``) や SSL 設定から構築した
  ``_transport`` と ``_mounts`` (プロキシ用 ``HTTPProxy`` / ``SOCKSProxy`` を
  含む) をそのまま活かしつつ、すべてのネットワーク層をキャンセル対応化する。
- ``_CancellableSyncStream``: 現在の文脈に紐づく ``CancellationToken`` に
  ``user_stop`` / ``server_shutdown`` が届いた瞬間に、そのリクエストのソケット
  だけを ``shutdown(SHUT_RDWR)`` + ``close()`` してブロック中の ``recv`` /
  ``send`` / TLS ハンドシェイク (``do_handshake``) を即座に解除する。
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
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Optional

import httpcore
import httpx
from httpcore._backends import sync as _hc_sync

LOGGER = logging.getLogger(__name__)

#: 現在実行中の LLM 呼び出しに紐づく CancellationToken (型依存を避けるため Any)。
#: ``sea`` や ``llm_clients`` を import せずに共有できるよう、完全なリーフ層に置く。
_ACTIVE_LLM_CANCEL_TOKEN: ContextVar[Optional[Any]] = ContextVar(
    "active_llm_cancel_token", default=None,
)


def current_llm_cancellation_token() -> Optional[Any]:
    """Return the CancellationToken bound to the current LLM call, if any."""
    return _ACTIVE_LLM_CANCEL_TOKEN.get()


def current_llm_stop_reason() -> Optional[str]:
    """Return interrupted_by if the active LLM call received user_stop / server_shutdown."""
    token = _ACTIVE_LLM_CANCEL_TOKEN.get()
    if token is not None and token.is_stopped():
        return token.interrupted_by
    return None


def raise_if_llm_stopped() -> None:
    """Raise ExecutionCancelledException if the active LLM call was stopped."""
    token = _ACTIVE_LLM_CANCEL_TOKEN.get()
    if token is not None and token.is_stopped():
        token.raise_if_stopped()


@contextmanager
def bind_llm_cancellation_token(token: Optional[Any]) -> Iterator[None]:
    """Bind ``token`` to the current execution context for the duration of an LLM call."""
    if token is None:
        yield
        return
    ctx_token = _ACTIVE_LLM_CANCEL_TOKEN.set(token)
    try:
        yield
    finally:
        _ACTIVE_LLM_CANCEL_TOKEN.reset(ctx_token)


def _abort_socket(sock: Any) -> None:
    """ソケット (通常ソケット / SSLSocket) を即座に閉じる。"""
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def _abort_stream(inner: Any) -> None:
    """待機中のソケットだけを即座に閉じる (他スレッドの接続には触れない)。"""
    sock = None
    try:
        sock = inner.get_extra_info("socket")
    except Exception:  # noqa: BLE001
        sock = None

    if sock is not None:
        _abort_socket(sock)
        return

    try:
        inner.close()
    except Exception:  # noqa: BLE001
        pass


def _start_tls_cancellable(
    inner: Any,
    ssl_context: ssl.SSLContext,
    server_hostname: Optional[str],
    timeout: Optional[float],
    *,
    sync_stream_cls: Any,
    connect_timeout_cls: Any,
    connect_error_cls: Any,
    wrapper_cls: Any,
) -> Any:
    """TLS ハンドシェイク中も実際の SSLSocket に停止コールバックを登録して即切断する。

    ``SSLContext.wrap_socket()`` の既定動作 (``do_handshake_on_connect=True``) は
    元ソケットから fd を切り離してからハンドシェイクするため、元ソケットを閉じても
    ハンドシェイク中の ``SSLSocket`` には届かない。先に ``do_handshake_on_connect=False``
    で ``SSLSocket`` を作り、その ``SSLSocket`` に停止コールバックを張ってから
    ``do_handshake()`` を実行する。
    """
    token = current_llm_cancellation_token()
    if token is None:
        return wrapper_cls(
            inner.start_tls(
                ssl_context,
                server_hostname=server_hostname,
                timeout=timeout,
            )
        )

    raise_if_llm_stopped()
    raw_sock = None
    try:
        raw_sock = inner.get_extra_info("socket")
    except Exception:  # noqa: BLE001
        raw_sock = None

    if raw_sock is None or isinstance(raw_sock, ssl.SSLSocket):
        # TLS-in-TLS やソケット非公開ストリームは既存の start_tls に委譲
        unregister = token.register_on_stop(lambda _reason: _abort_stream(inner))
        try:
            tls_stream = inner.start_tls(
                ssl_context,
                server_hostname=server_hostname,
                timeout=timeout,
            )
        except Exception:
            raise_if_llm_stopped()
            raise
        finally:
            unregister()
        return wrapper_cls(tls_stream)

    ssl_sock: Optional[ssl.SSLSocket] = None
    try:
        raw_sock.settimeout(timeout)
        inner_ctx = getattr(ssl_context, "_ctx", None)
        ctx_lock = getattr(ssl_context, "_ctx_lock", None)
        if inner_ctx is not None and ctx_lock is not None and type(ssl_context).__module__.startswith("truststore"):
            # truststore.SSLContext.wrap_socket() は do_handshake_on_connect=False でも
            # 戻り際に _verify_peercerts() を呼んでしまうため、内部 ctx で先に
            # SSLSocket を作って停止コールバックを張り、ハンドシェイク完了後に検証する。
            import contextlib
            from truststore import _api as _truststore_api

            with contextlib.ExitStack() as stack:
                with ctx_lock:
                    stack.enter_context(_truststore_api._configure_context(inner_ctx))
                ssl_sock = inner_ctx.wrap_socket(
                    raw_sock,
                    server_hostname=server_hostname,
                    do_handshake_on_connect=False,
                )
            unregister = token.register_on_stop(lambda _reason: _abort_socket(ssl_sock))
            try:
                ssl_sock.settimeout(timeout)
                ssl_sock.do_handshake()
            finally:
                unregister()
            _truststore_api._verify_peercerts(ssl_sock, server_hostname=server_hostname)
        else:
            ssl_sock = ssl_context.wrap_socket(
                raw_sock,
                server_hostname=server_hostname,
                do_handshake_on_connect=False,
            )
            unregister = token.register_on_stop(lambda _reason: _abort_socket(ssl_sock))
            try:
                ssl_sock.settimeout(timeout)
                ssl_sock.do_handshake()
            finally:
                unregister()
    except Exception as exc:
        if ssl_sock is not None:
            _abort_socket(ssl_sock)
        else:
            _abort_stream(inner)
        raise_if_llm_stopped()
        if isinstance(exc, socket.timeout):
            raise connect_timeout_cls(exc) from exc
        if isinstance(exc, OSError):
            raise connect_error_cls(exc) from exc
        raise

    return wrapper_cls(sync_stream_cls(ssl_sock))


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
        return _start_tls_cancellable(
            self._inner,
            ssl_context,
            server_hostname,
            timeout,
            sync_stream_cls=_hc_sync.SyncStream,
            connect_timeout_cls=httpcore.ConnectTimeout,
            connect_error_cls=httpcore.ConnectError,
            wrapper_cls=_CancellableSyncStream,
        )

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


class _CancellableTransportProxy(httpx.BaseTransport):
    """既存の httpx Transport (直接接続・プロキシ双方) に停止前検査と例外正規化を被せる。"""

    def __init__(self, inner: httpx.BaseTransport) -> None:
        self._inner = inner
        pool = getattr(inner, "_pool", None)
        if pool is not None and hasattr(pool, "_network_backend"):
            pool._network_backend = _CancellableSyncBackend()

    @property
    def _pool(self) -> Any:
        return getattr(self._inner, "_pool", None)

    @_pool.setter
    def _pool(self, value: Any) -> None:
        self._inner._pool = value

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        raise_if_llm_stopped()
        try:
            response = self._inner.handle_request(request)
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

    def close(self) -> None:
        self._inner.close()


class CancellableHTTPTransport(httpx.HTTPTransport):
    """停止合図に即応する ``httpx.HTTPTransport`` (直接利用・テスト用)。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if hasattr(self._pool, "_network_backend"):
            self._pool._network_backend = _CancellableSyncBackend()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
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


def install_cancellable_transport(client: Any) -> Any:
    """既存の ``httpx.Client`` にキャンセル対応トランスポートを組み込む。

    環境変数由来のプロキシ設定 (``_mounts``) や SSL 設定をそのまま維持したうえで、
    直接接続・プロキシ接続のすべてを停止合図で即切断できるようにする。
    """
    if client is None:
        return client
    transport = getattr(client, "_transport", None)
    if transport is not None and not isinstance(
        transport, (_CancellableTransportProxy, CancellableHTTPTransport)
    ):
        client._transport = _CancellableTransportProxy(transport)
    mounts = getattr(client, "_mounts", None)
    if isinstance(mounts, dict):
        for pattern, mounted in list(mounts.items()):
            if mounted is not None and not isinstance(
                mounted, (_CancellableTransportProxy, CancellableHTTPTransport)
            ):
                mounts[pattern] = _CancellableTransportProxy(mounted)
    return client


def cancellable_sleep(seconds: float) -> None:
    """リトライ待機用の sleep。停止合図が届いたら即座に ExecutionCancelledException を投げる。"""
    token = current_llm_cancellation_token()
    if token is None:
        time.sleep(seconds)
        return
    if token.wait_for_stop(seconds):
        raise_if_llm_stopped()


try:  # pragma: no cover - httpx2 is installed alongside anthropic 1.x / openai 3.x
    import httpcore2
    import httpx2
    from httpcore2._backends import sync as _hc2_sync

    _HAS_HTTPX2 = True
except Exception:  # noqa: BLE001
    httpcore2 = None  # type: ignore[assignment]
    httpx2 = None  # type: ignore[assignment]
    _hc2_sync = None  # type: ignore[assignment]
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
            return _start_tls_cancellable(
                self._inner,
                ssl_context,
                server_hostname,
                timeout,
                sync_stream_cls=_hc2_sync.SyncStream,
                connect_timeout_cls=httpcore2.ConnectTimeout,
                connect_error_cls=httpcore2.ConnectError,
                wrapper_cls=_CancellableSyncStream2,
            )

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

    class _CancellableTransportProxy2(httpx2.BaseTransport):
        """既存の httpx2 Transport (直接接続・プロキシ双方) に停止対応を被せる。"""

        def __init__(self, inner: httpx2.BaseTransport) -> None:
            self._inner = inner
            pool = getattr(inner, "_pool", None)
            if pool is not None and hasattr(pool, "_network_backend"):
                pool._network_backend = _CancellableSyncBackend2()

        @property
        def _pool(self) -> Any:
            return getattr(self._inner, "_pool", None)

        def handle_request(self, request: httpx2.Request) -> httpx2.Response:
            raise_if_llm_stopped()
            try:
                response = self._inner.handle_request(request)
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

        def close(self) -> None:
            self._inner.close()

    class CancellableHTTP2Transport(httpx2.HTTPTransport):
        """停止合図に即応する ``httpx2.HTTPTransport`` (直接利用・テスト用)。"""

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

    def install_cancellable_transport2(client: Any) -> Any:
        """既存の ``httpx2.Client`` にキャンセル対応トランスポートを組み込む。"""
        if client is None:
            return client
        transport = getattr(client, "_transport", None)
        if transport is not None and not isinstance(
            transport, (_CancellableTransportProxy2, CancellableHTTP2Transport)
        ):
            client._transport = _CancellableTransportProxy2(transport)
        mounts = getattr(client, "_mounts", None)
        if isinstance(mounts, dict):
            for pattern, mounted in list(mounts.items()):
                if mounted is not None and not isinstance(
                    mounted, (_CancellableTransportProxy2, CancellableHTTP2Transport)
                ):
                    mounts[pattern] = _CancellableTransportProxy2(mounted)
        return client
else:  # pragma: no cover
    CancellableHTTP2Transport = None  # type: ignore[assignment,misc]

    def install_cancellable_transport2(client: Any) -> Any:
        return client


__all__ = [
    "CancellableHTTPTransport",
    "CancellableHTTP2Transport",
    "install_cancellable_transport",
    "install_cancellable_transport2",
    "cancellable_sleep",
    "bind_llm_cancellation_token",
    "current_llm_cancellation_token",
    "current_llm_stop_reason",
    "raise_if_llm_stopped",
]
