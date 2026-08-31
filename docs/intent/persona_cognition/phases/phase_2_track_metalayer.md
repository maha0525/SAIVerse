# Phase 2 — Track / MetaLayer / Handler 基盤

**親**: [../README.md](../README.md)
**ステータス**: ✅ ほぼ完了 (`AI.current_active_track_id` カラムのみ残件)
**旧称**: Phase C-1 (MetaLayer / Track 基盤) + Phase 1.1 (Pulse-root context)

---

## 目的

action_tracks / notes テーブル + alert ベースのメタレイヤー + Handler パターン基盤の整備。Phase 3〜5 で構築する応用機構の足場となる。

---

## タスク

### Track / Note の永続化

| 項目 | 状態 | 実装場所 |
|------|------|---------|
| `action_tracks` テーブル | ✅ | `database/models.py:395` |
| `notes` テーブル | ✅ | `database/models.py:436` |
| `note_pages` テーブル | ✅ | `database/models.py:460` |
| `note_messages` テーブル | ✅ | `database/models.py:474` |
| `track_open_notes` テーブル | ✅ | `database/models.py:492` |
| `AI.ACTIVITY_STATE` カラム | ✅ | `database/models.py:56` |
| `AI.SLEEP_ON_CACHE_EXPIRE` カラム | ✅ | `database/models.py:59` |
| `AI.current_active_track_id` カラム | 🔲 | 未実装。運用上は不影響だが計画上は予定あり |

### MetaLayer / Track ツール群

| 項目 | 状態 | 実装場所 |
|------|------|---------|
| `MetaLayer` クラス (alert observer + Playbook ディスパッチ) | ✅ | `saiverse/meta_layer.py` |
| `track_*` ツール群 (create/activate/pause/wait/resume/complete/abort/forget/recall/list) | ✅ | `builtin_data/tools/track_*.py` |
| `set_alert` 機構 | ✅ | TrackManager 内部 |
| `inject_persona_event` 経由の alert ディスパッチ | ✅ | 既存経路を活用 |

### Handler 雛形

| 項目 | 状態 | 実装場所 |
|------|------|---------|
| `track_handlers/` パッケージ | ✅ | `saiverse/track_handlers/` |
| `UserConversationTrackHandler` | ✅ | `saiverse/track_handlers/user_conversation_handler.py` |
| `SocialTrackHandler` | ✅ | `saiverse/track_handlers/social_track_handler.py` |
| `AutonomousTrackHandler` | ✅ | `saiverse/track_handlers/autonomous_track_handler.py` |

### Pulse-root context (旧 Phase 1.1)

| 項目 | 状態 | 実装場所 |
|------|------|---------|
| `pulse_root_context.py` 構築機構 | ✅ | (Phase 1.1 マージ済み) |
| Handler に `track_specific_guidance` 属性 | ✅ | `track_handlers/*` |

---

## 設計上のポイント

### Handler パターン

新しい Track 種別を追加する時に Python コードを書くだけで拡張できるよう、TrackManager 本体には種別固有のロジックを入れない。詳細は [../04_handlers.md](../04_handlers.md)。

### 永続 Track と一時 Track の分離

`is_persistent=true` の Track (対ユーザー会話 / 交流) は `completed`/`aborted` に遷移しない。これにより:

- ペルソナの「核」となる関係性が永続化される
- 再会時に既存 Track が自然に再アクティブ化される (Phase 5 で完全運用)

### output_target と audience の分離

物理的到達範囲 (output_target) と意図的宛先 (audience) を別の軸で管理。1 対 1 もビルディング多者も `output_target=building:current` の同じ規格で統一できる。詳細は [../01_concepts.md](../01_concepts.md#output_target-と-audience-の分離)。

---

## 完了の判定基準

- [x] action_tracks / notes 系 5 テーブルが揃って、Track/Note の CRUD が API レベルで動く
- [x] MetaLayer が alert を受け取り、対応する Playbook (Phase 3 で整備) を起動できる
- [x] `track_*` ツール群すべてがテスト通過 + 運用ペルソナで動作確認済み
- [x] 3 種類の Handler (UserConversation / Social / Autonomous) が登録され、対応する Track を自動作成できる
- [x] ペルソナの ACTIVITY_STATE が 4 段階 (Stop/Sleep/Idle/Active) で動く
- [ ] `AI.current_active_track_id` カラムが追加され、各ペルソナの現在アクティブ Track が DB から参照できる

最後の項目以外は完了。`current_active_track_id` は Phase 4 で MainLineScheduler を実装する時に必要になる可能性が高いため、Phase 4 着手前に追加する。

---

## 残件: `AI.current_active_track_id` カラム

```sql
ALTER TABLE AI ADD COLUMN current_active_track_id TEXT;
```

**用途**:
- ランタイムで「このペルソナは今どの Track を実行中か」を高速に問い合わせる
- 起動時に前回の running Track を復元する (再起動を跨いだ継続性)
- メタレイヤー定期実行時の対象 Track 特定

**実装メモ**:
- マイグレーション必要 (`database/migrate.py`)
- `track_activate` ツール内で同時更新するロジック追加
- 既存 Track 状態管理と整合を保つ (DB の真実は `action_tracks.status='running'` の Track と一致すべき)

---

## Phase 3 以降への前提条件

- Track Handler が登録されていること → Phase 3 の Playbook が Handler.pulse_completion_notice を取得して固定情報を組む
- MetaLayer が alert を受け取れること → Phase 3 の `meta_judgment.json` が動く
- `track_*` ツール群が揃っていること → Phase 3 の Playbook 内でスペル発火に使う

---

## 関連ドキュメント

- [../01_concepts.md](../01_concepts.md) — Track / Note / アクティビティ状態
- [../03_data_model.md](../03_data_model.md) — テーブルスキーマ
- [../04_handlers.md](../04_handlers.md) — Handler パターン
- [phase_3_lines_playbooks.md](phase_3_lines_playbooks.md) — Track Playbook の整備
