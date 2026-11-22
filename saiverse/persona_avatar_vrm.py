"""ペルソナの VRM の置き場 (docs/intent/screen_avatar.md 設計 4)。

VRM はペルソナの持ち物で、ファイルは ``<SAIVERSE_HOME>/personas/<persona_id>/avatar/``
に一つだけ置く。DB の ``AI.AVATAR_VRM_PATH`` は ``SAIVERSE_HOME`` からの相対パス
(区切りは ``/``)。

ここに置くのは、置き場の決まり (フォルダ・名前・パスの解決)、avatar フォルダの中の
一時ファイルを本来の名前へ付け替えて DB を書く処理 (``install_vrm_file``)、Godot の
アドオンから呼ぶ関数 (``current_vrm_file`` / ``adopt_local_vrm`` /
``post_vrm_adoption_failed_alert``)、ペルソナを消すときの後始末。HTTP で受け取り・
返す処理は ``api/routes/people/avatar_vrm.py`` にある。
"""
from __future__ import annotations

import logging
import os
import shutil
import stat
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from saiverse.data_paths import get_saiverse_home

LOGGER = logging.getLogger(__name__)

#: VRM のファイルの付け替え・削除と ``AI.AVATAR_VRM_PATH`` の書き込みを一件ずつ通す。
#: アップロードのルート (イベントループの中) と、Godot のアドオン (別のスレッド) の
#: 両方から取る。ファイルの受け取りや写す処理の間は持たない (付け替えと DB の書き込み
#: だけを守る。イベントループを長く止めないため)。
VRM_FILE_LOCK = threading.Lock()

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


def vrm_url(persona_id: str, version: str) -> str:
    """VRM を返す API の URL。``version`` は、ファイルを入れ替えるたびに変わる文字列。

    画面は URL が同じなら読み込み済みの VRM を使い続け、読み込めなかった URL は読み直さない
    (frontend/src/lib/vrmAvatarEngine.ts)。だから、入れ替えたのに URL が同じだと、画面は
    前の姿のままになる。
    """
    return f"/api/people/{persona_id}/avatar-vrm?v={version}"


def vrm_version(stat_result: os.stat_result) -> str:
    """URL の ``v`` に入れる文字列。更新時刻 (ナノ秒) と大きさを並べたもの。

    秒に丸めた更新時刻だけでは、同じ一秒の中で入れ替えたときに同じ値になる。
    更新時刻の刻みが粗いファイルシステムのために、大きさも並べる。
    """
    return f"{stat_result.st_mtime_ns}-{stat_result.st_size}"


