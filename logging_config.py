"""Centralized logging configuration for SAIVerse.

This module provides:
- Per-startup log directories with timestamps
- Terminal mirror logging (backend.log)
- Unified LLM I/O logging (llm_io.log)
- Standard Python logging configuration
"""
from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# Determine project root
PROJECT_ROOT = Path(__file__).parent
LOGS_BASE_DIR = PROJECT_ROOT / "user_data" / "logs"

# Session-specific log directory (created once per startup)
_session_log_dir: Optional[Path] = None
_initialized: bool = False

# Pattern to match base64 data URLs
_BASE64_PATTERN = re.compile(r'(?P<prefix>"url":\s*"data:[^;]+;base64,)(?P<b64>[A-Za-z0-9+/=]+)(?P<suffix>")')


def _truncate_base64_filter(record: logging.LogRecord) -> bool:
    """Filter that truncates base64 data in log messages.
    
    Replaces base64 encoded data in data URLs with a short placeholder
    to avoid cluttering logs with long strings.
    """
    if not record.msg:
        return True
    
    msg_str = str(record.msg)
    
    # Check if message contains base64 data URLs
    if _BASE64_PATTERN.search(msg_str):
        # Replace base64 data with byte count placeholder
        truncated = _BASE64_PATTERN.sub(
            lambda m: f'{m.group("prefix")}[{len(m.group("b64"))} bytes]{m.group("suffix")}',
            msg_str
        )
        record.msg = truncated
    
    return True


def get_session_log_dir() -> Path:
    """Get the session-specific log directory, creating it if needed."""
    global _session_log_dir
    if _session_log_dir is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        _session_log_dir = LOGS_BASE_DIR / timestamp
        _session_log_dir.mkdir(parents=True, exist_ok=True)
    return _session_log_dir


def get_backend_log_path() -> Path:
    """Get the path to the backend terminal mirror log."""
    return get_session_log_dir() / "backend.log"


def get_llm_log_path() -> Path:
    """Get the path to the unified LLM I/O log."""
    return get_session_log_dir() / "llm_io.log"


class TeeHandler(logging.StreamHandler):
    """Handler that writes to both console (original stream) and a file."""
    
    def __init__(self, stream, file_path: Path):
        super().__init__(stream)
        self._file_handler = logging.FileHandler(file_path, encoding="utf-8")
        self._file_handler.setFormatter(self.formatter)
    
    def setFormatter(self, fmt):
        super().setFormatter(fmt)
        if hasattr(self, "_file_handler"):
            self._file_handler.setFormatter(fmt)
    
    def emit(self, record):
        # Emit to console
        super().emit(record)
        # Emit to file
        self._file_handler.emit(record)
    
    def close(self):
        self._file_handler.close()
        super().close()


def configure_logging(level_name: Optional[str] = None) -> Path:
    """Configure logging for the SAIVerse backend.
    
    Sets up:
    - Root logger with both console and file output (backend.log)
    - LLM I/O logger for unified LLM request/response logging
    
    Args:
        level_name: Log level name (DEBUG, INFO, WARNING, ERROR, CRITICAL).
                   Defaults to SAIVERSE_LOG_LEVEL env var or INFO.
    
    Returns:
        Path to the session log directory.
    """
    global _initialized
    if _initialized:
        return get_session_log_dir()
    
    # Determine log level
    if level_name is None:
        level_name = os.getenv("SAIVERSE_LOG_LEVEL", "INFO").upper()
    if level_name not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        level_name = "INFO"
    level = getattr(logging, level_name)
    
    # Get log paths
    log_dir = get_session_log_dir()
    backend_log_path = get_backend_log_path()
    
    # Create formatter
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    
    # Configure root logger with TeeHandler (console + file)
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    
    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    
    # Add TeeHandler for combined console + file output
    tee_handler = TeeHandler(sys.stderr, backend_log_path)
    tee_handler.setLevel(level)
    tee_handler.setFormatter(formatter)
    
    # Add filter to truncate base64 data in logs
    tee_handler.addFilter(_truncate_base64_filter)
    
    root_logger.addHandler(tee_handler)
    
    # Configure LLM I/O logger
    _configure_llm_logger()
    
    _initialized = True
    logging.info("Logging configured. Session logs: %s", log_dir)
    
    return log_dir


