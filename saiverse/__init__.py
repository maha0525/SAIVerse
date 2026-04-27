"""SAIVerse core package."""
from __future__ import annotations

import logging
from pathlib import Path

LOGGER = logging.getLogger(__name__)


def _read_version() -> str:
    """リポジトリルートの VERSION ファイルから現在バージョンを取得する。

    pyproject.toml の `[tool.setuptools.dynamic]` も同じファイルを参照しており、
    これがバージョン情報の唯一のソース。読めなかった場合は明示的に
    "0.0.0+unknown" を返し、ログに警告を残す（黙ってデフォルト値を返さない）。
    """
    version_file = Path(__file__).resolve().parent.parent / "VERSION"
    if not version_file.exists():
        LOGGER.warning("VERSION file not found at %s", version_file)
        return "0.0.0+unknown"
    try:
        text = version_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        LOGGER.warning("Failed to read VERSION file %s: %s", version_file, exc, exc_info=True)
        return "0.0.0+unknown"
    if not text:
        LOGGER.warning("VERSION file %s is empty", version_file)
        return "0.0.0+unknown"
    return text


__version__: str = _read_version()
