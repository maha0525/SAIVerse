"""in_flight 台帳の関所: 次アクション欄が「器」に収まっているかを検査する。

台帳 (docs/overview/in_flight.md) の次アクション欄に書けるのは
「現在地 1 文 + 次の一手 1〜2 文」だけ (上限 300 字)。経緯の堆積は
各案件 doc の「経緯」節が受け持つ。この検査は器からの逸脱 —
字数超過と過去形マーカー (日付・コミットハッシュ) の混入 — を検出する。

検出パターンはこの台帳の実際の記法 (ISO 日付・git ハッシュ、大文字小文字不問) を
狙った backstop であり、日付・ハッシュ文法の完全実装は目的にしない。同様に
台帳の表は「外側パイプ付きの正規形 1 つ」だけを正とする — GFM が許す変形
(外側パイプ省略・先頭空白の表) は解析せず、構造違反として落とす。

移送検査 (--base、opt-in): 台帳の行の次アクションを差し替えたら、押し出した旧文面を
同じコミットでその行がリンクする doc の「経緯」節へ移す、という規則 (CLAUDE.md の
Progress Tracking) の機械検査。移送は逐語でなく要約でもよいので文面の一致は見ない。
見るのは「次アクションが変わった行・消えた行について、リンク先の doc も同じ差分で
変更されているか」だけ。差分は基準 ref と HEAD の merge-base から作業ツリーまで
(未追跡ファイルを含む) — 基準 ref 側が先へ進んでいても、その変更をこちらの差分と
取り違えない。行は案件名で同定するので、案件名を変えると「旧名の行が消えた」扱いに
なる。新しく足した行は移送不要。リンクを 1 つも解決できない行 (リンク無し・外部 URL・
repo の外・実在しないパス) は警告に留める。リンクが複数あれば、どれか 1 つが変更されて
いれば通す。

終了コードの契約: exit 0 = 違反なし (経過措置・リンク解決不能の警告のみは通す) /
exit 1 = 免除外の違反、表構造の不正、移送漏れ、または --base の基準 ref との差分を取れない。

使い方: python scripts/check_in_flight.py
        python scripts/check_in_flight.py --base              (基準 = origin/develop)
        python scripts/check_in_flight.py --base origin/main
"""
from __future__ import annotations

import argparse
import hashlib
import posixpath
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

REPO_ROOT = Path(__file__).resolve().parent.parent
LEDGER_REL = "docs/overview/in_flight.md"
LEDGER = REPO_ROOT / LEDGER_REL
DEFAULT_BASE = "origin/develop"

MAX_CELL_CHARS = 300
# 日付: 2026-08 / 2026/08 / 2026/8 / 2026年 / 令和8年
DATE_RE = re.compile(r"20\d{2}(?:[-/]\d{1,2}|年)|令和\d+年")
# コミットハッシュ: バッククォート付き 7〜40 桁 hex、または裸の 7〜40 桁 hex。
# 裸は「数字を含む」ものだけ (英字のみの hex 単語は通常の語と衝突しうるので除外)。
# 数字のみも検出する — この repo の実短縮ハッシュに数字のみが実在する (65件) ため。
# 7 桁以上の生の数値 (件数等) が誤検出されたら、それはセルに書く数字ではないので
# 言い換える側で対応する (関所は fail-closed に倒す)。
# 境界に \b を使うと日本語文字も word character 扱いで「コミットABCDEF1を」型を
# 見逃すため、境界は「前後が ASCII 英数字でない」で判定する
COMMIT_HASH_RE = re.compile(
    r"`[0-9a-fA-F]{7,40}`"
    r"|(?<![0-9A-Za-z])(?=[0-9a-fA-F]*\d)[0-9a-fA-F]{7,40}(?![0-9A-Za-z])"
)
ACTION_COLUMN = 3  # | 状態 | 案件 | 次アクション | 誰待ち | doc / issue | 更新 |
HEADER_CELLS = ["状態", "案件", "次アクション", "誰待ち", "doc / issue", "更新"]

