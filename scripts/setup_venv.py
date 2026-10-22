"""setup.bat / setup.sh が SAIVerse の ``.venv`` を作る前と後に呼ぶ、標準ライブラリだけの補助。

``.venv`` は作ったときの Python の系列 (3.12 など) に固定される: ``.venv/pyvenv.cfg``
の ``home`` がその Python のフォルダを指し、中のパッケージはその系列専用のファイル
(``cp312`` など) になる。だから Python 3.13 を入れても、``.venv`` は古い系列のまま
動き続ける。setup は 3.13 を名指しして探し (setup.bat はレジストリ、setup.sh は
``python3.13``)、既存の ``.venv`` が 3.13 以外で作られていれば作り直す。設計:
``docs/intent/dependency_management.md`` §2-7。

このスクリプトは ``.venv`` を有効にする前に、setup が選んだ Python で実行する。
SAIVerse のパッケージ (``saiverse/``) も ``.venv`` のパッケージも import しない
(作り直す前の ``.venv`` は動かない Python を指していることがある)。対応する
Python は 3.11 以降 (``SAIVERSE_ALLOW_NON_RECOMMENDED_PYTHON=1`` で推奨外の
Python のまま進むとき、このスクリプトもその Python で動く)。

使い方 (カレントディレクトリは SAIVerse のフォルダ):

    python scripts/setup_venv.py check-stopped
        setup が環境を変え始める前 (Python を選んだ直後) に呼ぶ。SAIVerse 本体
        (runtime marker)・このフォルダの画面のサーバー・SearXNG のサーバーが動いて
        いる、または動いていないと確かめられないときは、何も変えずに終了コード 1。
        Linux / macOS は使用中のフォルダも改名できるので、.venv の改名の失敗は
        確かめの代わりにならない。確かめ方は ``check_stopped`` を見る。
    python scripts/setup_venv.py prepare [--expect-series 3.13]
        既存の .venv をそのまま使うか、作り直すかを決める。作り直すときは、
        .venv を消さずに .venv.old-<日時> へ改名する。結果を標準出力に一行で書く
        (説明の文は標準エラーに書く):
            keep            そのまま使う
            create          .venv が無い。新しく作る
            renamed <name>  .venv を <name> に改名した (前の回の setup が改名した
                            あと、新しい .venv を作る前に止まっていた場合も)。新しく作る
            resume <name>   前の回の setup が .venv を <name> に改名して新しい .venv を
                            作ったあと、終わる前に止まっていた。.venv はそのまま使い、
                            renamed と同じく、パッケージとアドオンの入れ直しを最後まで行う
            locked          改名を断られた (中のファイルが使用中)。何も変えていない
        --expect-series を渡さないときは、系列を理由に作り直さない (壊れた .venv
        だけを作り直す)。
    python scripts/setup_venv.py finish
        新しい .venv にパッケージが入り、アドオンの入れ直しの結果を記録したあとに
        呼ぶ。.venv.old-<日時> の形のフォルダを全部消す。消せなかったものは知らせる
        だけで、終了コードは 0。
    python scripts/setup_venv.py rollback <name>
        新しい .venv の作成かパッケージの導入に失敗したときに呼ぶ。作りかけの
        .venv を消し、<name> を .venv に戻す。戻せなかったら終了コード 1。
"""
from __future__ import annotations

import argparse
import codecs
import datetime as _dt
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

VENV_NAME = ".venv"
OLD_VENV_PREFIX = ".venv.old-"
# prepare が作る名前の形。finish はこの形のフォルダだけを消す
# (利用者が手で作った ``.venv.old`` などには触らない)。
OLD_VENV_RE = re.compile(r"^\.venv\.old-\d{8}-\d{6}(?:-\d+)?$")
_SERIES_RE = re.compile(r"^(\d+)\.(\d+)(?:\.|$)")


def _say(text: str) -> None:
    """説明の文は標準エラーへ (標準出力は setup が結果の一行として読む)。"""
    print(text, file=sys.stderr, flush=True)


def _is_windows() -> bool:
    return os.name == "nt"


