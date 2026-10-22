"""アドオン専用ストレージパスのヘルパー。

アドオンが独自スキーマの SQLite DB やキャッシュファイル等のローカル状態を
持ちたい場合の規約上の保存場所を提供する。

使い方:
    from saiverse.addon_paths import get_addon_storage_path

    storage_dir = get_addon_storage_path("saiverse-x-addon")
    db_path = storage_dir / "x_reply_log.db"

設計方針:
    - コアの saiverse.db には触らせない（アドオンが独自テーブルを作りたい場合は
      専用 SQLite を持つ）
    - リポジトリ内の expansion_data/ ではなく ~/.saiverse/addons/ 配下に置く
      （git pull でユーザーデータが消えないように）
    - ペルソナ別データは ~/.saiverse/personas/<id>/ なので、ここはアドオン
      共有データ専用
"""
from __future__ import annotations

import json
import logging
import platform
import re
from pathlib import Path

from saiverse.data_paths import USER_DATA_DIR, get_saiverse_home

LOGGER = logging.getLogger(__name__)

# 専用の環境の中に置く、作ったときの記録 (使った Python のバージョン)
ADDON_ENV_RECORD_NAME = "saiverse_env.json"
_ENV_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class AddonEnvError(RuntimeError):
    """アドオン専用の Python 環境が使えない (無い / 本体の Python と食い違う)。"""


def _validate_addon_id(addon_name: str) -> None:
    if not addon_name or "/" in addon_name or "\\" in addon_name or ".." in addon_name:
        raise ValueError(f"Invalid addon_name: {addon_name!r}")


def _validate_env_name(name: str) -> None:
    if not _ENV_NAME_RE.match(name or ""):
        raise ValueError(f"Invalid addon env name: {name!r}")


def get_addon_install_dir(addon_name: str) -> Path:
    """アドオンの導入物の置き場所を返す (作成はしない)。

    返値: ``~/.saiverse/addon_install/<addon_name>/``

    専用の Python 環境 (``envs/<name>/``) と導入時の答え (``setup_answers.json``)
    を置く。世界の状態ではなく再生成できる導入物なので、スナップショットの
    対象外 (``scripts/snapshot.py`` の ``EXCLUDED_FROM_SNAPSHOT``)。アンインストール
    ではアドオンのフォルダと一緒に必ず消える。
    設計: ``docs/intent/addon_catalog_management.md``「導入時の質問と、アドオン専用の
    Python 環境」。
    """
    _validate_addon_id(addon_name)
    return get_saiverse_home() / "addon_install" / addon_name


def get_addon_env_dir(addon_name: str, env_name: str) -> Path:
    """アドオン専用の Python 環境のフォルダ (作成はしない)。"""
    _validate_env_name(env_name)
    return get_addon_install_dir(addon_name) / "envs" / env_name


def addon_env_python_path(env_dir: Path) -> Path:
    """venv の中の python 実行ファイルの場所 (存在は確かめない)。"""
    if platform.system() == "Windows":
        return env_dir / "Scripts" / "python.exe"
    return env_dir / "bin" / "python"


def current_python_version() -> str:
    """本体の Python のバージョン (専用の環境の作り直しの判定に使う)。"""
    return platform.python_version()


_PYTHON_MINOR_RE = re.compile(r"^(\d+)\.(\d+)(?:\.|$)")


def python_minor_version(version: object) -> tuple[int, int] | None:
    """Python のバージョンの文字列 (``"3.13.2"``) から ``(major, minor)`` を返す。

    解析できない値 (文字列でない・数字の並びで始まらない) は None。
    """
    if not isinstance(version, str):
        return None
    match = _PYTHON_MINOR_RE.match(version.strip())
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def addon_env_matches_current_python(record: dict | None) -> bool:
    """専用の環境の記録が、本体の Python と同じ major.minor で作られたものか。

    venv はパッチ更新 (3.13.2 → 3.13.17) を跨いで使える: venv の中の python は
    元の Python の場所を指し、標準ライブラリと C 拡張の ABI は major.minor の中で
    変わらない。完全一致を要求すると、Python のパッチ更新だけで専用の環境を使う
    処理が止まる。記録は診断のためにフルバージョンのまま書く。

    記録が無い・``python_version`` が解析できないときは False。
    ``_ensure_addon_env`` / ``_env_needs_full_run`` / ``get_addon_env_python`` が
    この同じ判定を使う。
    """
    if record is None:
        return False
    recorded = python_minor_version(record.get("python_version"))
    return recorded is not None and recorded == python_minor_version(current_python_version())


