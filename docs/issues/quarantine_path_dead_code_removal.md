# Issue: quarantine 経路 (corrupted log.json recovery) の dead code 撤去

**ステータス**: 🔲 未着手
**優先度**: low (= 害なし、 ただし「触れない UI が残っている」 状態)
**作成日**: 2026-05-20
**関連**:
- `docs/intent/building_memory_unified.md` (= Phase 2+3 で 5 状態判定廃止)
- `docs/handoff_2026-05-20_building_memory_db.md`

## 背景

Phase 2+3 で source of truth が DB (`building_messages`) に移行した結果、 旧 `log.json` の「5 状態判定 + atomic save + corrupted 隔離」 機構は廃止された。 起動時に corrupted log.json で `quarantine` 入りすることが **構造的に発生しない**状態になっている。

しかし quarantine 関連のコード一式は dead code として残存している。 動作影響はない (= entry が増える経路がないので、 UI / API が呼ばれても 404 で返る or 空 list が返る) が、 「触れない UI が表示される」 「使われない大型コードが残る」 状態。

## 残存箇所

### Backend

| 場所 | 内容 |
|---|---|
| `manager/initialization.py:165-258` | `_quarantine_building` メソッド定義 (= 呼び出し元 0、 grep 確認済) |
| `manager/initialization.py:177-181` | quarantine 説明 docstring |
| `manager/initialization.py:225-236` | quarantine alert メッセージ template (= 「対応する」 ボタン誘導文) |
| `saiverse/saiverse_manager.py:109-114` | `self.quarantined_buildings = {}` 初期化 (= 常に空) |
| `manager/gateway.py:393` | `if building_id in self.quarantined_buildings:` (= 常に False) |
| `manager/history.py:103` | 同上 |
| `saiverse/occupancy_manager.py:58-65` | quarantined building への移動を block する処理 (= 常に通る) |
| `api/routes/system.py:188-204` | `GET /api/system/quarantine` (= 常に空 list 返す) |
| `api/routes/system.py:207-282` | `POST /api/system/quarantine/{bid}/restore` (= 常に 404、 ただし通った場合は `building_histories[bid] = data` で DB 不整合リスク) |
| `api/routes/system.py:285-333` | `POST /api/system/quarantine/{bid}/reset` (= 常に 404) |
| `manager/runtime.py` / `manager/admin.py` | `quarantined_buildings` 参照箇所 |

### Frontend

| 場所 | 内容 |
|---|---|
| `frontend/src/components/QuarantineModal.tsx` | quarantine entry を表示するモーダル本体 (= entries が常に空で UI には何も出ない) |
| `frontend/src/components/SystemAlertBanner.tsx` | quarantine alert の表示 (= 常に出ない) |
| `frontend/src/components/Sidebar.tsx` | quarantine 関連の参照 (= 要確認) |

## 整理方針

**選択肢 A: 完全撤去**

- backend の `_quarantine_building` メソッド、 `quarantined_buildings` dict、 `/quarantine/*` 3 つの API、 関連 alert 生成処理を削除
- frontend の `QuarantineModal.tsx` 削除、 `SystemAlertBanner.tsx` から quarantine 分岐削除
- `OccupancyManager` の quarantine check 削除

**選択肢 B: 保留 (= Phase 2+3 が安定するまで)**

- 「DB が壊れた場合の復旧経路を future feature として残しておく」 のなら、 dead code でも維持しておく価値がある
- ただしその場合は **DB ベースの recovery 機構として再設計**が必要 (= `building_messages` テーブル単位での restore/reset、 SQLite snapshot ベースの復旧 etc)

選択肢 B (新設計) は別 intent doc / feature として切り出すべき。 現状の log.json ベース実装は DB と整合しないので、 「残しておく」 を **正当化できない** (= 触っても害がある実装をそのまま放置する根拠がない)。

→ 推奨は **選択肢 A: 完全撤去**。 必要になったら DB ベースで作り直す。

## 検証 (= 完全撤去後)

- `pytest` 950 件が引き続き PASS
- 起動時に `quarantined_buildings` 参照箇所が無くなって例外なし
- フロントエンドビルドが警告なく通る
- alert banner にも何も出ない (= 元から出てないが、 確認)

## 関連リソース

- 廃止された 5 状態判定の経緯: `docs/intent/building_memory_unified.md`
- Phase 2+3 縮退の概要: `docs/handoff_2026-05-20_building_memory_db.md`

## ログ

- 2026-05-20: 害なし整理対象として issue 化。 Phase 2+3 commit 取り後の余裕あるタイミングで対処予定
