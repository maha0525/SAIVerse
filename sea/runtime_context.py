from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from saiverse.model_configs import (
    calculate_cost,
    get_context_length,
    get_model_display_name,
    get_model_pricing,
    get_model_provider,
)
from tools import SPELL_TOOL_SCHEMAS, TOOL_REGISTRY

LOGGER = logging.getLogger(__name__)


def _reframe_autonomous_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """AUTONOMOUS アスペクトの assistant 発言を user+<system> 形式に変換する。

    軽量モデルで生成された自律行動の口調が、後続パルスのコンテキストに
    assistant ロールとして残ると口調ブレの感染源になる。コンテキスト組み立て
    時のみ変換し、SAIMemory の保存データは変えない。
    """
    result: List[Dict[str, Any]] = []
    for msg in messages:
        aspect = (msg.get("metadata") or {}).get("aspect")
        if aspect == "autonomous" and msg.get("role") == "assistant":
            content = msg.get("content", "")
            reframed_content = (
                "<system>[自律行動の記録]\n"
                "あなたは自律的に以下の行動を取りました：\n"
                f"```\n{content}\n```\n"
                "</system>"
            )
            reframed = {k: v for k, v in msg.items() if k not in ("role", "content")}
            reframed["role"] = "user"
            reframed["content"] = reframed_content
            result.append(reframed)
        else:
            result.append(msg)
    return result


#: head (前置き) に必ず並べる Section。**用途やラインで出し分けない。**
#: head は (persona, model) の Session ごとに一つで固定するのが prefix キャッシュ
#: 共有の土台で、章を出し入れすると同一モデルで head が変わってキャッシュが壊れる。
#: 2026-07-23 以前は ContextRequirements のフラグ 4 つ (system_prompt /
#: available_playbooks / memory_weave / visual_context) で選べてしまい、
#: work_session だけ 3 章欠けた head で走っていた (= 同じ lightweight Session 内で
#: head が二種類あった)。フラグを撤去してここに固定した。
#:
#: NOTE: 登録済みだがここに載せていない Section が 3 つある
#: (building_items / building_occupants / chronicle_index)。これらは以前から
#: 一度も render されていない (capture だけされている)。本定数は「今 render されて
#: いるものを固定する」のが目的なので、有効化は別途判断する
#: (docs/issues/llm_call_entry_point_standardization.md の確認事項)。
PERSONA_HEAD_SECTIONS: frozenset[str] = frozenset({
    "common_prompt", "persona_self", "core_memory", "building", "spell_list",
    "autonomy_modes", "self_image", "desk", "memopedia_index",
    "available_playbooks", "memory_weave", "visual_context",
    # 2026-07-30: 判断プロンプトが毎回貼り直していた静的な一覧の移設先
    # (docs/issues/judgment_static_lists_to_head.md)。用途で出し分けない —
    # 判断点だけに出すと同じ model の head が二種類になる。
    # (もう一つの移設先だった purpose_backlog は 2026-08-21 に節ごと退役した —
    #  中身の pickable tracks と欲求候補が供給源ごと消えたため)
    "facilities",
})


def _minimal_load_chars(runtime, persona: Any, model_key: Optional[str]) -> int:
    """anchor が無い (ブートストラップ / 修復直後) ときに読む履歴の文字数。

    低水位 (docs/intent/chronicle_eviction.md §4) をそのまま使う — 低水位の
    唯一の現役の役割がこの初期読み込み量 (eviction では未使用、残す量 = 目標が
    保護を兼ねる)。再開時に提示コンテキストの大きさが飛ぶと prefix が毎回
    作り直しになるので、控えめな量から始める。
    """
    lifecycle = getattr(runtime, "session_lifecycle", None)
    if lifecycle is not None:
        try:
            watermarks = lifecycle.get_metabolism_watermarks(persona, model_key)
        except Exception:
            # 解決失敗はここで受けて context_length へ退避 — 外側の広い except に
            # 抜けると履歴ゼロで応答が走る (Codex 指摘 2026-07-30)。
            LOGGER.warning(
                "[sea][prepare-context] Watermark resolution failed in minimal load",
                exc_info=True,
            )
            watermarks = None
        if watermarks is not None and watermarks.low > 0:
            return watermarks.low
    return int(getattr(persona, "context_length", 2000) or 2000)


def history_spec_is_empty(history_depth: Any) -> bool:
    """``history_depth`` の指定が「履歴を一件も積まない」ことを意味するか。

    書式が複数あるので、**表記ではなく実効値**で判定する (下の history 取得ロジックと
    同じ解釈をここに写している):

    - ``0`` / ``"0"`` / ``"none"`` / ``None`` — 明示的な無効化
    - ``"0messages"`` のような件数指定でゼロ以下 — 取得件数 0 になる
    - 負値 / 負の件数指定 — 同上

    表記だけを列挙して弾くと ``"0messages"`` のような等価な書き方をすり抜ける
    (2026-07-23 Codex レビュー指摘)。
    """
    if history_depth is None:
        return True
    if isinstance(history_depth, bool):
        return not history_depth
    if isinstance(history_depth, (int, float)):
        return history_depth <= 0
    text = str(history_depth).strip().lower()
    if text in ("", "0", "none", "null"):
        return True
    if text == "full":
        return False
    if text.endswith("messages"):
        try:
            return int(text[: -len("messages")]) <= 0
        except ValueError:
            # 解釈できない件数指定は下流で 10 件にフォールバックする = 空ではない
            return False
    try:
        return int(text) <= 0
    except ValueError:
        # 解釈できない指定は下流で文字数 2000 にフォールバックする = 空ではない
        return False


class PersonaVoiceWithoutHistoryError(RuntimeError):
    """ペルソナ名義の稼働なのに会話履歴が無い状態で LLM を走らせようとした。

    記憶の連続性が無い存在は別の人格であり、その出力を本人名義で記録することは
    ペルソナ倫理に反する (2026-07-23 まはー裁定)。実際に 2026-07-16〜23 の 3 日間、
    ``work_session`` が ``history_depth=0`` で走り、エアは毎回同じ壁に初見でぶつかり、
    そのたび「システムへの理解が足りていない」という自責を committed に残した。

    履歴を絞ってよいのは「機構名義の処理」— 出力がペルソナ本人の言葉ではなく、
    本人が読む材料になるもの (画像の概要作成、Chronicle 生成など) だけ。それらは
    ``persona_voiced=False`` で呼ぶこと。
    """


