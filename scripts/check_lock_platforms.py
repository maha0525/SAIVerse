"""requirements.lock の各 pin が、配布対象の全プラットフォームで実際に入るかを PyPI に問う。

`uv pip compile --universal` は依存関係のメタデータだけで解決し、**その版の wheel が
各プラットフォーム向けに公開されているか**は見ない。だから lock 上は整合していても、
「macOS x86_64 向けの wheel も sdist も無い版」を掴んでいると、その環境では
`pip install -r requirements.lock` 自体が失敗する (2026-09-02 に onnxruntime 1.24.1 で
実際に起きた。Intel Mac では入らない lock を Windows で検証して通していた)。

対象: Windows x86_64 / Linux x86_64 / macOS arm64 / macOS x86_64 x CPython 3.11〜3.13。
環境マーカーでそのプラットフォームから外れている行は対象外。

判定は pip と同じ `packaging.tags` で行う (文字列の部分一致ではない):
- 対象ごとに互換タグの集合を作り、wheel のタグ集合と交わるかを見る
- macOS の下限は x86_64 が 13.0 (対応する Intel Mac の床)、arm64 が 14.0
- Linux は glibc 系ディストリビューションだけを対象にする (manylinux のみ、musllinux は見ない)
- pin の Requires-Python が対象の Python を除外していたら、sdist があっても入らないので失敗

結果の格:
- FAIL (exit 1): wheel も sdist も無い / Requires-Python が除外 / PyPI の照会に失敗
  (版が存在しないかネットワークが落ちている。判定できなかった pin を「入る」とは言わない)
- WARN (exit 0 のまま): wheel は無いが sdist はある (ビルドすれば入る)
exit 0 は「全 pin を取得して判定し、FAIL が無かった」ときだけ。

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
from typing import NamedTuple

from packaging import tags as packaging_tags
from packaging.markers import Marker
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import InvalidWheelFilename, parse_wheel_filename

PYTHONS = ["3.11", "3.12", "3.13"]
PLATFORMS = {
    "win_amd64": dict(sys_platform="win32", platform_machine="AMD64", platform_system="Windows", os_name="nt"),
    "linux_x86_64": dict(sys_platform="linux", platform_machine="x86_64", platform_system="Linux", os_name="posix"),
    "macos_arm64": dict(sys_platform="darwin", platform_machine="arm64", platform_system="Darwin", os_name="posix"),
    "macos_x86_64": dict(sys_platform="darwin", platform_machine="x86_64", platform_system="Darwin", os_name="posix"),
}
# macOS の床。x86_64 は 13.0 = 対応する Intel Mac の最低 OS 版 (intent §3-2 が引用する値)。
# これより新しい OS しか受け付けない wheel (macosx_14_0_x86_64 など) は Intel Mac 向けには
# 「無い」と数える。arm64 は 14.0。
MACOS_FLOOR = {"macos_x86_64": (13, 0), "macos_arm64": (14, 0)}
_PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;]+)(?:\s*;\s*(.+))?$")


def _platform_tags(platform: str) -> list[str]:
    if platform == "win_amd64":
        return ["win_amd64"]
    if platform == "linux_x86_64":
        # 現代の pip が glibc 系で受け付ける manylinux タグ。対象は glibc の
        # ディストリビューションだけなので musllinux (Alpine 系) は含めない。
        modern = [f"manylinux_2_{minor}_x86_64" for minor in range(40, 4, -1)]
        legacy = ["manylinux2014_x86_64", "manylinux2010_x86_64", "manylinux1_x86_64"]
        return modern + legacy + ["linux_x86_64"]
    arch = PLATFORMS[platform]["platform_machine"]
    return list(packaging_tags.mac_platforms(version=MACOS_FLOOR[platform], arch=arch))


_TAG_CACHE: dict[tuple[str, str], frozenset[packaging_tags.Tag]] = {}


def _target_tags(platform: str, py: str) -> frozenset[packaging_tags.Tag]:
    """対象 (platform, python) が受け付ける wheel タグの集合。pip と同じ生成規則。"""
    key = (platform, py)
    if key not in _TAG_CACHE:
        python_version = (3, int(py.split(".")[1]))
        platforms = _platform_tags(platform)
        tags = set(packaging_tags.cpython_tags(python_version=python_version, platforms=platforms))
        tags.update(packaging_tags.compatible_tags(python_version=python_version, platforms=platforms))
        _TAG_CACHE[key] = frozenset(tags)
    return _TAG_CACHE[key]


def _wheel_fits(filename: str, platform: str, py: str) -> bool:
    if not filename.endswith(".whl"):
        return False
    try:
        wheel_tags = parse_wheel_filename(filename)[3]
    except InvalidWheelFilename:
        return False
    return not wheel_tags.isdisjoint(_target_tags(platform, py))


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


class PyPIRelease(NamedTuple):
    files: list[str]
    requires_python: str | None


def _release_on_pypi(name: str, version: str) -> PyPIRelease | None:
    """その版の配布ファイル名と Requires-Python。取れなければ None (呼び手が FAIL にする)。"""
    try:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=30) as resp:
            payload = json.load(resp)
        return PyPIRelease(
            [entry["filename"] for entry in payload["urls"]],
            payload.get("info", {}).get("requires_python") or None,
        )
    except Exception:
        return None


def _python_excluded(requires_python: str | None, py: str) -> bool:
    """Requires-Python が対象の Python を除外しているか。読めない指定は除外扱いにしない。"""
    if not requires_python:
        return False
    try:
        return not SpecifierSet(requires_python).contains(py, prereleases=True)
    except InvalidSpecifier:
        return False


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
        release = _release_on_pypi(name, version)
        if release is None:
            failures.append(
                f"{name}=={version}: PyPI lookup failed (the version may not exist, or the network is down)"
            )
            continue
        has_sdist = any(f.endswith((".tar.gz", ".zip")) for f in release.files)
        for platform in PLATFORMS:
            for py in PYTHONS:
                if marker and not Marker(marker).evaluate(_marker_env(platform, py)):
                    continue
                if _python_excluded(release.requires_python, py):
                    failures.append(
                        f"{name}=={version}: Requires-Python {release.requires_python} excludes Python {py}"
                        f" ({platform}; an sdist cannot rescue that)"
                    )
                    continue
                if any(_wheel_fits(f, platform, py) for f in release.files):
                    continue
                msg = f"{name}=={version}: no wheel for {platform} / Python {py}"
                if has_sdist:
                    warnings.append(msg + " (sdist only: needs a build)")
                else:
                    failures.append(msg + " (no sdist either: install fails)")
    print(f"checked {len(lines)} pins in {lock}")
    for w in warnings:
        print("WARN", w)
    for f in failures:
        print("FAIL", f)
    if failures:
        print(f"{len(failures)} pin/platform combination(s) cannot be installed or could not be judged")
        return 1
    print("every pin was fetched and is installable on every target platform")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
