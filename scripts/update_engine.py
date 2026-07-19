"""Canonical fail-closed SAIVerse update engine.

The API updater and the platform wrappers all delegate here.  This module is
deliberately self-contained because the working tree can change while it is
running.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

LOGGER = logging.getLogger("saiverse.update")


class UpdateError(RuntimeError):
    """A phase failed and no later mutating phase may run."""


def setup_logging(project_dir: Path) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(project_dir / "self_update.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def _run(
    command: list[str],
    *,
    cwd: Path,
    label: str,
    timeout: int = 900,
) -> subprocess.CompletedProcess[str]:
    LOGGER.info("Phase: %s", label)
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateError(f"{label} could not run: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-2000:]
        raise UpdateError(f"{label} failed with exit {result.returncode}: {detail}")
    return result


def _process_create_time(pid: int) -> float | None:
    try:
        import psutil

        return float(psutil.Process(pid).create_time())
    except Exception:
        return None


def _process_alive(pid: int) -> bool:
    try:
        import psutil

        return bool(psutil.pid_exists(pid))
    except Exception:
        if sys.platform == "win32":
            return _process_create_time(pid) is not None
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def _identity_matches(pid: int, expected_created_at: float | None) -> bool:
    if expected_created_at is None:
        return False
    actual = _process_create_time(pid)
    return actual is not None and abs(actual - expected_created_at) <= 0.01


def wait_for_owned_process_exit(
    pid: int,
    expected_created_at: float | None,
    *,
    timeout: float = 30.0,
) -> None:
    """Wait for the recorded process; only terminate that verified identity."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            return
        time.sleep(0.25)
    if not _identity_matches(pid, expected_created_at):
        raise UpdateError(
            f"PID {pid} did not exit and its identity cannot be verified; refusing to signal it"
        )
    LOGGER.warning("Verified main process PID %d did not exit; terminating it", pid)
    try:
        import psutil

        process = psutil.Process(pid)
        process.terminate()
        process.wait(timeout=10)
    except Exception as exc:
        raise UpdateError(f"Verified main process PID {pid} could not be stopped: {exc}") from exc


def _ensure_portable_git_on_path(project_dir: Path) -> None:
    """Make a setup-installed PortableGit visible to this (separate) session.

    ``setup.bat`` installs PortableGit into ``.git-portable/`` and prepends
    ``.git-portable\\cmd`` to PATH within its own session. ``update.bat`` runs in
    a *later* session that does not inherit that PATH, so a user who has only
    PortableGit (no system / winget Git) would otherwise fail the git readiness
    check below. Prepend the portable ``cmd`` dir so ``shutil.which('git')`` and
    the ``git`` subprocess calls resolve. No-op when the binary is absent
    (non-Windows setups never create it) or the dir is already on PATH.
    See docs/issues/git_required_for_zip_install.md.
    """
    portable_cmd = project_dir / ".git-portable" / "cmd"
    if not (portable_cmd / "git.exe").exists():
        return
    portable_str = str(portable_cmd)
    current = os.environ.get("PATH", "")
    if portable_str in current.split(os.pathsep):
        return
    os.environ["PATH"] = portable_str + os.pathsep + current
    LOGGER.info("Using setup-installed PortableGit at %s", portable_cmd)


def assert_git_update_ready(project_dir: Path) -> str:
    if not (project_dir / ".git").is_dir() or shutil.which("git") is None:
        raise UpdateError(
            "Automatic update requires a Git checkout. The former ZIP overlay path is "
            "disabled because it cannot safely remove retired files without deleting "
            "unknown user files."
        )
    status = _run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=project_dir,
        label="verify clean working tree",
        timeout=60,
    ).stdout
    if status.strip():
        raise UpdateError(
            "Working tree has local changes. Update was not started; commit or otherwise "
            "resolve them explicitly. The updater never stashes or resets user work."
        )
    return _run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_dir,
        label="record current revision",
        timeout=60,
    ).stdout.strip()


def create_pre_update_snapshot(project_dir: Path, python: str) -> str:
    name = datetime.now(timezone.utc).strftime("auto_before_update_%Y%m%d_%H%M%S_%f")
    _run(
        [
            python,
            str(project_dir / "scripts" / "snapshot.py"),
            "save",
            name,
            "--note",
            "Automatic restore point before code update",
        ],
        cwd=project_dir,
        label="create and validate pre-update world snapshot",
    )
    return name


def update_code(project_dir: Path) -> None:
    _run(
        ["git", "pull", "--ff-only"],
        cwd=project_dir,
        label="fast-forward code update",
        timeout=300,
    )


def update_dependencies(project_dir: Path, python: str) -> None:
    _run(
        [python, "-m", "pip", "install", "-r", "requirements.txt"],
        cwd=project_dir,
        label="install Python dependencies",
    )
    frontend = project_dir / "frontend"
    if not frontend.is_dir():
        raise UpdateError("frontend directory is missing after code update")
    npm = shutil.which("npm")
    portable_npm = project_dir / ".node" / ("npm.cmd" if sys.platform == "win32" else "npm")
    if npm is None and portable_npm.is_file():
        npm = str(portable_npm)
    if npm is None:
        raise UpdateError("npm is required to update the frontend")
    npm_command = "ci" if (frontend / "package-lock.json").is_file() else "install"
    _run(
        [npm, npm_command],
        cwd=frontend,
        label=f"npm {npm_command}",
    )