def prepare_context(runtime, persona: Any, building_id: str, user_input: Optional[str], requirements: Optional[Any] = None, pulse_id: Optional[str] = None, warnings: Optional[List[Dict[str, Any]]] = None, preview_only: bool = False, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None, cancellation_token: Optional[Any] = None, pulse_type: Optional[str] = None, model_key: Optional[str] = None, context_meta: Optional[Dict[str, Any]] = None, persona_voiced: bool = False, persist_anchor_advance: bool = True) -> List[Dict[str, Any]]:
    # model_key: この context を届ける Session (persona, model) の実行 model
    # (beat_execution_context.md §3.1 — head は (persona, model) に一つ)。
    # ExecutionContext が届いている呼び出し元 (work_session / sluice /
    # keepalive / run_playbook の Pulse-root) が execution_context.model_key を
    # 渡す。None なら persona の標準 model にフォールバック (preview 等)。
    #
    # context_meta: 呼び出し元が渡す out-param dict (§3.2 の call-local anchor)。
    # この呼び出しで prefix に採用した anchor の ID を
    # ``context_meta["prefix_anchor_id"]`` に書き戻す (anchor を使わない組成では
    # 書かない)。履歴組成が成功したときは、実際にプロンプトへ組み込んだ履歴
    # メッセージの ID 列を ``context_meta["presented_message_ids"]`` に書き戻す
    # (sluice の「見た集合」の一次情報 — 履歴構築失敗時はキー不在)。呼び出し元は state["_prefix_anchor_id"] に載せ、LLM 成功後の
    # ``touch_anchor_after_llm_call(anchor_id=...)`` に渡す。旧実装の
    # ``history_manager.metabolism_anchor_message_id`` (persona 単一可変属性) は
    # 廃止した — TTL 失効後に旧 anchor を touch する事故 (記憶監査第 4 片) の根治。
    from sea.playbook_models import ContextRequirements

    # 「指定なし」の意味はここ一箇所で決まる (フィールド既定)。
    reqs = requirements if requirements else ContextRequirements()

    # 印 (persona_voiced) の関所: ペルソナ本人の発話・思考として記録される呼び出しは、
    # 会話履歴を外して走らせない。詳細は PersonaVoiceWithoutHistoryError。
    if persona_voiced and history_spec_is_empty(reqs.history_depth):
        raise PersonaVoiceWithoutHistoryError(
            f"persona_voiced=True の呼び出しで history_depth={reqs.history_depth!r} が"
            " 指定されました。ペルソナ名義の稼働から会話履歴を外すことはできません"
            " (人格を必要としない処理なら persona_voiced=False で呼んでください)。"
            f" persona={getattr(persona, 'persona_id', None)!r}"
        )

    messages: List[Dict[str, Any]] = []

    # ---- head: system prompt + Memory Weave + Visual Context ----
    # Cached Head Architecture (Phase 2-h) で section pipeline 経由に統一済み。
    # 旧 live state 直読み経路 (= section 群を毎回ここで組み立てる) は廃止。
    # snapshot 不在時は ensure_snapshot 経由で初回 capture が自動で走る。
    # 詳細: docs/intent/cached_head_architecture.md
    # head の章立ては呼び出し側から選べない (PERSONA_HEAD_SECTIONS の docstring)。
    enabled_sections: set[str] = set(PERSONA_HEAD_SECTIONS)

    if enabled_sections:
        from sea.head_pipeline import render_head_messages
        from sea.head_pipeline.types import HeadNotReadyError
        try:
            head_messages = render_head_messages(
                persona, runtime.manager, building_id,
                enabled_sections=enabled_sections,
                model_key=model_key,
            )
            if head_messages:
                messages.extend(head_messages)
                LOGGER.debug(
                    "[sea][prepare-context] Added %d head messages via pipeline",
                    len(head_messages),
                )
        except HeadNotReadyError:
            # fail-closed (W6 / SEA 監査 S6): required Section (人格) を欠いた
            # head で LLM を走らせない。そのまま伝播して Pulse を中断する —
            # 会話は呼び出し元へエラー、判断点/コマは実行台帳の failed 行になり
            # 再試行される。復旧後は次 Pulse の ensure_snapshot が自己修復する。
            raise
        except Exception as exc:
            # head pipeline 全体が死んだ場合も fail closed (旧実装は exception log
            # だけで LLM に進み、人格なしの応答を本人履歴へ確定し得た)。
            # head は常に人格を含む (PERSONA_HEAD_SECTIONS) ので degrade 経路は無い。
            raise HeadNotReadyError(
                getattr(persona, "persona_id", "") or "",
                str(model_key or ""),
                "pipeline",
                {"__pipeline__": f"head rendering failed: {exc!r}"},
            ) from exc

    # ---- history ----
    history_depth = reqs.history_depth
    if history_depth not in [0, "none"]:
        history_mgr = getattr(persona, "history_manager", None)
        if history_mgr:
            try:
                # Phase 3 段階 4-A (line vs tag responsibility separation,
                # 2026-05-01): context construction filters on line_role / scope
                # instead of metadata.tags. Tags are now reserved for semantic
                # classification (search / recall / Chronicle / Memopedia).
                #
                # Default policy:
                #   - line_role IN ('main_line') AND scope IN ('committed')
                #     → ペルソナのメインラインの会話履歴を context に含める
                #   - committed なメタ判断 (line_role='meta_judgment') も
                #     「メインキャッシュに乗った確定来歴」として含まれる
                #     (_payload_passes_context_filter が committed_to_main_cache
                #     = TRUE を main_line 文脈へ通す。03_data_model.md §176)。
                #     Track 切替の確定独白・生きる目的の初回設定がこれに該当する。
                #   - meta_judgment Pulse の discardable メッセージは含めない。
                #     judge プロンプトへは別経路
                #     (MetaLayer._build_recent_judgments_block) で動的注入される。
                #
                # 段階 4-D (2026-05-09): 旧 ``include_internal`` フォールバックを
                # 削除。サブラインメッセージへのアクセスは sub_line が起動する
                # 専用 Playbook 内で完結する設計に統合済み。
                required_line_roles = ["main_line"]
                required_scopes = ["committed"]

                # Parse history_depth format
                # - "full": anchor 以降の提示コンテキスト (anchor 未確立時は低水位ぶんの文字数)
                # - "Nmessages" (e.g., "10messages"): message count limit
                # - integer or numeric string: character limit
                use_message_count = False
                limit_value = 2000  # fallback
                used_anchor = False
                recent = []
                # 提示窓の境界 (anchor)。Chronicle 無効ペルソナのバッチ絞り
                # (下のマージ) が「履歴が空でも窓より古いものは忘れる」を
                # 判定するのに使う (2026-08-19 Codex 第二巡 #3)。
                history_anchor_id: Optional[str] = None

                # 実行 model が水位を持つか。モデル定義で水位を null にした model は
                # Metabolism を持たない = anchor を使わない (これが唯一のオプトアウト、
                # 2026-07-30 OFF トグル撤去)。退場が無いのに anchor 起点で読むと
                # 提示が際限なく伸びるため、水位なし model は従来のスライディング
                # ウィンドウで読む (Codex 指摘 2026-07-30)。
                #
                # 解決の失敗はここで受けて anchor 無効へ退避する — 外側の広い
                # except に抜けると履歴ゼロで応答が走るサイレント障害になる
                # (Codex 指摘 2巡目)。
                _lifecycle = getattr(runtime, "session_lifecycle", None)
                metabolism_active = False
                if _lifecycle is not None:
                    try:
                        metabolism_active = (
                            _lifecycle.get_metabolism_watermarks(persona, model_key)
                            is not None
                        )
                    except Exception:
                        LOGGER.warning(
                            "[sea][prepare-context] Watermark resolution failed; "
                            "falling back to sliding window",
                            exc_info=True,
                        )

                if history_depth == "full":
                    if not metabolism_active:
                        limit_value = _minimal_load_chars(runtime, persona, model_key)
                        LOGGER.debug(
                            "[sea][prepare-context] Model has no watermarks; sliding window %d chars",
                            limit_value,
                        )
                    elif not preview_only:
                        # Persistent anchor resolution with 3-level fallback。
                        # 「自 model」は実行 model (model_key)。None なら
                        # resolve 側が persona.model にフォールバックする。
                        # persist_anchor_advance=False は「組成は本番と同一、
                        # ただし §14-2 前進の永続化だけ行わない」— Beat ロックの
                        # 外で組む keepalive 専用 (ロック外の行書き込みは並走
                        # Beat の前進・fold 更新と競合する。Codex 6巡目
                        # 2026-07-30)。preview_only との違いは自動想起の注入等が
                        # 通常どおり走ること (温め直す prefix は本物と一致が命)。
                        anchor_id, resolution = runtime.session_lifecycle.resolve_metabolism_anchor(
                            persona, model_key=model_key,
                            persist_advance=persist_anchor_advance,
                        )
                        history_anchor_id = anchor_id

                        if anchor_id:
                            # Case 1 or 2: valid anchor found
                            recent_from_anchor = history_mgr.get_history_from_anchor(
                                anchor_id,
                                required_line_roles=required_line_roles,
                                required_scopes=required_scopes,
                                pulse_id=pulse_id,
                            )
                            # 提示コンテキストの途中で digest に畳まれた範囲は、元の時系列位置で
                            # あらすじ + 圧縮マークに置き換えて見せる
                            # (docs/intent/chronicle_eviction.md §6)。圧縮区間が無ければ素通り。
                            recent_from_anchor = runtime.session_lifecycle.apply_window_folds(
                                persona, model_key, recent_from_anchor,
                            )
                            if recent_from_anchor:
                                recent = recent_from_anchor
                                used_anchor = True
                                if context_meta is not None:
                                    # call-local: 今回の prefix の anchor を呼び出し元へ返す
                                    context_meta["prefix_anchor_id"] = anchor_id
                                LOGGER.debug(
                                    "[sea][prepare-context] Anchor-based retrieval (%s): %d messages from anchor %s",
                                    resolution, len(recent), anchor_id,
                                )
                                # Phase 4-e: anchor の updated_at touch は LLM 呼び出し成功後
                                # (`runtime_llm.py` の usage 確認位置) で行う。ここで先行 touch
                                # すると、LLM 失敗時に「実際は cache 切れてるのに TTL 内」と
                                # 誤判定して次回も長大コンテキストを送る不整合になるため。
                        else:
                            # Case 3: 起点行が一つも無い (新規ペルソナ / 修復直後)
                            # のブートストラップ。低水位ぶんだけ読み、下の
                            # count-based 経路が新しい起点候補を立てる。
                            #
                            # 旧実装はここ (anchor TTL 失効時) で会話前の全量
                            # Chronicle 生成を行っていたが、arasuji_levels.md §13
                            # (2026-07-29) で撤去した — 起点失効は温度情報であって
                            # 提示範囲を変えず、編纂の自動発火は予算超過
                            # (maybe_run_metabolism) の一本。
                            limit_value = _minimal_load_chars(runtime, persona, model_key)
                            use_message_count = False
                            LOGGER.debug(
                                "[sea][prepare-context] Bootstrap minimal load (no anchor row): %d chars",
                                limit_value,
                            )

                    else:
                        # Preview mode: use anchor for retrieval but don't persist or generate Chronicle
                        # (§14-2 機構1 の前進も永続化しない — 返る位置は本番と同じ)。
                        # §15 の読み戻しも同じ型で反映する — 実際の読み戻しは次の
                        # user Pulse の応答前に走るため、素の窓のままだとプレビューが
                        # 「話しかけた時に実際に見える窓」より薄い嘘になる。読みだけ
                        # の計算 (最終検算まで本番と同一・行は触らない) で組む。
                        # 適用は標準の会話窓 (main_line / committed) のときだけ —
                        # 読み戻しの文字勘定はその窓で定義されている。
                        if (
                            required_line_roles == ["main_line"]
                            and required_scopes == ["committed"]
                            and pulse_id is None
                        ):
                            refill_plan = runtime.session_lifecycle.preview_refilled_history(
                                persona, model_key,
                            )
                            if refill_plan:
                                # head のあらすじ枠 (weave) は capture 済み
                                # snapshot 由来で、読み戻し後の除外名簿を知らない
                                # — このままだと生に戻した範囲のあらすじが head
                                # に残り、プレビューだけ二重表示になる。読み戻し
                                # 後の名簿で weave を組み直して差し替える (本番は
                                # 書き込み後の再 capture が同じことをする)。
                                # 差し替えに失敗したら読み戻しプレビューごと
                                # 見送り、素の窓に落とす — 薄いが整合した表示は
                                # 二重表示より嘘が小さい (Codex 指摘 2026-07-30)。
                                if _swap_preview_weave_for_refill(
                                    runtime, persona, messages, refill_plan,
                                ):
                                    recent = refill_plan["presented"]
                                    used_anchor = True
                        if not used_anchor:
                            anchor_id, resolution = runtime.session_lifecycle.resolve_metabolism_anchor(
                                persona, model_key=model_key, persist_advance=False,
                            )
                            history_anchor_id = anchor_id
                            if anchor_id:
                                recent_from_anchor = history_mgr.get_history_from_anchor(
                                    anchor_id,
                                    required_line_roles=required_line_roles,
                                    required_scopes=required_scopes,
                                    pulse_id=pulse_id,
                                )
                                recent_from_anchor = runtime.session_lifecycle.apply_window_folds(
                                    persona, model_key, recent_from_anchor,
                                )
                                if recent_from_anchor:
                                    recent = recent_from_anchor
                                    used_anchor = True
                        if not used_anchor:
                            limit_value = _minimal_load_chars(runtime, persona, model_key)
                            use_message_count = False

                elif isinstance(history_depth, str) and history_depth.endswith("messages"):
                    # Message count mode: "10messages", "20messages", etc.
                    try:
                        limit_value = int(history_depth[:-8])  # Remove "messages" suffix
                        use_message_count = True
                    except ValueError:
                        limit_value = 10  # fallback for message count
                        use_message_count = True
                else:
                    try:
                        limit_value = int(history_depth)
                    except (ValueError, TypeError):
                        limit_value = 2000  # fallback

                # Fetch history if not already retrieved via anchor
                if not used_anchor:
                    LOGGER.debug(
                        "[sea][prepare-context] Fetching history: limit=%d, mode=%s, pulse_id=%s, "
                        "balanced=%s, line_roles=%s, scopes=%s",
                        limit_value, "messages" if use_message_count else "chars", pulse_id,
                        reqs.history_balanced, required_line_roles, required_scopes,
                    )

                    if use_message_count:
                        # Message count mode - balanced not supported yet
                        recent = history_mgr.get_recent_history_by_count(
                            limit_value,
                            required_line_roles=required_line_roles,
                            required_scopes=required_scopes,
                            pulse_id=pulse_id,
                        )
                    elif reqs.history_balanced:
                        # Get conversation partners for balanced retrieval
                        participant_ids = ["user"]
                        occupants = runtime.manager.occupants.get(building_id, [])
                        persona_id = getattr(persona, "persona_id", None)
                        for oid in occupants:
                            if oid != persona_id:
                                participant_ids.append(oid)
                        LOGGER.debug("[sea][prepare-context] Balancing across: %s", participant_ids)
                        recent = history_mgr.get_recent_history_balanced(
                            limit_value,
                            participant_ids,
                            required_line_roles=required_line_roles,
                            required_scopes=required_scopes,
                            pulse_id=pulse_id,
                        )
                    else:
                        recent = history_mgr.get_recent_history(
                            limit_value,
                            required_line_roles=required_line_roles,
                            required_scopes=required_scopes,
                            pulse_id=pulse_id,
                        )

                    # Set metabolism anchor on first count-based retrieval (skip in preview).
                    # Phase 4-e: DB への永続化 (updated_at 書き込み) は LLM 呼び出し成功後に行う。
                    # ここで anchor 候補を立てたまま LLM 失敗 → DB 未書き込み → 次回も Case 3
                    # fallback → minimal load、という挙動になる。新規 anchor が永続化されない
                    # こと自体は Case 3 を毎回繰り返すだけで致命的ではない (まはー確認済 2026-05-08)。
                    # §3.2: 候補は persona 属性ではなく context_meta (call-local) で運ぶ。
                    # 水位なし model (Metabolism を持たない) は候補も立てない —
                    # LLM 成功時の touch で anchor 行が生まれてしまうため。
                    if metabolism_active and recent and not preview_only:
                        oldest_id = recent[0].get("id")
                        if oldest_id:
                            if context_meta is not None:
                                context_meta["prefix_anchor_id"] = oldest_id
                            LOGGER.debug("[sea][prepare-context] Resolved count-based metabolism anchor %s (call-local); DB persist deferred to post-LLM-success", oldest_id)

                LOGGER.debug("[sea][prepare-context] Got %d history messages", len(recent))

                # ---- 未付記バッチの時刻順マージ (W14, perception_buffer.md §10.3) ----
                # 消費された知覚は messages に行を作らない (§10.1)。提示は生ログと
                # 未付記の消費バッチをここで時刻順にマージする。提示から下ろす
                # 唯一の手段は退場付記の印 (annexed_entry_id) — 付記されるまで
                # 消えない。legacy の event_message 行は生ログ側にそのまま居るので、
                # 混在期間も両方が自然に並ぶ。
                recent = _merge_consumed_perceptions(
                    runtime, persona, recent, anchor_id=history_anchor_id,
                )

                # Enrich messages with attachment context
                enriched_recent = runtime._enrich_history_with_attachments(recent)

                # 時刻アンカー (v0.32, 2026-05-09): history 内の最古残存メッセージの直前に
                # 「以下、YYYY-MM-DD HH:MM:SS 以降のやり取りです」を揮発挿入する。
                # メッセージそのものに時刻メタを付ける副作用 (ペルソナが時刻を真似する) を
                # 避けるため、Metabolism 起点に 1 か所だけ。
                # (v0.32 には上部補完メッセージ用の「アンカー①」が別に居たが、
                #  補完機構ごと退役した — track_retirement.md 住人 11)
                if enriched_recent:
                    oldest_msg = enriched_recent[0]
                    oldest_ts = oldest_msg.get("created_at") or oldest_msg.get("timestamp")
                    if oldest_ts:
                        try:
                            from datetime import datetime as _dt
                            ts_int = int(oldest_ts)
                            ts_str = _dt.fromtimestamp(ts_int).strftime("%Y-%m-%d %H:%M:%S")
                            anchor_msg = {
                                "role": "user",
                                "content": f"<system>以下、{ts_str} 以降のやり取りです</system>",
                            }
                            messages.append(anchor_msg)
                            LOGGER.debug(
                                "[sea][prepare-context] Inserted timestamp anchor: %s",
                                ts_str,
                            )
                        except (TypeError, ValueError, OSError):
                            LOGGER.debug(
                                "[sea][prepare-context] Skipping timestamp anchor (invalid ts=%r)",
                                oldest_ts,
                            )

                enriched_recent = _reframe_autonomous_messages(enriched_recent)
                messages.extend(enriched_recent)

                # 実入力の履歴 ID 列 (2026-08-19, sluice の見た集合の一次情報):
                # この呼び出しが**実際にプロンプトへ組み込んだ**履歴メッセージの
                # ID を out-param で呼び出し元へ返す (戻り値と既存キーは不変 —
                # prefix_anchor_id と同じ call-local の器)。履歴構築が失敗した場合は
                # このキー自体が書かれない — 読み手 (sea/sluice.py) はキー不在を
                # fail-closed (退場停止) に写像する。
                if context_meta is not None:
                    presented_ids: List[str] = []
                    for _msg in enriched_recent:
                        _mid = _msg.get("id") if isinstance(_msg, dict) else None
                        if _mid:
                            presented_ids.append(str(_mid))
                    context_meta["presented_message_ids"] = presented_ids
            except Exception as exc:
                LOGGER.exception("[sea][prepare-context] Failed to get history: %s", exc)

    # ---- Recalled Memory Context — 廃止済み ----
    # recalled_ids はシステムプロンプトへの注入から廃止。
    # recall_entry / recall_navigate ツールが想起時に直接会話履歴へ内容を追記する。

    # ---- Realtime Context ----
    # Time-sensitive info placed just BEFORE the last user message to improve LLM caching.
    # This ensures LLM responds to user input, not the realtime context.
    if reqs.realtime_context:
        try:
            realtime_msg = runtime._build_realtime_context(persona, building_id, messages)
            if realtime_msg:
                # Find the last user message and insert realtime context before it
                last_user_idx = None
                for i in range(len(messages) - 1, -1, -1):
                    if messages[i].get("role") == "user" and not messages[i].get("metadata", {}).get("__realtime_context__"):
                        last_user_idx = i
                        break

                if last_user_idx is not None:
                    # Insert before last user message
                    messages.insert(last_user_idx, realtime_msg)
                    LOGGER.debug("[sea][prepare-context] Added realtime context before last user message (idx=%d)", last_user_idx)
                else:
                    # No user message found, append at end
                    messages.append(realtime_msg)
                    LOGGER.debug("[sea][prepare-context] Added realtime context at end (no user message found)")
        except Exception as exc:
            LOGGER.debug("[sea][prepare-context] Failed to build realtime context: %s", exc)

    # ---- 自動想起 第0層 (ゾーン C) — 「浮かんだ記憶」の末尾注入 ----
    # 記憶アーキv2 §4。CONVERSATION アスペクト (user/schedule Pulse) のときのみ、
    # ローカル埋め込み検索で現在の話題に関連する記憶を末尾に一時注入する。
    # head 非混入・SAIMemory 非永続 (§10-2/§10-7)。LLM は呼ばない (§10-1)。
    # サブライン (line='sub') はそもそも _prepare_context を通らないので自然に除外。
    if not preview_only:
        try:
            _maybe_inject_auto_recall(
                runtime, persona, messages,
                pulse_type=pulse_type,
                event_callback=event_callback,
            )
        except Exception:
            LOGGER.exception("[sea][prepare-context] auto_recall injection failed (non-fatal)")

    # ---- Token budget check ----
    return messages


