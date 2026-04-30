# 認知モデル: データモデル

**親**: [README.md](README.md)
**関連**: [01_concepts.md](01_concepts.md) (用語) / [02_mechanics.md](02_mechanics.md) (動的な仕組み)

このファイルは認知モデルを支える**永続化スキーマ**を集約する。

---

## 7 層ストレージのテーブル対応

[01_concepts.md の 7 層ストレージモデル](01_concepts.md#7-層ストレージモデル)が、本ドキュメントのテーブル設計でどう実現されるかの俯瞰:

| 層 | テーブル / 保存先 | 備考 |
|---|---|---|
| [1] メタ判断ログ領域 | `meta_judgment_log` | ペルソナ単位、メタ判断の全履歴。次のメタ判断時に参考情報として動的注入 |
| [2] メインキャッシュ | `messages` (line_role='main_line', scope='committed') + per-model anchor | Track 横断 1 本、commit/discard 機構あり |
| [3] Track 内サブキャッシュ群 | `messages` (line_role='sub_line', track_id=X, line_id=Y) + per-model anchor | Track + 起点ライン単位、複数起点サブが並走しうる |
| [4] 入れ子ライン一時コンテキスト | ランタイム揮発 (PulseContext 内に保持、DB には保存しない) | 完了時に親へ `report_to_parent`、中間履歴は基本破棄 |
| [5] Track ローカルログ | `track_local_logs` | Track 内のイベント・モニタログ・起点サブの中間ステップトレース |
| [6] SAIMemory (会話の核 / Note) | 既存 `messages` (scope='committed') + `notes` 系 | 想起対象の会話メッセージ、Memopedia/Chronicle の抽出元 |
| [7] アーカイブ | 別 SQLite DB or ファイル (現状未整理、Phase 6 に後送り) | worker 結果等、想起対象外 |

`messages` テーブルは既存を拡張する形で **複数の層 ([2]/[3]/[6])** を担う。区別はメタデータカラム `line_role` / `scope` / `track_id` / `line_id` で行う。

---

## 新設テーブル

### `action_tracks` — 行動 Track

行動制御単位の永続化テーブル。Track ID は **独立した UUID**。

```sql
CREATE TABLE action_tracks (
    track_id TEXT PRIMARY KEY,                  -- UUID
    persona_id TEXT NOT NULL,
    title TEXT,                                 -- "対 mahomu 会話", "交流", "記憶整理", "Kitchen LoRA 完成待ち"
    track_type TEXT NOT NULL,                   -- user_conversation / social / autonomous / waiting / scheduled / external / ...
    is_persistent BOOLEAN NOT NULL DEFAULT FALSE, -- 永続 Track フラグ
    output_target TEXT NOT NULL DEFAULT 'none', -- 'none' / 'building:current' / 'external:<channel>:<address>'
    status TEXT NOT NULL DEFAULT 'unstarted',   -- running / alert / pending / waiting / unstarted / completed / aborted
    is_forgotten BOOLEAN NOT NULL DEFAULT FALSE, -- 直交フラグ (他状態と両立)
    intent TEXT,                                -- 進行中の意図 (自然言語)
    metadata TEXT,                              -- JSON: 相手識別、外部参照、parameters、schedules、thresholds 等
    pause_summary TEXT,                         -- 中断時に作成されたサマリ (最新)
    pause_summary_updated_at TIMESTAMP,
    last_active_at TIMESTAMP,
    waiting_for TEXT,                           -- waiting 状態の待ち相手 (JSON)
    waiting_timeout_at TIMESTAMP,               -- waiting のタイムアウト時刻 (NULL = 無期限)
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,                     -- completed 遷移時刻 (永続 Track では NULL)
    aborted_at TIMESTAMP                        -- aborted 遷移時刻 (永続 Track では NULL)
);
CREATE INDEX idx_action_tracks_persona ON action_tracks(persona_id, status, is_forgotten);
CREATE INDEX idx_action_tracks_last_active ON action_tracks(persona_id, last_active_at DESC);
CREATE INDEX idx_action_tracks_waiting_timeout ON action_tracks(waiting_timeout_at) WHERE status='waiting';
CREATE INDEX idx_action_tracks_persistent ON action_tracks(persona_id, is_persistent, track_type);
```

`metadata` JSON 内の典型項目 (Phase 5 で本格運用):

```json
{
  "parameters": {
    "dirtiness": 0.32,
    "hunger": 0.78
  },
  "schedules": [
    {"cron": "0 10 * * 0", "label": "weekly"}
  ],
  "thresholds": {
    "dirtiness_alert": 0.7,
    "hunger_alert": 0.8
  },
  "pulse_interval_seconds": 30,
  "max_consecutive_pulses": -1,
  "cache_built_at": "2026-04-30T12:00:00Z"
}
```

将来この共通形式を `track_parameters` テーブル等として正規化する可能性はあるが、まず metadata JSON で運用してから判断する (早すぎる正規化を避ける)。

### `notes` — Note (関心の固まり)

```sql
CREATE TABLE notes (
    note_id TEXT PRIMARY KEY,                   -- UUID
    persona_id TEXT NOT NULL,
    title TEXT NOT NULL,                        -- "対エイド", "Project N.E.K.O.", "絵描き"
    note_type TEXT NOT NULL,                    -- person / project / vocation (3 種のみ)
    description TEXT,                           -- ノートの目的説明
    metadata TEXT,                              -- JSON: 対人なら相手 ID、project なら締切等
    is_active BOOLEAN DEFAULT TRUE,             -- アーカイブ済みかどうか
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_opened_at TIMESTAMP,
    closed_at TIMESTAMP                         -- project 完了時等
);
CREATE INDEX idx_notes_persona_type ON notes(persona_id, note_type, is_active);
```

**運用ガイド**:
- `vocation` Note は 1 ペルソナにつき少数 (1〜数個) に保つ
- `person` Note は最初の会話で自動作成
- `project` Note はペルソナまたはユーザーが明示的に作成

### `note_pages` — Note ↔ Memopedia ページ (多対多)

```sql
CREATE TABLE note_pages (
    note_id TEXT NOT NULL,
    page_id TEXT NOT NULL,
    PRIMARY KEY (note_id, page_id)
);
CREATE INDEX idx_note_pages_page ON note_pages(page_id);
```

### `note_messages` — Note ↔ メッセージ (多対多)

```sql
CREATE TABLE note_messages (
    note_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    added_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    auto_added BOOLEAN DEFAULT FALSE,           -- audience 由来 vs 明示追加
    PRIMARY KEY (note_id, message_id)
);
CREATE INDEX idx_note_messages_msg ON note_messages(message_id);
```

### `track_open_notes` — Track ↔ 開いている Note (多対多)

```sql
CREATE TABLE track_open_notes (
    track_id TEXT NOT NULL,
    note_id TEXT NOT NULL,
    opened_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (track_id, note_id)
);
CREATE INDEX idx_track_open_notes_track ON track_open_notes(track_id);
CREATE INDEX idx_track_open_notes_note ON track_open_notes(note_id);
```

### `meta_judgment_log` — メタ判断ログ領域 [1]

7 層 [1] の実体。メタ判断の全履歴を保存する。Track のメインキャッシュからは分離された独立領域で、次のメタ判断時に参考情報として動的注入される。

```sql
CREATE TABLE meta_judgment_log (
    judgment_id TEXT PRIMARY KEY,                -- UUID
    persona_id TEXT NOT NULL,
    judged_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    track_at_judgment_id TEXT,                   -- 判断時点のアクティブ Track (NULL = idle 状態)
    trigger_type TEXT NOT NULL,                  -- 'periodic_tick' / 'alert' / 'pulse_completion' / ...
    trigger_context TEXT,                        -- JSON: alert track_id, alert reason, etc
    prompt_snapshot TEXT,                        -- 動的注入された参考情報含む、判断時のプロンプト要約 (デバッグ用)
    judgment_action TEXT NOT NULL,               -- 'continue' / 'switch' / 'wait' / 'close'
    judgment_thought TEXT,                       -- 判断に至った思考
    switch_to_track_id TEXT,                     -- switch の場合の移動先
    new_track_spec TEXT,                         -- JSON: 新規 Track 作成スペック
    notify_to_track TEXT,                        -- continue の場合の通知内容
    raw_response TEXT,                           -- LLM の生レスポンス (デバッグ用)
    committed_to_main_cache BOOLEAN NOT NULL DEFAULT FALSE  -- このターンがメインキャッシュにも commit されたか
);
CREATE INDEX idx_meta_judgment_persona ON meta_judgment_log(persona_id, judged_at DESC);
CREATE INDEX idx_meta_judgment_track ON meta_judgment_log(track_at_judgment_id);
```

**設計ポイント**:

- `judged_at` で時系列降順に並べて、参考情報注入時に新しい順から取得
- `committed_to_main_cache=TRUE` のレコードは Track 移動の来歴として既にメインキャッシュに乗っている (= 重複注入を避けるための識別子)
- `prompt_snapshot` は判断時のプロンプト全文を要約保存。後追いで「なぜそう判断したか」を追える
- 古い判断は Metabolism 的に要約していく機構が将来必要 (現状は生データ保持、最新 N 件を参考情報注入する単純運用)

### `track_local_logs` — Track ローカルログ [5]

7 層 [5] の実体。Track 内のイベント・モニタログ・起点サブラインの中間ステップトレース等を保管する。Track 内では参照できるが、想起対象 ([6] SAIMemory) には乗らない。

```sql
CREATE TABLE track_local_logs (
    log_id TEXT PRIMARY KEY,                     -- UUID
    track_id TEXT NOT NULL,
    occurred_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    log_kind TEXT NOT NULL,                      -- 'event_message' / 'monitor_signal' / 'sub_step' / 'tool_trace' / ...
    payload TEXT,                                -- JSON: イベント詳細 / モニタ検知値 / サブステップ情報等
    source_line_id TEXT,                         -- ログの発生元ライン ID (NULL = Track 単位で発生)
    visible_to_other_tracks BOOLEAN NOT NULL DEFAULT FALSE  -- Track 越境参照フラグ (将来用、初期は FALSE 固定)
);
CREATE INDEX idx_track_local_logs_track ON track_local_logs(track_id, occurred_at DESC);
CREATE INDEX idx_track_local_logs_kind ON track_local_logs(track_id, log_kind, occurred_at DESC);
```

**`log_kind` の代表的な値**:

- `event_message`: 入退室・Chronicle 完了通知・Track 削除通知 等
- `monitor_signal`: モニタリングラインの検知イベント (Phase 6)
- `sub_step`: 起点サブラインの中間ステップ (ツール呼び出し・思考等)
- `tool_trace`: ツール実行トレース

`visible_to_other_tracks` は将来の Track 越境参照のための予約フィールド (例: ユーザー会話中に「さっきエイドが入室したよね」と話題化する経路)。現在は FALSE 固定、運用機構は Phase 6 後送り。

---

## 既存テーブルの拡張

### `messages` (SAIMemory) のメタデータ拡張

```sql
ALTER TABLE messages ADD COLUMN origin_track_id TEXT;          -- 生成時のアクティブ Track
ALTER TABLE messages ADD COLUMN line_role TEXT;                -- 'main_line' / 'sub_line' / 'meta_judgment' / 'nested'
ALTER TABLE messages ADD COLUMN line_id TEXT;                  -- 起点ライン識別子
ALTER TABLE messages ADD COLUMN scope TEXT NOT NULL DEFAULT 'committed';  -- 'committed' / 'discardable' / 'volatile'
ALTER TABLE messages ADD COLUMN paired_action_text TEXT;       -- action 文 + 応答ペア保存
CREATE INDEX idx_messages_track_line ON messages(origin_track_id, line_role, line_id);
CREATE INDEX idx_messages_scope ON messages(scope);
```

各カラムの用途:

| カラム | 値 | 用途 |
|---|---|---|
| `line_role` | `main_line` | メインライン応答 ([2]) |
|  | `sub_line` | サブライン応答 ([3]) |
|  | `meta_judgment` | メタ判断ターン (続行時 scope='discardable'、移動時 'committed' に昇格) |
|  | `nested` | 入れ子子ラインの中間 (基本 [4] ランタイム揮発、デバッグ目的の保存時のみ) |
| `line_id` | 起点ライン識別子 (UUID) | 1 Track 内で複数起点サブが並走する場合の識別 |
| `scope` | `committed` | 通常メッセージ、コンテキスト構築時に取得対象 |
|  | `discardable` | メタ判断分岐ターン等、続行時は次プロンプトに含めない |
|  | `volatile` | 一時保存 (Pulse 内のみ)、Pulse 完了で削除 |
| `paired_action_text` | LLM ノードの action 文 | 応答メッセージに紐付けて保存 (action 文を user メッセージとして単独保存しない) |

### `pulse_logs` の役割縮退

- `track_id TEXT` カラム追加 (どの Track の pulse か識別、index 付き)
- **役割は実行トレース専用に縮退** (7 層モデル方針に基づく)
- 内的独白の pulse_logs 保管は **`messages` テーブル ([3] サブキャッシュ または [6] SAIMemory) に移管**
- pulse_logs は「どの Pulse でどのノード・ツールが動いたか」のトレース情報のみを残す

旧 `unified_memory_architecture.md` v3 で「pulse_logs を統一記憶の本体に」と位置づけた方針は、本データモデルで **修正対象**。Phase 1 で実装した内的独白の pulse_logs 保管は、新層 ([3] [4]) に移行する作業が必要 (現状はまだ完全移行されていない、Phase 3 完了後の整理事項)。

### `AI` テーブルへの追加カラム

```sql
ALTER TABLE AI ADD COLUMN ACTIVITY_STATE TEXT NOT NULL DEFAULT 'Idle';
-- 'Stop' / 'Sleep' / 'Idle' / 'Active'
ALTER TABLE AI ADD COLUMN SLEEP_ON_CACHE_EXPIRE BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE AI ADD COLUMN current_active_track_id TEXT;
```

既存の `INTERACTION_MODE` (auto/user/sleep) は `ACTIVITY_STATE` (Stop/Sleep/Idle/Active) に置き換える:

| 旧 INTERACTION_MODE | 新 ACTIVITY_STATE | 備考 |
|---------------------|-------------------|------|
| `auto` | `Active` | 自律行動含めて全動作 |
| `user` | `Idle` | 起きてるが自発的には行動しない |
| `sleep` | `Sleep` | 寝てる、ユーザー発言で起きる |
| (新規) | `Stop` | 機能停止、ユーザー操作のみで起きる |

`current_active_track_id` は Phase 2 残件。`AI.METABOLISM_ANCHORS` の per-model 機構と組み合わせて、軽量・重量級それぞれ独立 anchor で 2 本のキャッシュを管理する。

---

## マイグレーション順序

新カラム追加は段階的に行う (`database/migrate.py` で個別マイグレーション):

| Phase | マイグレーション内容 |
|------|---------------------|
| Phase 1 | `meta_judgment_log` / `track_local_logs` テーブル新規作成 |
| Phase 1 | `messages` への line_role / line_id / scope / paired_action_text カラム追加 (デフォルト値で既存行は埋まる) |
| Phase 1 | 既存の旧形式メッセージ (line_role IS NULL) の段階的分類スクリプト |
| Phase 2 | `action_tracks` / `notes` / `note_pages` / `note_messages` / `track_open_notes` テーブル新規作成 |
| Phase 2 | `AI.ACTIVITY_STATE` / `SLEEP_ON_CACHE_EXPIRE` カラム追加 + 旧 INTERACTION_MODE からの一括変換 |
| Phase 2 残件 | `AI.current_active_track_id` カラム追加 |
| Phase 3 | scope 昇格機構の実 SQL UPDATE 整備 (Phase 1.3 で着手済み) |

すべてのマイグレーションは `database/migrate.py` で自動バックアップ後に実行される ([CLAUDE.md](../../CLAUDE.md) 参照)。

---

## SAIMemory thread との関係

**Track と thread は別概念**として運用する:

- メッセージは引き続き thread に物理保存される (既存通り)
- Track はメッセージ集合を直接持たず、Note を介してメッセージとつながる
- 同じメッセージが「対 A Note」「対 B Note」両方に属することができ、3 人会話問題が解消される
- `thread_switch` ツールは既存のままだが、Track 切り替えの主機構ではなくなる

---

## 状態遷移

```
   create
  ────────► unstarted ────activate────► running ────complete────► completed
                                          │   ▲
                                          │   │
                                  pause   │   │ activate
                                          ▼   │
                                       pending ◄───────┐
                                          │            │
                                  activate│            │ activate
                                          ▼            │
   activate ────────► running         (running 占有時) │
   が呼ばれたら                         自動 pending化  │
   現 running は                                       │
   pending に                                          │
                                                       │
   running ──wait_for(...)──► waiting ──response/timeout──► (event)
                                          │
                                          │ resume_from_wait(mode)
                                          ▼
                                  running / pending / aborted

   abort は任意状態から ────► aborted (terminal、永続 Track では遷移不可)
   complete は running から ► completed (terminal、永続 Track では遷移不可)

   is_forgotten フラグは任意状態と両立、forget/recall ツールで切り替え
```

**主要遷移トリガー**:

| 遷移 | トリガー |
|------|---------|
| (new) → `unstarted` | `track_create` |
| `unstarted`/`pending`/`waiting` → `running` | `track_activate` (既存 `running` があれば自動で `pending` に) |
| `running` → `pending` | `track_pause` または別 Track の `track_activate` で押し出される |
| `running` → `waiting` | `track_wait(track_id, waiting_for, timeout)` |
| `waiting` → `running` / `pending` / `aborted` | `track_resume_from_wait(track_id, mode)` <br> mode=activate/pause/abort |
| `running` → `completed` | `track_complete` |
| 任意 → `aborted` | `track_abort` |
| `is_forgotten` ON/OFF | `track_forget` / `track_recall` |
| `waiting` の応答到達 / タイムアウト | SAIVerse 側で検知 → `inject_persona_event` → メタレイヤー判断 |

**`alert` への遷移トリガー** (SAIVerse 側で自動発生):

| トリガー | 対象 Track |
|---------|----------|
| ユーザー発言到着 | 対ユーザー会話 Track (該当ユーザー) |
| 別ペルソナからの自分宛発言 | 交流 Track |
| Kitchen 完了通知 (重要度高い場合) | 関連 Track |
| MCP Elicitation 応答到達 | 関連 waiting Track |
| 外部チャネルからの直接通信 | 該当 Track |
| 内部パラメータ閾値超過 (Phase 5) | 身体的欲求 Track 等 |
| スケジュール時刻到来 (Phase 5) | スケジュール起因 Track |

---

## Track 種別

### 永続 Track (`is_persistent=true`、`completed`/`aborted` 不可)

| `track_type` | 用途 | output_target | 数 |
|------------|------|---------------|---|
| `user_conversation` | ユーザーとの 1 対 1 関係 (永続的な核) | `building:current` または `external:...` | ユーザーごとに 1 個 |
| `social` | 他ペルソナとの会話を扱う**交流 Track** | `building:current` | ペルソナにつき 1 個 |

**ユーザーがペルソナと初めて関わるタイミング**で対応する `user_conversation` Track が自動作成される。**ペルソナ作成時**に `social` Track が自動作成される。

### 一時 Track (`is_persistent=false`、完了/中止可能)

| `track_type` | 用途 | output_target |
|------------|------|---------------|
| `autonomous` | プロジェクト遂行、記憶整理、創作等の自律行動 | `none` (基本独白) |
| `waiting` | 外部応答待ち (スケジュール、Kitchen 完了等) | `none` |
| `external` | 外部 SAIVerse / Discord 等への通信 | `external:<channel>:<address>` |

### 「対ペルソナ会話 Track」を持たない理由

- ペルソナ B との関係性は **対 B Person Note** で記録
- B との会話の場の文脈は **交流 Track** が担う (場所・時間軸での文脈)
- 同ビルディングに居れば交流 Track の output_target=building:current で B に届く
- 別ビルディング・外部経由なら external 通信 Track を一時的に作る

これにより Track 数を抑えつつ、関係性と会話の場を分離して扱える。

---

## メタレイヤーのトラック管理ツール群

メタレイヤー (重量級モデル) が独白 + スペル (ツール呼び出し) で Track を管理する:

| ツール | 用途 | 状態遷移 |
|--------|------|---------|
| `track_create(title, type, intent, metadata?)` | 新規 Track 作成 | (new) → `unstarted` |
| `track_activate(track_id)` | アクティブ化 (既存 `running` があれば自動で `pending` に) | `unstarted`/`pending`/`waiting` → `running` |
| `track_pause(track_id?)` | 後回し (省略時は現 `running`) | `running` → `pending` |
| `track_wait(track_id, waiting_for, timeout?)` | 応答待ち | `running` → `waiting` |
| `track_resume_from_wait(track_id, mode)` | 待機取り下げ。mode = `"activate"`/`"pause"`/`"abort"` | `waiting` → `running`/`pending`/`aborted` |
| `track_complete(track_id?)` | 完了 | `running` → `completed` |
| `track_abort(track_id)` | 中止 | (任意) → `aborted` |
| `track_forget(track_id)` | 忘却フラグ ON | + `is_forgotten=TRUE` |
| `track_recall(track_id)` | 忘却フラグ OFF | + `is_forgotten=FALSE` |
| `track_list(states?, include_forgotten=False)` | 一覧取得 | - |

「実行中は 1 本」の保証: `track_activate` の実装上、既存 `running` があれば自動で `pending` に遷移させる。これによりレース条件なく不変条件 1 が守られる。

---

## Note 系ツール群

| ツール | 用途 |
|--------|------|
| `note_search(query, type?)` | Note 一覧から検索 |
| `note_open(note_id)` | アクティブ Track に Note を追加 (`track_open_notes` 行追加) |
| `note_close(note_id)` | アクティブ Track から Note を外す |
| `note_create(title, type, description?, metadata?)` | 新規 Note 作成 |
| `note_write(note_id, content)` | Note への書き込み (Memopedia ページの作成・更新を含む) |
| `note_read(note_id)` | Note の内容を読み取る (明示的取得) |

### 自動メンバーシップ生成

audience を持つメッセージは、自動的に対応する Person Note の `note_messages` に追加される (`auto_added=TRUE`)。

- A の発言 → audience: [B, C]
- 自動メンバーシップ: A の「対 B Note」「対 C Note」両方に追加される
- 該当 Note が存在しなければ Person Note を自動作成

非 audience なメッセージ (独白、自律行動の内的思考等) は明示的にメタレイヤーまたはペルソナがメンバーシップを付与する。

### メンバーシップ付与のタイミング

Metabolism 時に後付けで決まる。理由:

- すぐ使うならコンテキストにメッセージが残っている (仕組み不要)
- Metabolism で押し出される時に「このメッセージはどの Note に属するか」を判定
- Chronicle 生成、Memopedia 抽出と同じタイミングで一括処理できる

---

## 環境変数

| 変数名 | 用途 | デフォルト |
|--------|------|-----------|
| `SAIVERSE_TRACK_PAUSE_SUMMARY_THRESHOLD` | 中断時サマリ作成の最小メッセージ数 | 7 |
| `SAIVERSE_TRACK_RESUME_TAIL_MESSAGES` | 再開時に末尾から取得するメッセージ数 | 6 |
| `SAIVERSE_META_LAYER_INTERVAL_SECONDS` | メタレイヤー定期実行のインターバル (秒) | 3000 (50 分) |
| `SAIVERSE_TRACK_MAX_DORMANT_COUNT` | dormant トラックの最大数 | 暫定値は実運用で確定 |
| `SAIVERSE_TRACK_FORGET_AFTER_DAYS` | dormant → forgotten への自動遷移日数 | 暫定値は実運用で確定 |
| `SAIVERSE_TRACK_AUTO_PAUSE_HINT_TURNS` | 多者会話のループ防止ヒント発話数 | 20 |
| `SAIVERSE_HANDLER_TICK_INTERVAL_SECONDS` | Handler tick (Track パラメータ更新 + 内部 alert 判定) の周期 (Phase 5) | 60 |
| `SAIVERSE_SUBLINE_SCHEDULER_INTERVAL_SECONDS` | SubLineScheduler のポーリング周期 | 5 |

---

## 守るべき不変条件 (B 固有)

[01_concepts.md の不変条件 1〜12](01_concepts.md#守るべき不変条件) を継承した上で、データモデル固有:

### B1. Track ID は永続的

一度発行された track_id は、ペルソナのライフタイム中ずっと同じ ID として扱われる。状態が forgotten になっても closed になっても、ID は再利用しない。

### B2. pause_summary の上書きは中断時のみ

アクティブ中に pause_summary を勝手に書き換えない。中断時にのみ作成・上書きする。これにより「再開時に取得する pause_summary」の内容が予測可能になる。

### B3. 再開コンテキストは軽量モデル側にのみ挿入

重量級モデル側 (メタレイヤー判断履歴) には再開コンテキストを挿入しない。重量級は判断履歴を連続的に積む。

### B4. forgotten Track は無条件に削除しない

ストレージが圧迫されても、forgotten Track は保持する。完全削除は明示的な操作 (管理者操作 or ペルソナの「忘れる」操作) でのみ行う。

---

## 関連ドキュメント

- [01_concepts.md](01_concepts.md) — 用語と不変条件
- [02_mechanics.md](02_mechanics.md) — 動的な仕組み
- [04_handlers.md](04_handlers.md) — Handler パターン
- [phases/phase_1_base.md](phases/phase_1_base.md) — マイグレーション 1 の詳細
- [phases/phase_2_track_metalayer.md](phases/phase_2_track_metalayer.md) — マイグレーション 2 の詳細
