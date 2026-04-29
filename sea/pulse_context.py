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

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger("saiverse.pulse_context")


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
class LineFrame:
    """Identifies one running line within a Pulse.

    A "line" is the execution flow defined by Intent A v0.14: model/cache type
    (main/sub) × call relation (parent/child) × Pulse-stack position
    (root/nested). One Pulse holds a stack of LineFrames as ranges open and
    close; the topmost frame is the currently executing line.

    Attributes:
        line_id: UUID identifying this line. Persisted as ``messages.line_id``
            so layer [3] (Track-internal sub-cache) can distinguish parallel
            root sub-lines within the same Track.
        role: ``'main_line'`` / ``'sub_line'`` / ``'meta_judgment'`` / ``'nested'``.
            Same vocabulary as ``messages.line_role``. The mapping to 7-layer
            storage:
              * main_line + root  → [2] main cache
              * sub_line + root   → [3] Track-internal sub-cache
              * sub_line + nested → [4] nested temp context
              * meta_judgment     → [1] meta-judgment log (with discardable scope)
        parent_id: Parent LineFrame's ``line_id``, or ``None`` when this is the
            root line for the current Pulse.
        track_id: Active Track at line creation time. Mirrored to
            ``messages.origin_track_id`` for stored messages.
        created_at: Unix epoch timestamp for diagnostics.
    """

    line_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    role: str = "main_line"
    parent_id: Optional[str] = None
    track_id: Optional[str] = None
    created_at: int = field(default_factory=lambda: int(time.time()))

    @property
    def is_root(self) -> bool:
        """True when this line was started by a Pulse scheduler (no parent)."""
        return self.parent_id is None

    @property
    def is_nested(self) -> bool:
        """True when this line was spawned by another line within the Pulse."""
        return self.parent_id is not None


@dataclass
class DeferredTrackOp:
    """A Track operation queued during a Pulse, applied when the Pulse completes.

    Direct in-Pulse Track switching causes the LLM to keep emitting "next-Track
    work" within the current Pulse's main cache, because the cache already
    contains the persona's stated decision to switch (Intent A v0.14, Intent B
    v0.11). Deferring all Track-status-changing operations until Pulse
    completion guarantees that Track switches happen at Pulse boundaries —
    the persona's current Pulse winds down naturally, then the next Pulse
    starts under the new active Track.

    Last-wins resolution applies only between competing ``activate`` ops in the
    same Pulse (multiple "set running Track" requests). Other op types stack
    in order so a sequence like ``pause(A) → create(B) → activate(B)`` applies
    cleanly at flush time.
    """

    op_type: str
    # 'create_post_activate' / 'activate' / 'pause' / 'complete' / 'abort'
    # Note: track_create itself runs immediately so the persona can read the
    # new track_id in the same round; only the optional activate-after-create
    # path is enqueued, recorded as 'activate' with the new track_id.
    track_id: Optional[str]
    args: Dict[str, Any] = field(default_factory=dict)
    requested_at: int = field(default_factory=lambda: int(time.time()))


