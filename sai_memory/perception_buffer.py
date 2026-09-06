"""知覚バッファ (Perception Buffer) = 知覚台帳のストレージ層。

ペルソナが発話していない間 (主観時間が止まっている間) に外界で発生した知覚を、
型付きで溜め込む**永続**台帳。Beat 頭の消費で型別 reduce され、消費印
(``consumed_at`` ほか) が打たれる。

設計の核 (docs/intent/perception_buffer.md、§10 = W14 知覚レンダリング):
- 主観時間は Beat でのみ進む。知覚する = 台帳に消費印が入る (§10.7 C1)。
  messages に event_message 行は作らない — 提示はコンテキスト組み立て時に
  messages と消費済み台帳を時刻順マージする (§10.3)。
- 未消費の間だけ型別 reduce (相殺・集約) できる。消費済みは相殺不可。
- 揮発ではなく永続 (再起動耐性・任意タイミングのプレビューのため)。テーブルは
  ペルソナの memory.db に同居する (core_memory / Memopedia と同じ conn)。
- 消費済み行も削除しない。台帳がそのまま「その瞬間に知覚した」証跡であり、
  退場 (Chronicle fold) 時の決定論付記 (§10.4) と読み口の実体になる。
- 「部屋の様子」だけは再訪で差分に縮む — その記帳と、付記と同一 tx で走る
  提示文面の移管は sai_memory/room_state.py が持つ。
- 提示に出る知覚の**合計**には上限がある (§10.9)。超えたら古い側をまとめて
  下ろし、その境界 (``perception_presentation`` の 1 行) は一方向にしか
  進まない。下ろすのは提示だけ — 台帳の行も付記印も変えないので、その期間の
  編纂が来れば材料として引き取られる。
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Union

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PerceptionItem:
    """知覚台帳の 1 件 (未消費 = ``consumed_at`` が None)。

    ``kind``: 型 (reduce / 表示の単位)。例: 'core_memory_correction'。
    ``content``: ペルソナに見せる文 (整形済み)。消費時にそのまま本文へ入る。
    ``reduce_key``: 同型内で集約・相殺するキー (例: 'c:5' = 同じコア記憶への
        複数操作)。None なら個別に残る (集約対象外)。
    ``salient``: 起動力フラグ (1 = 到着で Pulse を起こす「絶対反応する」)。Phase 1
        では格納のみで未使用 (起動力ディスパッチャは Phase 2 以降)。
    ``media``: 添付メディア (画像等) の JSON 文字列。``[{"path","mime_type","role"}, ...]``
        形式。移動時の内装画像・他ペルソナ外見画像などを運ぶ (None = メディアなし)。
        提示 (時刻順マージ) 時にブロックの metadata.media に載せる。
    ``metadata``: JSON 付加情報 (由来参照など)。将来余地。
    ``created_at``: 発生時刻 (Unix 秒, 客観時間)。
    ``consumed_at`` / ``consumed_batch_id``: 消費印 (perception_buffer.md §10.2)。
        消費 = メッセージ行を書くことではなく、消費バッチ (:class:`PerceptionBatch`)
        を確定して台帳にこの印を打つこと。消費済み行は削除しない — 台帳がそのまま
        証跡と読み口の実体になる。「消費のまとまり」はバッチ id が持つ (秒精度の
        時刻からは再構成しない)。
    """
    id: int
    kind: str
    content: str
    reduce_key: Optional[str]
    salient: int
    media: Optional[str]
    metadata: Optional[str]
    created_at: int
    consumed_at: Optional[int] = None
    consumed_batch_id: Optional[int] = None

    def media_list(self) -> list:
        """``media`` (JSON) を list に復元する。空/不正なら空 list。"""
        return _decode_media(self.media)


def _decode_media(raw: Optional[str]) -> list:
    if not raw:
        return []
    try:
        import json
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


@dataclass(frozen=True)
class PerceptionBatch:
    """消費バッチ 1 件 (perception_buffer.md §10.2)。

    flush が単一トランザクションで確定する「その瞬間に知覚した」証跡。
    ``rendered_text`` は消費時の reduce → format の**確定文面** — ペルソナが
    見た文そのものが永続化され、提示は後から生の項目を読み直して再構成しない
    (再構成は reduce で消えた中間状態を復活させ、秒精度の時刻衝突でグループを
    混ぜる — 2026-08-18 Codex レビュー)。

    ``annexed_entry_id``: 付記印。Chronicle 編纂がこのバッチを digest へ転写した
    とき、digest 確定と同一 tx で当該 Chronicle エントリ id が入る。**提示から
    下ろす唯一の手段**がこの印 (§10.3)。

    ``boundary_created_at`` / ``boundary_rowid``: バッチ確定時点で最後に保存済み
    だった message の正典順序キー (無ければ NULL)。Chronicle 無効ペルソナの
    窓絞りで anchor 行と同じ包含規則の比較に使う。

    ``room_state_json``: このバッチに含まれる「部屋の様子」エントリの記帳
    (sai_memory/room_state.py)。再訪の差分がどの全文を土台にしているかと、
    確定文面の中のどこにその文面が居るかを持つ。移管 (土台が付記で下りたとき
    最古の差分を全文へ差し替える) がこの記帳を読み書きする。
    """
    id: int
    consumed_at: int
    pulse_id: Optional[str]
    episode_id: Optional[str]
    rendered_text: str
    media: Optional[str]
    annexed_entry_id: Optional[str]
    boundary_created_at: Optional[int] = None
    boundary_rowid: Optional[int] = None
    room_state_json: Optional[str] = None

    def media_list(self) -> list:
        return _decode_media(self.media)


def init_perception_buffer_table(
    conn: sqlite3.Connection, *, resource_id: Optional[str] = None,
) -> None:
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
    # 消費記帳 (W14 知覚レンダリング, perception_buffer.md §10.2)。
    # flush は「メッセージ行を書く → 削除」の二段から「消費バッチの確定」単一 tx へ。
    upgraded_from_two_phase = False
    for ddl in (
        "ALTER TABLE perception_buffer ADD COLUMN consumed_at INTEGER",
        "ALTER TABLE perception_buffer ADD COLUMN consumed_batch_id INTEGER",
    ):
        try:
            conn.execute(ddl)
            if "consumed_at" in ddl:
                # consumed_at 列が今この場で生えた = 旧二段 flush 世代の DB。
                upgraded_from_two_phase = True
        except sqlite3.OperationalError:
            pass  # 既に存在する
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_perception_buffer_consumed_at "
        "ON perception_buffer(consumed_at)"
    )
    # 省略の印の件数 (:func:`count_batch_records`) は、下ろされたバッチごとに
    # 台帳の行を引き直して数える。これは提示を組むたびに走るので、索引が無いと
    # 「知覚が堆積した環境」— つまりこの機構が救おうとしている形 — で毎回
    # 台帳の全走査になる。
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_perception_buffer_consumed_batch "
        "ON perception_buffer(consumed_batch_id)"
    )
    # 台帳配送 (execution ledger outbox) の冪等キー専用列 (2026-08-19 Codex
    # 第八巡 #1)。metadata JSON の check-then-act 照合は同時配送の競合に破れ、
    # 全行 LIKE 走査は消費済み行の蓄積で線形悪化する — UNIQUE 索引で DB 側に
    # 原子的な冪等を強制する (SQLite の UNIQUE は NULL の重複を許すので、
    # 通常の push (NULL) には影響しない)。
    try:
        conn.execute(
            "ALTER TABLE perception_buffer ADD COLUMN ledger_outbox_id TEXT"
        )
    except sqlite3.OperationalError:
        pass  # 既に存在する
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_perception_buffer_ledger_outbox "
        "ON perception_buffer(ledger_outbox_id)"
    )
    # 消費バッチ (§10.2): 消費の単位とレンダリング済み文面の正準。
    # boundary_created_at / boundary_rowid = バッチ確定時点で最後に保存済み
    # だった message の正典順序キー (created_at, rowid)。Chronicle 無効ペルソナ
    # の窓絞りが anchor 行と同秒のバッチを正典順どおりに判定するための境界
    # (epoch 比較だけだと同秒の直前/直後が区別できない)。取れなければ NULL —
    # その行は epoch 比較へフォールバックする。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS perception_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            consumed_at INTEGER NOT NULL,
            pulse_id TEXT,
            episode_id TEXT,
            rendered_text TEXT NOT NULL,
            media TEXT,
            annexed_entry_id TEXT,
            boundary_created_at INTEGER,
            boundary_rowid INTEGER,
            room_state_json TEXT
        )
        """
    )
    # 既存 DB の追従 (境界キー列 = 本ワークツリー内の先行世代、
    # room_state_json = 部屋の様子の差分+移管を入れた 2026-09-05 より前の世代)。
    for ddl in (
        "ALTER TABLE perception_batches ADD COLUMN boundary_created_at INTEGER",
        "ALTER TABLE perception_batches ADD COLUMN boundary_rowid INTEGER",
        "ALTER TABLE perception_batches ADD COLUMN room_state_json TEXT",
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass  # 既に存在する
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_perception_batches_consumed_at "
        "ON perception_batches(consumed_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_perception_batches_annexed "
        "ON perception_batches(annexed_entry_id)"
    )
    # 提示の状態 (1 行だけ): 知覚の合計が上の水位を超えて「まとめて下ろした」
    # 境界。値は「この id までのバッチは提示に出さない」で、**一方向にしか
    # 進まない** (advance_presentation_cutoff)。台帳の行も付記印も触らない —
    # 下ろすのは提示だけで、その期間の編纂が来れば材料として引き取られる。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS perception_presentation (
            id TEXT PRIMARY KEY,
            dropped_through_batch_id INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER
        )
        """
    )
    if upgraded_from_two_phase:
        # 一度きりの清算 (2026-08-19 Codex 第七巡 #4): 旧二段 flush (event_message
        # を書く → pending を削除) が「書き終えたのに削除だけ失敗して」中断した
        # 状態の DB では、既に messages に行がある知覚が pending に残っている。
        # 新経路はその照合 (旧 C6) を持たないので、放置すると次の flush が同じ
        # 内容をバッチとして再消費し、legacy 行との**二重提示**になる。旧実装の
        # 後始末 (照合して削除だけやり直す) 相当をここで一度だけ実行する。
        _reconcile_interrupted_two_phase_flush(conn, resource_id=resource_id)
    conn.commit()


#: 中断 flush の照合で遡る余裕 (秒)。知覚が積まれた時刻と event_message の
#: 時刻は別々の壁時計読み取りなので、時計の巻き戻りに備える (旧 C6 の
#: PERCEPTION_LOOKBACK_SLACK_SEC と同じ値・同じ根拠)。
_RECONCILE_LOOKBACK_SLACK_SEC = 3600


def _reconcile_interrupted_two_phase_flush(
    conn: sqlite3.Connection, *, resource_id: Optional[str] = None,
) -> None:
    """旧二段 flush の中断残骸 (書き込み済み pending) を削除する (移行時一度きり)。

    messages の metadata.perception_ids (旧 C6 の冪等キー — 撤去済み
    ``_already_written_perception_ids`` と同じ照合) に現れる id を集め、
    未消費のまま残っている同 id の行を削除する。**バッチ化ではなく削除**を
    選ぶ理由: その知覚の本文は legacy event_message 行として既に提示されて
    おり、バッチを作ると同じ内容が二枚 (legacy 行 + マージブロック) 並ぶ —
    削除は旧実装の後始末 (削除だけやり直す) と同じ意味論で、提示は変わらない。

    走査は旧 C6 と同じ絞り (2026-08-19 Codex 第八巡 #4 — 全履歴の LIKE 走査は
    長寿ペルソナで移行を不必要に重くする): **pending が空なら何も読まない**。
    あるときは「最古 pending の created_at − 余裕 3600 秒」を下限に、
    (resource_id, created_at) の索引に乗る範囲だけを照合する — 中断 flush の
    event_message は、それが書き出した知覚より後に書かれているので必ずこの
    窓の中にいる。行はカーソルで逐次読み、JSON 解析後にタグを検証する。
    """
    import json
    try:
        oldest = conn.execute(
            "SELECT MIN(created_at) FROM perception_buffer "
            "WHERE consumed_at IS NULL"
        ).fetchone()
    except sqlite3.OperationalError:
        return
    if oldest is None or oldest[0] is None:
        return  # pending なし = 中断残骸なし。messages は一行も読まない。
    cutoff = int(oldest[0]) - _RECONCILE_LOOKBACK_SLACK_SEC
    try:
        params: list = [cutoff]
        resource_clause = ""
        if resource_id:
            resource_clause = "resource_id = ? AND "
            params.insert(0, resource_id)
        cursor = conn.execute(
            f"SELECT metadata FROM messages "
            f"WHERE {resource_clause}created_at >= ? "
            "AND metadata LIKE '%perception_ids%'",
            tuple(params),
        )
    except sqlite3.OperationalError:
        return  # messages を持たない DB (単体テスト等) — 清算対象なし
    written: set = set()
    for (meta_json,) in cursor:
        try:
            meta = json.loads(meta_json) if meta_json else None
        except (TypeError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        # タグ検証: 旧 flush の行は tags に event_message を持つ。LIKE の粗い
        # 一致 (本文中の偶然の文字列等) を perception_ids の実在 + タグで確定。
        tags = meta.get("tags")
        if not (isinstance(tags, list) and "event_message" in tags):
            continue
        ids = meta.get("perception_ids")
        if isinstance(ids, list):
            written.update(i for i in ids if isinstance(i, int))
    if not written:
        return
    deleted = 0
    id_list = sorted(written)
    for i in range(0, len(id_list), 500):
        chunk = id_list[i:i + 500]
        placeholders = ",".join("?" for _ in chunk)
        cur = conn.execute(
            f"DELETE FROM perception_buffer WHERE consumed_at IS NULL "
            f"AND id IN ({placeholders})",
            tuple(chunk),
        )
        deleted += int(cur.rowcount)
    if deleted:
        LOGGER.info(
            "[perception_buffer] one-time migration: removed %d pending item(s) "
            "already written as legacy event_message rows by an interrupted "
            "two-phase flush", deleted,
        )


def push_perception(
    conn: sqlite3.Connection,
    kind: str,
    content: str,
    *,
    reduce_key: Optional[str] = None,
    salient: bool = False,
    media: Optional[list] = None,
    metadata: Optional[str] = None,
    ledger_outbox_id: Optional[str] = None,
) -> Optional[int]:
    """知覚を 1 件バッファに積む。採番された id を返す。

    書き込みは客観時間で随時起きる (ペルソナはまだ知覚しない)。実際に知覚される
    のは次の Beat 頭の消費時 (``list_pending`` → reduce → format →
    ``create_consumption_batch``)。

    ``media`` は ``[{"path","mime_type","role"}, ...]`` の list (画像等)。JSON 化して
    保存し、提示時にマージブロックの metadata.media へ載せる。

    ``ledger_outbox_id``: 実行台帳の配送 (outbox) 由来のときの冪等キー。UNIQUE
    索引で DB 側が原子的に重複を弾く — 既に同じキーの行が居たら積まず **None**
    を返す (check-then-act の照合は同時配送の競合に破れる。2026-08-19 Codex
    第八巡 #1)。None (通常 push) は UNIQUE の対象外で従来どおり必ず積まれる。
    """
    now = int(time.time())
    media_json = None
    if media:
        import json
        media_json = json.dumps(media, ensure_ascii=False)
    cur = conn.execute(
        "INSERT OR IGNORE INTO perception_buffer "
        "(kind, content, reduce_key, salient, media, metadata, created_at, "
        "ledger_outbox_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            kind, content, reduce_key, 1 if salient else 0, media_json,
            metadata, now, ledger_outbox_id,
        ),
    )
    conn.commit()
    if cur.rowcount == 0:
        return None  # UNIQUE (ledger_outbox_id) の冪等スキップ
    return int(cur.lastrowid)


_SELECT_COLUMNS = (
    "id, kind, content, reduce_key, salient, media, metadata, created_at, "
    "consumed_at, consumed_batch_id"
)


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
        consumed_at=int(row[8]) if len(row) > 8 and row[8] is not None else None,
        consumed_batch_id=int(row[9]) if len(row) > 9 and row[9] is not None else None,
    )


_BATCH_SELECT_COLUMNS = (
    "id, consumed_at, pulse_id, episode_id, rendered_text, media, "
    "annexed_entry_id, boundary_created_at, boundary_rowid, room_state_json"
)


def _row_to_batch(row) -> PerceptionBatch:
    return PerceptionBatch(
        id=int(row[0]),
        consumed_at=int(row[1]),
        pulse_id=row[2] if row[2] is not None else None,
        episode_id=row[3] if row[3] is not None else None,
        rendered_text=str(row[4] or ""),
        media=row[5] if row[5] is not None else None,
        annexed_entry_id=row[6] if row[6] is not None else None,
        boundary_created_at=(
            int(row[7]) if len(row) > 7 and row[7] is not None else None
        ),
        boundary_rowid=int(row[8]) if len(row) > 8 and row[8] is not None else None,
        room_state_json=(
            row[9] if len(row) > 9 and row[9] is not None else None
        ),
    )


def list_pending(conn: sqlite3.Connection) -> List[PerceptionItem]:
    """未消費の知覚を発生順 (created_at → id 昇順) で全件返す。

    消費 (Beat 頭の flush) とプレビュー (任意タイミング) の両方がこの読み口を
    使う。プレビューは読むだけ、消費は読んで整形後に
    ``create_consumption_batch`` でバッチを確定する (行は削除しない — §10.2)。
    """
    rows = conn.execute(
        f"SELECT {_SELECT_COLUMNS} FROM perception_buffer "
        "WHERE consumed_at IS NULL ORDER BY created_at ASC, id ASC"
    ).fetchall()
    return [_row_to_item(row) for row in rows]


def count_pending(conn: sqlite3.Connection, kind: str) -> int:
    """未消費の知覚のうち指定 ``kind`` の件数を返す。

    フィード配送の膨張ガード (saiverse/feed_manager.py) が「これ以上積まない」
    判定に使う読み口。
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM perception_buffer "
        "WHERE kind = ? AND consumed_at IS NULL", (kind,)
    ).fetchone()
    return int(row[0]) if row else 0


