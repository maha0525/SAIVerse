"""PulseContext — Pulse-level log sharing for unified memory architecture.

Each Pulse (a single unit of AI cognition, triggered by user input or
autonomous schedule) generates a PulseContext that tracks all node outputs
across all Playbooks executed within that Pulse.  Sub-playbooks share the
same PulseContext via parent_state reference inheritance (same pattern as
``_pulse_usage_accumulator`` and ``_activity_trace``).

PulseContext replaces ``_intermediate_msgs`` and additionally serves as the
source for ``pulse_logs`` DB persistence.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class PulseLogEntry:
    """A single log entry within a Pulse.

    Attributes:
        id: Unique identifier (UUID).
        role: Message role — "user", "assistant", "tool", "system".
        content: Textual content of the entry.
        node_id: Playbook node that produced this entry.
        playbook_name: Playbook that was executing.
        important: If True, this entry is also written to SAIMemory messages.
        created_at: Unix epoch timestamp.
        tool_calls: For assistant messages that invoke tools — list of tool call
                    dicts following the function calling protocol.
        tool_call_id: For tool result messages — the corresponding call ID.
        tool_name: For tool result messages — the tool function name.
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    role: str = ""
    content: str = ""
    node_id: Optional[str] = None
    playbook_name: Optional[str] = None
    important: bool = False
    created_at: int = field(default_factory=lambda: int(time.time()))
    # Function calling protocol fields
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    tool_name: Optional[str] = None


@dataclass
class PulseContext:
    """Shared mutable log list for a single Pulse execution.

    Passed through ``state["_pulse_context"]`` and inherited by sub-playbooks
    via shared reference.  At Pulse completion the logs are flushed to the
    ``pulse_logs`` DB table.
    """

    pulse_id: str
    thread_id: str
    logs: List[PulseLogEntry] = field(default_factory=list)

    def append(self, entry: PulseLogEntry) -> None:
        """Append a log entry to this Pulse's log list."""
        self.logs.append(entry)

    def get_important_logs(self) -> List[PulseLogEntry]:
        """Return only the entries flagged as important."""
        return [e for e in self.logs if e.important]

    def get_protocol_messages(self) -> List[Dict[str, Any]]:
        """Reconstruct LLM API-compatible message list from logged entries.

        This replaces ``_intermediate_msgs`` — context_profile-based LLM nodes
        call this method to obtain the conversation history accumulated during
        the current Pulse (including function calling protocol pairs).

        All protocol-compatible roles (user, assistant, tool, system) are
        included.  Mid-conversation system messages are handled by the LLM
        client layer's ``convert_system_to_user`` mechanism for backends that
        require system messages at the beginning only.

        Entries with roles outside the LLM protocol (e.g. speak-only entries
        that duplicate assistant content) are skipped.
        """
        msgs: List[Dict[str, Any]] = []
        for entry in self.logs:
            if entry.role == "tool" and entry.tool_call_id:
                msg: Dict[str, Any] = {
                    "role": "tool",
                    "tool_call_id": entry.tool_call_id,
                    "name": entry.tool_name or "",
                    "content": entry.content,
                }
            elif entry.role == "assistant" and entry.tool_calls:
                msg = {
                    "role": "assistant",
                    "content": entry.content,
                    "tool_calls": entry.tool_calls,
                }
            elif entry.role in ("user", "assistant", "system"):
                msg = {"role": entry.role, "content": entry.content}
            else:
                continue
            msgs.append(msg)
        return msgs
