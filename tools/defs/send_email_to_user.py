from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from typing import Dict, Union, Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import Base, User, AI
from database.paths import default_db_path
from tools.context import get_active_persona_id
from tools.defs import ToolSchema

# Minimal logger that writes to the shared SAIVerse log.
LOG_FILE = Path(os.getenv("SAIVERSE_LOG_PATH", str(Path.cwd() / "saiverse_log.txt")))
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
LOG_FILE.touch(exist_ok=True)

logger = logging.getLogger(__name__)
if not any(isinstance(h, logging.FileHandler) and h.baseFilename == str(LOG_FILE) for h in logger.handlers):
    handler = logging.FileHandler(LOG_FILE)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False


def _load_smtp_config() -> Union[Dict[str, Union[str, int, bool]], str]:
    """Validate and return SMTP configuration from environment variables."""
    host = os.getenv("SMTP_HOST")
    username = os.getenv("SMTP_USERNAME")
    password = os.getenv("SMTP_PASSWORD")
    port = int(os.getenv("SMTP_PORT", "587") or 587)
    use_tls = os.getenv("SMTP_USE_TLS", "true").lower() not in {"0", "false", "no"}
    from_raw = os.getenv("SMTP_FROM", username or "")

    if not host or not username or not password:
        return "SMTP configuration is incomplete; set SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD."
    if not from_raw:
        return "SMTP_FROM is missing and SMTP_USERNAME is empty."

    return {
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "use_tls": use_tls,
        "from_raw": from_raw,
    }


def _format_from_address(base_from: str, persona_name: Optional[str]) -> str:
    """Attach persona name if not already present in the From header."""
    if "<" in base_from and ">" in base_from:
        # Already formatted as display <addr>; leave as-is.
        return base_from
    if persona_name:
        return f"{persona_name} <{base_from}>"
    return base_from


def _log_and_return(message: str) -> str:
    logger.info("send_email_to_user result: %s", message)
    return message


def send_email_to_user(user_id: int, subject: str, body: str) -> str:
    """Send an email to a user looked up by USERID using SMTP settings from env."""
    persona_id = get_active_persona_id()
    logger.info(
        "send_email_to_user called persona_id=%s user_id=%s subject_len=%s body_len=%s",
        persona_id,
        user_id,
        len(subject) if subject is not None else None,
        len(body) if body is not None else None,
    )

    db_path = default_db_path()
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    persona_name: Optional[str] = None
    with Session() as session:
        user = session.query(User).filter(User.USERID == user_id).first()
        if not user:
            return _log_and_return(f"User {user_id} not found.")
        to_addr = user.MAILADDRESS

        if persona_id:
            ai = session.query(AI).filter(AI.AIID == persona_id).first()
            if ai and ai.AINAME:
                persona_name = ai.AINAME

    if not to_addr:
        return _log_and_return("No email address configured; skipped.")

    cfg = _load_smtp_config()
    if isinstance(cfg, str):
        return _log_and_return(cfg)

    from_addr = _format_from_address(cfg["from_raw"], persona_name)

    message = EmailMessage()
    message["From"] = from_addr
    message["To"] = to_addr
    message["Subject"] = subject
    message.set_content(body)

    try:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=10) as server:
            if cfg["use_tls"]:
                context = ssl.create_default_context()
                server.starttls(context=context)
            if os.getenv("SMTP_DEBUG", "0") not in {"0", "false", "no"}:
                server.set_debuglevel(1)
            server.login(cfg["username"], cfg["password"])
            server.send_message(message)
        return _log_and_return("Email sent.")
    except Exception as exc:  # noqa: BLE001
        logger.exception("email send failed for user_id=%s", user_id)
        return f"Failed to send email: {exc}"


def schema() -> ToolSchema:
    return ToolSchema(
        name="send_email_to_user",
        description="Send an email to a user by USERID using SMTP settings from environment variables."
        " Adds persona display name to From if available.",
        parameters={
            "type": "object",
            "properties": {
                "user_id": {"type": "integer", "description": "USERID of the recipient."},
                "subject": {"type": "string", "description": "Email subject line."},
                "body": {"type": "string", "description": "Email body text."},
            },
            "required": ["user_id", "subject", "body"],
        },
        result_type="string",
    )
