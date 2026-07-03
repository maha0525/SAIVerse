# 自律行動モード

ユーザーからの入力がなくても、ペルソナが能動的に思考・行動する仕組み。概念の詳細は [concepts/pulse.md](../concepts/pulse.md) / [track.md](../concepts/track.md) / [meta-judgment.md](../concepts/meta-judgment.md) を参照。

## 概要

ペルソナは [Pulse](../concepts/pulse.md)（認知サイクル1回分）を回して自律的に活動する。Pulse は「どの [Track](../concepts/track.md)（進行中の作業文脈）に対して思考するか」を [Meta-Judgment](../concepts/meta-judgment.md) が決め、その Track のメインライン Playbook が発話・ツール実行などの行動を生む。

## 何が Pulse を起こすか（時間機構）

> ⚠️ 旧 `ConversationManager`（10秒ごとに全員を回すプロトタイプ）は**廃止済み**（2026-05-01 の認知モデル移行で no-op 化）。現在の自律稼働は2層のリズムで駆動される。

- **AutonomyManager**（`saiverse/autonomy_manager.py`、既定 約50分間隔）— per-persona のタイマー。tick でメタ判断 Pulse を起こす。**自律バイオリズムの大リズム**
- **SubLineScheduler**（`saiverse/pulse_scheduler.py`、既定5秒ポーリング）— running 状態の自律 Track を拾って Pulse を連続実行する。**小リズム**

これらが [PulseController](../concepts/pulse.md) に Pulse を投げ、優先度（USER > SCHEDULE > AUTO）で捌かれる。

Building 側の自動 pulse 間隔は `AUTO_INTERVAL_SEC` カラム（既定 10 秒）で持つ。

## ACTIVITY_STATE（自律性の宣言）

各ペルソナは `ACTIVITY_STATE`（`ai` テーブル、既定 `Idle`）で自律性を外部に宣言する。

| 状態 | 説明 |
|---|---|
| `Active` | アクティブに活動中 |
| `Idle` | 待機（応答可能、次の起動を待つ） |
| `Sleep` | 休眠 |
| `Stop` | 停止 |

## 自律行動の中身

メタ判断が自律 Track を選ぶと、`track_autonomous`（自律 Track メインライン）が回る。さらに `meta_autonomy_decision` が次に実行する能力 Playbook を選び、以下のような自律活動を行う（→ [Playbook カタログ](../reference/playbook-catalog.md)）:

- `autonomy_creation` — 創作（ドキュメント執筆・画像生成）
- `autonomy_memory_organization` — 記憶整理（Memopedia）
- `autonomy_web_research` — Web 調査

## グローバル制御

サイドバー / ライフビューから自律行動の再生・停止をトグルできる。停止中は自律 Pulse が起きない。

## 次のステップ

- [concepts/pulse.md](../concepts/pulse.md) - Pulse と時間機構
- [concepts/meta-judgment.md](../concepts/meta-judgment.md) - どの Track を動かすか
- [Playbook/SEA](./playbooks.md) - 行動パターンの定義
