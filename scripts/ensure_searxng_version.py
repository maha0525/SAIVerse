"""Let SearXNG start on a machine where it cannot run ``git``.

Called by run_searxng_server.ps1 / .sh right before SearXNG starts, with the
SearXNG source directory as its argument.

SearXNG works out its own version at import time by running ``git``
(searx/version.py). A failing git command is tolerated there, but a missing
``git`` executable is not: the import dies with FileNotFoundError and the
server never starts. That is the situation of a Windows install where setup
put Git inside the SAIVerse folder (``.git-portable``): start.bat does not put
that folder on PATH (docs/issues/archive/searxng_needs_git_on_path.md).

SearXNG skips git entirely when ``searx/version_frozen.py`` exists. So when git
cannot be found, this script writes that file with the same fallback values
SearXNG itself uses when git fails. When git can be found, nothing is written
and SearXNG keeps reporting its real version.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

# The fallback values in searx/version.py.
FROZEN_SOURCE = '''\
# Written by SAIVerse (scripts/ensure_searxng_version.py) because git was not
# available when SearXNG was started. SearXNG reads these instead of asking git.
VERSION_STRING = "1.0.0"
VERSION_TAG = "1.0.0"
DOCKER_TAG = "1.0.0"
GIT_URL = "unknown"
GIT_BRANCH = "unknown"
'''


def ensure_version_frozen(src_dir: Path) -> bool:
    """Write ``searx/version_frozen.py`` when it is needed. Returns whether it was written."""
    package_dir = src_dir / "searx"
    target = package_dir / "version_frozen.py"
    if target.exists() or not package_dir.is_dir():
        return False
    if shutil.which("git"):
        return False
    target.write_text(FROZEN_SOURCE, encoding="utf-8")
    return True


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: ensure_searxng_version.py <searxng source dir>")
        return 2
    try:
        if ensure_version_frozen(Path(argv[1])):
            print("[INFO] git is not available; SearXNG will start without asking git for its version.")
    except OSError as exc:
        # Not fatal here: SearXNG's own start-up reports the problem if it remains.
        print(f"[WARN] Could not prepare SearXNG's version file: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
