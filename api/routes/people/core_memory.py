"""コア記憶 (記憶アーキ v2 ゾーン A) の探索・作成 API。

口調が安定しないペルソナに悩むユーザーが、「そのペルソナらしさが最も出た過去の
会話」を探して選び、ワンクリックでコア記憶 (scene) に刻めるようにするための導線。
仕様の出典: docs/intent/memory_architecture_v2.md §5「UI 導線」。

エンドポイント:
- GET    /{persona_id}/memory/messages/search        会話メッセージのキーワード検索
- GET    /{persona_id}/memory/messages/{id}/window   アンカー周辺の会話窓プレビュー
- POST   /{persona_id}/core-memory/scene             scene を刻む (スペルとロジック共通化)
- GET    /{persona_id}/core-memory                   生存コア記憶の一覧 (未確認フラグ付き)
- GET    /{persona_id}/core-memory/trash             ごみ箱 (soft-delete 済み) の一覧
- POST   /{persona_id}/core-memory/{id}/confirm      未確認 (自動採取) 項目をユーザーが確認
- PUT    /{persona_id}/core-memory/{id}              本文をユーザーが訂正 (確認済みになる)
- DELETE /{persona_id}/core-memory/{id}              soft-delete (ごみ箱へ)
- POST   /{persona_id}/core-memory/{id}/restore      ごみ箱から復元

scene 作成の窓切り出し・整形・保存は sai_memory.core_memory.create_scene_core_memory
に集約されており、スペル (memory_clip mode='transcribe' paste_to='core'、旧
core_memory_add_scene) と本 API が同じ関数を呼ぶ。

訂正導線 (confirm/edit/delete/restore) は gold_panning の自動採取 (confirmed=0) を
含むコア記憶をユーザーが後追いで直せるようにするための面。ユーザーの edit/delete/
restore は「仮想センサー」(_notify_persona_correction) でペルソナへ event_message
通知する — 記憶を黙って書き換えず本人が気づける形にして自己像の尊厳を保つ
(docs/intent/memory_architecture_v2.md §5.1)。confirm は内容不変なので通知しない。
"""
import logging
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

LOGGER = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# 仮想センサー: ユーザーの訂正を知覚バッファへ積む (Pulse 消費時にペルソナが知覚)
# ---------------------------------------------------------------------------
#
# ユーザーがコア記憶を書き換え・削除・復元したとき、その事実をペルソナ本人が次に
# 考えるときに気づける形で残す。ペルソナの記憶を黙って書き換えるのではなく、本人が
# 検知できるようにすることで自己像の尊厳を保つ (docs/intent/memory_architecture_v2.md
# §5.1)。confirm (承認) は内容が変わらないので通知しない。
#
# 【知覚バッファ経由に変更 (2026-07-09)】REST 文脈で発生したこの訂正は「非状態
# イベント」として知覚バッファへ push するだけ。実際に SAIMemory へ入る (= ペルソナ
# が知覚する) のは次の Pulse 開始時の flush で、そこで型別 reduce され 1 メッセージに
# まとまる。これにより一括操作 (ごみ箱整理・連続修正) でも通知が会話文脈を埋めない。
# 詳細: docs/intent/perception_buffer.md (Phase 1 の最初の利用者)。
#
# reduce_key に同一コア記憶の参照を渡すことで、同じ記憶への複数操作は最新 1 件に
# 集約される (未消費バッファ内での型別 reduce)。


def _notify_persona_correction(adapter, notice: str, *, reduce_key: Optional[str] = None) -> None:
    """訂正 1 件を知覚バッファへ積む (Pulse 消費時に event_message として知覚される)。

    ``notice`` はペルソナに見せる本文。push 失敗は API 応答を妨げない
    (WARNING に落として続行 — 通知はメインの DB 反映より優先度が低い)。
    """
    if adapter is None or not getattr(adapter, "is_ready", lambda: False)():
        return
    try:
        adapter.push_perception(
            "core_memory_correction", notice, reduce_key=reduce_key,
        )
    except Exception:
        LOGGER.warning("[core_memory] correction perception push failed", exc_info=True)


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
    ref: str          # "core:N"
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

    窓切り出し・整形・保存はスペル (memory_clip mode='transcribe' paste_to='core'、
    旧 core_memory_add_scene) と共通の sai_memory.core_memory.create_scene_core_memory
    を呼ぶ (ロジック非複製)。
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
        ref=f"core:{result.memory_id}",
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
    ref: str          # "core:N"
    kind: str         # "note" | "scene"
    preview: str      # 内容の先頭 80 字 (既存 UI 互換のため残置)
    content: str      # 全文 (UI の展開表示用)
    char_count: int
    confirmed: int    # 1=確認済み / 0=未確認 (自動採取・ユーザー確認待ち)
    created_at: int
    updated_at: int
    deleted_at: Optional[int] = None  # 生存一覧では常に None、ごみ箱一覧で埋まる


