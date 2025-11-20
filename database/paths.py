"""Shared helpers for locating database files."""
from __future__ import annotations

from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
DATA_DIR = PACKAGE_ROOT / "data"
DEFAULT_DB_NAME = "saiverse.db"


def ensure_data_dir() -> Path:
    """Ensure the writable data directory exists and return it."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR


def default_db_path() -> Path:
    """Return the default path to the unified SQLite database."""
    return ensure_data_dir() / DEFAULT_DB_NAME


__all__ = ["DATA_DIR", "DEFAULT_DB_NAME", "default_db_path", "ensure_data_dir"]
