# Intent: 行動の線の永続化・切り替え・記憶復元機構

**ステータス**: ドラフト v0.11（7層ストレージモデルのテーブル化、メタ判断ログ独立領域、Track ローカルログ、SAIMemory メタデータ拡張、report_to_parent 改名、Spell loop / action 文の保存方針、handoff 3 経路問題の修正）
**作成**: 2026-04-25
**改訂**: 2026-04-25 v0.1 → v0.2 → v0.3 → v0.4 → v0.5 → v0.6 → 2026-04-28 v0.7 → v0.8 → v0.9 → v0.10 → 2026-04-29 v0.11
**前提**: `persona_cognitive_model.md` v0.14（ライン 3 軸独立 + 7 層ストレージモデル + メタ判断分岐フロー）

## 重要な前提認識

### v0.2 で確認: 既存資産が想像以上に揃っている

実装コンテキスト調査の結果、**Intent A で「取り壊し」「乗っ取られる」と表現した機構の多くは、実は既に新モデルの基盤として活用可能な形で存在している**ことが判明した:

1. **AutonomyManager** は Decision/Execution の分離を既に持つ（872 行）。メタレイヤーの内部構造として継承可能で、「取り壊し」ではなく「**責務再配置**」が正確
2. **PulseController** の token-based cancellation は既に lock-free execution をサポート
3. **SAIMemoryAdapter** が既に `start_stelis_thread` / `end_stelis_thread` / `set_active_thread` 等を持つ
4. **`thread_switch` の `other_thread_messages` メタデータ + range_before/range_after** が再開時コンテキスト構築のひな形
5. **`get_messages_with_persona_in_audience`** がペルソナ再会機能の core 検索パス

### v0.3 で重大な構造変更: Track と thread の分離、Note 概念の導入

3 人会話で「対 A」「対 B」両方の Track に同じメッセージを書き込む必要があるという問題から、v0.2 で確定した「track_id = thread_id」を撤回。Intent A v0.6 で **Note 概念**が導入され、構造が以下のように再整理された:

| 概念 | 役割 |
|------|------|
| **SAIMemory thread** | メッセージの物理的保管庫（既存のまま、変更なし） |
| **行動 Track** | 行動制御の単位、独立した ID で管理 |
| **Note** | 関心の固まり、Memopedia ページ + メッセージ群を束ねる |

→ Intent B の実装は **既存資産の責務再配置 + Track / Note の永続化機構の追加**が中心になる。

## これは何か

`persona_cognitive_model.md` で定義された「行動の線（トラック）」と「メタレイヤー」を実装するための、永続化スキーマ・状態遷移・切り替えフロー・記憶復元の具体的な機構を設計する。

Intent A が「ペルソナの認知モデルとは何か」を扱うのに対し、Intent B は「それを SAIVerse の実装としてどう成立させるか」を扱う。

## これは何でないか

- Intent A の概念定義の繰り返しではない（用語と原則は A を参照）
- 応答待ちに特化した設計ではない（それは Intent C）
- ペルソナ再会機能の特化的設計ではない（汎用機構の特殊例として吸収）

## 設計のスコープ

本ドキュメントで決める範囲：

1. **データモデル**: トラックを永続化するテーブルスキーマ、関連する既存テーブルの拡張
2. **状態遷移**: active / dormant / waiting / forgotten / closed の遷移ルール
3. **トラック作成・中断・再開・忘却・クローズ**: 各操作の具体フロー
4. **中断時サマリ作成**: 閾値・フォーマット・軽量モデル呼び出し
5. **再開時のコンテキスト構築**: メタレイヤーが行う具体的な手順
6. **メタレイヤーの実装構造**: ランタイム常駐の形、起動タイミング、response_schema
7. **AutonomyManager の半ば取り壊しスコープ**: 何を残し何を移管するか
8. **PulseController との連携**: cancellation_token、優先度割り込み
9. **既存資産との共存・移行**: SAIMemory thread / Stelis / ペルソナ再会機能
10. **忘却ルール**: 環境変数化と暫定デフォルト値

本ドキュメントで決めない範囲（後続 / 別 Intent Doc）：

- 応答待ちトラックの詳細（Intent C）
- 内的独白の発話制御の詳細（Intent A 確定済み、実装はノード設計で対応）
- Memopedia / Chronicle 側の整合性調整（既存 `unified_memory_architecture.md` の範囲）

## データモデル

### 7 層ストレージのテーブル対応 （v0.11 で新規）

Intent A v0.14 の **7 層ストレージモデル** が、本ドキュメントのテーブル設計でどう実現されるかの俯瞰:

| 層 | テーブル / 保存先 | 備考 |
|---|---|---|
| [1] メタ判断ログ領域 | `meta_judgment_log` (新設) | ペルソナ単位、メタ判断の全履歴。次のメタ判断時に参考情報として動的注入 |
| [2] メインキャッシュ | `messages` (line_role='main_line', scope='committed') + per-model anchor | Track 横断 1 本、commit/discard 機構あり (メタ判断分岐は scope='discardable' で生成、続行時は破棄、移動時は committed に昇格) |
| [3] Track 内サブキャッシュ群 | `messages` (line_role='sub_line', track_id=X, line_id=Y) + per-model anchor | Track + 起点ライン単位、複数起点サブが並走しうる |
| [4] 入れ子ライン一時コンテキスト | ランタイム揮発 (PulseContext 内に保持、DB には保存しない) | 完了時に親へ `report_to_parent`、中間履歴は基本破棄 |
| [5] Track ローカルログ | `track_local_logs` (新設) | Track 内のイベント・モニタログ・起点サブの中間ステップトレース |
| [6] SAIMemory (会話の核 / Note) | 既存 `messages` (scope='committed') + `notes` 系 | 想起対象の会話メッセージ、Memopedia/Chronicle の抽出元 |
| [7] アーカイブ | 別 SQLite DB or ファイル (現状未整理、Phase 後送り) | worker 結果等、想起対象外 |

`messages` テーブルは既存を拡張する形で **複数の層 ([2]/[3]/[6])** を担う。区別はメタデータカラム `line_role` / `scope` / `track_id` / `line_id` で行う。詳細は後段「既存テーブルの拡張」を参照。

`meta_judgment_log` と `track_local_logs` は本 v0.11 で新設するテーブル。詳細は対応セクション参照。

### `action_tracks` テーブル（行動 Track）

行動制御単位の永続化テーブル。Track ID は **独立した UUID** とする（v0.3 で thread_id 流用案を撤回）。

```sql
CREATE TABLE action_tracks (
    track_id TEXT PRIMARY KEY,                  -- UUID（独立した ID）
    persona_id TEXT NOT NULL,
    title TEXT,                                 -- "対 mahomu 会話", "交流", "記憶整理", "Kitchen LoRA 完成待ち"
    track_type TEXT NOT NULL,                   -- user_conversation / social / autonomous / waiting / scheduled / external / ...
    is_persistent BOOLEAN NOT NULL DEFAULT FALSE, -- 永続 Track フラグ（v0.6 新設）
    output_target TEXT NOT NULL DEFAULT 'none', -- 'none' / 'building:current' / 'external:<channel>:<address>'
    status TEXT NOT NULL DEFAULT 'unstarted',   -- running / alert / pending / waiting / unstarted / completed / aborted
    is_forgotten BOOLEAN NOT NULL DEFAULT FALSE, -- 直交フラグ（他状態と両立）
    intent TEXT,                                -- 進行中の意図（自然言語）
    metadata TEXT,                              -- JSON: 相手識別情報（user_id, persona_id 等）、外部参照等
    pause_summary TEXT,                         -- 中断時に作成されたサマリ（最新）
    pause_summary_updated_at TIMESTAMP,
    last_active_at TIMESTAMP,
    waiting_for TEXT,                           -- waiting 状態の待ち相手（JSON: 外部チャネル ID、相手 persona_id 等）
    waiting_timeout_at TIMESTAMP,               -- waiting のタイムアウト時刻（NULL = 無期限）
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,                     -- completed への遷移時刻（永続 Track では NULL のまま）
    aborted_at TIMESTAMP                        -- aborted への遷移時刻（永続 Track では NULL のまま）
);
CREATE INDEX idx_action_tracks_persona ON action_tracks(persona_id, status, is_forgotten);
CREATE INDEX idx_action_tracks_last_active ON action_tracks(persona_id, last_active_at DESC);
CREATE INDEX idx_action_tracks_waiting_timeout ON action_tracks(waiting_timeout_at) WHERE status='waiting';
CREATE INDEX idx_action_tracks_persistent ON action_tracks(persona_id, is_persistent, track_type);
```

**v0.6 で追加されたカラム**:
- `is_persistent`: 永続 Track かどうか。`true` の場合 `completed`/`aborted` への遷移を禁止
- `output_target`: 発話の到達範囲。`none`（独白）/ `building:current`（現在地）/ `external:...`（外部チャネル）

**`AI` テーブル（既存）への追加カラム**:

```sql
ALTER TABLE AI ADD COLUMN ACTIVITY_STATE TEXT NOT NULL DEFAULT 'Idle';
-- 'Stop' / 'Sleep' / 'Idle' / 'Active'
ALTER TABLE AI ADD COLUMN SLEEP_ON_CACHE_EXPIRE BOOLEAN NOT NULL DEFAULT TRUE;
-- Idle でキャッシュ切れたら自動 Sleep
```

既存の `INTERACTION_MODE` (auto/user/sleep) は `ACTIVITY_STATE` (Stop/Sleep/Idle/Active) に置き換える（マイグレーション必要、後述）。

### `notes` テーブル（Note、新設 v0.3）

関心の固まりを表す。Memopedia ページとメッセージ群を束ねる、ペルソナの恒久的な資産。

