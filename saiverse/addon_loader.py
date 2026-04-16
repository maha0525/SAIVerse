"""アドオン API ルーター自動ロードモジュール。

expansion_data/<addon_name>/api_routes.py を検索し、
FastAPI アプリに /api/addon/<addon_name>/... としてマウントする。

main.py で呼ぶ:
    from saiverse.addon_loader import load_addon_routers
    load_addon_routers(app)
"""
from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

LOGGER = logging.getLogger(__name__)


def load_addon_routers(app) -> None:
    """expansion_data/ 下の全アドオンの api_routes.py を自動ロードする。

    api_routes.py に `router` 属性（FastAPI APIRouter）が定義されていれば
    /api/addon/<addon_name>/ プレフィックスでマウントする。

    Args:
        app: FastAPI アプリインスタンス
    """
    from saiverse.data_paths import EXPANSION_DATA_DIR

    if not EXPANSION_DATA_DIR.exists():
        LOGGER.debug("addon_loader: expansion_data/ not found, skipping")
        return

    loaded_count = 0
    for addon_dir in sorted(EXPANSION_DATA_DIR.iterdir()):
        if not addon_dir.is_dir():
            continue

        routes_file = addon_dir / "api_routes.py"
        if not routes_file.exists():
            continue

        addon_name = addon_dir.name
        module_name = f"_addon_{addon_name.replace('-', '_')}_api_routes"

        try:
            spec = importlib.util.spec_from_file_location(module_name, routes_file)
            if spec is None or spec.loader is None:
                LOGGER.warning("addon_loader: failed to load spec for %s", routes_file)
                continue

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            if not hasattr(module, "router"):
                LOGGER.warning(
                    "addon_loader: %s has no 'router' attribute, skipping",
                    routes_file,
                )
                continue

            prefix = f"/api/addon/{addon_name}"
            app.include_router(
                module.router,
                prefix=prefix,
                tags=[f"addon-{addon_name}"],
            )
            LOGGER.info("addon_loader: mounted %s at %s", addon_name, prefix)
            loaded_count += 1

        except Exception:
            LOGGER.exception(
                "addon_loader: error loading api_routes.py for addon '%s'", addon_name
            )

    LOGGER.info("addon_loader: %d addon router(s) loaded", loaded_count)
