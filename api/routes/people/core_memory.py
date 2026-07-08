"""コア記憶 (記憶アーキ v2 ゾーン A) の探索・作成 API。

口調が安定しないペルソナに悩むユーザーが、「そのペルソナらしさが最も出た過去の
会話」を探して選び、ワンクリックでコア記憶 (scene) に刻めるようにするための導線。
仕様の出典: docs/intent/memory_architecture_v2.md §5「UI 導線」。

エンドポイント:
- GET  /{persona_id}/memory/messages/search        会話メッセージのキーワード検索
- GET  /{persona_id}/memory/messages/{id}/window   アンカー周辺の会話窓プレビュー
- POST /{persona_id}/core-memory/scene             scene を刻む (スペルとロジック共通化)
- GET  /{persona_id}/core-memory                   既存コア記憶の一覧 (読み取り専用)

scene 作成の窓切り出し・整形・保存は sai_memory.core_memory.create_scene_core_memory
に集約されており、スペル (core_memory_add_scene) と本 API が同じ関数を呼ぶ。
"""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.deps import get_manager
from builtin_data.tools._core_memory_common import (
    DEFAULT_CORE_MEMORY_CHAR_BUDGET,
    parse_message_ref,
)

from .utils import get_adapter

router = APIRouter()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
#
# ペルソナ表示名 / 容量目安は、リクエストの ``manager`` (SessionLocal) から直接
# 解決する。tools/context の resolve_* 系は ContextVar (Pulse 実行中のみセット
# される active manager) に依存しており、通常の REST リクエストでは None に
# フォールバックしてしまう (表示名が persona_id に、budget が既定値に落ちる)。


def _resolve_persona_name(manager, persona_id: str) -> str:
    """AINAME を manager.SessionLocal から解決する。取れなければ persona_id。"""
    session_factory = getattr(manager, "SessionLocal", None) if manager else None
    if not session_factory:
        return persona_id
    db = session_factory()
    try:
        from database.models import AI as AIModel
        ai = db.query(AIModel).filter_by(AIID=persona_id).first()
        if ai is None or not ai.AINAME:
            return persona_id
        return str(ai.AINAME)
    except Exception:
        return persona_id
    finally:
        db.close()


def _resolve_budget(manager, persona_id: str) -> int:
    """CORE_MEMORY_CHAR_BUDGET を manager.SessionLocal から解決する。未設定なら既定。"""
    session_factory = getattr(manager, "SessionLocal", None) if manager else None
    if not session_factory:
        return DEFAULT_CORE_MEMORY_CHAR_BUDGET
    db = session_factory()
    try:
        from database.models import AI as AIModel
        ai = db.query(AIModel).filter_by(AIID=persona_id).first()
        if ai is None:
            return DEFAULT_CORE_MEMORY_CHAR_BUDGET
        value = ai.CORE_MEMORY_CHAR_BUDGET
        if value is None or int(value) <= 0:
            return DEFAULT_CORE_MEMORY_CHAR_BUDGET
        return int(value)
    except Exception:
        return DEFAULT_CORE_MEMORY_CHAR_BUDGET
    finally:
        db.close()


def _parse_date_from(date_from: Optional[str]) -> Optional[int]:
    """YYYY-MM-DD → その日の開始 (00:00:00) の Unix 秒。解釈不能なら None。"""
    if not date_from:
        return None
    try:
        return int(datetime.strptime(date_from, "%Y-%m-%d").timestamp())
    except ValueError:
        return None


def _parse_date_to(date_to: Optional[str]) -> Optional[int]:
    """YYYY-MM-DD → その日の終端 (23:59:59) の Unix 秒。解釈不能なら None。"""
    if not date_to:
        return None
    try:
        return int(datetime.strptime(date_to, "%Y-%m-%d").timestamp()) + 86400 - 1
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 1. GET /{persona_id}/memory/messages/search
# ---------------------------------------------------------------------------

class MessageSearchHit(BaseModel):
    id: str
    role: str
    excerpt: str
    created_at: int


class MessageSearchResponse(BaseModel):
    keyword: str
    mode: str  # "keyword" | "semantic"
    total_hits: int
    hits: List[MessageSearchHit]


