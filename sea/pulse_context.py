"""PulseContext — Pulse-level log sharing for unified memory architecture.

Each Pulse (a single unit of AI cognition, triggered by user input or
autonomous schedule) generates a PulseContext that tracks all node outputs
across all Playbooks executed within that Pulse.  Sub-playbooks share the
same PulseContext via parent_state reference inheritance (same pattern as
``_pulse_usage_accumulator`` and ``_activity_trace``).

PulseContext replaces ``_intermediate_msgs`` and additionally serves as the
source for ``pulse_logs`` DB persistence.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger("saiverse.pulse_context")


def default_lightweight_model() -> str:
    """軽量モデル未設定時のフォールバックチェーン (env → builtin 既定)。

    ``sea/runtime.py`` の LLM 選択と ``resolve_execution_context`` の双方が
    同じ解決結果になるよう、供給源をここに一本化している。
    """
    from saiverse.model_defaults import BUILTIN_DEFAULT_LITE_MODEL
    return os.getenv("SAIVERSE_DEFAULT_LIGHTWEIGHT_MODEL", BUILTIN_DEFAULT_LITE_MODEL)


class Aspect(str, Enum):
    """ペルソナの言動の「側面」 (認知モデル v0.2, line_tag_responsibility.md §10)。

    全て同じペルソナの言動だが、状態の異なる一側面を表す。ライン起動時に1つ
    指定され、``line_role`` / ``scope`` / モデル tier を**導出**する。Playbook
    ノードはこれらを個別指定せず、起動元 (track handler / run_playbook /
    meta_layer) が指定したアスペクトから構造的に決まる。

    - ``CONVERSATION``: 通常会話 (対ユーザー / social / external)。main_line / committed / 標準
    - ``WORKER``: run_playbook スペルのサブライン処理。sub_line / volatile / 軽量
    - ``AUTONOMOUS``: 自律行動。main_line / committed / 軽量
    - ``META``: メタ判断。meta_judgment / discardable (確定分は committed に昇格) / 標準
    """

    CONVERSATION = "conversation"
    WORKER = "worker"
    AUTONOMOUS = "autonomous"
    META = "meta"

    @property
    def line_role(self) -> str:
        """このアスペクトが記録される ``messages.line_role``。"""
        return _ASPECT_DERIVATION[self][0]

    @property
    def scope(self) -> str:
        """このアスペクトの ``messages.scope`` (永続性)。"""
        return _ASPECT_DERIVATION[self][1]

    @property
    def model_tier(self) -> str:
        """このアスペクトで使うモデル tier (``"standard"`` / ``"lightweight"``)。"""
        return _ASPECT_DERIVATION[self][2]

    @property
    def mode_display_name(self) -> str:
        """ペルソナに見せる「モード」名 (mode_spell_permissions.md §3)。

        コード上は ``aspect`` のまま扱い、ペルソナ向けの出力 (スペル権限のゲット文、
        システムプロンプト解説、「今どのモードか」通知) でのみこの表記を使う。
        """
        return _ASPECT_MODE_DISPLAY_NAME[self]


# Aspect → (line_role, scope, model_tier)。アスペクトは「呼び出し時に1つ指定」
# される唯一の供給源で、ここから3軸を導出する (line_tag_responsibility.md §10.2)。
_ASPECT_DERIVATION: Dict["Aspect", tuple] = {
    Aspect.CONVERSATION: ("main_line", "committed", "standard"),
    Aspect.WORKER: ("sub_line", "volatile", "lightweight"),
    Aspect.AUTONOMOUS: ("main_line", "committed", "lightweight"),
    Aspect.META: ("meta_judgment", "discardable", "standard"),
}

# Aspect → ペルソナ向け「モード」名 (mode_spell_permissions.md §3)。コード上は
# aspect で扱い、ペルソナに見せる文字列でのみモード表記を使う。
_ASPECT_MODE_DISPLAY_NAME: Dict["Aspect", str] = {
    Aspect.CONVERSATION: "メインモード",
    Aspect.META: "自律制御モード",
    Aspect.AUTONOMOUS: "自律作業モード",
    Aspect.WORKER: "分身モード",
}


def aspect_from_pulse_type(pulse_type: Optional[str]) -> "Aspect":
    """Pulse-root のアスペクトを ``pulse_type`` から導出する (§10.2)。

    - ``"auto"`` → ``AUTONOMOUS`` (自律 Track の Pulse)
    - ``"meta_judgment"`` → ``META`` (メタ判断ディスパッチ)
    - それ以外 (``"user"`` / ``"schedule"`` / None) → ``CONVERSATION``

    サブライン (run_playbook) は ``WORKER`` を別途 push するためここには含めない。
    """
    if pulse_type == "auto":
        return Aspect.AUTONOMOUS
    if pulse_type == "meta_judgment":
        return Aspect.META
    return Aspect.CONVERSATION


@dataclass
class PulseLogEntry:
    """A single log entry within a Pulse.

    Attributes:
        id: Unique identifier (UUID).
        role: Message role — "user", "assistant", "tool", "system".
        content: Textual content of the entry.
        node_id: Playbook node that produced this entry.
        playbook_name: Playbook that was executing.
        important: If True, this entry is also written to SAIMemory messages.
        created_at: Unix epoch timestamp.
        tool_calls: For assistant messages that invoke tools — list of tool call
                    dicts following the function calling protocol.
        tool_call_id: For tool result messages — the corresponding call ID.
        tool_name: For tool result messages — the tool function name.
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    role: str = ""
    content: str = ""
    node_id: Optional[str] = None
    playbook_name: Optional[str] = None
    important: bool = False
    created_at: int = field(default_factory=lambda: int(time.time()))
    # Function calling protocol fields
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    tool_name: Optional[str] = None


