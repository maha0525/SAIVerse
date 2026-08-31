"""同じ DB を触る書き手が、同じ錠前を持つための配り所。

**錠前は呼び出し側が持ち回るものではなく、DB ファイルに紐づくもの。**
``lock_for(conn)`` を呼べば、その接続が指している DB ファイルの錠前が返る。
同じファイルを開いた書き手は、接続が別でも同じ錠前を受け取る。

## なぜこの形か

以前は「錠前を引数で手渡しする」規約だった。渡し忘れると受け取る側が自分用の
錠前を勝手に作り、**守っているつもりで排他が成立しない**状態になる。音を立てて
壊れないので、気づくのは記憶が落ちた後。2026-08-06 のレビューで、この渡し忘れが
6 箇所見つかった（`docs/issues/memopedia_writers_bypass_adapter_lock.md`）。

個別に塞いでも「渡し忘れが起こせる」構造は残る。だから配り所を一つにして、
**渡し忘れという事故をそもそも起こせなくした**（まはー裁定 2026-08-06、案A）。

## 何を守り、何を守らないか

守るのは **Python 側の直列化** —— 同じプロセスの複数スレッドが、同じ接続や同じ
ファイルへ順番に書くこと。SQLite の ``BEGIN IMMEDIATE`` は DB の書き込みロックで、
これは**別プロセス**との競合を止めるが、同一接続を共有する別スレッドの
``commit()`` は止められない。両方要る。

守らないのは、別プロセスとの排他（それは SQLite に任せる）。
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from typing import Dict

LOGGER = logging.getLogger(__name__)

#: DB ファイルの実体パス → その DB の錠前。
#: プロセスが生きているあいだ持ち続ける（ペルソナごとに 1 つなので数は増えない）。
_LOCKS: Dict[str, threading.RLock] = {}

#: ``_LOCKS`` 自体を守る錠前（同時に二人が同じ鍵を作らないように）。
_REGISTRY_GUARD = threading.Lock()


def lock_for(conn: sqlite3.Connection) -> threading.RLock:
    """この接続が指している DB ファイルの錠前を返す。

    同じファイルを開いた接続同士は、同じ錠前を受け取る。メモリ上の DB
    （``:memory:``、テスト用）は接続ごとに別の DB なので、接続ごとの錠前になる。

    再入可能（``RLock``）なので、同じスレッドが入れ子で取っても止まらない。
    """
    return _lock_for_key(_db_key(conn))


def lock_for_path(db_path: str) -> threading.RLock:
    """接続を開く前に錠前が要るとき用（パスから直接引く）。

    ``init_db`` のようにテーブルの用意そのものが書き込みになる処理は、接続を
    作る前から錠前が必要になる。
    """
    return _lock_for_key(_normalize(db_path))


def _lock_for_key(key: str) -> threading.RLock:
    with _REGISTRY_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


def _db_key(conn: sqlite3.Connection) -> str:
    """接続 → 錠前の鍵（DB ファイルの実体パス）。

    ファイルを持たない接続（メモリ DB）は、接続そのものを鍵にする。
    """
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        # (seq, name, file) —— file はメモリ DB では空文字
        path = row[2] if row and len(row) >= 3 else ""
    except sqlite3.Error:
        # 接続が壊れている等。鍵を作れないので接続そのものを鍵にする
        # （少なくとも、その接続を共有する書き手同士は直列化される）
        path = ""
    if path:
        return _normalize(path)
    return f"conn:{id(conn)}"


def _normalize(db_path: str) -> str:
    """同じファイルが同じ鍵になるように整える（大文字小文字・相対パス・別名）。"""
    return os.path.normcase(os.path.abspath(db_path))
