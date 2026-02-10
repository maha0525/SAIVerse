from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from llm_clients import get_llm_client
from saiverse.model_configs import get_context_length, get_model_provider
from persona.tasks.storage import TaskStorage

DEFAULT_MODEL = "gemini-2.5-flash"
MODEL_ENV = "SAIVERSE_TASK_CREATION_MODEL"
USE_LLM_ENV = "SAIVERSE_TASK_CREATION_USE_LLM"


@dataclass
class TaskRequest:
    request_id: str
    summary: str
    context: Optional[str]
    priority: str
    persona_id: str
    created_at: str


class TaskCreationProcessor:
    def __init__(self, persona_dir: Path, *, use_llm: Optional[bool] = None) -> None:
        self.persona_dir = persona_dir
        self.persona_id = persona_dir.name
        flag = use_llm
        if flag is None:
            env = os.getenv(USE_LLM_ENV)
            if env is not None:
                flag = env.lower() not in {"0", "false", "no"}
            else:
                flag = True
        self.use_llm = flag
        self.model_name = os.getenv(MODEL_ENV, DEFAULT_MODEL)

    def process_pending_requests(self) -> List[str]:
        pending_file = self.persona_dir / "task_requests.jsonl"
        if not pending_file.exists():
            return []
        requests = list(self._read_requests(pending_file))
        if not requests:
            return []

        storage = TaskStorage(self.persona_id, base_dir=self.persona_dir.parent.parent)
        processed_ids: List[str] = []
        try:
            for request in requests:
                try:
                    definition = self._generate_definition(request)
                    storage.create_task(
                        title=definition["title"],
                        goal=definition["goal"],
                        summary=definition["summary"],
                        notes=definition.get("notes"),
                        steps=definition["steps"],
                        priority=request.priority,
                        origin="auto",
                        actor=self.persona_id,
                    )
                    processed_ids.append(request.request_id)
                    self._append_processed_record(request, definition)
                except Exception as exc:  # pragma: no cover - logged for later inspection
                    logging.exception("Failed to create task for request %s: %s", request.request_id, exc)
        finally:
            storage.close()

        if processed_ids:
            remaining = [req for req in requests if req.request_id not in processed_ids]
            self._write_requests(pending_file, remaining)
        return processed_ids

    def _generate_definition(self, request: TaskRequest) -> Dict[str, Any]:
        if self.use_llm:
            try:
                provider = get_model_provider(self.model_name)
                context_length = get_context_length(self.model_name)
                client = get_llm_client(self.model_name, provider, context_length)
                prompt = self._build_prompt(request)
                response = client.generate([
                    {"role": "system", "content": "You create structured TODO tasks for an autonomous persona."},
                    {"role": "user", "content": prompt},
                ])
                definition = self._parse_definition(response)
                return definition
            except Exception as exc:
                logging.warning("Task creation via LLM failed (%s); falling back to heuristic.", exc)
        return self._heuristic_definition(request)

    def _build_prompt(self, request: TaskRequest) -> str:
        context_block = f"\nコンテキスト:\n{request.context}" if request.context else ""
        return (
            "以下の要求を完遂するためのタスクを1件作成してください。\n"
            "JSONのみで出力し、キーは goal, summary, title, notes, steps の5つにしてください。\n"
            "steps は配列で、各要素が {\"title\": ..., \"description\": ...} のオブジェクトです。\n"
            f"要求概要: {request.summary}{context_block}"
        )

    def _parse_definition(self, response: str) -> Dict[str, Any]:
        try:
            start = response.find("{")
            end = response.rfind("}")
            if start == -1 or end == -1:
                raise ValueError("No JSON object found in response")
            payload = json.loads(response[start : end + 1])
            steps = payload.get("steps") or []
            if not isinstance(steps, list) or not steps:
                steps = self._default_steps(payload.get("title") or payload.get("goal", "タスク"))
            definition = {
                "title": payload.get("title") or payload.get("goal") or payload.get("summary") or "自律タスク",
                "goal": payload.get("goal") or payload.get("summary") or payload.get("title"),
                "summary": payload.get("summary") or payload.get("goal") or payload.get("title"),
                "notes": payload.get("notes"),
                "steps": [self._normalise_step(step, index) for index, step in enumerate(steps, start=1)],
            }
            return definition
        except Exception as exc:
            raise ValueError(f"Failed to parse task definition: {exc}") from exc

    def _heuristic_definition(self, request: TaskRequest) -> Dict[str, Any]:
        title = request.summary.strip()
        steps = self._default_steps(title)
        return {
            "title": title,
            "goal": title,
            "summary": title,
            "notes": request.context,
            "steps": steps,
        }

    def _default_steps(self, title: str) -> List[Dict[str, str]]:
        return [
            {"title": "計画立案", "description": f"{title} に必要な素材や要件を整理する"},
            {"title": "実作業", "description": f"{title} の主な作業を行う"},
            {"title": "仕上げ", "description": f"{title} の内容を確認し仕上げる"},
        ]

    def _normalise_step(self, step: Dict[str, any], index: int) -> Dict[str, str]:
        title = str(step.get("title") or step.get("summary") or f"Step {index}").strip()
        description = str(step.get("description") or step.get("detail") or title).strip()
        return {"title": title, "description": description}

    def _append_processed_record(self, request: TaskRequest, definition: Dict[str, any]) -> None:
        processed_file = self.persona_dir / "task_requests_processed.jsonl"
        record = {
            "request_id": request.request_id,
            "task_title": definition.get("title"),
            "created_task_goal": definition.get("goal"),
            "processed_at": request.created_at,
        }
        with processed_file.open("a", encoding="utf-8") as handler:
            handler.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _read_requests(self, path: Path) -> Iterable[TaskRequest]:
        with path.open("r", encoding="utf-8") as handler:
            for line in handler:
                if not line.strip():
                    continue
                data = json.loads(line)
                yield TaskRequest(
                    request_id=data["id"],
                    summary=data["summary"],
                    context=data.get("context"),
                    priority=data.get("priority", "normal"),
                    persona_id=data.get("persona_id", self.persona_id),
                    created_at=data.get("created_at", ""),
                )

    def _write_requests(self, path: Path, requests: List[TaskRequest]) -> None:
        if not requests:
            path.unlink(missing_ok=True)
            return
        with path.open("w", encoding="utf-8") as handler:
            for request in requests:
                handler.write(
                    json.dumps(
                        {
                            "id": request.request_id,
                            "summary": request.summary,
                            "context": request.context,
                            "priority": request.priority,
                            "persona_id": request.persona_id,
                            "created_at": request.created_at,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )


def process_all_personas(base_dir: Optional[Path] = None) -> Dict[str, List[str]]:
    base = base_dir or (Path.home() / ".saiverse" / "personas")
    results: Dict[str, List[str]] = {}
    if not base.exists():
        return results
    for persona_dir in base.iterdir():
        if not persona_dir.is_dir():
            continue
        processor = TaskCreationProcessor(persona_dir)
        processed = processor.process_pending_requests()
        if processed:
            results[persona_dir.name] = processed
    return results
