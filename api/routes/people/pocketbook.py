"""手帳 (アクティビティ + メモ) とタスク帳の読み口 — 読み取り専用 API。

正典: docs/intent/autonomous_behavior_v3.md §13 (手帳とスルース)。

目的は「見えないと直せない」。手帳とタスク帳はペルソナが自分で書く自己像の
一部で、スルースのたびに本人が読み返す。ユーザーから見えないと、間違った
約束や的外れな「やりたいこと」を抱え込んでもユーザーは気づけない — コア記憶に
メモリタブがあるのと同じ理屈で、閲覧の口をここに開ける。

エンドポイント:
- GET /{persona_id}/pocketbook   手帳 (activities + memos、memory.db)
- GET /{persona_id}/task-book    タスク帳の open 一覧 (中央 DB)

**v0.3 は読むだけ** — 訂正 (削除・編集) の口は置かない。手帳の器の作り直しと
タスク帳の CAS (楽観ロック) との整合が要るため、v0.4 で合わせて決める。

読み取り失敗のうち「テーブルがまだ無い」(手帳を一度も書いていないペルソナ、
軽量シンク前の DB) だけを空配列に倒す。それ以外の失敗は 500 のまま上げる —
壊れた読み取りを空に丸めると「手帳が空」と「手帳が読めない」が画面で同じ顔に
なり、ユーザーが直せなくなる。
"""
import logging
import sqlite3
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.exc import OperationalError as SAOperationalError

from api.deps import get_manager

from .utils import ensure_persona_exists, get_adapter

LOGGER = logging.getLogger(__name__)

router = APIRouter()


def _is_missing_table(exc: BaseException) -> bool:
    """「テーブルがまだ無い」だけを見分ける (他の読み取り失敗と混ぜない)。"""
    return "no such table" in str(exc).lower()


# ---------------------------------------------------------------------------
# 1. GET /{persona_id}/pocketbook — 手帳
# ---------------------------------------------------------------------------


class PocketbookMemo(BaseModel):
    """メモ一件 (§13.6 の memos)。

    本文 (``text``) はペルソナ本人の言葉なので切り詰めない (表示側で折り返す)。
    ``created_at`` に相当する列は memos に無い — 手帳は日粒度の記録で、時刻を
    持つのは日付 (``date``) だけ。
    """

    id: int
    date: str          # 'YYYY-MM-DD'
    kind: str          # 'did' (やった) | 'want' (やりたい)
    text: str
    span_start_id: Optional[str] = None
    span_end_id: Optional[str] = None


class PocketbookActivity(BaseModel):
    """アクティビティ一件 (§13.6 の activities) と、そのメモ (日付降順)。

    誕生の時刻は ``born_at`` (epoch 秒) で、activities に ``created_at`` 列は
    無い。``last_memo_date`` は「眠っている」の導出材料 (§13.1 — 列にしない)。
    """

    id: int
    name: str
    status: str        # 'open' | 'closed'
    # origin: 'sluice' (ペルソナ本人由来 — スルースの採取と手帳のスペルの両方) |
    # 'user' | 'initial' | 'migration'
    origin: str
    born_at: int
    closed_at: Optional[int] = None
    last_memo_date: Optional[str] = None
    memos: List[PocketbookMemo] = []


class PocketbookResponse(BaseModel):
    activities: List[PocketbookActivity]
    include_closed: bool


@router.get("/{persona_id}/pocketbook", response_model=PocketbookResponse)
def get_pocketbook(
    persona_id: str,
    include_closed: bool = False,
    manager=Depends(get_manager),
):
    """手帳を読む — アクティビティごとにメモを日付降順で束ねて返す。

    既定は開いているアクティビティだけ。``include_closed=true`` で閉じたものも
    含める (誕生順は変えない)。読み取り専用 — 書き込み・LLM 呼び出し・Pulse 起動は
    一切しない。
    """
    from sai_memory.memory.pocketbook import list_activities, list_memos

    with get_adapter(persona_id, manager) as adapter:
        try:
            with adapter._db_lock:
                activities = list_activities(
                    adapter.conn, include_closed=include_closed
                )
                rows: List[PocketbookActivity] = []
                for act in activities:
                    memos = list_memos(adapter.conn, act.id)
                    # 最終メモ日付 (§13.1 の「眠っている」の導出材料) は取得済みの
                    # メモから取る — get_last_memo_date と同じ集合の MAX(date) で、
                    # アクティビティ数ぶんの追加クエリを撃たない。
                    last_date = max((m.date for m in memos), default=None)
                    rows.append(
                        PocketbookActivity(
                            id=act.id,
                            name=act.name,
                            status=act.status,
                            origin=act.origin,
                            born_at=act.born_at,
                            closed_at=act.closed_at,
                            last_memo_date=last_date,
                            memos=[
                                PocketbookMemo(
                                    id=m.id,
                                    date=m.date,
                                    kind=m.kind,
                                    text=m.text,
                                    span_start_id=m.span_start_id,
                                    span_end_id=m.span_end_id,
                                )
                                # 日付降順 (新しいメモが上)。list_memos は
                                # (date, id) 昇順なので、その逆順が同順序の反転。
                                for m in reversed(memos)
                            ],
                        )
                    )
        except sqlite3.OperationalError as e:
            if _is_missing_table(e):
                # 手帳を一度も書いていないペルソナ = 空の手帳。
                LOGGER.debug(
                    "[pocketbook] tables not initialised for persona=%s", persona_id
                )
                return PocketbookResponse(
                    activities=[], include_closed=include_closed
                )
            raise HTTPException(status_code=500, detail=f"Pocketbook error: {e}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Pocketbook error: {e}")

    return PocketbookResponse(activities=rows, include_closed=include_closed)


# ---------------------------------------------------------------------------
# 2. GET /{persona_id}/task-book — タスク帳 (open な一件の一覧)
# ---------------------------------------------------------------------------


class TaskBookItem(BaseModel):
    """タスク帳の一件 (saiverse.task_book._to_dict の写し)。

    ``due_at`` の None は「期限なし」— 期限のない約束は正当な行 (§4.1)。
    """

    task_id: str
    persona_id: str
    content: str
    due_at: Optional[int] = None
    counterpart: Optional[str] = None
    origin: str
    origin_ref: Optional[str] = None
    status: str
    artifact_ref: Optional[str] = None
    outcome: Optional[str] = None
    created_at: Optional[int] = None
    closed_at: Optional[int] = None
    meta: Optional[Dict[str, Any]] = None
    idem_key: Optional[str] = None
    revision: Optional[int] = None


class TaskBookResponse(BaseModel):
    tasks: List[TaskBookItem]


@router.get("/{persona_id}/task-book", response_model=TaskBookResponse)
def get_task_book(persona_id: str, manager=Depends(get_manager)):
    """タスク帳の open な一件を作成順に返す (読み取り専用)。"""
    from saiverse import task_book

    ensure_persona_exists(persona_id, manager)
    try:
        rows = task_book.list_open(manager, persona_id)
    except SAOperationalError as e:
        if _is_missing_table(e):
            # 軽量シンク前の DB — タスク帳がまだ無い = 開いている約束はゼロ。
            LOGGER.debug(
                "[pocketbook] task_book table missing for persona=%s", persona_id
            )
            return TaskBookResponse(tasks=[])
        raise HTTPException(status_code=500, detail=f"Task book error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Task book error: {e}")

    return TaskBookResponse(tasks=[TaskBookItem(**r) for r in rows])
