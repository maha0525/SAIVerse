"""Unified data path management for SAIVerse.

Provides centralized access to user_data and builtin_data directories.
Files in user_data take priority over builtin_data.

Environment variables:
    SAIVERSE_USER_DATA_DIR: Override user_data directory (for testing)
    SAIVERSE_HOME: Override ~/.saiverse directory (for testing)
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterator

LOGGER = logging.getLogger(__name__)

# Root directories (parent.parent because this file is now in saiverse/)
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def get_saiverse_home() -> Path:
    """Get the SAIVerse home directory (~/.saiverse or SAIVERSE_HOME env var).

    This directory contains:
        - personas/<id>/: SAIMemory databases and logs
        - cities/<city>/buildings/<building>/: Building logs
        - image/: Uploaded images
        - backups/: Backup files
        - user_data/: User customization data (tools, playbooks, database, etc.)
    """
    env_home = os.getenv("SAIVERSE_HOME")
    if env_home:
        return Path(env_home)
    return Path.home() / ".saiverse"


# USER_DATA_DIR can be overridden via environment variable (for testing)
_user_data_env = os.getenv("SAIVERSE_USER_DATA_DIR")
USER_DATA_DIR = Path(_user_data_env) if _user_data_env else get_saiverse_home() / "user_data"

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


def iter_project_files(subdir: str, pattern: str = "*") -> Iterator[Path]:
    """Iterate over files across all projects in user_data.

    This supports the project-based directory structure:
        user_data/<project>/<subdir>/

    Files from earlier projects take priority - if the same filename exists
    in multiple projects, only the first one is yielded.

    Args:
        subdir: Subdirectory name (e.g., "models", "playbooks")
        pattern: Glob pattern for files (default: "*")

    Yields:
        Path objects for matching files
    """
    seen_names: set[str] = set()

    # Iterate over all project subdirectories
    for subdir_path in iter_project_subdirs(subdir):
        for file_path in subdir_path.glob(pattern):
            if file_path.is_file() and file_path.name not in seen_names:
                seen_names.add(file_path.name)
                yield file_path


def iter_project_subdirs(subdir: str) -> Iterator[Path]:
    """Iterate over subdirectories across all projects in user_data.

    This supports the project-based directory structure:
        user_data/<project>/<subdir>/

    For example, iter_project_subdirs("tools") yields:
        - user_data/discord/tools/
        - user_data/another_project/tools/
        - builtin_data/tools/  (legacy/builtin compatibility)

    Args:
        subdir: Subdirectory name (e.g., "tools", "phenomena", "playbooks")

    Yields:
        Path objects for each project's subdirectory
    """
    seen_project_names: set[str] = set()

    # Scan all projects in user_data
    if USER_DATA_DIR.exists():
        for project_dir in sorted(USER_DATA_DIR.iterdir()):
            # Skip hidden directories and non-directories
            if not project_dir.is_dir() or project_dir.name.startswith(("_", ".")):
                continue

            subdir_path = project_dir / subdir
            if subdir_path.exists() and subdir_path.is_dir():
                seen_project_names.add(project_dir.name)
                yield subdir_path

    # Also yield builtin_data for backwards compatibility
    builtin_path = BUILTIN_DATA_DIR / subdir
    if builtin_path.exists() and builtin_path.is_dir():
        yield builtin_path


def get_project_data_paths(subdir: str) -> list[Path]:
    """Get all project subdirectory paths for a given subdirectory type.

    Similar to iter_project_subdirs but returns a list.

    Args:
        subdir: Subdirectory name (e.g., "tools", "phenomena")

    Returns:
        List of paths in discovery order
    """
    return list(iter_project_subdirs(subdir))


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


def migrate_legacy_user_data() -> bool:
    """Migrate legacy user_data/ (inside repository) to ~/.saiverse/user_data/.

    Called at startup to transparently move user data to the new location.
    If the new location already has some content (e.g. from a previous partial
    run), items are merged: legacy items are moved unless they already exist
    at the destination.

    Returns:
        True if migration was performed, False otherwise.
    """
    legacy_dir = PROJECT_ROOT / "user_data"

    # Skip if legacy dir doesn't exist or is already the target
    if not legacy_dir.exists() or legacy_dir.resolve() == USER_DATA_DIR.resolve():
        return False

    # Skip if legacy dir is empty
    if not any(legacy_dir.iterdir()):
        return False

    import shutil

    LOGGER.info("Migrating user_data from %s to %s ...", legacy_dir, USER_DATA_DIR)
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

    moved_count = 0

    def _move_item(src: Path, dst: Path) -> bool:
        """Move a file or directory, falling back to copy if move fails."""
        try:
            shutil.move(str(src), str(dst))
            return True
        except (PermissionError, OSError) as exc:
            # On Windows, files locked by other processes can't be moved.
            # Fall back to copy and leave the source in place.
            LOGGER.debug("move failed (%s), trying copy: %s", exc, src.name)
            try:
                if src.is_dir():
                    shutil.copytree(str(src), str(dst))
                else:
                    shutil.copy2(str(src), str(dst))
                return True
            except (PermissionError, OSError) as copy_exc:
                LOGGER.warning("Could not copy %s: %s", src, copy_exc)
                return False

    def _merge_dir(src_dir: Path, dst_dir: Path, depth: int = 0) -> int:
        """Recursively merge src_dir into dst_dir.

        Legacy (source) files always win — destination files that were
        auto-created by startup code are overwritten with the real data.
        """
        count = 0
        prefix = "  " * (depth + 1)
        for item in list(src_dir.iterdir()):
            dest = dst_dir / item.name
            if item.is_dir() and dest.is_dir():
                # Both are directories — recurse into them
                LOGGER.debug("%sMerging directory %s/", prefix, item.name)
                count += _merge_dir(item, dest, depth + 1)
            elif item.is_file() and dest.is_file():
                # Both are files — legacy wins (overwrite auto-created stubs)
                src_size = item.stat().st_size
                dst_size = dest.stat().st_size
                if src_size > dst_size:
                    LOGGER.info(
                        "%sOverwriting %s (legacy %d bytes > dest %d bytes)",
                        prefix, item.name, src_size, dst_size,
                    )
                    dest.unlink()
                    if _move_item(item, dest):
                        count += 1
                else:
                    LOGGER.debug("%sSkipping %s (dest already has data)", prefix, item.name)
            elif dest.exists():
                LOGGER.debug("%sSkipping %s (already exists)", prefix, item.name)
            else:
                LOGGER.info("%sMoving %s -> %s", prefix, item.name, dest)
                dest.parent.mkdir(parents=True, exist_ok=True)
                if _move_item(item, dest):
                    count += 1
        return count

    moved_count = _merge_dir(legacy_dir, USER_DATA_DIR)

    # If legacy dir is now empty (all items moved), rename it
    remaining = list(legacy_dir.iterdir())
    if not remaining:
        migrated_marker = legacy_dir.parent / "user_data.migrated"
        try:
            legacy_dir.rename(migrated_marker)
            LOGGER.info("Legacy user_data renamed to %s", migrated_marker)
        except OSError:
            LOGGER.warning("Could not rename legacy user_data directory. Please remove it manually.")
    else:
        LOGGER.warning(
            "Legacy user_data still contains %d items that could not be moved: %s",
            len(remaining), ", ".join(r.name for r in remaining),
        )

    LOGGER.info("user_data migration complete. Moved %d items.", moved_count)
    return moved_count > 0


__all__ = [
    "PROJECT_ROOT",
    "USER_DATA_DIR",
    "BUILTIN_DATA_DIR",
    "TOOLS_DIR",
    "PHENOMENA_DIR",
    "PLAYBOOKS_DIR",
    "MODELS_DIR",
    "DATABASE_DIR",
    "PROMPTS_DIR",
    "ICONS_DIR",
    "get_saiverse_home",
    "get_data_paths",
    "get_all_data_paths",
    "find_file",
    "iter_files",
    "iter_directories",
    "iter_project_files",
    "iter_project_subdirs",
    "get_project_data_paths",
    "load_prompt",
    "get_user_icons_dir",
    "get_user_database_dir",
    "ensure_user_data_dirs",
    "migrate_legacy_user_data",
]
