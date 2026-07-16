"""Process-owned lifecycle markers used by destructive maintenance commands.

One SAIVERSE_HOME may host more than one City process.  Markers are therefore
City-scoped, while maintenance commands inspect the whole marker directory and
refuse to mutate the world if *any* verified SAIVerse process is alive.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from saiverse.data_paths import get_saiverse_home


def runtime_marker_dir() -> Path:
    return get_saiverse_home() / ".runtime"


def _city_marker_path(city_name: str) -> Path:
    city_key = hashlib.sha256(city_name.encode("utf-8")).hexdigest()
    return runtime_marker_dir() / f"{city_key}.json"


def _marker_paths() -> list[Path]:
    paths: list[Path] = []
    directory = runtime_marker_dir()
    if directory.exists():
        paths.extend(sorted(directory.glob("*.json")))
    legacy = get_saiverse_home() / ".runtime.json"
    if legacy.exists():
        paths.append(legacy)
    return paths


def _process_create_time(pid: int) -> float | None:
    try:
        import psutil

        return float(psutil.Process(pid).create_time())
    except Exception:
        return None


def _read_marker(path: Path) -> tuple[dict | None, str | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        pid = int(data["pid"])
        token = data["token"]
        city_name = data["city_name"]
        if not isinstance(token, str) or not token:
            raise ValueError("invalid token")
        if not isinstance(city_name, str) or not city_name:
            raise ValueError("invalid city name")
        data["pid"] = pid
        return data, None
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return None, f"runtime marker {path.name} is unreadable: {exc}"


def _marker_state(path: Path) -> tuple[str, str, dict | None]:
    data, error = _read_marker(path)
    if data is None:
        return "unknown", error or f"runtime marker {path.name} is unreadable", None

    pid = data["pid"]
    expected_created = data.get("process_created_at")
    actual_created = _process_create_time(pid)
    if actual_created is None:
        try:
            os.kill(pid, 0)
        except OSError:
            return "stopped", f"stale runtime marker for pid {pid}", data
        return (
            "unknown",
            f"pid {pid} exists but process identity cannot be verified",
            data,
        )
    try:
        identity_matches = (
            expected_created is not None
            and abs(float(expected_created) - actual_created) <= 0.01
        )
    except (TypeError, ValueError):
        identity_matches = False
    if not identity_matches:
        return "stopped", f"stale runtime marker with reused pid {pid}", data
    return (
        "running",
        f"verified SAIVerse City {data['city_name']!r} process pid {pid}",
        data,
    )


def marker_status() -> tuple[str, str]:
    """Return aggregate (stopped|running|unknown, reason) for this home."""
    paths = _marker_paths()
    if not paths:
        return "stopped", "runtime markers absent"

    unknown_reasons: list[str] = []
    for path in paths:
        state, reason, _data = _marker_state(path)
        if state == "running":
            return state, reason
        if state == "unknown":
            unknown_reasons.append(reason)
    if unknown_reasons:
        return "unknown", "; ".join(unknown_reasons)
    return "stopped", "only stale runtime markers were found"


def acquire_runtime_marker(*, city_name: str, db_path: Path, argv: list[str]) -> str:
    """Acquire a marker for one City without excluding other City processes."""
    directory = runtime_marker_dir()
    directory.mkdir(parents=True, exist_ok=True)

    for existing in _marker_paths():
        state, reason, data = _marker_state(existing)
        if state == "unknown":
            raise RuntimeError(f"Cannot start SAIVerse: {reason}")
        if state == "running" and data and data["city_name"] == city_name:
            raise RuntimeError(f"Cannot start duplicate City {city_name!r}: {reason}")
        if state == "stopped":
            existing.unlink(missing_ok=True)

    token = uuid4().hex
    payload = {
        "format_version": 1,
        "token": token,
        "pid": os.getpid(),
        "process_created_at": _process_create_time(os.getpid()),
        "city_name": city_name,
        "db_path": str(db_path.resolve()),
        "argv": argv,
        "python": sys.executable,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    path = _city_marker_path(city_name)
    try:
        with path.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
    except FileExistsError as exc:
        state, reason, _data = _marker_state(path)
        raise RuntimeError(
            f"Cannot start duplicate City {city_name!r}: {state}: {reason}"
        ) from exc
    return token


def release_runtime_marker(token: str) -> None:
    for path in _marker_paths():
        data, _error = _read_marker(path)
        if data and data.get("token") == token:
            path.unlink(missing_ok=True)
            try:
                runtime_marker_dir().rmdir()
            except OSError:
                pass
            return
