"""起動前・setup で起きた環境の失敗を、画面のバナー (startup_alerts) に出す。

本体の起動時に main.py から一度だけ呼ぶ。集めるのは次の二つ:

- 更新・起動前のパッケージの入れ直しの失敗 (``saiverse/update_dependencies_alert.py``)
- setup.bat / setup.sh でのアドオンのパッケージの入れ直しの失敗 (本体の ``.venv`` に
  入れる step の入れ直しと、アドオン専用の Python 環境の作り直し。
  ``addon_installer.ENV_REBUILD_FAILURES_PATH`` に setup が書く。次の setup で
  入れ直しが成功すると setup が消す)

どちらも起動を止めないため、例外は出さない。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

from saiverse.update_dependencies_alert import update_dependencies_alerts

LOGGER = logging.getLogger(__name__)

ADDON_ENV_REBUILD_ALERT_ID = "addon_env_rebuild_failed"


def addon_env_rebuild_alerts(path: Path | None = None) -> List[Dict[str, Any]]:
    """setup が書いたアドオンのパッケージの入れ直しの失敗があれば、警告を 1 件返す。無ければ空。"""
    try:
        if path is None:
            from saiverse.addon_installer import ENV_REBUILD_FAILURES_PATH

            path = ENV_REBUILD_FAILURES_PATH
        if not path.is_file():
            return []
        document = json.loads(path.read_text(encoding="utf-8"))
        failures = document.get("failures") if isinstance(document, dict) else None
        if not isinstance(failures, list):
            failures = []
    except Exception:  # 記録を読めないことで起動を止めない
        LOGGER.warning("アドオンの環境の作り直しの失敗の記録を読めませんでした", exc_info=True)
        failures = []

    # 一つのアドオンが、本体の .venv への入れ直しと専用の環境の作り直しの両方で失敗すると
    # 二項目になる (``core_venv`` で区別)。名前は一度だけ出す。
    addon_ids: List[str] = []
    for item in failures:
        if isinstance(item, dict) and item.get("addon_id") and str(item["addon_id"]) not in addon_ids:
            addon_ids.append(str(item["addon_id"]))
    errors = [
        f"{item.get('addon_id')}"
        f" ({'SAIVerse .venv' if item.get('core_venv') is True else 'addon environment'}): "
        f"{item.get('error')}"
        for item in failures
        if isinstance(item, dict) and item.get("error")
    ]
    names = "、".join(addon_ids) if addon_ids else "(名前は記録されていません)"
    LOGGER.warning("setup でのアドオンのパッケージの入れ直しが失敗しています: %s", names)
    details: Dict[str, Any] = {"reason": addon_ids}
    if errors:
        details["error"] = "\n".join(errors)[-4000:]
    return [{
        "id": ADDON_ENV_REBUILD_ALERT_ID,
        "level": "warning",
        "title": "アドオンのパッケージを入れ直せませんでした",
        "message": (
            f"SAIVerse の Python の環境が作り直されたため、setup でアドオン ({names}) の"
            "パッケージ (SAIVerse 本体の .venv に入れるもの、またはアドオン専用の Python 環境) を"
            "入れ直そうとしましたが、失敗しました。このアドオンは今のままでは動きません。"
            "SAIVerse を閉じてから setup.bat (Mac / Linux では setup.sh) をもう一度実行するか、"
            "アドオン管理でそのアドオンを入れ直してください (アドオンのデータは残ります)。"
            "入れ直しが成功すると、この表示は消えます。"
        ),
        "details": details,
    }]


def environment_alerts(project_dir: Path) -> List[Dict[str, Any]]:
    """起動時に startup_alerts へ積む、環境の失敗の警告を全部返す。"""
    alerts: List[Dict[str, Any]] = []
    for collect in (lambda: update_dependencies_alerts(project_dir), addon_env_rebuild_alerts):
        try:
            alerts.extend(collect())
        except Exception:  # 一つが壊れても、他の警告と起動は止めない
            LOGGER.warning("環境の失敗の警告を集められませんでした", exc_info=True)
    return alerts
