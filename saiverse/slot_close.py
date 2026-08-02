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
cache が熱い。gold_panning と同じ理由でこの瞬間に置く)。

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


def make_close_hook(
    manager: Any, persona_id: str, plan_date_str: str, slot: Dict[str, Any], index: int
) -> Callable[[Any], None]:
    """``run_work_session(close_hook=...)`` に渡す締めフックを作る。"""

    def _hook(close_ctx: Any) -> None:
        run_slot_close(
            close_ctx,
            manager=manager,
            persona_id=persona_id,
            plan_date_str=plan_date_str,
            slot=slot,
            index=index,
        )

    return _hook


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
) -> None:
    """コマ締めの一手を実行する (帰属 + 経験値ノート、構造化出力 1 コール)。

    ``close_ctx`` は :class:`sea.work_session.SessionCloseContext`。失敗は
    raise せず WARNING に落とす (呼び出し側の hook 隔離と二重の安全)。
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
        return

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
        return

    # ---- 帰属の検証と適用 (タグ棚) ----
    if belongs_to not in belongs_enum:
        LOGGER.warning(
            "[slot_close] belongs_to %r is not in the offered enum; treating as "
            "'none' (persona=%s)", belongs_to, persona_id,
        )
        belongs_to = BELONGS_NONE
    _apply_attribution(close_ctx, adapter, persona_id, belongs_to)

    # ---- 経験値ノート → テーマページ fragment ----
    wrote_note = _write_experience_note(
        close_ctx, adapter, persona_id, plan_date_str, slot, note,
    )

    LOGGER.info(
        "[slot_close] done: persona=%s date=%s index=%d kind=%s belongs_to=%s note=%s",
        persona_id, plan_date_str, index, slot.get("kind"), belongs_to,
        "written" if wrote_note else "empty",
    )


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
) -> None:
    """帰属タグを purpose_tags 棚へ書く (層2 棚入れ — judgment_finalize と同族)。

    target はこのセッションの出来事 (kind='work_session' の episode_ref)。
    post_session 判断の episode_purposes と同じ対象なので、同じ帰属が両方から
    宣言された場合は upsert が再訪 (revisit_count+1) として同じ行に濃さを積む。
    """
    if belongs_to == BELONGS_NONE:
        return
    if belongs_to == BELONGS_NEW:
        # v1 はログのみ (将来: desire 候補化と連動しうる)。
        LOGGER.info(
            "[slot_close] belongs_to='new' (persona=%s) — no tag written in v1",
            persona_id,
        )
        return
    episode_ref = getattr(close_ctx, "episode_ref", None)
    if not episode_ref:
        LOGGER.warning(
            "[slot_close] no episode_ref for this session; attribution tag "
            "skipped (persona=%s belongs_to=%s)", persona_id, belongs_to,
        )
        return
    add = getattr(adapter, "add_purpose_tag", None)
    if not callable(add):
        LOGGER.warning(
            "[slot_close] memory adapter has no add_purpose_tag; attribution "
            "tag skipped (persona=%s)", persona_id,
        )
        return
    from sai_memory.purpose_tags import LAYER_SHELVE

    if not add(target_ref=episode_ref, purpose_ref=belongs_to, layer=LAYER_SHELVE):
        LOGGER.warning(
            "[slot_close] failed to persist attribution tag (%s -> %s, persona=%s)",
            episode_ref, belongs_to, persona_id,
        )


def _write_experience_note(
    close_ctx: Any,
    adapter: Any,
    persona_id: str,
    plan_date_str: str,
    slot: Dict[str, Any],
    note: str,
) -> bool:
    """経験値ノートをコマ種別のテーマページ fragment に書く (空 note は何もしない)。

    ページはカタログの kind 名がタイトル。無ければ lazy creation
    (experience_ledger.md §5)。由来リンクは fragment に metadata 列が無いため
    content 末尾の由来注記 + source_date (コマの日付) で持つ — 注記は付加で
    あって本人テキストの切り詰めではない。
    """
    if not note:
        return False
    kind = str(slot.get("kind") or "").strip()
    if not kind:
        LOGGER.warning(
            "[slot_close] slot has no kind; experience note skipped (persona=%s)",
            persona_id,
        )
        return False
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
        return True
    except Exception:
        LOGGER.warning(
            "[slot_close] failed to write experience note (persona=%s kind=%s)",
            persona_id, kind, exc_info=True,
        )
        return False
