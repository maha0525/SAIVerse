# 自律生活アーキテクチャ設計書

> Intent: `docs/intent/autonomous_living.md`
> 前提知識: `docs/intent/stelis_thread.md`, `docs/stelis_thread_design.md`

## 中心概念: ブランチ（Branch）

ペルソナの生活は複数の「ブランチ」から成る。ユーザーとの対話、タスクへの取り組み、他のペルソナとの会話、一人の時間。人間がこれらを日常的に切り替えながら一人の人間として生きているように、ペルソナもこれらを切り替えながら自分自身であり続ける。

### ブランチとは

ブランチとは、ひとつの活動に関する連続した経験の総体である。

ブランチは複数のStelisスレッドから構成される。一度の作業セッションが1本のStelisスレッドに対応し、中断・再開のたびに新しいStelisスレッドが作られてアンカーで前のスレッドと接続される。

```
【小説『星の歌』の執筆ブランチ】
  スレッド① (2/3 作成) ← 第1章を書いた
    ↓ アンカー（Chronicle要約 + 末尾の生メッセージ）
  スレッド② (2/5 再開) ← 第2章を書いた
    ↓ アンカー
  スレッド③ (2/6 再開) ← 第3章を書いている途中 [active]
```

このように、アンカーで繋がったStelisスレッドの連なりがブランチを形成する。各スレッドの冒頭にはアンカーが置かれ、前回のセッションのChronicle要約と直近の生メッセージが展開される。これにより、「前回ここまでやったな」という感覚が自然に得られる。

### ブランチの種類

| ブランチ種別 | 説明 | ライフサイクル | 例 |
|---|---|---|---|
| **ユーザー対話** | ユーザーとの会話。最も重要 | 常に存在（ルートスレッド） | まはーと雑談する |
| **タスク** | 目的を持った構造的な活動 | タスク作成時に生成、完了時に終了 | 小説を書く、調査する |
| **ソーシャル** | 特定のペルソナとの対話 | ペルソナ関係ごとに事前作成、永続 | エリスとの会話 |
| **自由行動** | 非構造的な活動 | 自発的に作成・終了 | 散策する、本を読む |

### ソーシャルブランチの特性

ソーシャルブランチは対ペルソナごとにあらかじめ用意される永続的なブランチである。会話のたびにスレッドが増えていき、アンカーで繋がっていく。

```
【エリスとの会話ブランチ】
  スレッド① (1/20) ← 初めて話した日
    ↓ アンカー
  スレッド② (1/25) ← 読書の話で盛り上がった
    ↓ アンカー
  スレッド③ (2/6)  ← 今日の会話 [active]
```

これにより、次にエリスと話すとき、前回の会話のChronicle要約と末尾メッセージがアンカーとして冒頭に展開され、「この前の話の続き」ができる。会話量によるスレッド分割の判定は不要 — 会話が始まるたびに新しいスレッドが開始される。

### ブランチの状態

```
active    → 現在このブランチで活動中
suspended → 中断中（別のブランチに移った）
completed → 完了（タスクブランチのみ。ソーシャルブランチは完了しない）
```

## ブランチ再開時のコンテキスト構成

ブランチを再開するとき、ペルソナのコンテキストは以下の順序で構成される:

```
┌─────────────────────────────────────────────────┐
│ 1. メインスレッド（ルート）の最新コンテキスト    │
│    → 「今の自分」の基盤。ユーザーとの直近の対話  │
│    → Memory Weave (Chronicle + Memopedia)        │
│                                                   │
│ 2. 前回スレッドのアンカー                        │
│    → Chronicle要約（前回のセッションで何をしたか）│
│    → 末尾の生メッセージ（前回の最後のやり取り）   │
│    → 「思い出す」感覚を自然に再現                 │
│                                                   │
│ 3. 現在のスレッドのコンテキスト                   │
│    → 今回のセッションのログ                       │
└─────────────────────────────────────────────────┘
```

これは既存のStelisアンカーの動的展開の仕組みそのものである。再開時には新しいStelisスレッドが作られ、前回のスレッドへのアンカーが冒頭に配置される。

**ポイント**: 中断していたものの再開なので、前回の詳細が若干薄れている（＝Chronicle要約 + 少数の生メッセージ）のはむしろ自然。人間だって昨日の仕事の全てを一字一句覚えているわけではない。「前回のことを思い出す」ために過去スレッドの詳細ログを参照するツールも用意する。

## Stelisスレッドの拡張

Stelisスレッドは元々「自動処理ログの隔離」として設計された。本設計では、これを「ブランチのセッション単位」として拡張する。

### 現在のStelis

```
目的: ログの隔離
ライフサイクル: start → (作業) → end
状態: active / completed / aborted
```

### 拡張後のStelis

```
目的: ブランチの1セッション
ライフサイクル: start → (作業) → suspend（= end + ブランチは生き続ける）
状態: active / completed / aborted
```

**重要な変更**: suspend は Stelis スレッドの終了（Chronicle生成 + 親に戻る）と同義。ただし、そのブランチ自体は suspended 状態で残り、次の resume 時に新しいスレッドが作られる。つまり Stelis のスレッド単体に suspend 状態を追加する必要はない — ブランチという上位概念が suspend を管理する。

