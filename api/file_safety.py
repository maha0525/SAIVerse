"""Shared filesystem and upload boundaries for API routes."""
from __future__ import annotations

from pathlib import Path
import re

from fastapi import HTTPException, UploadFile

from saiverse.data_paths import get_saiverse_home
from saiverse.file_policy import enforce_allowed_file_path


def ensure_allowed_path(path: Path, *, home: Path | None = None) -> Path:
    """Resolve *path* and reject files outside managed or explicitly mounted roots."""
    try:
        return enforce_allowed_file_path(path, home=home)
    except ValueError:
        raise HTTPException(status_code=403, detail="File path is outside configured storage roots")


def resolve_allowed_path(raw_path: str, *, home: Path | None = None) -> Path:
    base = home or get_saiverse_home()
    path = Path(raw_path)
    if not path.is_absolute():
        path = base / path
    return ensure_allowed_path(path, home=base)


def safe_filename(filename: str) -> str:
    """Accept a single portable filename component, never a path."""
    value = filename.strip()
    if (
        not value
        or value in {".", ".."}
        or Path(value).name != value
        or "/" in value
        or "\\" in value
        or ":" in value
        or "\x00" in value
    ):
        raise HTTPException(status_code=400, detail="Invalid filename")
    return value


async def read_upload_bounded(file: UploadFile, max_bytes: int) -> bytes:
    """Read at most max_bytes plus one byte so oversized bodies are never fully buffered."""
    content = await file.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise HTTPException(status_code=413, detail=f"Upload exceeds {max_bytes} byte limit")
    return content


async def write_upload_bounded(
    file: UploadFile,
    destination: Path,
    max_bytes: int,
    *,
    chunk_bytes: int = 1024 * 1024,
) -> int:
    """Stream an upload to disk with a hard limit and remove partial output."""
    total = 0
    try:
        with destination.open("xb") as stream:
            while True:
                chunk = await file.read(chunk_bytes)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Upload exceeds {max_bytes} byte limit",
                    )
                stream.write(chunk)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return total


def safe_upload_suffix(filename: str | None, *, fallback: str = ".tmp") -> str:
    suffix = Path(filename or "").suffix.lower()
    if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
        return suffix
    return fallback
