"""アップデートに必要な入れ替えが済んでいないことを、画面のバナー (startup_alerts) に出す。

``update_engine`` は、次の二つの場合に SAIVerse フォルダの
``DEPENDENCIES_FAILURE_RECORD`` に失敗の内容を書き残す
(docs/intent/update_completes_with_new_code.md §3)。

- start.bat / start.sh が本体より先に実行する ``update_engine.py --check-complete`` で、
  ``update_dependencies`` を一度やり直し、それが失敗したとき。
- 更新の中 (またはそのやり直しの中) の ``update_dependencies`` で、同梱の Node.js の
  入れ替えか SearXNG の入れ直しが失敗したとき。更新そのものは止めずに続ける。
  この場合、失敗した作業の名前が記録の ``"steps"`` に入る。

どちらの場合も、入れ替えを済ませた記録は書かれないので、次に start.bat / start.sh で
起動したときに入れ替えがもう一度行われる。

この関数は本体の起動時にそのファイルを読み、``manager.startup_alerts`` に積む警告を
返す (``GET /api/system/alerts`` が画面上部のバナーに出す)。ファイルを消すのは
``update_engine`` の側で、次に入れ替えが最後まで成功したときに消す。ここでは消さない。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

LOGGER = logging.getLogger(__name__)

ALERT_ID = "update_dependencies_failed"

# 記録の "steps" に入る名前 (update_engine の DEPENDENCY_STEP_*) と、画面での呼び方。
# update_engine をここで読み込まずに済むよう、名前は文字列で持つ。
_STEP_LABELS = {
    "portable_node": "同梱の Node.js の入れ替え",
    "searxng": "SearXNG の入れ直し",
    "addon_envs": "アドオンの Python 環境の確かめ",
}
# "steps" が無い記録は、入れ替えのやり直し全体が失敗したもの。
_WHOLE_STEP_LABEL = "パッケージの入れ替え (起動前のやり直し)"


def _failed_step_labels(failure: Dict[str, Any]) -> List[str]:
    steps = failure.get("steps")
    if not isinstance(steps, list) or not steps:
        return [_WHOLE_STEP_LABEL]
    return [_STEP_LABELS.get(step, str(step)) for step in steps]


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
        LOGGER.warning("アップデートの入れ替えの失敗の記録を読めませんでした", exc_info=True)
        failure = {}
    if failure is None:
        return []

    details: Dict[str, Any] = {}
    if failure:
        # 中身が読めた記録だけ、何が失敗したかを出す (読めない記録は詳細なし)。
        details["reason"] = _failed_step_labels(failure)
    error = failure.get("error")
    if isinstance(error, str) and error:
        details["error"] = error
    LOGGER.warning(
        "アップデートに必要な入れ替えの一部が失敗しています (バージョン %s、%s): %s",
        failure.get("version") or "不明",
        "、".join(details.get("reason") or ["詳細不明"]),
        error or "詳細は記録されていません",
    )
    return [{
        "id": ALERT_ID,
        "level": "warning",
        "title": "アップデートに必要な入れ替えの一部が失敗しました",
        "message": (
            "このバージョンの SAIVerse に必要なもの (Python のパッケージ、画面のパッケージ、"
            "同梱の Node.js、SearXNG) の入れ替えの一部が、アップデートの中、または起動前の"
            "やり直しで失敗しました。SAIVerse の起動はそのまま続けていますが、失敗したものは"
            "古いまま、または入っていないままの可能性があります。インターネットにつながっていることを確かめ、"
            "SAIVerse の窓 (Backend / Frontend / SearXNG) をすべて閉じてから、start.bat "
            "(Mac / Linux では start.sh) をもう一度実行してください。起動の前に入れ替えが"
            "やり直され、成功するとこの表示は消えます。"
        ),
        "details": details,
    }]
