# Issue: チャット UI のメッセージバブルのコピーボタンがスマホで機能しない

**ステータス**: 🔲 未着手
**優先度**: medium
**作成日**: 2026-05-09
**関連**: `frontend/` チャット UI のメッセージバブル / コピー実装

## 背景

PC ブラウザではメッセージバブルのコピーボタンでテキストをクリップボードにコピーできるが、スマホ (モバイル Safari/Chrome) では機能しない。

原因候補:
- `navigator.clipboard.writeText` が HTTPS 限定 (モバイルだと localhost 以外要 HTTPS)
- タップ時のイベントハンドラが click ではなく touchend で発火しないなど
- ボタンのホバー表示が touch 環境で表示されず押せない

## 確認事項

1. スマホで実際にどう失敗しているか (ボタンが押せない / 押せるがコピーされない / エラー)
2. アクセス URL が HTTP か HTTPS か
3. `navigator.clipboard` 利用可能性チェックがあるか、フォールバックがあるか

## 解決案候補

- `navigator.clipboard` 不可時の `document.execCommand('copy')` または textarea fallback
- HTTPS 化 / localhost 経由のアクセス
- ホバーで出るボタンを touch 環境では常時表示にする

## 関連リソース

- メモリ: Frontend: saiverse:// URI Protocol Handling (関連は薄い)

## ログ

- 2026-05-09: issue 起票。スマホでの失敗モード未確認。
