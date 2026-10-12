"""アドオンの案内の提供口の登録簿。

アドオンは manifest の ``context_provider`` で「いまこのペルソナに伝えたい案内」
を返す関数を一つ登録できる (例: Godot 仮想身体の部屋でだけ使える全身エモートの
記法)。登録と解除は :mod:`saiverse.addon_loader` が行い、読み手は head の
``addon_guidance`` セクション (sea/head_pipeline/sections/addon_guidance.py)
だけである。

案内は head に置かれ、部屋の移動・アドオンの有効化と無効化・設定の変更で案内が
増えたり消えたり変わったりしたときは、スペル一覧と同じ差分通知の道で
「[システム通知]」としてペルソナの知覚に届く。2026-10-11 に一度リアルタイム
情報 (毎ターン差し替わる末尾メッセージ) へ載せたが、それでは部屋を出て案内が
消えたことが出来事として残らず、過去に自分が書いた記法だけが履歴に残って使い
続ける恐れがあるため、2026-10-12 に head と差分通知へ移した
(docs/intent/addon_speak_hooks.md)。

提供口の呼び出し契約: ``provider(persona=..., building_id=...) -> str``。
読み取り専用で、記憶・知覚・世界の状態に書いてはならない。空文字や文字列で
ない値は「案内なし」として扱う。例外はその提供口だけ警告ログに残して
「案内なし」として扱い、他のアドオンの案内には波及させない。
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable

LOGGER = logging.getLogger(__name__)
_providers: dict[str, Callable[..., str]] = {}
_lock = threading.Lock()


def register_provider(addon_name: str, provider: Callable[..., str]) -> None:
    with _lock:
        _providers[addon_name] = provider


def unregister_provider(addon_name: str) -> None:
    with _lock:
        _providers.pop(addon_name, None)


def collect_guidance(persona: Any, building_id: Any) -> list[tuple[str, str]]:
    """登録された提供口を addon 名の昇順で呼び、``(addon_name, text)`` の列を返す。

    順序を addon 名で固定するのは、同じ世界なら head に毎回同じ文字列が出る
    ことが prefix キャッシュの前提だから。案内を返さなかった提供口は列に
    含めない。
    """
    with _lock:
        providers = sorted(_providers.items())
    guidance: list[tuple[str, str]] = []
    for name, provider in providers:
        try:
            text = provider(persona=persona, building_id=building_id)
        except Exception:
            LOGGER.warning("addon context provider failed addon=%s", name, exc_info=True)
            continue
        if isinstance(text, str) and text.strip():
            guidance.append((name, text.strip()))
    return guidance


def addon_display_name(addon_name: str) -> str:
    """``expansion_data/<addon_name>/addon.json`` の表示名を日本語優先で返す。

    ``display_name`` は文字列のことも ``{"ja": ..., "en": ...}`` の辞書のことも
    ある (Godot 仮想身体アドオンは辞書)。解決はアドオン管理画面と同じ
    :func:`saiverse.i18n_utils.resolve_i18n_text` に任せ、manifest が読めない・
    表示名が無いときは addon 名をそのまま返す。
    """
    try:
        from saiverse.data_paths import EXPANSION_DATA_DIR
        from saiverse.i18n_utils import resolve_i18n_text

        manifest_path = EXPANSION_DATA_DIR / addon_name / "addon.json"
        with open(manifest_path, encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return addon_name
        display_en = raw.get("display_name_en")
        return resolve_i18n_text(
            raw.get("display_name"),
            target_lang="ja",
            alt_en=display_en if isinstance(display_en, str) else None,
            default=addon_name,
        ) or addon_name
    except FileNotFoundError:
        return addon_name
    except Exception:
        LOGGER.debug(
            "addon_context: failed to read display_name for addon=%s",
            addon_name, exc_info=True,
        )
        return addon_name
