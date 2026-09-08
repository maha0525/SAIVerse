"""二重運転を防ぐ検査 (another_running_process_owns_db) の fail-closed 回帰。

psutil の無い環境ではプロセス照合が "unknown" になる。かつては "running" しか
拒否側に数えず、稼働中プロセスの City を CITY_SLUG 自動修復が改名しうる穴が
あった (docs/issues/self_update_unsafe_without_psutil.md)。unknown は
「稼働中かもしれない」として起動時検査と同じ向き (拒否側) に数える。

同族の欠陥として、os.kill(pid, 0) の PermissionError (プロセスは存在するが
アクセスできない) を「死んでいる」扱いに潰さないことも、ここで固定する
(runtime_marker._marker_state / sai_memory.backup._is_process_alive)。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from saiverse import runtime_marker


def _write_marker(home: Path, name: str, payload: dict) -> Path:
    marker_dir = home / ".runtime"
    marker_dir.mkdir(parents=True, exist_ok=True)
    path = marker_dir / f"{name}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_unknown_marker_with_matching_db_refuses_without_psutil(monkeypatch, tmp_path):  # type: ignore[no-untyped-def]
    """生きている別プロセスの pid を指すが psutil で照合できないマーカーは、
    db_path が一致する限り「稼働中かもしれない」として拒否する。"""
    monkeypatch.setenv("SAIVERSE_HOME", str(tmp_path))
    monkeypatch.setitem(sys.modules, "psutil", None)  # makes `import psutil` fail

    db_path = tmp_path / "saiverse.db"
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _write_marker(
            tmp_path,
            "live",
            {
                "format_version": 1,
                "token": "tok",
                "pid": child.pid,
                "process_created_at": 1.0,
                "city_name": "city_a",
                "db_path": str(db_path.resolve()),
            },
        )
        owned, reason = runtime_marker.another_running_process_owns_db(db_path)
    finally:
        child.terminate()
        child.wait(timeout=10)

    assert owned is True
    assert "cannot be verified" in reason


def test_unreadable_marker_refuses_unconditionally(monkeypatch, tmp_path):  # type: ignore[no-untyped-def]
    """読めないマーカーはどの DB のものか判定できないので、無条件に拒否する。"""
    monkeypatch.setenv("SAIVERSE_HOME", str(tmp_path))
    marker_dir = tmp_path / ".runtime"
    marker_dir.mkdir(parents=True)
    (marker_dir / "broken.json").write_text("{not json", encoding="utf-8")

    owned, reason = runtime_marker.another_running_process_owns_db(tmp_path / "saiverse.db")

    assert owned is True
    assert "unreadable" in reason


def test_stale_marker_for_dead_pid_still_passes(monkeypatch, tmp_path):  # type: ignore[no-untyped-def]
    """死んだ pid のマーカーは psutil が無くても "stopped" と判定でき、
    修復を止めない (安全側に倒しすぎて何も直せなくなる回帰を防ぐ)。"""
    monkeypatch.setenv("SAIVERSE_HOME", str(tmp_path))
    monkeypatch.setitem(sys.modules, "psutil", None)

    db_path = tmp_path / "saiverse.db"
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=30)  # dead, pid very unlikely to be reused immediately
    if hasattr(child, "_handle"):
        # Windows: Popen が握るハンドルがカーネルのプロセスオブジェクトを
        # 生かし続け、os.kill(pid, 0) が「存在する」と答えてしまう。
        # 実運用の死んだプロセスに合わせ、ここで解放して pid を消す。
        child._handle.Close()
    _write_marker(
        tmp_path,
        "stale",
        {
            "format_version": 1,
            "token": "tok",
            "pid": child.pid,
            "process_created_at": 1.0,
            "city_name": "city_a",
            "db_path": str(db_path.resolve()),
        },
    )

    owned, reason = runtime_marker.another_running_process_owns_db(db_path)

    assert owned is False
    assert reason == ""


def test_live_pid_without_identity_record_refuses_with_psutil(monkeypatch, tmp_path):  # type: ignore[no-untyped-def]
    """psutil ありの経路: 生きた pid を指すが process_created_at が None の
    マーカーは、psutil の無い環境で書かれた正規の姿であり、生きている本物の
    プロセスかもしれない。"stopped" (pid 再利用の確定) に潰すとマーカーが
    削除されるので、"unknown" として拒否側に数える。"""
    pytest.importorskip("psutil")
    monkeypatch.setenv("SAIVERSE_HOME", str(tmp_path))

    db_path = tmp_path / "saiverse.db"
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _write_marker(
            tmp_path,
            "no_identity",
            {
                "format_version": 1,
                "token": "tok",
                "pid": child.pid,
                "process_created_at": None,
                "city_name": "city_a",
                "db_path": str(db_path.resolve()),
            },
        )
        owned, reason = runtime_marker.another_running_process_owns_db(db_path)
    finally:
        child.terminate()
        child.wait(timeout=10)

    assert owned is True
    assert "no identity record" in reason


def test_unknown_marker_without_db_path_refuses(monkeypatch, tmp_path):  # type: ignore[no-untyped-def]
    """db_path の無い unknown マーカーは同じ DB かを判定できないので拒否する。
    判定できないなら素通し (False) ではなく止まるのが本件の芯。"""
    monkeypatch.setenv("SAIVERSE_HOME", str(tmp_path))
    monkeypatch.setitem(sys.modules, "psutil", None)  # 照合できず unknown になる

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _write_marker(
            tmp_path,
            "no_db",
            {
                "format_version": 1,
                "token": "tok",
                "pid": child.pid,
                "process_created_at": 1.0,
                "city_name": "city_a",
                # db_path なし
            },
        )
        owned, reason = runtime_marker.another_running_process_owns_db(
            tmp_path / "saiverse.db"
        )
    finally:
        child.terminate()
        child.wait(timeout=10)

    assert owned is True
    assert reason


@pytest.mark.parametrize(
    "corrupt_value",
    ["1.0", True, float("inf")],
    ids=["numeric-string", "bool", "infinity"],
)
def test_live_pid_with_corrupt_identity_record_refuses(monkeypatch, tmp_path, corrupt_value):  # type: ignore[no-untyped-def]
    """正規の書き手は float か None しか書かない。数値文字列 / bool / Infinity は
    破損値 = 照合できないので、"stopped" (マーカー削除) に落とさず拒否側に倒す。"""
    pytest.importorskip("psutil")
    monkeypatch.setenv("SAIVERSE_HOME", str(tmp_path))

    db_path = tmp_path / "saiverse.db"
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _write_marker(
            tmp_path,
            "corrupt",
            {
                "format_version": 1,
                "token": "tok",
                "pid": child.pid,
                "process_created_at": corrupt_value,
                "city_name": "city_a",
                "db_path": str(db_path.resolve()),
            },
        )
        owned, reason = runtime_marker.another_running_process_owns_db(db_path)
    finally:
        child.terminate()
        child.wait(timeout=10)

    assert owned is True
    assert "corrupt" in reason


def test_acquire_refuses_to_start_over_marker_without_identity_record(monkeypatch, tmp_path):  # type: ignore[no-untyped-def]
    """同条件 (生きた pid + 照合値なし) では起動も拒否し、復旧手段 (マーカー
    削除) をメッセージで案内する。"""
    pytest.importorskip("psutil")
    monkeypatch.setenv("SAIVERSE_HOME", str(tmp_path))

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _write_marker(
            tmp_path,
            "no_identity",
            {
                "format_version": 1,
                "token": "tok",
                "pid": child.pid,
                "process_created_at": None,
                "city_name": "city_b",
                "db_path": str((tmp_path / "saiverse.db").resolve()),
            },
        )
        with pytest.raises(RuntimeError, match="no identity record") as excinfo:
            runtime_marker.acquire_runtime_marker(
                city_name="city_a",
                db_path=tmp_path / "saiverse.db",
                argv=["main.py", "city_a"],
            )
    finally:
        child.terminate()
        child.wait(timeout=10)

    assert "delete the marker files" in str(excinfo.value)


def test_live_pid_with_mismatched_identity_is_still_stopped(monkeypatch, tmp_path):  # type: ignore[no-untyped-def]
    """両方の照合値が取れて数値で本当に不一致なら、従来どおり "stopped"
    (pid 再利用の確定) — 安全側に倒しすぎて何も直せなくなる回帰を防ぐ。"""
    pytest.importorskip("psutil")
    monkeypatch.setenv("SAIVERSE_HOME", str(tmp_path))

    db_path = tmp_path / "saiverse.db"
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _write_marker(
            tmp_path,
            "reused",
            {
                "format_version": 1,
                "token": "tok",
                "pid": child.pid,
                "process_created_at": 1.0,  # 実際の create_time と確実に不一致
                "city_name": "city_a",
                "db_path": str(db_path.resolve()),
            },
        )
        owned, reason = runtime_marker.another_running_process_owns_db(db_path)
    finally:
        child.terminate()
        child.wait(timeout=10)

    assert owned is False
    assert reason == ""


def test_permission_error_marker_refuses_as_unknown(monkeypatch, tmp_path):  # type: ignore[no-untyped-def]
    """os.kill(pid, 0) が PermissionError を返すプロセス (存在するがアクセス
    できない) は「生きているかもしれない」ので、"stopped" に潰さず拒否する。"""
    monkeypatch.setenv("SAIVERSE_HOME", str(tmp_path))
    monkeypatch.setitem(sys.modules, "psutil", None)

    def _kill_denied(pid, sig):  # type: ignore[no-untyped-def]
        raise PermissionError("access denied")

    monkeypatch.setattr(runtime_marker.os, "kill", _kill_denied)

    db_path = tmp_path / "saiverse.db"
    _write_marker(
        tmp_path,
        "denied",
        {
            "format_version": 1,
            "token": "tok",
            "pid": 12345,
            "process_created_at": 1.0,
            "city_name": "city_a",
            "db_path": str(db_path.resolve()),
        },
    )

    owned, reason = runtime_marker.another_running_process_owns_db(db_path)

    assert owned is True
    assert "may be alive" in reason


def test_backup_is_process_alive_treats_permission_error_as_alive(monkeypatch):  # type: ignore[no-untyped-def]
    """sai_memory.backup._is_process_alive も同族: PermissionError を死亡扱いに
    潰すと、生きているロック保持者のロックファイルを誤削除しうる。"""
    from sai_memory import backup

    def _kill_denied(pid, sig):  # type: ignore[no-untyped-def]
        raise PermissionError("access denied")

    monkeypatch.setattr(backup.os, "kill", _kill_denied)

    assert backup._is_process_alive(12345) is True
