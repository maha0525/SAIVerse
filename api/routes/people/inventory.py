from fastapi import APIRouter, Depends, HTTPException
from typing import List
from api.deps import get_manager
from .models import InventoryItem
from database.models import Item as ItemModel, ItemLocation
import json

router = APIRouter()

@router.get("/{persona_id}/items", response_model=List[InventoryItem])
def list_persona_items(persona_id: str, manager = Depends(get_manager)):
    """List items held by a persona."""
    session = manager.SessionLocal()
    try:
        # Query items where location owner is this persona
        items = (
            session.query(ItemModel)
            .join(ItemLocation, ItemModel.ITEM_ID == ItemLocation.ITEM_ID)
            .filter(
                ItemLocation.OWNER_KIND == "persona",
                ItemLocation.OWNER_ID == persona_id
            )
            .order_by(ItemModel.NAME)
            .all()
        )

        return [
            InventoryItem(
                id=i.ITEM_ID,
                name=i.NAME,
                type=i.TYPE,
                description=i.DESCRIPTION,
                file_path=i.FILE_PATH,
                created_at=i.CREATED_AT
            )
            for i in items
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()
