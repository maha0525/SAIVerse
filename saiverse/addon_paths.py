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

from saiverse.data_paths import get_saiverse_home

LOGGER = logging.getLogger(__name__)


def get_addon_storage_path(addon_name: str) -> Path:
    """アドオン専用のディスクストレージディレクトリを返す。

    存在しなければ作成する。アドオンを物理削除した後もこのディレクトリは
    残る（誤削除防止のため）。明示的に消したい場合はアドオン管理側で行う。

    Args:
        addon_name: アドオン名（expansion_data/ 下のディレクトリ名）

    Returns:
        ~/.saiverse/addons/<addon_name>/ への Path
    """
    if not addon_name or "/" in addon_name or "\\" in addon_name or ".." in addon_name:
        raise ValueError(f"Invalid addon_name for storage path: {addon_name!r}")

    path = get_saiverse_home() / "addons" / addon_name
    path.mkdir(parents=True, exist_ok=True)
    LOGGER.debug("addon_paths: storage path resolved to %s", path)
    return path


__all__ = ["get_addon_storage_path"]
