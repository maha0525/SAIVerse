"""X (Twitter) mention polling integration.

Periodically checks for new mentions on each X-connected persona's account
and emits TriggerEvents for the PhenomenonManager.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from phenomena.triggers import TriggerEvent, TriggerType
from saiverse.integrations.base import BaseIntegration

if TYPE_CHECKING:
    from saiverse.saiverse_manager import SAIVerseManager

LOGGER = logging.getLogger(__name__)

# State file stored per persona for since_id tracking
_STATE_FILENAME = "x_mention_state.json"


class XMentionIntegration(BaseIntegration):
    """Polls X API for new mentions on connected personas."""

    name = "x_mentions"
    poll_interval_seconds = 300  # 5 minutes

    def poll(self, manager: "SAIVerseManager") -> List[TriggerEvent]:
        """Poll X mentions for all connected personas."""
        events: List[TriggerEvent] = []

        # Find personas with X credentials
        connected = self._find_connected_personas(manager)
        if not connected:
            LOGGER.debug("[XMentionIntegration] No X-connected personas found")
            return events

        for persona_id, persona_path in connected:
            try:
                new_events = self._poll_persona(persona_id, persona_path)
                events.extend(new_events)
            except Exception:
                LOGGER.error(
                    "[XMentionIntegration] Error polling mentions for %s",
                    persona_id,
                    exc_info=True,
                )

        return events

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _find_connected_personas(
        self, manager: "SAIVerseManager"
    ) -> List[tuple[str, Path]]:
        """Return (persona_id, persona_dir) pairs for X-connected personas."""
        from saiverse.data_paths import get_saiverse_home

        result: List[tuple[str, Path]] = []
        personas_dir = get_saiverse_home() / "personas"
        if not personas_dir.exists():
            return result

        # Only check personas belonging to this city
        for persona_id in manager.all_personas:
            persona_dir = personas_dir / persona_id
            cred_file = persona_dir / "x_credentials.json"
            if cred_file.exists():
                result.append((persona_id, persona_dir))

        return result

    def _poll_persona(
        self, persona_id: str, persona_path: Path
    ) -> List[TriggerEvent]:
        """Poll mentions for a single persona and return new events."""
        # Dynamic import to avoid import-time issues with x_lib
        x_lib_dir = Path(__file__).resolve().parents[2] / "builtin_data" / "tools" / "x_lib"
        if str(x_lib_dir.parent) not in sys.path:
            sys.path.insert(0, str(x_lib_dir.parent))

        from x_lib.credentials import load_credentials
        from x_lib.client import read_mentions, XAPIError

        creds = load_credentials(persona_path)
        if creds is None:
            return []

        # Load since_id state
        since_id = self._load_since_id(persona_path)

        try:
            mentions = read_mentions(
                creds, persona_path, max_results=20, since_id=since_id
            )
        except XAPIError as e:
            LOGGER.warning(
                "[XMentionIntegration] X API error for %s: %s (status %d)",
                persona_id, e.body, e.status_code,
            )
            return []

        if not mentions:
            return []

        LOGGER.info(
            "[XMentionIntegration] Found %d mentions for %s (since_id=%s)",
            len(mentions), persona_id, since_id,
        )

        events: List[TriggerEvent] = []
        max_id: Optional[str] = since_id

        for mention in mentions:
            tweet_id = mention.get("id")
            if not tweet_id:
                continue

            # Track highest ID for next poll
            if max_id is None or tweet_id > max_id:
                max_id = tweet_id

            # Build playbook params for mechanical propagation
            playbook_params = {
                "selected_playbook": "x_reply",
                "trigger_tweet_id": tweet_id,
                "trigger_author_username": mention.get("author_username", ""),
                "trigger_author_name": mention.get("author_name", ""),
                "trigger_mention_text": mention.get("text", ""),
            }

            event = TriggerEvent(
                type=TriggerType.X_MENTION_RECEIVED,
                data={
                    "persona_id": persona_id,
                    "tweet_id": tweet_id,
                    "author_username": mention.get("author_username", ""),
                    "author_name": mention.get("author_name", ""),
                    "mention_text": mention.get("text", ""),
                    "playbook_params_json": json.dumps(
                        playbook_params, ensure_ascii=False
                    ),
                },
            )
            events.append(event)

        # Save updated since_id
        if max_id and max_id != since_id:
            self._save_since_id(persona_path, max_id)

        return events

    def _load_since_id(self, persona_path: Path) -> Optional[str]:
        """Load the last seen mention ID for a persona."""
        state_file = persona_path / _STATE_FILENAME
        if not state_file.exists():
            return None
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            return data.get("last_seen_id")
        except Exception:
            LOGGER.warning(
                "[XMentionIntegration] Failed to read %s", state_file, exc_info=True
            )
            return None

    def _save_since_id(self, persona_path: Path, since_id: str) -> None:
        """Save the last seen mention ID for a persona."""
        state_file = persona_path / _STATE_FILENAME
        try:
            state_file.write_text(
                json.dumps({"last_seen_id": since_id}),
                encoding="utf-8",
            )
            LOGGER.debug(
                "[XMentionIntegration] Saved since_id=%s to %s", since_id, state_file
            )
        except Exception:
            LOGGER.error(
                "[XMentionIntegration] Failed to save %s", state_file, exc_info=True
            )
