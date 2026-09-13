"""埋もれたアイテムを見る (スペル ``buried_items_view``)。

正典: docs/intent/room_item_display_cap.md 設計 3。

部屋の様子には「最近触られた順」の上位だけが並び、残りは表示から埋もれる —
散らかった部屋では物が見えなくなる、という世界の物理 (設計 1)。**物は消えない**
(不変条件 1) ので、埋もれた物へ届く道が要る。このスペルがその道。

出すのは今いる部屋の**全アイテム** — 建物に直接置かれた物に加えて、**部屋にある
入れ物 (Bag) の中身も入れ子の奥まで**含める (2026-09-11 第三回裁定)。開いた入れ物
の中身の描画にも上限が入ったので、入れ物の中で埋もれた物への道もこのスペル一本が
担う。並びは全体で一本の「最近触られた順」で、各件に置き場所を添える — 部屋に
直接置かれた物はそのまま、入れ物の中の物は「（入れ物「◯◯」の中）」。

- 1 ページ :data:`ITEMS_PER_PAGE` 件。ページ指定は ``page="2"`` の一枚と
  ``page="1-3"`` の範囲の両方を受け、一度に開けるのは 5 ページまで
  (:mod:`builtin_data.tools._paging_common`)。
- **一件の説明は途中で切らない** (不変条件 2・4)。省略の単位は件数だけ。
- 一件の描き方は部屋の様子と同じ (:func:`builtin_data.tools.
  get_visual_context._render_item`) — 同じ物が二つの見え方をしないように、
  描画は一箇所から借りる。ただし入れ物の**中身の入れ子描画だけは外す** — 中身は
  一件ずつ独立に出すので、入れ物の描画にもう一度描かせると同じ物が二度出る。
  画像などのメディアはこのスペルでは添えない (返りは文字列一つ)。

読み取り専用 — 書き込み・LLM 呼び出しはしない。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from builtin_data.tools._paging_common import (
    MAX_PAGE_SPAN,
    chunk_by_count,
    format_missing_page,
    format_page_guide,
    parse_page_arg,
    select_pages,
)
# 「最近触られた」の物差しは部屋の様子と同じ一枚を借りる (二通りに実装しない)。
from builtin_data.tools.get_visual_context import _touched_epoch
from tools.context import get_active_manager, get_active_persona_id
from tools.core import ToolSchema

#: 1 ページに出すアイテムの件数。部屋の様子の表示上限とは独立の定数
#: (intent 設計 3 — たまたま同じ数)。
ITEMS_PER_PAGE = 10

#: 入れ物の入れ子を辿る深さの上限 (``get_bag_contents_recursive`` と同じ数)。
_MAX_BAG_DEPTH = 10


def _touched_at(item: Dict[str, Any], location: Optional[Dict[str, Any]]) -> float:
    """「最近触られた」の物差し — 二つの時刻の**新しい方**。

    アイテム自身の更新時刻 (``Item.UPDATED_AT`` — 開閉・説明や中身の書き換えで
    進む) と、置き場所の更新時刻 (``ItemLocation.UPDATED_AT`` — 設置・移動・
    拾得で進む)。片方だけでは「Bag から出して部屋に置いた物」(場所だけ動いた物)
    が埋もれたままになる (intent 設計 1)。

    置き場所の時刻は ``get_all_items_in_building`` が載せる
    ``location_updated_at`` から取り、無ければ manager の置き場所キャッシュを
    直接引く。

    一つ一つの時刻を秒に直すのは部屋の様子と**同じ関数**
    (:func:`builtin_data.tools.get_visual_context._touched_epoch`) — 同じ軸を
    二通りに実装すると、片方だけが読める時刻の形が生まれて並びが割れる。
    """
    return max(
        _touched_epoch(item.get("updated_at")),
        _touched_epoch(item.get("location_updated_at")),
        _touched_epoch((location or {}).get("updated_at")),
    )


def _sort_key(entry: Tuple[Dict[str, Any], str, float]) -> Tuple[float, int]:
    """新しい順・同時刻は ``SHORT_ID`` の降順 (どちらも降順で使う)。"""
    item, _where, touched = entry
    short_id = item.get("short_id")
    return (touched, short_id if isinstance(short_id, int) else -1)


def _bag_children(manager: Any, bag_item_id: str) -> List[Dict[str, Any]]:
    """入れ物の直下の中身 (一段だけ — 入れ子は呼び手が辿る)。"""
    if hasattr(manager, "get_items_in_bag"):
        return list(manager.get_items_in_bag(bag_item_id) or [])
    items = getattr(manager, "items", None) or {}
    by_bag = getattr(manager, "items_by_bag", None) or {}
    return [items[i] for i in by_bag.get(bag_item_id, []) if i in items]


def _collect_entries(
    manager: Any, building_id: str,
) -> List[Tuple[Dict[str, Any], str]]:
    """部屋の全アイテムを ``(アイテム, 置き場所の添え書き)`` で、新しい順に返す。

    建物直下から始めて、入れ物に出会ったらその中身へ降りる (入れ子も同じ規則)。
    同じアイテムを二度積まないよう通った id を覚える — 置き場所の記録が壊れて
    輪になっていても止まる。並びは建物直下と中身を混ぜた**一本**の
    「最近触られた順」(intent 設計 3)。
    """
    if not hasattr(manager, "get_all_items_in_building"):
        return []
    locations = getattr(manager, "item_locations", None) or {}
    collected: List[Tuple[Dict[str, Any], str, float]] = []
    visited: set = set()

    def walk(items: List[Dict[str, Any]], where: str, depth: int) -> None:
        for item in items:
            item_id = item.get("item_id")
            if item_id in visited:
                continue
            visited.add(item_id)
            collected.append(
                (item, where, _touched_at(item, locations.get(item_id))),
            )
            if (item.get("type") or "").lower() != "bag" or depth <= 0:
                continue
            name = item.get("name") or item_id
            walk(_bag_children(manager, item_id), f"（入れ物「{name}」の中）", depth - 1)

    walk(manager.get_all_items_in_building(building_id) or [], "", _MAX_BAG_DEPTH)
    collected.sort(key=_sort_key, reverse=True)
    return [(item, where) for item, where, _ in collected]


class _WithoutBagContents:
    """入れ子の中身の描画だけを外した manager の覗き窓。

    :func:`builtin_data.tools.get_visual_context._render_item` は、開いた入れ物を
    描くときに ``get_bag_contents_recursive`` があれば中身も入れ子で描く。この
    スペルは中身を一件ずつ独立の行として出すので、そのまま渡すと同じ物が二度
    出る (しかも入れ子側の描画は説明を切り詰める)。属性を一つだけ隠して借りる。
    """

    def __init__(self, manager: Any) -> None:
        self._manager = manager

    def __getattr__(self, name: str) -> Any:
        if name == "get_bag_contents_recursive":
            raise AttributeError(name)
        return getattr(self._manager, name)


def _render_one(item: Dict[str, Any], where: str, manager: Any) -> List[str]:
    """アイテム 1 件を部屋の様子と同じ見た目で描く (メディアは捨てる)。

    置き場所の添え書きは見出しの行 (``[item:N] [種類] 名前``) の末尾に付ける。
    """
    from builtin_data.tools.get_visual_context import _render_item

    short_id = item.get("short_id")
    ref = f"item:{short_id}" if short_id is not None else None
    lines: List[str] = []
    media: List[Dict[str, str]] = []
    _render_item(item, lines, media, _WithoutBagContents(manager), ref=ref)
    while lines and not lines[-1].strip():
        lines.pop()
    if where and lines:
        lines[0] = f"{lines[0]}{where}"
    return lines


def buried_items_view(page: str = "1") -> str:
    """今いる部屋の全アイテムを、最近触られた順にページでめくって見る。"""
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona context is not set. Use tools.context.persona_context().")

    manager = get_active_manager()
    if manager is None:
        raise RuntimeError("Manager context is not available; buried_items_view cannot be executed.")

    page_range, error = parse_page_arg(page)
    if error:
        return f"アイテムを見られませんでした: {error}"

    persona = getattr(manager, "all_personas", {}).get(persona_id)
    if persona is None:
        persona = getattr(manager, "personas", {}).get(persona_id)
    building_id = getattr(persona, "current_building_id", None) if persona else None
    if not building_id:
        return "アイテムを見られませんでした: 現在地が不明です。"

    building = getattr(manager, "building_map", {}).get(building_id)
    building_name = getattr(building, "name", None) or building_id

    entries = _collect_entries(manager, building_id)
    header = f"【この部屋のアイテム】{building_name}"
    if not entries:
        return f"{header}\nこの部屋にはアイテムがありません。"

    pages = chunk_by_count(entries, ITEMS_PER_PAGE)
    selected = select_pages(pages, page_range)
    if not selected:
        return f"{header}\n{format_missing_page(pages, page_range)}"

    lines: List[str] = [header, format_page_guide(pages, page_range)]
    for chunk in selected:
        for item, where in chunk:
            lines.append("")
            lines.extend(_render_one(item, where, manager))
    return "\n".join(lines)


def schema() -> ToolSchema:
    return ToolSchema(
        name="buried_items_view",
        description=(
            "部屋の様子に見えていないアイテムも含めて、いまいる部屋の全アイテムを"
            "新しく触った順にページでめくって見られます。"
            "入れ物 (Bag) の中に入っている物も、置き場所つきで一覧に出ます。"
            "部屋に物が増えると、様子に並ぶのは最近触った分だけになり、"
            "残りは埋もれて見えなくなります — 埋もれた物を探すときに使ってください。"
            f"1 ページは {ITEMS_PER_PAGE} 件です。"
            "page='2' で 2 ページ目、page='1-3' で 1〜3 ページ目をまとめて開けます"
            f"（一度に開けるのは {MAX_PAGE_SPAN} ページまで）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "page": {
                    "type": "string",
                    "description": (
                        "開くページ。'2' のような番号か '1-3' のような範囲"
                        f"（一度に {MAX_PAGE_SPAN} ページまで）。省略すると 1 ページ目"
                    ),
                },
            },
            "required": [],
        },
        result_type="string",
        spell=True,
        spell_display_name="埋もれたアイテムを見る",
    )
