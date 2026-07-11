"""judgment_finalize: 判断点 (judgment_points.md) の後処理ツール。

判断点 Playbook (judgment_day_open / judgment_post_conversation /
judgment_post_session / judgment_on_event / judgment_day_close) の最終ノードで
呼ばれる。meta_judgment_finalize と同じ様式の kind ディスパッチ:

1. judge LLM ノードが返した dict (構造化出力結果) を受け取る
2. kind に応じて検証・適用する。不正な項目は **該当項目だけ棄却 + WARN**
   (判断全体を落とさない。握り潰さない)
   - day_open: timetable 検証 → save_day_plan + schedule_day_plan。
     promotions → track_create (from_candidate) スペル
   - post_conversation: picked_tasks → タスク作成 (origin_quote 必須の接地、
     track_ref='new' は新規 autonomous Track) / resume_session (resume_now は
     再開コマの即時挿入 / drop は作業メモ片づけ) / new_desires /
     remaining_timetable
   - post_session: task_verdict 適用 (done は artifact_ref の接地検証つき) /
     desk_memo → Track metadata / track_op / new_desires → purpose_seed /
     remaining_timetable → 残りコマの全置換
   - on_event: reaction (engage_now は結果への反映のみ — 応対の起動は
     呼び出し側の責務 / insert_slot は時刻整合検証つき挿入 / note_only は
     plan meta への覚え書き / ignore は記録のみ)。alert では engage_now 以外を
     棄却 (スキーマ縮退の二重ガード)
   - day_close: tomorrow_memo + day_theme + day_digest (実績の決定論要約) +
     user_report_seeds → plan meta / desire_reviews → apply_desire_reviews
     (今日触れた欲求のみ)
3. 整形済みテキスト ``monologue + 適用結果の要約行 + /spell 行`` を SAIMemory に
   ``role='assistant', line_role='meta_judgment'`` で保存する。
   メインキャッシュに LLM の生 JSON は残らない (不変条件 v2-A 継承)。

詳細: ``docs/intent/persona_cognition/judgment_points.md``
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from saiverse import clock
from saiverse import day_plan as day_plan_mod
from saiverse.desire_engine import DESIRE_TYPES, apply_desire_reviews
from saiverse.judgment_points import (
    KIND_DAY_CLOSE,
    KIND_DAY_OPEN,
    KIND_ON_EVENT,
    KIND_POST_CONVERSATION,
    KIND_POST_SESSION,
    REACTION_ENGAGE_NOW,
    REACTION_IGNORE,
    REACTION_INSERT_SLOT,
    REACTION_NOTE_ONLY,
    RESUME_DEFER,
    RESUME_DROP,
    RESUME_NOW,
    build_day_results_text,
    clear_desk_memo,
    collect_promotion_refs,
    insert_timetable_slot,
    normalize_task_ref,
    sanitize_timetable,
    save_desk_memo,
)
from saiverse.persona_task_manager import (
    PARENT_TRACK,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    PersonaTaskManager,
    TaskNotFoundError,
)
from tools.context import (
    get_active_manager,
    get_active_persona_id,
    get_active_pulse_context,
)
from tools.core import ToolResult, ToolSchema

LOGGER = logging.getLogger("saiverse.tools.judgment_finalize")


def _spell_to_text(name: str, args: Dict[str, Any]) -> str:
    """記録用の /spell 行 (正準形式。実行は args dict で直接渡る)。"""
    return f"/spell name='{name}' args={json.dumps(args, ensure_ascii=False)}"


def _fire_spell(
    name: str, args: Dict[str, Any], spells_record: List[Dict[str, Any]]
) -> None:
    """TOOL_REGISTRY 経由でスペルを実行する (meta_judgment_finalize と同じ経路)。

    Track 系スペルは PulseContext の deferred-track-ops に enqueue され、Pulse
    完了時に適用される。失敗は記録 + WARN (判断全体は落とさない)。
    """
    from tools import TOOL_REGISTRY

    tool_func = TOOL_REGISTRY.get(name)
    if tool_func is None:
        LOGGER.warning("[judgment_finalize] spell %r not found in registry", name)
        spells_record.append({
            "name": name, "args": dict(args),
            "result": "tool not found in registry",
        })
        return
    try:
        result = tool_func(**args)
        if isinstance(result, tuple):
            result_str = str(result[0]) if result else ""
        else:
            result_str = str(result)
        spells_record.append({"name": name, "args": dict(args), "result": result_str})
    except Exception as exc:
        LOGGER.exception("[judgment_finalize] spell %r raised", name)
        spells_record.append({
            "name": name, "args": dict(args), "result": f"error: {exc}",
        })


# ---------------------------------------------------------------------------
# day_open
# ---------------------------------------------------------------------------


def _finalize_day_open(
    manager: Any,
    persona_id: str,
    output: Dict[str, Any],
    ctx: Dict[str, Any],
    lines: List[str],
    warnings: List[str],
    spells_record: List[Dict[str, Any]],
) -> bool:
    """起床判断の適用。timetable 保存 or promotions 発火があれば True (committed)。"""
    applied = False
    plan_date = ctx.get("plan_date") or clock.now().date().isoformat()

    # --- timetable: 検証 → save_day_plan + schedule_day_plan -------------
    slots, tt_warnings = sanitize_timetable(manager, persona_id, output.get("timetable"))
    warnings.extend(tt_warnings)

    # 予算合計の検証はソフト制約 (judgment_points.md §3.2)。超過は WARN のみで
    # 保存は通す — 予算ゲート (v2 §4.5) が発火時に日次残高でラウンドを切り詰める。
    daily_budget = ctx.get("daily_budget_rounds")
    if not isinstance(daily_budget, int) or isinstance(daily_budget, bool) or daily_budget <= 0:
        from saiverse.judgment_points import DEFAULT_DAILY_BUDGET_ROUNDS
        daily_budget = DEFAULT_DAILY_BUDGET_ROUNDS
    total = sum(s["budget_rounds"] for s in slots)
    if total > daily_budget:
        warnings.append(
            f"budget_rounds 合計 {total} が日次予算 {daily_budget} を超過 (保存は続行)"
        )

    if not slots:
        # 空配列は不可 — 最低 1 コマを要求する (judgment_points.md §4)。
        # 検証で全滅した場合も plan は保存しない (前日の plan / 既存 plan を壊さない)。
        # 却下はペルソナの文脈 (lines) にも載せる — 黙って捨てない
        # (_apply_remaining_timetable と同じ接地原則)。
        warnings.append(
            "timetable が検証後に空になったため、時間割は保存しませんでした"
        )
        lines.append(
            "（時間割は保存されませんでした: 提出されたコマがすべて無効でした。"
            "今日の時間割は編成されていません）"
        )
    else:
        # 全置換: 旧 plan の予約 (index ベースの key) を先に落としてから保存する。
        day_plan_mod.cancel_scheduled_slots(manager, persona_id, plan_date)
        try:
            day_plan_mod.save_day_plan(manager, persona_id, plan_date, slots)
        except ValueError as exc:
            warnings.append(f"時間割の保存に失敗: {exc}")
            lines.append(
                f"（時間割は保存されませんでした（{exc}）。"
                "今日の時間割は編成されていません）"
            )
        else:
            pushed = day_plan_mod.schedule_day_plan(manager, persona_id, plan_date)
            applied = True
            lines.append(
                f"（今日の時間割を編成: {len(slots)} コマ、{pushed} コマを予約）"
            )
            for s in slots:
                lines.append(
                    f"  {s['start']} {s['kind']}"
                    + (f"「{s['title']}」" if s.get("title") else "")
                    + (f" {s['ref']}" if s["ref"] != "none" else "")
                    + f" @{s['facility']}"
                    + (f"（{s['note']}）" if s["note"] else "")
                )
            # 日次予算台帳の初期化 (v2 §4.5)。total を書き、消費済み (used) は
            # 保持する — 発火時の予算ゲートがこの残高でラウンドを切り詰める。
            try:
                budget_state = day_plan_mod.init_budget_ledger(
                    manager, persona_id, plan_date, daily_budget,
                )
            except Exception as exc:
                LOGGER.exception("[judgment_finalize] init_budget_ledger raised")
                warnings.append(f"日次予算台帳の初期化に失敗: {exc}")
            else:
                budget_line = f"（今日の作業ラウンド予算: {budget_state['total']}"
                if budget_state["used"]:
                    budget_line += (
                        f"、消費済み {budget_state['used']}"
                        f"、残り {budget_state['remaining']}"
                    )
                lines.append(budget_line + "）")

    # --- promotions: 欲求 → 関心 (Track 化) ------------------------------
    promo_valid = set(collect_promotion_refs(manager, persona_id))
    for i, promo in enumerate(output.get("promotions") or []):
        if not isinstance(promo, dict):
            warnings.append(f"promotions[{i}] rejected: not a dict")
            continue
        desire_ref = str(promo.get("desire_ref") or "").strip()
        title = str(promo.get("title") or "").strip()
        if desire_ref not in promo_valid:
            warnings.append(
                f"promotions[{i}] rejected: {desire_ref!r} は昇格候補にありません"
            )
            continue
        if not title:
            warnings.append(f"promotions[{i}] rejected: title が空です")
            continue
        _fire_spell("track_create", {
            "track_type": "autonomous",
            "title": title,
            "intent": str(promo.get("intent") or ""),
            "from_candidate": normalize_task_ref(desire_ref),
        }, spells_record)
        applied = True

    return applied


# ---------------------------------------------------------------------------
# 層2 棚入れ: episode_purposes → purpose_tags (life_concept_map.md §9.1)
# ---------------------------------------------------------------------------


def _write_shelving_tags(
    manager: Any,
    persona_id: str,
    pairs: List[Tuple[str, str]],
    lines: List[str],
    warnings: List[str],
) -> bool:
    """検証済み (episode_ref, purpose_ref) ペア群を層2タグとして永続化する。

    書き込み口はペルソナの SAIMemory adapter (``add_purpose_tag``、memory.db
    相乗り)。adapter が未対応 (テスト・シムの軽量スタブ等) なら WARN して
    棄却する — 黙って捨てない。
    """
    if not pairs:
        return False
    from sai_memory.purpose_tags import LAYER_SHELVE

    persona = (getattr(manager, "personas", None) or {}).get(persona_id)
    adapter = getattr(persona, "sai_memory", None) if persona is not None else None
    add = getattr(adapter, "add_purpose_tag", None)
    if not callable(add):
        warnings.append(
            "episode_purposes: 記憶アダプタが目的タグ未対応のため保存できません"
        )
        return False
    applied = False
    by_episode: Dict[str, List[str]] = {}
    for episode_ref, purpose_ref in pairs:
        if add(target_ref=episode_ref, purpose_ref=purpose_ref, layer=LAYER_SHELVE):
            applied = True
            by_episode.setdefault(episode_ref, []).append(purpose_ref)
        else:
            warnings.append(
                f"episode_purposes: タグの保存に失敗 ({episode_ref} → {purpose_ref})"
            )
    for episode_ref, purposes in by_episode.items():
        lines.append(
            f"（この出来事 {episode_ref} を {', '.join(purposes)} の棚に入れた）"
        )
    return applied


def _apply_episode_purposes_single(
    manager: Any,
    persona_id: str,
    output: Dict[str, Any],
    ctx: Dict[str, Any],
    lines: List[str],
    warnings: List[str],
) -> bool:
    """episode_purposes (purpose 参照の配列) の適用 — 対象の出来事は判断文脈で単一。

    post_conversation (閉じた会話) / post_session (セッション) が使う。
    enum 外 ref は該当項目だけ棄却 + WARN。
    """
    raw = output.get("episode_purposes")
    if not raw:
        return False
    if not isinstance(raw, list):
        warnings.append(
            f"episode_purposes rejected: 配列が必要 (got {type(raw).__name__})"
        )
        return False
    episode_ref = str(ctx.get("episode_ref") or "")
    if not episode_ref:
        warnings.append("episode_purposes rejected: 対象の出来事が不明です")
        return False
    valid = set(ctx.get("purpose_refs") or [])
    pairs: List[Tuple[str, str]] = []
    for i, ref in enumerate(raw):
        ref_str = str(ref or "").strip()
        if ref_str not in valid:
            warnings.append(
                f"episode_purposes[{i}] rejected: {ref_str!r} は選択可能な目的にありません"
            )
            continue
        pairs.append((episode_ref, ref_str))
    return _write_shelving_tags(manager, persona_id, pairs, lines, warnings)


def _apply_episode_purposes_pairs(
    manager: Any,
    persona_id: str,
    output: Dict[str, Any],
    ctx: Dict[str, Any],
    lines: List[str],
    warnings: List[str],
) -> bool:
    """episode_purposes ({episode, purpose} ペア配列) の適用 — day_close 用。"""
    raw = output.get("episode_purposes")
    if not raw:
        return False
    if not isinstance(raw, list):
        warnings.append(
            f"episode_purposes rejected: 配列が必要 (got {type(raw).__name__})"
        )
        return False
    valid_episodes = set(ctx.get("episode_refs") or [])
    valid_purposes = set(ctx.get("purpose_refs") or [])
    pairs: List[Tuple[str, str]] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            warnings.append(f"episode_purposes[{i}] rejected: not a dict")
            continue
        episode_ref = str(item.get("episode") or "").strip()
        purpose_ref = str(item.get("purpose") or "").strip()
        if episode_ref not in valid_episodes:
            warnings.append(
                f"episode_purposes[{i}] rejected: {episode_ref!r} は今日の出来事にありません"
            )
            continue
        if purpose_ref not in valid_purposes:
            warnings.append(
                f"episode_purposes[{i}] rejected: {purpose_ref!r} は選択可能な目的にありません"
            )
            continue
        pairs.append((episode_ref, purpose_ref))
    return _write_shelving_tags(manager, persona_id, pairs, lines, warnings)


# ---------------------------------------------------------------------------
# 共通適用部品 (post_conversation / post_session / on_event が共有)
# ---------------------------------------------------------------------------


def _apply_new_desires(
    output: Dict[str, Any],
    warnings: List[str],
    spells_record: List[Dict[str, Any]],
) -> bool:
    """new_desires → purpose_seed スペル (type/source 付き; v2 §5.2)。

    P2c-1 で候補生成の動詞を desire_add → purpose_seed へ切替 (引数は同一)。
    """
    applied = False
    for i, desire in enumerate(output.get("new_desires") or []):
        if not isinstance(desire, dict):
            warnings.append(f"new_desires[{i}] rejected: not a dict")
            continue
        dtype = desire.get("type")
        title = str(desire.get("title") or "").strip()
        source_quote = str(desire.get("source_quote") or "").strip()
        if dtype not in DESIRE_TYPES:
            warnings.append(f"new_desires[{i}] rejected: 未知の型 {dtype!r}")
            continue
        if not title:
            warnings.append(f"new_desires[{i}] rejected: title が空です")
            continue
        _fire_spell("purpose_seed", {
            "title": title,
            "type": dtype,
            "source": source_quote,
        }, spells_record)
        applied = True
    return applied


def _apply_remaining_timetable(
    manager: Any,
    persona_id: str,
    output: Dict[str, Any],
    ctx: Dict[str, Any],
    lines: List[str],
    warnings: List[str],
) -> bool:
    """remaining_timetable: null=変更なし / 配列=残りコマの全置換 (§3.3)。

    却下 (全滅・置換失敗) は warnings (ログ) だけでなく **lines (ペルソナの
    文脈に乗る適用サマリ) にも必ず載せる** — 黙って捨てるとペルソナは
    「組み替えた」つもりのまま一日を続け、就寝判断が実態とズレた総括をする
    (接地原則違反。2026-07-05 実 LLM シム 3回目で実証)。
    """
    rt = output.get("remaining_timetable")
    if isinstance(rt, list):
        plan_date = ctx.get("plan_date") or clock.now().date().isoformat()
        if not rt:
            # 空配列は「残りを全部無くす」とも読めるが、空の時間割は保存できない
            # (最低 1 コマ要件) ため変更なし扱いにする。
            warnings.append(
                "remaining_timetable が空配列のため、時間割は変更しません"
            )
            lines.append(
                "（時間割の変更は適用されませんでした: 空の時間割には"
                "置き換えられません。今日の残りのコマは元のままです）"
            )
        else:
            slots, rt_warnings = sanitize_timetable(manager, persona_id, rt)
            warnings.extend(rt_warnings)
            if not slots:
                warnings.append(
                    "remaining_timetable が検証後に空になったため、時間割は変更しません"
                )
                lines.append(
                    "（時間割の変更は適用されませんでした: 提出されたコマが"
                    "すべて無効でした。今日の残りのコマは元のままです）"
                )
            else:
                try:
                    pushed = day_plan_mod.replace_remaining_slots(
                        manager, persona_id, plan_date, slots
                    )
                except ValueError as exc:
                    warnings.append(
                        f"remaining_timetable の置換に失敗 (時間割は不変): {exc}"
                    )
                    lines.append(
                        f"（時間割の変更は適用されませんでした（{exc}）。"
                        "今日の残りのコマは元のままです）"
                    )
                else:
                    lines.append(
                        f"（残りの時間割を組み替えた: {len(slots)} コマ、{pushed} コマを予約）"
                    )
                    dropped = len(rt) - len(slots)
                    if dropped > 0:
                        lines.append(
                            f"（うち {dropped} コマは無効のため除外されました）"
                        )
                    return True
    elif rt is not None:
        warnings.append(
            f"remaining_timetable rejected: 配列または null が必要 (got {type(rt).__name__})"
        )
    return False


# ---------------------------------------------------------------------------
# post_session
# ---------------------------------------------------------------------------


def _apply_task_verdict(
    manager: Any,
    persona_id: str,
    output: Dict[str, Any],
    ctx: Dict[str, Any],
    lines: List[str],
    warnings: List[str],
) -> bool:
    """task_verdict の適用。タスク完了 or 作業メモ保存があれば True。"""
    verdict = output.get("task_verdict")
    if not isinstance(verdict, dict):
        return False
    task_ref = ctx.get("task_ref")
    if not task_ref:
        warnings.append("task_verdict がありますが対象タスクが不明のため棄却")
        return False

    status = verdict.get("status")
    desk_memo = str(verdict.get("desk_memo") or "").strip()
    session_artifacts = [str(a) for a in (ctx.get("artifacts") or [])]

    ptm = PersonaTaskManager(manager.SessionLocal)
    try:
        task_id = ptm.resolve_task_ref(persona_id, normalize_task_ref(str(task_ref)))
        task = ptm.get_task(task_id, persona_id=persona_id)
    except TaskNotFoundError:
        warnings.append(f"task_verdict rejected: タスク {task_ref!r} が見つかりません")
        return False

    # 終了済みタスクへの再裁定は棄却する (二重ガード — スキーマ側でも終了済み
    # タスクには裁定欄を出さない: judgment_points.build_post_session_schema)。
    # 再 done を通すと artifact_refs への多重追記・completed_at の上書きが起きる
    # (2026-07-05 実 LLM シム 3回目 異常③)。desk_memo の保存もしない —
    # 終了済みタスクを「中断中の作業」に見せてしまうため。
    current_status = task.get("status")
    if current_status in (STATUS_COMPLETED, STATUS_CANCELLED):
        warnings.append(
            f"task_verdict rejected: タスク {task_ref} は既に {current_status} です"
            " (再裁定はできません)"
        )
        return False

    applied = False
    if status == "done":
        artifact_ref = str(verdict.get("artifact_ref") or "")
        if artifact_ref and artifact_ref in session_artifacts:
            # 接地検証 OK: 完了 + 成果物参照をタスクに刻む
            ptm.update_task_status(
                task_id, status=STATUS_COMPLETED, actor="judgment_post_session",
                persona_id=persona_id,
                reason=f"session verdict: done (artifact={artifact_ref})",
            )
            ptm.append_artifact_ref(
                task_id, artifact_ref,
                persona_id=persona_id, actor="judgment_post_session",
            )
            lines.append(
                f"（タスク {task_ref} を完了にした。成果物: {artifact_ref}）"
            )
            applied = True
        else:
            # やったフリの棄却: 成果物リストに無い ref は完了させない。
            # continue 相当に降格 (タスクは動かさず、作業メモだけ残す)。
            warnings.append(
                f"task_verdict 'done' rejected: artifact_ref={artifact_ref!r} は"
                "このセッションの成果物リストにありません (タスクは完了させません)"
            )
            status = "continue"

    if status in ("continue", "blocked"):
        memo_label = "詰まり" if status == "blocked" else "続き"
        if desk_memo:
            lines.append(f"（作業メモ [{memo_label}]: {desk_memo}）")
        track_id = ctx.get("track_id")
        if track_id:
            memo = {
                "text": desk_memo,
                "status": status,
                "task_ref": str(task_ref),
                "updated_at": clock.now().isoformat(timespec="seconds"),
            }
            try:
                if save_desk_memo(manager, str(track_id), memo):
                    applied = True
                else:
                    warnings.append(
                        f"desk_memo の保存先 Track {track_id!r} が見つかりません"
                    )
            except Exception as exc:
                LOGGER.exception("[judgment_finalize] save_desk_memo raised")
                warnings.append(f"desk_memo の保存に失敗: {exc}")
        else:
            warnings.append(
                "desk_memo の保存先 Track が不明のため、独白記録にのみ残します"
            )
    elif status not in ("done",):
        warnings.append(f"task_verdict rejected: 未知の status={status!r}")

    return applied


def _finalize_post_session(
    manager: Any,
    persona_id: str,
    output: Dict[str, Any],
    ctx: Dict[str, Any],
    lines: List[str],
    warnings: List[str],
    spells_record: List[Dict[str, Any]],
) -> bool:
    """セッション終了判断の適用。何か 1 つでも適用したら True (committed)。"""
    applied = _apply_task_verdict(manager, persona_id, output, ctx, lines, warnings)

    # --- track_op: complete は Track の全タスク消化済みのときのみ --------
    track_op = output.get("track_op")
    track_id = ctx.get("track_id")
    if track_op == "complete":
        if not track_id:
            warnings.append("track_op 'complete' rejected: 対象 Track が不明です")
        else:
            ptm = PersonaTaskManager(manager.SessionLocal)
            open_tasks = [
                t for t in ptm.get_track_tasks(str(track_id)) if not t.get("done")
            ]
            if open_tasks:
                refs = ", ".join(t.get("task_ref") or "task:?" for t in open_tasks)
                warnings.append(
                    f"track_op 'complete' rejected: 未消化のタスクが残っています ({refs})"
                )
            else:
                _fire_spell("track_complete", {"track_id": str(track_id)}, spells_record)
                applied = True
    elif track_op not in (None, "none"):
        warnings.append(f"track_op rejected: 未知の値 {track_op!r}")

    # --- new_desires → purpose_seed (type/source 付き; v2 §5.2) ----------
    applied |= _apply_new_desires(output, warnings, spells_record)

    # --- episode_purposes → 層2 棚入れタグ (§9.1) -------------------------
    applied |= _apply_episode_purposes_single(
        manager, persona_id, output, ctx, lines, warnings,
    )

    # --- remaining_timetable: null=変更なし / 配列=残りコマの全置換 ------
    applied |= _apply_remaining_timetable(
        manager, persona_id, output, ctx, lines, warnings,
    )

    return applied


# ---------------------------------------------------------------------------
# post_conversation
# ---------------------------------------------------------------------------


def _create_picked_task(
    manager: Any,
    persona_id: str,
    ptm: PersonaTaskManager,
    title: str,
    track_ref: str,
    origin_quote: str,
    valid_track_refs: set,
    lines: List[str],
    warnings: List[str],
) -> bool:
    """picked_tasks 1 件の適用 (タスク作成。track_ref='new' は新規 Track も)。"""
    track_manager = getattr(manager, "track_manager", None)
    track_id: Optional[str] = None
    dest_label = "(未所属)"

    if track_ref == "new":
        # 新しい関心として Track を立てる (track_create 相当。unstarted のまま —
        # 稼働させるかは以後のメタ判断/起床判断に委ねる)
        if track_manager is None:
            warnings.append(
                f"picked_task「{title}」: track_manager が無いため Track を作れません; "
                "未所属タスクとして作成します"
            )
        else:
            try:
                try:
                    # 通常経路: tools ローダが builtin tools ディレクトリを
                    # sys.path に載せている (tools/__init__.py)。
                    from _track_common import resolve_default_entry_line_role
                    entry_line_role = resolve_default_entry_line_role("autonomous")
                except ImportError:
                    # 単体ロード (テスト等) では _track_common が見えない。
                    # resolve_default_entry_line_role 自身の unknown fallback と
                    # 同じ安全側 (= main_line) に倒す。
                    entry_line_role = "main_line"
                track_id = track_manager.create(
                    persona_id=persona_id,
                    track_type="autonomous",
                    title=title,
                    intent=f"会話からの持ち帰り。根拠: {origin_quote}",
                    metadata=json.dumps(
                        {"entry_line_role": entry_line_role}, ensure_ascii=False,
                    ),
                )
                created = track_manager.get(track_id)
                short = (
                    f"track:{created.short_id}" if created.short_id is not None
                    else track_id[:8]
                )
                dest_label = f"新しい関心「{title}」({short})"
            except Exception as exc:
                LOGGER.exception(
                    "[judgment_finalize] track create for picked_task failed"
                )
                warnings.append(
                    f"picked_task「{title}」rejected: Track の作成に失敗: {exc}"
                )
                return False
    else:
        if track_ref not in valid_track_refs:
            warnings.append(
                f"picked_task「{title}」rejected: track_ref={track_ref!r} は"
                "選択可能な Track にありません"
            )
            return False
        if track_manager is None:
            warnings.append(
                f"picked_task「{title}」rejected: track_manager が無いため "
                f"{track_ref!r} を解決できません"
            )
            return False
        try:
            track_id = track_manager.resolve_track_ref(persona_id, track_ref)
        except Exception as exc:
            warnings.append(
                f"picked_task「{title}」rejected: track_ref={track_ref!r} が"
                f"解決できません: {exc}"
            )
            return False
        dest_label = track_ref

    task = ptm.create_task(
        persona_id=persona_id,
        title=title,
        # origin_quote は接地の証跡としてタスクの自由記述 (notes) に残す
        notes=origin_quote,
        parent_kind=PARENT_TRACK if track_id else None,
        track_id=track_id,
        auto_activate=False,
        actor="judgment_post_conversation",
    )
    lines.append(
        f"（会話からタスクを拾った: {task.get('task_ref')}「{title}」→ {dest_label}）"
    )
    return True


def _apply_resume_now(
    manager: Any,
    persona_id: str,
    plan_date: str,
    resume_ctx: Dict[str, Any],
    lines: List[str],
    warnings: List[str],
) -> bool:
    """resume_session='resume_now' の適用。

    **妥協点** (judgment_points.md §5): セッションの凍結コンテキストを復元する
    「再開機構」は未実装のため、resume_now は「凍結済み作業メモ付きタスクを参照
    する『作る/知る』コマの現在時刻への即時挿入」で表現する。kind / facility /
    予算は今日の時間割で同じタスクを指していた元コマから引き継ぐ (無ければ
    「作る」/ own_room / 既定予算)。挿入されたコマは即時発火し、作業メモは
    セッション指示書の参照先 (Track metadata) に残ったまま新セッションが走る。
    """
    task_ref = str(resume_ctx.get("task_ref") or "").strip()
    if not task_ref:
        warnings.append("resume_now rejected: 再開対象の task_ref が不明です")
        return False

    kind = day_plan_mod.KIND_CREATE
    facility = day_plan_mod.FACILITY_OWN_ROOM
    budget = day_plan_mod.DEFAULT_BUDGET_ROUNDS
    norm = normalize_task_ref(task_ref)
    for s in day_plan_mod.load_day_plan(manager, persona_id, plan_date) or []:
        if normalize_task_ref(str(s.get("ref") or "")) == norm:
            if s.get("kind") == day_plan_mod.KIND_LEARN:
                kind = day_plan_mod.KIND_LEARN
            facility = s.get("facility") or facility
            if int(s.get("budget_rounds") or 0) > 0:
                budget = int(s["budget_rounds"])
            break

    memo_text = str(resume_ctx.get("text") or "").strip()
    note = "中断していた作業の再開"
    if memo_text:
        note += f"（作業メモ: {memo_text[:60]}）"
    now_hhmm = clock.now().strftime("%H:%M")
    slot = {
        "start": now_hhmm,
        "kind": kind,
        "title": "中断していた作業を再開する",
        "ref": task_ref,
        "facility": facility,
        "budget_rounds": budget,
        "note": note,
    }
    pushed, insert_warnings = insert_timetable_slot(
        manager, persona_id, plan_date, slot,
    )
    warnings.extend(insert_warnings)
    if pushed is None:
        return False
    lines.append(
        f"（中断していた作業 {task_ref} を今すぐ再開する: {slot['start']} の"
        f"「{kind}」コマとして挿入）"
    )
    return True


def _finalize_post_conversation(
    manager: Any,
    persona_id: str,
    output: Dict[str, Any],
    ctx: Dict[str, Any],
    lines: List[str],
    warnings: List[str],
    spells_record: List[Dict[str, Any]],
) -> bool:
    """会話終了判断の適用。何か 1 つでも適用したら True (committed)。

    収穫ゼロ (picked_tasks / new_desires が空 + timetable 変更なし +
    resume なし) は正常系 — 全会話がタスクを生むわけではない (§5)。
    """
    applied = False
    plan_date = ctx.get("plan_date") or clock.now().date().isoformat()
    valid_track_refs = set(ctx.get("track_refs") or [])
    ptm = PersonaTaskManager(manager.SessionLocal)

    # --- picked_tasks → タスク作成 (origin_quote 必須の接地) --------------
    for i, picked in enumerate(output.get("picked_tasks") or []):
        if not isinstance(picked, dict):
            warnings.append(f"picked_tasks[{i}] rejected: not a dict")
            continue
        title = str(picked.get("title") or "").strip()
        track_ref = str(picked.get("track_ref") or "").strip()
        origin_quote = str(picked.get("origin_quote") or "").strip()
        if not title:
            warnings.append(f"picked_tasks[{i}] rejected: title が空です")
            continue
        if not origin_quote:
            # 無根拠のタスク発生をここで塞ぐ (§5 — 接地の証跡)
            warnings.append(
                f"picked_tasks[{i}]「{title}」rejected: origin_quote (根拠となる"
                "会話中の発言の引用) がありません"
            )
            continue
        try:
            applied |= _create_picked_task(
                manager, persona_id, ptm, title, track_ref, origin_quote,
                valid_track_refs, lines, warnings,
            )
        except Exception as exc:
            LOGGER.exception("[judgment_finalize] picked_task creation raised")
            warnings.append(f"picked_tasks[{i}]「{title}」の作成に失敗: {exc}")

    # --- new_desires → purpose_seed --------------------------------------
    applied |= _apply_new_desires(output, warnings, spells_record)

    # --- episode_purposes → 層2 棚入れタグ (§9.1) -------------------------
    applied |= _apply_episode_purposes_single(
        manager, persona_id, output, ctx, lines, warnings,
    )

    # --- remaining_timetable: 全置換を先に適用してから resume_now を挿入 --
    applied |= _apply_remaining_timetable(
        manager, persona_id, output, ctx, lines, warnings,
    )

    # --- resume_session (中断中セッションがあったときのみ ctx に resume) --
    resume_choice = output.get("resume_session")
    resume_ctx = ctx.get("resume")
    if resume_choice is not None:
        if not isinstance(resume_ctx, dict) or not resume_ctx:
            warnings.append(
                "resume_session rejected: 中断中セッションがありません"
            )
        elif resume_choice == RESUME_NOW:
            applied |= _apply_resume_now(
                manager, persona_id, plan_date, resume_ctx, lines, warnings,
            )
        elif resume_choice == RESUME_DEFER:
            # 時間割側 (remaining_timetable) に委ねる。状態は変えない。
            lines.append("（中断中の作業は残りの時間割の中で再開する）")
        elif resume_choice == RESUME_DROP:
            track_id = str(resume_ctx.get("track_id") or "")
            try:
                if track_id and clear_desk_memo(manager, track_id):
                    applied = True
                    lines.append("（中断中の作業は取りやめ、作業メモを片づけた）")
                else:
                    warnings.append(
                        f"resume_session 'drop': 作業メモの削除先 Track "
                        f"{track_id!r} が見つかりません"
                    )
            except Exception as exc:
                LOGGER.exception("[judgment_finalize] clear_desk_memo raised")
                warnings.append(f"作業メモの片づけに失敗: {exc}")
        else:
            warnings.append(
                f"resume_session rejected: 未知の値 {resume_choice!r}"
            )

    return applied


# ---------------------------------------------------------------------------
# on_event
# ---------------------------------------------------------------------------


def _append_event_memo(
    manager: Any,
    persona_id: str,
    plan_date: str,
    memo_text: str,
    event_text: Optional[str],
) -> None:
    """note_only の覚え書きを plan meta (``event_memos`` 配列) に積む。

    作業メモ (Track metadata) 様式の記録先だが、イベントは Track に属さないため
    「その日」の付帯情報 (persona_day_plan.meta_json) を置き場にする。
    """
    meta = day_plan_mod.load_plan_meta(manager, persona_id, plan_date)
    memos = meta.get("event_memos")
    memos = list(memos) if isinstance(memos, list) else []
    memos.append({
        "text": memo_text,
        "event": str(event_text or "")[:120],
        "at": clock.now().isoformat(timespec="seconds"),
    })
    day_plan_mod.update_plan_meta(
        manager, persona_id, plan_date, {"event_memos": memos},
    )


def _finalize_on_event(
    manager: Any,
    persona_id: str,
    output: Dict[str, Any],
    ctx: Dict[str, Any],
    lines: List[str],
    warnings: List[str],
    spells_record: List[Dict[str, Any]],
    summary_extras: List[str],
) -> bool:
    """イベント到着判断の適用。

    engage_now は状態を変えない — 「今すぐ応対する」の実行 (Pulse 起動 /
    Track alert 処理) は呼び出し側の責務で、ここでは判断結果を要約
    (summary_extras) と記録テキストに反映するのみ (本番の応対起動は
    saiverse/autonomy_wiring.py の handle_external_event が judgment_applied
    イベント経由で reaction を読んで行う)。
    """
    applied = False
    plan_date = ctx.get("plan_date") or clock.now().date().isoformat()
    is_alert = bool(ctx.get("is_alert"))

    reaction = output.get("reaction")
    rtype = reaction.get("type") if isinstance(reaction, dict) else None
    if not isinstance(reaction, dict) or not rtype:
        warnings.append(
            f"reaction rejected: type を持つ object が必要 (got {reaction!r})"
        )
    elif is_alert and rtype != REACTION_ENGAGE_NOW:
        # スキーマ縮退 (engage_now のみ) の二重ガード
        warnings.append(
            f"reaction rejected: alert イベントでは engage_now のみ選べます "
            f"(got {rtype!r})"
        )
    elif rtype == REACTION_ENGAGE_NOW:
        summary_extras.append("reaction=engage_now")
        lines.append("（このイベントに今すぐ応対する）")
        applied = True
    elif rtype == REACTION_INSERT_SLOT:
        summary_extras.append("reaction=insert_slot")
        slot = reaction.get("slot")
        if not isinstance(slot, dict):
            warnings.append("insert_slot rejected: slot (object) がありません")
        else:
            pushed, insert_warnings = insert_timetable_slot(
                manager, persona_id, plan_date, slot,
                not_before=clock.now().strftime("%H:%M"),
            )
            warnings.extend(insert_warnings)
            if pushed is not None:
                applied = True
                lines.append(
                    f"（このイベントのためのコマを時間割へ挿入: "
                    f"{slot.get('start')} {slot.get('kind')}）"
                )
    elif rtype == REACTION_NOTE_ONLY:
        summary_extras.append("reaction=note_only")
        memo_text = str(reaction.get("memo") or "").strip()
        if not memo_text:
            warnings.append("note_only rejected: memo が空です")
        else:
            try:
                _append_event_memo(
                    manager, persona_id, plan_date, memo_text,
                    ctx.get("event_text"),
                )
                applied = True
                lines.append(f"（覚え書きに留める: {memo_text}）")
            except Exception as exc:
                LOGGER.exception("[judgment_finalize] event memo append raised")
                warnings.append(f"覚え書きの保存に失敗: {exc}")
    elif rtype == REACTION_IGNORE:
        summary_extras.append("reaction=ignore")
        lines.append("（このイベントには反応しない）")
    else:
        warnings.append(f"reaction rejected: 未知の type {rtype!r}")

    applied |= _apply_new_desires(output, warnings, spells_record)
    return applied


# ---------------------------------------------------------------------------
# curation_reviews (P4-a 編纂裁定)
# ---------------------------------------------------------------------------


def _apply_curation_reviews(
    manager: Any,
    persona_id: str,
    output: Dict[str, Any],
    ctx: Dict[str, Any],
    lines: List[str],
    warnings: List[str],
) -> bool:
    """curation_reviews → approve 分を編纂プランとして永続化する (P4-a)。

    - approve: ペルソナの memory.db の curation_plans テーブルに pending 行を積む。
      同じ op_id の pending が既にあれば重複挿入しない（冪等）。
    - skip: 何もしない（条件が続けば翌日以降に再提示）。

    **実際の分割・統合（書き換え）は P4-a2 の curation_ops.py で実装する。
    このモジュールは「プランを積む」だけで実行しない。**
    """
    raw = output.get("curation_reviews")
    if not raw:
        return False
    if not isinstance(raw, list):
        warnings.append(
            f"curation_reviews rejected: 配列が必要 (got {type(raw).__name__})"
        )
        return False

    # ctx に格納された候補リストから有効な op_id を収集
    candidates = ctx.get("curation_candidates") or []
    valid_ops: Dict[str, Any] = {}  # op_id → candidate dict
    for c in candidates:
        oid = c.get("op_id")
        if oid:
            valid_ops[oid] = c

    if not valid_ops:
        warnings.append(
            "curation_reviews: 候補が不明のため適用できません (judgment_context に "
            "curation_candidates がありません)"
        )
        return False

    # adapter / conn の取得
    persona_obj = (getattr(manager, "personas", None) or {}).get(persona_id)
    adapter = getattr(persona_obj, "sai_memory", None) if persona_obj else None
    mem_conn = getattr(adapter, "conn", None) if adapter else None
    if mem_conn is None:
        warnings.append(
            "curation_reviews: 記憶アダプタが利用できないため編纂プランを保存できません"
        )
        return False

    from sai_memory.curation_ops import enqueue_plan

    applied = False
    for i, review in enumerate(raw):
        if not isinstance(review, dict):
            warnings.append(f"curation_reviews[{i}] rejected: not a dict")
            continue
        op_id = str(review.get("op_id") or "").strip()
        verdict = str(review.get("verdict") or "").strip()
        if op_id not in valid_ops:
            warnings.append(
                f"curation_reviews[{i}] rejected: op_id={op_id!r} は"
                "今日の棚の乱れ候補にありません"
            )
            continue
        if verdict not in ("approve", "skip"):
            warnings.append(
                f"curation_reviews[{i}] rejected: verdict={verdict!r} は"
                " approve または skip が必要です"
            )
            continue
        if verdict == "skip":
            # skip は何もしない（翌日再提示）
            LOGGER.debug(
                "[judgment_finalize] curation_reviews: op_id=%r skipped (persona=%s)",
                op_id, persona_id,
            )
            continue

        # approve → プランをキューに積む
        cand = valid_ops[op_id]
        try:
            plan_id = enqueue_plan(
                conn=mem_conn,
                kind=cand.get("kind", "split"),
                op_id=op_id,
                refs=list(cand.get("refs") or []),
            )
            applied = True
            lines.append(
                f"（棚の整理を予定に入れた: {op_id}）"
            )
            LOGGER.info(
                "[judgment_finalize] curation_reviews: op_id=%r enqueued "
                "as plan_id=%s (persona=%s)",
                op_id, plan_id, persona_id,
            )
        except Exception as exc:
            LOGGER.exception(
                "[judgment_finalize] curation_reviews: enqueue_plan raised "
                "(op_id=%r, persona=%s)", op_id, persona_id,
            )
            warnings.append(
                f"curation_reviews[{i}] の編纂プラン保存に失敗: {exc}"
            )

    return applied


# ---------------------------------------------------------------------------
# day_close
# ---------------------------------------------------------------------------


def _finalize_day_close(
    manager: Any,
    persona_id: str,
    output: Dict[str, Any],
    ctx: Dict[str, Any],
    lines: List[str],
    warnings: List[str],
) -> bool:
    """就寝判断の適用。

    - tomorrow_memo / day_theme / user_report_seeds → 当日 plan 行の meta_json
      (翌朝の day_open は「昨日 = この plan_date」の meta を読む —
      ``build_day_open_situation_text`` と対になる読み書き)
    - day_digest: LLM 出力ではなく **実績の決定論要約**
      (:func:`saiverse.judgment_points.build_day_results_text`) を保存する。
      翌朝 day_open の「昨日のふりかえり」が最優先で読むフォールバック元
    - desire_reviews → apply_desire_reviews (今日触れた欲求のみ。enum 外は棄却)
    """
    applied = False
    plan_date = ctx.get("plan_date") or clock.now().date().isoformat()

    updates: Dict[str, Any] = {}
    tomorrow_memo = str(output.get("tomorrow_memo") or "").strip()
    if tomorrow_memo:
        updates["tomorrow_memo"] = tomorrow_memo
    else:
        warnings.append(
            "tomorrow_memo が空です (明日の起床判断はメモなしで始まります)"
        )
    day_theme = str(output.get("day_theme") or "").strip()
    if day_theme:
        updates["day_theme"] = day_theme

    seeds_raw = output.get("user_report_seeds")
    seeds: List[str] = []
    if isinstance(seeds_raw, list):
        for i, seed in enumerate(seeds_raw):
            if isinstance(seed, str) and seed.strip():
                seeds.append(seed.strip())
            else:
                warnings.append(
                    f"user_report_seeds[{i}] rejected: 空でない文字列が必要"
                )
        if len(seeds) > 3:
            warnings.append(
                f"user_report_seeds は最大 3 件 (got {len(seeds)}); 先頭 3 件のみ保存"
            )
            seeds = seeds[:3]
    elif seeds_raw is not None:
        warnings.append(
            f"user_report_seeds rejected: 配列が必要 (got {type(seeds_raw).__name__})"
        )
    if seeds:
        updates["user_report_seeds"] = seeds

    # day_digest は実績からの決定論構築 (接地保証。虚構のふりかえりが翌朝に残らない)
    updates["day_digest"] = build_day_results_text(manager, persona_id, plan_date)

    try:
        day_plan_mod.update_plan_meta(manager, persona_id, plan_date, updates)
        applied = True
        lines.append("（今日のふりかえりを記録した）")
        if tomorrow_memo:
            lines.append(f"（明日の自分へのメモ: {tomorrow_memo}）")
        if day_theme:
            lines.append(f"（今日のテーマ: {day_theme}）")
        if seeds:
            lines.append("（ユーザーに話したいこと: " + " / ".join(seeds) + "）")
    except Exception as exc:
        LOGGER.exception("[judgment_finalize] update_plan_meta raised")
        warnings.append(f"ふりかえりの保存に失敗: {exc}")

    # --- desire_reviews → apply_desire_reviews (今日触れた欲求のみ) -------
    valid_refs = set(ctx.get("touched_desire_refs") or [])
    reviews: List[Dict[str, Any]] = []
    for i, review in enumerate(output.get("desire_reviews") or []):
        if not isinstance(review, dict):
            warnings.append(f"desire_reviews[{i}] rejected: not a dict")
            continue
        ref = str(review.get("desire_ref") or "")
        if ref not in valid_refs:
            warnings.append(
                f"desire_reviews[{i}] rejected: {ref!r} は今日触れた欲求にありません"
            )
            continue
        reviews.append({"desire_ref": ref, "verdict": review.get("verdict")})
    if reviews:
        result = apply_desire_reviews(manager, persona_id, reviews)
        for skipped_ref in result.get("skipped") or []:
            warnings.append(f"desire_review を適用できませんでした: {skipped_ref}")
        n_fulfilled = len(result.get("fulfilled") or [])
        n_fading = len(result.get("fading") or [])
        n_kept = len(result.get("kept") or [])
        if n_fulfilled or n_fading or n_kept:
            applied = True
            lines.append(
                f"（やりたいことのたな卸し: 満たされた {n_fulfilled} / "
                f"薄れていく {n_fading} / 持ち続ける {n_kept}）"
            )

    # --- episode_purposes → 層2 棚入れタグ ({episode, purpose} ペア; §9.1) --
    applied |= _apply_episode_purposes_pairs(
        manager, persona_id, output, ctx, lines, warnings,
    )

    # --- curation_reviews → 承認分を編纂プランとして永続化 (P4-a) ----------
    applied |= _apply_curation_reviews(
        manager, persona_id, output, ctx, lines, warnings,
    )

    # --- curation バッチ起動 (裁定 (c): 就寝判断適用直後の背景ジョブ) --------
    # pending プランがあれば背景スレッドで即実行する。
    # スレッド起動前に必要な依存（manager, persona_id）は全て構築済み。
    _maybe_launch_curation_batch(manager, persona_id)

    return applied


def _maybe_launch_curation_batch(manager: Any, persona_id: str) -> None:
    """pending の編纂プランがあれば背景スレッドで run_pending_plans を起動する。

    - daemon スレッドとして起動するので本体の終了を妨げない。
    - 依存（manager, persona_id）はスレッド起動前にここで確認し、問題があれば
      起動しない——スレッド内での None アクセスを防ぐ。
    - 既存の背景スレッド起動流儀（event_scheduler / integration_manager）に倣い
      threading.Thread(daemon=True) を使う。
    """
    import threading

    try:
        persona = (getattr(manager, "personas", None) or {}).get(persona_id)
        if persona is None:
            return
        adapter = getattr(persona, "sai_memory", None)
        mem_conn = getattr(adapter, "conn", None) if adapter is not None else None
        if mem_conn is None:
            return

        from sai_memory.curation_ops import list_pending
        pending = list_pending(mem_conn)
        if not pending:
            LOGGER.debug(
                "[judgment_finalize] curation_batch: no pending plans, skip (persona=%s)",
                persona_id,
            )
            return

        LOGGER.info(
            "[judgment_finalize] curation_batch: launching background thread "
            "(persona=%s pending=%d)",
            persona_id, len(pending),
        )

        from sai_memory.curation_ops import run_pending_plans

        def _run() -> None:
            try:
                run_pending_plans(manager, persona_id)
            except Exception:
                LOGGER.warning(
                    "[judgment_finalize] curation_batch: background run raised",
                    exc_info=True,
                )

        t = threading.Thread(
            target=_run,
            name=f"CurationBatch-{persona_id[:8]}",
            daemon=True,
        )
        t.start()

    except Exception:
        LOGGER.warning(
            "[judgment_finalize] _maybe_launch_curation_batch raised",
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------


def judgment_finalize(
    judgment_output: Optional[Dict[str, Any]] = None,
    kind: str = "",
    judgment_context: str = "",
    situation_text: str = "",
) -> Tuple[str, ToolResult, None]:
    """Finalize a judgment-point turn (see module docstring)."""
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("judgment_finalize requires an active persona context")
    manager = get_active_manager()
    if manager is None:
        raise RuntimeError("judgment_finalize requires an active manager context")

    output = judgment_output if isinstance(judgment_output, dict) else {}
    monologue = (output.get("monologue") or "").strip()
    try:
        ctx = json.loads(judgment_context) if judgment_context else {}
    except (TypeError, ValueError):
        LOGGER.warning(
            "[judgment_finalize] judgment_context is not valid JSON: %r",
            judgment_context,
        )
        ctx = {}
    if not isinstance(ctx, dict):
        ctx = {}

    lines: List[str] = []
    warnings: List[str] = []
    spells_record: List[Dict[str, Any]] = []
    summary_extras: List[str] = []

    if kind == KIND_DAY_OPEN:
        committed = _finalize_day_open(
            manager, persona_id, output, ctx, lines, warnings, spells_record,
        )
    elif kind == KIND_POST_CONVERSATION:
        committed = _finalize_post_conversation(
            manager, persona_id, output, ctx, lines, warnings, spells_record,
        )
    elif kind == KIND_POST_SESSION:
        committed = _finalize_post_session(
            manager, persona_id, output, ctx, lines, warnings, spells_record,
        )
    elif kind == KIND_ON_EVENT:
        committed = _finalize_on_event(
            manager, persona_id, output, ctx, lines, warnings, spells_record,
            summary_extras,
        )
    elif kind == KIND_DAY_CLOSE:
        committed = _finalize_day_close(
            manager, persona_id, output, ctx, lines, warnings,
        )
    else:
        LOGGER.warning("[judgment_finalize] unknown kind=%r; nothing applied", kind)
        warnings.append(f"unknown judgment kind: {kind!r}")
        committed = False

    for w in warnings:
        LOGGER.warning("[judgment_finalize] (%s/%s) %s", persona_id, kind, w)

    # --- 整形済みテキスト (JSON 非混入; 不変条件 v2-A) --------------------
    spell_lines = [_spell_to_text(s["name"], s["args"]) for s in spells_record]
    body_parts = [p for p in (monologue, "\n".join(lines), "\n".join(spell_lines)) if p]
    final_text = "\n\n".join(body_parts) or "(empty judgment)"
    scope = "committed" if committed or spells_record else "discardable"

    # --- SAIMemory への保存 (meta_judgment_finalize と同形式) --------------
    persona = (getattr(manager, "personas", None) or {}).get(persona_id)
    if persona is not None:
        adapter = getattr(persona, "sai_memory", None)
        if adapter is not None:
            pulse_ctx = get_active_pulse_context()
            pulse_id = getattr(pulse_ctx, "pulse_id", None) if pulse_ctx else None
            try:
                adapter.append_persona_message({
                    "role": "assistant",
                    "content": final_text,
                    # tz-aware UTC ISO で渡す (naive ISO は adapter 側で ±9h ずれる。
                    # docs/issues/history_manager_timestamp_tz_drift.md と同根)。
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    # metadata.judgment: 独白本文を適用エコー行と構造的に分離して
                    # 持つ。一日新聞 (saiverse/day_report.py) が「就寝のふりかえり」
                    # に独白だけを載せるための読み口 (content は従来どおり
                    # 独白+要約行の全文 — ペルソナの文脈に乗る内容は変えない)。
                    "metadata": {
                        "tags": ["meta_judgment", f"judgment:{kind}"],
                        "judgment": {"kind": kind, "monologue": monologue},
                    },
                    "line_role": "meta_judgment",
                    "scope": scope,
                    "pulse_id": pulse_id,
                    # 判断時に渡された状況テキストを Pulse タイムラインで見えるようにする。
                    "paired_action_text": situation_text.strip() or None,
                })
            except Exception:
                LOGGER.exception(
                    "[judgment_finalize] Failed to append persona message"
                )

    summary = (
        f"Judgment finalized (kind={kind}, applied={committed}, "
        f"spells={len(spells_record)}, warnings={len(warnings)}, scope={scope})"
    )
    if summary_extras:
        # on_event の reaction 等、呼び出し側が読む判断結果。
        summary += " [" + ", ".join(summary_extras) + "]"

    # 適用結果を Pulse の event_callback へ通知する (best-effort)。
    # run_judgment_point (saiverse/judgment_points.py) がこれを捕捉して
    # applied_events として呼び出し側へ返す — on_event の engage_now で
    # 応対 Pulse を起動するか等を、本番配線 (saiverse/autonomy_wiring.py) が
    # 判断結果に基づいて選ぶための唯一の戻り経路。
    try:
        from tools.context import get_event_callback

        _cb = get_event_callback()
        if callable(_cb):
            _cb({
                "type": "judgment_applied",
                "kind": kind,
                "applied": committed,
                "extras": list(summary_extras),
            })
    except Exception:
        LOGGER.debug(
            "[judgment_finalize] failed to emit judgment_applied event",
            exc_info=True,
        )
    return summary, ToolResult(history_snippet=summary), None


def schema() -> ToolSchema:
    return ToolSchema(
        name="judgment_finalize",
        description=(
            "Internal tool for judgment-point Playbooks only (judgment_day_open / "
            "judgment_post_conversation / judgment_post_session / "
            "judgment_on_event / judgment_day_close). Receives the judge node's "
            "structured output (dict), validates and applies it per judgment kind "
            "(day plan save, task verdict with artifact grounding, desk memo, "
            "promotions, picked tasks with origin-quote grounding, event "
            "reactions, day-close review, new desires), and persists the "
            "resulting monologue + summary text to SAIMemory. Invalid items are "
            "rejected individually with warnings."
        ),
        parameters={
            "type": "object",
            "properties": {
                "judgment_output": {"type": "object"},
                "kind": {"type": "string"},
                "judgment_context": {"type": "string"},
                "situation_text": {"type": "string"},
            },
            "required": ["judgment_output", "kind"],
        },
        result_type="string",
        spell=False,
    )
