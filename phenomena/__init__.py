"""
phenomena ― フェノメノン（現象）システム

SAIVerse世界で発生する現象を定義し、トリガーイベントに応じて自動実行する。
ペルソナとは独立してバックグラウンドで動作可能。

使用方法:
    1. phenomena/defs/ 以下に .py ファイルを配置
    2. schema() -> PhenomenonSchema と 同名関数を定義
    3. 自動的にレジストリに登録される
"""
import importlib
import os
import pkgutil
from pathlib import Path
from typing import Callable, Dict, List

from phenomena.defs import PhenomenonSchema

PHENOMENON_REGISTRY: Dict[str, Callable] = {}
PHENOMENON_SCHEMAS: List[PhenomenonSchema] = []


def _autodiscover_phenomena() -> None:
    """phenomena/defs/ 以下のモジュールを自動的に読み込み、レジストリに登録する"""
    defs_path = Path(__file__).parent / "defs"
    if not defs_path.exists():
        return

    for modinfo in pkgutil.iter_modules([str(defs_path)]):
        if modinfo.name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"phenomena.defs.{modinfo.name}")
            if hasattr(module, "schema") and callable(module.schema):
                meta: PhenomenonSchema = module.schema()
                impl: Callable = getattr(module, meta.name, None)
                if impl and callable(impl):
                    PHENOMENON_REGISTRY[meta.name] = impl
                    PHENOMENON_SCHEMAS.append(meta)
        except Exception as e:
            import logging
            logging.warning("Failed to load phenomenon module '%s': %s", modinfo.name, e)


# 環境変数でスキップ可能
if os.getenv("SAIVERSE_SKIP_PHENOMENA_IMPORTS") != "1":
    _autodiscover_phenomena()
