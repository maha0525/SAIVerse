from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

DEFAULT_PRIORITY = "normal"
ISO_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime(ISO_FORMAT)


class TaskStorageError(RuntimeError):
    """Base exception for task storage failures."""


class TaskConflictError(TaskStorageError):
    """Raised when an update fails due to optimistic locking conflicts."""


class TaskNotFoundError(TaskStorageError):
    """Raised when the requested task or step cannot be located."""


TaskStatus = str
TaskStepStatus = str


@dataclass(frozen=True)
class TaskStepRecord:
    id: str
    task_id: str
    position: int
    title: str
    description: Optional[str]
    status: TaskStepStatus
    notes: Optional[str]
    created_at: str
    updated_at: str
    completed_at: Optional[str]
    version: int


@dataclass(frozen=True)
class TaskRecord:
    id: str
    title: str
    goal: str
    summary: str
    notes: Optional[str]
    status: TaskStatus
    priority: str
    origin: str
    active_step_id: Optional[str]
    due_at: Optional[str]
    created_at: str
    updated_at: str
    completed_at: Optional[str]
    version: int
    last_actor: Optional[str]
    steps: List[TaskStepRecord]


@dataclass(frozen=True)
class TaskHistoryEntry:
    id: str
    task_id: str
    step_id: Optional[str]
    event_type: str
    payload: Dict[str, Any]
    actor: Optional[str]
    created_at: str


