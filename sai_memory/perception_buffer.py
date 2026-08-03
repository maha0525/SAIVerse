"""知覚バッファ (Perception Buffer) のストレージ層。

ペルソナが発話していない間 (主観時間が止まっている間) に外界で発生した知覚を、
型付きで溜め込む**永続**バッファ。次の Pulse 開始時にまとめて型別 reduce し、
1 つのシステムメッセージとして SAIMemory へ書き出して消費する。

設計の核 (docs/intent/perception_buffer.md):
- 主観時間は Pulse でのみ進む。知覚が SAIMemory に入る (= ペルソナが知覚する) のは
  Pulse 消費時のみ。バッファへの書き込み自体は客観時間で随時起きる。
- 未消費の間だけ型別 reduce (相殺・集約) できる。消費済みは相殺不可。
- 揮発ではなく永続 (再起動耐性・任意タイミングのプレビューのため)。テーブルは
  ペルソナの memory.db に同居する (core_memory / Memopedia と同じ conn)。
- 会話履歴 (messages) とは別テーブル。まだ「知覚」になっていない未消費のものを
  会話に混ぜないため。消費時にここから SAIMemory (messages) へ移す。

Phase 1 の利用者はメタ記憶訂正 (kind='core_memory_correction') のみ。状態差分
(入退室等) の載せ替えと起動力ディスパッチャは Phase 2 以降。
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class PerceptionItem:
    """未消費の知覚 1 件。

    ``kind``: 型 (reduce / 表示の単位)。例: 'core_memory_correction'。
    ``content``: ペルソナに見せる文 (整形済み)。消費時にそのまま本文へ入る。
    ``reduce_key``: 同型内で集約・相殺するキー (例: 'c:5' = 同じコア記憶への
        複数操作)。None なら個別に残る (集約対象外)。
    ``salient``: 起動力フラグ (1 = 到着で Pulse を起こす「絶対反応する」)。Phase 1
        では格納のみで未使用 (起動力ディスパッチャは Phase 2 以降)。
    ``media``: 添付メディア (画像等) の JSON 文字列。``[{"path","mime_type","role"}, ...]``
        形式。移動時の内装画像・他ペルソナ外見画像などを運ぶ (None = メディアなし)。
        消費時に flush が全知覚のメディアを集めて event_message の metadata.media に載せる。
    ``metadata``: JSON 付加情報 (由来参照など)。将来余地。
    ``created_at``: 発生時刻 (Unix 秒, 客観時間)。
    """
    id: int
    kind: str
    content: str
    reduce_key: Optional[str]
    salient: int
    media: Optional[str]
    metadata: Optional[str]
    created_at: int

    def media_list(self) -> list:
        """``media`` (JSON) を list に復元する。空/不正なら空 list。"""
        if not self.media:
            return []
        try:
            import json
            data = json.loads(self.media)
            return data if isinstance(data, list) else []
        except (ValueError, TypeError):
            return []


def init_perception_buffer_table(conn: sqlite3.Connection) -> None:
    """知覚バッファテーブルを初期化する (冪等)。

    新設テーブルなので、将来使う ``salient`` / ``metadata`` も最初から DDL に含める
    (後のマイグレーションを不要にする)。
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS perception_buffer (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            content TEXT NOT NULL,
            reduce_key TEXT,
            salient INTEGER NOT NULL DEFAULT 0,
            media TEXT,
            metadata TEXT,
            created_at INTEGER NOT NULL
        )
        """
    )
    # 既存 DB 向けの追加系マイグレーション (core_memory と同方式)。
    # media: 移動時の内装/外見画像などの添付 (JSON)。Phase 1a 時点の DB には無い。
    try:
        conn.execute("ALTER TABLE perception_buffer ADD COLUMN media TEXT")
    except sqlite3.OperationalError:
        pass  # 既に存在する
    conn.commit()


def push_perception(
    conn: sqlite3.Connection,
    kind: str,
    content: str,
    *,
    reduce_key: Optional[str] = None,
    salient: bool = False,
    media: Optional[list] = None,
    metadata: Optional[str] = None,
) -> int:
    """知覚を 1 件バッファに積む。採番された id を返す。

    書き込みは客観時間で随時起きる (ペルソナはまだ知覚しない)。実際に知覚される
    のは次の Pulse 消費時 (``list_pending`` → reduce → SAIMemory → ``delete``)。

    ``media`` は ``[{"path","mime_type","role"}, ...]`` の list (画像等)。JSON 化して
    保存し、消費時に event_message の metadata.media へ載せる。
    """
    now = int(time.time())
    media_json = None
    if media:
        import json
        media_json = json.dumps(media, ensure_ascii=False)
    cur = conn.execute(
        "INSERT INTO perception_buffer (kind, content, reduce_key, salient, media, metadata, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (kind, content, reduce_key, 1 if salient else 0, media_json, metadata, now),
    )
    conn.commit()
    return int(cur.lastrowid)


_SELECT_COLUMNS = "id, kind, content, reduce_key, salient, media, metadata, created_at"


def _row_to_item(row) -> PerceptionItem:
    return PerceptionItem(
        id=int(row[0]),
        kind=str(row[1]),
        content=str(row[2]),
        reduce_key=row[3] if row[3] is not None else None,
        salient=int(row[4]) if row[4] is not None else 0,
        media=row[5] if row[5] is not None else None,
        metadata=row[6] if row[6] is not None else None,
        created_at=int(row[7]),
    )


def list_pending(conn: sqlite3.Connection) -> List[PerceptionItem]:
    """未消費の知覚を発生順 (created_at → id 昇順) で全件返す。

    消費 (Pulse) とプレビュー (任意タイミング) の両方がこの読み口を使う。
    プレビューは読むだけ (削除しない)、消費は読んで整形・書き出し後に ``delete`` する。
    """
    rows = conn.execute(
        f"SELECT {_SELECT_COLUMNS} FROM perception_buffer ORDER BY created_at ASC, id ASC"
    ).fetchall()
    return [_row_to_item(row) for row in rows]


def count_pending(conn: sqlite3.Connection, kind: str) -> int:
    """未消費の知覚のうち指定 ``kind`` の件数を返す。

    フィード配送の膨張ガード (saiverse/feed_manager.py) が「これ以上積まない」
    判定に使う読み口。
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM perception_buffer WHERE kind = ?", (kind,)
    ).fetchone()
    return int(row[0]) if row else 0


