"""ペルソナの VRM を受け取り・返し・消すルート (docs/intent/screen_avatar.md 設計 4)。

置き場の決まりは ``saiverse/persona_avatar_vrm.py``。状態を変える POST / DELETE は、
ほかの ``/api`` のルートと同じくアプリ全体のミドルウェア (``api/owner_auth.py`` の
``BrowserOriginMiddleware`` / ``OwnerAuthMiddleware``、main.py で登録) を通る。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from api.deps import get_manager
from api.file_safety import write_upload_bounded
from saiverse import persona_avatar_vrm
from saiverse.data_paths import get_saiverse_home
from saiverse.persona_avatar_vrm import (
    GLB_MAGIC,
    MAX_VRM_BYTES,
    VRM_FILE_LOCK,
    VRM_MEDIA_TYPE,
    VRM_SUFFIX,
    AvatarFolderOutsideError,
    PersonaNotFoundError,
    VrmReplaceError,
    ensure_avatar_dir,
    install_vrm_file,
    new_temp_path,
    resolve_stored_vrm,
    safe_vrm_filename,
    unlink_quietly,
    vrm_info,
)

from .models import AvatarVrmInfo

LOGGER = logging.getLogger(__name__)

router = APIRouter()

#: 入れる・消すを一件ずつ通す (同じペルソナへの同時の入れ直しで、DB が指すファイルを
#: もう片方が消すことがないように)。ファイルの付け替えと DB の書き込みは、それに加えて
#: ``persona_avatar_vrm.VRM_FILE_LOCK`` で、Godot のアドオンからの書き込み (別のスレッド)
#: とも一件ずつ通す。
_VRM_WRITE_LOCK = asyncio.Lock()


def _read_stored_path(manager, persona_id: str) -> Optional[str]:
    """``AI.AVATAR_VRM_PATH`` を読む。ペルソナが無ければ 404。"""
    try:
        return persona_avatar_vrm.read_stored_vrm_path(manager, persona_id)
    except PersonaNotFoundError:
        raise HTTPException(status_code=404, detail="Persona not found") from None


def _write_stored_path(manager, persona_id: str, value: Optional[str]) -> None:
    try:
        persona_avatar_vrm.write_stored_vrm_path(manager, persona_id, value)
    except PersonaNotFoundError:
        raise HTTPException(status_code=404, detail="Persona not found") from None


@router.post("/{persona_id}/avatar-vrm", response_model=AvatarVrmInfo)
async def upload_avatar_vrm(
    persona_id: str,
    file: UploadFile = File(...),
    manager=Depends(get_manager),
):
    """VRM を一つ受け取り、そのペルソナの VRM として置く (前の VRM は保存が済んでから消す)。

    付け替え・DB の書き込み・前の VRM の削除の順番の決まりは
    ``persona_avatar_vrm.install_vrm_file`` にある。
    """
    home = get_saiverse_home()
    _read_stored_path(manager, persona_id)  # ペルソナが無ければ 404

    original_name = file.filename or ""
    if not original_name.lower().endswith(VRM_SUFFIX):
        raise HTTPException(status_code=400, detail="Only .vrm files can be uploaded as the 3D avatar")

    async with _VRM_WRITE_LOCK:
        try:
            folder = ensure_avatar_dir(persona_id, home)
        except AvatarFolderOutsideError as exc:
            raise HTTPException(
                status_code=500, detail="The avatar folder is not inside the persona folder",
            ) from exc

        temp_path = new_temp_path(folder)
        try:
            size = await write_upload_bounded(file, temp_path, MAX_VRM_BYTES)
            with temp_path.open("rb") as stream:
                head = stream.read(len(GLB_MAGIC))
            if size < len(GLB_MAGIC) or head != GLB_MAGIC:
                raise HTTPException(
                    status_code=400,
                    detail="This file is not a VRM (it does not start with the glTF binary header)",
                )
            try:
                installed = install_vrm_file(
                    manager, persona_id, temp_path, safe_vrm_filename(original_name), home=home,
                )
            except PersonaNotFoundError:
                raise HTTPException(status_code=404, detail="Persona not found") from None
            except VrmReplaceError as exc:
                raise HTTPException(
                    status_code=500,
                    detail="Could not put the VRM file in place (the previous file may be in use). Try again.",
                ) from exc
        except BaseException:
            unlink_quietly(temp_path, persona_id)
            raise

    LOGGER.info(
        "Stored the VRM of %s at %s (%d bytes; previous=%r)",
        persona_id, installed.stored, size, installed.previous,
    )
    return vrm_info(persona_id, installed.path)


@router.get("/{persona_id}/avatar-vrm")
def get_avatar_vrm(persona_id: str, manager=Depends(get_manager)):
    """そのペルソナの VRM のファイルを返す。"""
    home = get_saiverse_home()
    stored = _read_stored_path(manager, persona_id)
    if not stored:
        raise HTTPException(status_code=404, detail="This persona has no VRM")
    path = resolve_stored_vrm(persona_id, stored, home)
    if path is None:
        raise HTTPException(status_code=403, detail="The VRM path is outside the persona's avatar folder")
    if not path.is_file():
        LOGGER.warning("AVATAR_VRM_PATH of %s is set (%r) but the file does not exist", persona_id, stored)
        raise HTTPException(status_code=404, detail="The VRM file was not found")
    return FileResponse(path, media_type=VRM_MEDIA_TYPE, filename=path.name)


@router.delete("/{persona_id}/avatar-vrm")
async def delete_avatar_vrm(persona_id: str, manager=Depends(get_manager)):
    """VRM を消し、``AVATAR_VRM_PATH`` を NULL にする。VRM が無くても成功を返す。"""
    home = get_saiverse_home()
    async with _VRM_WRITE_LOCK:
        with VRM_FILE_LOCK:
            stored = _read_stored_path(manager, persona_id)
            if stored:
                _write_stored_path(manager, persona_id, None)
                path = resolve_stored_vrm(persona_id, stored, home)
                if path is not None:
                    unlink_quietly(path, persona_id)
    if stored:
        LOGGER.info("Removed the VRM of %s (%r)", persona_id, stored)
    return {"success": True}