class CoreMemoryListResponse(BaseModel):
    items: List[CoreMemoryListItem]
    total_chars: int
    budget: int
    over_budget: bool
    unconfirmed_count: int  # 未確認 (confirmed=0) の生存件数。チャットのバッジ用。


def _to_list_item(it) -> "CoreMemoryListItem":
    return CoreMemoryListItem(
        id=it.id,
        ref=it.ref,
        kind=it.kind,
        preview=(it.content[:80] + ("…" if len(it.content) > 80 else "")),
        content=it.content,
        char_count=len(it.content),
        confirmed=it.confirmed,
        created_at=it.created_at,
        updated_at=it.updated_at,
        deleted_at=it.deleted_at,
    )


@router.get("/{persona_id}/core-memory", response_model=CoreMemoryListResponse)
def list_core_memory(
    persona_id: str,
    manager=Depends(get_manager),
):
    """生存中のコア記憶を一覧する (未確認フラグ付き)。訂正・確認・削除は各変更系 API で。"""
    from sai_memory.core_memory import (
        count_unconfirmed_core_memories,
        init_core_memory_table,
        list_core_memories,
        total_core_memory_chars,
    )

    with get_adapter(persona_id, manager) as adapter:
        with adapter._db_lock:
            init_core_memory_table(adapter.conn)
            items = list_core_memories(adapter.conn)
            total = total_core_memory_chars(adapter.conn)
            unconfirmed = count_unconfirmed_core_memories(adapter.conn)

    budget = _resolve_budget(manager, persona_id)
    return CoreMemoryListResponse(
        items=[_to_list_item(it) for it in items],
        total_chars=total,
        budget=budget,
        over_budget=total > budget,
        unconfirmed_count=unconfirmed,
    )


# ---------------------------------------------------------------------------
# 5. GET /{persona_id}/core-memory/trash  — ごみ箱 (soft-delete 済み)
# ---------------------------------------------------------------------------


@router.get("/{persona_id}/core-memory/trash", response_model=CoreMemoryListResponse)
def list_core_memory_trash(
    persona_id: str,
    manager=Depends(get_manager),
):
    """ごみ箱 (soft-delete 済み) のコア記憶を削除の新しい順に一覧する。

    容量目安 (total_chars / budget / over_budget) は生存分のみで計算する
    (削除済みは容量に数えない)。``unconfirmed_count`` も生存分の値。
    """
    from sai_memory.core_memory import (
        count_unconfirmed_core_memories,
        init_core_memory_table,
        list_deleted_core_memories,
        total_core_memory_chars,
    )

    with get_adapter(persona_id, manager) as adapter:
        with adapter._db_lock:
            init_core_memory_table(adapter.conn)
            items = list_deleted_core_memories(adapter.conn)
            total = total_core_memory_chars(adapter.conn)
            unconfirmed = count_unconfirmed_core_memories(adapter.conn)

    budget = _resolve_budget(manager, persona_id)
    return CoreMemoryListResponse(
        items=[_to_list_item(it) for it in items],
        total_chars=total,
        budget=budget,
        over_budget=total > budget,
        unconfirmed_count=unconfirmed,
    )


# ---------------------------------------------------------------------------
# 6. 変更系 (confirm / edit / delete / restore)
# ---------------------------------------------------------------------------
#
# いずれも「ユーザーによる訂正操作」。将来この面に仮想センサー (event_message で
# ペルソナへ「あなたのコア記憶がユーザーに直されました」を通知) をフックする
# (docs/intent/memory_architecture_v2.md §5)。今は DB 反映のみ。


class CoreMemoryMutationResponse(BaseModel):
    """変更系の共通レスポンス。UI がヘッダ数値とバッジを追加取得なしで更新できる。"""
    ok: bool
    total_chars: int
    budget: int
    over_budget: bool
    unconfirmed_count: int


class UpdateCoreMemoryRequest(BaseModel):
    content: str


def _mutation_response(adapter, manager, persona_id: str) -> "CoreMemoryMutationResponse":
    """変更後の容量・未確認件数を集計して共通レスポンスを組む (呼び出し側で lock 取得済み)。"""
    from sai_memory.core_memory import (
        count_unconfirmed_core_memories,
        total_core_memory_chars,
    )

    total = total_core_memory_chars(adapter.conn)
    unconfirmed = count_unconfirmed_core_memories(adapter.conn)
    budget = _resolve_budget(manager, persona_id)
    return CoreMemoryMutationResponse(
        ok=True,
        total_chars=total,
        budget=budget,
        over_budget=total > budget,
        unconfirmed_count=unconfirmed,
    )


