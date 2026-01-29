"""Pulse execution controller for priority-based playbook scheduling.

This module manages concurrent playbook executions per persona,
handling priority-based interruption and queueing.
"""
from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from enum import IntEnum
from queue import Queue
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Literal, Optional

from sea.cancellation import CancellationToken, ExecutionCancelledException

if TYPE_CHECKING:
    from sea.runtime import SEARuntime

LOGGER = logging.getLogger(__name__)

# Queue limit - log error if exceeded
QUEUE_LIMIT = 10


class Priority(IntEnum):
    """Execution priority levels (lower number = higher priority)."""
    USER = 1
    SCHEDULE = 2
    AUTO = 3


@dataclass
class ExecutionType:
    """Configuration for each execution type."""
    name: str
    priority: Priority
    same_priority_policy: Literal["first", "last"]  # Which wins when same priority
    on_blocked: Literal["wait", "skip"]  # Behavior when blocked or interrupted


# Execution type configurations
EXECUTION_TYPES: Dict[str, ExecutionType] = {
    "user": ExecutionType(
        name="user",
        priority=Priority.USER,
        same_priority_policy="last",  # Later user message wins
        on_blocked="skip",  # Don't retry interrupted user messages
    ),
    "schedule": ExecutionType(
        name="schedule",
        priority=Priority.SCHEDULE,
        same_priority_policy="first",  # First schedule wins, others queue
        on_blocked="wait",  # Queue for retry after interruption
    ),
    "auto": ExecutionType(
        name="auto",
        priority=Priority.AUTO,
        same_priority_policy="first",  # First auto wins
        on_blocked="skip",  # Skip if busy (will retry in 10s anyway)
    ),
}


@dataclass
class ExecutionRequest:
    """Represents a pending or running playbook execution request."""
    type: str  # "user", "schedule", "auto"
    persona_id: str
    building_id: str
    user_input: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    meta_playbook: Optional[str] = None
    playbook_params: Optional[Dict[str, Any]] = None  # Parameters for meta playbook
    event_callback: Optional[Callable[[Dict[str, Any]], None]] = None
    pulse_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    cancellation_token: CancellationToken = field(default_factory=CancellationToken)
    
    # For schedule resumption
    is_resumption: bool = False
    original_prompt: Optional[str] = None
    
    @property
    def config(self) -> ExecutionType:
        """Get the execution type configuration."""
        return EXECUTION_TYPES.get(self.type, EXECUTION_TYPES["auto"])
    
    @property
    def priority(self) -> Priority:
        """Get the priority level."""
        return self.config.priority


