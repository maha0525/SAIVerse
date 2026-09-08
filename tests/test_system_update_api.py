"""UI 更新エンドポイントの psutil 事前検査の HTTP テスト。

UI 更新で走るアップデータは更新前のチェックアウトのもの。psutil の無い環境では
アップデータの終了待ちが fail-closed で中止し、バックエンドだけが落ちて戻らない
(docs/issues/self_update_unsafe_without_psutil.md)。断るなら本体が生きている
うちに断る — ここで固定するのは:

- 検査は API プロセス自身の import ではなく、アップデータが実際に使う venv の
  interpreter (subprocess probe) で行う。probe が失敗すると 409 で断り、
  メッセージが復旧手段 (update.bat / update.sh) を案内する
- 断った場合、アップデータの spawn もシャットダウン予約も起きない
  (config が書かれないことで確かめる — config 書き込みは spawn より前の工程)
- probe が成功すれば psutil の 409 では止まらず、次の検証 (git 検査) へ進む
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import system as system_routes
from saiverse import app_state


class UpdatePsutilPrecheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name)
        script = self.project / "scripts" / "update_engine.py"
        script.parent.mkdir(parents=True)
        script.write_text("", encoding="utf-8")

        self._prev_project_dir = app_state.project_dir
        app_state.project_dir = str(self.project)
        self.addCleanup(self._restore_project_dir)

        app = FastAPI()
        app.include_router(system_routes.router, prefix="/api/system")
        self.client = TestClient(app)

    def _restore_project_dir(self) -> None:
        app_state.project_dir = self._prev_project_dir

    def test_failed_probe_refuses_before_spawn_and_shutdown(self) -> None:
        # tmp プロジェクトには .venv が無いので、venv interpreter での probe は
        # 実際に失敗する (OSError)。monkeypatch なしの実挙動で 409 を確かめる。
        res = self.client.post("/api/system/update")

        self.assertEqual(res.status_code, 409)
        detail = res.json()["detail"]
        self.assertIn("psutil", detail)
        self.assertIn("update.bat", detail)
        self.assertIn("update.sh", detail)
        # アップデータは spawn されず、シャットダウンも予約されない。
        self.assertFalse((self.project / ".update_config.json").exists())

    def test_successful_probe_passes_the_psutil_gate(self) -> None:
        # probe が成功すれば psutil の 409 では止まらず、次の検証へ進む。
        # tmp プロジェクトは git checkout ではないので git 検査の 409 で
        # 止まるが、それは psutil メッセージではない — ここまでで十分。
        with patch.object(
            system_routes.subprocess, "run", return_value=Mock(returncode=0)
        ) as probe:
            res = self.client.post("/api/system/update")

        probe.assert_called_once()
        self.assertEqual(res.status_code, 409)
        detail = res.json()["detail"]
        self.assertNotIn("psutil", detail)
        self.assertIn("Git", detail)  # 次の検証 (git 検査) に到達した証拠
        self.assertFalse((self.project / ".update_config.json").exists())


if __name__ == "__main__":
    unittest.main()
