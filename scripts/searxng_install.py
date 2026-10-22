"""Install and check the selected SearXNG release without touching world data."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

from searxng_config import (
    SCRIPT_DIR,
    WORKSPACE_ROOT,
    configured_path,
    pinned_release,
    settings_path,
    source_directory,
    venv_directory,
    venv_python,
)

SOURCE_MARKER = ".saiverse-source.json"
INSTALL_MARKER = ".saiverse-searxng.json"
REQUIRED_FILES = ("requirements.txt", "requirements-dev.txt", "README.rst", "setup.py", "searx/settings.yml")

# Lower bounds written over the pinned source's requirements.txt before pip
# install. SearXNG's setup.py builds install_requires from the same file, so
# the editable install's metadata carries these bounds and `pip check` agrees.
# - certifi: commit ff8f5ddcc pins certifi==2026.7.22, which still trusts the
#   emSign root CAs removed in certifi 2026.10.19 after their key compromise
#   (GHSA-6q48-6c7w-9gw2). Unless SSL_CERT_FILE / CURL_CA_BUNDLE /
#   REQUESTS_CA_BUNDLE or a Python default cafile is set, curl_cffi verifies
#   the TLS connections to external search engines with this CA bundle.
# - anyio: versions before 4.14.2 are affected by CVE-2026-63374 (TLS
#   certificate spoofing). Commit ff8f5ddcc does not require anyio, but venvs
#   first installed from older SearXNG sources keep httpx and anyio (pip
#   install -r never removes packages), so the bound upgrades that copy.
# Remove an entry once the SearXNG commit in searxng_pin.json itself requires
# a fixed version (upstream master still pinned certifi 2026.7.22 on 2026-10-22).
REQUIREMENT_FLOORS = {"certifi": "2026.10.19", "anyio": "4.14.2"}
_REQUIREMENT_NAME = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)")

# Import the selected package and an engine that needs IANA timezone data.
# Unlike importing webapp, this does not initialize engine networking/threads.
PROBE = """
import sys
from pathlib import Path
from zoneinfo import ZoneInfo
import searx
import flask_babel
import curl_cffi
from lxml import etree
from searx.engines import load_engine
from searx.version import GIT_BRANCH
from searx.webutils import get_static_file_list
if Path(searx.__file__).resolve().parent != Path(sys.argv[1]).resolve() / 'searx':
    raise RuntimeError('wrong SearXNG source')
if GIT_BRANCH != sys.argv[2]:
    raise RuntimeError('wrong SearXNG commit')
ZoneInfo('Asia/Shanghai')
ZoneInfo('UTC')
if 'themes/simple/sxng-ltr.min.css' not in get_static_file_list():
    raise RuntimeError('static asset paths are not usable as URLs')
if load_engine({'name': 'bilibili', 'engine': 'bilibili'}) is None:
    raise RuntimeError('bilibili failed to load')