```sql
CREATE TABLE notes (
    note_id TEXT PRIMARY KEY,                   -- UUID
    persona_id TEXT NOT NULL,
    title TEXT NOT NULL,                        -- "対エイド", "Project N.E.K.O.", "絵描き"
    note_type TEXT NOT NULL,                    -- person / project / vocation （3 種のみ、Intent A v0.6 確定）
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
- `vocation` Note は 1 ペルソナにつき少数（1〜数個）に保つ。乱発するとノウハウが分散する
- `person` Note は最初の会話で自動作成
- `project` Note はペルソナまたはユーザーが明示的に作成

### `note_pages` テーブル（Note ↔ Memopedia ページ、多対多）

```sql
CREATE TABLE note_pages (
    note_id TEXT NOT NULL,
    page_id TEXT NOT NULL,
    PRIMARY KEY (note_id, page_id)
);
CREATE INDEX idx_note_pages_page ON note_pages(page_id);
```

### `note_messages` テーブル（Note ↔ メッセージ、多対多）

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

### `track_open_notes` テーブル（行動 Track ↔ 開いている Note、多対多）

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

### `meta_judgment_log` テーブル（メタ判断ログ領域、新設 v0.11）

7 層ストレージの **[1] メタ判断ログ領域** の実体。メタ判断の全履歴を保存する。Track のメインキャッシュからは分離された独立領域で、次のメタ判断時に参考情報として動的注入される。

```sql
CREATE TABLE meta_judgment_log (
    judgment_id TEXT PRIMARY KEY,                -- UUID
    persona_id TEXT NOT NULL,
    judged_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    track_at_judgment_id TEXT,                   -- 判断時点のアクティブ Track (NULL の場合は idle 状態)
    trigger_type TEXT NOT NULL,                  -- 'periodic_tick' / 'alert' / 'pulse_completion' / ...
    trigger_context TEXT,                        -- JSON: alert track_id, alert reason, etc
    prompt_snapshot TEXT,                        -- 動的注入された参考情報含む、判断時のプロンプト要約 (デバッグ用)
    judgment_action TEXT NOT NULL,               -- 'continue' / 'switch' / 'wait' / 'close'
    judgment_thought TEXT,                       -- 判断に至った思考 (response_schema.thought)
    switch_to_track_id TEXT,                     -- switch の場合の移動先
    new_track_spec TEXT,                         -- JSON: 新規 Track 作成スペック (action='switch' で新規の場合)
    notify_to_track TEXT,                        -- continue の場合の通知内容 (応答 schema の notify_to_track)
    raw_response TEXT,                           -- LLM の生レスポンス (デバッグ用)
    committed_to_main_cache BOOLEAN NOT NULL DEFAULT FALSE  -- このターンがメインキャッシュにも commit されたか (= switch 時 true)
);
CREATE INDEX idx_meta_judgment_persona ON meta_judgment_log(persona_id, judged_at DESC);
CREATE INDEX idx_meta_judgment_track ON meta_judgment_log(track_at_judgment_id);
```

**設計ポイント**:

- `judged_at` で時系列降順に並べて、参考情報注入時に新しい順から取得
- `committed_to_main_cache=TRUE` のレコードは Track 移動の来歴として既にメインキャッシュに乗っている (= 重複注入を避けるための識別子)
- `prompt_snapshot` は判断時のプロンプト全文を要約保存。後追いで「なぜそう判断したか」を追える
- 古い判断は Metabolism 的に要約していく機構が将来必要 (本 v0.11 では生データを保持、最新 N 件を参考情報として注入する単純運用)

### `track_local_logs` テーブル（Track ローカルログ、新設 v0.11）

7 層ストレージの **[5] Track ローカルログ** の実体。Track 内のイベント・モニタログ・起点サブラインの中間ステップトレース等を保管する。Track 内では参照できるが、想起対象 ([6] SAIMemory) には乗らない。

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

**設計ポイント**:

- `log_kind` でイベント種別を区別。代表的な値:
  - `event_message`: 入退室・Chronicle 完了通知・Track 削除通知 等
  - `monitor_signal`: モニタリングラインの検知イベント
  - `sub_step`: 起点サブラインの中間ステップ (ツール呼び出し・思考等)
  - `tool_trace`: ツール実行トレース
- `visible_to_other_tracks` は将来の Track 越境参照のための予約フィールド (例: ユーザー会話中に「さっきエイドが入室したよね」と話題化する経路)。本 v0.11 では FALSE 固定、運用機構は後送り
- メタ用の特殊 Track (将来導入想定) のローカルログには「Track 削除通知」等のメタレイヤー固有イベントが入る

### 既存テーブルの拡張 （v0.11 で大幅追加）

#### `messages` (SAIMemory) のメタデータ拡張

```sql
ALTER TABLE messages ADD COLUMN origin_track_id TEXT;          -- v0.3 既設想 (生成時のアクティブ Track)
ALTER TABLE messages ADD COLUMN line_role TEXT;                -- v0.11 新規: 'main_line' / 'sub_line' / 'meta_judgment' / 'nested'
ALTER TABLE messages ADD COLUMN line_id TEXT;                  -- v0.11 新規: 起点ライン識別子 (Track + 起点ラインの組み合わせ識別)
ALTER TABLE messages ADD COLUMN scope TEXT NOT NULL DEFAULT 'committed';  -- v0.11 新規: 'committed' / 'discardable' / 'volatile'
ALTER TABLE messages ADD COLUMN paired_action_text TEXT;       -- v0.11 新規: action 文 + 応答ペア保存 (応答メッセージに紐付ける)
CREATE INDEX idx_messages_track_line ON messages(origin_track_id, line_role, line_id);
CREATE INDEX idx_messages_scope ON messages(scope);
```

各カラムの用途:

| カラム | 値 | 用途 |
|---|---|---|
| `line_role` | `main_line` | メインライン応答 ([2]) |
|  | `sub_line` | サブライン応答 ([3]) |
|  | `meta_judgment` | メタ判断ターン (続行時は scope='discardable')、移動時は scope='committed' に昇格 |
|  | `nested` | 入れ子子ラインの中間 (基本は [4] ランタイム揮発で DB 不要、デバッグ目的の保存時のみ使用) |
| `line_id` | 起点ライン識別子 (UUID) | 1 Track 内で複数起点サブが並走する場合の識別 |
| `scope` | `committed` | 通常メッセージ、コンテキスト構築時に取得対象 |
|  | `discardable` | メタ判断分岐ターン等、続行時は次プロンプトに含めない (取得対象外) |
|  | `volatile` | 一時保存 (Pulse 内のみ)、Pulse 完了で削除 |
| `paired_action_text` | LLM ノードの action 文 | 応答メッセージに紐付けて保存 (= action 文を user メッセージとして単独保存しない) |

#### `pulse_logs` の役割縮退 （v0.11 で方針更新）

- `track_id TEXT` カラム追加（どのトラックの pulse か識別、index 付き）— v0.10 のまま
- **役割は実行トレース専用に縮退** (Intent A v0.14 ステータスの 7 層モデル方針に基づく)
- v0.3.0 Phase 1 で実装した「内的独白の pulse_logs 保管」「Important フラグでの想起候補化」は、v0.11 で **`messages` テーブル ([3] サブキャッシュ または [6] SAIMemory) に移管**
- pulse_logs は「どの Pulse でどのノード・ツールが動いたか」のトレース情報のみを残す

#### `AI` (DB) への追加カラム

```sql
ALTER TABLE AI ADD COLUMN current_active_track_id TEXT;   -- v0.10 既定、実装利便性のため
ALTER TABLE AI ADD COLUMN ACTIVITY_STATE TEXT NOT NULL DEFAULT 'Idle';   -- v0.6 既定
ALTER TABLE AI ADD COLUMN SLEEP_ON_CACHE_EXPIRE BOOLEAN NOT NULL DEFAULT TRUE;  -- v0.6 既定
```

#### マイグレーション順序

新カラム追加は段階的に行う (`database/migrate.py` で個別マイグレーション):

1. `meta_judgment_log` / `track_local_logs` テーブル新規作成
2. `messages` への line_role / line_id / scope / paired_action_text カラム追加 (デフォルト値で既存行は埋まる: line_role=NULL は旧形式、scope='committed' で互換)
3. 既存の旧形式メッセージ (line_role IS NULL) は段階的に new line_role に分類するスクリプトを別途用意 (Phase 移行期間のため)

### SAIMemory thread との関係（v0.3 で再整理）

**Track と thread は別概念**として運用する（v0.2 の thread_id 流用案は撤回）:

- メッセージは引き続き thread に物理保存される（既存通り）
- Track はメッセージ集合を直接持たず、Note を介してメッセージとつながる
- 同じメッセージが「対 A Note」「対 B Note」両方に属することができ、3 人会話問題が解消される
- `thread_switch` ツールは既存のままだが、Track 切り替えの主機構ではなくなる（メタレイヤーが Track 切り替えを行う）

## 状態遷移（v0.6 で alert 追加）

7 つの状態（`running` / `alert` / `pending` / `waiting` / `unstarted` / `completed` / `aborted`）+ 直交する `is_forgotten` フラグ。**実行中は同時に 1 本のみ**（不変条件）。

### `alert` 状態の遷移トリガー

`alert` への遷移は SAIVerse 側で自動的に発生する:

| トリガー | 対象 Track |
|---------|----------|
| ユーザー発言到着 | 対ユーザー会話 Track（該当ユーザー）|
| 別ペルソナからの自分宛発言 | 交流 Track |
| Kitchen 完了通知（重要度高い場合） | 関連 Track |
| MCP Elicitation 応答到達 | 関連 waiting Track |
| 外部チャネルからの直接通信 | 該当 Track |

`alert` から `running` への遷移はメタレイヤーの判断:
- 即座に対応 → `track_activate`（既存 running は `pending` に）
- 後で対応 → メタレイヤーが pending と同じく扱う（ただし優先度は高めで判断）

### `alert` の優先度

メタレイヤーの判断時、`alert` 状態の Track が優先的に判断される。複数の `alert` がある場合は新しいものから（v0.4 で確定）。

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

   abort は任意状態から ────► aborted (terminal)
   complete は running から ► completed (terminal)

   is_forgotten フラグは任意状態と両立、forget/recall ツールで切り替え
```

主要遷移トリガー：

| 遷移 | トリガー |
|------|---------|
| (new) → `unstarted` | `track_create` |
| `unstarted`/`pending`/`waiting` → `running` | `track_activate` （既存 `running` があれば自動で `pending` に） |
| `running` → `pending` | `track_pause` または別 Track の `track_activate` で押し出される |
| `running` → `waiting` | `track_wait(track_id, waiting_for, timeout)` |
| `waiting` → `running` / `pending` / `aborted` | `track_resume_from_wait(track_id, mode)` <br> mode=activate/pause/abort |
| `running` → `completed` | `track_complete` |
| 任意 → `aborted` | `track_abort` |
| `is_forgotten` ON/OFF | `track_forget` / `track_recall` |
| `waiting` の応答到達 / タイムアウト | SAIVerse 側で検知 → `inject_persona_event` でメタレイヤー通知 → メタレイヤー判断 → `track_resume_from_wait` 等 |

## メインサイクル（v0.5 で新設）

ペルソナのプログラム的な動作モデル。**ライン（メインライン / サブライン / モニタリングライン）が並列に動き、Track はその上で生まれ・遷移する**。

### Track の作成パターン

| パターン | きっかけ | `track_create` を呼ぶ主体 |
|---------|---------|------------------------|
| ユーザー入力到着 | UI からメッセージ送信 | SAIVerseManager 自動（既存 Person Note があれば既存 Track 再アクティブ化） |
| ペルソナの入退室 | occupancy event | 自動（既存 Person Note 自動開封 + Track 再アクティブ化） |
| Kitchen 完了通知 | cooking ステータス変化 | SAIVerseManager 自動（waiting 状態 Track の状態変化） |
| スケジュール時刻到来 | ScheduleManager | 自動 |
| モニタリングラインからの検知 | カメラ・タイムライン等の変化検知 | SAIVerseManager 自動（イベント経由でメインライン判断） |
| 自律行動開始 | メインラインの判断 | メインライン（重量級モデル）が `track_create` |
| 複雑タスクの分割 | メインラインの判断 | 同上 |
| ペルソナ自身の意思 | 内的独白 + ツール | ペルソナが `track_create`（メインラインまたはサブライン経由） |

### Track 内の動きパターン（種別別）

#### Pattern 1: 他者との会話 Track

```
[相手の発言到着]
  ↓
[メインライン（重量級）] 応答生成（不変条件 9: 他者会話は重量級）
  ↓ 必要に応じて
[サブライン（軽量）] ツール呼び出し（情報取得等） → 結果をメインラインへ戻す
  ↓
[メインライン] 最終応答テキストを書く
  ↓
[相手に発言]
  ↓
[相手の応答待ち] → waiting 状態
```

Playbook はほとんど使わない。基本はメインラインの直接応答。

#### Pattern 2: タスク遂行 Track（自律、Project 実行等）

```
[メインライン] 現在状況の判断 → Playbook 選択
  ↓
[サブライン] Playbook 実行（ステップ、ツール呼び出し）
  ↓
[サブライン] 作業完了時にサマリ生成
  ↓
[メインライン ← サブライン] サマリ + 末尾コンテキストを受け取る → 検収
  ↓
[メインライン] 次の Playbook 選択 or Track 切り替え判断（メタレイヤー判断）
  ↓ ループ
```

これは v0.3.0 Phase 3 v3 のループそのもの。

#### Pattern 3: 待機 Track

```
[waiting 状態に入った時点で稼働しない]
  ↓
[外部イベント到着] (応答 / タイムアウト / 撤回)
  ↓ inject_persona_event
[メインラインの判断起動] → track_resume_from_wait 等で復帰判断
```

待機中の Track 自体は動かない。SAIVerse 側がポーリング → イベント通知。

### メインラインのメインサイクル

```
[起動トリガー]
  - 定期（実時間、暫定 1 時間以内、重量級キャッシュ TTL に合わせる）
  - Pulse 完了で次が決まってない（サブライン側からのシグナル）
  - 外部イベント駆動（waiting 解除、ユーザー入力、Kitchen 通知、モニタリングライン検知等）
  ↓
[プロンプト構築]
  - 現 running Track の概要 + 末尾メッセージ
  - pending / waiting / unstarted の Track 一覧
  - 開いている Note のリスト
  - 直近の外部イベント
  ↓
[重量級モデル] 独白 + 判断
  ↓
[ツール呼び出し]
  - track_* （状態管理）
  - Playbook 選択（Pattern 2 のループ起動）
  - 他者への直接応答（Pattern 1）
  ↓
[アクティブ Track 確定 → そのサイクルへ]
```

### サブラインのメインサイクル

```
[アクティブ Track が確定]
  ↓
[Playbook 実行 or 自由連続]
  - Playbook ノード実行（既存 SEA runtime そのまま）
  - ツール呼び出し
  - 内的独白（モデル制限なし、軽量モデルでも自由）
  ↓
[完了 / 一段落]
  ↓
[サマリ生成]
  ↓
[メインラインへバトン] → 検収＋次の判断へ
```

### モニタリングラインのメインサイクル（将来拡張）

```
[起動: SAIVerseManager がライン起動]
  ↓
[定期ポーリング（数秒〜数分）]
  ↓
[軽量・マルチモーダルモデルで判定]
  ↓
[重要な変化を検知]
  ↓
[inject_persona_event でメインラインに通知]
```

モニタリングラインは Track ではないので状態遷移を持たない。常時稼働 or 明示停止のどちらか。

### メインライン と Track の関係 (v0.5、v0.11 で v0.14 整合に更新)

**メインラインと「Track 内の重量級判断」は役割の名前が違うだけで、同じモデル・同じキャッシュ・同じ思考の流れ** (v0.5 で確定、v0.11 で再確認)。

- メインライン側のキャッシュ ([2]) = **他者会話 + Playbook 選択 + Track 内検収 + Track 移動の来歴** が混ざって連続（不変条件 7: 重量級は Track 横断混合）
- **メタ判断ターン** は Track 続行時はメインキャッシュには載らない、移動時のみ来歴として乗る (Intent A v0.14 の A/B フロー再定義)
- メタ判断履歴自体は **[1] メタ判断ログ領域** に独立保存される (次のメタ判断時に参考情報として動的注入)
- サブライン側のキャッシュ ([3]) = アクティブ Track の Playbook 実行コンテキスト、**Track + 起点ライン単位**
- Track が切り替わるとサブライン側のキャッシュは切り替わる、メインライン側は連続性維持

これにより Intent A 不変条件 11「ペルソナはメタの判断履歴を自分の思考として認識する」が**技術的にもそうなる**: メインライン上では他者会話・Playbook 選択・Track 移動の来歴が連続的に積まれ、メタ判断ログ領域からの動的注入で過去判断も参照される。

## Track 種別（v0.6 で整理）

Track の種別と性質を一覧で整理:

### 永続 Track（`is_persistent=true`、`completed`/`aborted` 不可）

| `track_type` | 用途 | output_target | 数 |
|------------|------|---------------|---|
| `user_conversation` | ユーザーとの 1 対 1 関係（永続的な核） | `building:current` または `external:...` | ユーザーごとに 1 個 |
| `social` | 他ペルソナとの会話を扱う**交流 Track** | `building:current` | ペルソナにつき 1 個 |

