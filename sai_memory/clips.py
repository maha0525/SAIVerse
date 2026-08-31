"""クリップ (clip) ストア — 土地 (生ログ) への統一参照プリミティブ。

concept_consolidation.md「クリップ — 土地参照の汎用プリミティブ」の実装。
クリップは生ログ (SAIMemory メッセージ) の一部を「そのまま切り出して指す」参照で、
Memory Atlas の全地図 (時間・意味・目的) が共用する:

- **点クリップ** (``message_id_end`` = NULL): 1 メッセージ内の逐語引用
  ``(message_id, quote)``。旧 mark (観測点、life_concept_map.md §9.1 層 1) が
  これに当たる — mark はクリップの一状態 (どの地図にもまだ貼られていない
  クリップ) になった。quote 必須 (実在検証可能なアンカー)。
- **範囲クリップ** (``message_id_end`` あり): ``message_id`` 〜 ``message_id_end``
  のメッセージ範囲。コア記憶 SCENE の由来参照・Chronicle の source_ids が
  この形に収束する。quote は任意 (ラベル用途)。

``pasted_to`` は「どの地図 (ページ等) に貼られたか」の来歴 ref。旧 mark の
``harvested_to`` (収穫 = mark → 候補) は貼り付けの一種として一般化された。
未貼り付け (pasted_to IS NULL) のクリップの集合が「土壌プール」(life_concept_map
§5.1 潜在衝動プール) に当たる。

保存先は旧 marks と同じく **memory.db 相乗り** (クリップのアンカーは SAIMemory
メッセージなので、注釈対象と同じファイルに置く)。``init_clips_tables`` が旧世代の
DB を一度だけ移行する (``marks`` → データ移送 / ``photos`` → 改名)。旧 path は
残さない。

**名前の来歴** (2026-07-15): 本ストアは当初「写真」と呼ばれていたが、画像
(カメラで撮った写真) と紛らわしいため語をクリップへ統一した。比喩を捨てたのでは
なく抽象化した — クリップは「地図に留める」行為と「切り出した一片」(video clip)
の両義を持ち、写真が担っていた意味を内包する。スペル ``memory_clip`` は先に
この語を採っていたため、名詞側が追いついた形になる。

時刻は epoch 秒 int で、必ず ``saiverse.clock.now()`` 経由で刻む (一日シミュレータ
の仮想クロック尊重。autonomous_behavior_v2.md §12)。
"""
from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass
from typing import List, Optional

from saiverse import clock, references

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Clip:
    """クリップ 1 枚。点クリップは ``message_id_end`` が None、範囲クリップは
    開始〜終了を持つ。

    ``quote`` は点クリップでは必須 (message 本文からの逐語引用アンカー)、
    範囲クリップでは任意 (ラベル)。
    """

    clip_id: str
    message_id: str                    # 点: 対象 / 範囲: 開始メッセージ
    quote: Optional[str]               # 点: 逐語引用 (必須) / 範囲: 任意ラベル
    message_id_end: Optional[str]      # None = 点クリップ / あり = 範囲クリップの終了メッセージ
    purpose_ref: Optional[str]         # 目的ノード参照 (``task:N`` 等)。素の予約は None
    created_at: int                    # epoch 秒 (clock 経由)
    pasted_to: Optional[str]           # 貼り付け先の来歴 ref (未貼り付けは None = 土壌プール)
    origin_episode_ref: Optional[str]  # 切り出された時に開いていた出来事 (``episode:N``)
    # Per-DB sequential ID (P2b, 2026-07-10): Memory Atlas の ``clip:N`` 短縮参照。
    # 「全文はクリップそのものを読む」= memory_read clip:N の宛先
    # (concept_consolidation.md「クリップの見え方」)。arasuji_entries /
    # memopedia_pages の short_id と同じ流儀。
    short_id: Optional[int] = None

    @property
    def is_range(self) -> bool:
        """範囲クリップなら True。"""
        return self.message_id_end is not None

    @property
    def ref(self) -> str:
        """ペルソナ提示用の参照 (例: ``clip:3``)。short_id 未採番なら clip_id で代替。

        書式は統一グラマー (``saiverse/references.py``) が単一真実源。
        """
        if self.short_id is None:
            return self.clip_id
        return references.to_short_ref("clip", self.short_id)