@dataclass
class LineFrame:
    """Identifies one running line within a Pulse.

    A "line" is the execution flow defined by Intent A v0.14: model/cache type
    (main/sub) × call relation (parent/child) × Pulse-stack position
    (root/nested). One Pulse holds a stack of LineFrames as ranges open and
    close; the topmost frame is the currently executing line.

    Attributes:
        line_id: UUID identifying this line. Persisted as ``messages.line_id``
            so layer [3] (Track-internal sub-cache) can distinguish parallel
            root sub-lines within the same Track.
        role: ``'main_line'`` / ``'sub_line'`` / ``'meta_judgment'`` / ``'nested'``.
            Same vocabulary as ``messages.line_role``. The mapping to 7-layer
            storage:
              * main_line + root  → [2] main cache
              * sub_line + root   → [3] Track-internal sub-cache
              * sub_line + nested → [4] nested temp context
              * meta_judgment     → [1] meta-judgment log (with discardable scope)
        parent_id: Parent LineFrame's ``line_id``, or ``None`` when this is the
            root line for the current Pulse.
        created_at: Unix epoch timestamp for diagnostics.

    NOTE (2026-08-21): ``track_id`` (``messages.origin_track_id`` へ写す欄) は
    Track 撤廃で書き手ごと退役した (track_retirement.md §2 住人 3)。
    """

    line_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    role: str = "main_line"
    parent_id: Optional[str] = None
    created_at: int = field(default_factory=lambda: int(time.time()))
    # 認知モデル v0.2 (§10): このラインのアスペクト。設定されていれば line_role /
    # scope / model tier の唯一の供給源となり、role はアスペクトから導出される。
    # None の場合は legacy 動作 (role を直接指定、scope は未導出で committed 既定)。
    aspect: Optional[Aspect] = None

    def __post_init__(self) -> None:
        # アスペクトが設定されていれば line_role はそこから導出する (アスペクト優先)。
        if self.aspect is not None:
            self.role = self.aspect.line_role

    @property
    def scope(self) -> Optional[str]:
        """アスペクト由来の scope。legacy frame (aspect=None) では None。"""
        return self.aspect.scope if self.aspect is not None else None

    @property
    def model_tier(self) -> Optional[str]:
        """アスペクト由来のモデル tier。legacy frame では None。"""
        return self.aspect.model_tier if self.aspect is not None else None

    @property
    def is_root(self) -> bool:
        """True when this line was started by a Pulse scheduler (no parent)."""
        return self.parent_id is None

    @property
    def is_nested(self) -> bool:
        """True when this line was spawned by another line within the Pulse."""
        return self.parent_id is not None