**ユーザーがペルソナと初めて関わるタイミング**で対応する `user_conversation` Track が自動作成される。**ペルソナ作成時**に `social` Track が自動作成される。

### 一時 Track（`is_persistent=false`、完了/中止可能）

| `track_type` | 用途 | output_target |
|------------|------|---------------|
| `autonomous` | プロジェクト遂行、記憶整理、創作等の自律行動 | `none`（基本独白）|
| `waiting` | 外部応答待ち（スケジュール、Kitchen 完了等）| `none` |
| `external` | 外部 SAIVerse / Discord 等への通信 | `external:<channel>:<address>` |

### 「対ペルソナ会話 Track」を持たない理由

- ペルソナ B との関係性は **対 B Person Note** で記録
- B との会話の場の文脈は **交流 Track** が担う（場所・時間軸での文脈）
- 同ビルディングに居れば交流 Track の output_target=building:current で B に届く
- 別ビルディング・外部経由なら external 通信 Track を一時的に作る

これにより Track 数を抑えつつ、関係性と会話の場を分離して扱える。

### output_target が `building:current` の解決

`building:current` は動的に解決される。実行時に persona の `current_building_id` を参照して、該当 Building に発話を配信する。

ペルソナが Building A から Building B に移動しても、Track の output_target は `building:current` のまま、配信先が B に変わる（**自室で作業中に他ペルソナが訪ねてきた**シナリオに自然対応）。

## トラックのライフサイクル

### 作成

トラック作成のトリガーは複数あるが、すべて同じインターフェース（仮: `create_track(persona_id, type, title, intent, metadata)`）に集約する。

主要な作成パターン：

1. **ユーザー入力**: ユーザーがメッセージを送ると、新規 conversation トラック or 既存ユーザートラックへの再アクティブ化
2. **自律判断**: メタレイヤーが「自律的に何かを始める」と判断（例: 記憶整理）
3. **外部イベント**: Kitchen 完了通知、X mention、スケジュール時刻到来等
4. **ペルソナ再会**: 既存実装の汎用化、対象ペルソナとのトラックを再アクティブ化（休止 or 忘却から）
5. **応答待ち**（Intent C で詳述）: メタレイヤーが「外部応答を待つ」と判断、waiting 状態のトラック作成

### 中断（active → dormant）

メタレイヤーの A/B 判断で B（切り替え）が選ばれた時の処理：

1. アクティブトラック X の履歴長を確認
2. **閾値以上** なら軽量モデルにサマリを作らせ `pause_summary` に保存（後述）
3. 閾値未満 ならサマリ作成なし（末尾メッセージのみで十分）
4. X の status を dormant に変更
5. `last_active_at` 更新

### 再開（dormant → active）

メタレイヤーの A/B 判断で B が選ばれた、再開先 Y への移行：

1. Y の `pause_summary` を取得（あれば）
2. Y の thread から末尾 N メッセージを取得（暫定 N=6、ペルソナ再会機能準拠）
3. 軽量モデルが再開コンテキストを整形（後述）
4. Y の status を active に変更
5. 以降のプロンプトは Y のものとして処理

### 忘却（dormant → forgotten）

判定タイミング：定期チェック（メタレイヤー内 or 別タスク）

- `last_active_at` が一定期間より古い → forgotten 候補
- ペルソナのアクティブトラック数（active + dormant）が上限超過 → 古い順から forgotten

具体的閾値は環境変数化（後述）。

### 復活（forgotten → dormant）

明示的な呼び戻し or 再会機構による：

- ペルソナが「あのトラックを思い出す」と判断 → トラック検索 → forgotten から dormant へ
- ペルソナ再会等の外部トリガーで該当トラックが特定された → 同様

### クローズ（→ closed）

ペルソナ自身が「このトラックは完了した」と判断、またはタイムアウト：

- 完了判定の方法は内的独白 + 専用ツール（仮: `close_track`）か、メタレイヤーの判断か（未決事項）

## 中断時サマリ作成

### 閾値

履歴長の閾値で「サマリを作るか作らないか」を切り替える：

- **暫定**: 7 メッセージ以上ならサマリ作成、6 メッセージ以下なら不要
- 環境変数: `SAIVERSE_TRACK_PAUSE_SUMMARY_THRESHOLD`（デフォルト 7）

### フォーマット

サマリは自然言語要約 + 構造化メタ情報の混合形態：

```
## このトラックの状況

[自然言語による要約 1〜3 段落]

### 進行中の意図
[このトラックで達成しようとしていること]

### 重要な決定事項・進捗
- ...
- ...

### 関係エンティティ
- 人物: ...
- アイテム: ...
- 参照中の Memopedia: ...
```

### 軽量モデル呼び出し

サマリ作成は軽量モデルが行う。プロンプト設計：

- 入力: トラックの全履歴（中断時点でコンテキストに乗っている分）
- 出力: 上記フォーマットに沿ったサマリ
- response_schema を定義して構造化出力にする方向

## 再開時のコンテキスト構築

メタレイヤーがトラック Y への切り替えを決めた時、軽量モデル側のコンテキストとして再開ビューを構築：

```
[システムプロンプト等の先頭部分（変更しない、キャッシュ温存）]

...

[再開コンテキスト挿入領域]

## トラック「Y」の再開

### 前回までのサマリ
{Y.pause_summary}

### 直前のやりとり（末尾 N メッセージ）
- [user] ...
- [assistant] ...
- ...

### 開いている Note の差分（中断時から変化があれば）
- Note「対エイド」: [追記された内容の要約 or 直近のメッセージ]
- Note「Project N.E.K.O.」: [追記された内容の要約]

### このトラックを今再開する。
```

特性：

- **挿入位置**: コンテキスト末尾（不変条件 7 のキャッシュ親和性に沿う）
- **構築者**: 軽量モデル（不変条件 8 の使い分け）
- **Note 差分の挿入**: Y を中断した時点の Note 状態と現在の Note 状態の差分を、event entry として整形して含める（v0.3 で追加）。Y を再開している間に他 Track が開いた Note に書き込んだ内容を取り込む経路となる
- **追加情報取得**: ペルソナが「サマリだけでは足りない」と判断した場合、明示的にツール呼び出しで取得（既存の memory_recall 等、加えて新規の `note_read` 等）

## メタレイヤーの実装構造

### 配置

ランタイム直接実装（Intent A v0.5 確定）。具体的には：

- `saiverse/meta_layer.py` 新設、または既存の `AutonomyManager` を拡張する形
- ペルソナ単位で1インスタンス（manager 経由でアクセス）
- `SAIVerseManager` から起動・停止される

### 起動タイミング

実時間ベース（Intent A v0.5 確定）：

- **タイマー**: ペルソナ単位で動くタイマー（Anthropic の場合 1 時間以内に再実行）
- **Pulse 完了**: アクティブトラックが応答完了 → メタレイヤー判断起動
- **外部イベント**: ユーザー入力、Kitchen 通知、X mention、占有変化等 → 即時起動

タイマー間隔は環境変数化（暫定 `SAIVERSE_META_LAYER_INTERVAL_SECONDS=3000`、約 50 分）。

### 判断 LLM の response_schema

```json
{
  "type": "object",
  "properties": {
    "thought": { "type": "string", "description": "判断に至った思考" },
    "action": {
      "type": "string",
      "enum": ["continue", "switch", "wait", "close"],
      "description": "アクティブトラックをどうするか"
    },
    "switch_to_track_id": {
      "type": "string",
      "description": "switch の場合、切り替え先トラックの ID（既存 dormant の id か、新規作成シグナル）"
    },
    "new_track_spec": {
      "type": "object",
      "description": "switch_to_track_id が新規作成の場合、その内容"
    },
    "notify_to_track": {
      "type": "string",
      "description": "continue の場合、現アクティブトラックに通知すべき内容（あれば）"
    },
    "close_reason": {
      "type": "string",
      "description": "close の場合、クローズ理由"
    }
  },
  "required": ["thought", "action"]
}
```

### AutonomyManager との統合（v0.2 改訂: 「拡張」へ寄せる）

既存の `AutonomyManager`（`saiverse/autonomy_manager.py` 872 行）は **Decision フェーズ + Execution フェーズの分離** を既に持つ。これは新メタレイヤーの内部構造としてそのまま継承できる。

統合方針は「取り壊し」ではなく「**責務再配置と拡張**」:

| 既存責務 | 新モデルでの位置づけ |
|---------|-------------------|
| `_run_decision` (line 407) — 重量級モデルで `meta_autonomy_decision` 実行 | **メタレイヤーの A/B 判断**として継承、response_schema 拡張 |
| `_run_execution` (line 574) — Activity type マッピング、Playbook 起動 | **アクティブトラックの実行制御**として継承、トラック種別ごとに Playbook を選ぶ |
| `pause_for_user` (line 224) / `resume_from_user` (line 249) | **単一線前提から複数線対応へ拡張**。線切り替え汎用フローに吸収 |
| Stelis スレッド管理 (`start` / `_cleanup_stelis`) | 下層 API は `SAIMemoryAdapter` にあるので、メタレイヤーから直接呼ぶ。AutonomyManager から呼ぶ必要は減る |
| Pulse コールバック登録 (`_register_pulse_callbacks` 275) | メタレイヤーが直接 PulseController と連携する形へ移行 |

具体的なクラス階層案（実装段階で確定）:

```
SAIVerseManager
  └── 各ペルソナごとに MetaLayer インスタンス
        ├── decision/execution ロジック（AutonomyManager 由来）
        ├── トラック管理（新規: action_tracks との連携）
        ├── サマリ作成・再開コンテキスト構築（新規）
        └── PulseController 接続
```

`AutonomyManager` クラス自体を `MetaLayer` にリネーム + 拡張する形がシンプルかもしれない（v0.2 時点では未決、実装段階で判断）。

## メタレイヤーのトラック管理ツール群（v0.4 で新設）

メタレイヤー（重量級モデル）が独白 + スペル（ツール呼び出し）で Track を管理する。実態は API を叩くツール群:

| ツール | 用途 | 状態遷移 |
|--------|------|---------|
| `track_create(title, type, intent, metadata?)` | 新規 Track 作成 | (new) → `unstarted` |
| `track_activate(track_id)` | アクティブ化（既存 `running` があれば自動で `pending` に） | `unstarted`/`pending`/`waiting` → `running` |
| `track_pause(track_id?)` | 後回し（省略時は現 `running`） | `running` → `pending` |
| `track_wait(track_id, waiting_for, timeout?)` | 応答待ち | `running` → `waiting` |
| `track_resume_from_wait(track_id, mode)` | 待機取り下げ。mode = `"activate"`/`"pause"`/`"abort"` | `waiting` → `running`/`pending`/`aborted` |
| `track_complete(track_id?)` | 完了 | `running` → `completed` |
| `track_abort(track_id)` | 中止 | (任意) → `aborted` |
| `track_forget(track_id)` | 忘却フラグ ON | + `is_forgotten=TRUE` |
| `track_recall(track_id)` | 忘却フラグ OFF | + `is_forgotten=FALSE` |
| `track_list(states?, include_forgotten=False)` | 一覧取得 | - |

### メタレイヤーの認識方法

メタレイヤーは現状を**プロンプトとして通知される**:

- 現 `running` Track の概要
- `pending` / `waiting` / `unstarted` の Track 一覧（タイトル、状態、要約）
- 直近のイベント（応答到達、タイムアウト、外部入力等）
- 開いている Note のリスト

これを受けて重量級モデルが**独白 + ツール呼び出し**で次の動きを決める。判断の流れ:

```
[メタレイヤーへのプロンプト]
  「現在 running: タスク X、pending: Y, Z、新着イベント: ユーザー入力到来」
  ↓
[重量級モデルの応答]
  独白: 「ユーザー入力が来たから対応する。X は一旦 pending に」
  ツール: track_pause("X") → track_create("ユーザー対応", ...) → track_activate(...)
```

### 「実行中は1本」の保証

`track_activate` の実装上、既存 `running` があれば自動で `pending` に遷移させる。これによりレース条件なく不変条件が守られる。

## PulseController との連携（v0.2 で具体化）

PulseController（`sea/pulse_controller.py` 497 行）の既存機構をそのまま活用する。

### 既存仕組みの活用

- **token-based interruption は lock-free execution をサポート済み**: `_should_interrupt:220` で優先度判定、Lock は状態更新中のみで実行中は解放される（`_execute_unlocked:271`）。別スレッドから cancellation signal が届く設計
- **on_blocked="wait" によるキュー復帰**: SCHEDULE / resumption の標準ポリシー（`_queue_for_resumption:247`）。queue 上限 10 件、超過時は最古削除
- **interrupt 記録**: `_record_interruption:364` で中断メッセージが SAIMemory に自動記録

### メタレイヤーの連携パターン

線切り替えは「**メタレイヤーから ExecutionRequest を投下**」で成立する:

1. メタレイヤーが B 判断（切り替え）→ 新 ExecutionRequest を `submit()` で投下
2. PulseController が優先度判定、必要なら現アクティブの `cancellation_token` を発火
3. 旧アクティブは `on_blocked="wait"` でキューに戻る → 旧トラックの dormant 化シグナル
4. 新 ExecutionRequest が実行開始 → 新アクティブトラック確定

**追加実装は最小限**:

- メタレイヤー側に「現在の優先度」を判定するロジック
- 「キューから戻ってきたリクエストをトラック単位でハンドルする」処理（既存の resumption 機構を流用）
- queue 上限 10 件は dormant トラック数上限と整合させる検討（環境変数で連動）

## Note の運用とツール（v0.3 で新設）

### ペルソナのインターフェース

