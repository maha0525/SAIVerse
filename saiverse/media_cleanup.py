"""Ownership of unpublished files created for one item registration.

Only exclusive creation grants ownership. A caller must preserve files before
attempting a commit, since an exception from commit does not prove rollback.
"""
from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

LOGGER = logging.getLogger(__name__)


class NewMediaFiles:
    """Remove this operation's unpublished new files until commit is attempted.

    Callers must not share paths with concurrent writers while ownership is held.
    The identity/link checks detect prior changes; they are not an atomic unlink
    guard against an external process replacing a path during cleanup.
    """

    def __init__(self) -> None:
        self._created: dict[Path, tuple[int, int]] = {}
        self._preserve = False

    def __enter__(self) -> NewMediaFiles:
        return self

    def write_bytes(self, path: Path, data: bytes) -> None:
        # 'xb' both proves ownership and prevents clobbering a collision/symlink.
        with path.open("xb") as stream:
            info = os.fstat(stream.fileno())
            self._created[path] = (info.st_dev, info.st_ino)
            stream.write(data)

    def preserve(self) -> None:
        """Relinquish cleanup before commit or publication to another consumer."""
        self._preserve = True

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if exc_type is not None and not self._preserve:
            for path, identity in reversed(list(self._created.items())):
                try:
                    info = path.lstat()
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or (info.st_dev, info.st_ino) != identity
                        or info.st_nlink != 1
                    ):
                        LOGGER.warning("Preserving changed or shared new media file: %s", path)
                        continue
                    path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    LOGGER.warning("Failed to remove unregistered media file: %s", path, exc_info=True)
        self._created.clear()
