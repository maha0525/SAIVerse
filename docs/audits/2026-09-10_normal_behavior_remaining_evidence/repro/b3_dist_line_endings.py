"""配布 ZIP (git archive) の改行が、受け取った人の環境で正しく動く形かを実測する。

FLOW-27 / FLOW-32 / 元議題 8。前段 (docs/audits/2026-09-09_product_normal_behavior/
flows/F_ops.md §5) が「実行していないので不明」と残した境界を、実際に生成して確かめる。

確かめること
------------
1. `.github/workflows/release.yml:17` と同じコマンドで配布 ZIP を作り、その中の
   `.sh` / `.bat` / `.ps1` の改行を 1 バイトずつ判定する。
   - `.sh` に CRLF が入っていれば、受け取った人の bash が
     `/usr/bin/env bash\r` を探して落ちる (実害)。
   - `.bat` が LF かどうかも併せて記録する。
2. その改行が「git が保管している中身 (blob)」と同じか、
   `git archive` が `.gitattributes` の `eol=` を適用して変換しているかを区別する。
3. ZIP を展開して `setup.bat:257-264` / `setup.sh:105-110` と同じ
   `git init` → `fetch` → `reset origin/main` を踏み、その直後に
   `scripts/update_engine.py:745-750` と同じ
   `git status --porcelain --untracked-files=no` が「変更あり」を返すかを見る。
   ここが汚れると README.md:139 の約束が FLOW-27 の更新拒否に直行する。
   一般利用者の Windows 既定 (`core.autocrlf=true`) を含む 3 通りで踏む。

実行方法
--------
    .venv/Scripts/python.exe docs/audits/2026-09-10_normal_behavior_remaining_evidence/repro/b3_dist_line_endings.py

すべて読み取り専用。作業ツリー・index・ref を一切変更しない
(`git archive` / `git cat-file` / `git ls-tree` のみ)。生成物は一時ディレクトリに
置き、終了時に消す。ZIP はどこにも公開しない。

観測結果 (2026-09-10 実行、git 2.49.0.windows.1、origin/main = e7d8e7d4)
-----------------------------------------------------------------------
- `git archive` は `.gitattributes` の `eol=` を**適用する**。git が保管している
  blob は 23 スクリプト全件 LF だが、ZIP の中では 14 件が CRLF に変換されていた。
  内訳は `.bat` 8 件と `.ps1` 6 件で、`eol=crlf` を指定した対象と完全に一致する。
- `.sh` は ZIP の中でも全件 LF (9 件)。CRLF が混ざったものは 0 件。
  **受け取った人の Mac/Linux で `bash: setup.sh: /bin/bash^M: bad interpreter`
  になる形にはなっていない。**
- `.gitattributes` に `export-ignore` / `export-subst` の行は 0 件。
  よって ZIP には tests/ も docs/ も .github/ もそのまま入る (改行とは別の論点)。
- 展開 → `git init` → `reset origin/main` の直後、
  `git status --porcelain --untracked-files=no` は
  **core.autocrlf = true / input / false の 3 通りすべてで空**だった。
  ZIP が届けた CRLF の .bat は checkin 側の正規化で LF に戻り、blob と一致する。
  つまり FLOW-27 の更新拒否 (update_engine.py:752) には**直行しない**。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]

# release.yml が実際に固める ref。リリースは main 上のタグで打たれるので
# origin/main を使う (ローカルの作業ブランチではなく)。
ARCHIVE_REF = "origin/main"

SCRIPT_SUFFIXES = (".sh", ".bat", ".ps1", ".cmd")


def run(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=str(cwd),
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def classify_eol(data: bytes) -> str:
    """バイト列の改行の種類を返す。"""
    crlf = data.count(b"\r\n")
    lf_total = data.count(b"\n")
    lone_lf = lf_total - crlf
    lone_cr = data.count(b"\r") - crlf
    if crlf and not lone_lf and not lone_cr:
        return "CRLF"
    if lone_lf and not crlf and not lone_cr:
        return "LF"
    if not crlf and not lone_lf and not lone_cr:
        return "none"
    return f"MIXED(crlf={crlf},lf={lone_lf},cr={lone_cr})"


def build_archive(dest: Path) -> None:
    """release.yml:17 と同じコマンド。"""
    run(
        [
            "git",
            "archive",
            "--format=zip",
            "--prefix=SAIVerse/",
            "-o",
            str(dest),
            ARCHIVE_REF,
        ],
        cwd=REPO,
    )


def blob_bytes(path: str) -> bytes:
    """git が保管している中身 (変換前の blob)。"""
    proc = subprocess.run(
        ["git", "cat-file", "blob", f"{ARCHIVE_REF}:{path}"],
        cwd=str(REPO),
        check=True,
        capture_output=True,
    )
    return proc.stdout


def inspect_zip(zip_path: Path) -> list[tuple[str, str, str]]:
    """ZIP 内スクリプトの改行を blob と並べて返す: (path, zip_eol, blob_eol)。"""
    rows: list[tuple[str, str, str]] = []
    with zipfile.ZipFile(zip_path) as zf:
        for name in sorted(zf.namelist()):
            inner = name[len("SAIVerse/") :] if name.startswith("SAIVerse/") else name
            if not inner or not inner.lower().endswith(SCRIPT_SUFFIXES):
                continue
            rows.append((inner, classify_eol(zf.read(name)), classify_eol(blob_bytes(inner))))
    return rows


def simulate_setup(zip_path: Path, workdir: Path, autocrlf: str) -> tuple[str, str]:
    """ZIP 展開 → setup.bat / setup.sh と同じ git 初期化 → 更新前検査。

    戻り値: (git status --porcelain の出力, ls-files --eol の抜粋)
    """
    extract = workdir / f"extract_{autocrlf}"
    extract.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract)
    proj = extract / "SAIVerse"

    # setup.bat:257-264 / setup.sh:105-110 と同じ順序。
    # origin は本物の GitHub ではなくローカルの repo を指す (ネットワークを使わない)。
    run(["git", "init", "-q"], cwd=proj)
    run(["git", "config", "core.autocrlf", autocrlf], cwd=proj)
    run(["git", "config", "user.email", "repro@example.invalid"], cwd=proj)
    run(["git", "config", "user.name", "repro"], cwd=proj)
    run(["git", "remote", "add", "origin", REPO.as_uri()], cwd=proj)
    run(["git", "fetch", "-q", "--depth", "1", "origin", ARCHIVE_REF.split("/", 1)[1]], cwd=proj)
    run(["git", "branch", "-M", "main"], cwd=proj)
    run(["git", "reset", "-q", "FETCH_HEAD"], cwd=proj)

    # update_engine.py:745-750 が更新の可否を決める、まさにその検査。
    status = run(
        ["git", "status", "--porcelain", "--untracked-files=no"], cwd=proj
    ).stdout
    eol = run(
        ["git", "ls-files", "--eol", "setup.sh", "setup.bat", "start.bat", "main.py"],
        cwd=proj,
    ).stdout
    return status, eol


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="b3_dist_eol_"))
    try:
        zip_path = tmp / "SAIVerse.zip"
        build_archive(zip_path)
        print(f"# archive ref : {ARCHIVE_REF} ({run(['git', 'rev-parse', ARCHIVE_REF], cwd=REPO).stdout.strip()})")
        print(f"# archive size: {zip_path.stat().st_size:,} bytes")
        print(f"# git version : {run(['git', '--version'], cwd=REPO).stdout.strip()}")

        print("\n## 1. 配布 ZIP の中のスクリプトの改行 (zip / git の blob)")
        rows = inspect_zip(zip_path)
        width = max(len(r[0]) for r in rows)
        mismatched = 0
        for path, zip_eol, blob_eol in rows:
            flag = ""
            if zip_eol != blob_eol:
                flag = "  <== archive が変換した"
                mismatched += 1
            print(f"  {path:<{width}}  zip={zip_eol:<5} blob={blob_eol:<5}{flag}")
        print(f"\n  -> archive が blob と違う形にしたファイル: {mismatched} 件")
        bad_sh = [p for p, z, _ in rows if p.endswith(".sh") and z != "LF"]
        print(f"  -> LF でない .sh (受け取った人の bash が落ちる形): {len(bad_sh)} 件 {bad_sh}")

        print("\n## 2. .gitattributes に export-ignore があるか")
        attrs = blob_bytes(".gitattributes").decode("utf-8")
        hits = [ln for ln in attrs.splitlines() if "export-ignore" in ln or "export-subst" in ln]
        print(f"  export-ignore / export-subst の行: {len(hits)} 件 {hits}")

        print("\n## 3. 展開 → git init → reset origin/main の直後の更新前検査")
        for autocrlf in ("true", "input", "false"):
            status, eol = simulate_setup(zip_path, tmp, autocrlf)
            verdict = "クリーン (更新できる)" if not status.strip() else "変更あり (更新拒否)"
            print(f"\n  [core.autocrlf={autocrlf}] {verdict}")
            if status.strip():
                for line in status.splitlines()[:20]:
                    print(f"      {line}")
            for line in eol.splitlines():
                print(f"      {line}")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