def read_addon_env_record(env_dir: Path) -> dict | None:
    """専用の環境を作ったときの記録。無い / 壊れていれば None。"""
    path = env_dir / ADDON_ENV_RECORD_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        LOGGER.warning("addon_paths: unreadable env record %s", path, exc_info=True)
        return None
    return data if isinstance(data, dict) else None


def get_addon_env_python(addon_name: str, env_name: str) -> Path:
    """アドオン専用の Python 環境の python の場所を返す。

    アドオンのコードは、専用の環境のパッケージを使う処理を、この Python で別の
    プロセスとして起動する (SAIVerse のプロセスの中では import できない)。

    環境が無いとき、作ったときの記録が無いとき、記録された Python のバージョンが
    解析できないとき、記録された Python の major.minor (``3.13``) が本体の Python の
    major.minor と食い違うときは ``AddonEnvError`` を投げる。パッチ番号だけの違い
    (記録 ``3.13.2``、本体 ``3.13.17``) は同じ環境として使う
    (``addon_env_matches_current_python``)。古い環境の場所を黙って返さない —
    作り直しは setup のやり直しで行う。
    """
    env_dir = get_addon_env_dir(addon_name, env_name)
    python = addon_env_python_path(env_dir)
    record = read_addon_env_record(env_dir)
    if record is None or not python.exists():
        raise AddonEnvError(
            f"addon {addon_name!r}: Python environment {env_name!r} is not set up "
            f"({env_dir}). Run the addon's setup again."
        )
    recorded = record.get("python_version")
    recorded_minor = python_minor_version(recorded)
    if recorded_minor is None:
        raise AddonEnvError(
            f"addon {addon_name!r}: Python environment {env_name!r} has an unreadable "
            f"Python version in its record ({recorded!r}). "
            "Run the addon's setup again to rebuild it."
        )
    if not addon_env_matches_current_python(record):
        current = current_python_version()
        current_minor = python_minor_version(current)
        current_label = (
            f"{current_minor[0]}.{current_minor[1]}" if current_minor else current
        )
        raise AddonEnvError(
            f"addon {addon_name!r}: Python environment {env_name!r} was created with "
            f"Python {recorded_minor[0]}.{recorded_minor[1]} ({recorded}), but SAIVerse "
            f"now runs Python {current_label} ({current}). "
            "Run the addon's setup again to rebuild it."
        )
    return python


def get_addon_data_dir(addon_name: str) -> Path:
    """アドオン永続データの統一配置ディレクトリを返す (v2 規約)。

    返値: ``~/.saiverse/user_data/addon_data/<addon_name>/``

    `docs/intent/addon_catalog_management.md` の「アドオン永続データ規約」を
    参照。既存 ``get_addon_storage_path`` (legacy: ``~/.saiverse/addons/``) と
    は別経路で、Phase 4 で各アドオンを順次こちらに移行 + 自動マイグレーション
    する予定。新規コードは必ずこちらを使うこと。
    """
    _validate_addon_id(addon_name)
    path = USER_DATA_DIR / "addon_data" / addon_name
    path.mkdir(parents=True, exist_ok=True)
    LOGGER.debug("addon_paths: data dir resolved to %s", path)
    return path


def get_addon_storage_path(addon_name: str) -> Path:
    """アドオン専用のディスクストレージディレクトリを返す。

    存在しなければ作成する。アドオンを物理削除した後もこのディレクトリは
    残る（誤削除防止のため）。明示的に消したい場合はアドオン管理側で行う。

    Args:
        addon_name: アドオン名（expansion_data/ 下のディレクトリ名）

    Returns:
        ~/.saiverse/addons/<addon_name>/ への Path
    """
    _validate_addon_id(addon_name)
    path = get_saiverse_home() / "addons" / addon_name
    path.mkdir(parents=True, exist_ok=True)
    LOGGER.debug("addon_paths: storage path resolved to %s", path)
    return path


__all__ = [
    "AddonEnvError",
    "get_addon_storage_path",
    "get_addon_data_dir",
    "get_addon_install_dir",
    "get_addon_env_dir",
    "get_addon_env_python",
]
