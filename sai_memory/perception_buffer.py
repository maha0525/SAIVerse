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
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import List, Optional


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
            boundary_rowid INTEGER
        )
        """
    )
    # 本ワークツリー内の先行世代 (境界キー列なし) で作られた DB の追従。
    for ddl in (
        "ALTER TABLE perception_batches ADD COLUMN boundary_created_at INTEGER",
        "ALTER TABLE perception_batches ADD COLUMN boundary_rowid INTEGER",
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
    import logging
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
        logging.getLogger(__name__).info(
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
    "annexed_entry_id, boundary_created_at, boundary_rowid"
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
) -> int:
    """消費バッチを単一トランザクションで確定する (§10.2)。

    バッチ行 (レンダリング済み文面 = ペルソナが見た文そのもの) を INSERT し、
    消費した項目に (consumed_at, batch_id) の印を打つ。「知覚した」の証跡は
    このバッチであって messages の行ではない (§10.1)。旧 flush の「メッセージ
    行を書く → 削除」二段が持っていた二度書きの口 (C6) は、消費がこの単一 tx に
    なったことで構造ごと消える。

    未消費でない id が混ざっていたら (呼び出し側の並び違反) rollback して
    ValueError — 消費済みの再消費 (C2 違反) を部分成立させない。

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
            "annexed_entry_id, boundary_created_at, boundary_rowid) "
            "VALUES (?, ?, ?, ?, ?, NULL, ?, ?)",
            (
                int(consumed_at), pulse_id, episode_id, rendered_text, media_json,
                int(boundary_created_at) if boundary_created_at is not None else None,
                int(boundary_rowid) if boundary_rowid is not None else None,
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


def list_unannexed_batches(
    conn: sqlite3.Connection,
    *,
    since: Optional[int] = None,
    before: Optional[int] = None,
) -> List[PerceptionBatch]:
    """付記印のない消費バッチを consumed_at → id 昇順で返す。

    提示 (時刻順マージ, §10.3) と退場付記 (§10.4) の読み口。提示から下ろす
    唯一の手段は付記印 (``annexed_entry_id``) なので、印が付くまでここに出続ける。
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


def mark_batches_annexed(
    conn: sqlite3.Connection, batch_ids: List[int], entry_id: str,
) -> int:
    """バッチに付記印 (転写先 Chronicle エントリ id) を打つ。**commit しない**。

    Chronicle チャンクの digest 確定と同一トランザクションで呼ぶ契約 (§10.4) —
    呼び出し元 (arasuji executor) の tx が rollback すれば印も戻り、バッチは
    未付記 = 提示に残る (fail-open)。

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
    return int(cur.rowcount)


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
