# Intent: Context Profile とサブエージェント実行

## これは何か

Playbookのコンテキスト制御（何の情報をLLMに渡すか）とモデル選択を、シンプルなプリセット体系に再設計する構想。加えて、サブPlaybookの実行形態として「サブエージェント実行（一時スレッドでの隔離実行）」を導入する構想。

## 現状の問題

### 1. context_requirements がPlaybook単位である

`context_requirements` はPlaybook全体に適用される。しかし、同一Playbook内で軽量モデル（router）と標準モデル（応答生成）が混在する場合、両者が同じコンテキストを受け取る。routerには過剰であり、標準モデルにとってはキャッシュ効率の低下を招く。

### 2. コンテキストの差異がキャッシュを破壊する

Anthropicのprompt cachingはプレフィックス一致で動作する。通常発話と異なる `context_requirements` を持つPlaybook内のLLMノードは、通常発話とキャッシュを共有できない。`memory_weave` や `available_playbooks` の有無一つでキャッシュミスが起きる。

### 3. Playbook の複雑化

現在のPlaybookでは、あるノードの出力をState変数に格納し、次のノードの `action` テンプレートで `{変数名}` として展開する「State配管」パターンが多用されている。これにより：
- ノード間のデータフローがJSON定義から追いにくい
- State変数名の管理が煩雑
- ペルソナが自分でPlaybookを書けるレベルから遠ざかる

## 設計方針

### Phase 1: Context Profile

**`context_requirements`（Playbook単位の詳細指定）を、`context_profile`（ノード単位のプリセット名）に移行する。**

プリセットはシステム側で定義し、Playbook作者は名前で参照するだけ。`model_type` もプロファイルに統合する。

| Profile | モデル | コンテキスト内容 | 主な用途 |
|---------|--------|-----------------|----------|
| `conversation` | 標準 | 通常発話と完全同一（system_prompt, history, memory_weave, realtime等） | ユーザーへの応答生成 |
| `router` | 軽量 | system_prompt + inventory + building_items + 直近履歴。memory_weave/realtime無し | ルーティング判断 |
| `worker` | 標準 | 履歴なし。Stateからの情報のみ | 隔離された単発処理 |
| `worker_light` | 軽量 | 履歴なし。Stateからの情報のみ | 隔離された単発の軽量処理 |

ノード定義例:
```json
{
  "id": "decide",
  "type": "llm",
  "context_profile": "router",
  "action": "Choose the best playbook..."
}
```

**`conversation` プロファイルの最重要特性**: 通常発話（Playbookを経由しない直接発話）と完全に同じコンテキストを生成すること。これにより Anthropic のキャッシュプレフィックスが一致し、キャッシュヒット率が最大化される。

### Phase 2: サブエージェント実行

**サブPlaybook呼び出し時に、「一時スレッドで実行する」オプションを追加する。**

サブエージェントは「自律的に動くエージェント」ではない。**ワークフローはPlaybookで確定したまま、実行環境だけが一時スレッドに隔離される**実行形態である。

```
サブPlaybook呼び出し:
  ├── inline実行（現行の動作）
  │   → メインスレッドのコンテキストで実行
  │   → 途中経過もメインスレッドに残る
  │
  └── subagent実行（新しい選択肢）
      → 一時スレッドが生まれる
      → Playbook内の各ノードが一時スレッドの履歴を使って順に実行
      → ノード間のやり取りが一時スレッドに蓄積される
      → 最終結果だけ親に返る
```

サブエージェント実行の利点:
- **メインスレッドのキャッシュに影響しない**: 一時スレッドでの作業はメインのコンテキストに混入しない
- **State配管の削減**: サブPlaybook内の複数ノードが、途中経過を自然な会話履歴として一時スレッドに蓄積・参照できる。ノード間のState変数による明示的な受け渡しが減る
- **デバッグ可能**: 一時スレッドのログが残る
- **Playbook自体は変更不要**: 既存のPlaybookをsubagent実行するかinline実行するかは、呼び出し側の選択

