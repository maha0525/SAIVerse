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

# Log directory inside user_data
from .data_paths import USER_DATA_DIR as _USER_DATA_DIR
LOGS_BASE_DIR = _USER_DATA_DIR / "logs"

# Session-specific log directory (created once per startup)
_session_log_dir: Optional[Path] = None
_initialized: bool = False

# Patterns to match base64 data in logs
# 1. data URL format: "url": "data:image/png;base64,iVBOR..."
_BASE64_DATA_URL = re.compile(r'(?P<prefix>"url":\s*"data:[^;]+;base64,)(?P<b64>[A-Za-z0-9+/=]{200,})(?P<suffix>")')
# 2. Anthropic format: "data": "iVBOR..." (long base64 string in "data" field)
_BASE64_DATA_FIELD = re.compile(r'(?P<prefix>"data":\s*")(?P<b64>[A-Za-z0-9+/=]{200,})(?P<suffix>")')


def _truncate_base64_in_string(msg_str: str) -> str:
    """Truncate base64 data in a string, replacing long base64 content with byte count."""
    for pattern in (_BASE64_DATA_URL, _BASE64_DATA_FIELD):
        msg_str = pattern.sub(
            lambda m: f'{m.group("prefix")}[base64: {len(m.group("b64"))} chars]{m.group("suffix")}',
            msg_str
        )
    return msg_str


def _truncate_base64_filter(record: logging.LogRecord) -> bool:
    """Filter that truncates base64 data in log messages."""
    if not record.msg:
        return True

    msg_str = str(record.msg)
    truncated = _truncate_base64_in_string(msg_str)
    if truncated is not msg_str:
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

    # Configure error-only log (WARNING and above)
    _configure_error_logger()

    # Suppress overly verbose HTTP library debug logging (base64 request bodies)
    for noisy_logger in ("httpcore", "httpx", "anthropic._base_client"):
        logging.getLogger(noisy_logger).setLevel(max(level, logging.INFO))

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
    # Add base64 truncation filter to LLM logger
    file_handler.addFilter(_truncate_base64_filter)
    llm_logger.addHandler(file_handler)

    # Don't propagate to root logger (LLM logs are verbose)
    llm_logger.propagate = False


def _configure_error_logger() -> None:
    """Configure error-only log file (WARNING and above from all loggers)."""
    error_log_path = get_session_log_dir() / "error.log"
    error_handler = logging.FileHandler(error_log_path, encoding="utf-8")
    error_handler.setLevel(logging.WARNING)
    error_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    error_handler.addFilter(_truncate_base64_filter)
    # Add to root logger so it catches WARNING/ERROR/CRITICAL from all modules
    logging.getLogger().addHandler(error_handler)


def get_llm_logger() -> logging.Logger:
    """Get the unified LLM I/O logger."""
    return logging.getLogger("saiverse.llm")


def _sanitize_messages_for_log(messages: list) -> list:
    """Deep-sanitize messages to truncate base64 image data for logging.

    Handles Anthropic format: {"type": "image", "source": {"type": "base64", "data": "..."}}
    and OpenAI format: {"type": "image_url", "image_url": {"url": "data:...;base64,..."}}
    """

    def _sanitize_content(content):
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return [_sanitize_content(item) for item in content]
        if isinstance(content, dict):
            result = {}
            for k, v in content.items():
                if k == "data" and isinstance(v, str) and len(v) > 200:
                    result[k] = f"[base64: {len(v)} chars]"
                elif k == "url" and isinstance(v, str) and ";base64," in v and len(v) > 200:
                    prefix = v[:v.index(";base64,") + 8]
                    result[k] = f"{prefix}[{len(v) - len(prefix)} chars]"
                else:
                    result[k] = _sanitize_content(v)
            return result
        return content

    sanitized = []
    for msg in messages:
        new_msg = dict(msg)
        if "content" in new_msg:
            new_msg["content"] = _sanitize_content(new_msg["content"])
        sanitized.append(new_msg)
    return sanitized


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
    
    # Sanitize messages: truncate base64 image data before logging
    formatted_messages = _sanitize_messages_for_log(messages)
    
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


def get_sea_trace_log_path() -> Path:
    """Get the path to the SEA node execution trace log."""
    return get_session_log_dir() / "sea_trace.log"


def _configure_sea_trace_logger() -> None:
    """Configure the SEA trace logger for concise node execution tracking."""
    trace_logger = logging.getLogger("saiverse.sea_trace")
    trace_logger.setLevel(logging.DEBUG)

    # Remove existing handlers
    for handler in trace_logger.handlers[:]:
        trace_logger.removeHandler(handler)

    # Add file handler
    trace_log_path = get_sea_trace_log_path()
    file_handler = logging.FileHandler(trace_log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(message)s",
        datefmt="%H:%M:%S"
    ))
    trace_logger.addHandler(file_handler)

    # Don't propagate to root logger
    trace_logger.propagate = False


def get_sea_trace_logger() -> logging.Logger:
    """Get the SEA trace logger."""
    logger = logging.getLogger("saiverse.sea_trace")
    if not logger.handlers:
        _configure_sea_trace_logger()
    return logger


def log_sea_trace(playbook: str, node_id: str, node_type: str, detail: str = "") -> None:
    """Log a SEA node execution trace entry.

    Output format: ``HH:MM:SS playbook/node_id [TYPE] detail``

    No truncation — sea_trace.log is a file-only logger, not console.
    """
    logger = get_sea_trace_logger()
    logger.debug("%s/%s [%s] %s", playbook, node_id, node_type.upper(), detail)


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
