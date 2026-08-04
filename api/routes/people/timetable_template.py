"""習慣テンプレート API (時間割改修 T2、timetable_redesign.md §5.1)。

ペルソナの一日のコマ並びの枠 (習慣テンプレート) をユーザーが読み書きする口。
**この API がテンプレートを書き換える唯一の経路** (intent §9-1 — LLM 出力から
テンプレートへ書く経路は存在しない。実装は ``saiverse.timetable_template``)。

- GET    /{persona_id}/timetable-template : 取得 (未設定は null)
- PUT    /{persona_id}/timetable-template : 保存 (検証エラーは 422)
- DELETE /{persona_id}/timetable-template : 削除 (以後は従来の全生成に戻る)
- GET    /{persona_id}/timetable-template/facilities : コマの場所セレクト用の
  行ける場所一覧 (id + 表示名)

フロント UI は T2b (``frontend/src/components/TimetableTemplateModal.tsx``)。
kind セレクトの選択肢は ``GET /api/config/slot-kinds`` (コマ種別カタログ) から。
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.deps import get_manager
from database.models import AI as AIModel
from saiverse import timetable_template

LOGGER = logging.getLogger(__name__)

router = APIRouter()


def _require_persona(manager: Any, persona_id: str) -> None:
    db = manager.SessionLocal()
    try:
        exists = db.query(AIModel.AIID).filter(AIModel.AIID == persona_id).first()
    finally:
        db.close()
    if exists is None:
        raise HTTPException(status_code=404, detail=f"Persona {persona_id} not found")


class TemplateSlotModel(BaseModel):
    """コマ雛形 1 件。start 以外の null / 欠落 = 穴 (朝、ペルソナが埋める)。"""

    start: str
    kind: Optional[str] = None
    title: Optional[str] = None
    facility: Optional[str] = None
    note: Optional[str] = None
    budget_rounds: Optional[int] = None
    ref: Optional[str] = None


class TimetableTemplateRequest(BaseModel):
    slots: List[TemplateSlotModel]
    enabled: bool = True


@router.get("/{persona_id}/timetable-template")
def get_timetable_template(
    persona_id: str, manager=Depends(get_manager)
) -> Optional[Dict[str, Any]]:
    """習慣テンプレートを返す。未設定なら null。

    並びは現在の流れ順基準へ正規化して返す — 起床時刻の設定変更後に旧基準の
    並びで表示され、無変更保存が 422 になる齟齬を防ぐ (Codex 六巡目 #2。
    適用経路 get_active_template と同じ実装を共有)。正規化できない並び
    (同時刻重複等) は保存時の検証に委ねて生のまま返す。
    """
    _require_persona(manager, persona_id)
    template = timetable_template.get_template(manager, persona_id)
    if template is None:
        return None
    sorted_slots = timetable_template.sort_slots_by_current_basis(
        manager, persona_id, template["slots"],
    )
    if sorted_slots is not None and sorted_slots != template["slots"]:
        template = {**template, "slots": sorted_slots}
    return template


@router.put("/{persona_id}/timetable-template")
def put_timetable_template(
    persona_id: str,
    request: TimetableTemplateRequest,
    manager=Depends(get_manager),
) -> Dict[str, Any]:
    """習慣テンプレートを検証して保存する。

    検証エラー (昇順違反・カタログ外 kind・負の予算など) は 422 で、
    理由 (どのコマの何が不正か。カタログ外 kind はカタログ語彙つき) を返す。
    """
    _require_persona(manager, persona_id)
    try:
        saved = timetable_template.save_template(
            manager,
            persona_id,
            [slot.model_dump() for slot in request.slots],
            enabled=request.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    LOGGER.info(
        "[timetable-template] saved via API: persona=%s slots=%d enabled=%s",
        persona_id, len(saved["slots"]), saved["enabled"],
    )
    return saved


class FacilityOption(BaseModel):
    """コマの場所セレクトの選択肢 1 件。"""

    id: str
    name: str


@router.get(
    "/{persona_id}/timetable-template/facilities",
    response_model=List[FacilityOption],
)
def list_timetable_facilities(
    persona_id: str, manager=Depends(get_manager)
) -> List[FacilityOption]:
    """テンプレート編集 UI の場所セレクト用: 行ける場所の一覧 (id + 表示名)。

    候補集合は起床判断の facility enum
    (``saiverse.judgment_points.collect_facility_ids``) と同じ供給元
    (``facility_map.candidate_buildings`` + own_room) から出す — UI で選べるのに
    朝の判断では行けない (逆も) という食い違いを作らないため。
    """
    _require_persona(manager, persona_id)
    from saiverse.day_plan import FACILITY_OWN_ROOM
    from saiverse.facility_map import candidate_buildings

    options = [
        FacilityOption(id=b.building_id, name=getattr(b, "name", None) or b.building_id)
        for b in candidate_buildings(manager)
    ]
    options.append(FacilityOption(id=FACILITY_OWN_ROOM, name="自室"))
    return options


@router.delete("/{persona_id}/timetable-template")
def delete_timetable_template(
    persona_id: str, manager=Depends(get_manager)
) -> Dict[str, bool]:
    """習慣テンプレートを削除する (以後の起床判断は従来の全生成に戻る)。"""
    _require_persona(manager, persona_id)
    deleted = timetable_template.delete_template(manager, persona_id)
    return {"deleted": deleted}
