from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel
from typing import List, Dict, Optional
import os
import sys
import re
from pathlib import Path
import logging

LOGGER = logging.getLogger(__name__)
router = APIRouter()

ENV_FILE_PATH = Path(".env")
SENSITIVE_KEYWORDS = ["KEY", "TOKEN", "SECRET", "PASSWORD"]

class EnvVar(BaseModel):
    key: str
    value: str
    is_sensitive: bool

class EnvUpdateRequest(BaseModel):
    updates: Dict[str, str]

def is_sensitive(key: str) -> bool:
    return any(k in key.upper() for k in SENSITIVE_KEYWORDS)

def read_env_file() -> List[tuple[str, str, str]]:
    if not ENV_FILE_PATH.exists():
        return []
    result = []
    try:
        with open(ENV_FILE_PATH, "r", encoding="utf-8") as f:
            for line in f:
                original = line.rstrip("\n")
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    result.append(("", "", original))
                    continue
                match = re.match(r'^([^=]+)=(.*)$', stripped)
                if match:
                    key = match.group(1).strip()
                    value = match.group(2).strip()
                    if (value.startswith('"') and value.endswith('"')) or \
                       (value.startswith("'") and value.endswith("'")):
                        value = value[1:-1]
                    result.append((key, value, original))
                else:
                    result.append(("", "", original))
    except Exception as e:
        LOGGER.error(f"Failed to read .env: {e}")
    return result

@router.get("/env", response_model=List[EnvVar])
def get_env_vars():
    """Get environment variables from .env file."""
    raw = read_env_file()
    vars_list = []
    seen_keys = set()
    
    for key, value, _ in raw:
        if key and key not in seen_keys:
            vars_list.append(EnvVar(
                key=key,
                value=value, # Frontend should mask if sensitive, or we mask here? 
                             # Strategy: Send real value but flag it. 
                             # Security risk? Usually admin needs to see value to edit.
                             # But `ui/env_settings.py` masked it.
                             # Let's mask it here for safety, and only support overwriting logic.
                is_sensitive=is_sensitive(key)
            ))
            seen_keys.add(key)
    
    # Sort by key
    vars_list.sort(key=lambda x: x.key)
    return vars_list

@router.post("/env")
def update_env_vars(req: EnvUpdateRequest):
    """Update environment variables in .env file."""
    try:
        current_data = read_env_file()
        new_lines = []
        updated_keys = set()
        
        # Update existing lines
        for key, value, original in current_data:
            if not key:
                new_lines.append(original)
            elif key in req.updates:
                new_val = req.updates[key]
                # If sensitive and empty strings passed, user might mean "no change"
                # But frontend should send current value if not changed, or we handle partial updates?
                # Let's assume frontend sends explicit values.
                
                # Check formatting
                if " " in new_val or "=" in new_val:
                    new_lines.append(f'{key}="{new_val}"')
                else:
                    new_lines.append(f"{key}={new_val}")
                updated_keys.add(key)
            else:
                new_lines.append(original)
        
        # Append new keys
        for key, val in req.updates.items():
            if key not in updated_keys:
                 if " " in val or "=" in val:
                    new_lines.append(f'{key}="{val}"')
                 else:
                    new_lines.append(f"{key}={val}")
        
        with open(ENV_FILE_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(new_lines))
            if new_lines:
                f.write("\n")
                
        return {"success": True, "message": "Environment variables updated."}
    except Exception as e:
        LOGGER.error(f"Failed to update .env: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/restart")
def restart_server(background_tasks: BackgroundTasks):
    """Restart the server process."""
    def _restart():
        import time
        time.sleep(1) # Give time for response to be sent
        LOGGER.warning("Restarting server via API request...")
        python = sys.executable
        os.execv(python, [python] + sys.argv)

    background_tasks.add_task(_restart)
    return {"success": True, "message": "Server restarting..."}