@dataclass
class PulseContext:
    """Shared mutable log list for a single Pulse execution.

    Passed through ``state["_pulse_context"]`` and inherited by sub-playbooks
    via shared reference.  At Pulse completion the logs are flushed to the
    ``pulse_logs`` DB table.

    The ``_line_stack`` field tracks the currently-running line hierarchy
    (Intent A v0.14, Intent B v0.11). Push when entering a new line, pop when
    it completes. The topmost frame is the active line at any point.

    The ``deferred_track_ops`` queue holds Track operations issued by spell
    invocations during the Pulse; the runtime applies them at Pulse completion
    so Track switches never interrupt the current Pulse's content generation.
    """

    pulse_id: str
    thread_id: str
    logs: List[PulseLogEntry] = field(default_factory=list)
    _line_stack: List[LineFrame] = field(default_factory=list)
    deferred_track_ops: List[DeferredTrackOp] = field(default_factory=list)

    def append(self, entry: PulseLogEntry) -> None:
        """Append a log entry to this Pulse's log list."""
        self.logs.append(entry)

    def get_important_logs(self) -> List[PulseLogEntry]:
        """Return only the entries flagged as important."""
        return [e for e in self.logs if e.important]

    # ------------------------------------------------------------------
    # Line hierarchy management (Intent B v0.11, P0-1)
    # ------------------------------------------------------------------

    def push_line(
        self,
        role: str = "main_line",
        track_id: Optional[str] = None,
        parent_id: Optional[str] = None,
    ) -> LineFrame:
        """Open a new line frame and push it onto the stack.

        Args:
            role: Line role (``'main_line'`` / ``'sub_line'`` / ``'meta_judgment'``
                / ``'nested'``). See ``LineFrame.role`` docstring for the mapping
                to 7-layer storage.
            track_id: Active Track for the new line. If ``None``, inherits from
                the current line on the stack.
            parent_id: Parent line ID. If ``None``, inferred from the topmost
                frame on the stack (an empty stack yields a root line).

        Returns:
            The newly pushed ``LineFrame``. Callers should record its ``line_id``
            and pass to ``messages.line_id`` when persisting messages produced
            by this line.
        """
        current = self.current_line()
        if parent_id is None and current is not None:
            parent_id = current.line_id
        if track_id is None and current is not None:
            track_id = current.track_id
        frame = LineFrame(role=role, parent_id=parent_id, track_id=track_id)
        self._line_stack.append(frame)
        return frame

    def pop_line(self) -> Optional[LineFrame]:
        """Pop and return the topmost line frame.

        Returns ``None`` when the stack is empty (defensive — should not happen
        in well-formed playbook execution; logged but not raised so cleanup
        paths stay safe).
        """
        if not self._line_stack:
            return None
        return self._line_stack.pop()

    def current_line(self) -> Optional[LineFrame]:
        """Return the topmost line frame, or ``None`` when no line is active."""
        if not self._line_stack:
            return None
        return self._line_stack[-1]

    def current_line_metadata(self) -> Dict[str, Optional[str]]:
        """Return the active line's storage metadata as a flat dict.

        Convenience for ``_store_memory`` calls that need to forward
        ``line_role`` / ``line_id`` / ``origin_track_id``. Returns empty values
        when no line is active (caller falls back to legacy behavior).
        """
        current = self.current_line()
        if current is None:
            return {"line_role": None, "line_id": None, "origin_track_id": None}
        return {
            "line_role": current.role,
            "line_id": current.line_id,
            "origin_track_id": current.track_id,
        }

    # ------------------------------------------------------------------
    # Deferred Track operations (Intent A v0.14, Intent B v0.11)
    # ------------------------------------------------------------------

    def enqueue_track_op(
        self,
        op_type: str,
        track_id: Optional[str] = None,
        **args: Any,
    ) -> DeferredTrackOp:
        """Queue a Track operation to be applied at Pulse completion.

        For ``activate`` ops, last-wins resolution is applied: if the queue
        already contains an activate op, the older one is dropped with a
        warning before the new one is appended. This handles spell-loop rounds
        where the LLM tries to set multiple "next active" Tracks at once.

        Other op types (``pause`` / ``complete`` / ``abort``) preserve order so
        sequences like ``pause(A) → activate(B)`` produce the expected final
        state.
        """
        if op_type == "activate":
            existing = [op for op in self.deferred_track_ops if op.op_type == "activate"]
            if existing:
                LOGGER.warning(
                    "[pulse_context] Replacing %d earlier activate op(s) with new track_id=%s "
                    "(last-wins; earlier targets: %s)",
                    len(existing), track_id,
                    [op.track_id for op in existing],
                )
                self.deferred_track_ops = [
                    op for op in self.deferred_track_ops if op.op_type != "activate"
                ]

        op = DeferredTrackOp(op_type=op_type, track_id=track_id, args=dict(args))
        self.deferred_track_ops.append(op)
        LOGGER.debug(
            "[pulse_context] Enqueued deferred track op: type=%s track_id=%s args=%s",
            op_type, track_id, args,
        )
        return op

    def has_deferred_track_ops(self) -> bool:
        """True when at least one Track op is queued for Pulse completion."""
        return bool(self.deferred_track_ops)

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
                # LLM function-calling tool result (has matching assistant tool_calls)
                msg: Dict[str, Any] = {
                    "role": "tool",
                    "tool_call_id": entry.tool_call_id,
                    "name": entry.tool_name or "",
                    "content": entry.content,
                }
            elif entry.role == "tool" and not entry.tool_call_id:
                # Playbook-defined TOOL node result (no function-calling pair).
                # Emit as user message so LLM APIs accept it (tool role requires
                # tool_call_id).  Wrap in <system> tag for context.
                tool_label = entry.tool_name or "tool"
                msg = {
                    "role": "user",
                    "content": f"<system>[{tool_label}] {entry.content}</system>",
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