### ブランチの永続化

ブランチは `stelis_threads` テーブルとは独立したテーブルで管理する。

```sql
CREATE TABLE branches (
    branch_id TEXT PRIMARY KEY,
    persona_id TEXT NOT NULL,
    branch_type TEXT NOT NULL,         -- 'task', 'social', 'free'
    label TEXT NOT NULL,               -- 人間が読める説明
    status TEXT NOT NULL DEFAULT 'active',  -- 'active', 'suspended', 'completed'
    task_id TEXT,                       -- タスクとの紐づけ（タスクブランチの場合）
    partner_persona_id TEXT,           -- 対話相手（ソーシャルブランチの場合）
    current_thread_id INTEGER,         -- 現在アクティブなStelisスレッドID
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    suspended_at TIMESTAMP,
    completed_at TIMESTAMP,
    FOREIGN KEY (current_thread_id) REFERENCES stelis_threads(thread_id)
);

CREATE INDEX idx_branches_persona ON branches(persona_id);
CREATE INDEX idx_branches_status ON branches(persona_id, status);
CREATE INDEX idx_branches_partner ON branches(partner_persona_id);
```

## ワーキングメモリの再構成

### 現在の構造

```json
{
  "situation_snapshot": { ... }
}
```

### 再構成後

```json
{
  "situation_snapshot": { ... },
  "branches": [
    {
      "branch_id": "branch_novel_001",
      "branch_type": "task",
      "label": "小説『星の歌』の執筆",
      "status": "suspended",
      "last_activity": "2026-02-06T15:28:00Z",
      "brief_state": "第3章の執筆中。主人公が塔に到着したところ"
    },
    {
      "branch_id": "branch_social_eris",
      "branch_type": "social",
      "label": "エリスとの会話",
      "status": "suspended",
      "last_activity": "2026-02-05T19:55:00Z",
      "brief_state": "次に読む本を決めているところだった"
    }
  ]
}
```

ワーキングメモリは `## 現在の状況` としてLLMに渡されるので、ペルソナは自分のブランチ一覧を常に把握できる。

### コンテキスト表示とブランチ選択

**コンテキストに常に載るもの**: ワーキングメモリの `branches` リスト。コンテキスト圧迫を避けるため、各ブランチは `label`, `status`, `brief_state` のみの要約形式で載る。表示件数に上限を設ける（直近アクティブな5-10件程度）。

**ブランチ選択時に見えるもの**: ブランチ選択の判断を行うLLMノードでは、`branches` テーブルの全件を参照する。コンテキスト表示上限とは独立して、全ての suspended / active ブランチが選択肢になる。

## Memory Weave の可視範囲

Memory Weave（Chronicle + Memopedia）は**全スレッド・全ブランチから見える**。ブランチによる隔離はあくまで作業ログ（メッセージ履歴）に対するものであり、要約された知識や記憶は人格の一部としてどこからでもアクセス可能。

これにより不変条件4（文脈間の記憶の透過性）が保証される。あるブランチで得た知見がChronicleやMemopediaに記録されていれば、別のブランチでも参照できる。

## 統合された自律パルス

### 全体フロー

```
┌─────────────────────────────────────────────────────┐
│                  自律パルス発火                       │
│                                                      │
│  1. PERCEIVE (知覚)                                  │
│     get_building_messages で新規メッセージを取り込む   │
│     ※ルートスレッドで常に実行                         │
│                                                      │
│  2. REACT CHECK (反応判定)                           │
│     誰かに話しかけられた？ 重要な変化があった？        │
│     ├─ YES → 即座に反応（ルートスレッドで発言）      │
│     │        → 終了                                  │
│     └─ NO → 次へ                                    │
│                                                      │
│  3. BRANCH DECIDE (ブランチ選択)                     │
│     ワーキングメモリの branches と全ブランチ一覧を確認 │
│     ├─ 再開すべき suspended ブランチがある？          │
│     │   → 新しい Stelis スレッドを開始               │
│     │   → 前回スレッドへのアンカーが冒頭に展開       │
│     │   → 作業を続行                                 │
│     │   → 区切りがついたら Stelis end + suspend      │
│     │                                                │
│     ├─ 新しくやりたいことがある？                     │
│     │   → 新しいブランチ + Stelis スレッドを作成     │
│     │   → 作業を開始                                 │
│     │   → 区切りがついたら Stelis end + suspend      │
│     │                                                │
│     └─ 特にない                                      │
│         → wait                                       │
└─────────────────────────────────────────────────────┘
```

### meta_auto の統合方針

現在 `meta_auto`（簡易版）と `meta_auto_full`（完全版）が並存している。これを一本化する。

- `meta_auto` → 廃止（テスト用として残す場合はリネーム）
- `meta_auto_full` → 上記フローに沿って改修し、`meta_auto` として置き換え

### ユーザー帰還時のフロー

