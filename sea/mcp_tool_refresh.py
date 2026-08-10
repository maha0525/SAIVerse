"""per_persona MCP のツール一覧を、頭 (Pulse / Beat の始まり) で取り直す一手。

設計の正典は docs/intent/mcp_addon_integration.md §I。要点だけ:

- ツール一覧の真実はサーバー側にあり、証言できるのは **そのペルソナ自身の鍵で
  張った生きた接続** だけ。起動時に代表者の鍵を借りて一括登録する discovery は
  廃止された。
- 接続を張るのは **Pulse の頭** (``connect=True``)。一覧を聞き直すのは
  **Beat の頭** (``connect=False`` — 生きている接続の上でだけ)。
- 並びは必ず「取得 → 検知 → 消費 (flush) → 生成」。検知が消費より後だと、
  一覧の変動が知覚バッファに積まれたまま次の Beat まで読まれない。

この関数を置く理由は、Pulse root が 2 つあること (run_meta_user と
run_work_session)。片方だけに取得を書くと、もう片方は接続ゼロのまま head を
組む — 実際に作業セッションが素通しだった (Codex レビュー 2026-08-10)。
以後 Pulse root を増やすときは、この 1 行を頭に置けば済む。
"""

import logging
from typing import Any, Optional

LOGGER = logging.getLogger(__name__)

# 接続を張る回は subprocess 起動 / TLS 込みなので長め、生きた接続に聞き直す
# だけの回は短め (Beat 頻度で回るため、遅いサーバーに Beat を待たせない)。
_PULSE_HEAD_TIMEOUT_SECONDS = 15.0
_BEAT_HEAD_TIMEOUT_SECONDS = 8.0


def refresh_mcp_tools_at_head(
    persona: Any,
    manager: Any,
    building_id: Optional[str],
    *,
    connect: bool,
    notify: bool = True,
) -> bool:
    """ツール一覧を取り直し、変わっていたら検知器へ渡す。

    Args:
        persona: 対象ペルソナ (``persona_id`` を持つもの)。
        manager: SAIVerseManager (検知器へ渡す。None 可)。
        building_id: 検知の基準となる現在地。None なら検知は行わない
            (差分は次に building が確定した頭で拾われる)。
        connect: True = Pulse 頭 (無ければ接続を張る) / False = Beat 頭
            (生きている接続の上でだけ聞き直す)。
        notify: 変化を検知器 (``inject_diff_notifications``) へ渡すか。
            直後に無条件の検知フェーズが走る呼び出し元 (run_meta_user) だけ
            False にする — 同じ検知を二重に走らせないため。

    Returns:
        このペルソナのツール所属が変わったら True。例外は投げない
        (取得の失敗で Pulse を止めない — 失敗は次の頭で再試行される)。
    """
    persona_id = str(getattr(persona, "persona_id", "") or "")
    if not persona_id:
        return False

    timeout = (
        _PULSE_HEAD_TIMEOUT_SECONDS if connect else _BEAT_HEAD_TIMEOUT_SECONDS
    )
    try:
        from tools.mcp_client import refresh_persona_tools_sync

        changed = bool(
            refresh_persona_tools_sync(
                persona_id, connect=connect, timeout=timeout,
            )
        )
    except Exception:
        LOGGER.warning(
            "[mcp] per-persona tool refresh failed at head "
            "(persona=%s, connect=%s); continuing",
            persona_id, connect, exc_info=True,
        )
        return False

    if not changed or not notify or not building_id:
        return changed

    try:
        from sea.head_pipeline import inject_diff_notifications

        inject_diff_notifications(persona, manager, building_id)
    except Exception:
        LOGGER.warning(
            "[mcp] tool-list change detected but the perception push failed "
            "(persona=%s); the diff stays for the next head",
            persona_id, exc_info=True,
        )
    return True
