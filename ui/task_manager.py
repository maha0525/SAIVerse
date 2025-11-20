from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Tuple

import gradio as gr
import pandas as pd

from persona.tasks.storage import TaskHistoryEntry, TaskRecord, TaskStorage


def create_task_manager_ui(manager) -> None:
    personas = manager.personas
    persona_choices: List[Tuple[str, str]] = []
    for pid, persona in personas.items():
        label = f"{persona.persona_name} ({pid})"
        persona_choices.append((label, pid))

    initial_label = persona_choices[0][0] if persona_choices else None
    persona_dropdown = gr.Dropdown(
        choices=[label for label, _ in persona_choices],
        label="ペルソナ選択",
        interactive=True,
        value=initial_label,
    )

    def _resolve_persona_id(label: str) -> str:
        for display, pid in persona_choices:
            if display == label:
                return pid
        return label

    def load_tables(selected_label: str):
        if not selected_label:
            empty = pd.DataFrame()
            return empty, empty, empty
        persona_id = _resolve_persona_id(selected_label)
        base_dir = manager.saiverse_home
        storage = TaskStorage(persona_id, base_dir=base_dir)
        try:
            task_records = storage.list_tasks(include_steps=True)
            history_rows: List[TaskHistoryEntry] = []
            for task in task_records:
                history_rows.extend(storage.fetch_history(task.id, limit=5))
        finally:
            storage.close()

        tasks_df = pd.DataFrame(_serialise_tasks(task_records)) if task_records else pd.DataFrame()
        steps_df = pd.DataFrame(_serialise_steps(task_records)) if task_records else pd.DataFrame()
        history_df = pd.DataFrame(_serialise_history(history_rows)) if history_rows else pd.DataFrame()

        return tasks_df, steps_df, history_df

    initial_tables = load_tables(initial_label) if initial_label else (pd.DataFrame(),) * 3

    tasks_table = gr.DataFrame(label="タスク一覧", interactive=False, value=initial_tables[0])
    steps_table = gr.DataFrame(label="ステップ一覧", interactive=False, value=initial_tables[1])
    history_table = gr.DataFrame(label="履歴 (最新20件)", interactive=False, value=initial_tables[2])
    refresh_button = gr.Button("再読み込み")

    persona_dropdown.change(load_tables, inputs=persona_dropdown, outputs=[tasks_table, steps_table, history_table])
    refresh_button.click(load_tables, inputs=persona_dropdown, outputs=[tasks_table, steps_table, history_table])


def _serialise_tasks(records: List[TaskRecord]) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for task in records:
        rows.append(
            {
                "task_id": task.id,
                "title": task.title,
                "status": task.status,
                "priority": task.priority,
                "active_step_id": task.active_step_id,
                "updated_at": task.updated_at,
                "due_at": task.due_at,
            }
        )
    return rows


def _serialise_steps(records: List[TaskRecord]) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for task in records:
        for step in task.steps:
            rows.append(
                {
                    "task_id": task.id,
                    "step_id": step.id,
                    "position": step.position,
                    "title": step.title,
                    "status": step.status,
                    "updated_at": step.updated_at,
                }
            )
    return rows


def _serialise_history(entries: List[TaskHistoryEntry]) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for entry in entries:
        rows.append(
            {
                "task_id": entry.task_id,
                "step_id": entry.step_id,
                "event_type": entry.event_type,
                "actor": entry.actor,
                "created_at": entry.created_at,
                "payload": json.dumps(entry.payload, ensure_ascii=False),
            }
        )
    return rows