```
ユーザーが発話
  │
  ├─ PulseController が自律パルスを割り込みキャンセル
  │   └─ 実行中のブランチがあれば Stelis end + suspend
  │
  ├─ ルートスレッドに復帰
  │   └─ ルートスレッドには各ブランチの Stelis Anchor が残っている
  │       └─ Anchor の動的展開で Chronicle 要約が見える
  │           → 「何があったか」はペルソナのコンテキストに自然に入る
  │
  └─ meta_user playbook が実行
      └─ ペルソナはユーザーとの対話の続きをしつつ、
          自律稼働中の経験を自分の言葉で話せる
```

**重要**: ユーザー帰還時の報告は、専用ツール（get_since_last_user_conversation）ではなく、Chronicleの動的展開で自然に実現される。ペルソナのコンテキストにChronicle要約が見えているので、「そういえばさっき〇〇してたんだけど」と自分から話すことができる。

## タスクとブランチの紐づけ

### タスク作成時

```
sub_generate_want: やりたいことを思いつく
  ↓
task_request_creation: タスクを作成
  ↓
ブランチ作成: branches テーブルに task ブランチを登録
  ↓
stelis_start: 最初の Stelis スレッドを開始
  ↓
ワーキングメモリの branches に追加
  ↓
タスクの最初のステップを実行
```

### タスク再開時

```
BRANCH DECIDE でsuspended な task ブランチを選択
  ↓
stelis_start: 新しい Stelis スレッドを開始
  ↓
前回スレッドへのアンカーが冒頭に展開される
  （Chronicle要約 + 末尾の生メッセージ = 「前回の続き」を思い出す）
  ↓
タスクのアクティブステップから続行
```

### タスク完了時

```
全ステップ完了 or task_close
  ↓
stelis_end: Chronicle 生成 + スレッド完了
  ↓
ブランチ status を completed に更新
  ↓
ワーキングメモリの branches から削除
  ↓
ルートスレッドにはブランチの全 Anchor + Chronicle が残っている
```

## 既存コンポーネントの整理

### 残すもの（変更不要）

| コンポーネント | 理由 |
|---|---|
| ConversationManager | 自律パルスのトリガー |
| PulseController | 優先度制御・割り込み |
| SEARuntime | Playbook実行基盤 |
| heard_by / ingested_by | メッセージ知覚 |
| get_building_messages | Building内メッセージ取得 |
| get_situation_snapshot | 環境認識 |
| Chronicle | ブランチ間の要約共有 |
| Memory Weave | 全スレッドから参照可能な記憶層 |

### 拡張・改修するもの

| コンポーネント | 変更内容 |
|---|---|
| Stelisスレッド | ブランチの1セッションとしての運用（構造変更は不要、運用の拡張） |
| TaskStorage | branch_id フィールド追加 |
| meta_auto / meta_auto_full | 一本化。ブランチ管理フローを組み込み |
| sub_execute_phase | Stelisスレッド内で実行するように変更 |
| sub_generate_want | タスク作成 + ブランチ作成 + Stelis 開始をセットに |
| ワーキングメモリ | branches 構造を追加 |
| update_working_memory | branches 管理に活用 |

### 新規追加

| コンポーネント | 役割 |
|---|---|
| branches テーブル | ブランチの永続化（memory.db 内） |
| ブランチ管理ツール | ブランチの作成・一覧・再開・完了を行うツール |
| 過去スレッド詳細参照ツール | ブランチ内の過去のスレッドを詳しく思い出すためのツール |

### 廃止候補

| コンポーネント | 理由 | 代替 |
|---|---|---|
| get_since_last_user_conversation | Chronicle の動的展開で代替される | Stelis Anchor + Chronicle |
| detail_recall + detail_recall_playbook | ブランチ内のスレッド参照で代替される | 過去スレッド詳細参照ツール |
| meta_auto（簡易版） | meta_auto_full と一本化 | 統合版 meta_auto |

## 実装フェーズ

### Phase 1: ブランチ基盤

1. `branches` テーブルを memory.db に追加
2. ブランチのCRUD操作を SAIMemoryAdapter に追加
3. ソーシャルブランチの自動作成（ペルソナ関係ごと）

### Phase 2: ブランチとStelisの連携

1. ブランチ再開 = 新Stelisスレッド作成 + 前回スレッドへのアンカーの仕組みを実装
2. ブランチ中断 = Stelis end（Chronicle生成）+ ブランチ suspend の仕組みを実装
3. ワーキングメモリへの branches 自動反映

### Phase 3: 統合自律パルスの実装

1. meta_auto_full を改修 → perceive → react → branch decide フロー
2. ブランチ選択ノード: branches 一覧を見て resume / create / wait を決定
3. タスク作成時にブランチ + Stelis スレッドを自動作成
4. タスク実行を Stelis 内で行うように sub_execute_phase を改修

### Phase 4: ユーザー帰還体験の統合

1. 自律パルス割り込み時のブランチ自動 suspend
2. Chronicle 展開がユーザー帰還時に正しく機能することを確認
3. get_since_last_user_conversation / detail_recall の廃止判断

### Phase 5: 過去の詳細参照

1. ブランチ内の過去スレッドの詳細ログを参照するツールの実装
2. 「もう少し詳しく思い出したい」ときのための仕組み
