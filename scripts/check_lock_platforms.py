"""requirements.lock の各 pin が、配布対象の全プラットフォームで実際に入るかを PyPI に問う。

`uv pip compile --universal` は依存関係のメタデータだけで解決し、**その版の wheel が
各プラットフォーム向けに公開されているか**は見ない。だから lock 上は整合していても、
「macOS x86_64 向けの wheel も sdist も無い版」を掴んでいると、その環境では
`pip install -r requirements.lock` 自体が失敗する (2026-09-02 に onnxruntime 1.24.1 で
実際に起きた。Intel Mac では入らない lock を Windows で検証して通していた)。

対象: Windows x86_64 / Linux x86_64 / macOS arm64 / macOS x86_64 x CPython 3.11〜3.13。
環境マーカーでそのプラットフォームから外れている行は対象外。wheel が無くても sdist が
あれば「ビルドすれば入る」ので警告止まり、sdist も無ければ失敗として exit 1。

使い方 (lock を作り直したら一度回す。ネットワーク必須、1〜2 分):
    python scripts/check_lock_platforms.py            # requirements.lock
    python scripts/check_lock_platforms.py path/to/lock
"""
from __future__ import annotations

import io
import json
import re
import sys
import urllib.request
from pathlib import Path

from packaging.markers import Marker

PYTHONS = ["3.11", "3.12", "3.13"]
PLATFORMS = {
    "win_amd64": dict(sys_platform="win32", platform_machine="AMD64", platform_system="Windows", os_name="nt"),
    "linux_x86_64": dict(sys_platform="linux", platform_machine="x86_64", platform_system="Linux", os_name="posix"),
    "macos_arm64": dict(sys_platform="darwin", platform_machine="arm64", platform_system="Darwin", os_name="posix"),
    "macos_x86_64": dict(sys_platform="darwin", platform_machine="x86_64", platform_system="Darwin", os_name="posix"),
}
_PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;]+)(?:\s*;\s*(.+))?$")


def _marker_env(platform: str, py: str) -> dict[str, str]:
    env = dict(PLATFORMS[platform])
    env.update(
        python_version=py,
        python_full_version=py + ".0",
        implementation_name="cpython",
        implementation_version=py + ".0",
        platform_python_implementation="CPython",
        platform_release="",
        platform_version="",
    )
    return env


def _wheel_fits(filename: str, platform: str, py: str) -> bool:
    if not filename.endswith(".whl"):
        return False
    pytag, abitag, plattag = filename[:-4].split("-")[-3:]
    cp = "cp" + py.replace(".", "")
    target = int(py.replace(".", ""))
    tags = pytag.split(".")
    if abitag == "abi3":
        # cpXY-abi3 means "CPython 3.Y or newer"
        py_ok = any(t.startswith("cp3") and t[2:].isdigit() and int(t[2:]) <= target for t in tags)
    else:
        py_ok = any(t in ("py3", "py2.py3", cp) or t.startswith("py3") for t in tags)
    if not py_ok or abitag not in ("none", cp, "abi3"):
        return False
    if plattag == "any":
        return True
    if platform == "win_amd64":
        return "win_amd64" in plattag
    if platform == "linux_x86_64":
        return ("manylinux" in plattag or "musllinux" in plattag) and "x86_64" in plattag
    arch = PLATFORMS[platform]["platform_machine"]
    return "macosx" in plattag and (arch in plattag or "universal2" in plattag)


def _files_on_pypi(name: str, version: str) -> list[str] | None:
    try:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=30) as resp:
            return [entry["filename"] for entry in json.load(resp)["urls"]]
    except Exception:
        return None


def main(argv: list[str]) -> int:
    lock = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent / "requirements.lock"
    lines = [ln.strip() for ln in io.open(lock, encoding="utf-8") if re.match(r"^[A-Za-z0-9]", ln)]
    failures: list[str] = []
    warnings: list[str] = []
    for line in lines:
        match = _PIN.match(line)
        if match is None:
            failures.append(f"{line}: not an exact pin")
            continue
        name, version, marker = match.groups()
        files = _files_on_pypi(name, version)
        if files is None:
            warnings.append(f"{name}=={version}: PyPI lookup failed")
            continue
        has_sdist = any(f.endswith((".tar.gz", ".zip")) for f in files)
        for platform in PLATFORMS:
            for py in PYTHONS:
                if marker and not Marker(marker).evaluate(_marker_env(platform, py)):
                    continue
                if any(_wheel_fits(f, platform, py) for f in files):
                    continue
                msg = f"{name}=={version}: no wheel for {platform} / Python {py}"
                (warnings if has_sdist else failures).append(msg + (" (sdist only: needs a build)" if has_sdist else " (no sdist either: install fails)"))
    print(f"checked {len(lines)} pins in {lock}")
    for w in warnings:
        print("WARN", w)
    for f in failures:
        print("FAIL", f)
    if failures:
        print(f"{len(failures)} pin/platform combination(s) cannot be installed")
        return 1
    print("every pin is installable on every target platform")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
