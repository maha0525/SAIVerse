"""Beat ロック + 実行台帳の関所 (BeatGate)。

docs/intent/beat_execution_context.md §2.2 / §3.4 と
docs/intent/execution_ledger.md §2.2-2.3 の実装:

- **Beat ロック**: persona 単位の :class:`threading.RLock`。persona の記憶に
  書く可能性のある生成処理 (会話 Pulse / META 判断 / 自律 / 作業セッション /
  Metabolism / sluice) を直列化する。どの記録も直前の記録を読める状態で
  生成される — 「一直線 = 踏まえた」が構造的に真になる (不変条件 9)。
- **関所 (fail-closed)**: Beat の開始 (= 最外周のロック取得) 直後に
  ``execution_ledger.flush_pending_for_persona`` を実行し、pending が残る
  persona では Beat を開始しない (:class:`BeatGateClosedError`)。これにより
  「pending が残ったまま新しい記憶が書かれる」状態は構造的に存在しない
  (不変条件 8)。
- **再入可能 (RLock)**: spell は ``run_playbook`` 等で子ラインを同期実行し、
  子が親より先に記録を書く既存構造がある (sea/runtime_llm.py の spell 実行
  ブロック参照)。同一実行スタック内の子は別 Beat ではなく親 Beat の一部
  (「親の直列域を継承する」、intent §3.4)。関所は**最外周の取得時のみ**実行
  する — 子ライン再入のたびに flush が走ると同一 Beat 内で配送が挟まり記録順
  が乱れるため。
- **Beat 境界** (:meth:`BeatGate.boundary`): 連続 Beat (spell ループの周) の
  切れ目でロックを一度手放し、待機中の別 Beat (META/user) を挟ませる。
  cancel 要求はこの境界で評価する (実行中の Beat は完了を待つ — 課金と生成の
  保護、intent §3.4 まはー裁定 2026-07-16)。

ロック順序の不変条件 (デッドロック回避):
常に **MetaLayer Lock → Beat ロック** の一方向 (メタ判断は meta_layer.py で
per-persona Lock を取得してから submit_meta_judgment → run_meta_user → Beat
取得に至る)。**Beat ロックを保持したまま MetaLayer Lock を取得する経路を
新設しないこと。**

スレッドについて: RLock の再入と :meth:`boundary` の解放は「ロックを取得した
スレッド」でのみ有効。graph 実行が executor スレッドへ逃げる経路
(sea/runtime_graph.py / work_session._run_coro_sync の running-loop 分岐) では
保持スレッドと実行スレッドが分かれ得るため、boundary は「このスレッドが最外周
保持者 (depth==1) のとき」だけ動き、それ以外は no-op とする (安全側: 挟み込み
機会を失うだけで、直列性・解放の正しさは壊れない)。
"""
from __future__ import annotations

import contextlib
import logging
import threading
from typing import Any, Dict, Iterator, Optional

from sea.cancellation import CancellationToken, ExecutionCancelledException

LOGGER = logging.getLogger(__name__)


class BeatGateClosedError(Exception):
    """関所 (pending flush) が通らなかった (fail-closed)。

    pending が残る persona では Beat を開始しない (execution_ledger.md §2.2)。
    この例外が出た時点で**実行は始まっていない** (副作用ゼロ) — 会話なら
    ユーザーへエラー応答、自律・判断なら台帳に prepared で残る実行を回復処理が
    拾う (PulseController 側の分岐参照)。
    """

    def __init__(self, persona_id: Optional[str], purpose: str, detail: Optional[str] = None):
        message = (
            f"Beat gate closed for persona={persona_id} (purpose={purpose}): "
            f"{detail or 'pending outbox items remain (fail-closed)'}"
        )
        super().__init__(message)
        self.persona_id = persona_id
        self.purpose = purpose


