"""⭐ Gemini に向ける構造化出力の型に数値の欄を置かない — 全経路の見張り。

出自: docs/issues/sluice_structured_output_digit_loop.md (2026-08-24)。

**なぜ数値欄が危ないのか**: JSON の数値リテラルは文法で閉じられない。桁を
いくら並べても文法違反にならないので、Gemini の制約付きデコード
(constrained decoding) が数値の途中でループに入ると、何も止められない。実際に
スルース (sea/sluice.py) の整数欄で本番 7 回連続の失敗が起き、毎回
``ValueError: Exceeds the limit (4300 digits) for integer string conversion:
value has 65113 digits`` — 65,000 桁の数字を吐いて SDK が落ちていた。隔離実験
では 5 モデル中 3 で同じ欄が壊れた。文字列・enum・真偽値は文法で閉じられる
(``"`` / 候補 / 二択で終わる) のでこの罠に入らない。

**この検査がやること**: 型が Gemini の制約付きデコードに届く 3 経路
(Playbook JSON / Python 側の組み立て / スペルの引数) を、その場でファイル
システムを走査して集め、数値欄を全部拾う。手で並べた一覧ではないので、新しい
Playbook や新しいスペルが増えたら、何もしなくても対象に入る。走査の実装は
tests/schema_scan.py にあり、走査できない範囲 (アドオン・利用者定義) も
そこに書いてある。

**既知の違反**: いま残っている数値欄は :data:`KNOWN_NUMERIC_FIELDS` に列挙
してある。型を変えるには「隔離実験で新しい型を先に確かめる」手順が要るので、
この検査を入れる作業とは別セッションの仕事として残っている。リストは両方向で
検算する — 載っていない数値欄が現れても落ちるし、載っているのに見つからなく
なっても落ちる (直したのに消し忘れた一覧は「守っているつもり」の飾りになる)。
"""
from __future__ import annotations

import unittest
from typing import Dict, List

from schema_scan import (  # tests/schema_scan.py
    Finding,
    numeric_fields,
    numeric_fields_in_python_source,
    scan_all,
)

#: いま残っている数値欄と、それを追っている場所。
#:
#: 型の変更は「隔離実験で 3 モデルに叩いて崩れないことを見てから」という手順を
#: 踏むので (issue の再現実験の節)、ここに載っているものはまだ直っていない。
#: 直したら **この行を消すこと** — 消し忘れは stale の検査が落として教える。
KNOWN_NUMERIC_FIELDS: Dict[str, str] = {
    # ── 経路 A: Playbook JSON の response_schema ────────────────────────
    # 追跡: docs/issues/sluice_structured_output_digit_loop.md /
    #       docs/handoff/2026-08-24_v3_live_verification_handoff.md
    #       (「数値欄を持つ他の構造化出力 4 Playbook」= 建物作成 / 文書検索 /
    #        予定管理 / Web 調査。スルースと同じ手順で隔離実験してから直す)
    "playbook:public/create_building_playbook.json#plan_building $.capacity":
        "建物作成の収容人数",
    "playbook:public/document_search_playbook.json#analyze_request $.start_line":
        "文書検索の開始行",
    "playbook:public/document_search_playbook.json#analyze_request $.end_line":
        "文書検索の終了行",
    "playbook:public/schedule_management_playbook.json#prepare_add $.days_of_week[]":
        "予定管理の曜日 (整数の配列)",
    "playbook:public/schedule_management_playbook.json#prepare_add $.interval_seconds":
        "予定管理の間隔",
    "playbook:public/schedule_management_playbook.json#prepare_delete $.schedule_id":
        "予定管理の対象 ID",
    "playbook:public/web_research_playbook.json#plan_query $.max_results":
        "Web 調査の件数",

    # ── 経路 B: Python 側で組み立てる型 ────────────────────────────────
    # 追跡: 同ハンドオフ (起床判断は v0.4 の配線前、夜間編纂は編纂の再開前に直す)
    "python:saiverse/judgment_points.py::_build_slot_schema $.budget_rounds":
        "判断点の時間割のコマの作業ラウンド予算",
    "python:sai_memory/curation_ops.py::plan_split $.sections[].block_indices[]":
        "夜間編纂のページ分割のブロック番号 (整数の配列)",

    # ── 経路 C: スペルの引数の型 (spell_args_decider が response_schema に使う) ──
    # 追跡: docs/issues/sluice_structured_output_digit_loop.md。
    # 2026-08-24 にこの検査を作る過程で見つかった経路で、issue の「手当て」には
    # まだ載っていない (隔離実験も未着手)。引数指定なしの形 (/spell name='X') で
    # 唱えられたときだけ構造化出力になる。
    "spell:document_read $.start_line": "文書読みの開始行",
    "spell:document_read $.end_line": "文書読みの終了行",
    "spell:document_read $.limit": "文書読みの行数",
    "spell:document_search $.context_lines": "文書検索の前後行数",
    "spell:document_search $.max_matches": "文書検索の最大ヒット数",
    "spell:game_create_building $.capacity": "ゲーム内建物の収容人数",
    "spell:memory_clip $.rounds": "記憶の切り出しの往復数",
    "spell:messagelog_get_around $.count": "ログ前後取得の件数",
    "spell:pocketbook_open $.limit": "手帳を開くときの件数",
    "spell:read_url_outline $.full_threshold": "URL 概要の全文しきい値",
    "spell:read_url_section $.around": "URL 部分読みの前後量",
    "spell:resolve_uri $.max_total_chars": "URI 解決の総文字数",
    "spell:send_email_to_user $.user_id": "メール送信の宛先ユーザー ID",
}