def venv_is_complete(venv_dir: Path) -> bool:
    """有効にするためのファイルと python の実行ファイルが揃っているか。"""
    if _is_windows():
        return (venv_dir / "Scripts" / "activate.bat").is_file() and (
            venv_dir / "Scripts" / "python.exe"
        ).is_file()
    return (venv_dir / "bin" / "activate").is_file() and (venv_dir / "bin" / "python").exists()


def read_venv_series(venv_dir: Path) -> str | None:
    """``pyvenv.cfg`` の ``version`` の行から系列 (``"3.12"``) を返す。

    標準の ``venv`` は ``version = 3.12.4`` を書く。``version`` の行が無ければ、
    uv や virtualenv が書く ``version_info`` の行を見る。ファイルが読めない・
    どちらの行も無い・数字で始まらないときは None (系列が分からない)。
    """
    try:
        text = (venv_dir / "pyvenv.cfg").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values.setdefault(key.strip().lower(), value.strip())
    for key in ("version", "version_info"):
        match = _SERIES_RE.match(values.get(key, ""))
        if match:
            return f"{match.group(1)}.{match.group(2)}"
    return None


def _old_venv_name(root: Path, now: _dt.datetime) -> str:
    base = f"{OLD_VENV_PREFIX}{now:%Y%m%d-%H%M%S}"
    name = base
    n = 2
    while (root / name).exists():
        name = f"{base}-{n}"
        n += 1
    return name


def decide(root: Path, expect_series: str | None) -> tuple[str, str]:
    """``(action, reason)``。action は ``keep`` / ``create`` / ``rebuild``。"""
    venv_dir = root / VENV_NAME
    if not venv_dir.exists():
        return "create", ".venv does not exist"
    if not venv_is_complete(venv_dir):
        return "rebuild", ".venv is incomplete (the activate script or python is missing)"
    if expect_series is None:
        return "keep", "the Python series is not checked"
    series = read_venv_series(venv_dir)
    if series is None:
        return "rebuild", "the Python series of .venv is unknown (pyvenv.cfg has no readable version line)"
    if series != expect_series:
        return "rebuild", f".venv was created with Python {series}, setup needs Python {expect_series}"
    return "keep", f".venv uses Python {series}"


def _leftover_old_venv(root: Path) -> str | None:
    """前の回の setup が改名したまま残した ``.venv.old-<日時>`` のうち、一番新しい
    揃ったもの (有効にするためのファイルと python がある) の名前。無ければ None。

    揃っていないもの (``finish`` が途中まで消して残したもの) は戻し先にならないので
    数えない (``finish`` がまた消す)。
    """
    names = [
        path.name
        for path in root.iterdir()
        if path.is_dir() and OLD_VENV_RE.match(path.name) and venv_is_complete(path)
    ]
    return max(names, key=_old_venv_sort_key) if names else None


_OLD_VENV_STAMP_RE = re.compile(r"^(\d{8}-\d{6})(?:-(\d+))?$")


def _old_venv_sort_key(name: str) -> tuple[str, int]:
    """``.venv.old-<日時>`` と ``.venv.old-<日時>-<n>`` を作った順に並べる鍵。"""
    match = _OLD_VENV_STAMP_RE.match(name[len(OLD_VENV_PREFIX):])
    if match is None:
        return ("", 0)
    return (match.group(1), int(match.group(2) or 1))


