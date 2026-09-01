"""OS の証明書ストアが空の環境で、同梱の証明書束へ退避すること。

macOS の Python は OpenSSL からキーチェーンを読めず、``create_default_context()``
が証明書を 1 枚も持たない。その状態の ``urlopen`` はどこへ繋いでも
``CERTIFICATE_VERIFY_FAILED`` で落ちる (2026-09-02、実ユーザーの macOS 環境で
「読めた証明書 0 個」を実測)。

ここで固定するのは 3 つ:

- OS 側が読めているときは **何もしない** (通信を検査している環境を壊さない)
- 読めないときは同梱の certifi へ切り替える
- 切り替えたあと、``urlopen`` が実際にその証明書束を使う
  (差し替えた口を本当に urllib が見ているか、ローカルの HTTPS で確かめる)
"""
from __future__ import annotations

import datetime
import http.server
import os
import ssl
import threading
import unittest
from unittest.mock import patch

from saiverse import tls_trust

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    HAVE_CRYPTOGRAPHY = True
except ImportError:  # pragma: no cover - 依存が無い環境
    HAVE_CRYPTOGRAPHY = False


class _TrustStateMixin:
    """urllib の既定 opener とプロセス環境への差し替えを、テストごとに戻す。"""

    def _keep_global_trust_state(self) -> None:
        import urllib.request

        original_opener = urllib.request._opener
        original_env = os.environ.get("SSL_CERT_FILE")

        def restore() -> None:
            urllib.request._opener = original_opener
            if original_env is None:
                os.environ.pop("SSL_CERT_FILE", None)
            else:
                os.environ["SSL_CERT_FILE"] = original_env

        self.addCleanup(restore)


class TlsTrustDecisionTests(_TrustStateMixin, unittest.TestCase):
    def setUp(self) -> None:
        self._keep_global_trust_state()

    def test_does_nothing_when_the_os_store_is_usable(self) -> None:
        """読める環境では触らない。

        通信を検査しているソフトや機器がいる環境では、その中間 CA は OS の
        ストアにだけ入っている。無条件に差し替えると、いま繋がっている環境を
        壊す方向の変更になる。
        """
        import urllib.request

        sentinel = object()
        urllib.request._opener = sentinel
        with patch.object(tls_trust, "_os_trust_store_is_usable", return_value=True):
            self.assertIsNone(tls_trust.ensure_default_https_trust())
        self.assertIs(urllib.request._opener, sentinel)

    def test_falls_back_to_the_bundled_store_when_empty(self) -> None:
        import urllib.request

        urllib.request._opener = None
        with patch.object(tls_trust, "_os_trust_store_is_usable", return_value=False):
            cafile = tls_trust.ensure_default_https_trust()
        self.assertIsNotNone(cafile)
        self.assertTrue(os.path.exists(cafile))
        self.assertEqual(os.environ.get("SSL_CERT_FILE"), cafile)
        self.assertIsNotNone(
            urllib.request._opener, "urlopen が通る opener が入っていない"
        )

    def test_keeps_an_existing_ssl_cert_file_setting(self) -> None:
        """利用者が自分で指定していたら、そちらを尊重する。"""
        os.environ["SSL_CERT_FILE"] = "/somewhere/custom.pem"
        with patch.object(tls_trust, "_os_trust_store_is_usable", return_value=False):
            tls_trust.ensure_default_https_trust()
        self.assertEqual(os.environ["SSL_CERT_FILE"], "/somewhere/custom.pem")


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, "cryptography が無いので証明書を作れない")
class TlsTrustReachesUrlopenTests(_TrustStateMixin, unittest.TestCase):
    """差し替えた既定を urllib が本当に使うかを、ローカルの HTTPS で確かめる。

    外部へは接続しない。127.0.0.1 に立てた使い捨てのサーバーだけを相手にする。
    """

    def setUp(self) -> None:
        self._keep_global_trust_state()
        self.cert_path, key_path = self._make_self_signed()
        self.httpd = self._start_server(self.cert_path, key_path)
        self.addCleanup(self.httpd.shutdown)
        self.url = "https://localhost:%d/" % self.httpd.server_address[1]

    def _make_self_signed(self):
        import tempfile
        from pathlib import Path

        tmp = tempfile.TemporaryDirectory(prefix="tls_trust_test_")
        self.addCleanup(tmp.cleanup)
        work = Path(tmp.name)

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName("localhost")]), False
            )
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
            .sign(key, hashes.SHA256())
        )
        cert_path = work / "server.pem"
        key_path = work / "server.key"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
        return cert_path, key_path

    @staticmethod
    def _start_server(cert_path, key_path):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler の規約
                body = b"ok"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert_path), str(key_path))
        httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd

    def test_urlopen_succeeds_after_the_fallback(self) -> None:
        import urllib.error
        import urllib.request

        # この証明書はどこにも登録されていないので、まずは失敗する側を見る。
        with self.assertRaises(urllib.error.URLError):
            urllib.request.urlopen(self.url, timeout=10)

        with patch.object(
            tls_trust, "_os_trust_store_is_usable", return_value=False
        ), patch("certifi.where", return_value=str(self.cert_path)):
            tls_trust.ensure_default_https_trust()

        with urllib.request.urlopen(self.url, timeout=10) as resp:
            self.assertEqual(resp.read(), b"ok")


if __name__ == "__main__":
    unittest.main()
