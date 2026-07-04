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
- **終了時の三点セットのうち 2 点** (アーティファクト収集 + ダイジェスト):
  - アーティファクト: セッション前後の Item テーブル差分
    (CREATOR_ID = ペルソナ) で「このセッションが実際に作ったもの」を収集。
  - ダイジェスト: 締めに軽量モデル 1 コールで「実際に起きたことだけ」の
    短い要約を生成し、これ 1 件のみを committed (main_line) で SAIMemory に
    書く。セッションの生ログ (各ラウンドのやり取り) は WORKER アスペクト
    由来の scope='volatile' で保存され、メインライン context には乗らない
    (sea/runtime_context.py: required_scopes=['committed'])。
  - 机メモ (Track への状態メモ) は呼び出し側 (セッション終了判断,
    judgment_points.md §6) の責務であり本モジュールでは扱わない。

記憶の接地原則 (autonomous_behavior_v2.md §3-1, §8-5):
committed されるのはダイジェスト 1 件のみ。ダイジェストのプロンプトは
「この会話の中で実際に起きたことだけを書く」ことを明示する。

時刻はすべて ``saiverse.clock.now()`` を読む (v2 §12 の不変条件)。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set

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

#: committed されるダイジェスト 1 件の意味分類タグ。
DIGEST_TAG = "session_digest"

ENDED_FINISHED = "finished"
ENDED_BUDGET_EXHAUSTED = "budget_exhausted"
ENDED_ERROR = "error"


@dataclass
class WorkSessionResult:
    """作業セッション 1 本の結果 (セッション終了判断への入力契約)。

    Attributes:
        digest: committed されたダイジェスト本文 (エラー時は空文字)。
        artifacts: このセッション中に作られた成果物の ref (Item ID) リスト。
        rounds_used: 実際に消費したラウンド数 (spell 実行→再呼び出しの往復)。
        ended_reason: ``finished`` (スペルなし応答で自然終了) /
            ``budget_exhausted`` (予算上限で打ち切り) / ``error``。
        usage: Pulse 単位の usage accumulator のコピー
            (total_input_tokens / total_output_tokens / total_cost_usd /
            call_count / models_used 等)。
        task_ref: 呼び出し時に渡された対象タスク参照 (透過)。
        error: ended_reason='error' のときの ``型名: メッセージ``。
        started_at / ended_at: ISO 形式時刻 (saiverse.clock.now() 由来)。
    """

    digest: str
    artifacts: List[str]
    rounds_used: int
    ended_reason: str
    usage: Dict[str, Any] = field(default_factory=dict)
    task_ref: Optional[str] = None
    error: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None


