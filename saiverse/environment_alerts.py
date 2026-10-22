"""起動前・setup で起きた環境の失敗を、画面のバナー (startup_alerts) に出す。

本体の起動時に main.py から一度だけ呼ぶ。集めるのは次の四つ:

- 更新・起動前のパッケージの入れ直しの失敗 (``saiverse/update_dependencies_alert.py``)
- setup.bat / setup.sh でのアドオンのパッケージの入れ直しの失敗 (本体の ``.venv`` に
  入れる step の入れ直しと、アドオン専用の Python 環境の作り直し。
  ``addon_installer.ENV_REBUILD_FAILURES_PATH`` に setup が書く。次の setup で
  入れ直しが成功すると setup が消す)
- SearXNG の導入 (``scripts/searxng_install.py setup``) の失敗。setup.bat / setup.sh と、
  SearXNG を起動するスクリプトが動かす。SAIVerse のフォルダの
  ``SEARXNG_SETUP_FAILURE_RECORD`` に書かれ、次に導入が成功すると消える
- 画面の「Update」ボタンの更新で止めた SearXNG のサーバーを起動し直せなかったこと。
  ``update_engine`` がバックエンドを起動する前に ``SEARXNG_RESTART_FAILURE_RECORD``
  に書く。その起動し直しで起きた出来事の知らせなので、ここで読んだら消す

どれも起動を止めないため、例外は出さない。
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


# scripts/searxng_install.py の SETUP_FAILURE_RECORD と、scripts/update_engine.py の
# SEARXNG_RESTART_FAILURE_RECORD。どちらのスクリプトもここで読み込まずに済むよう、
# 名前は文字列で持つ (一致はテストが確かめる)。
SEARXNG_SETUP_FAILURE_RECORD = ".searxng_setup_failed.json"
SEARXNG_RESTART_FAILURE_RECORD = ".searxng_restart_failed.json"
SEARXNG_SETUP_ALERT_ID = "searxng_setup_failed"
SEARXNG_RESTART_ALERT_ID = "searxng_restart_failed"


def _read_failure_record(path: Path) -> Dict[str, Any] | None:
    """記録の中身。無ければ None、読めなければ空の dict (失敗した、詳細は不明)。"""
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # 記録を読めないことで起動を止めない
        LOGGER.warning("記録 %s を読めませんでした", path, exc_info=True)
        return {}
    return document if isinstance(document, dict) else {}


def _error_details(document: Dict[str, Any]) -> Dict[str, Any]:
    error = document.get("error")
    return {"error": error} if isinstance(error, str) and error else {}


def searxng_setup_alerts(project_dir: Path) -> List[Dict[str, Any]]:
    """SearXNG の導入の失敗の記録があれば、警告を 1 件返す。無ければ空。消すのは導入の側。"""
    document = _read_failure_record(project_dir / SEARXNG_SETUP_FAILURE_RECORD)
    if document is None:
        return []
    details = _error_details(document)
    LOGGER.warning("SearXNG の導入が失敗しています: %s", details.get("error") or "詳細は記録されていません")
    return [{
        "id": SEARXNG_SETUP_ALERT_ID,
        "level": "warning",
        "title": "SearXNG (Web 検索のサーバー) を入れられませんでした",
        "message": (
            "SearXNG の導入が途中で失敗しました。SAIVerse 本体は動きますが、Web 検索は使えない"
            "可能性があります。別の Python で作られていた SearXNG の環境を作り直していた場合は、"
            "前の環境に戻してあります。インターネットにつながっていることを確かめ、SAIVerse の窓"
            "をすべて閉じてから、setup.bat (Mac / Linux では setup.sh) をもう一度実行してください。"
            "導入が成功すると、この表示は消えます。"
        ),
        "details": details,
    }]


def searxng_restart_alerts(project_dir: Path) -> List[Dict[str, Any]]:
    """更新で止めた SearXNG を起動し直せなかった記録があれば、警告を 1 件返して記録を消す。"""
    path = project_dir / SEARXNG_RESTART_FAILURE_RECORD
    document = _read_failure_record(path)
    if document is None:
        return []
    try:
        path.unlink()
    except OSError:  # 消せなくても警告は出す (次の起動でもう一度出る)
        LOGGER.warning("記録 %s を消せませんでした", path, exc_info=True)
    details = _error_details(document)
    LOGGER.warning(
        "更新のあと SearXNG を起動し直せませんでした: %s", details.get("error") or "詳細は記録されていません"
    )
    return [{
        "id": SEARXNG_RESTART_ALERT_ID,
        "level": "warning",
        "title": "SearXNG (Web 検索のサーバー) を起動し直せませんでした",
        "message": (
            "アップデートのために止めた SearXNG を、アップデートのあとに起動し直せませんでした。"
            "いまは Web 検索が使えません。SAIVerse の窓をすべて閉じてから、start.bat "
            "(Mac / Linux では SAIVERSE_SEARXNG=1 ./start.sh) で起動し直してください。"
        ),
        "details": details,
    }]


def environment_alerts(project_dir: Path) -> List[Dict[str, Any]]:
    """起動時に startup_alerts へ積む、環境の失敗の警告を全部返す。"""
    alerts: List[Dict[str, Any]] = []
    for collect in (
        lambda: update_dependencies_alerts(project_dir),
        addon_env_rebuild_alerts,
        lambda: searxng_setup_alerts(project_dir),
        lambda: searxng_restart_alerts(project_dir),
    ):
        try:
            alerts.extend(collect())
        except Exception:  # 一つが壊れても、他の警告と起動は止めない
            LOGGER.warning("環境の失敗の警告を集められませんでした", exc_info=True)
    return alerts
