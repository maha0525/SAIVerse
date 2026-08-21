# Track

> 開発者向け概念リファレンス。**全体の位置づけ**は [landscape §3](../overview/landscape.md)、**設計意図**は intent [`persona_action_tracks.md`](../intent/persona_action_tracks.md)、**撤廃計画**は [`track_retirement.md`](../intent/track_retirement.md) を参照。

## 一言で

進行中の作業文脈そのものが **Track**（通称「行動の線」）。**撤廃の途中にある概念**で、担っていた仕事は順に別の機構へ引っ越している。

## いまどこまで消えたか

| 消えたもの | 引っ越し先 | 時期 |
|---|---|---|
| alert 状態機械 / v1 メタ判断 | on_event 判断点への直結 | 2026-08-14（撤去順序①） |
| Track 操作スペル（`track_*`）と deferred ops | 語彙ごと退役（後継は撤去順序④のレパートリー） | 2026-08-21（束 6 第二便） |
| **Track 種別ごとの Handler 三種と `on_track_activated` hook** | 機構ごと退役 | 2026-08-21（束 6 第三便） |
| **ユーザーとの会話の器** | `saiverse/user_conversation.py`（会話の出来事 + main_line 起動 + 沈黙タイマー） | 2026-08-21（束 6 第三便） |
| **メッセージへの Track 刻印（`messages.origin_track_id`）** | 書き手を全撤去（列と既存データは残置。読み手は旧データ向け） | 2026-08-21（束 6 第三便） |
| **ゲーム参加の帳簿（`game_session` Track）** | `region.state`（`is_participating`）一本へ単純撤去 | 2026-08-21（束 6 第三便） |

## いま残っている仕事

- **時間割の `track:N` コマ**（`saiverse/day_plan.py` の指示書組み立て）
- **想起の歩き**（`saiverse/recall_walk.py`）と**経験の台帳**の索引
- **`ActionTrack` テーブルと既存データの `track:N` 参照**

いずれも撤去順序④（レパートリーの新設）以降で引っ越す。**新しいコードから Track を参照しない** — 「いま何をしているか」は開いている出来事、予定は時間割が持つ。

## 実装

- 管理層: `saiverse/track_manager.py`（`TrackManager` — CRUD + 状態遷移のみ）
- 永続化: `action_track` テーブル（`database/models.py`、`ActionTrack`）

## 関連概念

- [Pulse](pulse.md) — ペルソナを動かす駆動単位
- [line / aspect](line.md) — Pulse 内の処理レーン。`line_role` / `scope` / モデル格の供給源は **aspect** 一本（Track には依存しない）

## 参照

- intent: [`persona_action_tracks.md`](../intent/persona_action_tracks.md) / [`track_retirement.md`](../intent/track_retirement.md)
- 地図: [`landscape.md`](../overview/landscape.md) §3
