"""Pulse execution controller for priority-based playbook scheduling.

This module manages concurrent playbook executions per persona,
handling priority-based interruption and queueing.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import IntEnum
from queue import Queue
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Literal, Optional

from llm_clients.exceptions import LLMError
from sea.beat_gate import BeatGateClosedError
from sea.cancellation import CancellationToken, ExecutionCancelledException

if TYPE_CHECKING:
    from sea.runtime import SEARuntime

LOGGER = logging.getLogger(__name__)

# Queue limit - log error if exceeded
QUEUE_LIMIT = 10


class Priority(IntEnum):
    """Execution priority levels (lower number = higher priority)."""
    USER = 1
    SCHEDULE = 2
    AUTO = 3


@dataclass
class ExecutionType:
    """Configuration for each execution type."""
    name: str
    priority: Priority
    same_priority_policy: Literal["first", "last"]  # Which wins when same priority
    on_blocked: Literal["wait", "skip"]  # Behavior when blocked or interrupted


# Execution type configurations
EXECUTION_TYPES: Dict[str, ExecutionType] = {
    "user": ExecutionType(
        name="user",
        priority=Priority.USER,
        same_priority_policy="last",  # Later user message wins
        on_blocked="skip",  # Don't retry interrupted user messages
    ),
    "schedule": ExecutionType(
        name="schedule",
        priority=Priority.SCHEDULE,
        same_priority_policy="first",  # First schedule wins, others queue
        on_blocked="wait",  # Queue for retry after interruption
    ),
    "auto": ExecutionType(
        name="auto",
        priority=Priority.AUTO,
        same_priority_policy="first",  # First auto wins
        on_blocked="skip",  # Skip if busy (will retry in 10s anyway)
    ),
    "autonomy": ExecutionType(
        name="autonomy",
        priority=Priority.AUTO,  # Same priority as auto
        same_priority_policy="first",
        on_blocked="skip",
    ),
    # pulse_dispatch.md §4.3 / §6: メタ判断は priority 体系外のレーン。
    # 中断対象にならず、他 Pulse を中断もしない (この点は不変)。
    # ただし「並列レーン」の並列性の保証は解体済み (beat_execution_context.md
    # §2.2): 実際の直列化は run_meta_user 内の Beat ロック (persona 単位) が
    # 担い、メタ判断は main レーンの Beat 境界に挟まる直列 Beat になった。
    # 同一ペルソナ内のメタ判断同士の直列化は従来どおり MetaLayer の
    # per-persona Lock (ロック順序: MetaLayer Lock → Beat ロックの一方向)。
    # priority / same_priority_policy / on_blocked のフィールドはダミー値
    # (submit() が type=meta_judgment を別レーンで処理するため未使用)。
    "meta_judgment": ExecutionType(
        name="meta_judgment",
        priority=Priority.USER,  # ダミー (並列レーンで管理外)
        same_priority_policy="first",
        on_blocked="skip",
    ),
}


@dataclass
class ExecutionRequest:
    """Represents a pending or running playbook execution request."""
    type: str  # "user", "schedule", "auto"
    persona_id: str
    building_id: str
    user_input: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    meta_playbook: Optional[str] = None
    args: Optional[Dict[str, Any]] = None  # Arguments for meta playbook
    # UI-triggered pre-spells executed before the first LLM call. Each entry is
    # a Spell invocation string (e.g. '/run_playbook(name="generate_image_playbook")').
    # See docs/intent/persona_cognition/nested_subline_spell.md §13.
    pre_spells: Optional[List[str]] = None
    event_callback: Optional[Callable[[Dict[str, Any]], None]] = None
    pulse_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    cancellation_token: CancellationToken = field(default_factory=CancellationToken)
    
    # For schedule resumption
    is_resumption: bool = False
    original_prompt: Optional[str] = None

    # W3 Chunk A (schedule 台帳化 D4): 呼び出し側が submit の顛末を観測する
    # ためのフィールド。submit() の戻り値契約 (List[str] / None) は不変のまま、
    # request オブジェクト経由で「受付の裁定」と「実行の顛末」を運ぶ。
    # - dispatch_action: submit() が action 決定箇所で記入
    #   ("execute" / "queued" / "skipped")
    # - runtime_outcome: _execute_unlocked() が各経路で記入
    #   ("completed" / "gate_closed" / "cancelled" / "error")
    dispatch_action: Optional[str] = None
    runtime_outcome: Optional[str] = None


    @property
    def config(self) -> ExecutionType:
        """Get the execution type configuration."""
        return EXECUTION_TYPES.get(self.type, EXECUTION_TYPES["auto"])
    
    @property
    def priority(self) -> Priority:
        """Get the priority level."""
        return self.config.priority


class PulseController:
    """Controls concurrent playbook executions per persona.
    
    Implements priority-based scheduling with interruption support:
    - User requests have highest priority
    - Schedule requests have medium priority
    - Auto requests have lowest priority
    
    When a higher priority request arrives during execution:
    - Current execution is cancelled
    - Interruption message is recorded to memory
    - Higher priority request executes
    - If interrupted request has on_blocked="wait", it's re-queued
    """
    
    def __init__(self, sea_runtime: "SEARuntime"):
        self.sea_runtime = sea_runtime

        # Per-persona state
        # メインレーン (USER/SCHEDULE/AUTO/autonomy): priority 体系で管理、
        # 同一ペルソナで同時に 1 本のみ
        self._current: Dict[str, ExecutionRequest] = {}  # persona_id -> running request
        self._queues: Dict[str, List[ExecutionRequest]] = {}  # persona_id -> pending queue
        self._locks: Dict[str, threading.RLock] = {}  # persona_id -> lock
        # メタ判断レーン (META_JUDGMENT): priority 体系外のレーン (中断対象外 /
        # 他を中断しない)。旧「並列レーン」の並列性の保証は解体済み
        # (beat_execution_context.md §2.2): 直列化は run_meta_user 内の Beat
        # ロックが担い、メタ判断は main の Beat 境界に挟まる直列 Beat になった。
        # _current_meta は観測用の記帳として残す。
        self._current_meta: Dict[str, ExecutionRequest] = {}

        # 終了処理中は新規 Pulse を受け付けない (shutdown() が立てる)。
        # 立った後の submit は skipped、待機列の繰り上げも止まる。
        self._shutting_down = False

        # Interrupt callbacks: called when auto execution is interrupted by user
        # Signature: callback(persona_id: str, interrupted_by: str) -> None
        self._on_interrupt_callbacks: List[Callable] = []
        # Completion callbacks: called when user execution finishes
        # Signature: callback(persona_id: str) -> None
        self._on_user_complete_callbacks: List[Callable] = []

        LOGGER.info("[PulseController] Initialized")

    def register_on_interrupt(self, callback: Callable) -> None:
        """Register a callback for when auto execution is interrupted."""
        self._on_interrupt_callbacks.append(callback)

    def register_on_user_complete(self, callback: Callable) -> None:
        """Register a callback for when user execution completes."""
        self._on_user_complete_callbacks.append(callback)

    def shutdown(self, timeout: float = 8.0) -> bool:
        """サーバー終了時に、走行中の全 Pulse を締めてから返る。

        後始末の形は停止ボタンと同じ (取り消し → Beat の出口の後始末) だが、
        対象は停止ボタンが触らないメタ判断レーン (_current_meta) も含む —
        終了時はすべて締める。

        走行中の request の cancellation_token に "server_shutdown" を刻んで
        取り消し、Beat の出口の後始末 (途中本文の確定・中断の通告・記憶書き込み。
        ``sea/runtime_llm.py`` の ``_settle_placeholder_on_beat_death``) が
        走り終えるのを待つ。これを呼ばずにプロセスが死ぬと、daemon の生成
        スレッドが凍った瞬間に下書き行 (content="") が未確定のまま残り、
        発言が画面・記録・記憶から丸ごと消える。

        Returns:
            全 Pulse が締切内に締まったら True。締切超過・待機の中断は False
            (呼び出し側は残りの終了処理を続けてよい)。
        """
        # 最初に受付を閉じる — 取り消しで空いた席に新しい生成が座るのを防ぐ。
        self._shutting_down = True

        active = list(self._current.items()) + list(self._current_meta.items())
        for persona_id, request in active:
            request.cancellation_token.cancel(interrupted_by="server_shutdown")

        if not active:
            # 台帳が空なら即 True でよい: 全登録経路 (submit メインレーン /
            # メタレーン / _process_queue) が publish-then-validate — 台帳へ
            # 登録してから旗を再検査し、立っていたら自分で席を消す — を守る
            # ので、この一覧に無い登録は必ず自分で席を立つ。
            LOGGER.debug("[PulseController] Shutdown: no active executions")
            return True

        persona_ids = sorted({pid for pid, _ in active})
        LOGGER.info(
            "[PulseController] Shutdown: cancelling %d active execution(s) "
            "(personas: %s), waiting up to %.1fs for cleanup",
            len(active), ", ".join(persona_ids), timeout,
        )

        # 台帳 (_current / _current_meta) から消える = _execute_unlocked /
        # _submit_meta_lane の finally を通過した = Beat の後始末 (途中本文の
        # 確定・記憶書き込み) まで完了した、という関係に依存して待つ。
        # 後始末は Beat のスレッド内で例外経路として走るので、ここは観測だけ。
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                if not self._current and not self._current_meta:
                    LOGGER.info(
                        "[PulseController] Shutdown: all active executions settled"
                    )
                    return True
                # 打ち直しの回収網 (保険): 一覧取得の後に台帳へ載った登録は
                # publish-then-validate の登録後検査で自分で席を立つのが本線
                # だが、万一それを外した登録が残っても、各周回で未取り消しの
                # request に打ち直して最大 0.1 秒で回収する (cancel() は
                # Event の再 set なので二度呼んで安全)。
                late = list(self._current.values()) + list(self._current_meta.values())
                for request in late:
                    if not request.cancellation_token.is_cancelled():
                        LOGGER.info(
                            "[PulseController] Shutdown: cancelling late-registered "
                            "%s request for persona %s",
                            request.type, request.persona_id,
                        )
                        request.cancellation_token.cancel(
                            interrupted_by="server_shutdown"
                        )
                time.sleep(0.1)
        except KeyboardInterrupt:
            # Ctrl+C 連打。後始末の完了は待てなかったが、残りの終了処理
            # (llama-server 停止等) は止めない。
            LOGGER.warning(
                "[PulseController] Shutdown wait interrupted by user; "
                "proceeding without waiting for cleanup"
            )
            return False

        remaining = sorted(set(self._current) | set(self._current_meta))
        LOGGER.warning(
            "[PulseController] Shutdown: %d execution(s) did not settle within "
            "%.1fs (personas: %s); their draft rows may remain unconfirmed",
            len(remaining), timeout, ", ".join(remaining),
        )
        return False


    def _get_lock(self, persona_id: str) -> threading.RLock:
        """Get or create lock for persona."""
        if persona_id not in self._locks:
            self._locks[persona_id] = threading.RLock()
        return self._locks[persona_id]
    
    def _get_queue(self, persona_id: str) -> List[ExecutionRequest]:
        """Get or create queue for persona."""
        if persona_id not in self._queues:
            self._queues[persona_id] = []
        return self._queues[persona_id]
    
    def submit(self, request: ExecutionRequest) -> Optional[List[str]]:
        """Submit an execution request for processing.

        Returns:
            List of output strings if executed, None if skipped

        Note: Lock is held only during state checks and updates, NOT during
        actual LLM execution. This allows higher priority requests to send
        cancellation signals immediately.
        """
        # 終了処理中 (shutdown() 後) は新規 Pulse を受け付けない — 取り消しで
        # 空いた席に新しい生成が座ると、締めたそばから次の下書き行が生まれる。
        if self._shutting_down:
            request.dispatch_action = "skipped"
            LOGGER.info(
                "[PulseController] Rejecting %s request for persona %s "
                "(shutdown in progress)",
                request.type, request.persona_id,
            )
            return None

        # pulse_dispatch.md §4.3 / §6: メタ判断は priority 体系外のレーンで
        # 処理する (中断しない / されない)。実際の実行タイミングは
        # run_meta_user 内の Beat ロックが直列化する (main の Beat 境界に挟まる)。
        if request.type == "meta_judgment":
            return self._submit_meta_lane(request)

        persona_id = request.persona_id
        lock = self._get_lock(persona_id)
        
        # Phase 1: Check state and determine action (with lock)
        with lock:
            # 冒頭の速い経路のガードを通過した後、ここへ来るまでの間に
            # shutdown() が始まっていることがある (check-then-act の窓)。
            # 台帳へ載せる直前にロック内でもう一度検査して、shutdown() の
            # 一覧取得に漏れた登録が取り消されずに走るのを防ぐ。
            if self._shutting_down:
                request.dispatch_action = "skipped"
                LOGGER.info(
                    "[PulseController] Rejecting %s request for persona %s "
                    "(shutdown in progress)",
                    request.type, persona_id,
                )
                return None

            current = self._current.get(persona_id)
            
            if current is None:
                # No execution running, register and proceed
                self._current[persona_id] = request
                action = "execute"
            elif self._should_interrupt(current, request):
                # Cancel current execution
                LOGGER.info(
                    "[PulseController] Interrupting %s (priority=%d) for %s (priority=%d) on persona %s",
                    current.type, current.priority, request.type, request.priority, persona_id
                )
                current.cancellation_token.cancel(interrupted_by=request.type)

                # Notify interrupt callbacks (e.g., AutonomyManager pause)
                if current.type == "auto" and request.type == "user":
                    for cb in self._on_interrupt_callbacks:
                        try:
                            cb(persona_id, request.type)
                        except Exception:
                            LOGGER.debug("[PulseController] Interrupt callback error", exc_info=True)

                # Queue current for resumption if it has wait policy
                if current.config.on_blocked == "wait":
                    self._queue_for_resumption(current)

                # Register new request
                self._current[persona_id] = request
                action = "execute"
            else:
                # New request doesn't win - queue or skip based on policy
                if request.config.on_blocked == "wait":
                    self._add_to_queue(request)
                    LOGGER.info(
                        "[PulseController] Queued %s request for persona %s (queue size: %d)",
                        request.type, persona_id, len(self._get_queue(persona_id))
                    )
                    action = "queued"
                else:
                    LOGGER.debug(
                        "[PulseController] Skipping %s request for persona %s (busy with %s)",
                        request.type, persona_id, current.type
                    )
                    action = "skipped"

            # publish-then-validate: 台帳へ登録した後に旗を再検査する。
            # shutdown() は「旗を立てる → 台帳の一覧を取る」の順で動くので、
            # 一覧取得より後に載った登録は、この時点で必ず旗が見える —
            # 見えたら自分で席を消して skipped で返る。これで「取り消されずに
            # 走る登録」の経路が消える。queued / skipped (登録していない)
            # の場合は席が無いので触らない。
            if self._shutting_down and self._current.get(persona_id) is request:
                del self._current[persona_id]
                request.dispatch_action = "skipped"
                LOGGER.info(
                    "[PulseController] Rejecting %s request for persona %s "
                    "(shutdown in progress)",
                    request.type, persona_id,
                )
                return None

        # W3 Chunk A: 受付の裁定を request に記入 (呼び出し側の観測用)
        request.dispatch_action = action

        # Phase 2: Execute WITHOUT holding lock (allows interruption)
        if action == "execute":
            return self._execute_unlocked(request)
        else:
            return None
    
    def _should_interrupt(self, current: ExecutionRequest, new: ExecutionRequest) -> bool:
        """Determine if new request should interrupt current execution."""
        # Higher priority always wins
        if new.priority < current.priority:
            return True
        
        # Same priority - check policy
        if new.priority == current.priority:
            return new.config.same_priority_policy == "last"
        
        # Lower priority never interrupts
        return False
    
    def _add_to_queue(self, request: ExecutionRequest) -> None:
        """Add request to the pending queue."""
        queue = self._get_queue(request.persona_id)
        
        if len(queue) >= QUEUE_LIMIT:
            LOGGER.error(
                "[PulseController] Queue limit (%d) exceeded for persona %s! "
                "Dropping oldest request.",
                QUEUE_LIMIT, request.persona_id
            )
            queue.pop(0)  # Remove oldest
        
        queue.append(request)
    
    def _queue_for_resumption(self, request: ExecutionRequest) -> None:
        """Queue an interrupted request for resumption."""
        # Create a new request with resumption flag.
        # args は純粋な入力データなのでコピーする — 落とすと schedule / phenomenon
        # 発の Pulse (inject_persona_event の playbook_args 等) が中断復帰時に
        # 元と異なる入力で再開される (2026-07-31 Codex 六巡目)。
        # pre_spells は**コピーしない** — これは実行前アクション (任意の Spell =
        # メール送信・画像生成等の副作用) で、中断が起きる時点ではほぼ実行済み。
        # 復帰 request に載せると割り込みのたびに再実行される (同七巡目)。
        # 「未実行のまま中断された pre_spells が失われる」窓は残るが、副作用の
        # 二重実行より害が小さい。
        resumed = ExecutionRequest(
            type=request.type,
            persona_id=request.persona_id,
            building_id=request.building_id,
            user_input=request.user_input,
            metadata=request.metadata,
            meta_playbook=request.meta_playbook,
            args=request.args,
            event_callback=request.event_callback,
            is_resumption=True,
            original_prompt=request.user_input,
        )
        
        # Add to front of queue (high priority for resumption)
        queue = self._get_queue(request.persona_id)
        queue.insert(0, resumed)
        
        LOGGER.info(
            "[PulseController] Queued %s for resumption on persona %s",
            request.type, request.persona_id
        )
    
    def _execute_unlocked(self, request: ExecutionRequest) -> List[str]:
        """Execute a request WITHOUT holding the lock during LLM calls.
        
        Note: _current[persona_id] must already be set before calling this.
        This allows other threads to send cancellation signals during execution.
        """
        persona_id = request.persona_id
        lock = self._get_lock(persona_id)
        
        try:
            result = self._do_execute(request)
            # W3 Chunk A: 実行の顛末を request に記入 (呼び出し側の観測用)
            request.runtime_outcome = "completed"
            return result
        except BeatGateClosedError as e:
            # 関所 fail-closed (execution_ledger.md §2.2): この persona 宛の
            # pending が配送できず、実行は開始されていない (副作用ゼロ)。
            # user はエラーとして API まで伝播しユーザーに見せる。
            # auto / schedule 等は WARNING + 空で落とす — 台帳に prepared で
            # 残る実行は回復処理 (execution_ledger_wiring) が拾う。
            request.runtime_outcome = "gate_closed"
            if request.type == "user":
                raise
            LOGGER.warning(
                "[PulseController] Beat gate closed for persona %s (type=%s): %s",
                persona_id, request.type, e,
            )
            return []
        except ExecutionCancelledException as e:
            request.runtime_outcome = "cancelled"
            LOGGER.info(
                "[PulseController] Execution cancelled for persona %s, interrupted_by=%s",
                persona_id, e.interrupted_by
            )
            # ここで中断を記憶へ書く機構はもう無い (2026-08-26 撤去)。
            #
            # 届く条件が「生成が始まる前に止められた」= 一言も出ていない回だけで、
            # 707 セッションの記録を数えて実際に通ったのは 1 回だった。そして
            # 一言も出ていない回は、ペルソナから見れば「返事が生まれなかった」回と
            # 区別がつかない — そちらには昔から何も書いておらず、中断だけ機構の声を
            # 足すと、そこだけ不揃いになる。
            #
            # 途中まで喋ってから止められた回は、言いかけた本文と中断の通告を
            # ``sea/runtime_llm.py`` の停止の後片付けで書いている (そちらは例外に
            # 包まれる前を通るので確実に届く)。
            return []
        except LLMError:
            # Propagate LLM errors to the caller for frontend display
            request.runtime_outcome = "error"
            raise
        except Exception as e:
            request.runtime_outcome = "error"
            LOGGER.exception(
                "[PulseController] Error executing %s for persona %s: %s",
                request.type, persona_id, e
            )
            return []
        finally:
            with lock:
                if self._current.get(persona_id) is request:
                    del self._current[persona_id]

                # Notify user-complete callbacks (e.g., AutonomyManager resume)
                if request.type == "user":
                    for cb in self._on_user_complete_callbacks:
                        try:
                            cb(persona_id)
                        except Exception:
                            LOGGER.debug("[PulseController] User-complete callback error", exc_info=True)

                # Process next queued request
                self._process_queue(persona_id)
    
    def _do_execute(self, request: ExecutionRequest) -> List[str]:
        """Actually execute the request via SEARuntime."""
        persona = self._get_persona(request.persona_id)
        if persona is None:
            LOGGER.warning("[PulseController] Persona %s not found", request.persona_id)
            return []
        
        # Build user input with resumption prompt if needed
        user_input = request.user_input
        if request.is_resumption and request.original_prompt:
            user_input = self._build_resumption_prompt(request)
        
        # All requests (user / schedule / auto) go through run_meta_user.
        # auto pulse は必ず meta_playbook を指定して呼ばれる前提
        # (track_autonomous など)。旧 run_meta_auto / meta_auto Playbook 経路は
        # 2026-05-01 の認知モデル移行に伴い廃止済み。
        if request.type == "auto" and request.meta_playbook is None:
            LOGGER.error(
                "[PulseController] auto request requires meta_playbook (旧 run_meta_auto 経路は廃止)。persona=%s",
                request.persona_id,
            )
            return []

        return self.sea_runtime.run_meta_user(
            persona=persona,
            user_input=user_input,
            building_id=request.building_id,
            metadata=request.metadata,
            meta_playbook=request.meta_playbook,
            args=request.args,
            event_callback=request.event_callback,
            cancellation_token=request.cancellation_token,
            pulse_type=request.type,
            pre_spells=request.pre_spells,
        )
    
    def _build_resumption_prompt(self, request: ExecutionRequest) -> str:
        """Build prompt with resumption context."""
        original = request.original_prompt or ""
        return f"""<system>