class PulseController:
    """Controls concurrent playbook executions per persona.
    
    Implements priority-based scheduling with interruption support:
    - User requests have highest priority
    - Schedule requests have medium priority
    - Auto requests have lowest priority
    
    When a higher priority request arrives during execution:
    - Current execution is cancelled
    - Interruption message is recorded to memory
    - Higher priority request executes
    - If interrupted request has on_blocked="wait", it's re-queued
    """
    
    def __init__(self, sea_runtime: "SEARuntime"):
        self.sea_runtime = sea_runtime
        
        # Per-persona state
        self._current: Dict[str, ExecutionRequest] = {}  # persona_id -> running request
        self._queues: Dict[str, List[ExecutionRequest]] = {}  # persona_id -> pending queue
        self._locks: Dict[str, threading.RLock] = {}  # persona_id -> lock
        
        LOGGER.info("[PulseController] Initialized")
    
    def _get_lock(self, persona_id: str) -> threading.RLock:
        """Get or create lock for persona."""
        if persona_id not in self._locks:
            self._locks[persona_id] = threading.RLock()
        return self._locks[persona_id]
    
    def _get_queue(self, persona_id: str) -> List[ExecutionRequest]:
        """Get or create queue for persona."""
        if persona_id not in self._queues:
            self._queues[persona_id] = []
        return self._queues[persona_id]
    
    def submit(self, request: ExecutionRequest) -> Optional[List[str]]:
        """Submit an execution request for processing.
        
        Returns:
            List of output strings if executed, None if skipped
            
        Note: Lock is held only during state checks and updates, NOT during
        actual LLM execution. This allows higher priority requests to send
        cancellation signals immediately.
        """
        persona_id = request.persona_id
        lock = self._get_lock(persona_id)
        
        # Phase 1: Check state and determine action (with lock)
        with lock:
            current = self._current.get(persona_id)
            
            if current is None:
                # No execution running, register and proceed
                self._current[persona_id] = request
                action = "execute"
            elif self._should_interrupt(current, request):
                # Cancel current execution
                LOGGER.info(
                    "[PulseController] Interrupting %s (priority=%d) for %s (priority=%d) on persona %s",
                    current.type, current.priority, request.type, request.priority, persona_id
                )
                current.cancellation_token.cancel(interrupted_by=request.type)
                
                # Queue current for resumption if it has wait policy
                if current.config.on_blocked == "wait":
                    self._queue_for_resumption(current)
                
                # Register new request
                self._current[persona_id] = request
                action = "execute"
            else:
                # New request doesn't win - queue or skip based on policy
                if request.config.on_blocked == "wait":
                    self._add_to_queue(request)
                    LOGGER.info(
                        "[PulseController] Queued %s request for persona %s (queue size: %d)",
                        request.type, persona_id, len(self._get_queue(persona_id))
                    )
                    action = "queued"
                else:
                    LOGGER.debug(
                        "[PulseController] Skipping %s request for persona %s (busy with %s)",
                        request.type, persona_id, current.type
                    )
                    action = "skipped"
        
        # Phase 2: Execute WITHOUT holding lock (allows interruption)
        if action == "execute":
            return self._execute_unlocked(request)
        else:
            return None
    
    def _should_interrupt(self, current: ExecutionRequest, new: ExecutionRequest) -> bool:
        """Determine if new request should interrupt current execution."""
        # Higher priority always wins
        if new.priority < current.priority:
            return True
        
        # Same priority - check policy
        if new.priority == current.priority:
            return new.config.same_priority_policy == "last"
        
        # Lower priority never interrupts
        return False
    
    def _add_to_queue(self, request: ExecutionRequest) -> None:
        """Add request to the pending queue."""
        queue = self._get_queue(request.persona_id)
        
        if len(queue) >= QUEUE_LIMIT:
            LOGGER.error(
                "[PulseController] Queue limit (%d) exceeded for persona %s! "
                "Dropping oldest request.",
                QUEUE_LIMIT, request.persona_id
            )
            queue.pop(0)  # Remove oldest
        
        queue.append(request)
    
    def _queue_for_resumption(self, request: ExecutionRequest) -> None:
        """Queue an interrupted request for resumption."""
        # Create a new request with resumption flag
        resumed = ExecutionRequest(
            type=request.type,
            persona_id=request.persona_id,
            building_id=request.building_id,
            user_input=request.user_input,
            metadata=request.metadata,
            meta_playbook=request.meta_playbook,
            event_callback=request.event_callback,
            is_resumption=True,
            original_prompt=request.user_input,
        )
        
        # Add to front of queue (high priority for resumption)
        queue = self._get_queue(request.persona_id)
        queue.insert(0, resumed)
        
        LOGGER.info(
            "[PulseController] Queued %s for resumption on persona %s",
            request.type, request.persona_id
        )
    
    def _execute_unlocked(self, request: ExecutionRequest) -> List[str]:
        """Execute a request WITHOUT holding the lock during LLM calls.
        
        Note: _current[persona_id] must already be set before calling this.
        This allows other threads to send cancellation signals during execution.
        """
        persona_id = request.persona_id
        lock = self._get_lock(persona_id)
        
        try:
            result = self._do_execute(request)
            return result
        except ExecutionCancelledException as e:
            LOGGER.info(
                "[PulseController] Execution cancelled for persona %s, interrupted_by=%s",
                persona_id, e.interrupted_by
            )
            # Record interruption to memory
            self._record_interruption(request, e.interrupted_by)
            return []
        except Exception as e:
            LOGGER.exception(
                "[PulseController] Error executing %s for persona %s: %s",
                request.type, persona_id, e
            )
            return []
        finally:
            with lock:
                if self._current.get(persona_id) is request:
                    del self._current[persona_id]
                
                # Process next queued request
                self._process_queue(persona_id)
    
    def _do_execute(self, request: ExecutionRequest) -> List[str]:
        """Actually execute the request via SEARuntime."""
        persona = self._get_persona(request.persona_id)
        if persona is None:
            LOGGER.warning("[PulseController] Persona %s not found", request.persona_id)
            return []
        
        # Build user input with resumption prompt if needed
        user_input = request.user_input
        if request.is_resumption and request.original_prompt:
            user_input = self._build_resumption_prompt(request)
        
        # Call SEARuntime with cancellation token and pulse_type
        if request.type == "auto":
            # Auto uses run_meta_auto which has no return value
            self.sea_runtime.run_meta_auto(
                persona=persona,
                building_id=request.building_id,
                occupants=self._get_occupants(request.building_id),
                cancellation_token=request.cancellation_token,
                pulse_type=request.type,
            )
            return []
        else:
            # User and schedule use run_meta_user
            return self.sea_runtime.run_meta_user(
                persona=persona,
                user_input=user_input,
                building_id=request.building_id,
                metadata=request.metadata,
                meta_playbook=request.meta_playbook,
                playbook_params=request.playbook_params,
                event_callback=request.event_callback,
                cancellation_token=request.cancellation_token,
                pulse_type=request.type,
            )
    
    def _build_resumption_prompt(self, request: ExecutionRequest) -> str:
        """Build prompt with resumption context."""
        original = request.original_prompt or ""
        return f"""<system>
[前回の処理が中断されました]
中断理由: 優先度の高いリクエストを処理しました
前回のプロンプト: {original}
</system>

{original}"""
    
    def _record_interruption(self, request: ExecutionRequest, interrupted_by: Optional[str]) -> None:
        """Record interruption message to SAIMemory."""
        persona = self._get_persona(request.persona_id)
        if persona is None:
            return
        
        will_resume = request.config.on_blocked == "wait"
        content = f"(中断: {interrupted_by}からのリクエストを優先しました)"
        
        try:
            msg = {
                "role": "assistant",
                "content": content,
                "metadata": {
                    "pulse_id": request.pulse_id,
                    "tags": ["internal", "interrupted"],
                    "interrupted_by": interrupted_by,
                    "will_resume": will_resume,
                },
            }
            persona.history_manager.add_message(msg, request.building_id, heard_by=None)
        except Exception:
            LOGGER.exception("[PulseController] Failed to record interruption message")
    
    def _process_queue(self, persona_id: str) -> None:
        """Process the next item in the queue for a persona."""
        lock = self._get_lock(persona_id)
        queue = self._get_queue(persona_id)
        
        with lock:
            if not queue:
                return
            
            if persona_id in self._current:
                # Something else is already running
                return
            
            next_request = queue.pop(0)
            self._current[persona_id] = next_request
        
        LOGGER.info(
            "[PulseController] Processing queued %s request for persona %s",
            next_request.type, persona_id
        )
        
        # Execute in a new thread to avoid blocking
        def run():
            self._execute_unlocked(next_request)
        
        threading.Thread(target=run, daemon=True).start()
    
    def _get_persona(self, persona_id: str):
        """Get persona object from manager."""
        manager = getattr(self.sea_runtime, "manager", None)
        if manager is None:
            return None
        personas = getattr(manager, "all_personas", {})
        return personas.get(persona_id)
    
    def _get_occupants(self, building_id: str) -> List[str]:
        """Get occupants of a building."""
        manager = getattr(self.sea_runtime, "manager", None)
        if manager is None:
            return []
        occupants = getattr(manager, "occupants", {})
        return occupants.get(building_id, [])
    
    # Convenience methods for callers
    def submit_user(
        self,
        persona_id: str,
        building_id: str,
        user_input: str,
        metadata: Optional[Dict[str, Any]] = None,
        meta_playbook: Optional[str] = None,
        playbook_params: Optional[Dict[str, Any]] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Optional[List[str]]:
        """Submit a user input request."""
        request = ExecutionRequest(
            type="user",
            persona_id=persona_id,
            building_id=building_id,
            user_input=user_input,
            metadata=metadata,
            meta_playbook=meta_playbook,
            playbook_params=playbook_params,
            event_callback=event_callback,
        )
        return self.submit(request)
    
    def submit_schedule(
        self,
        persona_id: str,
        building_id: str,
        user_input: str,
        metadata: Optional[Dict[str, Any]] = None,
        meta_playbook: Optional[str] = None,
        playbook_params: Optional[Dict[str, Any]] = None,
    ) -> Optional[List[str]]:
        """Submit a scheduled execution request."""
        request = ExecutionRequest(
            type="schedule",
            persona_id=persona_id,
            building_id=building_id,
            user_input=user_input,
            metadata=metadata,
            meta_playbook=meta_playbook,
            playbook_params=playbook_params,
        )
        return self.submit(request)
    
    def submit_auto(
        self,
        persona_id: str,
        building_id: str,
    ) -> Optional[List[str]]:
        """Submit an autonomous pulse request."""
        request = ExecutionRequest(
            type="auto",
            persona_id=persona_id,
            building_id=building_id,
        )
        return self.submit(request)


__all__ = [
    "PulseController",
    "ExecutionRequest",
    "ExecutionType",
    "EXECUTION_TYPES",
    "Priority",
    "QUEUE_LIMIT",
]
