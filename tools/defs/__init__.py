"""
tools.defs  ― ベンダー非依存ツール実装 + メタスキーマ

* 各ツールモジュールは
    - calculate_expression()
    - schema() -> ToolSchema
  を必ず公開する。
"""
from dataclasses import dataclass
from typing import Any, Dict

@dataclass
class ToolSchema:
    name: str
    description: str
    parameters: Dict[str, Any]   # JSON Schema
    result_type: str             # "string" / "number" / ...

# ここに共通ヘルパを追加しても良い