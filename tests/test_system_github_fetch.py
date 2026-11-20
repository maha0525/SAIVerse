"""GitHub への問い合わせが、バックエンドの受付 (イベントループ) を止めないこと。

お知らせ (/api/system/announcements) と更新確認 (/api/system/version) は
GitHub へ同期で問い合わせる。``async def`` のままだとイベントループの上で
待つので、その間は返事のストリーミングの ping も停止ボタンも捌けず、画面は
15 秒の沈黙で「通信が途中で切れました」を出していた。

- 問い合わせを待っている最中でも、ほかの要求が返ってくること
- 失敗した問い合わせを一定時間覚えて、呼ばれるたびに取り直さないこと
"""

from __future__ import annotations

import threading
import unittest
from unittest.mock import patch
from urllib.error import URLError

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import system as system_routes


def _reset_fetch_state() -> None:
    system_routes._cached_latest = None
    system_routes._cached_at = 0.0
    system_routes._latest_failed_at = 0.0
    system_routes._cached_release_list = None
    system_routes._cached_release_list_at = 0.0
    system_routes._release_list_failed_at = 0.0
    system_routes._cached_announcements = None
    system_routes._cached_announcements_at = 0.0
    system_routes._announcements_failed_at = 0.0


class _FetchStateCase(unittest.TestCase):
    def setUp(self) -> None:
        _reset_fetch_state()
        self.addCleanup(_reset_fetch_state)


class GitHubWaitDoesNotStallOtherRequestsTest(_FetchStateCase):
    """一つのイベントループを共有するクライアントで、待ちの最中に別の要求を通す。"""

    def _assert_other_request_served_while_waiting(self, path: str, fetcher: str) -> None:
        app = FastAPI()
        app.include_router(system_routes.router, prefix="/api/system")

        entered = threading.Event()
        release = threading.Event()

        def blocking_fetch(*_args, **_kwargs):
            entered.set()
            # 問い合わせが返らない状態を作る。テストの終わりで必ず解く。
            release.wait(timeout=10)
            return None

        with TestClient(app) as client, patch.object(system_routes, fetcher, side_effect=blocking_fetch):
            waiting = threading.Thread(target=lambda: client.get(path), daemon=True)
            waiting.start()
            try:
                self.assertTrue(entered.wait(timeout=5), "the GitHub fetch was never reached")

                done = threading.Event()
                result: dict = {}

                def other() -> None:
                    result["status"] = client.get("/api/system/alerts").status_code
                    done.set()

                threading.Thread(target=other, daemon=True).start()
                self.assertTrue(
                    done.wait(timeout=3),
                    f"another request stalled while {path} was waiting on GitHub",
                )
                self.assertEqual(result["status"], 200)
            finally:
                release.set()
                waiting.join(timeout=5)

    def test_announcements(self) -> None:
        self._assert_other_request_served_while_waiting(
            "/api/system/announcements", "_fetch_announcements",
        )

    def test_version(self) -> None:
        self._assert_other_request_served_while_waiting(
            "/api/system/version", "_fetch_latest_release",
        )


class FailedFetchIsRememberedTest(_FetchStateCase):
    def test_announcements_failure_is_not_retried_immediately(self) -> None:
        with patch.object(system_routes, "urlopen", side_effect=URLError("down")) as opened:
            self.assertIsNone(system_routes._fetch_announcements())
            self.assertIsNone(system_routes._fetch_announcements())
        self.assertEqual(opened.call_count, 1)

    def test_announcements_failure_keeps_serving_the_stale_copy(self) -> None:
        stale = {"announcements": [{"id": "a"}]}
        system_routes._cached_announcements = stale
        system_routes._cached_announcements_at = 1.0  # expired long ago
        with patch.object(system_routes, "urlopen", side_effect=URLError("down")) as opened:
            self.assertIs(system_routes._fetch_announcements(), stale)
            self.assertIs(system_routes._fetch_announcements(), stale)
        self.assertEqual(opened.call_count, 1)

    def test_latest_release_failure_is_not_retried_immediately(self) -> None:
        with patch.object(system_routes, "urlopen", side_effect=URLError("down")) as opened:
            self.assertIsNone(system_routes._fetch_latest_release())
            self.assertIsNone(system_routes._fetch_latest_release())
        self.assertEqual(opened.call_count, 1)

    def test_release_list_failure_is_not_retried_immediately(self) -> None:
        with patch.object(system_routes, "urlopen", side_effect=URLError("down")) as opened:
            self.assertIsNone(system_routes._fetch_release_list())
            self.assertIsNone(system_routes._fetch_release_list())
        self.assertEqual(opened.call_count, 1)

    def test_failure_is_retried_after_the_backoff(self) -> None:
        with patch.object(system_routes, "urlopen", side_effect=URLError("down")) as opened, patch.object(
            system_routes.time, "time", side_effect=[1000.0, 1000.0 + system_routes._FETCH_FAILURE_BACKOFF + 1],
        ):
            system_routes._fetch_latest_release()
            system_routes._fetch_latest_release()
        self.assertEqual(opened.call_count, 2)


if __name__ == "__main__":
    unittest.main()