def run_work_session(
    persona_id: str,
    instruction: str,
    budget_rounds: int,
    task_ref: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    *,
    manager: Optional[Any] = None,
    track_id: Optional[str] = None,
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

        # ---- PulseContext + WORKER ライン ----
        pulse_id = str(uuid.uuid4())
        adapter = getattr(persona, "sai_memory", None)
        thread_id = adapter.get_current_thread() if adapter else None
        pulse_ctx = runtime._get_or_create_pulse_context(pulse_id, thread_id or "")
        from sea.pulse_context import Aspect, PulseLogEntry

        pulse_ctx.push_line(aspect=Aspect.WORKER, track_id=track_id)
        frame_pushed = True

        # ---- context: head (既存 sub_line と同じペルソナ head) + 指示書 ----
        # 履歴 / Memory Weave / visual は載せない: セッションは指示書と世界の
        # 返事 (spell 結果) だけを文脈に進む。必要な記憶は指示書が
        # memory_recall で想起させる (v2 §7.2 のシナリオ形)。
        from sea.playbook_models import ContextRequirements

        requirements = ContextRequirements(
            history_depth=0,
            system_prompt=True,
            inventory=True,
            building_items=True,
            available_playbooks=False,
            visual_context=False,
            memory_weave=False,
            working_memory=False,
            realtime_context=True,
        )
        base_messages = runtime._prepare_context(
            persona, building_id, None, requirements, pulse_id=pulse_id,
        )
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

        # WORKER フレームが active な状態で選ぶ → 軽量モデルが導出される
        llm_client = runtime._select_llm_client(node_def, persona, state=state)

        pulse_ctx.append(PulseLogEntry(
            role="user", content=instruction_content,
            node_id="work_session_instruction",
            playbook_name=WORK_SESSION_PLAYBOOK_NAME,
        ))

        # ---- 初回 LLM 呼び出し ----
        LOGGER.info(
            "[work_session] start: persona=%s budget=%d task_ref=%s track_id=%s pulse_id=%s",
            persona_id, budget_rounds, task_ref, track_id, pulse_id,
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
        from sea.runtime_llm import _parse_spell_lines, _run_spell_loop

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

        # ---- digest: 軽量 1 コールで「実際に起きたことだけ」を要約 ----
        digest = ""
        digest_error: Optional[str] = None
        try:
            digest = _generate_digest(
                runtime, state, llm_client, persona, building_id,
                messages, rounds_used, budget_rounds, artifacts,
            )
        except Exception as exc:
            LOGGER.exception(
                "[work_session] digest generation failed (persona=%s pulse_id=%s)",
                persona_id, pulse_id,
            )
            digest_error = f"digest failed: {type(exc).__name__}: {exc}"

        ended_at = clock.now()
        if digest:
            ws_meta: Dict[str, Any] = {
                "work_session": {
                    "task_ref": task_ref,
                    "artifacts": list(artifacts),
                    "rounds_used": rounds_used,
                    "budget_rounds": budget_rounds,
                    "ended_reason": ended_reason,
                    "started_at": started_at.isoformat(timespec="seconds"),
                    "ended_at": ended_at.isoformat(timespec="seconds"),
                },
            }
            if metadata:
                ws_meta["work_session"]["extra"] = dict(metadata)
            # committed はこの 1 件のみ。WORKER フレームの volatile 解決を
            # 明示引数で上書きし、メインライン context に乗る形で保存する。
            runtime._store_memory(
                persona, digest, role="assistant",
                tags=[DIGEST_TAG], pulse_id=pulse_id,
                metadata=ws_meta,
                playbook_name=WORK_SESSION_PLAYBOOK_NAME,
                line_role="main_line",
                scope="committed",
                origin_track_id=track_id,
            )
            pulse_ctx.append(PulseLogEntry(
                role="assistant", content=digest,
                node_id="work_session_digest",
                playbook_name=WORK_SESSION_PLAYBOOK_NAME,
            ))

        LOGGER.info(
            "[work_session] end: persona=%s ended_reason=%s rounds=%d/%d "
            "artifacts=%d digest_len=%d",
            persona_id, ended_reason, rounds_used, budget_rounds,
            len(artifacts), len(digest),
        )
        return WorkSessionResult(
            digest=digest,
            artifacts=artifacts,
            rounds_used=rounds_used,
            ended_reason=ended_reason,
            usage=dict(usage_accumulator),
            task_ref=task_ref,
            error=digest_error,
            started_at=started_at.isoformat(timespec="seconds"),
            ended_at=ended_at.isoformat(timespec="seconds"),
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
        return WorkSessionResult(
            digest="",
            artifacts=artifacts,
            rounds_used=rounds_used,
            ended_reason=ENDED_ERROR,
            usage=dict(usage_accumulator),
            task_ref=task_ref,
            error=f"{type(exc).__name__}: {exc}",
            started_at=started_at.isoformat(timespec="seconds"),
            ended_at=clock.now().isoformat(timespec="seconds"),
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


def _build_digest_prompt(rounds_used: int, budget_rounds: int) -> str:
    """締めのダイジェスト生成プロンプト。実際に起きたこと以外を書かせない。"""
    return (
        "<system>"
        f"作業セッションはここで終了です（使用ラウンド: {rounds_used}/{budget_rounds}）。\n"
        "このセッションであなたが実際に行ったこと・作ったもの・途中のままのことを、"
        "あなた自身の内なる声で短く要約してください。\n"
        "- この会話の中で実際に起きたこと（スペルの実行結果で確認できたこと）だけを書いてください。\n"
        "- 実行していない作業や、確認できていない成果を書いてはいけません。\n"
        "- 成果物を作った場合は、その名前に触れてください。\n"
        "- 要約の本文だけを書いてください。"
        "</system>"
    )


def _generate_digest(
    runtime: Any,
    state: Dict[str, Any],
    llm_client: Any,
    persona: Any,
    building_id: str,
    messages: List[Dict[str, Any]],
    rounds_used: int,
    budget_rounds: int,
    artifacts: List[str],
) -> str:
    """セッションの会話にダイジェスト指示を足して軽量モデルで 1 コール。"""
    digest_messages = list(messages)
    digest_messages.append({
        "role": "user",
        "content": _build_digest_prompt(rounds_used, budget_rounds),
    })
    result = llm_client.generate(
        digest_messages,
        tools=[],
        temperature=runtime._default_temperature(persona),
        **runtime._get_cache_kwargs(getattr(persona, "persona_id", None)),
    )
    _record_llm_usage(
        runtime, state, llm_client, persona, building_id, "llm_work_session_digest"
    )
    digest = _extract_text(result).strip()
    try:
        runtime._dump_llm_io(
            WORK_SESSION_PLAYBOOK_NAME, "work_session_digest", persona,
            digest_messages, digest,
        )
    except Exception:
        LOGGER.warning("[work_session] failed to dump digest LLM I/O", exc_info=True)
    if not digest:
        LOGGER.warning(
            "[work_session] digest LLM call returned empty text (persona=%s)",
            getattr(persona, "persona_id", None),
        )
    return digest


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
) -> None:
    """LLM 1 コール分の usage を計上する (usage_tracker + Pulse accumulator)。

    sea/runtime_llm.py の spell-retry usage 計上と同じ形。usage が無い
    (mock クライアント等) 場合は no-op。
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
        playbook_name=WORK_SESSION_PLAYBOOK_NAME,
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
        runtime._touch_anchor_after_llm_call(persona, usage)
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
