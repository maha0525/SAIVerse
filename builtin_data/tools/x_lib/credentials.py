"""Per-persona X (Twitter) credential management.

Credentials are stored as JSON at:
    ~/.saiverse/personas/<persona_id>/x_credentials.json
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

LOGGER = logging.getLogger(__name__)

_CREDS_FILENAME = "x_credentials.json"


@dataclass
class XCredentials:
    access_token: str
    refresh_token: str
    x_user_id: str
    x_username: str
    token_expires_at: float = 0.0
    skip_confirmation: bool = False


def get_credentials_path(persona_path: Path) -> Path:
    return persona_path / _CREDS_FILENAME


def load_credentials(persona_path: Path) -> Optional[XCredentials]:
    """Load credentials from persona directory. Returns None if missing or invalid."""
    creds_path = get_credentials_path(persona_path)
    if not creds_path.exists():
        return None
    try:
        data = json.loads(creds_path.read_text("utf-8"))
        return XCredentials(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            x_user_id=data["x_user_id"],
            x_username=data["x_username"],
            token_expires_at=float(data.get("token_expires_at", 0.0)),
            skip_confirmation=bool(data.get("skip_confirmation", False)),
        )
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        LOGGER.warning("[x_credentials] Failed to parse %s: %s", creds_path, exc)
        return None


def save_credentials(persona_path: Path, creds: XCredentials) -> None:
    """Atomically save credentials (write to temp, then rename)."""
    creds_path = get_credentials_path(persona_path)
    creds_path.parent.mkdir(parents=True, exist_ok=True)

    data = asdict(creds)
    content = json.dumps(data, indent=2, ensure_ascii=False)

    # Atomic write: temp file in same directory, then rename
    fd, tmp_path = tempfile.mkstemp(
        dir=str(creds_path.parent), suffix=".tmp", prefix=".x_creds_"
    )
    try:
        os.write(fd, content.encode("utf-8"))
        os.close(fd)
        fd = -1
        # On Windows, target must not exist for rename
        if creds_path.exists():
            creds_path.unlink()
        Path(tmp_path).rename(creds_path)
        LOGGER.info("[x_credentials] Saved credentials for %s", creds.x_username)
    except Exception:
        if fd >= 0:
            os.close(fd)
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def delete_credentials(persona_path: Path) -> bool:
    """Delete credentials file. Returns True if file existed."""
    creds_path = get_credentials_path(persona_path)
    if creds_path.exists():
        creds_path.unlink()
        LOGGER.info("[x_credentials] Deleted credentials at %s", creds_path)
        return True
    return False


def is_connected(persona_path: Path) -> bool:
    return get_credentials_path(persona_path).exists()