_WHY_IT_MATTERS = (
    "JSON の数値リテラルは文法で閉じられない (桁をいくら並べても文法違反に\n"
    "ならない) ので、Gemini の制約付きデコードが数値の途中でループに入ると\n"
    "何も止められません。本番のスルースでは 65,000 桁の数字が返り、SDK が\n"
    "`Exceeds the limit (4300 digits) for integer string conversion` で落ちて\n"
    "7 回連続で失敗しました。桁が短いときは静かに壊れます (存在しない番号として\n"
    "要素だけ捨てられ、成功扱いで先へ進む)。\n"
    "  直し方: 参照は文字列で受けて (`core:2` のように、プロンプトに載っている\n"
    "  語の写し) こちら側で番号へ解決する。個数や行番号は enum か文字列にする。\n"
    "  実例と隔離実験の結果: docs/issues/sluice_structured_output_digit_loop.md"
)


def _format(findings: List[Finding]) -> str:
    lines = []
    for finding in sorted(findings, key=lambda f: f.key):
        lines.append(
            f"  - [{finding.route}] {finding.location}\n"
            f"      {finding.source}  の  {finding.field_path}  が {finding.field_type} 型"
        )
    return "\n".join(lines)


class ResponseSchemaNumericFieldTest(unittest.TestCase):
    """3 経路を走査して、数値欄が増えていない / 既知の一覧が腐っていないことを見る。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.results = scan_all()
        cls.findings = [f for r in cls.results.values() for f in r.findings]
        cls.found_keys = {f.key for f in cls.findings}

    # -- 走査そのものが働いていること ------------------------------------

    def test_every_route_is_actually_scanned(self):
        """走査が空振りしていないこと。

        3 経路のどれかが 0 件になったら、それは「違反が無い」ではなく
        「見に行けていない」の可能性が高い (ディレクトリの移動・改名など)。
        走査の失敗は握り潰さず、ここで穴として落とす。

        いまは 3 経路とも :data:`KNOWN_NUMERIC_FIELDS` に既知の欄があるので、
        経路がまるごと見えなくなれば stale の検査も同時に落ちる (既知の一覧が
        経路ごとの生存確認を兼ねている)。ただし最後の既知の欄を直した経路では
        その効き目が消えるので、ここで別途数えておく。
        """
        for route, result in self.results.items():
            with self.subTest(route=route):
                self.assertEqual(
                    result.problems, [],
                    f"経路 {route} の走査自体が失敗しました。読めない対象がある間、"
                    f"この検査はその範囲を見張れていません:\n  "
                    + "\n  ".join(result.problems),
                )
                self.assertGreater(
                    result.scanned, 0,
                    f"経路 {route} で型が 1 つも見つかりませんでした。"
                    "対象の置き場が変わった可能性があります "
                    "(tests/schema_scan.py の走査先を直してください)。",
                )

    # -- 方向 1: 新しい数値欄が現れたら落ちる ----------------------------

    def test_no_new_numeric_field_reaches_gemini(self):
        """⭐ 既知の一覧に無い数値欄が現れたら落ちる。"""
        new = [f for f in self.findings if f.key not in KNOWN_NUMERIC_FIELDS]
        self.assertEqual(
            new, [],
            "\n\nGemini に向く型に、新しい数値の欄が入りました:\n"
            + _format(new)
            + "\n\n"
            + _WHY_IT_MATTERS
            + "\n\n  どうしても数値欄を残す判断をした場合は、"
            "tests/test_response_schema_no_numeric_fields.py の\n"
            "  KNOWN_NUMERIC_FIELDS に、追跡している issue と一緒に書き足して"
            "ください。\n",
        )

    # -- 方向 2: 一覧が腐ったら落ちる ------------------------------------

    def test_known_numeric_field_list_has_no_stale_entries(self):
        """⭐ 一覧にあるのに見つからない項目があったら落ちる。

        直したのに消し忘れた行 (あるいは対象ごと消えた行) を残すと、一覧は
        「守っているつもり」の飾りになる。片方向の検算しかない見張りは、
        時間が経つほど嘘に近づく。
        """
        stale = sorted(set(KNOWN_NUMERIC_FIELDS) - self.found_keys)
        self.assertEqual(
            stale, [],
            "\n\nKNOWN_NUMERIC_FIELDS に載っているのに、走査では見つかりません"
            "でした:\n  "
            + "\n  ".join(f"{key}  ({KNOWN_NUMERIC_FIELDS[key]})" for key in stale)
            + "\n\n  数値欄を直したのなら、この行を消してください (それがこの"
            "一覧の更新の仕方です)。\n"
            "  直した覚えが無いなら、対象のファイルが消えた・改名された可能性が"
            "あります。\n",
        )

    # -- 検出器そのものの自己検査 ----------------------------------------

    def test_walker_sees_numeric_fields_hidden_in_branches_and_arrays(self):
        """入れ子・配列・anyOf の分岐の中の数値欄も拾えること。

        検出器が浅くなっていると、上の 2 つは静かに緑になる。
        """
        schema = {
            "type": "object",
            "properties": {
                "reflection": {"type": "string"},
                "rows": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"memory_id": {"type": "integer"}},
                    },
                },
                "verdict": {
                    "anyOf": [
                        {"type": "object", "properties": {"skip": {"type": "boolean"}}},
                        {"type": "object", "properties": {"score": {"type": "number"}}},
                    ],
                },
                "tags": {"type": "array", "items": {"type": "integer"}},
                "maybe": {"type": ["integer", "null"]},
            },
        }
        self.assertEqual(
            sorted(numeric_fields(schema)),
            sorted([
                ("$.rows[].memory_id", "integer"),
                ("$.verdict.anyOf[1].score", "number"),
                ("$.tags[]", "integer"),
                ("$.maybe", "integer"),
            ]),
        )

    def test_python_source_walker_sees_the_same_shapes(self):
        """Python のソースを読む側 (経路 B) の検出器も同じ深さで見ること。"""
        source = (
            "schema = {\n"
            '    "type": "object",\n'
            '    "properties": {\n'
            '        "note": {"type": "string"},\n'
            '        "slots": {"type": "array", "items": {"type": "object",\n'
            '            "properties": {"budget": {"type": "integer"}}}},\n'
            '        "verdict": {"anyOf": [{"type": "object",\n'
            '            "properties": {"ratio": {"type": "number"}}}]},\n'
            "    },\n"
            "}\n"
        )
        hits, roots = numeric_fields_in_python_source(source)
        self.assertEqual(roots, 1, "型の literal を 1 つとして数えられること")
        self.assertEqual(
            sorted((hit.field_path, hit.field_type) for hit in hits),
            sorted([
                ("$.slots[].budget", "integer"),
                ("$.verdict.anyOf[0].ratio", "number"),
            ]),
        )


if __name__ == "__main__":
    unittest.main()
