# ツールの追加

SAIVerseに新しいツールを追加する方法を説明します。

## 概要

ツールは `tools/defs/` ディレクトリに Python ファイルとして配置します。Function Calling の仕様に従ってツールを定義すると、ペルソナがそのツールを呼び出せるようになります。

## 基本的な構造

```python
# tools/defs/my_tool.py

from tools import register_tool

@register_tool
def my_tool(param1: str, param2: int = 10) -> str:
    """
    ツールの説明（LLMに提示される）
    
    Args:
        param1: パラメータ1の説明
        param2: パラメータ2の説明（デフォルト: 10）
    
    Returns:
        結果の説明
    """
    # 実装
    result = f"処理結果: {param1}, {param2}"
    return result
```

## 詳細な定義

より詳細な制御が必要な場合：

```python
from tools import Tool, register_tool_class

class MyComplexTool(Tool):
    name = "my_complex_tool"
    description = "複雑なツールの説明"
    
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "検索クエリ"
            },
            "limit": {
                "type": "integer",
                "description": "最大件数",
                "default": 10
            }
        },
        "required": ["query"]
    }
    
    async def execute(self, query: str, limit: int = 10) -> dict:
        # 非同期処理も可能
        results = await self._search(query, limit)
        return {"results": results}

register_tool_class(MyComplexTool)
```

## コンテキストの利用

ツール実行時のコンテキスト（現在のペルソナ、Building など）を取得：

```python
from tools.context import persona_context

@register_tool
def context_aware_tool() -> str:
    ctx = persona_context.get()
    persona = ctx["persona"]
    building = ctx["building"]
    manager = ctx["manager"]
    
    return f"{persona.name} は {building.name} にいます"
```

## Buildingへの紐付け

1. ツールを作成
2. データベースの `tool` テーブルにエントリを追加（seed.py または直接）
3. ワールドエディタでBuildingにツールを紐付け

## テスト

```python
# tests/test_my_tool.py

import unittest
from tools.defs.my_tool import my_tool

class TestMyTool(unittest.TestCase):
    def test_basic(self):
        result = my_tool("test", 5)
        self.assertIn("test", result)
```

## 次のステップ

- [ツールカタログ](../reference/tool-catalog.md) - 既存ツールの参照
- [Playbook作成](./creating-playbooks.md) - Playbookでツールを使う