def _now_epoch() -> int:
    """現在時刻 (epoch 秒)。仮想クロック尊重のため必ず clock.now() を通す。"""
    return int(clock.now().timestamp())


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return cur.fetchone() is not None


def _backfill_clip_short_ids(conn: sqlite3.Connection) -> None:
    """short_id を持たない既存クリップに採番する (migration helper、冪等)。

    created_at 昇順 (同点は rowid 昇順 = 挿入順) で MAX+1 から採番する
    (arasuji の _backfill_chronicle_short_ids と同じ流儀。旧 marks からの
    移行行にもここで番号が付く)。
    """
    cur = conn.execute(
        "SELECT clip_id FROM clips WHERE short_id IS NULL "
        "ORDER BY created_at ASC, rowid ASC"
    )
    rows = cur.fetchall()
    max_cur = conn.execute("SELECT COALESCE(MAX(short_id), 0) FROM clips")
    next_id = max_cur.fetchone()[0] + 1
    for (clip_id,) in rows:
        conn.execute(
            "UPDATE clips SET short_id = ? WHERE clip_id = ?",
            (next_id, clip_id),
        )
        next_id += 1
    if rows:
        conn.commit()


def _next_clip_short_id(conn: sqlite3.Connection) -> int:
    """新規クリップの次の short_id (MAX + 1、初回は 1)。"""
    cur = conn.execute("SELECT COALESCE(MAX(short_id), 0) FROM clips")
    return cur.fetchone()[0] + 1


#: 旧 ``photos`` 世代のインデックス名 (改名時に落として新名で貼り直す)
_LEGACY_PHOTO_INDEXES = (
    "idx_photos_message",
    "idx_photos_created",
    "idx_photos_pasted",
    "idx_photos_short_id",
)


def _rename_photos_to_clips(conn: sqlite3.Connection) -> None:
    """旧 ``photos`` テーブルを ``clips`` へ改名する (2026-07-15、一度きり)。

    データは動かさず名前だけ変える軽量パス (ALTER RENAME)。全書換は Windows で
    ハンドルが開いていると失敗するため使わない。インデックスは旧名のまま残るので
    落として、呼び出し元が新名で貼り直す。
    """
    conn.execute("ALTER TABLE photos RENAME TO clips")
    conn.execute("ALTER TABLE clips RENAME COLUMN photo_id TO clip_id")
    for idx in _LEGACY_PHOTO_INDEXES:
        conn.execute(f"DROP INDEX IF EXISTS {idx}")
    conn.commit()


