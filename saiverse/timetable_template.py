"""習慣テンプレート (時間割改修 T2、timetable_redesign.md §5.1/§5.2)。

ペルソナの一日のコマ並びの**枠**。ユーザーとペルソナの合意で決まり、日々は
固定 — 毎朝の起床判断 (day_open) の仕事は「組む」から「埋める」へ縮む:
テンプレートの穴 (null / 欠落のフィールド) だけをペルソナが埋め、確定
フィールドは LLM 出力がどうであれテンプレート値に矯正される
(:func:`compose_timetable_from_template`。呼び出し元は
``builtin_data/tools/judgment_finalize.py`` の day_open 適用)。

データの器は world DB の ``persona_timetable_template``
(``database.models.PersonaTimetableTemplate``、1 ペルソナ 1 行)。SLOTS_JSON は
コマ雛形の配列で、各要素:

    {"start": "HH:MM" (必須),
     "kind": コマ種別カタログの名前 | null,
     "title": str | null, "facility": building_id|"own_room" | null,
     "note": str | null, "budget_rounds": int | null,
     "ref": "task:N"|"desire:N"|"track:N"|"none" | null}

null / 欠落 = **穴** (朝、起床判断でペルソナが埋める。intent §5.3)。

**不変条件 (intent §9-1): 習慣テンプレートは LLM 出力で直接変化しない。**
このテーブルを書き換える唯一の経路は :func:`save_template` /
:func:`delete_template` で、呼び出し元はユーザー API
(``api/routes/people/timetable_template.py``) のみ。ペルソナの LLM 出力・
judgment 系コード (judgment_points / judgment_finalize / meta_layer) から
save_template を呼ぶ経路を**作らないこと** — ペルソナがテンプレートを変えたい
場合は候補として積み、再会時に提案し、ユーザーが承認する相談ルートのみ
(intent §5.1)。

途中起動の合流 (intent §11-12 裁定): 起床判断が開始時刻より後に走った日
(サーバー未起動等) は、過ぎたテンプレートコマを現在時刻へ丸めず
「流れた（サーバーが起動していなかったため）」(status=skipped,
skip_reason=``missed_start``) と正直に記録し、今の時刻以降のコマから合流する。
丸め (:func:`day_plan._normalize_slots_within_organized_range` のクランプ) は
:data:`MISSED_GRACE_MINUTES` 以内の数分のズレ救済に限定する。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from saiverse import clock, day_plan

LOGGER = logging.getLogger(__name__)

#: テンプレートコマが持てるフィールド (start 以外はすべて穴になれる)
TEMPLATE_SLOT_FIELDS = (
    "start", "kind", "title", "facility", "note", "budget_rounds", "ref",
)

#: 起床判断の遅発がこの分数以内なら「流れた」にせず既存の丸め (現在時刻への
#: クランプ) に委ねる — §11-12「丸めは数分のズレ救済に限定」の閾値。
#: 正典は day_plan 側 (再起動後の予約回復と同じ閾値を共有する — Codex 一巡目 #2)。
MISSED_GRACE_MINUTES = day_plan.MISSED_GRACE_MINUTES

#: 穴のままのフィールドが LLM 出力でも埋まらなかったときの表示語 (流れたコマの
#: 帳簿にのみ現れる。pending コマの kind の穴は既定種別で埋まる)。
UNDECIDED_LABEL = "未定"


# ---------------------------------------------------------------------------
# 検証と CRUD (書き換えの唯一の経路 — intent §9-1)
# ---------------------------------------------------------------------------


def _template_order_key(
    manager: Any, persona_id: str, plan_date: Any = None,
) -> Callable[[str], int]:
    """テンプレートの流れ順キー: 起床時刻を一日の起点 (0) とした経過分。

    ``plan_date`` が与えられ、その日の**確定済みライフ**があるときはそれを
    唯一の基準にする (適用先の保存検証 ``day_order_minutes(lives, ...)`` と
    同じ物差し — 起床時刻の設定変更後に同日を再実行しても基準が分裂しない。
    Codex 六巡目 #1)。無い場合はテンプレートは日付を持たないため
    PersonaSchedule の起床時刻 (``day_plan._resolve_wake``) を起点にする。
    起床未設定なら暦の時刻順に退化する。
    """
    if plan_date is not None:
        try:
            lives = day_plan.get_lives(manager, persona_id, plan_date)
        except Exception:
            lives = []
        if lives:
            def order_by_lives(hhmm: str) -> int:
                return day_plan.day_order_minutes(lives, hhmm)
            return order_by_lives

    wake = day_plan._resolve_wake(manager, persona_id)
    origin = day_plan._life_minutes(wake) if wake else 0

    def order(hhmm: str) -> int:
        return (day_plan._life_minutes(hhmm) - origin) % (24 * 60)

    return order


def validate_template_slots(
    manager: Any, persona_id: str, slots: Any
) -> List[Dict[str, Any]]:
    """テンプレートのコマ雛形配列を検証し、正規化したコピーを返す。

    検証項目 (intent §5.1/§5.3):
    - start は "HH:MM" 必須で、一日の流れ順 (起床起点) に厳密昇順
    - kind の確定値はコマ種別カタログの語彙のみ (穴 = null / 欠落は合法)
    - budget_rounds の確定値は非負 int (bool 不可)
    - ref の確定値は "task:N" / "desire:N" / "track:N" / "none"
    - title / facility / note の確定値は文字列 (facility は非空)

    Raises:
        ValueError: 検証失敗 (メッセージにコマ位置と理由を含む)。
    """
    if not isinstance(slots, list) or not slots:
        raise ValueError("slots must be a non-empty list")

    # 保存前の順序正規化: 現在基準で並べ直せるなら並べ直してから検証する —
    # 起床時刻の設定変更後、ユーザーが内容を変えない保存 (取得 → そのまま
    # 保存) が旧基準の並びのせいで 422 にならないため (Codex 六巡目 #2)。
    # 並べ直せない場合 (同時刻重複・不正 start) は生の並びのまま検証し、
    # 個別の正直なエラーを出す。
    presorted = sort_slots_by_current_basis(manager, persona_id, slots)
    if presorted is not None:
        slots = presorted

    order = _template_order_key(manager, persona_id)
    valid_kinds = day_plan.all_kinds()
    normalized: List[Dict[str, Any]] = []
    prev = -1
    for i, slot in enumerate(slots):
        if not isinstance(slot, dict):
            raise ValueError(f"slot[{i}] must be a dict (got {type(slot).__name__})")

        start = slot.get("start")
        if not day_plan.is_valid_hhmm(start):
            raise ValueError(f"slot[{i}].start must be 'HH:MM' (got {start!r})")
        minutes = order(start)
        if minutes <= prev:
            raise ValueError(
                f"slot[{i}].start={start!r} is not strictly ascending "
                "(slots must be sorted in the order of the day)"
            )
        prev = minutes
        entry: Dict[str, Any] = {"start": start}

        kind = slot.get("kind")
        if kind is not None:
            if not isinstance(kind, str) or kind not in valid_kinds:
                raise ValueError(
                    f"slot[{i}].kind={kind!r} is not in the slot kind catalog "
                    f"{valid_kinds} (穴にするなら null / 省略)"
                )
            entry["kind"] = kind

        for key in ("title", "note"):
            value = slot.get(key)
            if value is not None:
                if not isinstance(value, str):
                    raise ValueError(
                        f"slot[{i}].{key} must be a string or null "
                        f"(got {type(value).__name__})"
                    )
                entry[key] = value

        facility = slot.get("facility")
        if facility is not None:
            if not isinstance(facility, str) or not facility.strip():
                raise ValueError(
                    f"slot[{i}].facility must be a non-empty string or null "
                    f"(got {facility!r})"
                )
            entry["facility"] = facility.strip()

        budget = slot.get("budget_rounds")
        if budget is not None:
            if isinstance(budget, bool) or not isinstance(budget, int) or budget < 0:
                raise ValueError(
                    f"slot[{i}].budget_rounds must be a non-negative int or null "
                    f"(got {budget!r})"
                )
            entry["budget_rounds"] = budget

        ref = slot.get("ref")
        if ref is not None:
            if not isinstance(ref, str) or (
                ref != day_plan.REF_NONE and not day_plan._REF_RE.match(ref)
            ):
                raise ValueError(
                    f"slot[{i}].ref={ref!r} must be 'task:N', 'desire:N', "
                    "'track:N', 'none' or null"
                )
            entry["ref"] = ref

        normalized.append(entry)
    return normalized


def get_template(manager: Any, persona_id: str) -> Optional[Dict[str, Any]]:
    """保存済みの習慣テンプレートを返す。未設定なら None。

    Returns:
        ``{"slots": [...], "enabled": bool, "updated_at": iso文字列|None}``。
        壊れた SLOTS_JSON は WARN + None (未設定と同じ = 従来の全生成に退避)。
    """
    from database.models import PersonaTimetableTemplate

    db = manager.SessionLocal()
    try:
        row = (
            db.query(PersonaTimetableTemplate)
            .filter_by(PERSONA_ID=persona_id)
            .first()
        )
        if row is None:
            return None
        try:
            slots = json.loads(row.SLOTS_JSON)
        except (TypeError, ValueError):
            LOGGER.warning(
                "[timetable_template] SLOTS_JSON is not valid JSON "
                "(persona=%s); treating as unset", persona_id,
            )
            return None
        if not isinstance(slots, list) or not slots:
            LOGGER.warning(
                "[timetable_template] SLOTS_JSON is not a non-empty list "
                "(persona=%s); treating as unset", persona_id,
            )
            return None
        return {
            "slots": slots,
            "enabled": bool(row.ENABLED),
            "updated_at": (
                row.UPDATED_AT.isoformat(timespec="seconds")
                if row.UPDATED_AT else None
            ),
        }
    finally:
        db.close()


def save_template(
    manager: Any, persona_id: str, slots: Any, *, enabled: bool = True
) -> Dict[str, Any]:
    """習慣テンプレートを検証して upsert する (1 ペルソナ 1 行)。

    **これがテンプレートを書き換える唯一の経路** (intent §9-1)。呼び出し元は
    ユーザー API (PUT /api/people/{id}/timetable-template) のみに限る。
    ペルソナの LLM 出力・judgment 系コードからは呼ばないこと — ペルソナ発の
    変更は相談ルート (提案 → ユーザー承認) を通る。

    Returns:
        保存後のテンプレート (:func:`get_template` と同形)。

    Raises:
        ValueError: persona_id 空 / コマ雛形の検証失敗。
    """
    if not persona_id:
        raise ValueError("persona_id is required")
    normalized = validate_template_slots(manager, persona_id, slots)

    from database.models import PersonaTimetableTemplate

    now = clock.now()
    payload = json.dumps(normalized, ensure_ascii=False)
    db = manager.SessionLocal()
    try:
        row = (
            db.query(PersonaTimetableTemplate)
            .filter_by(PERSONA_ID=persona_id)
            .first()
        )
        if row is None:
            db.add(PersonaTimetableTemplate(
                PERSONA_ID=persona_id,
                SLOTS_JSON=payload,
                ENABLED=bool(enabled),
                CREATED_AT=now,
                UPDATED_AT=now,
            ))
        else:
            row.SLOTS_JSON = payload
            row.ENABLED = bool(enabled)
            row.UPDATED_AT = now
        db.commit()
    finally:
        db.close()
    LOGGER.info(
        "[timetable_template] saved: persona=%s slots=%d enabled=%s",
        persona_id, len(normalized), bool(enabled),
    )
    return {
        "slots": normalized,
        "enabled": bool(enabled),
        "updated_at": now.isoformat(timespec="seconds"),
    }


def delete_template(manager: Any, persona_id: str) -> bool:
    """習慣テンプレートを削除する (以後の起床判断は従来の全生成に戻る)。

    Returns:
        削除したら True。行が無ければ False。
    """
    from database.models import PersonaTimetableTemplate

    db = manager.SessionLocal()
    try:
        deleted = (
            db.query(PersonaTimetableTemplate)
            .filter_by(PERSONA_ID=persona_id)
            .delete(synchronize_session=False)
        )
        db.commit()
    finally:
        db.close()
    if deleted:
        LOGGER.info("[timetable_template] deleted: persona=%s", persona_id)
    return bool(deleted)


def sort_slots_by_current_basis(
    manager: Any, persona_id: str, slots: List[Dict[str, Any]],
    plan_date: Any = None,
) -> Optional[List[Dict[str, Any]]]:
    """コマ雛形配列を現在の流れ順基準で並べ直す (集合は変えない)。

    基準は :func:`_template_order_key` (当日の確定済みライフ > plan_date の
    有効スケジュール > 暦順)。並べ直しても厳密昇順にならない場合 (同時刻の
    重複等)・基準が解決できない場合は None。

    適用経路 (:func:`get_active_template`) と編集 API の GET/保存正規化が
    同じ実装を共有する — 画面と適用で順序の解釈が割れない (Codex 六巡目 #2)。
    """
    try:
        order = _template_order_key(manager, persona_id, plan_date)
        keyed = [(order(s.get("start") or ""), i, s) for i, s in enumerate(slots)]
        keyed.sort(key=lambda t: (t[0], t[1]))
        minutes = [k for k, _, _ in keyed]
        if any(b <= a for a, b in zip(minutes, minutes[1:])):
            return None
        return [s for _, _, s in keyed]
    except Exception:
        LOGGER.warning(
            "[timetable_template] failed to sort template slots by current "
            "basis (persona=%s)", persona_id, exc_info=True,
        )
        return None


def get_active_template(
    manager: Any, persona_id: str, plan_date: Any = None,
) -> Optional[Dict[str, Any]]:
    """起床判断が使う有効なテンプレート (enabled のみ)。無効・未設定は None。

    読み取り専用の入口 — 起床判断の状況テキスト
    (``judgment_points.build_day_open_situation_text``) と finalize
    (``judgment_finalize._finalize_day_open``) はここから読む。None のときは
    従来どおりの全生成 (移行の安全弁 — テンプレ未設定のペルソナの挙動は
    T2 前と変わらない)。

    保存後に起床時刻 (一日の流れの基準) が変わると、保存時に昇順だった並びが
    現在基準では昇順でなくなりうる — そのまま compose へ渡すと保存検証が
    拒否して day_open が時間割を保存できない (Codex 五巡目 #2)。コマの中身は
    有効なままなので、**現在基準で並べ直して**返す (集合は変えない — 順序の
    解釈だけが基準に従う)。基準は ``plan_date`` の確定済みライフを最優先
    (適用先の保存検証と同じ物差し — Codex 六巡目 #1)。並べ直しても厳密昇順に
    ならない場合だけ None (従来生成へ退避、WARN)。
    """
    template = get_template(manager, persona_id)
    if template is None or not template.get("enabled"):
        return None
    slots = template["slots"]
    sorted_slots = sort_slots_by_current_basis(
        manager, persona_id, slots, plan_date,
    )
    if sorted_slots is None:
        LOGGER.warning(
            "[timetable_template] template slots cannot form a strictly "
            "ascending day order under the current basis; falling back to "
            "free composition (persona=%s)", persona_id,
        )
        return None
    if sorted_slots != slots:
        LOGGER.info(
            "[timetable_template] template slots reordered under the "
            "current basis (persona=%s)", persona_id,
        )
        template = {**template, "slots": sorted_slots}
    return template


# ---------------------------------------------------------------------------
# 「埋める」化: テンプレート整合の強制 (judgment_finalize の day_open 適用が呼ぶ)
# ---------------------------------------------------------------------------


def _default_hole_kind() -> str:
    """kind の穴が埋まらなかった pending コマに使う既定種別。

    「自由時間」は intent §5.3 の「種別の穴の名前付き版」そのもの — 穴のまま
    走らせる意味論に最も近い。カタログから外されている環境では、作業セッション
    系でない最初の種別 (無ければ先頭) に退避する。
    """
    kinds = day_plan.all_kinds()
    if "自由時間" in kinds:
        return "自由時間"
    workers = set(day_plan.worker_session_kinds())
    for kind in kinds:
        if kind not in workers:
            return kind
    return kinds[0]


def _forced_ref_problem(manager: Any, persona_id: str, ref: str) -> Optional[str]:
    """テンプレートの確定 ref が今日も有効かの検査。問題があれば理由を返す。

    sanitize_timetable の ref 検証と同じ基準 (実在 + 終端状態でない) だが、
    問題のあるコマを**棄却せず ref='none' に降格**するための前判定 — テンプレの
    枠 (コマ数・時刻) は ref の陳腐化で崩さない。
    """
    if ref == day_plan.REF_NONE:
        return None
    if day_plan._TRACK_REF_RE.match(ref):
        track_manager = getattr(manager, "track_manager", None)
        if track_manager is None:
            return "track_manager が無く解決できません"
        from saiverse.track_manager import LIVE_STATUSES, TrackNotFoundError

        try:
            track = track_manager.get(
                track_manager.resolve_track_ref(persona_id, ref)
            )
        except TrackNotFoundError:
            return "存在しません"
        if track.status not in LIVE_STATUSES:
            return f"既に {track.status} の Track です"
        return None
    from saiverse.judgment_points import normalize_task_ref
    from saiverse.persona_task_manager import (
        PersonaTaskManager,
        TaskNotFoundError,
    )

    try:
        ptm = PersonaTaskManager(manager.SessionLocal)
        task = ptm.get_task(
            ptm.resolve_task_ref(persona_id, normalize_task_ref(ref)),
            persona_id=persona_id,
        )
    except TaskNotFoundError:
        return "存在しません"
    status = (task or {}).get("status")
    if status in ("completed", "cancelled"):
        return f"既に {status} のタスクです"
    return None


def _materialize_missed_slot(tslot: Dict[str, Any]) -> Dict[str, Any]:
    """過ぎたテンプレートコマを「流れた」帳簿コマにする (§11-12)。

    穴のフィールドは誰も埋めていない — 埋まったふりをせず、kind は
    :data:`UNDECIDED_LABEL` (帳簿区間は kind 語彙検証を受けない)、他は空値で
    記録する。start はテンプレートの時刻のまま (現在時刻へ丸めない)。
    """
    ref = tslot.get("ref") or day_plan.REF_NONE
    return {
        "start": tslot["start"],
        "kind": tslot.get("kind") or UNDECIDED_LABEL,
        "ref": ref,
        "facility": tslot.get("facility") or day_plan.FACILITY_OWN_ROOM,
        "budget_rounds": tslot.get("budget_rounds") or 0,
        "title": tslot.get("title") or "",
        "note": tslot.get("note") or "",
        "status": day_plan.STATUS_SKIPPED,
        "skip_reason": day_plan.SKIP_REASON_MISSED_START,
    }


def _materialize_retired_kind_slot(tslot: Dict[str, Any]) -> Dict[str, Any]:
    """現行カタログに無い確定 kind のテンプレートコマを帳簿コマにする。

    カタログの変更 (アドオン無効化・語彙再編) でテンプレートに残った旧種別が
    pending 区間へ入ると、保存検証 (kind 語彙) が全体を拒否して日次編成ごと
    止まる (Codex 三巡目)。帳簿区間は kind 語彙検証を受けないため、そこへ
    「この種別は現在使われていない」として正直に置き、日の残りは動かす。
    自動で別種別へ読み替えない (発火時の kind_not_in_vocabulary と同じ裁定 —
    読み替えは意図の捏造)。毎日の一日新聞に残り続けるので、ユーザーが
    テンプレートを直すまで可視の修復待ちになる。
    """
    return {
        "start": tslot["start"],
        "kind": tslot.get("kind") or UNDECIDED_LABEL,
        "ref": tslot.get("ref") or day_plan.REF_NONE,
        "facility": tslot.get("facility") or day_plan.FACILITY_OWN_ROOM,
        "budget_rounds": tslot.get("budget_rounds") or 0,
        "title": tslot.get("title") or "",
        "note": tslot.get("note") or "",
        "status": day_plan.STATUS_SKIPPED,
        "skip_reason": day_plan.SKIP_REASON_KIND_NOT_IN_VOCABULARY,
    }


_FIELD_LABELS = {
    "start": "開始時刻",
    "kind": "種別",
    "title": "表題",
    "facility": "場所",
    "note": "方針",
    "budget_rounds": "予算",
    "ref": "取り組む対象",
}


def compose_timetable_from_template(
    manager: Any,
    persona_id: str,
    plan_date: Any,
    template_slots: List[Dict[str, Any]],
    llm_slots: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
    """テンプレートの枠に LLM 出力を流し込み、今日の時間割を組み立てる (§5.2)。

    **「枠は LLM 出力で直接変わらない」を finalize の構造で保証する**中核:
    出力の時間割は常にテンプレートのコマ並びそのもので、テンプレートで確定して
    いるフィールドは LLM 出力と食い違ってもテンプレート値で上書きする
    (却下でなく矯正)。穴 (null) のフィールドだけが LLM の自由。テンプレートに
    無いコマは追加されない。

    LLM 出力との対応付け: start の完全一致を優先し、残りは流れ順の位置で
    充てる (テンプレートを写して埋めるのが指示なので、通常は完全一致する)。

    途中起動 (§11-12): 現在時刻より :data:`MISSED_GRACE_MINUTES` 分を超えて
    過去のテンプレートコマは「流れた」帳簿 (status=skipped,
    skip_reason=missed_start) になる。閾値以内の過去コマは pending のまま残し、
    既存の丸め (現在時刻へのクランプ) に委ねる。

    Args:
        template_slots: :func:`get_active_template` の slots (保存時検証済み)。
        llm_slots: ``sanitize_timetable`` 済みの LLM 出力コマ (穴の値の供給源)。

    Returns:
        ``(ledger, pending, corrections)``。``ledger`` は「流れた」帳簿コマ
        (時間割の先頭に置き、``replace_day_plan(ledger_prefix=len(ledger))`` で
        保存する)、``pending`` は確定枠 + 穴埋め済みの今日のコマ、
        ``corrections`` は矯正・除外の日常語メモ (monologue とは別に、適用
        サマリとログへ載せる)。
    """
    plan_date_str = (
        plan_date if isinstance(plan_date, str)
        else day_plan._normalize_plan_date(plan_date)
    )
    lives = day_plan.get_lives(manager, persona_id, plan_date_str)

    def order(hhmm: str) -> int:
        return day_plan.day_order_minutes(lives, hhmm)

    now_order = order(clock.now().strftime("%H:%M"))
    corrections: List[str] = []

    valid_kinds = set(day_plan.all_kinds())
    ledger: List[Dict[str, Any]] = []
    future_tslots: List[Dict[str, Any]] = []
    missed_count = 0
    retired_count = 0
    for tslot in template_slots:
        fixed_kind = tslot.get("kind")
        if fixed_kind is not None and fixed_kind not in valid_kinds:
            # 現行カタログに無い確定種別 — pending に入れると保存検証が日次
            # 編成ごと拒否する。帳簿区間へ正直に置き、日の残りは動かす。
            ledger.append(_materialize_retired_kind_slot(tslot))
            retired_count += 1
        elif now_order - order(tslot["start"]) > MISSED_GRACE_MINUTES:
            ledger.append(_materialize_missed_slot(tslot))
            missed_count += 1
        else:
            future_tslots.append(tslot)
    if missed_count:
        corrections.append(
            f"（テンプレートの {missed_count} コマは開始時刻を過ぎていたため"
            "「流れた（サーバーが起動していなかったため）」として記録しました）"
        )
    if retired_count:
        corrections.append(
            f"（テンプレートの {retired_count} コマの種別は現在の時間割では"
            "使われていないため実行できません。習慣テンプレートの種別を"
            "選び直してください）"
        )

    # --- LLM 出力との対応付け: start 完全一致 → 残りは流れ順の位置 ---------
    pool = [dict(s) for s in llm_slots]
    fillers: List[Optional[Dict[str, Any]]] = []
    for tslot in future_tslots:
        match_idx = next(
            (j for j, s in enumerate(pool) if s.get("start") == tslot["start"]),
            None,
        )
        fillers.append(pool.pop(match_idx) if match_idx is not None else None)
    for k in range(len(fillers)):
        if fillers[k] is None and pool:
            fillers[k] = pool.pop(0)
    for extra in pool:
        corrections.append(
            f"（テンプレートに無いコマ {extra.get('start')}"
            f"「{extra.get('title') or extra.get('kind') or ''}」は枠に追加"
            "できないため外しました。枠を変えたいときはユーザーに相談して"
            "テンプレートを更新してください）"
        )

    # --- 確定フィールドの矯正 + 穴の充填 ----------------------------------
    try:
        from saiverse.judgment_points import collect_facility_ids
        facilities = set(collect_facility_ids(manager))
    except Exception:
        LOGGER.warning(
            "[timetable_template] failed to collect facility ids; "
            "facility check degrades to pass-through", exc_info=True,
        )
        facilities = None
    worker_kinds = set(day_plan.worker_session_kinds())

    pending: List[Dict[str, Any]] = []
    for pos, (tslot, filler) in enumerate(zip(future_tslots, fillers), start=1):
        filler = filler or {}
        slot: Dict[str, Any] = {}
        for field in ("start", "kind", "title", "facility", "note",
                      "budget_rounds", "ref"):
            fixed = tslot.get(field)
            if fixed is not None:
                got = filler.get(field)
                # 「LLM が値を出さなかった」形 (sanitize の既定値) は逸脱と
                # みなさない — 毎朝の矯正メモを既定値のエコーで埋めないため。
                unset: tuple = (None, "")
                if field == "budget_rounds":
                    unset = (None, "", 0)
                elif field == "ref":
                    unset = (None, "", day_plan.REF_NONE)
                if filler and got not in unset and got != fixed:
                    corrections.append(
                        f"（テンプレートの確定項目を維持: {pos}番目のコマの"
                        f"{_FIELD_LABELS[field]}を「{got}」から「{fixed}」に"
                        "戻しました）"
                    )
                slot[field] = fixed
            else:
                slot[field] = filler.get(field)

        # 穴が埋まらなかったフィールドの既定値 (LLM 出力が壊れていても枠は守る)
        if not slot.get("kind"):
            slot["kind"] = _default_hole_kind()
            corrections.append(
                f"（{pos}番目のコマの種別が埋まらなかったため"
                f"「{slot['kind']}」にしました）"
            )
        if not slot.get("facility"):
            slot["facility"] = day_plan.FACILITY_OWN_ROOM
        slot["title"] = (slot.get("title") or "").strip()
        slot["note"] = slot.get("note") or ""
        budget = slot.get("budget_rounds")
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 0:
            budget = 0
        slot["budget_rounds"] = budget
        ref = slot.get("ref") or day_plan.REF_NONE

        # 整合の後始末: 種別と ref の関係は検証 (save_day_plan) の不変条件なので
        # ここで正す — 確定種別が作業セッション系でないのに ref が残る、確定 ref
        # が今日はもう有効でない、の両方を none へ降格する (コマは残す)。
        if slot["kind"] not in worker_kinds:
            if ref != day_plan.REF_NONE:
                corrections.append(
                    f"（{pos}番目のコマ ({slot['kind']}) は取り組む対象を"
                    "持てないため対象なしにしました）"
                )
            ref = day_plan.REF_NONE
        elif ref != day_plan.REF_NONE:
            problem = _forced_ref_problem(manager, persona_id, ref)
            if problem is not None:
                corrections.append(
                    f"（{pos}番目のコマの取り組む対象 {ref} は{problem}。"
                    "対象なしで実施します）"
                )
                ref = day_plan.REF_NONE
        slot["ref"] = ref

        if facilities is not None and slot["facility"] not in facilities:
            corrections.append(
                f"（{pos}番目のコマの場所 {slot['facility']} は現在行けない"
                "ため own_room にしました）"
            )
            slot["facility"] = day_plan.FACILITY_OWN_ROOM

        pending.append(slot)

    return ledger, pending, corrections


# ---------------------------------------------------------------------------
# 状況テキスト用の描画 (judgment_points.build_day_open_situation_text が使う)
# ---------------------------------------------------------------------------

#: 穴の表示語 (状況テキスト内)。「埋める」対象であることが読み取れる形にする。
_HOLE_MARK = "＜空欄＞"


def render_template_situation_lines(
    template_slots: List[Dict[str, Any]],
) -> List[str]:
    """状況テキストの [今日の習慣テンプレート] 一覧行 (穴の位置を明示)。"""
    lines: List[str] = []
    for slot in template_slots:
        parts = [f"- {slot['start']}"]
        parts.append(slot.get("kind") or f"種別{_HOLE_MARK}")
        title = slot.get("title")
        parts.append(f"「{title}」" if title else f"表題{_HOLE_MARK}")
        facility = slot.get("facility")
        parts.append(f"@{facility}" if facility else f"場所{_HOLE_MARK}")
        ref = slot.get("ref")
        if ref and ref != day_plan.REF_NONE:
            parts.append(f"ref={ref}")
        budget = slot.get("budget_rounds")
        if budget is not None:
            parts.append(f"予算{budget}")
        note = slot.get("note")
        parts.append(f"（方針: {note}）" if note else f"方針{_HOLE_MARK}")
        lines.append(" ".join(parts))
    return lines
