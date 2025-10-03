import json
import os
import sys
import time


def _enabled() -> bool:
    return str(os.getenv("SAIMEMORY_DEBUG", "false")).lower() in {"1", "true", "yes", "on"}


def debug(event: str, **kwargs) -> None:
    if not _enabled():
        return
    payload = {"ts": time.time(), "event": event}
    if kwargs:
        payload.update(kwargs)
    sys.stderr.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stderr.flush()
