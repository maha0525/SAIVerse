"""
tools.adapters  ― ToolSchema → プロバイダー固有フォーマット変換
"""
from .openai import to_openai
from .gemini import to_gemini

__all__ = ["to_openai", "to_gemini"]