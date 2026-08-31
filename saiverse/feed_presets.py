"""Feed preset management for SAIVerse.

フィードプリセット = 「購読束 + 施設 (Fixture) の見た目」のデータ
(docs/intent/rss_feed_intake.md §5 配置層)。City にフィード施設を置くときの
雛形で、FeedManager.create_fixture_from_preset が消費する。

Loads feed presets from:
    1. ~/.saiverse/user_data/feeds/  (highest priority)
    2. expansion_data/<addon>/feeds/  (middle priority)
    3. builtin_data/feeds/             (lowest priority)

スキーマ (JSON):
    {"id", "name", "description", "fixture_name", "fixture_description",
     "feeds": [{"url", "title"}]}

builtin プリセットは不変。同じ id の user_data ファイルを置くと次回リロードで
そちらが勝つ (saiverse/provider_configs.py と同じ三層先勝ちモデル)。
builtin に認証必須の供給源を入れない (intent 不変条件 4)。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .data_paths import (
    BUILTIN_DATA_DIR,
    FEEDS_DIR,
    iter_files,
)

LOGGER = logging.getLogger(__name__)


def _is_builtin_path(path: Path) -> bool:
    """Return True if path is under builtin_data/."""
    try:
        path.resolve().relative_to(BUILTIN_DATA_DIR.resolve())
        return True
    except ValueError:
        return False


def _validate_preset(raw: object, preset_file: Path) -> tuple[str, dict] | None:
    """プリセット 1 ファイルの構造検証。合格なら (preset_id, preset_dict)。

    JSON 構文が通っても構造が想定外 (トップレベルが配列や null、feeds に
    null やスカラー混入等) だと、消費側 (API /presets 一覧・
    create_fixture_from_preset) が dict 前提で触って 500 になる — キャッシュ
    登録前にここで弾く。不正は WARNING + None (そのファイルだけスキップし、
    他のプリセット = 機能全体を巻き込まない)。

    検証内容: トップレベルが dict / id (欠落はファイル名で補完)・name が
    非空文字列 / feeds が配列 (欠落は空) で各要素が dict かつ url が非空
    文字列。title は任意の文字列で、欠落・null は "" に正規化する。
    """
    if not isinstance(raw, dict):
        LOGGER.warning(
            "Feed preset %s is not a JSON object (got %s), skipping",
            preset_file.name, type(raw).__name__,
        )
        return None

    preset_id = raw.get("id") or preset_file.stem
    if not isinstance(preset_id, str) or not preset_id:
        LOGGER.warning(
            "Feed preset %s missing valid 'id', skipping", preset_file.name,
        )
        return None

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        LOGGER.warning(
            "Feed preset %s missing valid 'name', skipping", preset_file.name,
        )
        return None

    feeds = raw.get("feeds")
    if feeds is None:
        feeds = []
    if not isinstance(feeds, list):
        LOGGER.warning(
            "Feed preset %s has non-array 'feeds' (got %s), skipping",
            preset_file.name, type(feeds).__name__,
        )
        return None
    normalized_feeds: list[dict] = []
    for index, feed in enumerate(feeds):
        if not isinstance(feed, dict):
            LOGGER.warning(
                "Feed preset %s feeds[%d] is not an object (got %s), skipping",
                preset_file.name, index, type(feed).__name__,
            )
            return None
        url = feed.get("url")
        if not isinstance(url, str) or not url.strip():
            LOGGER.warning(
                "Feed preset %s feeds[%d] missing valid 'url', skipping",
                preset_file.name, index,
            )
            return None
        title = feed.get("title")
        if title is None:
            title = ""
        if not isinstance(title, str):
            LOGGER.warning(
                "Feed preset %s feeds[%d] has non-string 'title', skipping",
                preset_file.name, index,
            )
            return None
        # 未知キーは温存し、title だけ正規化した写しに差し替える
        normalized = dict(feed)
        normalized["title"] = title
        normalized_feeds.append(normalized)
    raw["feeds"] = normalized_feeds
    return preset_id, raw


def load_presets() -> dict[str, dict]:
    """Load feed presets from all sources, respecting priority.

    Returns:
        Dict mapping preset_id -> preset dict.
        Presets loaded from builtin_data automatically get ``builtin: True``.
    """
    presets: dict[str, dict] = {}
    seen_keys: set[str] = set()

    for preset_file in iter_files(FEEDS_DIR, "*.json"):
        try:
            raw = json.loads(preset_file.read_text(encoding="utf-8"))
        except Exception as exc:
            LOGGER.warning(
                "Failed to load feed preset from %s: %s", preset_file.name, exc,
            )
            continue

        validated = _validate_preset(raw, preset_file)
        if validated is None:
            continue
        preset_id, preset_data = validated

        if preset_id in seen_keys:
            continue

        if _is_builtin_path(preset_file):
            preset_data["builtin"] = True

        presets[preset_id] = preset_data
        seen_keys.add(preset_id)
        LOGGER.debug("Loaded feed preset: %s from %s", preset_id, preset_file)

    LOGGER.info("Loaded %d feed presets", len(presets))
    return presets


FEED_PRESETS: dict[str, dict] = load_presets()


def reload_presets() -> dict[str, dict]:
    """Reload feed presets from disk and refresh the global cache."""
    global FEED_PRESETS
    FEED_PRESETS = load_presets()
    LOGGER.info("Feed presets reloaded: %d presets", len(FEED_PRESETS))
    return FEED_PRESETS


def get_preset(preset_id: str) -> dict | None:
    """Get a feed preset by id, or None if not found."""
    return FEED_PRESETS.get(preset_id)
