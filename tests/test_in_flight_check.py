"""in_flight 台帳の関所 (scripts/check_in_flight.py) の回帰テスト。

二つの役割:
1. 実台帳が器に収まっていることをフルスイートで常時検査する
   (関所の手動実行を忘れた変更もここで赤くなる)。
2. 関所自身の検出ロジック (字数・過去形マーカー・表構造・経過措置、--base の
   移送検査) を固定する。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_in_flight  # noqa: E402
from check_in_flight import (  # noqa: E402
    GRANDFATHERED,
    LEDGER,
    _fingerprint,
    check,
    check_carryover,
    main,
)

SPLIT_RE = re.compile(r"(?<!\\)\|")

HEADER = "| 状態 | 案件 | 次アクション | 誰待ち | doc / issue | 更新 |"
SEP = "|---|---|---|---|---|---|"
OK_ROW = "| 🔵 設計中 | 正常行 | 現在地。次 = 一手。 | まはー | (none) | x |"


def make_ledger(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "ledger.md"
    p.write_text(f"## 台帳\n\n{body}\n", encoding="utf-8")
    return p


def row(action: str, name: str = "案件X") -> str:
    return f"| 🔵 設計中 | {name} | {action} | 私 | (none) | x |"


def table(*rows: str) -> str:
    return "\n".join([HEADER, SEP, *rows])


class TestRealLedger:
    def test_real_ledger_within_vessel(self):
        violations, _warnings = check(LEDGER)
        assert violations == []

    def test_grandfathered_rows_still_pinned(self):
        """経過措置の警告は名簿の行数以下 (行が器に合えば自然に減る)。"""
        _violations, warnings = check(LEDGER)
        assert len(warnings) <= len(GRANDFATHERED)


class TestActionCellRules:
    def test_normal_row_passes(self, tmp_path):
        v, w = check(make_ledger(tmp_path, table(OK_ROW)))
        assert v == [] and w == []

    def test_char_limit_is_hard(self, tmp_path):
        v, _ = check(make_ledger(tmp_path, table(row("あ" * 301))))
        assert len(v) == 1 and "301 字" in v[0]

    def test_char_limit_boundary_300_passes(self, tmp_path):
        v, _ = check(make_ledger(tmp_path, table(row("あ" * 300))))
        assert v == []

    @pytest.mark.parametrize(
        "text",
        [
            "2026-08-05 に完了。",
            "2026/8/4 に完了。",
            "2026年に裁定。",
            "令和8年に裁定。",
        ],
    )
    def test_date_markers(self, tmp_path, text):
        v, _ = check(make_ledger(tmp_path, table(row(text + "次 = 検証。"))))
        assert len(v) == 1 and "日付" in v[0]

    @pytest.mark.parametrize(
        "text",
        [
            "コミット `aeacb8e` で修正。",  # バッククォート
            "コミット aeacb8e で修正。",  # 裸 (英字+数字)
            "コミット 5291099 で修正。",  # 裸 (数字のみ、実在の短縮ハッシュ形)
            "コミットABCDEF1を反映。",  # 日本語隣接 + 大文字
        ],
    )
    def test_hash_markers(self, tmp_path, text):
        v, _ = check(make_ledger(tmp_path, table(row(text))))
        assert len(v) == 1 and "コミットハッシュ" in v[0]

    def test_letters_only_hex_word_not_flagged(self, tmp_path):
        # 英字のみの hex 単語 (deadbee 等) は通常語との衝突を避けるため対象外 (設計判断)
        v, _ = check(make_ledger(tmp_path, table(row("deadbee を検討。次 = 実装。"))))
        assert v == []


class TestTableStructure:
    def test_header_only(self, tmp_path):
        v, _ = check(make_ledger(tmp_path, HEADER))
        assert len(v) == 1 and "区切り行がない" in v[0]

    def test_empty_table(self, tmp_path):
        v, _ = check(make_ledger(tmp_path, f"{HEADER}\n{SEP}"))
        assert len(v) == 1 and "データ行が1行もない" in v[0]

    def test_blank_between_header_and_separator(self, tmp_path):
        v, _ = check(make_ledger(tmp_path, f"{HEADER}\n\n{SEP}\n{OK_ROW}"))
        assert len(v) == 1 and "描画されない" in v[0]

    def test_reordered_header_fails_loudly(self, tmp_path):
        bad = "| 状態 | 案件 | 誰待ち | 次アクション | doc / issue | 更新 |"
        v, _ = check(make_ledger(tmp_path, f"{bad}\n{SEP}\n{OK_ROW}"))
        assert len(v) == 1 and "ヘッダが想定と不一致" in v[0]

    def test_row_after_table_end(self, tmp_path):
        v, _ = check(make_ledger(tmp_path, f"{table(OK_ROW)}\n\n{OK_ROW}"))
        assert len(v) == 1 and "途切れた後" in v[0]

    def test_indented_or_pipeless_variant_rejected(self, tmp_path):
        variant = "  x | 忍び表 | 2026-08-05 | 私 | (none) | x"
        v, _ = check(make_ledger(tmp_path, f"{table(OK_ROW)}\n\n{variant}"))
        assert len(v) == 1 and "正規形" in v[0]

    def test_comment_prefix_does_not_bypass_after_blank(self, tmp_path):
        sneaky = "<!-- -->🟣 | 変形 | 2026-08-05 | 私 | (none) | x |"
        v, _ = check(make_ledger(tmp_path, f"{table(OK_ROW)}\n\n{sneaky}"))
        assert len(v) == 1

    def test_comment_prefix_does_not_bypass_inside_table(self, tmp_path):
        # 空行なし = 表の中の分岐。終端扱いで素通しする退行を殺す
        sneaky = "<!-- -->🟣 | 変形 | 2026-08-05 | 私 | (none) | x |"
        v, _ = check(make_ledger(tmp_path, f"{table(OK_ROW)}\n{sneaky}"))
        assert len(v) == 1 and "先頭 |" in v[0]

    def test_full_comment_line_is_fine(self, tmp_path):
        v, w = check(make_ledger(tmp_path, f"{table(OK_ROW)}\n\n<!-- 台帳外のメモ -->"))
        assert v == [] and w == []

    def test_mid_table_header_reappearance(self, tmp_path):
        v, _ = check(make_ledger(tmp_path, f"{table(OK_ROW)}\n{HEADER}"))
        assert len(v) == 1 and "ヘッダ行が表の途中" in v[0]

    def test_empty_state_cell_not_skipped(self, tmp_path):
        bad = f"|  | 空状態 | {'あ' * 301} | 私 | (none) | x |"
        v, _ = check(make_ledger(tmp_path, f"{table(OK_ROW)}\n{bad}"))
        assert len(v) == 1 and "セルが空" in v[0]


class TestGrandfathering:
    def _real_lines(self):
        return LEDGER.read_text(encoding="utf-8").split("\n")

    def _find_row(self, lines, name):
        for i, line in enumerate(lines):
            if not line.startswith("|"):
                continue
            cols = SPLIT_RE.split(line)
            if len(cols) == 8 and cols[2].strip() == name:
                return i
        return None

    def _find_exempt_row(self, lines, name):
        """免除がまだ生きている行の位置。生きていなければ skip する。

        免除が役目を終える形は 2 つある: 行が台帳から消える、または**行が残った
        まま器に収まって指紋が変わる**（`check_in_flight` のコメントいわく
        「該当セッションが行を器に合わせた時点で消えるのが正常系」＝名簿の手動
        解除は不要）。後者を skip 条件に入れていなかったため、時間割の行が器に
        収まった時点で経過措置のテストが落ちていた（2026-08-05 に修正）。
        """
        idx = self._find_row(lines, name)
        if idx is None:
            pytest.skip("経過措置行が台帳から消えている (免除が役目を終えた)")
        if GRANDFATHERED.get(name) != _fingerprint(lines[idx]):
            pytest.skip("経過措置行が器に収まって免除が失効している (正常系)")
        return idx

    @pytest.mark.parametrize("name", sorted(GRANDFATHERED))
    def test_any_column_edit_expires_exemption(self, tmp_path, name):
        """免除は行全体の指紋 — 次アクション欄以外の列の変更でも失効する。"""
        lines = self._real_lines()
        idx = self._find_exempt_row(lines, name)
        cols = SPLIT_RE.split(lines[idx])
        cols[6] = " 9999-12-31 "
        lines[idx] = "|".join(cols)
        p = tmp_path / "ledger.md"
        p.write_text("\n".join(lines), encoding="utf-8")
        v, _ = check(p)
        assert any(name in x for x in v)

    @pytest.mark.parametrize("name", sorted(GRANDFATHERED))
    def test_duplicated_exempt_row_rejected(self, tmp_path, name):
        lines = self._real_lines()
        idx = self._find_exempt_row(lines, name)
        lines.insert(idx + 1, lines[idx])
        p = tmp_path / "ledger.md"
        p.write_text("\n".join(lines), encoding="utf-8")
        v, _ = check(p)
        assert any("1 行限り" in x and name in x for x in v)

    @pytest.mark.parametrize("name", sorted(GRANDFATHERED))
    def test_exempt_row_currently_warns_not_fails(self, name):
        """免除中の行は (免除が生きている間) 警告として表面化し、違反にはならない。"""
        v, w = check(LEDGER)
        self._find_exempt_row(self._real_lines(), name)
        assert any(name in x for x in w)
        # 名前どおり「違反にはならない」側も検査する — 警告だけを見ていると、
        # 免除が違反側へ漏れても緑のまま通る
        assert not any(name in x for x in v)


class TestCarryover:
    """移送検査 (--base): 次アクションを差し替えた行・消した行のリンク先 doc が
    同じ差分で変更されているかだけを見る (文面の一致は見ない)。"""

    DOC = "docs/intent/case_a.md"
    ROW_A = "| 🟣 検証待ち | 案件A | 現在地 A。次 = 一手 A。 | まはー | [intent](../intent/case_a.md) | x |"
    ROW_NOLINK = "| 🔵 設計中 | 案件N | 現在地 N。次 = 一手 N。 | 私 | (none) | x |"

    @staticmethod
    def _git(root: Path, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(root), *args], check=True, capture_output=True
        )

    def _write(self, root: Path, rel: str, text: str) -> None:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def _ledger(self, *rows: str) -> str:
        return f"## 台帳\n\n{table(*rows)}\n"

    @pytest.fixture
    def repo(self, tmp_path):
        """基準コミット (ブランチ base) に台帳と案件 A の doc を置いた repo。"""
        root = tmp_path / "repo"
        root.mkdir()
        self._git(root, "init", "-q", "-b", "base")
        self._git(root, "config", "user.email", "t@example.com")
        self._git(root, "config", "user.name", "t")
        self._git(root, "config", "commit.gpgsign", "false")
        self._write(root, check_in_flight.LEDGER_REL, self._ledger(self.ROW_A, self.ROW_NOLINK))
        self._write(root, self.DOC, "# 案件A\n\n## 経緯\n")
        self._git(root, "add", "-A")
        self._git(root, "commit", "-q", "-m", "base")
        self._git(root, "switch", "-q", "-c", "work")
        return root

    def _set_ledger(self, root: Path, *rows: str) -> None:
        self._write(root, check_in_flight.LEDGER_REL, self._ledger(*rows))

    def test_changed_row_with_doc_untouched_fails(self, repo):
        self._set_ledger(repo, self.ROW_A.replace("一手 A", "新しい一手"), self.ROW_NOLINK)
        v, w = check_carryover("base", repo)
        assert len(v) == 1 and "案件A" in v[0] and self.DOC in v[0] and w == []

    def test_changed_row_with_doc_touched_passes(self, repo):
        self._set_ledger(repo, self.ROW_A.replace("一手 A", "新しい一手"), self.ROW_NOLINK)
        self._write(repo, self.DOC, "# 案件A\n\n## 経緯\n\n- 旧: 一手 A (要約)\n")
        assert check_carryover("base", repo) == ([], [])

    def test_committed_changes_are_in_the_diff(self, repo):
        """差分は merge-base から作業ツリーまで — コミット済みの変更も数える。"""
        self._set_ledger(repo, self.ROW_A.replace("一手 A", "新しい一手"), self.ROW_NOLINK)
        self._git(repo, "commit", "-qam", "ledger only")
        v, _ = check_carryover("base", repo)
        assert len(v) == 1
        self._write(repo, self.DOC, "# 案件A\n\n## 経緯\n\n- 旧: 一手 A\n")
        self._git(repo, "commit", "-qam", "doc")
        assert check_carryover("base", repo) == ([], [])

    def test_other_columns_changed_need_no_carryover(self, repo):
        self._set_ledger(repo, self.ROW_A.replace("| x |", "| y |"), self.ROW_NOLINK)
        assert check_carryover("base", repo) == ([], [])

    def test_removed_row_with_doc_untouched_fails(self, repo):
        self._set_ledger(repo, self.ROW_NOLINK)
        v, _ = check_carryover("base", repo)
        assert len(v) == 1 and "案件A" in v[0] and "消えた" in v[0]

    def test_removed_row_with_doc_touched_passes(self, repo):
        self._set_ledger(repo, self.ROW_NOLINK)
        self._write(repo, self.DOC, "# 案件A\n\nステータス: 完了\n\n## 経緯\n")
        assert check_carryover("base", repo) == ([], [])

    def test_removed_row_whose_doc_moved_to_archive_passes(self, repo):
        """完了で issue を archive/ へ移すと、旧リンクのパスは削除として差分に出る。"""
        self._set_ledger(repo, self.ROW_NOLINK)
        self._git(repo, "mv", self.DOC, "docs/intent/archive_case_a.md")
        assert check_carryover("base", repo) == ([], [])

    def test_added_row_passes(self, repo):
        new = "| 🔵 設計中 | 案件B | 現在地 B。次 = 一手 B。 | 私 | [intent](../intent/case_b.md) | x |"
        self._set_ledger(repo, self.ROW_A, self.ROW_NOLINK, new)
        assert check_carryover("base", repo) == ([], [])

    def test_untracked_new_doc_counts_as_touched(self, repo):
        row = self.ROW_A.replace("一手 A", "新しい一手").replace(
            "[intent](../intent/case_a.md)",
            "[intent](../intent/case_a.md) / [issue](../issues/new_issue.md)",
        )
        self._set_ledger(repo, row, self.ROW_NOLINK)
        self._write(repo, "docs/issues/new_issue.md", "# 新 issue\n\n## 経緯\n\n- 旧: 一手 A\n")
        assert check_carryover("base", repo) == ([], [])

    @pytest.mark.parametrize(
        "doc_cell",
        [
            "(none)",
            "[外部](https://example.com/x.md)",
            "[存在しない](../intent/missing.md)",
            "[repo 外](../../../outside.md)",
        ],
    )
    def test_unresolvable_link_is_warning(self, repo, doc_cell):
        base_row = f"| 🔵 設計中 | 案件U | 現在地 U。次 = 一手 U。 | 私 | {doc_cell} | x |"
        self._set_ledger(repo, self.ROW_A, self.ROW_NOLINK, base_row)
        self._git(repo, "commit", "-qam", "add U")
        self._git(repo, "branch", "-f", "base", "HEAD")
        self._set_ledger(repo, self.ROW_A, self.ROW_NOLINK, base_row.replace("一手 U", "新しい一手"))
        v, w = check_carryover("base", repo)
        assert v == [] and len(w) == 1 and "案件U" in w[0]

    def test_link_with_anchor_resolves(self, repo):
        row = self.ROW_A.replace("(../intent/case_a.md)", "(../intent/case_a.md#経緯)")
        self._set_ledger(repo, row.replace("一手 A", "新しい一手"), self.ROW_NOLINK)
        v, _ = check_carryover("base", repo)
        assert len(v) == 1 and self.DOC in v[0]

    def test_unknown_base_fails_closed(self, repo):
        v, w = check_carryover("no-such-ref", repo)
        assert len(v) == 1 and "no-such-ref" in v[0] and w == []

    def test_main_base_flag_exit_codes(self, monkeypatch, repo, capsys):
        monkeypatch.setattr(check_in_flight, "REPO_ROOT", repo)
        monkeypatch.setattr(check_in_flight, "LEDGER", repo / check_in_flight.LEDGER_REL)
        self._set_ledger(repo, self.ROW_A.replace("一手 A", "新しい一手"), self.ROW_NOLINK)
        assert main([]) == 0  # opt-in: --base 無しでは移送を見ない
        assert main(["--base", "base"]) == 1
        assert "移送検査で違反" in capsys.readouterr().out
        self._write(repo, self.DOC, "# 案件A\n\n## 経緯\n\n- 旧: 一手 A\n")
        assert main(["--base", "base"]) == 0


class TestMainExitCode:
    """関所の CLI 契約: exit 0 = 違反なし (警告のみ可) / exit 1 = 違反あり。"""

    def _run_main(self, monkeypatch, tmp_path, body):
        p = make_ledger(tmp_path, body)
        monkeypatch.setattr(check_in_flight, "LEDGER", p)
        return main()

    def test_clean_ledger_exits_zero(self, monkeypatch, tmp_path, capsys):
        assert self._run_main(monkeypatch, tmp_path, table(OK_ROW)) == 0
        capsys.readouterr()

    def test_violation_exits_one(self, monkeypatch, tmp_path, capsys):
        assert self._run_main(monkeypatch, tmp_path, table(row("2026-08-05 の記録。"))) == 1
        capsys.readouterr()

    def test_real_ledger_main_exits_zero(self, monkeypatch, tmp_path, capsys):
        """実台帳は経過措置の警告があっても exit 0 (警告のみは通す契約)。"""
        assert main() == 0
        out = capsys.readouterr().out
        assert "収まっています" in out
