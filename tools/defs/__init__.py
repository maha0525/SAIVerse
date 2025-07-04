"""
tools.defs  ― ベンダー非依存ツール実装 + メタスキーマ

* 各ツールモジュールは
    - calculate_expression()
    - schema() -> ToolSchema
  を必ず公開する。
"""
from dataclasses import dataclass
from typing import Any, Dict, Tuple, Optional

@dataclass
class ToolSchema:
    name: str
    description: str
    parameters: Dict[str, Any]   # JSON Schema
    result_type: str             # "string" / "number" / ...

@dataclass
class ToolResult:
    """Return type for tools that want to provide history snippets."""

    content: str
    history_snippet: Optional[str] = None


def parse_tool_result(res: Any) -> Tuple[str, Optional[str]]:
    """Normalize various tool return formats."""
    if isinstance(res, ToolResult):
        return res.content, res.history_snippet
    if isinstance(res, dict):
        content = str(res.get("content", ""))
        snippet = res.get("history_snippet")
        return content, snippet
    if isinstance(res, tuple) and len(res) == 2:
        return str(res[0]), res[1]
    return str(res), None

# ここに共通ヘルパを追加しても良い
