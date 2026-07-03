# Track / Handler

> 開発者向け概念リファレンス。**全体の位置づけ**は [landscape §3](../overview/landscape.md)、**設計意図**は intent [`persona_action_tracks.md`](../intent/persona_action_tracks.md) を参照。

## 一言で

進行中の作業文脈そのものが **Track**（通称「行動の線」）、Track 種別ごとの振る舞いを定義するパターンが **Handler**。

## 役割

対ユーザー会話・自律稼働・交流・外部通信などが、それぞれ1本の Track として並存する。実行されるのは常にアクティブな1本のみで、休止中の Track は状態を保ったまま残り、[Meta-Judgment](meta-judgment.md) の判断により再開される。これにより「並列に複数の関心事を抱えつつ、今は1つに集中する」というペルソナの行動継続が成立する。

## 仕組み

### 永続 Track と一時 Track

- **永続 Track** — ユーザーごとの会話・交流。完了・中止に遷移しない
- **一時 Track** — プロジェクト・自律行動。目的達成で完了・中止する

### Handler が Pulse 挙動を決める

Handler の中核は **`post_complete_behavior`**（[Pulse](pulse.md) 完了後にどうするか）。これが Track 種別ごとの Pulse 挙動を決める:

| Handler | 実装 | `post_complete_behavior` | 挙動 |
|---|---|---|---|
| AutonomousTrackHandler | `autonomous_track_handler.py` | `meta_judge` | 完了後メタ判断 → 続行/切替/完了。**連続実行型**（`max_consecutive_pulses=-1`、TTL まで） |
| UserConversationTrackHandler | `user_conversation_handler.py` | `wait_response` | 完了後アイドル化、応答待ち（`max_consecutive_pulses=1`、**単発**） |
| SocialTrackHandler | `social_track_handler.py` | （交流用） | ペルソナ同士の会話の器。※下記の未実装ギャップあり |

これにより「自律 Track は連続、会話 Track は単発で応答待ち」という差が生まれる。SubLineScheduler はこの属性を見て Pulse を回すか止めるかを決める。**新しい Track 種別の追加は対応する Handler を書くだけで済み、TrackManager 本体は変更しない。**

> ⚠️ **ペルソナ間会話の現状**: 交流（Social）Track と `SocialTrackHandler`・自動作成はあるが、**「他ペルソナ発話イベントの受け口」（入口）が未実装**。そのためペルソナ間会話の機序はまだ成立していない（→ [`roadmap_status.md`](../overview/roadmap_status.md) §2）。

## 実装

- 管理層: `saiverse/track_manager.py`（`TrackManager`）
- Handler 群: `saiverse/track_handlers/`（`autonomous_track_handler.py` / `user_conversation_handler.py` / `social_track_handler.py`）
- 永続化: `action_track` テーブル（`database/models.py`、`ActionTrack`）

## 関連概念

- [Pulse](pulse.md) — Track を動かす駆動単位。Handler が挙動を規定
- [Meta-Judgment](meta-judgment.md) — どの Track を動かすか選ぶ
- [line / aspect](line.md) — Track 内の処理レーン

## 参照

- intent: [`persona_action_tracks.md`](../intent/persona_action_tracks.md)
- 地図: [`landscape.md`](../overview/landscape.md) §3