# 経過措置 (2026-08-04 の台帳解体時点で進行中セッションの縄張りだったため未移送)。
# 免除は解体時点の「行全体」の指紋 (sha1 先頭 12 桁) に固定してある — どの列でも
# 一文字書き換えると指紋が合わなくなり自動失効して本検査に昇格する。同名行の複製も
# 出現 1 回限りの検査で失格になる。名簿の手動解除は不要 (該当セッションが行を器に
# 合わせた時点で消えるのが正常系)。
GRANDFATHERED = {
    "経験の台帳と経験値ノート (再開の想起の本体)": "c4a93b3ed09f",
    "RSS フィード施設 (偶然の供給側)": "54d50c0e314e",
    "時間割の抜本改修 (習慣×偶然)": "b4e13ab57dbb",
}

# markdown のエスケープ済み縦棒 \| は列区切りにしない
SPLIT_RE = re.compile(r"(?<!\\)\|")
SEP_CELL_RE = re.compile(r"^:?-+:?$")
# 免除するコメントは「行全体がコメント」の場合だけ。--> の後ろに可視文字が残る行を
# 免除すると <!-- -->表行 の形で検査を回避できる
FULL_COMMENT_RE = re.compile(r"<!--.*?-->")


def _is_full_comment(stripped: str) -> bool:
    return bool(stripped) and FULL_COMMENT_RE.fullmatch(stripped) is not None


