"""読めなくなった旧履歴ファイルを脇へ移す API の HTTP テスト。

直せないものを毎起動バナーで出し続けると、そのうち全部のバナーが読み飛ばされ、
検算という仕組み自体が死ぬ。ユーザーが「分かりました」と言える経路として、
ファイルを脇へ移すボタンを置いた (2026-08-16 まはー裁定)。

ここで固定するのは:

- 押すとファイルが同じフォルダで改名され、バナーが消える (削除はしない)
- 改名後の名前は log.json ではないので、次の起動の検算はもう拾わない
- 「読めない」と報告されていない部屋は動かせない (任意のパスを動かす口にしない)
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import system as system_routes
from saiverse import app_state


class LegacyLogArchiveApiTest(unittest.TestCase):
    BID = "room1"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.log_path = Path(self._tmp.name) / "log.json"
        self.log_path.write_text("{壊れている", encoding="utf-8")

        self.manager = SimpleNamespace(startup_alerts=[self._alert(self.BID)])
        self._prev_manager = app_state.manager
        app_state.manager = self.manager
        self.addCleanup(self._restore_manager)

        app = FastAPI()
        app.include_router(system_routes.router, prefix="/api/system")
        self.client = TestClient(app)

    def _restore_manager(self) -> None:
        app_state.manager = self._prev_manager

    def _alert(self, building_id: str) -> dict:
        return {
            "id": f"legacy_log_deficit_{building_id}",
            "level": "warning",
            "title": "過去ログが未取込",
            "message": "壊れています",
            "details": {
                "building_id": building_id,
                "kind": "unreadable",
                "reason": "JSON parse 失敗",
                "path": str(self.log_path),
            },
        }

    def _archive(self, building_id: str):
        return self.client.post(f"/api/system/legacy-log/{building_id}/archive")

    # ------------------------------------------------------------------

    def test_archive_renames_the_file_and_clears_the_banner(self) -> None:
        res = self._archive(self.BID)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["success"])

        self.assertFalse(self.log_path.exists())
        moved = list(Path(self._tmp.name).glob("log.json.unreadable_*"))
        self.assertEqual(len(moved), 1)
        # 中身は消さない — 後で救う手が見つかれば使える
        self.assertEqual(moved[0].read_text(encoding="utf-8"), "{壊れている")
        # 改名後は log.json ではないので、次の起動の検算はもう拾わない
        self.assertNotEqual(moved[0].name, "log.json")
        self.assertEqual(self.manager.startup_alerts, [])

    def test_only_the_targeted_building_alert_is_cleared(self) -> None:
        other = self._alert("room2")
        other["details"]["path"] = str(Path(self._tmp.name) / "other.json")
        self.manager.startup_alerts.append(other)

        self._archive(self.BID)
        remaining = [a["details"]["building_id"] for a in self.manager.startup_alerts]
        self.assertEqual(remaining, ["room2"])

    def test_building_without_an_unreadable_alert_is_refused(self) -> None:
        self.manager.startup_alerts[0]["details"]["kind"] = "not_imported"
        res = self._archive(self.BID)
        self.assertEqual(res.status_code, 404)
        self.assertTrue(self.log_path.exists())

    def test_unknown_building_is_refused(self) -> None:
        res = self._archive("no_such_room")
        self.assertEqual(res.status_code, 404)
        self.assertTrue(self.log_path.exists())

    def test_already_missing_file_just_closes_the_banner(self) -> None:
        self.log_path.unlink()
        res = self._archive(self.BID)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.manager.startup_alerts, [])


if __name__ == "__main__":
    unittest.main()