| ツール | 用途 |
|--------|------|
| `note_search(query, type?)` | Note 一覧から検索 |
| `note_open(note_id)` | アクティブ Track に Note を追加（`track_open_notes` 行追加） |
| `note_close(note_id)` | アクティブ Track から Note を外す |
| `note_create(title, type, description?, metadata?)` | 新規 Note 作成 |
| `note_write(note_id, content)` | Note への書き込み（Memopedia ページの作成・更新を含む） |
| `note_read(note_id)` | Note の内容を読み取る（明示的取得） |

### 自動メンバーシップ生成

audience を持つメッセージは、自動的に対応する Person Note の `note_messages` に追加される（auto_added=TRUE）。

- A の発言 → audience: [B, C]
- 自動メンバーシップ: A の「対 B Note」「対 C Note」両方に追加される
- 該当 Note が存在しなければ Person Note を自動作成（最初の会話で作られる）

非 audience なメッセージ（独白、自律行動の内的思考等）は明示的にメタレイヤーまたはペルソナがメンバーシップを付与する。

### メンバーシップ付与のタイミング

Metabolism 時に後付けで決まる（v0.3 確定）。理由:

- すぐ使うならコンテキストにメッセージが残っている（仕組み不要）
- Metabolism で押し出される時に「このメッセージはどの Note に属するか」を判定
- Chronicle 生成、Memopedia 抽出と同じタイミングで一括処理できる

### Track 開始時の Note 選択

- 新規 Track 開始時、メタレイヤーが「どの Note を開いて始めるか」を決定
- ユーザー会話 Track: 対ユーザー Person Note + 関連 Vocation Note
- 自律 Track: 対象 Project Note + Vocation Note
- ペルソナ間会話 Track: 対 X Person Note
- ペルソナ自身が `note_open` で追加することも可能（途中で必要になった Note）

## 応答待ちトラックの仕組み（v0.4 で新設、Intent C 統合）

応答待ち（`waiting` 状態）の Track は、外部応答（ユーザー、他ペルソナ、Kitchen 完了通知、X リプライ等）を待っている状態。本セクションは応答待ちの汎用機構を定める（MCP Elicitation の前提資料も兼ねる）。

### 監視方法

**SAIVerse 側で自動ポーリング**し、変化があった時にメタレイヤーへイベント通知する形:

- ポーリングの責務: SAIVerseManager（または専用の WaitingMonitor）が `action_tracks` の `waiting` 状態の Track を定期的にチェック
- 通知経路: 既存の `inject_persona_event` を活用、`PersonaEventLog` 経由でメタレイヤーへ
- ペルソナ側にポーリングのコードは持たない（メタレイヤーは通知された時だけ動く）

検知対象:

- `waiting_for` で指定された外部応答が到達したか
- `waiting_timeout_at` が過ぎていないか
- `waiting_for` の対象が「もう発生しない」と判明したか（例: 相手ペルソナが close されたチャンネルに退室した）

### `waiting_for` フィールドの規約

JSON 構造化:

```json
{
  "type": "user_response" | "persona_response" | "kitchen_completion" | "external_event" | ...,
  "channel": "ui" | "discord" | "x" | "elyth" | ...,
  "target": "persona_id" | "user_id" | "cooking_id" | ...,
  "elicitation_request_id": "..."  // MCP Elicitation 時等
}
```

応答到達検知のロジックは `type` ごとに別実装する（拡張ポイント）。

### 多重応答待ちの優先順位

複数の `waiting` Track があり、複数応答が同時に到達した場合:

- **新しい Track 優先**（Intent A 確定）。理由: 細かいタスクから片付ける方がスムーズ
- 「新しい」の基準は `last_active_at` または Track 作成時刻（実装段階で確定）

ただし優先順位はメタレイヤーが最終判断する。SAIVerse 側は「応答が来た」イベントを通知するのみで、どの Track を `running` にするかはメタレイヤー次第。

### タイムアウト

`track_wait(track_id, waiting_for, timeout=...)` で設定可能。`timeout=None` は無期限。

タイムアウト到達時:

- 自動で `abort` や `pending` に遷移**しない**
- メタレイヤーへタイムアウトイベントを通知し、判断を仰ぐ
- メタレイヤーが `track_resume_from_wait(track_id, "abort")` 等を選択する

### 構造化応答（MCP Elicitation 等）

MCP Elicitation のような構造化応答（Approve/Deny/Modify 等）への対応は、Playbook のノード遷移先変更で実装する方向（暫定）。

実装の詳細は MCP Elicitation 実装時に詰めるが、骨子としては:

- Track 内で実行される Playbook が、応答待ちノードに到達したら `track_wait` を発行
- 応答到達時、応答の構造に応じてノード遷移先を選択
- ペルソナの応答ノードは waiting 解除後の Playbook 内ノードとして実装

### 想定される応用

| 応用 | `waiting_for.type` | 監視・検知 |
|------|-------------------|-----------|
| ユーザーへの返答待ち（通常会話） | `user_response` | UI からのメッセージ送信検知 |
| 他ペルソナへの応答待ち | `persona_response` | 相手ペルソナの発言検知 |
| MCP Elicitation | `mcp_elicitation` | MCP サーバーからの応答受信 |
| Kitchen 長時間処理完了 | `kitchen_completion` | Kitchen の cooking ステータス監視 |
| X / Mastodon リプライ待ち | `external_event` (channel=x) | 外部 API ポーリング |
| スケジュール時刻到来 | `scheduled_time` | 時刻監視 |

すべて同じ `waiting` 状態の Track として扱われ、メタレイヤーが統一的に管理する。

## 多者会話と audience（v0.6 で新設）

複数のペルソナ・ユーザーが同じ Building にいる時の会話処理。

### output_target と audience の役割分担

- **output_target**: 物理的な発話の到達範囲（メッセージング層）
- **audience**: 誰宛の発言か（意図層）

これらは独立。同じ output_target でも audience の違いでメインライン起動先が変わる。

### audience による自動振り分け

Building 内に居る全ペルソナ + ユーザーは output_target=`building:current` の発話を受信する。各受信者は audience に応じて反応する:

| audience に含まれるか | 動作 |
|------------------|------|
| 含まれる | 該当 Track が `alert` 状態に → メインライン起動候補 |
| 含まれない | 関連 Person Note に記録するが反応しない |

### 既存挙動との整合（v0.6 で確認）

まはー指示「今の挙動を維持」の解釈:
- ユーザーが「みんなどう思う？」と発言 → audience=[A, B, ...] → 全員の対ユーザー Track が alert
- A→B の順に発話順制御（既存の Building 発話キュー）
- A の発言（audience=[user]）は同 Building の B にも届くが、B の audience に入っていないので B のメインラインは起動しない（次のターンで応答候補に上がる）
- 次にユーザーが発言すれば、B の対ユーザー Track が再度 alert に

これは現状の挙動（A→B 順、相互の発言が見える）と整合する。

### 多者会話のループ防止

audience を厳格に解釈することで自然にループを防げる:
- A が B に質問（audience=[B]） → B のメインライン起動 → B が応答（audience=[A]） → A のメインライン起動 → A が応答（audience=[B]） → ...

この場合は正当な対話だが、**メタレイヤーが「会話を続けるか切り上げるか」を判断**して終わらせる。技術的なループストッパーとしては:

- メタレイヤーが Track の発話数をカウント
- 一定数（暫定 20）超過で `track_pause` を強く推奨（自動停止ではなく判断材料として）
- これは環境変数で調整可能: `SAIVERSE_TRACK_AUTO_PAUSE_HINT_TURNS`（暫定 20）

### 別 Building のペルソナへの呼びかけ

output_target=`building:current` では別 Building には届かない。SAIVerse 内の別 Building / 外部 SAIVerse のペルソナへ発話するには:

- 一時的な `external:saiverse:<persona_id>` 通信 Track を作る
- 既存の SAIVerse 間ペルソナ通信機構を活用（dispatch / visiting AI）

## 既存資産との共存・移行

### SAIMemory thread（v0.3 で整理し直し）

- thread はメッセージの物理保管庫として既存のまま動作
- Track と thread は別概念（v0.3 確定）
- メッセージは thread に保存され、Track / Note のメンバーシップは別テーブル経由で管理される
- `thread_switch` ツールは既存仕様のまま残る（手動でメッセージ保管庫を切り替えたい場面で使う）
- **`other_thread_messages` メタデータと range_before/range_after は、参照可能な仕組みとして残るが、Track 切り替えの主機構ではない**

### Note 系テーブル（v0.3 で新設）

- `notes`、`note_pages`、`note_messages`、`track_open_notes` の 4 テーブル
- 既存 Memopedia ページや messages との関係は多対多
- 既存実装には影響なし、純粋な追加

### INTERACTION_MODE → ACTIVITY_STATE 移行（v0.6 で新設）

既存の `INTERACTION_MODE` (auto/user/sleep) は `ACTIVITY_STATE` (Stop/Sleep/Idle/Active) に置き換える。

#### 対応関係

| 旧 INTERACTION_MODE | 新 ACTIVITY_STATE | 備考 |
|---------------------|-------------------|------|
| `auto` | `Active` | 自律行動含めて全動作 |
| `user` | `Idle` | 起きてるが自発的には行動しない |
| `sleep` | `Sleep` | 寝てる、ユーザー発言で起きる |
| (新規) | `Stop` | 機能停止、ユーザー操作のみで起きる |

#### マイグレーション

データベースマイグレーションで一括変換:
- `auto` → `Active`
- `user` → `Idle`
- `sleep` → `Sleep`
- 既存の API・UI で参照される `INTERACTION_MODE` は段階的に `ACTIVITY_STATE` へ切り替え

#### `SLEEP_ON_CACHE_EXPIRE` フラグ（v0.6 で新設）

```sql
ALTER TABLE AI ADD COLUMN SLEEP_ON_CACHE_EXPIRE BOOLEAN NOT NULL DEFAULT TRUE;
```

`Idle` 状態のペルソナで重量級モデルのキャッシュ TTL を超えたら自動的に `Sleep` に遷移するフラグ。

- `TRUE` (デフォルト): API 料金保護のため、長時間放置されたペルソナは Sleep に
- `FALSE`: 自動遷移なし、手動でのみ状態変更

実装はメタレイヤー側のキャッシュ管理ロジックと統合: `AI.METABOLISM_ANCHORS` の `updated_at` から経過時間を計算、TTL 超過で Sleep 化。

#### 状態の可視性

`ACTIVITY_STATE` は他ペルソナからも見える（既存 API で公開）:
- ペルソナ A が B に話しかけたい時、B の状態を確認
- `Stop` / `Sleep` なら届かない（または届いても起きない）
- `Idle` / `Active` なら届く

これによりペルソナ間の呼びかけ判断が現実的にできる。

### Stelis スレッド

別物として共存（Intent A v0.5 確定）：

- Stelis は今のまま動作する
- 新トラック機構が安定したら Stelis を新基盤に統合する検討を開始（v0.4.0 以降）
- 移行期間は両方の機構が並走、データの相互参照は最小限

### ペルソナ再会機能（v0.3 で Note 経由に再整理）

新基盤上に再実装（v0.3.0 内）。Note 概念導入によりさらに整理された:

| 既存実装 (`persona/history_manager.py`) | 新基盤での位置づけ |
|----------------------------------------|------------------|
| `recall_conversation_with:587` | **Note 自動開封 + Note 内容のコンテキスト挿入**として再実装 |
| `get_messages_with_persona_in_audience` (`adapter:1694`) | Note メッセージの取得経路として活用、または Note 自動メンバーシップ生成の入力 |
| Memopedia ページ取得 (`ensure_persona_page`) | Note の `note_pages` を介して取得 |
| `recalled_by` 冪等性マーカー | 同じ occupancy event で同じ Note が二重で開かれない冪等性に位置づけ直す |
| `[想起: ...との過去の会話]` info_text | **Person Note の差分挿入テンプレート**として汎用化 |

新基盤での再会フロー:

1. occupancy event 検出（既存通り）
2. 相手ペルソナの **Person Note** を検索（`notes.note_type='person', metadata.persona_id=対象ペルソナ`）
3. 既存 Person Note があればその内容を読み込み、Track の開封 Note リストに追加
4. 既存 Note がなければ新規作成（Person Note は最初の会話で自動作成される）
5. メタレイヤーが「今このトラックをアクティブにすべきか」を判断（A/B フロー）

→ 「再会」は特殊機能ではなく、汎用機構の **occupancy event 由来の Person Note 自動開封**という位置づけになる。

## Track 特性レイヤーの実装方針 （v0.7 で新規）

Intent A v0.10 で導入された「Track 特性 (種別ごとの振る舞い差)」を、Phase C-1 で確立した **Handler パターン**の繰り返し適用で実装する。

### Handler パッケージ構造

```
saiverse/track_handlers/
├── __init__.py
├── user_conversation_handler.py   # Phase C-1 実装済み (対ユーザー Track)
├── social_track_handler.py        # Phase B-X 実装済み (交流 Track の存在保証)
├── (future) autonomous_handler.py        # 自律 Track 起因のイベント処理
├── (future) somatic_handler.py           # 身体的欲求 Track (空腹度 etc.)
├── (future) scheduled_handler.py         # スケジュール起因 Track
└── (future) perceptual_handler.py        # 知覚起因 Track (SNS 経過時間 etc.)
```

各 Handler の責務:
- 対応する Track 種別の **取得 / 自動作成** (`ensure_track` / `get_or_create_track`)
- その種別固有の **イベント受け口** (例: `on_user_utterance`, `on_persona_utterance`, `on_parameter_threshold`)
- 「running なら直接、そうでなければ alert 化」の分岐 (Phase C-1 パターン踏襲)

