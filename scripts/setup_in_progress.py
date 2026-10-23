"""Records of replacements that have started and not finished yet.

Some environments can only be replaced by first removing or renaming the one
that is there: SAIVerse's ``.venv`` (``scripts/setup_venv.py``), an addon's
own Python environment (``saiverse/addon_installer.py``) and the SearXNG venv
(``scripts/searxng_install.py``). Before such an operation removes or renames
anything, it writes an entry here, and it removes the entry only after the
whole replacement has finished. An entry that is still there means the
operation stopped part-way (the window was closed, Ctrl+C, a crash), and the
next run redoes everything the entry names, whatever the folders look like
now. Design: ``docs/intent/dependency_management.md`` section 2-7.

The entries are files in a ``.setup_in_progress`` folder, one file per entry:
SAIVerse's folder holds the entries of ``.venv`` and of the SearXNG venv, and
an addon's install folder (``addon_install/<id>/``) holds the entries of that
addon's environments. One file per entry, so processes that run at the same
time (the SAIVerse backend installing an addon, the SearXNG launcher) never
rewrite each other's entries.

Standard library only: ``scripts/setup_venv.py`` uses it before ``.venv``
exists.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DIRECTORY_NAME = ".setup_in_progress"
_SUFFIX = ".json"


def entry_path(base: Path, name: str) -> Path:
    """The file of the entry ``name`` under ``base`` (which may not exist)."""
    return base / DIRECTORY_NAME / f"{name}{_SUFFIX}"


def write_entry(base: Path, name: str, data: dict) -> None:
    """Write the entry: a file beside it first, then swapped in, so a stopped
    write never leaves half an entry. Raises OSError when it cannot be written;
    the caller must then not start the replacement."""
    target = entry_path(base, name)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, target)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def read_entry(base: Path, name: str) -> dict | None:
    """The entry, or None when there is none. An entry that exists but cannot
    be read is ``{}``: the replacement did start, only its details are lost."""
    path = entry_path(base, name)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def remove_entry(base: Path, name: str) -> None:
    """Remove the entry (nothing happens when there is none), and the folder
    when no entry is left in it. Raises OSError when the entry is there and
    cannot be removed."""
    path = entry_path(base, name)
    path.unlink(missing_ok=True)
    try:
        path.parent.rmdir()  # only when empty; another entry keeps it
    except OSError:
        pass


def entry_names(base: Path) -> list[str]:
    """The names of the entries under ``base``, sorted."""
    directory = base / DIRECTORY_NAME
    if not directory.is_dir():
        return []
    return sorted(
        path.name[: -len(_SUFFIX)]
        for path in directory.iterdir()
        if path.is_file() and path.name.endswith(_SUFFIX)
    )
