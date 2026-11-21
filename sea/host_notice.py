"""部屋の記録 (building_messages) へ host 名義の一行を置く、共通の書き手。

使うのは二つの機構:

- 返事が途中で止まった回の中断の通告 (``sea/reply_stop_exit.py``、
  docs/intent/reply_stop_exit.md)
- エラーで発言が生まれなかった回の知らせ (``sea/no_utterance_notice.py``、
  docs/intent/no_utterance_error_notice.md)

どちらも、その部屋に居合わせた全員 (在室者 + 本人) に届ける。取り込み
(``builtin_data/tools/get_building_messages.py`` の ``_transcribe_message``) が
建物名を添えて role=system のシステム通知へ組み替えてから各ペルソナの記憶へ
配るので、ここでその形を自分で作らない。取り込みが記憶へ写すのは本文だけで、
行の ``metadata`` は写さない — 画面だけに見せる材料は ``metadata`` の ``__`` 始まりの
鍵に置く (docs/intent/system_notice_delivery.md 不変条件 4)。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

LOGGER = logging.getLogger(__name__)


def record_host_notice(
    manager: Any,
    persona: Any,
    building_id: str,
    *,
    content: str,
    metadata: Optional[Dict[str, Any]] = None,
    ref: Optional[str] = None,
) -> bool:
    """部屋の記録へ host 名義で一行置く。置けたら True。

    取り込みが配るのは ``heard_by`` に載ったペルソナだけ — 在室者を渡さないと、
    行は部屋の記録に残るだけで誰の記憶にも永遠に届かない (2026-08-27 の実機
    検証で発覚)。本人は在室者に居なくても (スペルで部屋を移った後など) 載せる。

    書き込みに失敗しても例外は投げない (知らせは救済であって、本体ではない) が、
    置けたかどうかは返す。``ref`` はログに残す手がかり (発言の id など)。
    """
    try:
        heard_by = list(manager.occupants.get(building_id, []) or [])
        # 直接参照 (getattr にしない) — 属性名のタイポを黙って飲むと、本人が
        # heard_by から静かに落ちて行だけ配られる。無ければ except が握る
        # (通知ごと見送り + WARNING)。
        persona_id = persona.persona_id
        if persona_id and persona_id not in heard_by:
            heard_by.append(persona_id)
        row: Dict[str, Any] = {"role": "host", "content": content}
        if metadata:
            row["metadata"] = dict(metadata)
        written = persona.history_manager.add_to_building_only(
            building_id, row, heard_by=heard_by,
        )
        # 置けたかの判定は、戻り値に DB 採番の message_id が付いているか
        # (builtin_data/tools/tell.py と同じ裁定)。add_to_building_only は
        # DB 挿入が再試行の後に失敗しても例外を投げず、渡した dict を id 無しで
        # 返す — 例外が出なかったことは、置けた証拠にならない。
        if isinstance(written, dict) and written.get("message_id"):
            return True
        LOGGER.warning(
            "[host_notice] the host notice was not persisted to the building "
            "(ref=%s building=%s)", ref, building_id,
        )
        return False
    except Exception:
        LOGGER.warning(
            "[host_notice] could not record the host notice to the building "
            "(ref=%s building=%s)", ref, building_id, exc_info=True,
        )
        return False


__all__ = ["record_host_notice"]
