"""ペルソナの VRM を受け取り・返し・消すルート (docs/intent/screen_avatar.md 設計 4)。

置き場の決まりは ``saiverse/persona_avatar_vrm.py``。状態を変える POST / DELETE は、
ほかの ``/api`` のルートと同じくアプリ全体のミドルウェア (``api/owner_auth.py`` の
``BrowserOriginMiddleware`` / ``OwnerAuthMiddleware``、main.py で登録) を通る。
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from api.deps import get_manager
from api.file_safety import write_upload_bounded
from database.models import AI as AIModel
from saiverse.data_paths import get_saiverse_home
from saiverse.persona_avatar_vrm import (
    GLB_MAGIC,
    MAX_VRM_BYTES,
    UPLOAD_TEMP_PREFIX,
    VRM_MEDIA_TYPE,
    VRM_SUFFIX,
    avatar_dir,
    resolve_stored_vrm,
    safe_vrm_filename,
    to_stored_path,
    vrm_info,
)

from .models import AvatarVrmInfo

LOGGER = logging.getLogger(__name__)

router = APIRouter()

#: 入れる・消すを一件ずつ通す (同じペルソナへの同時の入れ直しで、DB が指すファイルを
#: もう片方が消すことがないように)
_VRM_WRITE_LOCK = asyncio.Lock()


def _read_stored_path(manager, persona_id: str) -> Optional[str]:
    """``AI.AVATAR_VRM_PATH`` を読む。ペルソナが無ければ 404。"""
    session = manager.SessionLocal()
    try:
        row = session.query(AIModel.AVATAR_VRM_PATH).filter(AIModel.AIID == persona_id).first()
    finally:
        session.close()
    if row is None:
        raise HTTPException(status_code=404, detail="Persona not found")
    return row[0]


def _write_stored_path(manager, persona_id: str, value: Optional[str]) -> None:
    session = manager.SessionLocal()
    try:
        ai = session.query(AIModel).filter(AIModel.AIID == persona_id).first()
        if ai is None:
            raise HTTPException(status_code=404, detail="Persona not found")
        ai.AVATAR_VRM_PATH = value
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _unlink_quietly(path: Path, persona_id: str) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        LOGGER.warning("Could not remove the VRM file of %s: %s", persona_id, path, exc_info=True)


@router.post("/{persona_id}/avatar-vrm", response_model=AvatarVrmInfo)
async def upload_avatar_vrm(
    persona_id: str,
    file: UploadFile = File(...),
    manager=Depends(get_manager),
):
    """VRM を一つ受け取り、そのペルソナの VRM として置く (前の VRM は保存が済んでから消す)。"""
    home = get_saiverse_home()
    _read_stored_path(manager, persona_id)  # ペルソナが無ければ 404

    original_name = file.filename or ""
    if not original_name.lower().endswith(VRM_SUFFIX):
        raise HTTPException(status_code=400, detail="Only .vrm files can be uploaded as the 3D avatar")

    folder = avatar_dir(persona_id, home)
    async with _VRM_WRITE_LOCK:
        old_stored = _read_stored_path(manager, persona_id)
        folder.mkdir(parents=True, exist_ok=True)
        persona_root = (home / "personas" / persona_id).resolve()
        if not folder.resolve().is_relative_to(persona_root):
            LOGGER.warning("Avatar folder of %s resolves outside %s: %s", persona_id, persona_root, folder)
            raise HTTPException(status_code=500, detail="The avatar folder is not inside the persona folder")

        temp_path = folder / f"{UPLOAD_TEMP_PREFIX}{uuid.uuid4().hex}.part"
        final_path = folder / safe_vrm_filename(original_name)
        old_path = resolve_stored_vrm(persona_id, old_stored, home)
        # 前の VRM と同じ名前で入れ替えるか。このときは、置き換えた時点で前の中身が
        # 無くなるので、置き換えを最後の一手にする (下の「同じ名前のとき」)。
        replaces_old_file = old_path is not None and old_path == final_path.resolve()
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
                os.replace(temp_path, final_path)
            except OSError as exc:
                # Windows では、ブラウザへ返している最中のファイルは置き換えられない。
                # 置き換えは一度に切り替わるので、失敗したときは前のファイルがそのまま残る。
                LOGGER.warning(
                    "Could not put the VRM of %s in place at %s", persona_id, final_path, exc_info=True,
                )
                raise HTTPException(
                    status_code=500,
                    detail="Could not put the VRM file in place (the previous file may be in use). Try again.",
                ) from exc
        except BaseException:
            _unlink_quietly(temp_path, persona_id)
            raise

        new_stored = to_stored_path(final_path, home)
        if not replaces_old_file:
            # 別の名前のとき: 前の VRM はまだ残っている。DB を新しいファイルへ向け、
            # 向け終わってから前の VRM を消す。DB に書けなかったら、置いたばかりの
            # ファイル (誰も指していない) を消して、前の VRM と DB の値をそのまま残す。
            try:
                _write_stored_path(manager, persona_id, new_stored)
            except BaseException:
                _unlink_quietly(final_path, persona_id)
                LOGGER.warning("Could not save AVATAR_VRM_PATH for %s", persona_id, exc_info=True)
                raise
            if old_path is not None:
                _unlink_quietly(old_path, persona_id)
        # 同じ名前のとき: DB はもうこのファイルを指しているので、DB には書かない。
        # 上の置き換えの後に失敗しうる処理を残さない — 置き換えた後で失敗を返すと、
        # 「失敗と言われたのに中身は新しい VRM に替わっていて、前の VRM は戻らない」になる。

    LOGGER.info(
        "Stored the VRM of %s at %s (%d bytes; previous=%r)",
        persona_id, new_stored, size, old_stored,
    )
    return vrm_info(persona_id, final_path)


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
        stored = _read_stored_path(manager, persona_id)
        if stored:
            _write_stored_path(manager, persona_id, None)
            path = resolve_stored_vrm(persona_id, stored, home)
            if path is not None:
                _unlink_quietly(path, persona_id)
    if stored:
        LOGGER.info("Removed the VRM of %s (%r)", persona_id, stored)
    return {"success": True}
