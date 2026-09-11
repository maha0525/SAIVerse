"""入れ物を作る (スペル ``bag_create``)。

正典: docs/intent/room_item_display_cap.md 設計 5。

Bag (物を入れられるアイテム) は既に世界に在り、物を移す口 (``item_move``) も
ペルソナにある。**無かったのは Bag を作る手段** — ワールドエディタからは作れる
のに、ペルソナには経路が無かった (docs/issues/bag_item_has_no_creation_path.md)。
このスペルがその経路で、名前と説明を決めた入れ物を今いる部屋に置く。

作った入れ物は閉じた状態で置かれる。閉じた入れ物の中身は部屋の様子に出ないので、
散らかった物を入れれば部屋が片付く (設計 5)。
"""
from __future__ import annotations

import json as _json

from tools.context import (
    get_active_manager,
    get_active_persona_id,
    get_active_playbook_name,
)
from tools.core import ToolSchema


def bag_create(name: str, description: str = "") -> str:
    """入れ物 (Bag) を作って、今いる部屋に置く。

    Args:
        name: 入れ物の名前 (例: '布の道具袋')。
        description: 何を入れる入れ物かの説明。部屋の様子に出る。

    Returns:
        作成結果の文と、できた入れ物のアイテムID (``item:N``)。
    """
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona context is not set. Use tools.context.persona_context().")

    manager = get_active_manager()
    if manager is None:
        raise RuntimeError("Manager context is not available; bag_create cannot be executed.")

    clean_name = (name or "").strip()
    if not clean_name:
        return "入れ物を作れませんでした: 名前 (name) が空です。"
    clean_description = (description or "").strip()

    playbook_name = get_active_playbook_name()
    source_context = _json.dumps({"playbook": playbook_name, "tool": "bag_create"})
    return manager.create_bag_item(
        persona_id, clean_name, clean_description, source_context=source_context,
    )


def schema() -> ToolSchema:
    return ToolSchema(
        name="bag_create",
        description=(
            "散らかった部屋を片付けるための入れ物 (Bag) を作ります。"
            "名前と説明を決めると、今いる部屋に置かれます。"
            "作った入れ物には item_move でアイテムをしまえて、"
            "閉じた入れ物の中身は部屋の様子に表示されません — "
            "物が増えて部屋が見通せなくなったときに使ってください。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "入れ物の名前（例: '布の道具袋', '資料箱'）。",
                },
                "description": {
                    "type": "string",
                    "description": "何を入れる入れ物かの説明。部屋の様子に出ます。",
                },
            },
            "required": ["name"],
        },
        result_type="string",
        spell=True,
        spell_display_name="入れ物を作る",
    )
