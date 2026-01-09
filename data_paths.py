"""Unified data path management for SAIVerse.

Provides centralized access to user_data and builtin_data directories.
Files in user_data take priority over builtin_data.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator

LOGGER = logging.getLogger(__name__)

# Root directories
PROJECT_ROOT = Path(__file__).resolve().parent
USER_DATA_DIR = PROJECT_ROOT / "user_data"
BUILTIN_DATA_DIR = PROJECT_ROOT / "builtin_data"

# Subdirectory names
TOOLS_DIR = "tools"
PHENOMENA_DIR = "phenomena"
PLAYBOOKS_DIR = "playbooks"
MODELS_DIR = "models"
DATABASE_DIR = "database"
PROMPTS_DIR = "prompts"
ICONS_DIR = "icons"


def get_data_paths(subdir: str) -> list[Path]:
    """Get both user_data and builtin_data paths for a subdirectory.
    
    Returns paths in priority order (user_data first, then builtin_data).
    Only returns paths that exist.
    """
    paths = []
    user_path = USER_DATA_DIR / subdir
    builtin_path = BUILTIN_DATA_DIR / subdir
    
    if user_path.exists():
        paths.append(user_path)
    if builtin_path.exists():
        paths.append(builtin_path)
    
    return paths


def get_all_data_paths(subdir: str) -> list[Path]:
    """Get both user_data and builtin_data paths, creating them if needed."""
    user_path = USER_DATA_DIR / subdir
    builtin_path = BUILTIN_DATA_DIR / subdir
    return [user_path, builtin_path]


def find_file(subdir: str, filename: str) -> Path | None:
    """Find a file in user_data or builtin_data (user_data takes priority).
    
    Args:
        subdir: Subdirectory name (e.g., "prompts", "models")
        filename: Name of the file to find
        
    Returns:
        Path to the file if found, None otherwise
    """
    # Check user_data first
    user_file = USER_DATA_DIR / subdir / filename
    if user_file.exists():
        return user_file
    
    # Fall back to builtin_data
    builtin_file = BUILTIN_DATA_DIR / subdir / filename
    if builtin_file.exists():
        return builtin_file
    
    return None


def iter_files(subdir: str, pattern: str = "*") -> Iterator[Path]:
    """Iterate over files in both user_data and builtin_data.
    
    Files from user_data take priority - if the same filename exists in both,
    only the user_data version is yielded.
    
    Args:
        subdir: Subdirectory name
        pattern: Glob pattern for files (default: "*")
        
    Yields:
        Path objects for matching files
    """
    seen_names: set[str] = set()
    
    # User data first (higher priority)
    user_path = USER_DATA_DIR / subdir
    if user_path.exists():
        for file_path in user_path.glob(pattern):
            if file_path.is_file():
                seen_names.add(file_path.name)
                yield file_path
    
    # Builtin data (skip if already seen in user_data)
    builtin_path = BUILTIN_DATA_DIR / subdir
    if builtin_path.exists():
        for file_path in builtin_path.glob(pattern):
            if file_path.is_file() and file_path.name not in seen_names:
                yield file_path


def iter_directories(subdir: str) -> Iterator[Path]:
    """Iterate over subdirectories in both user_data and builtin_data.
    
    Directories from user_data take priority.
    """
    seen_names: set[str] = set()
    
    user_path = USER_DATA_DIR / subdir
    if user_path.exists():
        for dir_path in user_path.iterdir():
            if dir_path.is_dir():
                seen_names.add(dir_path.name)
                yield dir_path
    
    builtin_path = BUILTIN_DATA_DIR / subdir
    if builtin_path.exists():
        for dir_path in builtin_path.iterdir():
            if dir_path.is_dir() and dir_path.name not in seen_names:
                yield dir_path


def load_prompt(name: str) -> str:
    """Load a prompt file from prompts directory.
    
    Args:
        name: Prompt filename (with or without .txt extension)
        
    Returns:
        Content of the prompt file
        
    Raises:
        FileNotFoundError: If prompt file is not found
    """
    if not name.endswith(".txt"):
        name = f"{name}.txt"
    
    path = find_file(PROMPTS_DIR, name)
    if path is None:
        raise FileNotFoundError(f"Prompt file not found: {name}")
    
    return path.read_text(encoding="utf-8")


def get_user_icons_dir() -> Path:
    """Get the user icons directory, creating it if needed."""
    icons_dir = USER_DATA_DIR / ICONS_DIR
    icons_dir.mkdir(parents=True, exist_ok=True)
    return icons_dir


def get_user_database_dir() -> Path:
    """Get the user database directory, creating it if needed."""
    db_dir = USER_DATA_DIR / DATABASE_DIR
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir


def ensure_user_data_dirs() -> None:
    """Ensure all user_data subdirectories exist."""
    for subdir in [TOOLS_DIR, PHENOMENA_DIR, PLAYBOOKS_DIR, MODELS_DIR, DATABASE_DIR, PROMPTS_DIR, ICONS_DIR]:
        (USER_DATA_DIR / subdir).mkdir(parents=True, exist_ok=True)


__all__ = [
    "USER_DATA_DIR",
    "BUILTIN_DATA_DIR",
    "TOOLS_DIR",
    "PHENOMENA_DIR",
    "PLAYBOOKS_DIR",
    "MODELS_DIR",
    "DATABASE_DIR",
    "PROMPTS_DIR",
    "ICONS_DIR",
    "get_data_paths",
    "get_all_data_paths",
    "find_file",
    "iter_files",
    "iter_directories",
    "load_prompt",
    "get_user_icons_dir",
    "get_user_database_dir",
    "ensure_user_data_dirs",
]
