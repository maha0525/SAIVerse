"""手帳を開く (スペル ``pocketbook_open``)。

正典: docs/intent/autonomous_behavior_v3.md §13.2.1 (読み口)。

**手帳はペルソナ自身のもので、本人が開いたときだけ中身が見える** (現実の手帳と
同じ)。v0.3 で本人が手帳を読む機会はスルースのプロンプトに載るアクティビティ名の
一覧だけで、書き溜めたメモを本人が読み返す口が無かった — このスペルがその口。

**本人から見える手帳は一冊で、欄が二つ**:

- メモ欄「やりたい・やった」— 保存の器は memory.db の activities / memos
  (:mod:`sai_memory.memory.pocketbook`)
- 約束の欄 — 保存の器は中央 DB の task_book (:mod:`saiverse.task_book`)

**器の区別は機構が引き受ける** (本人には覚えさせない)。ユーザー向けの読み口
(メモリタブの「手帳」節 / ``GET /api/people/{id}/pocketbook`` と ``/task-book``)
と同じものを、同じ構成で本人へ見せる — ユーザーとペルソナが同じ手帳を見ている状態。

省略は「文字」ではなく「ページ」で行う: **一件の本文は絶対に途中で切らない**。
省略の単位は件数だけで、省略したときは必ず「何件が、どこから先に、どうやって
読めるか」を同じ返答に載せる (既定 30 件、めくる鍵は日付)。

見せるのは**開いているアクティビティ**とその配下のメモだけ (ユーザー向け読み口の
既定と同じ)。閉じたアクティビティを混ぜると、目次に無い名前のメモが「最近のページ」
に出て食い違う。v0.3 に閉じる口はまだ無い (`close_activity` の呼び手はテストだけ)
ので、いまはこれが全件と一致する — 閉じる口を作るときに、閉じたページの読み方も
一緒に決める。

読み取り専用 — 書き込み・LLM 呼び出し・Pulse 起動はしない。「テーブルがまだ無い」
(手帳を一度も書いていないペルソナ) だけを空として扱い、それ以外の読み取り失敗は
失敗のまま上げる (壊れた読みを「空の手帳」に丸めない)。
"""
from __future__ import annotations

import datetime
import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from tools.context import (
    get_active_manager,
    get_active_persona_id,
    open_persona_memory,
)
from tools.core import ToolSchema

#: 一度に出すメモの既定件数 (§13.2.1 — 省略の単位は件数のみ)。
DEFAULT_LIMIT = 30

#: メモ種類の表示名 (本人・ユーザーに見える語)。
_KIND_LABEL = {"want": "やりたい", "did": "やった"}

#: 約束の相手 (task_book.COUNTERPART) の表示名。既知の値だけ訳す。
_COUNTERPART_LABEL = {"user": "ユーザー", "system": "システム"}

# 引数の日付は ASCII の 'YYYY-MM-DD' だけを受ける ([0-9] 明記 — \d は全角数字も
# 通す)。格納される日付の正しさは書き込み側 (pocketbook._validate_date) が持ち、
# ここが見るのは「引数として解釈できるか」だけ。
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def _is_missing_table(exc: BaseException) -> bool:
    """「テーブルがまだ無い」だけを見分ける (他の読み取り失敗と混ぜない)。"""
    return "no such table" in str(exc).lower()


def _parse_date_arg(value: Any) -> Tuple[Optional[str], Optional[str]]:
    """``before`` 引数を検査する。(日付, エラー文) を返す。"""
    if value is None:
        return (None, None)
    if not isinstance(value, str) or not value.strip():
        return (None, f"before は 'YYYY-MM-DD' の日付で指定してください: {value!r}")
    raw = value.strip()
    if not _DATE_RE.match(raw):
        return (None, f"before は 'YYYY-MM-DD' の日付で指定してください: {raw}")
    try:
        datetime.date.fromisoformat(raw)
    except ValueError:
        return (None, f"before に暦に無い日付が指定されています: {raw}")
    return (raw, None)


def _parse_limit_arg(value: Any) -> Tuple[Optional[int], Optional[str]]:
    """``limit`` 引数を検査する。(件数, エラー文) を返す。"""
    if value is None:
        return (DEFAULT_LIMIT, None)
    if isinstance(value, bool):
        return (None, f"limit は 1 以上の整数で指定してください: {value!r}")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = int(value.strip())
        except ValueError:
            return (None, f"limit は 1 以上の整数で指定してください: {value!r}")
    else:
        return (None, f"limit は 1 以上の整数で指定してください: {value!r}")
    if parsed < 1:
        return (None, f"limit は 1 以上の整数で指定してください: {parsed}")
    return (parsed, None)


