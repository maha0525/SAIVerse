"""アドオン向け共通 FastAPI dependency モジュール。

拡張パックの api_routes.py からインポートして使用する:

    from saiverse.addon_deps import get_manager

例:
    from fastapi import APIRouter, Depends
    from saiverse.addon_deps import get_manager

    router = APIRouter()

    @router.get("/audio/{message_id}")
    async def get_audio(message_id: str, manager=Depends(get_manager)):
        ...
"""
from __future__ import annotations


def get_manager():
    """SAIVerseManager インスタンスを返す dependency。

    本体の api/deps.py と同じ実装。アドオンが直接 api/deps.py を
    インポートしなくて済むように再エクスポートする。
    """
    from saiverse import app_state
    if not app_state.manager:
        raise RuntimeError("Manager not initialized")
    return app_state.manager