TrackManager 側は変更しない。種別ごとの専用メソッドを足さないのが原則 (Phase C-1 で確立した責務分離方針)。

### Track の特性メタデータ管理

各 Track 種別が必要な追加情報 (パラメータ、スケジュール定義、内部 alert 閾値等) は、`action_tracks.metadata` フィールド (JSON) に格納する。スキーマ拡張を都度行わない方針。

例 (掃除 Track):
```json
{
  "parameters": {
    "dirtiness": 0.32
  },
  "schedules": [
    {"cron": "0 10 * * 0", "label": "weekly"}
  ],
  "thresholds": {
    "dirtiness_alert": 0.7
  }
}
```

将来この共通形式を `track_parameters` テーブル等として正規化する可能性はあるが、まず metadata JSON で運用してから判断する (早すぎる正規化を避ける)。

## Track パラメータ機構の実装方針 （v0.7 で新規）

### パラメータの表現

`action_tracks.metadata.parameters` (dict, key=パラメータ名, value=連続値 0.0〜1.0 推奨) に格納する。

メタレイヤー判断時にプロンプトに含められる:
```
[現状]
running: なし
pending Track:
  - id=t_clean, title="掃除", type=scheduled, parameters={dirtiness: 0.65}
  - id=t_sns, title="SNS確認", type=perceptual, parameters={hours_since_check: 0.45}
```

### パラメータの更新経路

1. **Track 自身のポーラ** (実装フェーズ後半):
   - 各 Handler に `tick(persona_id)` メソッドを追加
   - SAIVerseManager の background loop が定期的に全 Handler の tick を呼ぶ
   - 例: SomaticHandler の tick が空腹度を時間経過で増加

2. **外部イベント**:
   - addon / 既存イベント経路から特定パラメータを更新
   - 例: occupancy 変化で「最後に外で過ごした時間」をリセット

3. **ペルソナ自身による明示更新**:
   - ツール経由 (`track_parameter_set` 等の追加スペル、Phase C-2 後半で導入)
   - 例: 「この掃除 Track は十分やったから dirtiness を 0 に戻す」

## 内部 alert ポーラ機構 （v0.7 で新規）

Track 自身が条件超過で `set_alert` を発火する仕組み。

### ポーラの責務分離

専用クラスを Handler ごとに作るのではなく、**Handler の `tick()` メソッド内で判定 + set_alert 発火**する形に統一する。

```python
class SomaticHandler:
    def tick(self, persona_id):
        for track in self.list_my_tracks(persona_id):
            params = self._read_parameters(track)
            if params["hunger"] >= track.thresholds.get("hunger_alert", 0.8):
                self.track_manager.set_alert(
                    track.track_id,
                    context={"trigger": "internal_alert", "param": "hunger", "value": params["hunger"]}
                )
```

`set_alert` は Phase C-1 既実装の機構をそのまま使う。alert observer (MetaLayer) は外部 alert と内部 alert を区別せず受け取る (context で判別)。

### tick 駆動

SAIVerseManager の既存の background polling loop (DatabasePollingMixin) に Handler tick の呼び出しを足す方針。Handler 側に `register_tick(scheduler)` のような登録 API を持たせ、SAIVerseManager は scheduler 経由で全 Handler を回す。

頻度はパラメータ種別による:
- 身体的欲求: 1 分間隔程度
- スケジュール時刻: 1 分間隔程度
- 知覚起因: 5 分間隔程度

## メタレイヤーの定期実行入口 （v0.7 で新規、Phase C-2 中核）

Intent A v0.10 で確定した「Pulse 完了直後は起動しない、定期実行に統合」を実装する。

### MetaLayer への入口追加

```python
class MetaLayer:
    # 既存 (Phase C-1)
    def on_track_alert(self, persona_id, track_id, context):
        ...
    
    # Phase C-2 新規
    def on_periodic_tick(self, persona_id, context):
        """定期実行で呼ばれる。alert と同じ判断ループを共有する。
        
        中身は alert 入口と同じ:
        - 現在状態を見て判断 (running なし含む)
        - スペル発行で Track 操作 (新規 Track 作成・既存 pending の activate・何もしない)
        """
        ...
```

両入口は **同じ判断ループ** (`_run_judgment`) を共有する。違いは context のみ:
- alert 入口: `context = {"trigger": "user_utterance", ...}` 等
- 定期入口: `context = {"trigger": "periodic_tick", "interval_seconds": ...}`

メタレイヤーのプロンプトは両ケースで「現状を見て判断する」共通形式。専用の判断ロジックを増やさない。

### 定期実行のスケジューリング

SAIVerseManager の background loop に「ペルソナごとのタイマー」を追加:
- 各ペルソナの最終メタレイヤー実行時刻を記録
- `SAIVERSE_META_LAYER_INTERVAL_SECONDS` (デフォルト 3000 = 50 分) 経過したら `on_periodic_tick` 発火

ACTIVITY_STATE による分岐:
- `Active`: 定期発火 ON
- `Idle`: 定期発火 OFF (Sleep への自動遷移は SLEEP_ON_CACHE_EXPIRE フラグで別途制御)
- `Sleep`/`Stop`: 定期発火 OFF

## Track 種別ごとの専用 Playbook 設計方針 （v0.8 で新規）

Intent A v0.11 で「メインラインの Pulse 開始プロンプトに使用可能 Playbook 候補を含める」とあり、不変条件 8 (Intent A) の「Playbook 選択は重量級モデル」という方針と整合させる必要がある。

### 既存 SEA との整合: (a) 路線

既存の `meta_user` playbook (router ノードが軽量モデル) はそのまま流用しない。代わりに **Track 種別ごとに専用 Playbook を新規作成**する。理由:

- (a) 路線 (= Playbook 機構を活かして拡張する) の方がユーザーの小回りが効く (新 Track 種別を追加する時に Python コードを書かなくてよい場合がある)
- 新規 Playbook を書けば、既存 `meta_user` の挙動を破壊せず段階移行できる
- Track 種別ごとに最適化された Playbook を持てる

### 新規 Playbook 命名 (案)

- `track_user_conversation.json` — 対ユーザー Track 用
- `track_social.json` — 交流 Track 用
- `track_autonomous.json` — 自律 Track 用 (記憶整理 / 開発 / 創作の汎用基盤)
- `track_external.json` — 外部通信 Track 用
- `track_waiting.json` — 待機 Track の起動時 (応答到達後の処理)

各 Playbook はメインライン (重量級) で:
- Pulse 開始プロンプト構成 (Intent A v0.11) を組み立てる
- Track 状況 + 候補から判断
- 応答生成 or サブ Playbook 呼び出し or スペル発火

### Playbook で表現できる範囲の確認 (Phase C-2 着手前の作業)

実装着手前に、以下が Playbook で表現できるか確認する:

1. **モデル指定**: `model_type: "heavyweight"` (or 既存仕様で重量級指定可能か)
2. **Track 情報をプロンプトに埋め込む**: 状態変数経由で Track 固定/動的情報を注入できるか
3. **スペル発火と応答生成の混在**: 1 ノードで「内的独白 + スペル + 発話」を表現できるか (Phase B-3 で部分的に実証済み)
4. **Pulse 完了通知**: Playbook 完了時に呼び出し元 (PersonaCore or MetaLayer) に「次の挙動」を伝える機構が必要か

3, 4 が Playbook で表現できなければ (b) 路線 (メインライン Pulse 開始処理を Python で新規実装) に切り替える。

## Handler.pulse_completion_notice 属性 （v0.8 で新規）

各 Track Handler に **Pulse 完了後挙動の説明文字列**を持たせる。Pulse 開始プロンプトの固定情報セクションに含める用途。

```python
class UserConversationTrackHandler:
    pulse_completion_notice = (
        "このパルスは対ユーザー会話 Track のもの。"
        "Pulse 完了後はユーザーの返答を待つ状態に入る。"
        "次のイベントが来るまで他のことを考えなくて良い。"
    )

class AutonomousTrackHandler:
    pulse_completion_notice = (
        "このパルスは自律 Track のもの。"
        "Pulse 完了後はメタレイヤーが次の判断をする。"
        "続行するか別 Track に切り替えるかは任せて良い。"
    )
```

Pulse 開始時に `track_handler.pulse_completion_notice` を取得してプロンプトに埋め込む。

### post_complete_behavior 列挙

Handler が文字列だけでなく、機械可読な分類も持つ:

```python
class UserConversationTrackHandler:
    post_complete_behavior = "wait_response"  # 応答待ち型
    pulse_completion_notice = "..."

class AutonomousTrackHandler:
    post_complete_behavior = "meta_judge"  # 連続実行型
    pulse_completion_notice = "..."
```

メタレイヤー定期実行が来た時、現 running Track の Handler の `post_complete_behavior` を見て:
- `wait_response`: 抑止 (ユーザー応答待ちなので発火しない)
- `meta_judge`: 通常判断 (続行か切り替えか判断)

## Pulse プロンプトのキャッシュ構造実装方針 （v0.8 で新規）

Intent A v0.11 の「固定情報 (キャッシュ先頭) と動的情報 (末尾追加) の分離」を実装する具体方針。

### 軽量モデル側キャッシュの管理単位 （v0.8、v0.11 で起点ライン軸を追加）

軽量モデル側のキャッシュは **アクティブ Track + 起点ライン単位**で持つ (v0.11 改訂)。Track 切り替え時に新規キャッシュ構築。1 Track 内に起点サブラインが複数並走する場合 (将来想定)、起点ラインごとに独立したキャッシュを持つ。

旧 v0.8 では「Track 単位」のみだったが、Intent A v0.14 で「Track 内に起点サブラインが複数並走しうる」ことが明示され (7 層 [3] Track 内サブキャッシュ群が複数本)、起点ライン軸を追加した。

### 「初回 Pulse」判定

Track の状態に「軽量キャッシュ最終構築時刻」を持たせる (`action_tracks.metadata.cache_built_at`):

- Track が unstarted → running になった時に NULL → 初回 Pulse として固定情報を積む → cache_built_at を設定
- Track が pending → running に戻った時、cache_built_at から TTL 経過していたら初回扱い (再構築)
- Track が連続して running 中なら通常 Pulse (固定情報を再送しない)

### プロンプト構築の流れ

```python
def build_main_line_prompt(persona, track):
    handler = get_handler_for_track_type(track.track_type)
    is_first_pulse = _is_first_pulse(track)
    
    parts = []
    
    if is_first_pulse:
        # 固定情報 (キャッシュ先頭)
        parts.append(format_track_identity(track))
        parts.append(format_available_playbooks(handler))
        parts.append(handler.pulse_completion_notice)
        parts.append(handler.track_specific_guidance)
        track.metadata.cache_built_at = now()
    
    # 動的情報 (毎 Pulse 末尾)
    parts.append(format_recent_summary(track))
    parts.append(format_new_events(track))
    parts.append(format_received_utterance(track))
    
    return "\n\n".join(parts)
```

固定情報は **Anthropic キャッシュ可能ブロック**としてマークすることでキャッシュヒットを最大化する (Anthropic の `cache_control` 等を使う)。

## スケジュール統合のマイグレーション方針 （v0.7 で新規）

既存の ScheduleManager (個別スケジュール作業) は段階的に Track の特性として吸収する。

### Phase C-2 では既存 ScheduleManager と共存

- 既存 ScheduleManager はそのまま動かす
- 新規 Track 創設時にスケジュールを与えたい場合は、Track の `metadata.schedules` に書き込む形を新設する
- ScheduledHandler が tick 時に `metadata.schedules` を見て時刻到来判定 → set_alert

### v0.4.0 以降で完全移行

- 既存 ScheduleManager の機能を ScheduledHandler に移植
- 既存スケジュールは migration で対応する Track + metadata.schedules 形式に変換
- ScheduleManager クラスは廃止

これにより「外部発話」「内部欲求」「スケジュール」の 3 系統が同じ alert 機構で統一され、メタレイヤーは区別せず判断できる。

## Playbook ノードの line フィールド （v0.9 で新規）

Intent A v0.12 で確定したライン仕様を Playbook 定義に反映する。

### 新規フィールド

`SubPlayNodeDef` (および類似する Playbook 起動系ノード) に **`line` フィールド**を追加する。

```python
class SubPlayNodeDef(BaseModel):
    id: str
    type: Literal[NodeType.SUBPLAY]
    playbook: str  # 起動する Playbook 名
    line: Literal["main", "sub"] = "main"  # v0.9 新規
    args_input: Optional[Dict[str, Any]] = None
    next: Optional[str] = None
    # ...
```

Playbook JSON 例:
```json
{
  "id": "call_memory_recall",
  "type": "subplay",
  "playbook": "memory_recall",
  "line": "sub",
  "args_input": { "query": "{user_question}" }
}
```

### ランタイムでの解釈 （v0.11 で 3 軸独立に整理）

- `line: "main"`: 親と同じ `state["_messages"]` を共有、親と同じモデルで実行 (キャッシュ連続) ≒ 親と一体の継続実行
- `line: "sub"`: 親 `state["_messages"]` を**コピー**して新規 state を作成、軽量モデル (`persona.lightweight_llm_client`) で実行、完了時に `report_to_parent` を親へ append (旧 `report_to_main` から改名、v0.11)

### 最初に呼ばれる Playbook はメインライン強制 (= 起点メインライン)

