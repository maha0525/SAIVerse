# Issue: Building 設定の「自動インターバル」設定を削除

**ステータス**: 🔲 未着手
**優先度**: low
**作成日**: 2026-05-09
**関連**: Building 設定 UI, `database/models.py` Building テーブル, 自律稼働 (Phase 3) への移行

## 背景

Building の設定項目に「自動インターバル」(ConversationManager が round-robin で `run_pulse()` を呼ぶ間隔) があるが、v0.3.0 のバイオリズム (1 時間サイクル) ベースの自律稼働に移行するため、Building 単位のインターバル設定はもう不要。

残しておくと UI ノイズになるので削除する。

## 確認事項

1. DB スキーマ: Building テーブルに `auto_pulse_interval` 等のカラムがあるか (CLAUDE.md には言及あり)
2. UI: Building 設定画面のどこに該当項目があるか
3. ロジック: ConversationManager が今もこの値を読んで動いているか

## 解決案候補

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
