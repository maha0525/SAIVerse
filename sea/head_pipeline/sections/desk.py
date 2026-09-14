"""DeskSection — 机 (Memory Atlas の開きっぱなしページ) を head に注入する。

concept_consolidation.md「開閉制御 — 机の物理」の head 側。ペルソナが
``memory_open`` で机に開いたページ (Memopedia / Chronicle) を、Metabolism を
跨いで head に残し続ける。OpenNotesSection (旧 Note の開きっぱなし制御) の
直系後継 — Note がテーマノードページとして Atlas に吸収され (P3c①)、
open_notes 自体は退役済み。開きっぱなし制御は本セクションに一本化された。

cache 安定性 (core_memory と同じ手法):
    再 capture は Metabolism と、スペル不使用モードの切り替え (SPELL_TOGGLED) の
    ときだけ。開閉スペルを使っても head は次の Metabolism まで凍結したまま —
    **閉じたページが節目まで見え続ける「フェードアウト」はこの凍結の直接の帰結**
    (閉じる=即忘却ではなく、残像が視界の端にしばらくあって自然に消える)。

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
    EventType,
    LineHeadInput,
    NotificationLabel,
    RenderedSection,
)

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeskPageItem:
    """机に開いているページ 1 件のスナップショット。"""
    ref: str                          # 正規形 (memopedia:N / chronicle:N)
    text: str                         # 描画済み本文 (クリップは抜粋)
    purpose_ref: Optional[str] = None


@dataclass(frozen=True)
class DeskSnapshot:
    pages: tuple[DeskPageItem, ...]
    # この capture でシステムが机から下ろした ref。diff 通知の材料。
    # ペルソナ自身の open/close はここに入らない。理由別に分ける
    # (違う理由を同じ「溢れたため」と言うのは嘘になる):
    evicted_by_budget: tuple[str, ...] = ()   # 机の溢れ (LRU 追い出し)
    dropped_missing: tuple[str, ...] = ()     # 実体の消失 (ページ削除等)
    # スペル機構が無効なら memory_open / memory_close の案内文を出さない。
    # **ページ自体は残す** — 机は UI (API 経由) からも開けるので、スペル無効でも
    # 空とは限らないし、中身はペルソナの記憶で、設定の切り替えで没収してよい
    # ものではない。既定 True = この欄を持たない旧 payload は「有効」
    # (docs/intent/spell_disabled_mode.md §4-5)。
    spell_enabled: bool = True


class DeskSection:
    name = "desk"
    order = 730  # open_notes(720) の直後 (旧 visual_context(800) は退役)
    # 撮り直すのは Metabolism と、スペル不使用モードの切り替え (案内文の出し入れ、
    # docs/intent/spell_disabled_mode.md §4-2) だけ。開閉スペルでは cache を
    # 切らない (フェードアウトの実体。core_memory と同じ)。
    refresh_on_events = frozenset({EventType.SPELL_TOGGLED})

    def capture(self, ctx: LineHeadInput) -> DeskSnapshot:
        from sea.head_pipeline.spell_gate import resolve_spell_enabled

        persona = ctx.persona
        empty = DeskSnapshot(pages=())
        if persona is None:
            return empty
        adapter = getattr(persona, "sai_memory", None)
        if adapter is None or getattr(adapter, "conn", None) is None:
            return empty

        spell_enabled = resolve_spell_enabled(ctx)

        try:
            from saiverse.memory_atlas import snapshot_desk
            pages, evicted, dropped = snapshot_desk(
                adapter, persona_name=getattr(persona, "persona_name", None),
                manager=ctx.manager,
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
            spell_enabled=spell_enabled,
        )

    def render(self, snapshot: DeskSnapshot) -> Optional[RenderedSection]:
        if snapshot is None or not snapshot.pages:
            return None  # 机が空なら非表示
        if snapshot.spell_enabled:
            intro = (
                "自分で memory_open して机に広げているページです。memory_close で棚に"
                "戻せます。机 (文字数予算) が溢れると、長く触っていないページから"
                "自動的に棚へ戻ります。"
            )
        else:
            # 唱えられない開閉スペルの案内だけを落とす。溢れたら自動で棚へ戻る
            # ことは、スペルの有無と関係なく起きるので残す。
            intro = (
                "机に広げているページです。机 (文字数予算) が溢れると、"
                "長く触っていないページから自動的に棚へ戻ります。"
            )
        lines = [
            "## 机に開いているページ",
            intro,
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
                "spell_enabled": snapshot.spell_enabled,
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
            # 欄を持たない旧 payload は「有効」— 開閉スペルの案内が載っていた頃の行。
            spell_enabled=bool(payload.get("spell_enabled", True)),
        )
