"""Canonical policy for files reachable from persisted SAIVerse paths."""
from __future__ import annotations

import os
from pathlib import Path

from saiverse.data_paths import PROJECT_ROOT, USER_DATA_DIR, get_saiverse_home


def allowed_file_roots(home: Path | None = None) -> tuple[Path, ...]:
    roots = [
        home or get_saiverse_home(),
        USER_DATA_DIR,
        PROJECT_ROOT / "assets",
        PROJECT_ROOT / "builtin_data",
        PROJECT_ROOT / "expansion_data",
        PROJECT_ROOT / "user_data",
    ]
    configured = os.getenv("SAIVERSE_EXTERNAL_FILE_ROOTS", "")
    roots.extend(
        Path(value.strip()).expanduser()
        for value in configured.split(os.pathsep)
        if value.strip()
    )
    return tuple(root.resolve() for root in roots)


def enforce_allowed_file_path(path: Path, *, home: Path | None = None) -> Path:
    resolved = path.expanduser().resolve()
    if not any(
        resolved == root or resolved.is_relative_to(root)
        for root in allowed_file_roots(home)
    ):
        raise ValueError("File path is outside configured storage roots")
    return resolved
