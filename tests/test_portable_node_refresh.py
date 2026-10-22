"""The portable Node.js (.node) follows the version install_node_portable.ps1 pins.

docs/intent/dependency_management.md section 2-6. Two halves:

* the update engine calls the installer only for a Windows checkout that has
  a .node, after the code moved and before npm ci, never in a rollback, and a
  failed replacement does not fail the update;
* the installer itself (run for real through Windows PowerShell, with a local
  archive built from the system node.exe standing in for the download) leaves
  a matching .node alone, replaces a differing one, and keeps the previous
  .node intact whenever the replacement fails.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts import update_engine

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLER = REPO_ROOT / "scripts" / "install_node_portable.ps1"


# --- the update engine ---------------------------------------------------------


def _checkout_with_portable_node(tmp_path: Path) -> Path:
    (tmp_path / ".node").mkdir()
    (tmp_path / ".node" / "node.exe").write_bytes(b"")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "install_node_portable.ps1").write_text("# stand-in\n", encoding="utf-8")
    return tmp_path


def test_refresh_does_nothing_without_a_portable_node(tmp_path: Path) -> None:
    """System Node.js users: their Node.js is theirs to update."""
    with patch.object(update_engine, "_is_windows", return_value=True), patch.object(update_engine, "_run") as run:
        update_engine.refresh_portable_node(tmp_path)
    run.assert_not_called()


def test_refresh_does_nothing_off_windows(tmp_path: Path) -> None:
    project = _checkout_with_portable_node(tmp_path)
    with patch.object(update_engine, "_is_windows", return_value=False), patch.object(update_engine, "_run") as run:
        update_engine.refresh_portable_node(project)
    run.assert_not_called()


def test_refresh_runs_the_installer_the_update_brought_in(tmp_path: Path) -> None:
    project = _checkout_with_portable_node(tmp_path)
    with patch.object(update_engine, "_is_windows", return_value=True), patch.object(
        update_engine, "_run", return_value=MagicMock(returncode=0, stdout="[OK] done\n", stderr="")
    ) as run:
        update_engine.refresh_portable_node(project)
    args, kwargs = run.call_args
    command = args[0]
    assert command[0] == "powershell"
    assert command[-2:] == ["-File", str(project / "scripts" / "install_node_portable.ps1")]
    assert kwargs["cwd"] == project
    # A non-zero exit is an answer handled below, not an exception.
    assert kwargs["check"] is False


def test_a_failed_replacement_does_not_fail_the_update(tmp_path: Path, caplog) -> None:  # type: ignore[no-untyped-def]
    project = _checkout_with_portable_node(tmp_path)
    with caplog.at_level(logging.WARNING, logger="saiverse.update"), patch.object(
        update_engine, "_is_windows", return_value=True
    ), patch.object(
        update_engine,
        "_run",
        return_value=MagicMock(returncode=1, stdout="[ERROR] download failed\n", stderr=""),
    ):
        update_engine.refresh_portable_node(project)  # must not raise
    assert "could not be replaced (exit 1)" in caplog.text


def test_powershell_that_cannot_start_does_not_fail_the_update(tmp_path: Path, caplog) -> None:  # type: ignore[no-untyped-def]
    project = _checkout_with_portable_node(tmp_path)
    with caplog.at_level(logging.WARNING, logger="saiverse.update"), patch.object(
        update_engine, "_is_windows", return_value=True
    ), patch.object(update_engine, "_run", side_effect=update_engine.UpdateError("could not run")):
        update_engine.refresh_portable_node(project)  # must not raise
    assert "could not be checked" in caplog.text


def test_refresh_runs_after_the_code_moved_and_before_npm_ci(tmp_path: Path) -> None:
    order: list[str] = []
    with patch.object(update_engine, "update_code", side_effect=lambda *_: order.append("code")), patch.object(
        update_engine, "invalidate_completion_marker", side_effect=lambda *_: order.append("marker dropped")
    ), patch.object(
        update_engine, "refresh_portable_node", side_effect=lambda *_: order.append("node")
    ), patch.object(update_engine, "update_dependencies", side_effect=lambda *_: order.append("deps")):
        update_engine._apply_code_and_dependencies(tmp_path, "python", update_engine._UpdateProgress())
    assert order == ["code", "marker dropped", "node", "deps"]


def test_rollback_leaves_the_portable_node_alone(tmp_path: Path) -> None:
    """The previous code runs on the newer Node.js; the replaced version is
    the one the pin moved away from and must not come back."""
    (tmp_path / "requirements.lock").write_text("", encoding="utf-8")
    with patch.object(update_engine, "_run"), patch.object(update_engine, "update_dependencies"), patch.object(
        update_engine, "refresh_portable_node"
    ) as refresh:
        update_engine._rollback_code_and_dependencies(tmp_path, "python", "old-head")
    refresh.assert_not_called()


def _portable_npm(project: Path) -> Path:
    npm = project / ".node" / ("npm.cmd" if sys.platform == "win32" else "npm")
    npm.parent.mkdir(parents=True, exist_ok=True)
    npm.write_text("", encoding="utf-8")
    return npm


def test_portable_npm_wins_over_the_one_on_path(tmp_path: Path) -> None:
    """start.bat puts .node first; the updater installs with the same Node.js."""
    npm = _portable_npm(tmp_path)
    with patch.object(update_engine.shutil, "which", return_value="C:/Program Files/nodejs/npm.cmd"):
        assert update_engine._find_npm(tmp_path) == str(npm)


def test_npm_on_path_is_used_without_a_portable_node(tmp_path: Path) -> None:
    with patch.object(update_engine.shutil, "which", return_value="/usr/bin/npm"):
        assert update_engine._find_npm(tmp_path) == "/usr/bin/npm"


def test_npm_ci_and_build_run_with_the_portable_node_first_on_path(tmp_path: Path) -> None:
    """npm's lifecycle scripts and ``next build`` run ``node`` by name, and
    update.bat's PATH has no .node: without this they would find another
    Node.js, or none."""
    npm = _portable_npm(tmp_path)
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package-lock.json").write_text("{}", encoding="utf-8")
    calls: dict[str, dict] = {}

    def fake_run(command, *, cwd, label, timeout=900, check=True, encoding=None, env=None):  # type: ignore[no-untyped-def]
        calls[label] = {"command": command, "env": env}
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch.object(update_engine, "_run", side_effect=fake_run), patch.object(
        update_engine, "_report_addon_conflicts"
    ):
        update_engine.update_dependencies(tmp_path, "python")
        update_engine.build_frontend(tmp_path)

    for label in ("npm ci", "npm run build"):
        assert calls[label]["command"][0] == str(npm)
        assert calls[label]["env"]["PATH"].split(os.pathsep)[0] == str(npm.parent)


# --- the installer, run for real -------------------------------------------------

_SYSTEM_NODE = shutil.which("node")
_POWERSHELL = shutil.which("powershell")

installer_only = pytest.mark.skipif(
    sys.platform != "win32" or _SYSTEM_NODE is None or _POWERSHELL is None,
    reason="runs install_node_portable.ps1 through Windows PowerShell with the system node.exe",
)


def _node_version(node_exe: Path) -> str:
    return subprocess.run([str(node_exe), "--version"], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture(scope="module")
def release(tmp_path_factory) -> dict[str, str]:  # type: ignore[no-untyped-def]
    """A local stand-in for nodejs.org's ZIP: the system node.exe, laid out
    like node-vX.Y.Z-win-x64.zip, with its sha256."""
    assert _SYSTEM_NODE is not None
    version = _node_version(Path(_SYSTEM_NODE))  # "v22.17.0"
    folder = f"node-{version}-win-x64"
    archive = tmp_path_factory.mktemp("release") / f"{folder}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.write(_SYSTEM_NODE, f"{folder}/node.exe")
        zf.writestr(f"{folder}/from-the-new-release.txt", "new")
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    return {"archive": str(archive), "version": version, "sha256": sha}


def _install(project: Path, release: dict[str, str], *, archive: str | None = None, sha256: str | None = None):  # type: ignore[no-untyped-def]
    assert _POWERSHELL is not None
    return subprocess.run(
        [
            _POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(INSTALLER),
            "-ProjectRoot", str(project),
            "-ArchivePath", archive if archive is not None else release["archive"],
            "-NodeVersion", release["version"].lstrip("v"),
            "-ExpectedSha256", sha256 if sha256 is not None else release["sha256"],
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )


def _old_portable_node(project: Path) -> Path:
    """A .node that is not the pinned version (its node.exe does not even run)."""
    node_dir = project / ".node"
    node_dir.mkdir(parents=True)
    (node_dir / "node.exe").write_bytes(b"old node")
    (node_dir / "from-the-old-install.txt").write_text("old", encoding="utf-8")
    # Names on both sides of node.exe in directory order, so a move that
    # goes file by file and stops half-way cannot pass for intact.
    (node_dir / "a-early.txt").write_text("old", encoding="utf-8")
    (node_dir / "z-late.txt").write_text("old", encoding="utf-8")
    return node_dir


def _assert_old_node_intact(project: Path) -> None:
    node_dir = project / ".node"
    assert (node_dir / "node.exe").read_bytes() == b"old node"
    assert sorted(path.name for path in node_dir.iterdir()) == [
        "a-early.txt",
        "from-the-old-install.txt",
        "node.exe",
        "z-late.txt",
    ]
    _assert_no_swap_leftovers(project)


def _assert_no_swap_leftovers(project: Path) -> None:
    assert not (project / ".node.new").exists()
    assert not (project / ".node.old").exists()


@installer_only
def test_installer_leaves_the_pinned_version_alone(tmp_path: Path, release: dict[str, str]) -> None:
    node_dir = tmp_path / ".node"
    node_dir.mkdir()
    assert _SYSTEM_NODE is not None
    shutil.copy2(_SYSTEM_NODE, node_dir / "node.exe")
    (node_dir / "from-the-old-install.txt").write_text("old", encoding="utf-8")
    before = (node_dir / "node.exe").stat().st_mtime_ns

    # No archive at all: reaching for one would fail the run.
    result = _install(tmp_path, release, archive=str(tmp_path / "absent.zip"))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "is the pinned version" in result.stdout
    assert (node_dir / "node.exe").stat().st_mtime_ns == before
    assert (node_dir / "from-the-old-install.txt").exists()
    _assert_no_swap_leftovers(tmp_path)


@installer_only
def test_installer_replaces_a_differing_version(tmp_path: Path, release: dict[str, str]) -> None:
    _old_portable_node(tmp_path)

    result = _install(tmp_path, release)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "[UPDATE]" in result.stdout
    assert _node_version(tmp_path / ".node" / "node.exe") == release["version"]
    # Replaced as a whole, not merged over the old folder.
    assert not (tmp_path / ".node" / "from-the-old-install.txt").exists()
    assert (tmp_path / ".node" / "from-the-new-release.txt").exists()
    _assert_no_swap_leftovers(tmp_path)


@installer_only
def test_installer_installs_when_there_is_no_portable_node(tmp_path: Path, release: dict[str, str]) -> None:
    result = _install(tmp_path, release)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _node_version(tmp_path / ".node" / "node.exe") == release["version"]
    _assert_no_swap_leftovers(tmp_path)


@installer_only
def test_failed_download_keeps_the_previous_node(tmp_path: Path, release: dict[str, str]) -> None:
    _old_portable_node(tmp_path)

    result = _install(tmp_path, release, archive=str(tmp_path / "never-downloaded.zip"))

    assert result.returncode == 1
    assert "existing .node/ was left as it was" in result.stdout
    _assert_old_node_intact(tmp_path)


@installer_only
def test_checksum_mismatch_keeps_the_previous_node(tmp_path: Path, release: dict[str, str]) -> None:
    _old_portable_node(tmp_path)

    result = _install(tmp_path, release, sha256="0" * 64)

    assert result.returncode == 1
    assert "Checksum mismatch" in result.stdout
    _assert_old_node_intact(tmp_path)


@installer_only
def test_broken_archive_keeps_the_previous_node(tmp_path: Path, release: dict[str, str]) -> None:
    _old_portable_node(tmp_path)
    broken = tmp_path / "broken.zip"
    broken.write_bytes(b"this is not a zip archive")

    result = _install(tmp_path, release, archive=str(broken), sha256=hashlib.sha256(broken.read_bytes()).hexdigest())

    assert result.returncode == 1
    _assert_old_node_intact(tmp_path)


@installer_only
def test_node_in_use_keeps_the_previous_node(tmp_path: Path, release: dict[str, str]) -> None:
    """A frontend or an MCP server still running from .node holds files in it;
    the rename that swaps the folders fails and nothing changes.

    The held file sorts last: PowerShell's Move-Item, refused the rename,
    falls back to moving file by file and stops at the held one, which once
    left every earlier file moved out of .node (caught here, 2026-10-22)."""
    _old_portable_node(tmp_path)

    with open(tmp_path / ".node" / "z-late.txt", encoding="utf-8"):
        result = _install(tmp_path, release)

    assert result.returncode == 1, result.stdout + result.stderr
    _assert_old_node_intact(tmp_path)


@installer_only
def test_an_interrupted_swap_is_put_back_first(tmp_path: Path, release: dict[str, str]) -> None:
    """A run that died between moving .node aside and moving the new one in."""
    retired = tmp_path / ".node.old"
    retired.mkdir()
    assert _SYSTEM_NODE is not None
    shutil.copy2(_SYSTEM_NODE, retired / "node.exe")

    result = _install(tmp_path, release, archive=str(tmp_path / "absent.zip"))

    assert result.returncode == 0, result.stdout + result.stderr
    assert _node_version(tmp_path / ".node" / "node.exe") == release["version"]
    _assert_no_swap_leftovers(tmp_path)
