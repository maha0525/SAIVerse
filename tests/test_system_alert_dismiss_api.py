"""画面上部のお知らせを閉じる API の HTTP テスト。

閉じたことを画面の中だけで覚えると、ブラウザを開き直すたびに同じお知らせが
また出る (2026-10-30、移行の完了のお知らせで起きた)。閉じたら、この起動の
あいだは閉じたままにする。お知らせそのものは消さない (設定の確認の画面の入口が
お知らせの有無で出し入れするので、消すとバナーを閉じた利用者が入る道まで消える)。
ここで固定するのは:

- 閉じたお知らせは、一覧に ``dismissed: true`` 付きで残る
- ほかのお知らせは ``dismissed: false``
- 件数の変化で同じ id のお知らせが置き直されても、閉じたまま
- 知らない id を閉じても、何も変えずに成功する
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import system as system_routes
from saiverse import app_state


class SystemAlertDismissApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = SimpleNamespace(startup_alerts=[
            {"id": "system_notice_migration_done", "level": "info", "title": "done", "message": "m"},
            {"id": "system_notice_review_pending", "level": "warning", "title": "review", "message": "m"},
        ])
        self._prev_manager = app_state.manager
        app_state.manager = self.manager
        self.addCleanup(self._restore_manager)
        app = FastAPI()
        app.include_router(system_routes.router, prefix="/api/system")
        self.client = TestClient(app)

    def _restore_manager(self) -> None:
        app_state.manager = self._prev_manager

    def _listed(self) -> list:
        res = self.client.get("/api/system/alerts")
        self.assertEqual(res.status_code, 200)
        return [(a["id"], a["dismissed"]) for a in res.json()["alerts"]]

    def test_a_closed_alert_stays_listed_but_marked(self) -> None:
        self.assertEqual(self._listed(), [
            ("system_notice_migration_done", False), ("system_notice_review_pending", False),
        ])
        res = self.client.post("/api/system/alerts/system_notice_review_pending/dismiss")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"dismissed": True})
        self.assertEqual(self._listed(), [
            ("system_notice_migration_done", False), ("system_notice_review_pending", True),
        ])
        # 確認待ちの件数が変わってお知らせが置き直されても、閉じたまま
        self.manager.startup_alerts = [
            self.manager.startup_alerts[0],
            {"id": "system_notice_review_pending", "level": "warning", "title": "review 2", "message": "m"},
        ]
        self.assertEqual(self._listed()[1], ("system_notice_review_pending", True))

    def test_closing_an_unknown_alert_changes_nothing(self) -> None:
        res = self.client.post("/api/system/alerts/no_such_alert/dismiss")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"dismissed": False})
        self.assertEqual(self._listed(), [
            ("system_notice_migration_done", False), ("system_notice_review_pending", False),
        ])

    def test_not_ready_manager_is_503(self) -> None:
        app_state.manager = None
        res = self.client.post("/api/system/alerts/system_notice_migration_done/dismiss")
        self.assertEqual(res.status_code, 503)


if __name__ == "__main__":
    unittest.main()
