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
        いるときは、何も変えずに終了コード 1。画面のサーバーと SearXNG のサーバーを
        自動で確かめられなかったときは、窓をすべて閉じたら Enter を押すよう利用者に
        頼んで待つ (標準入力が端末でないときは、頼めないので終了コード 1)。
        Linux / macOS は使用中のフォルダも改名できるので、.venv の改名の失敗は
        確かめの代わりにならない。確かめ方は ``check_stopped`` を見る。
    python scripts/setup_venv.py ensure-dotenv
        check-stopped のすぐ後、データのパス (SAIVERSE_HOME) を読む処理より前に呼ぶ。
        このフォルダから上へ探して .env が一つも無いときだけ .env.example を .env に
        写す (上のフォルダの .env が世界を選んでいるなら、それを隠さない)。写せな
        かったら終了コード 1。``ensure_dotenv`` を見る。
    python scripts/setup_venv.py prepare [--expect-series 3.13]
        既存の .venv をそのまま使うか、作り直すかを決める。作り直すときは、
        .venv を消さずに .venv.old-<日時> へ改名する。改名・作成の前に、.venv を
        やり直していることを ``.setup_in_progress/venv.json`` に書く
        (``scripts/setup_in_progress.py``。消すのは finish か rollback)。結果を
        標準出力に一行で書く (説明の文は標準エラーに書く):
            keep            そのまま使う
            create          .venv が無い。新しく作る (失敗しても戻す先は無い)
            renamed <name>  .venv を <name> に改名した。新しく作る
            resume [<name>] 前の回の setup が .venv をやり直している途中で止まっていて、
                            その回が作った .venv が揃っている。.venv はそのまま使い、
                            create / renamed と同じく、パッケージとアドオンの入れ直しを
                            最後まで行う。<name> は失敗したときに戻す古い .venv
            locked          改名か削除を断られた (中のファイルが使用中)。何も変えていない
            error           記録を書けなかった。何も変えていない
        前の回の記録が残っていれば、.venv の様子にかかわらず続きを行う (記録の
        古い方が戻し先になる)。--expect-series を渡さないときは、系列を理由に
        作り直さない (壊れた .venv だけを作り直す)。
    python scripts/setup_venv.py finish
        新しい .venv にパッケージが入り、アドオンの入れ直しが最後まで走ったあとに
        呼ぶ。記録を消し、.venv.old-<日時> の形のフォルダを全部消す。消すときは、先に
        .venv.deleting-<日時> へ改名してから消す (消している途中で止まったものが
        戻し先に選ばれないように。残った .venv.deleting-* も消す)。消せなかったものは
        知らせるだけで、終了コードは 0。
    python scripts/setup_venv.py rollback <name>
        新しい .venv の作成かパッケージの導入に失敗したときに呼ぶ。作りかけの
        .venv を消し、<name> を .venv に戻し、記録を消す。戻せなかったら終了コード 1
        (記録は残り、次の setup が続きを行う)。
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

# setup_in_progress.py sits next to this script; tests load this file by path.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
import setup_in_progress  # noqa: E402

VENV_NAME = ".venv"
OLD_VENV_PREFIX = ".venv.old-"
DELETING_VENV_PREFIX = ".venv.deleting-"
# prepare が作る名前の形。finish はこの形のフォルダだけを消す
# (利用者が手で作った ``.venv.old`` などには触らない)。
OLD_VENV_RE = re.compile(r"^\.venv\.old-\d{8}-\d{6}(?:-\d+)?$")
# finish が消す前に付ける名前の形。戻し先には決してならない。
DELETING_VENV_RE = re.compile(r"^\.venv\.deleting-\d{8}-\d{6}(?:-\d+)?$")
# .setup_in_progress/venv.json: .venv をやり直している (setup_in_progress.py)。
# 中身は {"old": 改名した古い .venv の名前 (作るだけの回は null)}。
VENV_ENTRY = "venv"
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


def _old_venv_name(root: Path, now: _dt.datetime, prefix: str = OLD_VENV_PREFIX) -> str:
    base = f"{prefix}{now:%Y%m%d-%H%M%S}"
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