"""


def run(args: list[str | Path], *, env: dict[str, str] | None = None) -> None:
    description = "SearXNG import/timezone check" if "-c" in args else " ".join(map(str, args))
    print(f"[INFO] Running: {description}", flush=True)
    subprocess.run(list(map(str, args)), env=env, check=True)


def install_identity() -> dict[str, str]:
    # Revision 2: REQUIREMENT_FLOORS. A changed revision makes ready() reject
    # existing installs, so setup reinstalls them with the rewritten requirements.
    return {**pinned_release(), "source": str(source_directory()), "installer_revision": "2"}


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def probe_environment() -> dict[str, str]:
    return {**os.environ, "SEARXNG_SETTINGS_PATH": str(source_directory() / "searx/settings.yml")}


def ready() -> bool:
    if read_json(venv_directory() / INSTALL_MARKER) != install_identity():
        return False
    if read_json(source_directory() / SOURCE_MARKER).get("commit") != pinned_release()["commit"]:
        return False
    if not venv_python().is_file():
        return False
    result = subprocess.run(
        [str(venv_python()), "-c", PROBE, str(source_directory()), pinned_release()["commit"]],
        env=probe_environment(), capture_output=True, text=True,
    )
    if result.returncode:
        print(f"[WARN] SearXNG dependency check failed:\n{result.stderr}")
    return result.returncode == 0


def prepare_source() -> None:
    from download_searxng_source import download_and_extract

    source = source_directory()
    commit = pinned_release()["commit"]
    if source.exists():
        if read_json(source / SOURCE_MARKER).get("commit") != commit:
            raise RuntimeError(f"Unmanaged or different SearXNG source at {source}; preserving it. Choose a new SEARXNG_SRC_DIR.")
        if all((source / name).is_file() for name in REQUIRED_FILES):
            return
        raise RuntimeError(f"Incomplete SearXNG source at {source}; preserving it. Choose a new SEARXNG_SRC_DIR.")

    source.parent.mkdir(parents=True, exist_ok=True)
    # Only replace a new destination after extraction and validation succeed.
    with tempfile.TemporaryDirectory(prefix="searxng-download-", dir=source.parent) as temporary:
        staged = Path(temporary) / "source"
        download_and_extract(staged, commit, selective=True)
        missing = [name for name in REQUIRED_FILES if not (staged / name).is_file()]
        if missing:
            raise RuntimeError(f"SearXNG archive is missing: {', '.join(missing)}")
        (staged / SOURCE_MARKER).write_text(json.dumps({"commit": commit}), encoding="utf-8")
        staged.rename(source)


def prepare_compatibility() -> None:
    pin = pinned_release()
    # Archives have no .git. Never derive their version from the enclosing
    # SAIVerse repository, and never require Git on PATH to import SearXNG.
    values = {
        "VERSION_STRING": pin["version"], "VERSION_TAG": pin["version"],
        "DOCKER_TAG": pin["version"].replace("+", "-"),
        "GIT_URL": "https://github.com/searxng/searxng", "GIT_BRANCH": pin["commit"],
    }
    frozen = "# Generated by SAIVerse for the pinned SearXNG archive.\n"
    frozen += "".join(f"{key} = {value!r}\n" for key, value in values.items())
    (source_directory() / "searx/version_frozen.py").write_text(frozen, encoding="utf-8")

    if os.name == "nt":
        target = source_directory() / "searx/valkeydb.py"
        body = target.read_text(encoding="utf-8")
        if "import pwd" in body and "class MockPwd:" not in body:
            body = body.replace("import pwd", """try:
    import pwd
