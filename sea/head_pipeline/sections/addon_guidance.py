"""AddonGuidanceSection — アドオンからの案内を head に置く。

アドオンは manifest の ``context_provider`` で「この部屋ではこういう書き方が
使える」のような案内を返せる (例: Godot 仮想身体の部屋の全身エモート記法)。
提供口の登録簿は :mod:`saiverse.addon_context`。

なぜ head + 差分通知なのか (2026-10-12、docs/intent/addon_speak_hooks.md):
    2026-10-11 の初版は案内をリアルタイム情報 (毎ターン差し替わる末尾の
    メッセージ) に載せた。それだと部屋を出て案内が消えても「使えなくなった」
    という出来事がペルソナの知覚にも記憶にも残らず、履歴には自分が過去に
    書いた記法だけが残るので、一度使ったら使い続けてしまう恐れがある。
    そこで Building 固有のスペルと同じ道にした — head に置いて Metabolism まで
    凍結し、案内が増えた・消えた・変わったことは ``diff_to_notifications`` が
    「[システム通知]」として知覚に届ける。

cache 安定性:
    ``refresh_on_events = frozenset()`` = Metabolism のみ再 capture
    (facilities と同じ考え方)。増減は差分通知で届く。順序は addon 名の昇順で
    固定 — 同じ世界なら毎回同じ文字列が出ることが prefix キャッシュの前提。

head は次の Metabolism まで凍結されるので、案内が新しく現れたときや変わった
ときは、差分通知が案内の本文を運ぶ唯一の情報源になる。だから通知には head と
同じ本文をそのまま同梱する (spell_list の付与通知と同じ原則)。

詳細: docs/intent/cached_head_architecture.md §5.3
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

SECTION_HEADING = "## アドオンからの案内"

LABEL_KIND_ADDED = "addon_guidance_added"
LABEL_KIND_REMOVED = "addon_guidance_removed"
LABEL_KIND_CHANGED = "addon_guidance_changed"


def _notice_metadata(label_kind: str) -> dict:
    """案内の通知に「用が済んだら下ろす操作通知」の型を刻む。

    スペル一覧の通知 (spell_list._notice_metadata) と同じ理由: head の案内が
    Metabolism で今の内容に描き直された後、この通知は同じことを二度言って
    いるだけになるので、提示の節約
    (:data:`sai_memory.presented_reduction.NOTICE_LABEL_KINDS`) が下ろす。
    型を metadata に載せないと、移動や入退室のような下ろしてはいけない
    ``world_state`` 通知と見分けが付かない。
    """
    from sai_memory.room_state import LABEL_KIND_META_KEY

    return {LABEL_KIND_META_KEY: label_kind}


@dataclass(frozen=True)
class AddonGuidance:
    """一つのアドオンからの案内。"""
    addon_name: str
    display_name: str
    text: str


@dataclass(frozen=True)
class AddonGuidanceSnapshot:
    items: tuple[AddonGuidance, ...] = ()


def _display(item: AddonGuidance) -> str:
    return item.display_name or item.addon_name


def _bundle(blocks: list[str], plural_heading: str) -> str:
    """同種の変化が一度に複数あれば 1 ラベルに束ねる (見出しが並ばないように)。"""
    if len(blocks) == 1:
        return blocks[0]
    return "\n\n".join([plural_heading] + blocks)


class AddonGuidanceSection:
    name = "addon_guidance"
    order = 610  # spell_list(600) の直後 = 「唱えられるもの」の次に「書き方の案内」
    refresh_on_events = frozenset()  # Metabolism のみ。増減・変化は差分通知で届く

    # ---- capture ----

    def capture(self, ctx: LineHeadInput) -> AddonGuidanceSnapshot:
        from saiverse.addon_context import addon_display_name, collect_guidance

        items = [
            AddonGuidance(
                addon_name=addon_name,
                display_name=addon_display_name(addon_name),
                text=text,
            )
            for addon_name, text in collect_guidance(
                ctx.persona, ctx.current_building_id,
            )
        ]
        items.sort(key=lambda item: item.addon_name)
        return AddonGuidanceSnapshot(items=tuple(items))

    # ---- render ----

    def render(self, snapshot: AddonGuidanceSnapshot) -> Optional[RenderedSection]:
        if snapshot is None or not snapshot.items:
            return None
        lines: list[str] = [SECTION_HEADING]
        for item in snapshot.items:
            lines.append(f"### {_display(item)}")
            lines.append(item.text)
        return RenderedSection(text="\n".join(lines))

    # ---- diff ----

    def diff_to_notifications(
        self,
        old: Optional[AddonGuidanceSnapshot],
        new: Optional[AddonGuidanceSnapshot],
    ) -> list[NotificationLabel]:
        if old is None or new is None:
            return []

        old_by_name = {item.addon_name: item for item in old.items}
        new_by_name = {item.addon_name: item for item in new.items}

        labels: list[NotificationLabel] = []

        added_blocks = [
            f"「{_display(new_by_name[name])}」からの案内が届きました\n"
            f"{new_by_name[name].text}"
            for name in sorted(new_by_name.keys() - old_by_name.keys())
        ]
        if added_blocks:
            labels.append(NotificationLabel(
                kind=LABEL_KIND_ADDED,
                label=_bundle(
                    added_blocks,
                    f"アドオンからの案内が {len(added_blocks)} 件届きました",
                ),
                metadata=_notice_metadata(LABEL_KIND_ADDED),
            ))

        changed_blocks = [
            f"「{_display(new_by_name[name])}」からの案内が変わりました\n"
            f"{new_by_name[name].text}"
            for name in sorted(new_by_name.keys() & old_by_name.keys())
            if new_by_name[name].text != old_by_name[name].text
        ]
        if changed_blocks:
            labels.append(NotificationLabel(
                kind=LABEL_KIND_CHANGED,
                label=_bundle(
                    changed_blocks,
                    f"アドオンからの案内が {len(changed_blocks)} 件変わりました",
                ),
                metadata=_notice_metadata(LABEL_KIND_CHANGED),
            ))

        # 消えた案内は本文を繰り返さない。何が使えなくなったかは名前で分かり、
        # 「その書き方を使わない」ことだけを伝えればよい。
        removed_names = sorted(old_by_name.keys() - new_by_name.keys())
        if removed_names:
            displays = "、".join(
                f"「{_display(old_by_name[name])}」" for name in removed_names
            )
            which = "この案内" if len(removed_names) == 1 else "これらの案内"
            labels.append(NotificationLabel(
                kind=LABEL_KIND_REMOVED,
                label=(
                    f"{displays}からの案内は、今は当てはまらなくなりました。"
                    f"{which}にあった書き方は使わないでください。"
                ),
                metadata=_notice_metadata(LABEL_KIND_REMOVED),
            ))
        return labels

    # ---- serialize / deserialize ----

    def serialize_snapshot(self, snapshot: AddonGuidanceSnapshot) -> str:
        return json.dumps(
            {"items": [asdict(item) for item in snapshot.items]},
            ensure_ascii=False,
        )

    def deserialize_snapshot(self, data: str) -> AddonGuidanceSnapshot:
        payload = json.loads(data)
        items = tuple(
            AddonGuidance(
                addon_name=str(raw.get("addon_name", "")),
                display_name=str(raw.get("display_name", "")),
                text=str(raw.get("text", "")),
            )
            for raw in payload.get("items", [])
            if isinstance(raw, dict)
        )
        return AddonGuidanceSnapshot(items=items)
