"""コマ締めの一手 — 帰属判定 + 経験値ノート (時間割改修 T4)。

作業セッション系コマ (execution_type='work_session': 調べる/絵を描く/日記を書く/
随筆を書く) のセッション終了直後に、**同一の構造化出力コール 1 発**で二つを
記録する (timetable_redesign.md §5.4 / experience_ledger.md §4-§6 二家系の
「節目の本人記録」):

1. **帰属 (belongs_to)**: このコマの経験は結局どの関心・タスク・欲求に属したか。
   事前 ref (slot.ref) は参考情報としてプロンプトに出すが、判断は実績ベース。
   結果は purpose_tags 棚 (層2 棚入れ、recall_tags_and_track_reduction.md §9.4
   の帰属契約) へ ``episode (work_session) → belongs_to`` で載る。
2. **経験値ノート (note)**: 「後の自分のために今の自分が得たものを残す」自由文。
   コマ種別 (kind) に対応するテーマページの fragment に書く。ページが無ければ
   この締めで lazy creation (experience_ledger.md §5: 初経験がページを開く)。
   書くことが無ければ空でよい — 空 note は正常応答 (充填の禁忌)。

発火点は :func:`sea.work_session.run_work_session` の ``close_hook`` (Beat
ロック内・セッションの messages と model がそのまま = 直前コールの prefix
cache が熱い。sluice と同じ理由でこの瞬間に置く)。

**v1 スコープ**: 締めコールを持つのは作業セッション系コマのみ。軽い一手コマ
(出かける/自室で過ごす) は Pulse 記録が SAIMemory に残り、あらすじ→関与タグは
代謝側 (entity_extractor B2 相乗り、別途) の担当 — コマごとの LLM コスト倍化を
避けるため、v1 では締めコールを足さない。

失敗の扱い: 締めの一手の失敗 (LLM/パース/保存) はコマの完了自体を壊さない
(WARNING + スキップ)。経験値ノートは油であって燃料ではない。
"""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any, Callable, Dict, List

LOGGER = logging.getLogger(__name__)

#: 帰属の特殊値。"none" = どの目的にも属さない (タグを書かない)。
BELONGS_NONE = "none"
#: "new" = 既存のどれでもない新しい何か。v1 ではログのみ (将来の候補化と連動しうる)。
BELONGS_NEW = "new"

#: テーマページ lazy creation の origin 刻印 (metadata.origin / 編集来歴)。
THEME_PAGE_ORIGIN = "slot_close"

# ---------------------------------------------------------------------------
# 締めの結果 (slot の close_outcome フィールド) — 欠落を沈黙させない (Codex
# 一巡目 #3)。「締めは油であって燃料ではない」(失敗はコマの完了を壊さない) は
# 維持しつつ、何が起きたかは状態として残す。post_session の層2 棚入れは
# 帰属が済んだ結果 (done / note_failed) のときだけ抑止される (Codex 一巡目 #5
# — 同一セッションの二重帰属宣言は revisit_count を偽増加させる)。
# ---------------------------------------------------------------------------

#: 帰属判定を適用し、経験値ノートも処理した (空 note は正常)。
CLOSE_OUTCOME_DONE = "done"
#: 帰属は適用できたがノートの書き込みに失敗した (ノートのみ欠落)。
CLOSE_OUTCOME_NOTE_FAILED = "note_failed"
#: 締めのコール/解析/帰属の適用が失敗した (帰属は post_session が代替しうる)。
CLOSE_OUTCOME_FAILED = "failed"
#: memory.db が使えず締め自体を見送った。
CLOSE_OUTCOME_SKIPPED_NO_MEMORY = "skipped_no_memory"
#: セッションがエラー終了し close_hook が呼ばれなかった (day_plan 側が記録)。
CLOSE_OUTCOME_NOT_RUN_SESSION_ERROR = "not_run_session_error"

#: 「帰属judgment は済んでいる」とみなす結果 (post_session の棚入れ抑止条件)。
ATTRIBUTION_SETTLED_OUTCOMES = (CLOSE_OUTCOME_DONE, CLOSE_OUTCOME_NOTE_FAILED)


