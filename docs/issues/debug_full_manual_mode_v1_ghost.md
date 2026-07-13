# Issue: デバッグの「完全手動モード（全タイマー停止）」は v1 の亡霊——退役 or 実態縮退の裁定済み掃除

**ステータス**: 🔲 未着手（まはー裁定済み: 掃除対象。2026-07-14）
**優先度**: low（実害なし・表示と実態の乖離）
**作成日**: 2026-07-14
**関連**: `frontend/src/components/DebugPanel.tsx`（完全手動モードトグル）/ `api/routes/people/debug.py` / `saiverse/saiverse_manager.py` L295・L1625（現役の読み手）/ `docs/intent/persona_cognition/debug_controller.md`

## 経緯

ライフ v0.5 改修B（v1 亡霊の掃除、2026-07-14）で、まはーの掃除指示にあった「タイマー停止」の正体がこれと判明。改修B の実装時は debug_controller.md の intent がある現役デバッグ機能と判断して残したが、**まはーの裁定は掃除対象**。

## 実態（2026-07-14 確認）

- UI 文言「完全手動モード: OFF 全タイマー停止して手動へ」——「全タイマー」は v1（SubLineScheduler の 30 秒 Pulse・50 分 tick が主駆動だった頃）の言葉
- v2 でこのモードが実際に止めるのは **wait_response タイムアウトの予約抑止**（`saiverse_manager._wait_response_timeout_provider` が対象ペルソナで None を返す）にほぼ縮んでいる
- 機能の読み手は現役（消すなら読み手ごと）

## 対応方向（次セッションで裁定）

1. **丸ごと退役**: v2 のデバッグは ACTIVITY_STATE（Stop/Idle/Active）とライフ設定で足りるなら、モードごと削除（UI・API・provider 分岐・debug_controller.md 改訂）
2. **実態縮退**: 「会話の応答待ちタイマーを止める」という現役の一機能に名前と文言を合わせて残す

まはーのデバッグ実需（まだ使う場面があるか）を聞いてから決める。
