"""実行台帳 (Execution Ledger) — 不可逆な実行と記録の分裂を防ぐ共通基盤 (Phase 0)。

docs/intent/execution_ledger.md の §2 (解の骨格) / §3 (不変条件) の実装。
台帳 API は本モジュールに閉じ、生 SQL を各所に書かせない (intent §4 設計メモ)。

部品は 2 つ + 原則 1 つ:

1. **実行台帳** (``execution_ledger`` テーブル): 一回の不可逆な実行に
   execution_id を採番し、状態機械
   ``prepared → running → applied → completed / failed / unknown``
   を強制する。``(kind, idempotency_key)`` の UNIQUE 制約で境界イベントの
   二重発火を一意に収束させる。
2. **送信トレイ** (``execution_outbox`` テーブル): world DB と memory.db を
   跨ぐ書き込みの配達保証。「memory.db にこれを書く」というやること自体を、
   世界側の適用と同一トランザクションで world DB に書く (:meth:`mark_applied`)。
   配送は persona 単位 FIFO。関所 (:meth:`flush_pending_for_persona`) が
   fail-closed で「pending が残ったまま新しい記憶が書かれる」状態を構造的に
   排除する (不変条件 8)。
3. **大原則**: LLM は自動再実行しない (intent §2.5)。本基盤が再試行するのは
   「結果の記録」(= outbox の配送) だけ。``unknown`` (LLM が動いたか不明) は
   照合対象として残し、決して自動再実行しない。

設計上の約束:

- **時刻は必ず ``saiverse.clock.now()`` 経由** (epoch 秒 int)。一日シミュレータ
  の仮想クロックを尊重する (autonomous_behavior_v2.md §12、episodes.py と同じ)。
- DB access は session_factory (``manager.SessionLocal`` 相当) → try/finally
  close の既存流儀。ORM オブジェクトは外に出さず dict に直列化して返す。
- 台帳はシステム側の器であり、ペルソナからは見えない (intent §5)。

プロセス世代照合について (intent §2.4 #4):
``saiverse/runtime_marker.py`` の process identity (pid + create_time + token)
は「いま生きている SAIVerse プロセス」の検証には使えるが、台帳 v0.1 スキーマは
行ごとの実行主プロセス identity を持たないため、「この running 行はどの世代の
プロセスが開始したか」を行単位で照合することはできない。世代照合は
**起動時の一括 sweep** (:meth:`recover_stale_running` の ``all_running=True``)
として表現する — プロセス起動直後なら running 行は定義上すべて前世代のもの
(main.py が runtime_marker を取得してから manager.start() を呼ぶ順序が
「同一 DB を共有する他 City プロセスの不在」を保証する)。

世界への結線 (manager 所有・起動時回復・60 秒掃除 tick・実ハンドラ 2 種) は
``saiverse/execution_ledger_wiring.py``。Pulse 前関所 (Beat ロックとの結線) は
beat_execution_context.md §3.4 の工事で入る (§6-2 後半)。
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.models import ExecutionLedgerEntry, ExecutionOutboxItem
from saiverse import clock

LOGGER = logging.getLogger(__name__)

# --- 台帳の状態語彙 (intent §2.1) ---
STATUS_PREPARED = "prepared"
STATUS_RUNNING = "running"
STATUS_APPLIED = "applied"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_UNKNOWN = "unknown"

# --- 送信トレイの状態語彙 (intent §4) ---
OUTBOX_PENDING = "pending"
OUTBOX_DELIVERED = "delivered"
OUTBOX_DEAD = "dead"

#: 配送再試行の既定上限。超過で dead (人裁定に回す終端、黙って捨てない)。
DEFAULT_MAX_ATTEMPTS = 20

#: 合法遷移 (intent §2.1 の状態遷移図)。
#: - prepared → running (不可逆処理の開始) / failed (期限切れ・破棄)
#: - running  → applied (世界適用 commit) / unknown (観測途絶) /
#:              failed (適用前の検証棄却のみ — 適用後の failed は存在しない)
#: - applied  → completed (outbox 全配送)
#: - unknown  → applied (外部証跡との照合で復元。LLM 再実行ではない)
_LEGAL_TRANSITIONS: Dict[str, frozenset] = {
    STATUS_PREPARED: frozenset({STATUS_RUNNING, STATUS_FAILED}),
    STATUS_RUNNING: frozenset({STATUS_APPLIED, STATUS_UNKNOWN, STATUS_FAILED}),
    STATUS_APPLIED: frozenset({STATUS_COMPLETED}),
    STATUS_UNKNOWN: frozenset({STATUS_APPLIED}),
    STATUS_COMPLETED: frozenset(),
    STATUS_FAILED: frozenset(),
}


class ExecutionLedgerError(Exception):
    """Base error for execution ledger operations."""


class ExecutionNotFoundError(ExecutionLedgerError):
    """Raised when an execution_id does not exist in the ledger."""


class IllegalTransitionError(ExecutionLedgerError):
    """Raised on a state transition not allowed by the state machine."""


def _now_epoch() -> int:
    """現在時刻 (epoch 秒)。仮想クロック尊重のため必ず clock.now() を通す。"""
    return int(clock.now().timestamp())


def _entry_to_dict(entry: ExecutionLedgerEntry) -> Dict[str, Any]:
    """台帳行を detached な dict に直列化する (ORM オブジェクトを外に出さない)。"""
    def _load(raw: Optional[str], label: str) -> Any:
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            LOGGER.warning("[ledger] %s is not valid JSON: %r", label, raw)
            return None

    return {
        "execution_id": entry.EXECUTION_ID,
        "kind": entry.KIND,
        "idempotency_key": entry.IDEMPOTENCY_KEY,
        "persona_id": entry.PERSONA_ID,
        "status": entry.STATUS,
        "payload": _load(entry.PAYLOAD_JSON, "PAYLOAD_JSON"),
        "result": _load(entry.RESULT_JSON, "RESULT_JSON"),
        "error": entry.ERROR,
        "created_at": entry.CREATED_AT,
        "updated_at": entry.UPDATED_AT,
    }


def _outbox_to_dict(item: ExecutionOutboxItem) -> Dict[str, Any]:
    """送信トレイ行を detached な dict に直列化する。"""
    try:
        payload = json.loads(item.PAYLOAD_JSON) if item.PAYLOAD_JSON else None
    except (TypeError, ValueError):
        LOGGER.warning("[ledger] outbox PAYLOAD_JSON is not valid JSON: %r", item.PAYLOAD_JSON)
        payload = None
    return {
        "outbox_id": item.OUTBOX_ID,
        "execution_id": item.EXECUTION_ID,
        "target": item.TARGET,
        "persona_id": item.PERSONA_ID,
        "payload": payload,
        "status": item.STATUS,
        "attempts": item.ATTEMPTS,
        "last_error": item.LAST_ERROR,
        "created_at": item.CREATED_AT,
        "delivered_at": item.DELIVERED_AT,
    }


class ExecutionLedger:
    """実行台帳 + 送信トレイの操作を一手に引き受けるヘルパー。

    世界に 1 インスタンス (manager 所有を想定。配線は次タスク)。テストは
    一時 DB の sessionmaker を渡して独立に使える。

    Args:
        session_factory: SQLAlchemy Session を返す callable
            (``manager.SessionLocal`` / ``sessionmaker(bind=engine)``)。
        max_attempts: outbox 配送の再試行上限。超過で dead。
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._session_factory = session_factory
        self._max_attempts = max_attempts
        self._handlers: Dict[str, Callable[[Dict[str, Any]], Any]] = {}
        self._handlers_lock = threading.Lock()
        # persona 単位 FIFO を並行 flush から守る (Phase 0 は単一ロックで十分。
        # persona 別ロックへの細粒度化は実測で必要になってから)。
        self._delivery_lock = threading.Lock()
        # 再入検知 (W5 Codex レビュー P1): outbox handler の実行は
        # _delivery_lock 保持下で行われる。handler が誘発した副作用 (例:
        # move.post_dynamic_state → inject_diff_notifications →
        # mark_applied(deliver=True)、move.post_game_lifecycle →
        # on_entity_moved → 別 persona の move_entity) が同じスレッドから
        # 再度 flush_pending_for_persona を呼ぶと、非再入ロックで永久待ちに
        # なる。スレッドローカルな旗で検知し、ネストした呼び出しは配送を
        # 試みずに即座に離脱する (次の即時配送呼び出し・Beat 関所・回復 tick
        # が拾う — 「今回は配送されない」であって「失われる」ではない)。
        self._flush_local = threading.local()

    # ------------------------------------------------------------------
    # 台帳: 登録と状態遷移
    # ------------------------------------------------------------------

    def begin_execution(
        self,
        kind: str,
        idempotency_key: Optional[str] = None,
        persona_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, bool]:
        """実行を台帳に登録する (status=prepared。副作用はまだ何も無い区間)。

        冪等 INSERT: ``(kind, idempotency_key)`` が既存行と衝突した場合、新規行を
        作らず既存行の ``(execution_id, False)`` を返す — 既存行の状態は問わない
        (「既に走った/走っている」の判定は kind ごとの回収規則の仕事、Phase 1)。
        ``idempotency_key=None`` は一意性不要な実行で、常に新規行を作る。

        Returns:
            ``(execution_id, created)``。``created=False`` は冪等キー衝突で
            既存の実行に合流したことを意味する。
        """
        if not kind:
            raise ValueError("kind is required")
        execution_id = str(uuid.uuid4())
        now = _now_epoch()
        db = self._session_factory()
        try:
            entry = ExecutionLedgerEntry(
                EXECUTION_ID=execution_id,
                KIND=kind,
                IDEMPOTENCY_KEY=idempotency_key,
                PERSONA_ID=persona_id,
                STATUS=STATUS_PREPARED,
                PAYLOAD_JSON=(
                    json.dumps(payload, ensure_ascii=False) if payload is not None else None
                ),
                CREATED_AT=now,
                UPDATED_AT=now,
            )
            db.add(entry)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                if idempotency_key is None:
                    # uuid4 PK 衝突は実質あり得ない — 別種の整合性違反を隠さない
                    raise
                existing = (
                    db.query(ExecutionLedgerEntry)
                    .filter(
                        ExecutionLedgerEntry.KIND == kind,
                        ExecutionLedgerEntry.IDEMPOTENCY_KEY == idempotency_key,
                    )
                    .first()
                )
                if existing is None:
                    raise
                LOGGER.info(
                    "[ledger] begin_execution dedup: kind=%s key=%s -> existing %s (status=%s)",
                    kind, idempotency_key, existing.EXECUTION_ID, existing.STATUS,
                )
                return existing.EXECUTION_ID, False
            LOGGER.info(
                "[ledger] prepared %s kind=%s key=%s persona=%s",
                execution_id, kind, idempotency_key, persona_id,
            )
            return execution_id, True
        finally:
            db.close()

    def claim_execution(
        self,
        kind: str,
        idempotency_key: Optional[str] = None,
        persona_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Optional[str], bool, Optional[str]]:
        """実行の「席」を取る (Phase 1 API — 判断点の重複抑止、intent §11-3)。

        :meth:`begin_execution` (冪等 INSERT のみ) と違い、既存行の**状態を見て**
        「いま走ってよいか」まで判定する:

        - 既存なし / ``idempotency_key=None`` → 新規 prepared → runnable
        - 既存 prepared → その行を再利用して runnable (payload は既存の凍結値を
          維持する — 上書きしない)。**注意**: ほぼ同時の二重 claim は同じ
          execution_id を両方に runnable として返しうる — 実行を一意化するのは
          :meth:`try_mark_running` (prepared→running の条件付き遷移) で、敗者は
          台帳へ書かずに離脱すること (:meth:`abandon_prepared` 参照)
        - 既存 failed → キーを ``{key}#failed-{id先頭8字}`` に退避して新規
          prepared → runnable (failed は副作用ゼロ保証なので再実行安全、
          intent §2.1)
        - 既存 running / applied / completed → runnable=False (既に走った /
          走っている)
        - 既存 unknown → runnable=False。**自動再実行禁止** (intent §2.5) —
          裁定 (list_unknown → 照合) まで同キーの実行はブロックする

        競合 (同時 INSERT) は :meth:`begin_execution` の IntegrityError 流儀に
        倣い、rollback → 再読で既存行へ収束させる。

        Returns:
            ``(execution_id, runnable, existing_status)``。``runnable=True`` の
            とき execution_id は実行に使ってよい prepared 行。
            ``existing_status`` は既存行に合流/退避したときの元の状態
            (新規作成なら None)。
        """
        if not kind:
            raise ValueError("kind is required")
        if idempotency_key is None:
            execution_id, _created = self.begin_execution(
                kind, idempotency_key=None, persona_id=persona_id, payload=payload,
            )
            return execution_id, True, None

        for _attempt in range(2):
            db = self._session_factory()
            try:
                existing = (
                    db.query(ExecutionLedgerEntry)
                    .filter(
                        ExecutionLedgerEntry.KIND == kind,
                        ExecutionLedgerEntry.IDEMPOTENCY_KEY == idempotency_key,
                    )
                    .first()
                )
                now = _now_epoch()
                if existing is None:
                    execution_id = str(uuid.uuid4())
                    db.add(ExecutionLedgerEntry(
                        EXECUTION_ID=execution_id,
                        KIND=kind,
                        IDEMPOTENCY_KEY=idempotency_key,
                        PERSONA_ID=persona_id,
                        STATUS=STATUS_PREPARED,
                        PAYLOAD_JSON=(
                            json.dumps(payload, ensure_ascii=False)
                            if payload is not None else None
                        ),
                        CREATED_AT=now,
                        UPDATED_AT=now,
                    ))
                    try:
                        db.commit()
                    except IntegrityError:
                        # 競合: 別スレッド/プロセスが先に INSERT — 再読で収束
                        db.rollback()
                        continue
                    LOGGER.info(
                        "[ledger] claimed %s kind=%s key=%s persona=%s",
                        execution_id, kind, idempotency_key, persona_id,
                    )
                    return execution_id, True, None

                status = existing.STATUS
                if status == STATUS_PREPARED:
                    LOGGER.info(
                        "[ledger] claim reuses prepared %s kind=%s key=%s "
                        "(payload frozen, not overwritten)",
                        existing.EXECUTION_ID, kind, idempotency_key,
                    )
                    return existing.EXECUTION_ID, True, STATUS_PREPARED
                if status == STATUS_FAILED:
                    retired_key = (
                        f"{idempotency_key}#failed-{existing.EXECUTION_ID[:8]}"
                    )
                    existing.IDEMPOTENCY_KEY = retired_key
                    execution_id = str(uuid.uuid4())
                    db.add(ExecutionLedgerEntry(
                        EXECUTION_ID=execution_id,
                        KIND=kind,
                        IDEMPOTENCY_KEY=idempotency_key,
                        PERSONA_ID=persona_id,
                        STATUS=STATUS_PREPARED,
                        PAYLOAD_JSON=(
                            json.dumps(payload, ensure_ascii=False)
                            if payload is not None else None
                        ),
                        CREATED_AT=now,
                        UPDATED_AT=now,
                    ))
                    try:
                        db.commit()
                    except IntegrityError:
                        db.rollback()
                        continue
                    LOGGER.info(
                        "[ledger] claim retired failed %s (key -> %s) and "
                        "claimed %s kind=%s key=%s",
                        existing.EXECUTION_ID, retired_key,
                        execution_id, kind, idempotency_key,
                    )
                    return execution_id, True, STATUS_FAILED

                if status == STATUS_UNKNOWN:
                    LOGGER.warning(
                        "[ledger] claim blocked by unknown execution %s "
                        "(kind=%s key=%s): 自動再実行は禁止 (intent §2.5)。"
                        "list_unknown で照合・裁定するまで同キーの実行は"
                        "ブロックされます",
                        existing.EXECUTION_ID, kind, idempotency_key,
                    )
                else:
                    LOGGER.info(
                        "[ledger] claim dedup: kind=%s key=%s -> existing %s "
                        "(status=%s, not runnable)",
                        kind, idempotency_key, existing.EXECUTION_ID, status,
                    )
                return existing.EXECUTION_ID, False, status
            finally:
                db.close()
        raise ExecutionLedgerError(
            f"claim_execution did not converge (kind={kind}, key={idempotency_key})"
        )

    def _get_entry(self, db: Session, execution_id: str) -> ExecutionLedgerEntry:
        entry = (
            db.query(ExecutionLedgerEntry)
            .filter(ExecutionLedgerEntry.EXECUTION_ID == execution_id)
            .first()
        )
        if entry is None:
            raise ExecutionNotFoundError(f"execution not found: {execution_id}")
        return entry

    def _transition(
        self,
        db: Session,
        execution_id: str,
        new_status: str,
        *,
        error: Optional[str] = None,
        result: Optional[Dict[str, Any]] = None,
    ) -> ExecutionLedgerEntry:
        """状態遷移の合法性を強制する唯一の口。不正遷移は IllegalTransitionError。"""
        entry = self._get_entry(db, execution_id)
        allowed = _LEGAL_TRANSITIONS.get(entry.STATUS, frozenset())
        if new_status not in allowed:
            raise IllegalTransitionError(
                f"illegal transition {entry.STATUS} -> {new_status} "
                f"(execution={execution_id}, kind={entry.KIND})"
            )
        entry.STATUS = new_status
        entry.UPDATED_AT = _now_epoch()
        if error is not None:
            entry.ERROR = error
        if result is not None:
            entry.RESULT_JSON = json.dumps(result, ensure_ascii=False)
        return entry

    def mark_running(
        self, execution_id: str, *, session: Optional[Session] = None
    ) -> None:
        """不可逆処理 (LLM 等) の開始を宣言する (prepared → running)。

        この遷移**後**に不可逆処理を開始すること (不変条件 1)。

        Args:
            session: 呼び出し元が開いている Session。**指定した場合、本メソッドは
                commit しない** — prepared→running 遷移が呼び出し元の 1 commit に
                同梱される (:meth:`mark_applied` の session 分岐と対称。予約 tx で
                slot 状態・予算・episode open と running 遷移を単一トランザクションに
                束ねる口)。指定なしなら自前 Session で即 commit する (従来挙動)。
        """
        if session is not None:
            entry = self._transition(session, execution_id, STATUS_RUNNING)
            LOGGER.debug(
                "[ledger] running staged: %s kind=%s (commit is caller's)",
                execution_id, entry.KIND,
            )
            return

        db = self._session_factory()
        try:
            entry = self._transition(db, execution_id, STATUS_RUNNING)
            db.commit()
            LOGGER.info("[ledger] running %s kind=%s", execution_id, entry.KIND)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def try_mark_running(
        self, execution_id: str, *, session: Optional[Session] = None
    ) -> bool:
        """prepared → running を**早い者勝ち**で行う条件付き遷移 (勝者の一意化)。

        :meth:`claim_execution` は既存 prepared 行を再利用するため、ほぼ同時の
        二重 claim は**同じ execution_id** を両方に runnable として返しうる。
        本メソッドは「status が prepared のときだけ running へ」の条件付き一括
        UPDATE で席を取り、勝者を一人に絞る — 敗者は False を受け取り、台帳に
        **一切書かず**離脱すること (勝者の走行中台帳を failed 等へ壊さないため)。

        Args:
            session: 呼び出し元が開いている Session。指定時は commit しない
                (:meth:`mark_running` の session 分岐と対称 — 予約 tx の 1 commit
                に同梱され、tx が転べば席取りも巻き戻る)。

        Returns:
            True = 席が取れた (running へ遷移)。False = 既に他状態 (別の実行者が
            走行中 / 完了済み / failed 等) — 呼び出し元は副作用を start しない。
        """

        def _cas(db: Session) -> int:
            return (
                db.query(ExecutionLedgerEntry)
                .filter(
                    ExecutionLedgerEntry.EXECUTION_ID == execution_id,
                    ExecutionLedgerEntry.STATUS == STATUS_PREPARED,
                )
                .update(
                    {
                        ExecutionLedgerEntry.STATUS: STATUS_RUNNING,
                        ExecutionLedgerEntry.UPDATED_AT: _now_epoch(),
                    },
                    synchronize_session=False,
                )
            )

        if session is not None:
            won = _cas(session) > 0
            LOGGER.debug(
                "[ledger] running seat %s: %s (commit is caller's)",
                "taken" if won else "lost", execution_id,
            )
            return won

        db = self._session_factory()
        try:
            won = _cas(db) > 0
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        if won:
            LOGGER.info("[ledger] running %s (seat taken)", execution_id)
        else:
            LOGGER.info("[ledger] running seat lost: %s", execution_id)
        return won

    def abandon_prepared(self, execution_id: str, error: str) -> bool:
        """prepared のまま**動いていない**実行だけを failed へ落とす条件付き遷移。

        二重 claim で同じ execution_id を共有した敗者が「対象コマ消失」等で離脱
        するとき、無条件の :meth:`mark_failed` では**勝者が走行中の running 台帳**
        まで failed に壊してしまう (勝者の精算が failed → applied の不正遷移で
        爆発する)。本メソッドは status=prepared のときだけ failed に落とすので、
        誰かが席を取った後 (running / applied / ...) は何もせず False を返す。

        Returns:
            True = failed へ落とした (誰も走っていなかった)。False = 既に他状態
            (勝者の所有) — 台帳は無変更。
        """
        db = self._session_factory()
        try:
            updated = (
                db.query(ExecutionLedgerEntry)
                .filter(
                    ExecutionLedgerEntry.EXECUTION_ID == execution_id,
                    ExecutionLedgerEntry.STATUS == STATUS_PREPARED,
                )
                .update(
                    {
                        ExecutionLedgerEntry.STATUS: STATUS_FAILED,
                        ExecutionLedgerEntry.ERROR: error,
                        ExecutionLedgerEntry.UPDATED_AT: _now_epoch(),
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        if updated:
            LOGGER.info(
                "[ledger] abandoned prepared %s error=%s", execution_id, error,
            )
        else:
            LOGGER.info(
                "[ledger] abandon skipped (not prepared — owned by another "
                "execution path): %s", execution_id,
            )
        return updated > 0

    def mark_applied(
        self,
        execution_id: str,
        *,
        result: Optional[Dict[str, Any]] = None,
        outbox_items: Optional[Sequence[Dict[str, Any]]] = None,
        session: Optional[Session] = None,
        deliver: bool = True,
    ) -> None:
        """世界への適用を宣言し、outbox 積みを**同一トランザクション**で行う。

        running → applied (通常) / unknown → applied (照合復元、intent §2.4 #5)。

        Args:
            result: RESULT_JSON に刻む実行結果の要約。
            outbox_items: 配送予約の列。各要素は
                ``{"target": str, "payload": dict, "persona_id": str | None}``。
                payload は実行時点の内容をそのまま凍結する (不変条件 6)。
            session: 呼び出し元が開いている Session。**指定した場合、本メソッドは
                commit しない** — 世界側の適用 (呼び出し元の書き込み) と台帳更新・
                outbox 積みが呼び出し元の 1 commit に同梱される (これが「world 側の
                適用と outbox 積みを同一トランザクションにする口」)。配送は
                呼び出し元 commit 後に関所 / 回復 tick が行う。
            deliver: 自前 session (``session=None``) のとき、commit 直後に対象
                persona の pending を即時配送するか (intent §2.2「適用 commit の
                直後に即時配送を試みる」)。配送失敗は applied を巻き戻さない
                (pending に残り、関所・回復 tick が引き継ぐ)。
        """
        if session is not None:
            self._apply_in_session(session, execution_id, result, outbox_items)
            return

        db = self._session_factory()
        personas: List[Optional[str]] = []
        try:
            personas = self._apply_in_session(db, execution_id, result, outbox_items)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        LOGGER.info(
            "[ledger] applied %s (outbox=%d)", execution_id,
            len(outbox_items) if outbox_items else 0,
        )
        if deliver:
            for pid in personas:
                try:
                    self.flush_pending_for_persona(pid)
                except Exception:
                    # 適用は committed 済み。配送失敗は pending に残るだけで、
                    # 関所 / 回復 tick が引き継ぐ (intent §2.2)。
                    LOGGER.error(
                        "[ledger] immediate delivery failed after apply "
                        "(execution=%s persona=%s); left pending",
                        execution_id, pid, exc_info=True,
                    )

    def _apply_in_session(
        self,
        db: Session,
        execution_id: str,
        result: Optional[Dict[str, Any]],
        outbox_items: Optional[Sequence[Dict[str, Any]]],
    ) -> List[Optional[str]]:
        """applied 遷移 + outbox INSERT を与えられた session 上で行う (commit しない)。

        Returns:
            outbox_items に現れた persona_id の列 (重複除去、出現順)。
        """
        entry = self._transition(db, execution_id, STATUS_APPLIED, result=result)
        now = _now_epoch()
        personas: List[Optional[str]] = []
        for index, item in enumerate(outbox_items or []):
            target = item.get("target")
            if not target:
                raise ValueError(f"outbox item [{index}] requires 'target'")
            payload = item.get("payload")
            if payload is None:
                raise ValueError(f"outbox item [{index}] requires 'payload'")
            persona_id = item.get("persona_id")
            db.add(ExecutionOutboxItem(
                EXECUTION_ID=execution_id,
                TARGET=target,
                PERSONA_ID=persona_id,
                PAYLOAD_JSON=json.dumps(payload, ensure_ascii=False),
                STATUS=OUTBOX_PENDING,
                ATTEMPTS=0,
                CREATED_AT=now,
            ))
            if persona_id not in personas:
                personas.append(persona_id)
        LOGGER.debug(
            "[ledger] apply staged: %s kind=%s outbox=%d (commit is caller's)",
            execution_id, entry.KIND, len(outbox_items or []),
        )
        return personas

    def mark_completed(self, execution_id: str) -> None:
        """終端遷移 (applied → completed)。

        成功報告は証跡から導出する (不変条件 4): outbox に未配送 (pending / dead)
        の行が 1 件でも残っていれば completed を許さず例外を投げる。outbox を
        持たない実行はそのまま閉じられる。

        NOTE: outbox を持つ実行は、配送器が全配送を確認した時点で自動的に
        completed へ遷移させる (:meth:`flush_pending_for_persona`) ため、
        通常は本メソッドを明示的に呼ぶ必要はない。
        """
        db = self._session_factory()
        try:
            undelivered = (
                db.query(ExecutionOutboxItem)
                .filter(
                    ExecutionOutboxItem.EXECUTION_ID == execution_id,
                    ExecutionOutboxItem.STATUS != OUTBOX_DELIVERED,
                )
                .count()
            )
            if undelivered:
                raise ExecutionLedgerError(
                    f"cannot complete {execution_id}: "
                    f"{undelivered} undelivered outbox item(s) remain"
                )
            entry = self._transition(db, execution_id, STATUS_COMPLETED)
            db.commit()
            LOGGER.info("[ledger] completed %s kind=%s", execution_id, entry.KIND)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def mark_failed(self, execution_id: str, error: str) -> None:
        """明示失敗 (prepared → failed = 副作用ゼロ / running → failed = 適用前の検証棄却)。

        failed は「安全に再試行・破棄できる」状態 (intent §2.1)。running からの
        failed は**世界適用前の検証棄却のみ**に使うこと — 適用後に失敗を宣言する
        経路は存在しない (適用済みなら applied、結果不明なら unknown)。
        """
        db = self._session_factory()
        try:
            entry = self._transition(db, execution_id, STATUS_FAILED, error=error)
            db.commit()
            LOGGER.info(
                "[ledger] failed %s kind=%s error=%s", execution_id, entry.KIND, error,
            )
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def mark_unknown(self, execution_id: str, reason: Optional[str] = None) -> None:
        """結果不明 (running → unknown)。**自動再実行禁止**の最重要状態 (intent §2.5)。

        回復処理は外部証跡との照合だけを行い、裁定できれば
        :meth:`mark_applied` (unknown → applied) で復元する。
        """
        db = self._session_factory()
        try:
            entry = self._transition(
                db, execution_id, STATUS_UNKNOWN,
                error=reason or "observation lost while running",
            )
            db.commit()
            LOGGER.warning(
                "[ledger] unknown %s kind=%s reason=%s",
                execution_id, entry.KIND, reason,
            )
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    # ------------------------------------------------------------------
    # 送信トレイ: 配送器と関所
    # ------------------------------------------------------------------

    def register_outbox_handler(
        self, target: str, fn: Callable[[Dict[str, Any]], Any]
    ) -> None:
        """TARGET 名 → 配送 handler を登録する。

        handler は配送 1 件ぶんの dict
        ``{outbox_id, execution_id, target, persona_id, payload, created_at}``
        を受け取り、失敗は例外で表明する (戻り値は見ない)。配送先の書き込みは
        execution_id を冪等キーとして刻むこと (再配送しても二重にならない —
        不変条件 3。強制は配送先の実装の責務)。
        """
        if not target:
            raise ValueError("target is required")
        if not callable(fn):
            raise ValueError("handler must be callable")
        with self._handlers_lock:
            if target in self._handlers:
                LOGGER.warning("[ledger] outbox handler for %r is being replaced", target)
            self._handlers[target] = fn

    def flush_pending_for_persona(self, persona_id: Optional[str]) -> bool:
        """関所 (intent §2.2): 対象 persona の pending を FIFO で全量配送試行する。

        - 配送は OUTBOX_ID 昇順 (= 実行時刻順)。先頭が失敗したら後続は試行しない
          (記憶の順序一貫性、不変条件 8)。別 persona のキューは独立。
        - 失敗が再試行上限に達した行は dead に落ちる。dead は「先頭」とみなさず
          飛ばす (後続をブロックしない) — 人裁定待ちとして :meth:`list_dead` に残る。
        - 全配送済みになった execution (status=applied) は completed へ自動遷移
          (「completed の根拠は outbox の全 delivered」不変条件 4)。

        Returns:
            True — その persona の pending が残っていない (実行を開始してよい)。
            False — 1 件でも pending が残った (fail-closed: 実行を開始しない)。
            ネストした呼び出し (下記) も False — 呼び出し元 (:meth:`mark_applied`
            の即時配送、outbox handler 内の後続配送) はいずれも戻り値を見ない
            void 呼び出しなので実害はない。戻り値を実際の判定に使う唯一の
            呼び出し元 (:mod:`sea.beat_gate` の Beat 関所) は Beat 境界の
            最外周からのみ呼ぶため、ネストされる側にはならない。

        再入検知 (W5): 同一スレッドで既に flush 実行中 (= outbox handler の
        中から呼ばれた) 場合は、ロックを取らずに False を返して離脱する。
        handler が別の実行 (移動の誘発等) を通じて自分自身の配送を再帰的に
        呼ぶ経路があり、非再入ロックのままだと永久待ちになるため。
        """
        if getattr(self._flush_local, "active", False):
            LOGGER.debug(
                "[ledger] flush_pending_for_persona reentered on the same "
                "thread (persona=%s) — an outbox handler triggered another "
                "flush; skipping the nested call (deferred to the next "
                "gate/recovery pass) to avoid deadlocking on _delivery_lock",
                persona_id,
            )
            return False
        self._flush_local.active = True
        try:
            with self._delivery_lock:
                return self._flush_queue(persona_id)
        finally:
            self._flush_local.active = False

    def _flush_queue(self, persona_id: Optional[str]) -> bool:
        db = self._session_factory()
        try:
            persona_filter = (
                ExecutionOutboxItem.PERSONA_ID.is_(None)
                if persona_id is None
                else ExecutionOutboxItem.PERSONA_ID == persona_id
            )
            rows = (
                db.query(ExecutionOutboxItem)
                .filter(persona_filter, ExecutionOutboxItem.STATUS == OUTBOX_PENDING)
                .order_by(ExecutionOutboxItem.OUTBOX_ID.asc())
                .all()
            )
            delivered_executions: List[str] = []
            for row in rows:
                if self._deliver_one(db, row):
                    delivered_executions.append(row.EXECUTION_ID)
                    continue
                if row.STATUS == OUTBOX_DEAD:
                    # dead は先頭とみなさず飛ばす (後続をブロックしない)
                    continue
                # pending のままの失敗 = FIFO 先頭ブロック。後続は試行しない
                break
            for execution_id in dict.fromkeys(delivered_executions):
                self._maybe_complete(db, execution_id)
            remaining = (
                db.query(ExecutionOutboxItem)
                .filter(persona_filter, ExecutionOutboxItem.STATUS == OUTBOX_PENDING)
                .count()
            )
            if remaining:
                LOGGER.warning(
                    "[ledger] flush persona=%s: %d pending item(s) remain (gate closed)",
                    persona_id, remaining,
                )
            return remaining == 0
        finally:
            db.close()

    def _deliver_one(self, db: Session, row: ExecutionOutboxItem) -> bool:
        """outbox 1 行の配送を試みる。成功で delivered、失敗で ATTEMPTS++/dead。

        handler 未登録は「環境不備」であって配送の実試行ではないため、
        ATTEMPTS は増やさない (dead に数えない) — ただし FIFO はブロックする
        (fail-closed。起動順の隙間で正当な記録が dead に焼かれるのを防ぐ)。
        """
        with self._handlers_lock:
            handler = self._handlers.get(row.TARGET)
        if handler is None:
            row.LAST_ERROR = f"no handler registered for target '{row.TARGET}'"
            db.commit()
            LOGGER.warning(
                "[ledger] no outbox handler for target=%s (outbox_id=%s execution=%s); "
                "delivery blocked (attempts not counted)",
                row.TARGET, row.OUTBOX_ID, row.EXECUTION_ID,
            )
            return False
        try:
            payload = json.loads(row.PAYLOAD_JSON)
            handler({
                "outbox_id": row.OUTBOX_ID,
                "execution_id": row.EXECUTION_ID,
                "target": row.TARGET,
                "persona_id": row.PERSONA_ID,
                "payload": payload,
                "created_at": row.CREATED_AT,
            })
        except Exception as exc:
            row.ATTEMPTS = (row.ATTEMPTS or 0) + 1
            row.LAST_ERROR = str(exc) or type(exc).__name__
            if row.ATTEMPTS >= self._max_attempts:
                row.STATUS = OUTBOX_DEAD
                LOGGER.error(
                    "[ledger] outbox item dead after %d attempts: outbox_id=%s "
                    "target=%s execution=%s persona=%s",
                    row.ATTEMPTS, row.OUTBOX_ID, row.TARGET,
                    row.EXECUTION_ID, row.PERSONA_ID, exc_info=True,
                )
            else:
                LOGGER.warning(
                    "[ledger] outbox delivery failed (attempt %d/%d): outbox_id=%s "
                    "target=%s execution=%s: %s",
                    row.ATTEMPTS, self._max_attempts, row.OUTBOX_ID,
                    row.TARGET, row.EXECUTION_ID, exc,
                )
            db.commit()
            return False
        row.STATUS = OUTBOX_DELIVERED
        row.DELIVERED_AT = _now_epoch()
        db.commit()
        LOGGER.info(
            "[ledger] delivered outbox_id=%s target=%s execution=%s persona=%s",
            row.OUTBOX_ID, row.TARGET, row.EXECUTION_ID, row.PERSONA_ID,
        )
        return True

    def _maybe_complete(self, db: Session, execution_id: str) -> None:
        """applied な実行の outbox が全 delivered なら completed へ自動遷移する。

        dead が 1 件でも残る実行は completed にしない (配送されなかった記録が
        ある以上「outbox まで全配送済み」ではない) — applied のまま観測面に残る。
        """
        entry = (
            db.query(ExecutionLedgerEntry)
            .filter(ExecutionLedgerEntry.EXECUTION_ID == execution_id)
            .first()
        )
        if entry is None or entry.STATUS != STATUS_APPLIED:
            return
        undelivered = (
            db.query(ExecutionOutboxItem)
            .filter(
                ExecutionOutboxItem.EXECUTION_ID == execution_id,
                ExecutionOutboxItem.STATUS != OUTBOX_DELIVERED,
            )
            .count()
        )
        if undelivered:
            return
        entry.STATUS = STATUS_COMPLETED
        entry.UPDATED_AT = _now_epoch()
        db.commit()
        LOGGER.info("[ledger] completed %s kind=%s (all outbox delivered)", execution_id, entry.KIND)

    def sweep_applied(self, *, kind_prefix: Optional[str] = None) -> List[str]:
        """applied 残留の照合掃除: outbox が全 delivered (または outbox 無し) の
        applied を completed へ進める (W3 Codex 第七陣 medium)。

        ``mark_applied`` の commit と ``mark_completed`` は別トランザクション
        なので、間の crash / 呼び出し失敗で outbox を持たない実行が applied の
        まま残りうる。applied は claim を正しくブロックする (occurrence は
        実行済み) が、非終端のまま ``list_unknown`` にも出ず観測面に隠れる —
        回復 tick / 起動時からこの照合で終端へ収束させる。dead が残る実行は
        :meth:`_maybe_complete` の規則どおり completed にしない。

        Returns:
            completed へ進めた execution_id のリスト。
        """
        db = self._session_factory()
        try:
            query = db.query(ExecutionLedgerEntry.EXECUTION_ID).filter(
                ExecutionLedgerEntry.STATUS == STATUS_APPLIED
            )
            if kind_prefix:
                query = query.filter(
                    ExecutionLedgerEntry.KIND.like(f"{kind_prefix}%")
                )
            ids = [row[0] for row in query.all()]
        finally:
            db.close()
        completed: List[str] = []
        for execution_id in ids:
            db = self._session_factory()
            try:
                self._maybe_complete(db, execution_id)
                entry = (
                    db.query(ExecutionLedgerEntry.STATUS)
                    .filter(ExecutionLedgerEntry.EXECUTION_ID == execution_id)
                    .first()
                )
                if entry is not None and entry[0] == STATUS_COMPLETED:
                    completed.append(execution_id)
            except Exception:
                LOGGER.exception(
                    "[ledger] sweep_applied failed for %s", execution_id,
                )
            finally:
                db.close()
        return completed

    # ------------------------------------------------------------------
    # 回復骨格 (intent §2.4)
    # ------------------------------------------------------------------

    def recover_stale_running(
        self,
        *,
        max_age_seconds: Optional[float] = None,
        all_running: bool = False,
        exclude_kinds: Optional[Sequence[str]] = None,
    ) -> List[str]:
        """観測が途絶えた running を unknown へ落とす (自動再実行はしない)。

        Args:
            max_age_seconds: UPDATED_AT がこの秒数より古い running を unknown 化
                (回復 tick の期限監視、intent §2.4 #3)。
            all_running: True なら running 全件を unknown 化 (起動時の世代 sweep、
                intent §2.4 #4)。台帳 v0.1 スキーマは行ごとのプロセス identity を
                持たないため、世代照合は「プロセス起動直後の running は定義上すべて
                前世代」という一括 sweep で表現する。同一 DB を共有する他 City
                プロセスの不在確認 (saiverse/runtime_marker.py) は呼び出し側の責務。
            exclude_kinds: これらの KIND を持つ running を sweep 対象から除外する。
                自前の期限・回復規則を持つ kind (例 ``slot.fire`` — 作業コマは
                固有の settle-close 回復を回復 tick 側で持つ) を汎用 unknown 化から
                守るため。除外された行は本メソッドが一切触らない。

        Returns:
            unknown 化した execution_id のリスト。
        """
        if max_age_seconds is None and not all_running:
            raise ValueError("specify max_age_seconds and/or all_running")
        now = _now_epoch()
        db = self._session_factory()
        try:
            query = db.query(ExecutionLedgerEntry).filter(
                ExecutionLedgerEntry.STATUS == STATUS_RUNNING
            )
            if exclude_kinds:
                query = query.filter(
                    ~ExecutionLedgerEntry.KIND.in_(tuple(exclude_kinds))
                )
            if not all_running:
                cutoff = now - int(max_age_seconds)
                query = query.filter(ExecutionLedgerEntry.UPDATED_AT <= cutoff)
            rows = query.all()
            recovered: List[str] = []
            for entry in rows:
                entry.STATUS = STATUS_UNKNOWN
                entry.UPDATED_AT = now
                entry.ERROR = (
                    "recovered: running at process startup (previous generation)"
                    if all_running
                    else f"recovered: running exceeded {int(max_age_seconds)}s deadline"
                )
                recovered.append(entry.EXECUTION_ID)
            if recovered:
                db.commit()
                LOGGER.warning(
                    "[ledger] recovered %d stale running execution(s) to unknown: %s",
                    len(recovered), recovered,
                )
            return recovered
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    # ------------------------------------------------------------------
    # 観測面
    # ------------------------------------------------------------------

    def get_execution(self, execution_id: str) -> Dict[str, Any]:
        """台帳 1 行を dict で返す (観測・テスト用)。無ければ ExecutionNotFoundError。"""
        db = self._session_factory()
        try:
            return _entry_to_dict(self._get_entry(db, execution_id))
        finally:
            db.close()

    def find_execution(
        self, kind: str, idempotency_key: str
    ) -> Optional[Dict[str, Any]]:
        """``(kind, idempotency_key)`` で台帳 1 行を読む (読み取り専用)。無ければ None。

        W3 Chunk A (schedule 台帳化 D6): reconciliation が「計算した次回
        occurrence のキーが既に台帳でブロックされているか (running / applied /
        completed / unknown)」を再登録前に確認するための照会口。UNIQUE 制約
        ``(kind, idempotency_key)`` により該当行は高々 1 行。
        """
        if not kind:
            raise ValueError("kind is required")
        if not idempotency_key:
            raise ValueError("idempotency_key is required")
        db = self._session_factory()
        try:
            entry = (
                db.query(ExecutionLedgerEntry)
                .filter(
                    ExecutionLedgerEntry.KIND == kind,
                    ExecutionLedgerEntry.IDEMPOTENCY_KEY == idempotency_key,
                )
                .first()
            )
            return _entry_to_dict(entry) if entry is not None else None
        finally:
            db.close()

    def list_unknown(self) -> List[Dict[str, Any]]:
        """unknown の実行一覧 (照合・裁定の観測面。intent §2.4 #5)。UPDATED_AT 昇順。"""
        db = self._session_factory()
        try:
            rows = (
                db.query(ExecutionLedgerEntry)
                .filter(ExecutionLedgerEntry.STATUS == STATUS_UNKNOWN)
                .order_by(ExecutionLedgerEntry.UPDATED_AT.asc())
                .all()
            )
            return [_entry_to_dict(r) for r in rows]
        finally:
            db.close()

    def list_prepared(self, kind_prefix: str) -> List[Dict[str, Any]]:
        """prepared の実行一覧 (回復 #2「prepared の回収」の観測面)。CREATED_AT 昇順。

        Args:
            kind_prefix: KIND の前方一致 (例 ``"judgment."``)。LIKE の
                ワイルドカード文字 (``%`` / ``_``) を含む prefix は想定しない。
        """
        if not kind_prefix:
            raise ValueError("kind_prefix is required")
        db = self._session_factory()
        try:
            rows = (
                db.query(ExecutionLedgerEntry)
                .filter(
                    ExecutionLedgerEntry.STATUS == STATUS_PREPARED,
                    ExecutionLedgerEntry.KIND.like(f"{kind_prefix}%"),
                )
                .order_by(ExecutionLedgerEntry.CREATED_AT.asc())
                .all()
            )
            return [_entry_to_dict(r) for r in rows]
        finally:
            db.close()

    def list_failed(
        self,
        kind_prefix: str,
        *,
        newer_than_seconds: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """failed の実行一覧 (W3 Codex 第八陣 — periodic の失われた retry の回収)。

        failed は副作用ゼロが保証された終端 (intent §2.1) なので、回収側が
        安全に refire を裁定できる。CREATED_AT 昇順。claim がキーを
        ``{key}#failed-...`` へ退避した行 (後継 claim が存在する) も含めて返す —
        除外判定は呼び出し側が IDEMPOTENCY_KEY の形で行う。

        Args:
            newer_than_seconds: 指定時、UPDATED_AT (mark_failed が刻む = 失敗
                時刻) がこの秒数以内の行だけを DB 側で絞る (Codex W3 第九・十陣
                — failed 履歴は再試行のたびに増えるため、無制限ロードは回復
                tick (単一 dispatch スレッド) を劣化させる。UPDATED_AT を使う
                のは既存の複合索引 (STATUS, UPDATED_AT) に乗せるためでもある —
                CREATED_AT には索引が無く、履歴総量に比例する走査になる)。
        """
        if not kind_prefix:
            raise ValueError("kind_prefix is required")
        db = self._session_factory()
        try:
            query = db.query(ExecutionLedgerEntry).filter(
                ExecutionLedgerEntry.STATUS == STATUS_FAILED,
                ExecutionLedgerEntry.KIND.like(f"{kind_prefix}%"),
            )
            if newer_than_seconds is not None:
                query = query.filter(
                    ExecutionLedgerEntry.UPDATED_AT
                    >= _now_epoch() - int(newer_than_seconds)
                )
            rows = query.order_by(ExecutionLedgerEntry.UPDATED_AT.asc()).all()
            return [_entry_to_dict(r) for r in rows]
        finally:
            db.close()

    def list_running(
        self,
        kind_prefix: str,
        *,
        older_than_seconds: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """running の実行一覧 (kind 固有の settle-close 回復の観測面)。UPDATED_AT 昇順。

        :meth:`list_prepared` の running 版。自前の期限・回復規則を持つ kind
        (例 ``slot.fire`` — 作業コマの精算失敗を settle-close で拾う) が、汎用の
        :meth:`recover_stale_running` 一括 unknown 化とは別に「まだ精算されていない
        running」を列挙するための口。

        Args:
            kind_prefix: KIND の前方一致 (例 ``"slot.fire"``)。LIKE の
                ワイルドカード文字 (``%`` / ``_``) を含む prefix は想定しない。
            older_than_seconds: 指定時は ``UPDATED_AT <= now - older_than_seconds``
                の行だけを返す (稼働中ハンドラの誤 settle を避ける deadline)。
                None なら期限で絞らず全 running を返す (起動時 = 前世代確定の
                一括 settle-close 用)。
        """
        if not kind_prefix:
            raise ValueError("kind_prefix is required")
        now = _now_epoch()
        db = self._session_factory()
        try:
            query = db.query(ExecutionLedgerEntry).filter(
                ExecutionLedgerEntry.STATUS == STATUS_RUNNING,
                ExecutionLedgerEntry.KIND.like(f"{kind_prefix}%"),
            )
            if older_than_seconds is not None:
                cutoff = now - int(older_than_seconds)
                query = query.filter(ExecutionLedgerEntry.UPDATED_AT <= cutoff)
            rows = query.order_by(ExecutionLedgerEntry.UPDATED_AT.asc()).all()
            return [_entry_to_dict(r) for r in rows]
        finally:
            db.close()

    def list_pending_personas(self) -> List[Optional[str]]:
        """pending の outbox 行を 1 件以上持つ persona_id の列 (重複なし)。

        起動時回復・定期 tick の flush 対象列挙に使う。メモリ上にロード済みの
        persona 集合ではなく DB の実態から取る — 削除済み persona 宛の pending も
        配送試行 → 再試行上限 → dead (人裁定) の正規経路に乗せるため。
        世界横断 (persona_id=NULL) の pending は None として現れる。
        """
        db = self._session_factory()
        try:
            rows = (
                db.query(ExecutionOutboxItem.PERSONA_ID)
                .filter(ExecutionOutboxItem.STATUS == OUTBOX_PENDING)
                .distinct()
                .all()
            )
            return [row[0] for row in rows]
        finally:
            db.close()

    def list_dead(self) -> List[Dict[str, Any]]:
        """dead の outbox 一覧 (人裁定待ちの観測面。intent §2.4 #6)。OUTBOX_ID 昇順。"""
        db = self._session_factory()
        try:
            rows = (
                db.query(ExecutionOutboxItem)
                .filter(ExecutionOutboxItem.STATUS == OUTBOX_DEAD)
                .order_by(ExecutionOutboxItem.OUTBOX_ID.asc())
                .all()
            )
            return [_outbox_to_dict(r) for r in rows]
        finally:
            db.close()