@dataclass(frozen=True)
class ExecutionContext:
    """実行の身分証 — Beat 開始時に一度だけ解決する不変の器。

    intent: docs/intent/beat_execution_context.md §2.1。「今この実行が何者か」
    (誰の・どの thread の・どの line の・どの model の実行か) を一箇所に集め、
    各層が persona の可変属性から都度推測するのをやめるための器。

    本段階 (移行手順 1「挙動不変の置換」) では LLM client 選択まわりの結線のみで、
    解決される値は従来の暗黙推測と同一。execution_id (実行台帳, 柱1) は常に None。

    Attributes:
        persona_id: 誰の実行か。
        thread_id: どの thread に記録するか (Beat 時点の adapter
            ``get_current_thread()`` の解決値。Stelis / subagent の切替は
            ``PulseContext.push_thread`` / ``pop_thread`` が adapter と同期する)。
        line_id: 実行中ラインの ``LineFrame.line_id``。frame の無い実行
            (keepalive / sluice 等の Pulse 外呼び出し) では空文字。
        aspect: このラインのアスペクト (§10)。legacy frame / frame 無しでは None。
        model_key: この Beat で実行する model の解決値 (aspect の tier から導出)。
        pulse_id: 所属 Pulse。Pulse 外の実行では空文字。
        execution_id: 実行台帳に載る実行のみ (柱1)。本段階では常に None。
    """

    persona_id: str
    thread_id: str
    line_id: str
    aspect: Optional[Aspect]
    model_key: str
    pulse_id: str
    execution_id: Optional[str] = None

    def with_model(self, actual_model: str) -> "ExecutionContext":
        """実行 model が選択後に変わった場合の差し替え (frozen なので新インスタンス)。

        structured-output fallback (sea/runtime.py の select_llm_client) 等で
        実際に呼ぶ model が解決値と異なった時、呼び出し側がこれで差し替える。
        """
        return replace(self, model_key=actual_model)


def resolve_execution_context(
    persona: Any,
    pulse_context: Optional["PulseContext"],
    state: Optional[Dict[str, Any]] = None,
    execution_id: Optional[str] = None,
) -> ExecutionContext:
    """Beat 開始点で ExecutionContext を解決する (beat_execution_context §2.1)。

    挙動不変の原則: model_key の導出は ``sea/runtime.py`` の従来の LLM 選択
    (aspect.model_tier → lightweight/standard、legacy frame では
    ``_force_lightweight_model`` / ``_pulse_type=='auto'`` フォールバック) と
    同じ結果になるように写している。値を変える変更は後続の段で行う。

    Args:
        persona: PersonaCore (またはテスト用の互換オブジェクト)。
        pulse_context: 現在の PulseContext。Pulse 外の実行 (keepalive /
            sluice) では None。
        state: 実行 state。legacy frame (aspect 無し) の tier フォールバック
            フラグと ``_pulse_id`` の供給源。無ければ standard 扱い。
        execution_id: 実行台帳の ID (柱1)。本段階では常に None。
    """
    frame: Optional[LineFrame] = None
    if pulse_context is not None:
        try:
            frame = pulse_context.current_line()
        except Exception:
            frame = None
    aspect = frame.aspect if frame is not None else None

    # ── model tier: aspect が唯一の供給源。無ければ legacy フラグ (従来どおり) ──
    tier = aspect.model_tier if aspect is not None else None
    if tier is None:
        force_lightweight = bool(state and (
            state.get("_force_lightweight_model")
            or state.get("_pulse_type") == "auto"
        ))
        tier = "lightweight" if force_lightweight else "standard"

    if tier == "lightweight":
        model_key = getattr(persona, "lightweight_model", None) or default_lightweight_model()
    else:
        model_key = getattr(persona, "model", "unknown")

    # ── thread_id: adapter の現在値が正 (Stelis/subagent の push/pop は
    #    PulseContext.push_thread / pop_thread が adapter と同期する) ──
    thread_id = ""
    adapter = getattr(persona, "sai_memory", None)
    if adapter is not None:
        try:
            thread_id = adapter.get_current_thread() or ""
        except Exception:
            LOGGER.debug("[execution_context] get_current_thread failed", exc_info=True)
            thread_id = ""

    if pulse_context is not None:
        pulse_id = pulse_context.pulse_id or ""
    else:
        pulse_id = str(state.get("_pulse_id") or "") if state else ""

    return ExecutionContext(
        persona_id=str(getattr(persona, "persona_id", "") or ""),
        thread_id=thread_id,
        line_id=frame.line_id if frame is not None else "",
        aspect=aspect,
        model_key=str(model_key),
        pulse_id=pulse_id,
        execution_id=execution_id,
    )


