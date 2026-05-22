"""旧アドオン永続データを v2 規約 (~/.saiverse/user_data/addon_data/<id>/) に移行。

設計は ``docs/intent/addon_catalog_management.md`` の「アドオン永続データ規約」を参照。

旧配置の散らかり:
    - ``~/.saiverse/addons/<addon_id>/``                 (stackchan / x-addon の独自ストレージ)
    - ``~/.saiverse/user_data/addon_files/<addon_id>/``  (voice-tts の参照音声等)
    - ``~/.saiverse/user_data/voice/out/``               (voice-tts の合成出力)

新規約: すべて ``~/.saiverse/user_data/addon_data/<addon_id>/`` 配下に統一。
voice-tts の場合は ``inputs/`` (旧 addon_files の中身) と ``outputs/`` (旧 voice/out)
にサブディレクトリを切る。

冪等性:
    - 旧パスがなければ no-op
    - 新パスに既に同名要素があれば衝突警告でスキップ (上書きしない)
    - 1 度移行完了後に再起動しても同じ migrator が走るが、何もしない

呼び出し: ``main.py`` の起動初期 (DB 初期化前後どちらでも) で 1 回呼ぶ。
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import List

from saiverse.data_paths import USER_DATA_DIR, get_saiverse_home

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _MigrationEntry:
    """1 件の (old_path → new_path) 移行。"""
    label: str             # ログ用の人間可読ラベル
    old_path: Path
    new_path: Path


def _build_migrations() -> List[_MigrationEntry]:
    home = get_saiverse_home()
    addon_data = USER_DATA_DIR / "addon_data"

    return [
        # stackchan: addons/saiverse-stackchan-addon/ を丸ごと移動
        _MigrationEntry(
            label="stackchan-addon: ~/.saiverse/addons/ → user_data/addon_data/",
            old_path=home / "addons" / "saiverse-stackchan-addon",
            new_path=addon_data / "saiverse-stackchan-addon",
        ),
        # x-addon: addons/saiverse-x-addon/ を丸ごと移動
        _MigrationEntry(
            label="x-addon: ~/.saiverse/addons/ → user_data/addon_data/",
            old_path=home / "addons" / "saiverse-x-addon",
            new_path=addon_data / "saiverse-x-addon",
        ),
        # voice-tts inputs: user_data/addon_files/saiverse-voice-tts/ → addon_data/saiverse-voice-tts/inputs/
        _MigrationEntry(
            label="voice-tts inputs: user_data/addon_files/ → addon_data/saiverse-voice-tts/inputs/",
            old_path=USER_DATA_DIR / "addon_files" / "saiverse-voice-tts",
            new_path=addon_data / "saiverse-voice-tts" / "inputs",
        ),
        # voice-tts outputs: user_data/voice/out/ → addon_data/saiverse-voice-tts/outputs/
        _MigrationEntry(
            label="voice-tts outputs: user_data/voice/out/ → addon_data/saiverse-voice-tts/outputs/",
            old_path=USER_DATA_DIR / "voice" / "out",
            new_path=addon_data / "saiverse-voice-tts" / "outputs",
        ),
    ]


def _try_move(entry: _MigrationEntry) -> str:
    """1 件の migration を試行。結果ステータス (人間可読) を返す。

    - 旧パス不存在: "skipped (no source)"
    - 旧パス空ディレクトリ: 旧パスを削除して "removed (empty source)"
    - 新パス既存 + 何か入ってる: 衝突 → 旧パスを残して警告
    - 新パス不存在 or 空: atomic rename (失敗時 copy + remove フォールバック)
    """
    old = entry.old_path
    new = entry.new_path
    if not old.exists():
        LOGGER.debug("addon_migrations: %s — old path not found, skip", entry.label)
        return "skipped (no source)"

    # 旧パスが空ディレクトリなら掃除だけして終わり (voice-tts の addons/ 空ケース等)
    if old.is_dir() and not any(old.iterdir()):
        try:
            old.rmdir()
            LOGGER.info("addon_migrations: %s — removed empty source %s", entry.label, old)
            return "removed (empty source)"
        except OSError as e:
            LOGGER.warning(
                "addon_migrations: %s — failed to remove empty source %s: %s",
                entry.label, old, e,
            )
            return f"failed to remove empty source: {e}"

    # 新パスが既に存在し、かつ非空なら衝突
    if new.exists():
        is_dir = new.is_dir()
        non_empty = is_dir and any(new.iterdir())
        if non_empty or not is_dir:
            LOGGER.warning(
                "addon_migrations: %s — destination %s already exists and is non-empty, "
                "skipping (manual merge required)",
                entry.label, new,
            )
            return "skipped (destination not empty)"
        # 空ディレクトリの new は削除して rename 用に空ける
        try:
            new.rmdir()
        except OSError as e:
            LOGGER.warning(
                "addon_migrations: %s — failed to clear empty destination %s: %s",
                entry.label, new, e,
            )
            return f"failed to clear empty destination: {e}"

    # ここまできたら new は不存在、または空 dir を削除済み
    new.parent.mkdir(parents=True, exist_ok=True)

    try:
        # 同一 volume なら atomic rename。違う volume だと OSError EXDEV
        old.rename(new)
        LOGGER.info("addon_migrations: %s — moved %s → %s", entry.label, old, new)
        return "moved"
    except OSError:
        # フォールバック: copytree (file の場合は copy2) + rmtree
        LOGGER.info(
            "addon_migrations: %s — rename failed, falling back to copy+remove",
            entry.label,
        )
        try:
            if old.is_dir():
                shutil.copytree(str(old), str(new))
                shutil.rmtree(str(old))
            else:
                shutil.copy2(str(old), str(new))
                old.unlink()
            LOGGER.info(
                "addon_migrations: %s — copy+remove fallback succeeded", entry.label
            )
            return "moved (via copy)"
        except (OSError, shutil.Error) as e:
            LOGGER.error(
                "addon_migrations: %s — copy fallback also failed: %s",
                entry.label, e,
            )
            return f"failed: {e}"


def migrate_addon_data_dirs() -> dict[str, str]:
    """旧 → 新パスの移行を実行 (起動時 1 回呼ぶ)。

    Returns:
        各 migration エントリの結果ステータス dict (label → status)。
        例: {"x-addon: ...": "moved", "voice-tts inputs: ...": "skipped (no source)"}
    """
    results: dict[str, str] = {}
    for entry in _build_migrations():
        results[entry.label] = _try_move(entry)
    moved_count = sum(1 for s in results.values() if s.startswith("moved"))
    skipped_count = sum(1 for s in results.values() if s.startswith("skipped"))
    failed_count = sum(1 for s in results.values() if s.startswith("failed"))
    LOGGER.info(
        "addon_migrations: done — %d moved, %d skipped, %d failed",
        moved_count, skipped_count, failed_count,
    )
    return results


__all__ = ["migrate_addon_data_dirs"]