def prepare(root: Path, expect_series: str | None, now: _dt.datetime | None = None) -> str:
    """``decide`` の結果に従い、作り直すなら改名する。標準出力に書く一行を返す。

    前の回の setup が古い ``.venv`` を改名したあと、終わる前に止まっていた
    (``.venv.old-<日時>`` が残っている) ときは、その名前を返して続きを行わせる。
    そのままだと、次の回は残った新しい ``.venv`` を ``keep`` と読み、アドオンの
    本体側の入れ直し (``--core-venv-recreated``) を飛ばし、pip が失敗しても古い方へ
    戻せず、``finish`` が古い方を消してしまう。
    """
    action, reason = decide(root, expect_series)
    leftover = _leftover_old_venv(root)
    if action == "keep":
        if leftover is None:
            return "keep"
        _say(f"[SETUP] The previous setup stopped before it finished; continuing it. "
             f"The previous .venv stays as {leftover} until this setup finishes.")
        _say(f"[SETUP] 前の回の setup が終わる前に止まっていたので、続きから行います。"
             f"前の .venv は、この setup が終わるまで {leftover} として残します。")
        return f"resume {leftover}"
    if action == "create":
        if leftover is None:
            return "create"
        _say(f"[SETUP] The previous setup moved the old .venv to {leftover} and stopped; "
             "creating the new .venv now.")
        _say(f"[SETUP] 前の回の setup が古い .venv を {leftover} に移したまま止まっていたので、"
             "新しい .venv を作ります。")
        return f"renamed {leftover}"
    old_name = _old_venv_name(root, now or _dt.datetime.now())
    _say(f"[SETUP] Rebuilding .venv: {reason}.")
    _say(f"[SETUP] .venv を作り直します ({reason})。")
    try:
        os.rename(root / VENV_NAME, root / old_name)
    except OSError as exc:
        _say(f"[ERROR] Could not rename .venv to {old_name}: {exc}")
        _say("  A program is still using files in .venv. Close every SAIVerse window")
        _say("  (and any other program that uses .venv, such as an editor), then run setup again.")
        _say("  Nothing was changed.")
        _say(f"[ERROR] .venv の名前を {old_name} に変えられませんでした。")
        _say("  .venv の中のファイルを使っているプログラムがあります。SAIVerse の窓をすべて閉じて")
        _say("  (.venv を使うエディタなどのプログラムも閉じて)、setup をもう一度実行してください。")
        _say("  何も変更していません。")
        return "locked"
    _say(f"[OK] Moved the old .venv to {old_name}. It is deleted after the new .venv is ready.")
    _say(f"[OK] 古い .venv を {old_name} に移しました。新しい .venv が揃ったら消します。")
    if leftover is not None and not venv_is_complete(root / old_name):
        # The .venv just moved is the half-made one of the previous setup; the
        # one to put back if this setup fails is the one that setup moved.
        _say(f"[SETUP] If this setup fails, the .venv from before the previous setup ({leftover}) is put back.")
        _say(f"[SETUP] この setup が失敗したときは、前の回の setup より前の .venv ({leftover}) を戻します。")
        return f"renamed {leftover}"
    return f"renamed {old_name}"


def _on_rm_error(func, path, exc_info):  # noqa: ANN001 - shutil.rmtree onerror signature
    """読み取り専用のファイルは書き込み可にしてから消し直す。"""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _on_rm_exc(func, path, exc):  # noqa: ANN001 - shutil.rmtree onexc signature
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _rmtree(path: Path) -> None:
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_on_rm_exc)
    else:
        shutil.rmtree(path, onerror=_on_rm_error)


def finish(root: Path) -> list[str]:
    """``.venv.old-<日時>`` を全部消す。消せなかったフォルダの名前を返す。"""
    failed: list[str] = []
    for path in sorted(root.iterdir()):
        if not (path.is_dir() and OLD_VENV_RE.match(path.name)):
            continue
        try:
            _rmtree(path)
        except OSError as exc:
            failed.append(path.name)
            _say(f"[WARN] Could not delete the old environment {path.name}: {exc}")
            _say(f"  SAIVerse works without it. Close SAIVerse and delete the {path.name} folder yourself.")
            _say(f"[WARN] 古い環境 {path.name} を消せませんでした。")
            _say(f"  SAIVerse の動作には影響しません。SAIVerse を閉じてから {path.name} フォルダを手で消してください。")
            continue
        _say(f"[OK] Deleted the old environment {path.name}")
    return failed