def _load_pocketbook(adapter) -> Tuple[List[Any], Dict[int, List[Any]]]:
    """開いているアクティビティと、そのメモを一度のロックで読む。

    「表が無い」(一度も手帳を書いていないペルソナ) だけを空として扱う。他の
    読み取り失敗は送出する — 空に丸めると「手帳が空」と「手帳が読めない」が
    本人から見て同じ顔になる。
    """
    from sai_memory.memory.pocketbook import list_activities, list_memos

    with adapter._db_lock:
        try:
            activities = list_activities(adapter.conn)
            memos = {a.id: list_memos(adapter.conn, a.id) for a in activities}
        except sqlite3.OperationalError as exc:
            if _is_missing_table(exc):
                return ([], {})
            raise
    return (activities, memos)


def _load_promises(persona_id: str, manager: Any) -> Optional[List[Dict[str, Any]]]:
    """開いている約束の一覧。タスク帳の器そのものが無い環境では None。

    None は「約束の欄を読めなかった」で、空リスト (= 開いている約束がゼロ) とは
    別物 — 読めなかったことを空に丸めない。
    """
    if manager is None or not hasattr(manager, "SessionLocal"):
        return None
    from saiverse.task_book import list_open

    try:
        return list_open(manager, persona_id)
    except Exception as exc:
        if _is_missing_table(exc):
            # 軽量シンク前の DB — タスク帳がまだ無い = 開いている約束はゼロ。
            return []
        raise


def _sort_newest_first(entries: List[Tuple[Any, str]]) -> List[Tuple[Any, str]]:
    """(メモ, アクティビティ名) を新しい順に並べる (日付 → id の降順)。"""
    return sorted(entries, key=lambda e: (e[0].date, e[0].id), reverse=True)


def _page(
    entries: List[Tuple[Any, str]],
    before: Optional[str],
    limit: int,
) -> Tuple[List[Tuple[Any, str]], int, Optional[str]]:
    """新しい順の一覧から 1 ページ切り出す。(ページ, 残り件数, めくる鍵の日付)。

    ページの切れ目は**日付の境目**に置く — ``limit`` 件目と同じ日付のメモは
    全部このページに入れる。めくる鍵が日付 (``before``) である以上、日付の
    途中で切ると次のページ (date < 鍵) が同日の残りを飛ばして、本人からは
    メモが黙って消えたように見える。切れ目を日付に合わせると、
    ``before=鍵`` が残り全部にちょうど一致する。
    """
    if before is not None:
        entries = [e for e in entries if e[0].date < before]
    if len(entries) <= limit:
        return (entries, 0, None)
    page = list(entries[:limit])
    pivot = page[-1][0].date
    index = limit
    while index < len(entries) and entries[index][0].date == pivot:
        page.append(entries[index])
        index += 1
    remaining = len(entries) - index
    if remaining <= 0:
        return (page, 0, None)
    return (page, remaining, pivot)


def _memo_line(memo: Any, activity_name: Optional[str]) -> str:
    """メモ 1 件の一行。本文は切らない (本人の言葉)。"""
    kind = _KIND_LABEL.get(memo.kind, memo.kind)
    if activity_name:
        return f"- {memo.date} [{kind}] {activity_name}: {memo.text}"
    return f"- {memo.date} [{kind}] {memo.text}"


def _more_line(remaining: int, pivot: str) -> str:
    return (
        f"さらに {remaining} 件、{pivot} より前にあります。"
        f"続きは before='{pivot}' で開けます。"
    )


def _promise_line(task: Dict[str, Any]) -> str:
    """約束 1 件の一行。中身は切らない (指示書であって要約ではない)。"""
    due_at = task.get("due_at")
    if due_at is None:
        due_label = "期限なし"
    else:
        try:
            due_label = "期限 " + datetime.datetime.fromtimestamp(
                due_at
            ).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            due_label = "期限 不明"
    counterpart = task.get("counterpart")
    if counterpart:
        who = _COUNTERPART_LABEL.get(counterpart, counterpart)
    else:
        who = "相手なし"
    return f"- {task.get('content')} ({due_label}、{who})"


def _promise_block(
    promises: Optional[List[Dict[str, Any]]], limit: int
) -> List[str]:
    lines = ["■ 約束の欄"]
    if promises is None:
        lines.append("（約束の欄を読めませんでした — タスク帳が利用できません）")
        return lines
    if not promises:
        lines.append("開いている約束はありません。")
        return lines
    # 並びは list_open と同じ作成順 — ユーザーが見ている画面 (メモリタブの
    # 「手帳」節) と同じ順に見せる。件数が少ない前提の欄だが、多いときは
    # メモと同じ規則で件数だけを削り、削ったことを必ず書く。
    lines.extend(_promise_line(t) for t in promises[:limit])
    if len(promises) > limit:
        lines.append(
            f"さらに {len(promises) - limit} 件の約束があります。"
            "limit を大きくすると続きが出ます。"
        )
    return lines