def create_consumption_batch(
    conn: sqlite3.Connection,
    item_ids: List[int],
    *,
    consumed_at: int,
    rendered_text: str,
    pulse_id: Optional[str] = None,
    episode_id: Optional[str] = None,
    media: Optional[list] = None,
    boundary_created_at: Optional[int] = None,
    boundary_rowid: Optional[int] = None,
    room_state_json: Optional[str] = None,
) -> int:
    """消費バッチを単一トランザクションで確定する (§10.2)。

    バッチ行 (レンダリング済み文面 = ペルソナが見た文そのもの) を INSERT し、
    消費した項目に (consumed_at, batch_id) の印を打つ。「知覚した」の証跡は
    このバッチであって messages の行ではない (§10.1)。旧 flush の「メッセージ
    行を書く → 削除」二段が持っていた二度書きの口 (C6) は、消費がこの単一 tx に
    なったことで構造ごと消える。

    未消費でない id が混ざっていたら (呼び出し側の並び違反) rollback して
    ValueError — 消費済みの再消費 (C2 違反) を部分成立させない。

    ``room_state_json``: このバッチに含まれる「部屋の様子」の記帳
    (:func:`sai_memory.room_state.collect_batch_room_states` が組む)。

    Returns: 確定したバッチ id。
    """
    if not item_ids:
        raise ValueError("create_consumption_batch requires at least one item id")
    media_json = None
    if media:
        import json
        media_json = json.dumps(media, ensure_ascii=False)
    placeholders = ",".join("?" for _ in item_ids)
    try:
        cur = conn.execute(
            "INSERT INTO perception_batches "
            "(consumed_at, pulse_id, episode_id, rendered_text, media, "
            "annexed_entry_id, boundary_created_at, boundary_rowid, "
            "room_state_json) "
            "VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)",
            (
                int(consumed_at), pulse_id, episode_id, rendered_text, media_json,
                int(boundary_created_at) if boundary_created_at is not None else None,
                int(boundary_rowid) if boundary_rowid is not None else None,
                room_state_json,
            ),
        )
        batch_id = int(cur.lastrowid)
        touched = conn.execute(
            f"UPDATE perception_buffer SET consumed_at = ?, consumed_batch_id = ? "
            f"WHERE id IN ({placeholders}) AND consumed_at IS NULL",
            (int(consumed_at), batch_id, *item_ids),
        ).rowcount
        if int(touched) != len(item_ids):
            raise ValueError(
                f"consumption batch covers {len(item_ids)} item(s) but only "
                f"{touched} were pending — refusing partial consumption"
            )
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    return batch_id


