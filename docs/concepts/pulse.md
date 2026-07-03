# Pulse / PulseController

> 開発者向け概念リファレンス。**全体の位置づけ**は [landscape §3](../overview/landscape.md)、**駆動の設計意図**は intent [`autonomous_living.md`](../intent/autonomous_living.md) / [`persona_action_tracks.md`](../intent/persona_action_tracks.md) を参照。ここは「何で・どう動き・どこに実装され・どう増やすか」のナビ。

## 一言で

ペルソナの認知サイクル1回分が **Pulse**、その起動を優先度で捌く制御層が **PulseController**、いつ起動するかを刻む時計が **SubLineScheduler / AutonomyManager** 等の時間機構。

## 役割

ペルソナが「長期的に行動し続ける」には、複数の進行中の [Track](track.md)（作業文脈）を保有しつつ各瞬間には1本だけを実行し、次に何をするか判断し続ける機構が要る。その最小の駆動単位が Pulse。1回の Pulse はアクティブな Track に対して思考・判断し、1つ以上の [Beat](beat.md)（最小行動単位）を生む。

## 仕組み

### Pulse の起動源は4種類

- **ユーザー発話**（chat API）
- **スケジュール**（EventScheduler）
- **Phenomena**（外部イベント → [phenomena.md](phenomena.md)）
- **自律 Track**

これらはすべて **PulseController**（`sea/pulse_controller.py`）に集約される（`submit_user` / `submit_schedule` / `submit_auto` / `submit_meta_judgment`）。

### PulseController = 優先度制御 + 割り込み

PulseController は **優先度ベースのスケジューリング**（USER > SCHEDULE > AUTO）で Pulse 実行を捌く。高優先度の要求が来ると現在の実行を**割り込む**（キャンセル + 割り込みメッセージを記録 + 必要なら再キュー）。per-persona で同時1本（メタ判断レーンのみ並列）。

ユーザー発話の経路は: **User が [Building](building-city.md) に書き込む → chat API → `SAIVerseManager.run_sea_user`（API とランタイムの仲介役）→ `PulseController.submit_user` → Pulse 起動**。この割り込み機構（ユーザーが話しかけたら自律行動を中断する）が「割り込みと復帰」の土台になっている。

### 時間機構（誰がいつ Pulse を起こすか）

PulseController は「起こされた Pulse を捌く」層で、**いつ起こすか**は別の時間機構が刻む。これらが `submit_*` で Pulse を投げる:

| 機構 | 実装 | リズム | 役割 |
|---|---|---|---|
| **SubLineScheduler** | `saiverse/pulse_scheduler.py` | 5秒ポーリング | running 状態の Track を拾って Pulse を回す。自律 Track の「連続する Pulse」を駆動する主体 |
| **AutonomyManager** | `saiverse/autonomy_manager.py` | 既定50分間隔 | per-persona の self-rescheduling timer。tick で `dispatch_autonomy_tick` → メタ判断 Pulse。自律バイオリズムの大リズム |
| EventScheduler / InternalAlertPoller / Phenomena | — | イベント駆動 | スケジュール実行・内部 alert ポーリング・外部イベント起動 |

つまり自律稼働は2層のリズム: **大リズム**（AutonomyManager 50分のメタ判断 tick）→ Track 選択 → **小リズム**（SubLineScheduler 5秒で running 自律 Track の Pulse を連続実行）。

## 実装

- Pulse 実行の入口: `SAIVerseManager.run_sea_user`（ユーザー発話）/ `run_sea_auto`（自律）（`saiverse/saiverse_manager.py`）→ `PulseController` 経由で `SEARuntime.run_meta_user` が実行（`run_pulse` という名前のメソッドは存在しない）
- 制御層: `sea/pulse_controller.py`（`submit_user` / `submit_schedule` / `submit_auto` / `submit_meta_judgment`）
- 時間機構: `saiverse/pulse_scheduler.py`（SubLineScheduler、`SAIVERSE_SUBLINE_SCHEDULER_ENABLED` で制御・既定有効）/ `saiverse/autonomy_manager.py`（AutonomyManager）
- Pulse 内の実行状態: `sea/pulse_context.py`（`PulseContext`）

## 関連概念

- [Track](track.md) — Pulse が動かす対象。Handler が Pulse 挙動（連続/単発）を規定する
- [Meta-Judgment](meta-judgment.md) — どの Track を動かすか決める上位視点
- [Beat](beat.md) — 1 Pulse が生む最小行動単位
- [line / aspect](line.md) — 1 Pulse 内の処理レーンの分岐
- [Phenomena](phenomena.md) — 外部イベントによる Pulse 起動

## 参照

- intent: [`autonomous_living.md`](../intent/autonomous_living.md) / [`persona_action_tracks.md`](../intent/persona_action_tracks.md)
- 地図: [`landscape.md`](../overview/landscape.md) §3
