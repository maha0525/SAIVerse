"""Tests for ``tools.mcp_client._classify_error``.

分類の結果はそのまま利用者の画面に出る。「不明なエラー」と言われた人は
自分の設定を疑うしかなくなるので、**分類できるものを分類できないまま出さない**
ことが機能要件になる。

2026-08-30、Elyth (Remote MCP) の接続先が 503 を返したとき、画面には
「不明なエラー（詳細: unhandled errors in a TaskGroup (1 sub-exception)）」と
出た。原因は anyio / TaskGroup が本当の失敗を ``ExceptionGroup`` に包んで投げ、
包み自身は上の一文しか名乗らないこと。文字列だけを見る分類器は、**中身が何で
あれ必ず unknown に落ちていた**。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.mcp_client import (  # noqa: E402
    ERROR_CATEGORY_AUTH_FAILED,
    ERROR_CATEGORY_NETWORK,
    ERROR_CATEGORY_RUNTIME_MISSING,
    ERROR_CATEGORY_SERVICE_UNAVAILABLE,
    ERROR_CATEGORY_UNKNOWN,
    _classify_error,
)


class ExceptionGroupUnwrappingTests(unittest.TestCase):
    """包みを剥がして中の例外で判定する。"""

    def test_taskgroup_wrapped_503_is_not_unknown(self):
        """今回の実害そのもの。包んだまま見ると unknown に落ちる。"""
        inner = RuntimeError("Server error '503 Service Unavailable' for url ...")
        wrapped = ExceptionGroup("unhandled errors in a TaskGroup", [inner])

        self.assertEqual(_classify_error(wrapped), ERROR_CATEGORY_SERVICE_UNAVAILABLE)

    def test_nested_groups_are_unwrapped(self):
        """包みが二重でも中身に辿り着く。"""
        inner = RuntimeError("401 Unauthorized")
        wrapped = ExceptionGroup(
            "outer", [ExceptionGroup("inner", [inner])],
        )

        self.assertEqual(_classify_error(wrapped), ERROR_CATEGORY_AUTH_FAILED)

    def test_first_classifiable_member_wins(self):
        """分類できない兄弟が先に居ても、分類できるものを拾う。"""
        wrapped = ExceptionGroup(
            "mixed",
            [RuntimeError("something odd"), RuntimeError("503 Service Unavailable")],
        )

        self.assertEqual(_classify_error(wrapped), ERROR_CATEGORY_SERVICE_UNAVAILABLE)

    def test_group_of_unclassifiable_stays_unknown(self):
        """中身も分からないなら unknown。ここで嘘の分類を作らない。"""
        wrapped = ExceptionGroup("odd", [RuntimeError("something odd")])

        self.assertEqual(_classify_error(wrapped), ERROR_CATEGORY_UNKNOWN)


class ServiceUnavailableTests(unittest.TestCase):
    """502/503/504 は「向こうが今は応答できない」。設定の誤りと分けて出す。"""

    def test_503_status_text(self):
        exc = RuntimeError("Server error '503 Service Unavailable' for url ...")

        self.assertEqual(_classify_error(exc), ERROR_CATEGORY_SERVICE_UNAVAILABLE)

    def test_502_and_504(self):
        for text in ("502 Bad Gateway", "504 Gateway Timeout"):
            with self.subTest(text=text):
                self.assertEqual(
                    _classify_error(RuntimeError(text)),
                    ERROR_CATEGORY_SERVICE_UNAVAILABLE,
                )

    def test_wording_without_a_number(self):
        exc = RuntimeError("upstream said: service unavailable, try later")

        self.assertEqual(_classify_error(exc), ERROR_CATEGORY_SERVICE_UNAVAILABLE)

    def test_a_port_number_is_not_a_status_code(self):
        """`:8503` のような数字を状態コードと読み違えない。"""
        exc = RuntimeError("could not connect to http://127.0.0.1:8503")

        self.assertNotEqual(_classify_error(exc), ERROR_CATEGORY_SERVICE_UNAVAILABLE)

    def test_a_three_digit_port_is_not_a_status_code(self):
        """ポート番号がちょうど 502/503/504 のときが本当の境目。

        4 桁 (8503) だけを見ていると、数字そのものが状態コードと同じ 3 桁の
        ケースを取りこぼす — Codex の指摘 (2026-08-30)。
        """
        for port in ("502", "503", "504"):
            with self.subTest(port=port):
                exc = RuntimeError(f"connection refused: http://127.0.0.1:{port}/mcp")
                self.assertNotEqual(
                    _classify_error(exc), ERROR_CATEGORY_SERVICE_UNAVAILABLE,
                )

    def test_gateway_timeout_is_not_swallowed_by_the_timeout_rule(self):
        """"timeout" を含むが、これはネットワーク不通ではなく向こうの応答。"""
        exc = RuntimeError("Server error '504 Gateway Timeout' for url ...")

        self.assertEqual(_classify_error(exc), ERROR_CATEGORY_SERVICE_UNAVAILABLE)


class ExistingCategoriesStillWorkTests(unittest.TestCase):
    """新しい分岐を前に差し込んだので、既存の判定を巻き込んでいないこと。"""

    def test_auth_failure(self):
        self.assertEqual(
            _classify_error(RuntimeError("401 Unauthorized")),
            ERROR_CATEGORY_AUTH_FAILED,
        )

    def test_plain_timeout_is_still_network(self):
        self.assertEqual(
            _classify_error(RuntimeError("request timed out")),
            ERROR_CATEGORY_NETWORK,
        )

    def test_missing_runtime(self):
        self.assertEqual(
            _classify_error(FileNotFoundError("uvx")),
            ERROR_CATEGORY_RUNTIME_MISSING,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
