"""起動前・setup で起きた環境の失敗を、画面のバナー (startup_alerts) に出す。

本体の起動時に main.py から一度だけ呼ぶ。集めるのは次の五つ:

- 更新・起動前のパッケージの入れ直しの失敗 (``saiverse/update_dependencies_alert.py``)
- setup.bat / setup.sh でのアドオンのパッケージの入れ直しの失敗 (本体の ``.venv`` に
  入れる step の入れ直しと、アドオン専用の Python 環境の作り直し。
  ``addon_installer.ENV_REBUILD_FAILURES_PATH`` に setup が書く。次の setup で
  入れ直しが成功すると setup が消す)
- setup (またはアドオン管理での導入・更新) が途中で止まったまま残った、やりかけの記録
  (``scripts/setup_in_progress.py``)。本体の ``.venv`` とアドオンの専用の環境のもの。
  窓を閉じるなどで止まると、上の失敗の記録は書かれずに、これだけが残る
- SearXNG の導入 (``scripts/searxng_install.py setup``) の失敗と、途中で止まったまま
  残ったやりかけの記録。setup.bat / setup.sh と、SearXNG を起動するスクリプトと、
  更新の入れ直しが動かす。SAIVerse のフォルダの ``SEARXNG_SETUP_FAILURE_RECORD`` と
  ``SEARXNG_SETUP_IN_PROGRESS_RECORD`` に書かれ、次に導入が成功すると消える
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


def _rebuild_failures_path(path: Path | None) -> Path:
    if path is not None:
        return path
    from saiverse.addon_installer import ENV_REBUILD_FAILURES_PATH

    return ENV_REBUILD_FAILURES_PATH


def _read_rebuild_failures(path: Path) -> List[Any] | None:
    """setup が書いたアドオンの入れ直しの失敗の項目。記録が無ければ None、読めなければ空。"""
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # 記録を読めないことで起動を止めない
        LOGGER.warning("アドオンの環境の作り直しの失敗の記録を読めませんでした", exc_info=True)
        return []
    failures = document.get("failures") if isinstance(document, dict) else None
    return failures if isinstance(failures, list) else []


def _failed_addon_ids(failures: List[Any]) -> List[str]:
    # 一つのアドオンが、本体の .venv への入れ直しと専用の環境の作り直しの両方で失敗すると
    # 二項目になる (``core_venv`` で区別)。名前は一度だけ出す。
    addon_ids: List[str] = []
    for item in failures:
        if isinstance(item, dict) and item.get("addon_id") and str(item["addon_id"]) not in addon_ids:
            addon_ids.append(str(item["addon_id"]))
    return addon_ids


def addon_env_rebuild_alerts(path: Path | None = None) -> List[Dict[str, Any]]:
    """setup が書いたアドオンのパッケージの入れ直しの失敗があれば、警告を 1 件返す。無ければ空。"""
    try:
        failures = _read_rebuild_failures(_rebuild_failures_path(path))
    except Exception:  # 記録の場所を決められないことで起動を止めない
        LOGGER.warning("アドオンの環境の作り直しの失敗の記録を読めませんでした", exc_info=True)
        return []
    if failures is None:
        return []

    addon_ids = _failed_addon_ids(failures)
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
SEARXNG_SETUP_STOPPED_ALERT_ID = "searxng_setup_stopped"
SEARXNG_RESTART_ALERT_ID = "searxng_restart_failed"
# scripts/update_engine.py の DEPENDENCY_STEP_SEARXNG (一致はテストが確かめる)。
SEARXNG_DEPENDENCY_STEP = "searxng"

# やりかけの記録 (scripts/setup_in_progress.py、docs/intent/dependency_management.md
# §2-7)。環境を壊す操作の前に書かれ、全部終わってから消える。残っていれば、その操作は
# 途中で止まった (窓を閉じた、Ctrl+C、落ちた) か、失敗して戻せなかった。名前は
# scripts/searxng_install.py の SET_ASIDE_ENTRY と scripts/setup_venv.py の VENV_ENTRY
# (一致はテストが確かめる)。どちらも SAIVerse のフォルダに置かれる。アドオンの専用の
# 環境の記録は ``addon_installer.find_addon_envs_left_part_way`` が読む。
SEARXNG_SETUP_IN_PROGRESS_RECORD = Path(".setup_in_progress") / "searxng-venv.json"
CORE_VENV_SETUP_IN_PROGRESS_RECORD = Path(".setup_in_progress") / "venv.json"
SETUP_STOPPED_ALERT_ID = "setup_stopped"
ADDON_ENV_STOPPED_ALERT_ID = "addon_env_setup_stopped"


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


def _searxng_named_by_dependencies_failure(project_dir: Path) -> bool:
    """更新・起動前のやり直しの失敗の記録 (``update_dependencies_alert``) が SearXNG の
    入れ直しの失敗を名指ししているか。名指ししていれば、その警告が SearXNG のことも
    知らせていて、次の start.bat / start.sh のやり直しが SearXNG も入れ直す。"""
    try:
        from scripts.update_engine import read_dependencies_failure

        failure = read_dependencies_failure(project_dir)
    except Exception:  # 読めないなら名指しは無いものとして、SearXNG の警告を出す
        return False
    steps = failure.get("steps") if isinstance(failure, dict) else None
    return isinstance(steps, list) and SEARXNG_DEPENDENCY_STEP in steps


def searxng_setup_alerts(project_dir: Path) -> List[Dict[str, Any]]:
    """SearXNG の導入が済んでいなければ、警告を 1 件返す。無ければ空。消すのは導入の側。

    知らせるのは二つで、同じ SearXNG のことなので 1 件にまとめる: 導入の失敗の記録
    (``SEARXNG_SETUP_FAILURE_RECORD``) と、導入が途中で止まった記録
    (``SEARXNG_SETUP_IN_PROGRESS_RECORD``。窓を閉じるなどで、失敗の記録を書く前に止まると
    これだけが残る)。途中で止まった記録があるときは、前の環境に戻せていないので、その
    文面にする。更新・起動前のやり直しの失敗の記録が SearXNG を名指ししているときは、
    その警告 (``update_dependencies_alert``) が同じことを知らせるので、ここでは出さない。
    """
    stopped = (project_dir / SEARXNG_SETUP_IN_PROGRESS_RECORD).is_file()
    document = _read_failure_record(project_dir / SEARXNG_SETUP_FAILURE_RECORD)
    if document is None and not stopped:
        return []
    if _searxng_named_by_dependencies_failure(project_dir):
        LOGGER.warning("SearXNG の導入が済んでいません (アップデートの入れ替えの失敗として知らせます)")
        return []
    details = _error_details(document or {})
    if stopped:
        LOGGER.warning(
            "SearXNG の導入が途中で止まったままです (%s が残っています): %s",
            SEARXNG_SETUP_IN_PROGRESS_RECORD, details.get("error") or "失敗の詳細は記録されていません",
        )
        return [{
            "id": SEARXNG_SETUP_STOPPED_ALERT_ID,
            "level": "warning",
            "title": "SearXNG (Web 検索のサーバー) の導入が途中で止まっています",
            "message": (
                "SearXNG の setup が途中で止まったため、Web 検索 (SearXNG) が使えない可能性が"
                "あります。SAIVerse 本体は動きます。インターネットにつながっていることを確かめ、"
                "SAIVerse の窓をすべて閉じてから、setup.bat (Mac / Linux では setup.sh) を"
                "もう一度実行してください。導入が最後まで済むと、この表示は消えます。"
            ),
            "details": details,
        }]
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


def _addon_label(addon_id: str) -> str:
    try:
        from saiverse.addon_context import addon_display_name

        name = addon_display_name(addon_id)
    except Exception:  # 表示名が読めなくても、id で知らせる
        name = addon_id
    return name if name == addon_id else f"{name} ({addon_id})"


def setup_stopped_alerts(
    project_dir: Path,
    failures_path: Path | None = None,
    expansion_dir: Path | None = None,
) -> List[Dict[str, Any]]:
    """setup が途中で止まったまま (やりかけの記録が残っている) なら、警告を 1 件返す。

    窓を閉じる・Ctrl+C などで setup が止まると、失敗の記録
    (``.addon_env_rebuild_failures.json``) を書く処理まで届かず、やりかけの記録だけが
    残る。読むのは、本体の ``.venv`` の記録 (``CORE_VENV_SETUP_IN_PROGRESS_RECORD``。
    本体が起動できているなら、``.venv`` は使えているが、そのあとの作業 — アドオンの
    パッケージの入れ直しと、前の ``.venv`` の片付け — が済んでいない可能性がある) と、
    アドオンの専用の環境の記録 (アドオン管理での導入・更新の途中で止まった・失敗した
    ものも含む)。SearXNG の記録は ``searxng_setup_alerts`` が読む。

    失敗の記録にあるアドオンは、その警告 (``addon_env_rebuild_alerts``) が知らせるので
    ここでは名前を出さない。失敗の記録があるときは、本体の ``.venv`` の記録だけでは
    警告を出さない (失敗の警告が同じく setup をもう一度と案内する)。本体の ``.venv`` と
    アドオンの記録が両方残っていれば 1 件にまとめる。読めない記録でも起動は止めない。
    """
    try:
        failures = _read_rebuild_failures(_rebuild_failures_path(failures_path))
    except Exception:
        LOGGER.warning("アドオンの環境の作り直しの失敗の記録を読めませんでした", exc_info=True)
        failures = None
    failed = set(_failed_addon_ids(failures or []))
    try:
        from saiverse.addon_installer import find_addon_envs_left_part_way

        left = {
            addon_id: envs
            for addon_id, envs in find_addon_envs_left_part_way(expansion_dir).items()
            if addon_id not in failed
        }
    except Exception:  # アドオンの記録が読めなくても、本体の .venv の記録は知らせる
        LOGGER.warning("アドオンの専用の環境のやりかけの記録を読めませんでした", exc_info=True)
        left = {}
    core_stopped = failures is None and (project_dir / CORE_VENV_SETUP_IN_PROGRESS_RECORD).is_file()
    if not core_stopped and not left:
        return []
    names = "、".join(_addon_label(addon_id) for addon_id in left)
    LOGGER.warning(
        "setup が途中で止まったままです (本体の .venv: %s、アドオン: %s)",
        "記録あり" if core_stopped else "記録なし", names or "なし",
    )
    details: Dict[str, Any] = {}
    if left:
        details["reason"] = list(left)
        details["envs"] = [f"{addon_id}: {', '.join(envs)}" for addon_id, envs in left.items()]
    if core_stopped:
        addons = f"とくにアドオン ({names}) は、" if left else "アドオンの一部が"
        return [{
            "id": SETUP_STOPPED_ALERT_ID,
            "level": "warning",
            "title": "setup が途中で止まっています",
            "message": (
                "setup.bat (Mac / Linux では setup.sh) が途中で止まったため、Python の環境の"
                "作り直しのあとの作業 (アドオンのパッケージの入れ直しと、前の環境の片付け) が"
                f"済んでいない可能性があります。SAIVerse 本体は起動できていますが、{addons}"
                "今のままでは使えない可能性があります。SAIVerse の窓をすべて閉じてから、"
                "setup.bat (Mac / Linux では setup.sh) をもう一度実行してください。setup が"
                "最後まで済むと、この表示は消えます。"
            ),
            "details": details,
        }]
    return [{
        "id": ADDON_ENV_STOPPED_ALERT_ID,
        "level": "warning",
        "title": "アドオンの Python 環境の作り直しが途中で止まっています",
        "message": (
            f"アドオン ({names}) の専用の Python 環境の作り直しが、setup またはアドオン管理での"
            "導入・更新の途中で止まったか失敗したため、このアドオンは今のままでは使えない可能性"
            "があります。SAIVerse を閉じてから setup.bat (Mac / Linux では setup.sh) をもう一度"
            "実行するか、アドオン管理でそのアドオンを入れ直してください (アドオンのデータは"
            "残ります)。作り直しが最後まで済むと、この表示は消えます。"
        ),
        "details": details,
    }]


def environment_alerts(project_dir: Path) -> List[Dict[str, Any]]:
    """起動時に startup_alerts へ積む、環境の失敗の警告を全部返す。"""
    alerts: List[Dict[str, Any]] = []
    for collect in (
        lambda: update_dependencies_alerts(project_dir),
        addon_env_rebuild_alerts,
        lambda: setup_stopped_alerts(project_dir),
        lambda: searxng_setup_alerts(project_dir),
        lambda: searxng_restart_alerts(project_dir),
    ):
        try:
            alerts.extend(collect())
        except Exception:  # 一つが壊れても、他の警告と起動は止めない
            LOGGER.warning("環境の失敗の警告を集められませんでした", exc_info=True)
    return alerts