def delete_perceptions(conn: sqlite3.Connection, ids: List[int]) -> None:
    """指定 id の知覚をバッファから削除する (消費完了時に呼ぶ)。"""
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    conn.execute(
        f"DELETE FROM perception_buffer WHERE id IN ({placeholders})", tuple(ids)
    )
    conn.commit()


def reduce_perceptions(items: List[PerceptionItem]) -> List[PerceptionItem]:
    """未消費知覚を型別に畳み込む (相殺・集約)。表示順は元の発生順を保つ。

    Phase 1 の方針: 同一 ``(kind, reduce_key)`` は**最新 (最後に積まれた) 1 件だけ**
    残す (= 同じコア記憶への複数操作を最新状態に集約)。``reduce_key`` が None の
    項目は集約せず全件残す。

    将来: 型ごとの reduce 関数 (例: occupant enter+leave の相殺) に一般化する。
    """
    last_pos: dict = {}
    for i, it in enumerate(items):
        if it.reduce_key is not None:
            last_pos[(it.kind, it.reduce_key)] = i
    out: List[PerceptionItem] = []
    for i, it in enumerate(items):
        if it.reduce_key is not None and last_pos[(it.kind, it.reduce_key)] != i:
            continue  # 同一キーのより新しい項目があるので畳む
        out.append(it)
    return out


# 型 → 消費メッセージ内の見出し。未知の型は汎用見出しにフォールバックする。
# 空文字列 "" を指定した型は見出しを付けない (content が自己完結している場合。
# 例: persona_recall は「過去の会話の想起」本文そのものなので見出し不要)。
_KIND_HEADERS = {
    "core_memory_correction": "[コア記憶の更新通知]",
    "world_state": "[システム通知]",       # 世界状態の差分 (入退室・アイテム・スペル 等)
    "feed": "[フィード]",                  # フィード施設の新着記事 (rss_feed_intake.md)
    "persona_recall": "",                     # 入室時の過去会話想起 (本文が自己完結)
    "surroundings": "",                       # 移動先の様子 (本文が <system> 見出し込みで自己完結)
}
_DEFAULT_HEADER = "[システム通知]"


def format_perception_message(items: List[PerceptionItem]) -> str:
    """reduce 済み知覚を 1 メッセージ分の本文に整形する (``<system>`` 包みは呼び出し側)。

    **発生順 (list_pending の created_at→id 順) を保って出す**。型でグルーピングすると
    時系列が壊れ、複数 Building を移動した場合に「後から入室した相手が前の部屋にいた」
    ように見えてしまう (実運用で発覚, 2026-07-09)。連続する ``world_state`` だけは
    1 つの見出しにまとめ (通知の乱発を防ぐ)、それ以外の型 (surroundings / correction /
    persona_recall 等) はその発生位置に独立ブロックとして差し込む。見出しが空文字列の
    型は content だけを出す。同一 Pulse で消費される全知覚を 1 メッセージにまとめる (C3)。
    """
    blocks: List[str] = []
    i = 0
    n = len(items)
    while i < n:
        kind = items[i].kind
        if kind == "world_state":
            # 連続する world_state を 1 見出しにまとめる。
            group: List[str] = []
            while i < n and items[i].kind == "world_state":
                group.append(items[i].content)
                i += 1
            header = _KIND_HEADERS.get("world_state", _DEFAULT_HEADER)
            blocks.append(f"{header}\n" + "\n\n".join(group))
        else:
            header = _KIND_HEADERS.get(kind, _DEFAULT_HEADER)
            content = items[i].content
            blocks.append(f"{header}\n{content}" if header else content)
            i += 1
    return "\n\n".join(blocks)