def init_clips_tables(conn: sqlite3.Connection) -> None:
    """clips テーブルを初期化する (冪等)。

    2 世代分の移行を一度だけ行う:

    - ``photos`` (2026-07-10〜07-15) → ``clips`` へ**改名** (ALTER RENAME。
      データは動かさない。列 ``photo_id`` → ``clip_id`` も同時に改名)
    - ``marks`` (点クリップのみ・列名が mark 語彙) → ``clips`` へ**移送**
      (mark_id → clip_id / harvested_to → pasted_to / message_id_end = NULL)

    short_id (``clip:N``、P2b) は新規 DB では DDL に含まれ、既存 DB では追加系
    migration (ALTER) で足す。NULL 行の backfill は marks 移行行にも番号を
    振る必要があるため、列の有無に関わらず毎回呼ぶ (NULL 行が無ければ no-op)。
    """
    has_clips = _table_exists(conn, "clips")
    if not has_clips and _table_exists(conn, "photos"):
        _rename_photos_to_clips(conn)
        has_clips = True
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS clips (
            clip_id TEXT PRIMARY KEY,
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
    if not has_clips and _table_exists(conn, "marks"):
        # 旧 marks からの一回きり移行 (挿入順 = rowid 順を保つ)
        conn.execute(
            """
            INSERT INTO clips (clip_id, message_id, quote, message_id_end,
                               purpose_ref, created_at, pasted_to, origin_episode_ref)
            SELECT mark_id, message_id, quote, NULL,
                   purpose_ref, created_at, harvested_to, origin_episode_ref
            FROM marks ORDER BY rowid ASC
            """
        )
        conn.execute("DROP TABLE marks")
    # Migration (P2b, 2026-07-10): 既存 DB (short_id 列なし) への追加系 migration
    try:
        conn.execute("SELECT short_id FROM clips LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE clips ADD COLUMN short_id INTEGER")
        conn.commit()
    # backfill は毎回 (冪等・NULL 行のみ対象): marks 移行行 / ALTER 直後の既存行
    _backfill_clip_short_ids(conn)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_clips_message ON clips(message_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_clips_created ON clips(created_at)"
    )
    # 未貼り付けフィルタ用 (list_clips(unpasted_only=True) が常用クエリ)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_clips_pasted ON clips(pasted_to)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_clips_short_id ON clips(short_id)"
    )
    conn.commit()
    _backfill_canonical_pasted_to(conn)


def _backfill_canonical_pasted_to(conn: sqlite3.Connection) -> int:
    """``pasted_to`` の旧 prefix を正典形へ寄せる (冪等)。2026-07-15 の移行。

    貼り先の照合は完全一致 (``list_clips_pasted_to``)。読む側が正規形
    (``memopedia:5``) で引くようになったので、旧表記 (``m:5``) のまま残すと
    **ページに貼ったクリップが見えなくなる**ため、ここで揃える。

    Returns: 書き換えた行数。
    """
    from saiverse import references

    rows = conn.execute(
        "SELECT clip_id, pasted_to FROM clips WHERE pasted_to IS NOT NULL"
    ).fetchall()
    changed = 0
    for clip_id, pasted_to in rows:
        canonical = references.normalize_short_ref(pasted_to)
        if canonical == pasted_to:
            continue
        conn.execute(
            "UPDATE clips SET pasted_to = ? WHERE clip_id = ?", (canonical, clip_id)
        )
        changed += 1
    if changed:
        conn.commit()
        LOGGER.info("clips: normalized %d legacy pasted_to ref(s)", changed)
    return changed


_CLIP_COLS = (
    "clip_id, message_id, quote, message_id_end, purpose_ref, "
    "created_at, pasted_to, origin_episode_ref, short_id"
)


def _row_to_clip(row: tuple) -> Clip:
    return Clip(
        clip_id=row[0],
        message_id=row[1],
        quote=row[2],
        message_id_end=row[3],
        purpose_ref=row[4],
        created_at=int(row[5]),
        pasted_to=row[6],
        origin_episode_ref=row[7],
        short_id=int(row[8]) if len(row) > 8 and row[8] is not None else None,
    )


def add_clip(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    quote: Optional[str] = None,
    message_id_end: Optional[str] = None,
    purpose_ref: Optional[str] = None,
    origin_episode_ref: Optional[str] = None,
    pasted_to: Optional[str] = None,
) -> Clip:
    """クリップを切り出す (永続化する)。

    点クリップ (``message_id_end`` なし) は ``quote`` 必須 — 対象メッセージ本文
    からの逐語引用。実在検証 (引用が本当に message 本文に含まれるか) は呼び出し側
    の責務 — 本レイヤーは永続化のみ。範囲クリップは ``quote`` 任意 (ラベル)。

    ``pasted_to`` を与えると最初から貼り付け済みで保存する (SCENE のように
    切り出した瞬間に地図へ貼る用途)。
    """
    if not message_id:
        raise ValueError("message_id is required")
    if message_id_end is None and not quote:
        raise ValueError("quote is required for a point clip")
    clip = Clip(
        clip_id=str(uuid.uuid4()),
        message_id=message_id,
        quote=quote or None,
        message_id_end=message_id_end,
        purpose_ref=purpose_ref,
        created_at=_now_epoch(),
        pasted_to=pasted_to,
        origin_episode_ref=origin_episode_ref,
        short_id=_next_clip_short_id(conn),
    )
    conn.execute(
        f"INSERT INTO clips ({_CLIP_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            clip.clip_id, clip.message_id, clip.quote, clip.message_id_end,
            clip.purpose_ref, clip.created_at, clip.pasted_to,
            clip.origin_episode_ref, clip.short_id,
        ),
    )
    conn.commit()
    return clip