def rollback(root: Path, old_name: str) -> bool:
    """作りかけの ``.venv`` を消して ``old_name`` を ``.venv`` に戻す。戻せたら True。"""
    if not OLD_VENV_RE.match(old_name):
        _say(f"[ERROR] {old_name!r} is not a folder name this script creates; nothing was changed.")
        return False
    old_dir = root / old_name
    venv_dir = root / VENV_NAME
    if not old_dir.is_dir():
        _say(f"[ERROR] The previous environment {old_name} is not there; nothing was changed.")
        return False
    try:
        if venv_dir.exists():
            _rmtree(venv_dir)
        os.rename(old_dir, venv_dir)
    except OSError as exc:
        _say(f"[ERROR] Could not put the previous .venv back: {exc}")
        _say(f"  Delete the .venv folder yourself, rename {old_name} to .venv, then run setup again.")
        _say("[ERROR] 前の .venv に戻せませんでした。")
        _say(f"  .venv フォルダを手で消し、{old_name} の名前を .venv に変えてから、setup をもう一度実行してください。")
        return False
    _say("[INFO] Put the previous .venv back. SAIVerse starts as it did before this setup.")
    _say("  Fix the problem shown above (for example the network connection), then run setup again.")
    _say("[INFO] 前の .venv に戻しました。SAIVerse はこの setup の前と同じように起動できます。")
    _say("  上に出ている問題 (ネットワークの接続など) を直してから、setup をもう一度実行してください。")
    return True


# --- check-stopped -----------------------------------------------------------
#
# setup changes .venv, the frontend packages, the portable Node.js and SearXNG,
# so it must not start while SAIVerse runs. The exact check is the one of the
# code itself: ``update_engine.py --check-stopped`` (the runtime markers with
# SAIVERSE_HOME resolved as main.py resolves it, and this folder's frontend and
# SearXNG servers through psutil). It needs the existing .venv. When that
# cannot run it (no .venv: a first install; a .venv whose Python is gone or
# that lacks python-dotenv / psutil), this script checks the runtime markers
# itself with the standard library and cannot list the servers.

# update_engine.check_stopped_for_setup's exit codes.
_ENGINE_NOTHING_RUNNING = 0
_ENGINE_MAY_BE_RUNNING = 20
_ENGINE_PROCESSES_UNCHECKED = 21
CHECK_STOPPED_TIMEOUT_SECONDS = 120


class CannotTell(Exception):
    """Where SAIVerse keeps its data (and so its runtime markers) cannot be told."""


def _venv_python(root: Path) -> Path:
    venv_dir = root / VENV_NAME
    return venv_dir / ("Scripts/python.exe" if _is_windows() else "bin/python")


def _check_with_venv(root: Path) -> int | None:
    """``update_engine.py --check-stopped`` with the existing .venv's python.
    Its exit code, or None when it could not be started."""
    python = _venv_python(root)
    engine = root / "scripts" / "update_engine.py"
    if not (python.is_file() and engine.is_file()):
        return None
    try:
        return subprocess.run(
            [str(python), str(engine), "--check-stopped"],
            cwd=str(root),
            timeout=CHECK_STOPPED_TIMEOUT_SECONDS,
        ).returncode
    except (OSError, subprocess.SubprocessError) as exc:
        _say(f"[INFO] The check with the existing .venv could not run ({exc}).")
        return None


# The .env grammar and the ``${NAME}`` / ``${NAME:-default}`` expansion of
# python-dotenv 1.x (dotenv/parser.py and dotenv/variables.py), which main.py's
# ``load_dotenv()`` uses. Kept here because this script runs before .venv
# exists; tests/test_setup_venv.py checks it against python-dotenv itself.
_ENV_MULTILINE_WHITESPACE = re.compile(r"\s*", re.MULTILINE)
_ENV_WHITESPACE = re.compile(r"[^\S\r\n]*")
_ENV_EXPORT = re.compile(r"(?:export[^\S\r\n]+)?")
_ENV_SINGLE_QUOTED_KEY = re.compile(r"'([^']+)'")
_ENV_UNQUOTED_KEY = re.compile(r"([^=\#\s]+)")
_ENV_EQUAL_SIGN = re.compile(r"(=[^\S\r\n]*)")
_ENV_SINGLE_QUOTED_VALUE = re.compile(r"'((?:\\.|[^'\\])*)'", re.DOTALL)
_ENV_DOUBLE_QUOTED_VALUE = re.compile(r'"((?:\\.|[^"\\])*)"', re.DOTALL)
_ENV_UNQUOTED_VALUE = re.compile(r"([^\r\n]*)")
_ENV_COMMENT = re.compile(r"(?:[^\S\r\n]*#[^\r\n]*)?")
_ENV_END_OF_LINE = re.compile(r"[^\S\r\n]*(?:\r\n|\n|\r|$)")
_ENV_REST_OF_LINE = re.compile(r"[^\r\n]*(?:\r|\n|\r\n)?")
_ENV_DOUBLE_QUOTE_ESCAPES = re.compile(r"\\[\\'\"abfnrtv]")
_ENV_SINGLE_QUOTE_ESCAPES = re.compile(r"\\[\\']")
_ENV_VARIABLE = re.compile(r"\$\{(?P<name>[^\}:]*)(?::-(?P<default>[^\}]*))?\}")
_DOTENV_DISABLED_VALUES = frozenset({"1", "true", "t", "yes", "y"})