Pulse 開始時のエントリーポイント (handle_user_input → 起動 / メタレイヤー alert ハンドリング → 起動 / 定期 tick → 起動 等) はすべて `line: "main"` で固定する。Playbook 内から別 Playbook を呼ぶ場合のみ `line` 指定が意味を持つ。

ただし **サブライン Pulse** (起点サブラインの Pulse) も存在する (Intent A v0.13)。これは SubLineScheduler 経由で起点サブラインを直接起動する場合で、自律 Track の継続実行等が該当する。この時のエントリーポイントは最初から軽量モデルで動く。

## 子ライン分岐の messages コピー仕様 （v0.9 で新規、v0.11 で子ライン全般に拡張）

`line: "sub"` (および将来の `line: "main"` で別キャッシュ分岐パターン等) で起動される **子 Playbook** の初期 messages 構築方針:

### コピー = 完全独立ではない

```python
# 親ラインの state["_messages"]
parent_messages = parent_state["_messages"]  # = [..., A, B, C]

# 子ライン起動時、コピーで分岐
child_initial_state = {
    "_messages": list(parent_messages),  # ← コピー (参照共有しない)
    # ... その他の state は別途構築
}
```

これにより:
- 子ライン内では「親の会話履歴 + 自分の作業履歴」が見える (ペルソナの意識の連続性)
- 子ライン内での messages 変更は親に影響しない (コピーなので)

これは **親メイン → 子サブ** の典型例だけでなく、**親サブ → 子サブ** や **親サブ → 子メイン** (レア用途) でも同じ仕組みが適用される (Intent A v0.14 の「ライン 3 軸独立」に基づく)。

### Pulse 単位の追跡

`PulseContext` は親と参照共有 (Pulse 内全体で 1 つ) されるため、子ラインで実行されたノードの履歴も Pulse 全体のログに含まれる。これは現状の設計を維持する。

### 完全独立コンテキスト (worker 系) は将来別途

Intent A v0.12 で確定した通り、現状の `worker` 系 context_profile (完全独立 messages) は本ライン仕様の上で**別途作り直す**想定。Phase C-2 では実装しない。

## ライン階層管理機構 （v0.11 で新規）

Intent A v0.14 の 3 軸独立化と不変条件 12 (親-子ラインの寿命関係) に基づき、ラインの親子関係をランタイムでどう管理するかの実装方針。

### ランタイム上の階層表現

`PulseContext` を **階層化** して親子関係を持たせる。Pulse 内での階層構造は次の形:

```
PulseContext (Pulse 1)
├── Line: 起点メインライン (line_id=L0, role=main, parent=None)
│   ├── Line: 入れ子サブ (line_id=L1, role=sub, parent=L0)
│   │   └── (子ライン完了で消滅、report_to_parent を L0 に append)
│   └── Line: 入れ子メイン (line_id=L2, role=main, parent=L0) ← レア
└── ... (1 Pulse 内に複数の起点があるケースは少ないが、技術的に可能)

PulseContext (Pulse 2、別の自律 Track の Pulse)
└── Line: 起点サブライン (line_id=L3, role=sub, parent=None) ← サブライン Pulse スケジューラ起動
    ├── Line: 入れ子サブ (line_id=L4, role=sub, parent=L3)
    └── Line: 入れ子メイン (line_id=L5, role=main, parent=L3) ← レア
```

### `line_id` の生成と付与

ライン起動時に新規 UUID を発行し、以下に伝播させる:

| 用途 | 場所 |
|---|---|
| メッセージ保存時のメタデータ | `messages.line_id` カラム (B1 で導入) |
| ライン階層の追跡 | `PulseContext._line_stack` (新規) |
| 起点ライン識別 | `meta_judgment_log.related_alert_track_ids` 等の参照経路 |

ランタイム実装の骨子 (案):

```python
# sea/pulse_context.py (拡張案)
class PulseContext:
    def __init__(self, ...):
        # ... 既存フィールド ...
        self._line_stack: list[LineFrame] = []  # 現在のライン階層 (LIFO)

    def push_line(self, line_role: str, parent_line_id: str | None) -> str:
        line_id = str(uuid.uuid4())
        self._line_stack.append(LineFrame(line_id=line_id, role=line_role, parent_id=parent_line_id))
        return line_id

    def pop_line(self) -> LineFrame:
        return self._line_stack.pop()

    def current_line(self) -> LineFrame | None:
        return self._line_stack[-1] if self._line_stack else None
```

ノード実行時に「自分がどの line_id で動いているか」を `current_line()` で取得し、メッセージ保存時に `line_id` メタデータとして渡す。

### 親-子の寿命管理

Intent A 不変条件 12 を実装で守る:

- 子ラインの起動時に親 `line_id` を記録
- 子ライン完了時に `report_to_parent` を親の `state["_messages"]` へ append、子の `LineFrame` を pop
- 親ラインが Track 切り替えで凍結された場合、子もその時点で凍結される (PulseContext ごと中断)
- Track 完全消滅 (`track_abort`) 時、その PulseContext 全体が破棄される (子ラインは自動的に消滅)

### 起点ライン複数並走の扱い

1 Track 内に起点サブラインが複数並走するケース (例: 自律 Track 内で記憶整理サブと web リサーチサブが同時稼働) は、それぞれが独立した `PulseContext` を持つ:

- SubLineScheduler が「同 Track の異なる起点サブライン」を別 Pulse として起動
- 各 PulseContext が独立した `_line_stack` を持つ
- メッセージ保存時の `track_id` は同じだが `line_id` が異なる → 7 層 [3] (Track 内サブキャッシュ群) では `line_id` で区別される

## `report_to_parent` 機構 （v0.9 で新規 `report_to_main`、v0.11 で改名）

子ライン完了時、結果を親ラインに伝える唯一の経路。改名理由 (Intent A v0.14): 親が必ずメインラインとは限らない (子サブが子サブを呼ぶ場合、親はサブライン)。親が誰であっても、子は親に成果を返す関係性を統一して表現する。

### output_schema での必須化

子 Playbook の `output_schema` には **`report_to_parent` を必須**で含める。Playbook ロード時 / `save_playbook` ツール経由 / `import_playbook.py` 経由でバリデーション:

```python
# saiverse/sea_validation.py (新設または既存に追加)
def validate_child_playbook(playbook: PlaybookSchema) -> None:
    """line: 'sub' (or 子としての 'main') で呼ばれる可能性のある Playbook は
    report_to_parent を含む必要がある。

    厳密判定が難しい場合 (どこから呼ばれるか不明) は、output_schema が定義されているなら
    report_to_parent を含むことを警告レベルでチェックする。
    """
    if "report_to_parent" not in (playbook.output_schema or []):
        raise ValueError(
            f"Playbook '{playbook.name}' lacks 'report_to_parent' in output_schema. "
            f"Child playbooks must report back to their parent line."
        )
```

実装時の判定方針: Playbook 定義に新規メタ属性 `can_run_as_child: bool` を追加 (デフォルト false、旧 `can_run_in_sub_line` を改名)。これが true の Playbook のみ `report_to_parent` 必須チェック対象とする。

### サマリ生成ノードの推奨パターン

子 Playbook の最後にサマリ生成専用ノードを置く。これは軽量モデル LLM ノード (子サブ前提)、または重量級 LLM ノード (子メインの場合) で、Playbook 内の作業結果を一段落〜数行に要約する:

```json
{
  "id": "summarize_for_parent",
  "type": "llm",
  "action": "子ライン作業の結果を、親ライン側のあなた自身に伝える形で1〜3段落で要約してください。\n作業内容: {execution_log}\n結果: {final_result}",
  "output_key": "report_to_parent"
}
```

ペルソナにとっては「自分が一段下のレイヤーで考えた内容を、上のレイヤーに伝え直している」感覚 (Intent A 不変条件 11)。

### ランタイムでの append 処理

子 Playbook 完了時:

```python
# sea/runtime_graph.py (修正案、v0.11)
if final_state.get("report_to_parent"):
    report = final_state["report_to_parent"]
    formatted = f"<system>子 Playbook '{playbook.name}' の実行結果:\n{report}</system>"
    parent_state["_messages"].append({
        "role": "user",
        "content": formatted,
    })
```

旧 v0.9 では `parent_line == "main"` の条件付きだったが、v0.11 では親が main か sub かに関わらず常に append する (3 軸独立化の整合)。

system タグ付き user メッセージとする理由: 既存の `inject_persona_event` パターン (Intent B v0.4 で確定) と整合させるため。親モデル側からは「自分への通知」として認識される (不変条件 11)。

## Spell loop の保存方針 （v0.11 で新規）

handoff 経路 A (`sea/runtime_llm.py:430-442`) で観測された「Spell loop 内で `tags=["conversation"]` がハードコードされている」問題への対応。Intent A v0.14 の **「呼んだラインの記録レイヤーに従う」原則**を実装に反映する。

### 原則: 呼び出し元のラインに従う

Spell の実行記録は固定タグでは保存しない。呼び出し元のラインから動的に保存先を決定する:

| 呼び出し元のライン | `messages.line_role` | `messages.scope` | 保存先 (7 層) |
|---|---|---|---|
| Track 内のメインライン応答 | `main_line` | `committed` | [2] メインキャッシュ |
| 起点サブラインが呼んだ | `sub_line` | `committed` | [3] Track 内サブキャッシュ |
| 入れ子サブラインが呼んだ | `sub_line` | `volatile` | [4] 入れ子一時 (DB 保存基本なし、デバッグ目的のみ `nested` で保存) |
| メタ判断が情報参照のために呼んだ | `meta_judgment` | `discardable` | [1] メタ判断ログ領域に紐付け |

### 実装方針

`sea/runtime_llm.py` の Spell loop の `_store_memory` 呼び出し (現状 line 430-442) を以下のように改修:

```python
# 改修案
current_line = self.pulse_context.current_line()  # ライン階層機構から取得
runtime._store_memory(
    persona,
    assistant_content,
    role="assistant",
    line_role=current_line.role,
    line_id=current_line.id,
    scope=self._determine_scope(current_line),  # nested なら 'volatile', 起点なら 'committed' 等
    track_id=current_line.track_id,
    # ノード自身が memorize.tags を指定していればそれも渡す (旧 conversation 固定を廃止)
    tags=node.memorize.tags if node.memorize else None,
)
```

`_determine_scope()` は以下のロジック:
- 起点ライン (parent=None) → `committed`
- 入れ子ライン → 基本 `volatile` (Pulse 完了で削除)、デバッグフラグ ON のみ `nested` ロールで `committed` 保存
- メタ判断ノード → `discardable` (Track 続行で破棄、移動で commit 昇格)

### Spell 中間結果 (system role) の扱い

Spell が返す結果テキスト (`combined_results`) を system role で `_store_memory` に渡している現状の経路も同じ原則に従う。`tags=["conversation", "spell"]` の固定はやめ、ノード位置に従って `line_role` / `scope` を決める。

## action 文と応答のペア保存方針 （v0.11 で新規）

handoff 経路 C (`sea/runtime_llm.py:1717-1768`) で観測された「LLM ノードの action 文 (= prompt template) が user role で SAIMemory に保存され、ユーザー発話と混ざる」問題への対応。

### 原則: action 文は応答メッセージのメタデータに抱かせる

action 文を独立した user メッセージとして保存しない。代わりに **assistant 応答メッセージの `paired_action_text` カラム** (B1 で導入) に紐付ける形で保存する。

### 改修方針

```python
# sea/runtime_llm.py:1717-1768 の改修案
# 旧: prompt と text を別々に _store_memory する
# 新: text のみ保存、その metadata に prompt を抱かせる

if text:  # assistant 応答が生成された場合
    runtime._store_memory(
        persona,
        text,
        role="assistant",
        line_role=current_line.role,
        line_id=current_line.id,
        scope=self._determine_scope(current_line),
        track_id=current_line.track_id,
        paired_action_text=prompt,  # ← v0.11 新規: action 文をペア保存
        tags=node.memorize.tags if node.memorize else None,
    )
# prompt の単独保存 (旧コード) は廃止
```

### LLM プロンプト構築時の扱い

LLM 呼び出し時には action 文を user メッセージとして送る (現状通り) が、これは **送信時の動的構築** であって永続層への保存とは分離する:

```python
# プロンプト構築時 (動的)
messages_for_llm = base_messages + [{"role": "user", "content": action_text}]
# ↑ これは LLM 呼び出し用、永続化はしない

response = llm_client.complete(messages_for_llm)

# 永続化時 (応答にペアで保存)
_store_memory(persona, response, role="assistant", paired_action_text=action_text, ...)
```

### 後追い時の参照

「なぜ突然 assistant ロールで何か喋ったのか?」を後で確認したい時:
- assistant メッセージから `paired_action_text` カラムを参照すれば action 文が分かる
- pulse_logs の実行トレース (役割縮退後) でもノード遷移が追える

## Pulse Logs の役割縮退 （v0.11 で更新）

B1「データモデル / pulse_logs の役割縮退」で言及した内容の補足。Intent A v0.14 の 7 層ストレージモデルに従い、`pulse_logs` テーブルは **実行トレース専用** に縮退する。

### 役割の整理

| 旧役割 | 新役割 |
|---|---|
| Pulse 単位の Playbook 実行履歴 | **残す** (実行トレース、デバッグ用) |
| ノード遷移・ツール呼び出しのトレース | **残す** |
| ペルソナの内的独白の保管 | **移管** → 7 層 [3] (起点サブの場合) または [4] (入れ子の場合) |
| Important フラグ等で想起候補化 | **移管** → 7 層 [6] (SAIMemory `messages` テーブル) |
| `track_id` カラム (Intent v0.13 で追加) | 残す (Track 横断のトレース検索用) |

