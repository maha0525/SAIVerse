"""in_flight 台帳の関所: 次アクション欄が「器」に収まっているかを検査する。

台帳 (docs/overview/in_flight.md) の次アクション欄に書けるのは
「現在地 1 文 + 次の一手 1〜2 文」だけ (上限 300 字)。経緯の堆積は
各案件 doc の「経緯」節が受け持つ。この検査は器からの逸脱 —
字数超過と過去形マーカー (日付・コミットハッシュ) の混入 — を検出する。

検出パターンはこの台帳の実際の記法 (ISO 日付・git ハッシュ、大文字小文字不問) を
狙った backstop であり、日付・ハッシュ文法の完全実装は目的にしない。同様に
台帳の表は「外側パイプ付きの正規形 1 つ」だけを正とする — GFM が許す変形
(外側パイプ省略・先頭空白の表) は解析せず、構造違反として落とす。

終了コードの契約: exit 0 = 違反なし (経過措置の警告のみは通す) /
exit 1 = 免除外の違反または表構造の不正。

使い方: python scripts/check_in_flight.py
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

LEDGER = Path(__file__).resolve().parent.parent / "docs" / "overview" / "in_flight.md"

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


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    violations, warnings = check()
    for w in warnings:
        print(f"経過措置 (2026-08-04 解体時の行のまま未移送。どの列でも書き換えたら本検査に昇格): {w}")
    if violations:
        print(f"in_flight 台帳の器からの逸脱 {len(violations)} 件:")
        for v in violations:
            print(f"  - {v}")
        return 1
    print("in_flight 台帳: 全行が器に収まっています (経過措置を除く)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