@router.post(
    "/{persona_id}/core-memory/{memory_id}/confirm",
    response_model=CoreMemoryMutationResponse,
)
def confirm_core_memory_item(
    persona_id: str,
    memory_id: int,
    manager=Depends(get_manager),
):
    """未確認 (自動採取) のコア記憶をユーザーが「確認済み」にする。"""
    from sai_memory.core_memory import confirm_core_memory, init_core_memory_table

    with get_adapter(persona_id, manager) as adapter:
        with adapter._db_lock:
            init_core_memory_table(adapter.conn)
            ok = confirm_core_memory(adapter.conn, memory_id)
            if not ok:
                raise HTTPException(
                    status_code=404,
                    detail=f"core:{memory_id} が見つからないか、既に削除されています。",
                )
            return _mutation_response(adapter, manager, persona_id)


@router.put(
    "/{persona_id}/core-memory/{memory_id}",
    response_model=CoreMemoryMutationResponse,
)
def update_core_memory_item(
    persona_id: str,
    memory_id: int,
    request: UpdateCoreMemoryRequest,
    manager=Depends(get_manager),
):
    """コア記憶の本文をユーザーが訂正する。訂正した時点で確認済み (confirmed=1) になる。"""
    from sai_memory.core_memory import init_core_memory_table, update_core_memory

    content = (request.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="本文が空です。")

    with get_adapter(persona_id, manager) as adapter:
        with adapter._db_lock:
            init_core_memory_table(adapter.conn)
            # ユーザーが目を通して直した = 確認済みに倒す。
            ok = update_core_memory(adapter.conn, memory_id, content, confirmed=1)
            if not ok:
                raise HTTPException(
                    status_code=404,
                    detail=f"core:{memory_id} が見つかりません。",
                )
            resp = _mutation_response(adapter, manager, persona_id)
        # 仮想センサー: 内容が書き換わった事実をペルソナへ通知 (ロック外)。
        _notify_persona_correction(
            adapter,
            f"ユーザーがあなたのコア記憶 core:{memory_id} の内容を書き換えました。\n"
            f"新しい内容:\n{content}",
            reduce_key=f"core:{memory_id}",
        )
        return resp


@router.delete(
    "/{persona_id}/core-memory/{memory_id}",
    response_model=CoreMemoryMutationResponse,
)
def delete_core_memory_item(
    persona_id: str,
    memory_id: int,
    manager=Depends(get_manager),
):
    """コア記憶を soft-delete する (ごみ箱へ)。物理削除はせず復元可能に残す。"""
    from sai_memory.core_memory import (
        get_core_memory,
        init_core_memory_table,
        remove_core_memory,
    )

    with get_adapter(persona_id, manager) as adapter:
        with adapter._db_lock:
            init_core_memory_table(adapter.conn)
            # 削除前に内容を控える (head から消えるので、何が失われたかを通知に載せる)。
            item = get_core_memory(adapter.conn, memory_id)
            ok = remove_core_memory(adapter.conn, memory_id)
            if not ok:
                raise HTTPException(
                    status_code=404,
                    detail=f"core:{memory_id} が見つからないか、既に削除されています。",
                )
            resp = _mutation_response(adapter, manager, persona_id)
        removed = item.content if item else ""
        # 仮想センサー: 削除された事実と内容をペルソナへ通知 (ロック外)。
        _notify_persona_correction(
            adapter,
            f"ユーザーがあなたのコア記憶 core:{memory_id} を削除しました（ごみ箱へ移動。復元可能）。\n"
            f"削除された内容:\n{removed}",
            reduce_key=f"core:{memory_id}",
        )
        return resp


@router.post(
    "/{persona_id}/core-memory/{memory_id}/restore",
    response_model=CoreMemoryMutationResponse,
)
def restore_core_memory_item(
    persona_id: str,
    memory_id: int,
    manager=Depends(get_manager),
):
    """ごみ箱からコア記憶を復元する。"""
    from sai_memory.core_memory import (
        get_core_memory,
        init_core_memory_table,
        restore_core_memory,
    )

    with get_adapter(persona_id, manager) as adapter:
        with adapter._db_lock:
            init_core_memory_table(adapter.conn)
            ok = restore_core_memory(adapter.conn, memory_id)
            if not ok:
                raise HTTPException(
                    status_code=404,
                    detail=f"core:{memory_id} はごみ箱にありません。",
                )
            item = get_core_memory(adapter.conn, memory_id)
            resp = _mutation_response(adapter, manager, persona_id)
        restored = item.content if item else ""
        # 仮想センサー: 復元された事実をペルソナへ通知 (ロック外)。
        _notify_persona_correction(
            adapter,
            f"ユーザーがあなたのコア記憶 core:{memory_id} をごみ箱から復元しました。\n"
            f"内容:\n{restored}",
            reduce_key=f"core:{memory_id}",
        )
        return resp