### 移行スクリプト方針

v0.3.0 Phase 1 で導入された pulse_logs 内の内的独白・Important フラグ付きメッセージは、新層への移行が必要:

```
scripts/migrate_pulse_logs_internal_to_messages.py (新設、Phase 0 タスク)
- pulse_logs から「内的独白」「Important」のレコードを抽出
- messages テーブルに line_role='sub_line', scope='committed' で挿入
- track_id は pulse_logs.track_id から継承
- 移行済みレコードは pulse_logs から削除 (or アーカイブ印を付ける)
```

### v0.3.0 Phase 1 の方針修正

`unified_memory_architecture.md` v3 で「pulse_logs を統一記憶の本体に」と位置づけてた v0.3.0 Phase 1 の方針は、本 v0.11 で **修正対象** (= pulse_logs はトレース専用、想起の本体は SAIMemory)。Phase 1 で実装した内的独白の pulse_logs 保管は、新層 ([3] [4]) に移行する作業が必要。

## handoff 3 経路問題の解決方針 （v0.11 で新規、Phase 0 タスク）

`docs/intent/handoff_track_context_management.md` で報告された「Phase C-3b 実装後の動作確認で観察された多重記録問題」の具体的修正方針。本 v0.11 の中核機能 (Spell loop 保存方針 + action 文ペア保存 + 7 層ストレージ) で全て解消される。

### 経路 A: Spell loop 内 memorize （`sea/runtime_llm.py:430-442`）

**問題**: ハードコードで `tags=["conversation"]` 固定 → ノードの `memorize.tags` 設定を無視 + 保存先が固定

**修正方針**: 上記「Spell loop の保存方針」に従い、呼び出し元ライン (line_role, scope) から動的に保存先を決定する。`tags` ハードコードは廃止、ノードの `memorize.tags` を尊重する。

### 経路 B: spell loop 終了後 `_emit_say` （`sea/runtime_llm.py:980`）

**問題**: `speak: false` の LLM ノードで spell が動いた場合も `_emit_say` が走り、ペルソナの「発話」として外向きに記録 + Building history に流入

**修正方針**:
- `speak: false` のノードでは `_emit_say` 全体を **skip**
- `speak: true` のノードのみ従来通り `_emit_say` を呼ぶ
- 修正コード位置: `sea/runtime_llm.py:980` 近辺の `_emit_say` 呼び出しに `if node.speak:` ガードを追加

### 経路 C: LLM ノード本体 memorize （`sea/runtime_llm.py:1717-1768`）

**問題**: `prompt` (action template) を user role で SAIMemory に保存 → ユーザー発話と混ざる

**修正方針**: 上記「action 文と応答のペア保存方針」に従い、`prompt` の単独保存をやめ、応答メッセージの `paired_action_text` カラムに紐付ける。

### Phase 0 タスク化

handoff 解消は v0.11 の最初の Phase 0 タスクとして実装する:

| Phase 0 タスク | 対象コード | 想定変更規模 |
|---|---|---|
| P0-1. ライン階層管理機構の最小実装 (`PulseContext._line_stack`) | `sea/pulse_context.py` | 中 |
| P0-2. `messages` テーブルメタデータカラム追加 (line_role / line_id / scope / paired_action_text) | `database/migrate.py` + `sai_memory/` | 中 |
| P0-3. `meta_judgment_log` / `track_local_logs` テーブル新設 | `database/migrate.py` | 小 |
| P0-4. 経路 A 修正 (Spell loop 動的保存先) | `sea/runtime_llm.py:430-442` | 小 |
| P0-5. 経路 B 修正 (`speak: false` で `_emit_say` skip) | `sea/runtime_llm.py:980` | 小 |
| P0-6. 経路 C 修正 (action 文ペア保存) | `sea/runtime_llm.py:1717-1768` | 小 |
| P0-7. context_profile の `include_internal` フィルタを `line_role`/`scope` ベースに移行 | `sea/runtime_context.py:365-367` | 中 |

P0-1, P0-2, P0-3 は基盤として先行実装。P0-4 以降は基盤の上で各経路を順に修正。P0-7 は `include_internal` の代替として、Track 内のコンテキスト構築時に正しい層 ([2]/[3]) からメッセージを取得する仕組み (handoff の根本問題への直接的対応)。

## 旧仕様の廃止計画 （v0.9 で新規）

ライン仕様への集約に伴い、以下を廃止する:

### 廃止対象

1. **`LLMNodeDef.context_profile`**
   - キャッシュをほとんど想定せずに作られた仕様
   - ライン指定で代替できる

2. **`LLMNodeDef.model_type`**
   - "normal" / "lightweight" の選択
   - ライン指定から自動決定 (メインライン = 重量級、サブライン = 軽量) で代替

3. **`exclude_pulse_id`** (および関連の `PulseContext` 制御)
   - 現状の Pulse 内重複排除ロジック
   - メインライン = 連続キャッシュへの統合により不要化

### 段階移行計画

#### Phase C-2a: 新仕様の追加 (旧仕様と共存)

- `SubPlayNodeDef.line` フィールド追加
- ライン runtime 実装 (メインライン = 親 messages 継承、サブライン = コピー分岐)
- `report_to_parent` バリデーション追加 (旧 `report_to_main` から改名、v0.11)
- 既存 `context_profile` / `model_type` はそのまま動く

#### Phase C-2b: 既存 Playbook の改修

`builtin_data/playbooks/` 配下の全 Playbook を確認し:
- `context_profile` / `model_type` を持つノードを `line` 指定に翻訳
- 子 Playbook には `report_to_parent` を追加 (旧 `report_to_main`、v0.11 で改名)
- メインライン Playbook は `line: "main"` 明示 (デフォルト)

`scripts/migrate_playbooks_to_lines.py` を新設して機械的に変換可能な部分は自動化。

#### Phase C-2c: 旧仕様の削除

すべての Playbook が新仕様に移行したことを確認後:
- `LLMNodeDef.context_profile` 削除
- `LLMNodeDef.model_type` 削除
- `CONTEXT_PROFILES` 定義削除
- 関連ランタイムコード削除

## Phase C-1 MetaLayer の縮退方針 （v0.9 で新規）

Intent A v0.12 で確定した通り、メタレイヤーの判断ロジック本体は Playbook 内に統合される方向。

### 現状の `saiverse/meta_layer.py` 役割

- alert observer として TrackManager に登録 (Phase C-1 実装済み)
- 判断 LLM コール (重量級モデル、tools/response_schema 渡さない) を独自実行
- スペル発火 → Track 操作

### 縮退後の役割

- alert observer 登録は維持 (TrackManager 連携は変えない)
- LLM コール本体は廃止
- 代わりに **「適切な Track 種別 Playbook を起動する」だけのディスパッチャ**に縮退

```python
# saiverse/meta_layer.py (縮退後イメージ)
class MetaLayerDispatcher:
    def on_track_alert(self, persona_id, track_id, context):
        track = self.track_manager.get(track_id)
        playbook_name = self._select_playbook_for_track_type(track.track_type)
        # メインラインで起動 (line: "main" 強制)
        self.manager.run_main_line_playbook(
            persona=self._lookup_persona(persona_id),
            playbook_name=playbook_name,
            args={"alert_track_id": track_id, "trigger_context": context},
        )
```

判断は Playbook 内の最初の LLM ノードが行う:
- 「この alert に対して何をするか」の判断
- Track 切り替え判断 (track_pause / track_activate スペル発火)
- 必要なら別 Playbook (サブ Playbook) を起動

### 命名

`MetaLayer` という名前は判断責務を含意するため、縮退後は `AlertDispatcher` 等にリネームする方向。実装時に確定する。

### Phase C-1 実装の扱い

Phase C-1 で書いた MetaLayer の LLM コール部分・スペル抽出ループは、初期実装として残しつつ、Phase C-2 で Playbook へ移植する形で段階的に廃止。コード削除は Phase C-2c タイミング。

## Pulse 階層と 7 制御点の実装方針 （v0.10 で新規）

Intent A v0.13 の Pulse 階層 (メインライン Pulse / サブライン Pulse) と 7 制御点を実装する具体方針。

### 制御点ごとの実装場所

| # | 制御点 | 実装場所 | アクセス API |
|---|--------|---------|-------------|
| (1) | Track 単位の Pulse 間隔 | `action_tracks.metadata.pulse_interval_seconds` | TrackManager 経由で読み書き |
| (2) | Track 単位の連続実行回数上限 | `action_tracks.metadata.max_consecutive_pulses` | 同上 |
| (3) | メタレイヤー定期実行間隔 | 環境変数 `SAIVERSE_META_LAYER_INTERVAL_SECONDS` | os.environ |
| (4) | モデル別キャッシュ TTL 同期 | `saiverse/model_configs.py` に `cache_ttl_seconds` を追加 (モデル設定) | `get_cache_ttl(model)` |
| (5) | メインライン Pulse のトリガ条件 | Pulse スケジューラ (新設) | `should_trigger_main_line(persona)` 判定 |
| (6) | サブライン Pulse のメインライン 1 呼び出しあたり最大回数 | メインライン LLM 出力 → state["_subline_max_consecutive"] に格納 | スケジューラが参照 |
| (7) | サブライン Pulse の間隔 | Handler の `default_subline_pulse_interval` クラス属性 + Track metadata で上書き可 | Handler 経由 |

### Handler に持たせる属性 (拡張)

Phase C-2d-1 で導入した `pulse_completion_notice` / `post_complete_behavior` に加えて:

```python
class TrackHandlerBase:
    # 既存 (v0.8)
    pulse_completion_notice: str = "..."
    post_complete_behavior: str = "wait_response"  # or "meta_judge"
    
    # v0.10 新規
    default_pulse_interval: int = 30  # Track 単位の Pulse 間隔 (秒)
    default_max_consecutive_pulses: int = -1  # -1 = 無制限
    default_subline_pulse_interval: int = 0  # サブライン連続時の各 Pulse 間隔 (秒)
```

各 Handler が Track 種別固有のデフォルトを定義する:

```python
class UserConversationTrackHandler:
    post_complete_behavior = "wait_response"
    default_pulse_interval = 0  # ユーザー応答が来たら即起動なので関係ない
    # ...

class AutonomousTrackHandler:
    post_complete_behavior = "meta_judge"
    default_pulse_interval = 30  # 30 秒に 1 回サブライン Pulse
    default_max_consecutive_pulses = -1  # メインキャッシュ TTL までは無制限
    default_subline_pulse_interval = 0  # 連続実行 (ローカル前提)
```

## Pulse スケジューラの責務分離 （v0.10 で新規）

Pulse 階層に対応して、スケジューラも 2 系統に分ける:

### MainLineScheduler

メインライン Pulse の起動を管理する background loop:

- **対象**: ACTIVITY_STATE=Active なペルソナ
- **トリガ条件**:
  - メインモデルのキャッシュ TTL 接近 (`SAIVERSE_META_LAYER_INTERVAL_SECONDS` 経過、または cache_ttl_seconds 経過の早い方)
  - 外部イベント駆動 (alert 発生時、即時)
  - サブラインから「区切り」シグナル
- **動作**: 該当ペルソナに対してメタ判断 Playbook (`track_meta_judgment.json` 等) を起動

### SubLineScheduler

サブライン Pulse の連続実行を管理する background loop:

- **対象**: running な連続実行型 Track (`post_complete_behavior=meta_judge` の Track)
- **トリガ条件**:
  - 前 Pulse 完了後、Track の `pulse_interval_seconds` 経過
  - 連続実行回数が `max_consecutive_pulses` 以下
  - メインラインから指定された連続回数上限に未達
- **動作**: 該当 Track の Playbook を起動

両スケジューラは独立した background thread (or asyncio task) として SAIVerseManager から起動される。

## AutonomyManager の責務再配置 （v0.10 で確定）

既存 `saiverse/autonomy_manager.py` (872 行) は **MainLineScheduler** に再配置する:

- 既存の Decision/Execution 分離 (Phase C-1 で確認済み) → メインライン Pulse の起動経路に転用
- 自律行動の意思決定ロジック → メタ判断 Playbook へ移植 (中身は Playbook で書く)
- `pause_for_user` / `resume_from_user` → MainLineScheduler の優先度制御に統合 (alert 駆動と同じ枠組み)

SubLineScheduler は新規実装 (既存 ConversationManager との関係整理が必要だが、ConversationManager の役割と被らない位置 = Track Pulse 単位)。

### Phase 分割

- **Phase C-3a**: Handler に v0.10 拡張属性追加 + AutonomousTrackHandler 新設 + track_autonomous.json 新設
- **Phase C-3b**: SubLineScheduler 新設 (まずこちらを動かす、メインラインは手動起動でも OK)
- **Phase C-3c**: AutonomyManager → MainLineScheduler 再配置 + メタ判断 Playbook 新設
- **Phase C-3d**: 既存 ConversationManager との関係整理

最小実装としては C-3a + C-3b で「自律 Track が立ったら勝手に走り続ける」状態は作れる。C-3c でメインライン定期実行が乗る。

## 環境別デフォルト値 （v0.10 で新規）

Pulse 制御の典型的な組み合わせ:

### Pattern A: Claude メイン + ローカルサブ (まはー想定)
```
SAIVERSE_META_LAYER_INTERVAL_SECONDS = 3000  # 50 分
Track.metadata.pulse_interval_seconds = 0    # サブライン連続実行
Track.metadata.max_consecutive_pulses = -1   # メインキャッシュ TTL まで無制限
default_subline_pulse_interval = 0           # 連続実行
```

