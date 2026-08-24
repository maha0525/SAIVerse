# Phase 4 — Pulse 階層 + Scheduler + メタ定期判断

**親**: [../README.md](../README.md)
**ステータス**: ✅ 完了 (2026-05-08, v0.30)
**旧称**: Phase C-3 (Pulse スケジューラ / 定期実行)

---

## 目的

メインライン Pulse / サブライン Pulse の 2 階層分離 + 各 Scheduler 実装 + メタレイヤーの定期実行入口。これにより「自律稼働ペルソナが勝手に走り続ける」状態を技術的に成立させる。

---

## サブ Phase の分割と最終状態

| サブ Phase | 内容 | 状態 |
|-----------|------|------|
| 4-a (旧 C-3a) | Handler に v0.10 拡張属性追加 + AutonomousTrackHandler 新設 + track_autonomous.json | ✅ |
| 4-b (旧 C-3b) | SubLineScheduler 新設 (まずこちらを動かす、メインラインは手動起動でも OK) | ✅ |
| 4-c (旧 C-3c) | AutonomyManager のタイマー化 + `meta_judgment` Playbook + `on_periodic_tick` 入口 | ✅ |
| 4-d (旧 C-3d) | 既存 ConversationManager との関係整理 (no-op 化) | ✅ |
| **4-e** (新規) | **anchor touch 修正 + EventScheduler 集約 + メタ判断 Pulse 失敗時挙動 + ペルソナ別 META_JUDGMENT_CONFIG** | ✅ (2026-05-08) |
| 4-f (新規) | `MainLineScheduler` 相当の機構が必要かの精査 | 🔲 (4-e で AutonomyManager + EventScheduler に集約され、独立した MainLineScheduler は不要と判断。今後の Handler 拡張で再考) |

---

## Phase 4-e の達成内容 (2026-05-08, v0.30)

当初は「メタ判断 Pulse の失敗時リカバリ」だけのつもりだったが、調査中にキャッシュ管理 / スケジューラ全体 / ペルソナ別パラメータ化まで一気に再構成することになった。詳細は [revisions.md v0.30](../revisions.md) を参照。

### 1. anchor touch を LLM 呼び出し成功後に移動

旧 prepare_context での先行 touch を廃止し、`sea/runtime.py:_touch_anchor_after_llm_call` を新設して `runtime_llm.py` の各 `usage = consume_usage()` 直後で呼ぶ。explicit cache モデルは `cache_read > 0 OR cache_write > 0` の時だけ touch、両方 0 なら WARN ログ。

これで「context 組成は走ったが LLM 失敗 → updated_at だけ前進 → 次回 TTL 内と誤判定」の Metabolism バグが解消。

### 2. `META_JUDGMENT_CONFIG` カラム新設

`AI.META_JUDGMENT_CONFIG` (Text/JSON, nullable) でペルソナ別 Pulse パラメータ:

| キー | 既定値 | 役割 |
|---|---|---|
| `cache_threshold_ratio` | 0.3 | TTL 残り割合の閾値 (0.3 = 残り 30% で前倒し fire) |
| `max_retries` | 1 | Pulse 失敗時の即時リトライ最大回数 |
| `retry_backoff_seconds` | 5 | リトライ間の待機秒数 |
| `periodic_interval_minutes` | 50 | メタ判断自動発話間隔 (旧 AutonomyManager.interval_minutes と統合) |
| `keep_cache_alive` | true | TTL 接近で前倒し fire するか (低頻度ペルソナ向けに OFF 可能) |

NULL 運用 = built-in default、`MetaLayer._DEFAULT_JUDGMENT_CONFIG` で管理。不正 JSON / 型不一致は WARN ログ + デフォルト fallback。UI: SettingsModal 自律行動マネージャー直下に「メタ判断 Pulse 設定」セクション。

### 3. EventScheduler 新設 + コア側ポーリング全廃

`saiverse/event_scheduler.py` (min-heap + Condition.wait_until) を新設し、ScheduleManager / AutonomyManager / InternalAlertPoller / DB polling / SDS heartbeat を全部 push 駆動に統合。コア側 background thread は EventScheduler の dispatch thread 1 本に集約された。

外部要因依存のポーリング (inter-city DB / 時間ドリフト型パラメータ / SDS heartbeat) は性質上残るが、tick 自体は EventScheduler に乗っている。addon ポーリングは別 issue 化。

#### キャッシュ TTL 監視

> ⚠ 本節は v0.30 当時の記録。現行 (2026-08-24 時点) は大きく変わっている: 予約は `SessionLifecycle.schedule_cache_ttl_pulse`、key は (persona, model) 単位の `ttl:{persona_id}:{model_key}`、発火するのはメタ判断ではなく keep-alive (意味的に不活性な極小 LLM コール。メタ判断 v1 は退役済み)。予約が立つのは explicit キャッシュ (Anthropic) のみで、非 explicit には何も予約しない (非 explicit の見張りタイマーは、その唯一の目的だったセッションクローズ採取と一緒に 2026-08-24 に撤去)。

`anchor touch` 直後に `_schedule_cache_ttl_pulse` で TTL 接近時刻を予約 (key=`ttl:<persona_id>`)。再 touch で予約上書きされるので「対話継続中は前倒し fire しない、TTL 残り少なくなったら自動的にメタ判断が走る」挙動。`keep_cache_alive=False` のペルソナはこの予約をスキップ + 既存予約を cancel する。

### 4. waiting Track timeout の push 化 (v0.31 で廃止)

> ⚠ v0.31 (2026-05-09) で waiting 機構ごと廃止された。本節は歴史的経緯として残置。詳細: [revisions.md](../revisions.md) v0.31 / [handoff_waiting_track_removal.md](../handoff_waiting_track_removal.md)。

