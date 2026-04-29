from typing import List

from fastapi import APIRouter
from pydantic import BaseModel

from . import summon, memory, recall, config, autonomous
from . import import_chatlog, reembed, memopedia, native_export_import
from . import schedule, tasks, inventory, arasuji
from . import pulse_logs, memory_notes, working_memory, autonomy
from . import storage_layers, tracks

router = APIRouter()


class PersonaListItem(BaseModel):
    id: str
    name: str


@router.get("/", response_model=List[PersonaListItem], tags=["people"])
@router.get("", response_model=List[PersonaListItem], include_in_schema=False)
def list_all_personas() -> List[PersonaListItem]:
    """Return all registered personas (AI rows).

    アドオン管理UIの「ペルソナ別設定」でペルソナを選択するために使用される。
    AddonManagerModal が `/api/people/` を叩いているが、対応するハンドラが
    無かったため 404 になっていた。シンプルな id/name のリストを返す。
    """
    from database.models import AI
    from database.session import SessionLocal

    db = SessionLocal()
    try:
        rows = db.query(AI.AIID, AI.AINAME).all()
        return [PersonaListItem(id=r.AIID, name=r.AINAME or r.AIID) for r in rows]
    finally:
        db.close()

# 各サブモジュールのルーターを include
router.include_router(summon.router, tags=["people"])
router.include_router(memory.router, tags=["people"])
router.include_router(recall.router, tags=["people"])
router.include_router(config.router, tags=["people"])
router.include_router(autonomous.router, tags=["people"])
router.include_router(import_chatlog.router, tags=["people"])
router.include_router(native_export_import.router, tags=["people"])
router.include_router(reembed.router, tags=["people"])
router.include_router(memopedia.router, tags=["people"])
router.include_router(schedule.router, tags=["people"])
router.include_router(tasks.router, tags=["people"])
router.include_router(inventory.router, tags=["people"])
router.include_router(arasuji.router, tags=["people"])
router.include_router(pulse_logs.router, tags=["people"])
router.include_router(memory_notes.router, tags=["people"])
router.include_router(working_memory.router, tags=["people"])
router.include_router(autonomy.router, tags=["people"])
router.include_router(storage_layers.router, tags=["people"])
router.include_router(tracks.router, tags=["people"])
