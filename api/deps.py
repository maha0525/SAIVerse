from typing import Generator, Optional
from fastapi import Request


def get_manager(request: Request):
    from saiverse.app_state import manager
    if not manager:
        raise RuntimeError("Manager not initialized")
    return manager


def avatar_path_to_url(avatar_path: Optional[str]) -> Optional[str]:
    """Convert stored avatar path (filesystem or relative) to browser-accessible URL."""
    if not avatar_path:
        return None
    # Already a URL
    if avatar_path.startswith("/api/") or avatar_path.startswith("http"):
        return avatar_path
    # Normalize backslashes (Windows paths)
    normalized = avatar_path.replace("\\", "/")
    # Relative or absolute paths containing known directories
    if "user_data/icons/" in normalized:
        filename = normalized.split("user_data/icons/")[-1]
        return f"/api/static/user_icons/{filename}"
    if "builtin_data/icons/" in normalized:
        filename = normalized.split("builtin_data/icons/")[-1]
        return f"/api/static/builtin_icons/{filename}"
    if normalized.startswith("assets/"):
        return f"/api/static/{normalized[7:]}"
    # Unknown format, return as-is
    return avatar_path


from database.session import get_db
