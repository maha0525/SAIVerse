"""アドオン拡張点の自動ロードモジュール。

API ルーター（api_routes.py）と外部連携（integrations/*.py）を
``expansion_data/<addon_name>/`` 配下から自動discoveryして登録する。

main.py で呼ぶ:
    from saiverse.addon_loader import load_addon_routers, load_addon_integrations
    load_addon_routers(app)
    load_addon_integrations(integration_manager)
"""
from __future__ import annotations

import importlib.util
import inspect
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Type

if TYPE_CHECKING:
    from saiverse.integration_manager import IntegrationManager
    from saiverse.integrations.base import BaseIntegration

LOGGER = logging.getLogger(__name__)

# addon_name -> 登録済みの integration.name のリスト。
# アドオン無効化時にこのマップから unregister 対象を引く。
_addon_integration_registry: Dict[str, List[str]] = {}


def _register_addon_externals() -> None:
    """expansion_data/ 下の全アドオンの external/ を名前空間隔離下に登録する。

    各アドオンの api_routes.py を読み込む**前**に呼ぶ必要がある(隔離機構の
    __import__ パッチが整ってから上流ライブラリを import するため)。
    """
    from saiverse.addon_external_loader import register_addon_external
    from saiverse.data_paths import EXPANSION_DATA_DIR

    if not EXPANSION_DATA_DIR.exists():
        return
    for addon_dir in sorted(EXPANSION_DATA_DIR.iterdir()):
        if not addon_dir.is_dir():
            continue
        external_dir = addon_dir / "external"
        if not external_dir.exists():
            continue
        try:
            register_addon_external(addon_dir.name, external_dir)
        except Exception:
            LOGGER.exception(
                "addon_loader: failed to register external/ for addon %r",
                addon_dir.name,
            )


def load_addon_routers(app) -> None:
    """expansion_data/ 下の全アドオンの api_routes.py を自動ロードする。

    api_routes.py に `router` 属性（FastAPI APIRouter）が定義されていれば
    /api/addon/<addon_name>/ プレフィックスでマウントする。

    本関数は同時に、各アドオンの ``external/`` ディレクトリを名前空間隔離
    機構に登録する(本体側 ``tools`` 等とのトップレベル名前衝突を防ぐ)。

    Args:
        app: FastAPI アプリインスタンス
    """
    from saiverse.data_paths import EXPANSION_DATA_DIR

    if not EXPANSION_DATA_DIR.exists():
        LOGGER.debug("addon_loader: expansion_data/ not found, skipping")
        return

    # external/ の名前空間隔離は **api_routes.py 読み込みより先**に行う。
    _register_addon_externals()

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


# ---------------------------------------------------------------------------
# Integrations auto-discovery
# ---------------------------------------------------------------------------

def _import_integration_module(addon_name: str, py_file: Path):
    """integrations/*.py を動的にロードする。"""
    module_key = (
        f"_addon_{addon_name.replace('-', '_')}_integration_{py_file.stem}"
    )
    spec = importlib.util.spec_from_file_location(module_key, py_file)
    if spec is None or spec.loader is None:
        raise ImportError(f"Failed to load spec for {py_file}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_key] = module
    spec.loader.exec_module(module)
    return module


def _find_base_integration_subclasses(module) -> List[Type["BaseIntegration"]]:
    """モジュール内の BaseIntegration 継承クラスを列挙する（直接サブクラスのみ）。"""
    from saiverse.integrations.base import BaseIntegration

    found: List[Type[BaseIntegration]] = []
    for _name, obj in inspect.getmembers(module, inspect.isclass):
        # モジュール定義のクラスのみ対象（import で持ち込まれた BaseIntegration 自身は除外）
        if obj is BaseIntegration:
            continue
        if not issubclass(obj, BaseIntegration):
            continue
        if obj.__module__ != module.__name__:
            continue
        found.append(obj)
    return found


def _register_addon_integrations_for(
    integration_manager: "IntegrationManager",
    addon_dir: Path,
) -> int:
    """単一アドオンの integrations/*.py を読んで register する。

    Returns:
        登録した integration の数
    """
    integrations_dir = addon_dir / "integrations"
    if not integrations_dir.exists() or not integrations_dir.is_dir():
        return 0

    addon_name = addon_dir.name
    registered_names: List[str] = []

    for py_file in sorted(integrations_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        try:
            module = _import_integration_module(addon_name, py_file)
        except Exception:
            LOGGER.exception(
                "addon_loader: failed to import integration %s", py_file
            )
            continue

        classes = _find_base_integration_subclasses(module)
        if not classes:
            LOGGER.debug(
                "addon_loader: no BaseIntegration subclass in %s", py_file
            )
            continue

        for cls in classes:
            try:
                instance = cls()
            except Exception:
                LOGGER.exception(
                    "addon_loader: failed to instantiate %s.%s",
                    py_file, cls.__name__,
                )
                continue

            integration_manager.register(instance)
            registered_names.append(instance.name)

    if registered_names:
        existing = _addon_integration_registry.setdefault(addon_name, [])
        for name in registered_names:
            if name not in existing:
                existing.append(name)

    return len(registered_names)


def load_addon_integrations(integration_manager: "IntegrationManager") -> None:
    """有効なアドオンの ``integrations/*.py`` をすべて自動 register する。

    起動時に main.py から1回だけ呼ぶ想定。
    アドオンが無効（AddonConfig.is_enabled == False）ならスキップする。

    Args:
        integration_manager: SAIVerseManager.integration_manager
    """
    from saiverse.addon_config import is_addon_enabled
    from saiverse.data_paths import EXPANSION_DATA_DIR

    if not EXPANSION_DATA_DIR.exists():
        LOGGER.debug("addon_loader: expansion_data/ not found, skipping integrations")
        return

    total = 0
    for addon_dir in sorted(EXPANSION_DATA_DIR.iterdir()):
        if not addon_dir.is_dir():
            continue
        addon_name = addon_dir.name

        if not is_addon_enabled(addon_name):
            LOGGER.debug(
                "addon_loader: skipping disabled addon '%s' for integrations",
                addon_name,
            )
            continue

        count = _register_addon_integrations_for(integration_manager, addon_dir)
        total += count

    LOGGER.info("addon_loader: %d addon integration(s) registered", total)


def register_addon_integrations(
    integration_manager: "IntegrationManager",
    addon_name: str,
) -> int:
    """指定アドオンの integrations をランタイムで register する（有効化時用）。

    Returns:
        登録した integration の数
    """
    from saiverse.data_paths import EXPANSION_DATA_DIR

    addon_dir = EXPANSION_DATA_DIR / addon_name
    if not addon_dir.is_dir():
        LOGGER.warning(
            "addon_loader: addon '%s' not found for integration registration",
            addon_name,
        )
        return 0
    return _register_addon_integrations_for(integration_manager, addon_dir)


def unregister_addon_integrations(
    integration_manager: "IntegrationManager",
    addon_name: str,
) -> int:
    """指定アドオンの integrations をランタイムで unregister する（無効化時用）。

    Returns:
        解除した integration の数
    """
    names = _addon_integration_registry.pop(addon_name, [])
    for name in names:
        try:
            integration_manager.unregister(name)
        except Exception:
            LOGGER.exception(
                "addon_loader: failed to unregister integration '%s' (addon=%s)",
                name, addon_name,
            )
    if names:
        LOGGER.info(
            "addon_loader: %d integration(s) unregistered for addon '%s'",
            len(names), addon_name,
        )
    return len(names)
