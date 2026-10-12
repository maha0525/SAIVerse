# AutonomyManager の watchdog tick が実時刻で刻まれる (仮想クロックを見ない)

**ステータス**: 検証待ち (2026-10-12、v0.4 段 3 の前提工事として修正 — 下の「経緯」)
**作成日**: 2026-08-22
**関連**: `saiverse/autonomy_manager.py` / `saiverse/clock.py` / `saiverse/event_scheduler.py` / [沈黙タイマーの同型修正](../intent/track_retirement.md) §8.5 穴 1

## 現物

`AutonomyManager` が EventScheduler へ tick を積むとき、期限を **`datetime.now()`**
で計算している:

- `saiverse/autonomy_manager.py:155` — `start()` の即時 fire
- `saiverse/autonomy_manager.py:277` — `_schedule_next_tick()` の次回予約

EventScheduler と DaySimulator は `saiverse.clock.now()` (仮想クロック) を見るので、
仮想日付のシミュレーション中は**期限がシミュレーション終了後へ飛ぶ**。watchdog の
tick はシムの中で一度も発火しない。

## なぜ今は実害が出ていないか

1. **v0.3 では自律行動そのものを隠している** (autonomous_behavior_v3.md §11)。
   `AutonomyManager` は `AUTONOMY_ENABLED` が真のペルソナにしか立たず、その ON/OFF
   の UI も v0.3 では出ていない
2. 一日シム (`scripts/run_day_sim.py`) は判断点を自分で撃つ設計で、watchdog を
   当てにしていない

つまり「動いていない機構が、動かないシムの中で動かない」状態。発見はしたが、直しても
確かめる手段が今は無い。

## 同型の先例 (直し方はここに書いてある)

会話の沈黙タイマーが**まったく同じ欠陥**を持っており、2026-08-21 の Codex レビューで
`saiverse.clock.now()` 基準へ統一して直した (track_retirement.md §8.5 穴 1)。あの
ときの実害は「仮想日付のシムで会話の出来事が最後まで閉じない」で、症状の出方も同じ。

## 直すとき

`datetime.now()` を `saiverse.clock.now()` へ置き換えるだけ。ただし**直すのは v0.4
の運転の層に着手するときが自然** — v3 §5 のティックスケジューラが `AutonomyManager`
の役割そのものを作り直す予定で、そのとき時計の出どころは設計に含まれる。いま単独で
直すと、消える予定のコードに回帰テストを一本増やすことになる。

**先に直すべき条件**: 一日シムが watchdog の発火を前提にする検証を書こうとしたとき。
その瞬間から「動かない」が実害になる。

## 同じ机で扱う要件 (2026-08-23 に転入)

[会話ロックを持ったまま DB へ書く issue](archive/conversation_lock_held_across_db_write.md)
は遷移の一行の撤去で対象が消滅して解決したが、あちらが v0.4 へ持ち込むと決めていた
**構造の要件**はここへ移す — 同じ「予定表 (スケジューラ) の作り直し」の机で扱うため。

**要件**: 発火スレッドの callback が、会話ロック・DB 書き込み・LLM 呼び出しのいずれも
待たない構造にする。現状の EventScheduler は全ペルソナ・全種類の予約を**単一のスレッド**
で順番に捌いており (`event_scheduler.py:99`)、callback がロックの外で同期実行される
(`event_scheduler.py:357`) ため、一つの callback が何かを待つと他の全ペルソナの時限処理が
その間ずっと後ろへずれる。いま実際に待つものが無くなっただけで、構造は残っている
(次に誰かがロック内で重い処理を書けば再発する)。v3 §5 のティックスケジューラの設計で
「callback は何を待ってよいか」を明示的に決めること。

## 経緯

- **2026-10-12 修正 (v0.4 段 3 の前提工事、`autonomous_behavior_v04_plan.md` §8.2 決定 A と同じ便)**:
  - 時計: `AutonomyManager` の `start()` の即時予約・`_schedule_next_tick()`・
    `get_next_tick_eta_seconds()` を `saiverse.clock.now()` 基準へ統一した。既知だった
    cache keep-alive の予約 (`sea/session_lifecycle.py` `schedule_cache_ttl_pulse`) に
    加え、全数掃討で同族が他に 5 箇所見つかり、同じ修正を当てた — 先回り畳みの見張り
    (`schedule_cold_window_sweep`)、SDS heartbeat の初回と再予約
    (`saiverse/saiverse_manager.py` / `manager/sds.py`)、ペルソナスケジュールの次回発火の
    計算 (`saiverse/schedule_manager.py` `_compute_next_fire_at`) と interval の起点に
    なる `LAST_EXECUTED_AT` の刻印。実モードでは `clock.now()` == `datetime.now()` なので
    挙動は変わらない。
  - 発火スレッドの要件 (上の「同じ机で扱う要件」): EventScheduler の dispatch スレッドは
    期限の来た callback を発火ごとのワーカースレッド (daemon) へ手渡すだけになった。
    callback はブロックしてよく、他の予約を遅らせない。仮想クロックの同期駆動
    (`run_due`) だけは呼び出しスレッド上で同期実行のまま。
  - 回帰テスト: `tests/test_event_scheduler.py` (ブロックする callback が他 key を
    遅らせない / 例外がワーカーで吸収される / run_due は同期)、
    `tests/test_autonomy_manager.py::TestTickDeadlineFollowsVirtualClock`、
    `tests/test_cache_keepalive.py::test_ttl_schedule_deadline_follows_virtual_clock`。
  - 残る検証: 段 3 のティックスケジューラを早回しのシムで回したときに、tick が仮想時刻で
    発火すること (この issue の「先に直すべき条件」そのもの)。