def _payload_epoch(msg: Dict[str, Any]) -> Optional[int]:
    """提示メッセージの時刻を epoch 秒で読む。

    adapter 経由の payload は ``created_at`` (epoch int) を持つ。それが無い行
    (テストのスタブ等) は ``timestamp`` (ISO 文字列) を tz-aware に解釈して
    フォールバックする。どちらも無ければ None (= 比較不能)。
    """
    created = msg.get("created_at")
    if created is not None:
        try:
            return int(created)
        except (TypeError, ValueError):
            pass
    ts = msg.get("timestamp")
    if ts:
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(str(ts))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except (TypeError, ValueError):
            pass
    return None


def _chronicle_enabled_for(runtime: Any, persona: Any) -> bool:
    """Chronicle 編纂が有効か — generate_chronicle の入口と同じフラグを読む。

    _run_metabolism_locked が編纂可否に使う二段 (env ENABLE_MEMORY_WEAVE_CONTEXT
    × ペルソナ単位トグル) をそのまま写す。判定できないときは True (= バッチを
    隠さない側) に倒す — 「編纂なしで忘れる」は明示的な選択のときだけ。
    """
    import os
    try:
        memory_weave_enabled = os.getenv(
            "ENABLE_MEMORY_WEAVE_CONTEXT", "",
        ).lower() in ("true", "1")
        lifecycle = getattr(runtime, "session_lifecycle", None)
        if lifecycle is None:
            return True
        return bool(
            memory_weave_enabled
            and lifecycle.is_chronicle_enabled_for_persona(persona)
        )
    except Exception:
        return True