@router.get("/{persona_id}/memory/messages/search", response_model=MessageSearchResponse)
def search_conversation_messages(
    persona_id: str,
    keyword: str = "",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 20,
    manager=Depends(get_manager),
):
    """会話メッセージをキーワード (空白区切りで AND) で検索する。

    まず LIKE 検索 (会話本文のみ・新しい順) を行い、0 件かつキーワード指定ありなら
    unified_recall (search_messages のみ) によるセマンティックフォールバックを行う。
    期間指定はフォールバック結果には後段でフィルタをかける。レスポンスの ``mode`` で
    どちらの経路だったかを示す。
    """
    from sai_memory.memory.storage import (
        search_conversation_messages as storage_search,
    )
    from sai_memory.unified_recall import _keyword_excerpt

    keywords = [k for k in keyword.split() if k.strip()]
    date_from_ts = _parse_date_from(date_from)
    date_to_ts = _parse_date_to(date_to)
    lim = max(1, min(int(limit), 100))

    with get_adapter(persona_id, manager) as adapter:
        with adapter._db_lock:
            rows = storage_search(
                adapter.conn,
                keywords,
                date_from_ts=date_from_ts,
                date_to_ts=date_to_ts,
                limit=lim,
            )

        if rows or not keywords:
            hits = [
                MessageSearchHit(
                    id=m.id,
                    role=m.role,
                    excerpt=_keyword_excerpt(m.content or "", keywords),
                    created_at=m.created_at,
                )
                for m in rows
            ]
            return MessageSearchResponse(
                keyword=keyword.strip(),
                mode="keyword",
                total_hits=len(hits),
                hits=hits,
            )

        # --- セマンティックフォールバック (LIKE 0 件 & キーワードあり) ---
        if not adapter.can_embed():
            # 埋め込み不可なら空の keyword 結果を返す (フォールバック不能)。
            return MessageSearchResponse(
                keyword=keyword.strip(), mode="keyword", total_hits=0, hits=[],
            )

        from sai_memory.unified_recall import unified_recall

        with adapter._db_lock:
            recall_hits = unified_recall(
                adapter.conn,
                adapter.embedder,
                keyword.strip(),
                topk=lim * 3,
                search_chronicle=False,
                search_memopedia=False,
                search_fragments=False,
                search_messages=True,
                persona_id=persona_id,
            )

        sem_hits: List[MessageSearchHit] = []
        for h in recall_hits:
            if h.source_type != "message":
                continue
            created = h.start_time or 0
            if date_from_ts is not None and created < date_from_ts:
                continue
            if date_to_ts is not None and created > date_to_ts:
                continue
            sem_hits.append(
                MessageSearchHit(
                    id=h.source_id,
                    role=(h.title.split(" @ ")[0] if h.title else "unknown"),
                    excerpt=h.content or "",
                    created_at=created,
                )
            )
            if len(sem_hits) >= lim:
                break

        return MessageSearchResponse(
            keyword=keyword.strip(),
            mode="semantic",
            total_hits=len(sem_hits),
            hits=sem_hits,
        )


# ---------------------------------------------------------------------------
# 2. GET /{persona_id}/memory/messages/{message_id}/window
# ---------------------------------------------------------------------------

class WindowMessage(BaseModel):
    id: str
    speaker: str      # 発話者ラベル (ペルソナ名 or "あなた")
    role: str
    content: str
    date: str         # YYYY-MM-DD


class MessageWindowResponse(BaseModel):
    anchor_id: str
    rounds: int
    total_chars: int  # 切り抜き整形後のトランスクリプト文字数 (プレビュー用)
    messages: List[WindowMessage]


@router.get(
    "/{persona_id}/memory/messages/{message_id}/window",
    response_model=MessageWindowResponse,
)
def get_message_window(
    persona_id: str,
    message_id: str,
    rounds: int = 3,
    manager=Depends(get_manager),
):
    """アンカーメッセージ周辺の会話窓を返す (scene プレビュー用)。

    ``total_chars`` は実際に刻まれるトランスクリプト (``ラベル「原文」`` を改行連結)
    の文字数で、scene 作成後の合計文字数プレビューと一致する。
    """
    from sai_memory.core_memory import format_scene_transcript
    from sai_memory.memory.storage import get_conversation_window_around

    mid = parse_message_ref(message_id)
    if not mid:
        raise HTTPException(status_code=400, detail=f"message_id を解釈できません: {message_id}")

    try:
        rounds_int = int(rounds)
    except (TypeError, ValueError):
        rounds_int = 3
    if rounds_int <= 0:
        rounds_int = 3

    persona_name = _resolve_persona_name(manager, persona_id)

    with get_adapter(persona_id, manager) as adapter:
        with adapter._db_lock:
            window = get_conversation_window_around(adapter.conn, mid, rounds=rounds_int)

    if not window:
        raise HTTPException(
            status_code=404,
            detail="メッセージが見つからないか、実会話ではありません（ツール実行ログ等は対象外）。",
        )

    transcript = format_scene_transcript(window, persona_name)
    messages = []
    for m in window:
        is_persona = m.role in ("model", "assistant")
        messages.append(
            WindowMessage(
                id=m.id,
                speaker=persona_name if is_persona else "あなた",
                role=m.role,
                content=m.content,
                date=datetime.fromtimestamp(m.created_at).strftime("%Y-%m-%d"),
            )
        )

    return MessageWindowResponse(
        anchor_id=mid,
        rounds=rounds_int,
        total_chars=len(transcript),
        messages=messages,
    )


