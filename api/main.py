from fastapi import APIRouter
from api.routes import chat, config, user, info, people

api_router = APIRouter()
api_router.include_router(chat.router, prefix="/chat", tags=["chat"])
api_router.include_router(config.router, prefix="/config", tags=["config"])
api_router.include_router(user.router, prefix="/user", tags=["user"])
api_router.include_router(info.router, prefix="/info", tags=["info"])
api_router.include_router(people.router, prefix="/people", tags=["people"])

from api.routes import admin, db_manager, world, media, phenomena, usage, tutorial, uri
api_router.include_router(admin.router, prefix="/admin", tags=["admin"])
api_router.include_router(db_manager.router, prefix="/db", tags=["db"])
api_router.include_router(world.router, prefix="/world", tags=["world"])
api_router.include_router(media.router, prefix="/media", tags=["media"])
api_router.include_router(phenomena.router, prefix="/phenomena", tags=["phenomena"])
api_router.include_router(usage.router, prefix="/usage", tags=["usage"])
api_router.include_router(tutorial.router, prefix="/tutorial", tags=["tutorial"])
api_router.include_router(uri.router, prefix="/uri", tags=["uri"])

