from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from sea.eviction_plan import CONSUMED_PERCEPTION_KEY
from saiverse.model_configs import (
    calculate_cost,
    get_context_length,
    get_model_display_name,
    get_model_pricing,
    get_model_provider,
    resolve_perception_watermarks,
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

    残す量 (``metabolism_target_chars``、docs/intent/chronicle_eviction.md §4)
    を流用する — 会話窓が常に目指す量なので、初期読み込みも同じ量から始めれば
    提示コンテキストの大きさが飛ばない。旧三水位の低水位が担っていた役割だが、
    低水位は 2026-09-04 に廃止した (docs/issues/
    watermarks_unsatisfiable_when_perception_is_large.md 裁定 5)。
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
        if watermarks is not None and watermarks.target > 0:
            return watermarks.target
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


class PinnedAnchorUnavailableError(RuntimeError):
    """起点を凍結した組成 (``pinned_anchor_id``) で、その起点から履歴を組めなかった。

    起点の凍結は「一回の整理 (Metabolism) は一つの一貫した窓で最後まで走る」
    (2026-08-24 まはー裁定) のための機構で、使うのは Metabolism 実行内の
    スルース経路だけ。凍結が効かないときに通常の起点解決へ黙って落とすと、
    凍結で塞いだ競合 (実行中の §14-2 起点前進で、退場計画の土台とスルース
    入力が別々の窓になる) が静かに再導入される — だからフォールバックせず
    送出する (fail-closed)。呼び出し元 (run_sluice → run_metabolism) は
    スルース失敗 = 退場停止に写像し、次回の Metabolism が再試行する。
    """


def _pinned_history_from_anchor(
    runtime,
    persona,
    history_mgr,
    pinned_anchor_id: str,
    model_key: Optional[str],
    required_line_roles: List[str],
    required_scopes: List[str],
    pulse_id: Optional[str],
) -> List[Dict[str, Any]]:
    """凍結された起点から提示履歴を組む (fail-closed)。

    :func:`resolve_metabolism_anchor` を呼ばない — §14-2 (機構1) の前進判定
    自体を行わないのが凍結の意味論で、``persist_anchor_advance=False``
    (前進を計算するが永続化しない、keepalive 用) とは別物。組めなかったら
    :class:`PinnedAnchorUnavailableError` を送出する (通常解決への
    フォールバック禁止 — クラス docstring 参照)。
    """
    recent = history_mgr.get_history_from_anchor(
        pinned_anchor_id,
        required_line_roles=required_line_roles,
        required_scopes=required_scopes,
        pulse_id=pulse_id,
    )
    recent = runtime.session_lifecycle.apply_window_folds(
        persona, model_key, recent,
    )
    if not recent:
        raise PinnedAnchorUnavailableError(
            f"pinned anchor {pinned_anchor_id!r} yielded no history "
            f"(persona={getattr(persona, 'persona_id', None)!r}); refusing to "
            "fall back to anchor resolution"
        )
    return recent


class WindowFloorUnmetError(RuntimeError):
    """発話直前の最終防衛ライン (会話の行 ≥ 残す量) を用意できなかった。

    :meth:`~sea.session_lifecycle.SessionLifecycle.ensure_window_floor` が
    "unmet" を返した回に ``run_meta_user`` が送出する — Playbook は走っておらず
    副作用ゼロ。``[]`` を返すと PulseController が "completed" と記帳し、
    schedule の occurrence が実行なしで消費されるため、型付き例外で失敗として
    伝える (PulseController は ``runtime_outcome="floor_unmet"``、ScheduleManager
    は failed = 再試行安全に分類する。Codex 二巡目 #2)。ユーザーへの通知は
    ``run_meta_user`` が送出前に一度だけ出す。
    """


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


def prepare_context(runtime, persona: Any, building_id: str, user_input: Optional[str], requirements: Optional[Any] = None, pulse_id: Optional[str] = None, warnings: Optional[List[Dict[str, Any]]] = None, preview_only: bool = False, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None, cancellation_token: Optional[Any] = None, pulse_type: Optional[str] = None, model_key: Optional[str] = None, context_meta: Optional[Dict[str, Any]] = None, persona_voiced: bool = False, persist_anchor_advance: bool = True, pinned_anchor_id: Optional[str] = None) -> List[Dict[str, Any]]:
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

    # 起点の凍結 (pinned_anchor_id) は anchor 起点の提示組成 (history_depth
    # "full") でだけ意味を持つ。別の depth や履歴の無い persona で黙って
    # 無視すると、凍結したつもりの呼び出し元 (sluice) が通常解決の窓で走る —
    # ここで落とす (fail-closed)。
    if pinned_anchor_id is not None and (
        reqs.history_depth != "full"
        or getattr(persona, "history_manager", None) is None
    ):
        raise PinnedAnchorUnavailableError(
            f"pinned_anchor_id={pinned_anchor_id!r} requires history_depth='full' "
            f"and a history_manager (got depth={reqs.history_depth!r}, "
            f"persona={getattr(persona, 'persona_id', None)!r})"
        )

    messages: List[Dict[str, Any]] = []

    # ---- head: system prompt + Memory Weave + Visual Context ----
    # Cached Head Architecture (Phase 2-h) で section pipeline 経由に統一済み。
    # 旧 live state 直読み経路 (= section 群を毎回ここで組み立てる) は廃止。
    # snapshot 不在時は ensure_snapshot 経由で初回 capture が自動で走る。
    # 詳細: docs/intent/cached_head_architecture.md
    # head の章立ては呼び出し側から選べない (PERSONA_HEAD_SECTIONS の docstring)。
    enabled_sections: set[str] = set(PERSONA_HEAD_SECTIONS)

    # 実際に描画した head が見せている部屋 (out-param で受け取る)。知覚の
    # 「部屋の様子」の差分をそのまま出すか全文へ開き直すかは、この prompt に
    # 部屋の全体像が載っているかで決まる — 後段で head を読み直すと、その間に
    # 走った Metabolism / TTL の撮り直しで別の head を見てしまい、全体像の無い
    # 差分を送りうる (2026-09-05 Codex 指摘)。この呼び出しの中で確定させた値を
    # 勘定と提示の両方へ渡す。head を組まない呼び出しは空のまま = 後段が自分で
    # 読む (従来どおり)。
    head_room_out: Dict[str, Any] = {}

    if enabled_sections:
        from sea.head_pipeline import render_head_messages
        from sea.head_pipeline.types import HeadNotReadyError
        try:
            head_messages = render_head_messages(
                persona, runtime.manager, building_id,
                enabled_sections=enabled_sections,
                model_key=model_key,
                head_room_out=head_room_out,
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
                # - "full": anchor 以降の提示コンテキスト (anchor 未確立時は残す量ぶんの文字数)
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
                    if pinned_anchor_id is not None:
                        # 起点の凍結 (2026-08-24 まはー裁定「一回の整理は一つの
                        # 一貫した窓で最後まで走る」): resolve_metabolism_anchor
                        # を呼ばず、呼び出し元 (run_metabolism) が実行頭に撮った
                        # 窓の起点から組む。実行中に Chronicle が確定して最前線
                        # が動いても、この組成は動かない — §14-2 (機構1) の前進
                        # 判定ごと走らせない。組めなければ送出 (fail-closed、
                        # ヘルパーの docstring 参照)。
                        recent = _pinned_history_from_anchor(
                            runtime, persona, history_mgr, pinned_anchor_id,
                            model_key, required_line_roles, required_scopes,
                            pulse_id,
                        )
                        history_anchor_id = pinned_anchor_id
                        used_anchor = True
                        if context_meta is not None:
                            context_meta["prefix_anchor_id"] = pinned_anchor_id
                        LOGGER.debug(
                            "[sea][prepare-context] Pinned-anchor retrieval: "
                            "%d messages from anchor %s (no anchor resolution)",
                            len(recent), pinned_anchor_id,
                        )
                    elif not metabolism_active:
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
                            # のブートストラップ。残す量ぶんだけ読み、下の
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
                        # Pulse の応答前に走るため、素の窓のままだとプレビューが
                        # 「話しかけた時に実際に見える窓」より薄い嘘になる。読みだけ
                        # の計算 (仕上げの検算まで本番と同一・行は触らない) で組む。
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
                # プレビューは送らない列なので、下ろし境界は進めない (測るだけ) —
                # 一方向にしか進まない境界を、実際には送らない組み立てで確定
                # させない (2026-09-05 四巡目 #6 と同じ理由の隣)。
                recent = _merge_consumed_perceptions(
                    runtime, persona, recent, anchor_id=history_anchor_id,
                    model_key=model_key, advance_cutoff=not preview_only,
                    head_room_key=_pinned_head_room_key(head_room_out),
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
            except PinnedAnchorUnavailableError:
                # 凍結起点の失敗は握らない — ここで warning に丸めると、呼び出し
                # 元 (sluice) は「presented_message_ids 不在」という遠い顔の
                # 失敗になる。実名で送出して退場停止に乗せる。
                raise
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

    _run_metabolism_locked が編纂可否に使う門 (ペルソナ単位トグル
    AI.CHRONICLE_ENABLED) をそのまま写す。判定できないときは True (= バッチを
    隠さない側) に倒す — 「編纂なしで忘れる」は明示的な選択のときだけ。

    かつては env ENABLE_MEMORY_WEAVE_CONTEXT との二段だったが、2026-09-01 に
    撤去した (v0.2 からのアップグレード組で記憶の整理が全停止する実害)。
    """
    try:
        lifecycle = getattr(runtime, "session_lifecycle", None)
        if lifecycle is None:
            return True
        return bool(lifecycle.is_chronicle_enabled_for_persona(persona))
    except Exception:
        return True


def _anchor_order_key_locked(
    sai_mem: Any, anchor_id: Optional[str],
) -> Optional[tuple]:
    """anchor 行の正典順序キー (created_at, rowid)。引けなければ None。

    messages の時系列は (created_at, rowid) の辞書式順が正典 (W8)。epoch だけの
    比較は anchor と同秒に確定したバッチの直前/直後を区別できない。

    **呼び出し側が ``sai_mem._db_lock`` を保持している前提** — 提示の組成は
    候補取得から境界前進までを一つのロック区間で完結させるので、ここで錠前を
    取り直すと (非再入の ``threading.Lock`` を持つ環境で) 自分自身と噛み合う。
    錠前を取る層は :func:`list_presented_perception_blocks` の一枚だけ。
    """
    if not anchor_id:
        return None
    try:
        row = sai_mem.conn.execute(
            "SELECT created_at, rowid FROM messages WHERE id = ?",
            (anchor_id,),
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return (int(row[0]), int(row[1]))
    except Exception:
        return None


def _perception_block_text(rendered_text: str) -> str:
    """バッチの確定文面を提示ブロックの本文にする (組成の一点)。

    勘定 (:func:`sea.eviction_plan.message_chars`) が数えるのはこの文字列なので、
    合計上限の判定も同じ関数で測る — 「送る側と測る側が同じ一枚を見る」規則を
    知覚の上限にも通す。
    """
    return f"<system>{rendered_text}</system>"


#: 下ろした跡地に置く機構名義の一行の見出し。既存の知覚ブロックの見出し
#: (``[システム通知]`` / ``[フィード]`` — sai_memory/perception_buffer の
#: ``_KIND_HEADERS``) と同じ流儀に合わせる。
_PERCEPTION_OMISSION_HEADER = "[省略された記録]"

#: 「下ろしても上の水位を下回れない」を (ペルソナ, 実行 model) ごとプロセス
#: ごとに 1 度だけ警告するための既出集合 (毎ターン同じ行でログを埋めない)。
#: 水位は model ごとなので、抑止キーも model を含める (session_lifecycle の
#: ``_watermark_inversion_warned`` と同じ形)。単独で上限を超える一個は下ろさずに
#: 超過を許す — 旗を立てる正直な形
#: (docs/issues/watermarks_unsatisfiable_when_perception_is_large.md 裁定 7)。
_PERCEPTION_CAP_WARNED: Set[Tuple[str, str]] = set()
_PERCEPTION_CAP_WARN_LOCK = threading.Lock()


def _perception_omission_block(
    dropped: Sequence[Any], created_at: int, *, chronicle_enabled: bool,
    records: int,
) -> Dict[str, Any]:
    """下ろした区間の跡地に置く機構名義のブロック (連続区間を一つに束ねる)。

    黙って消さない — 「そこに何も無かった」という記録の嘘をつかないため
    (2026-09-04 まはー裁定 1)。あらすじの畳みが跡地に digest を置くのと同じ
    原則で、文面の役目も同じ二つ: ①ここに記録があった ②消えたのではない。

    ``records`` は下ろした区間に**実際に入っていた記録の件数**
    (:func:`~sai_memory.perception_buffer.count_batch_records` の合計) で、
    バッチ数ではない — 1 枚のバッチは Beat 頭に溜まっていた知覚を全部束ねるので、
    バッチ数で書くと「部屋の様子 3 件 + 通知 5 件」の 1 枚が「1 件」になる。
    台帳の行を引けなかったバッチは 1 と数えるので合計は**下限**であり、文面も
    「N 件以上」と下限で書く (数えられた分より少なく言うことはない)。

    Chronicle 無効のペルソナには②を書かない — 編纂が走らない設定なので、
    あらすじで読み返せるという約束が嘘になる。有効なペルソナにも「もう入って
    いる」とは書かない: この区間の編纂はまだ走っていないことがあり、断定すると
    未成立の約束になる (2026-09-05 四巡目 #3)。
    """
    body = (
        f"ここにあった {records} 件以上の記録 (部屋の様子・通知など) は、"
        "古くなったため表示から外しました。"
    )
    if chronicle_enabled:
        body += "消えたのではなく、記憶の整理の際にあらすじへ引き継がれます。"
    return {
        "role": "user",
        "content": (
            f"<system>{_PERCEPTION_OMISSION_HEADER}\n{body}</system>"
        ),
        "created_at": int(created_at),
        "metadata": {
            "tags": ["internal", "event_message", "perception"],
            CONSUMED_PERCEPTION_KEY: True,
            "__perception_omitted__": int(records),
            "__perception_omitted_batches__": len(dropped),
        },
    }


#: 「head の部屋は呼び出し側から渡されていない」ことの印。``None`` は
#: 「head はどの部屋も見せていない」という**答え**なので、区別が要る。
_HEAD_ROOM_UNSET = object()


def _pinned_head_room_key(head_room_out: Dict[str, Any]) -> Any:
    """描画済み head の out-param (:func:`render_head_messages`) を部屋のキーにする。

    out-param が空 = この呼び出しは head を組んでいない (組成をスキップした /
    テストで差し替えられている) ので、判定は後段の読み直しに委ねる
    (:data:`_HEAD_ROOM_UNSET`)。値が入っていれば、たとえ ``None`` (どの部屋も
    見せていない) でもそれが答え。
    """
    if "building_id" not in head_room_out:
        return _HEAD_ROOM_UNSET
    building_id = head_room_out.get("building_id")
    if not building_id:
        return None
    try:
        from sai_memory.room_state import room_key
        return room_key(str(building_id))
    except Exception:
        LOGGER.warning(
            "[sea][perception] could not build the head's room key from the "
            "rendered head; reopening head-based room diffs to their full text",
            exc_info=True,
        )
        return None


def _head_room_key(persona: Any, model_key: Optional[str]) -> Optional[str]:
    """その回の提示先 model の head が今見せている部屋のキー。引けなければ None。

    「部屋の様子」の差分は、台帳に土台が無くても head が同じ部屋を見せていれば
    head の姿を土台にする (sai_memory/room_state.py)。その差分を提示してよいか
    は「**この** model の head が今もその部屋を見せているか」で決まる — head は
    (ペルソナ, model) ごとに別々の時点で capture されるので、台帳には書けない。

    撮り直しはしない読み口 (:func:`sea.head_pipeline.current_head_room`) なので、
    まだ一度も head を組んでいない Session では None になる。None は「head は
    その部屋を見せていない」と読まれ、差分は全文へ開き直される — 冗長な全文
    一枚は無害だが、全体像を失った差分は復元不能 (回復側と同じ倒し方)。

    **これを使うのは head を組まない呼び出しだけ** (勘定・退場計画・読み取り
    専用の画面)。同じ呼び出しで head を送る prepare_context は
    :func:`_pinned_head_room_key` で描画済みの値を渡す — 読み直すと、確定と
    読みの間に走った撮り直しで別の head を見てしまう。
    """
    try:
        from sai_memory.room_state import room_key
        from sea.head_pipeline import current_head_room
        building_id, _room_text = current_head_room(persona, model_key=model_key)
        return room_key(building_id) if building_id else None
    except Exception:
        LOGGER.warning(
            "[sea][perception] could not resolve the head's current room; "
            "reopening head-based room diffs to their full text", exc_info=True,
        )
        return None


def _perception_suffix_totals(
    presented: Sequence[Any], *, head_room_key: Optional[str] = None,
) -> List[int]:
    """``presented[i:]`` を提示したときの合計字数を ``i`` ごとに並べて返す。

    境界を進めると :func:`sai_memory.room_state.restore_room_state_bases` が
    同じ tx で走り、土台を失った差分を全文へ差し替える — 差分の小さい文面が
    全文に膨らむ。下ろす量を差し替え**前**の字数で決めると、下ろした直後に上の
    水位を超えたままになり、新着が無いのに次の呼び出しで境界がまた進む (提示は
    新着が無ければ変わらない、という約束が破れる)。だからここは開き直し**後**の
    姿で数える。

    数え方は回復側と同じ規則を、字数だけで辿り直したもの。バッチごとに JSON を
    読み直すのは**一度きり**で、そこから

    - ``suffix_raw[i]``     : 差し替え前の素の合計 (後ろからの累積)
    - ``suffix_forced[i]``  : 提示列の中で既に土台が切れている差分の膨らみ
      (どの ``i`` でも開き直すので、これも後ろからの累積で足りる)
    - 部屋ごとの「``presented[i:]`` に最初に現れるエントリ」の膨らみ

    の三つを合わせる。三つ目だけは ``i`` が進むと部屋ごとに一つずつ前へずれる
    ので、落ちたバッチに載っていた部屋だけを繰り上げて差分更新する。全体で
    バッチ数 + エントリ数に比例する手間で済む (2026-09-05 Codex 三巡 #3 —
    以前は境界候補ごとに残り全部を数え直していて、``_db_lock`` を握ったまま
    二乗時間で回っていた。Codex の材料で 1,000 件 4.1 秒 / 2,000 件 28.7 秒、
    回帰テストの材料でも 2,000 件 4.8 秒 → 線形化後は同じ材料で 0.05 秒)。

    **head を土台にした差分**は連なりの外なので、``i`` がどこであっても膨らむか
    どうかは同じ — その回の head が同じ部屋を見せていなければ (``head_room_key``
    と不一致) 必ず開き直され、見せていれば決して開き直されない。だから部屋ごと
    の「最初に現れるエントリ」の繰り上げには入れず、``forced`` 側だけで数える。

    返るのは長さ ``len(presented) + 1`` の list で、末尾は 0 (全部下ろした形)。
    """
    from sai_memory.room_state import (
        batch_room_states,
        chain_is_intact,
        is_head_based,
    )

    count = len(presented)
    raw: List[int] = [0] * count
    # エントリを時刻順に平坦化する (batch_index / key / 膨らみ / 土台の状態)。
    entry_batch: List[int] = []
    entry_delta: List[int] = []
    entry_forced: List[bool] = []
    key_entries: Dict[str, List[int]] = {}
    keys_in_batch: List[List[str]] = []
    previous_by_key: Dict[str, Any] = {}
    for index, batch in enumerate(presented):
        rendered = batch.rendered_text or ""
        raw[index] = len(_perception_block_text(rendered))
        seen_here: List[str] = []
        for entry in batch_room_states(batch.room_state_json):
            key = str(entry.get("key") or "")
            if not key:
                continue
            head_based = is_head_based(entry)
            previous = None
            if not head_based:
                previous = previous_by_key.get(key)
                previous_by_key[key] = entry
            block = entry.get("block") or ""
            snapshot = entry.get("snapshot") or ""
            # 回復側と同じ見送り条件 (差し替えられないものは膨らまない)。
            expandable = bool(
                entry.get("is_diff") and block and snapshot and block in rendered
            )
            position = len(entry_batch)
            entry_batch.append(index)
            entry_delta.append(len(snapshot) - len(block) if expandable else 0)
            if head_based:
                # 連なりの外: head が同じ部屋を見せているかだけで決まる。
                entry_forced.append(expandable and key != head_room_key)
                continue  # 部屋ごとの繰り上げ (key_entries) にも入れない
            entry_forced.append(
                expandable and not chain_is_intact(entry, previous)
            )
            key_entries.setdefault(key, []).append(position)
            seen_here.append(key)
        keys_in_batch.append(seen_here)

    forced_by_batch: List[int] = [0] * count
    for position, index in enumerate(entry_batch):
        if entry_forced[position]:
            forced_by_batch[index] += entry_delta[position]
    suffix_raw: List[int] = [0] * (count + 1)
    suffix_forced: List[int] = [0] * (count + 1)
    for index in range(count - 1, -1, -1):
        suffix_raw[index] = suffix_raw[index + 1] + raw[index]
        suffix_forced[index] = suffix_forced[index + 1] + forced_by_batch[index]

    # 部屋ごとの「この suffix で最初に現れるエントリ」の膨らみ。土台が切れて
    # いる (forced) ぶんは既に上で数えているので、ここでは足さない。
    pointer: Dict[str, int] = {key: 0 for key in key_entries}
    contribution: Dict[str, int] = {}
    first_total = 0

    def _refresh(key: str, start: int) -> None:
        nonlocal first_total
        positions = key_entries[key]
        cursor = pointer[key]
        while cursor < len(positions) and entry_batch[positions[cursor]] < start:
            cursor += 1
        pointer[key] = cursor
        value = 0
        if cursor < len(positions):
            position = positions[cursor]
            if not entry_forced[position]:
                value = entry_delta[position]
        previous_value = contribution.get(key, 0)
        if value != previous_value:
            first_total += value - previous_value
            contribution[key] = value

    for key in key_entries:
        _refresh(key, 0)

    totals: List[int] = [0] * (count + 1)
    for index in range(count):
        totals[index] = suffix_raw[index] + suffix_forced[index] + first_total
        for key in keys_in_batch[index]:
            _refresh(key, index + 1)
    return totals


def _presented_chars_after_transfer(
    presented: Sequence[Any], *, head_room_key: Optional[str] = None,
) -> int:
    """この並びを提示したときの合計字数 (部屋の様子の開き直しを織り込んだ値)。"""
    if not presented:
        return 0
    return _perception_suffix_totals(presented, head_room_key=head_room_key)[0]


def _plan_perception_drop(
    persona: Any, presented: Sequence[Any], cutoff: int,
    *, model_key: Optional[str] = None, head_room_key: Optional[str] = None,
) -> int:
    """知覚の合計が上の水位を超えていたら、下の水位まで下ろした境界を返す。

    下ろすのは**古い側からまとめて**。一個ずつ下ろす形は、知覚が多い環境で
    新着のたびに提示の前方が書き換わり、ほぼ毎ターン全文が定価の読み直しに
    なる (「窓の並びが変わるのは Metabolism のとき、たまに・まとめて」の
    不変条件 — intent cached_head_architecture)。

    **水位はその回の実行モデルのもの** (``model_key``、beat_execution_context.md
    §3.2 — 各 Session は自分の model の閾値で自分の提示コンテキストを管理する)。
    None のときだけ従来どおり ``persona.model`` へ落ちる。下ろし境界
    (``perception_presentation``) はペルソナ全体で一つのままで、**厳しい水位の
    モデルの回に多く進むのは正常**: 境界は一方向にしか進まないので、緩い
    モデルの回がそれを戻すことはなく、下ろした事実だけが共有される。model ごと
    に境界を分けると、同じ台帳に対して二つの提示が並立し、部屋の様子の土台の
    連なりもモデルごとに別々の切れ方をする (2026-09-05 Codex 三巡 #2)。

    **最新の 1 件は下ろさない**。単独で下の水位を超える一個 (巨大な部屋の様子
    など) を下ろすと、ペルソナはそれを一度も見ないまま失う。その場合は超過を
    許して旗を立てる (裁定 7 と同じ正直な形)。

    測るのは**開き直し後の字数** (:func:`_perception_suffix_totals`)。境界候補
    ごとの残りをあらかじめ一度に組み立て、下の水位に届く (または最新 1 件だけ
    が残る) 境界を確定してから返す。差し替え前の小さい字数で決めてしまうと、
    下ろした直後に上の水位を超えたままになり、新着が無いのに次の呼び出しで
    境界がまた進む。

    Returns: 新しい境界 (下ろすものが無ければ ``cutoff`` のまま)。
    """
    if not presented:
        return cutoff
    model = str(model_key or getattr(persona, "model", "") or "")
    target, high = resolve_perception_watermarks(model)
    if high is None:
        return cutoff  # モデル単位のオプトアウト (下ろしを持たない)
    totals = _perception_suffix_totals(presented, head_room_key=head_room_key)
    running = totals[0]
    if running <= high:
        return cutoff
    new_cutoff = cutoff
    dropped = 0
    for index in range(len(presented) - 1):
        if running <= target:
            break
        new_cutoff = max(new_cutoff, presented[index].id)
        dropped += 1
        running = totals[index + 1]
    persona_id = str(getattr(persona, "persona_id", "?"))
    if running > high:
        warn_key = (persona_id, model)
        with _PERCEPTION_CAP_WARN_LOCK:
            first_time = warn_key not in _PERCEPTION_CAP_WARNED
            if first_time:
                _PERCEPTION_CAP_WARNED.add(warn_key)
        if first_time:
            LOGGER.warning(
                "[sea][perception] the newest perception block alone is over "
                "the presentation cap (%d chars > high %d) for persona=%s "
                "model=%s; keeping it and letting the total run over — the "
                "supply side (one huge block) is what needs fixing, not the "
                "presentation",
                running, high, persona_id, model,
            )
    if dropped:
        LOGGER.info(
            "[sea][perception] presented perception total exceeded the cap "
            "(high=%d, target=%d); dropping %d older batch(es) through id %d "
            "in one step (persona=%s, model=%s, remaining=%d chars)",
            high, target, dropped, new_cutoff, persona_id, model, running,
        )
    return new_cutoff


def list_presented_perception_blocks(
    runtime: Any, persona: Any, recent: Sequence[Dict[str, Any]],
    *,
    anchor_id: Optional[str] = None,
    raise_on_error: bool = False,
    model_key: Optional[str] = None,
    advance_cutoff: bool = True,
    head_room_key: Any = _HEAD_ROOM_UNSET,
) -> List[Dict[str, Any]]:
    """いま提示に差し込まれる知覚ブロックを組む (組成規則の一点管理)。

    「どのバッチをどんな文面で出すか」はここだけに書く。送る側
    (:func:`_merge_consumed_perceptions`) と**測る側**
    (sea/session_lifecycle.py の文字数勘定・退場計画) が同じ関数を呼ぶ —
    勘定が実送信より小さい数字を出していた欠陥
    (docs/issues/context_accounting_excludes_injected_rows.md) の再発は、
    組成規則の二枚目を作らないことでしか防げない。

    - バッチの ``rendered_text`` (消費時に確定したペルソナが見た文面) を
      ``<system>`` 包みでそのまま出す — 生の台帳項目からの再構成 (再 reduce /
      再 format) はしない (reduce で消えた中間状態の復活・秒精度の時刻衝突に
      よるグループ混線の根)。
    - **付記の印** (``annexed_entry_id``) が付いたバッチは提示から下りる — 付記
      されるまで消えないので、下限「退場したものは必ず編纂されている」が提示側
      でも常に成立する。履歴が空でも未付記バッチは提示される。
    - **知覚の合計にも上限がある** (§10.9、2026-09-04 まはー裁定)。提示中の
      ブロックの合計が上の水位を超えたら、古い側を下の水位まで**まとめて**
      下ろし、その境界を一方向に進める。下ろすのは提示だけで台帳は無傷なので、
      その期間の編纂が来れば材料として引き取られる。跡地には省略の印
      (機構名義の一行) を置く — 黙って消さない。判定に使う水位は
      **その回の実行 model** (``model_key``) のもの — 送る側 (prepare_context) も
      測る側 (Metabolism) も実行 model で動くので、ここだけ ``persona.model`` を
      見ていると「実行モデルに保存した知覚の水位が効かない」(2026-09-05 Codex
      三巡 #2)。``model_key`` が無い呼び出しだけ ``persona.model`` へ落ちる。
    - 例外は Chronicle 無効のペルソナ (「編纂なしで忘れる」を選んだ) のみ:
      提示窓 (anchor 境界、無ければ提示最古行) より古いバッチは会話と
      同じように忘れる。**履歴が空でも** anchor が立っていれば絞る — 生ログ窓が
      空の瞬間に過去バッチを全部再提示しない (2026-08-19 Codex 第二巡 #3)。
      anchor も履歴も無い完全ブートストラップだけは全提示 (隠さない側)。
    - **窓絞りで底が抜けた「部屋の様子」の差分は、提示時に全文へ開き直す**
      (:func:`sai_memory.room_state.reopen_lost_bases`)。Chronicle 無効の窓絞り
      は台帳に書ける事実ではない (窓はペルソナと model ごとに動く) ので、台帳側
      の回復 (付記・境界前進と同一 tx) はここまで届かない — トグルを有効から
      無効へ切り替えると、有効な間に積んだ差分が土台なしで提示に残る
      (2026-09-05 四巡目 #1)。開き直しは純関数で、同じ並びからは必ず同じ文面に
      なるので、提示が呼び出しごとに揺れることはない。台帳も確定文面も触らない。
    - **head を土台にした「部屋の様子」の差分も、head が別の部屋を見せていたら
      提示時に全文へ開き直す**。台帳に土台が無くても head がその部屋を見せて
      いれば差分だけを積む (`room_state.build_room_state_push`) が、head は
      (ペルソナ, model) ごとに別々の時点で capture されるので、「その差分の
      部屋の全体像が今この Session に見えているか」は台帳へ書けない。判定に使う
      head は ``head_room_key`` で受け取る — 同じ prompt へ head を載せる
      呼び出し (prepare_context) は**実際に描画した head** の部屋を渡し、head を
      組まない呼び出し (勘定・退場計画・読み取り専用の画面) は省略して
      `_head_room_key` の読み直しに委ねる。どちらの値も、下ろし量の見積もり
      (`_plan_perception_drop`) と開き直しの**両方**へ同じものを渡す — 勘定と
      実送信が別の head を見ると、開き直しで膨らむ量が勘定から漏れる。
    - ``advance_cutoff=False`` は**測るだけ**のモード: 下ろし境界を進める判定は
      同じように行い、進めた**つもり**の提示を返すが、``perception_presentation``
      へは書かない。読み取り専用の画面 (context-status)・仮定の窓の下見
      (読み戻し / 引き戻しのプレビュー)・コンテキストプレビューがこちらを使う —
      境界は一方向で取り消せないので、実際には送らない列で確定させない
      (2026-09-05 四巡目 #6)。返るブロックは進めるモードと同一 (上の開き直しが
      台帳側の回復と同じ結果を与える) なので、勘定と実送信はズレない。
    - 失敗は空リスト + WARN に倒す — 送る側は「マージなし (元の履歴のまま)」、
      測る側は「知覚ぶん 0 (従来値)」へ縮退する。履歴ゼロで走らせるよりも
      知覚欠けの方が被害が小さい。``raise_on_error=True`` は失敗を例外で
      伝える — 透明性の画面 (context-status) が内部失敗を正常なゼロとして
      表示しないための口 (preview_refilled_history の raise_on_error と同じ型。
      Codex 指摘 2026-09-02)。門・発火・送信の各経路は従来どおり fail-open。
    """
    sai_mem = getattr(persona, "sai_memory", None)
    if sai_mem is None or not getattr(sai_mem, "is_ready", lambda: False)():
        return []
    try:
        from sai_memory.perception_buffer import (
            advance_presentation_cutoff,
            count_batch_records,
            get_presentation_cutoff,
            list_unannexed_batches,
        )
        from sai_memory.room_state import reopen_lost_bases
        chronicle_enabled = _chronicle_enabled_for(runtime, persona)
        # 「head がどの部屋を見せているか」は錠前の外で一度だけ確定させる
        # (head の読み口は知覚台帳を触らない)。下ろし量の見積もりと開き直しの
        # 両方が**同じ値**を見るように、ここから両方へ渡す。
        head_room = (
            _head_room_key(persona, model_key)
            if head_room_key is _HEAD_ROOM_UNSET else head_room_key
        )

        def _visible_candidates_locked() -> List[Any]:
            """このペルソナに見える余地のある未付記バッチ (窓絞りまで)。

            下ろした境界より古いものもここには入る — 省略の印の件数を数える
            のに要るため。境界での振り分けは呼び出し側。

            **呼び出し側が ``sai_mem._db_lock`` を保持している前提** (錠前を
            取る層は下の一枚だけ)。
            """
            found = list_unannexed_batches(sai_mem.conn)
            if not found or chronicle_enabled:
                return found
            anchor_key = _anchor_order_key_locked(sai_mem, anchor_id)
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
                return [b for b in found if _in_window(b)]
            oldest: Optional[int] = None
            for msg in recent:
                epoch = _payload_epoch(msg)
                if epoch is not None:
                    oldest = epoch if oldest is None else min(oldest, epoch)
            if oldest is None:
                return found
            return [b for b in found if b.consumed_at >= oldest]

        # 候補・境界・下ろし計画・前進・移管後の読み直し・省略件数の数え上げは
        # **一つのロック区間**で完結させる (2026-09-05 Codex 第二巡 high)。
        # ここを二区間に割ると、
        # 別スレッドの組成 (Pulse と context-status の勘定など) が隙間で境界を
        # 進めて全文を移管し、こちらは「自分では進めていない」ので古い文面の
        # まま新しい境界で振り分ける — 土台 (全文) だけが提示から下り、土台の
        # ない差分が送られる。区間を出たあとは、ここで確定した同一世代の
        # candidates と dropped_through だけから組成する。
        with sai_mem._db_lock:
            candidates = _visible_candidates_locked()
            if not candidates:
                return []
            dropped_through = get_presentation_cutoff(sai_mem.conn)
            planned = _plan_perception_drop(
                persona,
                [b for b in candidates if b.id > dropped_through],
                dropped_through,
                model_key=model_key,
                head_room_key=head_room,
            )
            if planned > dropped_through and not advance_cutoff:
                # 測るだけのモード: 進める判定はここまで同じで、書き込みだけを
                # しない。境界は一方向で取り消せないので、実際には送らない列
                # (読み取り専用の画面・仮定の窓の下見) で確定させない。以降は
                # 「進めたつもり」の境界で振り分ける — 実送信の側が同じ判定で
                # 同じ境界へ進めるので、勘定と実送信は一致する。
                dropped_through = planned
            elif planned > dropped_through:
                try:
                    # 戻り値 = 進めた後の実境界。別プロセスが先へ進めていたら
                    # planned より大きい値が返る (advance は一方向で no-op) —
                    # planned を信じると「もう下ろされたバッチ」を一回だけ
                    # 提示に復活させる (2026-09-05 ローカルレビュー #1)。
                    planned = advance_presentation_cutoff(sai_mem.conn, planned)
                    sai_mem.conn.commit()
                except Exception:
                    try:
                        sai_mem.conn.rollback()
                    except Exception:
                        pass
                    LOGGER.warning(
                        "[sea][perception] could not advance the presentation "
                        "cutoff; presenting every batch as before (persona=%s)",
                        getattr(persona, "persona_id", "?"), exc_info=True,
                    )
                else:
                    # 境界の前進は「部屋の様子」の移管を連れてくる (差分の
                    # 土台が下りたとき、残った最古のエントリを全文へ差し替
                    # える) ので、確定文面を読み直す (§10.8)。同じ区間の中で
                    # 読み直すので、読み直した文面と境界は必ず同じ世代。
                    # 読み直しが落ちたら関数ごと fail-open に倒す (境界だけ
                    # 進んで古い文面を配るくらいなら、知覚ぶん 0 の方が軽い)。
                    dropped_through = planned
                    candidates = _visible_candidates_locked()

            presented = [b for b in candidates if b.id > dropped_through]
            dropped = [b for b in candidates if b.id <= dropped_through]
            # 省略の印に出す件数は**記録の数**であってバッチ数ではない。台帳を
            # 読むので、候補・境界と同じロック区間・同じ世代で数える。
            record_counts = (
                count_batch_records(sai_mem.conn, [b.id for b in dropped])
                if dropped else {}
            )

        # 窓絞り (Chronicle 無効) と「測るだけ」の境界は台帳へ書けないので、
        # そこで土台が抜けた差分はここで全文へ開き直す (純関数・決定論)。
        # Chronicle 有効で境界も進めた回は、台帳側の回復が済んでいるので空。
        reopened = reopen_lost_bases(presented, head_room_key=head_room)

        blocks: List[Dict[str, Any]] = []
        if dropped:
            # 印の位置は「下ろした区間の末尾」= 最後に下ろしたバッチの時刻。
            # 提示に残る最古のブロックを追い越さないよう抑える (マージは
            # 時刻昇順の並びを前提にする)。
            mark_at = max(b.consumed_at for b in dropped)
            if presented:
                mark_at = min(mark_at, presented[0].consumed_at)
            blocks.append(_perception_omission_block(
                dropped, mark_at, chronicle_enabled=chronicle_enabled,
                records=sum(record_counts.get(b.id, 1) for b in dropped),
            ))
        for batch in presented:
            metadata: Dict[str, Any] = {
                # 旧 flush の event_message 行と同型のタグ + マージ由来の目印。
                "tags": ["internal", "event_message", "perception"],
                CONSUMED_PERCEPTION_KEY: True,
                "__perception_batch_id__": batch.id,
            }
            media = batch.media_list()
            if media:
                metadata["media"] = media
            blocks.append({
                "role": "user",
                "content": _perception_block_text(
                    reopened.get(batch.id, batch.rendered_text)
                ),
                "created_at": batch.consumed_at,
                "metadata": metadata,
            })
        return blocks
    except Exception:
        if raise_on_error:
            raise
        LOGGER.warning(
            "[sea][prepare-context] perception batch listing failed; "
            "presenting/counting history without perceptions", exc_info=True,
        )
        return []


def merge_perception_blocks(
    recent: Sequence[Dict[str, Any]], blocks: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """生ログと知覚ブロックを時刻順にマージした列を返す (純関数)。

    ブロックは「自分の ``consumed_at`` 以下の生ログ」の直後に入る (同時刻なら
    生ログが先 — 消費は書き込みの後に起きた事実)。送る側と測る側が**同じ並び**
    を見るための一点。
    """
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
    return merged


def _merge_consumed_perceptions(
    runtime: Any, persona: Any, recent: List[Dict[str, Any]],
    *,
    anchor_id: Optional[str] = None,
    model_key: Optional[str] = None,
    advance_cutoff: bool = True,
    head_room_key: Any = _HEAD_ROOM_UNSET,
) -> List[Dict[str, Any]]:
    """提示履歴に未付記の消費バッチを時刻順マージする (W14, §10.3)。

    組成は :func:`list_presented_perception_blocks`、並びは
    :func:`merge_perception_blocks` — ここは送信経路の入口として二つを繋ぐ
    だけで、規則そのものは持たない。``model_key`` はこの context を届ける
    Session の実行 model (知覚の水位の主語)。

    ``advance_cutoff=False`` は測るだけ (下ろし境界を書かない) — 組み立てが
    プレビュー (``preview_only``) の回に使う。プレビューの列は送られないので、
    一方向にしか進まない境界をそこで確定させない。返るブロックは同じ。

    ``head_room_key`` は同じ prompt へ載せる head が見せている部屋のキー
    (:func:`_pinned_head_room_key`)。省略すると組成側が自分で読み直す。
    """
    blocks = list_presented_perception_blocks(
        runtime, persona, recent, anchor_id=anchor_id, model_key=model_key,
        advance_cutoff=advance_cutoff, head_room_key=head_room_key,
    )
    if not blocks:
        return recent
    merged = merge_perception_blocks(recent, blocks)
    LOGGER.debug(
        "[sea][prepare-context] merged %d perception batch block(s) "
        "into history", len(blocks),
    )
    return merged


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
            from sai_memory.room_state import ensure_room_state_base
            with sai_mem._db_lock:
                pending = list_pending(sai_mem.conn)
                # 実 flush と同じ順・同じ開き直し (reduce → 土台を失った部屋の
                # 差分は全文へ)。読むだけ — 行は触らない。
                reduced_pending = (
                    ensure_room_state_base(sai_mem.conn, reduce_perceptions(pending))
                    if pending else []
                )
            if reduced_pending:
                pb_text = format_perception_message(reduced_pending)
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
