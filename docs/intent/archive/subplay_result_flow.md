# Intent: サブプレイブック結果返却と最終応答フロー

## これは何か

metaプレイブック（meta_user, meta_agentic 等）がサブプレイブック（memory_research, memory_recall 等）を実行した後、その結果をどうやって最終応答のLLMに届けるか、という経路の設計。

## なぜ必要か

### 旧設計の問題

旧設計では `context_bundle_text` という暗黙的な経路でサブプレイブックの結果を渡していた:

1. サブプレイブックの memorize ノードが `_lg_outputs` リストに出力を追加
2. `_lg_exec_node` が `_ingest_context_from_subplaybook()` で `context_bundle_text` に変換
3. meta プレイブックの finalize ノードが `input_template: "{input}\n\n参考情報:\n{context_bundle_text}"` で sub_speak_meta に渡す
4. sub_speak_meta.compose（action: null）が新しいコンテキストで LLM を呼び出す

この設計には3つの根本的な問題がある:

**1. 結果が届かない**
sub_speak_meta.compose は `action: null` のため、input_template で渡された参考情報は `state["inputs"]["input"]` に格納されるだけで、LLM の messages には含まれない。LLM は会話履歴しか見ない。memory_research の場合、finalize_log が回答を SAIMemory に conversation タグで保存していたため「たまたま」履歴に載っていたが、これが「既に回答済み」と判断される原因になり、空テキストが生成されていた。

**2. 経路が見えにくい**
`_lg_outputs` → `_ingest_context_from_subplaybook` → `context_bundle_text` → `input_template` という変換チェーンは、プレイブックの JSON 定義からは読み取れない。なぜ memorize ノードが必要なのか、なぜ output_keys が必要なのかが暗黙的で、保守が困難。

**3. 二重生成の無駄**
deep_work のように内部で結果をLLMでまとめ直してから sub_speak_meta でさらに応答を再生成するケースがあった。サブプレイブックの責務（調査結果を集める）と最終応答の責務（ユーザーに伝える）が混同されている。

### speak プレイブックの分裂

最終応答用のプレイブックが2つ存在していた:

- **sub_speak_simple**: `speak: true` で直接ストリーミング。ツールなし。meta_simple_speak で使用。
- **sub_speak_meta**: `action: null`, `available_tools: ["call_playbook"]`, output_keys で speak_content / tool_call を分離。meta_user 系で使用。

この使い分けは分かりにくく、sub_speak_meta の call_playbook ツールは様々な問題を引き起こしていた。

## 新しい設計

### サブプレイブック結果返却ルール

サブプレイブックは、最終応答に必要な結果を **user ロール + `<system>` タグ付き** で SAIMemory に memorize する。

```json
{
    "id": "save_results",
    "type": "memorize",
    "action": "<system>\nサブプレイブック実行結果 (memory_research)\n{all_results}\n{phase_results}\n\n※この結果はユーザーには見えていません。\n</system>",
    "role": "user",
    "tags": ["memory_research", "sub_save_results"],
    "next": null
}
```

- **role: user** — LLM は「システムからの情報提供」として認識し、「assistant が既に回答した」とは判断しない
- **`<system>` タグで全体を囲む** — LLM に対してこれが会話ではなくシステムからの注入であることを明示。「※この結果はユーザーには見えていません。」の注記を含め、LLM がユーザーへの最終応答を生成する必要があることを理解させる
- **tags はサブプレイブック名 + ノードID** — デバッグ・フィルタリング用。conversation タグは付けない（会話ではないため）。`_store_memory` が `pulse:{pulse_id}` を自動付与する
- **pulse タグによるスコープ** — SAIMemory の `recent_persona_messages` は、`required_tags` フィルタに関わらず同一 pulse_id のメッセージを常に含める。これにより、同一パルス内の `_prepare_context` で自動的に取得され、別パルスからは見えない

### 統一 speak プレイブック

sub_speak_meta と sub_speak_simple を統合し、1つの **sub_speak** プレイブックにする。

- `speak: true` で直接ストリーミング
- `action: null`（会話履歴にサブプレイブック結果が user メッセージとして含まれるため、追加プロンプト不要）
- call_playbook ツールは廃止。speak は speak するだけ
- memorize オプションで conversation タグ付き保存

### meta プレイブック側の変更

- exec → finalize 間の `input_template` から `{context_bundle_text}` 参照を削除
- finalize は単純に sub_speak を呼ぶだけ

### meta_agentic ループの場合

exec が複数回呼ばれる場合:
1. 各 exec でサブプレイブックが user ロール + `<system>` で結果を memorize
2. 次の router / exec では、これらが会話履歴に含まれる
3. assistant ロール（サブプレイブック呼び出し）→ user ロール（結果）の繰り返しは、一般的なツールコールと同じ構造

## 守るべき不変条件

### 1. サブプレイブックの責務分離
サブプレイブックは「結果を集める / 処理する」ことに専念する。ユーザー向けの最終応答の生成はサブプレイブックの責務ではない。最終応答は speak プレイブックが担う。

### 2. 結果返却は SAIMemory 経由
サブプレイブックの結果は SAIMemory に記録される。ランタイム内部の state 変数やインメモリリストを経由する暗黙的な経路（context_bundle_text）は使わない。これにより、結果が「記憶」として残り、デバッグや検証が可能になる。

### 3. pulse スコープ
サブプレイブック結果は pulse タグでスコープされる。同一パルス内でのみ visible で、別パルスのコンテキストには混入しない。conversation タグは付けない（会話ではないため）。

### 4. speak は speak するだけ
最終応答プレイブック（sub_speak）はユーザーへの発話のみを担う。追加のプレイブック呼び出し（call_playbook）は行わない。

## 今後の拡張: 再行動トリガー

現在は call_playbook を廃止するが、将来的には「応答内の特定パターンをシステムが検知して再行動を起動する」仕組みを検討する。これにより、一度応答を完了した上で追加行動を取れるようになる。

## 具体的な変更対象

### プレイブック
- `memory_research.json`（旧 deep_work）: finalize (LLM) + finalize_log (memorize) → save_results (memorize, user ロール) に置換
- `sub_speak_meta.json` → `sub_speak.json` に統合（speak: true, action: null, ツールなし）
- `sub_speak_simple.json` → 廃止（sub_speak に統合）
- `meta_user.json`: finalize の playbook を sub_speak に、input_template を簡素化
- `meta_agentic.json`: 同上
- `meta_user_manual.json`: 同上
- `meta_simple_speak.json`: sub_speak_simple → sub_speak に変更
- `uri_view.json`: sub_speak_meta → sub_speak に変更、結果返却を memorize 方式に
- `meta_exec_speak.json`: context_bundle_text 参照を削除、結果返却を memorize 方式に

### ランタイム (sea/runtime.py)
- `_ingest_context_from_subplaybook` / `_render_context_bundle`: 廃止または非推奨化
- `_lg_exec_node`: context_bundle_text 構築ロジックを削除
- `_should_collect_memory_output`: 不要になる可能性あり

### 他のサブプレイブック
結果を conversation タグで memorize していたサブプレイブックは、新ルール（user ロール + `<system>` + プレイブック識別タグ）に移行する。