class _EnvSyntax(Exception):
    pass


def _env_match(regex: re.Pattern[str], text: str, position: int) -> re.Match[str]:
    match = regex.match(text, position)
    if match is None:
        raise _EnvSyntax
    return match


def _decode_escapes(regex: re.Pattern[str], value: str) -> str:
    return regex.sub(lambda match: codecs.decode(match.group(0), "unicode-escape"), value)


def parse_dotenv(text: str) -> list[tuple[str, str | None]]:
    """The ``(name, value)`` pairs of a .env text, as python-dotenv parses them
    (a line it cannot parse is skipped, as python-dotenv skips it)."""
    text = text.removeprefix("﻿")
    pairs: list[tuple[str, str | None]] = []
    position = 0
    while position < len(text):
        try:
            position = _env_match(_ENV_MULTILINE_WHITESPACE, text, position).end()
            if position >= len(text):
                break
            position = _env_match(_ENV_EXPORT, text, position).end()
            char = text[position:position + 1]
            key: str | None
            if char == "#":
                key = None
            else:
                regex = _ENV_SINGLE_QUOTED_KEY if char == "'" else _ENV_UNQUOTED_KEY
                match = _env_match(regex, text, position)
                key, position = match.group(1), match.end()
            position = _env_match(_ENV_WHITESPACE, text, position).end()
            value: str | None = None
            if text[position:position + 1] == "=":
                position = _env_match(_ENV_EQUAL_SIGN, text, position).end()
                char = text[position:position + 1]
                if char == "'":
                    match = _env_match(_ENV_SINGLE_QUOTED_VALUE, text, position)
                    value, position = _decode_escapes(_ENV_SINGLE_QUOTE_ESCAPES, match.group(1)), match.end()
                elif char == '"':
                    match = _env_match(_ENV_DOUBLE_QUOTED_VALUE, text, position)
                    value, position = _decode_escapes(_ENV_DOUBLE_QUOTE_ESCAPES, match.group(1)), match.end()
                elif char in ("", "\n", "\r"):
                    value = ""
                else:
                    match = _env_match(_ENV_UNQUOTED_VALUE, text, position)
                    value = re.sub(r"\s+#.*", "", match.group(1)).rstrip()
                    position = match.end()
            position = _env_match(_ENV_COMMENT, text, position).end()
            position = _env_match(_ENV_END_OF_LINE, text, position).end()
        except _EnvSyntax:
            position = _ENV_REST_OF_LINE.match(text, position).end()  # type: ignore[union-attr]
            continue
        if key is not None:
            pairs.append((key, value))
    return pairs


def resolve_dotenv(pairs: list[tuple[str, str | None]], environ: dict[str, str]) -> dict[str, str | None]:
    """``${...}`` expanded as ``load_dotenv`` (override=False) expands it: a
    variable already in ``environ`` wins over an earlier line of the file."""
    resolved: dict[str, str | None] = {}
    for name, value in pairs:
        if value is None:
            resolved[name] = None
            continue
        env: dict[str, str | None] = {**resolved, **environ}

        def substitute(match: re.Match[str], env: dict[str, str | None] = env) -> str:
            default = match.group("default")
            result = env.get(match.group("name"), default if default is not None else "")
            return result if result is not None else ""

        resolved[name] = _ENV_VARIABLE.sub(substitute, value)
    return resolved


