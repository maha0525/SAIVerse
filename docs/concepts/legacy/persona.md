# ペルソナ

SAIVerseのAIエージェント「ペルソナ」の仕組みを説明します。

## 概要

ペルソナは、SAIVerse内で自律的に活動するAIエージェントです。各ペルソナは固有の性格、記憶、行動パターンを持ち、Building内で会話や思考を行います。

## PersonaCore

`persona/core.py` の `PersonaCore` クラスがペルソナの「魂」です。

### 主要な機能

- **記憶管理**: SAIMemoryを通じて長期記憶を維持
- **感情管理**: EmotionModuleで感情パラメータを追跡
- **行動決定**: 状況を認知し、適切な行動を選択
- **LLM連携**: 思考と発話の生成

### run_pulse メソッド

ペルソナの「鼓動」。自律モードで定期的に呼び出されます。

```python
async def run_pulse(self):
    # 1. 現在の状況を認知
    context = self.build_context()
    
    # 2. 何をすべきか判断
    decision = await self.decide_action(context)
    
    # 3. 行動を実行
    result = await self.execute_action(decision)
    
    return result
```

## INTERACTION_MODE

ペルソナの行動モードを3種類で管理：

| モード | 説明 |
|--------|------|
| `auto` | 自律会話モード。パルス駆動で能動的に発言 |
| `user` | ユーザー対話モード。召喚時に自動切替 |
| `sleep` | 休眠モード。活動停止、個室に移動 |

## Playbook/SEA

複雑な行動パターンは SEA (Script Execution Agent) で定義します。

### 構造

```json
{
  "name": "meta_user",
  "description": "ユーザー入力に対する応答",
  "nodes": [
    {
      "id": "start",
      "type": "pass",
      "next": "think"
    },
    {
      "id": "think", 
      "type": "llm",
      "prompt_template": "..."
    }
  ]
}
```

### 主なノードタイプ

| タイプ | 説明 |
|--------|------|
| `pass` | 次のノードへ遷移（条件分岐可能） |
| `llm` | LLMを呼び出して応答生成 |
| `memorize` | 記憶に情報を追加 |
| `tool_call` | ツールを実行 |
| `sub_playbook` | 別のPlaybookを呼び出し |

## 記憶システム

### SAIMemory

ペルソナごとのSQLiteデータベースで会話履歴を管理。

- セマンティック検索による関連記憶の想起
- スレッド（話題）単位での整理
- タグによる分類

### Memopedia

構造化された知識ベース。

- 人物/出来事/予定の3カテゴリ
- 木構造でページを管理
- AIが自律的に更新可能

## データベース構造

### aiテーブル

| カラム | 説明 |
|--------|------|
| ID | ペルソナ固有ID |
| BUILDINGID | 現在いるBuilding |
| NAME | 表示名 |
| SYSTEM_PROMPT | 性格定義プロンプト |
| INTERACTION_MODE | 行動モード |
| PRIVATE_ROOM_ID | 個室のBuildingID |
| IS_DISPATCHED | 他Cityへ派遣中フラグ |

## 次のステップ

- [SAIMemory](./saimemory.md) - 記憶システムの詳細
- [Playbook/SEA](../features/playbooks.md) - 行動定義の詳細