def get_clip(conn: sqlite3.Connection, clip_id: str) -> Optional[Clip]:
    """clip_id で 1 枚引く。無ければ None。"""
    cur = conn.execute(
        f"SELECT {_CLIP_COLS} FROM clips WHERE clip_id = ?", (clip_id,)
    )
    row = cur.fetchone()
    return _row_to_clip(row) if row else None


def get_clip_by_short_id(conn: sqlite3.Connection, short_id: int) -> Optional[Clip]:
    """short_id (``clip:N`` の N) で 1 枚引く。無ければ None。"""
    cur = conn.execute(
        f"SELECT {_CLIP_COLS} FROM clips WHERE short_id = ?", (short_id,)
    )
    row = cur.fetchone()
    return _row_to_clip(row) if row else None


def list_clips(
    conn: sqlite3.Connection,
    *,
    unpasted_only: bool = False,
    since: Optional[int] = None,
    message_id: Optional[str] = None,
) -> List[Clip]:
    """クリップの一覧 (created_at 昇順)。

    Args:
        unpasted_only: True なら未貼り付け (pasted_to IS NULL) のみ —
            土壌プール (収穫 = クリップ → 候補、起床判断等の前段操作 §5.1) の
            読み手が使う。
        since: epoch 秒。指定時は ``created_at >= since`` のみ。
        message_id: 指定時はそのメッセージを開始点とするクリップのみ。
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
        # 同一 epoch 秒の同点解決は挿入順 (rowid)。clip_id はランダム UUID
        # なので tiebreak に使うと並びが非決定になる (2026-07-06 フレーク修正)
        f"SELECT {_CLIP_COLS} FROM clips {where} ORDER BY created_at ASC, rowid ASC",
        params,
    )
    return [_row_to_clip(row) for row in cur.fetchall()]


def list_clips_pasted_to(conn: sqlite3.Connection, pasted_to: str) -> List[Clip]:
    """指定した貼り付け先 (ref) に貼られているクリップの一覧 (created_at 昇順)。

    Memory Atlas のページ読み出し (``saiverse/memory_atlas.py``) が「このページに
    貼られたクリップ」を列挙するのに使う。``pasted_to`` はページの参照
    (``c:3`` / ``memopedia:5`` / ``chronicle:2`` 等) をそのまま渡す。
    """
    if not pasted_to:
        return []
    cur = conn.execute(
        f"SELECT {_CLIP_COLS} FROM clips WHERE pasted_to = ? "
        f"ORDER BY created_at ASC, rowid ASC",
        (pasted_to,),
    )
    return [_row_to_clip(row) for row in cur.fetchall()]


def clip_pasted(
    conn: sqlite3.Connection, clip_id: str, pasted_to: str
) -> Optional[Clip]:
    """貼り付け済みにする (pasted_to = 貼り付け先ページ・生まれた候補等への来歴 ref)。

    §3.1「収穫されて初めて候補が生まれる (来歴リンクで接地)」のクリップ側の刻印。
    クリップ自体は消さない (歴史として残す §5.1)。無ければ None。
    """
    if not pasted_to:
        raise ValueError("pasted_to is required")
    cur = conn.execute(
        "UPDATE clips SET pasted_to = ? WHERE clip_id = ?",
        (pasted_to, clip_id),
    )
    conn.commit()
    if cur.rowcount == 0:
        return None
    return get_clip(conn, clip_id)
