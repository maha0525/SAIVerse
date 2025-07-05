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
    """Container for history snippets returned by tools."""

    history_snippet: Optional[str] = None


def parse_tool_result(res: Any) -> Tuple[str, Optional[str], Optional[str]]:
    """Normalize various tool return formats."""
    if isinstance(res, ToolResult):
        return "", res.history_snippet, None
    if isinstance(res, dict):
        content = str(res.get("content", ""))
        snippet = res.get("history_snippet")
        file_path = res.get("file")
        if file_path is not None:
            file_path = str(file_path)
        return content, snippet, file_path
    if isinstance(res, tuple):
        if len(res) == 2:
            content = str(res[0])
            snip = res[1]
            if isinstance(snip, ToolResult):
                snip = snip.history_snippet
            return content, snip, None
        if len(res) >= 3:
            content = str(res[0])
            snip = res[1]
            file_path = res[2]
            if isinstance(snip, ToolResult):
                snip = snip.history_snippet
            if file_path is not None:
                file_path = str(file_path)
            return content, snip, file_path
    return str(res), None, None

# ここに共通ヘルパを追加しても良い