def _anchor_order_key(
    sai_mem: Any, anchor_id: Optional[str],
) -> Optional[tuple]:
    """anchor 行の正典順序キー (created_at, rowid)。引けなければ None。

    messages の時系列は (created_at, rowid) の辞書式順が正典 (W8)。epoch だけの
    比較は anchor と同秒に確定したバッチの直前/直後を区別できない。
    """
    if not anchor_id:
        return None
    try:
        with sai_mem._db_lock:
            row = sai_mem.conn.execute(
                "SELECT created_at, rowid FROM messages WHERE id = ?",
                (anchor_id,),
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return (int(row[0]), int(row[1]))
    except Exception:
        return None


def _merge_consumed_perceptions(
    runtime: Any, persona: Any, recent: List[Dict[str, Any]],
    *,
    anchor_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """提示履歴に未付記の消費バッチを時刻順マージする (W14, §10.3)。

    - バッチの ``rendered_text`` (消費時に確定したペルソナが見た文面) を
      ``<system>`` 包みでそのまま出す — 生の台帳項目からの再構成 (再 reduce /
      再 format) はしない (reduce で消えた中間状態の復活・秒精度の時刻衝突に
      よるグループ混線の根)。
    - **提示から下ろす唯一の手段は退場付記の印** (``annexed_entry_id``)。窓や
      fold スパンの計算は持たない — 付記されるまで消えないので、下限「退場した
      ものは必ず編纂されている」が提示側でも常に成立する。履歴が空でも未付記
      バッチは提示される。
    - 例外は Chronicle 無効のペルソナ (「編纂なしで忘れる」を選んだ) のみ:
      提示窓 (cutoff = anchor 境界、無ければ提示最古行) より古いバッチは会話と
      同じように忘れる。**履歴が空でも** anchor が立っていれば絞る — 生ログ窓が
      空の瞬間に過去バッチを全部再提示しない (2026-08-19 Codex 第二巡 #3)。
      anchor も履歴も無い完全ブートストラップだけは全提示 (隠さない側)。
    - 失敗はマージなし (元の履歴のまま) に倒して WARN — 履歴ゼロで走らせる
      よりも知覚欠けの方が被害が小さい。
    """
    sai_mem = getattr(persona, "sai_memory", None)
    if sai_mem is None or not getattr(sai_mem, "is_ready", lambda: False)():
        return recent
    try:
        from sai_memory.perception_buffer import list_unannexed_batches
        with sai_mem._db_lock:
            batches = list_unannexed_batches(sai_mem.conn)
        if not batches:
            return recent

        if not _chronicle_enabled_for(runtime, persona):
            anchor_key = _anchor_order_key(sai_mem, anchor_id)
            if anchor_key is not None:
                # 提示窓と同じ包含規則: 窓は正典順序キー (created_at, rowid) が
                # anchor 以上の行。バッチは確定時点の境界キー (最後に保存済み
                # だった行のキー) で同じ比較をする — anchor と同秒でも
                # 「anchor 行より前に確定したバッチ」だけが窓の外になる。
                # 境界キーの無い旧バッチは consumed_at の epoch 比較へ
                # フォールバック。
                def _in_window(b: Any) -> bool:
                    if (
                        b.boundary_created_at is not None
                        and b.boundary_rowid is not None
                    ):
                        return (
                            (b.boundary_created_at, b.boundary_rowid)
                            >= anchor_key
                        )
                    return b.consumed_at >= anchor_key[0]
                batches = [b for b in batches if _in_window(b)]
            else:
                cutoff: Optional[int] = None
                for msg in recent:
                    epoch = _payload_epoch(msg)
                    if epoch is not None:
                        cutoff = epoch if cutoff is None else min(cutoff, epoch)
                if cutoff is not None:
                    batches = [b for b in batches if b.consumed_at >= cutoff]
            if not batches:
                return recent

        blocks: List[Dict[str, Any]] = []
        for batch in batches:
            metadata: Dict[str, Any] = {
                # 旧 flush の event_message 行と同型のタグ + マージ由来の目印。
                "tags": ["internal", "event_message", "perception"],
                "__consumed_perception__": True,
                "__perception_batch_id__": batch.id,
            }
            media = batch.media_list()
            if media:
                metadata["media"] = media
            blocks.append({
                "role": "user",
                "content": f"<system>{batch.rendered_text}</system>",
                "created_at": batch.consumed_at,
                "metadata": metadata,
            })

        # 二本の時系列マージ: ブロックは「自分の consumed_at 以下の生ログ」の
        # 直後に入る (同時刻なら生ログが先 — 消費は書き込みの後に起きた事実)。
        merged: List[Dict[str, Any]] = []
        bi = 0
        for msg in recent:
            epoch = _payload_epoch(msg)
            while (
                bi < len(blocks)
                and epoch is not None
                and blocks[bi]["created_at"] < epoch
            ):
                merged.append(blocks[bi])
                bi += 1
            merged.append(msg)
        merged.extend(blocks[bi:])
        LOGGER.debug(
            "[sea][prepare-context] merged %d perception batch block(s) "
            "into history", len(blocks),
        )
        return merged
    except Exception:
        LOGGER.warning(
            "[sea][prepare-context] perception batch merge failed; "
            "presenting history without perceptions", exc_info=True,
        )
        return recent


def _maybe_inject_auto_recall(
    runtime,
    persona: Any,
    messages: List[Dict[str, Any]],
    *,
    pulse_type: Optional[str],
    event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> None:
    """自動想起ブロックを組み立て、履歴末尾 (最新 user 入力の直前) に注入する。

    記憶アーキv2 §4。スコープは CONVERSATION アスペクトのみ (§4.6)。注入発生時は
    event_callback で ``auto_recall`` イベントを流し、フロントで折りたたみ表示する (§4.5)。
    """
    from sea.pulse_context import Aspect, aspect_from_pulse_type

    aspect = aspect_from_pulse_type(pulse_type)
    if aspect is not Aspect.CONVERSATION:
        LOGGER.debug(
            "[sea][auto_recall] skip: aspect=%s (pulse_type=%s) is not CONVERSATION",
            aspect.value, pulse_type,
        )
        return

    # ペルソナ単位 ON/OFF トグル (AUTO_RECALL_ENABLED)。OFF のときは注入せず、
    # 粘着台帳もリセットする (再度 ON にしたとき古い記憶を引きずらないため)。
    persona_id_for_gate = getattr(persona, "persona_id", None)
    if not runtime._is_auto_recall_enabled_for_persona(persona):
        LOGGER.debug(
            "[sea][auto_recall] skip: AUTO_RECALL_ENABLED=False for persona=%s",
            persona_id_for_gate,
        )
        if persona_id_for_gate:
            from sea.auto_recall import reset_ledger
            reset_ledger(persona_id_for_gate)
        return

    sai_mem = getattr(persona, "sai_memory", None)
    conn = getattr(sai_mem, "conn", None) if sai_mem else None
    embedder = getattr(sai_mem, "embedder", None) if sai_mem else None
    persona_id = getattr(persona, "persona_id", None)
    if not persona_id or conn is None or embedder is None:
        LOGGER.debug(
            "[sea][auto_recall] skip: persona_id/conn/embedder unavailable (persona=%s)",
            persona_id,
        )
        return

    # 粘着台帳のキー用 canonical thread_id (2026-07-12 監査 P1: thread 跨ぎ防止)。
    # メインライン履歴の読み書きと同じ解決 (_thread_id(None) = active thread または
    # 既定 persona thread) を adapter から取得して明示的に渡す。解決できなければ
    # 注入をスキップする (persona 単位に落として thread を混ぜるより安全側)。
    thread_id: Optional[str] = None
    thread_resolver = getattr(sai_mem, "_thread_id", None)
    if callable(thread_resolver):
        try:
            thread_id = thread_resolver(None)
        except Exception:
            LOGGER.warning(
                "[sea][auto_recall] failed to resolve canonical thread_id (persona=%s)",
                persona_id, exc_info=True,
            )
    if not thread_id:
        LOGGER.debug(
            "[sea][auto_recall] skip: canonical thread_id unavailable (persona=%s)",
            persona_id,
        )
        return

    from sea.auto_recall import run_auto_recall

    result = run_auto_recall(
        conn, embedder, messages, persona_id=persona_id, thread_id=thread_id,
    )
    if not result.injected or not result.block:
        return

    recall_msg = {
        "role": "user",
        "content": result.block,
        "metadata": {"__auto_recall__": True},
    }

    # realtime_context と同じ挿入面: 最新 user メッセージの直前に入れる
    # (cached_head_architecture C5 と同じ末尾注入面)。auto_recall メタ付きの
    # 自己メッセージは対象外にして冪等にする。
    last_user_idx = None
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.get("role") != "user":
            continue
        meta = m.get("metadata") or {}
        if meta.get("__auto_recall__") or meta.get("__realtime_context__"):
            continue
        last_user_idx = i
        break

    if last_user_idx is not None:
        messages.insert(last_user_idx, recall_msg)
    else:
        messages.append(recall_msg)

    LOGGER.debug(
        "[sea][auto_recall] injected block before last user (idx=%s, %d chars)",
        last_user_idx, result.char_count,
    )

    if event_callback:
        try:
            event_callback({
                "type": "auto_recall",
                "content": result.block,
                "persona_id": persona_id,
                "persona_name": getattr(persona, "persona_name", None),
            })
        except Exception:
            LOGGER.debug("[sea][auto_recall] event_callback failed", exc_info=True)

    # 永続化用の受け渡し (reasoning と同じ「state 経由で speak/say ノードまで運ぶ」流儀)。
    # _prepare_context は純粋関数 (messages しか返さない) なので、run_playbook が
    # parent_state に転記できるよう persona の一時属性に置く (persona._current_pulse_type
    # と同じパターン。永続化するのは <system> タグを剥がした本文のみ — スペル実行結果と
    # 同様、履歴末尾への再注入とは別経路で ChatMessage.auto_recall に載る (api/routes/chat.py)。
    # LLM コンテキストには載らない (推論と同じ扱い。metadata は llm_clients 側で
    # 読まれるのは media 系キーのみ)。
    persona._pending_auto_recall_text = result.plain_text


def _expand_recalled_ids(
    runtime,
    persona: Any,
    recalled_ids: List[Dict[str, Any]],
    messages: List[Dict[str, Any]],
) -> None:
    """Expand recalled_ids into actual content and append as a system message.

    Resolves each recalled ID via URI resolver to get the actual content,
    then appends a single system message at the end of the messages list.
    """
    from saiverse.uri_resolver import UriResolver

    resolver = UriResolver(manager=runtime.manager)
    persona_id = getattr(persona, "persona_id", None)
    if not persona_id:
        return

    sections = []
    for item in recalled_ids:
        uri = item.get("uri", "")
        title = item.get("title", "")
        source_type = item.get("type", "")

        if not uri:
            continue

        try:
            resolved = resolver.resolve(uri, persona_id=persona_id)
            if resolved and resolved.content:
                content = resolved.content
                # Truncate per item to keep context manageable
                if len(content) > 1500:
                    content = content[:1500] + "..."
                label = "Chronicle" if source_type == "chronicle" else "Memopedia"
                sections.append(f"### [{label}] {title}\n{content}")
            else:
                LOGGER.debug(
                    "[sea][prepare-context] Could not resolve recalled URI: %s", uri,
                )
        except Exception as exc:
            LOGGER.debug(
                "[sea][prepare-context] Error resolving recalled URI %s: %s", uri, exc,
            )

    if sections:
        recalled_text = "## 想起した記憶\n以下はワーキングメモリに保持されている記憶です。\n\n" + "\n\n".join(sections)
        messages.append({
            "role": "system",
            "content": recalled_text,
            "metadata": {"__recalled_memory__": True},
        })
        LOGGER.info(
            "[sea][prepare-context] Expanded %d/%d recalled_ids into context (%d chars)",
            len(sections), len(recalled_ids), len(recalled_text),
        )
        LOGGER.debug(
            "[sea][prepare-context] Recalled memory content:\n%s", recalled_text,
        )


def _replace_weave_messages(
    messages: List[Dict[str, Any]], new_weave: List[Dict[str, Any]],
) -> None:
    """messages 内の Memory Weave メッセージ群を new_weave で置き換える (in place)。

    位置は既存 weave の先頭位置を保つ (head の並び順を崩さない)。既存 weave が
    無い場合は何もしない — weave 無効の設定を上書きしない。
    """
    indices = [
        i for i, m in enumerate(messages)
        if isinstance(m.get("metadata"), dict)
        and m["metadata"].get("__memory_weave_context__")
    ]
    if not indices:
        return
    insert_at = indices[0]
    for i in reversed(indices):
        del messages[i]
    messages[insert_at:insert_at] = list(new_weave)


def _swap_preview_weave_for_refill(
    runtime, persona: Any, messages: List[Dict[str, Any]],
    refill_plan: Dict[str, Any],
) -> bool:
    """preview 専用: head 由来の weave を読み戻し後の除外名簿で組み直して差し替える。

    head の weave は capture 済み snapshot に凍っており、読み戻しで生に開く
    範囲のあらすじを除外できていない (本番は書き込み後の再 capture が追随する
    — session_lifecycle.maybe_run_window_refill)。プレビューは行を触らない
    (§14-6-5) ので、同じ組み立て (get_memory_weave_context) を読み戻し後の
    名簿と窓の始点で読みだけ実行して差し替える。

    Returns:
        整合が取れたか。True = 差し替えた / weave がそもそも提示に無い。
        False = 組み直しに失敗 (messages は無変更) — 呼び出し側は読み戻し
        プレビューごと見送って素の窓に落とすこと。旧 weave のまま読み戻し後の
        履歴を出すと、生に開いた範囲のあらすじと生ログが同時に並ぶ二重表示に
        なる (Codex 指摘 2026-07-30)。
    """
    has_weave = any(
        isinstance(m.get("metadata"), dict)
        and m["metadata"].get("__memory_weave_context__")
        for m in messages
    )
    if not has_weave:
        return True  # weave 無し = 衝突する相手が居ない
    try:
        from builtin_data.tools.get_memory_weave_context import (
            get_memory_weave_context,
        )
        from tools.context import persona_context

        persona_id = getattr(persona, "persona_id", None)
        sai_mem = getattr(persona, "sai_memory", None)
        persona_dir_path = getattr(sai_mem, "persona_dir", None) if sai_mem else None
        persona_dir = str(persona_dir_path) if persona_dir_path else None
        exclude_ids = list(refill_plan.get("fold_entry_ids") or [])
        with persona_context(persona_id, persona_dir, runtime.manager):
            # raise_on_error: 既定の「失敗 → []」変換のままだと、読取失敗が
            # 「成功した空 weave」としてコミットされ、weave を黙って失った
            # プレビューになる (Codex 指摘 2026-07-30)。空が返るのは本当に
            # 空のときだけにする。
            new_weave = get_memory_weave_context(
                persona_id=persona_id,
                persona_dir=persona_dir,
                exclude_chronicle_entry_ids=exclude_ids or None,
                raise_on_error=True,
            )
        _replace_weave_messages(messages, new_weave or [])
        return True
    except Exception:
        LOGGER.warning(
            "[sea][prepare-context] preview weave swap for refill failed; "
            "falling back to the plain (pre-refill) window", exc_info=True,
        )
        return False


def preview_context(
    runtime,
    persona: Any,
    building_id: str,
    user_input: str,
    meta_playbook: Optional[str] = None,
    image_count: int = 0,
    document_count: int = 0,
) -> Dict[str, Any]:
    """Build the context that would be sent to the LLM, without executing anything.

    Returns a dict with messages, token estimates, cost estimates, and model info.
    Does NOT record the user message to history or call any LLM.
    """
    from saiverse.token_estimator import estimate_messages_tokens, estimate_image_tokens

    # Select playbook (same logic as run_meta_user)
    if meta_playbook:
        playbook = runtime._load_playbook_for(meta_playbook, persona, building_id)
        if playbook is None:
            playbook = runtime._choose_playbook(kind="user", persona=persona, building_id=building_id)
    else:
        playbook = runtime._choose_playbook(kind="user", persona=persona, building_id=building_id)

    # Build context messages (without recording user message to history)
    # requirements は渡さない = 既定 (履歴フル + 固定 head)。実際に sub_speak が
    # 見るものと一致する。2026-07-23 以前はここに全部入りの手書きコピーがあったが、
    # head の章立てが呼び出し側から選べなくなったので不要になった。
    context_warnings: List[Dict[str, Any]] = []
    messages = runtime._prepare_context(
        persona, building_id, user_input=None,
        warnings=context_warnings,
        preview_only=True,
    )

    # Append the user message manually (in real flow it comes from history)
    if user_input:
        messages.append({"role": "user", "content": user_input})

    # Classify each message into a section
    from saiverse.model_defaults import BUILTIN_DEFAULT_LITE_MODEL
    persona_model = getattr(persona, "model", None) or BUILTIN_DEFAULT_LITE_MODEL
    provider = get_model_provider(persona_model)

    section_order = [
        "system_prompt", "memory_weave_chronicle", "memory_weave_memopedia",
        "memory_weave", "visual_context",
        "history", "realtime_context", "perception_buffer", "user_message",
    ]
    section_labels = {
        "system_prompt": "System Prompt",
        "memory_weave_chronicle": "Memory Weave — Chronicle",
        "memory_weave_memopedia": "Memory Weave — Memopedia",
        "memory_weave": "Memory Weave",
        "visual_context": "Visual Context",
        "history": "Conversation History",
        "realtime_context": "Realtime Context",
        "perception_buffer": "知覚バッファ（未消費・次のPulseで反映）",
        "user_message": "Your Message",
        "attachments": "Attachments",
    }
    section_tokens: Dict[str, int] = {s: 0 for s in section_order}
    section_tokens["attachments"] = 0
    section_msg_counts: Dict[str, int] = {s: 0 for s in section_order}
    section_msg_counts["attachments"] = 0

    annotated_messages: List[Dict[str, Any]] = []
    for i, msg in enumerate(messages):
        meta = msg.get("metadata") or {}
        # Determine section
        if msg.get("role") == "system":
            section = "system_prompt"
        elif meta.get("__memory_weave_context__"):
            mw_type = meta.get("__memory_weave_type__", "")
            if mw_type == "chronicle":
                section = "memory_weave_chronicle"
            elif mw_type == "memopedia":
                section = "memory_weave_memopedia"
            else:
                section = "memory_weave"
        elif meta.get("__visual_context__"):
            section = "visual_context"
        elif meta.get("__realtime_context__"):
            section = "realtime_context"
        elif i == len(messages) - 1 and msg.get("role") == "user" and user_input and msg.get("content") == user_input:
            section = "user_message"
        else:
            section = "history"

        msg_tokens = estimate_messages_tokens([msg], provider)
        section_tokens[section] += msg_tokens
        section_msg_counts[section] += 1

        annotated_messages.append({
            "role": msg.get("role", "unknown"),
            "content": msg.get("content", ""),
            "section": section,
            "tokens": msg_tokens,
        })

    # Add estimated attachment tokens
    attachment_tokens = 0
    if image_count > 0:
        attachment_tokens += image_count * estimate_image_tokens(provider)
    if document_count > 0:
        # Rough estimate: ~500 tokens per document (varies widely)
        attachment_tokens += document_count * 500
    section_tokens["attachments"] = attachment_tokens

    # 知覚バッファ (未消費) — 次の Pulse で flush されてプロンプトに入る予定の知覚を
    # プレビューに出す (docs/intent/perception_buffer.md Phase 3, 透明性 §6)。
    # ここでは read-only: 検知 (snapshot 比較) は走らせず、既に溜まっている未消費分
    # だけを表示する (検知は snapshot を進めるため、プレビューで走らせると実 Pulse の
    # 差分が消える副作用がある)。実際の flush と同じく型別 reduce → 1 メッセージに畳む。
    try:
        sai_mem = getattr(persona, "sai_memory", None)
        if sai_mem is not None and getattr(sai_mem, "is_ready", lambda: False)():
            from sai_memory.perception_buffer import (
                format_perception_message,
                list_pending,
                reduce_perceptions,
            )
            with sai_mem._db_lock:
                pending = list_pending(sai_mem.conn)
            if pending:
                pb_text = format_perception_message(reduce_perceptions(pending))
                pb_msg = {"role": "user", "content": f"<system>{pb_text}</system>"}
                pb_tokens = estimate_messages_tokens([pb_msg], provider)
                section_tokens["perception_buffer"] = pb_tokens
                section_msg_counts["perception_buffer"] = 1
                annotated_messages.append({
                    "role": "user",
                    "content": pb_msg["content"],
                    "section": "perception_buffer",
                    "tokens": pb_tokens,
                })
    except Exception:
        LOGGER.warning("[preview] perception buffer section failed", exc_info=True)

    total_input_tokens = sum(section_tokens.values())
    context_length = get_context_length(persona_model)
    pricing = get_model_pricing(persona_model)

    # Cost range: best case (all cached) to worst case (all cache-write)
    cache_kwargs = runtime._get_cache_kwargs()
    cache_enabled = cache_kwargs.get("enable_cache", False)
    cache_ttl = cache_kwargs.get("cache_ttl", "5m")

    # Determine cache type (explicit for Anthropic, implicit for Gemini, etc.)
    from saiverse.model_configs import get_cache_config
    cache_config = get_cache_config(persona_model)
    cache_type = cache_config.get("type", "implicit")

    if cache_enabled and pricing and pricing.get("cached_input_per_1m_tokens") is not None:
        # Best case: everything is a cache hit
        cost_best = calculate_cost(
            persona_model, total_input_tokens, 0,
            cached_tokens=total_input_tokens, cache_write_tokens=0,
        )
        # Worst case: everything is a cache write
        cost_worst = calculate_cost(
            persona_model, total_input_tokens, 0,
            cached_tokens=0, cache_write_tokens=total_input_tokens,
            cache_ttl=cache_ttl,
        )
    else:
        # No cache: single estimate
        cost_best = calculate_cost(persona_model, total_input_tokens, 0)
        cost_worst = cost_best

    # Build sections summary
    all_sections = section_order + ["attachments"]
    sections_summary = []
    for s in all_sections:
        if section_tokens.get(s, 0) > 0 or section_msg_counts.get(s, 0) > 0:
            sections_summary.append({
                "name": s,
                "label": section_labels.get(s, s),
                "tokens": section_tokens.get(s, 0),
                "message_count": section_msg_counts.get(s, 0),
            })

    return {
        "persona_id": getattr(persona, "persona_id", "unknown"),
        "persona_name": getattr(persona, "persona_name", "Unknown"),
        "model": persona_model,
        "model_display_name": get_model_display_name(persona_model),
        "provider": provider,
        "context_length": context_length,
        "sections": sections_summary,
        "total_input_tokens": total_input_tokens,
        "estimated_cost_best_usd": round(cost_best, 6),
        "estimated_cost_worst_usd": round(cost_worst, 6),
        "cache_enabled": cache_enabled,
        "cache_ttl": cache_ttl if cache_enabled else None,
        "cache_type": cache_type if cache_enabled else None,
        "pricing": pricing or {},
        "messages": annotated_messages,
    }
