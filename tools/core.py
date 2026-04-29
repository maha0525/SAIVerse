"""
tools.core  ― ベンダー非依存ツール実装 + メタスキーマ

* ToolSchema: ツールのメタ情報を定義するデータクラス
* ToolResult: ツールの結果を格納するデータクラス
* parse_tool_result: ツール結果のパース関数
"""
from dataclasses import dataclass
from typing import Any, Callable, Dict, Tuple, Optional


@dataclass
class ToolSchema:
    name: str
    description: str
    parameters: Dict[str, Any]   # JSON Schema
    result_type: str             # "string" / "number" / ...
    spell: bool = False          # If True, tool is available as a spell (invoked via /spell in LLM text output)
    spell_display_name: str = ""  # Japanese display name for spell UI (e.g. "特定時刻のログ取得")
    spell_visible: bool = True   # If False, spell is executable but hidden from system prompt (revealed via help spell)
    # Optional per-persona gate. When set, the spell is hidden from a
    # persona's system prompt and addon_spell_help unless the callable
    # returns True for that persona_id. Mirrors the role MCP plays for
    # MCP-backed spells (env placeholder resolution); use this for native
    # Python tools whose availability depends on per-persona state such
    # as OAuth connection status, license, etc. ``persona_id`` may be
    # None when the runtime cannot identify the active persona — in that
    # case, return False to keep the spell hidden conservatively.
    availability_check: Optional[Callable[[Optional[str]], bool]] = None
    # アドオン所属の識別子。`expansion_data/<addon_name>/tools/` 配下のネイティブ
    # ツールはローダーが自動でセットする。MCP 由来のスペルは `<addon_name>__<spell>`
    # 命名規則が別ルートで判定される。明示的に None なら built-in 扱い。
    addon_name: Optional[str] = None


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
