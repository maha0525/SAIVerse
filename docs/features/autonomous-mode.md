# 自律行動モード

ユーザーからの入力がなくても、ペルソナが能動的に思考・行動する仕組み。概念の詳細は [concepts/pulse.md](../concepts/pulse.md) / [track.md](../concepts/track.md) / [meta-judgment.md](../concepts/meta-judgment.md) を参照。

## 概要

ペルソナは [Pulse](../concepts/pulse.md)（認知サイクル1回分）を回して自律的に活動する。Pulse は「どの [Track](../concepts/track.md)（進行中の作業文脈）に対して思考するか」を [Meta-Judgment](../concepts/meta-judgment.md) が決め、その Track のメインライン Playbook が発話・ツール実行などの行動を生む。

## 何が Pulse を起こすか（時間機構）

> ⚠️ 旧 `ConversationManager`（10秒ごとに全員を回すプロトタイプ）と、その後継だった `SubLineScheduler`（running Track の連続 Pulse）は**廃止済み**。v0.3（develop）の自律稼働は**時間割（自律行動 v2）**の形で、止め具により発火しない。**develop-v0.4 では 2026-10-09 に時間割の運転を撤去し**、自律行動 v3（[autonomous_behavior_v3.md](../intent/autonomous_behavior_v3.md)）の運転へ作り替えている途中（[v0.4 実装計画](../intent/autonomous_behavior_v04_plan.md)）。

develop-v0.4 の現在の姿:

- **ライフ（起床〜就寝）** — 起床・就寝の時刻に、ライフの確定と開始・終了の節目だけが機械の帳簿処理として走る（LLM なし、`saiverse/day_plan.py`）
- **アラーム** — 定時に鳴る（`PersonaSchedule` / スケジューラ）
- **判断点** — 残るのは実イベントへの on_event だけ（`saiverse/autonomy_wiring.py`）。反応は engage_now / add_task（タスク帳に一件積む）/ note_only / ignore
- **ティック**（ライフ中、最後に標準モデルを呼んでから T 分たつと本人が動く一枠）— v0.4 計画の段 2〜3 で入る。それまでの自律の活動はアラームとイベントへの応対だけ
- **AutonomyManager**（`saiverse/autonomy_manager.py`）— 定期 tick は watchdog に縮退。正常時は何もせず、「自律 ON・起床時間帯なのに今日のライフが無い」ときだけライフを張り直す

これらが [PulseController](../concepts/pulse.md) に Pulse を投げ、優先度（USER > SCHEDULE > AUTO）で捌かれる。

Building の旧自動 pulse 間隔 `AUTO_INTERVAL_SEC`（既定 10）は API・DB 互換のための残置値で、現行の駆動には使わない。Building 設定モーダルとワールドエディタの入力欄は撤去し、他項目の保存時は既存値をそのまま送る。

## AUTONOMY_ENABLED（自律行動の ON/OFF）

各ペルソナは `AUTONOMY_ENABLED`（`ai` テーブル、真偽値、既定 ON）だけを持つ。意味は「自律行動を動かしてよいか」の一点。

- **ON（既定）** — ライフの帳簿処理と判断点（develop-v0.4 では on_event）が動く。ただし起床・就寝が未設定ならライフは確定しない（[ライフ](../intent/life.md)が確定しないため）。つまり実質の起動条件は「生きる時間を決めること」
- **OFF** — 自律行動が一切起きない。会話への返答は**止まらない**（話しかければ返事する）

「いま活動時間か、休憩中か」は自律スイッチではなく**ライフ**が持つ。スイッチは元栓（動かしてよいか）、ライフは蛇口（いまその時間か）。

> ⚠️ 旧 `ACTIVITY_STATE`（Stop / Sleep / Idle / Active）は 2026-07-14 に**解体**。実装を追うと全ゲートが「Active か否か」しか見ておらず 3 値は名前だけの飾りで、さらに「Stop＝機能停止」「Sleep＝ユーザー発言で起きる」はコードが存在しなかった（Stop でも返答していた）。経緯は [landscape §9](../overview/landscape.md)。

## 自律行動の中身

v0.3（develop）の形: 起床判断で編成した時間割のコマが発火すると、予算（ラウンド数）付きの作業セッションが走り、対象タスク（目的ノード）に対して実作業を行う。セッション終了・就寝などの節目では判断点がふりかえりを行う（止め具で発火しない）。

develop-v0.4 の形: 時間割・作業セッション・起床就寝とセッション終了の判断は 2026-10-09 に撤去された。やることは台帳（ルーチン / タスク帳 / 手帳）に置き、ティックが一つずつ引いて本人が動く形になる（v3 §4〜§5。ティックは段 2〜3）。ふりかえりの代わりに、約束・やりたいこと・やったことの捕獲は会話の記憶が整理で畳まれる直前のスルースが担い、自律の ON/OFF と関係なく全ペルソナで走る。

> ⚠️ v1 の自律系 Playbook（`track_autonomous` / `meta_autonomy_decision` / `autonomy_*`）は**退役済み**（2026-07-10、時間割への完全移行）。v1 が担った機能は全て v2 に座席がある — 連続実行→コマ内作業セッション、自発性→無意味の予算コマ、割り込み→呼びかけ即応、途絶検知→watchdog。

## グローバル制御

サイドバー / ライフビューから自律行動の再生・停止をトグルできる。停止中は自律 Pulse が起きない。

## 次のステップ

- [concepts/pulse.md](../concepts/pulse.md) - Pulse と時間機構
- [concepts/meta-judgment.md](../concepts/meta-judgment.md) - どの Track を動かすか
- [Playbook/SEA](./playbooks.md) - 行動パターンの定義