def make_close_hook(
    manager: Any, persona_id: str, plan_date_str: str, slot: Dict[str, Any], index: int
) -> Callable[[Any], None]:
    """``run_work_session(close_hook=...)`` に渡す締めフックを作る。

    締めの結果 (close_outcome) は成否を問わず slot に永続化する — 欠落が
    沈黙しないための状態記録で、_handle_worker_slot が post_session の帰属
    抑止判定に読む。
    """

    def _hook(close_ctx: Any) -> None:
        outcome = CLOSE_OUTCOME_FAILED
        try:
            outcome = run_slot_close(
                close_ctx,
                manager=manager,
                persona_id=persona_id,
                plan_date_str=plan_date_str,
                slot=slot,
                index=index,
            )
        finally:
            # プロセス内の第一経路: 結果を hook 属性で直接手渡す (呼び出し側の
            # run_worker_slot_session が result に載せる)。slot への永続化は
            # 再起動を跨ぐ可視性のための第二経路 — CAS 競合等で書けなくても
            # 帰属抑止の判定は in-process 値が担う (Codex 四巡目 #2)。
            _hook.last_outcome = outcome  # type: ignore[attr-defined]
            _persist_close_outcome(
                manager, persona_id, plan_date_str, slot, index, outcome,
            )

    _hook.last_outcome = None  # type: ignore[attr-defined]
    return _hook


def _persist_close_outcome(
    manager: Any,
    persona_id: str,
    plan_date_str: str,
    slot: Dict[str, Any],
    index: int,
    outcome: str,
) -> None:
    """close_outcome を slot に書く (失敗は WARNING — 締めの隔離と同じ扱い)。"""
    try:
        from saiverse import day_plan

        updated = day_plan._update_slot(
            manager, persona_id, plan_date_str, index,
            expected_id=slot.get("id"), close_outcome=outcome,
        )
        if updated is None:
            # CAS 競合 / コマ消失 — マーカーは残らないが、帰属抑止の判定は
            # in-process の手渡し (hook.last_outcome) が担うので二重帰属には
            # ならない。再起動後の可視性だけが欠ける (WARNING で追える)。
            LOGGER.warning(
                "[slot_close] close_outcome=%s not persisted (slot moved or "
                "vanished); in-process handoff still carries it "
                "(persona=%s date=%s index=%d)",
                outcome, persona_id, plan_date_str, index,
            )
    except Exception:
        LOGGER.warning(
            "[slot_close] failed to persist close_outcome=%s "
            "(persona=%s date=%s index=%d)",
            outcome, persona_id, plan_date_str, index, exc_info=True,
        )


# ---------------------------------------------------------------------------
# response_schema (Gemini 制約: additionalProperties 禁止、フラット)
# ---------------------------------------------------------------------------


def _build_response_schema(belongs_to_enum: List[str]) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "belongs_to": {
                "type": "string",
                "enum": list(belongs_to_enum),
                "description": (
                    "このコマの経験が結局属した先 (実績ベース)。どれにも属さな"
                    "ければ 'none'、既存のどれでもない新しい何かなら 'new'。"
                ),
            },
            "note": {
                "type": "string",
                "description": "経験値ノート (自由文)。書くことが無ければ空文字列。",
            },
        },
        "required": ["belongs_to"],
    }


# ---------------------------------------------------------------------------
# 帰属 enum とプロンプト
# ---------------------------------------------------------------------------


def _collect_belongs_to_enum(manager: Any, persona_id: str) -> List[str]:
    """belongs_to の enum: 実在の生きた目的ノード + "none" + "new"。

    集合はコマの ref enum (:func:`saiverse.judgment_points.collect_slot_ref_enum`
    = バックログタスク + 欲求候補 + 生きた Track + "none") と同一供給。head の
    一覧 (PurposeBacklogSection) と同じ供給源なので「読む情報と選べる選択肢の
    一致」(judgment_points の不変条件) がそのまま成り立つ。track:N を含めるのは
    track:N コマ (P5 大枝コマ) の実績が none に落ちないため。
    """
    from saiverse.judgment_points import collect_slot_ref_enum

    refs = list(collect_slot_ref_enum(manager, persona_id))
    refs.append(BELONGS_NEW)
    return refs


