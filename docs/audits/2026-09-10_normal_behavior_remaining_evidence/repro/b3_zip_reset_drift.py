"""配布 ZIP の版が main より古いとき、setup 直後の作業ツリーが汚れるかを実測する。

FLOW-27 / FLOW-32 の同じ境界の、改行ではない方の変数。

`b3_dist_line_endings.py` で「改行は原因にならない」ことは確定した
(ZIP の .sh は LF、.bat は CRLF、setup 直後の `git status` は空)。
ただしそれは **配布 ZIP の版と `origin/main` が同じコミットのとき**の結果である。
2026-09-10 時点では `v0.3.11` == `origin/main` なので、たまたま一致していた。

README.md:222 のダウンロードリンクは `releases/latest/download/SAIVerse.zip`、
すなわち**最新のリリース**を指す。一方 `setup.bat:262` / `setup.sh:109` は
`git reset origin/main`、すなわち**その時点の main の先端**に合わせる。
版を出した後に main が進めば、この 2 つは別のコミットになる。
`git reset` は mixed なので HEAD と index だけが main の先端へ動き、
作業ツリーは ZIP のまま (古い版) 残る。差分は全部「変更あり」に見える。

そうなると `scripts/update_engine.py:745-752` の更新前検査が
「Working tree has local changes」で更新を拒否する。

確かめること
------------
`v0.3.10` の ZIP を作り、`origin/main` (= `v0.3.11`) に対して
setup と同じ手順を踏んで、`git status --porcelain --untracked-files=no` に
何件出るかを数える。

実行方法
--------
    .venv/Scripts/python.exe docs/audits/2026-09-10_normal_behavior_remaining_evidence/repro/b3_zip_reset_drift.py

読み取り専用 (`git archive` のみ)。origin は本物の GitHub ではなく
ローカルの repo を指すのでネットワークを使わない。一時領域だけを使う。

観測結果 (2026-09-10 実行、v0.3.10 = 8612f216 / origin/main = e7d8e7d4)
-----------------------------------------------------------------------
- **版がずれていれば実際に汚れる**: `v0.3.10` の ZIP に対して
  `git reset origin/main` (= v0.3.11) を踏むと、
  `git status --porcelain --untracked-files=no` が **69 件** (M と D) を返した。
  `assert_git_update_ready` はこれを見て更新を拒否する。
  ただし拒否文は 20 件を挙げて「... and 49 more」と要約し、
  `git checkout -- .` を案内するので、利用者は自力で抜けられる
  (`update_engine.py:696-723`)。

- **ただしこの repo では、その「ずれ」がほぼ起きない。**
  `git log origin/main --first-parent` を v0.2.28〜v0.3.11 の 14 版まで遡って見たところ、
  **first-parent の全コミットにリリースタグが付いていた。**
  main は版を出すときにしか進まず、タグはその同じコミットに置かれる。
  つまり定常状態では `origin/main` == 最新リリースで、ZIP を入れた直後は汚れない
  (`b3_dist_line_endings.py` の §3 がその状態を実測している)。

- **ずれるのは、main が進んでからリリースが公開されるまでの間だけ。**
  GitHub の releases API で直近 5 版の `created_at` (タグ先のコミット時刻) と
  `published_at` (Release 公開時刻) を比べると、その差は
  v0.3.11 から順に **49 / 77 / 129 / 21 / 17 秒** (最短 17 秒・最長 129 秒) だった。
  利用者がこの 1〜2 分の窓の中で `setup` の `git fetch` を踏んだときだけ、
  上の 69 件の状態になりうる。**窓の中で実際に踏んだ事例は確認していない。**
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]

OLD_REF = "v0.3.10"  # 利用者が落とす「最新リリース」に相当
NEW_REF = "origin/main"  # setup が `git reset origin/main` で合わせる先


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


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="b3_drift_"))
    try:
        old = run(["git", "rev-parse", f"{OLD_REF}^{{commit}}"], cwd=REPO).stdout.strip()
        new = run(["git", "rev-parse", NEW_REF], cwd=REPO).stdout.strip()
        print(f"# 配布 ZIP の版 : {OLD_REF} ({old})")
        print(f"# setup の合わせ先: {NEW_REF} ({new})")
        if old == new:
            print("# 2 つが同じコミットなので、この検査は差を作れない。")
            return 0

        zip_path = tmp / "SAIVerse.zip"
        run(
            ["git", "archive", "--format=zip", "--prefix=SAIVerse/", "-o", str(zip_path), OLD_REF],
            cwd=REPO,
        )

        extract = tmp / "extract"
        extract.mkdir()
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extract)
        proj = extract / "SAIVerse"

        # setup.bat:257-264 / setup.sh:105-110 と同じ手順。
        run(["git", "init", "-q"], cwd=proj)
        run(["git", "config", "core.autocrlf", "true"], cwd=proj)  # Windows 既定
        run(["git", "remote", "add", "origin", REPO.as_uri()], cwd=proj)
        run(
            ["git", "fetch", "-q", "--depth", "1", "origin", f"{new}:refs/remotes/origin/main"],
            cwd=proj,
        )
        run(["git", "branch", "-M", "main"], cwd=proj)
        run(["git", "reset", "-q", "origin/main"], cwd=proj)

        # update_engine.py:745-752 が見る、まさにその検査。
        status = run(
            ["git", "status", "--porcelain", "--untracked-files=no"], cwd=proj
        ).stdout
        lines = [ln for ln in status.splitlines() if ln.strip()]
        print(f"\n## setup 直後の `git status --porcelain --untracked-files=no`: {len(lines)} 件")
        for line in lines[:15]:
            print(f"   {line}")
        if len(lines) > 15:
            print(f"   ... 他 {len(lines) - 15} 件")

        print()
        if lines:
            print("## 判定: 更新前検査は「Working tree has local changes」で更新を拒否する。")
            print("   版が main より古い ZIP を入れた利用者は、最初の update.bat で止まる。")
        else:
            print("## 判定: クリーン。版がずれていても更新は始められる。")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