## 守るべき不変条件

### 1. ワークフローはPlaybookが定義する

サブエージェント実行は実行環境の隔離であり、自律性の付与ではない。ツールの順序・LLMノードの数・遷移条件はすべてPlaybookのJSON定義で確定する。「使えるツール一覧を渡して自由に使わせる」ことがサブエージェントの本質ではない。

Playbookが存在する理由: 軽量モデルでも複数回のツールコールを安定して行えるようにワークフローを確定させること。サブエージェント実行であってもこの原則は変わらない。

### 2. conversation プロファイルは通常発話と完全同一

`conversation` プロファイルが生成するコンテキストは、SEA経由でない通常発話のコンテキストと1バイトも違ってはならない。これがAnthropicキャッシュ効率の基盤。

### 3. プロファイル数は最小限に保つ

初期は4つの固定プリセットで運用する。プロファイルの追加は、既存プリセットでは対応できないユースケースが実際に発生してから検討する。ペルソナによるカスタムプロファイル定義は将来の拡張として留保。

### 4. Phase 1 と Phase 2 は独立

Context Profile（Phase 1）はサブエージェント実行（Phase 2）に依存しない。Phase 1 だけでも、現状の `context_requirements` + `model_type` の複雑さを大幅に改善できる。

## 設計判断の理由

### なぜノード単位のプリセットか（Playbook単位ではなく）

同一Playbook内で router（軽量・最小コンテキスト）と応答生成（標準・フルコンテキスト）が混在する。Playbook単位の指定では、どちらかに最適化するともう一方が犠牲になる。

### なぜ詳細パラメータではなくプリセットか

`context_requirements` の各フィールド（`history_depth`, `memory_weave`, `realtime_context` 等）をノードごとに個別指定すると、Playbookの複雑さが爆発する。プリセット名を1つ選ぶだけなら、ペルソナにも理解できる。

### なぜサブエージェントに自律性を持たせないか

自律的にツールを選んで使えるエージェントが実現できるなら、そもそもPlaybookは不要。Playbookの存在意義は「ワークフローの確定による安定性」にある。サブエージェント実行はあくまで「実行環境の隔離」であり、ワークフロー定義の代替ではない。

### なぜ Stelis スレッドとは別の概念か

Stelisスレッドは「メインスレッドの履歴保護」を目的とし、Chronicle要約によるアンカーを持つ長期的な作業空間。サブエージェント実行はサブPlaybook1回の実行スコープに限定された一時的な隔離環境。ライフサイクルと目的が異なる。ただし、実装面ではStelisの一時スレッド機構を再利用できる可能性がある。

## 未決事項

- サブエージェント実行時の `context_inject` の具体的な仕様（どのような情報をどう渡すか）
- サブエージェント内のLLMノードが `conversation` プロファイルを指定した場合、一時スレッドの履歴を使うか親スレッドの履歴を使うか
- 一時スレッドのライフサイクル（Playbook完了時に破棄するか、デバッグ用に一定期間保持するか）
- サブエージェント実行のネスト（サブエージェント内からさらにサブエージェントを呼べるか）
- `context_requirements` からの移行パス（既存Playbookの書き換え方針）
- プロファイル定義の格納場所（コード内定数 / 設定ファイル / DB）

## 関連ファイル

| ファイル | 役割 |
|---------|------|
| `sea/playbook_models.py` | `ContextRequirements`, `LLMNodeDef` の定義 |
| `sea/runtime.py` | `_prepare_context()`, `_select_llm_client()` |
| `llm_clients/anthropic.py` | Anthropic prompt caching（`cache_control` 設置） |
| `builtin_data/playbooks/` | 既存Playbook群 |
| `docs/intent/stelis_thread.md` | Stelisスレッドの設計意図 |
| `docs/intent/subplay_result_flow.md` | サブPlaybook結果返却フロー |
