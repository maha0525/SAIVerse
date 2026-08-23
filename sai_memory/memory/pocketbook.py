"""手帳 (pocketbook) — アクティビティとメモの記録層。

正典: docs/intent/autonomous_behavior_v3.md §13.1 / §13.6 (2026-08-18 まはー承認)。

- **activities**: 活動粒度の名前と開閉状態。「眠っている」は列にせず、
  最終メモの日付 (:func:`get_last_memo_date`) から導出する。
- **memos**: 日付つき一行 (やった did / やりたい want)。本文はペルソナ本人の
  言葉で、システムは構造だけを読み本文を解釈しない。``span_start_id`` /
  ``span_end_id`` は生ログ (messages.id) への降り口 — 本人には書かせず
  機械が刻印する。
- 「未消化のやりたいメモ」「消化済み」は列にしない — 列にすると書き漏れが
  嘘の列になる。導出クエリ (:func:`list_undigested_want_memos`) は嘘をつけない。
- アクティビティを閉じる口は :func:`close_activity` 一つだけ (本人かユーザーの
  明示だけが閉じる — §13.6 の v1 教訓)。自動クローズの経路は作らない。

storage.py の流儀に合わせ、conn を第一引数に取る素朴な関数群で構成する。
テーブルは :func:`init_pocketbook_tables` が CREATE TABLE IF NOT EXISTS で
用意する (storage.init_db から呼ばれる)。
"""

from __future__ import annotations

import datetime
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import List, Optional

#: activities.origin の閉語彙 (§13.6)。
#:
#: - ``sluice``: **ペルソナ本人由来** — スルースの採取と、本人が唱える手帳の
#:   スペル (``pocketbook_write``) の両方がこれ。読み口 UI の表示も
#:   「ペルソナが書いた」で、区別しているのは「誰が立てたか」であって
#:   「どの機構を通ったか」ではない (2026-08-23、§13.2.1 でスペルを足したとき
#:   に語の意味をこちらへ確定させた — 語を増やすと UI の表示も同じ一語に
#:   潰れるだけで、区別が使われる先が無い)
#: - ``user``: ユーザーが立てた / ``initial``: キャラクター作成時の初期関心 /
#:   ``migration``: 既存データからの機械写し
ACTIVITY_ORIGINS = ("sluice", "user", "initial", "migration")

#: memos.kind の閉語彙 (§13.1)。
MEMO_KINDS = ("did", "want")

# [0-9] 明記 — \d は全角数字 (２０２６ 等) も通してしまう。ASCII 桁のみ。
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def owns_transaction(conn: sqlite3.Connection, commit: bool = True) -> bool:
    """この呼び出しがトランザクションの持ち主かを判定する。

    本モジュールの書き込み関数は ``commit=True`` を既定に持つが、これは
    「確定してよい」ではなく「**この関数がトランザクションを所有する**」の旗
    である。呼び手が既に束を開いている (``conn.in_transaction``) ところへ
    既定のまま呼ばれたとき、旗を額面どおり受け取ると呼び手の未確定分まで
    巻き込んで確定させ (成功時)、あるいは巻き込んで捨てる (失敗時)。所有は
    ``commit`` だけでは決まらず、呼び手の状態と合わせて初めて決まる。

    ⚠ **最初の ``execute`` より前に一度だけ**呼び、その結果を成功時の commit
    と失敗時の rollback の**両方**で使うこと。DML を一文でも実行すると暗黙に
    トランザクションが開き、以後 ``conn.in_transaction`` は呼び手の有無に
    関わらず True になるため、実行後に判定しても意味を成さない。片方だけを
    所有判定にすると、失敗時に呼び手の束を巻き戻す経路が残る。

    出自: docs/issues/pocketbook_commit_flag_is_not_ownership_check.md
    (2026-08-22 裁定 — 判定をここ一箇所に集約し、書き写しをやめる)。
    """
    return bool(commit) and not conn.in_transaction


