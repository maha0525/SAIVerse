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

    python scripts/setup_venv.py prepare [--expect-series 3.13]
        既存の .venv をそのまま使うか、作り直すかを決める。作り直すときは、
        .venv を消さずに .venv.old-<日時> へ改名する。結果を標準出力に一行で書く
        (説明の文は標準エラーに書く):
            keep            そのまま使う
            create          .venv が無い。新しく作る
            renamed <name>  .venv を <name> に改名した。新しく作る
            locked          改名を断られた (中のファイルが使用中)。何も変えていない
        --expect-series を渡さないときは、系列を理由に作り直さない (壊れた .venv
        だけを作り直す)。
    python scripts/setup_venv.py finish
        新しい .venv にパッケージが入ったあとに呼ぶ。.venv.old-<日時> の形の
        フォルダを全部消す。消せなかったものは知らせるだけで、終了コードは 0。
    python scripts/setup_venv.py rollback <name>
        新しい .venv の作成かパッケージの導入に失敗したときに呼ぶ。作りかけの
        .venv を消し、<name> を .venv に戻す。戻せなかったら終了コード 1。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import re
import shutil
import stat
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


def prepare(root: Path, expect_series: str | None, now: _dt.datetime | None = None) -> str:
    """``decide`` の結果に従い、作り直すなら改名する。標準出力に書く一行を返す。"""
    action, reason = decide(root, expect_series)
    if action == "keep":
        return "keep"
    if action == "create":
        return "create"
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check, rebuild and clean up SAIVerse's .venv for setup")
    sub = parser.add_subparsers(dest="command", required=True)
    p_prepare = sub.add_parser("prepare")
    p_prepare.add_argument("--expect-series", default=None)
    sub.add_parser("finish")
    p_rollback = sub.add_parser("rollback")
    p_rollback.add_argument("old_name")
    args = parser.parse_args(argv)

    root = Path.cwd()
    if args.command == "prepare":
        print(prepare(root, args.expect_series), flush=True)
        return 0
    if args.command == "finish":
        finish(root)
        return 0
    return 0 if rollback(root, args.old_name) else 1


if __name__ == "__main__":
    sys.exit(main())
