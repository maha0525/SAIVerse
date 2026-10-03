# Pulse タイムラインがライトモードに対応していない (逆パターン)

**ステータス: ✅ 完了** (2026-08-08 起票。時間割実機検証中にまはーが発見)。修正は develop に取り込み済みで、隔離環境の実ブラウザで両テーマの見え方を確認した。

## 症状

メモリーモーダルの「Pulse タイムライン」タブが、**ライトモードでも暗い配色のまま**。モーダル本体は白いのに、タイムライン一覧のヘッダ (「200 Pulse (新しい順、最大 200)」) と各行の背景が黒系で表示される。

ダークモード未対応 (ライトの色で固定) は頻出パターンだが、その逆 (ダークの色で固定) はこれが初。ダーク側の色をハードコードしたまま実装された可能性がある。

## 直す時期

表示は読める (実害は低) ので、検証一巡後のダークモード系 UI 修正の束 ([feed_tab_dark_mode_pulldowns.md](feed_tab_dark_mode_pulldowns.md) / [sidebar_autonomy_status_stale_after_start.md](../sidebar_autonomy_status_stale_after_start.md)) と一緒に。両テーマ対応チェックリスト (memory: feedback_darkmode_checklist) は「ライト固定」だけでなく「ダーク固定」も対象にすること。

## 関連

- [統合検証手順](../../handoff/2026-08-07_timetable_live_verification_run.md) Step 2〜3 (検証中に発見)

## 修正候補と検証 (2026-10-02)

原因は `PulseTimelineViewer.tsx` の sticky ヘッダー・select・本文背景と補助文字にダーク固定色が散在していたこと。中立色を既存のテーマ変数へ寄せ、役割・Spell・注意色はメモリー画面の既存パレットで明暗を切り替える。候補リスト、長文の折り返し、狭幅の操作列も対象。取得・タグ・編集保存の挙動は変更しない。[表示 intent](../../intent/pulse_timeline_display.md)。

- 隔離した React element/handler テスト: 一覧→展開→select 編集→折りたたみ→再展開、未保存状態と詳細キャッシュの保持を確認。`cd frontend && node scripts/test-pulse-timeline-theme.cjs`。
- 実視認は未完了: dot クラウドブラウザーが隔離 localhost を `net::ERR_BLOCKED_BY_CLIENT` で拒否。テーマ・360/960/1600px の見え方が合格したとは扱わない。
- 次は両テーマで、ヘッダー・行・入力プロンプト・gap・select 候補・未保存の印、長文、更新と閉じる/再表示を確認。まはーのレビューまで issue は archive しない。

検証記録: `npm test`、`node scripts/test-pulse-timeline-theme.cjs`、対象 TSX の ESLint、`npx tsc --noEmit`、`npm run build`、`python scripts/check_in_flight.py`、`git diff --check` は通過。全体 `npm run lint` は既存 CJS にも再現する設定エラー (`react-hooks/set-state-in-effect` に plugin が適用されない) で停止。実視認の代替合格にはしない。

## 経緯

- 2026-10-03: PR #358 を develop へマージした (レビューはメティス、マージの判断はまはー)。台帳から移送した旧次アクション: 「修正候補と隔離した表示/操作テストの確認待ち。次は両テーマ・狭幅/通常/ワイド画面の実視認とレビューを行う。」(誰待ち: 私 (実視認可能な環境) → まはー (レビュー))
- 2026-10-03: 完了とした。このタブは `MemoryModal` の `HIDDEN_TABS` で通常の画面から隠されていて、普段使いで実機確認をする機会が無い。[PR #358 のレビュー](https://github.com/maha0525/SAIVerse/pull/358) で、メティスが Windows の隔離環境の実ブラウザ (確認用の作業コピーの中だけでタブを表示) で、ダーク・ライトの両方の一覧・展開した行・role の色・select が読めることを確認済み。台帳から外した行の次アクション: 「ライトとダークの両方に合わせる修正は develop に入った。次 = まはーがライトモードで Pulse タイムラインを開き、見出し・行・選択欄の文字が背景に埋もれず読めることを見る。」(誰待ち: まはー (実機確認))
