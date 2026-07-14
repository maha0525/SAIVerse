# Intent: 自律稼働デバッグコントローラー

**親**: [README.md](README.md)
**ステータス**: v0.1 起草 (2026-05-25)。**v0.2 (2026-07-13, life.md v0.5 §9.2-2 改修B)**: 自律行動 v2 で SubLineScheduler (running autonomous Track への 30 秒間隔 sub_line Pulse) 自体が廃止されたため、UI の「sub_line Pulse を 1 回」ボタンと「SubLineScheduler on/off」トグルを `frontend/src/components/DebugPanel.tsx` から削除した。バックエンド (`api/routes/people/debug.py` の `fire-subline-pulse` / `scheduler.subline`) は互換のため no-op のまま残存 (触る意味は無い)。以下 §「発火項目」「タイマー制御」の該当行は歴史的記録として残す。
**関連**: [pulse_dispatch.md](pulse_dispatch.md), [04_handlers.md](04_handlers.md), `saiverse/pulse_scheduler.py`, `saiverse/meta_layer.py`, `saiverse/autonomy_manager.py`

## これは何か

稼働中サーバーに対して、自律稼働のタイマーを無視して **メタ判断 / sub_line Pulse を手動で 1 回ずつ発火**でき、さらに**全タイマーを止めて完全手動でペルソナを駆動**できるデバッグツール。API + ペルソナ画面の UI パネルで提供する。

`scripts/debug_track.py` は DB 直叩きで稼働中サーバーのインメモリ状態に効かない (別物)。本ツールは稼働中の `SAIVerseManager` に直接働きかける。

## なぜ必要か

自律 Track の運用フローは 3 つのタイマーが並行する:
- SubLineScheduler: running autonomous Track に 30 秒間隔で sub_line Pulse
- AutonomyManager: 50 分間隔で idle 時のメタ判断 (periodic tick)
- wait_response timeout: 対話 Track が running の間、30 分沈黙で pause → メタ判断

UC-2「割り込みと復帰」のような往復シナリオを検証するとき、これらのタイマー待ち (30秒/50分/30分) が混ざると非決定論的で観察しづらい。タイマーを止めて手動でステップ実行できれば、検証が決定論的になる。

## 発火項目 (UI ボタン)

| ボタン | 動作 | 実体 |
|---|---|---|
| メタ判断を 1 回 | `manager.meta_layer.on_periodic_tick(persona_id, ctx)` を即発火 | `force` トグル付き |
| sub_line Pulse を 1 回 | 選択した running autonomous Track に `manager.pulse_dispatcher.dispatch_subline_poll(...)` | 30 秒間隔を無視 |
| 会話を切り上げ | running の wait_response Track を pause → メタ判断発火 | `TrackManager._handle_wait_response_timeout` 相当を即時 |

**`force` トグル**: OFF = 本番同様の抑止 (自律行動が OFF / running が wait_response 型なら skip。2026-07-14 以前は `ACTIVITY_STATE != Active` 判定)。ON = 抑止無視で強制発火。`on_periodic_tick` に `force: bool = False` 引数を足し、True のとき 2 つの抑止 (`meta_layer.py:379`, `391`) をスキップする。

## タイマー制御 (UI トグル)

| トグル | 対象 | 実装 |
|---|---|---|
| SubLineScheduler on/off | manager 全体で 1 本 | `subline_scheduler.start()/stop()` は既存。API から叩く経路 + 現在状態の取得を追加 |
| AutonomyManager on/off | per-persona | 既存 `/autonomy/start,stop` API |
| 完全手動モード | 上記 + wait_response timeout | manager にフラグを持ち、`TrackManager._schedule_wait_response_timeout` がフラグ ON 時は予約しない。既存予約は cancel |

完全手動モード ON のとき自動発火はゼロになり、上の 3 ボタンだけでペルソナを駆動する。

## API (案)

`/people/{persona_id}/debug/` 配下:
- `POST .../fire-meta-judgment` (body: `{force: bool}`)
- `POST .../fire-subline-pulse` (body: `{track_id: str}`)
- `POST .../wrap-up-conversation`
- `POST .../scheduler` (body: `{subline, autonomy, manual_mode}`)
- `GET .../scheduler` (現在のタイマー稼働状態)

## UI 置き場所

ペルソナ画面のデバッグセクション (操作対象が per-persona のため)。

## 守るべき点 (不変条件は薄いが)

1. **本番経路と分離**: `force` / 完全手動モードはデバッグ専用フラグ。本番のメタ判断・Pulse 起動経路の挙動を変えない (フラグ OFF 時は現状と完全一致)。
2. **新しい状態遷移を作らない**: 手動発火は既存の `on_periodic_tick` / `dispatch_subline_poll` / `_handle_wait_response_timeout` を呼ぶだけ。Track 状態遷移やメッセージ永続化の新経路を作らない。
3. **完全手動モードの解除でタイマーが正しく復帰**: モード OFF 時に SubLine/Autonomy/wait_response timeout が通常稼働に戻ること。

## ログ

- 2026-05-25: 起草。UC-2 検証の道具として設計確定 (まはー承認)。実装着手。
