"""錠前の配り所の契約 (まはー裁定 2026-08-06、案A)。

以前は「錠前を引数で手渡しする」規約で、渡し忘れると受け取る側が自分用の錠前を
作り、守っているつもりで排他が成立しなかった。錠前を DB ファイルに紐づけて、
渡し忘れという事故をそもそも起こせなくする。

docs/issues/memopedia_writers_bypass_adapter_lock.md
"""

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from sai_memory.db_locks import lock_for, lock_for_path


class TestDbLocks(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "memory.db"
        sqlite3.connect(self.db_path).close()

    def tearDown(self):
        try:
            self._tmp.cleanup()
        except (PermissionError, OSError):
            pass

    def test_two_connections_to_the_same_file_share_one_lock(self):
        """⭐ 接続が別でも、同じ DB ファイルなら同じ錠前。

        これが案A の要。書き手が錠前を受け取り損ねる道が無くなる。
        """
        a = sqlite3.connect(self.db_path)
        b = sqlite3.connect(self.db_path)
        self.addCleanup(a.close)
        self.addCleanup(b.close)

        self.assertIs(lock_for(a), lock_for(b))

    def test_a_path_and_a_connection_agree(self):
        """接続を開く前 (パス) と開いた後 (接続) で同じ錠前になる。"""
        conn = sqlite3.connect(self.db_path)
        self.addCleanup(conn.close)

        self.assertIs(lock_for_path(str(self.db_path)), lock_for(conn))

    def test_different_spellings_of_the_same_path_agree(self):
        """同じファイルを別の書き方で指しても同じ錠前 (相対・大文字小文字)。"""
        nested = str(self.db_path.parent / "sub" / ".." / "memory.db")
        self.assertIs(lock_for_path(str(self.db_path)), lock_for_path(nested))

    def test_different_files_get_different_locks(self):
        """別のペルソナの DB は別の錠前 (無関係な書き込みを待たせない)。"""
        other = self.db_path.parent / "other.db"
        sqlite3.connect(other).close()

        self.assertIsNot(
            lock_for_path(str(self.db_path)), lock_for_path(str(other)),
        )

    def test_memory_databases_are_per_connection(self):
        """メモリ DB は接続ごとに別の DB なので、錠前も接続ごと。"""
        a = sqlite3.connect(":memory:")
        b = sqlite3.connect(":memory:")
        self.addCleanup(a.close)
        self.addCleanup(b.close)

        self.assertIsNot(lock_for(a), lock_for(b))
        self.assertIs(lock_for(a), lock_for(a))

    def test_the_lock_is_reentrant(self):
        """同じスレッドが入れ子で取っても止まらない (RLock)。

        Memopedia の中で storage を呼ぶような入れ子は普通に起きる。
        """
        lock = lock_for_path(str(self.db_path))
        with lock:
            with lock:
                pass

    def test_memopedia_without_an_explicit_lock_shares_the_file_lock(self):
        """⭐ 錠前を渡さずに作った Memopedia が、その DB の錠前を持つ。

        以前はここで自前の錠前を作っていた (= 誰とも排他しない)。
        """
        from sai_memory.memopedia import Memopedia
        from sai_memory.memory.storage import init_db

        conn = init_db(str(self.db_path))
        self.addCleanup(conn.close)

        memopedia = Memopedia(conn)
        self.assertIs(memopedia._lock, lock_for_path(str(self.db_path)))

    def test_only_one_lock_is_created_under_concurrent_first_use(self):
        """同時に初めて引いても、錠前は一つしか生まれない。"""
        other = self.db_path.parent / "race.db"
        sqlite3.connect(other).close()
        seen = []
        barrier = threading.Barrier(8)

        def grab():
            barrier.wait()
            seen.append(lock_for_path(str(other)))

        threads = [threading.Thread(target=grab) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(seen), 8)
        self.assertEqual(len({id(lock) for lock in seen}), 1)


if __name__ == "__main__":
    unittest.main()
