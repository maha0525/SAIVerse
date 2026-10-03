# Issue: 汎用 DB 編集が既存 Building の City 変更拒否を迂回する

**ステータス**: 検証待ち (2026-10-03、修正・隔離 API 回帰済み。実機での操作確認は未実施)
**優先度**: high
**関連**: `api/routes/db_manager.py:upsert_row` / `manager/admin.py:update_building` / [City intent §4-1](../intent/city_identity.md#4-1-既存-building-の所属-city-も通常編集では変えない)

## 守ることと問題

[W7 D5](../handoff/2026-07-21_w7_location_occupancy_handoff.md#d5-p1-7--building-の-city-変更を-immutable-化) は、既存 Building の所属 City を通常更新で変えることを禁止している。ユーザーの現在地・Region・私室・item/tool link などの参照をまとめて移送しない限り、DB の City だけ変わると世界の参照範囲が食い違う。

通常の `PUT /api/world/buildings/{id}` は `AdminService.update_building` を通り、現在の `CITYID` と違う値を拒否する。しかし修正前の `POST /api/db/tables/building` は、受け取った列から `Building` を組み立て `db.merge(instance)` → `db.commit()` で保存していた。既存行かどうかの識別は主キーに任せており、City 不変条件も管理サービスも通らなかった。BuildingSettingsModal / WorldEditor の表示専用化だけでは、この入口は塞がらなかった。

## 再現と証拠

[PR #365 のレビュー](https://github.com/maha0525/SAIVerse/pull/365#issuecomment-5964788128) をコードと隔離した実ルートで裏取りした。

1. 一時 HOME / SAIVERSE_HOME を指定し、メモリ内 SQLite に合成 City 1・2 と City 1 の Building `synthetic` を作る。
2. `get_db` をそのセッションに置き換えた FastAPI TestClient で実ルートへ `{"data":{"BUILDINGID":"synthetic","CITYID":2}}` を POST する。
3. HTTP 200 / `success: true` となり、再読込した既存行の `CITYID` は 2。建物名は保持される。
4. 既存 `BuildingCityImmutableTestCase` の通常サービス経路では City 変更拒否と通常項目保存の 2 ケースが合格する。同じ制約の入口差と確認できる。

本番 DB・実サーバー・ペルソナ・LLM は一切使っていない。認証を外から突破する話ではなく、既にアクセスできる DB 編集入口の整合性問題。

## 修正範囲

汎用 API の保存境界に、既存 Building の `CITYID` と指定値の照合を加えた。`db.merge` が通常項目をコピーする前に、通常サービスと同じ理由で変更を拒否し、既存の例外経路でロールバックして HTTP 400 を返す。`RowData` の値は `Any` なので Python の型違いだけでは拒否せず、DB の整数列との比較で同値を判定する。許可時は既存の整数 ID を保存し、`int(1.5)` のような切り捨て変換は行わない。

- `CITYID` を省いた通常項目編集・現在と同値の更新は保存できる。SQLite が同じ整数として扱う数値文字列 (`"1"` / `"01"` / `"1.0"` / `"1e0"`) も維持する。
- 新規 Building は指定した City に作成できる。
- 他 City (数値文字列も含む)・null・空文字・小数・不正文字列への変更は拒否し、同じ要求に含まれる建物名・収容数も保存しない。
- DB スキーマ・通常サービス・画面の仕様は変えない。汎用 DB 編集全体の再設計や City 移送の実装は含めない。

## 検証

- `tests/test_db_manager_building_city.py` の 17 ケースは、実 FastAPI ルートを外部キー制約を有効にした合成 SQLite に接続する。通常更新 (City 省略 / 整数・数値文字列の同値)、City 2 への整数・数値文字列での新規作成、変更拒否 (別 City の整数・数値文字列 / null / 空文字 / 1.5 / `"1.5"` / 不正文字列)、SQLAlchemy の rollback 実行、更新全体の非保存、拒否後の通常保存を確認する。
- 修正前には City 2 への更新が HTTP 200 となり、null / 空文字は不変条件でなく NOT NULL 制約で失敗した。新規回帰は 3 failed / 3 passed で入口差を捉え、修正後は全件合格した。
- 既存の `tests/test_db_manager_api.py`、`tests/test_region_admin.py`、`tests/test_city_identity.py` と合わせて 95 件・2 subtests が合格。通常サービスの City 拒否・同値での通常項目更新も含む。変更 Python の `ruff check`、`scripts/check_in_flight.py`、`git diff --check` は合格。
- 参照文書の再生成で API 一覧・DB スキーマは差分なし。ツールカタログだけは隔離環境にアドオンが無いため 50 件減る差分となり、この変更と無関係なので取り込まない (`--check` もその差分だけを報告)。
- HOME / SAIVERSE_HOME / user_data / ログ先を一時ディレクトリへ隔離。本番 DB・実サーバー・ペルソナ・LLM は使わない。
- 未検証: 実ブラウザからのエラー表示・保存後の読み戻し、稼働中の世界の操作。全体テストの成功とは扱わない。

次 = レビュー後、隔離されたブラウザ環境で通常項目の保存・読み戻しと変更拒否の表示を確認する。本番確認が必要なら、その操作の明示承認を別途得る。完了扱い・archive 移動はしない。

## 経緯

- 2026-10-03: PR #365 の軽い表示修正から分離して起票。起票時の方針は「汎用 upsert の既存 Building 更新も同じ不変条件で拒否する責任境界を決める。通常項目の編集と新規 Building の City 指定を壊さず、拒否時は全体をロールバックする」。バックエンド修正は当時未実装だった。
- 2026-10-03: 汎用 API の保存前ガードと隔離 DB 回帰を実装。実機未検証のため検証待ちのまま維持する。
- 2026-10-03: レビューで数値文字列の互換性を点検。現行フロントは汎用 API を GET に使用し、Building の保存は型付き world API へ送るが、汎用 POST 自体は `Any` の入口である。最初の Python 比較では同じ City の文字列表現 4 件が回帰で失敗したため、DB の列との比較と既存整数の保存に置き換えた。小数や不正文字列を整数へ切り捨てずに拒否することも追加検証した。
