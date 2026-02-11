"""
Application-wide state management.

This module provides a central place for managing global application state,
independent of any UI framework. The manager instance is set during startup
and accessed by API endpoints via dependency injection.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional, Tuple, Union

if TYPE_CHECKING:
    from .saiverse_manager import SAIVerseManager

# --- Core application state ---
manager: Optional["SAIVerseManager"] = None

# --- Configuration values (set during startup) ---
model_choices: List[Union[str, Tuple[str, str]]] = []
chat_history_limit: int = 120
version: str = ""
city_name: str = ""
project_dir: str = ""


def bind_manager(instance: "SAIVerseManager") -> None:
    """Register the SAIVerseManager instance for the application."""
    global manager
    manager = instance


def set_model_choices(choices: List[Union[str, Tuple[str, str]]]) -> None:
    """Set available model choices for the application."""
    global model_choices
    model_choices = choices


def set_chat_history_limit(limit: int) -> None:
    """Set the chat history limit."""
    global chat_history_limit
    chat_history_limit = max(0, limit)


def set_version(value: str) -> None:
    """Set the application version string."""
    global version
    version = value


def set_city_name(value: str) -> None:
    """Set the city name for the running instance."""
    global city_name
    city_name = value


def set_project_dir(value: str) -> None:
    """Set the project root directory path."""
    global project_dir
    project_dir = value
