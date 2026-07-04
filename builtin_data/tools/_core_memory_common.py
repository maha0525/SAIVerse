"""コア記憶スペル群 (core_memory_add / update / remove) の共有ヘルパー。

- 容量目安の解決 (per-persona CORE_MEMORY_CHAR_BUDGET, NULL → 既定 2000)
- 目安超過通知の文言生成 (切り詰めは一切しない、通知のみ)
- ``c:3`` / ``3`` どちらの ID 形式も受理するパーサ

詳細: docs/intent/memory_architecture_v2.md §5
"""
from __future__ import annotations

import logging
from typing import Optional

from tools.context import get_active_manager

LOGGER = logging.getLogger(__name__)

DEFAULT_CORE_MEMORY_CHAR_BUDGET = 2000


def resolve_core_memory_budget(persona_id: str) -> int:
    """ペルソナの CORE_MEMORY_CHAR_BUDGET を解決する。未設定なら既定 2000。"""
    manager = get_active_manager()
    session_factory = getattr(manager, "SessionLocal", None) if manager else None
    if not session_factory:
        return DEFAULT_CORE_MEMORY_CHAR_BUDGET
    db = session_factory()
    try:
        from database.models import AI as AIModel
        ai = db.query(AIModel).filter_by(AIID=persona_id).first()
        if ai is None:
            return DEFAULT_CORE_MEMORY_CHAR_BUDGET
        value = ai.CORE_MEMORY_CHAR_BUDGET
        if value is None or int(value) <= 0:
            return DEFAULT_CORE_MEMORY_CHAR_BUDGET
        return int(value)
    except Exception:
        LOGGER.warning(
            "core_memory: failed to resolve CORE_MEMORY_CHAR_BUDGET persona=%s",
            persona_id, exc_info=True,
        )
        return DEFAULT_CORE_MEMORY_CHAR_BUDGET
    finally:
        db.close()


def format_budget_note(total_chars: int, budget: int) -> str:
    """目安超過時の通知文を返す (超過していなければ空文字)。

    切り詰め・拒否は一切しない。ペルソナに整理を促す通知を添えるだけ
    (docs/intent/memory_architecture_v2.md §5)。
    """
    if total_chars > budget:
        return "目安を超えています。整理を検討してください。"
    return ""


def resolve_persona_display_name(persona_id: str) -> str:
    """ペルソナの表示名 (AINAME) を解決する。取得できなければ persona_id を返す。"""
    manager = get_active_manager()
    session_factory = getattr(manager, "SessionLocal", None) if manager else None
    if not session_factory:
        return persona_id
    db = session_factory()
    try:
        from database.models import AI as AIModel
        ai = db.query(AIModel).filter_by(AIID=persona_id).first()
        if ai is None or not ai.AINAME:
            return persona_id
        return str(ai.AINAME)
    except Exception:
        LOGGER.warning(
            "core_memory: failed to resolve AINAME persona=%s",
            persona_id, exc_info=True,
        )
        return persona_id
    finally:
        db.close()


def parse_core_memory_id(raw) -> Optional[int]:
    """``c:3`` / ``3`` / ``C:3`` のいずれの形式も受理して int の id を返す。

    解釈できなければ None を返す。
    """
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    text = str(raw).strip()
    if not text:
        return None
    if text.lower().startswith("c:"):
        text = text[2:].strip()
    try:
        value = int(text)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


_MESSAGE_URI_PREFIX = "saiverse://self/message/"


def parse_message_ref(raw) -> Optional[str]:
    """``saiverse://self/message/<id>`` URI と生 ID の両方を受理し message_id を返す。

    自動想起 (unified_recall) が浮かべるハンドルは URI 形式 (``uri=f"saiverse://self/
    message/{msg_id}"``, sai_memory/unified_recall.py) なので、ペルソナがそのまま
    貼り付けても解決できるようにする。解釈できなければ None。
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text.startswith(_MESSAGE_URI_PREFIX):
        text = text[len(_MESSAGE_URI_PREFIX):].strip()
    return text or None
