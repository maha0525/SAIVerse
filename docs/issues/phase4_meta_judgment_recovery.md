# Issue: Phase 4 — メタ判断 Pulse の失敗時リカバリ

**ステータス**: 🟡 進行中
**優先度**: medium
**作成日**: 2026-05-08
**関連**: [docs/intent/persona_cognition/README.md](../intent/persona_cognition/README.md) Phase 4 進捗表, `saiverse/meta_layer.py`, [addon_event_scheduler_integration.md](addon_event_scheduler_integration.md)

## 背景

Phase 4 (Pulse 階層 + Scheduler + メタ定期判断) の実装で「メタ判断 Pulse の失敗時リカバリ」が未対応のまま。

現状の `MetaLayer.on_track_alert` / `on_periodic_tick` は `meta_judgment` Playbook を呼ぶが、以下のケースが未ハンドル:

- **LLM error** (API エラー、ネットワーク、レート制限等)
- **parse error** (LLM 応答が response_schema に合わない、JSON パース失敗)
- **Lock 解放のタイミング** (例外で抜けた時に per-persona Lock が確実に解放されるか)

LLM が一時的に応答しなかったり、構造化出力を間違えたりした場合、メタ判断が無音で止まる可能性がある。alert が積まれても判断されずに残る → 自律稼働全体が止まる。

## 解決案候補

### 案 A: try/except でメタ判断 Pulse 全体を包む

- `meta_layer.py` の `_run_meta_judgment` 相当箇所で全エラーを catch
- ログに記録 + per-persona Lock を確実に解放
- alert は再エンキューするか、捨てるかを決める

### 案 B: リトライ機構

- LLM error / parse error の時に N 回まで再試行
- 指数バックオフ
- リトライしても失敗なら捨てる + ログ + ペルソナへの通知 (system event)

### 案 C: ヘルスチェック + 警告通知

- メタ判断 Pulse の失敗が一定回数連続したら、ペルソナの `ACTIVITY_STATE` を `Idle` に下げる + UI 通知
- 自律稼働を一時停止して、人間 (まはー) が気づける状態にする

実用的には A + B + C の組み合わせ:
1. 全例外 catch + Lock 解放 (A)
2. LLM/parse error は数回リトライ (B)
3. それでも失敗なら警告通知 + Activity を下げる (C)

## 関連リソース

- `saiverse/meta_layer.py` — メタ判断 Pulse の現行実装
- `database/models.py` — `meta_judgment_log` テーブル (失敗ログ書き込み先候補)
- [docs/intent/persona_cognition/revisions.md](../intent/persona_cognition/revisions.md) v0.16 — per-persona Lock 機構導入時の経緯
- README.md Phase 4 進捗表 — 「メタ判断 Pulse の失敗時リカバリ (LLM error / parse error / Lock 解放)」の項目

## ログ

- 2026-05-08: issue 起票。Phase 4 残件として認識。実機で問題が顕在化していないが、自律稼働の堅牢性に関わるので medium 優先度。
- 2026-05-08 (追記): Phase 4-e として大規模な再設計に拡張。当初の「失敗時リカバリ」だけでなく以下も含むスコープに:
  - **anchor touch タイミングの修正** (旧: prepare_context での先行 touch → 新: LLM 呼び出し成功後)。Metabolism の現状仕様にあった「キャッシュ切れているのに TTL 内と誤認して長大コンテキスト送信」バグを同時修正。完了 (タスク #1 #2)。
  - **`META_JUDGMENT_CONFIG` ペルソナ別設定カラム** (`cache_threshold_ratio` / `max_retries` / `retry_backoff_seconds`)。AI テーブル + UI + API 整備。完了 (タスク #7)。
  - **EventScheduler への集約** (新規実装)。ScheduleManager / AutonomyManager / internal_alert_poller / `_db_polling_loop` / `_sds_background_loop` のポーリング loop 群を全廃して push 駆動に統一。リアルタイム性をローカル LLM 用途まで考慮した秒精度に。タスク #8 で着手予定。
  - **メタ判断 Pulse 失敗時の統一挙動定義** (本 issue 起票時の中心トピック)。`META_JUDGMENT_CONFIG.max_retries` + `retry_backoff_seconds` に基づく。タスク #9。
  - 関連: addon (X 監視等) のポーリング統合は別 issue ([addon_event_scheduler_integration.md](addon_event_scheduler_integration.md)) に切り出し。Phase 4-e のスコープ外。
