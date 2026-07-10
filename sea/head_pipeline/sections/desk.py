"""DeskSection — 机 (Memory Atlas の開きっぱなしページ) を head に注入する。

concept_consolidation.md「開閉制御 — 机の物理」の head 側。ペルソナが
``memory_open`` で机に開いたページ (Memopedia / Chronicle) を、Metabolism を
跨いで head に残し続ける。OpenNotesSection (旧 Note の開きっぱなし制御) の
直系後継 — Note がテーマノードとして Atlas に吸収されたら open_notes は
本セクションに畳まれる予定 (P2c/P3c)。

cache 安定性 (open_notes / core_memory と同じ手法):
    ``refresh_on_events = frozenset()`` = Metabolism のみ再 capture。開閉スペル
    を使っても head は次の Metabolism まで凍結したまま — **閉じたページが節目
    まで見え続ける「フェードアウト」はこの凍結の直接の帰結** (閉じる=即忘却
    ではなく、残像が視界の端にしばらくあって自然に消える)。

Metabolism 追い出しフック: capture 冒頭の ``memory_atlas.snapshot_desk`` が
予算を再評価して溢れ分を LRU で棚に戻す (ページは成長するので、開いた時に
収まっていても節目には溢れていることがある)。追い出し・実体消失による
自動クローズは diff 通知でペルソナに伝える (開閉スペルは本人の行為なので
通知しない — 通知するのはシステムが勝手に動かした分だけ)。

詳細: docs/intent/concept_consolidation.md「開閉制御 — 机の物理」
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


@dataclass(frozen=True)
class DeskPageItem:
    """机に開いているページ 1 件のスナップショット。"""
    ref: str                          # 正規形 (m:N / ch:N)
    text: str                         # 描画済み本文 (写真は抜粋)
    purpose_ref: Optional[str] = None


@dataclass(frozen=True)
class DeskSnapshot:
    pages: tuple[DeskPageItem, ...]
    # この capture でシステムが机から下ろした ref。diff 通知の材料。
    # ペルソナ自身の open/close はここに入らない。理由別に分ける
    # (違う理由を同じ「溢れたため」と言うのは嘘になる):
    evicted_by_budget: tuple[str, ...] = ()   # 机の溢れ (LRU 追い出し)
    dropped_missing: tuple[str, ...] = ()     # 実体の消失 (ページ削除等)


class DeskSection:
    name = "desk"
    order = 730  # open_notes(720) の直後、visual_context(800) の前
    # refresh_on_events 空 = Metabolism のみ。開閉スペルでは cache を切らない
    # (フェードアウトの実体。open_notes / core_memory と同じ)。
    refresh_on_events = frozenset()

    def capture(self, ctx: LineHeadInput) -> DeskSnapshot:
        persona = ctx.persona
        empty = DeskSnapshot(pages=())
        if persona is None:
            return empty
        adapter = getattr(persona, "sai_memory", None)
        if adapter is None or getattr(adapter, "conn", None) is None:
            return empty

        try:
            from saiverse.memory_atlas import snapshot_desk
            pages, evicted, dropped = snapshot_desk(
                adapter, persona_name=getattr(persona, "persona_name", None)
            )
        except Exception:
            LOGGER.warning(
                "desk: failed to snapshot desk persona=%s",
                ctx.persona_id, exc_info=True,
            )
            return empty

        return DeskSnapshot(
            pages=tuple(
                DeskPageItem(ref=p.ref, text=p.text, purpose_ref=p.purpose_ref)
                for p in pages
            ),
            evicted_by_budget=tuple(evicted),
            dropped_missing=tuple(dropped),
        )

    def render(self, snapshot: DeskSnapshot) -> Optional[RenderedSection]:
        if snapshot is None or not snapshot.pages:
            return None  # 机が空なら非表示
        lines = [
            "## 机に開いているページ",
            "自分で memory_open して机に広げているページです。memory_close で棚に"
            "戻せます。机 (文字数予算) が溢れると、長く触っていないページから"
            "自動的に棚へ戻ります。",
        ]
        for page in snapshot.pages:
            lines.append("")
            lines.append(page.text)
        return RenderedSection(text="\n".join(lines))

    def diff_to_notifications(
        self,
        old: Optional[DeskSnapshot],
        new: Optional[DeskSnapshot],
    ) -> list[NotificationLabel]:
        # 開閉スペルは本人の行為なので通知しない。通知するのはシステムが
        # 勝手に机から下ろした分だけ — 理由別に正直に言う。
        if new is None:
            return []
        labels: list[NotificationLabel] = []
        if new.evicted_by_budget:
            refs = "、".join(new.evicted_by_budget)
            labels.append(NotificationLabel(
                kind="desk_evicted",
                label=f"机が溢れたため {refs} を棚に戻しました",
            ))
        if new.dropped_missing:
            refs = "、".join(new.dropped_missing)
            labels.append(NotificationLabel(
                kind="desk_dropped",
                label=f"{refs} は元のページが失われたため机から下ろしました",
            ))
        return labels

    def serialize_snapshot(self, snapshot: DeskSnapshot) -> str:
        return json.dumps(
            {
                "pages": [asdict(p) for p in snapshot.pages],
                "evicted_by_budget": list(snapshot.evicted_by_budget),
                "dropped_missing": list(snapshot.dropped_missing),
            },
            ensure_ascii=False,
        )

    def deserialize_snapshot(self, data: str) -> DeskSnapshot:
        payload = json.loads(data)
        pages = tuple(DeskPageItem(**p) for p in payload.get("pages", []))
        return DeskSnapshot(
            pages=pages,
            evicted_by_budget=tuple(payload.get("evicted_by_budget", [])),
            dropped_missing=tuple(payload.get("dropped_missing", [])),
        )
