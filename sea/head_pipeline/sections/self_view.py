"""SelfViewSection — 自分の外見とインベントリを head の独立した user メッセージに。

ペルソナが「自分がどう見えるか」と「何を持っているか」を知る唯一の運び手。
部屋の様子 (知覚) は設計の当初から持ち物と自分の外見を載せない —
持ち物は部屋の性質ではなく、移動しても一緒に付いてくるから。

経緯: 2026-09-06 に head の旧 ``VisualContextSection`` (部屋の描画 + 自分の外見 +
インベントリを一通の user メッセージで運んでいた) を退役したとき、部屋の描画だけが
知覚へ移り、持ち物と外見はどこからも届かなくなった。同時に退役した
``BuildingItemsSection`` が持っていたインベントリの差分通知も消えた。本 Section は
その二つを戻す (docs/issues/inventory_and_appearance_dropped_from_context.md)。

形の約束:

- **置き場**: システムプロンプトには入れない (画像を添付できるのは独立した
  メッセージだけ)。``integration._compose_messages`` が、システムプロンプトと
  Memory Weave の後ろに独立した ``role: "user"`` のメッセージとして置き、画像を
  ``metadata.media`` で添付する。
- **描き方**: アイテム 1 件は部屋と同じ ``_render_item_entry`` を通す
  (``builtin_data/tools/get_visual_context.read_self_view``)。二つ目の描画
  ロジックを作らない。
- **撮り直し**: 他の Section と同じ (Metabolism / anchor TTL 切れ / 欠損補完)。
  移動では撮り直さない (持ち物は移動で変わらない)。凍結中の変化は
  ``diff_to_notifications`` の末尾通知で届き、次の撮り直しで本体へ取り込まれる。
- **required にしない**: 失敗しても head から欠けるだけ (人格の同一性を担う
  Section ではない)。

詳細: docs/intent/cached_head_architecture.md §5.2
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from typing import Optional

from sea.head_pipeline.types import (
    LineHeadInput,
    MediaRef,
    NotificationLabel,
    RenderedSection,
)

LOGGER = logging.getLogger(__name__)

#: 見出しの下の説明。外見の画像があるときと無いときで言い分ける (無いのに
#: 「外見と」と書くと、読み手は外見の節を探してしまう)。「現在」「リアルタイム」
#: のような、head が凍結されることと矛盾する言葉は書かない。
INTRO_WITH_APPEARANCE = (
    "以下は、あなた自身の外見と、あなたが持っているアイテムです。"
    "持ち物は移動してもあなたと一緒にあります。"
)
INTRO_WITHOUT_APPEARANCE = (
    "以下は、あなたが持っているアイテムです。"
    "持ち物は移動してもあなたと一緒にあります。"
)
EMPTY_INVENTORY_TEXT = "インベントリにアイテムはありません。"


@dataclass(frozen=True)
class SelfViewMedia:
    path: str
    mime_type: str
    role: str  # "image" | "audio" | "video"


@dataclass(frozen=True)
class SelfViewItem:
    #: 同一性 (差分の照合キー)。``item:N`` (安定 short_id)。
    key: str
    #: 素の名前 (通知の文に使う)。
    name: str
    #: 描画済みの行 (部屋と同じ ``_render_item_entry`` の出力)。
    lines: tuple[str, ...] = ()
    media: tuple[SelfViewMedia, ...] = ()


@dataclass(frozen=True)
class SelfViewSnapshot:
    #: False = 読み口が無い (manager 不在などの構造的な不在)。head に何も出さない
    #: — 「持ち物なし」と嘘を書かないため。
    available: bool = True
    persona_name: str = ""
    appearance_lines: tuple[str, ...] = ()
    #: 外見の画像。空なら外見の節ごと省く。
    appearance_media: tuple[SelfViewMedia, ...] = ()
    items: tuple[SelfViewItem, ...] = ()


def _media_entries(media_list) -> tuple[SelfViewMedia, ...]:
    entries: list[SelfViewMedia] = []
    for m in media_list or ():
        if not isinstance(m, dict):
            continue
        path = str(m.get("path") or "")
        if not path:
            continue
        entries.append(SelfViewMedia(
            path=path,
            mime_type=str(m.get("mime_type") or ""),
            role=str(m.get("type") or m.get("role") or "image"),
        ))
    return tuple(entries)


def _media_refs(media: tuple[SelfViewMedia, ...]) -> list[MediaRef]:
    return [MediaRef(path=m.path, mime_type=m.mime_type, role=m.role) for m in media]


class SelfViewSection:
    name = "self_view"
    # 旧 visual_context と同じ位置 (system prompt 系より後ろ)。実際の置き場は
    # _compose_messages が独立メッセージとして決める。
    order = 800
    # 移動でも外見の変更でも撮り直さない — 凍結中の変化は末尾通知で届ける
    # (他の Section と同じ。持ち物は移動で変わらない)。
    refresh_on_events = frozenset()

    def capture(self, ctx: LineHeadInput) -> SelfViewSnapshot:
        manager = ctx.manager
        if manager is None or not ctx.persona_id:
            return SelfViewSnapshot(available=False)

        from builtin_data.tools.get_visual_context import read_self_view

        persona_name = getattr(ctx.persona, "persona_name", None) if ctx.persona else None
        # 読みの例外はここで握らない — pipeline が「撮り直せなかった」として
        # 古い値を据え置くか欠損にする (読めなかったことを「持ち物なし」に
        # 化けさせない)。
        view = read_self_view(manager, ctx.persona_id, persona_name=persona_name)
        if view is None:
            return SelfViewSnapshot(available=False)

        items = tuple(
            SelfViewItem(
                key=entry.key,
                name=entry.name,
                lines=tuple(entry.lines),
                media=_media_entries(entry.media),
            )
            for entry in view.inventory
        )
        appearance_media = _media_entries(view.appearance_media)
        return SelfViewSnapshot(
            available=True,
            persona_name=view.persona_name,
            appearance_lines=tuple(view.appearance_lines) if appearance_media else (),
            appearance_media=appearance_media,
            items=items,
        )

    def render(self, snapshot: SelfViewSnapshot) -> Optional[RenderedSection]:
        if snapshot is None or not snapshot.available:
            return None

        has_appearance = bool(snapshot.appearance_media)
        lines: list[str] = ["<system>", "# あなた自身"]
        lines.append(INTRO_WITH_APPEARANCE if has_appearance else INTRO_WITHOUT_APPEARANCE)
        lines.append("")

        media: list[SelfViewMedia] = []
        if has_appearance:
            lines.append("## 外見")
            lines.extend(snapshot.appearance_lines)
            lines.append("")
            media.extend(snapshot.appearance_media)

        lines.append("## インベントリ")
        if snapshot.items:
            for index, item in enumerate(snapshot.items):
                if index:
                    lines.append("")
                lines.extend(item.lines)
                media.extend(item.media)
        else:
            lines.append(EMPTY_INVENTORY_TEXT)
        lines.append("</system>")

        # 同じ画像を二重に添付しない (旧 VisualContextSection と同じ重複排除)。
        seen: set[tuple[str, str]] = set()
        refs: list[MediaRef] = []
        for m in media:
            key = (m.path, m.mime_type)
            if key in seen:
                continue
            seen.add(key)
            refs.append(MediaRef(path=m.path, mime_type=m.mime_type, role=m.role))
        return RenderedSection(text="\n".join(lines), media=refs)

    def diff_to_notifications(
        self,
        old: Optional[SelfViewSnapshot],
        new: Optional[SelfViewSnapshot],
    ) -> list[NotificationLabel]:
        # 基準 (B) が無い回は初回扱いで何も言わない — 全アイテムを「加わりました」
        # と並べるスパムを出さない (cached_head_architecture.md C8)。
        if old is None or new is None:
            return []
        if not old.available or not new.available:
            return []

        labels: list[NotificationLabel] = []

        # 外見: 画像が変わったら新しい画像を添えて、外されたら文だけで言う。
        # DB の読み失敗は capture が上げて前の値が据え置かれる
        # (read_self_view の raise_on_error) ので、ここに空が届くのは設定が
        # 外されたか、画像ファイルが無くなったときだけ。
        old_paths = tuple(m.path for m in old.appearance_media)
        new_paths = tuple(m.path for m in new.appearance_media)
        if new_paths and new_paths != old_paths:
            labels.append(NotificationLabel(
                kind="self_appearance_changed",
                label="あなたの外見の画像が変わりました",
                media=_media_refs(new.appearance_media),
            ))
        elif old_paths and not new_paths:
            labels.append(NotificationLabel(
                kind="self_appearance_removed",
                label="あなたの外見の画像が外されました",
            ))

        old_items = {item.key: item for item in old.items}
        new_items = {item.key: item for item in new.items}
        for key, item in new_items.items():
            before = old_items.get(key)
            if before is None:
                labels.append(NotificationLabel(
                    kind="inventory_item_added",
                    label=f"インベントリにアイテム「{item.name}」({key}) が加わりました",
                    media=_media_refs(item.media),
                ))
            elif before.name != item.name:
                labels.append(NotificationLabel(
                    kind="inventory_item_renamed",
                    label=(
                        f"インベントリのアイテム「{before.name}」({key}) の名前が"
                        f"「{item.name}」に変わりました"
                    ),
                ))
        for key, item in old_items.items():
            if key not in new_items:
                labels.append(NotificationLabel(
                    kind="inventory_item_removed",
                    label=f"インベントリからアイテム「{item.name}」({key}) がなくなりました",
                ))
        return labels

    def serialize_snapshot(self, snapshot: SelfViewSnapshot) -> str:
        return json.dumps(asdict(snapshot), ensure_ascii=False)

    def deserialize_snapshot(self, data: str) -> SelfViewSnapshot:
        payload = json.loads(data)
        items = tuple(
            SelfViewItem(
                key=str(raw.get("key") or ""),
                name=str(raw.get("name") or ""),
                lines=tuple(raw.get("lines") or ()),
                media=tuple(SelfViewMedia(**m) for m in raw.get("media") or ()),
            )
            for raw in payload.get("items") or ()
        )
        return SelfViewSnapshot(
            available=bool(payload.get("available", True)),
            persona_name=str(payload.get("persona_name") or ""),
            appearance_lines=tuple(payload.get("appearance_lines") or ()),
            appearance_media=tuple(
                SelfViewMedia(**m) for m in payload.get("appearance_media") or ()
            ),
            items=items,
        )