@dataclass
class PulseContext:
    """Shared mutable log list for a single Pulse execution.

    Passed through ``state["_pulse_context"]`` and inherited by sub-playbooks
    via shared reference.  At Pulse completion the logs are flushed to the
    ``pulse_logs`` DB table.

    The ``_line_stack`` field tracks the currently-running line hierarchy
    (Intent A v0.14, Intent B v0.11). Push when entering a new line, pop when
    it completes. The topmost frame is the active line at any point.

    The ``_thread_stack`` field (S4, beat_execution_context.md 不変条件6) records
    parent thread ids for Stelis / subagent thread switches performed during
    this Pulse. ``push_thread`` / ``pop_thread`` keep the persona's
    ``active_state.json`` in sync, and the graph runner's ``finally``
    (sea/runtime_graph.py) calls ``unwind_threads`` back to the entry depth so
    the parent thread is restored even when an end node is never reached
    (exception / cancel).
    """

    pulse_id: str
    logs: List[PulseLogEntry] = field(default_factory=list)
    _line_stack: List[LineFrame] = field(default_factory=list)
    _thread_stack: List[str] = field(default_factory=list)
    # メタ判断 Pulse 用バッファ (Intent A v0.15 + Phase 2)。
    # pulse_type == 'meta_judgment' の Pulse でのみ populate される。
    # spell loop が判断 LLM 応答 + 発動 spell + 各 spell の結果を蓄積し、
    # Pulse 完了時に MetaLayer が meta_judgment_log テーブルへ flush する。
    # 形式: {"thought_parts": List[str], "spells": List[{name, args, result}]}
    meta_judgment_buffer: Optional[Dict[str, Any]] = None

    def append(self, entry: PulseLogEntry) -> None:
        """Append a log entry to this Pulse's log list."""
        self.logs.append(entry)

    def get_important_logs(self) -> List[PulseLogEntry]:
        """Return only the entries flagged as important."""
        return [e for e in self.logs if e.important]

    # ------------------------------------------------------------------
    # Line hierarchy management (Intent B v0.11, P0-1)
    # ------------------------------------------------------------------

    def push_line(
        self,
        role: str = "main_line",
        parent_id: Optional[str] = None,
        aspect: Optional[Aspect] = None,
    ) -> LineFrame:
        """Open a new line frame and push it onto the stack.

        Args:
            role: Line role (``'main_line'`` / ``'sub_line'`` / ``'meta_judgment'``
                / ``'nested'``). 認知モデル v0.2 以降は ``aspect`` を渡すのが既定で、
                その場合 role はアスペクトから導出され本引数は無視される。``aspect``
                を渡さない legacy 経路でのみ role が直接使われる。
            parent_id: Parent line ID. If ``None``, inferred from the topmost
                frame on the stack (an empty stack yields a root line).
            aspect: このラインのアスペクト (§10)。設定すると line_role / scope /
                model tier がここから導出される。

        Returns:
            The newly pushed ``LineFrame``. Callers should record its ``line_id``
            and pass to ``messages.line_id`` when persisting messages produced
            by this line.
        """
        current = self.current_line()
        if parent_id is None and current is not None:
            parent_id = current.line_id
        frame = LineFrame(role=role, parent_id=parent_id, aspect=aspect)
        self._line_stack.append(frame)
        return frame

    def pop_line(self) -> Optional[LineFrame]:
        """Pop and return the topmost line frame.

        Returns ``None`` when the stack is empty (defensive — should not happen
        in well-formed playbook execution; logged but not raised so cleanup
        paths stay safe).
        """
        if not self._line_stack:
            return None
        return self._line_stack.pop()

    def current_line(self) -> Optional[LineFrame]:
        """Return the topmost line frame, or ``None`` when no line is active."""
        if not self._line_stack:
            return None
        return self._line_stack[-1]

    def current_line_metadata(self) -> Dict[str, Optional[str]]:
        """Return the active line's storage metadata as a flat dict.

        Convenience for ``_store_memory`` calls that need to forward
        ``line_role`` / ``line_id``. Returns empty values when no line is active
        (caller falls back to legacy behavior).
        """
        current = self.current_line()
        if current is None:
            return {"line_role": None, "line_id": None, "scope": None, "aspect": None}
        return {
            "line_role": current.role,
            "line_id": current.line_id,
            # アスペクト由来の scope (§10.3)。legacy frame では None で、
            # _store_memory 側で SQL 既定 'committed' に落ちる。
            "scope": current.scope,
            "aspect": current.aspect.value if current.aspect is not None else None,
        }

    # ------------------------------------------------------------------
    # Thread push/pop (S4 — beat_execution_context.md §2.1 / 不変条件6)
    # ------------------------------------------------------------------

    def push_thread(
        self,
        adapter: Any,
        child_thread_id: str,
        parent_thread_id: Optional[str] = None,
    ) -> Optional[str]:
        """Switch the persona's active thread to ``child_thread_id``, recording
        the parent on this Pulse's thread stack.

        「thread は実行の属性」(不変条件6) の実装点。Stelis start / subagent
        start はこれを経由することで、end ノード不達 (例外 / cancel) でも
        graph 実行の ``finally`` (``unwind_threads``) が親 thread を復元できる。
        push 時には ``active_state.json`` へ ``pulse_scoped_parent`` (最外周の
        親) も書き込み、プロセス死で孤児化した場合の起動時復旧
        (adapter の ``recover_orphaned_thread``) の手がかりにする。

        Args:
            adapter: SAIMemoryAdapter (またはテスト用の互換オブジェクト)。
            child_thread_id: 切替先の thread id。
            parent_thread_id: 呼び出し側が既に解決した親 thread id。``None``
                なら ``adapter.get_current_thread()`` (無ければ既定 thread) を使う。

        Returns:
            記録した親 thread id。親が解決できなかった場合は空文字を記録した
            うえで空文字を返す (pop 時は active thread を動かさない)。adapter
            が無い場合は何もせず ``None``。
        """
        if adapter is None:
            LOGGER.warning(
                "[pulse_context] push_thread called without adapter — thread not switched "
                "(pulse_id=%s child=%s)", self.pulse_id, child_thread_id,
            )
            return None
        parent = parent_thread_id
        if not parent:
            try:
                parent = adapter.get_current_thread()
            except Exception:
                LOGGER.debug("[pulse_context] get_current_thread failed during push_thread", exc_info=True)
                parent = None
        if not parent:
            fallback = getattr(adapter, "_thread_id", None)
            if callable(fallback):
                try:
                    parent = fallback(None)
                except Exception:
                    parent = None
        parent = parent or ""
        if not parent:
            LOGGER.warning(
                "[pulse_context] push_thread: no parent thread resolvable for child=%s — "
                "pop will leave the active thread unchanged (pulse_id=%s)",
                child_thread_id, self.pulse_id,
            )
        self._thread_stack.append(parent)
        adapter.set_active_thread(child_thread_id)
        self._write_orphan_marker(adapter)
        LOGGER.debug(
            "[pulse_context] Pushed thread: %s -> %s (depth=%d, pulse_id=%s)",
            parent, child_thread_id, len(self._thread_stack), self.pulse_id,
        )
        return parent

    def pop_thread(self, adapter: Any) -> Optional[str]:
        """Pop the most recent thread push and restore the recorded parent.

        Returns the restored parent thread id. スタックが空なら WARN + no-op
        (``None``)。adapter が無い場合はスタックだけ縮めて記録値を返す
        (active thread は復元できない)。
        """
        if not self._thread_stack:
            LOGGER.warning(
                "[pulse_context] pop_thread on empty thread stack — no-op (pulse_id=%s)",
                self.pulse_id,
            )
            return None
        parent = self._thread_stack.pop()
        if adapter is None:
            LOGGER.warning(
                "[pulse_context] pop_thread without adapter — active thread not restored "
                "(parent=%s, pulse_id=%s)", parent, self.pulse_id,
            )
            return parent
        if parent:
            try:
                adapter.set_active_thread(parent)
            except Exception:
                LOGGER.warning(
                    "[pulse_context] Failed to restore parent thread %s (pulse_id=%s)",
                    parent, self.pulse_id, exc_info=True,
                )
        else:
            LOGGER.warning(
                "[pulse_context] pop_thread: recorded parent was empty — active thread "
                "left unchanged (pulse_id=%s)", self.pulse_id,
            )
        self._write_orphan_marker(adapter)
        LOGGER.debug(
            "[pulse_context] Popped thread back to %s (depth=%d, pulse_id=%s)",
            parent, len(self._thread_stack), self.pulse_id,
        )
        return parent

    def thread_stack_depth(self) -> int:
        """Current depth of the thread push stack."""
        return len(self._thread_stack)

    def unwind_threads(self, adapter: Any, depth: int) -> int:
        """Pop thread pushes until the stack is back at ``depth``.

        graph 実行の ``finally`` から呼ばれる非常口。正常系 (Stelis end /
        subagent end がすべて pop 済み) では no-op。巻き戻しが起きた場合は
        「end ノード不達で親 thread へ戻れていなかった」ことを意味するので
        WARN を出す。Returns the number of pops performed.
        """
        target = max(0, int(depth))
        popped = 0
        while len(self._thread_stack) > target:
            self.pop_thread(adapter)
            popped += 1
        if popped:
            LOGGER.warning(
                "[pulse_context] Unwound %d dangling thread push(es) to depth=%d "
                "(pulse_id=%s) — a Stelis/subagent section exited without reaching "
                "its end node; parent thread restored by the graph finally",
                popped, target, self.pulse_id,
            )
        return popped

    def _write_orphan_marker(self, adapter: Any) -> None:
        """Sync ``pulse_scoped_parent`` in ``active_state.json`` with the stack.

        スタックが空でなければ最外周の親 (``stack[0]``) を書き、空になったら
        消す。adapter 側のメソッドが無い (テストスタブ等) 場合は黙って skip —
        マーカーはクラッシュ復旧専用で、プロセス内の復元はスタックが正。
        ``set_active_thread`` はファイルを丸ごと書き直す (未知キーを保持しない)
        ため、push/pop で thread を切り替えた直後に毎回ここで書き直す。
        """
        if self._thread_stack:
            outermost = self._thread_stack[0]
            if not outermost:
                return
            setter = getattr(adapter, "set_pulse_scoped_parent", None)
            if callable(setter):
                try:
                    setter(outermost)
                except Exception:
                    LOGGER.debug("[pulse_context] set_pulse_scoped_parent failed", exc_info=True)
        else:
            clearer = getattr(adapter, "clear_pulse_scoped_parent", None)
            if callable(clearer):
                try:
                    clearer()
                except Exception:
                    LOGGER.debug("[pulse_context] clear_pulse_scoped_parent failed", exc_info=True)

    # ------------------------------------------------------------------
    # Meta judgment buffer (Phase 2 — handoff_2026-04-30 Part 2)
    # ------------------------------------------------------------------

    def init_meta_judgment_buffer(self) -> Dict[str, Any]:
        """メタ判断バッファを初期化し、参照を返す。

        既に初期化済みなら既存バッファをそのまま返す。
        ``pulse_type == 'meta_judgment'`` の Pulse の最初の spell loop 入口で
        呼び出され、判断 LLM の独白テキストと発動 spell をここに蓄積する。
        """
        if self.meta_judgment_buffer is None:
            self.meta_judgment_buffer = {
                "thought_parts": [],
                "spells": [],
            }
        return self.meta_judgment_buffer

    def append_meta_judgment_thought(self, text: str) -> None:
        """メタ判断バッファに独白テキストを追記する (バッファ未初期化時は no-op)。"""
        if self.meta_judgment_buffer is None:
            return
        if text:
            self.meta_judgment_buffer["thought_parts"].append(text)

    def append_meta_judgment_spell(
        self, name: str, args: Dict[str, Any], result: str
    ) -> None:
        """メタ判断バッファに発動 spell + 結果を追記する (バッファ未初期化時は no-op)。"""
        if self.meta_judgment_buffer is None:
            return
        self.meta_judgment_buffer["spells"].append({
            "name": name,
            "args": dict(args),
            "result": result,
        })

    def get_protocol_messages(self) -> List[Dict[str, Any]]:
        """Reconstruct LLM API-compatible message list from logged entries.

        This replaces ``_intermediate_msgs`` — context_profile-based LLM nodes
        call this method to obtain the conversation history accumulated during
        the current Pulse (including function calling protocol pairs).

        All protocol-compatible roles (user, assistant, tool, system) are
        included.  Mid-conversation system messages are handled by the LLM
        client layer's ``convert_system_to_user`` mechanism for backends that
        require system messages at the beginning only.

        Entries with roles outside the LLM protocol (e.g. speak-only entries
        that duplicate assistant content) are skipped.
        """
        msgs: List[Dict[str, Any]] = []
        for entry in self.logs:
            if entry.role == "tool" and entry.tool_call_id:
                # LLM function-calling tool result (has matching assistant tool_calls)
                msg: Dict[str, Any] = {
                    "role": "tool",
                    "tool_call_id": entry.tool_call_id,
                    "name": entry.tool_name or "",
                    "content": entry.content,
                }
            elif entry.role == "tool" and not entry.tool_call_id:
                # Playbook-defined TOOL node result (no function-calling pair).
                # tool role は tool_call_id が必須なのでここでは使えず、また
                # role='system' を messages 中途に挿入すると Gemini 等で互換性
                # 問題が出る。そのため user role + <system> タグの統一形式で
                # 送る (詳細: sea/runtime_llm.py の同様の自動ラップ箇所のコメント参照)。
                tool_label = entry.tool_name or "tool"
                msg = {
                    "role": "user",
                    "content": f"<system>[{tool_label}] {entry.content}</system>",
                }
            elif entry.role == "assistant" and entry.tool_calls:
                msg = {
                    "role": "assistant",
                    "content": entry.content,
                    "tool_calls": entry.tool_calls,
                }
            elif entry.role in ("user", "assistant", "system"):
                msg = {"role": entry.role, "content": entry.content}
            else:
                continue
            msgs.append(msg)
        return msgs
