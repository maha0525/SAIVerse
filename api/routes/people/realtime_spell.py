"""ペルソナ単位のリアルタイムスペル binding CRUD + スペル一覧 API。"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.deps import get_manager

router = APIRouter()


@router.get("/realtime-spell-catalog")
def get_spell_catalog():
    """リアルタイムスペルとして設定可能なスペル一覧とスキーマを返す。

    ``spell_visible=False`` のスペルは除外する。非表示のスペルは「実行はできるが
    一覧には出さない」ものなので、ここに出すとユーザーが選べてしまい、非表示に
    した判断が UI 側から素通しになる (``/api/people/spells`` と同じ規則)。
    """
    from tools import SPELL_TOOL_SCHEMAS

    catalog = []
    for name, schema in SPELL_TOOL_SCHEMAS.items():
        if not getattr(schema, "spell_visible", True):
            continue
        params = schema.parameters or {}
        properties = params.get("properties", {})
        required = params.get("required", [])
        catalog.append({
            "name": name,
            "description": schema.description or "",
            "parameters": {
                "properties": properties,
                "required": required,
            },
        })
    catalog.sort(key=lambda x: x["name"])
    return catalog


class CreateBindingRequest(BaseModel):
    spell_name: str
    spell_args_json: Optional[str] = None
    label: Optional[str] = None
    enabled: bool = True
    priority: int = 0


@router.get("/{persona_id}/realtime-spell")
def list_persona_realtime_spells(persona_id: str, manager=Depends(get_manager)):
    """ペルソナに設定されたリアルタイムスペル一覧を取得する。"""
    from database.models import RealtimeSpellBinding

    db = manager.SessionLocal()
    try:
        bindings = (
            db.query(RealtimeSpellBinding)
            .filter(
                RealtimeSpellBinding.OWNER_KIND == "persona",
                RealtimeSpellBinding.OWNER_ID == persona_id,
            )
            .order_by(RealtimeSpellBinding.PRIORITY.desc())
            .all()
        )
        return [
            {
                "binding_id": b.BINDING_ID,
                "spell_name": b.SPELL_NAME,
                "spell_args_json": b.SPELL_ARGS_JSON,
                "label": b.LABEL,
                "enabled": b.ENABLED,
                "priority": b.PRIORITY,
            }
            for b in bindings
        ]
    finally:
        db.close()


@router.post("/{persona_id}/realtime-spell")
def create_persona_realtime_spell(
    persona_id: str,
    body: CreateBindingRequest,
    manager=Depends(get_manager),
):
    """ペルソナにリアルタイムスペル binding を追加する。"""
    from database.models import RealtimeSpellBinding

    db = manager.SessionLocal()
    try:
        binding = RealtimeSpellBinding(
            OWNER_KIND="persona",
            OWNER_ID=persona_id,
            SPELL_NAME=body.spell_name,
            SPELL_ARGS_JSON=body.spell_args_json,
            LABEL=body.label,
            ENABLED=body.enabled,
            PRIORITY=body.priority,
        )
        db.add(binding)
        db.commit()
        db.refresh(binding)
        return {"status": "ok", "binding_id": binding.BINDING_ID}
    finally:
        db.close()


@router.delete("/{persona_id}/realtime-spell/{binding_id}")
def delete_persona_realtime_spell(
    persona_id: str,
    binding_id: int,
    manager=Depends(get_manager),
):
    """ペルソナのリアルタイムスペル binding を削除する。"""
    from database.models import RealtimeSpellBinding

    db = manager.SessionLocal()
    try:
        binding = db.query(RealtimeSpellBinding).filter(
            RealtimeSpellBinding.BINDING_ID == binding_id,
            RealtimeSpellBinding.OWNER_KIND == "persona",
            RealtimeSpellBinding.OWNER_ID == persona_id,
        ).first()
        if not binding:
            raise HTTPException(status_code=404, detail="Binding not found")
        db.delete(binding)
        db.commit()
        return {"status": "ok"}
    finally:
        db.close()
