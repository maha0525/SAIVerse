"""requirements.txt (意図) と requirements.lock (検証済みの固定) の契約。

docs/intent/dependency_management.md §2-4 の不変条件のうち、機械で検査できる
三つをここで固定する:

(a) requirements.txt の直接依存は、どれも lock の中に「その範囲を満たす版」で
    固定されている (lock が意図の外に出ていない)。
(b) lock は ASCII のみで、コメント以外の全行が ``name==version`` (任意で
    ``; marker``) である (固定されていない行が紛れ込んでいない)。
(c) 部品を入れる経路は requirements.txt ではなく lock を読む (一箇所でも
    requirements.txt を直接読む経路が残ると、その経路だけ違う組み合わせが入る)。
"""
from __future__ import annotations

import re
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = REPO_ROOT / "requirements.txt"
LOCK = REPO_ROOT / "requirements.lock"

# ``name==version`` followed optionally by `` ; marker``. The marker itself is
# validated by packaging below; this regex only rejects anything that is not an
# exact pin (``>=``, URLs, ``-e``, bare names...).
_PINNED_LINE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)(?:\s*;\s*(.+))?$")


def _requirement_lines(path: Path) -> list[str]:
    """Non-empty, non-comment lines, with trailing comments stripped."""
    lines: list[str] = []
    for raw in path.read_text(encoding="ascii").splitlines():
        line = re.split(r"(?:^|\s)#", raw, maxsplit=1)[0].strip()
        if line:
            lines.append(line)
    return lines


def _lock_pins() -> dict[str, list[Version]]:
    pins: dict[str, list[Version]] = {}
    for line in _requirement_lines(LOCK):
        match = _PINNED_LINE.match(line)
        assert match, f"requirements.lock has a line that is not an exact pin: {line!r}"
        pins.setdefault(canonicalize_name(match.group(1)), []).append(Version(match.group(2)))
    return pins


# --- (a) every direct requirement is pinned inside its own range -------------


def test_every_direct_requirement_is_pinned_within_its_range() -> None:
    pins = _lock_pins()
    problems: list[str] = []
    for line in _requirement_lines(REQUIREMENTS):
        requirement = Requirement(line)
        name = canonicalize_name(requirement.name)
        versions = pins.get(name)
        if not versions:
            problems.append(f"{requirement.name}: not in requirements.lock at all")
            continue
        if not any(requirement.specifier.contains(v, prereleases=True) for v in versions):
            problems.append(
                f"{requirement.name}: lock pins {[str(v) for v in versions]} "
                f"but requirements.txt asks for {requirement.specifier or 'any'}"
            )
    assert not problems, (
        "requirements.lock is out of step with requirements.txt -- regenerate it "
        "(commands in the lock header):\n" + "\n".join(problems)
    )


def test_intent_file_has_no_exact_pins() -> None:
    """requirements.txt is the intent (lower bounds, reasoned upper bounds);
    an ``==`` there would silently move the pinning job out of the lock."""
    offenders = [line for line in _requirement_lines(REQUIREMENTS) if "==" in line]
    assert not offenders, (
        "requirements.txt must not pin with ==; the exact version belongs in "
        "requirements.lock:\n" + "\n".join(offenders)
    )


# --- (b) the lock is pip-readable everywhere and contains only exact pins ----


def test_lock_is_ascii_only() -> None:
    """pip decodes the file with the OS locale (cp932 on Japanese Windows)."""
    data = LOCK.read_bytes()
    offenders = [
        f"{lineno}: {line.decode('utf-8', 'replace')}"
        for lineno, line in enumerate(data.splitlines(), start=1)
        if any(byte > 0x7F for byte in line)
    ]
    assert not offenders, "Non-ASCII bytes in requirements.lock:\n" + "\n".join(offenders)


def test_every_lock_line_is_an_exact_pin_with_a_valid_marker() -> None:
    lines = _requirement_lines(LOCK)
    assert lines, "requirements.lock has no requirement lines"
    for line in lines:
        match = _PINNED_LINE.match(line)
        assert match, f"not an exact pin: {line!r}"
        # packaging must accept the whole line, marker included, because
        # update_engine.scan_requirements evaluates it with the same parser.
        requirement = Requirement(line)
        assert str(requirement.specifier).startswith("=="), line
        if match.group(3):
            assert requirement.marker is not None, line
            requirement.marker.evaluate()  # raises on an invalid marker


# --- (c) every install path reads the lock -----------------------------------


# The seven paths of docs/intent/dependency_management.md §2-2 that pip reads
# from. Each is checked for the install-target spelling that would bypass the
# lock; prose mentions of "requirements.txt" (as a file name in a comment) are
# allowed, an ``-r requirements.txt`` is not.
_INSTALL_PATHS = [
    "setup.bat",
    "setup.sh",
    "scripts/update_engine.py",
    "saiverse/addon_installer.py",
    "discord_gateway/requirements-dev.txt",
    "docs/getting-started/installation.md",
]
_BYPASS = re.compile(r"(?:-r|--requirement)[\s=]+(?:\.\./)?requirements\.txt\b")


def test_install_paths_read_the_lock_not_the_intent_file() -> None:
    offenders: list[str] = []
    for relative in _INSTALL_PATHS:
        text = (REPO_ROOT / relative).read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _BYPASS.search(line):
                offenders.append(f"{relative}:{lineno}: {line.strip()}")
    assert not offenders, (
        "An install path still reads requirements.txt directly; every path must "
        "install from requirements.lock:\n" + "\n".join(offenders)
    )


def test_install_paths_mention_the_lock() -> None:
    """The positive half of (c): each path actually names the lock."""
    for relative in _INSTALL_PATHS:
        text = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "requirements.lock" in text, f"{relative} does not reference requirements.lock"
