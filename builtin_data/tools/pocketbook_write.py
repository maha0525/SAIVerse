"""手帳に書く (スペル ``pocketbook_write``)。

正典: docs/intent/autonomous_behavior_v3.md §13.2.1 (読み口)。

:mod:`builtin_data.tools.pocketbook_open` の対。**本人から見える手帳は一冊で、
欄が二つ** — 「やりたい・やった」のメモ欄と「約束」の欄。本人が選ぶのは
**種類だけ** (``kind`` = want / did / promise) で、どの器へ書くかは機構が振り分ける:

- ``want`` / ``did`` → memory.db の activities / memos
  (:mod:`sai_memory.memory.pocketbook`)
- ``promise`` → 中央 DB の task_book (:mod:`saiverse.task_book`)

**期限は相手から言われたときだけ**書く (期限を発明しない — 2026-08-19 まはー裁定)。
期限の文字列の読み方は :func:`sea.sluice.parse_due` 一箇所が持つ — 同じ
'YYYY-MM-DD' がスルース経由とスペル経由で別の epoch にならないため。

本文 (``text``) はペルソナ本人の言葉なので切り詰めない。
"""
from __future__ import annotations

from typing import Any, Optional, Tuple

from tools.context import (
    get_active_manager,
    get_active_persona_id,
    open_persona_memory,
)
from tools.core import ToolSchema

#: 本人が選ぶ種類 (器ではなく意味)。
MEMO_KINDS = ("want", "did")
PROMISE_KIND = "promise"
WRITE_KINDS = MEMO_KINDS + (PROMISE_KIND,)

_KIND_LABEL = {"want": "やりたい", "did": "やった"}

#: メモ欄のアクティビティを本人が立てたときの出自。activities.origin は閉語彙
#: (:data:`sai_memory.memory.pocketbook.ACTIVITY_ORIGINS`) で、そのうち
#: 「ペルソナ本人が書いた」を表すのがこの値 (読み口 UI の表示も
#: sluice = 「ペルソナが書いた」)。閉語彙を増やさずに本人由来を表す。
_ACTIVITY_ORIGIN = "sluice"

#: 約束をペルソナ本人が書いたときの出自。task_book の origin は開語彙
#: (検査に使わない — 書き手が増えたら増える) なので、書き手ごとに名前を持つ。
_PROMISE_ORIGIN = "persona"

#: 約束の相手の既定 (スルースの適用側と同じ — 本人の約束の相手はまずユーザー)。
_DEFAULT_COUNTERPART = "user"

_COUNTERPART_LABEL = {"user": "ユーザー", "system": "システム"}


def _today() -> str:
    """メモの日付 (YYYY-MM-DD)。仮想クロックを尊重する。"""
    from saiverse import clock

    return clock.now().date().isoformat()


def _write_memo(kind: str, text: str, activity_name: str) -> str:
    """メモ欄へ一行書く。アクティビティが無ければ立てる。"""
    from sai_memory.memory.pocketbook import add_memo, get_or_create_activity

    today = _today()
    with open_persona_memory() as adapter:
        if not adapter.is_ready():
            raise RuntimeError("SAIMemory not ready")
        with adapter._db_lock:
            conn = adapter.conn
            try:
                # アクティビティの用意とメモの追加を一つの束にする — 途中で
                # 失敗したときに、メモの無いアクティビティだけが残らない。
                activity = get_or_create_activity(
                    conn, activity_name, _ACTIVITY_ORIGIN, commit=False,
                )
                add_memo(conn, activity.id, today, kind, text, commit=False)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
    return (
        f"手帳に書きました: {today} [{_KIND_LABEL[kind]}] "
        f"{activity.name}: {text}"
    )


