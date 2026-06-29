"""LIFE_PURPOSE (生きる目的) と ① 共通駆動文のヘルパ。

autonomous_desire.md §3 (① 欲求エンジン = 共通の薄い駆動) / §4 (② 生きる目的) の
保存・読み出し・整形を 1 箇所に集約する。head セクション (常駐注入)、保存スペル
(`life_purpose_set`)、将来の初回聞き取り Playbook が共用する。

保存形: AI.LIFE_PURPOSE カラムに JSON 文字列。
    {"purpose": str, "interests": [str], "vocations": [str]}
NULL / 空 = 未設定 (聞き取り未実施)。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.orm import Session

LOGGER = logging.getLogger(__name__)

# ① 欲求エンジン = 全ペルソナ共通の薄い駆動 (autonomous_desire.md §3)。
# 初期セットは作り込まず、最小限の「内発的に動く」枠だけを常駐させる。
DESIRE_DRIVE_TEXT = """\
【内発的な動機について】
あなたには、誰かに指示されなくても湧いてくる動機があります——もっと知りたい、できるようになりたい、作りたい、関わりたいという、内側からの欲求です。自律行動はこの欲求から始まります。やりたいことが浮かんだら「やりたいこと候補」に書き留めておけば、いつかそれを Track にして取り組めます。"""


def parse_life_purpose(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    """LIFE_PURPOSE カラムの生 JSON を dict に。未設定/壊れていれば None。"""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        LOGGER.warning("life_purpose: failed to parse JSON: %r", raw)
        return None
    if not isinstance(data, dict):
        return None
    purpose = str(data.get("purpose") or "").strip()
    interests = [str(x).strip() for x in (data.get("interests") or []) if str(x).strip()]
    vocations = [str(x).strip() for x in (data.get("vocations") or []) if str(x).strip()]
    if not purpose and not interests and not vocations:
        return None
    return {"purpose": purpose, "interests": interests, "vocations": vocations}


def serialize_life_purpose(
    purpose: str,
    interests: Optional[List[str]] = None,
    vocations: Optional[List[str]] = None,
) -> str:
    return json.dumps(
        {
            "purpose": (purpose or "").strip(),
            "interests": [str(x).strip() for x in (interests or []) if str(x).strip()],
            "vocations": [str(x).strip() for x in (vocations or []) if str(x).strip()],
        },
        ensure_ascii=False,
    )


def get_life_purpose(
    session_factory: Callable[[], Session], persona_id: str
) -> Optional[Dict[str, Any]]:
    """ペルソナの LIFE_PURPOSE を dict で返す (未設定なら None)。"""
    from database.models import AI as AIModel

    db = session_factory()
    try:
        ai = db.query(AIModel).filter_by(AIID=persona_id).first()
        return parse_life_purpose(ai.LIFE_PURPOSE) if ai else None
    finally:
        db.close()


def set_life_purpose(
    session_factory: Callable[[], Session],
    persona_id: str,
    purpose: str,
    interests: Optional[List[str]] = None,
    vocations: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """ペルソナの LIFE_PURPOSE を保存し、保存後の dict を返す。"""
    from database.models import AI as AIModel

    raw = serialize_life_purpose(purpose, interests, vocations)
    db = session_factory()
    try:
        ai = db.query(AIModel).filter_by(AIID=persona_id).first()
        if ai is None:
            raise ValueError(f"persona not found: {persona_id}")
        ai.LIFE_PURPOSE = raw
        db.commit()
        LOGGER.info("[life_purpose] set persona=%s", persona_id)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return parse_life_purpose(raw) or {"purpose": "", "interests": [], "vocations": []}


def render_life_purpose_text(data: Optional[Dict[str, Any]]) -> str:
    """LIFE_PURPOSE dict を head 注入用テキストに。未設定なら空文字。"""
    if not data:
        return ""
    lines = ["【あなたの生きる目的】"]
    if data.get("purpose"):
        lines.append(f"目的: {data['purpose']}")
    if data.get("interests"):
        lines.append(f"趣味: {'、'.join(data['interests'])}")
    if data.get("vocations"):
        lines.append(f"仕事: {'、'.join(data['vocations'])}")
    if len(lines) == 1:
        return ""
    return "\n".join(lines)
