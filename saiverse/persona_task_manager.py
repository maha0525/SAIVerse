"""PersonaTaskManager: 統合 Task のライフサイクル管理 (main DB 版)。

旧 track_task (ActionTrack.tasks_json 軽量チェックリスト) と旧 standalone Task
(per-persona tasks.db のリッチタスク) を 1 枚の ``persona_task`` テーブルに統合した
上での CRUD + 状態遷移を担う純粋ロジックレイヤー。

責務:
- Task の作成 / 取得 / 一覧 (親バインドでフィルタ)
- 状態遷移 (active/paused/completed/cancelled) + ステップ更新
- 親バインド: 候補 (note_id) / Track 内小目標 (track_id) / 未所属
- 昇格 (候補 → Track) = 親を note_id → track_id に張り替え (コピー/破棄しない)
- 旧 track_task 互換層 (get_track_tasks / add_track_task / complete_track_task /
  format_track_task_list) — 既存の Track チェックリスト呼び出しを温存する

責務外:
- スペル登録 (builtin_data/tools 配下で別途)
- Note との連携配線 (NoteManager / head セクションで扱う)

設計: docs/intent/persona_cognition/unified_task_model.md
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence

from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from database.models import PersonaTask, PersonaTaskHistory, PersonaTaskStep
from saiverse import clock

LOGGER = logging.getLogger(__name__)

DEFAULT_PRIORITY = "normal"

# task:N 短縮参照子 (Track の t:N と対称、名前空間のみ別)。
_TASK_REF_RE = re.compile(r"^task:(\d+)$")

# --- Task 状態定数 (standalone 踏襲) ---
STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"

TERMINAL_TASK_STATUSES = frozenset({STATUS_COMPLETED, STATUS_CANCELLED})

# --- 親バインド種別 ---
PARENT_NOTE = "note"    # 候補 (desire ノート内のやりたいこと)
PARENT_TRACK = "track"  # Track 内の実行小目標 (旧 track_task)

# --- 欲求の帳簿: desire_state 値 (自律行動 v2 §5.3。運用は saiverse/desire_engine.py) ---
DESIRE_STATE_FRESH = "fresh"      # 新鮮 (既定。NULL も fresh 扱い)
DESIRE_STATE_FADING = "fading"    # 薄れつつある (放置 or 就寝レビューの fading 裁定)
DESIRE_STATE_EXPIRED = "expired"  # 期限切れ (status=cancelled の論理アーカイブと対で付く)

# --- 目的ノードの段階 (stage; life_concept_map.md §3.1「種・段階・位置」) ---
# desire と task は同一実体の段階違い (採用の前後で呼び名が変わるだけ)。
# 物理カラム persona_task.stage が NULL の既存行は derive_stage() で決定論導出する。
STAGE_CANDIDATE = "candidate"  # 候補 (未採用 = 従来の desire)
STAGE_ADOPTED = "adopted"      # 採用済 (木の中 = 従来の task)
STAGE_DORMANT = "dormant"      # 休眠 (薄れても消滅でなく休眠 §5。欲求の期限切れが対応)
STAGE_COMPLETED = "completed"  # 完了
STAGE_ABORTED = "aborted"      # 中止

# --- ノード種別 (nature; life_concept_map.md §3 の大枝二種。将来用・値は models.py 参照) ---
NATURE_PRACTICE = "practice"  # 営み (終わらない)
NATURE_VENTURE = "venture"    # 企て (全完了で終わる)


def derive_stage(
    status: Optional[str],
    parent_kind: Optional[str],
    desire_state: Optional[str],
) -> str:
    """stage カラムが NULL の既存行から段階を決定論導出する (後方互換の既定規則)。

    life_concept_map.md §3.1 / §10.1「desire の実装 → 目的ノード stage=候補へ正規化」
    の読み出し側。既存カラムだけから一意に決まる:

    - status=completed → completed
    - status=cancelled かつ desire の期限切れ (desire_state='expired') → dormant
      (§5「薄れても消滅でなく休眠」— desire_engine の論理アーカイブが対応)
    - status=cancelled (その他) → aborted
    - 生きている行: parent_kind='note' (desire ノートの候補) → candidate、
      それ以外 (track 内小目標・未所属) → adopted
    """
    if status == STATUS_COMPLETED:
        return STAGE_COMPLETED
    if status == STATUS_CANCELLED:
        if parent_kind == PARENT_NOTE and desire_state == DESIRE_STATE_EXPIRED:
            return STAGE_DORMANT
        return STAGE_ABORTED
    if parent_kind == PARENT_NOTE:
        return STAGE_CANDIDATE
    return STAGE_ADOPTED


class TaskError(Exception):
    """Base error for PersonaTaskManager."""


class TaskNotFoundError(TaskError):
    """Raised when a task or step cannot be located."""


class TaskConflictError(TaskError):
    """Raised when an optimistic-locking update conflicts."""


def _now() -> datetime:
    # naive datetime は main DB の他テーブル (ActionTrack 等) と同じ慣習。
    # ``saiverse.clock.now()`` は実モードでは ``datetime.now()`` と同一で、
    # 一日シミュレータの仮想時刻を尊重する (autonomous_behavior_v2.md §12 の不変条件)。
    return clock.now()


class PersonaTaskManager:
    """``persona_task`` の永続化と状態遷移を担う。

    全メソッドは 1 トランザクション内で完結する (内部で SessionLocal を開閉する)。
    返り値は detached な dict (ORM オブジェクトを外に出さない — JSON 化しやすく、
    セッションクローズ後の遅延ロード事故を防ぐ)。
    """

    def __init__(self, session_factory: Callable[[], Session]):
        self.SessionLocal = session_factory

    # ------------------------------------------------------------------
    # 直列化ヘルパ
    # ------------------------------------------------------------------

    @staticmethod
    def _step_to_dict(step: PersonaTaskStep) -> Dict[str, Any]:
        return {
            "id": step.id,
            "task_id": step.task_id,
            "position": step.position,
            "title": step.title,
            "description": step.description,
            "status": step.status,
            "notes": step.notes,
            "created_at": step.created_at.isoformat() if step.created_at else None,
            "updated_at": step.updated_at.isoformat() if step.updated_at else None,
            "completed_at": step.completed_at.isoformat() if step.completed_at else None,
            "version": step.version,
        }

    @classmethod
    def _task_to_dict(
        cls, task: PersonaTask, steps: Sequence[PersonaTaskStep]
    ) -> Dict[str, Any]:
        return {
            "id": task.id,
            "short_id": task.short_id,
            "task_ref": f"task:{task.short_id}" if task.short_id is not None else None,
            "persona_id": task.persona_id,
            "parent_kind": task.parent_kind,
            "note_id": task.note_id,
            "track_id": task.track_id,
            "title": task.title,
            "goal": task.goal,
            "summary": task.summary,
            "notes": task.notes,
            "status": task.status,
            "priority": task.priority,
            "origin": task.origin,
            "active_step_id": task.active_step_id,
            "due_at": task.due_at.isoformat() if task.due_at else None,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "updated_at": task.updated_at.isoformat() if task.updated_at else None,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            "version": task.version,
            "last_actor": task.last_actor,
            # 欲求の帳簿 (desire ledger; parent_kind='note' の候補行でのみ意味を持つ)
            "desire_type": task.desire_type,
            "desire_source": task.desire_source,
            "desire_state": task.desire_state,
            "last_touched_at": task.last_touched_at.isoformat() if task.last_touched_at else None,
            "touch_count": task.touch_count,
            # 成果物参照 (judgment_points.md §6 の接地の証跡)。JSON 配列を list に展開。
            "artifact_refs": cls._parse_artifact_refs(task.artifact_refs),
            # 目的ノードの段階 (life_concept_map.md §3.1)。物理カラムが NULL の
            # 既存行は derive_stage() の既定規則で埋めて返す (= 実効値を見せる)。
            "stage": task.stage or derive_stage(
                task.status, task.parent_kind, task.desire_state
            ),
            "nature": task.nature,
            # 昇格・命名の来歴 (JSON 配列を list に展開。artifact_refs と同形式)。
            "promoted_from": cls._parse_artifact_refs(task.promoted_from),
            "steps": [cls._step_to_dict(s) for s in steps],
        }

    @staticmethod
    def _parse_artifact_refs(raw: Optional[str]) -> List[str]:
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            LOGGER.warning("[task] artifact_refs is not valid JSON: %r", raw)
            return []
        if not isinstance(parsed, list):
            return []
        return [str(r) for r in parsed]

    # ------------------------------------------------------------------
    # 内部: 取得
    # ------------------------------------------------------------------

    @staticmethod
    def _next_short_id(db: Session, persona_id: str) -> int:
        """persona 内で次に使う short_id を返す (MAX + 1, 初回は 1)。

        **不変条件**: タスク行は物理削除しない (掃除は status での論理削除) ため、
        MAX(short_id) は単調増加し、番号は二度と再利用されない (Track と対称)。
        """
        current_max = (
            db.query(sa_func.max(PersonaTask.short_id))
            .filter(PersonaTask.persona_id == persona_id)
            .scalar()
        )
        return (current_max or 0) + 1

    def resolve_task_ref(self, persona_id: str, ref: str) -> str:
        """短縮参照 (task:N) または UUID hex を task id に解決する。

        - ``task:5`` → persona_id のタスクで short_id=5 の id を返す
        - UUID hex (32 文字, ハイフンなし) → そのまま返す
        - それ以外 → TaskNotFoundError
        """
        if not ref:
            raise TaskNotFoundError("empty task reference")
        m = _TASK_REF_RE.match(ref.strip())
        if m:
            short_id = int(m.group(1))
            db = self.SessionLocal()
            try:
                row = (
                    db.query(PersonaTask.id)
                    .filter(
                        PersonaTask.persona_id == persona_id,
                        PersonaTask.short_id == short_id,
                    )
                    .first()
                )
                if row is None:
                    raise TaskNotFoundError(
                        f"task not found: task:{short_id} (persona={persona_id})"
                    )
                return row[0]
            finally:
                db.close()
        ref_stripped = ref.strip()
        if len(ref_stripped) == 32 and "-" not in ref_stripped:
            return ref_stripped
        raise TaskNotFoundError(
            f"invalid task reference: {ref!r} (expected 'task:N' or UUID hex)"
        )

    @staticmethod
    def _fetch_task_or_raise(db: Session, task_id: str, persona_id: Optional[str]) -> PersonaTask:
        q = db.query(PersonaTask).filter(PersonaTask.id == task_id)
        if persona_id is not None:
            q = q.filter(PersonaTask.persona_id == persona_id)
        task = q.first()
        if task is None:
            raise TaskNotFoundError(f"task not found: {task_id}")
        return task

    @staticmethod
    def _fetch_steps(db: Session, task_id: str) -> List[PersonaTaskStep]:
        return (
            db.query(PersonaTaskStep)
            .filter(PersonaTaskStep.task_id == task_id)
            .order_by(PersonaTaskStep.position.asc())
            .all()
        )

    def _insert_history(
        self,
        db: Session,
        *,
        task_id: str,
        step_id: Optional[str],
        event_type: str,
        payload: Dict[str, Any],
        actor: Optional[str],
    ) -> None:
        db.add(
            PersonaTaskHistory(
                id=uuid.uuid4().hex,
                task_id=task_id,
                step_id=step_id,
                event_type=event_type,
                payload=json.dumps(payload, ensure_ascii=False) if payload else None,
                actor=actor,
                created_at=_now(),
            )
        )

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create_task(
        self,
        *,
        persona_id: str,
        title: str,
        goal: str = "",
        summary: str = "",
        notes: Optional[str] = None,
        steps: Sequence[Dict[str, Any]] = (),
        priority: str = DEFAULT_PRIORITY,
        origin: str = "auto",
        due_at: Optional[datetime] = None,
        actor: Optional[str] = None,
        parent_kind: Optional[str] = None,
        note_id: Optional[str] = None,
        track_id: Optional[str] = None,
        auto_activate: bool = True,
        desire_type: Optional[str] = None,
        desire_source: Optional[str] = None,
        stage: Optional[str] = None,
        nature: Optional[str] = None,
        promoted_from: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        """新規 Task を作成する。

        ``parent_kind`` / ``note_id`` / ``track_id`` で所属を決める:
        - ``parent_kind='note'`` + ``note_id`` = 候補 (desire ノート内)
        - ``parent_kind='track'`` + ``track_id`` = Track 内小目標
        - いずれも省略 = 未所属

        ``auto_activate=True`` のとき、そのペルソナに active な Task が無ければ
        作成した Task を active にし最初の未完ステップを active_step にする。

        ``desire_type`` / ``desire_source`` は欲求の六型と接地参照 (自律行動 v2 §5)。
        候補 (parent_kind='note') では帳簿 (last_touched_at / touch_count /
        desire_state) も初期化する。値の検証は呼び出し側 (desire_add スペル等) の
        責務 — 本レイヤーは priority / origin と同じく永続化のみ担う。

        ``stage`` / ``nature`` / ``promoted_from`` は目的ノードの段階・種別・来歴
        (life_concept_map.md §3.1)。省略 (None) 時は物理カラムを NULL のままにし、
        読み出し側が derive_stage() の既定規則で埋める — 既存呼び出し元の挙動は
        一切変わらない。``promoted_from`` は ref のシーケンスで、JSON 配列として
        永続化される (artifact_refs と同形式)。
        """
        if not persona_id:
            raise ValueError("persona_id is required")
        if not title:
            raise ValueError("title is required")
        kind, n_id, t_id = self._normalize_parent(parent_kind, note_id, track_id)

        task_id = uuid.uuid4().hex
        now = _now()
        db = self.SessionLocal()
        try:
            activate_new = auto_activate and (
                db.query(PersonaTask)
                .filter(
                    PersonaTask.persona_id == persona_id,
                    PersonaTask.status == STATUS_ACTIVE,
                )
                .first()
                is None
            )

            short_id = self._next_short_id(db, persona_id)
            task = PersonaTask(
                id=task_id,
                persona_id=persona_id,
                short_id=short_id,
                parent_kind=kind,
                note_id=n_id,
                track_id=t_id,
                title=title,
                goal=goal or "",
                summary=summary or "",
                notes=notes,
                status=STATUS_PENDING,
                priority=priority,
                origin=origin,
                active_step_id=None,
                due_at=due_at,
                created_at=now,
                updated_at=now,
                completed_at=None,
                version=0,
                last_actor=actor,
                desire_type=desire_type,
                desire_source=desire_source,
                # 帳簿の初期化は候補 (note 親) のみ。鮮度の起点 = 作成時刻。
                desire_state=DESIRE_STATE_FRESH if kind == PARENT_NOTE else None,
                last_touched_at=now if kind == PARENT_NOTE else None,
                touch_count=0 if kind == PARENT_NOTE else None,
                stage=stage,
                nature=nature,
                promoted_from=(
                    json.dumps([str(r) for r in promoted_from], ensure_ascii=False)
                    if promoted_from else None
                ),
            )
            db.add(task)

            step_records: List[PersonaTaskStep] = []
            for position, step in enumerate(steps, start=1):
                step_id = uuid.uuid4().hex
                step_title = step.get("title") or step.get("summary") or f"Step {position}"
                rec = PersonaTaskStep(
                    id=step_id,
                    task_id=task_id,
                    position=position,
                    title=step_title,
                    description=step.get("description"),
                    status=step.get("status", "pending"),
                    notes=step.get("notes"),
                    created_at=now,
                    updated_at=now,
                    completed_at=None,
                    version=0,
                )
                db.add(rec)
                step_records.append(rec)

            self._insert_history(
                db,
                task_id=task_id,
                step_id=None,
                event_type="create_task",
                payload={
                    "title": title,
                    "goal": goal,
                    "summary": summary,
                    "priority": priority,
                    "origin": origin,
                    "parent_kind": kind,
                    "note_id": n_id,
                    "track_id": t_id,
                    "desire_type": desire_type,
                    "desire_source": desire_source,
                    "steps": [
                        {"title": s.title, "description": s.description, "status": s.status}
                        for s in step_records
                    ],
                },
                actor=actor,
            )
            db.commit()
            LOGGER.info(
                "[task] created %s persona=%s parent=%s/%s status=%s",
                task_id, persona_id, kind, n_id or t_id, STATUS_PENDING,
            )
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        if activate_new:
            try:
                self.set_active_task(task_id, persona_id=persona_id, actor=actor)
                created = self.get_task(task_id, persona_id=persona_id)
                next_step = next(
                    (s["id"] for s in created["steps"] if s["status"] not in {"completed", "skipped"}),
                    None,
                )
                self.set_active_step(task_id, step_id=next_step, persona_id=persona_id, actor=actor)
            except TaskConflictError:
                pass

        return self.get_task(task_id, persona_id=persona_id)

    @staticmethod
    def _normalize_parent(
        parent_kind: Optional[str], note_id: Optional[str], track_id: Optional[str]
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """親バインドを正規化し排他性を検証する (note か track の一方のみ)。"""
        if note_id and track_id:
            raise ValueError("a task cannot bind to both a note and a track")
        if parent_kind == PARENT_NOTE or (parent_kind is None and note_id):
            if not note_id:
                raise ValueError("parent_kind='note' requires note_id")
            return PARENT_NOTE, note_id, None
        if parent_kind == PARENT_TRACK or (parent_kind is None and track_id):
            if not track_id:
                raise ValueError("parent_kind='track' requires track_id")
            return PARENT_TRACK, None, track_id
        if parent_kind not in (None, PARENT_NOTE, PARENT_TRACK):
            raise ValueError(f"invalid parent_kind: {parent_kind!r}")
        return None, None, None

    def get_task(self, task_id: str, *, persona_id: Optional[str] = None) -> Dict[str, Any]:
        db = self.SessionLocal()
        try:
            task = self._fetch_task_or_raise(db, task_id, persona_id)
            steps = self._fetch_steps(db, task_id)
            return self._task_to_dict(task, steps)
        finally:
            db.close()

    def list_tasks(
        self,
        persona_id: str,
        *,
        statuses: Optional[Sequence[str]] = None,
        parent_kind: Optional[str] = None,
        note_id: Optional[str] = None,
        track_id: Optional[str] = None,
        limit: Optional[int] = None,
        include_steps: bool = True,
    ) -> List[Dict[str, Any]]:
        db = self.SessionLocal()
        try:
            q = db.query(PersonaTask).filter(PersonaTask.persona_id == persona_id)
            if statuses:
                q = q.filter(PersonaTask.status.in_(list(statuses)))
            if parent_kind is not None:
                q = q.filter(PersonaTask.parent_kind == parent_kind)
            if note_id is not None:
                q = q.filter(PersonaTask.note_id == note_id)
            if track_id is not None:
                q = q.filter(PersonaTask.track_id == track_id)
            q = q.order_by(PersonaTask.updated_at.desc())
            if limit:
                q = q.limit(int(limit))
            tasks = q.all()
            result: List[Dict[str, Any]] = []
            for task in tasks:
                steps = self._fetch_steps(db, task.id) if include_steps else []
                result.append(self._task_to_dict(task, steps))
            return result
        finally:
            db.close()

    # ------------------------------------------------------------------
    # 状態遷移
    # ------------------------------------------------------------------

    def update_task_status(
        self,
        task_id: str,
        *,
        status: str,
        actor: Optional[str],
        persona_id: Optional[str] = None,
        reason: Optional[str] = None,
        expected_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        now = _now()
        db = self.SessionLocal()
        try:
            task = self._fetch_task_or_raise(db, task_id, persona_id)
            if expected_version is not None and task.version != expected_version:
                raise TaskConflictError(f"task {task_id} version conflict")
            task.status = status
            task.updated_at = now
            task.completed_at = now if status in TERMINAL_TASK_STATUSES else None
            task.last_actor = actor
            task.version = task.version + 1
            self._insert_history(
                db,
                task_id=task_id,
                step_id=None,
                event_type="update_task_status",
                payload={"status": status, "reason": reason},
                actor=actor,
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        return self.get_task(task_id, persona_id=persona_id)

    def set_active_task(
        self, task_id: str, *, persona_id: str, actor: Optional[str]
    ) -> Dict[str, Any]:
        """指定 Task を active にし、同ペルソナの既存 active を paused に押し出す。"""
        now = _now()
        db = self.SessionLocal()
        try:
            task = self._fetch_task_or_raise(db, task_id, persona_id)
            db.query(PersonaTask).filter(
                PersonaTask.persona_id == persona_id,
                PersonaTask.status == STATUS_ACTIVE,
                PersonaTask.id != task_id,
            ).update(
                {
                    PersonaTask.status: STATUS_PAUSED,
                    PersonaTask.updated_at: now,
                    PersonaTask.last_actor: actor,
                    PersonaTask.version: PersonaTask.version + 1,
                },
                synchronize_session=False,
            )
            task.status = STATUS_ACTIVE
            task.updated_at = now
            task.last_actor = actor
            task.version = task.version + 1
            self._insert_history(
                db,
                task_id=task_id,
                step_id=None,
                event_type="set_active_task",
                payload={},
                actor=actor,
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        return self.get_task(task_id, persona_id=persona_id)

    def set_active_step(
        self,
        task_id: str,
        *,
        step_id: Optional[str],
        persona_id: Optional[str] = None,
        actor: Optional[str],
    ) -> Dict[str, Any]:
        now = _now()
        db = self.SessionLocal()
        try:
            task = self._fetch_task_or_raise(db, task_id, persona_id)
            task.active_step_id = step_id
            task.updated_at = now
            task.last_actor = actor
            task.version = task.version + 1
            self._insert_history(
                db,
                task_id=task_id,
                step_id=step_id,
                event_type="set_active_step",
                payload={"active_step_id": step_id},
                actor=actor,
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        return self.get_task(task_id, persona_id=persona_id)

    def update_step_status(
        self,
        step_id: str,
        *,
        status: str,
        actor: Optional[str],
        notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = _now()
        db = self.SessionLocal()
        try:
            step = db.query(PersonaTaskStep).filter(PersonaTaskStep.id == step_id).first()
            if step is None:
                raise TaskNotFoundError(f"step not found: {step_id}")
            task_id = step.task_id
            step.status = status
            if notes is not None:
                step.notes = notes
            step.updated_at = now
            step.completed_at = now if status == "completed" else None
            step.version = step.version + 1
            task = db.query(PersonaTask).filter(PersonaTask.id == task_id).first()
            if task is not None:
                task.updated_at = now
                task.last_actor = actor
                task.version = task.version + 1
            self._insert_history(
                db,
                task_id=task_id,
                step_id=step_id,
                event_type="update_step_status",
                payload={"status": status, "notes": notes},
                actor=actor,
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        return self.get_task(task_id)

    def set_steps(
        self,
        task_id: str,
        steps: Sequence[Dict[str, Any]],
        *,
        persona_id: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """タスクのステップ群を与えられた配列で**置換**する (= 分解 decompose)。

        既存ステップを削除し ``steps`` (各要素 ``{title, description?, status?}``)
        を position 1..N で作り直す。active_step は最初の未完ステップに合わせる。
        分解の知性は呼び出し側 LLM (自律モノローグ) が担い、本メソッドは記録のみ。
        """
        now = _now()
        db = self.SessionLocal()
        try:
            task = self._fetch_task_or_raise(db, task_id, persona_id)
            # 既存ステップを削除して作り直す
            db.query(PersonaTaskStep).filter(
                PersonaTaskStep.task_id == task_id
            ).delete(synchronize_session=False)
            first_open_step_id: Optional[str] = None
            for position, step in enumerate(steps, start=1):
                step_id = uuid.uuid4().hex
                status = step.get("status", "pending")
                db.add(PersonaTaskStep(
                    id=step_id,
                    task_id=task_id,
                    position=position,
                    title=step.get("title") or step.get("summary") or f"Step {position}",
                    description=step.get("description"),
                    status=status,
                    notes=step.get("notes"),
                    created_at=now,
                    updated_at=now,
                    completed_at=now if status == "completed" else None,
                    version=0,
                ))
                if first_open_step_id is None and status not in {"completed", "skipped"}:
                    first_open_step_id = step_id
            task.active_step_id = first_open_step_id
            task.updated_at = now
            task.last_actor = actor
            task.version = task.version + 1
            self._insert_history(
                db,
                task_id=task_id,
                step_id=None,
                event_type="decompose",
                payload={"step_count": len(steps),
                         "titles": [s.get("title") for s in steps]},
                actor=actor,
            )
            db.commit()
            LOGGER.info("[task] decomposed %s into %d steps", task_id, len(steps))
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        return self.get_task(task_id, persona_id=persona_id)

    def append_artifact_ref(
        self,
        task_id: str,
        artifact_ref: str,
        *,
        persona_id: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """タスクに成果物参照 (Item ID 等) を追記する (重複は無視、履歴つき)。

        セッション終了判断 (judgment_points.md §6) の done 裁定が「このセッションが
        実際に作った成果物」の ref を刻む接地の証跡。起床判断のバックログ提示
        (§4「成果物参照の有無つき」) がここを読む。
        """
        if not artifact_ref:
            raise ValueError("artifact_ref is required")
        now = _now()
        db = self.SessionLocal()
        try:
            task = self._fetch_task_or_raise(db, task_id, persona_id)
            refs = self._parse_artifact_refs(task.artifact_refs)
            if artifact_ref not in refs:
                refs.append(artifact_ref)
            task.artifact_refs = json.dumps(refs, ensure_ascii=False)
            task.updated_at = now
            task.last_actor = actor
            task.version = task.version + 1
            self._insert_history(
                db,
                task_id=task_id,
                step_id=None,
                event_type="append_artifact_ref",
                payload={"artifact_ref": artifact_ref},
                actor=actor,
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        return self.get_task(task_id, persona_id=persona_id)

    # ------------------------------------------------------------------
    # 親バインド: 昇格 (候補 → Track)
    # ------------------------------------------------------------------

    def promote_to_track(
        self, task_id: str, track_id: str, *, persona_id: Optional[str] = None, actor: Optional[str] = None
    ) -> Dict[str, Any]:
        """候補 Task の親を ``note_id`` → ``track_id`` に張り替える (= 昇格)。

        Task をコピー/破棄せず親だけ付け替えるので、履歴・ステップ・status は
        そのまま連続する (不変条件2)。
        """
        now = _now()
        db = self.SessionLocal()
        try:
            task = self._fetch_task_or_raise(db, task_id, persona_id)
            prev_note = task.note_id
            task.parent_kind = PARENT_TRACK
            task.note_id = None
            task.track_id = track_id
            task.updated_at = now
            task.last_actor = actor
            task.version = task.version + 1
            self._insert_history(
                db,
                task_id=task_id,
                step_id=None,
                event_type="promote_to_track",
                payload={"from_note_id": prev_note, "to_track_id": track_id},
                actor=actor,
            )
            db.commit()
            LOGGER.info("[task] promoted %s note=%s -> track=%s", task_id, prev_note, track_id)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        return self.get_task(task_id, persona_id=persona_id)

    def detach_parent(
        self, task_id: str, *, persona_id: Optional[str] = None, actor: Optional[str] = None
    ) -> Dict[str, Any]:
        """Task の親バインドを外して未所属にする (parent_kind/note_id/track_id を NULL)。

        親なし採用ノード (life_concept_map.md §3.1「採用時の親なし — 第一階層に
        小さく立つ」) を作る採用操作 (saiverse/purpose_tree.py の adopt) が使う。
        promote_to_track と同じく行をコピー/破棄せず親だけ変える (履歴つき)。
        """
        now = _now()
        db = self.SessionLocal()
        try:
            task = self._fetch_task_or_raise(db, task_id, persona_id)
            prev_kind, prev_note, prev_track = task.parent_kind, task.note_id, task.track_id
            task.parent_kind = None
            task.note_id = None
            task.track_id = None
            task.updated_at = now
            task.last_actor = actor
            task.version = task.version + 1
            self._insert_history(
                db,
                task_id=task_id,
                step_id=None,
                event_type="detach_parent",
                payload={
                    "from_parent_kind": prev_kind,
                    "from_note_id": prev_note,
                    "from_track_id": prev_track,
                },
                actor=actor,
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        return self.get_task(task_id, persona_id=persona_id)

    def set_purpose_fields(
        self,
        task_id: str,
        *,
        persona_id: Optional[str] = None,
        actor: Optional[str] = None,
        stage: Optional[str] = None,
        nature: Optional[str] = None,
        promoted_from: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        """目的ノードの段階・種別・来歴を更新する (life_concept_map.md §3.1)。

        None の引数は据え置き (部分更新)。stage の遷移規則 (candidate→adopted 等)
        の検証は呼び出し側 (saiverse/purpose_tree.py) の責務 — 本レイヤーは
        priority / origin と同じく永続化のみ担う。履歴 (set_purpose_fields) つき。
        """
        if stage is None and nature is None and promoted_from is None:
            raise ValueError("at least one of stage/nature/promoted_from is required")
        now = _now()
        db = self.SessionLocal()
        try:
            task = self._fetch_task_or_raise(db, task_id, persona_id)
            if stage is not None:
                task.stage = stage
            if nature is not None:
                task.nature = nature
            if promoted_from is not None:
                task.promoted_from = json.dumps(
                    [str(r) for r in promoted_from], ensure_ascii=False
                )
            task.updated_at = now
            task.last_actor = actor
            task.version = task.version + 1
            self._insert_history(
                db,
                task_id=task_id,
                step_id=None,
                event_type="set_purpose_fields",
                payload={
                    "stage": stage,
                    "nature": nature,
                    "promoted_from": list(promoted_from) if promoted_from else None,
                },
                actor=actor,
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        return self.get_task(task_id, persona_id=persona_id)

    # ------------------------------------------------------------------
    # 履歴
    # ------------------------------------------------------------------

    def fetch_history(self, task_id: str, *, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        db = self.SessionLocal()
        try:
            q = (
                db.query(PersonaTaskHistory)
                .filter(PersonaTaskHistory.task_id == task_id)
                .order_by(PersonaTaskHistory.created_at.desc())
            )
            if limit:
                q = q.limit(int(limit))
            return [
                {
                    "id": h.id,
                    "task_id": h.task_id,
                    "step_id": h.step_id,
                    "event_type": h.event_type,
                    "payload": json.loads(h.payload) if h.payload else {},
                    "actor": h.actor,
                    "created_at": h.created_at.isoformat() if h.created_at else None,
                }
                for h in q.all()
            ]
        finally:
            db.close()

    # ------------------------------------------------------------------
    # 旧 track_task 互換層 (Track 内チェックリスト)
    # ------------------------------------------------------------------
    # 旧 TrackManager.get_tasks/add_task/complete_task/format_task_list は
    # ActionTrack.tasks_json の ``[{title, done}]`` を扱っていた。統合後は
    # track_id バインドの persona_task 行に置き換える。互換 API を提供して
    # 既存の Track チェックリスト呼び出しを温存する。

    def get_track_tasks(self, track_id: str) -> List[Dict[str, Any]]:
        """Track 内小目標を ``[{title, done}]`` 互換形式で position 順に返す。"""
        db = self.SessionLocal()
        try:
            rows = (
                db.query(PersonaTask)
                .filter(
                    PersonaTask.track_id == track_id,
                    PersonaTask.parent_kind == PARENT_TRACK,
                )
                .order_by(PersonaTask.created_at.asc())
                .all()
            )
            return [
                {
                    "id": r.id,
                    "short_id": r.short_id,
                    "task_ref": f"task:{r.short_id}" if r.short_id is not None else None,
                    "title": r.title,
                    "done": r.status in TERMINAL_TASK_STATUSES,
                }
                for r in rows
            ]
        finally:
            db.close()

    def add_track_task(
        self, track_id: str, title: str, *, persona_id: str, actor: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Track に小目標を追加して更新後のリスト (互換形式) を返す。"""
        self.create_task(
            persona_id=persona_id,
            title=title,
            goal=title,
            parent_kind=PARENT_TRACK,
            track_id=track_id,
            actor=actor,
            auto_activate=False,
        )
        return self.get_track_tasks(track_id)

    def complete_track_task(
        self, track_id: str, index: int, *, persona_id: Optional[str] = None, actor: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Track 内小目標を index 指定で完了にし更新後のリスト (互換形式) を返す。"""
        tasks = self.get_track_tasks(track_id)
        if index < 0 or index >= len(tasks):
            raise ValueError(f"task index out of range: {index} (total {len(tasks)})")
        self.update_task_status(
            tasks[index]["id"],
            status=STATUS_COMPLETED,
            actor=actor,
            persona_id=persona_id,
        )
        return self.get_track_tasks(track_id)

    def format_track_task_list(self, track_id: str) -> str:
        """Track 内小目標を Markdown チェックリスト形式で返す。

        各行に ``task:N`` 参照子を見せる (done/update_step/decompose はこの参照で指す)。
        """
        tasks = self.get_track_tasks(track_id)
        if not tasks:
            return "(タスクなし)"
        lines = []
        for t in tasks:
            mark = "x" if t.get("done") else " "
            ref = t.get("task_ref") or "task:?"
            lines.append(f"- [{mark}] {ref} {t['title']}")
        return "\n".join(lines)
