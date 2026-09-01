"""汎用テーブル閲覧 API (api/routes/db_manager.py) のページ送りテスト。

この API は既定で 100 行しか返さない。以前は総件数を返す手段が無かったため、
ワールドエディタは 101 件目以降が落ちたことに気づけず、「アイテムが全部
表示されない」という形でユーザーに見えていた (2026-09-01)。

ここで固定するのは:

- 既定の 100 行を超えるテーブルでも、応答ヘッダ X-Total-Count に総件数が載る
  (呼び出し側が「続きがある」と判定できる)
- offset をずらすと重複も抜けもなく読み進められる (主キー順に固定されている)
- limit は上限 (1000) を超えたら黙って切り詰めず 422 で弾く。負値・0 も同様
- 存在しないテーブルは 404
"""
from __future__ import annotations

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.routes import db_manager
from database.models import Base, Tool

ROW_COUNT = 250


class DbManagerPaginationTest(unittest.TestCase):
    def setUp(self) -> None:
        # TestClient は別スレッドで route を動かすので、接続を 1 本に固定しないと
        # ":memory:" が別々の空 DB になる
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.Session = sessionmaker(bind=self.engine)

        db = self.Session()
        try:
            # 主キー順に並ぶことを見るため、挿入順は主キー順とわざとずらす
            for tool_id in sorted(range(1, ROW_COUNT + 1), key=lambda n: (n * 7) % ROW_COUNT):
                db.add(Tool(
                    TOOLID=tool_id,
                    TOOLNAME=f"tool_{tool_id:04d}",
                    MODULE_PATH=f"tools/tool_{tool_id:04d}.py",
                ))
            db.commit()
        finally:
            db.close()

        app = FastAPI()
        app.include_router(db_manager.router, prefix="/api/db")

        def override_get_db():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[db_manager.get_db] = override_get_db
        self.client = TestClient(app)

    def test_default_page_reports_total_count(self):
        """引数なしでも「全部で何件あるか」が分かる。"""
        res = self.client.get("/api/db/tables/tool")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()), 100)
        self.assertEqual(res.headers["X-Total-Count"], str(ROW_COUNT))

    def test_offset_walks_the_whole_table_without_gaps(self):
        """offset をずらして読むと、重複も抜けもなく全件そろう。"""
        seen: list[int] = []
        offset = 0
        while True:
            res = self.client.get(f"/api/db/tables/tool?limit=100&offset={offset}")
            self.assertEqual(res.status_code, 200)
            rows = res.json()
            self.assertEqual(res.headers["X-Total-Count"], str(ROW_COUNT))
            if not rows:
                break
            seen.extend(row["TOOLID"] for row in rows)
            offset += len(rows)

        self.assertEqual(seen, list(range(1, ROW_COUNT + 1)))

    def test_offset_beyond_the_end_returns_empty_with_total(self):
        res = self.client.get(f"/api/db/tables/tool?offset={ROW_COUNT + 10}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), [])
        self.assertEqual(res.headers["X-Total-Count"], str(ROW_COUNT))

    def test_limit_at_the_cap_is_accepted(self):
        res = self.client.get("/api/db/tables/tool?limit=1000")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()), ROW_COUNT)

    def test_limit_over_the_cap_is_rejected(self):
        """黙って切り詰めない。上限超えは 422 で返す。"""
        res = self.client.get("/api/db/tables/tool?limit=100000")
        self.assertEqual(res.status_code, 422)

    def test_non_positive_limit_is_rejected(self):
        for bad in (0, -1):
            with self.subTest(limit=bad):
                res = self.client.get(f"/api/db/tables/tool?limit={bad}")
                self.assertEqual(res.status_code, 422)

    def test_negative_offset_is_rejected(self):
        res = self.client.get("/api/db/tables/tool?offset=-1")
        self.assertEqual(res.status_code, 422)

    def test_empty_table_reports_zero(self):
        res = self.client.get("/api/db/tables/blueprint")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), [])
        self.assertEqual(res.headers["X-Total-Count"], "0")

    def test_unknown_table_is_404(self):
        res = self.client.get("/api/db/tables/not_a_table")
        self.assertEqual(res.status_code, 404)


if __name__ == "__main__":
    unittest.main()
