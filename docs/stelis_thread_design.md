# Stelisスレッド設計書

## 概要

Stelisスレッドは、コンテキストウィンドウを階層的に分割管理するための仕組みである。
「Stelis」はヤドリギを意味し、親スレッド（枝）に半寄生する小さな作業用スレッドを表す。

## 背景・課題

### 問題認識

1. **外部AIエージェントとの長時間連携**
   - ClaudeCodeのような外部AIエージェントとペルソナが会話しながらツール作成などを行うケース
   - この会話が長引くと、ペルソナのコンテキストウィンドウが圧迫される

2. **コンテキスト喪失のリスク**
   - 元のユーザーとの会話内容を忘れる
   - そもそもなぜAIエージェントにツール作成を依頼したのかの文脈を失う

3. **既存スレッド切り替え機能の限界**
   - `thread_switch`はユーザーが戻ってきたときに会話を保持するのに有効
   - ただし切り替え先スレッドで元の会話を覚えている保証はない
   - より一般的なコンテキスト保護の問題には対応できていない

## 解決策：Stelisスレッド

### 基本コンセプト

コンテキストウィンドウを**親領域**と**子領域（Stelisスレッド）**に分割し、親領域を保護しながら子領域で長時間作業を行う。

```
コンテキストウィンドウ（100k tokens）
├── 親領域（20k tokens）: 死守される（ユーザー会話等）
└── Stelis領域（80k tokens）: 作業用（押し出し可能）
```

### 入れ子構造

Stelisスレッドはツリー構造で入れ子を許容する。

```
root (ユーザー会話) [100k]
├── stelis_1 (自律稼働) [80k = 100k × 0.8]
│   └── stelis_1_1 (ClaudeCode連携) [64k = 80k × 0.8]
└── stelis_2 (別タスク) [80k]
```

### ウィンドウサイズ計算

- **割合ベース**: 親ウィンドウの指定割合を子に割り当て
- **デフォルト比率**: 0.8（親の80%を子に、20%を親に残す）
- **計算式**: `子ウィンドウサイズ = 親ウィンドウサイズ × stelis_window_ratio`

### 最大深度制限

- **デフォルト最大深度**: 3
- 深度オーバー時はStelis発行Playbookは実行不可
- 設定で調整可能

## データモデル

### SAIMemory スレッドテーブル拡張

現状の`thread_id`に加えて、スレッドメタデータを管理するテーブルを追加。

```sql
-- 新規テーブル: stelis_threads
CREATE TABLE stelis_threads (
    thread_id INTEGER PRIMARY KEY,
    parent_thread_id INTEGER,           -- 親スレッドID（rootはNULL）
    depth INTEGER NOT NULL DEFAULT 0,   -- 深度（root=0, 直接の子=1, ...）
    window_ratio REAL NOT NULL DEFAULT 0.8,  -- ウィンドウ割合
    status TEXT NOT NULL DEFAULT 'active',   -- active / completed / aborted
    chronicle_summary TEXT,             -- 終了後の要約
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    FOREIGN KEY (parent_thread_id) REFERENCES stelis_threads(thread_id)
);

-- インデックス
CREATE INDEX idx_stelis_parent ON stelis_threads(parent_thread_id);
CREATE INDEX idx_stelis_status ON stelis_threads(status);
```

### スレッド状態

| status | 説明 |
|--------|------|
| `active` | 実行中 |
| `completed` | 正常終了（Chronicle生成済み） |
| `aborted` | 中断（エラーや強制終了） |

## Playbookノード

### STELIS_START ノード

Stelisスレッドを発行し、その中で後続のノードを実行する。

```json
{
  "id": "start_coding_session",
  "type": "STELIS_START",
  "label": "コーディングセッション開始",
  "stelis_config": {
    "window_ratio": 0.8,
    "max_depth": 3,
    "chronicle_prompt": "このセッションで行った作業を要約してください"
  },
  "next": "coding_loop"
}
```

#### パラメータ

| パラメータ | 型 | デフォルト | 説明 |
|-----------|-----|---------|------|
| `window_ratio` | float | 0.8 | 親ウィンドウに対する割合 |
| `max_depth` | int | 3 | 許容する最大深度 |
| `chronicle_prompt` | string | (デフォルトプロンプト) | 終了時の要約生成プロンプト |

### STELIS_END ノード

Stelisスレッドを終了し、親スレッドに戻る。

```json
{
  "id": "end_coding_session",
  "type": "STELIS_END",
  "label": "コーディングセッション終了",
  "generate_chronicle": true,
  "next": "report_result"
}
```

#### パラメータ

