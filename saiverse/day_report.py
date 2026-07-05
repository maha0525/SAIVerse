"""一日レポート (一日新聞) — ペルソナの一日を予定 vs 実績で読む (自律行動 v2 §12)。

シム専用ではなく **ユーザー向け機能** として設計する: ライフビューの日次まとめ
として、ペルソナの一日 (時間割の予定と実績・セッションの成果物・欲求の動き・
予算・就寝のふりかえり) を一枚の markdown で眺めて楽しめる「一日新聞」。

情報源はすべて実在の記録 (接地原則):

- 時間割と実績: ``persona_day_plan.slots_json`` (status は発火機構が刻む)
- 予算: ``persona_day_plan.meta_json`` の日次予算台帳 (実測消費)
- セッションの成果: SAIMemory の committed ダイジェスト
  (``sea.work_session.DIGEST_TAG``) と、その metadata の成果物 ref。
  成果物名は Item テーブルから解決し ``saiverse://item/<id>/content`` を添える
- 欲求の動き: desire ノート内の候補 Task の帳簿カラム (生まれた/触れた/消えた)
- 就寝のふりかえり: SAIMemory の ``judgment:day_close`` 記録の
  ``metadata.judgment.monologue`` (独白本文のみ。適用エコー行は載せない —
  同内容が plan meta 由来の節で再掲されるため) と
  plan meta (tomorrow_memo / day_theme / user_report_seeds)

LLM は呼ばない (全節が決定論構築)。データの無い節は「(なし)」で埋め、
穴を作らない。文言は客観 + 丁寧語。

読ませ方 (2026-07-05 まはーフィードバック):

- 節順序は「人の一日の読み物」を先に、システム的な帳簿を後に —
  ヘッダ/テーマ → 時間割 → 就寝のふりかえり → セッションの成果 →
  やりたいことの動き → 予算 → 独白
- 時間割テーブルは「時刻 | やること (ペルソナ自身が付けた表題) | 実績」が主役。
  型・場所・参照・予算・予定時の覚え書きは補足列に格下げする
- title の無い旧データは note 先頭 or kind で代替表示する (後方互換)

保存先: ``~/.saiverse/personas/<id>/day_reports/<date>.md``
(:func:`save_day_report`。SAIVERSE_HOME は ``saiverse.data_paths`` の解決に従う)。
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from saiverse.day_plan import (
    REF_NONE,
    get_budget_state,
    load_day_plan,
    load_plan_meta,
    slot_result_label,
)

LOGGER = logging.getLogger(__name__)

NONE_TEXT = "（なし）"

# コマの実績ラベルは day_plan.slot_result_label (就寝判断の予定 vs 実績と同じ
# 語彙)。skipped はシステム都合 (実行手段未実装 / 予算切れ / 会話優先) を明示し、
# 本人の「見送り」判断として表示しない。record_level='presence_only' な done
# (暮らし/休む スタブ) は「実行済み」でなく「時間を過ごした（詳細な記録なし）」。

#: SAIMemory から読む最大件数 (一日分には十分な深さ)
_FETCH_LIMIT = 50


# ---------------------------------------------------------------------------
# SAIMemory / DB 読み出しヘルパ (best-effort — 読めない節は「(なし)」になる)
# ---------------------------------------------------------------------------


def _normalize_plan_date(plan_date: Any) -> str:
    if isinstance(plan_date, datetime):
        return plan_date.date().isoformat()
    if isinstance(plan_date, date):
        return plan_date.isoformat()
    return date.fromisoformat(str(plan_date).strip()).isoformat()


def _fetch_memory_payloads(
    manager: Any, persona_id: str, required_tags: List[str],
) -> List[Dict[str, Any]]:
    """adapter 経由で committed 記録を読む。読めない構成では空リスト。"""
    persona = (getattr(manager, "personas", None) or {}).get(persona_id)
    adapter = getattr(persona, "sai_memory", None) if persona is not None else None
    fetch = getattr(adapter, "recent_persona_messages_by_count", None)
    if not callable(fetch):
        return []
    try:
        return list(fetch(_FETCH_LIMIT, required_tags=required_tags) or [])
    except Exception:
        LOGGER.warning(
            "[day_report] failed to fetch memory payloads (persona=%s tags=%s)",
            persona_id, required_tags, exc_info=True,
        )
        return []


def _payload_tags(payload: Dict[str, Any]) -> List[str]:
    """payload の metadata tags。

    adapter のタグフィルタはタグ無し行 (レガシー互換 + paired_action 展開) を
    素通しにするため、タグが本当に付いているかは呼び出し側で確認する。
    """
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    raw = metadata.get("tags")
    return [str(t) for t in raw if t] if isinstance(raw, list) else []


def _payload_created_at(payload: Dict[str, Any]) -> str:
    raw = payload.get("created_at") or payload.get("timestamp") or ""
    # 実 adapter は Unix epoch (int) を返す。日付比較のため ISO に正規化する
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(raw).isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    return str(raw)


def _digest_plan_date(payload: Dict[str, Any], ws: Dict[str, Any]) -> str:
    """ダイジェストが属する日付 (YYYY-MM-DD)。

    シム中の記録は payload の created_at が実時刻でも、work_session metadata が
    仮想クロックの日付を持つ。仮想日 > 仮想開始時刻 > created_at の順で解決する。
    """
    extra = ws.get("extra") if isinstance(ws.get("extra"), dict) else {}
    day_plan = extra.get("day_plan") if isinstance(extra.get("day_plan"), dict) else {}
    if day_plan.get("plan_date"):
        return str(day_plan["plan_date"])[:10]
    started = str(ws.get("started_at") or "")
    if started:
        return started[:10]
    return _payload_created_at(payload)[:10]


def _collect_session_digests(
    manager: Any, persona_id: str, plan_date_str: str,
) -> List[Dict[str, Any]]:
    """当日の作業セッションダイジェスト (content + work_session metadata)。"""
    from sea.work_session import DIGEST_TAG

    out: List[Dict[str, Any]] = []
    for payload in _fetch_memory_payloads(manager, persona_id, [DIGEST_TAG]):
        if DIGEST_TAG not in _payload_tags(payload):
            continue
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        ws = metadata.get("work_session") if isinstance(metadata.get("work_session"), dict) else {}
        digest_date = _digest_plan_date(payload, ws)
        if digest_date and digest_date != plan_date_str:
            continue
        content = str(payload.get("content") or "").strip()
        out.append({
            "content": content,
            "task_ref": ws.get("task_ref"),
            "artifacts": [str(a) for a in (ws.get("artifacts") or [])],
            "rounds_used": ws.get("rounds_used"),
            "budget_rounds": ws.get("budget_rounds"),
            "ended_reason": ws.get("ended_reason"),
        })
    return out


def _latest_day_close_monologue(manager: Any, persona_id: str) -> str:
    """最新の就寝判断 (judgment:day_close) の独白本文。無ければ空文字。

    記録の content は「独白 + 適用エコー行 (（今日のふりかえりを記録した）等、
    judgment_finalize が連結)」の全文で、エコー行の中身は新聞自身が plan meta
    から再掲するため二重になる。metadata.judgment.monologue (finalize が独白を
    分離して持たせる読み口) を優先し、それが無い旧記録のみ content 全文へ
    フォールバックする (旧データはクラッシュせず従来表示)。

    NOTE: 判断記録のタイムスタンプは adapter 互換のため実時刻 (UTC) で刻まれる
    ことがあり、シム中は仮想日と一致しない。就寝判断は一日 1 回なので
    「最新 1 件」で当日分と見なす。
    """
    payloads = _fetch_memory_payloads(manager, persona_id, ["judgment:day_close"])
    for payload in reversed(payloads):
        if "judgment:day_close" not in _payload_tags(payload):
            continue
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        judgment = metadata.get("judgment")
        if isinstance(judgment, dict) and "monologue" in judgment:
            return str(judgment.get("monologue") or "").strip()
        return str(payload.get("content") or "").strip()
    return ""


def _resolve_item_labels(manager: Any, item_ids: List[str]) -> Dict[str, str]:
    """Item ID → 「名前 (saiverse://item/<id>/content)」ラベル。"""
    labels: Dict[str, str] = {}
    if not item_ids:
        return labels
    session_factory = getattr(manager, "SessionLocal", None)
    names: Dict[str, str] = {}
    if session_factory is not None:
        try:
            from database.models import Item

            db = session_factory()
            try:
                rows = (
                    db.query(Item.ITEM_ID, Item.NAME)
                    .filter(Item.ITEM_ID.in_(list(item_ids)))
                    .all()
                )
                names = {row[0]: (row[1] or "") for row in rows}
            finally:
                db.close()
        except Exception:
            LOGGER.warning("[day_report] item name lookup failed", exc_info=True)
    for item_id in item_ids:
        name = names.get(item_id) or "(名称不明)"
        labels[item_id] = f"{name}（saiverse://item/{item_id}/content）"
    return labels


def _list_all_desires(manager: Any, persona_id: str) -> List[Dict[str, Any]]:
    """desire ノート内の候補 Task 全件 (status 問わず — 消えたものも見るため)。"""
    try:
        from saiverse.note_manager import NOTE_TYPE_DESIRE, NoteManager
        from saiverse.persona_task_manager import PARENT_NOTE, PersonaTaskManager

        notes = NoteManager(manager.SessionLocal).list_for_persona(
            persona_id, note_type=NOTE_TYPE_DESIRE,
        )
        if not notes:
            return []
        return PersonaTaskManager(manager.SessionLocal).list_tasks(
            persona_id,
            note_id=notes[0].note_id,
            parent_kind=PARENT_NOTE,
            include_steps=False,
        )
    except Exception:
        LOGGER.warning(
            "[day_report] desire listing failed (persona=%s)", persona_id, exc_info=True,
        )
        return []


def _persona_display_name(manager: Any, persona_id: str) -> str:
    persona = (getattr(manager, "personas", None) or {}).get(persona_id)
    name = getattr(persona, "persona_name", None) if persona is not None else None
    if name:
        return str(name)
    session_factory = getattr(manager, "SessionLocal", None)
    if session_factory is not None:
        try:
            from database.models import AI

            db = session_factory()
            try:
                row = db.query(AI.AINAME).filter(AI.AIID == persona_id).first()
                if row and row[0]:
                    return str(row[0])
            finally:
                db.close()
        except Exception:
            LOGGER.debug("[day_report] AI name lookup failed", exc_info=True)
    return persona_id


# ---------------------------------------------------------------------------
# 節の構築
# ---------------------------------------------------------------------------


def _facility_labels(manager: Any) -> Dict[str, str]:
    """facility 値 → 表示名 (Building 名 / 自分の部屋)。解決不能な ID は素通し。"""
    labels: Dict[str, str] = {"own_room": "自分の部屋"}
    for b in getattr(manager, "buildings", None) or []:
        bid = getattr(b, "building_id", None)
        if bid:
            labels[bid] = str(getattr(b, "name", "") or bid)
    return labels


def _section_timetable(
    slots: Optional[List[Dict[str, Any]]], facility_labels: Dict[str, str],
) -> List[str]:
    """時間割の予定 vs 実績。

    主役は「時刻 | やること (ペルソナが付けた表題) | 実績」の 3 列。
    型・場所・参照・予算・予定時の覚え書き (note) は補足列に格下げする。
    title の無い旧データは note 先頭 or kind で代替表示する (後方互換)。
    """
    lines = ["## 時間割（予定と実績）", ""]
    if not slots:
        lines.append("この日の時間割はありませんでした。")
        return lines
    lines.append("| 時刻 | やること | 実績 | 補足 |")
    lines.append("|---|---|---|---|")
    for s in slots:
        label = slot_result_label(s)
        kind = str(s.get("kind") or "")
        title = (s.get("title") or "").strip()
        note = (s.get("note") or "").strip()
        # title の無い旧データは note 先頭で代替する (長文 note は切り詰め、
        # 全文は補足列に残る)。note も無ければ kind。
        note_head = note if len(note) <= 30 else note[:30] + "……"
        doing = title or note_head or kind or "—"

        extras: List[str] = []
        if kind and kind != doing:
            extras.append(kind)
        facility = str(s.get("facility") or "")
        if facility:
            extras.append(facility_labels.get(facility, facility))
        ref = s.get("ref")
        if ref and ref != REF_NONE:
            extras.append(f"参照: {ref}")
        budget = int(s.get("budget_rounds") or 0)
        if budget > 0:
            extras.append(f"予算: {budget}")
        if note and note != doing:
            extras.append(note)
        extras_text = " ／ ".join(extras) if extras else "—"
        lines.append(f"| {s.get('start')} | {doing} | {label} | {extras_text} |")
    return lines


def _section_sessions(
    digests: List[Dict[str, Any]], item_labels: Dict[str, str],
) -> List[str]:
    lines = ["## 作業セッションの成果", ""]
    if not digests:
        lines.append(NONE_TEXT)
        return lines
    for i, d in enumerate(digests, start=1):
        header = f"### セッション {i}"
        extras: List[str] = []
        if d.get("task_ref"):
            extras.append(f"対象: {d['task_ref']}")
        rounds = d.get("rounds_used")
        budget = d.get("budget_rounds")
        if rounds is not None:
            extras.append(
                f"使用ラウンド: {rounds}/{budget}" if budget else f"使用ラウンド: {rounds}"
            )
        if d.get("ended_reason"):
            extras.append(f"終了理由: {d['ended_reason']}")
        if extras:
            header += "（" + " / ".join(extras) + "）"
        lines.append(header)
        lines.append("")
        lines.append(d.get("content") or "（ダイジェストなし）")
        lines.append("")
        lines.append("作られた成果物:")
        artifacts = d.get("artifacts") or []
        if artifacts:
            for a in artifacts:
                lines.append(f"- {item_labels.get(a, a)}")
        else:
            lines.append(f"- {NONE_TEXT}")
        lines.append("")
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def _section_desires(
    desires: List[Dict[str, Any]], plan_date_str: str,
) -> List[str]:
    born: List[str] = []
    touched: List[str] = []
    gone: List[str] = []
    for task in desires:
        title = task.get("title") or "(無題)"
        dtype = task.get("desire_type") or "未分類"
        created = str(task.get("created_at") or "")
        touched_at = str(task.get("last_touched_at") or "")
        updated = str(task.get("updated_at") or "")
        status = task.get("status")

        if created.startswith(plan_date_str):
            source = (task.get("desire_source") or "").strip()
            line = f"- [{dtype}] {title}"
            if source:
                line += f" — 出自: {source}"
            born.append(line)
        if touched_at.startswith(plan_date_str) and (task.get("touch_count") or 0) > 0:
            touched.append(
                f"- [{dtype}] {title}（再訪: {task.get('touch_count') or 0} 回）"
            )
        if status in ("completed", "cancelled") and updated.startswith(plan_date_str):
            reason = "満たされました" if status == "completed" else "静かに消えました"
            gone.append(f"- [{dtype}] {title} — {reason}")

    lines = ["## やりたいことの動き", ""]
    lines.append("生まれたもの:")
    lines.extend(born or [f"- {NONE_TEXT}"])
    lines.append("")
    lines.append("再訪したもの:")
    lines.extend(touched or [f"- {NONE_TEXT}"])
    lines.append("")
    lines.append("消えたもの:")
    lines.extend(gone or [f"- {NONE_TEXT}"])
    return lines


def _section_budget(budget_state: Optional[Dict[str, int]]) -> List[str]:
    lines = ["## 作業予算", ""]
    if budget_state is None:
        lines.append("この日の予算台帳はありません。")
    else:
        lines.append(
            f"消費 {budget_state['used']} / 全体 {budget_state['total']} ラウンド"
            f"（残り {budget_state['remaining']}）"
        )
    return lines


def _section_day_close(
    meta: Dict[str, Any], monologue: str,
) -> List[str]:
    lines = ["## 就寝のふりかえり", ""]
    lines.append(monologue or NONE_TEXT)
    lines.append("")
    memo = (meta.get("tomorrow_memo") or "").strip() if isinstance(meta, dict) else ""
    lines.append(f"明日の自分へのメモ: {memo or NONE_TEXT}")
    seeds = meta.get("user_report_seeds") if isinstance(meta, dict) else None
    lines.append("")
    lines.append("ユーザーに話したいこと:")
    if isinstance(seeds, list) and seeds:
        lines.extend(f"- {s}" for s in seeds)
    else:
        lines.append(f"- {NONE_TEXT}")
    memos = meta.get("event_memos") if isinstance(meta, dict) else None
    if isinstance(memos, list) and memos:
        lines.append("")
        lines.append("イベントの覚え書き:")
        for m in memos:
            if isinstance(m, dict) and (m.get("text") or "").strip():
                event = (m.get("event") or "").strip()
                line = f"- {m['text']}"
                if event:
                    line += f"（きっかけ: {event}）"
                lines.append(line)
    return lines


def _section_monologues(digests: List[Dict[str, Any]]) -> List[str]:
    """committed された独白 (session_digest 等) の抜粋。"""
    lines = ["## 今日の独白から", ""]
    excerpts: List[str] = []
    for d in digests:
        content = (d.get("content") or "").strip()
        if not content:
            continue
        excerpt = content if len(content) <= 200 else content[:200] + "……"
        excerpts.append(f"> {excerpt}")
    if excerpts:
        lines.extend(excerpts)
    else:
        lines.append(NONE_TEXT)
    return lines


# ---------------------------------------------------------------------------
# 公開 API
# ---------------------------------------------------------------------------


def generate_day_report(manager: Any, persona_id: str, plan_date: Any) -> str:
    """ペルソナの一日レポート (markdown) を生成して返す。

    実在の記録 (時間割・台帳・SAIMemory・Item) だけから決定論で構築する。
    データの無い節は「(なし)」で埋める。
    """
    plan_date_str = _normalize_plan_date(plan_date)
    persona_name = _persona_display_name(manager, persona_id)

    slots = load_day_plan(manager, persona_id, plan_date_str)
    meta = load_plan_meta(manager, persona_id, plan_date_str)
    budget_state = get_budget_state(manager, persona_id, plan_date_str)
    digests = _collect_session_digests(manager, persona_id, plan_date_str)
    all_artifact_ids: List[str] = []
    for d in digests:
        for a in d.get("artifacts") or []:
            if a not in all_artifact_ids:
                all_artifact_ids.append(a)
    item_labels = _resolve_item_labels(manager, all_artifact_ids)
    desires = _list_all_desires(manager, persona_id)
    monologue = _latest_day_close_monologue(manager, persona_id)

    day_theme = (meta.get("day_theme") or "").strip() if isinstance(meta, dict) else ""

    # 節順序: 人の一日の読み物 (時間割 → 就寝のふりかえり) を先に、
    # システム的な帳簿 (セッション → 欲求 → 予算 → 独白) を後に。
    parts: List[str] = [
        f"# {persona_name} の一日新聞 — {plan_date_str}",
        "",
        f"今日のテーマ: {day_theme or NONE_TEXT}",
        "",
    ]
    parts.extend(_section_timetable(slots, _facility_labels(manager)))
    parts.append("")
    parts.extend(_section_day_close(meta if isinstance(meta, dict) else {}, monologue))
    parts.append("")
    parts.extend(_section_sessions(digests, item_labels))
    parts.append("")
    parts.extend(_section_desires(desires, plan_date_str))
    parts.append("")
    parts.extend(_section_budget(budget_state))
    parts.append("")
    parts.extend(_section_monologues(digests))
    parts.append("")
    return "\n".join(parts)


def save_day_report(
    manager: Any,
    persona_id: str,
    plan_date: Any,
    report_text: Optional[str] = None,
    base_dir: Optional[Path] = None,
) -> Path:
    """一日レポートをファイルに保存し、保存先パスを返す。

    Args:
        report_text: 省略時は :func:`generate_day_report` で生成する。
        base_dir: 保存先ルートの上書き (テスト用)。省略時は
            ``~/.saiverse/personas/<id>/day_reports/`` (SAIVERSE_HOME 準拠)。
    """
    plan_date_str = _normalize_plan_date(plan_date)
    if report_text is None:
        report_text = generate_day_report(manager, persona_id, plan_date_str)
    if base_dir is None:
        from saiverse.data_paths import get_saiverse_home

        base_dir = get_saiverse_home() / "personas" / persona_id / "day_reports"
    else:
        base_dir = Path(base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    path = base_dir / f"{plan_date_str}.md"
    path.write_text(report_text, encoding="utf-8")
    LOGGER.info(
        "[day_report] saved: persona=%s date=%s -> %s", persona_id, plan_date_str, path,
    )
    return path
