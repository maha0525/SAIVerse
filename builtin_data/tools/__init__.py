"""
builtin_data/tools ― ビルトインツール定義

ツールの共通型 (ToolSchema, ToolResult, parse_tool_result) は
tools.core からインポートしてください。
ここでは後方互換性のためにそれらを再エクスポートしています。
"""
# Re-export core types for backward compatibility
from tools.core import ToolSchema, ToolResult, parse_tool_result

__all__ = ["ToolSchema", "ToolResult", "parse_tool_result"]
