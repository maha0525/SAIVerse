"""
tools.defs.invoke_phenomenon ― フェノメノン呼び出しツール

ツールやプレイブックからフェノメノンを直接呼び出すためのツール。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from tools.defs import ToolSchema


def invoke_phenomenon(phenomenon_name: str, arguments: Optional[str] = None) -> str:
    """フェノメノンを直接呼び出す

    Args:
        phenomenon_name: 実行するフェノメノンの名前
        arguments: フェノメノンに渡す引数（JSON文字列）

    Returns:
        フェノメノンの実行結果
    """
    from phenomena import PHENOMENON_REGISTRY

    impl = PHENOMENON_REGISTRY.get(phenomenon_name)
    if not impl:
        available = list(PHENOMENON_REGISTRY.keys())
        return f"Error: Phenomenon '{phenomenon_name}' not found. Available: {available}"

    # 引数をパース
    kwargs: Dict[str, Any] = {}
    if arguments:
        try:
            kwargs = json.loads(arguments)
        except json.JSONDecodeError as e:
            return f"Error: Invalid JSON in arguments: {e}"

    try:
        result = impl(**kwargs)
        return str(result) if result is not None else "Phenomenon executed successfully"
    except Exception as e:
        return f"Error executing phenomenon: {e}"


def schema() -> ToolSchema:
    return ToolSchema(
        name="invoke_phenomenon",
        description="フェノメノン（現象）を直接呼び出して実行します。フェノメノンはSAIVerse世界で発生させることができる汎用的な処理単位です。",
        parameters={
            "type": "object",
            "properties": {
                "phenomenon_name": {
                    "type": "string",
                    "description": "実行するフェノメノンの名前",
                },
                "arguments": {
                    "type": "string",
                    "description": "フェノメノンに渡す引数（JSON形式の文字列）。例: {\"message\": \"Hello\", \"level\": \"info\"}",
                },
            },
            "required": ["phenomenon_name"],
        },
        result_type="string",
    )