except ImportError:
    class MockPwd:
        def getpwuid(self, uid):
            return ['searx']
    pwd = MockPwd()""")
            target.write_text(body, encoding="utf-8")
        # URL/template identifiers use '/' even when enumerated on Windows.
        target = source_directory() / "searx/webutils.py"
        body = target.read_text(encoding="utf-8")
        body = body.replace("str(f.relative_to(static_path))", "f.relative_to(static_path).as_posix()")
        body = body.replace(
            "os.path.join(directory[templates_path_length:], filename)",
            "pathlib.Path(directory).relative_to(templates_path).joinpath(filename).as_posix()",
        )
        target.write_text(body, encoding="utf-8")


def apply_requirement_floors(text: str) -> str:
    """Return requirements.txt text with each REQUIREMENT_FLOORS entry as `name>=floor`.

    The first line naming a package is replaced, later lines naming it are
    dropped, and packages without a line are appended. Applying it again to
    its own result returns the same text. A replaced line keeps neither
    extras nor an environment marker, so REQUIREMENT_FLOORS must only name
    packages whose upstream line uses neither (true of certifi and anyio).
    """
    pending = dict(REQUIREMENT_FLOORS)
    lines = []
    for line in text.splitlines():
        match = _REQUIREMENT_NAME.match(line)
        name = re.sub(r"[-_.]+", "-", match.group(1)).lower() if match else None
        if name in REQUIREMENT_FLOORS:
            if name in pending:
                lines.append(f"{name}>={pending.pop(name)}")
            continue
        lines.append(line)
    lines.extend(f"{name}>={floor}" for name, floor in pending.items())
    return "\n".join(lines) + "\n"


def prepare_requirements() -> None:
    target = source_directory() / "requirements.txt"
    body = target.read_text(encoding="utf-8")
    updated = apply_requirement_floors(body)
    if updated == body:
        return
    # Write beside the target and swap it in, so a failed write never leaves
    # a truncated requirements.txt in the source.
    descriptor, temporary = tempfile.mkstemp(prefix=".requirements-", suffix=".txt", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(updated)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


_VENV_SERIES_RE = re.compile(r"^\s*(?:version|version_info)\s*=\s*(\d+)\.(\d+)")


def venv_series(directory: Path) -> str | None:
    """The Python series (``"3.13"``) a venv was made with, from its pyvenv.cfg."""
    try:
        text = (directory / "pyvenv.cfg").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        match = _VENV_SERIES_RE.match(line)
        if match:
            return f"{match.group(1)}.{match.group(2)}"
    return None


def set_aside_venv_of_other_series() -> Path | None:
    """Rename the SearXNG venv when it was made with another Python series.

    The packages in a venv are built for one series and its ``home`` points at
    that series' install, so after SAIVerse moves to another series (setup
    rebuilt ``.venv``) this venv keeps the old Python, and breaks once that
    Python is uninstalled. Renamed rather than deleted: on Windows a running
    SearXNG holds its files, the rename then fails and nothing is lost.
    Returns the renamed folder, to remove after the new venv is installed.
    """
    venv = venv_directory()
    if not venv.exists():
        return None
    current = f"{sys.version_info[0]}.{sys.version_info[1]}"
    series = venv_series(venv)
    if series == current:
        return None
    aside = venv.with_name(f"{venv.name}.old-{time.strftime('%Y%m%d-%H%M%S')}")
    print(f"[INFO] The SearXNG venv was made with Python {series or 'unknown'}; "
          f"making it again with Python {current}", flush=True)
    try:
        os.replace(venv, aside)
    except OSError as exc:
        raise RuntimeError(
            f"The SearXNG venv {venv} could not be renamed ({exc}). Close the SearXNG window "
            "and run setup again.\nSearXNG の専用の環境を作り直せませんでした。SearXNG の窓を"
            "閉じてから、setup をもう一度実行してください。"
        ) from exc
    return aside


def put_back_set_aside_venv(aside: Path) -> bool:
    """After a failed install: remove the half-made venv and give ``aside``
    its name back. Returns whether that worked; prints what to do when not."""
    venv = venv_directory()
    try:
        if venv.exists():
            shutil.rmtree(venv)
        os.replace(aside, venv)
    except OSError as exc:
        print(
            f"[ERROR] The previous SearXNG venv {aside} could not be put back ({exc}). Delete "
            f"{venv} yourself, rename {aside.name} to {venv.name}, then run setup again.\n"
            f"[ERROR] 前の SearXNG の専用の環境 {aside} を元に戻せませんでした。{venv} を手で消し、"
            f"{aside.name} の名前を {venv.name} に変えてから、setup をもう一度実行してください。",
            file=sys.stderr,
            flush=True,
        )
        return False
    print(
        "[INFO] Put the previous SearXNG venv back; SearXNG runs as it did before.\n"
        "[INFO] 前の SearXNG の専用の環境に戻しました。SearXNG はこれまでどおり動きます。",
        flush=True,
    )
    return True


def setup() -> None:
    print(f"[INFO] SearXNG commit: {pinned_release()['commit']}", flush=True)
    prepare_source()
    prepare_compatibility()
    prepare_requirements()
    aside = set_aside_venv_of_other_series()
    try:
        install_into_venv()
    except BaseException:
        # The venv of the other series was the one that worked: do not leave
        # SearXNG without it, whatever stopped the new one (put_back_set_aside_venv
        # prints whether that worked).
        if aside is not None:
            put_back_set_aside_venv(aside)
        raise
    if aside is not None:
        shutil.rmtree(aside, ignore_errors=True)
    print("[OK] SearXNG setup complete", flush=True)


def install_into_venv() -> None:
    """Create the venv if needed, install the pinned SearXNG into it, write
    the settings and check it (the part of ``setup`` after the set-aside)."""
    if not venv_python().is_file():
        run([sys.executable, "-m", "venv", venv_directory()])
    marker = venv_directory() / INSTALL_MARKER
    already_ready = ready()
    marker.unlink(missing_ok=True)
    if not already_ready:
        run([venv_python(), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"])
        run([venv_python(), "-m", "pip", "install", "-r", source_directory() / "requirements.txt",
             f"tzdata=={pinned_release()['tzdata']}"])
        run([venv_python(), "-m", "pip", "install", "--no-build-isolation", "-e", source_directory()],
            env=probe_environment())
        run([venv_python(), "-m", "pip", "check"])

    # The merge script uses these exact source/settings paths, including when
    # setup was invoked with relative paths from another working directory.
    env = {**os.environ, "SEARXNG_SRC_DIR": str(source_directory()),
           "SEARXNG_SETTINGS_PATH": str(settings_path())}
    run([venv_python(), SCRIPT_DIR / "merge_searxng_settings.py"], env=env)
    limiter = configured_path("SEARXNG_LIMITER_PATH", SCRIPT_DIR / "limiter.toml")
    limiter.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_directory() / "searx/limiter.toml", limiter)
    run([venv_python(), "-c", PROBE, source_directory(), pinned_release()["commit"]], env=probe_environment())
    marker.write_text(json.dumps(install_identity(), indent=2), encoding="utf-8")


# A failed ``setup`` is written here (the SAIVerse folder), so the SAIVerse
# screen shows it in its alert banner (saiverse/environment_alerts.py reads it)
# and not only the setup / SearXNG window. The next successful ``setup``
# removes it. Not written when the update engine runs the setup
# (SKIP_FAILURE_RECORD_ENV): it records the failure in its own record, and a
# second banner would repeat it.
SETUP_FAILURE_RECORD = ".searxng_setup_failed.json"
SKIP_FAILURE_RECORD_ENV = "SAIVERSE_SEARXNG_SKIP_FAILURE_RECORD"


def setup_failure_record_path() -> Path:
    return WORKSPACE_ROOT / SETUP_FAILURE_RECORD


def write_setup_failure(error: str) -> None:
    """Record a failed setup for the banner; a write failure is only reported."""
    if os.environ.get(SKIP_FAILURE_RECORD_ENV) == "1":
        return
    target = setup_failure_record_path()
    document = {"error": error.strip()[-2000:], "failed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    temporary = target.with_name(f"{target.name}.tmp{os.getpid()}")
    try:
        temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, target)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        print(f"[WARN] Could not write {target} ({exc}); the SAIVerse screen will not show this failure",
              file=sys.stderr)


def clear_setup_failure() -> None:
    try:
        setup_failure_record_path().unlink(missing_ok=True)
    except OSError as exc:
        print(f"[WARN] Could not remove {setup_failure_record_path()} ({exc})", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("setup", "check", "source", "python", "settings"))
    args = parser.parse_args()
    try:
        if args.action == "source":
            print(source_directory())
        elif args.action == "python":
            print(venv_python())
        elif args.action == "settings":
            print(settings_path())
        elif args.action == "check":
            return 0 if ready() else 1
        else:
            try:
                setup()
            except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
                write_setup_failure(str(exc))
                raise
            clear_setup_failure()
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"[ERROR] SearXNG: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