[前回の処理が中断されました]
中断理由: 優先度の高いリクエストを処理しました
前回のプロンプト: {original}
</system>

{original}"""
    
    def _process_queue(self, persona_id: str) -> None:
        """Process the next item in the queue for a persona."""
        lock = self._get_lock(persona_id)
        queue = self._get_queue(persona_id)

        with lock:
            # 終了処理中は待機列を繰り上げない — 取り消しで空いた席に待機列の
            # 次が座って、終了処理中に新しい生成が始まるのを防ぐ。
            if self._shutting_down:
                return

            if not queue:
                return

            if persona_id in self._current:
                # Something else is already running
                return

            next_request = queue.pop(0)
            self._current[persona_id] = next_request

            # publish-then-validate: 台帳へ登録した後に旗を再検査する。
            # shutdown() の一覧取得より後に載った登録は、この時点で必ず旗が
            # 見える — 見えたら自分で席を消して繰り上げをやめる。
            if self._shutting_down:
                del self._current[persona_id]
                LOGGER.info(
                    "[PulseController] Discarding queued %s request for "
                    "persona %s (shutdown in progress)",
                    next_request.type, persona_id,
                )
                return

        LOGGER.info(
            "[PulseController] Processing queued %s request for persona %s",
            next_request.type, persona_id
        )
        
        # Execute in a new thread to avoid blocking
        def run():
            self._execute_unlocked(next_request)
        
        threading.Thread(target=run, daemon=True).start()
    
    def _get_persona(self, persona_id: str):
        """Get persona object from manager."""
        manager = getattr(self.sea_runtime, "manager", None)
        if manager is None:
            return None
        personas = getattr(manager, "all_personas", {})
        return personas.get(persona_id)
    
    def _get_occupants(self, building_id: str) -> List[str]:
        """Get occupants of a building."""
        manager = getattr(self.sea_runtime, "manager", None)
        if manager is None:
            return []
        occupants = getattr(manager, "occupants", {})
        return occupants.get(building_id, [])
    
    # Convenience methods for callers
    # ------------------------------------------------------------------
    # メタ判断並列レーン (pulse_dispatch.md §4.3 / §6)
    # ------------------------------------------------------------------

    def _submit_meta_lane(self, request: ExecutionRequest) -> Optional[List[str]]:
        """メタ判断レーン: priority 体系外の直列 Beat。

        中断対象外 / 他 Pulse を中断もしない (不変)。旧「並列レーン」の並列性の
        保証は解体済み (beat_execution_context.md §2.2): 直列化は run_meta_user
        内の Beat ロックが担い、メタ判断 Beat は main レーンの Beat 境界に
        挟まって走る。メタ判断同士の直列化は MetaLayer の per-persona Lock
        (ロック順序は MetaLayer Lock → Beat ロックの一方向)。_current_meta は
        観測用の記帳。
        """
        persona_id = request.persona_id
        # submit() 冒頭のガードとここの間に shutdown() が始まっていることが
        # ある (check-then-act の窓)。メインレーンと違いこちらはロック無しの
        # 台帳書き込みなので、書き込む直前に再検査する (兄弟の塞ぎ忘れ防止)。
        if self._shutting_down:
            request.dispatch_action = "skipped"
            LOGGER.info(
                "[PulseController] Rejecting %s request for persona %s "
                "(shutdown in progress)",
                request.type, persona_id,
            )
            return None
        # 念のため簡易な多重防御 (MetaLayer Lock とは独立)。先行メタ判断が
        # 動いているのに重ねて submit が来たら警告を出す。
        if self._current_meta.get(persona_id) is not None:
            LOGGER.warning(
                "[PulseController] Meta-judgment already running for persona %s; "
                "MetaLayer Lock should serialize but observed concurrent submit",
                persona_id,
            )
        self._current_meta[persona_id] = request

        # publish-then-validate: 台帳へ登録した後に旗を再検査する。shutdown()
        # の一覧取得より後に載った登録は、この時点で必ず旗が見える — 見えたら
        # 自分で席を消して skipped で返る (submit() メインレーンと同じ規律)。
        if self._shutting_down:
            if self._current_meta.get(persona_id) is request:
                del self._current_meta[persona_id]
            request.dispatch_action = "skipped"
            LOGGER.info(
                "[PulseController] Rejecting %s request for persona %s "
                "(shutdown in progress)",
                request.type, persona_id,
            )
            return None

        # A7 (W1 Chunk A): 例外は [] に変換せず再送出する — 呼び出し側
        # (judgment_points.run_judgment_point) が台帳の failed/unknown 分類に
        # 使う。「正常 return = 成功」の偽装をここで作らない。ログは従来どおり
        # 残し、event_callback には error event を通知する。
        try:
            return self._do_execute(request)
        except BeatGateClosedError as e:
            # 関所 fail-closed: 実行は始まっていない (副作用ゼロ)。メタ判断
            # レーンに user は来ないので常に WARNING — 台帳に prepared で
            # 残る実行は回復処理が拾う。
            LOGGER.warning(
                "[PulseController] Beat gate closed for persona %s (type=%s): %s",
                persona_id, request.type, e,
            )
            self._notify_meta_error(request, e)
            raise
        except ExecutionCancelledException as e:
            LOGGER.info(
                "[PulseController] Meta-judgment cancelled for persona %s, interrupted_by=%s",
                persona_id, e.interrupted_by,
            )
            self._notify_meta_error(request, e)
            raise
        except LLMError:
            raise
        except Exception as e:
            LOGGER.exception(
                "[PulseController] Meta-judgment error for persona %s",
                persona_id,
            )
            self._notify_meta_error(request, e)
            raise
        finally:
            if self._current_meta.get(persona_id) is request:
                del self._current_meta[persona_id]

    @staticmethod
    def _notify_meta_error(request: ExecutionRequest, exc: BaseException) -> None:
        """メタ判断レーンの失敗を event_callback に通知する (通知の失敗は握る)。"""
        callback = request.event_callback
        if callback is None:
            return
        try:
            callback({"type": "error", "message": str(exc)})
        except Exception:
            LOGGER.exception(
                "[PulseController] meta-judgment error callback failed for persona %s",
                request.persona_id,
            )

    def submit_meta_judgment(
        self,
        persona_id: str,
        building_id: str,
        meta_playbook: str,
        args: Optional[Dict[str, Any]] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Optional[List[str]]:
        """メタ判断 Pulse をメタ判断レーンで起動する。

        priority 体系の外側で動くので、メインレーンの Pulse を中断しない。
        実行は run_meta_user 内の Beat ロックで直列化され、main の Beat 境界に
        挟まる (beat_execution_context.md §2.2)。MetaLayer から呼ばれる前提
        (pulse_dispatch.md §6.3 案 A: PulseController 経由で統一)。
        """
        request = ExecutionRequest(
            type="meta_judgment",
            persona_id=persona_id,
            building_id=building_id,
            user_input=None,
            meta_playbook=meta_playbook,
            args=args,
            event_callback=event_callback,
        )
        return self.submit(request)

    # NOTE: 旧 ``on_track_status_change`` (Track 状態変化で進行中 Pulse を cancel
    # する observer。pulse_dispatch.md §6.2) は 2026-08-21 に撤去した。発火元だった
    # v1 メタ判断の Track 操作が退役し、判定に使っていた
    # ``ExecutionRequest.origin_track_id`` も Track 撤廃で書き手ごと消えた
    # (track_retirement.md §2 住人 3・6)。

    # ------------------------------------------------------------------
    # Convenience methods for callers
    # ------------------------------------------------------------------

    def submit_user(
        self,
        persona_id: str,
        building_id: str,
        user_input: str,
        metadata: Optional[Dict[str, Any]] = None,
        meta_playbook: Optional[str] = None,
        args: Optional[Dict[str, Any]] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        pre_spells: Optional[List[str]] = None,
    ) -> Optional[List[str]]:
        """Submit a user input request."""
        request = ExecutionRequest(
            type="user",
            persona_id=persona_id,
            building_id=building_id,
            user_input=user_input,
            metadata=metadata,
            meta_playbook=meta_playbook,
            args=args,
            pre_spells=pre_spells,
            event_callback=event_callback,
        )
        return self.submit(request)
    
    def submit_schedule(
        self,
        persona_id: str,
        building_id: str,
        user_input: str,
        metadata: Optional[Dict[str, Any]] = None,
        meta_playbook: Optional[str] = None,
        args: Optional[Dict[str, Any]] = None,
        pre_spells: Optional[List[str]] = None,
    ) -> Optional[List[str]]:
        """Submit a scheduled execution request.

        ``pre_spells`` is forwarded to the Pulse so the schedule can request
        Spells (with or without args) to execute before the first LLM call.
        Args-omitted form (``/spell name='X'``) routes through spell_args_decider
        for dynamic argument generation. Phase 3 B (handoff_2026-05-08).
        """
        request = ExecutionRequest(
            type="schedule",
            persona_id=persona_id,
            building_id=building_id,
            user_input=user_input,
            metadata=metadata,
            meta_playbook=meta_playbook,
            args=args,
            pre_spells=pre_spells,
        )
        return self.submit(request)
    
    def submit_auto(
        self,
        persona_id: str,
        building_id: str,
        meta_playbook: Optional[str] = None,
        args: Optional[Dict[str, Any]] = None,
    ) -> Optional[List[str]]:
        """Submit an autonomous pulse request.

        Args:
            persona_id, building_id: 対象。
            meta_playbook: auto pulse として起動する Playbook 名 (例:
                track_autonomous)。2026-05-01 の認知モデル移行以降は **必須**。
                None で呼ぶと _do_execute が ERROR ログを出して空配列を返す。
            args: meta_playbook に渡す引数。
        """
        request = ExecutionRequest(
            type="auto",
            persona_id=persona_id,
            building_id=building_id,
            meta_playbook=meta_playbook,
            args=args,
        )
        return self.submit(request)


__all__ = [
    "PulseController",
    "ExecutionRequest",
    "ExecutionType",
    "EXECUTION_TYPES",
    "Priority",
    "QUEUE_LIMIT",
]