def latest_message_boundary(
    conn: sqlite3.Connection, *, strict: bool = False,
) -> tuple:
    """バッチ確定時点で最後に保存済みの message の正典順序キー ``(created_at, rowid)``。

    message がまだ無ければ ``(None, None)`` — その行は epoch 比較へフォール
    バックする。通常の flush (saiverse_memory/adapter.py) と機構の置き直し
    (:func:`insert_presentation_batch` の呼び出し側) が同じ規則で境界キーを
    記帳するための一点。

    ``strict=True`` は**読みの失敗** (テーブル不在以外の OperationalError —
    ロック等) を例外のまま伝える — 「message がまだ無い」という正当な
    ``(None, None)`` に化かさない (2026-09-06 五巡目修正 3)。使い手は置き直し
    (:func:`sai_memory.room_state.reseat_current_room`): 置き直しバッチの
    ``consumed_at`` は意図的に最古なので、キーなしで積むと窓判定
    (:func:`batch_in_window`) の epoch フォールバックで窓の外に立ち、次の
    検知が「運搬役が見えない」と判定してまた置き直す — 単発の読み取り失敗が
    全文バッチの重複を生む。通常の flush は ``strict=False`` のまま — 新規
    バッチの ``consumed_at`` は現在時刻 (提示の末尾) なので、キーなしの epoch
    フォールバックが実害の形にならない。
    """
    try:
        row = conn.execute(
            "SELECT created_at, rowid FROM messages "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        if row is not None and row[0] is not None:
            return (int(row[0]), int(row[1]))
    except sqlite3.OperationalError as exc:
        if strict:
            from sai_memory.arasuji.storage import is_missing_table_error
            if not is_missing_table_error(exc):
                raise
        # messages テーブルの無い DB (単体テスト等) は「message がまだ無い」
        # と同じ縮退 — strict でも (None, None) でよい (窓の材料も存在しない)。
    return (None, None)


def batch_in_window(batch: PerceptionBatch, anchor_key: Optional[tuple]) -> bool:
    """Chronicle 無効ペルソナの提示窓 (anchor) にこのバッチが入るか。

    窓は正典順序キー ``(created_at, rowid)`` が anchor 以上の行。バッチは確定
    時点の境界キー (最後に保存済みだった行のキー) で同じ比較をする — anchor と
    同秒でも「anchor 行より前に確定したバッチ」だけが窓の外になる。境界キーの
    無い旧バッチは ``consumed_at`` の epoch 比較へフォールバック。
    ``anchor_key`` が None (窓なし) なら常に True。

    提示の組成 (sea/runtime_context.list_presented_perception_blocks) と、
    検知の瞬間の自己回復判定 (sea/head_pipeline/integration.py) が同じ規則を
    共有するための一点 — 二枚書くと窓の解釈がずれる。
    """
    if anchor_key is None:
        return True
    if batch.boundary_created_at is not None and batch.boundary_rowid is not None:
        return (batch.boundary_created_at, batch.boundary_rowid) >= anchor_key
    return batch.consumed_at >= anchor_key[0]


class WindowResolutionError(RuntimeError):
    """提示窓の解決に失敗した — 「窓なし」と区別する三値の「判定不能」。

    :func:`resolve_window_key` は三値を返す/送出する: 窓キー (tuple) / 窓なし
    (None — 正当) / 解決失敗 (この例外)。失敗を None に畳むと「窓なし = 全件
    可視」になり、検知が床で隠れた運搬役を「見えている」と誤判定して自己回復を
    抑止する — 「検知は見えない側にしか倒れない」契約の破れ (2026-09-06 四巡目
    修正 2)。逆に「全部見えない」へ倒すと、置き直しで作った新しいバッチまで
    見えない扱いになり、失敗が続く限り毎検知で置き直しが積もる。受け手
    (saiverse_memory/adapter.latest_room_snapshot → sea/head_pipeline/
    integration の検知) はその回の部屋の判定そのものを見送り、WARN を出して
    次の検知でやり直す。提示側は床を ``floor_epoch`` (純計算) で渡すので、
    この例外を出さない — 提示の挙動は変わらない。
    """


def resolve_window_key(
    conn: sqlite3.Connection,
    anchor_id: Optional[str],
    *,
    floor_epoch: Optional[int] = None,
    floor_chars: Union[int, Callable[[], int], None] = None,
) -> Optional[tuple]:
    """提示窓の起点キーの解決の一枚: anchor 行 → 引けなければ床。None = 窓なし。

    提示の組成 (sea/runtime_context._window_predicate_locked) と検知の読み・
    自己回復 (saiverse_memory/adapter の latest_room_snapshot /
    reseat_room_state) は
    **必ずこの関数でキーを解決し**、包含は :func:`batch_in_window` で判定する。
    解決の規則を二枚書くと、anchor が読めない劣化時に「提示では部屋が窓の外
    なのに検知は見えている扱い」の形で両側が割れる (2026-09-06 三巡目 #1)。

    返りは三値: 窓キー (tuple) / 窓なし (None — anchor も床の材料も正当に
    無い)。床 (``floor_chars``) の**読みが失敗**した回と、床の近似そのものが
    成立しない回 (:func:`_window_floor_key` の「判定不能」— 履歴は有るのに
    正典キーを作れる行が無い) は :class:`WindowResolutionError` を送出する —
    「読めなかった・判定できなかった」を「窓なし」に化かさない (2026-09-06
    四巡目修正 2)。

    解決の順序:

    1. ``anchor_id`` の messages 行が読めれば、その正典順序キー
       ``(created_at, rowid)``。
    2. 読めなければ床 (窓の近似) へフォールバック。床は呼び出し側の手元に
       ある材料で渡す:

       - ``floor_epoch`` — 提示側: recent (提示中の生ログ) の最古行の epoch。
         擬似キー ``(floor_epoch, 0)`` になる (rowid 0 はどの実行より小さい
         ので、同秒は見える側に倒れる)。
       - ``floor_chars`` — 検知側 (recent を持たない): messages の末尾
         ``floor_chars`` 文字ぶんに入る最古行のキー。提示の最小ロード
         (sea/runtime_context._minimal_load_chars) と同じ予算を渡すことで
         「anchor が死んだときに提示が実際に読む窓」を近似する。フィルタ
         (line_role / thread) をかけない全行の勘定なので、窓は提示側の床
         **以上** (= 見えない側) に倒れる — 検知が余分に発火しても全文一枚で
         済むが、逆向き (検知だけ見えている扱い) は自己回復を殺す。
         **予算は int のほか、呼ぶと int を返す遅延の口 (callable) でも渡せる**
         — 床の読みはこの床の枝に入った回だけ行う (2026-09-06 五巡目修正 2:
         呼び出し側で先に解決すると、床の一時失敗が正当な anchor で判定できる
         回まで「窓の解決失敗」に巻き込む)。

    3. どの材料も無ければ None (= 窓なし・全部見える)。両側とも同じ None に
       落ちるので、完全ブートストラップ (anchor も履歴も無い) の全提示は
       これまでどおり両側で一致する。
    """
    if anchor_id:
        try:
            row = conn.execute(
                "SELECT created_at, rowid FROM messages WHERE id = ?",
                (str(anchor_id),),
            ).fetchone()
            if row is not None and row[0] is not None:
                return (int(row[0]), int(row[1]))
        except Exception:
            pass  # 読めない = 「行なし」と同じ扱いで床へ (旧二枚と同じ寛さ)
    if floor_epoch is not None:
        return (int(floor_epoch), 0)
    if floor_chars is not None:
        if callable(floor_chars):
            # 床の予算の遅延解決 (2026-09-06 五巡目修正 2) — 床は「anchor の
            # 行が読めないときの代替」の材料なので、読みはこの枝に入った回
            # だけ行う。口の失敗は WindowResolutionError のまま伝播する
            # (三値の意味は変わらない)。
            floor_chars = floor_chars()
        return _window_floor_key(conn, int(floor_chars))
    return None


def _window_floor_key(
    conn: sqlite3.Connection, floor_chars: int,
) -> Optional[tuple]:
    """messages の末尾 ``floor_chars`` 文字ぶんに入る最古行の正典順序キー。

    :func:`resolve_window_key` の検知側フォールバック専用。候補 (正典キー
    ``(created_at, rowid)`` を作れる行) を新しい側から ``LENGTH(content)``
    で積み、予算を超える行の手前で止める — 提示の文字勘定
    (adapter.recent_persona_messages: ``len(content)`` を積んで超えたら
    打ち切り) と同じ規則。

    返りは四状態 (2026-09-06 十三巡目 — 十二巡目の三状態に「判定不能」を
    足して契約を言い切った):

    - **通常**: 予算内に収まる最古の候補行のキー。
    - **一行窓**: 予算に収まる行が一つも無い — 最新の一行だけで予算を
      超える・予算が 0 以下・空内容の並びで勘定が進まない、すべて —
      なら**最新の候補行そのもの**が床 (最新一行だけの保守的な窓)。
      None (= 窓なし・全件可視) に畳むと、巨大な最新メッセージがあるだけで
      検知が床の外の古い運搬役を「見えている」と誤認して自己回復を抑止する
      (「検知は見えない側にしか倒れない」契約の破れ — 四巡目修正 2 と同じ
      契約)。狭い窓に倒しても置き直しはループしない — 置き直しバッチの
      境界キーは最新の message なので ``>=`` の包含で窓の内に立つ。
    - **窓なし** (messages に行が無い): None — 正当な全件可視。
    - **判定不能**: 履歴は有るのに候補が一つも無い (created_at が全行
      NULL — スキーマは NULL を許す) は :class:`WindowResolutionError`。
      None に畳むと「履歴が有るのに全件可視」へ反転し (上と同じ契約の
      破れ)、最古の広い窓に倒しても向きが逆 — 正典キーの秩序が壊れた DB は
      近似そのものが成立しないので、受け手 (検知) がその回の判定を WARN で
      見送る (四巡目修正 2 の三値と同じ向き)。

    **読みの失敗も** :class:`WindowResolutionError` — None (= 床なし =
    窓なし・全件可視) に畳むと、床が要る劣化の回に検知だけ「全部見える」へ
    倒れて自己回復を抑止する (2026-09-06 四巡目修正 2)。
    """
    floor: Optional[tuple] = None
    newest: Optional[tuple] = None
    saw_unkeyed_row = False
    consumed = 0
    try:
        rows = conn.execute(
            "SELECT created_at, rowid, LENGTH(COALESCE(content, '')) "
            "FROM messages ORDER BY created_at DESC, rowid DESC"
        )
        for created_at, rowid, chars in rows:
            if created_at is None:
                # 正典キーを作れない行 — 床の候補にならない (候補が一つでも
                # あれば素通し、全行これなら下の「判定不能」)。
                saw_unkeyed_row = True
                continue
            key = (int(created_at), int(rowid))
            if newest is None:
                newest = key
                if floor_chars <= 0:
                    break  # 予算がそもそも行を許さない — 一行窓で確定
            consumed += int(chars or 0)
            if consumed > floor_chars:
                break
            floor = key
    except Exception as exc:
        raise WindowResolutionError(
            "could not read the messages ledger to approximate the window floor"
        ) from exc
    if floor is not None:
        return floor  # 通常
    if newest is not None:
        return newest  # 一行窓 (予算に収まる行が無い)
    if saw_unkeyed_row:
        raise WindowResolutionError(
            "the messages ledger is non-empty but no row carries a canonical "
            "(created_at, rowid) key — the window floor cannot be approximated"
        )
    return None  # 窓なし (履歴空)


def insert_presentation_batch(
    conn: sqlite3.Connection,
    *,
    consumed_at: int,
    rendered_text: str,
    media: Optional[list] = None,
    room_state_json: Optional[str] = None,
    boundary_created_at: Optional[int] = None,
    boundary_rowid: Optional[int] = None,
) -> int:
    """知覚項目の消費を伴わないバッチ行を追加する。**commit しない**。

    使い手は「部屋の様子」の機構の置き直し
    (:func:`sai_memory.room_state.reseat_current_room`) だけ — 既に知覚済みの
    部屋の全文を、提示の最古端 (``consumed_at`` を残る提示より古い時刻にする)
    へ立て直すための行で、新しい知覚を作るものではない (perception_buffer.md
    C1 は破らない)。台帳の既存の行・バッチは書き換えない — 追加だけ。

    こうして作られたバッチは ``room_state_json`` のエントリに ``reseated`` の
    印を持ち、(a) 知覚の合計上限の下ろし候補から外れ (id と consumed_at の
    順序が食い違うため — sea/runtime_context._plan_perception_drop)、(b) 編纂の
    付記では印だけ受けて材料には載らない (機構の置き直しは出来事ではない —
    sai_memory/arasuji/executor.collect_annex_items)。
    """
    cur = conn.execute(
        "INSERT INTO perception_batches "
        "(consumed_at, pulse_id, episode_id, rendered_text, media, "
        "annexed_entry_id, boundary_created_at, boundary_rowid, room_state_json) "
        "VALUES (?, NULL, NULL, ?, ?, NULL, ?, ?, ?)",
        (
            int(consumed_at),
            rendered_text,
            _encode_media(media),
            int(boundary_created_at) if boundary_created_at is not None else None,
            int(boundary_rowid) if boundary_rowid is not None else None,
            room_state_json,
        ),
    )
    return int(cur.lastrowid)


def _encode_media(media: Optional[list]) -> Optional[str]:
    if not media:
        return None
    import json
    return json.dumps(media, ensure_ascii=False)


def list_unannexed_batches(
    conn: sqlite3.Connection,
    *,
    since: Optional[int] = None,
    before: Optional[int] = None,
) -> List[PerceptionBatch]:
    """付記印のない消費バッチを consumed_at → id 昇順で返す。

    退場付記 (§10.4) の読み口 = 「まだ編纂に引き取られていない」全件。**提示は
    こちらではなく** :func:`list_presented_batches` を読む — 知覚の合計上限で
    下ろした境界より古いバッチは、未付記のまま提示にだけ出なくなるため
    (§10.9)。台帳から消えるわけではないので、材料集めはここを読み続ける。
    ``since`` (以上) / ``before`` (未満) は付記スパンの絞り込み用。
    """
    sql = (
        f"SELECT {_BATCH_SELECT_COLUMNS} FROM perception_batches "
        "WHERE annexed_entry_id IS NULL"
    )
    params: List[int] = []
    if since is not None:
        sql += " AND consumed_at >= ?"
        params.append(int(since))
    if before is not None:
        sql += " AND consumed_at < ?"
        params.append(int(before))
    sql += " ORDER BY consumed_at ASC, id ASC"
    rows = conn.execute(sql, tuple(params)).fetchall()
    return [_row_to_batch(row) for row in rows]


#: 提示の状態を持つ 1 行のキー (テーブルは 1 ペルソナ 1 行)。
_PRESENTATION_STATE_ID = "main"


def get_presentation_cutoff(conn: sqlite3.Connection) -> int:
    """知覚の合計上限で「まとめて下ろした」境界 (この id までは提示に出ない)。

    まだ一度も下ろしていない / テーブルの無い DB なら 0 = 全部が提示に出る。
    テーブル不在**以外**の失敗 (ロック等) は raise する — 0 を返すと「一度も
    下ろしていない」と同じ顔になり、下ろしたはずのバッチが提示へ戻る
    (§10.9 の一方向性が読み取り事故で破れる。Codex 三巡 F2 と同じ型)。
    """
    try:
        row = conn.execute(
            "SELECT dropped_through_batch_id FROM perception_presentation "
            "WHERE id = ?", (_PRESENTATION_STATE_ID,),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        from sai_memory.arasuji.storage import is_missing_table_error
        if is_missing_table_error(exc):
            return 0  # 下ろす仕組みより古い DB = まだ一度も下ろしていない
        raise
    return int(row[0]) if row and row[0] is not None else 0


def advance_presentation_cutoff(
    conn: sqlite3.Connection, batch_id: int,
    *, in_window: Optional[Callable[[PerceptionBatch], bool]] = None,
) -> int:
    """下ろした境界を ``batch_id`` まで進める。**commit しない**。

    **一方向にしか進まない** — 既にそれ以上まで進んでいれば何もしない。この
    片道性が設計の核 (docs/intent/perception_buffer.md §10.9): 下ろす瞬間に
    プロンプトキャッシュの前方一致が割れるのは一回きりで、下ろしたバッチが
    提示に戻ってまた割れる揺り戻しを構造的に禁じる。後退は DB 側の UPSERT の
    条件でも弾く (同時に走った二本のうち小さい方が勝たない)。負けた側も
    返すのは**読み直した実境界** — 自分の ``batch_id`` を名乗ると、呼び出し
    側がその値で提示を組んで、勝った側が既に下ろしたバッチを再提示する
    (2026-09-06 十巡目)。

    境界が実際に進んだら、続けて「部屋の様子」の置き直し
    (:func:`sai_memory.room_state.reseat_current_room`) と土台の回復
    (:func:`sai_memory.room_state.restore_room_state_bases`) を
    **この tx の中で**走らせる — 可視性が変わる瞬間の一つなので、不変条件
    「今いる部屋の全体像が提示のどこかに見えている」「提示に見えているどの
    差分も自分の土台が直前に見えている」をここで守る (付記で下りるときと
    同じ扱い。§10.8 / room_state_packages.md §6-4)。

    ``in_window`` は置き直しの運搬役判定に渡す提示窓の篩
    (:func:`~sai_memory.room_state.reseat_current_room` の同名引数へそのまま
    配線する。None = 窓なし)。境界前進は Chronicle 無効ペルソナの提示組成
    (sea/runtime_context.list_presented_perception_blocks — 窓 = anchor /
    recent の床) からも起きるので、窓を持つ呼び出し側は自分の組成と同じ
    述語を渡す — 渡さないと、窓の外に立つ古い同部屋束を運搬役に数えて
    置き直しが抑止され、境界だけが確定してそのペルソナの提示から部屋の
    全文が一拍 (次の Pulse 頭の自己回復まで) 消える (2026-09-06 九巡目修正
    1)。もう一つの hook (:func:`mark_batches_annexed` — 編纂の付記) は
    Chronicle 有効のみの経路で窓の概念が無いため、篩なしのまま。

    置き直し・回復のどちらかが失敗したら**この tx を rollback して例外を送出
    する** — 境界だけが進むと、部屋の運搬役や土台の全文バッチが提示から下りた
    まま戻せない (検知の自己回復より先に送信が起きる経路では部屋なしで送られ
    る)。境界の前進は一方向で取り消せないので、中途半端に進めるより「何も
    しなかった」へ倒す (次の機会に全体をやり直す)。

    Returns: 進めた後の**実境界** (UPSERT 後に DB の行を読み直した値。呼び出し
        前より小さくはならず、別の接続が先に更に先へ進めていたら ``batch_id``
        より大きい)。読み直した値が ``batch_id`` に届いていなければ書き込み
        自体の異常 (ガードで負ける相手は「より大きい値」だけ) なので、この tx
        を rollback して RuntimeError を送出する。
    """
    current = get_presentation_cutoff(conn)
    target = int(batch_id)
    if target <= current:
        return current
    conn.execute(
        "INSERT INTO perception_presentation "
        "(id, dropped_through_batch_id, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "dropped_through_batch_id = excluded.dropped_through_batch_id, "
        "updated_at = excluded.updated_at "
        "WHERE excluded.dropped_through_batch_id > "
        "perception_presentation.dropped_through_batch_id",
        (_PRESENTATION_STATE_ID, target, int(time.time())),
    )
    # 行の実物を読み直す — 上の current の読みと UPSERT の間に別接続が先に
    # もっと大きい境界へ進めて commit していたら、こちらの書き込みはガードで
    # 無効になり、境界は相手の値のまま。target を返すと、呼び出し側がその値で
    # 提示を組んで、もう下ろされたバッチを再提示する (2026-09-06 十巡目)。
    advanced = get_presentation_cutoff(conn)
    if advanced < target:
        conn.rollback()
        raise RuntimeError(
            f"perception presentation cutoff did not advance: wrote {target} "
            f"but the row reads {advanced} (the guard only loses to a larger "
            "value, so this is a failed write, not a race)"
        )
    from sai_memory.room_state import reseat_current_room, restore_room_state_bases
    # 今いる部屋の最後の運搬役がこの前進で下りたら、最新の全文を提示の最古端へ
    # 置き直す (room_state_packages.md §6-4 — 同一 tx なので「置き直してから
    # 下ろした」のと外からは区別がつかない)。置き直しに失敗したら前進ごと
    # rollback して見送る — 境界だけが進むと「今いる部屋の全体像が提示の
    # どこにも無い」状態が確定し、検知の自己回復 (§6-2) より先に送信が起きる
    # 経路 (検知を通らない組成) では部屋なしで送られる。運搬役の判定には
    # 呼び出し側の提示窓の篩 (in_window) をそのまま通す — 窓の外の束は
    # この提示に出ないので、運搬役に数えてはいけない。
    try:
        reseat_current_room(conn, in_window=in_window)
    except Exception:
        conn.rollback()
        LOGGER.error(
            "[perception] could not reseat the current room while advancing "
            "the presentation cutoff to %s; rolled the whole step back so the "
            "cutoff stays where it was", target, exc_info=True,
        )
        raise
    try:
        restore_room_state_bases(conn)
    except Exception:
        conn.rollback()
        LOGGER.error(
            "[perception] the room-state base repair failed while advancing the "
            "presentation cutoff to %s; rolled the whole step back so the "
            "cutoff stays where it was", target, exc_info=True,
        )
        raise
    return advanced


def _list_batches_by_cutoff(
    conn: sqlite3.Connection, *, above: bool, cutoff: Optional[int],
) -> List[PerceptionBatch]:
    if cutoff is None:
        cutoff = get_presentation_cutoff(conn)
    op = ">" if above else "<="
    rows = conn.execute(
        f"SELECT {_BATCH_SELECT_COLUMNS} FROM perception_batches "
        f"WHERE annexed_entry_id IS NULL AND id {op} ? "
        "ORDER BY consumed_at ASC, id ASC",
        (int(cutoff),),
    ).fetchall()
    return [_row_to_batch(row) for row in rows]


def list_presented_batches(
    conn: sqlite3.Connection, *, cutoff: Optional[int] = None,
) -> List[PerceptionBatch]:
    """いま提示に出るバッチ = 未付記 **かつ** 下ろした境界より新しいもの。

    提示 (§10.3) と、提示の可視性に依存する判定 (部屋の様子の土台 §10.8) の
    読み口。編纂の材料集め (arasuji executor の
    :func:`~sai_memory.arasuji.executor.collect_annex_items`) は**こちらを
    使わない** — 下ろしたのは提示だけで、台帳の行も付記印もそのままなので、
    その期間の編纂が来れば従来どおり材料として引き取られる。
    """
    return _list_batches_by_cutoff(conn, above=True, cutoff=cutoff)


def list_dropped_batches(
    conn: sqlite3.Connection, *, cutoff: Optional[int] = None,
) -> List[PerceptionBatch]:
    """下ろされたまま、まだ編纂に引き取られていないバッチ (境界以下・未付記)。

    提示に置く省略の印 (「N 件を省略」) の件数と位置がここから決まる。付記が
    済んだバッチは Chronicle の digest がその位置を語るので数から外れる。
    """
    return _list_batches_by_cutoff(conn, above=False, cutoff=cutoff)


def count_batch_records(
    conn: sqlite3.Connection, batch_ids: Sequence[int],
) -> Dict[int, int]:
    """バッチごとの「確定文面に出ていた記録の件数」(下限) を返す。

    1 枚のバッチは複数の知覚記録を束ねる — Beat 頭の消費は、その時点で未消費
    だったものを全部まとめて 1 つの文面にするため。だから省略の印の件数は
    バッチ数ではなくここの合計で出す (バッチ数だけを数えると、部屋の様子 3 件
    + 通知 5 件を束ねた 1 枚が「1 件」になる)。

    数え方は**消費時と同じ規則**をもう一度かけたもの: そのバッチの台帳の行を
    発生順 (created_at → id) で読み直し、:func:`reduce_perceptions` を通した
    件数を数える。消費は「その時点の未消費全件」を ``item_ids`` に渡し、本文は
    reduce 後の項目からだけ組まれるので、これが文面に出ていた記録の数と一致
    する。本文は読まない (下ろされたバッチの本文は 10 万字規模になりうる) —
    reduce に要るのは ``kind`` と ``reduce_key`` だけ。

    行を引けないバッチ (台帳の行を消した / テーブルの無い DB) は **1** と数える。
    「少なくとも 1 件はここにあった」は必ず真なので、合計は常に下限になる —
    印の文面もそれに合わせて「N 件以上」と書く。
    """
    ids = [int(b) for b in batch_ids]
    counts: Dict[int, int] = {}
    if not ids:
        return counts
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        placeholders = ",".join("?" for _ in chunk)
        try:
            rows = conn.execute(
                "SELECT consumed_batch_id, id, kind, reduce_key, created_at "
                f"FROM perception_buffer WHERE consumed_batch_id IN ({placeholders}) "
                "ORDER BY created_at ASC, id ASC",
                tuple(chunk),
            ).fetchall()
        except sqlite3.OperationalError:
            LOGGER.warning(
                "[perception_buffer] could not read the ledger rows behind the "
                "dropped batches; counting one record per batch (the omission "
                "mark stays a lower bound)", exc_info=True,
            )
            rows = []
        grouped: Dict[int, List[PerceptionItem]] = {}
        for batch_id, item_id, kind, reduce_key, created_at in rows:
            if batch_id is None:
                continue
            grouped.setdefault(int(batch_id), []).append(PerceptionItem(
                id=int(item_id), kind=str(kind), content="",
                reduce_key=reduce_key, salient=0, media=None, metadata=None,
                created_at=int(created_at or 0),
            ))
        for batch_id in chunk:
            items = grouped.get(batch_id)
            counts[batch_id] = len(reduce_perceptions(items)) if items else 1
    return counts


def mark_batches_annexed(
    conn: sqlite3.Connection, batch_ids: List[int], entry_id: str,
) -> int:
    """バッチに付記印 (転写先 Chronicle エントリ id) を打つ。**commit しない**。

    Chronicle チャンクの digest 確定と同一トランザクションで呼ぶ契約 (§10.4) —
    呼び出し元 (arasuji executor) の tx が rollback すれば印も戻り、バッチは
    未付記 = 提示に残る (fail-open)。

    印が 1 行でも立ったら、続けて「部屋の様子」の置き直し
    (:func:`sai_memory.room_state.reseat_current_room`) と土台の回復
    (:func:`sai_memory.room_state.restore_room_state_bases`) を
    **この tx の中で**走らせる。全文のバッチが提示から下りる瞬間に、部屋の
    運搬役を置き直し、土台を失った差分エントリを全文へ差し替える一点がここ —
    提示の書き換えを編纂の発火に相乗りさせ、プロンプトキャッシュの壊れ時点を
    増やさないための配置なので、付記なしでこの回復だけを呼んではいけない。

    置き直し・回復のどちらかが失敗したら**この tx を rollback して例外を送出
    する** — 付記だけが確定すると、部屋の運搬役や土台の全文バッチが提示から
    下りたまま戻せない (差分は付記済みバッチを土台にできず、部屋なしの状態は
    検知の自己回復より先に送信が起きる経路で部屋なしのまま送られる)。呼び出し
    元の digest ごと巻き戻し、次の編纂でチャンクごとやり直す。

    Returns: 印を打てた行数 (未付記だった行のみ)。
    """
    if not batch_ids or not entry_id:
        return 0
    placeholders = ",".join("?" for _ in batch_ids)
    cur = conn.execute(
        f"UPDATE perception_batches SET annexed_entry_id = ? "
        f"WHERE id IN ({placeholders}) AND annexed_entry_id IS NULL",
        (str(entry_id), *batch_ids),
    )
    stamped = int(cur.rowcount)
    if stamped:
        from sai_memory.room_state import reseat_current_room, restore_room_state_bases
        # 今いる部屋の最後の運搬役がこの付記で下りたら、最新の全文を提示の
        # 最古端へ置き直す (room_state_packages.md §6-4 — 付記と同一 tx なので
        # 「置き直してから下ろした」のと外からは区別がつかない)。置き直しに
        # 失敗したら付記ごと rollback して見送る — 付記だけが確定すると
        # 「今いる部屋の全体像が提示のどこにも無い」状態が確定し、検知の
        # 自己回復 (§6-2) より先に送信が起きる経路では部屋なしで送られる。
        # in_window は渡さない (篩なし) — 付記は編纂の畳みで、編纂は
        # Chronicle 有効のペルソナにしか走らず、提示窓 (anchor) は Chronicle
        # 無効の忘れ方なので、この経路に窓の概念は無い。窓を持ちうるもう
        # 一方の hook (advance_presentation_cutoff — Chronicle 無効の組成
        # からも呼ばれる) は、呼び出し側から同名引数で篩を受け取る
        # (2026-09-06 九巡目修正 1)。
        try:
            reseat_current_room(conn)
        except Exception:
            conn.rollback()
            LOGGER.error(
                "[perception] could not reseat the current room while stamping "
                "%d batch(es) for entry %s; rolled the whole transaction back "
                "so the annexation is retried as a whole",
                stamped, entry_id, exc_info=True,
            )
            raise
        try:
            restore_room_state_bases(conn)
        except Exception:
            conn.rollback()
            LOGGER.error(
                "[perception] the room-state base repair failed while stamping "
                "%d batch(es) for entry %s; rolled the whole transaction back "
                "so the annexation is retried as a whole",
                stamped, entry_id, exc_info=True,
            )
            raise
    return stamped


def list_batches_annexed_to(
    conn: sqlite3.Connection, entry_id: str,
) -> List[PerceptionBatch]:
    """指定 Chronicle エントリへ付記済みのバッチを consumed_at → id 昇順で返す。

    Chronicle 再生成 (regenerate_entry) が旧 entry の付記を replacement へ
    継承するための読み口 (2026-08-19 Codex 第三巡 #3)。
    """
    if not entry_id:
        return []
    try:
        rows = conn.execute(
            f"SELECT {_BATCH_SELECT_COLUMNS} FROM perception_batches "
            "WHERE annexed_entry_id = ? ORDER BY consumed_at ASC, id ASC",
            (str(entry_id),),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        from sai_memory.arasuji.storage import is_missing_table_error
        if is_missing_table_error(exc):
            return []  # 知覚台帳の無い DB (旧テスト等)
        raise  # ロック等 — 「付記バッチなし」の顔をしない (Codex 三巡 F2)
    return [_row_to_batch(row) for row in rows]


def reassign_batches_annexed(
    conn: sqlite3.Connection, old_entry_id: str, new_entry_id: str,
) -> int:
    """付記印を旧 entry から新 entry へ付け替える。**commit しない**。

    Chronicle 再生成の swap 用 — replacement 本文への転写 (継承) と同一 tx で
    呼ぶ契約。付け替え後は旧 entry の削除 (unmark) が no-op になる。
    """
    if not old_entry_id or not new_entry_id:
        return 0
    try:
        cur = conn.execute(
            "UPDATE perception_batches SET annexed_entry_id = ? "
            "WHERE annexed_entry_id = ?",
            (str(new_entry_id), str(old_entry_id)),
        )
    except sqlite3.OperationalError as exc:
        from sai_memory.arasuji.storage import is_missing_table_error
        if is_missing_table_error(exc):
            return 0  # 知覚台帳の無い DB (旧テスト等)
        raise  # ロック等 — 付け替え失敗を件数 0 の顔にしない (Codex 三巡 F2)
    return int(cur.rowcount)


def unmark_batches_annexed(
    conn: sqlite3.Connection, entry_ids: List[str],
) -> int:
    """指定 Chronicle エントリへの付記印を戻す。**commit しない**。

    Chronicle エントリを削除する経路 (個別削除・全削除・再生成 swap・dismantle
    等) は、削除と同一トランザクションで必ずこれを通す — 付記印は entry id を
    指すので、entry だけ消すと「付記済み = 提示に出ない」のに転写先も無い =
    知覚の恒久消失になる (下限違反, 2026-08-19 Codex 第二巡 #1)。印が戻った
    バッチは提示に再登場し、次の編纂の一括回収 (recover_before) が引き取る
    (rendered_text はバッチ自身が持つので転写本文の引き継ぎは不要)。

    ``entry_ids`` が空なら 0。テーブルの無い DB (旧テスト・別用途 conn) は
    黙って 0 (戻すものが無い)。
    """
    if not entry_ids:
        return 0
    placeholders = ",".join("?" for _ in entry_ids)
    try:
        cur = conn.execute(
            f"UPDATE perception_batches SET annexed_entry_id = NULL "
            f"WHERE annexed_entry_id IN ({placeholders})",
            tuple(str(e) for e in entry_ids),
        )
    except sqlite3.OperationalError as exc:
        from sai_memory.arasuji.storage import is_missing_table_error
        if is_missing_table_error(exc):
            return 0  # perception_batches の無い DB
        raise  # ロック等 — 印を戻せていないのに削除へ進ませない (Codex 三巡 F2)
    return int(cur.rowcount)


def list_consumed_since(
    conn: sqlite3.Connection, since_epoch: int,
) -> List[PerceptionItem]:
    """``since_epoch`` 以降に消費された知覚項目を返す (台帳の読み口 / 検証用)。

    提示は本関数ではなく :func:`list_unannexed_batches` (確定文面) を読む —
    生の項目からの再構成は reduce で消えた中間状態を復活させるため提示には
    使わない (§10.2)。順序は consumed_at → created_at → id。
    """
    rows = conn.execute(
        f"SELECT {_SELECT_COLUMNS} FROM perception_buffer "
        "WHERE consumed_at IS NOT NULL AND consumed_at >= ? "
        "ORDER BY consumed_at ASC, created_at ASC, id ASC",
        (int(since_epoch),),
    ).fetchall()
    return [_row_to_item(row) for row in rows]


def delete_perceptions(conn: sqlite3.Connection, ids: List[int]) -> None:
    """指定 id の知覚を台帳から削除する (プレビューでの項目編集の将来用途)。

    消費経路はもう削除しない (``create_consumption_batch`` が印を打つだけ)。
    """
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