def init_pocketbook_tables(conn: sqlite3.Connection) -> None:
    """手帳のテーブル二枚 (activities / memos) を用意する。冪等。"""
    # storage が init_db 内で本モジュールを import する (相互依存の腕が片方
    # 遅延) ため、逆向きのこの import も遅延させて循環を避ける。
    from sai_memory.memory.storage import _ensure_column

    # commit の旗を持たない関数だが、末尾の確定が呼び手の束を巻き込む構図は
    # 書き込み関数と同じ (むしろ断り方が無いぶん悪い) — 所有判定を最初の
    # execute より前に取る。
    owns_txn = owns_transaction(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS activities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            born_at INTEGER NOT NULL,
            origin TEXT NOT NULL,
            closed_at INTEGER
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_activities_status ON activities(status)"
    )
    # open な同名一意の DB 境界 (部分 UNIQUE)。アプリ層の直列化
    # (get_or_create_activity の BEGIN IMMEDIATE) は commit=False モードを
    # 守れないため、最終保証はここが持つ。closed の同名は歴史として合法。
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_activities_open_name "
        "ON activities(name) WHERE status = 'open'"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            activity_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            kind TEXT NOT NULL,
            text TEXT NOT NULL,
            span_start_id TEXT,
            span_end_id TEXT,
            FOREIGN KEY (activity_id) REFERENCES activities(id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memos_activity_date ON memos(activity_id, date)"
    )
    # idem_key: スルース再試行の重複防止キー (NULL 可 — NULL は UNIQUE に
    # かからないので既存動作は不変)。既存 DB へは冪等な後付け (storage.py の
    # _ensure_column の流儀)。
    _ensure_column(conn, "memos", "idem_key", "TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_memos_idem ON memos(idem_key)"
    )
    if owns_txn:
        conn.commit()


@dataclass
class Activity:
    id: int
    name: str
    status: str  # 'open' | 'closed'
    born_at: int
    origin: str  # 'sluice' | 'user' | 'initial' | 'migration'
    closed_at: Optional[int] = None


@dataclass
class Memo:
    id: int
    activity_id: int
    date: str  # 'YYYY-MM-DD' (日粒度)
    kind: str  # 'did' | 'want'
    text: str
    span_start_id: Optional[str] = None
    span_end_id: Optional[str] = None


_ACTIVITY_COLUMNS = "id, name, status, born_at, origin, closed_at"
_MEMO_COLUMNS = "id, activity_id, date, kind, text, span_start_id, span_end_id"


def _row_to_activity(row) -> Activity:
    return Activity(
        id=int(row[0]),
        name=row[1],
        status=row[2],
        born_at=int(row[3]),
        origin=row[4],
        closed_at=int(row[5]) if row[5] is not None else None,
    )


def _row_to_memo(row) -> Memo:
    return Memo(
        id=int(row[0]),
        activity_id=int(row[1]),
        date=row[2],
        kind=row[3],
        text=row[4],
        span_start_id=row[5],
        span_end_id=row[6],
    )


def _validate_date(date: str) -> None:
    """メモ日付の検査 — ASCII の 'YYYY-MM-DD' 形式で、実在する暦日だけを通す。

    正規表現を先に通すのは、fromisoformat が受け付ける別形式 (幅のある入力 —
    週番号形式 '2026-W34-2' や序数日形式など) を締め出して、格納形式を
    'YYYY-MM-DD' の一本に固定するため。その上で fromisoformat が暦日として
    実在するかを検証する — '9999-99-99' や '2026-02-31' は字面が合っても暦に
    無く、通すと MAX(date) の文字列比較による導出 (最終メモ日付・未消化) を
    毒する。
    """
    if not isinstance(date, str) or not _DATE_RE.match(date):
        raise ValueError(f"memo date must be 'YYYY-MM-DD', got: {date!r}")
    try:
        datetime.date.fromisoformat(date)
    except ValueError:
        raise ValueError(
            f"memo date must be a real calendar date, got: {date!r}"
        ) from None


