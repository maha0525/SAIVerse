"""Autonomous behavior manager for individual personas.

Each AutonomyManager runs a background loop per persona:
  Decision (heavy model) → Execution (light model) → Inspection → Decision → ...

Replaces ConversationManager's building-level round-robin with
persona-level autonomous decision-making.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from saiverse.saiverse_manager import SAIVerseManager
    from persona.core import PersonaCore

LOGGER = logging.getLogger(__name__)

# Default interval between cycles (minutes)
DEFAULT_INTERVAL_MINUTES = 5


class AutonomyState(str, Enum):
    """Current state of the autonomy manager."""
    STOPPED = "stopped"
    RUNNING = "running"
    DECIDING = "deciding"
    EXECUTING = "executing"
    WAITING = "waiting"      # Interval wait between cycles
    INTERRUPTED = "interrupted"  # Paused for user interaction


@dataclass
class CycleReport:
    """Report from a completed execution cycle."""
    cycle_id: str
    playbook: Optional[str] = None
    intent: str = ""
    status: str = "pending"  # pending, completed, error, interrupted, timeout
    started_at: float = 0.0
    completed_at: float = 0.0
    error: Optional[str] = None
    execution_output: str = ""   # Raw output from the execution playbook
    artifacts: str = ""          # Post-execution artifact summary


class AutonomyManager:
    """Manages the autonomous behavior loop for a single persona.

    Lifecycle:
        1. start() → Creates Stelis thread, begins background loop
        2. Loop runs: decide → execute → wait → decide → ...
        3. User interrupt → pause loop, switch to main thread, respond, resume
        4. stop() → Ends Stelis thread, stops background loop
    """

    def __init__(
        self,
        persona_id: str,
        manager: SAIVerseManager,
        *,
        interval_minutes: float = DEFAULT_INTERVAL_MINUTES,
        decision_model: Optional[str] = None,
        execution_model: Optional[str] = None,
    ):
        self.persona_id = persona_id
        self.manager = manager
        self.interval_minutes = interval_minutes
        self.decision_model = decision_model
        self.execution_model = execution_model

        self._state = AutonomyState.STOPPED
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._stelis_thread_id: Optional[str] = None
        self._original_thread_id: Optional[str] = None
        self._current_cycle_id: Optional[str] = None
        self._last_report: Optional[CycleReport] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def state(self) -> AutonomyState:
        return self._state

    @property
    def is_running(self) -> bool:
        return self._state not in (AutonomyState.STOPPED,)

    @property
    def last_report(self) -> Optional[CycleReport]:
        return self._last_report

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> bool:
        """Start the autonomous behavior loop.

        Creates a Stelis thread and begins the background loop.
        Returns True if started successfully, False if already running.
        """
        with self._lock:
            if self._state != AutonomyState.STOPPED:
                LOGGER.warning(
                    "[Autonomy:%s] Cannot start: already in state %s",
                    self.persona_id, self._state,
                )
                return False

            # Start Stelis thread
            persona = self._get_persona()
            if not persona:
                LOGGER.error("[Autonomy:%s] Persona not found", self.persona_id)
                return False

            sai_mem = getattr(persona, "sai_memory", None)
            if not sai_mem or not sai_mem.is_ready():
                LOGGER.error("[Autonomy:%s] SAIMemory not ready", self.persona_id)
                return False

            # Save original thread for restoration on stop/interrupt
            self._original_thread_id = sai_mem.get_current_thread()

            # Create Stelis thread for autonomous work
            stelis = sai_mem.start_stelis_thread(
                label="autonomy",
                chronicle_prompt="自律行動サイクルの記録",
            )
            if stelis:
                self._stelis_thread_id = stelis.thread_id
                sai_mem.set_active_thread(stelis.thread_id)
                LOGGER.info(
                    "[Autonomy:%s] Started Stelis thread: %s",
                    self.persona_id, stelis.thread_id,
                )
            else:
                LOGGER.warning(
                    "[Autonomy:%s] Failed to create Stelis thread, using main thread",
                    self.persona_id,
                )

            # Register PulseController callbacks for interrupt/resume
            self._register_pulse_callbacks()

            self._stop_event.clear()
            self._state = AutonomyState.RUNNING
            self._thread = threading.Thread(
                target=self._loop,
                name=f"autonomy-{self.persona_id}",
                daemon=True,
            )
            self._thread.start()

            LOGGER.info(
                "[Autonomy:%s] Started (interval=%.1f min)",
                self.persona_id, self.interval_minutes,
            )
            return True

    def stop(self) -> bool:
        """Stop the autonomous behavior loop.

        Ends the Stelis thread and restores the original thread.
        Returns True if stopped, False if not running.
        """
        with self._lock:
            if self._state == AutonomyState.STOPPED:
                return False

            self._stop_event.set()
            self._state = AutonomyState.STOPPED

        # Wait for thread to finish (outside lock)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)

        # End Stelis thread and restore original
        self._cleanup_stelis()

        LOGGER.info("[Autonomy:%s] Stopped", self.persona_id)
        return True

    def pause_for_user(self) -> Optional[str]:
        """Pause autonomy for user interaction.

        Switches to the original thread so user conversation
        happens in the normal context.

        Returns the original thread_id, or None if not running.
        """
        if not self.is_running:
            return None

        self._state = AutonomyState.INTERRUPTED

        # Switch to original thread
        persona = self._get_persona()
        sai_mem = getattr(persona, "sai_memory", None) if persona else None
        if sai_mem and self._original_thread_id:
            sai_mem.set_active_thread(self._original_thread_id)
            LOGGER.info(
                "[Autonomy:%s] Paused for user, switched to thread %s",
                self.persona_id, self._original_thread_id,
            )

        return self._original_thread_id

    def resume_from_user(self) -> bool:
        """Resume autonomy after user interaction completes.

        Switches back to the Stelis thread.
        Returns True if resumed, False if not in interrupted state.
        """
        if self._state != AutonomyState.INTERRUPTED:
            return False

        # Switch back to Stelis thread
        persona = self._get_persona()
        sai_mem = getattr(persona, "sai_memory", None) if persona else None
        if sai_mem and self._stelis_thread_id:
            sai_mem.set_active_thread(self._stelis_thread_id)
            LOGGER.info(
                "[Autonomy:%s] Resumed, switched to Stelis thread %s",
                self.persona_id, self._stelis_thread_id,
            )

        self._state = AutonomyState.RUNNING
        return True

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _register_pulse_callbacks(self) -> None:
        """Register callbacks on PulseController for interrupt/resume."""
        pc = getattr(self.manager, "pulse_controller", None)
        if not pc:
            return

        my_id = self.persona_id

        def on_interrupt(persona_id: str, interrupted_by: str) -> None:
            if persona_id == my_id and self.is_running:
                self.pause_for_user()

        def on_user_complete(persona_id: str) -> None:
            if persona_id == my_id and self._state == AutonomyState.INTERRUPTED:
                self.resume_from_user()

        pc.register_on_interrupt(on_interrupt)
        pc.register_on_user_complete(on_user_complete)

    def set_interval(self, minutes: float) -> None:
        """Update the loop interval (takes effect next cycle)."""
        self.interval_minutes = max(0.5, minutes)
        LOGGER.info(
            "[Autonomy:%s] Interval set to %.1f min",
            self.persona_id, self.interval_minutes,
        )

    def set_models(
        self,
        decision_model: Optional[str] = None,
        execution_model: Optional[str] = None,
    ) -> None:
        """Update the models used for decision/execution phases."""
        if decision_model is not None:
            self.decision_model = decision_model
        if execution_model is not None:
            self.execution_model = execution_model

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """Get current status as a dict (for API responses)."""
        return {
            "persona_id": self.persona_id,
            "state": self._state.value,
            "interval_minutes": self.interval_minutes,
            "decision_model": self.decision_model,
            "execution_model": self.execution_model,
            "stelis_thread_id": self._stelis_thread_id,
            "current_cycle_id": self._current_cycle_id,
            "last_report": {
                "cycle_id": self._last_report.cycle_id,
                "playbook": self._last_report.playbook,
                "intent": self._last_report.intent,
                "status": self._last_report.status,
                "has_artifacts": bool(self._last_report.artifacts),
            } if self._last_report else None,
        }

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        """Background loop: decide → execute → wait → repeat."""
        LOGGER.info("[Autonomy:%s] Loop started", self.persona_id)

        while not self._stop_event.is_set():
            try:
                # Skip cycle if interrupted (user interaction in progress)
                if self._state == AutonomyState.INTERRUPTED:
                    self._stop_event.wait(5)
                    continue

                cycle_id = str(uuid.uuid4())[:8]
                self._current_cycle_id = cycle_id
                report = CycleReport(cycle_id=cycle_id, started_at=time.time())

                LOGGER.info("[Autonomy:%s] Cycle %s: starting", self.persona_id, cycle_id)

                # Phase 1: Decision
                self._state = AutonomyState.DECIDING
                decision = self._run_decision(report)

                if self._stop_event.is_set():
                    break

                # Phase 2: Execution
                if decision:
                    self._state = AutonomyState.EXECUTING
                    self._run_execution(decision, report)

                report.completed_at = time.time()
                if report.status == "pending":
                    report.status = "completed"
                self._last_report = report

                elapsed = report.completed_at - report.started_at
                LOGGER.info(
                    "[Autonomy:%s] Cycle %s: %s (%.1fs)",
                    self.persona_id, cycle_id, report.status, elapsed,
                )

            except Exception as exc:
                LOGGER.exception(
                    "[Autonomy:%s] Cycle error: %s", self.persona_id, exc,
                )
                report.status = "error"
                report.error = str(exc)
                self._last_report = report

            # Phase 3: Wait
            self._state = AutonomyState.WAITING
            self._current_cycle_id = None
            wait_seconds = self.interval_minutes * 60
            self._stop_event.wait(wait_seconds)

        self._state = AutonomyState.STOPPED
        LOGGER.info("[Autonomy:%s] Loop ended", self.persona_id)

    # ------------------------------------------------------------------
    # Decision phase
    # ------------------------------------------------------------------

    def _run_decision(self, report: CycleReport) -> Optional[Dict[str, Any]]:
        """Run the decision phase using the heavy model.

        Executes meta_autonomy_decision playbook and parses the structured output.
        Returns a dict with 'activity_type' and 'intent' keys, or None to skip.
        """
        persona = self._get_persona()
        if not persona:
            return None

        building_id = self._get_building_id(persona)
        if not building_id:
            LOGGER.warning("[Autonomy:%s] No building assigned", self.persona_id)
            return None

        from sea.pulse_controller import ExecutionRequest

        # Build last report text for the decision playbook
        last_report_text = "初回サイクル（前回の結果なし）"
        if self._last_report:
            parts = [
                f"activity_type: {self._last_report.playbook}",
                f"intent: {self._last_report.intent}",
                f"status: {self._last_report.status}",
            ]
            if self._last_report.error:
                parts.append(f"error: {self._last_report.error}")
            if self._last_report.execution_output:
                # Truncate to keep cache-friendly
                output_preview = self._last_report.execution_output[:500]
                parts.append(f"\n実行出力:\n{output_preview}")
            if self._last_report.artifacts:
                parts.append(f"\n成果物:\n{self._last_report.artifacts}")
            last_report_text = "\n".join(parts)

        request = ExecutionRequest(
            type="autonomy",
            persona_id=self.persona_id,
            building_id=building_id,
            user_input=None,
            meta_playbook="meta_autonomy_decision",
            args={"last_report": last_report_text},
        )

        try:
            result = self.manager.pulse_controller.submit(request)
        except Exception as exc:
            LOGGER.error(
                "[Autonomy:%s] Decision phase failed: %s",
                self.persona_id, exc,
            )
            report.status = "error"
            report.error = f"Decision failed: {exc}"
            return None

        # Parse decision - try submit result first, then fall back to SAIMemory
        decision = self._parse_decision_output(result)
        if not decision:
            decision = self._read_decision_from_memory()
        if not decision:
            LOGGER.info("[Autonomy:%s] Decision: no actionable output", self.persona_id)
            return None

        activity_type = decision.get("activity_type", "wait")
        intent = decision.get("intent", "")

        LOGGER.info(
            "[Autonomy:%s] Decision: %s — %s",
            self.persona_id, activity_type, intent,
        )

        report.playbook = activity_type
        report.intent = intent

        if activity_type == "wait":
            report.status = "completed"
            return None

        return decision

    def _read_decision_from_memory(self) -> Optional[Dict[str, Any]]:
        """Read the decision from the most recent autonomy_decision message in SAIMemory."""
        import json
        import re

        persona = self._get_persona()
        if not persona:
            return None

        sai_mem = getattr(persona, "sai_memory", None)
        if not sai_mem or not sai_mem.is_ready():
            return None

        try:
            # Get recent messages and look for the decision
            recent = sai_mem.recent_persona_messages(max_chars=5000)
            for msg in reversed(recent):
                content = msg.get("content", "")
                metadata = msg.get("metadata", {})
                tags = metadata.get("tags", [])

                if "autonomy_decision" not in tags:
                    continue

                # Try to parse JSON from the message content
                text = content.strip()
                md_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
                if md_match:
                    text = md_match.group(1).strip()

                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        next_task = parsed.get("next_task", {})
                        if next_task:
                            LOGGER.info(
                                "[Autonomy:%s] Decision read from memory: %s",
                                self.persona_id, next_task.get("activity_type"),
                            )
                            return {
                                "activity_type": next_task.get("activity_type", "wait"),
                                "intent": next_task.get("intent", ""),
                                "inspection": parsed.get("inspection", {}),
                            }
                except (json.JSONDecodeError, TypeError):
                    continue
        except Exception as exc:
            LOGGER.debug("[Autonomy:%s] Failed to read decision from memory: %s", self.persona_id, exc)

        return None

    def _parse_decision_output(self, result: Optional[list]) -> Optional[Dict[str, Any]]:
        """Parse the decision playbook output into activity_type + intent."""
        if not result:
            return None

        # The playbook output is a list of strings. The decision is stored
        # in the last message which should contain the JSON structured output.
        import json
        import re

        for item in reversed(result):
            if not isinstance(item, str):
                continue
            text = item.strip()
            # Try to extract JSON from the output
            md_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
            if md_match:
                text = md_match.group(1).strip()
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    next_task = parsed.get("next_task", {})
                    if next_task:
                        return {
                            "activity_type": next_task.get("activity_type", "wait"),
                            "intent": next_task.get("intent", ""),
                            "inspection": parsed.get("inspection", {}),
                        }
            except (json.JSONDecodeError, TypeError):
                continue

        return None

    # ------------------------------------------------------------------
    # Execution phase
    # ------------------------------------------------------------------

    def _run_execution(self, decision: Dict[str, Any], report: CycleReport) -> None:
        """Run the execution phase using a playbook matching the activity type.

        The intent from the decision phase is passed as user input to the
        execution playbook, guiding the lightweight model's behavior.
        After execution, artifacts are gathered for the next inspection.
        """
        persona = self._get_persona()
        if not persona:
            report.status = "error"
            report.error = "Persona not found"
            return

        building_id = self._get_building_id(persona)
        if not building_id:
            report.status = "error"
            report.error = "No building assigned"
            return

        activity_type = decision.get("activity_type", "wait")
        intent = decision.get("intent", "")

        # Map activity_type to a playbook name
        playbook_map = {
            "conversation": "meta_auto",
            "memory_organization": "autonomy_memory_organization",
            "creation": "autonomy_creation",
            "self_reflection": "meta_auto",
            "web_research": "autonomy_web_research",
        }
        playbook_name = playbook_map.get(activity_type, "meta_auto")

        from sea.pulse_controller import ExecutionRequest

        # Pass intent through args (not user_input, to avoid showing in Building UI)
        request = ExecutionRequest(
            type="autonomy",
            persona_id=self.persona_id,
            building_id=building_id,
            user_input=None,
            meta_playbook=playbook_name,
            args={"input": intent},
        )

        try:
            result = self.manager.pulse_controller.submit(request)
            report.status = "completed"
            if result:
                report.execution_output = "\n".join(result)[:2000]
        except Exception as exc:
            LOGGER.error(
                "[Autonomy:%s] Execution phase failed: %s",
                self.persona_id, exc,
            )
            report.status = "error"
            report.error = f"Execution failed: {exc}"
            return

        # Gather artifacts post-execution
        try:
            report.artifacts = self._gather_artifacts(activity_type)
        except Exception as exc:
            LOGGER.warning(
                "[Autonomy:%s] Artifact gathering failed: %s",
                self.persona_id, exc,
            )

    # ------------------------------------------------------------------
    # Artifact gathering
    # ------------------------------------------------------------------

    def _gather_artifacts(self, activity_type: str) -> str:
        """Gather post-execution artifacts for inspection.

        Returns a compact summary of what was created/changed.
        """
        persona = self._get_persona()
        if not persona:
            return ""

        sai_mem = getattr(persona, "sai_memory", None)
        if not sai_mem or not sai_mem.is_ready():
            return ""

        if activity_type == "memory_organization":
            return self._gather_memopedia_health()
        elif activity_type in ("web_research", "creation"):
            return self._gather_recent_artifacts()
        return ""

    def _gather_memopedia_health(self) -> str:
        """Run memopedia_health to show post-organization state."""
        try:
            from builtin_data.tools.memopedia_health import memopedia_health
            from tools.context import persona_context

            persona = self._get_persona()
            persona_dir = getattr(
                getattr(persona, "sai_memory", None), "persona_dir", None,
            )
            with persona_context(self.persona_id, str(persona_dir) if persona_dir else None, self.manager):
                return memopedia_health()
        except Exception as exc:
            LOGGER.debug("[Autonomy:%s] memopedia_health failed: %s", self.persona_id, exc)
            return ""

    def _gather_recent_artifacts(self) -> str:
        """List recently created/updated Memopedia pages and documents."""
        persona = self._get_persona()
        if not persona:
            return ""

        lines = []

        # Recent Memopedia pages (updated in the last 10 minutes)
        try:
            sai_mem = getattr(persona, "sai_memory", None)
            if sai_mem and sai_mem.is_ready():
                from sai_memory.memopedia import Memopedia, init_memopedia_tables
                init_memopedia_tables(sai_mem.conn)
                memopedia = Memopedia(sai_mem.conn)
                cutoff = time.time() - 600  # 10 minutes ago

                tree = memopedia.get_tree()
                recent_pages = []

                def _find_recent(pages):
                    for p in pages:
                        updated = p.get("updated_at", 0)
                        if updated > cutoff:
                            recent_pages.append(p)
                        _find_recent(p.get("children", []))

                for cat in ("people", "terms", "plans", "events"):
                    _find_recent(tree.get(cat, []))

                if recent_pages:
                    lines.append(f"### 直近更新された Memopedia ページ ({len(recent_pages)}件)")
                    for p in recent_pages:
                        content_len = len(p.get("content", ""))
                        lines.append(f"  - {p.get('title', '?')} ({content_len}字, {p.get('category', '?')})")
        except Exception as exc:
            LOGGER.debug("[Autonomy:%s] Memopedia artifact scan failed: %s", self.persona_id, exc)

        # Recent documents from building
        try:
            building_id = self._get_building_id(persona)
            if building_id:
                items = self.manager.item_service.items_by_building.get(building_id, [])
                recent_docs = []
                for item_id in items:
                    item = self.manager.item_service.items.get(item_id)
                    if item and (item.get("type") or "").lower() == "document":
                        recent_docs.append(item)

                if recent_docs:
                    lines.append(f"### ビルディング内ドキュメント ({len(recent_docs)}件)")
                    for doc in recent_docs[:5]:
                        lines.append(f"  - {doc.get('name', '?')} (id: {doc.get('id', '?')[:12]}...)")
        except Exception as exc:
            LOGGER.debug("[Autonomy:%s] Document artifact scan failed: %s", self.persona_id, exc)

        return "\n".join(lines) if lines else ""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_persona(self) -> Optional[PersonaCore]:
        """Get the persona object from the manager."""
        return self.manager.all_personas.get(self.persona_id)

    def _get_building_id(self, persona) -> Optional[str]:
        """Get the building ID where the persona currently is."""
        return getattr(persona, "current_building_id", None)

    def _cleanup_stelis(self) -> None:
        """End the Stelis thread and restore original thread."""
        persona = self._get_persona()
        sai_mem = getattr(persona, "sai_memory", None) if persona else None

        if sai_mem and self._stelis_thread_id:
            try:
                sai_mem.end_stelis_thread(
                    thread_id=self._stelis_thread_id,
                    status="completed",
                )
                LOGGER.info(
                    "[Autonomy:%s] Ended Stelis thread %s",
                    self.persona_id, self._stelis_thread_id,
                )
            except Exception as exc:
                LOGGER.warning(
                    "[Autonomy:%s] Failed to end Stelis thread: %s",
                    self.persona_id, exc,
                )

        # Restore original thread
        if sai_mem and self._original_thread_id:
            try:
                sai_mem.set_active_thread(self._original_thread_id)
            except Exception:
                pass

        self._stelis_thread_id = None
        self._original_thread_id = None
