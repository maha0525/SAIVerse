# Issue: 汎用 DB 編集が既存 Building の City 変更拒否を迂回する

**ステータス**: 検証待ち (2026-10-04、汎用書き込みルートを撤去・隔離 API 回帰済み。レビュー・実機確認待ち)
**優先度**: high
**関連**: `api/routes/db_manager.py` / `manager/admin.py:update_building` / [City intent §4-1](../intent/city_identity.md#4-1-既存-building-の所属-city-も通常編集では変えない)

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

## 採用した修正（2026-10-04）

`POST /api/db/tables/{table}` と `DELETE /api/db/tables/{table}` のルートを撤去した。既存 Building への保存前 `CITYID` ガードも、その入口と一緒に取り除く。汎用書き込み窓口が無いので、管理サービスを通らない直接更新・削除→同じ ID での再作成・削除時の後始末を飛ばす迂回経路は残さない。

- `GET /api/db/tables` のテーブル一覧・スキーマと `GET /api/db/tables/{table}` の行一覧は維持する。ページ送り、上限、主キー順、`X-Total-Count` も変えない。
- 画面の作成・更新・削除は既存の専用 world API を使い続ける。DB スキーマ、City 移送の禁止、退役 ID・関連データの後始末は変更しない。コード上の入口の撤去であり、既存 DB の内容を消す作業はない。
- **OPS-09 の結論**: 「位置づけは要議論」だった汎用 POST / DELETE は、まはーが未使用を確認し撤去を選択したため廃止し、画面が使う GET のみ維持する（[方針のレビュー](https://github.com/maha0525/SAIVerse/pull/369#issuecomment-5975901900)）。監査当時の記録・共有台帳は変更しない。

### 撤去前の caller 監査

`9b935b98` で全 tracked files を対象に、`db/tables` / `/db` / `tables/` / `upsert_row` / `delete_row` / `RowData` / `DeleteRequest` と読み取り helper を検索し、HTTP method と呼び出し先を確認した。

- frontend: `page.tsx`・`TutorialWizard.tsx` の直接呼び出しと `lib/dbTable.ts` 経由は GET のみ。`apiFetch` は method を書き換えず `fetch(input, init)` を呼ぶ。WorldEditor の作成・更新・削除は `/api/world/*`、建物削除は deletion-preview → 専用 DELETE を使う。
- scripts・同梱資材: `scripts/`・`builtin_data/`・アドオンの loader / installer / catalog / panel 関連と `test_fixtures/` を含め、書き込み caller は無し。`test_fixtures/test_api.py` も GET のみ。gitignored の利用者追加アドオンや外部の API client はこの静的監査の対象外。
- tests: 書き込み caller はこの PR で追加した `tests/test_db_manager_building_city.py` の17ケースのみ。これは撤去し、`tests/test_db_manager_api.py` にルート不在の回帰を置き換えた。既存 GET 回帰は保持した。
- docs: 操作可能な API 参照は生成し直す。過去の監査・handoff の記録は当時の証拠として残し、現行方針は本 issue と City intent §4-1 に記す。

## 検証

- 撤去前の実 FastAPI ルート + 外部キー有効の合成 SQLite で、Building `synthetic`（City 1）を DELETE → 同じ ID・City 2 で POST すると、両方 HTTP 200 となり City 2 に移ることを再現した。単独の更新ガードだけでは防げないことを確認した。
- 新しいルート回帰は変更前に失敗（OpenAPI に POST / DELETE が存在）。変更後は、全 `TABLE_MAP` と未知テーブルへの POST / DELETE が 405 / `Allow: GET` となり、OpenAPI も GET のみになる。書き込み試行前後の合成テーブルの内容は同じ。
- テーブル一覧・スキーマの GET 回帰を追加。既存の GET ページ送り・上限・空テーブル・未知テーブルと、`tests/test_region_admin.py` / `tests/test_city_identity.py` を合わせ、80 passed・108 subtests passed。通常サービスの City 変更拒否・同値での通常項目更新も含む。
- 建物作成・削除内容・退役 ID・world audit の既存回帰も含む8ファイルでは 158 passed・120 subtests passed。
- 変更 Python の `ruff check` と `git diff --check` は合格。`python scripts/gen_reference_docs.py` で API 参照を再生成し、エンドポイント数は 363 → 361。`--check` で API と DB schema は一致。tool catalog は未導入アドオン50件の環境差分だけなので元を保持し、無関係な削除を含めない。
- HOME / SAIVERSE_HOME を一時ディレクトリへ隔離。本番 DB・実サーバー・ペルソナ・LLM は使わない。
- 未検証: 実ブラウザでの表示・保存後の読み戻し、稼働中の世界の操作。旧ガードに対する過去の全体検証を、この撤去差分の全体検証や CI 合格として扱わない。

次 = レビューで caller 監査・入口撤去・読み取り契約の保持を確認し、マージの判断をまはーに渡す。完了扱い・archive 移動はしない。

## 経緯

- 2026-10-03: PR #365 の軽い表示修正から分離して起票。起票時の方針は「汎用 upsert の既存 Building 更新も同じ不変条件で拒否する責任境界を決める。通常項目の編集と新規 Building の City 指定を壊さず、拒否時は全体をロールバックする」。バックエンド修正は当時未実装だった。
- 2026-10-03: 汎用 API の保存前ガードと隔離 DB 回帰を実装。実機未検証のため検証待ちのまま維持する。
- 2026-10-03: レビューで数値文字列の互換性を点検。現行フロントは汎用 API を GET に使用し、Building の保存は型付き world API へ送るが、汎用 POST 自体は `Any` の入口である。最初の Python 比較では同じ City の文字列表現 4 件が回帰で失敗したため、DB の列との比較と既存整数の保存に置き換えた。小数や不正文字列を整数へ切り捨てずに拒否することも追加検証した。

- 2026-10-04: DELETE → 同 ID の POST による迂回が判明。初案は更新一回だけの不変条件に絞り、逆向きの操作と実利用の根拠を検討していなかった。レビューで未使用の書き込み窓口自体を撤去するまはーの方針を確認し、個別ガード追加から入口撤去へ変更した。
