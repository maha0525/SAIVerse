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


def parse_tool_result(res: Any) -> Tuple[str, Optional[str], Optional[str], Optional[Dict[str, Any]]]:
    """Normalize various tool return formats."""
    metadata: Optional[Dict[str, Any]] = None
    if isinstance(res, ToolResult):
        return "", res.history_snippet, None, metadata
    if isinstance(res, dict):
        content = str(res.get("content", ""))
        snippet = res.get("history_snippet")
        file_path = res.get("file")
        if file_path is not None:
            file_path = str(file_path)
        metadata = res.get("metadata")
        return content, snippet, file_path, metadata
    if isinstance(res, tuple):
        if len(res) == 2:
            content = str(res[0])
            snip = res[1]
            if isinstance(snip, ToolResult):
                snip = snip.history_snippet
            return content, snip, None, metadata
        if len(res) >= 3:
            content = str(res[0])
            snip = res[1]
            file_path = res[2]
            if isinstance(snip, ToolResult):
                snip = snip.history_snippet
            if file_path is not None:
                file_path = str(file_path)
            if len(res) >= 4 and isinstance(res[3], dict):
                metadata = res[3]
            return content, snip, file_path, metadata
    return str(res), None, None, metadata

# ここに共通ヘルパを追加しても良い
