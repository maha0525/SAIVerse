# Issue: Building 設定の「自動インターバル」設定を削除

**ステータス**: 🟣 検証待ち — UI 入力の撤去は develop に取り込み済み。既存値と API・DB 互換は保持。実機表示・保存往復は未検証。
**優先度**: low
**作成日**: 2026-05-09
**関連**: Building 設定 UI, `database/models.py` Building テーブル, 自律稼働 (Phase 3) への移行

## 背景

起票時 (2026-05-09)、Building の設定項目に「自動インターバル」(ConversationManager が round-robin で `run_pulse()` を呼ぶ間隔) があるが、v0.3.0 のバイオリズム (1 時間サイクル) ベースの自律稼働に移行するため、Building 単位のインターバル設定はもう不要。

残しておくと UI ノイズになるので削除する。

## 確認結果 (2026-10-02)

- 列は `Building.AUTO_INTERVAL_SEC`（非 null の整数、既定 10）。更新 API `BuildingUpdate.auto_interval` は必須で、`manager/admin.py:update_building` が列へ保存する。
- 入力欄は `BuildingSettingsModal.tsx` と `settings/WorldEditor.tsx` の 2 か所。表示だけを削除し、更新 payload には既存値を保持する。
- `ConversationManager` は `interval` を属性として持つだけで、`start` / `stop` / `trigger_next_turn` はすべて即時 return。UI 削除で駆動は変わらない。
- モーダルの読み込みは `|| 10` を `?? 10` に変え、保存済み 0 の上書きを防ぐ。ワールドエディタは取得値を保持する既存経路を維持する。
- DB 列・API 必須項目・バックエンド検証・ランタイムには手を加えない。クラス / 列の清掃は別の段階。

詳細と不変条件は [intent](../intent/building_auto_interval_setting_removal.md)。

## 起票時の確認事項

1. DB スキーマ: Building テーブルに `auto_pulse_interval` 等のカラムがあるか (CLAUDE.md には言及あり)
2. UI: Building 設定画面のどこに該当項目があるか
3. ロジック: ConversationManager が今もこの値を読んで動いているか

## 起票時の解決案候補

- UI から該当項目を削除
- DB カラムは migration で削除 (or 残しておいて未使用にする — 残す場合は別 issue で清掃)
- ConversationManager のインターバル参照を削除 / 別の (固定 or バイオリズム連動) 値に置換

## 関連リソース

- `saiverse/conversation_manager.py`
- `database/models.py` Building
- `frontend/` Building 設定画面
- メモリ: v0.3.0 Roadmap の Phase 3 (バイオリズム)

## ログ

- 2026-05-09: issue 起票。Phase 3 自律稼働実装と連動して進める想定。

## 検証と残り

- `cd frontend && node scripts/test-building-auto-interval.cjs`: 合格。合成データと fake API で実コンポーネントを通す。両画面の入力欄不在、37 / 0 の更新 payload 保持、別項目の編集、保存失敗と再試行、モーダルの閉じる・開き直しと読み込み ID の保存ガード、建物選択の遅延応答、新規作成への切り替えを検査する。
- UI 欄が残る変更前コードと `|| 10` に戻した変異で、回帰テストがそれぞれ失敗することも確認。
- `npm test`（i18n と既存回帰）、`npm run build`、`npx tsc --noEmit`（prebuild の addon registry 生成後）、`python scripts/check_in_flight.py`、`git diff --check`: 合格。
- `npm run lint`: 既存の ESLint 設定が CJS に対して `react-hooks` plugin を見つけられず停止。変更のない `scripts/test-i18n.cjs` 単体でも同じエラーを確認。変更 TSX の lint は両方エラー 0（モーダルは変更前後とも警告 9、WorldEditor は 66 → 65）。新規テストは `node --check` 合格。
- ブラウザの実レイアウト・キーボード操作・Back/Forward・実機での保存往復は未検証。localhost の CUA 検証は利用できないため、合成テストを実機合格の代用にはしない。
- 実機確認: モーダルとワールドエディタで旧間隔欄が無く、収容数・表示アイテム数を編集できること。別項目を保存しても既存 `AUTO_INTERVAL_SEC` が変わらないこと。本番への操作は別途、対象と操作を指定した承認が必要。

## 経緯

- 2026-10-02: 既存 issue の UI 撤去方針に沿って 2 か所の入力と不要な翻訳キーを削除。DB / API の清掃は切り離し、既存値を保持する。issue は実機検証まで未解決のまま残す。

- 2026-10-03: PR #364 を develop へマージした (レビューはメティス、マージの判断はまはー)。台帳から移送した旧次アクション: 「入力欄の撤去は未マージで、更新 API には既存値を保持する。次 = PR レビューと、両画面の配置・別項目の保存でも既存値が変わらないことの実機確認。」(誰待ち: 私 (PR レビュー) / まはー (実機確認))