def _fingerprint(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def check(path: Path | None = None) -> tuple[list[str], list[str]]:
    """(違反, 経過措置の警告) を返す。path 省略時は呼び出し時点の LEDGER を読む。"""
    if path is None:
        path = LEDGER
    violations: list[str] = []
    warnings: list[str] = []
    in_table = False
    header_seen = False
    separator_seen = False
    data_seen = False
    table_done = False
    grandfathered_hits: dict[str, int] = {}
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not in_table:
            if line.startswith("## 台帳"):
                in_table = True
            continue
        if table_done:
            # 表が途切れた後の表行・表らしき行 (先頭空白つき・外側パイプ省略の GFM
            # 変形を含む) は黙って通さない — 台帳の表は外側パイプ付き正規形が仕様
            stripped = line.strip()
            if stripped and not _is_full_comment(stripped) and SPLIT_RE.search(line):
                violations.append(
                    f"L{lineno}: 表が途切れた後に表らしき行がある (台帳の表は 1 つ・正規形のみ)"
                )
            continue
        if line.startswith("#"):
            table_done = True  # 次の見出しで表領域は終わる
            continue
        if not line.startswith("|"):
            if not header_seen:
                # 表の手前の空行・説明は許すが、縦棒を含む行 (外側パイプ省略の表など)
                # はヘッダより先に表が始まった可能性 — 正規形違反として落とす
                if SPLIT_RE.search(line) and not _is_full_comment(line.strip()):
                    violations.append(
                        f"L{lineno}: ヘッダより前に縦棒を含む行がある (台帳の表は外側パイプ付き正規形のみ)"
                    )
                continue
            if not separator_seen:
                # GFM ではヘッダの直後の行が区切り行でなければ表として描画されない
                violations.append(
                    f"L{lineno}: ヘッダと区切り行の間に別の行が挟まっている (表が描画されない)"
                )
                return violations, warnings
            if not line.strip() or _is_full_comment(line.strip()):
                table_done = True  # 空行・行全体コメントは表の終端
                continue
            if SPLIT_RE.search(line):
                # 表行らしき行 (縦棒を含む) が先頭 | で始まっていないと検査から漏れる
                violations.append(
                    f"L{lineno}: 縦棒を含む行が先頭 | で始まっていない (表行なら検査から漏れる)"
                )
            else:
                table_done = True  # 散文は表の終端
            continue
        cols = SPLIT_RE.split(line)
        cells = [c.strip() for c in cols[1:-1]]
        is_separator = bool(cells) and all(SEP_CELL_RE.match(c) for c in cells)
        if not header_seen:
            # 表の先頭行で列の並びを検証する。並びが変わったら黙って誤読せず失敗
            if cells != HEADER_CELLS:
                violations.append(
                    f"L{lineno}: 表のヘッダが想定と不一致 {cells}。"
                    f"列を変えた場合はこの検査 (ACTION_COLUMN) も同じ変更で直すこと"
                )
                return violations, warnings
            header_seen = True
            continue
        if not separator_seen:
            # ヘッダ直後は正しい列数の区切り行でなければ表として描画されない
            if is_separator and len(cols) == len(HEADER_CELLS) + 2:
                separator_seen = True
                continue
            violations.append(
                f"L{lineno}: ヘッダ直後に正しい列数の区切り行がない (表が描画されない)"
            )
            return violations, warnings
        if is_separator:
            violations.append(f"L{lineno}: 区切り行が表の途中に再出現")
            continue
        if cells == HEADER_CELLS:
            violations.append(f"L{lineno}: ヘッダ行が表の途中に再出現")
            continue
        data_seen = True
        if len(cols) != len(HEADER_CELLS) + 2:
            violations.append(f"L{lineno}: 列数がヘッダと不一致 ({len(cols) - 2} 列)")
            continue
        name = cells[1]
        if not cells[0] or not name:
            violations.append(f"L{lineno}: 状態または案件セルが空")
            continue
        cell = cells[ACTION_COLUMN - 1]
        if name in GRANDFATHERED:
            grandfathered_hits[name] = grandfathered_hits.get(name, 0) + 1
        problems: list[str] = []
        if len(cell) > MAX_CELL_CHARS:
            problems.append(f"{len(cell)} 字 (上限 {MAX_CELL_CHARS} 字)")
        for pattern, label in ((DATE_RE, "日付"), (COMMIT_HASH_RE, "コミットハッシュ")):
            found = pattern.findall(cell)
            if found:
                problems.append(f"{label}の混入 {found[:3]}")
        if not problems:
            continue
        detail = f"L{lineno} [{name}]: " + " / ".join(problems)
        if GRANDFATHERED.get(name) == _fingerprint(line):
            warnings.append(detail)
        else:
            violations.append(
                detail + "。経緯は doc の「経緯」節へ移送して、セルは現在地+次の一手だけに"
            )
    if not header_seen:
        violations.append("台帳の表が見つからない (「## 台帳」節とヘッダ行を確認)")
    elif not separator_seen:
        violations.append("ヘッダの後に区切り行がないまま表が終わっている")
    elif not data_seen:
        violations.append("台帳にデータ行が1行もない (全案件が消えていないか確認)")
    for name, count in grandfathered_hits.items():
        # 免除行の複製・再挿入で経過措置を使い回させない
        if count > 1:
            violations.append(f"経過措置の案件 [{name}] が {count} 行に出現 (1 行限り)")
    return violations, warnings


# ---- 移送検査 (--base) ----

DOC_COLUMN = 5
# [ラベル](パス) のパス部分。ラベル内の空白や「§」は問わない
LINK_RE = re.compile(r"\]\(([^)\s]+)\)")


def _ledger_rows(text: str) -> dict[str, tuple[int, list[str]]]:
    """台帳の表のデータ行を {案件名: (行番号, セル)} で返す (同名は最初の行)。

    表構造の検査は check() の役目なので、ここは正規形のデータ行を拾うだけ。
    """
    rows: dict[str, tuple[int, list[str]]] = {}
    in_table = False
    for lineno, line in enumerate(text.splitlines(), 1):
        if not in_table:
            in_table = line.startswith("## 台帳")
            continue
        if line.startswith("#"):
            break
        if not line.startswith("|"):
            continue
        cols = SPLIT_RE.split(line)
        if len(cols) != len(HEADER_CELLS) + 2:
            continue
        cells = [c.strip() for c in cols[1:-1]]
        if cells == HEADER_CELLS or all(SEP_CELL_RE.match(c) for c in cells):
            continue
        if cells[1]:
            rows.setdefault(cells[1], (lineno, cells))
    return rows


def _link_paths(cell: str) -> list[str]:
    """doc / issue セルのリンクを repo 相対パスへ解決する。解決できないものは落とす。

    リンクは台帳の置き場 (docs/overview/) からの相対。外部 URL・アンカーだけのリンク・
    repo の外へ出るパスは解決不能として扱う。ファイルの実在はここでは見ない。
    """
    base_dir = posixpath.dirname(LEDGER_REL)
    paths: list[str] = []
    for target in LINK_RE.findall(cell):
        target = unquote(target.split("#", 1)[0])
        if not target or "://" in target or target.startswith(("mailto:", "/")):
            continue
        resolved = posixpath.normpath(posixpath.join(base_dir, target))
        if resolved == ".." or resolved.startswith("../"):
            continue
        if resolved not in paths:
            paths.append(resolved)
    return paths


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "core.quotepath=off", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} が失敗")
    return result.stdout


