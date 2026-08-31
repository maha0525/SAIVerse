"""PersonaTaskStore: 旧 ``TaskStorage`` 互換の persona バインドアダプタ。

Task は統合 ``persona_task`` テーブル (main DB) へ一本化された
(unified_task_model.md §5 step 4)。旧 standalone ``TaskStorage``
(per-persona tasks.db) を直接使っていた消費者 (API ルート / スペル /
get_task_summary / TaskCreationProcessor) を最小改変で載せ替えるため、
``TaskStorage`` と同じメソッド署名・同じ戻り値型 (``TaskRecord`` /
``TaskStepRecord`` / ``TaskHistoryEntry`` dataclass) を提供する薄いラッパ。

内部では ``PersonaTaskManager`` (main DB, dict 返し) に委譲し、戻り値の dict を
旧 dataclass に再構築する。これにより呼び出し側の属性アクセス
(``record.title`` 等) と FastAPI の直列化契約 (`TaskRecordModel`) が無改変で動く。

**standalone が見るのは「Track 非所属」のタスクのみ**: 統合テーブルには Track 内
小目標 (旧 track_task, ``parent_kind='track'``) も同居するため、standalone 系の
``list_tasks`` は ``parent_kind='track'`` を除外する (未所属 + 候補ノートのみ)。
さもないと Track チェックリスト項目が standalone タスク一覧に混入する。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence

from persona.tasks.storage import (
    DEFAULT_PRIORITY,
    TaskHistoryEntry,
    TaskNotFoundError,  # noqa: F401  (re-export for callers that import from here)
    TaskRecord,
    TaskStepRecord,
)
from saiverse.persona_task_manager import PARENT_TRACK, PersonaTaskManager


def _to_step_record(d: Dict[str, Any]) -> TaskStepRecord:
    return TaskStepRecord(
        id=d["id"],
        task_id=d["task_id"],
        position=d["position"],
        title=d["title"],
        description=d.get("description"),
        status=d["status"],
        notes=d.get("notes"),
        created_at=d.get("created_at"),
        updated_at=d.get("updated_at"),
        completed_at=d.get("completed_at"),
        version=d.get("version", 0),
    )


def _to_task_record(d: Dict[str, Any]) -> TaskRecord:
    return TaskRecord(
        id=d["id"],
        title=d["title"],
        goal=d.get("goal") or "",
        summary=d.get("summary") or "",
        notes=d.get("notes"),
        status=d["status"],
        priority=d.get("priority") or DEFAULT_PRIORITY,
        origin=d.get("origin") or "auto",
        active_step_id=d.get("active_step_id"),
        due_at=d.get("due_at"),
        created_at=d.get("created_at"),
        updated_at=d.get("updated_at"),
        completed_at=d.get("completed_at"),
        version=d.get("version", 0),
        last_actor=d.get("last_actor"),
        steps=[_to_step_record(s) for s in d.get("steps", [])],
    )


def _coerce_due_at(value: Any) -> Optional[datetime]:
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", ""))
        except ValueError:
            return None
    return None


class PersonaTaskStore:
    """``TaskStorage`` 互換 API を持つ persona バインドアダプタ (main DB 委譲)。"""

    def __init__(
        self,
        persona_id: str,
        *,
        session_factory: Optional[Callable[[], Any]] = None,
        base_dir: Any = None,  # 旧 TaskStorage 互換の no-op 引数 (main DB なので未使用)
    ) -> None:
        self.persona_id = persona_id
        if session_factory is None:
            # 既定はアプリのメイン DB セッション (track 系と同じ canonical な main DB)。
            from database.session import SessionLocal
            session_factory = SessionLocal
        self._manager = PersonaTaskManager(session_factory)

    # ------------------------------------------------------------------ #
    # TaskStorage 互換メソッド
    # ------------------------------------------------------------------ #

    def close(self) -> None:
        """main DB はセッションを呼び出しごとに開閉するので no-op (互換用)。"""

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
        due_at: Optional[Any] = None,
        actor: Optional[str] = None,
    ) -> TaskRecord:
        d = self._manager.create_task(
            persona_id=self.persona_id,
            title=title,
            goal=goal,
            summary=summary,
            notes=notes,
            steps=steps,
            priority=priority,
            origin=origin,
            due_at=_coerce_due_at(due_at),
            actor=actor,
        )
        return _to_task_record(d)

    def list_tasks(
        self,
        *,
        statuses: Optional[Sequence[str]] = None,
        limit: Optional[int] = None,
        include_steps: bool = True,
    ) -> List[TaskRecord]:
        # Track 内小目標 (parent_kind='track') を除外。limit はフィルタ後に適用するため
        # ここでは渡さない (タスクプールは小規模なので全件取得して絞る)。
        rows = self._manager.list_tasks(
            self.persona_id,
            statuses=statuses,
            include_steps=include_steps,
        )
        filtered = [r for r in rows if r.get("parent_kind") != PARENT_TRACK]
        if limit:
            filtered = filtered[: int(limit)]
        return [_to_task_record(r) for r in filtered]

    def get_task(self, task_id: str) -> TaskRecord:
        return _to_task_record(self._manager.get_task(task_id, persona_id=self.persona_id))

    def set_active_task(self, task_id: str, *, actor: Optional[str]) -> TaskRecord:
        return _to_task_record(
            self._manager.set_active_task(task_id, persona_id=self.persona_id, actor=actor)
        )

    def set_active_step(
        self, task_id: str, *, step_id: Optional[str], actor: Optional[str]
    ) -> TaskRecord:
        return _to_task_record(
            self._manager.set_active_step(
                task_id, step_id=step_id, persona_id=self.persona_id, actor=actor
            )
        )

    def update_task_status(
        self,
        task_id: str,
        *,
        status: str,
        actor: Optional[str],
        reason: Optional[str] = None,
        expected_version: Optional[int] = None,
    ) -> TaskRecord:
        return _to_task_record(
            self._manager.update_task_status(
                task_id,
                status=status,
                actor=actor,
                persona_id=self.persona_id,
                reason=reason,
                expected_version=expected_version,
            )
        )

    def update_step_status(
        self,
        step_id: str,
        *,
        status: str,
        actor: Optional[str],
        notes: Optional[str] = None,
    ) -> TaskRecord:
        return _to_task_record(
            self._manager.update_step_status(
                step_id, status=status, actor=actor, notes=notes
            )
        )

    def fetch_history(self, task_id: str, *, limit: Optional[int] = None) -> List[TaskHistoryEntry]:
        return [
            TaskHistoryEntry(
                id=h["id"],
                task_id=h["task_id"],
                step_id=h.get("step_id"),
                event_type=h["event_type"],
                payload=h.get("payload") or {},
                actor=h.get("actor"),
                created_at=h.get("created_at"),
            )
            for h in self._manager.fetch_history(task_id, limit=limit)
        ]
