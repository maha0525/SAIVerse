# -*- coding: utf-8 -*-
"""ページ本文を「人が書いた本文」と「抽出器が書いた Fragment 行」に割る。

設計: ``docs/intent/memopedia_body_to_fragment.md``

v0.2.x までの Memopedia は、抽出器 (``entity_extractor``) が抽出した知識も、
ペルソナやユーザーが自分で書いた記述も、同じ ``content`` 列へ文字として
積んでいた。このモジュールは前者だけを取り出して Fragment へ移す。

## 何を判定しているのか

**判定の目的は「誰が書いたか」ではない。「ユーザーとして本文に残しておかないと
まずいものかどうか」である** (まはー裁定 2026-08-05、intent §5.2)。

記法の判定が実際に効いているのも著者ではなく形のほうで、地の文・太字・入れ子・
コードフェンスを本文に残すのは「人が書いたから」ではなく **Fragment (1 行 1 件)
の器に収まらないから**。著者は代理指標であって目的ではない。

そのうえで、記法は片側にしか効かない:

* 抽出器が *書かない* 形 → **本文に残すべき形**だと言える
* 抽出器が *書く* 形 (``- `` 一行完結) → **抽出器が書いたとは言えない**。人も同じ形で書ける

だから判定は三段になる。①編集来歴が「抽出器が足した」と裏づけた行は確証あり、
②記法だけが根拠の行は保留、③保留は 1 行ずつユーザーが決める。**機械は決めない。**

## このモジュールの構成

前半は :func:`split_page_body` を中心とした**判定だけの純粋な層** (DB を触らない)。
後半にその判定を実データへ適用する層 (下見 / 実行 / 取り消し) を置く。
"""
from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Mapping, Optional, Tuple

LOGGER = logging.getLogger(__name__)

#: 指紋を作るときの区切り (値の境界を潰さないため)
SEP = b"\x00"
END = b"\x01"

__all__ = [
    "FragmentDraft",
    "BoundaryMark",
    "LinePlan",
    "BodySplit",
    "split_page_body",
    "attested_machine_lines",
    "CONVERSION_SOURCE",
    "init_body_conversion_table",
    "preview_conversion",
    "apply_conversion",
    "revert_conversion",
    "list_conversion_runs",
]

# 抽出器が書いた日付ブロックの見出し。``## `` (空白必須) + 実在する日付だけ。
# 空白を任意にすると ``##2026-01-01`` のような Markdown 見出しでない行を拾い、
# 日付の実在を見ないと ``2026-13-45`` も通る (Codex 指摘 2026-08-05)。
DATE_HEADING = re.compile(r"^##[ \t]+(\d{4}-\d{2}-\d{2})[ \t]*$")


def _date_heading(line: str) -> Optional[str]:
    """日付見出しなら日付文字列を返す。実在しない日付は見出しとして扱わない。"""
    matched = DATE_HEADING.match(line)
    if not matched:
        return None
    try:
        date.fromisoformat(matched.group(1))
    except ValueError:
        return None
    return matched.group(1)


# 任意の見出し (ブロックの終端判定に使う)。
ANY_HEADING = re.compile(r"^#{1,6}[ \t]+\S")
# 抽出物とみなす箇条書き: 行頭の "- " で始まり中身がある。
# 先頭に空白を許さないので、ネストした箇条書きは意図的に外れる。
MACHINE_BULLET = re.compile(r"^-[ \t]+\S")


@dataclass
class FragmentDraft:
    """Fragment にする 1 行 (まだ DB へ書いていない)。

    ``evidence`` は「なぜこの行を Fragment にしてよいと言えるか」:

    ``history``
        編集来歴に ``entity_extractor`` の追記として現れる。**確証あり**。
    ``notation``
        来歴に無く、記法が同じというだけ。**確証なし** —— この段の行は
        自動では変換せず、1 行ずつユーザーが決める (まはー裁定 2026-08-05)。
    """

    content: str
    source_date: str
    line_no: int
    """元本文での行番号 (1 始まり)。下見の表示と出自の記録に使う。"""
    evidence: str = "notation"


@dataclass
class BoundaryMark:
    """判定が際どかった箇所。下見でユーザーに見せる (intent §6)。

    kind:
        ``attested_outside_block``
            来歴では抽出器の追記だが、日付ブロックの外にある。
        ``attested_wrapped``
            来歴では抽出器の追記だが、次の行へ続いていて範囲が定まらない。
        ``ambiguous_duplicate``
            同じ記述が来歴の回数より多く本文にあり、どれがそれか決められない。
    """

    kind: str
    line_no: int
    text: str
    note: str


@dataclass
class LinePlan:
    """本文 1 行の行き先。

    ``raw`` は**改行文字を含んだ原文の 1 行**。本文の組み立てはこれを連結する
    だけにして、改行コードも空行も一切いじらない (intent 不変条件 1・2)。

    role:
        ``fragment``
            来歴が「抽出器が足した行」だと裏づけたもの。確証あり。
        ``pending``
            来歴に無く、記法だけが根拠のもの。**ユーザーが 1 行ずつ決める**
            (まはー裁定 2026-08-05)。機械は決めない。
        ``date_heading``
            日付見出し。ブロックの中身が全部出ていったら消える。
        ``body``
            本文に残る行。
    """

    line_no: int
    raw: str
    role: str
    block_id: int = -1
    source_date: Optional[str] = None
    content: Optional[str] = None


@dataclass
class BodySplit:
    """1 ページぶんの判定結果。

    三段 (まはー裁定 2026-08-05):

    1. 来歴に ``entity_extractor`` の追記として現れる行 → :attr:`fragments`
    2. 来歴に無いが記法が同じ行 → :attr:`pending`
    3. :attr:`pending` は 1 行ずつユーザーが「Fragment にする / 本文に残す」を決める
    """

    lines: List[LinePlan] = field(default_factory=list)
    marks: List[BoundaryMark] = field(default_factory=list)

    def _drafts(self, role: str, evidence: str) -> List[FragmentDraft]:
        return [
            FragmentDraft(
                content=lp.content or "",
                source_date=lp.source_date or "",
                line_no=lp.line_no,
                evidence=evidence,
            )
            for lp in self.lines
            if lp.role == role
        ]

    @property
    def fragments(self) -> List[FragmentDraft]:
        """来歴が裏づけた行。確認を挟まず Fragment にしてよい。"""
        return self._drafts("fragment", "history")

    @property
    def pending(self) -> List[FragmentDraft]:
        """記法だけが根拠の行。ユーザーの判断待ち。"""
        return self._drafts("pending", "notation")

    @property
    def changed(self) -> bool:
        return any(lp.role in ("fragment", "pending") for lp in self.lines)

    def taken(self, decisions: Optional[Dict[int, str]] = None) -> List[FragmentDraft]:
        """この判断で Fragment になる行 (確証あり + 明示的に選ばれた保留行)。"""
        decisions = decisions or {}
        return sorted(
            self.fragments
            + [d for d in self.pending if decisions.get(d.line_no) == "fragment"],
            key=lambda d: d.line_no,
        )

    def dropped_lines(self, decisions: Optional[Dict[int, str]] = None) -> set:
        """本文から抜く行の行番号。

        抜くのは 3 種類だけ:

        1. Fragment へ移す行
        2. 中身が全部出ていったブロックの日付見出し
        3. **その見出しの直上に連なる空白行の帯** —— 各見出しは自分の直上の
           空白帯 (前のブロックとの区切り。空白・全角空白だけの行を含み、
           連続していれば複数行) を所有していて、見出しが消えるときは帯ごと
           消える。ファイル先頭のブロックでは、先頭に積まれた空白行がその帯に
           あたる。見出しが残るなら帯もそのまま残る

        3 を一緒に抜くのが要点 (まはー 2026-08-06)。抜いたあとで「残った文字列が
        空っぽか」を判定する後始末をやると、「空っぽとは何か」を定義する羽目に
        なり、消える見出しと無関係な場所の *書かれた空白* まで消してしまう。
        この規則が触るのは消える見出しの直上だけで、残る本文の中の空白行には
        一切触れない。**抜くときに正しく抜けば、判定そのものが要らない。**
        """
        decisions = decisions or {}
        drop = set()
        alive_blocks = set()
        for lp in self.lines:
            if lp.role == "fragment":
                drop.add(lp.line_no)
            elif lp.role == "pending":
                if decisions.get(lp.line_no, "body") == "fragment":
                    drop.add(lp.line_no)
                else:
                    alive_blocks.add(lp.block_id)
            elif lp.role != "date_heading" and lp.raw.strip() and lp.block_id >= 0:
                alive_blocks.add(lp.block_id)

        by_line = {lp.line_no: lp for lp in self.lines}
        for lp in self.lines:
            if lp.role != "date_heading" or lp.block_id in alive_blocks:
                continue
            drop.add(lp.line_no)
            prev = lp.line_no - 1
            while prev >= 1:
                above = by_line.get(prev)
                if above is None or above.raw.strip():
                    break
                drop.add(prev)
                prev -= 1
        return drop

    def render_body(self, decisions: Optional[Dict[int, str]] = None) -> str:
        """保留行の決定を織り込んで本文を組み立てる。

        **抜く行を除いて、原文の行をそのまま連結するだけ。** 改行コードの変換も、
        空行の畳み込みも、前後の空行の除去も、空になったかどうかの判定もしない。
        整形を混ぜると「文字を移すだけ」という約束が崩れ、しかも逐語の検算では
        捕まらない (Codex 指摘 2026-08-05)。

        Args:
            decisions: 行番号 → ``"fragment"`` / ``"body"``。**既定は本文に残す** ——
                決めていない保留行を勝手に持っていかない。
        """
        drop = self.dropped_lines(decisions)
        return "".join(lp.raw for lp in self.lines if lp.line_no not in drop)

    def has_human_body(self, decisions: Optional[Dict[int, str]] = None) -> bool:
        return bool(self.render_body(decisions))