def _configure_llm_logger() -> None:
    """Configure the unified LLM I/O logger."""
    llm_logger = logging.getLogger("saiverse.llm")
    llm_logger.setLevel(logging.DEBUG)
    
    # Remove existing handlers
    for handler in llm_logger.handlers[:]:
        llm_logger.removeHandler(handler)
    
    # Add file handler for LLM I/O
    llm_log_path = get_llm_log_path()
    file_handler = logging.FileHandler(llm_log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    llm_logger.addHandler(file_handler)
    
    # Don't propagate to root logger (LLM logs are verbose)
    llm_logger.propagate = False


def get_llm_logger() -> logging.Logger:
    """Get the unified LLM I/O logger."""
    return logging.getLogger("saiverse.llm")


def log_llm_request(
    source: str,
    node_id: str,
    persona_id: Optional[str],
    persona_name: Optional[str],
    messages: list,
) -> None:
    """Log an LLM request.
    
    Args:
        source: Source of the request (e.g., "sea", "direct")
        node_id: Node ID (for SEA playbooks) or operation name
        persona_id: Persona ID if applicable
        persona_name: Persona name if applicable
        messages: List of messages sent to the LLM
    """
    import json
    logger = get_llm_logger()
    
    # Use messages as-is (no truncation for debugging purposes)
    formatted_messages = messages
    
    entry = {
        "type": "request",
        "source": source,
        "node": node_id,
        "persona_id": persona_id,
        "persona_name": persona_name,
        "messages": formatted_messages,
    }
    logger.debug("LLM_REQUEST: %s", json.dumps(entry, ensure_ascii=False))


def log_llm_response(
    source: str,
    node_id: str,
    persona_id: Optional[str],
    persona_name: Optional[str],
    output: str,
) -> None:
    """Log an LLM response.
    
    Args:
        source: Source of the request (e.g., "sea", "direct")
        node_id: Node ID (for SEA playbooks) or operation name
        persona_id: Persona ID if applicable
        persona_name: Persona name if applicable
        output: The LLM's output text
    """
    import json
    logger = get_llm_logger()
    
    # Log full output (no truncation for debugging purposes)
    
    entry = {
        "type": "response",
        "source": source,
        "node": node_id,
        "persona_id": persona_id,
        "persona_name": persona_name,
        "output": output,
    }
    logger.debug("LLM_RESPONSE: %s", json.dumps(entry, ensure_ascii=False))


def get_timeout_diagnostics_log_path() -> Path:
    """Get the path to the timeout diagnostics log."""
    return get_session_log_dir() / "timeout_diagnostics.log"


def _configure_timeout_diagnostics_logger() -> None:
    """Configure the timeout diagnostics logger."""
    timeout_logger = logging.getLogger("saiverse.timeout")
    timeout_logger.setLevel(logging.DEBUG)
    
    # Remove existing handlers
    for handler in timeout_logger.handlers[:]:
        timeout_logger.removeHandler(handler)
    
    # Add file handler for timeout diagnostics
    timeout_log_path = get_timeout_diagnostics_log_path()
    file_handler = logging.FileHandler(timeout_log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    timeout_logger.addHandler(file_handler)
    
    # Don't propagate to root logger
    timeout_logger.propagate = False


def get_timeout_diagnostics_logger() -> logging.Logger:
    """Get the timeout diagnostics logger."""
    logger = logging.getLogger("saiverse.timeout")
    # Ensure configured (lazy initialization)
    if not logger.handlers:
        _configure_timeout_diagnostics_logger()
    return logger


def log_timeout_event(
    timeout_type: str,
    model: str,
    wait_duration_sec: float,
    message_count: int,
    total_chars: int,
    image_count: int,
    has_tools: bool,
    use_stream: bool,
    client_type: str,
    retry_attempt: int,
    extra_info: Optional[dict] = None,
) -> None:
    """Log a timeout event with detailed diagnostics.
    
    Args:
        timeout_type: Type of timeout (e.g., "ReadTimeout", "ChunkTimeout")
        model: Model name being used
        wait_duration_sec: How long we waited before timeout
        message_count: Number of messages in the request
        total_chars: Total characters in all messages
        image_count: Number of images included
        has_tools: Whether tools were enabled
        use_stream: Whether streaming was used
        client_type: "free" or "paid"
        retry_attempt: Current retry attempt number
        extra_info: Optional additional info dict
    """
    import json
    from datetime import datetime
    logger = get_timeout_diagnostics_logger()
    
    entry = {
        "timestamp": datetime.now().isoformat(),
        "timeout_type": timeout_type,
        "wait_duration_sec": wait_duration_sec,
        "request_info": {
            "model": model,
            "message_count": message_count,
            "total_chars": total_chars,
            "image_count": image_count,
            "has_tools": has_tools,
            "use_stream": use_stream,
        },
        "client_type": client_type,
        "retry_attempt": retry_attempt,
    }
    if extra_info:
        entry["extra"] = extra_info
    
    logger.warning("TIMEOUT: %s", json.dumps(entry, ensure_ascii=False))
