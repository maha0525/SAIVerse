"""
phenomena.core ― フェノメノン定義用のベーススキーマ

各フェノメノンモジュールは以下を必ず公開する:
    - phenomenon_function()  # 実際の処理
    - schema() -> PhenomenonSchema
"""
from dataclasses import dataclass
from typing import Any, Dict


@dataclass
class PhenomenonSchema:
    """フェノメノンのスキーマ定義"""
    name: str
    description: str
    parameters: Dict[str, Any]  # JSON Schema
    is_async: bool = True  # デフォルトは非同期実行
