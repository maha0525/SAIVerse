"""ペルソナの VRM の置き場 (docs/intent/screen_avatar.md 設計 4)。

VRM はペルソナの持ち物で、ファイルは ``<SAIVERSE_HOME>/personas/<persona_id>/avatar/``
に一つだけ置く。DB の ``AI.AVATAR_VRM_PATH`` は ``SAIVERSE_HOME`` からの相対パス
(区切りは ``/``)。

ここに置くのは、置き場の決まり (フォルダ・名前・パスの解決) と、ペルソナを消すときの
後始末だけ。受け取り・返す処理は ``api/routes/people/avatar_vrm.py`` にある。
"""
from __future__ import annotations

import logging
import os
import stat
from pathlib import Path
from typing import Any, Dict, Optional

from saiverse.data_paths import get_saiverse_home

LOGGER = logging.getLogger(__name__)

AVATAR_DIR_NAME = "avatar"
VRM_SUFFIX = ".vrm"
#: glTF バイナリ (VRM はこの形) の先頭 4 バイト
GLB_MAGIC = b"glTF"
MAX_VRM_BYTES = 300 * 1024 * 1024
DEFAULT_VRM_FILENAME = "avatar.vrm"
VRM_MEDIA_TYPE = "model/gltf-binary"
#: 受け取り途中のファイルの名前の頭。avatar フォルダの中で、名前の付け替えの前だけ使う
UPLOAD_TEMP_PREFIX = ".upload-"

_FORBIDDEN_NAME_CHARS = set('<>:"|?*')


def avatar_dir(persona_id: str, home: Optional[Path] = None) -> Path:
    """そのペルソナの avatar フォルダ (作らない)。"""
    base = Path(home) if home is not None else get_saiverse_home()
    return base / "personas" / persona_id / AVATAR_DIR_NAME


def to_stored_path(path: Path, home: Optional[Path] = None) -> str:
    """ファイルの場所を ``AI.AVATAR_VRM_PATH`` に保存する形 (SAIVERSE_HOME からの相対、``/`` 区切り) にする。"""
    base = Path(home) if home is not None else get_saiverse_home()
    return Path(path).resolve().relative_to(base.resolve()).as_posix()


def resolve_stored_vrm(
    persona_id: str, stored: Optional[str], home: Optional[Path] = None
) -> Optional[Path]:
    """``AI.AVATAR_VRM_PATH`` の値を解決し、そのペルソナの avatar フォルダの中を指すときだけ返す。

    値が空なら None。解決した先が avatar フォルダの外 (``..`` やほかのペルソナの
    フォルダ、絶対パス) なら WARNING を出して None。ファイルがあるかは見ない。
    """
    if not stored:
        return None
    base = Path(home) if home is not None else get_saiverse_home()
    root = avatar_dir(persona_id, base).resolve()
    try:
        resolved = (base / stored).resolve()
    except (OSError, RuntimeError, ValueError):
        LOGGER.warning(
            "AVATAR_VRM_PATH of %s could not be resolved: %r", persona_id, stored,
            exc_info=True,
        )
        return None
    if resolved == root or not resolved.is_relative_to(root):
        LOGGER.warning(
            "AVATAR_VRM_PATH of %s points outside %s: %r; not serving it",
            persona_id, root, stored,
        )
        return None
    return resolved


def safe_vrm_filename(filename: Optional[str]) -> str:
    """アップロードされた元の名前を、avatar フォルダに置ける一つの名前にする。

    フォルダの区切り (``/`` ``\\``) より前を捨て、Windows で名前に使えない文字と
    制御文字を ``_`` に置き換え、拡張子の前の部分の前後の空白と ``.`` を落とす
    (``.`` で始まる名前は受け取り途中のファイルと紛れ、``..`` は親フォルダを指すため)。
    拡張子の前が空になったら :data:`DEFAULT_VRM_FILENAME` を返す。
    """
    name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(
        "_" if (ch in _FORBIDDEN_NAME_CHARS or ord(ch) < 32) else ch for ch in name
    )
    name = name.strip()
    if name.lower().endswith(VRM_SUFFIX):
        stem, suffix = name[: -len(VRM_SUFFIX)], name[-len(VRM_SUFFIX):]
    else:
        stem, suffix = name, VRM_SUFFIX
    stem = stem.strip().strip(". ")
    if not stem:
        return DEFAULT_VRM_FILENAME
    return stem + suffix


def vrm_url(persona_id: str, updated_at: int) -> str:
    return f"/api/people/{persona_id}/avatar-vrm?v={int(updated_at)}"


def vrm_info(persona_id: str, path: Path) -> Dict[str, Any]:
    """VRM の情報 (``filename`` / ``size_bytes`` / ``updated_at`` / ``url``)。ファイルが無ければ OSError。"""
    stat = Path(path).stat()
    updated_at = int(stat.st_mtime)
    return {
        "filename": Path(path).name,
        "size_bytes": int(stat.st_size),
        "updated_at": updated_at,
        "url": vrm_url(persona_id, updated_at),
    }


def existing_vrm_info(
    persona_id: str, stored: Optional[str], home: Optional[Path] = None
) -> Optional[Dict[str, Any]]:
    """DB の値が指す VRM の情報。値が無い・avatar フォルダの外・ファイルが無いときは None。

    値はあるのにファイルが無い・読めないときは WARNING を出す。
    """
    path = resolve_stored_vrm(persona_id, stored, home)
    if path is None:
        return None
    if not path.is_file():
        LOGGER.warning(
            "AVATAR_VRM_PATH of %s is set (%r) but the file does not exist",
            persona_id, stored,
        )
        return None
    try:
        return vrm_info(persona_id, path)
    except OSError:
        LOGGER.warning("Could not stat the VRM of %s at %s", persona_id, path, exc_info=True)
        return None


def _is_link(path: Path) -> bool:
    """symlink か Windows のジャンクションか。

    ``Path.is_junction`` は Python 3.12 から。3.11 (pyproject.toml の下限) では
    ``os.lstat`` の ``st_file_attributes`` の reparse point の印で見る。
    """
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction is not None:
        return bool(is_junction())
    attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def remove_avatar_files(persona_id: str, home: Optional[Path] = None) -> None:
    """そのペルソナの avatar フォルダの中のファイルを消し、空になったフォルダも消す。

    ペルソナの削除 (``delete_ai``) が ai 行を消した後に呼ぶ。消すのはフォルダの直下の
    ファイルだけで、リンクや下のフォルダは辿らない (残ったものは WARNING に出す)。
    失敗は WARNING に残し、例外は上げない (ai 行の削除はもう確定しているため)。
    """
    folder = avatar_dir(persona_id, home)
    try:
        if _is_link(folder):
            LOGGER.warning("Avatar folder of %s is a link; leaving it: %s", persona_id, folder)
            return
        if not folder.is_dir():
            return
        for entry in folder.iterdir():
            if _is_link(entry) or not entry.is_file():
                LOGGER.warning(
                    "Leaving a non-file entry in the avatar folder of %s: %s", persona_id, entry,
                )
                continue
            entry.unlink()
            LOGGER.info("Removed the avatar file of deleted persona %s: %s", persona_id, entry)
        folder.rmdir()
    except OSError:
        LOGGER.warning(
            "Could not remove the avatar folder of deleted persona %s: %s",
            persona_id, folder, exc_info=True,
        )
