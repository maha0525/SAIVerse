from fastapi import APIRouter
from . import summon, memory, recall, config, autonomous
from . import import_chatlog, reembed, memopedia, native_export_import
from . import schedule, tasks, inventory, arasuji

router = APIRouter()

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