def _nul_split(output: str) -> set[str]:
    return {p for p in output.split("\0") if p}


def check_carryover(base: str = DEFAULT_BASE, root: Path | None = None) -> tuple[list[str], list[str]]:
    """(移送漏れの違反, リンク解決不能の警告) を返す。

    基準 ref を解決できない・git が使えない場合は違反 1 件として返す (fail-closed —
    検査したつもりで素通しさせない)。
    """
    if root is None:
        root = REPO_ROOT
    try:
        merge_base = _git(root, "merge-base", base, "HEAD").strip()
        # --no-renames: 移動 (issue の archive 行きなど) は旧パスの削除 + 新パスの追加として
        # 両方の名前が並ぶので、旧リンクのままの行も「変更された」と判定できる
        touched = _nul_split(_git(root, "diff", "--name-only", "--no-renames", "-z", merge_base))
        touched |= _nul_split(_git(root, "ls-files", "--others", "--exclude-standard", "-z"))
    except (RuntimeError, OSError) as exc:
        return [f"移送検査の基準 [{base}] との差分を取れない: {exc}"], []
    try:
        base_text = _git(root, "show", f"{merge_base}:{LEDGER_REL}")
    except RuntimeError:
        return [], []  # 基準に台帳が無い = 押し出された旧文面も無い
    ledger = root / LEDGER_REL
    current_text = ledger.read_text(encoding="utf-8") if ledger.is_file() else ""

    current_rows = _ledger_rows(current_text)
    action = ACTION_COLUMN - 1
    doc = DOC_COLUMN - 1
    violations: list[str] = []
    warnings: list[str] = []
    for name, (base_lineno, base_cells) in _ledger_rows(base_text).items():
        current = current_rows.get(name)
        if current is None:
            what = f"基準 L{base_lineno} [{name}]: 行が台帳から消えた"
            doc_cells = [base_cells[doc]]
        elif current[1][action] != base_cells[action]:
            what = f"L{current[0]} [{name}]: 次アクションが差し替わった"
            doc_cells = [current[1][doc], base_cells[doc]]
        else:
            continue
        candidates: list[str] = []
        for cell in doc_cells:
            candidates += [p for p in _link_paths(cell) if p not in candidates]
        if any(p in touched for p in candidates):
            continue
        existing = [p for p in candidates if (root / p).is_file()]
        if existing:
            violations.append(
                f"{what}のに、リンク先の doc が同じ差分で変更されていない {existing}。"
                f"押し出した旧文面を doc の「経緯」節へ移送すること (要約でよい)"
            )
        else:
            warnings.append(f"{what}が、リンク先の doc を解決できず未検査 (doc / issue 列: {doc_cells[0]})")
    return violations, warnings


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="in_flight 台帳の関所")
    parser.add_argument(
        "--base",
        nargs="?",
        const=DEFAULT_BASE,
        default=None,
        metavar="REF",
        help="移送検査も行う。REF との差分で、次アクションを差し替えた行・消した行の"
        f"リンク先 doc が変更されているかを見る (REF 省略時は {DEFAULT_BASE})",
    )
    args = parser.parse_args([] if argv is None else argv)
    violations, warnings = check()
    for w in warnings:
        print(f"経過措置 (2026-08-04 解体時の行のまま未移送。どの列でも書き換えたら本検査に昇格): {w}")
    carry_violations: list[str] = []
    if args.base is not None:
        carry_violations, carry_warnings = check_carryover(args.base)
        for w in carry_warnings:
            print(f"移送検査の警告: {w}")
    if violations:
        print(f"in_flight 台帳の器からの逸脱 {len(violations)} 件:")
        for v in violations:
            print(f"  - {v}")
    if carry_violations:
        print(f"in_flight 台帳の移送検査で違反 {len(carry_violations)} 件 (基準 {args.base}):")
        for v in carry_violations:
            print(f"  - {v}")
    if violations or carry_violations:
        return 1
    print("in_flight 台帳: 全行が器に収まっています (経過措置を除く)")
    if args.base is not None:
        print(f"移送検査: 次アクションを差し替えた行・消した行のリンク先 doc は全て変更されています (基準 {args.base})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