`TrackManager.wait()` で `waiting_timeout_at` を EventScheduler に予約 push (key=`wait_timeout:<track_id>`)。timeout 到達時は `_handle_waiting_timeout` で再 fetch → waiting のままなら `_notify_alert(persona_id, track_id, context={"trigger": "waiting_timeout", ...})` を発火。intent 通り **自動遷移しない**、メタレイヤーへ判断を委ねる。waiting 解除/abort/pause 経路で予約 cancel。

### 5. メタ判断 Pulse 失敗時の retry ループ

`MetaLayer._run_judgment_via_playbook` に `for attempt in range(max_retries + 1):` ループ。`META_JUDGMENT_CONFIG.max_retries` + `retry_backoff_seconds` で制御。

- 例外 → リトライ対象 (WARN ログ + `last_failure_reason` 記録)
- `event_callback` が `error` event 捕捉 → リトライ対象
- 成功 → 即 return
- 全試行枯渇 → WARN ログ "exhausted retries" + 諦める (次回 EventScheduler の TTL 接近 / interval 経過で自動 push)

リトライ前は `time.sleep(retry_backoff_seconds)` で per-persona Lock を保持したまま wait。

### 6. 自動発話間隔の二重管理を解消

旧実装では `AutonomyManager.interval_minutes` (実行時値、再起動で 50 分にリセット) と `META_JUDGMENT_CONFIG.periodic_interval_minutes` (永続値) が独立していた。永続値を真実に統一:

- `AutonomyManager.__init__` の引数優先順: 引数 > META_JUDGMENT_CONFIG > env > module default
- `/api/people/{id}/autonomy/start` / `update_autonomy_config` で interval を受け取った時に `set_interval()` + DB 永続化を併せて行う
- メタ判断 Pulse 設定 UI から「自動発話間隔」削除 (自律行動マネージャー UI が真実の入口)

副次的に `set_interval` の `should_reschedule` 判定が `state in (RUNNING, WAITING)` に修正された (旧 `RUNNING` のみで判定すると WAITING 中の interval 変更が即時反映されないバグ)。

---

## 7 制御点の最終マッピング

| 制御点 | 実装場所 | 状態 |
|--------|---------|------|
| (1) Track 単位の Pulse 間隔 | `action_tracks.metadata.pulse_interval_seconds` | 🟡 metadata 構造のみ、API 未整備 |
| (2) Track 単位の連続実行回数上限 | `action_tracks.metadata.max_consecutive_pulses` | 🟡 同上 |
| (3) メタレイヤー定期実行間隔 | `META_JUDGMENT_CONFIG.periodic_interval_minutes` | ✅ ペルソナ別、UI で編集可、DB 永続化 |
| (4) モデル別キャッシュ TTL | `_get_anchor_validity_seconds` (`saiverse/model_configs.py:get_cache_config`) | ✅ |
| (5) メインライン Pulse のトリガ条件 | EventScheduler の `ttl:<id>` / `autonomy:<id>` / alert observer | ✅ |
| (6) サブライン Pulse のメインライン 1 呼び出しあたり最大回数 | メインライン LLM 出力 → state 経由 | 🔲 |
| (7) サブライン Pulse の間隔 | Handler の `default_subline_pulse_interval` クラス属性 | ✅ |

---

## 完了の判定基準 (Phase 4 全体)

- [x] SubLineScheduler が動作し、自律 Track が立ったら定期的に Pulse が走る
- [x] Handler に Pulse 制御属性が揃い、metadata 経由で個別調整可能
- [x] AutonomyManager がメタレイヤー定期 tick タイマーとして動作し、`on_periodic_tick` が呼ばれる
- [x] ConversationManager と Scheduler 群の責務が整理され、重複や競合がない (ConversationManager は no-op 化)
- [x] env `SAIVERSE_META_LAYER_INTERVAL_SECONDS` で interval 上書き可能 (4-e で META_JUDGMENT_CONFIG が優先、env は fallback)
- [x] メタ判断 Pulse の失敗時リカバリ (LLM error / parse error / Lock 解放 / Track 状態整合) が定義され、テスト済み
- [x] Pattern A/B/C による自動推定 → ペルソナ別の `META_JUDGMENT_CONFIG` で UI 編集可能に変更 (Pattern 自動推定は将来対応)
- [x] 「MainLineScheduler 相当が必要か」の精査結果 → 不要 (AutonomyManager + EventScheduler + alert observer で完結)

`pause_for_user` / `resume_from_user` の alert 経路統合は MetaLayer + alert observer の組み合わせで自然に実現されており、追加の専用 API は不要と判断。

---

## Phase 5 以降への前提条件

- ✅ メインライン Pulse / メタ判断 Pulse の起動経路が EventScheduler に集約 → Phase 5 の Handler tick 機構と統一的に協調できる
- ✅ `on_periodic_tick` が動いている → Phase 5 の内部 alert がメタ判断に乗る
- ✅ 7 制御点の (3)(4)(5)(7) が運用可能 → Phase 5 の Track パラメータが意味を持つ

---

## 関連ドキュメント

- [../02_mechanics.md](../02_mechanics.md) — Pulse 階層 / 7 制御点 / Pulse 完了後挙動
- [../04_handlers.md](../04_handlers.md) — Handler 基底属性
- [../revisions.md](../revisions.md) v0.30 — Phase 4-e の詳細な変更履歴
- [phase_5_autonomy.md](phase_5_autonomy.md) — Handler tick / 内部 alert
- [`../../../issues/uvicorn_traceback_not_in_logs.md`](../../../issues/uvicorn_traceback_not_in_logs.md)
- [`../../../issues/addon_event_scheduler_integration.md`](../../../issues/addon_event_scheduler_integration.md)
