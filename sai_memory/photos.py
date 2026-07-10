"""写真 (photo) ストア — 土地 (生ログ) への統一参照プリミティブ。

concept_consolidation.md「写真 — 土地参照の汎用プリミティブ」の実装。写真は
生ログ (SAIMemory メッセージ) の一部を「そのまま切り出して指す」参照で、
Memory Atlas の全地図 (時間・意味・目的) が共用する:

- **点写真** (``message_id_end`` = NULL): 1 メッセージ内の逐語引用
  ``(message_id, quote)``。旧 mark (観測点、life_concept_map.md §9.1 層 1) が
  これに当たる — mark は写真の一状態 (どの地図にもまだ貼られていない写真) に
  なった。quote 必須 (実在検証可能なアンカー)。
- **範囲写真** (``message_id_end`` あり): ``message_id`` 〜 ``message_id_end``
  のメッセージ範囲。コア記憶 SCENE の由来参照・Chronicle の source_ids が
  この形に収束する。quote は任意 (ラベル用途)。

``pasted_to`` は「どの地図 (ページ等) に貼られたか」の来歴 ref。旧 mark の
``harvested_to`` (収穫 = mark → 候補) は貼り付けの一種として一般化された。
未貼り付け (pasted_to IS NULL) の写真の集合が「土壌プール」(life_concept_map
§5.1 潜在衝動プール) に当たる。

保存先は旧 marks と同じく **memory.db 相乗り** (写真のアンカーは SAIMemory
メッセージなので、注釈対象と同じファイルに置く)。旧 ``marks`` テーブルが
存在する DB では ``init_photos_tables`` が一度だけデータを ``photos`` へ移行し、
``marks`` を落とす (旧 path は残さない)。

時刻は epoch 秒 int で、必ず ``saiverse.clock.now()`` 経由で刻む (一日シミュレータ
の仮想クロック尊重。autonomous_behavior_v2.md §12)。
"""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from typing import List, Optional

from saiverse import clock


@dataclass(frozen=True)
class Photo:
    """写真 1 枚。点写真は ``message_id_end`` が None、範囲写真は開始〜終了を持つ。

    ``quote`` は点写真では必須 (message 本文からの逐語引用アンカー)、
    範囲写真では任意 (ラベル)。
    """

    photo_id: str
    message_id: str                    # 点写真: 対象 / 範囲写真: 開始メッセージ
    quote: Optional[str]               # 点写真: 逐語引用 (必須) / 範囲写真: 任意ラベル
    message_id_end: Optional[str]      # None = 点写真 / あり = 範囲写真の終了メッセージ
    purpose_ref: Optional[str]         # 目的ノード参照 (``task:N`` 等)。素の予約は None
    created_at: int                    # epoch 秒 (clock 経由)
    pasted_to: Optional[str]           # 貼り付け先の来歴 ref (未貼り付けは None = 土壌プール)
    origin_episode_ref: Optional[str]  # 撮られた時に開いていた出来事 (``episode:N``)
    # Per-DB sequential ID (P2b, 2026-07-10): Memory Atlas の ``p:N`` 短縮参照。
    # 「全文は写真そのものを読む」= memory_read p:N の宛先 (concept_consolidation.md
    # 「clip と写真の見え方」)。arasuji_entries / memopedia_pages の short_id と同じ流儀。
    short_id: Optional[int] = None

    @property
    def is_range(self) -> bool:
        """範囲写真なら True。"""
        return self.message_id_end is not None

    @property
    def ref(self) -> str:
        """ペルソナ提示用の参照 (例: ``p:3``)。short_id 未採番なら photo_id で代替。"""
        return f"p:{self.short_id}" if self.short_id is not None else self.photo_id


def _now_epoch() -> int:
    """現在時刻 (epoch 秒)。仮想クロック尊重のため必ず clock.now() を通す。"""
    return int(clock.now().timestamp())


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return cur.fetchone() is not None


def _backfill_photo_short_ids(conn: sqlite3.Connection) -> None:
    """short_id を持たない既存写真に採番する (migration helper、冪等)。

    created_at 昇順 (同点は rowid 昇順 = 挿入順) で MAX+1 から採番する
    (arasuji の _backfill_chronicle_short_ids と同じ流儀。旧 marks からの
    移行行にもここで番号が付く)。
    """
    cur = conn.execute(
        "SELECT photo_id FROM photos WHERE short_id IS NULL "
        "ORDER BY created_at ASC, rowid ASC"
    )
    rows = cur.fetchall()
    max_cur = conn.execute("SELECT COALESCE(MAX(short_id), 0) FROM photos")
    next_id = max_cur.fetchone()[0] + 1
    for (photo_id,) in rows:
        conn.execute(
            "UPDATE photos SET short_id = ? WHERE photo_id = ?",
            (next_id, photo_id),
        )
        next_id += 1
    if rows:
        conn.commit()