def _is_self_contained(lines: List[str], i: int) -> bool:
    """``lines[i]`` の箇条書きが 1 行で完結しているか (改行を除いた本文で判定)。

    次の行が「空行 / 別の箇条書き / 見出し / 終端」なら完結。それ以外
    (地の文の続き、ネストした箇条書き) は継続行とみなす。

    ⚠ **これは著者の判定ではない。** Markdown の構文上「その箇条書き項目がどこで
    終わるか」を見ているだけ。項目の直後に字下げなしの行が来ると、その行は項目の
    続きとして読める (lazy continuation) ので範囲が確定せず、切り取ると記述が
    途中で千切れる。
    """
    if i + 1 >= len(lines):
        return True

    nxt = lines[i + 1]
    if nxt.strip():
        # 直後の行: 箇条書き・見出しなら別の要素。それ以外は緩い継続 (lazy)。
        return bool(MACHINE_BULLET.match(nxt) or ANY_HEADING.match(nxt))

    # 空行を挟んだ場合、続きになりうるのは **字下げされた行だけ**。
    # 字下げの無い段落は、Markdown ではリストの外の新しい段落。
    j = i + 1
    while j < len(lines) and not lines[j].strip():
        j += 1
    if j >= len(lines):
        return True
    return not lines[j][:1].isspace()


def _strip_bullet(line: str) -> str:
    """``- 本文`` から本文だけを取り出す (逐語。要約も補完もしない)。

    **末尾は削らない。** Markdown では行末の空白 2 つが改行の意味を持つ。
    ``strip()`` で丸ごと落とすと、原文と Fragment が食い違うのに逐語の検算は
    通ってしまう (Codex 指摘 2026-08-05)。落とすのは行頭の字下げと箇条書き記号だけ。
    """
    body = line.lstrip()[1:]
    return body[1:] if body[:1] in (" ", "\t") else body


#: コードフェンスの開始・終了。Markdown では **同じ記号で、開始と同じ長さ以上**の
#: 行だけが閉じる。単一の真偽値で開閉すると、backtick フェンスの中の ~~~ や、
#: 長いフェンスの中の短い ``` で閉じたことになり、コード例の箇条書きが記憶として
#: 抽出される (Codex 指摘 2026-08-05)。
_FENCE_OPEN = re.compile(r"^ {0,3}(?P<mark>`{3,}|~{3,})")
#: 閉じ行は記号だけで完結すること。``` の後ろに言語名が付いた行 (```python) は
#: **開始行であって閉じ行ではない**。
_FENCE_CLOSE = re.compile(r"^ {0,3}(?P<mark>`{3,}|~{3,})[ \t]*$")


def _fence_event(line: str, fence: Optional[tuple]) -> Optional[tuple]:
    """フェンスの状態遷移。開いたら (記号, 長さ)、閉じたら None、変化なしは fence。"""
    if fence is None:
        matched = _FENCE_OPEN.match(line)
        if not matched:
            return None
        mark = matched.group("mark")
        return (mark[0], len(mark))

    matched = _FENCE_CLOSE.match(line)
    if not matched:
        return fence
    mark = matched.group("mark")
    open_char, open_len = fence
    if mark[0] == open_char and len(mark) >= open_len:
        return None
    return fence


def split_page_body(
    content: str, attested: Optional[Mapping[str, int]] = None
) -> BodySplit:
    """本文の各行の行き先を決める。

    文字を移すだけで、要約・言い換え・補完はしない。LLM も呼ばない
    (intent 不変条件 2)。

    Args:
        content: ページ本文。
        attested: 編集来歴が「抽出器が足した」と裏づけた行と、**その回数**。
            渡さない場合は全行が :attr:`BodySplit.pending` になる ——
            **来歴が無いことを「抽出器が書いた」と読み替えない**。
    """
    if not content:
        return BodySplit()

    counts: Dict[str, int] = dict(attested or {})
    raw_lines = content.splitlines(keepends=True)
    bare = [ln.rstrip("\r\n") for ln in raw_lines]
    plans: List[LinePlan] = []
    marks: List[BoundaryMark] = []
    block_id = -1
    current_date: Optional[str] = None
    in_block = False
    fence: Optional[tuple] = None
    seen: Dict[str, int] = {}

    for i, raw in enumerate(raw_lines):
        line_no = i + 1
        line = bare[i]

        # コードフェンスの中は Markdown の記法として読めない。まるごと本文。
        next_fence = _fence_event(line, fence)
        if next_fence is not fence or fence is not None:
            fence = next_fence
            plans.append(LinePlan(line_no, raw, "body", block_id if in_block else -1))
            continue

        matched = _date_heading(line)
        if matched:
            block_id += 1
            current_date = matched
            in_block = True
            plans.append(LinePlan(line_no, raw, "date_heading", block_id, current_date))
            continue
        if ANY_HEADING.match(line):
            in_block = False
            current_date = None
            plans.append(LinePlan(line_no, raw, "body"))
            continue

        stripped = line.strip()
        is_bullet = bool(MACHINE_BULLET.match(line))

        if not in_block:
            # 日付ブロックの外。来歴が抽出器の追記だと言っていても、日付が付けられず
            # 前後の文脈も分からないので本文に残す。起きたことは印で知らせる。
            if is_bullet and counts.get(stripped, 0) > 0:
                marks.append(
                    BoundaryMark(
                        kind="attested_outside_block",
                        line_no=line_no,
                        text=stripped,
                        note=(
                            "会話から自動で書き出された行ですが、日付の見出しの外に"
                            "あるため本文に残しました。"
                        ),
                    )
                )
            plans.append(LinePlan(line_no, raw, "body"))
            continue

        if not stripped or not is_bullet:
            plans.append(LinePlan(line_no, raw, "body", block_id))
            continue

        if not _is_self_contained(bare, i):
            # 項目の範囲が確定しないので切れない。来歴の裏づけがあっても同じ。
            if counts.get(stripped, 0) > 0:
                marks.append(
                    BoundaryMark(
                        kind="attested_wrapped",
                        line_no=line_no,
                        text=stripped,
                        note=(
                            "会話から自動で書き出された行ですが、次の行へ続いていて"
                            "どこまでが一つの記述か定まらないため本文に残しました。"
                        ),
                    )
                )
            plans.append(LinePlan(line_no, raw, "body", block_id))
            continue

        plans.append(
            LinePlan(line_no, raw, "pending", block_id, current_date, _strip_bullet(line))
        )

    # 本文にその記述が何行あるか。**候補にならなかった行も数える** ——
    # コードフェンスの中や日付ブロックの外に同じ記述があるとき、候補側だけを
    # 数えると来歴の回数に届いてしまい、確証あり扱いになる (Codex 指摘 2026-08-05)。
    for text in bare:
        key = text.strip()
        if key:
            seen[key] = seen.get(key, 0) + 1

    # 来歴の裏づけを割り当てる。**同じ文字列が来歴の回数より多く本文にあるときは、
    # どれが抽出器の行か決められないので 1 つも確証にしない**。回数を上から順に
    # 消費すると、人が先に書いた行を抽出器由来と誤認する (Codex 指摘 2026-08-05)。
    for plan in plans:
        if plan.role != "pending":
            continue
        text = plan.raw.strip()
        if counts.get(text, 0) >= seen.get(text, 0):
            plan.role = "fragment"
        elif counts.get(text, 0) > 0:
            marks.append(
                BoundaryMark(
                    kind="ambiguous_duplicate",
                    line_no=plan.line_no,
                    text=text,
                    note=(
                        f"同じ記述が本文に {seen[text]} 行あり、自動で書き出された"
                        f"記録は {counts[text]} 行ぶんです。どれがそれに当たるか"
                        "決められないので、すべて判断待ちにしました。"
                    ),
                )
            )

    return BodySplit(lines=plans, marks=marks)


