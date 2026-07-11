"""欲求の帳簿 (desire ledger) — 六型欲求の鮮度・再訪・淘汰の決定論処理 (自律行動 v2 §5)。

欲求の実体は親なし + ``stage='candidate'`` の目的ノード (``persona_task`` 行、
P3c-0 desire 正規化。旧「desire ノートに紐づく Task (parent_kind='note')」
という表現は撤去) であり、本モジュールはその行の帳簿カラム (``desire_type`` /
``desire_source`` / ``desire_state`` / ``last_touched_at`` / ``touch_count``)
を決定論で運用する。**LLM は呼ばない**:

- :func:`decay_desires` — 起床判断 (day_open) 前の帳簿処理。放置された欲求を
  fading → 期限切れ (論理アーカイブ) へ送る
- :func:`apply_desire_reviews` — 就寝判断 (day_close) の ``desire_reviews``
  (keep / fading / fulfilled) を帳簿へ反映する (judgment_points.md §8)
- :func:`touch_desire` — 再訪の記録。時間割のコマが desire を参照して発火した
  とき (``day_plan._fire_slot``) に呼ばれる
- :func:`promotion_candidates` — 再訪回数が閾値以上の欲求のリスト
  (起床判断 ``promotions`` の enum 供給。judgment_points.md §4)
- :func:`desire_summary_for_prompt` — 起床判断の状況テキスト用一覧
  (ref / 型 / タイトル / 鮮度 / 再訪回数)

減衰 (淘汰) のルール — 「選ばれない欲求は減衰し、静かに消える」(v2 §5.3):

- 鮮度の基準は ``last_touched_at`` (無ければ ``created_at``)
- 基準から :data:`FADING_AFTER_DAYS` 日 (暦日) で ``fading``
- :data:`EXPIRE_AFTER_DAYS` 日で期限切れ = status を cancelled にする
  **論理アーカイブ** (物理削除しない)。``persona_task`` は「行を物理削除しない」
  不変条件 (short_id の単調増加) を持ち、候補プールの読み手 (メタ判断 / 本
  モジュール) は全て stage='candidate' でフィルタするため、cancelled 化に
  伴う stage 遷移 (dormant/aborted) だけで候補から自動的に消え、履歴と接地の
  証跡 (desire_source) は残る
- 再訪 (touch) は鮮度を全回復する (state を fresh に戻す)

閾値の初期値 (FADING_AFTER_DAYS=7 / EXPIRE_AFTER_DAYS=14 /
PROMOTION_TOUCH_THRESHOLD=3) は本モジュールの定数。淘汰パラメータの調整は
未決事項 (judgment_points.md §9-3) — 運用観察後にここを変える。

時刻はすべて ``saiverse.clock.now()`` を読む (v2 §12 の不変条件)。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from database.models import PersonaTask
from saiverse import clock
from saiverse.day_plan import SIX_KINDS
from saiverse.persona_task_manager import (
    DESIRE_STATE_EXPIRED,
    DESIRE_STATE_FADING,
    DESIRE_STATE_FRESH,
    STAGE_CANDIDATE,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    PersonaTaskManager,
    TaskNotFoundError,
)

LOGGER = logging.getLogger(__name__)

#: 欲求の六つの原始型 (autonomous_behavior_v2.md §5.1)。day_plan のコマ kind と同一語彙。
DESIRE_TYPES = SIX_KINDS

#: 型未指定 (desire_type=NULL) の表示ラベル
UNTYPED_LABEL = "未分類"

#: last_touched から何日 (暦日) 放置で fading になるか
FADING_AFTER_DAYS = 7

#: last_touched から何日 (暦日) 放置で期限切れ (論理アーカイブ) になるか
EXPIRE_AFTER_DAYS = 14

#: promotion_candidates に載る再訪回数の閾値 (touch_count >= この値)
PROMOTION_TOUCH_THRESHOLD = 3

# 就寝判断 desire_reviews の verdict (judgment_points.md §8)
VERDICT_KEEP = "keep"
VERDICT_FADING = "fading"
VERDICT_FULFILLED = "fulfilled"

_FRESHNESS_LABELS = {
    DESIRE_STATE_FRESH: "新鮮",
    DESIRE_STATE_FADING: "薄れつつある",
    DESIRE_STATE_EXPIRED: "期限切れ",
}


# ---------------------------------------------------------------------------
# 内部ヘルパ
# ---------------------------------------------------------------------------


def _normalize_ref(ref: str) -> str:
    """``desire:N`` を ``task:N`` へ正規化する (同じ short_id 参照空間。day_plan 参照)。"""
    ref = (ref or "").strip()
    if ref.startswith("desire:"):
        return "task:" + ref[len("desire:"):]
    return ref


def _as_date(value: datetime | date) -> date:
    return value.date() if isinstance(value, datetime) else value


def _list_desires(manager: Any, persona_id: str) -> List[Dict[str, Any]]:
    """生きている欲求候補 (stage='candidate' の目的ノード) の dict リスト (P3c-0)。

    刻印が正しければ stage='candidate' だけで生存候補と一致する (完了/失効/
    昇格は書き込み時の刻印で stage が変わるため、status での併用フィルタは
    不要)。
    """
    ptm = PersonaTaskManager(manager.SessionLocal)
    return ptm.list_tasks(
        persona_id,
        stage=STAGE_CANDIDATE,
        include_steps=False,
    )


def _update_ledger_fields(manager: Any, persona_id: str, task_id: str, **changes: Any) -> bool:
    """帳簿カラムのみを直接更新する (updated_at / version も揃えて進める)。

    status 遷移 (履歴が要る意味のあるイベント) は PersonaTaskManager 側の
    ``update_task_status`` を使うこと。ここは日次の決定論的な帳簿つけ専用で、
    persona_task_history を毎日膨らませないために履歴は書かない。
    """
    db = manager.SessionLocal()
    try:
        row = (
            db.query(PersonaTask)
            .filter(PersonaTask.id == task_id, PersonaTask.persona_id == persona_id)
            .first()
        )
        if row is None:
            return False
        for key, value in changes.items():
            setattr(row, key, value)
        row.updated_at = clock.now()
        row.version = row.version + 1
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 公開 API
# ---------------------------------------------------------------------------


def decay_desires(
    manager: Any, persona_id: str, today: Optional[datetime | date] = None
) -> Dict[str, Any]:
    """起床判断前の帳簿処理: 放置された欲求を fading → 期限切れへ送る。

    Args:
        today: 基準日 (省略時 ``clock.now()``)。日数は暦日差で数える。

    Returns:
        ``{"checked": int, "faded": [ref], "expired": [ref]}``
        (faded は今回 fading に**変わった**もののみ。既 fading は含まない)
    """
    today_d = _as_date(today if today is not None else clock.now())
    ptm = PersonaTaskManager(manager.SessionLocal)
    faded: List[str] = []
    expired: List[str] = []
    desires = _list_desires(manager, persona_id)
    for task in desires:
        ref = task.get("task_ref") or task["id"]
        base_iso = task.get("last_touched_at") or task.get("created_at")
        if not base_iso:
            continue
        days = (today_d - datetime.fromisoformat(base_iso).date()).days
        if days >= EXPIRE_AFTER_DAYS:
            # 論理アーカイブ: 帳簿の expired を**先に**刻んでから status 遷移を
            # 記録する (P3c-0: update_task_status の cancelled 刻印規則は
            # desire_state を見て dormant/aborted を選ぶため、順序が不変条件 —
            # 逆順だと desire_state がまだ expired でなく aborted に落ちる)。
            _update_ledger_fields(
                manager, persona_id, task["id"], desire_state=DESIRE_STATE_EXPIRED,
            )
            ptm.update_task_status(
                task["id"], status=STATUS_CANCELLED, actor="desire_engine",
                persona_id=persona_id, reason="desire_expired",
            )
            expired.append(ref)
        elif days >= FADING_AFTER_DAYS:
            if (task.get("desire_state") or DESIRE_STATE_FRESH) != DESIRE_STATE_FADING:
                _update_ledger_fields(
                    manager, persona_id, task["id"], desire_state=DESIRE_STATE_FADING,
                )
                faded.append(ref)
    if faded or expired:
        LOGGER.info(
            "[desire] decay: persona=%s checked=%d faded=%s expired=%s",
            persona_id, len(desires), faded, expired,
        )
    return {"checked": len(desires), "faded": faded, "expired": expired}


def apply_desire_reviews(
    manager: Any, persona_id: str, reviews: Optional[List[Dict[str, Any]]]
) -> Dict[str, Any]:
    """就寝判断の desire_reviews (keep/fading/fulfilled) を帳簿へ反映する。

    - ``fulfilled``: 即消化 = status を completed にする (履歴つき)
    - ``fading``: 減衰加速 = state を fading にし、鮮度基準 (last_touched_at) を
      「既に FADING_AFTER_DAYS 日前」まで引き戻す (以後 EXPIRE_AFTER_DAYS -
      FADING_AFTER_DAYS 日の放置で期限切れ)。既にそれより古ければ据え置き
    - ``keep``: 据え置き (何もしない。judgment_points.md §8)

    解決できない ref / 未知の verdict は WARN + skip (判断全体を落とさない)。

    Returns:
        ``{"fulfilled": [ref], "fading": [ref], "kept": [ref], "skipped": [ref]}``
    """
    result: Dict[str, List[str]] = {
        "fulfilled": [], "fading": [], "kept": [], "skipped": [],
    }
    ptm = PersonaTaskManager(manager.SessionLocal)
    for review in reviews or []:
        ref = str((review or {}).get("desire_ref") or "")
        verdict = (review or {}).get("verdict")
        try:
            task_id = ptm.resolve_task_ref(persona_id, _normalize_ref(ref))
        except TaskNotFoundError:
            LOGGER.warning(
                "[desire] review skipped (unresolvable ref): persona=%s ref=%r",
                persona_id, ref,
            )
            result["skipped"].append(ref)
            continue
        if verdict == VERDICT_FULFILLED:
            ptm.update_task_status(
                task_id, status=STATUS_COMPLETED, actor="desire_review",
                persona_id=persona_id, reason="desire_fulfilled",
            )
            result["fulfilled"].append(ref)
        elif verdict == VERDICT_FADING:
            backdate = clock.now() - timedelta(days=FADING_AFTER_DAYS)
            task = ptm.get_task(task_id, persona_id=persona_id)
            touched_iso = task.get("last_touched_at")
            changes: Dict[str, Any] = {"desire_state": DESIRE_STATE_FADING}
            if touched_iso is None or datetime.fromisoformat(touched_iso) > backdate:
                changes["last_touched_at"] = backdate
            _update_ledger_fields(manager, persona_id, task_id, **changes)
            result["fading"].append(ref)
        elif verdict == VERDICT_KEEP:
            result["kept"].append(ref)
        else:
            LOGGER.warning(
                "[desire] review skipped (unknown verdict): persona=%s ref=%r verdict=%r",
                persona_id, ref, verdict,
            )
            result["skipped"].append(ref)
    return result


def touch_desire(manager: Any, persona_id: str, ref: str) -> Optional[Dict[str, Any]]:
    """欲求への再訪を記録する (last_touched_at 更新 + touch_count 加算 + fresh 復帰)。

    時間割のコマが発火したとき (``day_plan._fire_slot``) に、その ref が指すタスクへ
    呼ばれる。``ref`` は ``task:N`` / UUID hex。参照アドレッシング統一 (Q2) 後は
    バックログタスクのコマ発火でもこれが呼ばれるため、desire でない (stage が
    candidate でない) 場合の no-op は正常系 (DEBUG ログ)。

    Returns:
        更新後の Task dict。ref が候補 (stage='candidate') でなければ None。

    Raises:
        TaskNotFoundError: ref が解決できないとき。
    """
    ptm = PersonaTaskManager(manager.SessionLocal)
    task_id = ptm.resolve_task_ref(persona_id, _normalize_ref(ref))
    task = ptm.get_task(task_id, persona_id=persona_id)
    if task.get("stage") != STAGE_CANDIDATE:
        # 統合後はバックログタスクのコマ発火でも呼ばれる正常系なので DEBUG。
        LOGGER.debug(
            "[desire] touch skipped (not a desire candidate): persona=%s ref=%r stage=%r",
            persona_id, ref, task.get("stage"),
        )
        return None
    new_count = (task.get("touch_count") or 0) + 1
    _update_ledger_fields(
        manager, persona_id, task_id,
        last_touched_at=clock.now(),
        touch_count=new_count,
        desire_state=DESIRE_STATE_FRESH,
    )
    LOGGER.info(
        "[desire] touched: persona=%s ref=%s count=%d", persona_id, ref, new_count,
    )
    return ptm.get_task(task_id, persona_id=persona_id)


def promotion_candidates(manager: Any, persona_id: str) -> List[Dict[str, Any]]:
    """再訪回数が閾値以上の欲求 = 関心への昇格候補 (v2 §5.3)。

    起床判断 (day_open) の ``promotions.desire_ref`` enum 供給元
    (judgment_points.md §4 — 候補 enum は淘汰機構が絞る)。

    Returns:
        ``[{"task_ref", "title", "desire_type", "touch_count"}]``
    """
    out: List[Dict[str, Any]] = []
    for task in _list_desires(manager, persona_id):
        ref = task.get("task_ref")
        if not ref:
            continue
        count = task.get("touch_count") or 0
        if count >= PROMOTION_TOUCH_THRESHOLD:
            out.append({
                "task_ref": ref,
                "title": task.get("title") or "",
                "desire_type": task.get("desire_type"),
                "touch_count": count,
            })
    return out


def desire_summary_for_prompt(manager: Any, persona_id: str) -> str:
    """起床判断の状況テキスト用の欲求一覧 (ref / 型 / タイトル / 鮮度 / 再訪回数)。

    文言は客観 + 丁寧語 (キャラ付けしない)。欲求が無ければその旨を 1 行で返す。
    ref の表記は ``task:N`` — 欲求もバックログタスクも同じ short_id 参照空間で、
    参照アドレッシング統一 (docs/intent/reference_addressing.md, Q2) により符号は
    ``task:`` に一本化した。欲求らしさはこの「やりたいこと候補」という提示文脈で伝わる。
    """
    desires = _list_desires(manager, persona_id)
    if not desires:
        return "やりたいこと候補はありません。"
    lines = ["やりたいこと候補:"]
    for task in desires:
        ref = task.get("task_ref") or "task:?"
        dtype = task.get("desire_type") or UNTYPED_LABEL
        title = task.get("title") or "(無題)"
        state = task.get("desire_state") or DESIRE_STATE_FRESH
        freshness = _FRESHNESS_LABELS.get(state, state)
        count = task.get("touch_count") or 0
        lines.append(f"- {ref} [{dtype}] {title} (鮮度: {freshness} / 再訪: {count}回)")
    return "\n".join(lines)
