from __future__ import annotations

from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers

from api.file_safety import (
    read_upload_bounded,
    safe_filename,
    safe_upload_suffix,
    write_upload_bounded,
)
from api.routes import media
from saiverse.file_policy import enforce_allowed_file_path
from saiverse.data_paths import PROJECT_ROOT


def _upload(data: bytes, *, filename: str, content_type: str) -> UploadFile:
    return UploadFile(
        file=BytesIO(data),
        filename=filename,
        headers=Headers({"content-type": content_type}),
    )


@pytest.mark.asyncio
async def test_bounded_read_returns_413_instead_of_consuming_oversize() -> None:
    file = _upload(b"12345", filename="image.png", content_type="image/png")
    with pytest.raises(HTTPException) as captured:
        await read_upload_bounded(file, 4)
    assert captured.value.status_code == 413


@pytest.mark.asyncio
async def test_streaming_upload_removes_partial_file_on_limit(tmp_path: Path) -> None:
    file = _upload(b"0123456789", filename="large.bin", content_type="application/octet-stream")
    destination = tmp_path / "upload.tmp"
    with pytest.raises(HTTPException) as captured:
        await write_upload_bounded(file, destination, 5, chunk_bytes=3)
    assert captured.value.status_code == 413
    assert not destination.exists()


@pytest.mark.asyncio
async def test_image_route_preserves_upload_limit_status() -> None:
    file = _upload(b"12345", filename="image.png", content_type="image/png")
    with patch.object(media, "IMAGE_INPUT_MAX_BYTES", 4):
        with pytest.raises(HTTPException) as captured:
            await media.upload_image(file)
    assert captured.value.status_code == 413


def test_filename_and_suffix_reject_path_syntax() -> None:
    for value in ("../secret", "..\\secret", "C:secret", ""):
        with pytest.raises(HTTPException):
            safe_filename(value)
    assert safe_upload_suffix("../../archive.JSON") == ".json"
    assert safe_upload_suffix("file.really-long-unsafe-extension") == ".tmp"


def test_file_policy_rejects_outside_managed_roots(tmp_path: Path) -> None:
    home = tmp_path / "home"
    inside = home / "personas" / "air" / "memory.db"
    outside = PROJECT_ROOT / ".env"
    assert enforce_allowed_file_path(inside, home=home) == inside.resolve()
    with pytest.raises(ValueError, match="outside"):
        enforce_allowed_file_path(outside, home=home)