def pocketbook_open(
    activity: Optional[str] = None,
    before: Optional[str] = None,
    limit: int = DEFAULT_LIMIT,
) -> str:
    """自分の手帳を開いて読む (読み取り専用)。"""
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona is not set")

    before_date, error = _parse_date_arg(before)
    if error:
        return f"手帳を開けませんでした: {error}"
    page_limit, error = _parse_limit_arg(limit)
    if error:
        return f"手帳を開けませんでした: {error}"

    with open_persona_memory() as adapter:
        if not adapter.is_ready():
            raise RuntimeError(f"SAIMemory not ready for {persona_id}")
        activities, memos_by_activity = _load_pocketbook(adapter)

    if activity is not None and str(activity).strip():
        return _render_single_activity(
            str(activity).strip(), activities, memos_by_activity,
            before_date, page_limit,
        )

    promises = _load_promises(persona_id, get_active_manager())
    return _render_whole_book(
        activities, memos_by_activity, promises, before_date, page_limit,
    )


def _render_single_activity(
    name: str,
    activities: List[Any],
    memos_by_activity: Dict[int, List[Any]],
    before: Optional[str],
    limit: int,
) -> str:
    target = next((a for a in activities if a.name == name), None)
    if target is None:
        if not activities:
            return "手帳にはまだ何も書いていません。"
        names = "、".join(a.name for a in activities)
        return (
            f"手帳に「{name}」のページはありません。"
            f"いま開いているのは: {names}"
        )
    entries = _sort_newest_first(
        [(m, target.name) for m in memos_by_activity.get(target.id, [])]
    )
    lines = [f"【手帳】{target.name}"]
    if not entries:
        lines.append("このページにはまだ何も書いていません。")
        return "\n".join(lines)
    page, remaining, pivot = _page(entries, before, limit)
    if not page:
        lines.append(f"{before} より前のメモはありません。")
        return "\n".join(lines)
    lines.extend(_memo_line(m, None) for m, _ in page)
    if remaining and pivot:
        lines.append(_more_line(remaining, pivot))
    return "\n".join(lines)


def _render_whole_book(
    activities: List[Any],
    memos_by_activity: Dict[int, List[Any]],
    promises: Optional[List[Dict[str, Any]]],
    before: Optional[str],
    limit: int,
) -> str:
    lines: List[str] = ["【手帳】"]

    # 1) 目次 — 開いているアクティビティごとに件数と最後に書いた日。
    lines.append("")
    lines.append("■ 目次（メモ欄のアクティビティ）")
    if not activities:
        lines.append("手帳にはまだ何も書いていません。")
    else:
        for act in activities:
            memos = memos_by_activity.get(act.id, [])
            last_date = max((m.date for m in memos), default=None)
            if last_date is None:
                lines.append(f"- {act.name}（メモ 0 件、まだ書いていません）")
            else:
                lines.append(
                    f"- {act.name}（メモ {len(memos)} 件、"
                    f"最後に書いた日 {last_date}）"
                )

    # 2) 最近のページ — 全アクティビティ横断で新しい順。
    entries = _sort_newest_first([
        (memo, act.name)
        for act in activities
        for memo in memos_by_activity.get(act.id, [])
    ])
    lines.append("")
    lines.append("■ 最近のページ（メモ欄）")
    page, remaining, pivot = _page(entries, before, limit)
    if not page:
        if before is not None:
            lines.append(f"{before} より前のメモはありません。")
        else:
            lines.append("手帳にはまだ何も書いていません。")
    else:
        lines.extend(_memo_line(m, name) for m, name in page)
        if remaining and pivot:
            lines.append(_more_line(remaining, pivot))

    # 3) 約束の欄。
    lines.append("")
    lines.extend(_promise_block(promises, limit))
    return "\n".join(lines)


def schema() -> ToolSchema:
    return ToolSchema(
        name="pocketbook_open",
        description=(
            "自分の手帳を開いて読みます。手帳には「やりたい・やった」のメモ欄と、"
            "「約束」の欄があります。"
            "記憶の地図帳（memory_read など）は知っていること・あったことを引く"
            "場所で、手帳は自分のやりたいこと・やったこと・約束を書きとめる場所です。"
            "手帳は開いたときしか中身が見えません — 前に何を書いたか思い出したい"
            "ときは、このスペルで開いてください。"
            "引数なしで開くと、メモ欄の目次と最近のメモ、そして開いている約束が"
            "出ます。activity を指定するとそのアクティビティのメモだけ、"
            "before に日付を指定するとその日より前のメモをめくれます。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "activity": {
                    "type": "string",
                    "description": (
                        "アクティビティ名。指定するとそのアクティビティの"
                        "メモだけを出します（約束の欄は出ません）"
                    ),
                },
                "before": {
                    "type": "string",
                    "description": (
                        "この日付より前のメモをめくる（'YYYY-MM-DD'）。"
                        "省略すると新しい方から出ます"
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        f"一度に出すメモの件数（既定 {DEFAULT_LIMIT}）。"
                        "本文は切らず、件数だけで区切ります"
                    ),
                },
            },
            "required": [],
        },
        result_type="string",
        spell=True,
        spell_display_name="手帳を開く",
    )