def _next_photo_short_id(conn: sqlite3.Connection) -> int:
    """新規写真の次の short_id (MAX + 1、初回は 1)。"""
    cur = conn.execute("SELECT COALESCE(MAX(short_id), 0) FROM photos")
    return cur.fetchone()[0] + 1


def init_photos_tables(conn: sqlite3.Connection) -> None:
    """photos テーブルを初期化する (冪等)。

    旧 ``marks`` テーブル (点写真のみ・列名が mark 語彙) が存在し、``photos`` が
    未作成の DB では、一度だけデータを移行して marks を DROP する
    (mark_id → photo_id / harvested_to → pasted_to / message_id_end = NULL)。

    short_id (``p:N``、P2b) は新規 DB では DDL に含まれ、既存 DB では追加系
    migration (ALTER) で足す。NULL 行の backfill は marks 移行行にも番号を
    振る必要があるため、列の有無に関わらず毎回呼ぶ (NULL 行が無ければ no-op)。
    """
    has_photos = _table_exists(conn, "photos")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS photos (
            photo_id TEXT PRIMARY KEY,
            message_id TEXT NOT NULL,
            quote TEXT,
            message_id_end TEXT,
            purpose_ref TEXT,
            created_at INTEGER NOT NULL,
            pasted_to TEXT,
            origin_episode_ref TEXT,
            short_id INTEGER
        )
        """
    )
    if not has_photos and _table_exists(conn, "marks"):
        # 旧 marks からの一回きり移行 (挿入順 = rowid 順を保つ)
        conn.execute(
            """
            INSERT INTO photos (photo_id, message_id, quote, message_id_end,
                                purpose_ref, created_at, pasted_to, origin_episode_ref)
            SELECT mark_id, message_id, quote, NULL,
                   purpose_ref, created_at, harvested_to, origin_episode_ref
            FROM marks ORDER BY rowid ASC
            """
        )
        conn.execute("DROP TABLE marks")
    # Migration (P2b, 2026-07-10): 既存 DB (short_id 列なし) への追加系 migration
    try:
        conn.execute("SELECT short_id FROM photos LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE photos ADD COLUMN short_id INTEGER")
        conn.commit()
    # backfill は毎回 (冪等・NULL 行のみ対象): marks 移行行 / ALTER 直後の既存行
    _backfill_photo_short_ids(conn)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_photos_message ON photos(message_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_photos_created ON photos(created_at)"
    )
    # 未貼り付けフィルタ用 (list_photos(unpasted_only=True) が常用クエリ)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_photos_pasted ON photos(pasted_to)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_photos_short_id ON photos(short_id)"
    )
    conn.commit()


_PHOTO_COLS = (
    "photo_id, message_id, quote, message_id_end, purpose_ref, "
    "created_at, pasted_to, origin_episode_ref, short_id"
)


def _row_to_photo(row: tuple) -> Photo:
    return Photo(
        photo_id=row[0],
        message_id=row[1],
        quote=row[2],
        message_id_end=row[3],
        purpose_ref=row[4],
        created_at=int(row[5]),
        pasted_to=row[6],
        origin_episode_ref=row[7],
        short_id=int(row[8]) if len(row) > 8 and row[8] is not None else None,
    )


def add_photo(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    quote: Optional[str] = None,
    message_id_end: Optional[str] = None,
    purpose_ref: Optional[str] = None,
    origin_episode_ref: Optional[str] = None,
    pasted_to: Optional[str] = None,
) -> Photo:
    """写真を撮る (永続化する)。

    点写真 (``message_id_end`` なし) は ``quote`` 必須 — 対象メッセージ本文からの
    逐語引用。実在検証 (引用が本当に message 本文に含まれるか) は呼び出し側の
    責務 — 本レイヤーは永続化のみ。範囲写真は ``quote`` 任意 (ラベル)。

    ``pasted_to`` を与えると最初から貼り付け済みで保存する (SCENE のように
    撮った瞬間に地図へ貼る用途)。
    """
    if not message_id:
        raise ValueError("message_id is required")
    if message_id_end is None and not quote:
        raise ValueError("quote is required for a point photo")
    photo = Photo(
        photo_id=str(uuid.uuid4()),
        message_id=message_id,
        quote=quote or None,
        message_id_end=message_id_end,
        purpose_ref=purpose_ref,
        created_at=_now_epoch(),
        pasted_to=pasted_to,
        origin_episode_ref=origin_episode_ref,
        short_id=_next_photo_short_id(conn),
    )
    conn.execute(
        f"INSERT INTO photos ({_PHOTO_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            photo.photo_id, photo.message_id, photo.quote, photo.message_id_end,
            photo.purpose_ref, photo.created_at, photo.pasted_to,
            photo.origin_episode_ref, photo.short_id,
        ),
    )
    conn.commit()
    return photo


def get_photo(conn: sqlite3.Connection, photo_id: str) -> Optional[Photo]:
    """photo_id で 1 枚引く。無ければ None。"""
    cur = conn.execute(
        f"SELECT {_PHOTO_COLS} FROM photos WHERE photo_id = ?", (photo_id,)
    )
    row = cur.fetchone()
    return _row_to_photo(row) if row else None


def get_photo_by_short_id(conn: sqlite3.Connection, short_id: int) -> Optional[Photo]:
    """short_id (``p:N`` の N) で 1 枚引く。無ければ None。"""
    cur = conn.execute(
        f"SELECT {_PHOTO_COLS} FROM photos WHERE short_id = ?", (short_id,)
    )
    row = cur.fetchone()
    return _row_to_photo(row) if row else None


def list_photos(
    conn: sqlite3.Connection,
    *,
    unpasted_only: bool = False,
    since: Optional[int] = None,
    message_id: Optional[str] = None,
) -> List[Photo]:
    """写真の一覧 (created_at 昇順)。

    Args:
        unpasted_only: True なら未貼り付け (pasted_to IS NULL) のみ —
            土壌プール (収穫 = 写真 → 候補、起床判断等の前段操作 §5.1) の
            読み手が使う。
        since: epoch 秒。指定時は ``created_at >= since`` のみ。
        message_id: 指定時はそのメッセージを開始点とする写真のみ。
    """
    conditions: List[str] = []
    params: List = []
    if unpasted_only:
        conditions.append("pasted_to IS NULL")
    if since is not None:
        conditions.append("created_at >= ?")
        params.append(int(since))
    if message_id is not None:
        conditions.append("message_id = ?")
        params.append(message_id)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    cur = conn.execute(
        # 同一 epoch 秒の同点解決は挿入順 (rowid)。photo_id はランダム UUID
        # なので tiebreak に使うと並びが非決定になる (2026-07-06 フレーク修正)
        f"SELECT {_PHOTO_COLS} FROM photos {where} ORDER BY created_at ASC, rowid ASC",
        params,
    )
    return [_row_to_photo(row) for row in cur.fetchall()]


def list_photos_pasted_to(conn: sqlite3.Connection, pasted_to: str) -> List[Photo]:
    """指定した貼り付け先 (ref) に貼られている写真の一覧 (created_at 昇順)。

    Memory Atlas のページ読み出し (``saiverse/memory_atlas.py``) が「このページに
    貼られた写真」を列挙するのに使う。``pasted_to`` はページの参照 (``c:3`` /
    ``m:5`` / ``ch:2`` 等) をそのまま渡す。
    """
    if not pasted_to:
        return []
    cur = conn.execute(
        f"SELECT {_PHOTO_COLS} FROM photos WHERE pasted_to = ? "
        f"ORDER BY created_at ASC, rowid ASC",
        (pasted_to,),
    )
    return [_row_to_photo(row) for row in cur.fetchall()]


def photo_pasted(
    conn: sqlite3.Connection, photo_id: str, pasted_to: str
) -> Optional[Photo]:
    """貼り付け済みにする (pasted_to = 貼り付け先ページ・生まれた候補等への来歴 ref)。

    §3.1「収穫されて初めて候補が生まれる (来歴リンクで接地)」の写真側の刻印。
    写真自体は消さない (歴史として残す §5.1)。無ければ None。
    """
    if not pasted_to:
        raise ValueError("pasted_to is required")
    cur = conn.execute(
        "UPDATE photos SET pasted_to = ? WHERE photo_id = ?",
        (pasted_to, photo_id),
    )
    conn.commit()
    if cur.rowcount == 0:
        return None
    return get_photo(conn, photo_id)