def _rollback_code_and_dependencies(
    project_dir: Path,
    python: str,
    old_revision: str,
) -> None:
    """Best-effort repair used only after the initial clean-tree invariant."""
    LOGGER.error("Rolling code back to %s", old_revision)
    _run(
        ["git", "reset", "--hard", old_revision],
        cwd=project_dir,
        label="rollback code revision",
        timeout=120,
    )
    try:
        update_dependencies(project_dir, python)
    except UpdateError:
        LOGGER.exception("Dependency repair for the previous revision also failed")


def restart_application(config: dict[str, Any]) -> subprocess.Popen[Any]:
    project_dir = Path(config["project_dir"]).resolve()
    python = str(config["venv_python"])
    main_args = config.get("main_args")
    if not isinstance(main_args, list) or not all(isinstance(item, str) for item in main_args):
        raise UpdateError("Update config is missing the exact main arguments")
    command = [python, str(project_dir / "main.py"), *main_args]
    LOGGER.info("Restarting the same City process with %d preserved arguments", len(main_args))
    kwargs: dict[str, Any] = {
        "cwd": str(project_dir),
        "close_fds": True,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(command, **kwargs)
    except OSError as exc:
        raise UpdateError(f"Could not restart SAIVerse: {exc}") from exc


def _health_url(config: dict[str, Any]) -> str:
    host = str(config.get("listen_host") or "127.0.0.1")
    if host in {"0.0.0.0", "::", "localhost"}:
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{int(config['backend_port'])}/api/system/version"


def wait_for_healthy_restart(
    process: subprocess.Popen[Any],
    config: dict[str, Any],
    *,
    timeout: float = 120.0,
) -> dict[str, Any]:
    url = _health_url(config)
    deadline = time.monotonic() + timeout
    last_error = "health endpoint not reached"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise UpdateError(f"Restarted process exited with code {process.returncode}")
        headers = {"User-Agent": "SAIVerse-Updater"}
        owner_token = os.getenv("SAIVERSE_OWNER_TOKEN")
        if owner_token:
            headers["Authorization"] = f"Bearer {owner_token}"
        try:
            with urlopen(Request(url, headers=headers), timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("city_name") != config.get("city_name"):
                raise UpdateError(
                    "Restarted backend reports the wrong City: "
                    f"{payload.get('city_name')!r}"
                )
            if payload.get("db_identity") != config.get("db_identity"):
                raise UpdateError("Restarted backend reports a different database identity")
            return payload
        except UpdateError:
            raise
        except (OSError, URLError, ValueError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            time.sleep(1)
    raise UpdateError(f"Restart health check timed out: {last_error}")


def _terminate_spawned(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=10)
    except Exception:
        LOGGER.exception("Could not terminate failed restarted process PID %s", process.pid)


def run_update(config: dict[str, Any] | None, project_dir: Path) -> None:
    python = str(config.get("venv_python", sys.executable)) if config else sys.executable
    _ensure_portable_git_on_path(project_dir)
    old_revision = assert_git_update_ready(project_dir)

    if config:
        wait_for_owned_process_exit(
            int(config["main_pid"]),
            config.get("main_process_created_at"),
        )
    snapshot_name = create_pre_update_snapshot(project_dir, python)
    LOGGER.info("Pre-update restore point: %s", snapshot_name)

    code_changed = False
    try:
        update_code(project_dir)
        code_changed = True
        update_dependencies(project_dir, python)
    except UpdateError:
        if code_changed:
            _rollback_code_and_dependencies(project_dir, python, old_revision)
        raise

    if not config:
        LOGGER.info("Update applied. Start SAIVerse normally to run startup migrations.")
        return

    process: subprocess.Popen[Any] | None = None
    try:
        process = restart_application(config)
        payload = wait_for_healthy_restart(process, config)
        LOGGER.info(
            "Update complete: City=%s version=%s PID=%s",
            payload.get("city_name"),
            payload.get("version"),
            process.pid,
        )
    except UpdateError:
        if process is not None:
            _terminate_spawned(process)
        _rollback_code_and_dependencies(project_dir, python, old_revision)
        rollback_process = restart_application(config)
        try:
            wait_for_healthy_restart(rollback_process, config)
            LOGGER.error("Previous revision was restored and restarted successfully")
        except UpdateError:
            LOGGER.exception("Previous revision also failed to restart")
        raise


def _load_config(config_path: Path) -> dict[str, Any]:
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpdateError(f"Update config is unreadable: {exc}") from exc
    project_dir = Path(config.get("project_dir", "")).resolve()
    expected = Path(__file__).resolve().parent.parent
    if project_dir != expected:
        raise UpdateError(f"Update config project mismatch: {project_dir} != {expected}")
    return config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Canonical SAIVerse updater")
    parser.add_argument("--manual", action="store_true", help="Update while SAIVerse is stopped")
    parser.add_argument("--config", type=Path, help="Detached updater config path")
    args = parser.parse_args(argv)

    project_dir = Path(__file__).resolve().parent.parent
    setup_logging(project_dir)
    config_path = args.config or project_dir / ".update_config.json"
    try:
        config = None if args.manual else _load_config(config_path)
        run_update(config, project_dir)
    except UpdateError as exc:
        LOGGER.error("Update aborted: %s", exc)
        return 1

    if not args.manual:
        try:
            config_path.unlink()
        except OSError:
            LOGGER.warning("Update succeeded but config cleanup failed", exc_info=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
