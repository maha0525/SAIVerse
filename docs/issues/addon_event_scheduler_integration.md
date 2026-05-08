# Issue: addon ポーリングの EventScheduler 統合

**ステータス**: 🔲 未着手
**優先度**: medium
**作成日**: 2026-05-08
**関連**: Phase 4-e (`docs/intent/persona_cognition/phases/phase_4_pulse_scheduler.md`), `docs/intent/x_integration.md`, `docs/intent/addon_extension_points.md`

## 背景

Phase 4-e でコア側のポーリング (ScheduleManager / AutonomyManager / internal_alert_poller / `_db_polling_loop` / `_sds_background_loop`) を `EventScheduler` に統合する。これによりコア側の background thread は EventScheduler 1 本に集約される予定。

一方、addon 側のポーリング (代表例: X リプライ監視 = `TriggerType.X_POLL_DETECTED` を発火する addon) は **Phase 4-e のスコープ外** とした。理由:

- addon に EventScheduler API を公開する話は「addon API 拡張」として独立した設計が必要 (`addon_extension_points.md` の更新を伴う)
- X 監視 addon は OAuth との結合が強く、変更時のリスクが高い
- コア側で完全 push 化が達成できれば、addon が当面独自ポーリングでも害は限定的

このため、**addon を EventScheduler に統合する作業を別タスクとして切り出して記録**する。

## やりたいこと

addon が EventScheduler の `schedule(fire_at, callback, key)` / `cancel(key)` を呼べるようにし、X 監視等の独自 ポーリング loop を廃止する。最終的にプロセス全体で background thread が EventScheduler 1 本に統合される状態を目指す。

## 解決案候補

### (1) addon 拡張ポイントとして EventScheduler を公開

`saiverse/addon_extension_points.md` に新しい hook を追加:

```python
# addon の登録 hook
def on_addon_load(context):
    scheduler = context.event_scheduler  # SAIVerseManager.event_scheduler の参照
    scheduler.schedule(
        fire_at=now + poll_interval,
        callback=lambda: poll_x_and_emit(persona_id),
        key=f"x_poll:{persona_id}",
    )
```

`callback` 内で次回ポーリングを再 schedule する形 (周期処理は手動再 schedule、一発実行は単発 schedule)。

### (2) ヘルパー: 周期 schedule を addon 向けに薄くラップ

addon が頻繁に書く「ポーリング再 schedule」を毎回手書きさせるのは煩雑なので、ヘルパー関数を addon API に追加:

```python
context.event_scheduler.schedule_periodic(
    interval_seconds=poll_interval,
    callback=lambda: poll_x_and_emit(persona_id),
    key=f"x_poll:{persona_id}",
)
```

内部で callback 完了後に自動的に次回を再 schedule する。エラー時の backoff 戦略 (rate limit 検知時等) は addon 側で `interval_seconds` を動的に変えて再登録する形。

### (3) 既存の X 監視 addon を移行

X 監視 addon の独自 thread / sleep loop を捨て、(2) の `schedule_periodic` を使う形に書き換える。

## 関連リソース

- 既存ポーリング実装 (Phase 4-e で統合される側):
  - `saiverse/schedule_manager.py:58-71` (`_schedule_loop`)
  - `saiverse/autonomy_manager.py` (per-persona)
  - `saiverse/internal_alert_poller.py`
  - `saiverse/saiverse_manager.py:341` (`_sds_background_loop`)
  - `saiverse/saiverse_manager.py:391` (`_db_polling_loop`)

- addon 関連:
  - `saiverse/addon_loader.py`
  - `saiverse/addon_extension_points.md`
  - `phenomena/triggers.py:65` (`TriggerType.X_POLL_DETECTED`)
  - X 監視 addon 本体 (パス未確認、`expansion_data/` 配下と推定)

## ログ

- 2026-05-08: Phase 4-e の設計議論で「コア側だけ EventScheduler に統合、addon は別タスク」と判断。本 issue として切り出し。
