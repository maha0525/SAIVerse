# 自律行動モード

ユーザーからの入力がなくても、ペルソナが能動的に思考・行動する仕組み。概念の詳細は [concepts/pulse.md](../concepts/pulse.md) / [track.md](../concepts/track.md) / [meta-judgment.md](../concepts/meta-judgment.md) を参照。

## 概要

ペルソナは [Pulse](../concepts/pulse.md)（認知サイクル1回分）を回して自律的に活動する。Pulse は「どの [Track](../concepts/track.md)（進行中の作業文脈）に対して思考するか」を [Meta-Judgment](../concepts/meta-judgment.md) が決め、その Track のメインライン Playbook が発話・ツール実行などの行動を生む。

## 何が Pulse を起こすか（時間機構）

> ⚠️ 旧 `ConversationManager`（10秒ごとに全員を回すプロトタイプ）と、その後継だった `SubLineScheduler`（running Track の連続 Pulse）は**廃止済み**。現在の自律稼働は**時間割（自律行動 v2）**で駆動される — 数分刻みの自律 Pulse は意味のある行動を生まない、という v1 の失敗診断に基づく移行（2026-07-10 確定）。

- **時間割（day plan）** — 起床判断（`judgment_day_open`）でペルソナ自身が今日のコマを編成し、各コマが `PersonaSchedule` / スケジューラに予約される。コマ発火で予算付きの作業セッションが走る
- **判断点（judgment points）** — 起床・就寝はスケジュール駆動（`judgment_day_open` / `judgment_day_close`）、会話終了・セッション終了・イベント到着は文脈駆動で発火する（`saiverse/autonomy_wiring.py`）
- **AutonomyManager**（`saiverse/autonomy_manager.py`）— 定期 tick は watchdog に縮退。正常時は何もせず、「自律 ON・起床時間帯なのに今日の時間割が無い / コマ予約が途絶」のときだけ火入れし直す

これらが [PulseController](../concepts/pulse.md) に Pulse を投げ、優先度（USER > SCHEDULE > AUTO）で捌かれる。

Building 側の自動 pulse 間隔は `AUTO_INTERVAL_SEC` カラム（既定 10 秒）で持つ。

## AUTONOMY_ENABLED（自律行動の ON/OFF）

各ペルソナは `AUTONOMY_ENABLED`（`ai` テーブル、真偽値、既定 ON）だけを持つ。意味は「自律行動を動かしてよいか」の一点。

- **ON（既定）** — 時間割・判断点が発火する。ただし起床・就寝が未設定なら何も起きない（[ライフ](../intent/life.md)が確定しないため）。つまり実質の起動条件は「生きる時間を決めること」
- **OFF** — 自律行動が一切起きない。会話への返答は**止まらない**（話しかければ返事する）

「いま活動時間か、休憩中か」は自律スイッチではなく**ライフ**が持つ。スイッチは元栓（動かしてよいか）、ライフは蛇口（いまその時間か）。

> ⚠️ 旧 `ACTIVITY_STATE`（Stop / Sleep / Idle / Active）は 2026-07-14 に**解体**。実装を追うと全ゲートが「Active か否か」しか見ておらず 3 値は名前だけの飾りで、さらに「Stop＝機能停止」「Sleep＝ユーザー発言で起きる」はコードが存在しなかった（Stop でも返答していた）。経緯は [landscape §9](../overview/landscape.md)。

## 自律行動の中身

起床判断で編成した時間割のコマ（作る / 知る / 無意味の予算 等）が発火すると、予算（ラウンド数）付きの作業セッションが走り、対象タスク（目的ノード）に対してドキュメント執筆・調査などの実作業を行う。セッション終了・会話終了・就寝などの節目では判断点がふりかえり（タスク裁定・やりたいこと候補の採取・残り時間割の組み替え）を行う。

> ⚠️ v1 の自律系 Playbook（`track_autonomous` / `meta_autonomy_decision` / `autonomy_*`）は**退役済み**（2026-07-10、時間割への完全移行）。v1 が担った機能は全て v2 に座席がある — 連続実行→コマ内作業セッション、自発性→無意味の予算コマ、割り込み→呼びかけ即応、途絶検知→watchdog。

## グローバル制御

サイドバー / ライフビューから自律行動の再生・停止をトグルできる。停止中は自律 Pulse が起きない。

## 次のステップ

- [concepts/pulse.md](../concepts/pulse.md) - Pulse と時間機構
- [concepts/meta-judgment.md](../concepts/meta-judgment.md) - どの Track を動かすか
- [Playbook/SEA](./playbooks.md) - 行動パターンの定義
