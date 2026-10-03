# Issue: quarantine 経路 (corrupted log.json recovery) の dead code 撤去

**ステータス**: 🟣 検証待ち (撤去差分の PR レビュー待ち、未マージ)
**優先度**: low (= 害なし、 ただし「触れない UI が残っている」 状態)
**作成日**: 2026-05-20
**関連**:
- `docs/intent/building_memory_unified.md` (= Phase 2+3 で 5 状態判定廃止)
- `docs/handoff_2026-05-20_building_memory_db.md`

## 現在地 (2026-10-02)

選択肢 A の完全撤去。実行コード全体の文字列・参照を再調査し、登録元は
`InitializationMixin._quarantine_building` の一箇所のみ、呼び出し元はゼロだった。
manager の登録簿も空の初期化しかなく、新しい実稼働の登録元は無い。

- backend の登録元・登録簿・API 3 本・chat の常時 false の `quarantined` フィールドと、
  Persona / Gateway / 移動 / event の拒否分岐を撤去。
- 旧復旧だけが使う cursor clamp / 採番リセット / conscious_log 保存補助と、
  呼び手が無い JSON バックアップ補助を撤去。通常の DB バックアップは変更しない。
- モーダル・CSS・サイドバー取得 / クリック制限・専用アラート分岐と不要な翻訳を撤去。
- **現役の旧ログ検算、取り込み、読めないファイルの退避 API / ボタン、通常の
  startup alerts は維持**。退避 API が共用していたレスポンス型は専用の名前へ変更。
- 既存ファイルや DB の移行・削除はしない。`building_histories` 整理は別件のまま。

検証結果は下の経緯へ記録する。次は draft PR のレビュー。マージや本番への適用は未実施。

## 背景 (起票時の記録)

Phase 2+3 で source of truth が DB (`building_messages`) に移行した結果、 旧 `log.json` の「5 状態判定 + atomic save + corrupted 隔離」 機構は廃止された。 起動時に corrupted log.json で `quarantine` 入りすることが **構造的に発生しない**状態になっている。

しかし quarantine 関連のコード一式は dead code として残存している。 動作影響はない (= entry が増える経路がないので、 UI / API が呼ばれても 404 で返る or 空 list が返る) が、 「触れない UI が表示される」 「使われない大型コードが残る」 状態。

## 残存箇所 (起票時、撤去前の記録)

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

## 検証方針 (起票時)

- `pytest` 950 件が引き続き PASS
- 起動時に `quarantined_buildings` 参照箇所が無くなって例外なし
- フロントエンドビルドが警告なく通る
- alert banner にも何も出ない (= 元から出てないが、 確認)

## 関連リソース

- 廃止された 5 状態判定の経緯: `docs/intent/building_memory_unified.md`
- Phase 2+3 縮退の概要: `docs/handoff_2026-05-20_building_memory_db.md`

## 経緯

- 2026-05-20: 害なし整理対象として issue 化。 Phase 2+3 commit 取り後の余裕あるタイミングで対処予定
- 2026-10-02: 登録元・読み手・API/UI を再調査し、選択肢 A の撤去差分を実装。`_quarantine_building` は定義だけで呼び手が無く、実稼働の登録元はゼロ。旧ファイルの検算と退避は別経路として保全。未マージのため issue は archive へ動かさず、台帳に PR レビュー待ちとして追加。

### 撤去差分の検証 (2026-10-02)

- 隔離した `SAIVERSE_HOME` と一時 SQLite DB、合成データ、偽 LLM の既存テストだけを使用。本番ペルソナ・本番 DB・有料推論は使用しない。
- 起動・共有ログ・移動・Gateway・履歴 API・取り込み・退避・システム API の関連テスト: **824 passed、85 subtests passed** (`pytest -n 4`)。
- 最終差分の追加再検査 (新規撤去テスト / 退避 API / localization / i18n / tell / streaming placeholder): **159 passed**。上記と対象が一部重なるため合算しない。全リポジトリの full suite は未実行。
- 新規 `tests/test_quarantine_retirement.py`: 壊れた旧ファイルで起動 → event / Gateway / Persona の DB 書き込み → HTTP 履歴表示 → 退避 → 再起動の検算。旧 API 3 本は 404、chat の廃止フラグは OpenAPI にも無い、DB の履歴と同じ部屋の別アラートは退避後も残る。
- 既存退避テストの `/../` は Linux の HTTP クライアントが正規化して 404 となり、変更前の `system.py` でも失敗した。HTTP の拒否 (400 または 404) と、同じ ID を関数へ渡す内部の 400 を分けて固定。ファイルが変わらない検査も維持。
- `npm test`、独立した `npm run test:alerts`、`npx --no-install tsc --noEmit`、標準の `npm run build` (**Next.js 16.1.6 / Turbopack**) が合格。アラートの critical 自動展開、開閉、退避キャンセル、HTTP / 通信失敗、再試行、処理中のボタン無効化、他のアラートの残存をコンポーネントの state / handler で検査。ブラウザ実機の外観確認は未実施。
- 変更 Python の `ruff check`、`git diff --check`、`scripts/check_in_flight.py` が合格 (台帳の既存経過措置 1 行は警告のまま)。
- `scripts/gen_reference_docs.py` で API 参照を再生成 (366 → 363 ルート)。`--check` は API / DB 参照が一致。tool-catalog はこの環境に無いアドオン由来の既存差分が出るため変更を戻し、無関係なツール一覧削除は含めない。
