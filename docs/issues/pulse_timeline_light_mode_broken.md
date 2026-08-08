# Pulse タイムラインがライトモードに対応していない (逆パターン)

**ステータス: 未解決** (2026-08-08 起票。時間割実機検証中にまはーが発見)

## 症状

メモリーモーダルの「Pulse タイムライン」タブが、**ライトモードでも暗い配色のまま**。モーダル本体は白いのに、タイムライン一覧のヘッダ (「200 Pulse (新しい順、最大 200)」) と各行の背景が黒系で表示される。

ダークモード未対応 (ライトの色で固定) は頻出パターンだが、その逆 (ダークの色で固定) はこれが初。ダーク側の色をハードコードしたまま実装された可能性がある。

## 直す時期

表示は読める (実害は低) ので、検証一巡後のダークモード系 UI 修正の束 ([feed_tab_dark_mode_pulldowns.md](feed_tab_dark_mode_pulldowns.md) / [sidebar_autonomy_status_stale_after_start.md](sidebar_autonomy_status_stale_after_start.md)) と一緒に。両テーマ対応チェックリスト (memory: feedback_darkmode_checklist) は「ライト固定」だけでなく「ダーク固定」も対象にすること。

## 関連

- [統合検証手順](../handoff/2026-08-07_timetable_live_verification_run.md) Step 2〜3 (検証中に発見)