# ---------------------------------------------------------------------------
# 3. POST /{persona_id}/core-memory/scene
# ---------------------------------------------------------------------------

class CreateSceneRequest(BaseModel):
    anchor_id: str
    rounds: int = 3


class CreateSceneResponse(BaseModel):
    memory_id: int
    ref: str          # "c:N"
    message_count: int
    char_count: int   # この切り抜き単体の文字数
    total_chars: int  # 追加後のコア記憶合計文字数
    budget: int       # 目安文字数
    over_budget: bool
    date_start: str
    date_end: str


@router.post("/{persona_id}/core-memory/scene", response_model=CreateSceneResponse)
def create_scene(
    persona_id: str,
    request: CreateSceneRequest,
    manager=Depends(get_manager),
):
    """アンカー周辺の会話を scene としてコア記憶に刻む。

    窓切り出し・整形・保存はスペル (core_memory_add_scene) と共通の
    sai_memory.core_memory.create_scene_core_memory を呼ぶ (ロジック非複製)。
    """
    from sai_memory.core_memory import create_scene_core_memory

    mid = parse_message_ref(request.anchor_id)
    if not mid:
        raise HTTPException(
            status_code=400, detail=f"anchor_id を解釈できません: {request.anchor_id}"
        )

    try:
        rounds_int = int(request.rounds)
    except (TypeError, ValueError):
        rounds_int = 3
    if rounds_int <= 0:
        rounds_int = 3

    persona_name = _resolve_persona_name(manager, persona_id)

    with get_adapter(persona_id, manager) as adapter:
        with adapter._db_lock:
            result = create_scene_core_memory(
                adapter.conn, mid, rounds=rounds_int, persona_name=persona_name,
            )

    if result is None:
        raise HTTPException(
            status_code=404,
            detail="メッセージが見つからないか、実会話ではありません（ツール実行ログ等は対象外）。",
        )

    budget = _resolve_budget(manager, persona_id)
    return CreateSceneResponse(
        memory_id=result.memory_id,
        ref=f"c:{result.memory_id}",
        message_count=result.message_count,
        char_count=result.char_count,
        total_chars=result.total_chars,
        budget=budget,
        over_budget=result.total_chars > budget,
        date_start=result.date_start,
        date_end=result.date_end,
    )


# ---------------------------------------------------------------------------
# 4. GET /{persona_id}/core-memory
# ---------------------------------------------------------------------------

class CoreMemoryListItem(BaseModel):
    id: int
    ref: str          # "c:N"
    kind: str         # "note" | "scene"
    preview: str      # 内容の先頭 80 字 (既存 UI 互換のため残置)
    content: str      # 全文 (UI の展開表示用)
    char_count: int


class CoreMemoryListResponse(BaseModel):
    items: List[CoreMemoryListItem]
    total_chars: int
    budget: int
    over_budget: bool


@router.get("/{persona_id}/core-memory", response_model=CoreMemoryListResponse)
def list_core_memory(
    persona_id: str,
    manager=Depends(get_manager),
):
    """既存コア記憶の一覧 (読み取り専用)。編集はペルソナの領分のため UI からは追加のみ。"""
    from sai_memory.core_memory import (
        init_core_memory_table,
        list_core_memories,
        total_core_memory_chars,
    )

    with get_adapter(persona_id, manager) as adapter:
        with adapter._db_lock:
            init_core_memory_table(adapter.conn)
            items = list_core_memories(adapter.conn)
            total = total_core_memory_chars(adapter.conn)

    budget = _resolve_budget(manager, persona_id)
    return CoreMemoryListResponse(
        items=[
            CoreMemoryListItem(
                id=it.id,
                ref=it.ref,
                kind=it.kind,
                preview=(it.content[:80] + ("…" if len(it.content) > 80 else "")),
                content=it.content,
                char_count=len(it.content),
            )
            for it in items
        ],
        total_chars=total,
        budget=budget,
        over_budget=total > budget,
    )
