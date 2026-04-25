# Intent: 行動の線の永続化・切り替え・記憶復元機構

**ステータス**: ドラフト v0.6（永続 Track 種別、Alert 状態、output_target 分離、Stop/Sleep/Idle/Active）
**作成**: 2026-04-25
**改訂**: 2026-04-25 v0.1 → v0.2 → v0.3 → v0.4 → v0.5 → v0.6
**前提**: `persona_cognitive_model.md` v0.9（永続 Track + Alert 状態 + output_target 分離 + アクティビティ状態）

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

### 既存テーブルの拡張

- **`pulse_logs`**: `track_id TEXT` カラム追加（どのトラックの pulse か識別、index 付き）
- **`messages`** (SAIMemory): 既存の `thread_id` は変更なし。`origin_track_id TEXT` カラム追加（生成時のアクティブ Track）
- **`AI`** (DB): 必要に応じて `current_active_track_id` カラム追加（実装上の利便性）

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

### メインライン と Track の関係

**メインラインと「Track 内の重量級判断」は役割の名前が違うだけで、同じモデル・同じキャッシュ・同じ思考の流れ**。

- メインライン側のキャッシュ = メタレイヤー判断 + 他者会話 + Playbook 選択 + Track 内検収が**全部混ざって連続**（不変条件: 重量級はトラック横断混合）
- サブライン側のキャッシュ = アクティブ Track の Playbook 実行コンテキスト
- Track が切り替わるとサブライン側のキャッシュは切り替わる、メインライン側は連続性維持

これにより不変条件 11「ペルソナはメタの判断履歴を自分の思考として認識する」が**技術的にもそうなる**: メインライン上ではメタ判断・他者会話・Playbook 選択が同じ「思考の流れ」として連続する。

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

## 環境変数

| 変数名 | 用途 | デフォルト |
|--------|------|-----------|
| `SAIVERSE_TRACK_PAUSE_SUMMARY_THRESHOLD` | 中断時サマリ作成の最小メッセージ数 | 7 |
| `SAIVERSE_TRACK_RESUME_TAIL_MESSAGES` | 再開時に末尾から取得するメッセージ数 | 6 |
| `SAIVERSE_META_LAYER_INTERVAL_SECONDS` | メタレイヤー定期実行のインターバル（秒） | 3000 |
| `SAIVERSE_TRACK_MAX_DORMANT_COUNT` | dormant トラックの最大数 | 暫定値は実運用で確定 |
| `SAIVERSE_TRACK_FORGET_AFTER_DAYS` | dormant → forgotten への自動遷移日数 | 暫定値は実運用で確定 |

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
