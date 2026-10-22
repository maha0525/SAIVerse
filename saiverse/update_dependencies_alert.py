"""起動前のパッケージの入れ直しが失敗したことを、画面のバナー (startup_alerts) に出す。

start.bat / start.sh は本体より先に ``update_engine.py --check-complete`` を実行する。
古い更新処理で更新された環境では、そこで一度だけ ``update_dependencies`` をやり直す
(docs/intent/update_completes_with_new_code.md §3)。この時点では本体がまだ動いて
いないので、失敗した場合 ``update_engine`` は内容を SAIVerse フォルダの
``DEPENDENCIES_FAILURE_RECORD`` に書き残し、起動はそのまま続ける。

この関数は本体の起動時にそのファイルを読み、``manager.startup_alerts`` に積む警告を
返す (``GET /api/system/alerts`` が画面上部のバナーに出す)。ファイルを消すのは
``update_engine`` の側で、次に入れ直しが成功したときに消す。ここでは消さない。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

LOGGER = logging.getLogger(__name__)

ALERT_ID = "update_dependencies_failed"


def update_dependencies_alerts(project_dir: Path) -> List[Dict[str, Any]]:
    """失敗の記録があれば、startup_alerts と同じ形の警告を 1 件返す。無ければ空。

    起動を止めないため、例外は出さない (読めないファイルは「失敗した、詳細は不明」
    として扱う)。
    """
    try:
        # 起動の途中で読み込む。update_engine の読み込み自体が失敗しても起動を止めない。
        from scripts.update_engine import read_dependencies_failure

        failure = read_dependencies_failure(project_dir)
    except Exception:  # 記録を読めないことで起動を止めない
        LOGGER.warning("パッケージの入れ直しの失敗の記録を読めませんでした", exc_info=True)
        failure = {}
    if failure is None:
        return []

    details: Dict[str, Any] = {}
    error = failure.get("error")
    if isinstance(error, str) and error:
        details["error"] = error
    LOGGER.warning(
        "起動前のパッケージの入れ直しが失敗しています (バージョン %s): %s",
        failure.get("version") or "不明",
        error or "詳細は記録されていません",
    )
    return [{
        "id": ALERT_ID,
        "level": "warning",
        "title": "このバージョンのパッケージの入れ直しに失敗しました",
        "message": (
            "前回のアップデートは古い更新処理で行われたため、このバージョンに必要な"
            "パッケージの入れ替えの一部が行われていませんでした。起動の前にもう一度"
            "入れ直そうとしましたが、失敗しました。SAIVerse の起動はそのまま続けています。"
            "インターネットにつながっていることを確かめ、SAIVerse の窓 (Backend / "
            "Frontend / SearXNG) をすべて閉じてから、start.bat (Mac / Linux では "
            "start.sh) をもう一度実行してください。入れ直しが成功すると、この表示は"
            "消えます。"
        ),
        "details": details,
    }]