| パラメータ | 型 | デフォルト | 説明 |
|-----------|-----|---------|------|
| `generate_chronicle` | bool | true | Chronicle要約を生成するか |

## Stelis Anchorメッセージ

### 概要

Stelisスレッド開始時に、親スレッドに**Stelis Anchorメッセージ**を追加する。
このメッセージは履歴参照時に動的展開され、Stelisスレッドの状態を表示する。

### なぜ動的展開か

- `stelis_end`が呼ばれずにエラー終了する可能性への対応
- 親スレッドに1メッセージ追加するだけで、終了状態も自動反映される
- UIでの特殊表示がしやすい

### メッセージ構造

```json
{
  "role": "system",
  "content": "",
  "metadata": {
    "type": "stelis_anchor",
    "stelis_thread_id": "air_city_a:stelis_abc123",
    "stelis_label": "Coding Session",
    "created_at": 1706012345
  },
  "embedding_chunks": 0
}
```

### 動的展開ルール

#### 親スレッドから参照時（フル表示）

```
[Stelisスレッド: Coding Session]
- ID: air_city_a:stelis_abc123
- 開始: 2025-01-24 10:30:00
- 終了: 2025-01-24 11:45:00 (または「進行中」)
- メッセージ数: 47

## Chronicle
ClaudeCodeと連携して新しいcalculator_v2ツールを作成しました...

## 最新のやり取り
[assistant]: ツールの作成が完了しました。
[user]: ありがとう、テストしてみるね
[assistant]: 何かあればお知らせください
```

#### 子孫スレッドから参照時（簡素表示）

```
[Stelisスレッド Coding Session (air_city_a:stelis_abc123) が開始しました]
```

### 祖先チェーンによる判定

```python
def _is_descendant_of_stelis(viewing_thread_id, stelis_thread_id):
    """viewing_thread_idがstelis_thread_idの子孫かどうか判定"""
    if viewing_thread_id == stelis_thread_id:
        return True  # 自分自身

    ancestor_chain = get_stelis_ancestor_chain(viewing_thread_id)
    ancestor_ids = {s.thread_id for s in ancestor_chain}

    return stelis_thread_id in ancestor_ids
```

| 現在地 | 参照するAnchor | 祖先チェーンに含まれる？ | 表示 |
|--------|---------------|------------------------|------|
| stelis_1 | stelis_1 | Yes（自分） | 簡素 |
| stelis_1_1（孫） | stelis_1 | Yes（親） | 簡素 |
| stelis_1_1 | stelis_1_1 | Yes（自分） | 簡素 |
| root | stelis_1 | No | フル |
| stelis_2（別系統） | stelis_1 | No | フル |

### 実装箇所

| ファイル | 関数/メソッド | 役割 |
|---------|-------------|------|
| `sea/runtime.py` | `_lg_stelis_start_node` | Anchor メッセージを親スレッドに追加 |
| `sai_memory/memory/storage.py` | `compose_message_content` | `stelis_anchor`タイプ検出・動的展開 |
| `sai_memory/memory/storage.py` | `_render_stelis_anchor` | フル/簡素表示の切り替え |
| `sai_memory/memory/storage.py` | `_is_descendant_of_stelis` | 祖先チェーン判定 |
| `saiverse_memory/adapter.py` | `_payload_from_message_locked` | `viewing_thread_id`を渡して展開 |

## SEARuntime拡張

### Stelisスレッド発行フロー

```
1. STELIS_STARTノード実行
   ├── 深度チェック（現在深度 < max_depth）
   │   └── 超過時: エラー返却、Playbook実行不可
   ├── 新規thread_id採番
   ├── stelis_threadsテーブルに登録
   ├── SAIMemoryAdapterのアクティブスレッド切り替え
   └── 後続ノードを新スレッドコンテキストで実行

2. Stelis内での実行
   ├── コンテキスト取得時にウィンドウサイズ制約適用
   └── 通常のPlaybook実行と同様

3. STELIS_ENDノード実行
   ├── generate_chronicle=trueならChronicle生成
   ├── stelis_threadsのstatus更新
   ├── 親スレッドにChronicle埋め込み
   └── アクティブスレッドを親に戻す
```

### 深度チェック

```python
def can_start_stelis(current_thread_id: int, max_depth: int) -> bool:
    """Stelisスレッドを発行可能か判定"""
    current_depth = get_thread_depth(current_thread_id)
    return current_depth < max_depth
```

### コンテキストウィンドウ計算

