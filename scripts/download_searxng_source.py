#!/usr/bin/env python3
"""Download SearXNG source from GitHub archive when git is not available.

This is a fallback for scripts/setup_searxng.{ps1,sh} when git is not
installed.  It downloads the ZIP archive from GitHub and extracts the
necessary files into the destination directory.

Usage:
    python scripts/download_searxng_source.py <dest_dir> [--ref master] [--selective]

Options:
    dest_dir      Target directory for extracted source
    --ref         Branch or tag name (default: master)
    --selective   Only extract searx/, setup.py, setup.cfg, requirements.txt
                  and skip files with Windows-invalid characters (for NTFS)
"""

import argparse
import os
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlretrieve

# Characters that are invalid in Windows (NTFS) filenames
_WIN_INVALID_CHARS = frozenset(':<>"|?*')

# Files/directories to keep in selective mode (relative to ZIP root)
_SELECTIVE_ROOTS = ("searx/", "setup.py", "setup.cfg", "requirements.txt")


def _has_invalid_chars(name: str) -> bool:
    """Check if any path component contains Windows-invalid characters."""
    for part in Path(name).parts:
        if any(c in _WIN_INVALID_CHARS for c in part):
            return True
    return False


def _build_zip_url(ref: str, kind: str = "heads") -> str:
    """Build the GitHub archive URL for a branch or tag."""
    return f"https://github.com/searxng/searxng/archive/refs/{kind}/{ref}.zip"


def _find_prefix(namelist: list) -> str:
    """Discover the single root directory in the ZIP archive.

    GitHub ZIPs always contain a single root dir like ``searxng-master/``.
    We detect it dynamically instead of computing it from the ref name
    because GitHub normalises certain characters (e.g. ``/`` → ``-``).
    """
    prefixes = {n.split("/")[0] for n in namelist if "/" in n}
    if len(prefixes) != 1:
        raise RuntimeError(
            f"Expected exactly 1 root directory in ZIP, found: {prefixes}"
        )
    return prefixes.pop() + "/"


def _should_extract(member: str, prefix: str, selective: bool) -> bool:
    """Decide whether a ZIP member should be extracted."""
    if not member.startswith(prefix):
        return False
    relative = member[len(prefix):]
    if not relative:
        return False  # skip the root directory entry itself

    if selective:
        # Skip files with characters invalid on NTFS
        if _has_invalid_chars(relative):
            return False
        return any(
            relative == root or relative.startswith(root)
            for root in _SELECTIVE_ROOTS
        )
    return True


def _download_zip(ref: str) -> str:
    """Download the ZIP archive to a temporary file.

    Tries ``refs/heads/{ref}`` first, then ``refs/tags/{ref}`` on 404.
    Returns the path to the downloaded temporary file.
    """
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".zip")
    os.close(tmp_fd)

    for kind in ("heads", "tags"):
        url = _build_zip_url(ref, kind)
        try:
            print(f"[INFO] Downloading {url}", file=sys.stderr)
            urlretrieve(url, tmp_path)
            return tmp_path
        except HTTPError as exc:
            if exc.code == 404 and kind == "heads":
                print(
                    f"[INFO] Branch '{ref}' not found, trying as tag...",
                    file=sys.stderr,
                )
                continue
            # Clean up on non-404 errors
            os.unlink(tmp_path)
            raise
        except (URLError, OSError):
            os.unlink(tmp_path)
            raise

    # Both attempts returned 404
    os.unlink(tmp_path)
    raise RuntimeError(
        f"Could not find '{ref}' as either a branch or tag on GitHub"
    )


def download_and_extract(dest: Path, ref: str, selective: bool) -> None:
    """Download and extract SearXNG source into *dest*."""
    tmp_path = _download_zip(ref)

    try:
        with zipfile.ZipFile(tmp_path) as zf:
            prefix = _find_prefix(zf.namelist())
            dest.mkdir(parents=True, exist_ok=True)

            extracted = 0
            for info in zf.infolist():
                if not _should_extract(info.filename, prefix, selective):
                    continue

                relative = info.filename[len(prefix):]
                target = dest / relative

                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as src, open(target, "wb") as dst:
                        dst.write(src.read())
                    extracted += 1

        print(
            f"[INFO] Extracted {extracted} files to {dest}",
            file=sys.stderr,
        )
    finally:
        os.unlink(tmp_path)

    # Sanity check
    if not (dest / "requirements.txt").is_file():
        raise RuntimeError(
            f"Extraction verification failed: {dest / 'requirements.txt'} "
            "not found"
        )

    print(f"[OK] SearXNG source ready at {dest}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download SearXNG source from GitHub archive"
    )
    parser.add_argument("dest", help="Destination directory for source files")
    parser.add_argument(
        "--ref",
        default="master",
        help="Branch or tag to download (default: master)",
    )
    parser.add_argument(
        "--selective",
        action="store_true",
        help=(
            "Only extract searx/, setup.py, setup.cfg, requirements.txt "
            "and skip files with Windows-invalid characters"
        ),
    )
    args = parser.parse_args()

    try:
        download_and_extract(Path(args.dest), args.ref, args.selective)
    except KeyboardInterrupt:
        print("\n[ABORT] Cancelled by user", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
