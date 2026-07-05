from fastapi import APIRouter, Depends, HTTPException
from typing import List
from api.deps import get_manager
from .models import (
    TaskRecordModel, TaskStep, CreateTaskRequest, UpdateTaskStatusRequest
)

router = APIRouter()

@router.get("/{persona_id}/tasks", response_model=List[TaskRecordModel])
def list_tasks(persona_id: str, manager = Depends(get_manager)):
    """List all tasks for a persona (候補 / Track 小目標 / 単独 を所属ラベル付きで)。

    旧実装は ``PersonaTaskStore`` 経由で Track 小目標を除外していたが、UI で全体を
    一望できるよう ``PersonaTaskManager`` 直叩きに切替え、所属 (parent_kind) と
    親ラベル (候補 / t:N {title} / 単独) を付けて返す (unified_task_model.md)。
    """
    from saiverse.persona_task_manager import PersonaTaskManager

    ptm = PersonaTaskManager(manager.SessionLocal)
    tasks = ptm.list_tasks(persona_id, include_steps=True)

    # Track 小目標は所属 Track の "t:N {title}" を解決 (batch、N+1 回避)。
    track_ids = {t.get("track_id") for t in tasks if t.get("track_id")}
    track_labels: dict = {}
    if track_ids:
        from database.models import ActionTrack
        db = manager.SessionLocal()
        try:
            for tr in db.query(ActionTrack).filter(ActionTrack.track_id.in_(track_ids)).all():
                label = f"track:{tr.short_id}" if tr.short_id is not None else tr.track_id[:8]
                if tr.title:
                    label = f"{label} {tr.title}"
                track_labels[tr.track_id] = label
        finally:
            db.close()

    def _parent_label(t: dict) -> str:
        pk = t.get("parent_kind")
        if pk == "note":
            return "候補"
        if pk == "track":
            return track_labels.get(t.get("track_id"), "Track")
        return "単独"

    return [
        TaskRecordModel(
            id=t["id"],
            title=t["title"],
            goal=t.get("goal") or "",
            summary=t.get("summary") or "",
            status=t["status"],
            priority=t.get("priority") or "normal",
            active_step_id=t.get("active_step_id"),
            updated_at=t.get("updated_at") or "",
            parent_kind=t.get("parent_kind"),
            task_ref=t.get("task_ref"),
            parent_label=_parent_label(t),
            steps=[
                TaskStep(
                    id=s["id"],
                    position=s["position"],
                    title=s["title"],
                    description=s.get("description"),
                    status=s["status"],
                    notes=s.get("notes"),
                    updated_at=s.get("updated_at") or "",
                )
                for s in t.get("steps", [])
            ],
        )
        for t in tasks
    ]

@router.post("/{persona_id}/tasks")
def create_task(persona_id: str, req: CreateTaskRequest, manager = Depends(get_manager)):
    """Create a new task."""
    from persona.tasks import PersonaTaskStore
    storage = PersonaTaskStore(persona_id)
    try:
        task = storage.create_task(
            title=req.title,
            goal=req.goal,
            summary=req.summary,
            notes=req.notes,
            steps=req.steps,
            priority=req.priority,
            origin="manual"
        )
        return {"success": True, "task_id": task.id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        storage.close()

@router.patch("/{persona_id}/tasks/{task_id}")
def update_task_status(
    persona_id: str, 
    task_id: str, 
    req: UpdateTaskStatusRequest, 
    manager = Depends(get_manager)
):
    """Update task status."""
    from persona.tasks import PersonaTaskStore, TaskNotFoundError
    storage = PersonaTaskStore(persona_id)
    try:
        storage.update_task_status(task_id, status=req.status, actor="user", reason=req.reason)
        return {"success": True}
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail="Task not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        storage.close()

@router.get("/{persona_id}/tasks/{task_id}/history")
def get_task_history(persona_id: str, task_id: str, manager = Depends(get_manager)):
    """Get history for a specific task."""
    from persona.tasks import PersonaTaskStore
    storage = PersonaTaskStore(persona_id)
    try:
        history = storage.fetch_history(task_id, limit=50) # Limit to 50 for now
        return [
            {
                "id": h.id,
                "event_type": h.event_type,
                "payload": h.payload,
                "actor": h.actor,
                "created_at": h.created_at
            }
            for h in history
        ]
    finally:
        storage.close()