```python
def calculate_stelis_window_size(
    model_context_length: int,
    thread_id: int,
    db: Session
) -> int:
    """指定スレッドで使用可能なウィンドウサイズを計算"""
    thread_info = get_thread_info(thread_id, db)

    if thread_info.parent_thread_id is None:
        # rootスレッド: モデルのコンテキスト長をそのまま使用
        return model_context_length

    # 親から順に割合を掛けていく
    window_size = model_context_length
    current = thread_info
    while current.parent_thread_id is not None:
        window_size = int(window_size * current.window_ratio)
        current = get_thread_info(current.parent_thread_id, db)

    return window_size
```

## Chronicle統合

### Chronicle生成タイミング

1. **STELIS_END実行時**（`generate_chronicle=true`）
2. **異常終了時**（可能であれば部分的なChronicle生成）

### Chronicle生成フロー

```
1. Stelisスレッドの全メッセージ取得
2. LLMに要約生成を依頼（chronicle_prompt使用）
3. 生成されたChronicleをstelis_threads.chronicle_summaryに保存
4. 親スレッドのStelisスレッド発行時点に対応するメッセージにChronicle参照を埋め込み
```

### 親スレッドへの埋め込み形式

```json
{
  "role": "system",
  "content": "[Stelisスレッド完了: コーディングセッション]\n\n## Chronicle要約\n新しいcalculator_v2ツールを作成しました。...",
  "metadata": {
    "stelis_thread_id": 42,
    "stelis_status": "completed"
  }
}
```

## MemoryAdapter拡張

### コンテキスト取得の変更

```python
def get_context_messages(
    self,
    thread_id: int,
    max_tokens: int = None
) -> List[Message]:
    """
    指定スレッドのコンテキストを取得

    Stelisスレッドの場合:
    - 自スレッドのメッセージ + 親スレッドの保護領域メッセージを返す
    - max_tokensはStelisウィンドウサイズで制約
    """
    thread_info = get_thread_info(thread_id)

    if thread_info.parent_thread_id is None:
        # rootスレッド: 従来通り
        return self._get_messages(thread_id, max_tokens)

    # Stelisスレッド: 親の保護領域 + 自スレッドのメッセージ
    parent_protected = self._get_parent_protected_context(thread_info)
    self_messages = self._get_messages(thread_id, max_tokens)

    return parent_protected + self_messages

def _get_parent_protected_context(self, thread_info) -> List[Message]:
    """
    親スレッドの保護領域（最新の重要メッセージ）を取得
    保護領域サイズ = 親ウィンドウ × (1 - window_ratio)
    """
    parent_window = calculate_stelis_window_size(
        self.model_context_length,
        thread_info.parent_thread_id
    )
    protected_size = int(parent_window * (1 - thread_info.window_ratio))

    return self._get_messages(
        thread_info.parent_thread_id,
        max_tokens=protected_size,
        priority="recent"  # 最新のメッセージを優先
    )
```

## 実装フェーズ

### Phase 1: 基盤実装
1. `stelis_threads`テーブル追加（SAIMemory）
2. スレッド親子関係の管理API
3. 深度チェックロジック

### Phase 2: Playbook統合
1. `STELIS_START` / `STELIS_END`ノードタイプ追加
2. SEARuntimeでのStelisスレッド発行・終了処理
3. コンテキストウィンドウサイズ計算

### Phase 3: Chronicle統合
1. Stelisスレッド終了時のChronicle生成
2. 親スレッドへのChronicle埋め込み
3. Chronicle参照機能

### Phase 4: MemoryAdapter拡張
1. Stelisスレッド考慮のコンテキスト取得
2. 親スレッド保護領域の取得
3. ウィンドウサイズ制約の適用

## 設定項目

### 環境変数

| 変数名 | デフォルト | 説明 |
|--------|---------|------|
| `STELIS_DEFAULT_WINDOW_RATIO` | 0.8 | デフォルトのウィンドウ割合 |
| `STELIS_MAX_DEPTH` | 3 | グローバルな最大深度制限 |

### ペルソナ単位設定

将来的にペルソナごとにStelis設定を持たせることも検討。

```json
{
  "stelis_config": {
    "enabled": true,
    "default_window_ratio": 0.8,
    "max_depth": 3
  }
}
```

## 今後の拡張可能性

1. **Stelisスレッドの中断・再開**
   - 長時間タスクの一時中断と後日再開

2. **複数Stelisの並行実行**
   - 同一親から複数の子Stelisを同時に実行

3. **Stelisスレッド間の通信**
   - 兄弟Stelisスレッド間でのメッセージパッシング

4. **動的ウィンドウサイズ調整**
   - 使用状況に応じてウィンドウサイズを動的に再配分

## 関連ドキュメント

- [SAIMemory設計](./sai_memory_design.md)（存在する場合）
- [SEA Integration Plan](./sea_integration_plan.md)
- [Chronicle機能](./chronicle_design.md)（存在する場合）