def saiverse_home(root: Path, environ: dict[str, str] | None = None) -> Path:
    """SAIVERSE_HOME as main.py resolves it: the environment, else the checkout's
    .env (``load_dotenv``, already-set variables win), else ``~/.saiverse``.
    Raises ``CannotTell`` when .env exists but cannot be read."""
    environ = dict(os.environ if environ is None else environ)
    value = environ.get("SAIVERSE_HOME")
    if value is None and environ.get("PYTHON_DOTENV_DISABLED", "").casefold() not in _DOTENV_DISABLED_VALUES:
        dotenv_path = root / ".env"
        if dotenv_path.is_file():
            try:
                text = dotenv_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise CannotTell(f"{dotenv_path} could not be read ({exc})") from exc
            value = resolve_dotenv(parse_dotenv(text), environ).get("SAIVERSE_HOME")
    if value:
        return Path(value)
    return Path.home() / ".saiverse"


# What _process_created_at returns for a process that does not exist.
_GONE = "gone"


def _windows_process_created_at(pid: int) -> float | str:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    filetime_p = ctypes.POINTER(wintypes.FILETIME)
    kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE, filetime_p, filetime_p, filetime_p, filetime_p)
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    process_query_limited_information = 0x1000
    error_invalid_parameter = 87
    still_active = 259
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == error_invalid_parameter:
            return _GONE
        raise OSError(error, f"pid {pid} exists but cannot be opened")
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            raise OSError(ctypes.get_last_error(), f"the state of pid {pid} cannot be read")
        if exit_code.value != still_active:
            return _GONE
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            raise OSError(ctypes.get_last_error(), f"the start time of pid {pid} cannot be read")
        ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        return ticks / 10_000_000 - 11_644_473_600  # FILETIME (1601, 100 ns) -> Unix time
    finally:
        kernel32.CloseHandle(handle)


def _linux_process_created_at(pid: int) -> float | str:
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return _GONE
    # The fields after the command name in parentheses start at field 3;
    # starttime is field 22, in clock ticks since boot (as psutil reads it).
    start_ticks = int(stat_text[stat_text.rindex(")") + 2:].split()[19])
    boot_time = None
    for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
        if line.startswith("btime "):
            boot_time = float(line.split()[1])
    if boot_time is None:
        raise OSError(f"the boot time cannot be read, so pid {pid} cannot be verified")
    return boot_time + start_ticks / os.sysconf("SC_CLK_TCK")


def _process_created_at(pid: int) -> float | str | None:
    """The start time of ``pid`` as Unix time (as psutil's ``create_time``),
    ``_GONE`` when there is no such process, None when it exists but its start
    time cannot be read here. Raises OSError when not even that can be told."""
    if _is_windows():
        return _windows_process_created_at(pid)
    if Path("/proc/self/stat").is_file():
        return _linux_process_created_at(pid)
    try:
        os.kill(pid, 0)  # POSIX only: signal 0 checks that the process exists
    except ProcessLookupError:
        return _GONE
    except PermissionError:
        return None
    return None


# A reused pid starts at least the lifetime of the earlier process later, so
# one second of tolerance cannot mistake it for the marker's process.
_START_TIME_TOLERANCE_SECONDS = 1.0


def runtime_marker_status(home: Path) -> tuple[str, str]:
    """``("stopped" | "running" | "unknown", reason)`` from the runtime markers
    under ``home``, read the way ``saiverse.runtime_marker.marker_status``
    reads them, with the standard library only."""
    paths: list[Path] = []
    directory = home / ".runtime"
    if directory.is_dir():
        paths.extend(sorted(directory.glob("*.json")))
    legacy = home / ".runtime.json"
    if legacy.exists():
        paths.append(legacy)
    if not paths:
        return "stopped", "runtime markers absent"
    unknown: list[str] = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pid = int(data["pid"])
            if not isinstance(data["token"], str) or not data["token"]:
                raise ValueError("invalid token")
            city = data["city_name"]
            if not isinstance(city, str) or not city:
                raise ValueError("invalid city name")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            unknown.append(f"runtime marker {path} is unreadable: {exc}")
            continue
        try:
            actual = _process_created_at(pid)
        except (OSError, ValueError, IndexError) as exc:
            unknown.append(f"pid {pid} of {path} cannot be checked: {exc}")
            continue
        if actual == _GONE:
            continue
        expected = data.get("process_created_at")
        if (
            actual is None
            or not isinstance(expected, (int, float))
            or isinstance(expected, bool)
            or not math.isfinite(expected)
        ):
            unknown.append(f"pid {pid} of {path} is alive and cannot be told apart from SAIVerse")
            continue
        if abs(float(actual) - float(expected)) <= _START_TIME_TOLERANCE_SECONDS:
            return "running", f"SAIVerse City {city!r} pid {pid} ({path})"
    if unknown:
        return "unknown", "; ".join(unknown)
    return "stopped", "only stale runtime markers were found"