# ==========================================================================
# 適用層 — 下見 / 実行 / 取り消し
#
# 判定は原理的に不完全 (intent §5.3) なので、この層の役割は「正しく変換する
# こと」ではなく **「間違えても取り返せる形で変換すること」** にある。
# ==========================================================================

CONVERSION_SOURCE = "body_to_fragment"

#: Chronicle のページは対象外。Fragment が ``chronicle_entry_id`` で *指す先*
#: であって、Fragment を持つ側ではない。物語文を行へ刻むと文脈が壊れる。
_EXCLUDED_CATEGORIES = ("chronicle",)

_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")

#: 抽出器が本文へ知識を書き足す経路。ここに現れた行だけが「確証あり」。
_MACHINE_EDIT_SOURCES = ("entity_extractor",)


def init_body_conversion_table(conn: sqlite3.Connection) -> None:
    """変換の実行記録テーブルを冪等に用意する。

    「どの実行が何を作ったか」を持つのはこの表だけ。取り消しはここを見る。
    変換前の本文は編集来歴 (``memopedia_page_edit_history``) が持つので、
    この表には持たせない (真実の置き場をひとつにする)。
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memopedia_body_conversion (
            run_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            ref_id TEXT NOT NULL,
            converted_at INTEGER NOT NULL,
            digest TEXT,
            page_id TEXT,
            attest_digest TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_body_conversion_run "
        "ON memopedia_body_conversion(run_id)"
    )
    # 後から足した列。古いビルドで作られた行は NULL になりうる。
    for column in ("digest", "page_id", "attest_digest"):
        try:
            conn.execute(f"SELECT {column} FROM memopedia_body_conversion LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute(
                f"ALTER TABLE memopedia_body_conversion ADD COLUMN {column} TEXT"
            )
    # ⚠ 呼び出し元が開けているトランザクションを確定させない。ここで commit すると、
    # 呼び出し元の未確定の変更まで先に確定し、あとで rollback しても戻せない
    # (Codex 指摘 2026-08-05)。
    if not conn.in_transaction:
        conn.commit()


def _target_pages(conn: sqlite3.Connection) -> List[tuple]:
    """変換対象のページ。

    対象は **抽出器 (``entity_extractor``) が本文へ書き足した実績のあるページだけ**。
    v0.2.x の抽出本文を移すのが目的なので、その痕跡が来歴に無いページ
    (コア記憶・テーマ・手書きのページなど) は最初から範囲外にする。カテゴリの
    一覧で線を引くと、新しいカテゴリが増えるたびに漏れる (Codex 指摘 2026-08-05)。

    Chronicle は例外的に名指しで外す。Fragment が ``chronicle_entry_id`` で
    *指す先* であって Fragment を持つ側ではなく、物語文を行へ刻むと文脈が壊れる。
    trunk (カテゴリの根っこ) も中身を持たない器なので外す。
    """
    holes = ",".join("?" for _ in _EXCLUDED_CATEGORIES)
    sources = ",".join("?" for _ in _MACHINE_EDIT_SOURCES)
    return conn.execute(
        f"""
        SELECT p.id, p.title, p.summary, p.content, p.category
        FROM memopedia_pages p
        WHERE (p.is_deleted = 0 OR p.is_deleted IS NULL)
          AND (p.is_trunk = 0 OR p.is_trunk IS NULL)
          AND (p.category IS NULL OR p.category NOT IN ({holes}))
          AND p.content IS NOT NULL AND p.content <> ''
          AND EXISTS (
              SELECT 1 FROM memopedia_page_edit_history h
              WHERE h.page_id = p.id AND h.edit_source IN ({sources})
          )
        ORDER BY p.title
        """,
        (*_EXCLUDED_CATEGORIES, *_MACHINE_EDIT_SOURCES),
    ).fetchall()


def _line_key(text: str) -> str:
    """検算用の行キー。箇条書きは ``- 本文`` の形に均す。

    本文に残る行は原文のまま、Fragment 側は中身だけを持つ。同じ記述を同じキーで
    数えないと、書きぶりの揺れが「行が消えた・現れた」として検算に出てしまう。
    """
    stripped = text.strip()
    if MACHINE_BULLET.match(stripped):
        return "- " + stripped[1:].strip()
    return stripped


def _record_lines(bucket: Dict[str, int], text: str) -> None:
    """検算用に記述行を数える。日付見出しは器なので数えない。"""
    for line in (text or "").splitlines():
        if not line.strip() or _date_heading(line):
            continue
        key = _line_key(line)
        bucket[key] = bucket.get(key, 0) + 1


def _diff_buckets(before: Dict[str, int], after: Dict[str, int]) -> Dict[str, List[str]]:
    lost, gained = [], []
    for text, count in before.items():
        lost.extend([text] * max(0, count - after.get(text, 0)))
    for text, count in after.items():
        gained.extend([text] * max(0, count - before.get(text, 0)))
    return {"lost": lost, "gained": gained}


def _verbatim_breach(
    original: str, body: str, taken: List[FragmentDraft], dropped: set
) -> Optional[str]:
    """本文が「抜くと決めた行を抜いただけ」になっているかを、原文の行で確かめる。

    多重集合の突き合わせは空行・改行コード・日付見出しを見ないので、整形の混入を
    捕まえられない (Codex 指摘 2026-08-05)。ここでは **原文の行をそのまま**
    突き合わせる。

    抜いてよい行は :meth:`BodySplit.dropped_lines` が決めたものだけ。
    「空っぽとは何か」「消えてよい行とは何か」をここで判定し直さない ——
    判定が二箇所に散ると、片方だけ直して食い違う (2026-08-06 に実際にやった)。
    """
    original_lines = original.splitlines(keepends=True)

    # 取り出した行は本文から消えるので、残った本文の比較だけでは中身の欠けを
    # 捕まえられない。Fragment の中身を原文の当該行と直接突き合わせる。
    for draft in taken:
        if not (1 <= draft.line_no <= len(original_lines)):
            return f"{draft.line_no} 行目が原文にありません"
        source = original_lines[draft.line_no - 1].rstrip("\r\n")
        if _strip_bullet(source) != draft.content:
            return (
                f"{draft.line_no} 行目の取り出しが原文と食い違います: "
                f"{source!r} -> {draft.content!r}"
            )

    expected = "".join(
        raw for i, raw in enumerate(original_lines, start=1) if i not in dropped
    )
    if body != expected:
        return "本文が「抜いた行を除いた原文」と一致しません"
    return None


def _added_lines(diff_text: str) -> List[str]:
    """unified diff から追加された行を取り出す。

    ``storage.generate_diff`` は ``lineterm=""`` のまま ``"".join()`` するので、
    ファイル見出しどうし、およびハンク見出しとその直後の 1 行が、同じ行に癒着する。
    **構造見出しは行の先頭でだけ剥がす** —— diff 全体へ文字列置換をかけると、
    本文に同じ文字列が含まれていたときに中身を壊す (Codex 指摘 2026-08-05)。
    """
    if not diff_text:
        return []
    out: List[str] = []
    for line in diff_text.splitlines():
        # 癒着した構造見出しを、行頭から順に 1 つずつ剥がす
        while True:
            if line.startswith("--- before"):
                line = line[len("--- before"):]
                continue
            if line.startswith("+++ after"):
                line = line[len("+++ after"):]
                continue
            hunk = _HUNK_HEADER.match(line)
            if hunk:
                line = line[hunk.end():]
                continue
            break
        if not line.startswith("+"):
            continue
        body = line[1:].strip()
        if body:
            out.append(body)
    return out


def attested_machine_lines(conn: sqlite3.Connection) -> Dict[str, Dict[str, int]]:
    """ページごとに「抽出器が足した行」と **その回数** を集める。

    回数で持つのは、同じ文字列が本文に複数あるとき *抽出器が足した分だけ* を確証
    ありにするため。集合で持つと、人が複製した 2 つ目や、削除後に人が書き直した
    行まで自動変換されてしまう (Codex 指摘 2026-08-05)。

    ⚠ ``edit_source`` が示すのは **編集がどの口から入ったか** であって、その文章を
    誰が書いたかではない。実機 aifi の ``manual_ui`` 編集は、抽出器と同じ日付ブロック
    形式の内容を一括で流し込んでいた。だから確証に数えるのは
    ``entity_extractor`` の追記だけで、他の経路で入った行は
    :attr:`BodySplit.pending` に落ちてユーザーの判断を待つ。
    """
    holes = ",".join("?" for _ in _MACHINE_EDIT_SOURCES)
    buckets: Dict[str, Dict[str, int]] = {}
    for page_id, diff_text in conn.execute(
        f"SELECT page_id, diff_text FROM memopedia_page_edit_history "
        f"WHERE edit_source IN ({holes})",
        _MACHINE_EDIT_SOURCES,
    ):
        bucket = buckets.setdefault(page_id, {})
        for text in _added_lines(diff_text):
            bucket[text] = bucket.get(text, 0) + 1
    return buckets


def assert_ledger_is_usable(conn: sqlite3.Connection) -> None:
    """変換台帳に、ページや指紋を持たない行が無いことを確かめる。

    ``page_id`` / ``digest`` は後から足した列なので、古いビルドで作られた行は
    NULL になりうる。NULL を黙って読み飛ばすと「移動済み」を数え落として、
    ユーザーが書き直した行を抽出器由来と誤認する (Codex 指摘 2026-08-05)。
    """
    broken = conn.execute(
        "SELECT COUNT(*) FROM memopedia_body_conversion "
        "WHERE page_id IS NULL OR digest IS NULL"
    ).fetchone()[0]
    if broken:
        raise ValueError(
            f"変換台帳に、ページか指紋を持たない古い行が {broken} 件あります。"
            "移動済みの判定と取り消しの安全を確かめられないため中止しました"
            "（古い実行を取り消してから、下見をやり直してください）。"
        )


def _attest_key(content: str) -> str:
    """来歴との照合に使うキー。``_added_lines`` の集め方 (strip 済み) に揃える。"""
    return ("- " + content).strip()


def _digest_of(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _page_digest(title: str, summary: str, content: str, is_deleted: Any) -> str:
    """取り消しが上書きする範囲ぜんぶの指紋。

    本文だけを見ていると、変換後にタイトルや要約だけを直した編集を競合と
    見なせず、取り消しがそれを黙って戻してしまう (Codex 指摘 2026-08-05)。
    復元する値は全部ここに含める。
    """
    return _digest_of(
        "\x00".join([
            title or "", summary or "", content or "", str(int(bool(is_deleted)))
        ])
    )


def _already_moved_out(conn: sqlite3.Connection) -> Dict[str, Dict[str, int]]:
    """ページごとに、この変換がすでに本文の外へ出した記述の指紋と回数。

    来歴の回数は「抽出器がこれまでに何回足したか」の累計で、いまの本文のどの行に
    対応するかを持たない。一度変換したあとユーザーが同じ文字列を本文へ書き直すと、
    来歴 1 回・本文 1 行となって自動変換されてしまう (Codex 指摘 2026-08-05)。

    数える先は **変換台帳に記録した移動分だけ**。現存する Fragment の本文を
    数えると、手で作った Fragment まで移動分として差し引き、逆に変換後に
    Fragment の本文が編集されると差し引けなくなる。台帳は移した時点の指紋を
    持つので、あとで中身が編集されても移動の実績は揺るがない。

    ただし **消された Fragment は「外に出したまま」ではない**。一般の rollback や
    手動削除のあと本文に戻った記述を、移動済みとして数え落とさないよう、いま
    存在する Fragment に紐づく行だけを数える。
    """
    moved: Dict[str, Dict[str, int]] = {}
    for page_id, digest in conn.execute(
        "SELECT c.page_id, c.attest_digest FROM memopedia_body_conversion c "
        "WHERE c.kind IN ('fragment', 'dedup') AND c.page_id IS NOT NULL "
        "  AND c.attest_digest IS NOT NULL "
        "  AND EXISTS (SELECT 1 FROM memopedia_fragments f "
        "              WHERE f.id = c.ref_id AND f.entity_id = c.page_id)"
    ):
        bucket = moved.setdefault(page_id, {})
        bucket[digest] = bucket.get(digest, 0) + 1
    return moved


def _existing_fragment_index(
    conn: sqlite3.Connection,
) -> Dict[str, Dict[Tuple[str, Optional[str]], List[str]]]:
    """ページごとに、既にある Fragment を (本文, 日付) で引ける索引にする。

    Fragment 移行期の抽出器は、本文への追記と Fragment の作成を同時に行っていた
    (コミット e1866ef)。その時期のデータでは、本文の行と同じ内容の Fragment が
    既に存在する —— 照合せずに作ると同じ記憶が二重になり、想起の重複と過重評価を
    招く (Codex 指摘 2026-08-06)。

    キーの日付は Fragment の ``source_date``。値は Fragment id のリスト
    (挿入順)。同じ内容が複数あれば、その数だけ照合に使える。
    """
    index: Dict[str, Dict[Tuple[str, Optional[str]], List[str]]] = {}
    for frag_id, entity_id, content, source_date in conn.execute(
        "SELECT id, entity_id, content, source_date FROM memopedia_fragments "
        "ORDER BY rowid"
    ):
        index.setdefault(entity_id, {}).setdefault(
            (content, source_date or None), []
        ).append(frag_id)
    return index


def _partition_drafts(
    drafts: List[FragmentDraft],
    existing: Optional[Dict[Tuple[str, Optional[str]], List[str]]],
) -> Tuple[List[FragmentDraft], List[Tuple[FragmentDraft, str]]]:
    """Fragment にする行を「新しく作る」と「既にあるものへ寄せる」に分ける。

    同じページに、同じ本文・両立する日付 (同じ日付か、日付なし) の Fragment が
    既にあれば、新しくは作らず **本文から抜くだけ** にする。記憶は既に Fragment 側に
    あるので、ユーザーとして失うものは無い (まはー裁定 2026-08-05:
    本質は「本文に残しておかないとまずいものかどうか」)。

    既存 Fragment 1 つが照合に使えるのは 1 回だけ —— 同じ行が 2 回観測されて
    いれば、1 行は既存へ寄り、もう 1 行は新しく作られる。
    """
    pool = {key: list(ids) for key, ids in (existing or {}).items()}
    to_create: List[FragmentDraft] = []
    dedup: List[Tuple[FragmentDraft, str]] = []
    for draft in drafts:
        keys = [(draft.content, draft.source_date or None)]
        if draft.source_date:
            keys.append((draft.content, None))
        for key in keys:
            ids = pool.get(key)
            if ids:
                dedup.append((draft, ids.pop(0)))
                break
        else:
            to_create.append(draft)
    return to_create, dedup


def _attestation_budget(
    attested_all: Dict[str, Dict[str, int]],
    moved_all: Dict[str, Dict[str, int]],
    page_id: str,
) -> Dict[str, int]:
    """このページで、まだ本文に残っているはずの抽出器の寄与。"""
    attested = attested_all.get(page_id) or {}
    moved = moved_all.get(page_id) or {}
    if not moved:
        return attested
    return {
        text: count - moved.get(_digest_of(text.strip()), 0)
        for text, count in attested.items()
    }


def _page_decisions(
    decisions: Optional[Dict[str, Dict[Any, str]]], page_id: str
) -> Dict[int, str]:
    """API から来る ``{page_id: {line_no: 判断}}`` を 1 ページぶんへ均す。

    JSON を経由すると行番号が文字列になるので、ここで整数へ揃える。
    """
    raw = (decisions or {}).get(page_id) or {}
    out: Dict[int, str] = {}
    for line_no, choice in raw.items():
        try:
            out[int(line_no)] = str(choice)
        except (TypeError, ValueError):
            continue
    return out


def _block_view(split: BodySplit) -> List[Dict[str, Any]]:
    """保留行を含む日付ブロックを、ユーザーが判断できる形に整える。

    **そのブロックの行を全部返す。** 保留行だけを裸で並べても「本文に残すべきか」
    は判断できない —— 前後の地の文や、同じブロックの確証あり行が見えて初めて、
    その行が何の話の一部なのか分かる (Codex 指摘 2026-08-05)。

    ``role`` で用途が分かる: ``pending`` は判断が要る行、``fragment`` は確証あり、
    ``body`` は判断の対象外でそのまま本文に残る行 (文脈として見せるだけ)。
    """
    blocks: Dict[int, Dict[str, Any]] = {}
    for lp in split.lines:
        if lp.block_id < 0 or lp.role == "date_heading":
            if lp.role == "date_heading" and lp.block_id >= 0:
                blocks.setdefault(
                    lp.block_id,
                    {"date": lp.source_date, "lines": [], "has_pending": False},
                )["date"] = lp.source_date
            continue
        block = blocks.setdefault(
            lp.block_id, {"date": lp.source_date, "lines": [], "has_pending": False}
        )
        if lp.role == "body" and not lp.raw.strip():
            continue  # 空行は文脈にならない
        block["lines"].append({
            "line_no": lp.line_no,
            # body 行は箇条書きとは限らないので原文をそのまま見せる
            "content": lp.content if lp.content is not None else lp.raw.rstrip("\r\n"),
            "role": lp.role,
        })
        if lp.role == "pending":
            block["has_pending"] = True
    return [b for b in blocks.values() if b["has_pending"]]


def _ledger_state(conn: sqlite3.Connection) -> List[tuple]:
    """指紋に含める変換台帳の状態。

    **Fragment がいま存在するかも含める。** 差し引き (:func:`_already_moved_out`)
    は現存する Fragment に紐づく行だけを数えるので、下見のあとに Fragment が
    消されると判定が変わる。指紋がそれを見ていないと、保留だった行が確証あり
    として無承認で変換される (Codex 指摘 2026-08-05)。
    """
    return conn.execute(
        "SELECT c.run_id, c.kind, c.ref_id, COALESCE(c.page_id, ''), "
        "       COALESCE(c.digest, ''), COALESCE(c.attest_digest, ''), "
        "       CASE WHEN f.id IS NULL THEN 0 ELSE 1 END, COALESCE(f.entity_id, '') "
        "FROM memopedia_body_conversion c "
        "LEFT JOIN memopedia_fragments f ON f.id = c.ref_id AND c.kind = 'fragment' "
        "ORDER BY c.run_id, c.kind, c.ref_id"
    ).fetchall()


def _fingerprint_of(
    rows,
    attested_all: Dict[str, Dict[str, int]],
    ledger: Optional[List[tuple]] = None,
    frag_index: Optional[Dict[str, Dict[Tuple[str, Optional[str]], List[str]]]] = None,
) -> str:
    """読み取ったその行そのものから指紋を作る。

    別 SELECT で取り直すと、下見が読んだ状態と指紋の状態がずれる —— 返した
    指紋は新しいのに画面の中身は古い、という食い違いが起こる
    (Codex 指摘 2026-08-05)。
    """
    digest = hashlib.sha256()
    for page_id, title, summary, content, category in rows:
        # 判断の材料として画面に出る値は全部入れる。タイトルが変われば同じ行でも
        # 意味が変わるので、下見のあとの変更は再下見を要求する。
        for value in (page_id, title, summary, category, content):
            digest.update((value or "").encode("utf-8"))
            digest.update(SEP)
        for text, count in sorted((attested_all.get(page_id) or {}).items()):
            digest.update(f"{count}:{text}".encode("utf-8"))
            digest.update(SEP)
        # 既にある Fragment は「新しく作るか、既存へ寄せるか」の判定に効く。
        # 下見のあとで Fragment が増減したら、選んだ行の行き先が変わりうるので
        # 再下見を要求する (台帳に載らない移行期の Fragment は _ledger_state では
        # 捕まらない)。
        for (text, frag_date), ids in sorted(
            (frag_index or {}).get(page_id, {}).items(),
            key=lambda kv: (kv[0][1] or "", kv[0][0]),
        ):
            digest.update(f"{len(ids)}:{frag_date or ''}:{text}".encode("utf-8"))
            digest.update(SEP)
        digest.update(END)
    # 既に外へ出した分は判定に効くので、台帳が変われば指紋も変わるべき
    for row in ledger or ():
        digest.update(("|".join(str(v) for v in row)).encode("utf-8"))
        digest.update(SEP)
    return digest.hexdigest()


def conversion_fingerprint(conn: sqlite3.Connection) -> str:
    """判定の入力すべての指紋 (対象ページの本文 **と** 来歴の裏づけ **と** 台帳)。

    下見と実行の間に本文が変われば、ユーザーが選んだ行番号は別の行を指しうる。
    本文が同じでも来歴や台帳が変われば「確証あり / 保留」の振り分けが変わり、
    ユーザーが「本文に残す」と決めた行が確証あり側へ回って勝手に変換される。
    """
    return _fingerprint_of(
        _target_pages(conn),
        attested_machine_lines(conn),
        _ledger_state(conn),
        _existing_fragment_index(conn),
    )


def preview_conversion(
    conn: sqlite3.Connection,
    decisions: Optional[Dict[str, Dict[Any, str]]] = None,
) -> Dict[str, Any]:
    """書き込まずに、変換したら何がどうなるかを返す (intent §6 の「下見」)。

    実行と同じ判定コードを通る —— 下見で見た結果と実行結果が食い違わないこと。

    Args:
        decisions: ``{page_id: {line_no: "fragment" | "body"}}``。保留行への
            ユーザーの判断。渡さない行は **本文に残す** 扱い。
    """
    init_body_conversion_table(conn)
    assert_ledger_is_usable(conn)

    # 下見の結果と指紋は **同じ一読み** から作る。途中でページが増えたり本文が
    # 変わったりすると、返す指紋と画面の中身が別々の状態を指す。
    own_snapshot = not conn.in_transaction
    if own_snapshot:
        conn.execute("BEGIN DEFERRED")
    try:
        attested_all = attested_machine_lines(conn)
        moved_all = _already_moved_out(conn)
        frag_index = _existing_fragment_index(conn)
        rows = _target_pages(conn)
        fingerprint = _fingerprint_of(
            rows, attested_all, _ledger_state(conn), frag_index
        )
    finally:
        if own_snapshot:
            conn.rollback()

    pages: List[Dict[str, Any]] = []
    marks: List[Dict[str, Any]] = []
    pending_pages: List[Dict[str, Any]] = []
    breaches: List[Dict[str, str]] = []
    before: Dict[str, int] = {}
    after: Dict[str, int] = {}
    taken_total = dedup_total = pending_count = decided_count = 0
    emptied = kept_body = 0

    for page_id, title, _summary, content, category in rows:
        split = split_page_body(
            content, _attestation_budget(attested_all, moved_all, page_id)
        )
        page_decisions = _page_decisions(decisions, page_id)
        body = split.render_body(page_decisions)
        taken = split.taken(page_decisions)
        _to_create, dedup = _partition_drafts(taken, frag_index.get(page_id))
        decided_count += sum(1 for d in taken if d.evidence == "notation")

        _record_lines(before, content)
        _record_lines(after, body)
        # 既存 Fragment へ寄せる行も含めて数える —— どちらの行き先でも、その記述は
        # 逐語で Fragment 側に存在する (寄せる先は同一内容の照合で選ばれている)。
        for draft in taken:
            key = _line_key("- " + draft.content)
            after[key] = after.get(key, 0) + 1

        breach = _verbatim_breach(
            content or "", body, taken, split.dropped_lines(page_decisions)
        )
        if breach:
            breaches.append({"page_id": page_id, "title": title, "detail": breach})

        for mark in split.marks:
            marks.append({
                "kind": mark.kind, "page_id": page_id, "page_title": title,
                "line_no": mark.line_no, "text": mark.text, "note": mark.note,
            })
        for draft, _frag_id in dedup:
            marks.append({
                "kind": "already_fragment", "page_id": page_id, "page_title": title,
                "line_no": draft.line_no, "text": draft.content,
                "note": "同じ内容が Fragment に既にあるため、新しくは作らず本文からだけ抜きます",
            })

        blocks = _block_view(split)
        if blocks:
            pending_pages.append({
                "page_id": page_id, "title": title, "category": category,
                "blocks": blocks,
            })
        pending_count += len(split.pending)

        if not taken:
            continue
        taken_total += len(taken)
        dedup_total += len(dedup)
        if body.strip():
            kept_body += 1
        else:
            emptied += 1
        pages.append({
            "page_id": page_id, "title": title, "category": category,
            "fragment_count": len(taken) - len(dedup),
            "dedup_count": len(dedup),
            "before_chars": len(content or ""),
            "after_chars": len(body),
        })

    conservation = _diff_buckets(before, after)
    is_safe = not conservation["lost"] and not conservation["gained"] and not breaches
    return {
        "fingerprint": fingerprint,
        "page_count": len(pages),
        "fragment_count": taken_total - dedup_total,
        "dedup_count": dedup_total,
        "confirmed_count": taken_total - decided_count,
        "pending_count": pending_count,
        "decided_count": decided_count,
        "emptied_count": emptied,
        "kept_body_count": kept_body,
        "pages": pages,
        "pending_pages": pending_pages,
        "marks": marks,
        "verbatim_breaches": breaches,
        "conservation": {
            "before_lines": sum(before.values()),
            "after_lines": sum(after.values()),
            "lost": conservation["lost"],
            "gained": conservation["gained"],
        },
        "is_safe": is_safe,
    }


def apply_conversion(
    conn: sqlite3.Connection,
    *,
    expected_fingerprint: str,
    decisions: Optional[Dict[str, Dict[Any, str]]] = None,
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """変換を実行する。取り消せる形で行う (intent §5.4)。

    来歴が裏づけた行はそのまま Fragment にする。記法だけが根拠の保留行は、
    ``decisions`` で ``"fragment"`` と明示されたものだけを Fragment にする ——
    **判断がない保留行は本文に残す**。機械が代わりに決めない (まはー裁定)。

    **全ページを単一トランザクションで書く。** 途中で失敗したら 1 ページも
    変わっていない状態へ戻す —— ページごとに確定していくと、失敗した変換の
    残骸が本文と Fragment に散る (Codex 指摘 2026-08-05)。

    Args:
        expected_fingerprint: 下見が返した指紋。判定の入力が変わっていれば、
            ユーザーが選んだ行番号が別の行を指しうるので実行しない。

    Raises:
        ValueError: 逐語の検算に失敗した / 指紋が食い違う / 指紋が無い場合。
    """
    from sai_memory.memopedia.storage import generate_diff, record_page_edit

    init_body_conversion_table(conn)

    if not expected_fingerprint:
        raise ValueError(
            "実行には下見が返した指紋が要ります（下見からやり直してください）。"
        )
    if conversion_fingerprint(conn) != expected_fingerprint:
        raise ValueError(
            "下見のあとで対象ページの本文か編集来歴が変わりました。選んだ行が"
            "別の行を指す恐れがあるため実行しません（下見からやり直してください）。"
        )

    preview = preview_conversion(conn, decisions)
    if not preview["is_safe"]:
        detail = ""
        if preview["verbatim_breaches"]:
            first = preview["verbatim_breaches"][0]
            detail = f" 例: {first['title']} — {first['detail']}"
        raise ValueError(
            "逐語の検算に失敗したため変換を中止しました "
            f"(消えた行 {len(preview['conservation']['lost'])} / "
            f"現れた行 {len(preview['conservation']['gained'])} / "
            f"原文とずれたページ {len(preview['verbatim_breaches'])}){detail}"
        )

    run = run_id or uuid.uuid4().hex[:12]
    edit_source = f"{CONVERSION_SOURCE}:{run}"
    now = int(time.time())
    page_n = frag_n = dedup_n = 0

    conn.execute("BEGIN IMMEDIATE")
    try:
        # 書き込みロックを取ってから、判定の入力がまだ同じかを取り直して確かめる。
        # 検算とロック取得の間に割り込まれると、確かめた姿と違う本文へ書いてしまう。
        if conversion_fingerprint(conn) != expected_fingerprint:
            raise ValueError(
                "実行直前に対象ページの本文か編集来歴が変わりました"
                "（下見からやり直してください）。"
            )
        attested_all = attested_machine_lines(conn)
        moved_all = _already_moved_out(conn)
        frag_index = _existing_fragment_index(conn)
        for page_id, title, summary, content, _category in _target_pages(conn):
            split = split_page_body(
                content, _attestation_budget(attested_all, moved_all, page_id)
            )
            page_decisions = _page_decisions(decisions, page_id)
            drafts = split.taken(page_decisions)
            if not drafts:
                continue
            to_create, dedup = _partition_drafts(drafts, frag_index.get(page_id))
            body = split.render_body(page_decisions)

            # 変換前の姿を編集来歴へ。取り消しはここから復元する。
            old_text = f"title: {title}\nsummary: {summary or ''}\ncontent:\n{content}"
            new_text = f"title: {title}\nsummary: {summary or ''}\ncontent:\n{body}"
            record_page_edit(
                conn,
                page_id=page_id,
                diff_text=generate_diff(old_text, new_text),
                edit_type="update",
                edit_source=edit_source,
                before_title=title,
                before_summary=summary or "",
                before_content=content,
                commit=False,
            )
            conn.execute(
                "UPDATE memopedia_pages SET content = ?, updated_at = ? WHERE id = ?",
                (body, now, page_id),
            )
            conn.execute(
                "INSERT INTO memopedia_body_conversion "
                "(run_id, kind, ref_id, converted_at, digest, page_id) "
                "VALUES (?, 'page', ?, ?, ?, ?)",
                (run, page_id, now, _page_digest(title, summary, body, False), page_id),
            )
            page_n += 1

            for draft in to_create:
                frag_id = str(uuid.uuid4())
                # 列の並びは storage.create_fragment と同じ。あちらが正典で、ここは
                # 一括変換を 1 トランザクションに収めるための同型の書き込み。
                conn.execute(
                    "INSERT INTO memopedia_fragments "
                    "(id, content, entity_id, chronicle_entry_id, vividness, source_date, created_at) "
                    "VALUES (?, ?, ?, NULL, 'vivid', ?, ?)",
                    (frag_id, draft.content, page_id, draft.source_date or None, now),
                )
                conn.execute(
                    "INSERT INTO memopedia_body_conversion "
                    "(run_id, kind, ref_id, converted_at, digest, page_id, attest_digest) "
                    "VALUES (?, 'fragment', ?, ?, ?, ?, ?)",
                    (run, frag_id, now, _digest_of(draft.content), page_id,
                     _digest_of(_attest_key(draft.content))),
                )
                frag_n += 1
            for draft, existing_id in dedup:
                # 同じ内容の Fragment が既にある行 (移行期の二重書き込み)。新しくは
                # 作らず、本文から抜いた事実だけを台帳に刻む。ref_id は寄せた先の
                # 既存 Fragment —— 取り消しは本文の復元だけ行い、この Fragment には
                # 触らない (変換が作ったものではないから)。
                conn.execute(
                    "INSERT INTO memopedia_body_conversion "
                    "(run_id, kind, ref_id, converted_at, digest, page_id, attest_digest) "
                    "VALUES (?, 'dedup', ?, ?, ?, ?, ?)",
                    (run, existing_id, now, _digest_of(draft.content), page_id,
                     _digest_of(_attest_key(draft.content))),
                )
                dedup_n += 1
        conn.commit()
    except Exception:
        conn.rollback()
        LOGGER.exception("[body_to_fragment] 変換に失敗したため全て巻き戻しました run=%s", run)
        raise

    LOGGER.info(
        "[body_to_fragment] 変換完了 run=%s pages=%d fragments=%d dedup=%d "
        "(来歴確証 %d / 判断済 %d) marks=%d",
        run, page_n, frag_n, dedup_n, preview["confirmed_count"],
        preview["decided_count"], len(preview["marks"]),
    )
    return {
        "run_id": run,
        "page_count": page_n,
        "fragment_count": frag_n,
        "dedup_count": dedup_n,
        "confirmed_count": preview["confirmed_count"],
        "decided_count": preview["decided_count"],
        "pending_left": preview["pending_count"] - preview["decided_count"],
        "marks": preview["marks"],
    }


def _edited_fragments(
    conn: sqlite3.Connection, rows: List[tuple]
) -> List[Dict[str, Any]]:
    """変換で作ったあとに中身が編集された Fragment を洗う。

    Fragment の編集はページの編集来歴に残らないので、来歴だけを見ていると
    「後続の編集は無い」と判断して、編集内容ごと消してしまう
    (Codex 指摘 2026-08-05)。作った時点の指紋と突き合わせる。
    """
    edited: List[Dict[str, Any]] = []
    for row in rows:
        kind, ref_id, digest = row[0], row[1], row[2]
        if kind != "fragment" or not digest:
            continue
        current = conn.execute(
            "SELECT content FROM memopedia_fragments WHERE id = ?", (ref_id,)
        ).fetchone()
        if current is None:
            # 既に消えている。取り消しで復元できないので、黙って進めない。
            edited.append({"fragment_id": ref_id, "content": None, "missing": True})
            continue
        if _digest_of(current[0] or "") != digest:
            edited.append({"fragment_id": ref_id, "content": current[0]})
    return edited


def _later_edits(
    conn: sqlite3.Connection, page_rows: List[tuple], edit_source: str
) -> List[Dict[str, Any]]:
    """変換より後に本文が変わったページを洗う。

    判定の軸は **「変換が書いた姿の指紋」と現在の値の突き合わせ**。編集来歴を
    数える方式に頼らないのは、``Memopedia.update_page`` がページ更新と来歴を別々に
    commit するため、その隙間では「本文は変わったのに来歴が無い」状態が成立する
    から (Codex 指摘 2026-08-05)。他所の記帳の仕方に取り消しの安全を預けない。

    来歴は補助として併記する (誰が触ったかが分かると判断しやすい)。
    """
    found: List[Dict[str, Any]] = []
    for page_id, digest in page_rows:
        current = conn.execute(
            "SELECT title, summary, content, is_deleted FROM memopedia_pages WHERE id = ?",
            (page_id,),
        ).fetchone()
        if current is None:
            found.append({
                "page_id": page_id, "title": page_id,
                "edit_count": 0, "sources": "ページが存在しません",
            })
            continue
        if digest and _page_digest(*current) == digest:
            continue  # 変換が書いたままの姿

        anchor = conn.execute(
            "SELECT rowid FROM memopedia_page_edit_history "
            "WHERE page_id = ? AND edit_source = ? ORDER BY edited_at ASC, rowid ASC LIMIT 1",
            (page_id, edit_source),
        ).fetchone()
        sources = ""
        count = 0
        if anchor is not None:
            later = conn.execute(
                "SELECT COUNT(*), GROUP_CONCAT(DISTINCT COALESCE(edit_source, '(なし)')) "
                "FROM memopedia_page_edit_history "
                "WHERE page_id = ? AND rowid > ? AND COALESCE(edit_source, '') <> ?",
                (page_id, anchor[0], edit_source),
            ).fetchone()
            if later:
                count, sources = later[0] or 0, later[1] or ""
        found.append({
            "page_id": page_id,
            "title": current[0],
            "edit_count": count,
            "sources": sources or "(来歴に記録が無い書き換え)",
        })
    return found


def revert_conversion(
    conn: sqlite3.Connection, run_id: str, *, force: bool = False
) -> Dict[str, Any]:
    """変換を丸ごと取り消す (intent §5.4)。

    作った Fragment を消し、ページ本文を変換前へ戻す。個々の Fragment を人が
    点検して直すのではなく、実行単位でなかったことにする。

    **変換より後にそのページへ別の編集が入っていたら、既定では取り消さない。**
    変換前の本文へ戻すと、その後の編集ごと消える。さらに後続の別変換が作った
    Fragment はこの実行の記録に無いので消えず、同じ記述が本文と Fragment の
    両方に残る (Codex 指摘 2026-08-05)。何を失うかを示して、判断を仰ぐ。

    Args:
        force: 後続の編集ごと変換前へ戻してよいと、呼び出し側が明示したとき。

    Raises:
        ValueError: 記録が無い / 復元できない / 後続の編集があるのに
            ``force`` でない場合。
    """
    from sai_memory.memopedia.storage import generate_diff, record_page_edit

    init_body_conversion_table(conn)
    now = int(time.time())
    restored = 0
    blocked: List[Dict[str, Any]] = []
    conn.execute("BEGIN IMMEDIATE")
    try:
        # ⚠ 検査は必ず書き込みロックを取ってから。ロックの手前で調べると、
        # 調べてから消すまでの隙間に入った編集を見落として上書きする
        # (Codex 指摘 2026-08-05)。
        rows = conn.execute(
            "SELECT kind, ref_id, digest, page_id FROM memopedia_body_conversion "
            "WHERE run_id = ?",
            (run_id,),
        ).fetchall()
        if not rows:
            raise ValueError(f"変換の記録が見つかりません: run_id={run_id}")

        frag_ids = [r[1] for r in rows if r[0] == "fragment"]
        # dedup 行は「既存 Fragment へ寄せて本文から抜いた」記録。取り消しでは
        # 本文の復元 (page 行) に含まれて戻るだけで、寄せた先の Fragment は
        # 変換が作ったものではないから消さない。
        dedup_count = sum(1 for r in rows if r[0] == "dedup")
        page_rows = [(r[1], r[2]) for r in rows if r[0] == "page"]
        page_ids = [r[1] for r in rows if r[0] == "page"]
        edit_source = f"{CONVERSION_SOURCE}:{run_id}"

        # 見るのは **この実行の行だけ**。他の実行に古い行があっても、この実行の
        # 取り消しは塞がない —— 全操作を止めると、古い行を片づける手段まで
        # 無くなって行き止まりになる (Codex 指摘 2026-08-05)。
        incomplete = [r[1] for r in rows if not r[2] or not r[3]]
        if incomplete:
            raise ValueError(
                f"この実行の記録に、指紋かページを持たない古い行が {len(incomplete)} 件"
                "あります。取り消しの安全を確かめられないため中止しました"
                "（この実行は台帳から手で外すしかありません）。"
            )

        # 後続の変換 run がある実行は、force でも戻さない。本文だけ古い姿へ戻り、
        # 後続 run の Fragment と実行記録が孤立して二重化する (Codex 指摘 2026-08-05)。
        #
        # 「後続」の判定に converted_at を使わないこと。秒単位なので同じ秒に入った
        # 実行を取りこぼすし、逆に同秒の自分自身を後続と誤判定する。挿入順そのものを
        # 持つ rowid で見る (取り消しの編集検査と同じ流儀)。
        holes = ",".join("?" for _ in page_ids) or "''"
        later_runs = [
            r[0] for r in conn.execute(
                f"SELECT DISTINCT run_id FROM memopedia_body_conversion "
                f"WHERE page_id IN ({holes}) AND run_id <> ? "
                f"  AND rowid > (SELECT MAX(rowid) "
                f"      FROM memopedia_body_conversion WHERE run_id = ?)",
                (*page_ids, run_id, run_id),
            )
        ]
        if later_runs:
            raise ValueError(
                f"この変換のあとで別の変換が同じページを触っています"
                f"（{', '.join(later_runs[:5])}）。先に新しい方から取り消してください。"
            )

        blocked = _later_edits(conn, page_rows, edit_source)
        edited = _edited_fragments(conn, rows)
        if (blocked or edited) and not force:
            parts = []
            if blocked:
                names = "、".join(b["title"] for b in blocked[:5])
                parts.append(
                    f"{len(blocked)} ページに別の編集"
                    f"（{names}{' ほか' if len(blocked) > 5 else ''}）"
                )
            if edited:
                parts.append(f"{len(edited)} 件の Fragment に編集")
            raise ValueError(
                "この変換のあとで " + " と ".join(parts) + " が入っています。"
                "変換前へ戻すとその編集も消えるため、取り消しを中止しました。"
                "承知のうえで戻す場合だけ、強制指定で実行してください。"
            )

        # ⚠ 何かを消す前に、全ページを復元できることを確かめる。
        # 復元できないページを飛ばしながら Fragment と実行記録だけ消すと、
        # 本文は変換後のまま・Fragment は消滅・戻す手がかりも消滅、という
        # 記憶が失われた状態が「成功」として残る (Codex 指摘 2026-08-05)。
        snapshots: Dict[str, tuple] = {}
        for page_id in page_ids:
            snap = conn.execute(
                "SELECT before_title, before_summary, before_content "
                "FROM memopedia_page_edit_history "
                "WHERE page_id = ? AND edit_source = ? ORDER BY edited_at ASC, rowid ASC LIMIT 1",
                (page_id, edit_source),
            ).fetchone()
            current = conn.execute(
                "SELECT title, summary, content FROM memopedia_pages WHERE id = ?", (page_id,)
            ).fetchone()
            if snap is None or current is None:
                raise ValueError(
                    f"変換前の姿を復元できないページがあるため、取り消しを中止しました "
                    f"(page={page_id} / スナップショット={'あり' if snap else '無し'} / "
                    f"ページ={'あり' if current else '無し'})。"
                    "実行記録と Fragment はそのまま残してあります。"
                )
            snapshots[page_id] = (snap, current)

        for i in range(0, len(frag_ids), 500):
            chunk = frag_ids[i:i + 500]
            holes = ",".join("?" for _ in chunk)
            conn.execute(
                f"DELETE FROM memopedia_fragment_embeddings WHERE fragment_id IN ({holes})",
                chunk,
            )
            conn.execute(f"DELETE FROM memopedia_fragments WHERE id IN ({holes})", chunk)

        for page_id in page_ids:
            snap, current = snapshots[page_id]
            record_page_edit(
                conn,
                page_id=page_id,
                diff_text=generate_diff(
                    f"title: {current[0]}\nsummary: {current[1] or ''}\ncontent:\n{current[2] or ''}",
                    f"title: {snap[0]}\nsummary: {snap[1] or ''}\ncontent:\n{snap[2] or ''}",
                ),
                edit_type="rollback",
                edit_source=f"{CONVERSION_SOURCE}_revert:{run_id}",
                before_title=current[0],
                before_summary=current[1] or "",
                before_content=current[2] or "",
                commit=False,
            )
            # 変換対象は必ず is_deleted=0 のページなので、変換前の姿へ戻すとは
            # 閉架も解くこと。ここを落とすと、force で戻したページが本文だけ
            # 復元されて一覧から消えたまま残る (Codex 指摘 2026-08-05)。
            conn.execute(
                "UPDATE memopedia_pages "
                "SET title = ?, summary = ?, content = ?, is_deleted = 0, updated_at = ? "
                "WHERE id = ?",
                (snap[0], snap[1] or "", snap[2] or "", now, page_id),
            )
            restored += 1

        conn.execute("DELETE FROM memopedia_body_conversion WHERE run_id = ?", (run_id,))
        conn.commit()
    except ValueError:
        # 拒否 (復元不能・後続編集) は想定内。実行記録も Fragment も残す。
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        LOGGER.exception("[body_to_fragment] 取り消しに失敗したため巻き戻しました run=%s", run_id)
        raise

    if blocked:
        LOGGER.warning(
            "[body_to_fragment] 強制指定で、変換後の編集ごと戻しました run=%s pages=%s",
            run_id, [b["title"] for b in blocked],
        )
    LOGGER.info(
        "[body_to_fragment] 取り消し完了 run=%s pages=%d fragments=%d",
        run_id, restored, len(frag_ids),
    )
    return {
        "run_id": run_id,
        "restored_pages": restored,
        "deleted_fragments": len(frag_ids),
        "restored_dedup_lines": dedup_count,
        "overwritten_pages": blocked,
    }


def list_conversion_runs(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """取り消せる変換の一覧 (新しい順)。"""
    init_body_conversion_table(conn)
    return [
        {
            "run_id": row[0],
            "converted_at": row[1],
            "page_count": row[2],
            "fragment_count": row[3],
            "dedup_count": row[4],
        }
        for row in conn.execute(
            """
            SELECT run_id, MIN(converted_at),
                   SUM(CASE WHEN kind = 'page' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN kind = 'fragment' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN kind = 'dedup' THEN 1 ELSE 0 END)
            FROM memopedia_body_conversion
            GROUP BY run_id ORDER BY MIN(converted_at) DESC
            """
        ).fetchall()
    ]