class TaskStorage:
    """SQLite-backed task repository scoped to a single persona."""

    def __init__(
        self,
        persona_id: str,
        base_dir: Optional[Path] = None,
        *,
        create_dir: bool = True,
    ) -> None:
        self.persona_id = persona_id
        home_dir = base_dir or (Path.home() / ".saiverse")
        personas_root = home_dir / "personas"
        if create_dir:
            personas_root.mkdir(parents=True, exist_ok=True)
        persona_root = personas_root / persona_id
        if create_dir:
            persona_root.mkdir(parents=True, exist_ok=True)

        self.db_path = persona_root / "tasks.db"
        self._conn = sqlite3.connect(
            self.db_path,
            timeout=10.0,
            isolation_level=None,  # autocommit
            detect_types=sqlite3.PARSE_DECLTYPES,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self._apply_migrations()

    # --------------------------------------------------------------------- #
    # public helpers
    # --------------------------------------------------------------------- #
    def close(self) -> None:
        self._conn.close()

    def create_task(
        self,
        *,
        title: str,
        goal: str,
        summary: str,
        notes: Optional[str],
        steps: Sequence[Dict[str, Any]],
        priority: str = DEFAULT_PRIORITY,
        origin: str = "auto",
        due_at: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> TaskRecord:
        task_id = uuid.uuid4().hex
        now = _utc_now()
        activate_new = self._conn.execute(
            "SELECT 1 FROM tasks WHERE persona_id = ? AND status = 'active' LIMIT 1",
            (self.persona_id,),
        ).fetchone() is None

        with self._conn:
            self._conn.execute(
                """
                INSERT INTO tasks (
                    id, persona_id, title, goal, summary, notes, status,
                    priority, origin, active_step_id, due_at,
                    created_at, updated_at, completed_at, version, last_actor
                )
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, NULL, ?, ?, ?, NULL, 0, ?)
                """,
                (
                    task_id,
                    self.persona_id,
                    title,
                    goal,
                    summary,
                    notes,
                    priority,
                    origin,
                    due_at,
                    now,
                    now,
                    actor,
                ),
            )

            step_records: List[TaskStepRecord] = []
            for position, step in enumerate(steps, start=1):
                step_id = uuid.uuid4().hex
                step_title = step.get("title") or step.get("summary") or f"Step {position}"
                description = step.get("description")
                status = step.get("status", "pending")
                step_note = step.get("notes")
                self._conn.execute(
                    """
                    INSERT INTO task_steps (
                        id, task_id, position, title, description, status,
                        notes, created_at, updated_at, completed_at, version
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0)
                    """,
                    (
                        step_id,
                        task_id,
                        position,
                        step_title,
                        description,
                        status,
                        step_note,
                        now,
                        now,
                    ),
                )
                step_records.append(
                    TaskStepRecord(
                        id=step_id,
                        task_id=task_id,
                        position=position,
                        title=step_title,
                        description=description,
                        status=status,
                        notes=step_note,
                        created_at=now,
                        updated_at=now,
                        completed_at=None,
                        version=0,
                    )
                )

            self._insert_history(
                task_id=task_id,
                step_id=None,
                event_type="create_task",
                payload={
                    "title": title,
                    "goal": goal,
                    "summary": summary,
                    "priority": priority,
                    "origin": origin,
                    "steps": [
                        {
                            "title": step.title,
                            "description": step.description,
                            "status": step.status,
                        }
                        for step in step_records
                    ],
                },
                actor=actor,
            )

        if activate_new:
            try:
                self.set_active_task(task_id, actor=actor)
                next_step = next(
                    (step.id for step in step_records if step.status not in {"completed", "skipped"}),
                    None,
                )
                self.set_active_step(task_id, step_id=next_step, actor=actor)
            except TaskConflictError:
                pass

        return self.get_task(task_id)

    def list_tasks(
        self,
        *,
        statuses: Optional[Sequence[str]] = None,
        limit: Optional[int] = None,
        include_steps: bool = True,
    ) -> List[TaskRecord]:
        clauses: List[str] = ["persona_id = ?"]
        params: List[Any] = [self.persona_id]
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)
        where = " AND ".join(clauses)
        limit_clause = f" LIMIT {int(limit)}" if limit else ""
        rows = self._conn.execute(
            f"""
            SELECT *
            FROM tasks
            WHERE {where}
            ORDER BY updated_at DESC
            {limit_clause}
            """,
            params,
        ).fetchall()

        if not include_steps:
            return [
                TaskRecord(
                    id=row["id"],
                    title=row["title"],
                    goal=row["goal"],
                    summary=row["summary"],
                    notes=row["notes"],
                    status=row["status"],
                    priority=row["priority"],
                    origin=row["origin"],
                    active_step_id=row["active_step_id"],
                    due_at=row["due_at"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    completed_at=row["completed_at"],
                    version=row["version"],
                    last_actor=row["last_actor"],
                    steps=[],
                )
                for row in rows
            ]

        return [self.get_task(row["id"]) for row in rows]

    def get_task(self, task_id: str) -> TaskRecord:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE id = ? AND persona_id = ?",
            (task_id, self.persona_id),
        ).fetchone()
        if row is None:
            raise TaskNotFoundError(f"Task {task_id} not found for persona {self.persona_id}")

        step_rows = self._conn.execute(
            """
            SELECT *
            FROM task_steps
            WHERE task_id = ?
            ORDER BY position ASC
            """,
            (task_id,),
        ).fetchall()

        steps = [
            TaskStepRecord(
                id=step_row["id"],
                task_id=step_row["task_id"],
                position=step_row["position"],
                title=step_row["title"],
                description=step_row["description"],
                status=step_row["status"],
                notes=step_row["notes"],
                created_at=step_row["created_at"],
                updated_at=step_row["updated_at"],
                completed_at=step_row["completed_at"],
                version=step_row["version"],
            )
            for step_row in step_rows
        ]

        return TaskRecord(
            id=row["id"],
            title=row["title"],
            goal=row["goal"],
            summary=row["summary"],
            notes=row["notes"],
            status=row["status"],
            priority=row["priority"],
            origin=row["origin"],
            active_step_id=row["active_step_id"],
            due_at=row["due_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
            version=row["version"],
            last_actor=row["last_actor"],
            steps=steps,
        )

    def update_task_status(
        self,
        task_id: str,
        *,
        status: TaskStatus,
        actor: Optional[str],
        reason: Optional[str] = None,
        expected_version: Optional[int] = None,
    ) -> TaskRecord:
        now = _utc_now()
        if expected_version is None:
            row = self._conn.execute(
                "SELECT version FROM tasks WHERE id = ? AND persona_id = ?",
                (task_id, self.persona_id),
            ).fetchone()
            if row is None:
                raise TaskNotFoundError(f"Task {task_id} not found for persona {self.persona_id}")
            version = row["version"]
        else:
            exists = self._conn.execute(
                "SELECT 1 FROM tasks WHERE id = ? AND persona_id = ?",
                (task_id, self.persona_id),
            ).fetchone()
            if exists is None:
                raise TaskNotFoundError(f"Task {task_id} not found for persona {self.persona_id}")
            version = expected_version
        completed_at = now if status in {"completed", "cancelled"} else None

        with self._conn:
            cur = self._conn.execute(
                """
                UPDATE tasks
                SET status = ?, updated_at = ?, completed_at = ?, last_actor = ?, version = version + 1
                WHERE id = ? AND persona_id = ? AND version = ?
                """,
                (
                    status,
                    now,
                    completed_at,
                    actor,
                    task_id,
                    self.persona_id,
                    version,
                ),
            )
            if cur.rowcount == 0:
                raise TaskConflictError(f"Task {task_id} update conflict")

            self._insert_history(
                task_id=task_id,
                step_id=None,
                event_type="update_task_status",
                payload={"status": status, "reason": reason},
                actor=actor,
            )

        return self.get_task(task_id)

    def set_active_task(self, task_id: str, *, actor: Optional[str]) -> TaskRecord:
        now = _utc_now()
        with self._conn:
            # Pause any currently active tasks
            self._conn.execute(
                """
                UPDATE tasks
                SET status = 'paused', updated_at = ?, version = version + 1, last_actor = ?
                WHERE persona_id = ? AND status = 'active'
                """,
                (now, actor, self.persona_id),
            )

            row = self._conn.execute(
                "SELECT version FROM tasks WHERE id = ? AND persona_id = ?",
                (task_id, self.persona_id),
            ).fetchone()
            if row is None:
                raise TaskNotFoundError(f"Task {task_id} not found for persona {self.persona_id}")
            version = row["version"]
            cur = self._conn.execute(
                """
                UPDATE tasks
                SET status = 'active', updated_at = ?, last_actor = ?, version = version + 1
                WHERE id = ? AND persona_id = ? AND version = ?
                """,
                (now, actor, task_id, self.persona_id, version),
            )
            if cur.rowcount == 0:
                raise TaskConflictError(f"Task {task_id} activation conflict")

            self._insert_history(
                task_id=task_id,
                step_id=None,
                event_type="set_active_task",
                payload={},
                actor=actor,
            )

        return self.get_task(task_id)

    def update_step_status(
        self,
        step_id: str,
        *,
        status: TaskStepStatus,
        actor: Optional[str],
        notes: Optional[str] = None,
    ) -> TaskRecord:
        now = _utc_now()
        step_row = self._conn.execute(
            """
            SELECT ts.task_id, ts.version
            FROM task_steps ts
            JOIN tasks t ON t.id = ts.task_id
            WHERE ts.id = ? AND t.persona_id = ?
            """,
            (step_id, self.persona_id),
        ).fetchone()
        if step_row is None:
            raise TaskNotFoundError(f"Step {step_id} not found for persona {self.persona_id}")
        task_id = step_row["task_id"]
        version = step_row["version"]
        completed_at = now if status == "completed" else None

        with self._conn:
            cur = self._conn.execute(
                """
                UPDATE task_steps
                SET status = ?, notes = ?, updated_at = ?, completed_at = ?, version = version + 1
                WHERE id = ? AND version = ?
                """,
                (status, notes, now, completed_at, step_id, version),
            )
            if cur.rowcount == 0:
                raise TaskConflictError(f"Step {step_id} update conflict")

            self._conn.execute(
                """
                UPDATE tasks
                SET updated_at = ?, last_actor = ?, version = version + 1
                WHERE id = ?
                """,
                (now, actor, task_id),
            )

            self._insert_history(
                task_id=task_id,
                step_id=step_id,
                event_type="update_step_status",
                payload={"status": status, "notes": notes},
                actor=actor,
            )

        return self.get_task(task_id)

    def set_active_step(
        self,
        task_id: str,
        *,
        step_id: Optional[str],
        actor: Optional[str],
    ) -> TaskRecord:
        now = _utc_now()
        row = self._conn.execute(
            "SELECT version FROM tasks WHERE id = ? AND persona_id = ?",
            (task_id, self.persona_id),
        ).fetchone()
        if row is None:
            raise TaskNotFoundError(f"Task {task_id} not found for persona {self.persona_id}")
        version = row["version"]
        with self._conn:
            cur = self._conn.execute(
                """
                UPDATE tasks
                SET active_step_id = ?, updated_at = ?, last_actor = ?, version = version + 1
                WHERE id = ? AND persona_id = ? AND version = ?
                """,
                (step_id, now, actor, task_id, self.persona_id, version),
            )
            if cur.rowcount == 0:
                raise TaskConflictError(f"Task {task_id} active_step conflict")
            self._insert_history(
                task_id=task_id,
                step_id=step_id,
                event_type="set_active_step",
                payload={"active_step_id": step_id},
                actor=actor,
            )
        return self.get_task(task_id)

    def append_history(
        self,
        *,
        task_id: str,
        event_type: str,
        payload: Dict[str, Any],
        actor: Optional[str],
        step_id: Optional[str] = None,
    ) -> None:
        self._insert_history(
            task_id=task_id,
            step_id=step_id,
            event_type=event_type,
            payload=payload,
            actor=actor,
        )

    def fetch_history(
        self,
        task_id: str,
        *,
        limit: Optional[int] = None,
    ) -> List[TaskHistoryEntry]:
        rows = self._conn.execute(
            f"""
            SELECT * FROM task_history
            WHERE task_id = ?
            ORDER BY created_at DESC
            {f"LIMIT {int(limit)}" if limit else ""}
            """,
            (task_id,),
        ).fetchall()
        return [
            TaskHistoryEntry(
                id=row["id"],
                task_id=row["task_id"],
                step_id=row["step_id"],
                event_type=row["event_type"],
                payload=json.loads(row["payload"]) if row["payload"] else {},
                actor=row["actor"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------ #
    # internal helpers
    # ------------------------------------------------------------------ #
    def _configure(self) -> None:
        self._conn.execute("PRAGMA foreign_keys = ON;")
        self._conn.execute("PRAGMA journal_mode = WAL;")
        self._conn.execute("PRAGMA synchronous = NORMAL;")
        self._conn.execute("PRAGMA busy_timeout = 5000;")

    def _apply_migrations(self) -> None:
        user_version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if user_version == 0:
            self._create_schema()
            self._conn.execute("PRAGMA user_version = 1;")

    def _create_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                persona_id TEXT NOT NULL,
                title TEXT NOT NULL,
                goal TEXT NOT NULL,
                summary TEXT NOT NULL,
                notes TEXT,
                status TEXT NOT NULL,
                priority TEXT NOT NULL DEFAULT 'normal',
                origin TEXT NOT NULL DEFAULT 'auto',
                active_step_id TEXT,
                due_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                version INTEGER NOT NULL DEFAULT 0,
                last_actor TEXT
            );

            CREATE TABLE IF NOT EXISTS task_steps (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                status TEXT NOT NULL,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                version INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS task_history (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                step_id TEXT,
                event_type TEXT NOT NULL,
                payload TEXT,
                actor TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
                FOREIGN KEY (step_id) REFERENCES task_steps(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_tasks_persona_status
                ON tasks(persona_id, status);
            CREATE INDEX IF NOT EXISTS idx_tasks_updated_at
                ON tasks(persona_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_steps_task_position
                ON task_steps(task_id, position);
            CREATE INDEX IF NOT EXISTS idx_history_task_created
                ON task_history(task_id, created_at DESC);
            """
        )

    def _insert_history(
        self,
        *,
        task_id: str,
        step_id: Optional[str],
        event_type: str,
        payload: Dict[str, Any],
        actor: Optional[str],
    ) -> None:
        now = _utc_now()
        entry_id = uuid.uuid4().hex
        json_payload = json.dumps(payload, ensure_ascii=False) if payload else None
        self._conn.execute(
            """
            INSERT INTO task_history (id, task_id, step_id, event_type, payload, actor, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (entry_id, task_id, step_id, event_type, json_payload, actor, now),
        )