def _write_venv_entry(root: Path, old_name: str | None) -> bool:
    """「.venv をやり直している」を記録に書く。書けなかったら知らせて False。"""
    try:
        setup_in_progress.write_entry(root, VENV_ENTRY, {
            "old": old_name,
            "started_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        })
    except OSError as exc:
        _say(f"[ERROR] Could not write {setup_in_progress.entry_path(root, VENV_ENTRY)}: {exc}")
        _say("  setup writes it before it changes .venv. Nothing was changed.")
        _say(f"[ERROR] {setup_in_progress.entry_path(root, VENV_ENTRY)} を書けませんでした。")
        _say("  setup は .venv を変える前にこの記録を書きます。何も変更していません。")
        return False
    return True


def _remove_venv_entry(root: Path) -> None:
    """記録を消す。消せなかったら知らせるだけ (次の setup が続きとして入れ直しをやり直す)。"""
    try:
        setup_in_progress.remove_entry(root, VENV_ENTRY)
    except OSError as exc:
        _say(f"[WARN] Could not delete {setup_in_progress.entry_path(root, VENV_ENTRY)}: {exc}")
        _say("  The next setup installs the packages once more.")
        _say(f"[WARN] {setup_in_progress.entry_path(root, VENV_ENTRY)} を消せませんでした。")
        _say("  次の setup で、パッケージの入れ直しをもう一度行います。")


def _say_locked(what_en: str, what_ja: str, exc: OSError, *, kept_en: str, kept_ja: str) -> str:
    _say(f"[ERROR] Could not {what_en}: {exc}")
    _say("  A program is still using files in .venv. Close every SAIVerse window")
    _say("  (and any other program that uses .venv, such as an editor), then run setup again.")
    _say(f"  {kept_en}")
    _say(f"[ERROR] {what_ja}")
    _say("  .venv の中のファイルを使っているプログラムがあります。SAIVerse の窓をすべて閉じて")
    _say("  (.venv を使うエディタなどのプログラムも閉じて)、setup をもう一度実行してください。")
    _say(f"  {kept_ja}")
    return "locked"


def _entry_old_name(root: Path, entry: dict) -> str | None:
    """記録にある古い ``.venv`` の名前。この形の名前でない・フォルダが無いときは None。"""
    old = entry.get("old")
    if isinstance(old, str) and OLD_VENV_RE.match(old) and (root / old).is_dir():
        return old
    return None


def prepare(root: Path, expect_series: str | None, now: _dt.datetime | None = None) -> str:
    """``decide`` の結果に従い、作り直すなら改名する。標準出力に書く一行を返す。

    改名・作成の前に、記録 (``VENV_ENTRY``) に「.venv をやり直している」と古い方の
    名前を書く。記録が残っていれば、前の回の setup がやり直しの途中で止まっていた
    ということなので、.venv の様子にかかわらず続きを行う (``_continue``)。
    """
    entry = setup_in_progress.read_entry(root, VENV_ENTRY)
    action, reason = decide(root, expect_series)
    if entry is not None:
        result = _continue(root, entry, action)
        if result is not None:
            return result
    if action == "keep":
        return "keep"
    if action == "create":
        return "create" if _write_venv_entry(root, None) else "error"
    old_name = _old_venv_name(root, now or _dt.datetime.now())
    _say(f"[SETUP] Rebuilding .venv: {reason}.")
    _say(f"[SETUP] .venv を作り直します ({reason})。")
    if not _write_venv_entry(root, old_name):
        return "error"
    try:
        os.rename(root / VENV_NAME, root / old_name)
    except OSError as exc:
        _remove_venv_entry(root)
        return _say_locked(
            f"rename .venv to {old_name}", f".venv の名前を {old_name} に変えられませんでした。", exc,
            kept_en="Nothing was changed.", kept_ja="何も変更していません。")
    _say(f"[OK] Moved the old .venv to {old_name}. It is deleted after the new .venv is ready.")
    _say(f"[OK] 古い .venv を {old_name} に移しました。新しい .venv が揃ったら消します。")
    return f"renamed {old_name}"


def _continue(root: Path, entry: dict, action: str) -> str | None:
    """前の回の setup がやり直しの途中で止まっていたときの一行。None なら、記録より
    前の状態のまま (前の回は改名する前に止まっていた) なので、ふつうに決める。

    記録の古い方が残っていれば、それが改名済みの戻し先で、いまの ``.venv`` は前の回が
    作ったもの。揃っていて系列も合えば (``keep``) そのまま使い、揃っていなければ消して
    作り直す。古い方が無ければ戻し先は無い: 揃った ``.venv`` は前の回が作ったものか
    改名される前のものかのどちらかで、どちらでもパッケージを入れ直せばよい
    (``resume``)。作り直しが要る ``.venv`` は、ふつうの作り直しと同じく改名して
    戻し先にする (前の回が作りかけたものでも、改名なら失うものは無い)。
    """
    old = _entry_old_name(root, entry)
    _say("[SETUP] The previous setup stopped before it finished; doing the rest of it now.")
    _say("[SETUP] 前の回の setup が終わる前に止まっていたので、残りを行います。")
    if old is not None:
        _say(f"  If this setup fails, the .venv from before that setup ({old}) is put back.")
        _say(f"  この setup が失敗したときは、その setup より前の .venv ({old}) を戻します。")
    if action == "keep":
        return f"resume {old}" if old else "resume"
    if action == "create":
        if old is None and entry.get("old") is not None and not _write_venv_entry(root, None):
            return "error"  # the entry names a folder that is gone; nothing to put back now
        return f"renamed {old}" if old else "create"
    if old is None:
        return None
    try:
        _rmtree(root / VENV_NAME)
    except OSError as exc:
        return _say_locked(
            "delete the .venv the previous setup left unfinished",
            "前の回の setup が作りかけた .venv を消せませんでした。", exc,
            kept_en=f"The .venv from before that setup is kept as {old}.",
            kept_ja=f"その setup より前の .venv は {old} として残っています。")
    _say("[OK] Deleted the .venv the previous setup left unfinished.")
    _say("[OK] 前の回の setup が作りかけた .venv を消しました。")
    return f"renamed {old}"


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


def _say_not_deleted(name: str, exc: OSError) -> None:
    _say(f"[WARN] Could not delete the old environment {name}: {exc}")
    _say(f"  SAIVerse works without it. Close SAIVerse and delete the {name} folder yourself.")
    _say(f"[WARN] 古い環境 {name} を消せませんでした。")
    _say(f"  SAIVerse の動作には影響しません。SAIVerse を閉じてから {name} フォルダを手で消してください。")


def finish(root: Path, now: _dt.datetime | None = None) -> list[str]:
    """記録を消し、``.venv.old-<日時>`` を全部消す。消せなかったフォルダの名前を返す。

    消す前に ``.venv.deleting-<日時>`` へ改名する。途中まで消えたフォルダが
    ``.venv.old-<日時>`` の名前のまま残ると、戻し先と見分けがつかなくなるため。
    改名を断られたもの (Windows で中のファイルが使用中) は消さずに残す (次の
    finish がまた消す)。前の finish が消しきれなかった ``.venv.deleting-*`` も消す。
    """
    _remove_venv_entry(root)
    failed: list[str] = []
    now = now or _dt.datetime.now()
    for path in sorted(root.iterdir()):
        if not (path.is_dir() and OLD_VENV_RE.match(path.name)):
            continue
        deleting = root / _old_venv_name(root, now, DELETING_VENV_PREFIX)
        try:
            os.rename(path, deleting)
        except OSError as exc:
            failed.append(path.name)
            _say_not_deleted(path.name, exc)
            continue
        try:
            _rmtree(deleting)
        except OSError as exc:
            failed.append(deleting.name)
            _say_not_deleted(deleting.name, exc)
            continue
        _say(f"[OK] Deleted the old environment {path.name}")
    for path in sorted(root.iterdir()):
        if not (path.is_dir() and DELETING_VENV_RE.match(path.name)) or path.name in failed:
            continue
        try:
            _rmtree(path)
        except OSError as exc:
            failed.append(path.name)
            _say_not_deleted(path.name, exc)
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
    _remove_venv_entry(root)
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
# itself with the standard library and cannot list the servers. Whenever the
# servers could not be listed, setup asks the user to close the windows and
# waits for Enter instead of going on (a first install is not asked).

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


def find_dotenv_path(root: Path) -> Path | None:
    """The .env main.py's ``load_dotenv()`` reads: python-dotenv's ``find_dotenv``
    starts in the folder of main.py (``root``) and goes up one folder at a time
    to the top, taking the first ``.env`` that is a file or a FIFO
    (``dotenv.main._walk_to_root`` / ``_is_file_or_fifo``). None when there is
    none. ``update_engine.find_dotenv_path`` is the same; the tests compare
    both with python-dotenv itself."""
    current = os.path.abspath(root)
    last = None
    while last != current:
        candidate = os.path.join(current, ".env")
        if os.path.isfile(candidate):
            return Path(candidate)
        try:
            if stat.S_ISFIFO(os.stat(candidate).st_mode):
                return Path(candidate)
        except OSError:
            pass
        last, current = current, os.path.abspath(os.path.join(current, os.path.pardir))
    return None


def saiverse_home(root: Path, environ: dict[str, str] | None = None) -> Path:
    """SAIVERSE_HOME as main.py resolves it: the environment, else the .env
    main.py's ``load_dotenv`` finds (``find_dotenv_path``; already-set variables
    win), else ``~/.saiverse``. Raises ``CannotTell`` when that .env cannot be
    read."""
    environ = dict(os.environ if environ is None else environ)
    value = environ.get("SAIVERSE_HOME")
    if value is None and environ.get("PYTHON_DOTENV_DISABLED", "").casefold() not in _DOTENV_DISABLED_VALUES:
        dotenv_path = find_dotenv_path(root)
        if dotenv_path is not None:
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


def check_stopped(root: Path, stdin=None) -> bool:  # noqa: ANN001 - a text stream
    """Whether setup may change this checkout's environments (see the section
    comment). On False the reason and what to do are printed.

    When the frontend and SearXNG servers cannot be listed (psutil is missing
    or broken in .venv, or there is no usable .venv), setup does not go on by
    itself: it asks the user to close the windows and press Enter
    (``_ask_to_close_windows``). A first install (nothing installed here yet)
    has nothing that could be running and is not asked.
    """
    code = _check_with_venv(root)
    if code == _ENGINE_NOTHING_RUNNING:
        return True
    if code == _ENGINE_MAY_BE_RUNNING:
        _refuse_while_running("see the [check-stopped] line above")
        return False
    if code == _ENGINE_PROCESSES_UNCHECKED:
        return _ask_to_close_windows(stdin)
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
    if not _anything_installed(root):
        return True
    return _ask_to_close_windows(stdin)


def _anything_installed(root: Path) -> bool:
    """Whether a frontend or SearXNG server could run from this folder: its
    .venv, the frontend packages or the SearXNG venv (``SEARXNG_VENV_DIR``,
    relative to the folder, as scripts/searxng_config.py resolves it)."""
    searxng = Path(os.environ.get("SEARXNG_VENV_DIR") or Path("scripts") / ".searxng-venv")
    if not searxng.is_absolute():
        searxng = root / searxng
    return any(path.exists() for path in (root / VENV_NAME, root / "frontend" / "node_modules", searxng))


def _ask_to_close_windows(stdin=None) -> bool:  # noqa: ANN001 - a text stream
    """Ask the user to close the SAIVerse windows and press Enter. False when
    the user stops with Ctrl+C, or when there is no terminal to ask in
    (setup run by another program): then setup stops before changing anything."""
    stream = sys.stdin if stdin is None else stdin
    _say("[WARN] setup could not check by itself whether a SAIVerse window of this folder is open.")
    _say("[WARN] このフォルダの SAIVerse の窓が開いているかを、setup が自動で確かめられませんでした。")
    try:
        interactive = stream is not None and stream.isatty()
    except (AttributeError, ValueError, OSError):
        interactive = False
    if not interactive:
        _say("[ERROR] setup is not running in a terminal, so it cannot ask; it stopped before changing")
        _say("  anything. Close every SAIVerse window (Backend / Frontend / SearXNG), then run setup")
        _say("  again in a terminal.")
        _say("[ERROR] 端末で実行されていないため確認できず、setup は何も変えずに止まりました。")
        _say("  SAIVerse の窓 (Backend / Frontend / SearXNG) をすべて閉じてから、端末で setup を")
        _say("  もう一度実行してください。")
        return False
    _say("  Close every SAIVerse window (Backend / Frontend / SearXNG), then press Enter.")
    _say("  To stop setup, press Ctrl+C.")
    _say("  SAIVerse の窓 (Backend / Frontend / SearXNG) をすべて閉じたら Enter を押してください。")
    _say("  やめるときは Ctrl+C を押してください。")
    try:
        line = stream.readline()
    except (KeyboardInterrupt, EOFError, OSError):
        line = ""
    if not line:
        _say("[INFO] setup stopped before changing anything.")
        _say("[INFO] setup は何も変えずに止まりました。")
        return False
    return True


def ensure_dotenv(root: Path) -> bool:
    """Make sure SAIVerse has a .env, without changing which one it reads.

    main.py reads the first .env from the SAIVerse folder upward
    (``find_dotenv_path``). When there is one -- in this folder or above it --
    it is left as it is: a new .env here would hide one above and change the
    SAIVERSE_HOME (the world) SAIVerse uses. Only when there is none is
    ``.env.example`` copied to ``.env`` here. setup runs this right after it
    checked that SAIVerse is stopped and before anything that reads data
    paths, so every later step reads the .env SAIVerse will read. The .env here
    is made readable by its owner only on Mac / Linux, as setup.sh did.
    Returns False when .env.example could not be copied.
    """
    found = find_dotenv_path(root)
    local = root / ".env"
    if found is None:
        # Created only if it still does not exist ("x"): a .env put here since
        # the check (a restored setting, another setup) is kept, never
        # replaced with the template.
        try:
            template = (root / ".env.example").read_bytes()
            with open(local, "xb") as handle:
                handle.write(template)
        except FileExistsError:
            print(f"[OK] Using the .env that appeared while setup checked: {local}", flush=True)
        except OSError as exc:
            print(f"[ERROR] Could not create .env from .env.example ({exc})", flush=True)
            return False
        else:
            print("[OK] Created .env (API keys can be configured in the first-run tutorial)", flush=True)
        found = local
    elif found.resolve() == local.resolve():
        print("[OK] .env already exists", flush=True)
    else:
        print(f"[OK] Using the .env SAIVerse reads: {found}", flush=True)
    if os.name != "nt" and found.resolve() == local.resolve():
        try:
            os.chmod(local, 0o600)
        except OSError:
            pass
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check, rebuild and clean up SAIVerse's .venv for setup")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check-stopped")
    sub.add_parser("ensure-dotenv")
    p_prepare = sub.add_parser("prepare")
    p_prepare.add_argument("--expect-series", default=None)
    sub.add_parser("finish")
    p_rollback = sub.add_parser("rollback")
    p_rollback.add_argument("old_name")
    args = parser.parse_args(argv)

    root = Path.cwd()
    if args.command == "check-stopped":
        return 0 if check_stopped(root) else 1
    if args.command == "ensure-dotenv":
        return 0 if ensure_dotenv(root) else 1
    if args.command == "prepare":
        print(prepare(root, args.expect_series), flush=True)
        return 0
    if args.command == "finish":
        finish(root)
        return 0
    return 0 if rollback(root, args.old_name) else 1


if __name__ == "__main__":
    sys.exit(main())
