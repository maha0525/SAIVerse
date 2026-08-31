# Observer push API のクライアント再送に冪等性がない

**ステータス**: 未解決 (2026-08-03 起票。RSS フィード実装の Codex レビュー十八巡目から切り出し)

## 問題

Observer の push API (`record_metrics` 系) は、クライアント (外部デバイス等) が**応答消失後に同じ push を再送**した場合の冪等性を持たない:

1. **メトリクス履歴の重複**: `recorded_at` 省略時はサーバーが呼び出しごとに現在時刻を生成するため、再送は別の (observer_id, recorded_at) キーになり同じ観測値が二重に記録される。明示的な `recorded_at` 付きなら同キー書き直しで冪等 (2026-08-03 の json_set 化で整備) だが、省略時は効かない。
2. **通知イベントの重複**: `_evaluate_notify_rules` → `_notify_building` は building event に一意キー (client_message_id 等) を渡しておらず、明示的 `recorded_at` の再送でもメトリクス行は重複しない一方で**同じ通知イベントが二重に保存される**。

## なぜ RSS 実装のスコープで直さなかったか

- どちらもフィード機能以前からある observer push API の性質で、フィード実装が触ったのは「プロセス内の再試行 (WAL snapshot 競合)」の冪等性のみ (そちらは整備済み)。
- 対策はクライアント生成の batch/request ID の必須化 + 一意制約という **API 契約の変更**を含み、既存クライアント (Stackchan 等の外部デバイス) への影響確認が要る。

## 解決の方向

- push リクエストに安定したクライアント生成 ID (batch_id) を導入し、(observer_id, batch_id) の一意制約で再送を同一バッチに畳む
- 通知イベントにも (observer_id, recorded_at, ルール識別子) の決定的キーを付与して挿入を冪等化
- `recorded_at` を冪等性キーの代替にしない (呼び出し時刻は再送で変わる)

## 関連

- `saiverse/observer_manager.py` (`record_metrics` / `_evaluate_notify_rules`)
- 発見経緯: RSS フィード実装レビュー (in_flight 台帳の RSS 行、2026-08-03)