def _validate_activity_id(value: object) -> None:
    """activity_id の検査 — int だけを通す (bool 拒否)。

    bool は int のサブクラスで、True が id=1 の行に一致して無関係な
    アクティビティを黙って読み書きする。str の '1' も INTEGER affinity の
    変換で 1 に一致するため、暗黙変換に頼らず入口で拒否する。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"activity_id must be an int, got: {value!r}")


def _validate_optional_str(name: str, value: object) -> None:
    """str か None だけを通す (span_start_id / span_end_id / idem_key 等)。

    非文字列は TEXT affinity で '42' の顔に着地して messages.id への降り口が
    壊れるため、暗黙変換で救わず入口で拒否する。
    """
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{name} must be a string or None, got: {value!r}")


def validate_epoch(name: str, value: object) -> None:
    """epoch 秒の引数検査 — int か None だけを通す。

    SQLite は列型を強制しないため、文字列や float を渡すとそのまま永続して
    時刻順の比較を毒する。bool は int のサブクラスなので isinstance だけでは
    通ってしまう — 明示的に拒否する (True が 1 として永続すると「1970 年の
    刻印」という嘘になる)。continuity / recall_edges の同族の口もこれを使う。
    """
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"{name} must be an int (epoch seconds) or None, got: {value!r}"
        )


# ---------------------------------------------------------------------------
# activities
# ---------------------------------------------------------------------------

def add_activity(
    conn: sqlite3.Connection,
    name: str,
    origin: str,
    *,
    born_at: Optional[int] = None,
    commit: bool = True,
) -> Activity:
    """アクティビティを追加する。origin は閉語彙 (:data:`ACTIVITY_ORIGINS`)。

    open な同名が既にあると INSERT は部分 UNIQUE (idx_activities_open_name)
    に弾かれる — そのときは既存の open 同名行へ収束して返す (get-or-create)。
    ``commit=False`` は、呼び手が一連の操作を一つのトランザクションに束ねて
    最後に ``conn.commit()`` する用 (既定 True = 従来どおり即 commit)。既定の
    まま呼ばれても、呼び手が既にトランザクションを開いていれば確定も巻き戻しも
    しない (:func:`owns_transaction` — 所有していない束には触らない)。
    """
    if origin not in ACTIVITY_ORIGINS:
        raise ValueError(
            f"unknown activity origin: {origin!r} (expected one of {ACTIVITY_ORIGINS})"
        )
    if not isinstance(name, str) or not name.strip():
        raise ValueError("activity name must be a non-empty string")
    validate_epoch("born_at", born_at)
    owns_txn = owns_transaction(conn, commit)
    ts = int(time.time()) if born_at is None else born_at
    try:
        cur = conn.execute(
            "INSERT INTO activities(name, status, born_at, origin, closed_at) "
            "VALUES (?, 'open', ?, ?, NULL)",
            (name.strip(), ts, origin),
        )
        if owns_txn:
            conn.commit()
    except sqlite3.IntegrityError:
        # open 同名の部分 UNIQUE (idx_activities_open_name) に弾かれた — 並行の
        # 追加が先に入っている。DB 境界の最終保証なので、既存の open 同名行を
        # 読み直して返す (get-or-create へ収束)。所有しているとき (owns_txn) は
        # 失敗した文が暗黙に開いたトランザクションを先に巻き戻す。所有していない
        # ときは失敗した文だけが巻き戻っており、束は呼び手が閉じるので触らない。
        if owns_txn:
            conn.rollback()
        row = conn.execute(
            f"SELECT {_ACTIVITY_COLUMNS} FROM activities "
            "WHERE name = ? AND status = 'open' ORDER BY id ASC LIMIT 1",
            (name.strip(),),
        ).fetchone()
        if row is None:
            raise
        return _row_to_activity(row)
    except BaseException:
        # 本関数がトランザクションを所有しているときの失敗で、失敗した文が
        # 暗黙に開いたトランザクションを開きっぱなしにしない — 巻き戻してから
        # 送出する。所有していなければ呼び手の束なので触らない。
        if owns_txn:
            conn.rollback()
        raise
    return Activity(
        id=int(cur.lastrowid),
        name=name.strip(),
        status="open",
        born_at=ts,
        origin=origin,
        closed_at=None,
    )


def get_or_create_activity(
    conn: sqlite3.Connection,
    name: str,
    origin: str,
    *,
    born_at: Optional[int] = None,
    commit: bool = True,
) -> Activity:
    """open な同名アクティビティがあればそれを返し、無ければ追加する。

    スルースの再試行で同じ ``new_activity_name`` が二度来ても一本に収束させる
    (get-or-create)。名前は strip して比較する (大文字小文字は区別する)。
    closed の同名は再利用しない — 明示的に閉じたものを機械が蘇らせない。

    原子性: 既存確認 → INSERT は、本関数がトランザクションを所有するとき
    (``commit=True`` かつ呼び手がトランザクション外) ``BEGIN IMMEDIATE`` で
    不可分にする — これ無しでは確認と INSERT の間に他接続の同名追加が
    滑り込み、open な同名が二本できる。呼び手が既にトランザクション中
    (``conn.in_transaction``)、または ``commit=False`` (呼び手が束ねる) の
    ときは BEGIN も commit も発行せず呼び手に委ねる。ロック競合
    (``sqlite3.OperationalError: database is locked``) はそのまま送出する
    (呼び手の再試行の領分)。

    最終保証は DB 境界の部分 UNIQUE (idx_activities_open_name) が持つ —
    BEGIN IMMEDIATE が発行されない並び (``commit=False`` や呼び手の
    トランザクション中) で確認と INSERT の間に他接続の同名追加が滑り込んでも、
    INSERT が UNIQUE に弾かれて既存の open 同名行へ収束する
    (:func:`add_activity` の IntegrityError 処理)。直列化は先取り、UNIQUE が
    最後の網、の二段。
    """
    if origin not in ACTIVITY_ORIGINS:
        raise ValueError(
            f"unknown activity origin: {origin!r} (expected one of {ACTIVITY_ORIGINS})"
        )
    if not isinstance(name, str) or not name.strip():
        raise ValueError("activity name must be a non-empty string")
    stripped = name.strip()
    manage_txn = owns_transaction(conn, commit)
    if manage_txn:
        conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            f"SELECT {_ACTIVITY_COLUMNS} FROM activities "
            "WHERE name = ? AND status = 'open' ORDER BY id ASC LIMIT 1",
            (stripped,),
        ).fetchone()
        if row:
            if manage_txn:
                conn.rollback()  # 何も書いていない — 予約ロックだけ手放す
            return _row_to_activity(row)
        activity = add_activity(conn, stripped, origin, born_at=born_at, commit=False)
    except BaseException:
        if manage_txn:
            conn.rollback()
        raise
    if manage_txn:
        conn.commit()
    return activity


def get_activity(conn: sqlite3.Connection, activity_id: int) -> Optional[Activity]:
    _validate_activity_id(activity_id)
    row = conn.execute(
        f"SELECT {_ACTIVITY_COLUMNS} FROM activities WHERE id = ?",
        (activity_id,),
    ).fetchone()
    return _row_to_activity(row) if row else None


def rename_activity(
    conn: sqlite3.Connection,
    activity_id: int,
    new_name: str,
    *,
    commit: bool = True,
) -> bool:
    """名前を変更する。対象が存在すれば True。"""
    _validate_activity_id(activity_id)
    if not isinstance(new_name, str) or not new_name.strip():
        raise ValueError("activity name must be a non-empty string")
    owns_txn = owns_transaction(conn, commit)
    try:
        cur = conn.execute(
            "UPDATE activities SET name = ? WHERE id = ?",
            (new_name.strip(), activity_id),
        )
        if owns_txn:
            conn.commit()
    except BaseException:
        # 所有しているときの失敗はトランザクションを開きっぱなしにしない —
        # 巻き戻してから送出する。所有していなければ呼び手の束に委ねる。
        if owns_txn:
            conn.rollback()
        raise
    return cur.rowcount > 0


def close_activity(
    conn: sqlite3.Connection,
    activity_id: int,
    *,
    closed_at: Optional[int] = None,
    commit: bool = True,
) -> bool:
    """アクティビティを明示的に閉じる (唯一のクローズ経路 — §13.6)。

    closed_at は「明示という出来事の日付」(born_at と同じ性質) なので、
    既に closed のものへ再度呼んでも上書きしない (False を返す)。
    """
    _validate_activity_id(activity_id)
    validate_epoch("closed_at", closed_at)
    owns_txn = owns_transaction(conn, commit)
    ts = int(time.time()) if closed_at is None else closed_at
    try:
        cur = conn.execute(
            "UPDATE activities SET status = 'closed', closed_at = ? "
            "WHERE id = ? AND status = 'open'",
            (ts, activity_id),
        )
        if owns_txn:
            conn.commit()
    except BaseException:
        # 所有しているときの失敗はトランザクションを開きっぱなしにしない —
        # 巻き戻してから送出する。所有していなければ呼び手の束に委ねる。
        if owns_txn:
            conn.rollback()
        raise
    return cur.rowcount > 0


def list_activities(
    conn: sqlite3.Connection,
    *,
    include_closed: bool = False,
) -> List[Activity]:
    """アクティビティ一覧 (誕生順)。既定は開いているものだけ (閉語彙の同梱用)。"""
    if include_closed:
        cur = conn.execute(
            f"SELECT {_ACTIVITY_COLUMNS} FROM activities ORDER BY born_at ASC, id ASC"
        )
    else:
        cur = conn.execute(
            f"SELECT {_ACTIVITY_COLUMNS} FROM activities "
            "WHERE status = 'open' ORDER BY born_at ASC, id ASC"
        )
    return [_row_to_activity(row) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# memos
# ---------------------------------------------------------------------------

def _get_memo_by_idem_key(conn: sqlite3.Connection, idem_key: str) -> Optional[Memo]:
    row = conn.execute(
        f"SELECT {_MEMO_COLUMNS} FROM memos WHERE idem_key = ?",
        (idem_key,),
    ).fetchone()
    return _row_to_memo(row) if row else None


def find_memo_by_content(
    conn: sqlite3.Connection,
    activity_id: int,
    date: str,
    kind: str,
    text: str,
) -> Optional[Memo]:
    """同じ日・同じアクティビティ・同じ種類・同じ本文のメモを探す (重複防止用)。

    冪等キー (``idem_key``) が守るのは「同じ担当範囲の同じ番号」の再適用だけ
    なので、担当範囲が変われば同じ内容でも別キーになって通る。退場が次回へ
    繰り越された回は採取済みの会話が窓に残り、本人が同じメモをもう一度返す —
    その内容ベースの重複をここで見つける。

    日付を条件に含めるのは、手帳が日々の記録だから — 「今日も小説を書いた」が
    二日続くのは重複ではなく事実で、単純な内容一致で弾くと正しい記録が落ちる。
    種類 (want / did) も分けるのは同じ理由 (「やりたい」と「やった」は別の記録)。

    照合は書き込みと同じトランザクションの中で行うこと (check-then-act の隙間を
    作らない)。出自: docs/issues/sluice_memo_duplicate_across_spans.md。
    """
    _validate_activity_id(activity_id)
    _validate_date(date)
    if kind not in MEMO_KINDS:
        raise ValueError(f"unknown memo kind: {kind!r} (expected one of {MEMO_KINDS})")
    if not isinstance(text, str):
        raise ValueError(f"memo text must be a string, got: {text!r}")
    row = conn.execute(
        f"SELECT {_MEMO_COLUMNS} FROM memos "
        "WHERE activity_id = ? AND date = ? AND kind = ? AND text = ? "
        "ORDER BY id ASC LIMIT 1",
        (activity_id, date, kind, text),
    ).fetchone()
    return _row_to_memo(row) if row else None


def add_memo(
    conn: sqlite3.Connection,
    activity_id: int,
    date: str,
    kind: str,
    text: str,
    *,
    span_start_id: Optional[str] = None,
    span_end_id: Optional[str] = None,
    idem_key: Optional[str] = None,
    commit: bool = True,
) -> Memo:
    """メモを追加する。span はスルースの一手が担当した範囲を機械が刻印する引数。

    ``idem_key`` は冪等キー。与えると get-or-create になり、スルースの再試行で
    同じメモが二重に書かれるのを防ぐ (既存が勝つ)。キーは呼び手 (スルース) が
    スルース実行 ID + 操作番号で採番する。省略 (None) は従来どおり毎回新規。
    ``commit=False`` は、呼び手が一連の操作を一つのトランザクションに束ねて
    最後に ``conn.commit()`` する用 (既定 True = 従来どおり即 commit)。既定の
    まま呼ばれても、呼び手が既にトランザクションを開いていれば確定も巻き戻しも
    しない (:func:`owns_transaction`)。
    """
    owns_txn = owns_transaction(conn, commit)
    _validate_activity_id(activity_id)
    if kind not in MEMO_KINDS:
        raise ValueError(f"unknown memo kind: {kind!r} (expected one of {MEMO_KINDS})")
    _validate_date(date)
    if not isinstance(text, str) or not text.strip():
        raise ValueError("memo text must be a non-empty string")
    _validate_optional_str("span_start_id", span_start_id)
    _validate_optional_str("span_end_id", span_end_id)
    # idem_key は非空の str か None — 空白のみを None に倒すと冪等性が静かに
    # 無効化され、そのまま通すと空白キー同士が UNIQUE 衝突で既存返しに化ける。
    if idem_key is not None and (
        not isinstance(idem_key, str) or not idem_key.strip()
    ):
        raise ValueError(
            f"idem_key must be a non-empty string or None, got: {idem_key!r}"
        )
    if get_activity(conn, activity_id) is None:
        raise ValueError(f"activity not found: {activity_id}")
    if idem_key is not None:
        existing = _get_memo_by_idem_key(conn, idem_key)
        if existing is not None:
            # 所有しているときの契約は「この呼び出しから戻るときトランザクション
            # が確定している」— INSERT 経路と同様、冪等ヒットの早期 return でも
            # 確定してから返す。確定せず返すと、同接続に未確定の書き込みがある
            # とき書き込みロックが残り、他接続を塞ぐ。所有していない (呼び手が
            # 束を開いている) ときは確定しない — 呼び手の未確定分を巻き込む。
            if owns_txn:
                conn.commit()
            return existing
    try:
        cur = conn.execute(
            "INSERT INTO memos("
            "activity_id, date, kind, text, span_start_id, span_end_id, idem_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (activity_id, date, kind, text, span_start_id, span_end_id, idem_key),
        )
        if owns_txn:
            conn.commit()
    except sqlite3.IntegrityError:
        # 並行の add が同じ idem_key を先に入れた (UNIQUE idx_memos_idem) —
        # 既存行を読み直して返す (冪等)。本関数がトランザクションを所有して
        # いるなら、失敗した INSERT が暗黙に開いたトランザクションを先に
        # 巻き戻す — 開いたまま return するとこの接続が書き込みロックを
        # 握り続け、他接続の書き込みを塞ぐ。所有していなければ呼び手の束なので
        # 巻き戻さない (失敗した文だけが巻き戻り、束は呼び手が閉じる)。
        if owns_txn:
            conn.rollback()
        if idem_key is None:
            raise
        existing = _get_memo_by_idem_key(conn, idem_key)
        if existing is None:
            raise
        return existing
    except BaseException:
        # IntegrityError 以外の失敗 (ロック競合等) も同じ規則で巻き戻してから
        # 送出する。
        if owns_txn:
            conn.rollback()
        raise
    return Memo(
        id=int(cur.lastrowid),
        activity_id=activity_id,
        date=date,
        kind=kind,
        text=text,
        span_start_id=span_start_id,
        span_end_id=span_end_id,
    )


def list_memos(
    conn: sqlite3.Connection,
    activity_id: int,
    *,
    kind: Optional[str] = None,
) -> List[Memo]:
    """アクティビティのメモ一覧 (日付順)。kind で did / want に絞れる。"""
    _validate_activity_id(activity_id)
    if kind is not None and kind not in MEMO_KINDS:
        raise ValueError(f"unknown memo kind: {kind!r} (expected one of {MEMO_KINDS})")
    if kind is None:
        cur = conn.execute(
            f"SELECT {_MEMO_COLUMNS} FROM memos WHERE activity_id = ? "
            "ORDER BY date ASC, id ASC",
            (activity_id,),
        )
    else:
        cur = conn.execute(
            f"SELECT {_MEMO_COLUMNS} FROM memos WHERE activity_id = ? AND kind = ? "
            "ORDER BY date ASC, id ASC",
            (activity_id, kind),
        )
    return [_row_to_memo(row) for row in cur.fetchall()]


def list_undigested_want_memos(
    conn: sqlite3.Connection,
    activity_id: Optional[int] = None,
) -> List[Memo]:
    """未消化のやりたいメモを導出する (§13.6 — 列ではなく導出)。

    未消化 = 同じアクティビティに、その want メモの日付より**後**の did メモが
    無いこと。同日の did は消化とみなさない (「その日付より後」の字義通り)。
    activity_id を渡すとそのアクティビティだけに絞る。
    """
    where = "w.kind = 'want'"
    params: tuple = ()
    if activity_id is not None:
        _validate_activity_id(activity_id)
        where += " AND w.activity_id = ?"
        params = (activity_id,)
    cur = conn.execute(
        f"""
        SELECT w.id, w.activity_id, w.date, w.kind, w.text, w.span_start_id, w.span_end_id
        FROM memos w
        WHERE {where}
          AND NOT EXISTS (
              SELECT 1 FROM memos d
              WHERE d.activity_id = w.activity_id
                AND d.kind = 'did'
                AND d.date > w.date
          )
        ORDER BY w.date ASC, w.id ASC
        """,
        params,
    )
    return [_row_to_memo(row) for row in cur.fetchall()]


def get_last_memo_date(conn: sqlite3.Connection, activity_id: int) -> Optional[str]:
    """アクティビティの最終メモ日付 ('YYYY-MM-DD')。メモが無ければ None。

    「眠っている」状態の導出材料 (§13.1 — 眠りは列にせずここから導く)。
    """
    _validate_activity_id(activity_id)
    row = conn.execute(
        "SELECT MAX(date) FROM memos WHERE activity_id = ?",
        (activity_id,),
    ).fetchone()
    return row[0] if row and row[0] is not None else None