def _write_promise(
    persona_id: str,
    text: str,
    counterpart: Optional[str],
    due: Optional[str],
) -> str:
    """約束の欄へ一件書く (器はタスク帳)。"""
    from sea.sluice import parse_due
    from saiverse.task_book import TaskBookError, add_entry

    manager = get_active_manager()
    if manager is None or not hasattr(manager, "SessionLocal"):
        return "手帳の約束の欄に書けませんでした: いま約束の欄を開けません。"

    if due is None:
        due_raw = ""
    elif isinstance(due, str):
        due_raw = due.strip()
    else:
        return (
            "手帳の約束の欄に書けませんでした: due は 'YYYY-MM-DD' の文字列で"
            f"書いてください: {due!r}"
        )
    due_at, due_error = parse_due(due_raw)
    if counterpart is None:
        counterpart_value: Optional[str] = _DEFAULT_COUNTERPART
    else:
        counterpart_value = counterpart

    try:
        add_entry(
            manager, persona_id, text,
            origin=_PROMISE_ORIGIN,
            due_at=due_at,
            counterpart=counterpart_value,
        )
    except TaskBookError as exc:
        if "期限も相手もない" in str(exc):
            # 器の受け入れ不変条件 (§4.1) を、器の名前を出さずに本人の語彙で返す。
            return (
                "手帳の約束の欄に書けませんでした: 期限も相手もない一件は、"
                "約束ではなく「やりたいこと」です。相手がいるなら counterpart を、"
                "日が決まっているなら due を書いてください。"
                "どちらも無いなら kind='want' でメモ欄に書けます。"
            )
        return f"手帳の約束の欄に書けませんでした: {exc}"

    if due_at is not None:
        due_label = f"期限 {due_raw}"
    else:
        due_label = "期限なし"
    who = _COUNTERPART_LABEL.get(
        (counterpart_value or "").strip(), (counterpart_value or "").strip()
    ) or "相手なし"
    message = f"手帳の約束の欄に書きました: {text}（{due_label}、{who}）"
    if due_error is not None:
        # 約束は失わない (2026-08-19 まはー裁定 — タスク帳の芯) ので、期限だけを
        # 落として書き、落としたことを本人へ明示する。
        message += (
            f"\n期限『{due_raw}』を日付として読めなかったので、期限なしで"
            "書きました。日付が決まっているなら 'YYYY-MM-DD' で書き直してください。"
        )
    return message


def _open_activity_names() -> Tuple[str, ...]:
    """開いているアクティビティ名 (引数不足のときの案内用)。"""
    from sai_memory.memory.pocketbook import list_activities

    with open_persona_memory() as adapter:
        if not adapter.is_ready():
            return ()
        with adapter._db_lock:
            return tuple(a.name for a in list_activities(adapter.conn))


def pocketbook_write(
    kind: str,
    text: str,
    activity: Optional[str] = None,
    counterpart: Optional[str] = None,
    due: Optional[str] = None,
) -> str:
    """自分の手帳に一行書く (種類で欄が決まる)。"""
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona is not set")

    kind_value = (kind or "").strip() if isinstance(kind, str) else kind
    if kind_value not in WRITE_KINDS:
        return (
            "手帳に書けませんでした: kind は want（やりたい）/ did（やった）/ "
            f"promise（約束）のどれかで指定してください: {kind!r}"
        )
    if not isinstance(text, str) or not text.strip():
        return "手帳に書けませんでした: 書く内容 (text) が空です。"
    body = text.strip()

    if kind_value == PROMISE_KIND:
        return _write_promise(persona_id, body, counterpart, due)

    activity_name = activity.strip() if isinstance(activity, str) else ""
    if not activity_name:
        names = _open_activity_names()
        hint = (
            "いま開いているのは: " + "、".join(names)
            if names else "まだアクティビティはありません（新しく立てられます）"
        )
        return (
            "手帳に書けませんでした: やりたい・やったのメモには、どの活動の"
            f"ことかを activity で書いてください。{hint}"
        )
    return _write_memo(kind_value, body, activity_name)


def schema() -> ToolSchema:
    return ToolSchema(
        name="pocketbook_write",
        description=(
            "自分の手帳に一行書きます。やりたいこと・やったことはメモ欄へ、"
            "誰かとの約束や引き受けた頼まれごとは約束の欄へ入ります"
            "（kind で選ぶと、どちらの欄に入るかは自動で決まります）。"
            "どちらか迷ったら、相手がいるなら約束です。"
            "記憶の地図帳（memory_write）は知ったこと・覚えておくことを書く場所で、"
            "手帳は自分の行動の記録です。"
            "期限（due）は相手から言われたときだけ書いてください — "
            "言われていない期限を自分で決めないでください。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": list(WRITE_KINDS),
                    "description": (
                        "want=やりたい（これからやりたいこと） / "
                        "did=やった（実際にやったこと） / "
                        "promise=約束（誰かと交わした約束・引き受けた頼まれごと）"
                    ),
                },
                "text": {
                    "type": "string",
                    "description": "書きたい一行（自分の言葉で。切り詰められません）",
                },
                "activity": {
                    "type": "string",
                    "description": (
                        "want / did のときの活動の名前（「小説を書く」「絵の練習」"
                        "のような粒度）。まだ無い名前なら新しく立ちます。"
                        "promise では使いません"
                    ),
                },
                "counterpart": {
                    "type": "string",
                    "description": (
                        "promise のときの相手（既定はユーザー）。"
                        "ユーザーとの約束ならそのままで構いません"
                    ),
                },
                "due": {
                    "type": "string",
                    "description": (
                        "promise の期限（'YYYY-MM-DD'）。"
                        "相手から言われたときだけ書いてください"
                    ),
                },
            },
            "required": ["kind", "text"],
        },
        result_type="string",
        spell=True,
        spell_display_name="手帳に書く",
    )