def vrm_info(persona_id: str, path: Path) -> Dict[str, Any]:
    """VRM の情報 (``filename`` / ``size_bytes`` / ``updated_at`` / ``url``)。ファイルが無ければ OSError。"""
    stat_result = Path(path).stat()
    return {
        "filename": Path(path).name,
        "size_bytes": int(stat_result.st_size),
        "updated_at": int(stat_result.st_mtime),
        "url": vrm_url(persona_id, vrm_version(stat_result)),
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


class PersonaNotFoundError(LookupError):
    """``AI`` 表にそのペルソナの行が無い。"""


class VrmReplaceError(OSError):
    """一時ファイルを本来の名前へ付け替えられなかった (前の VRM と DB の値はそのまま)。"""


def read_stored_vrm_path(manager, persona_id: str) -> Optional[str]:
    """``AI.AVATAR_VRM_PATH`` を ``manager.SessionLocal`` で読む。ペルソナが無ければ PersonaNotFoundError。"""
    from database.models import AI as AIModel

    session = manager.SessionLocal()
    try:
        row = session.query(AIModel.AVATAR_VRM_PATH).filter(AIModel.AIID == persona_id).first()
    finally:
        session.close()
    if row is None:
        raise PersonaNotFoundError(persona_id)
    return row[0]


def write_stored_vrm_path(manager, persona_id: str, value: Optional[str]) -> None:
    """``AI.AVATAR_VRM_PATH`` を書く。ペルソナが無ければ PersonaNotFoundError。"""
    from database.models import AI as AIModel

    session = manager.SessionLocal()
    try:
        ai = session.query(AIModel).filter(AIModel.AIID == persona_id).first()
        if ai is None:
            raise PersonaNotFoundError(persona_id)
        ai.AVATAR_VRM_PATH = value
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def unlink_quietly(path: Path, persona_id: str) -> None:
    """ファイルを消す。無ければ何もしない。消せなければ WARNING を出す (例外は上げない)。"""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        LOGGER.warning("Could not remove the VRM file of %s: %s", persona_id, path, exc_info=True)


@dataclass(frozen=True)
class InstalledVrm:
    """``install_vrm_file`` の結果。"""

    path: Path               # 置いた先
    stored: str              # ``AI.AVATAR_VRM_PATH`` の値 (置いた後)
    previous: Optional[str]  # ``AI.AVATAR_VRM_PATH`` の値 (置く前)


def install_vrm_file(
    manager,
    persona_id: str,
    temp_path: Path,
    filename: str,
    *,
    home: Optional[Path] = None,
    keep_existing: bool = False,
) -> Optional[InstalledVrm]:
    """avatar フォルダの中の一時ファイル ``temp_path`` を、``filename`` の名前で、そのペルソナの VRM にする。

    ``filename`` は ``safe_vrm_filename`` を通した名前。中身の検査は呼び出し元が済ませておく。
    :data:`VRM_FILE_LOCK` を取り、DB の今の値を読み直してから付け替える。

    - 前の VRM と別の名前のとき: 付け替えた後で DB を新しいファイルへ向け、向け終わって
      から前の VRM を消す。DB に書けなかったら、置いたばかりのファイル (誰も指していない)
      を消して、前の VRM と DB の値をそのまま残し、例外をそのまま上げる。
    - 前の VRM と同じ名前のとき: 付け替えた時点で前の中身が無くなるので、付け替えを最後の
      一手にする。DB はもうこのファイルを指しているので、DB には書かない。付け替えた後で
      失敗を返すと、「失敗と言われたのに中身は新しい VRM に替わっていて、前の VRM は
      戻らない」になるため。
    - ``keep_existing`` が真で、DB の値が avatar フォルダの中の実際にあるファイルを指して
      いるときは、何もせず None を返す。

    ``temp_path`` は、成功したときは無くなる。それ以外のときに消すのは呼び出し元。

    Raises:
        PersonaNotFoundError: ペルソナが無い。
        VrmReplaceError: 付け替えられなかった。Windows では、ブラウザへ返している最中の
            ファイルは置き換えられない。置き換えは一度に切り替わるので、前のファイルは
            そのまま残る。
        Exception: DB に書けなかったときは、``write_stored_vrm_path`` の例外をそのまま。
    """
    base = Path(home) if home is not None else get_saiverse_home()
    final_path = avatar_dir(persona_id, base) / filename
    with VRM_FILE_LOCK:
        old_stored = read_stored_vrm_path(manager, persona_id)
        old_path = resolve_stored_vrm(persona_id, old_stored, base)
        if keep_existing and old_path is not None and old_path.is_file():
            return None
        replaces_old_file = old_path is not None and old_path == final_path.resolve()
        try:
            os.replace(temp_path, final_path)
        except OSError as exc:
            LOGGER.warning(
                "Could not put the VRM of %s in place at %s", persona_id, final_path, exc_info=True,
            )
            raise VrmReplaceError(str(exc)) from exc

        new_stored = to_stored_path(final_path, base)
        if not replaces_old_file:
            try:
                write_stored_vrm_path(manager, persona_id, new_stored)
            except BaseException:
                unlink_quietly(final_path, persona_id)
                LOGGER.warning("Could not save AVATAR_VRM_PATH for %s", persona_id, exc_info=True)
                raise
            if old_path is not None:
                unlink_quietly(old_path, persona_id)
    return InstalledVrm(path=final_path, stored=new_stored, previous=old_stored)


class AvatarFolderOutsideError(OSError):
    """avatar フォルダが、ペルソナのフォルダの外へ解決される。"""


def ensure_avatar_dir(persona_id: str, home: Optional[Path] = None) -> Path:
    """avatar フォルダを作って返す。

    Raises:
        AvatarFolderOutsideError: ペルソナのフォルダの外へ解決される。
        OSError: フォルダを作れなかった。
    """
    base = Path(home) if home is not None else get_saiverse_home()
    folder = avatar_dir(persona_id, base)
    folder.mkdir(parents=True, exist_ok=True)
    persona_root = (base / "personas" / persona_id).resolve()
    if not folder.resolve().is_relative_to(persona_root):
        LOGGER.warning("Avatar folder of %s resolves outside %s: %s", persona_id, persona_root, folder)
        raise AvatarFolderOutsideError(f"The avatar folder of {persona_id} is not inside the persona folder")
    return folder


def new_temp_path(folder: Path) -> Path:
    """avatar フォルダの中の、受け取り途中のファイルの場所 (作らない)。"""
    return folder / f"{UPLOAD_TEMP_PREFIX}{uuid.uuid4().hex}.part"


# --- Godot のアドオンから呼ぶ関数 (docs/intent/screen_avatar.md の段階 2 の (3)、案 A) ---


def current_vrm_file(manager, persona_id: str) -> Optional[Path]:
    """そのペルソナの VRM のファイル (絶対パス)。

    DB の ``AI.AVATAR_VRM_PATH`` を ``manager.SessionLocal`` で読み、``resolve_stored_vrm``
    で解決する。ペルソナが無い・値が無い・avatar フォルダの外を指している・ファイルが
    無いときは None。例外は上げない (DB を読めなかったときは WARNING を出して None)。
    """
    try:
        stored = read_stored_vrm_path(manager, persona_id)
    except PersonaNotFoundError:
        LOGGER.debug("current_vrm_file: persona %s does not exist", persona_id)
        return None
    except Exception:  # noqa: BLE001 — 呼び出し元 (アドオン) へ例外を上げない
        LOGGER.warning("Could not read AVATAR_VRM_PATH of %s", persona_id, exc_info=True)
        return None
    try:
        path = resolve_stored_vrm(persona_id, stored)
        if path is None:
            return None
        if not path.is_file():
            LOGGER.warning(
                "AVATAR_VRM_PATH of %s is set (%r) but the file does not exist", persona_id, stored,
            )
            return None
        return path
    except Exception:  # noqa: BLE001 — 呼び出し元 (アドオン) へ例外を上げない
        LOGGER.warning("Could not resolve the VRM of %s (%r)", persona_id, stored, exc_info=True)
        return None


VRM_ADOPT_ADOPTED = "adopted"
VRM_ADOPT_ALREADY_HAS_VRM = "already_has_vrm"
VRM_ADOPT_PERSONA_MISSING = "persona_missing"
VRM_ADOPT_SOURCE_MISSING = "source_missing"
VRM_ADOPT_NOT_VRM = "not_vrm"
VRM_ADOPT_TOO_LARGE = "too_large"
VRM_ADOPT_FAILED = "failed"


@dataclass(frozen=True)
class VrmAdoptResult:
    """``adopt_local_vrm`` の結果。"""

    status: str            # VRM_ADOPT_* のどれか
    path: Optional[Path]   # 写した先 (status が adopted のときだけ)
    detail: str            # ログと知らせに使う短い英語の説明 (空でもよい)


def adopt_local_vrm(manager, persona_id: str, source: Path) -> VrmAdoptResult:
    """本体と同じ PC の上にある VRM のファイルを、そのペルソナの VRM として本体の置き場所へ写す。

    - そのペルソナに既に VRM がある (``current_vrm_file`` が None でない) ときは何もせず
      already_has_vrm。写している間に別の経路で VRM が入ったときも同じ (写したものは消す)。
    - source が無い・ファイルでないときは source_missing。名前が ``.vrm`` で終わらない、
      または先頭が ``GLB_MAGIC`` でないときは not_vrm。``MAX_VRM_BYTES`` を超えるときは
      too_large。
    - 写すときは、avatar フォルダの中の一時ファイルへ中身を変えずに写してから、
      ``install_vrm_file`` で ``safe_vrm_filename(source.name)`` の名前にし、
      ``AI.AVATAR_VRM_PATH`` を書く。DB に書けなかったら、置いたファイルを消して failed。
    - source のファイルは変えない・消さない。
    - 例外は上げない (想定外の失敗は WARNING を出して failed)。
    """
    def result(status: str, detail: str = "", path: Optional[Path] = None) -> VrmAdoptResult:
        log = LOGGER.info if status in (VRM_ADOPT_ADOPTED, VRM_ADOPT_ALREADY_HAS_VRM) else LOGGER.warning
        log("adopt_local_vrm: persona=%s source=%s status=%s %s", persona_id, source, status, detail)
        return VrmAdoptResult(status=status, path=path, detail=detail)

    try:
        source_path = Path(source)
        try:
            read_stored_vrm_path(manager, persona_id)
        except PersonaNotFoundError:
            return result(VRM_ADOPT_PERSONA_MISSING, "persona not found")
        if current_vrm_file(manager, persona_id) is not None:
            return result(VRM_ADOPT_ALREADY_HAS_VRM, "persona already has a VRM")

        if not source_path.is_file():
            return result(VRM_ADOPT_SOURCE_MISSING, "source file not found")
        if not source_path.name.lower().endswith(VRM_SUFFIX):
            return result(VRM_ADOPT_NOT_VRM, "file name does not end with .vrm")
        with source_path.open("rb") as stream:
            head = stream.read(len(GLB_MAGIC))
        if head != GLB_MAGIC:
            return result(VRM_ADOPT_NOT_VRM, "file does not start with the glTF binary header")
        size = source_path.stat().st_size
        if size > MAX_VRM_BYTES:
            return result(VRM_ADOPT_TOO_LARGE, f"file is {size} bytes (limit {MAX_VRM_BYTES})")

        home = get_saiverse_home()
        folder = ensure_avatar_dir(persona_id, home)
        temp_path = new_temp_path(folder)
        try:
            shutil.copyfile(source_path, temp_path)
            copied = temp_path.stat().st_size
            if copied > MAX_VRM_BYTES:
                # 写している間に source が大きくなった
                return result(VRM_ADOPT_TOO_LARGE, f"file is {copied} bytes (limit {MAX_VRM_BYTES})")
            installed = install_vrm_file(
                manager, persona_id, temp_path, safe_vrm_filename(source_path.name),
                home=home, keep_existing=True,
            )
        except PersonaNotFoundError:
            return result(VRM_ADOPT_PERSONA_MISSING, "persona not found")
        except Exception as exc:  # noqa: BLE001 — 呼び出し元 (アドオン) へ例外を上げない
            LOGGER.warning("adopt_local_vrm: could not adopt %s for %s", source_path, persona_id, exc_info=True)
            return result(VRM_ADOPT_FAILED, f"{type(exc).__name__}: {exc}")
        finally:
            unlink_quietly(temp_path, persona_id)
        if installed is None:
            return result(VRM_ADOPT_ALREADY_HAS_VRM, "persona already has a VRM")
        return result(VRM_ADOPT_ADOPTED, f"{size} bytes", installed.path)
    except Exception as exc:  # noqa: BLE001 — 呼び出し元 (アドオン) へ例外を上げない
        LOGGER.warning("adopt_local_vrm: unexpected failure for %s (%s)", persona_id, source, exc_info=True)
        return VrmAdoptResult(status=VRM_ADOPT_FAILED, path=None, detail=f"{type(exc).__name__}: {exc}")


#: 知らせの本文のキー。理由ごとに文を分ける (画面の文言の表の、ほかの知らせと同じ形)。
#: 知らせは閉じるまでアップデートをまたいで残るので、キーの名前は後から変えない。
_ADOPTION_FAILED_MESSAGE_KEYS = {
    VRM_ADOPT_SOURCE_MISSING: "api.runtimeAlerts.vrmAdoptionFailed.sourceMissing",
    VRM_ADOPT_NOT_VRM: "api.runtimeAlerts.vrmAdoptionFailed.notVrm",
    VRM_ADOPT_TOO_LARGE: "api.runtimeAlerts.vrmAdoptionFailed.tooLarge",
    VRM_ADOPT_FAILED: "api.runtimeAlerts.vrmAdoptionFailed.failed",
}


def post_vrm_adoption_failed_alert(persona_id: str, persona_name: str, source: str, status: str) -> None:
    """写せなかったペルソナを、画面の右下の知らせ (``saiverse/runtime_alerts.py``) に出す。

    key は ``vrm_adoption_failed:<persona_id>``、level は ``"warning"``。本文は ``status``
    ごとに分かれ、表に無い status は ``failed`` の文になる。例外は上げない。
    """
    try:
        from api.ui_messages import ui_message
        from saiverse.runtime_alerts import post_runtime_alert

        message_key = _ADOPTION_FAILED_MESSAGE_KEYS.get(status)
        if message_key is None:
            LOGGER.warning(
                "post_vrm_adoption_failed_alert: unexpected status %r for %s; using the generic message",
                status, persona_id,
            )
            message_key = _ADOPTION_FAILED_MESSAGE_KEYS[VRM_ADOPT_FAILED]
        post_runtime_alert(
            f"vrm_adoption_failed:{persona_id}",
            "warning",
            ui_message("api.runtimeAlerts.vrmAdoptionFailed.title"),
            ui_message(message_key, persona=persona_name or persona_id),
            details={"persona_id": persona_id, "source": source, "status": status},
        )
    except Exception:  # noqa: BLE001 — 知らせの失敗で呼び出し元 (アドオンの読み込み) を止めない
        LOGGER.warning("post_vrm_adoption_failed_alert: could not post the alert", exc_info=True)


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
