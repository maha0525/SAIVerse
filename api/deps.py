from typing import Generator
from fastapi import Request


def get_manager(request: Request):
    from app_state import manager
    if not manager:
        raise RuntimeError("Manager not initialized")
    return manager


from database.session import get_db
