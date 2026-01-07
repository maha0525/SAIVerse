from fastapi import APIRouter, Depends, HTTPException
from typing import List
from api.deps import get_manager
from .models import (
    TaskRecordModel, TaskStep, CreateTaskRequest, UpdateTaskStatusRequest
)

router = APIRouter()

@router.get("/{persona_id}/tasks", response_model=List[TaskRecordModel])
def list_tasks(persona_id: str, manager = Depends(get_manager)):
    """List all tasks for a persona."""
    from persona.tasks.storage import TaskStorage
    base_dir = manager.saiverse_home
    storage = TaskStorage(persona_id, base_dir=base_dir)
    try:
        tasks = storage.list_tasks(include_steps=True)
        return [
            TaskRecordModel(
                id=t.id,
                title=t.title,
                goal=t.goal,
                summary=t.summary,
                status=t.status,
                priority=t.priority,
                active_step_id=t.active_step_id,
                updated_at=t.updated_at,
                steps=[
                    TaskStep(
                        id=s.id,
                        position=s.position,
                        title=s.title,
                        description=s.description,
                        status=s.status,
                        notes=s.notes,
                        updated_at=s.updated_at
                    )
                    for s in t.steps
                ]
            )
            for t in tasks
        ]
    finally:
        storage.close()

@router.post("/{persona_id}/tasks")
def create_task(persona_id: str, req: CreateTaskRequest, manager = Depends(get_manager)):
    """Create a new task."""
    from persona.tasks.storage import TaskStorage
    base_dir = manager.saiverse_home
    storage = TaskStorage(persona_id, base_dir=base_dir)
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
    from persona.tasks.storage import TaskStorage, TaskNotFoundError
    base_dir = manager.saiverse_home
    storage = TaskStorage(persona_id, base_dir=base_dir)
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
    from persona.tasks.storage import TaskStorage
    base_dir = manager.saiverse_home
    storage = TaskStorage(persona_id, base_dir=base_dir)
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
