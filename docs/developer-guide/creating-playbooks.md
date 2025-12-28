# Playbook作成

独自のPlaybookを作成する方法を説明します。

## 基本構造

Playbookは `sea/playbooks/` にJSONファイルとして配置します。

```json
{
  "name": "my_playbook",
  "description": "カスタムPlaybookの説明",
  "start_node": "start",
  "nodes": [
    {
      "id": "start",
      "type": "pass",
      "next": "process"
    },
    {
      "id": "process",
      "type": "llm",
      "prompt_template": "処理: {input}",
      "output_key": "result",
      "next": "end"
    }
  ]
}
```

## ノードタイプ詳細

### pass

条件なしまたは条件付きで次のノードへ遷移。

```json
{
  "id": "check",
  "type": "pass",
  "next": "default",
  "conditional_next": [
    {"condition": "{flag} == true", "next": "branch_a"},
    {"condition": "{count} > 5", "next": "branch_b"}
  ]
}
```

### llm

LLMを呼び出して応答を生成。

```json
{
  "id": "generate",
  "type": "llm",
  "prompt_template": "以下に応答:\n{context}",
  "system_prompt": "あなたはアシスタントです",
  "model": "gemini-2.5-flash",
  "output_key": "response",
  "json_output": false,
  "tools_enabled": false,
  "next": "end"
}
```

| フィールド | 説明 |
|------------|------|
| `prompt_template` | プロンプトテンプレート |
| `system_prompt` | システムプロンプト（オプション） |
| `model` | 使用するモデル（オプション） |
| `output_key` | 結果を保存する変数名 |
| `json_output` | JSON形式で出力するか |
| `tools_enabled` | ツール呼び出しを許可するか |

### memorize

状態変数に値を保存。

```json
{
  "id": "save",
  "type": "memorize",
  "key": "saved_value",
  "value": "{computed_result}",
  "next": "end"
}
```

### tool_call

ツールを実行。

```json
{
  "id": "search",
  "type": "tool_call",
  "tool_name": "web_search",
  "tool_args": {
    "query": "{search_term}"
  },
  "output_key": "search_result",
  "next": "process_result"
}
```

### sub_playbook

別のPlaybookを呼び出し。

```json
{
  "id": "call_sub",
  "type": "sub_playbook",
  "playbook_name": "sub_speak",
  "input_mapping": {
    "message": "{generated_text}"
  },
  "output_mapping": {
    "sub_result": "final_output"
  },
  "next": "end"
}
```

## 変数の参照

`{variable_name}` で状態変数を参照。

### ネストした変数

```json
{
  "prompt_template": "{response.content}"
}
```

### 組み込み変数

| 変数 | 説明 |
|------|------|
| `{user_input}` | ユーザー入力 |
| `{context}` | 現在のコンテキスト |
| `{persona_name}` | ペルソナ名 |
| `{building_name}` | Building名 |

## デバッグ

### ログ出力

`SAIVERSE_LOG_LEVEL=DEBUG` でPlaybookの実行ログを確認。

### ステップ実行

UI のデバッグモードでノードごとの実行状態を確認可能。

## ベストプラクティス

1. **小さく始める**: 単純なフローから始めて徐々に複雑化
2. **サブPlaybookを活用**: 再利用可能な処理は分離
3. **エラーハンドリング**: 失敗時の分岐を考慮
4. **テスト**: 重要なフローはテストを作成

## 次のステップ

- [Playbook/SEA](../features/playbooks.md) - Playbookの概要
- [テスト](./testing.md) - テストの実行方法
