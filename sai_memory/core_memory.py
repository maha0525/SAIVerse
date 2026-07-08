"""コア記憶 (記憶アーキv2 ゾーン A) のストレージ層。

コア記憶＝ペルソナが**自分で選んで刻む恒常知識**。head (システムプロンプト部) に
常駐し、Metabolism 時のみ更新が反映される。編集主体はペルソナ自身 (専用スペル
core_memory_add / core_memory_update / core_memory_remove)。システムは容量目安を
超過しても絶対に切り詰めない (通知のみ)。

ペルソナへの提示時は ``c:{id}`` 形式で参照する (Memopedia の ``m:N`` と同じ操作感)。

テーブルはペルソナの memory.db に同居する (Memopedia / Chronicle と同じ conn)。
``init_core_memory_table`` は SAIMemoryAdapter の初期化時に冪等に呼ばれる。

詳細設計: docs/intent/memory_architecture_v2.md §5
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional


@dataclass(frozen=True)
class CoreMemory:
    """コア記憶 1 件。

    ``kind`` は項目種別。今回実装するのは 'note' (書き下ろしテキスト) のみだが、
    直後の増分で 'scene' (実会話の切り抜き＝口調・性格のアンカー) が入る予定。
    ``metadata`` は scene の由来参照 (元 message_id 群・日付等) などが将来入る余白。
    """
    id: int
    content: str
    created_at: int
    updated_at: int
    kind: str = "note"
    metadata: Optional[str] = None
    confirmed: int = 1
    deleted_at: Optional[int] = None

    @property
    def ref(self) -> str:
        """ペルソナ提示用の参照 (例: ``c:3``)。"""
        return f"c:{self.id}"


def init_core_memory_table(conn: sqlite3.Connection) -> None:
    """コア記憶テーブルを初期化する (冪等)。

    ``kind`` / ``metadata`` は将来拡張 (scene 種別・由来参照) 用に最初から DDL に
    含める。新設テーブルなので今入れておけば将来のマイグレーションが不要になる。
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS core_memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            kind TEXT NOT NULL DEFAULT 'note',
            metadata TEXT,
            confirmed INTEGER NOT NULL DEFAULT 1,
            deleted_at INTEGER
        )
        """
    )
    # 既存 DB 向けの追加系マイグレーション (memopedia_pages と同方式)。
    # confirmed: 0=自動採取の未確認 / 1=確認済み。既存行は 1 (確認済み扱い)。
    # deleted_at: soft-delete (ごみ箱) 用。NULL=生存。
    for ddl in (
        "confirmed INTEGER NOT NULL DEFAULT 1",
        "deleted_at INTEGER",
    ):
        try:
            conn.execute(f"ALTER TABLE core_memories ADD COLUMN {ddl}")
        except sqlite3.OperationalError:
            pass  # 既に存在する
    conn.commit()


def add_core_memory(
    conn: sqlite3.Connection,
    content: str,
    *,
    kind: str = "note",
    metadata: Optional[str] = None,
    confirmed: int = 1,
) -> int:
    """コア記憶を1件追加し、採番された id を返す。

    ``kind`` / ``metadata`` は 'scene' (実会話の切り抜き) / 由来参照用。
    ``confirmed`` は 0 で「未確認 (自動採取)」= ユーザーの確認待ち。ペルソナ自身や
    ユーザーの手動追加は 1 (確認済み)。gold_panning の自動採取だけ 0 で書く。
    既存呼び出し (省略) は後方互換で confirmed=1 のまま動く。
    """
    now = int(time.time())
    cur = conn.execute(
        "INSERT INTO core_memories (content, created_at, updated_at, kind, metadata, confirmed) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (content, now, now, kind, metadata, confirmed),
    )
    conn.commit()
    return int(cur.lastrowid)


def update_core_memory(
    conn: sqlite3.Connection, memory_id: int, content: str, *, confirmed: Optional[int] = None,
) -> bool:
    """既存のコア記憶を書き換える。対象が存在すれば True。

    ``confirmed`` を渡すと確認フラグも更新する (gold_panning の自動 update は
    confirmed=0 で「未確認」に戻し、ユーザーの再確認を促す)。省略時は現状維持。
    """
    now = int(time.time())
    if confirmed is None:
        cur = conn.execute(
            "UPDATE core_memories SET content = ?, updated_at = ? WHERE id = ?",
            (content, now, memory_id),
        )
    else:
        cur = conn.execute(
            "UPDATE core_memories SET content = ?, updated_at = ?, confirmed = ? WHERE id = ?",
            (content, now, int(confirmed), memory_id),
        )
    conn.commit()
    return cur.rowcount > 0


def remove_core_memory(conn: sqlite3.Connection, memory_id: int) -> bool:
    """コア記憶を soft-delete する (ごみ箱へ移す)。生存中の対象があれば True。

    物理削除しないのは、gold_panning の自動 remove やペルソナの誤削除を
    ユーザーが後から復元できるようにするため (restore_core_memory)。
    """
    now = int(time.time())
    cur = conn.execute(
        "UPDATE core_memories SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
        (now, memory_id),
    )
    conn.commit()
    return cur.rowcount > 0


_SELECT_COLUMNS = "id, content, created_at, updated_at, kind, metadata, confirmed, deleted_at"


def _row_to_core_memory(row) -> CoreMemory:
    return CoreMemory(
        id=int(row[0]),
        content=str(row[1]),
        created_at=int(row[2]),
        updated_at=int(row[3]),
        kind=str(row[4]) if row[4] is not None else "note",
        metadata=row[5] if row[5] is not None else None,
        confirmed=int(row[6]) if row[6] is not None else 1,
        deleted_at=int(row[7]) if row[7] is not None else None,
    )


def list_core_memories(conn: sqlite3.Connection) -> List[CoreMemory]:
    """生存中 (未削除) の全コア記憶を id 昇順で返す。"""
    rows = conn.execute(
        f"SELECT {_SELECT_COLUMNS} FROM core_memories "
        "WHERE deleted_at IS NULL ORDER BY id ASC"
    ).fetchall()
    return [_row_to_core_memory(row) for row in rows]


def list_deleted_core_memories(conn: sqlite3.Connection) -> List[CoreMemory]:
    """ごみ箱 (soft-delete 済み) のコア記憶を削除の新しい順に返す。"""
    rows = conn.execute(
        f"SELECT {_SELECT_COLUMNS} FROM core_memories "
        "WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC"
    ).fetchall()
    return [_row_to_core_memory(row) for row in rows]


def confirm_core_memory(conn: sqlite3.Connection, memory_id: int) -> bool:
    """未確認 (自動採取) のコア記憶をユーザーが確認済みにする。生存中の対象があれば True。"""
    cur = conn.execute(
        "UPDATE core_memories SET confirmed = 1 WHERE id = ? AND deleted_at IS NULL",
        (memory_id,),
    )
    conn.commit()
    return cur.rowcount > 0


def restore_core_memory(conn: sqlite3.Connection, memory_id: int) -> bool:
    """ごみ箱から復元する (deleted_at をクリア)。対象が削除済みなら True。"""
    cur = conn.execute(
        "UPDATE core_memories SET deleted_at = NULL WHERE id = ? AND deleted_at IS NOT NULL",
        (memory_id,),
    )
    conn.commit()
    return cur.rowcount > 0


def count_unconfirmed_core_memories(conn: sqlite3.Connection) -> int:
    """未確認 (confirmed=0) かつ生存中の件数。チャットの「N件更新」バッジ用。"""
    row = conn.execute(
        "SELECT COUNT(*) FROM core_memories WHERE confirmed = 0 AND deleted_at IS NULL"
    ).fetchone()
    return int(row[0]) if row else 0


def total_core_memory_chars(conn: sqlite3.Connection) -> int:
    """全コア記憶の本文文字数の合計を返す (容量目安判定に使う)。"""
    row = conn.execute(
        "SELECT COALESCE(SUM(LENGTH(content)), 0) FROM core_memories WHERE deleted_at IS NULL"
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


# ---------------------------------------------------------------------------
# Scene (実会話の切り抜き) — スペルと UI API の共通ロジック
# ---------------------------------------------------------------------------
#
# scene の窓切り出し・トランスクリプト整形・保存はスペル
# (builtin_data/tools/core_memory_add_scene.py) と REST API
# (api/routes/people/core_memory.py) の両方から呼ばれる。ロジックの複製を避け、
# 手本の劣化 (言い換えドリフト) を防ぐ「参照によるコピー」の一点管理をここに集約する。
# 詳細: docs/intent/memory_architecture_v2.md §5


DEFAULT_SCENE_ROUNDS = 3

# 会話メッセージで persona 応答とみなす role。'model' (Gemini 系呼称) と
# 'assistant' (OpenAI 系呼称・インポートログ) の両方が実データに存在する
# (実 DB 実査で確認、2026-07-04)。それ以外 (user 等) は user ラベルにする。
_PERSONA_ROLES = ("model", "assistant")


@dataclass(frozen=True)
class SceneResult:
    """scene 作成の結果 (スペルの文言生成・API レスポンス双方が参照する)。"""
    memory_id: int
    transcript: str
    message_count: int
    char_count: int          # この切り抜き単体の文字数
    total_chars: int         # 追加後のコア記憶合計文字数
    date_start: str          # YYYY-MM-DD
    date_end: str            # YYYY-MM-DD
    message_ids: List[str]
    anchor_id: str


def format_scene_transcript(messages, persona_name: str) -> str:
    """会話メッセージ列を ``[時刻] [ラベル]: 原文`` 形式に整形する。content は無改変。

    記法は複数メッセージ表示の既存慣行 (chronicle_context_down 等) に揃える
    (2026-07-07 まはー指定。旧 ``ラベル「原文」`` 形式はカギカッコが content 内の
    カギカッコと衝突してメッセージ境界が読み取れなかった)。

    persona 応答の role は 'model' / 'assistant' 両方が実データに存在するため
    ``_PERSONA_ROLES`` で判定する。それ以外は user ラベル。
    """
    lines = []
    for msg in messages:
        label = persona_name if msg.role in _PERSONA_ROLES else "user"
        ts = getattr(msg, "created_at", None)
        ts_label = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "?"
        lines.append(f"[{ts_label}] [{label}]: {msg.content}")
    return "\n".join(lines)


def create_scene_core_memory(
    conn: sqlite3.Connection,
    anchor_id: str,
    *,
    rounds: int = DEFAULT_SCENE_ROUNDS,
    persona_name: str,
) -> Optional[SceneResult]:
    """アンカー中心の会話窓を切り抜き、scene としてコア記憶に刻む (決定論・LLM 不使用)。

    - ``conn``: 呼び出し側で db lock を取得済みの memory.db 接続。
    - ``anchor_id``: 生の message_id (URI 剥がしは呼び出し側の責務)。
    - ``rounds``: アンカー前後に含めるおおよその往復数。
    - ``persona_name``: トランスクリプトの persona 応答ラベルに使う表示名。

    窓が取れない (アンカー不在・アンカー自体が除外対象・周辺に会話なし) 場合は
    ``None`` を返す。スペル/API 側でそれぞれのエラー文言に変換する。
    """
    from sai_memory.memory.storage import get_conversation_window_around

    window = get_conversation_window_around(conn, anchor_id, rounds=rounds)
    if not window:
        return None

    transcript = format_scene_transcript(window, persona_name)
    date_start = datetime.fromtimestamp(window[0].created_at).strftime("%Y-%m-%d")
    date_end = datetime.fromtimestamp(window[-1].created_at).strftime("%Y-%m-%d")
    message_ids = [m.id for m in window]

    metadata = json.dumps(
        {
            "anchor_id": anchor_id,
            "message_ids": message_ids,
            "date_range": [date_start, date_end],
        },
        ensure_ascii=False,
    )

    new_id = add_core_memory(conn, transcript, kind="scene", metadata=metadata)
    total = total_core_memory_chars(conn)

    return SceneResult(
        memory_id=new_id,
        transcript=transcript,
        message_count=len(window),
        char_count=len(transcript),
        total_chars=total,
        date_start=date_start,
        date_end=date_end,
        message_ids=message_ids,
        anchor_id=anchor_id,
    )
