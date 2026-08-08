"""予算付き作業セッションランナー (自律行動 v2 §4.3 の心臓部)。

ペルソナが「実在の対象に働きかけ、世界の返事を見て次を決める」ための
act→observe ループを、ラウンド予算付きで運転する。実体は既存の
``sea.runtime_llm._run_spell_loop`` (多ラウンド spell 実行) であり、本モジュールは
それに以下を付けた運転形態を提供する:

- **WORKER アスペクトの予算付き運転**: セッションは WORKER アスペクト
  (sub_line / volatile / lightweight, ``sea.pulse_context.Aspect``) のライン
  として走る。新アスペクトは作らない。
- **指示書注入**: ``instruction`` をセッションラインの最初のメッセージ
  (head 直後の user + ``<system>`` タグ) として注入する。head は既存
  sub_line と同じくペルソナの (persona, model) 固定 head をそのまま使う。
- **ラウンド予算**: ``budget_rounds`` を ``_run_spell_loop(max_rounds=...)``
  に渡す。env グローバル ``SAIVERSE_SPELL_MAX_ROUNDS`` の既存挙動は不変。
- **終了時のアーティファクト収集**: セッション前後の Item テーブル差分
  (CREATOR_ID = ペルソナ) で「このセッションが実際に作ったもの」を収集。
  - ダイジェストは本モジュールでは**生成しない** (W1 Chunk C / D9,
    judgment_points.md §6 改定 2026-07-18)。セッション終了判断 (post_session)
    が原本 (origin_episode で引ける生ログ) を見て digest 欄に書き、
    judgment_finalize → 実行台帳の配送 handler (``saimemory.append_digest``)
    が committed (main_line, :data:`DIGEST_TAG`) で SAIMemory に届ける。
    出来事 (Episode) は ``digest_ref=None`` で閉じ、配送成功時に handler が
    ``episodes.set_digest_ref`` で再訪の鍵を後段確定する。
  - セッションの生ログ (各ラウンドのやり取り) は WORKER アスペクト
    由来の scope='volatile' で保存され、メインライン context には乗らない
    (sea/runtime_context.py: required_scopes=['committed'])。
  - 作業メモ (desk_memo — Track への状態メモ) は呼び出し側 (セッション終了判断,
    judgment_points.md §6) の責務であり本モジュールでは扱わない。

記憶の接地原則 (autonomous_behavior_v2.md §3-1, §8-5):
committed される要約は post_session 判断が書く digest 1 件のみで、その
状況テキストは「セッションの記録に基づき、実際に起きたことだけを書く」
ことを明示する (旧 digest 専用コールの規律を判断点へ移植)。

時刻はすべて ``saiverse.clock.now()`` を読む (v2 §12 の不変条件)。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Set

from saiverse import clock
from saiverse.usage_tracker import get_usage_tracker

LOGGER = logging.getLogger(__name__)

#: SAIMemory 上での識別子 (messages.metadata.tags の ``playbook:<name>`` と
#: usage_tracker の playbook_name に載る)。実 Playbook ではなく運転形態の名前。
WORK_SESSION_PLAYBOOK_NAME = "work_session"

#: セッション生ログ (volatile) の意味分類タグ。track_autonomous の
#: ``autonomous_pulse`` と同じく「どの活動由来か」を示す純粋な意味分類で、
#: context 制御は line_role / scope 側が担う (Phase 3 段階 4-C の責務分離)。
RAW_LOG_TAG = "work_session"

#: committed されるダイジェスト 1 件の意味分類タグ。生成は post_session 判断
#: (builtin_data/tools/judgment_finalize.py)、配送は台帳 handler
#: (saiverse/execution_ledger_wiring.py の ``saimemory.append_digest``)。
#: 読み手 (day_close の収集 / 一日新聞) はこのタグ定数を参照し続ける。
DIGEST_TAG = "session_digest"

ENDED_FINISHED = "finished"
ENDED_BUDGET_EXHAUSTED = "budget_exhausted"
ENDED_ERROR = "error"


@dataclass
class WorkSessionResult:
    """作業セッション 1 本の結果 (セッション終了判断への入力契約)。

    digest フィールドは W1 Chunk C (D9) で廃止 — digest は post_session 判断が
    原本から書く (本 dataclass はセッションの機械的事実だけを運ぶ)。

    Attributes:
        artifacts: このセッション中に作られた成果物の ref (Item ID) リスト。
        rounds_used: 実際に消費したラウンド数 (spell 実行→再呼び出しの往復)。
        ended_reason: ``finished`` (スペルなし応答で自然終了) /
            ``budget_exhausted`` (予算上限で打ち切り) / ``error``。
        usage: Pulse 単位の usage accumulator のコピー
            (total_input_tokens / total_output_tokens / total_cost_usd /
            call_count / models_used 等)。
        task_ref: 呼び出し時に渡された対象タスク参照 (透過)。
        track_id: 呼び出し時に渡された所属 Track ID (透過)。track:N コマ
            (P5 コマ参照の任意階層化) では判断点がここから desk_memo /
            track_op の対象を読む。
        episode_ref: このセッションの出来事 (kind='work_session') の参照。
            セッション終了判断の層2 棚入れ (episode_purposes) と原本注入
            (D9-2) の対象。
        error: ended_reason='error' のときの ``型名: メッセージ``。
        started_at / ended_at: ISO 形式時刻 (saiverse.clock.now() 由来)。
        budget_rounds: 呼び出し時のラウンド予算 (透過)。digest メッセージの
            work_session metadata (判断点が復元する ws_meta) に載る。
        extra: 呼び出し時に渡された追加 metadata (透過。day_plan のコマ情報等)。
            旧 digest 保存の ``metadata.work_session.extra`` と同じ内容。
    """

    artifacts: List[str]
    rounds_used: int
    ended_reason: str
    usage: Dict[str, Any] = field(default_factory=dict)
    task_ref: Optional[str] = None
    track_id: Optional[str] = None
    episode_ref: Optional[str] = None
    error: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    budget_rounds: Optional[int] = None
    extra: Optional[Dict[str, Any]] = None


@dataclass
class SessionCloseContext:
    """締めフック (``close_hook``) に渡す、セッション終了直後の実行文脈。

    コマ締めの一手 (帰属判定 + 経験値ノート、timetable_redesign.md §5.4 /
    experience_ledger.md §4) が「セッションと同じ model・同じ messages
    (直前コールで prefix cache が熱い)」のまま構造化出力を 1 発追加するための
    受け渡し。``messages`` はセッションの生文脈そのもの (head + 履歴 + 指示書 +
    各ラウンド) — 最終応答 (``final_continuation``) はループが append しない
    ため別フィールドで渡す (使う側が assistant として積んでから続ける)。
    """

    manager: Any
    persona: Any
    runtime: Any
    llm_client: Any
    execution_context: Any
    state: Dict[str, Any]
    messages: List[Dict[str, Any]]
    pulse_ctx: Any
    building_id: str
    episode_ref: Optional[str]
    artifacts: List[str]
    rounds_used: int
    ended_reason: str
    final_continuation: str


def run_work_session(
    persona_id: str,
    instruction: str,
    budget_rounds: int,
    task_ref: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    *,
    manager: Optional[Any] = None,
    track_id: Optional[str] = None,
    title: Optional[str] = None,
    close_hook: Optional[Callable[[SessionCloseContext], None]] = None,
    profile: str = "work",
) -> WorkSessionResult:
    """指示書とラウンド予算を渡して作業セッションを 1 本運転する。

    Args:
        persona_id: セッションを走らせるペルソナ。
        instruction: 指示書 (目的・参照すべき記憶や資料・成果物の形式・
            完成条件)。セッションラインの最初のメッセージとして注入される。
        budget_rounds: ラウンド予算 (spell 実行→結果確認の往復の上限)。
        task_ref: 対象タスクの参照 (例 ``task:3``)。結果とダイジェスト
            metadata に透過するだけで、本モジュールはタスク操作をしない
            (タスクの裁定はセッション終了判断の責務。また WORKER アスペクト
            では task 操作スペル自体がモードゲートで拒否される)。
        metadata: ダイジェストの SAIMemory metadata に添える追加情報。
        manager: SAIVerseManager。省略時は ``tools.context.get_active_manager()``
            (ツール実行中の contextvar) から解決する。
        track_id: セッションが属する Track の ID (あれば)。ラインの
            origin_track_id として記録される。
        title: セッションの短い表題 (コマの title / タスク題)。出来事
            (Episode) の ``meta.title`` に透過する (meta 書式契約
            life_concept_map.md §14)。省略可。
        close_hook: セッション終了直後 (Beat ロック内・prefix cache が熱い
            まま) に一度だけ呼ばれる締めの一手 (:class:`SessionCloseContext`
            を受ける)。コマ締めの帰属判定 + 経験値ノート (T4) の注入点。
            フックの失敗はセッションの結果を壊さない (WARNING + スキップ)。
        profile: ``"work"`` (既定) / ``"life"``。運転機構は同一で、暮らし
            プロファイル (autonomous_pulse_vehicle.md) は次の 2 点だけ変わる:
            指示書の包みが許可形 (作業ルール・成果義務を課さない) になり、
            kind='work_session' の出来事を開かない (コマ発火が開いた
            kind='presence' が実行区間の記録を既に持つため)。予算・締めの
            有無は既存パラメータ (budget_rounds / close_hook) で表現する。

    Returns:
        WorkSessionResult。例外は握り潰さず LOGGER に記録した上で
        ``ended_reason='error'`` の結果として返す (呼び出し側の判断点が
        セッション失敗も含めて裁定できるようにするため raise しない)。
    """
    started_at = clock.now()
    usage_accumulator: Dict[str, Any] = {
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cached_tokens": 0,
        "total_cache_write_tokens": 0,
        "total_cost_usd": 0.0,
        "call_count": 0,
        "models_used": [],
    }
    rounds_used = 0
    runtime: Optional[Any] = None
    persona: Optional[Any] = None
    pulse_ctx: Optional[Any] = None
    frame_pushed = False
    items_before: Optional[Set[str]] = None
    episode_ref: Optional[str] = None  # 開いた出来事の参照 (close 用)

    try:
        # ---- setup: manager / persona / runtime ----
        if manager is None:
            manager = _resolve_manager_from_context()
        if manager is None:
            raise RuntimeError(
                "manager is not available (pass manager= or call within a tool context)"
            )
        if not instruction or not str(instruction).strip():
            raise ValueError("instruction must be a non-empty string")
        if not isinstance(budget_rounds, int) or budget_rounds < 1:
            raise ValueError(f"budget_rounds must be a positive int (got {budget_rounds!r})")
        if profile not in ("work", "life"):
            raise ValueError(f"profile must be 'work' or 'life' (got {profile!r})")

        personas = getattr(manager, "personas", {}) or {}
        persona = personas.get(persona_id)
        if persona is None:
            raise RuntimeError(f"persona '{persona_id}' not found on manager")
        runtime = getattr(manager, "sea_runtime", None)
        if runtime is None:
            raise RuntimeError("SEA runtime is not available on manager")
        building_id = getattr(persona, "current_building_id", None)
        if not building_id:
            raise RuntimeError(f"persona '{persona_id}' has no current building")

        # ---- artifact capture: セッション開始時点の Item ID スナップショット ----
        # 採用方式は DB 差分 (開始時 ID 集合 vs 終了時 ID 集合)。時刻比較は
        # 使わない: Item.CREATED_AT は datetime.utcnow() (naive UTC) で刻まれる
        # 一方 clock.now() は naive ローカル時刻であり直接比較できず、仮想
        # クロック (シムモード) 下では実時刻刻印と必ずズレるため。ID 集合差分
        # なら時計に依存しない。
        items_before = _snapshot_item_ids(manager, persona_id)

        # ---- 出来事 (Episode) を開く: kind='work_session' ----
        # コマ発火から呼ばれた場合、開いている kind='slot' の出来事が「出自」。
        # コマ外 (直接呼び出し) では task_ref を出自として残す。出来事は記録
        # 専用でセッションの運転には影響しない — 失敗は WARN のみ。
        # 暮らしプロファイルは開かない: コマ発火が開いた kind='presence' が
        # 実行区間の記録を既に持ち、二重の出来事は経験台帳の読みを濁す。
        if profile == "work":
            episode_ref = _open_ws_episode(
                manager, persona_id, building_id, task_ref=task_ref, title=title,
            )

        # ---- PulseContext + WORKER ライン ----
        pulse_id = str(uuid.uuid4())
        pulse_ctx = runtime._get_or_create_pulse_context(pulse_id)
        from sea.pulse_context import Aspect, PulseLogEntry, resolve_execution_context

        pulse_ctx.push_line(aspect=Aspect.WORKER, track_id=track_id)
        frame_pushed = True

        # WORKER フレームが active な状態で解決 → 軽量モデルが導出される
        # (Beat 相当の開始点、beat_execution_context §2.1。挙動不変の置換)。
        # _prepare_context より先に解決するのは、head を同じ model の Session
        # (persona, model) に向けて render するため (§3.1)。
        execution_context = resolve_execution_context(persona, pulse_ctx)

        # Beat ロック (beat_execution_context.md §3.4): 組成〜初回 LLM〜ループを
        # 一つの hold で persona の直列域に入れる。§14-2 以降、context 組成は
        # anchor 前進という書き込みを持ちうるため、ロック外で組むと並走 Beat の
        # 前進と交錯する。初回呼び出しをロック外に置くと、その touch が前進の
        # 後から届いて anchor を巻き戻す (Codex 5巡目 2026-07-30)。ループ内の
        # boundary が周ごとにロックを手放すため、会話 Beat は周の合間に挟まれる
        # (「作業中に話しかけたら応答」の土台)。締めの生ログ保存〜成果物収集も
        # 最後の Beat の記録として同じ hold 内で行う (記録はロック下で、の
        # 不変条件)。関所 fail-closed (BeatGateClosedError) は外側の except
        # Exception が拾い ended_reason='error' になる (関所が初回 LLM 課金より
        # 先に来るので、閉じた関所の陰で課金だけ発生することもない)。manager に
        # beat_gate が無いテスト環境では no-op。
        from sea.beat_gate import hold_beat
        from sea.runtime_llm import _parse_spell_lines, _run_spell_loop

        with hold_beat(manager, persona_id, purpose="work_session"):
            # ---- Beat 頭の知覚消費 (perception_buffer.md §4.2) ----
            # 作業セッションの開始も Beat の頭。直前の移動で届いた「移動先の
            # 様子」やフィードの未読を、最初の生成が見るコンテキストに入れる。
            # ここで SAIMemory へ書き出してから _prepare_context を組むので、
            # 続く履歴読みが自然に拾う (run_meta_user と同じ並び — こちらは
            # 作業中コンテキストへの差し込みが要らない)。周の切れ目の消費
            # (_run_spell_loop) だけでは、スペルの無い応答や spell 無効の
            # ペルソナでセッション中ずっと消費されない (Codex レビュー、
            # 2026-08-08)。子ライン相当の入れ子では消費しない (§4.2 (a):
            # 子は親 Beat の一部)。失敗は WARN で続行 — 知覚は永続バッファに
            # 残り、次の Beat 頭で再試行される。
            try:
                _gate = getattr(manager, "beat_gate", None)
                _outermost = (
                    _gate is None
                    or not persona_id
                    or _gate.held_depth(persona_id) == 1
                )
                _sai_mem = getattr(persona, "sai_memory", None)
                _flush_fn = getattr(_sai_mem, "flush_perception_buffer", None)
                if _outermost and _flush_fn is not None:
                    _flush_fn()
            except Exception:
                LOGGER.warning(
                    "[work_session] Beat-head perception flush failed for %s; "
                    "continuing", persona_id, exc_info=True,
                )

            # ---- context: 通常のペルソナ文脈 (head + 会話履歴) + 指示書 ----
            # 作業セッションの出力はペルソナ本人の発話・思考として記録される
            # (assistant role + session_digest / post_session 判断の材料) ため、
            # 会話履歴を外して走らせてはいけない = persona_voiced=True。
            #
            # 2026-07-23 以前はここで ContextRequirements(history_depth=0, ...) を
            # 直に組み、履歴なしで走らせていた。run_playbook のサブライン (通常の
            # WORKER) が親メインラインの履歴をコピーして引き継ぐのに対し、コマ発火
            # から起動する作業セッションには親がおらず、その代わりを組まずに「空」を
            # 選んでいたのが原因。結果、エアは 3 日連続で同じ壁に初見でぶつかり、
            # 毎回「システムへの理解が足りていない」と自責を記録した。
            # 詳細: docs/issues/llm_call_entry_point_standardization.md
            _context_meta: Dict[str, Any] = {}
            base_messages = runtime._prepare_context(
                persona, building_id, None, pulse_id=pulse_id,
                model_key=execution_context.model_key,
                context_meta=_context_meta,
                persona_voiced=True,
            )
            if profile == "life":
                instruction_content = _build_life_instruction_message(
                    str(instruction).strip(), budget_rounds
                )
            else:
                instruction_content = _build_instruction_message(
                    str(instruction).strip(), budget_rounds, Aspect.WORKER.mode_display_name
                )
            messages: List[Dict[str, Any]] = list(base_messages)
            messages.append({"role": "user", "content": instruction_content})

            state: Dict[str, Any] = {
                "_pulse_id": pulse_id,
                "_pulse_type": "work_session",
                "_pulse_context": pulse_ctx,
                "_pulse_usage_accumulator": usage_accumulator,
                "_activity_trace": [],
                "_cancellation_token": None,
                "_messages": messages,
                # call-local anchor (§3.2)。2026-07-23 以降 prefix は main-line
                # 履歴を含むため、通常は実 anchor が載る (touch は Beat 内)。
                "_prefix_anchor_id": _context_meta.get("prefix_anchor_id"),
            }
            # node_def / playbook は _run_spell_loop・_store_memory が参照する
            # 最小属性 (memorize.tags / name) だけを持つ軽量スタブ。
            node_def = SimpleNamespace(
                id="work_session_llm",
                memorize={"tags": [RAW_LOG_TAG]},
                speak=False,
            )
            playbook_ref = SimpleNamespace(name=WORK_SESSION_PLAYBOOK_NAME)

            spell_enabled = bool(runtime._is_spell_enabled_for_persona(persona))
            if not spell_enabled:
                LOGGER.warning(
                    "[work_session] spells are disabled for persona=%s — the session "
                    "cannot act on the world and will end after one response",
                    persona_id,
                )

            # 上で解決済みの ExecutionContext を state へ載せる (下流は state 経由で読む)。
            state["_execution_context"] = execution_context
            llm_client, _selected_model = runtime.select_llm_client(
                node_def, persona, execution_context=execution_context, state=state,
            )
            if _selected_model != execution_context.model_key:
                execution_context = execution_context.with_model(_selected_model)
                state["_execution_context"] = execution_context

            pulse_ctx.append(PulseLogEntry(
                role="user", content=instruction_content,
                node_id="work_session_instruction",
                playbook_name=WORK_SESSION_PLAYBOOK_NAME,
            ))

            # ---- 初回 LLM 呼び出し ----
            LOGGER.info(
                "[work_session] start: persona=%s profile=%s budget=%d task_ref=%s "
                "track_id=%s pulse_id=%s",
                persona_id, profile, budget_rounds, task_ref, track_id, pulse_id,
            )
            initial_result = llm_client.generate(
                messages,
                tools=[],
                temperature=runtime._default_temperature(persona),
                **runtime._get_cache_kwargs(persona_id),
            )
            _record_llm_usage(
                runtime, state, llm_client, persona, building_id, "llm_work_session"
            )
            text = _extract_text(initial_result)
            try:
                runtime._dump_llm_io(
                    WORK_SESSION_PLAYBOOK_NAME, "work_session_llm", persona, messages, text,
                )
            except Exception:
                LOGGER.warning("[work_session] failed to dump initial LLM I/O", exc_info=True)

            # ---- act→observe ループ (予算付き) ----
            merged_text, continuation, rounds_used = _run_coro_sync(_run_spell_loop(
                text=text,
                spell_enabled=spell_enabled,
                llm_client=llm_client,
                runtime=runtime,
                persona=persona,
                building_id=building_id,
                state=state,
                messages=messages,
                playbook=playbook_ref,
                event_callback=None,
                node_def=node_def,
                action_text=instruction_content,
                max_rounds=budget_rounds,
            ))

            # 予算切れ判定: ループが予算上限に達し、かつ最終応答にまだ spell が
            # 残っている (= 続きをやりたがっていた) とき budget_exhausted。
            # 予算内で spell の無い応答が来た場合は自然終了 (finished)。
            budget_exhausted = False
            if rounds_used >= budget_rounds and continuation:
                try:
                    budget_exhausted = bool(_parse_spell_lines(continuation, quiet=True))
                except Exception:
                    LOGGER.warning(
                        "[work_session] failed to parse final continuation for "
                        "budget-exhaustion check", exc_info=True,
                    )
            ended_reason = ENDED_BUDGET_EXHAUSTED if budget_exhausted else ENDED_FINISHED

            # 締めの発話 (spell を含まない最終 continuation) も生ログの一部として
            # volatile で保存する (WORKER フレームが active なので scope は
            # volatile に解決される)。ラウンド途中の発話は _run_spell_loop が
            # 同様に保存済み。
            if continuation and continuation.strip():
                runtime._store_memory(
                    persona, continuation, role="assistant",
                    tags=[RAW_LOG_TAG], pulse_id=pulse_id,
                    playbook_name=WORK_SESSION_PLAYBOOK_NAME,
                    pulse_context=pulse_ctx,
                )
                pulse_ctx.append(PulseLogEntry(
                    role="assistant", content=continuation,
                    node_id="work_session_final",
                    playbook_name=WORK_SESSION_PLAYBOOK_NAME,
                ))

            # ---- artifacts: 終了時点との差分 ----
            artifacts = _collect_new_item_ids(manager, persona_id, items_before)

            # ---- コマ締めの一手 (T4: 帰属判定 + 経験値ノート) ----
            # セッションと同じ messages / model のまま (直前コールで prefix が
            # 熱い — gold_panning の defer-to-hot と同じ理由) 呼び出し側の
            # close_hook に構造化出力 1 発を委ねる。hold_beat の内側 = 記録は
            # ロック下で、の不変条件に従う。締めは油であって燃料ではない —
            # 失敗はコマ (セッション) の完了を壊さない。
            if close_hook is not None:
                try:
                    close_hook(SessionCloseContext(
                        manager=manager,
                        persona=persona,
                        runtime=runtime,
                        llm_client=llm_client,
                        execution_context=execution_context,
                        state=state,
                        messages=messages,
                        pulse_ctx=pulse_ctx,
                        building_id=building_id,
                        episode_ref=episode_ref,
                        artifacts=artifacts,
                        rounds_used=rounds_used,
                        ended_reason=ended_reason,
                        final_continuation=continuation or "",
                    ))
                except Exception:
                    LOGGER.warning(
                        "[work_session] close hook failed (persona=%s) — "
                        "session result unaffected", persona_id, exc_info=True,
                    )

            ended_at = clock.now()

        # ---- 出来事を閉じる (meta 書式契約: title / artifacts) ----
        # digest は本モジュールでは生成しない (D9)。digest_ref=None で閉じ、
        # post_session 判断→配送 handler が set_digest_ref で後段確定する。
        _close_ws_episode(
            manager, persona_id, episode_ref,
            title=title, artifacts=artifacts,
            ended_reason=ended_reason, digest_ref=None,
        )

        LOGGER.info(
            "[work_session] end: persona=%s ended_reason=%s rounds=%d/%d artifacts=%d",
            persona_id, ended_reason, rounds_used, budget_rounds, len(artifacts),
        )
        return WorkSessionResult(
            artifacts=artifacts,
            rounds_used=rounds_used,
            ended_reason=ended_reason,
            usage=dict(usage_accumulator),
            task_ref=task_ref,
            track_id=track_id,
            episode_ref=episode_ref,
            error=None,
            started_at=started_at.isoformat(timespec="seconds"),
            ended_at=ended_at.isoformat(timespec="seconds"),
            budget_rounds=budget_rounds,
            extra=dict(metadata) if metadata else None,
        )
    except Exception as exc:
        LOGGER.exception(
            "[work_session] session failed (persona=%s task_ref=%s)",
            persona_id, task_ref,
        )
        artifacts: List[str] = []
        if manager is not None and items_before is not None:
            try:
                artifacts = _collect_new_item_ids(manager, persona_id, items_before)
            except Exception:
                LOGGER.warning(
                    "[work_session] artifact collection failed on error path",
                    exc_info=True,
                )
        # エラー終了でも出来事は閉じる (実行区間は終わった — 事実の記録)。
        _close_ws_episode(
            manager, persona_id, episode_ref,
            title=title, artifacts=artifacts,
            ended_reason=ENDED_ERROR, digest_ref=None,
        )
        return WorkSessionResult(
            artifacts=artifacts,
            rounds_used=rounds_used,
            ended_reason=ENDED_ERROR,
            usage=dict(usage_accumulator),
            task_ref=task_ref,
            track_id=track_id,
            episode_ref=episode_ref,
            error=f"{type(exc).__name__}: {exc}",
            started_at=started_at.isoformat(timespec="seconds"),
            ended_at=clock.now().isoformat(timespec="seconds"),
            budget_rounds=budget_rounds if isinstance(budget_rounds, int) else None,
            extra=dict(metadata) if metadata else None,
        )
    finally:
        if pulse_ctx is not None:
            if frame_pushed:
                try:
                    pulse_ctx.pop_line()
                except Exception:
                    LOGGER.exception("[work_session] failed to pop WORKER line frame")
            if runtime is not None and persona is not None:
                try:
                    runtime._flush_pulse_logs(persona, pulse_ctx)
                except Exception:
                    LOGGER.exception("[work_session] failed to flush pulse logs")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _open_ws_episode(
    manager: Any,
    persona_id: str,
    building_id: Optional[str],
    *,
    task_ref: Optional[str],
    title: Optional[str],
) -> Optional[str]:
    """kind='work_session' の出来事を開き、episode_ref を返す (失敗時 None)。

    出自 (origin_ref): 開いている kind='slot' の出来事 (コマ発火の実行区間 —
    day_plan._fire_slot が開く) があればその参照。コマ外の直接呼び出しでは
    task_ref。どちらも無ければ None (= 自発)。
    """
    try:
        from saiverse import episodes

        origin_ref: Optional[str] = None
        slot_ep = episodes.get_open_episode(
            manager, persona_id, kind=episodes.KIND_SLOT,
        )
        if slot_ep is not None and slot_ep.get("episode_ref"):
            origin_ref = slot_ep["episode_ref"]
        elif task_ref:
            origin_ref = task_ref

        meta = {"title": title} if title else None
        ep = episodes.open_episode(
            manager, persona_id, episodes.KIND_WORK_SESSION,
            building_id=building_id,
            participants=[persona_id],
            origin_ref=origin_ref,
            meta=meta,
        )
        return ep.get("episode_ref")
    except Exception:
        LOGGER.warning(
            "[work_session] failed to open episode (persona=%s) — "
            "session continues without it", persona_id, exc_info=True,
        )
        return None


def _close_ws_episode(
    manager: Any,
    persona_id: str,
    episode_ref: Optional[str],
    *,
    title: Optional[str],
    artifacts: List[str],
    ended_reason: str,
    digest_ref: Optional[str],
) -> None:
    """作業セッションの出来事を閉じる (meta 書式契約 life_concept_map.md §14)。

    ``meta.title`` / ``meta.artifacts`` はフロント (lib/episodeText.ts) が読む
    キー名 — 変えないこと。事実の記録のみを書く (意味づけは書かない §9)。
    """
    if not episode_ref or manager is None:
        return
    try:
        from saiverse import episodes

        meta: Dict[str, Any] = {
            "artifacts": list(artifacts),
            "ended_reason": ended_reason,
        }
        if title:
            meta["title"] = title
        episodes.close_episode(
            manager, persona_id, episode_ref,
            digest_ref=digest_ref, meta=meta,
        )
    except Exception:
        LOGGER.warning(
            "[work_session] failed to close episode %s (persona=%s)",
            episode_ref, persona_id, exc_info=True,
        )


def _resolve_manager_from_context() -> Optional[Any]:
    """ツール実行 contextvar からアクティブな manager を引く (無ければ None)。"""
    try:
        from tools.context import get_active_manager
        return get_active_manager()
    except Exception:
        LOGGER.debug("[work_session] tools.context manager resolution failed", exc_info=True)
        return None


def _build_instruction_message(
    instruction: str, budget_rounds: int, mode_name: str
) -> str:
    """指示書をセッションラインの最初のメッセージとして整形する。

    Playbook の action と同じ「user role + <system> タグ」統一形式
    (プロバイダ互換のための対策済み形式、sea/runtime_llm.py の解説参照)。
    """
    return (
        "<system>"
        f"あなたは今「{mode_name}」で作業セッションを開始します。\n"
        "以下の指示書に従い、スペルを使って実際に作業してください。\n\n"
        "【指示書】\n"
        f"{instruction}\n\n"
        "【セッションのルール】\n"
        f"- 使えるラウンド数（スペル発動と結果確認の往復）は最大 {budget_rounds} 回です。\n"
        "- スペルの実行結果を確認してから次の手を決めてください。\n"
        "- 指示書の作業が終わったら、スペルを含まない短い締めの言葉で終了してください。\n"
        "- 実行していない作業を「やった」と書かないでください。\n"
        "</system>"
    )


def _build_life_instruction_message(situation: str, budget_rounds: int) -> str:
    """暮らしプロファイルの状況提示 (autonomous_pulse_vehicle.md §A)。

    作業の指示書と違い、目的・成果義務・作業ルールを課さない。許可形のみで
    書く (義務形は充填独白 v1 を呼び戻す — timetable_redesign §5.5)。
    形式は :func:`_build_instruction_message` と同じ「user role + <system>」。
    """
    spell_line = (
        f"気になったことがあればスペルを使ってもかまいません（最大 {budget_rounds} 回）。"
        if budget_rounds > 0 else ""
    )
    return (
        "<system>"
        f"{situation}\n"
        "この時間の過ごし方は自由です。目に入るもの・流れてきたものを眺めて、"
        "感じたことがあればそのまま書き留めておけます。何かを作ったり成果を"
        f"出したりする必要はありません。{spell_line}\n"
        "</system>"
    )


def _extract_text(result: Any) -> str:
    """LLM client の generate 戻り値 (str または dict) から text を取り出す。"""
    if isinstance(result, dict):
        return str(result.get("content") or "")
    if isinstance(result, str):
        return result
    return ""


def _record_llm_usage(
    runtime: Any,
    state: Dict[str, Any],
    llm_client: Any,
    persona: Any,
    building_id: str,
    node_type: str,
    playbook_name: str = WORK_SESSION_PLAYBOOK_NAME,
) -> None:
    """LLM 1 コール分の usage を計上する (usage_tracker + Pulse accumulator)。

    sea/runtime_llm.py の spell-retry usage 計上と同じ形。usage が無い
    (mock クライアント等) 場合は no-op。tell スペルの 1 Beat
    (builtin_data/tools/tell.py) も同じ器で計上する — playbook_name だけ変わる。
    """
    usage = llm_client.consume_usage() if hasattr(llm_client, "consume_usage") else None
    if not usage:
        return
    persona_id = getattr(persona, "persona_id", None)
    get_usage_tracker().record_usage(
        model_id=usage.model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cached_tokens=usage.cached_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        cache_ttl=usage.cache_ttl,
        persona_id=persona_id,
        building_id=building_id,
        node_type=node_type,
        playbook_name=playbook_name,
        category="persona_speak",
    )
    from sea.runtime_llm import _maybe_record_cache_storage
    _maybe_record_cache_storage(usage, persona_id, building_id)
    from saiverse.model_configs import calculate_cost
    cost = calculate_cost(
        usage.model, usage.input_tokens, usage.output_tokens,
        usage.cached_tokens, usage.cache_write_tokens, cache_ttl=usage.cache_ttl,
    )
    runtime._accumulate_usage(
        state, usage.model, usage.input_tokens, usage.output_tokens, cost,
        usage.cached_tokens, usage.cache_write_tokens,
    )
    try:
        # anchor は call-local (§3.2)。2026-07-23 以降 prefix は main-line 履歴を
        # 含むため、通常は実 anchor が載る。呼び出し面は hold_beat の内側
        # (Codex 5巡目 2026-07-30) — Beat 内 touch は前進と直列化済みで CAS 不要。
        runtime.session_lifecycle.touch_anchor_after_llm_call(
            persona, usage, anchor_id=state.get("_prefix_anchor_id"),
        )
    except Exception:
        LOGGER.debug("[work_session] anchor touch failed (non-fatal)", exc_info=True)


def _snapshot_item_ids(manager: Any, persona_id: str) -> Optional[Set[str]]:
    """ペルソナが CREATOR_ID の Item ID 集合を返す。失敗時は None (収集不能)。"""
    session_factory = getattr(manager, "SessionLocal", None)
    if session_factory is None:
        LOGGER.warning(
            "[work_session] manager has no SessionLocal — artifact capture disabled"
        )
        return None
    try:
        db = session_factory()
    except Exception:
        LOGGER.exception("[work_session] failed to open DB session for artifact snapshot")
        return None
    try:
        from database.models import Item
        rows = db.query(Item.ITEM_ID).filter(Item.CREATOR_ID == persona_id).all()
        return {row[0] for row in rows}
    except Exception:
        LOGGER.exception("[work_session] artifact snapshot query failed")
        return None
    finally:
        db.close()


def _collect_new_item_ids(
    manager: Any, persona_id: str, before: Optional[Set[str]]
) -> List[str]:
    """セッション開始時のスナップショットとの差分 (新規 Item ID) を作成順で返す。

    ``before`` が None (開始時スナップショット失敗) のときは、差分が定義
    できないため空リストを返す (誤って既存物を成果と主張しない側に倒す)。
    """
    if before is None:
        return []
    session_factory = getattr(manager, "SessionLocal", None)
    if session_factory is None:
        return []
    try:
        db = session_factory()
    except Exception:
        LOGGER.exception("[work_session] failed to open DB session for artifact diff")
        return []
    try:
        from database.models import Item
        rows = (
            db.query(Item.ITEM_ID)
            .filter(Item.CREATOR_ID == persona_id)
            .order_by(Item.CREATED_AT)
            .all()
        )
        return [row[0] for row in rows if row[0] not in before]
    except Exception:
        LOGGER.exception("[work_session] artifact diff query failed")
        return []
    finally:
        db.close()


def _run_coro_sync(coro: Any) -> Any:
    """async コルーチンを同期文脈から実行する (runtime_graph と同じパターン)。

    既にイベントループが走っているスレッドから呼ばれた場合は executor 上の
    別スレッドで ``asyncio.run`` する。
    """
    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None
    if running_loop and running_loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(asyncio.run, coro).result()
    return asyncio.run(coro)
