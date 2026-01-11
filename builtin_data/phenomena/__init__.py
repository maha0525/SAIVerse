"""
builtin_data/phenomena ― ビルトインフェノメノン定義

フェノメノンの共通型 (PhenomenonSchema) は
phenomena.core からインポートしてください。
ここでは後方互換性のためにそれらを再エクスポートしています。
"""
# Re-export core types for backward compatibility
from phenomena.core import PhenomenonSchema

__all__ = ["PhenomenonSchema"]
