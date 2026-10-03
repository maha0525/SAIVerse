# Issue: 汎用 DB 編集が既存 Building の City 変更拒否を迂回する

**ステータス**: 未着手 (2026-10-03 起票、再現確認済み)
**優先度**: high
**関連**: `api/routes/db_manager.py:upsert_row` / `manager/admin.py:update_building` / [City intent §4-1](../intent/city_identity.md#4-1-既存-building-の所属-city-も通常編集では変えない)

## 守ることと問題

[W7 D5](../handoff/2026-07-21_w7_location_occupancy_handoff.md#d5-p1-7--building-の-city-変更を-immutable-化) は、既存 Building の所属 City を通常更新で変えることを禁止している。ユーザーの現在地・Region・私室・item/tool link などの参照をまとめて移送しない限り、DB の City だけ変わると世界の参照範囲が食い違う。

通常の `PUT /api/world/buildings/{id}` は `AdminService.update_building` を通り、現在の `CITYID` と違う値を拒否する。しかし `POST /api/db/tables/building` は、受け取った列から `Building` を組み立て `db.merge(instance)` → `db.commit()` で保存する。既存行かどうかの識別は主キーに任せており、City 不変条件も管理サービスも通らない。BuildingSettingsModal / WorldEditor を表示専用にしても、この入口は残る。

## 再現と証拠

[PR #365 のレビュー](https://github.com/maha0525/SAIVerse/pull/365#issuecomment-5964788128) をコードと隔離した実ルートで裏取りした。

1. 一時 HOME / SAIVERSE_HOME を指定し、メモリ内 SQLite に合成 City 1・2 と City 1 の Building `synthetic` を作る。
2. `get_db` をそのセッションに置き換えた FastAPI TestClient で実ルートへ `{"data":{"BUILDINGID":"synthetic","CITYID":2}}` を POST する。
3. HTTP 200 / `success: true` となり、再読込した既存行の `CITYID` は 2。建物名は保持される。
4. 既存 `BuildingCityImmutableTestCase` の通常サービス経路では City 変更拒否と通常項目保存の 2 ケースが合格する。同じ制約の入口差と確認できる。

本番 DB・実サーバー・ペルソナ・LLM は一切使っていない。認証を外から突破する話ではなく、既にアクセスできる DB 編集入口の整合性問題。

## 次に決める範囲

汎用 upsert の既存 Building 更新も同じ不変条件で拒否する責任境界を決める。通常項目の編集と新規 Building の City 指定を壊さず、拒否時は全体をロールバックする。回帰は両入口の拒否・同値更新・通常項目更新・新規作成を隔離 DB で比較する。汎用 DB 編集全体を置き換える設計や City 移送の実装は、この起票では決定しない。

PR #365 の軽い表示修正からは切り離し、バックエンド修正は未実装。完了扱い・archive 移動はしない。
