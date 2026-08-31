"""Task の値型 (dataclass / 例外 / 定数)。

Task は統合 ``persona_task`` テーブル (main DB) へ一本化された
(unified_task_model.md §5 step 6, 2026-06-28)。旧 per-persona tasks.db の
``TaskStorage`` クラスは廃止され、本モジュールには ``PersonaTaskStore`` /
``PersonaTaskManager`` および API ルートが共有する**値型のみ**が残る。

これらの dataclass は ``PersonaTaskStore`` (persona/tasks/store.py) が
``PersonaTaskManager`` の dict 戻り値を旧 ``TaskStorage`` 互換の型へ再構築する
ために使う (= FastAPI 直列化契約と呼び出し側の属性アクセスを無改変に保つ)。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

DEFAULT_PRIORITY = "normal"

TaskStatus = str
TaskStepStatus = str


class TaskStorageError(RuntimeError):
    """Base exception for task storage failures."""


class TaskConflictError(TaskStorageError):
    """Raised when an update fails due to optimistic locking conflicts."""


class TaskNotFoundError(TaskStorageError):
    """Raised when the requested task or step cannot be located."""


@dataclass(frozen=True)
class TaskStepRecord:
    id: str
    task_id: str
    position: int
    title: str
    description: Optional[str]
    status: TaskStepStatus
    notes: Optional[str]
    created_at: Optional[str]
    updated_at: Optional[str]
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
    created_at: Optional[str]
    updated_at: Optional[str]
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
    created_at: Optional[str]
