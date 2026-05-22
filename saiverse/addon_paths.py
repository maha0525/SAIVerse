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

import logging
from pathlib import Path

from saiverse.data_paths import USER_DATA_DIR, get_saiverse_home

LOGGER = logging.getLogger(__name__)


def _validate_addon_id(addon_name: str) -> None:
    if not addon_name or "/" in addon_name or "\\" in addon_name or ".." in addon_name:
        raise ValueError(f"Invalid addon_name: {addon_name!r}")


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


__all__ = ["get_addon_storage_path", "get_addon_data_dir"]