def _refuse_while_running(reason: str) -> None:
    _say(f"[ERROR] SAIVerse may still be running: {reason}")
    _say("  setup stopped before changing anything. Close every SAIVerse window")
    _say("  (Backend / Frontend / SearXNG), then run setup again.")
    _say("  If the reason above is a runtime marker and no SAIVerse is running anywhere, the marker")
    _say("  is left over: delete the files under .runtime in the SAIVerse data folder")
    _say("  (~/.saiverse unless SAIVERSE_HOME is set), then run setup again.")
    _say("[ERROR] SAIVerse が動いている (か確かめられない) ため、setup は何も変えずに止まりました。")
    _say("  SAIVerse の窓 (Backend / Frontend / SearXNG) をすべて閉じてから、setup をもう一度実行してください。")
    _say("  上の理由が runtime marker で、どこでも SAIVerse を動かしていないのに止まるときは、")
    _say("  SAIVerse のデータのフォルダ (SAIVERSE_HOME を設定していなければ ~/.saiverse) の")
    _say("  .runtime の中のファイルが残っています。それを消してから、setup をもう一度実行してください。")


def check_stopped(root: Path) -> bool:
    """Whether setup may change this checkout's environments (see the section
    comment). On False the reason and what to do are printed."""
    code = _check_with_venv(root)
    if code == _ENGINE_NOTHING_RUNNING:
        return True
    if code == _ENGINE_MAY_BE_RUNNING:
        _refuse_while_running("see the [check-stopped] line above")
        return False
    if code == _ENGINE_PROCESSES_UNCHECKED:
        _warn_servers_unchecked()
        return True
    # No usable .venv for the exact check: the runtime markers with the
    # standard library; the servers cannot be listed without psutil.
    try:
        home = saiverse_home(root)
    except CannotTell as exc:
        _refuse_while_running(f"where SAIVerse keeps its data cannot be told: {exc}")
        return False
    state, reason = runtime_marker_status(home)
    if state != "stopped":
        _refuse_while_running(reason)
        return False
    if (root / VENV_NAME).exists():
        # A first install has nothing that could be running here.
        _warn_servers_unchecked()
    return True


def _warn_servers_unchecked() -> None:
    _say("[WARN] Could not check whether this folder's SAIVerse Frontend or SearXNG window is open;")
    _say("  setup goes on. If one is open, close this window and that one, then run setup again.")
    _say("[WARN] このフォルダの SAIVerse の Frontend や SearXNG の窓が開いているかを確かめられませんでした。")
    _say("  setup は続けます。開いている場合は、この窓とその窓を閉じてから、setup をもう一度実行してください。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check, rebuild and clean up SAIVerse's .venv for setup")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check-stopped")
    p_prepare = sub.add_parser("prepare")
    p_prepare.add_argument("--expect-series", default=None)
    sub.add_parser("finish")
    p_rollback = sub.add_parser("rollback")
    p_rollback.add_argument("old_name")
    args = parser.parse_args(argv)

    root = Path.cwd()
    if args.command == "check-stopped":
        return 0 if check_stopped(root) else 1
    if args.command == "prepare":
        print(prepare(root, args.expect_series), flush=True)
        return 0
    if args.command == "finish":
        finish(root)
        return 0
    return 0 if rollback(root, args.old_name) else 1


if __name__ == "__main__":
    sys.exit(main())