def _build_choice_lines(manager: Any, persona_id: str) -> List[str]:
    """帰属先一覧の提示行 (ref + 題)。enum と同じ供給関数から組む。"""
    from saiverse.judgment_points import (
        list_backlog_tasks,
        list_desire_tasks,
        list_pickable_tracks,
    )

    lines: List[str] = []
    try:
        for t in list_backlog_tasks(manager, persona_id):
            ref = t.get("task_ref")
            if ref:
                lines.append(f"- {ref} 「{t.get('title') or '(無題)'}」（タスク）")
        for t in list_desire_tasks(manager, persona_id):
            ref = t.get("task_ref")
            if ref:
                lines.append(f"- {ref} 「{t.get('title') or '(無題)'}」（欲求候補）")
        for tr in list_pickable_tracks(manager, persona_id):
            lines.append(
                f"- track:{tr.short_id} 「{getattr(tr, 'title', None) or '(無題)'}」（関心）"
            )
    except Exception:
        LOGGER.warning(
            "[slot_close] failed to build choice lines (persona=%s)",
            persona_id, exc_info=True,
        )
    return lines


def _build_close_prompt(
    slot: Dict[str, Any], choice_lines: List[str]
) -> str:
    """<system> 包みの締めプロンプト。

    経験値ノートの意義説明は experience_ledger.md §4 の定義の短い写し。
    接地条件 (直前の実体験に基づく・実際にやったこと以外を書かない) と
    充填の禁忌 (空 note は正常) を明記する。
    """
    kind = str(slot.get("kind") or "").strip() or "(不明)"
    ref = str(slot.get("ref") or "").strip()

    choices_block = (
        "\n".join(choice_lines) if choice_lines else "（いまは生きている目的がありません）"
    )
    if ref and ref != BELONGS_NONE:
        prior_ref_line = (
            f"事前の予定では、このコマは {ref} のためのものでした。これは参考"
            "情報です — 実際にやったことが違う先に繋がったなら、実績の方を"
            "選んでください。\n"
        )
    else:
        prior_ref_line = ""

    return (
        "<system>\n"
        f"## コマの締め\n"
        f"「{kind}」のコマが終わりました。締めとして、今のセッションの実績を"
        "二つだけ記録します。\n"
        "\n"
        "1. 帰属 (belongs_to): このコマでやったことは、結局どの関心・タスク・"
        "欲求のためのものになりましたか。実際にやったこと (実績) に基づいて、"
        "下の一覧から選んでください。\n"
        f"{prior_ref_line}"
        "どれにも属さなければ \"none\"、既存のどれでもない新しい何かに繋がった"
        "なら \"new\" を選んでください。\n"
        "\n"
        "選べる先:\n"
        f"{choices_block}\n"
        "\n"
        "2. 経験値ノート (note): 経験値ノートは、後の自分のために、今の自分が"
        "得たものを残すものです。何をどうやって、何が効いて何がダメで、いま"
        "どう考えていて、次は何か — 自分の言葉で自由に書いてください。結果への"
        "評価や感想を含めてかまいません。\n"
        "- 今のセッションで実際にやったこと・考えたことだけを書いてください。"
        "やっていないことを書かないでください。\n"
        "- 書き残すほどのことが無ければ、note は空のままでかまいません"
        "（無理に埋めないでください）。\n"
        "</system>"
    )


# ---------------------------------------------------------------------------
# 本体
# ---------------------------------------------------------------------------


