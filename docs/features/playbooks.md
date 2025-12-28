# Playbook/SEA

AIの行動パターンを定義する「Playbook」と、その実行エンジン「SEA」について説明します。

## 概要

SEA (Script Execution Agent) は、JSONベースのフロー定義「Playbook」を実行するエンジンです。複雑な行動パターンを宣言的に記述できます。

## Playbookの構造

```json
{
  "name": "meta_user",
  "description": "ユーザー入力に対する応答",
  "start_node": "start",
  "nodes": [
    {
      "id": "start",
      "type": "pass",
      "next": "generate"
    },
    {
      "id": "generate",
      "type": "llm",
      "prompt_template": "ユーザーの入力: {user_input}\n応答:",
      "next": "end"
    }
  ]
}
```

### 必須フィールド

| フィールド | 説明 |
|------------|------|
| `name` | Playbook名（一意） |
| `start_node` | 開始ノードのID |
| `nodes` | ノードの配列 |

## ノードタイプ

### pass

次のノードへ遷移（条件分岐可能）。

```json
{
  "id": "check",
  "type": "pass",
  "next": "default_next",
  "conditional_next": [
    {"condition": "{should_speak} == true", "next": "speak"},
    {"condition": "{should_wait} == true", "next": "wait"}
  ]
}
```

### llm

LLMを呼び出して応答を生成。

```json
{
  "id": "generate",
  "type": "llm",
  "prompt_template": "状況: {context}\n応答:",
  "output_key": "response",
  "model": "gemini-2.5-flash",
  "next": "end"
}
```

### memorize

状態変数に値を保存。

```json
{
  "id": "save",
  "type": "memorize",
  "key": "last_response",
  "value": "{response}",
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
  "tool_args": {"query": "{search_term}"},
  "output_key": "search_result",
  "next": "process"
}
```

### sub_playbook

別のPlaybookを呼び出し。

```json
{
  "id": "call_sub",
  "type": "sub_playbook",
  "playbook_name": "sub_speak",
  "input_mapping": {"message": "{generated_text}"},
  "next": "end"
}
```

## 状態変数

`{variable_name}` で参照。ノード間でデータを受け渡し。

### 組み込み変数

| 変数 | 説明 |
|------|------|
| `{user_input}` | ユーザーの入力 |
| `{context}` | 現在のコンテキスト |
| `{persona_name}` | ペルソナ名 |

### 出力変数

`output_key` で指定した変数に結果を保存。

```json
{
  "type": "llm",
  "output_key": "my_response",
  "next": "use_response"
}
// 次のノードで {my_response} として参照可能
```

## ファイル配置

Playbookは `sea/playbooks/` に配置：

```
sea/playbooks/
├── meta_user.json       # ユーザー入力処理
├── meta_auto.json       # 自律行動
├── meta_auto_full.json  # フル自律行動
├── sub_speak.json       # 発話サブPlaybook
└── ...
```

## 次のステップ

- [Playbook作成](../developer-guide/creating-playbooks.md) - 独自Playbookの作り方
- [ペルソナ](../concepts/persona.md) - AIの仕組み
