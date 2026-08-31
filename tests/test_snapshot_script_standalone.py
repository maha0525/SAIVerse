"""snapshot.py がサブプロセスとして単体起動できることの回帰テスト。

update_engine は ``python scripts/snapshot.py save ...`` をサブプロセスで呼ぶ。
このとき sys.path[0] は scripts/ で、venv に saiverse の editable install が
無い素のユーザー環境 (setup.bat は requirements.txt しか入れない) では
saiverse パッケージが import できない。snapshot.py 自身がリポジトリルートを
sys.path へ通していないと、起動中検出 (is_saiverse_running →
saiverse.runtime_marker) が ModuleNotFoundError で必ず落ち、update.bat の
新経路が全ユーザーでスナップショット段で中断する (2026-08-30 実機で発見)。

``-S`` で site を無効化して開発 venv の editable install を遮断し、素の環境を
忠実に再現する — snapshot.py の import 連鎖 (saiverse.runtime_marker →
saiverse.data_paths) は標準ライブラリのみで完結する契約なので、-S でも動く。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_snapshot_save_runs_without_editable_install(tmp_path):
    home = tmp_path / "home"
    (home / "user_data").mkdir(parents=True)
    (home / "user_data" / "settings.json").write_text("{}", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-S", str(REPO_ROOT / "scripts" / "snapshot.py"), "save", "regression"],
        cwd=REPO_ROOT,
        env={
            **{k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
            "SAIVERSE_HOME": str(home),
        },
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "ModuleNotFoundError" not in result.stderr
    snapshots = list((home / "snapshots").glob("*.zip"))
    assert len(snapshots) == 1