def run_slot_close(
    close_ctx: Any,
    *,
    manager: Any,
    persona_id: str,
    plan_date_str: str,
    slot: Dict[str, Any],
    index: int,
) -> str:
    """コマ締めの一手を実行する (帰属 + 経験値ノート、構造化出力 1 コール)。

    ``close_ctx`` は :class:`sea.work_session.SessionCloseContext`。失敗は
    raise せず WARNING に落とす (呼び出し側の hook 隔離と二重の安全)。

    Returns:
        締めの結果 (CLOSE_OUTCOME_*)。呼び出し側 (make_close_hook) が slot の
        ``close_outcome`` として永続化する。
    """
    persona = close_ctx.persona
    runtime = close_ctx.runtime

    # 書き先 (memory.db) が無ければ LLM を焚く前に見送る。
    adapter = getattr(persona, "sai_memory", None)
    if (
        adapter is None
        or not getattr(adapter, "is_ready", lambda: False)()
        or getattr(adapter, "conn", None) is None
    ):
        LOGGER.info(
            "[slot_close] memory adapter not ready; skipping close "
            "(persona=%s date=%s index=%d)", persona_id, plan_date_str, index,
        )
        return CLOSE_OUTCOME_SKIPPED_NO_MEMORY

    # ---- 構造化出力 1 発 (セッションの messages + 最終応答の続きとして) ----
    try:
        belongs_enum = _collect_belongs_to_enum(manager, persona_id)
        choice_lines = _build_choice_lines(manager, persona_id)
        prompt = _build_close_prompt(slot, choice_lines)

        messages: List[Dict[str, Any]] = list(close_ctx.messages)
        final = (close_ctx.final_continuation or "").strip()
        if final:
            # スペルループは最終応答 (spell 無し) を messages に積まないため、
            # 締めコールの文脈にはここで積む (prefix 連続性の維持)。
            messages.append({"role": "assistant", "content": final})
        messages.append({"role": "user", "content": prompt})

        node_def = SimpleNamespace(id="slot_close", memorize=None, speak=False)
        execution_context = close_ctx.execution_context
        llm_client, selected_model = runtime.select_llm_client(
            node_def, persona,
            execution_context=execution_context,
            needs_structured_output=True,
            state=close_ctx.state,
        )
        if execution_context is not None and selected_model != execution_context.model_key:
            # structured-output fallback で model が変わるとキャッシュは冷えるが、
            # 正しさ (スキーマの効くクライアント) を優先する。
            LOGGER.info(
                "[slot_close] structured-output fallback changed model %s -> %s "
                "(persona=%s)", execution_context.model_key, selected_model, persona_id,
            )

        result = llm_client.generate(
            messages,
            tools=[],
            response_schema=_build_response_schema(belongs_enum),
            temperature=runtime._default_temperature(persona),
            **runtime._get_cache_kwargs(persona_id),
        )

        from sea.work_session import WORK_SESSION_PLAYBOOK_NAME, _record_llm_usage

        _record_llm_usage(
            runtime, close_ctx.state, llm_client, persona,
            close_ctx.building_id, "llm_slot_close",
        )
        try:
            runtime._dump_llm_io(
                WORK_SESSION_PLAYBOOK_NAME, "slot_close", persona, messages,
                result if isinstance(result, str) else json.dumps(result, ensure_ascii=False),
            )
        except Exception:
            LOGGER.debug("[slot_close] failed to dump LLM I/O", exc_info=True)

        belongs_to, note = _parse_result(result, persona_id)
    except Exception:
        LOGGER.warning(
            "[slot_close] close call failed; skipping (persona=%s date=%s index=%d) — "
            "slot completion is unaffected",
            persona_id, plan_date_str, index, exc_info=True,
        )
        return CLOSE_OUTCOME_FAILED

    # ---- 帰属の検証と適用 (タグ棚) ----
    if belongs_to not in belongs_enum:
        LOGGER.warning(
            "[slot_close] belongs_to %r is not in the offered enum; treating as "
            "'none' (persona=%s)", belongs_to, persona_id,
        )
        belongs_to = BELONGS_NONE
    attribution_ok = _apply_attribution(close_ctx, adapter, persona_id, belongs_to)

    # ---- 経験値ノート → テーマページ fragment ----
    note_result = _write_experience_note(
        close_ctx, adapter, persona_id, plan_date_str, slot, note,
    )

    LOGGER.info(
        "[slot_close] done: persona=%s date=%s index=%d kind=%s belongs_to=%s note=%s",
        persona_id, plan_date_str, index, slot.get("kind"), belongs_to, note_result,
    )
    if not attribution_ok:
        # 帰属の判定は出たが適用できなかった — post_session の棚入れが代替
        # できるよう「済み」扱いにしない。
        return CLOSE_OUTCOME_FAILED
    if note_result == "failed":
        return CLOSE_OUTCOME_NOTE_FAILED
    return CLOSE_OUTCOME_DONE


def _parse_result(result: Any, persona_id: str) -> tuple:
    """generate の戻り値 (dict または JSON str) から (belongs_to, note) を読む。"""
    parsed: Dict[str, Any] = {}
    if isinstance(result, dict):
        parsed = result
    elif isinstance(result, str):
        try:
            loaded = json.loads(result)
            if isinstance(loaded, dict):
                parsed = loaded
        except (ValueError, TypeError):
            pass
    if not parsed:
        LOGGER.warning(
            "[slot_close] structured output parse failed; treating as no-op "
            "(persona=%s)", persona_id,
        )
        return BELONGS_NONE, ""
    belongs_to = str(parsed.get("belongs_to") or BELONGS_NONE).strip()
    note = str(parsed.get("note") or "").strip()
    return belongs_to, note


