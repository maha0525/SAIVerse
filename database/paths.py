"""Shared helpers for locating database files."""
from __future__ import annotations

from pathlib import Path


# Import from data_paths for user_data directory
def _get_data_dir() -> Path:
    """Get the database directory from data_paths module."""
    try:
        from saiverse.data_paths import get_user_database_dir
        return get_user_database_dir()
    except ImportError:
        # Fallback to legacy location if data_paths not available
        return Path(__file__).resolve().parent / "data"


PACKAGE_ROOT = Path(__file__).resolve().parent
# Legacy path for backwards compatibility
LEGACY_DATA_DIR = PACKAGE_ROOT / "data"
DEFAULT_DB_NAME = "saiverse.db"


def ensure_data_dir() -> Path:
    """Ensure the writable data directory exists and return it."""
    data_dir = _get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def default_db_path() -> Path:
    """Return the default path to the unified SQLite database.
    
    First checks the new user_data/database location, then falls back to legacy.
    """
    # Check new location first
    new_db = ensure_data_dir() / DEFAULT_DB_NAME
    
    # If DB exists in legacy location but not new, use legacy for migration compatibility
    legacy_db = LEGACY_DATA_DIR / DEFAULT_DB_NAME
    if legacy_db.exists() and not new_db.exists():
        return legacy_db
    
    return new_db


__all__ = ["DEFAULT_DB_NAME", "default_db_path", "ensure_data_dir", "LEGACY_DATA_DIR"]
