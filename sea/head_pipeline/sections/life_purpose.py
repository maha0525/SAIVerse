"""LifePurposeSection — ① 共通駆動文 + ② 生きる目的を head に常駐注入する。

autonomous_desire.md §4.5 / §6: ペルソナの自律行動の駆動 (①) と、聞き取りで
確定した「生きる目的 / 趣味 / 仕事」(②、AI.LIFE_PURPOSE) をプロンプトに常駐させ、
候補 Task 生成・Track 化の方向付けとして働かせる。

- 駆動文 (①) は SAIVerse 共通の定数 (全ペルソナ・全モード共通)。
- 生きる目的 (②) は per-persona の動的値。Metabolism 凍結 + 確定時に diff 通知。

詳細: docs/intent/persona_cognition/autonomous_desire.md §3, §4
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from typing import Optional

from sea.head_pipeline.types import (
    LineHeadInput,
    NotificationLabel,
    RenderedSection,
)

LOGGER = logging.getLogger(__name__)

# NOTE: 「生きる目的を定めよ」という行動喚起は head に置かない。head は「ずっとある
# 背景」なのでペルソナ視点では「今反応すべき新しいこと」に見えず、命令文は効かない
# (まはー指摘 2026-06-28)。初回の目的設定は META 判断の専用状況
# (meta_judgment_life_purpose, 未設定の間毎回) が tail/判断サイクルとして行う
# (autonomous_desire.md §10)。本セクションは「設定済みの目的」と「① 駆動文」を
# 背景知識として常駐させるだけに留める。


@dataclass(frozen=True)
class LifePurposeSnapshot:
    drive_text: str       # ① 共通駆動文 (定数)
    purpose_text: str     # ② 生きる目的の整形済みテキスト ("" = 未設定)


class LifePurposeSection:
    name = "life_purpose"
    order = 560  # autonomy_modes(550) の直後 (自律の前提知識とセットで読ませる)
    refresh_on_events = frozenset()  # Metabolism のみ。確定は diff 通知 + 次回再capture

    def capture(self, ctx: LineHeadInput) -> LifePurposeSnapshot:
        from saiverse.life_purpose import (
            DESIRE_DRIVE_TEXT,
            get_life_purpose,
            render_life_purpose_text,
        )

        purpose_text = ""
        manager = ctx.manager
        session_factory = getattr(manager, "SessionLocal", None) if manager else None
        if session_factory is not None:
            try:
                data = get_life_purpose(session_factory, ctx.persona_id)
                purpose_text = render_life_purpose_text(data)
            except Exception:
                LOGGER.warning(
                    "life_purpose: failed to read LIFE_PURPOSE persona=%s",
                    ctx.persona_id, exc_info=True,
                )
        return LifePurposeSnapshot(drive_text=DESIRE_DRIVE_TEXT, purpose_text=purpose_text)

    def render(self, snapshot: LifePurposeSnapshot) -> Optional[RenderedSection]:
        if snapshot is None:
            return None
        # 背景知識として ① 駆動文 (常時) + ② 設定済みの目的 (あれば) のみを出す。
        # 未設定時の行動喚起は head に置かない (META 専用状況が担う)。
        parts = [p for p in (snapshot.drive_text, snapshot.purpose_text) if p]
        if not parts:
            return None
        return RenderedSection(text="\n\n".join(parts))

    def diff_to_notifications(
        self,
        old: Optional[LifePurposeSnapshot],
        new: Optional[LifePurposeSnapshot],
    ) -> list[NotificationLabel]:
        if old is None or new is None:
            return []
        if old.purpose_text != new.purpose_text and new.purpose_text:
            return [NotificationLabel(
                kind="life_purpose_set",
                label="生きる目的が更新されました",
            )]
        return []

    def serialize_snapshot(self, snapshot: LifePurposeSnapshot) -> str:
        return json.dumps(asdict(snapshot), ensure_ascii=False)

    def deserialize_snapshot(self, data: str) -> LifePurposeSnapshot:
        return LifePurposeSnapshot(**json.loads(data))