def _apply_attribution(
    close_ctx: Any, adapter: Any, persona_id: str, belongs_to: str
) -> bool:
    """帰属タグを purpose_tags 棚へ書く (層2 棚入れ — judgment_finalize と同族)。

    target はこのセッションの出来事 (kind='work_session' の episode_ref)。
    post_session 判断の episode_purposes と対象が同じため、ここで帰属が確定
    したセッションでは post_session の棚入れ欄を出さない (呼び出し側が
    close_outcome で抑止 — 同一セッションの二重宣言は「再訪」ではないのに
    revisit_count を偽増加させ、recall の順位を汚染する。Codex 一巡目 #5)。

    Returns:
        True = 帰属は確定した (none / new の「タグ無しが正解」を含む)。
        False = タグを書くべきだったのに書けなかった (post_session の棚入れが
        代替できるよう「済み」にしない)。
    """
    if belongs_to == BELONGS_NONE:
        return True
    if belongs_to == BELONGS_NEW:
        # v1 はログのみ (将来: desire 候補化と連動しうる)。
        LOGGER.info(
            "[slot_close] belongs_to='new' (persona=%s) — no tag written in v1",
            persona_id,
        )
        return True
    episode_ref = getattr(close_ctx, "episode_ref", None)
    if not episode_ref:
        LOGGER.warning(
            "[slot_close] no episode_ref for this session; attribution tag "
            "skipped (persona=%s belongs_to=%s)", persona_id, belongs_to,
        )
        return False
    add = getattr(adapter, "add_purpose_tag", None)
    if not callable(add):
        LOGGER.warning(
            "[slot_close] memory adapter has no add_purpose_tag; attribution "
            "tag skipped (persona=%s)", persona_id,
        )
        return False
    from sai_memory.purpose_tags import LAYER_SHELVE

    if not add(target_ref=episode_ref, purpose_ref=belongs_to, layer=LAYER_SHELVE):
        LOGGER.warning(
            "[slot_close] failed to persist attribution tag (%s -> %s, persona=%s)",
            episode_ref, belongs_to, persona_id,
        )
        return False
    return True


def _write_experience_note(
    close_ctx: Any,
    adapter: Any,
    persona_id: str,
    plan_date_str: str,
    slot: Dict[str, Any],
    note: str,
) -> str:
    """経験値ノートをコマ種別のテーマページ fragment に書く (空 note は何もしない)。

    ページはカタログの kind 名がタイトル。無ければ lazy creation
    (experience_ledger.md §5)。由来リンクは fragment に metadata 列が無いため
    content 末尾の由来注記 + source_date (コマの日付) で持つ — 注記は付加で
    あって本人テキストの切り詰めではない。

    Returns:
        "written" (書いた) / "empty" (note が空 — 正常) / "failed" (note が
        あるのに書けなかった — close_outcome の note_failed に対応)。
    """
    if not note:
        return "empty"
    kind = str(slot.get("kind") or "").strip()
    if not kind:
        LOGGER.warning(
            "[slot_close] slot has no kind; experience note skipped (persona=%s)",
            persona_id,
        )
        return "failed"
    try:
        from sai_memory.theme_pages import create_theme_page

        with adapter._db_lock:
            page_id = create_theme_page(
                adapter.conn,
                title=kind,
                member_refs=[],
                origin=THEME_PAGE_ORIGIN,
                content=f"「{kind}」の経験のページ（初経験のコマ締めで開かれた）",
            )

        episode_ref = getattr(close_ctx, "episode_ref", None)
        provenance = f"（{plan_date_str} のコマ「{kind}」の締めに記録"
        if episode_ref:
            provenance += f"。出来事: {episode_ref}"
        provenance += "）"

        from sai_memory.memopedia import Memopedia

        memopedia = Memopedia(adapter.conn, db_lock=adapter._db_lock)
        memopedia.create_fragment(
            entity_id=page_id,
            content=f"{note}\n{provenance}",
            source_date=plan_date_str,
        )
        return "written"
    except Exception:
        LOGGER.warning(
            "[slot_close] failed to write experience note (persona=%s kind=%s)",
            persona_id, kind, exc_info=True,
        )
        return "failed"
