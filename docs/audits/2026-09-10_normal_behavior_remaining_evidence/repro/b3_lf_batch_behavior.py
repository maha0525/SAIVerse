"""LF だけの .bat が Windows の cmd.exe で実際にどう動くかを実測する。

FLOW-27 / FLOW-32 / 元議題 8 の補足。`b3_dist_line_endings.py` で
「配布 ZIP の .bat は CRLF になる」ことは確定した。本スクリプトは
**その CRLF 化が無かったらどうなるか** (= `.gitattributes` の `*.bat eol=crlf`
が守っているものは何か) を、推測ではなく実行で確かめる。

`setup.bat` / `start.bat` は `goto :label`、`for /f`、`call :sub`、
括弧で囲んだ複数行ブロックを実際に使っている
(例: setup.bat:240-268 の `goto :git_skip` / `:git_ready` / `for /f`)。
そこで同じ構文を含む .bat を CRLF 版と LF 版で 1 本ずつ作り、
cmd.exe に食わせて出力と終了コードを比べる。

実行方法
--------
    .venv/Scripts/python.exe docs/audits/2026-09-10_normal_behavior_remaining_evidence/repro/b3_lf_batch_behavior.py

Windows 専用 (cmd.exe が要る)。一時ディレクトリだけを使い、
リポジトリにも `~/.saiverse/` にも触れない。

観測結果 (2026-09-10 実行、Windows 11 Pro 10.0.26200)
-----------------------------------------------------
- CRLF 版と LF 版は、**出力・終了コードともに完全に一致した**。
  `goto :label` / `for /f` / `call :sub` / 括弧の複数行ブロック /
  `setlocal enabledelayedexpansion` のいずれも LF だけで正しく動いた。
- つまり現行の Windows 11 では、LF だけの .bat も実行できる。
  `*.bat eol=crlf` が防いでいるのは「今の Windows で動かないこと」ではない。
  ただしこれは**この 1 台の Windows 11 での観測**であり、
  古い Windows や他のシェル実装に一般化はしない。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# setup.bat / start.bat が実際に使っている構文だけで組む。
BATCH_BODY = "\n".join(
    [
        "@echo off",
        "setlocal enabledelayedexpansion",
        "echo START",
        "if exist \"%~dp0\" (",
        "    echo IN_BLOCK",
        "    set \"VAR=value\"",
        "    echo DELAYED=!VAR!",
        ")",
        "for /f \"tokens=*\" %%v in ('echo forvalue') do set FOUND=%%v",
        "echo FOR=%FOUND%",
        "call :sub arg1",
        "if not exist \".no_such_file_here\" (",
        "    goto :skip",
        ")",
        "echo SHOULD_NOT_PRINT",
        ":skip",
        "echo AFTER_GOTO",
        "endlocal",
        "exit /b 7",
        "",
        ":sub",
        "echo SUB=%~1",
        "exit /b 0",
        "",
    ]
)


def write_batch(path: Path, newline: bytes) -> None:
    data = BATCH_BODY.replace("\n", newline.decode("ascii"))
    path.write_bytes(data.encode("ascii"))


def run_batch(path: Path) -> tuple[int, str]:
    proc = subprocess.run(
        ["cmd.exe", "/c", str(path)],
        cwd=str(path.parent),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    combined = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, combined.replace("\r\n", "\n").strip()


def main() -> int:
    if not sys.platform.startswith("win"):
        print("SKIP: Windows でのみ意味がある検査 (cmd.exe が要る)。未実施。")
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="b3_lf_bat_"))
    try:
        results: dict[str, tuple[int, str]] = {}
        for label, newline in (("CRLF", b"\r\n"), ("LF", b"\n")):
            target = tmp / f"probe_{label}.bat"
            write_batch(target, newline)
            raw = target.read_bytes()
            crlf = raw.count(b"\r\n")
            lone_lf = raw.count(b"\n") - crlf
            print(f"## {label} 版 ({target.name}): CRLF={crlf} 個 / 単独 LF={lone_lf} 個")
            code, out = run_batch(target)
            results[label] = (code, out)
            print(f"   exit={code}")
            for line in out.splitlines():
                print(f"   | {line}")
            print()

        same = results["CRLF"] == results["LF"]
        print(f"## 判定: CRLF 版と LF 版は{'完全に一致した' if same else '食い違った'}")
        if not same:
            print(f"   CRLF: exit={results['CRLF'][0]}")
            print(f"   LF  : exit={results['LF'][0]}")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
