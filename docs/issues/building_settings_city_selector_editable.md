# Building 設定の所属 City が変更できるように見える

**ステータス**: 検証待ち（隔離 UI の実操作は未実施）
**作成日**: 2026-10-02
**関連**: [City の不変条件](../intent/city_identity.md#4-1-既存-building-の所属-city-も通常編集では変えない) / [設定差分の監査](world_editor_settings_sync.md)

## 原因と修正範囲

`manager/admin.py:update_building` は、W7 分離監査 D5 の裁定に従って既存 Building の `CITYID` 変更を拒否する。WorldEditor では既に既存 Building の City 欄が無効だが、BuildingSettingsModal は選択・変更できるため、保存して初めてエラーになる。

個別モーダルの City 欄を disabled にし、変更ハンドラーを取り除く。API から読み込んだ City と選択肢の表示名（空なら `CITY_SLUG`）を表示し、PUT の `city_id` は引き続き読み込んだ値を送る。名前・説明・収容数など通常項目の編集と保存は維持する。City 移送の実装、バックエンドの制約緩和、他の設定項目や非同期処理の設計変更は含めない。

## 検証

- 単独回帰: `cd frontend && node scripts/test-building-city-immutable.cjs`。実際の TSX と保存ハンドラーを、合成データ・hook/API のモックで実行し、City select は ReactDOMServer の HTML でも検査する。
- 異なる City ID、表示名と slug フォールバック、disabled・変更ハンドラーなし、保存 payload の維持、通常編集、保存失敗と再試行、未保存で閉じて再開、古い選択の読込応答、ロード中と存在しない Building の保存拒否を対象とする。
- 実ブラウザ、実 API→DB、既存世界・本番ペルソナは操作しない。隔離ブラウザでの操作感と保存後の読戻しは未検証の境界として残す。

### 自動確認の結果

- 単独回帰 8 ケース、`npm test`、`tsc --noEmit`、`npm run build`、`node --check scripts/test-building-city-immutable.cjs`、`check_in_flight.py`、`git diff --check` は合格。
- 既存バックエンド回帰 `tests/test_region_admin.py::BuildingCityImmutableTestCase -n 0` は、独立した一時 HOME / SAIVERSE_HOME / ログ先で 2 件合格。City 変更拒否と通常項目の永続化を一時 SQLite で確認した。
- モーダルへの ESLint はエラー 0（既存警告 9）。CJS の ESLint は既存設定の `react-hooks` plugin 解決エラーで実行不可。無変更の `scripts/test-dev-origins.cjs` だけを対象にしても同じエラーとなる。CJS 自体の構文検査と実行は合格。
- 初回の単独 tsc は自動生成 `addon-panels.generated.ts` が無く失敗。通常の prebuild で生成後に再実行して合格した。

## 経緯

- 2026-10-02: 設定差分監査から、既存の City 変更不可契約に限定して切り出し。修正前の単独回帰は `existing Building City must not be editable` で失敗し、UI の食い違いを再現した。