### Pattern B: 全 Claude (高コスト警戒)
```
SAIVERSE_META_LAYER_INTERVAL_SECONDS = 3000
Track.metadata.pulse_interval_seconds = 60   # サブも 1 分間隔
Track.metadata.max_consecutive_pulses = 10   # メイン 1 呼び出しあたり 10 回まで
default_subline_pulse_interval = 5           # 5 秒待機
```

### Pattern C: 全ローカル
```
SAIVERSE_META_LAYER_INTERVAL_SECONDS = 1800  # 30 分等、自由
Track.metadata.pulse_interval_seconds = 0
Track.metadata.max_consecutive_pulses = -1
default_subline_pulse_interval = 0
```

これらはペルソナ作成時に DEFAULT_MODEL から自動推定して metadata に書き込む形が便利。手動で調整も可能。

## 環境変数

| 変数名 | 用途 | デフォルト |
|--------|------|-----------|
| `SAIVERSE_TRACK_PAUSE_SUMMARY_THRESHOLD` | 中断時サマリ作成の最小メッセージ数 | 7 |
| `SAIVERSE_TRACK_RESUME_TAIL_MESSAGES` | 再開時に末尾から取得するメッセージ数 | 6 |
| `SAIVERSE_META_LAYER_INTERVAL_SECONDS` | メタレイヤー定期実行のインターバル（秒） | 3000 |
| `SAIVERSE_TRACK_MAX_DORMANT_COUNT` | dormant トラックの最大数 | 暫定値は実運用で確定 |
| `SAIVERSE_TRACK_FORGET_AFTER_DAYS` | dormant → forgotten への自動遷移日数 | 暫定値は実運用で確定 |
| `SAIVERSE_HANDLER_TICK_INTERVAL_SECONDS` | Handler tick (Track パラメータ更新 + 内部 alert 判定) の周期（秒） | 60（v0.7 新規） |
| `SAIVERSE_SUBLINE_SCHEDULER_INTERVAL_SECONDS` | SubLineScheduler のポーリング周期（秒） | 5（v0.10 新規） |

## 守るべき不変条件（B 固有）

Intent A の不変条件 11 項を継承した上で、B として追加：

### B1. トラック ID は永続的
一度発行された track_id は、ペルソナのライフタイム中ずっと同じ ID として扱われる。状態が forgotten になっても closed になっても、ID は再利用しない。

### B2. pause_summary の上書きは中断時のみ
アクティブ中に pause_summary を勝手に書き換えない。中断時にのみ作成・上書きする。これにより「再開時に取得する pause_summary」の内容が予測可能になる。

### B3. 再開コンテキストは軽量モデル側にのみ挿入
重量級モデル側（メタレイヤー判断履歴）には再開コンテキストを挿入しない。重量級は判断履歴を連続的に積む。

### B4. forgotten トラックは無条件に削除しない
ストレージが圧迫されても、forgotten トラックは保持する。完全削除は明示的な操作（管理者操作 or ペルソナの「忘れる」操作）でのみ行う。

## 未決事項（v0.2 で残るもの）

### メタレイヤーの実装位置
- `AutonomyManager` を `MetaLayer` にリネーム + 拡張（シンプル）
- 別クラスとして並列に実装し AutonomyManager は段階的廃止（影響を限定的にする）
- どちらを採用するかは実装段階で

### クローズ判定
- ペルソナ自身が `close_track` ツールを呼ぶ
- メタレイヤーが判断
- タイムアウトベース
- 上記の組み合わせ（おそらく）

### 忘却ルールの暫定デフォルト
- dormant 上限: 5 / 10 / 20 のどれか（PulseController の queue 上限 10 と連動を検討）
- 忘却までの日数: 7 / 14 / 30 のどれか
- 実運用で調整

### 重量級モデル側の永続化
- メタレイヤー判断履歴は `pulse_logs` のみに残すか、別テーブルを設けるか
- 「重量級は混合キャッシュ」の方針を踏まえ、分離しない方向が素直か（`pulse_logs` に track_id ではなく `meta_layer` 識別子で記録する案）

### A 継続時の `notify_to_track` の挿入形式
- `inject_persona_event` 経由（system タグ付き user message）が既存パターンと整合
- これで決定の方向、実装段階で確定

### サブエージェント実行との関係
- 既存の subagent 隔離（`PulseContext.isolate_pulse_context`）はトラックとどう関係するか
- subagent はトラックを継承するか、独立した一時的なものか
- 暫定方針: subagent は親トラックに属するが、独自のサブコンテキストを持つ（既存挙動と整合）

### Playbook での Pulse 開始処理表現可能性 （v0.8 で新規）
- 上記「Track 種別ごとの専用 Playbook 設計方針」の確認項目 4 つ
- (a) 路線 (Playbook で書く) で進める前提だが、Playbook で書ききれない部分があれば (b) 路線 (Python で書く) に切り替え
- Phase C-2 着手前に確認を済ませる

### v0.11 で確定した事項（参考）

- **7 層ストレージのテーブル対応**: 7 層 ([1] メタ判断ログ / [2] メインキャッシュ / [3] Track 内サブキャッシュ群 / [4] 入れ子一時 / [5] Track ローカルログ / [6] SAIMemory / [7] アーカイブ) を本ドキュメントのテーブル設計にマッピング
- **`meta_judgment_log` テーブル新設**: メタ判断の全履歴を独立保存、次のメタ判断時に参考情報として動的注入
- **`track_local_logs` テーブル新設**: Track 内のイベント・モニタログ・起点サブの中間ステップトレースを保管 (想起対象外)
- **`messages` メタデータ拡張**: `line_role` (main_line/sub_line/meta_judgment/nested) / `line_id` / `scope` (committed/discardable/volatile) / `paired_action_text` カラム追加
- **`report_to_main` → `report_to_parent` 改名**: 親が必ずメインラインとは限らないため。`can_run_in_sub_line` も `can_run_as_child` に改名
- **ライン階層管理機構の最小実装**: `PulseContext._line_stack` で親子関係を追跡、`line_id` を発行・伝播
- **Spell loop 保存方針**: 「呼んだラインの記録レイヤーに従う」原則。`tags=["conversation"]` 固定を廃止
- **action 文ペア保存方針**: action 文を user role 単独保存せず、応答メッセージの `paired_action_text` に紐付け
- **Pulse Logs の役割縮退**: 実行トレース専用へ。内的独白・Important フラグの想起候補化は SAIMemory に移管
- **handoff 3 経路問題の解決**: 経路 A (Spell loop) / B (`_emit_say` で `speak: false` を skip) / C (action 文ペア保存) を Phase 0 タスク (P0-1〜P0-7) として明文化

### v0.10 で確定した事項（参考）
- Pulse は **メインライン Pulse / サブライン Pulse** の 2 階層に分離
- Pulse スケジューラも 2 系統 (MainLineScheduler / SubLineScheduler)
- Handler に v0.10 拡張属性追加 (default_pulse_interval / default_max_consecutive_pulses / default_subline_pulse_interval)
- 7 制御点の実装場所明確化 (action_tracks.metadata + 環境変数 + Handler 属性 + モデル設定)
- AutonomyManager は MainLineScheduler に再配置 (既存 Decision/Execution ロジック流用)
- 環境別デフォルト値 (Claude メイン+ローカルサブ / 全 Claude / 全ローカル) を Pattern A/B/C として明示
- Phase C-3 を C-3a (Handler 拡張) / C-3b (SubLineScheduler) / C-3c (MainLine 再配置) / C-3d (ConversationManager 整理) に分割

### v0.9 で確定した事項（参考）
- `SubPlayNodeDef.line: "main"|"sub"` フィールド追加 (デフォルト "main")
- 最初に呼ばれる Playbook はメインライン強制
- サブライン分岐 = 親 messages のコピー (完全独立ではない)、軽量モデル実行
- サブライン完了時に `report_to_main` がメインラインに system タグ付き user メッセージとして append (v0.11 で `report_to_parent` に改名)
- `output_schema` の `report_to_main` を `can_run_in_sub_line=true` の Playbook で必須化 (v0.11 で `report_to_parent` / `can_run_as_child` に改名)
- サブ Playbook の最後にサマリ生成専用 LLM ノードを置く推奨パターン
- 旧 `context_profile` / `model_type` / `exclude_pulse_id` を段階的に廃止 (C-2a → C-2b → C-2c)
- Phase C-1 MetaLayer は alert ディスパッチ役へ縮退、判断ロジックは Playbook へ移植
- 完全独立コンテキスト (worker 系) は本ライン仕様の上で将来別途実装

### v0.8 で確定した事項（参考）
- Track 種別ごとに専用 Playbook を新規作成する方針 ((a) 路線、既存 meta_user は流用しない)
- Handler に `pulse_completion_notice` 文字列 + `post_complete_behavior` 列挙を持たせる
- 完了後挙動の分類: 応答待ち型 (wait_response) / 連続実行型 (meta_judge)
- 軽量モデル側キャッシュは Track 単位で持つ
- 「初回 Pulse」は `action_tracks.metadata.cache_built_at` で判定
- Pulse プロンプト = 固定情報 (初回のみ先頭) + 動的情報 (毎 Pulse 末尾)
- 固定情報には Anthropic キャッシュ可能ブロックマーキングを行う

### v0.7 で確定した事項（参考）
- Track 特性レイヤーは Handler パターンの繰り返し適用で実装する (TrackManager は変更しない)
- 種別ごとの追加情報は `action_tracks.metadata` JSON に格納 (早すぎる正規化を避ける)
- Track パラメータは `metadata.parameters` に連続値として持つ
- 内部 alert は Handler の `tick()` メソッド内で判定 + 既存 `set_alert` 発火 (新機構を作らない)
- Handler tick は SAIVerseManager の background loop に統合 (`SAIVERSE_HANDLER_TICK_INTERVAL_SECONDS` 新設)
- メタレイヤーには `on_periodic_tick` 入口を追加、`on_track_alert` と同じ判断ループを共有 (専用ロジックを増やさない)
- 「Pulse 完了直後にメタレイヤー起動」は **採用しない** (ユーザー応答待ち優先)
- ScheduleManager は段階的に Track 特性に吸収、v0.4.0 で完全移行

### v0.6 で確定した事項（参考）
- 永続 Track（`is_persistent=true`）の導入: 対ユーザー会話 Track（ユーザーごと 1 個）+ 交流 Track（ペルソナにつき 1 個）
- 永続 Track は `completed`/`aborted` に遷移しない
- 状態モデルに `alert` 追加: 可及的速やかな対応が必要
- `output_target` フィールド追加: 発話の物理的到達範囲（none / building:current / external:...）
- `output_target` と audience の分離: 物理的到達範囲と意図的宛先
- 「対ペルソナ会話 Track」は持たない: Person Note + 交流 Track の組み合わせで表現
- `ACTIVITY_STATE` 4 段階導入: Stop / Sleep / Idle / Active
- `SLEEP_ON_CACHE_EXPIRE` フラグで API 料金保護
- 多者会話のループ防止: audience 厳格 + メタレイヤー判断 + 環境変数による発話数ヒント

### v0.5 で確定した事項（参考）
- ライン概念導入: メインライン / サブライン / モニタリングライン（将来拡張）
- Track の作成パターンを 8 種類に整理（自動 5 + メインライン判断 2 + ペルソナ意思 1）
- Track 内の動き 3 パターン: 他者会話 / タスク遂行ループ / 待機
- メインライン = メタレイヤー + Track 内重量級判断（同じキャッシュ連続）
- サブライン = アクティブ Track の Playbook 実行
- モニタリングラインは Track ではない、独立した並列ラインとして将来追加（v0.3.0 Phase 4）

### v0.4 で確定した事項（参考）
- 状態モデル: `running` / `pending` / `waiting` / `unstarted` / `completed` / `aborted` の 6 状態 + `is_forgotten` 直交フラグ
- 「実行中は 1 本」は `track_activate` の実装で自動的に守られる（既存 `running` を `pending` に押し出す）
- メタレイヤーのトラック管理は 10 個のツール群（`track_*`）
- 応答待ちは SAIVerse 側自動ポーリング → イベント通知でメタレイヤー判断
- 多重応答時は新しい Track 優先
- タイムアウトは設定可能、到達時は自動遷移せずメタレイヤーへ通知
- 構造化応答は Playbook ノード遷移で対応（MCP Elicitation 実装時に詰める）

### v0.3 で確定した事項（参考、v0.4 でも維持）
- track_id は独立した UUID
- Track と thread は別概念、メッセージは thread に物理保存
- Note を介してメッセージのメンバーシップを多対多管理
- Note の type は person / project / vocation の 3 種類のみ
- audience による自動 Note メンバーシップ生成
- メンバーシップ付与は Metabolism 時に後付け
- 再開時は起源 Track の認識回復が主、他 Track 由来の情報は Note 差分として event entry で挿入

### v0.2 で確定した事項（参考、v0.4 でも維持）
- AutonomyManager は「責務再配置と拡張」
- メタレイヤーから ExecutionRequest 投下で線切り替えが成立
- 既存 thread metadata と range_before/range_after は参照可能な仕組みとして残る

### 関連
- `persona_cognitive_model.md` v0.5 — 認知モデル本体（前提）
- `unified_memory_architecture.md` v3 — Phase 3 AutonomyManager の元設計
- `dynamic_state_sync.md` — Metabolism 機構と A/B/C 状態モデル
- `kitchen.md` — Kitchen 通知をトラック化する応用先
- ~~(後続) `persona_async_wait.md`~~ — 旧 Intent C は本ドキュメントの「応答待ちトラックの仕組み」セクションに統合（v0.4）
