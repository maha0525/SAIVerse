"""アドオン複合アクション CRUD + テスト実行 API。

フロントエンドの ActionsPanel から呼ばれる。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

LOGGER = logging.getLogger(__name__)

router = APIRouter()


class ActionBody(BaseModel):
    id: str
    display_name: str
    description: str = ""
    # 呼び出し時引数の宣言 ({name: {type, default, min, max, description}})。
    # UI のパラメータ編集 (Phase 2) 実装前でも save/load で欠落しないよう保持する。
    parameters: Dict[str, Any] = {}
    steps: List[Dict[str, Any]]


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

@router.get("/{addon_name}/actions")
def list_actions(addon_name: str):
    from saiverse.composite_actions import load_actions
    actions = load_actions(addon_name)
    return [a.to_dict() for a in actions]


@router.get("/{addon_name}/actions/available-tools")
def available_tools(addon_name: str):
    from saiverse.composite_actions import get_available_tools
    return get_available_tools(addon_name)


@router.get("/{addon_name}/actions/tool-schemas")
def tool_schemas(addon_name: str):
    """ステップで呼べるツールを引数スキーマ付きで返す (UI の引数フォーム用)。"""
    from saiverse.composite_actions import get_available_tool_schemas
    return get_available_tool_schemas(addon_name)


@router.get("/{addon_name}/actions/test-targets")
def test_targets(addon_name: str):
    """テスト実行の対象候補 (機体) を返す。

    複数機体アドオンでは native tool が「今ペルソナが降りている機体」へ振り分
    けるため、 persona 文脈を持たない UI のテスト実行では対象機体を選ぶ必要が
    ある。 addon が ``action_test_targets`` を宣言していれば候補を返し、 無けれ
    ば空リスト (= UI は機体プルダウンを出さず従来通りテストする)。

    各要素は ``{"value": <instance_id>, "label": <表示名>}``。
    """
    from saiverse.addon_loader import get_addon_action_test_targets
    return get_addon_action_test_targets(addon_name)


@router.get("/{addon_name}/actions/{action_id}")
def get_action(addon_name: str, action_id: str):
    from saiverse.composite_actions import load_actions
    for a in load_actions(addon_name):
        if a.id == action_id:
            return a.to_dict()
    raise HTTPException(status_code=404, detail=f"アクション '{action_id}' が見つかりません")


@router.post("/{addon_name}/actions", status_code=201)
def create_action(addon_name: str, body: ActionBody):
    from saiverse.composite_actions import (
        validate_action, save_action, load_actions,
        get_available_tools, register_action_spells, unregister_action_spells,
    )

    for existing in load_actions(addon_name):
        if existing.id == body.id:
            raise HTTPException(status_code=409, detail=f"アクション '{body.id}' は既に存在します")

    allowed = get_available_tools(addon_name) or None
    try:
        action = validate_action(body.model_dump(), allowed_tools=allowed)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    save_action(addon_name, action)
    unregister_action_spells(addon_name)
    register_action_spells(addon_name)
    return action.to_dict()


@router.put("/{addon_name}/actions/{action_id}")
def update_action(addon_name: str, action_id: str, body: ActionBody):
    from saiverse.composite_actions import (
        validate_action, save_action, load_actions, _actions_dir,
        get_available_tools, register_action_spells, unregister_action_spells,
    )

    path = _actions_dir(addon_name) / f"{action_id}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"アクション '{action_id}' が見つかりません")

    allowed = get_available_tools(addon_name) or None
    try:
        action = validate_action(body.model_dump(), allowed_tools=allowed)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if action.id != action_id:
        path.unlink()

    save_action(addon_name, action)
    unregister_action_spells(addon_name)
    register_action_spells(addon_name)
    return action.to_dict()


@router.delete("/{addon_name}/actions/{action_id}")
def delete_action_endpoint(addon_name: str, action_id: str):
    from saiverse.composite_actions import (
        delete_action, unregister_action_spells, register_action_spells,
    )
    if not delete_action(addon_name, action_id):
        raise HTTPException(status_code=404, detail=f"アクション '{action_id}' が見つかりません")
    unregister_action_spells(addon_name)
    register_action_spells(addon_name)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Test execution
# ---------------------------------------------------------------------------

@router.post("/{addon_name}/actions/test")
def test_action(
    addon_name: str,
    body: ActionBody,
    instance_id: str | None = Query(
        default=None,
        description=(
            "テスト実行先の機体 (MCP インスタンス) id。 複数機体アドオンで、 "
            "どの機体でステップを試すかを指定する。 省略時は persona 文脈解決"
            " (= 従来型アドオン / 単一接続) に委ねる。"
        ),
    ),
):
    from saiverse.composite_actions import validate_action, execute_action

    # テスト実行はステップの動作確認が目的。 id / display_name はまだ未入力でも
    # ステップを試せるよう、 空ならプレースホルダを補ってから検証する (保存時の
    # create/update は実 id / display_name を必須のままにする)。
    data = body.model_dump()
    if not str(data.get("id", "")).strip():
        data["id"] = "test_run"
    if not str(data.get("display_name", "")).strip():
        data["display_name"] = "テスト実行"

    try:
        action = validate_action(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        result = execute_action(
            addon_name, action, target_instance_id=instance_id,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        LOGGER.exception("addon_actions: test execution failed")
        raise HTTPException(status_code=500, detail=str(exc))

    return {"result": result}