class BeatGate:
    """persona 単位の Beat ロック レジストリ + 実行台帳の関所。

    manager 所有の世界に 1 インスタンス (saiverse_manager.__init__ で構築のみ、
    スレッドも DB アクセスも発生しない)。manager に ``execution_ledger`` が
    無い環境 (テストの SimpleNamespace manager 等) では関所をスキップして
    ロックの取得だけを行う。
    """

    def __init__(self, manager: Any) -> None:
        self._manager = manager
        self._locks: Dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        # スレッドごとの {persona_id: 保持深さ}。RLock 自体は保持カウントを
        # 公開しないため自前で追跡する (最外周判定 = depth==1 に使う)。
        self._local = threading.local()

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _get_lock(self, persona_id: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._locks.get(persona_id)
            if lock is None:
                lock = threading.RLock()
                self._locks[persona_id] = lock
            return lock

    def _depth_map(self) -> Dict[str, int]:
        depths = getattr(self._local, "depths", None)
        if depths is None:
            depths = {}
            self._local.depths = depths
        return depths

    def held_depth(self, persona_id: str) -> int:
        """現在のスレッドがこの persona のロックを何段保持しているか (観測用)。"""
        return self._depth_map().get(persona_id, 0)

    def _flush_gate(self, persona_id: str, purpose: str) -> None:
        """関所: pending の全量配送を試み、通らなければ BeatGateClosedError。

        ledger が無い manager (テストシーム) では DEBUG ログのみでスキップする。
        flush 自体の例外 (DB 障害等) も fail-closed — 「配送できたか不明」の
        まま Beat を開始しない。
        """
        ledger = getattr(self._manager, "execution_ledger", None)
        if ledger is None:
            LOGGER.debug(
                "[beat_gate] no execution_ledger on manager; gate check skipped "
                "(persona=%s purpose=%s)", persona_id, purpose,
            )
            return
        try:
            ok = ledger.flush_pending_for_persona(persona_id)
        except Exception as exc:
            raise BeatGateClosedError(
                persona_id, purpose,
                detail=f"pending flush raised {type(exc).__name__}: {exc}",
            ) from exc
        if not ok:
            raise BeatGateClosedError(persona_id, purpose)

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    @contextlib.contextmanager
    def hold(
        self,
        persona_id: str,
        *,
        purpose: str,
        check_gate: bool = True,
    ) -> Iterator["BeatGate"]:
        """Beat ロックを取得し、最外周なら関所 (pending flush) を通す。

        - 関所が通らない場合は**ロックを解放してから** :class:`BeatGateClosedError`
          を raise する (fail-closed。呼び出し側に lock leak しない)。
        - 同一スレッドの再入 (子ライン / Metabolism 等) では関所を再実行しない —
          子は親の直列域の一部であり、Beat 途中の flush は記録順を乱す。
        - exit では必ず release する。ただし :meth:`boundary` が cancel /
          gate-closed で手放したまま再取得しなかった場合 (このスレッドの depth
          が 0 になっている場合) は二重解放しない。
        """
        if not persona_id:
            raise ValueError("persona_id is required")
        lock = self._get_lock(persona_id)
        lock.acquire()
        depths = self._depth_map()
        depth = depths.get(persona_id, 0) + 1
        depths[persona_id] = depth
        if depth == 1:
            LOGGER.debug(
                "[beat_gate] acquired persona=%s purpose=%s", persona_id, purpose,
            )
            if check_gate:
                try:
                    self._flush_gate(persona_id, purpose)
                except BaseException:
                    # fail-closed: 関所が通らない Beat は開始しない。
                    # ロックは解放して例外を伝播する。
                    depths.pop(persona_id, None)
                    lock.release()
                    raise
        try:
            yield self
        finally:
            current = self._depth_map().get(persona_id, 0)
            if current > 0:
                if current == 1:
                    self._depth_map().pop(persona_id, None)
                    LOGGER.debug(
                        "[beat_gate] released persona=%s purpose=%s",
                        persona_id, purpose,
                    )
                else:
                    self._depth_map()[persona_id] = current - 1
                lock.release()
            # current == 0: boundary が cancel / gate-closed で手放したまま
            # 再取得しなかった経路。解放済みなので何もしない。

    def boundary(
        self,
        persona_id: str,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> None:
        """Beat 境界: 解放 → cancel 評価 → 再取得 → 関所。

        連続 Beat (spell ループの周) の切れ目で呼ぶ。解放の隙間で、待機中の
        別スレッドの Beat (META 判断 / user 会話) が挟まる。

        - cancel 済みなら**再取得せず** :class:`ExecutionCancelledException` を
          raise する (次の Beat を開始しない — intent §3.4 の割り込み裁定)。
        - 再取得後の関所が通らなければロックを解放して
          :class:`BeatGateClosedError` を raise する (Pulse は中断、記録済み分は正)。
        - 最外周保持 (このスレッドの depth==1) でのみ有効。ネスト中 (depth>1、
          子ラインは親の直列域の一部) と非保持スレッド (depth==0、executor へ
          逃げた graph 実行など) では何もしない。
        """
        depths = self._depth_map()
        if depths.get(persona_id, 0) != 1:
            return
        lock = self._get_lock(persona_id)
        depths.pop(persona_id, None)
        lock.release()
        LOGGER.debug("[beat_gate] boundary released persona=%s", persona_id)
        # --- 解放の隙間: 他スレッドの Beat がここで挟まる ---
        if cancellation_token is not None and cancellation_token.is_cancelled():
            raise ExecutionCancelledException(
                message="Execution interrupted at Beat boundary",
                interrupted_by=cancellation_token.interrupted_by,
            )
        lock.acquire()
        depths[persona_id] = 1
        try:
            self._flush_gate(persona_id, purpose="boundary")
        except BaseException:
            depths.pop(persona_id, None)
            lock.release()
            raise
        LOGGER.debug("[beat_gate] boundary reacquired persona=%s", persona_id)


def hold_beat(
    manager: Any,
    persona_id: Optional[str],
    *,
    purpose: str,
    check_gate: bool = True,
):
    """manager から beat_gate を引いて hold する context manager を返す。

    manager に ``beat_gate`` が無い環境 (テストの SimpleNamespace 等) や
    persona_id 不明のときは no-op (:func:`contextlib.nullcontext`) を返す —
    既存テスト・部分構築環境との互換シーム。
    """
    gate = getattr(manager, "beat_gate", None) if manager is not None else None
    if gate is None or not persona_id:
        return contextlib.nullcontext()
    return gate.hold(persona_id, purpose=purpose, check_gate=check_gate)


__all__ = ["BeatGate", "BeatGateClosedError", "hold_beat"]
