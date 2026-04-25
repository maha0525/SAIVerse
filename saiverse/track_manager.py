"""TrackManager: 行動 Track のライフサイクル管理。

Intent A v0.9 / Intent B v0.6 に準拠した、ペルソナの「行動 Track」の
CRUD + 状態遷移を扱う純粋ロジックレイヤー。

責務:
- Track の作成 / 取得 / 一覧
- 状態遷移メソッド (activate, pause, wait, resume_from_wait, complete, abort)
- 忘却フラグ (forget, recall)
- 不変条件の維持: 同時 running は 1 本、永続 Track の complete/abort 拒否

責務外:
- Track 作成の自動トリガー (ペルソナ作成 hook 等は別レイヤー)
- メタレイヤーの判断ロジック (AutonomyManager / 後継のメインライン)
- LLM ツールへの登録 (tools/ 配下で別途行う)
- Note との連携 (NoteManager で扱う、Phase C)

詳細: docs/intent/persona_action_tracks.md
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, List, Optional

from sqlalchemy.orm import Session

from database.models import ActionTrack

# --- 状態定数 ---
STATUS_RUNNING = "running"
STATUS_ALERT = "alert"
STATUS_PENDING = "pending"
STATUS_WAITING = "waiting"
STATUS_UNSTARTED = "unstarted"
STATUS_COMPLETED = "completed"
STATUS_ABORTED = "aborted"

ALL_STATUSES = frozenset({
    STATUS_RUNNING, STATUS_ALERT, STATUS_PENDING, STATUS_WAITING,
    STATUS_UNSTARTED, STATUS_COMPLETED, STATUS_ABORTED,
})
TERMINAL_STATUSES = frozenset({STATUS_COMPLETED, STATUS_ABORTED})
LIVE_STATUSES = ALL_STATUSES - TERMINAL_STATUSES
ACTIVATABLE_STATUSES = frozenset({
    STATUS_UNSTARTED, STATUS_PENDING, STATUS_WAITING, STATUS_ALERT,
})

# --- resume_from_wait モード ---
RESUME_MODE_ACTIVATE = "activate"
RESUME_MODE_PAUSE = "pause"
RESUME_MODE_ABORT = "abort"
RESUME_MODES = frozenset({RESUME_MODE_ACTIVATE, RESUME_MODE_PAUSE, RESUME_MODE_ABORT})


class TrackError(Exception):
    """Base error for track manager."""


class TrackNotFoundError(TrackError):
    """Raised when track_id is not found."""


class InvalidTrackStateError(TrackError):
    """Raised when an operation is attempted from an incompatible status."""


class PersistentTrackError(TrackError):
    """Raised when complete/abort is attempted on a persistent track."""


class TrackManager:
    """ActionTrack の永続化と状態遷移を担う。

    全メソッドは 1 トランザクション内で完結する (内部で SessionLocal を開閉する)。
    呼び出し側はセッション管理を意識しない。

    並列性: SQLite の WAL モードに依存。同一 persona に対する activate の
    競合は最終的に「running は 1 本」が保たれる前提で動作する。厳密な
    分離が必要になった場合は呼び出し側でロックを追加する。
    """

    def __init__(self, session_factory: Callable[[], Session]):
        self.SessionLocal = session_factory

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create(
        self,
        persona_id: str,
        track_type: str,
        title: Optional[str] = None,
        intent: Optional[str] = None,
        output_target: str = "none",
        is_persistent: bool = False,
        metadata: Optional[str] = None,
    ) -> str:
        """新規 Track を作成する。初期状態は unstarted。

        Returns:
            track_id (UUID 文字列)
        """
        if not persona_id:
            raise ValueError("persona_id is required")
        if not track_type:
            raise ValueError("track_type is required")

        track_id = str(uuid.uuid4())
        db = self.SessionLocal()
        try:
            track = ActionTrack(
                track_id=track_id,
                persona_id=persona_id,
                title=title,
                track_type=track_type,
                is_persistent=bool(is_persistent),
                output_target=output_target,
                status=STATUS_UNSTARTED,
                is_forgotten=False,
                intent=intent,
                track_metadata=metadata,
            )
            db.add(track)
            db.commit()
            logging.info(
                "[track] created %s persona=%s type=%s persistent=%s",
                track_id, persona_id, track_type, is_persistent,
            )
            return track_id
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def get(self, track_id: str) -> ActionTrack:
        """Track を取得。存在しなければ TrackNotFoundError。"""
        db = self.SessionLocal()
        try:
            track = db.query(ActionTrack).filter_by(track_id=track_id).first()
            if track is None:
                raise TrackNotFoundError(f"track not found: {track_id}")
            db.expunge(track)  # detach so caller can read after session close
            return track
        finally:
            db.close()

    def list_for_persona(
        self,
        persona_id: str,
        statuses: Optional[Iterable[str]] = None,
        include_forgotten: bool = False,
    ) -> List[ActionTrack]:
        """ペルソナの Track 一覧を返す。"""
        db = self.SessionLocal()
        try:
            query = db.query(ActionTrack).filter_by(persona_id=persona_id)
            if statuses is not None:
                query = query.filter(ActionTrack.status.in_(list(statuses)))
            if not include_forgotten:
                query = query.filter_by(is_forgotten=False)
            tracks = query.order_by(ActionTrack.last_active_at.desc().nullslast()).all()
            for t in tracks:
                db.expunge(t)
            return tracks
        finally:
            db.close()

    def get_running(self, persona_id: str) -> Optional[ActionTrack]:
        """ペルソナの現在の running Track（あれば）。"""
        db = self.SessionLocal()
        try:
            track = (
                db.query(ActionTrack)
                .filter_by(persona_id=persona_id, status=STATUS_RUNNING)
                .first()
            )
            if track is not None:
                db.expunge(track)
            return track
        finally:
            db.close()

    # ------------------------------------------------------------------
    # 状態遷移
    # ------------------------------------------------------------------

    def activate(self, track_id: str) -> ActionTrack:
        """Track をアクティブ化する。

        - 同一ペルソナの既存 running が居れば pending に押し出す
        - 自身が completed/aborted なら InvalidTrackStateError
        """
        db = self.SessionLocal()
        try:
            track = self._fetch_or_raise(db, track_id)
            if track.status in TERMINAL_STATUSES:
                raise InvalidTrackStateError(
                    f"cannot activate terminal track ({track.status}): {track_id}"
                )

            # 既存 running を pending に押し出す (自身を除く)
            running_q = (
                db.query(ActionTrack)
                .filter(
                    ActionTrack.persona_id == track.persona_id,
                    ActionTrack.status == STATUS_RUNNING,
                    ActionTrack.track_id != track_id,
                )
            )
            for existing in running_q.all():
                existing.status = STATUS_PENDING
                logging.info(
                    "[track] auto-pause %s (was running) for activation of %s",
                    existing.track_id, track_id,
                )

            track.status = STATUS_RUNNING
            track.last_active_at = datetime.now()
            db.commit()
            db.refresh(track)
            db.expunge(track)
            logging.info("[track] activated %s persona=%s", track_id, track.persona_id)
            return track
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def pause(self, track_id: str) -> ActionTrack:
        """running -> pending。"""
        return self._set_status(
            track_id,
            new_status=STATUS_PENDING,
            allowed_from={STATUS_RUNNING, STATUS_ALERT},
            log_label="paused",
        )

    def wait(
        self,
        track_id: str,
        waiting_for: str,
        timeout_seconds: Optional[int] = None,
    ) -> ActionTrack:
        """running -> waiting。

        Args:
            waiting_for: JSON 文字列 (Intent B v0.6 規約に従う構造化文字列)
            timeout_seconds: None なら無期限
        """
        if not waiting_for:
            raise ValueError("waiting_for is required")
        db = self.SessionLocal()
        try:
            track = self._fetch_or_raise(db, track_id)
            if track.status not in {STATUS_RUNNING, STATUS_ALERT}:
                raise InvalidTrackStateError(
                    f"cannot wait from status {track.status}: {track_id}"
                )
            track.status = STATUS_WAITING
            track.waiting_for = waiting_for
            track.waiting_timeout_at = (
                datetime.now() + timedelta(seconds=timeout_seconds)
                if timeout_seconds is not None
                else None
            )
            db.commit()
            db.refresh(track)
            db.expunge(track)
            logging.info(
                "[track] waiting %s timeout=%s",
                track_id, track.waiting_timeout_at,
            )
            return track
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def resume_from_wait(self, track_id: str, mode: str) -> ActionTrack:
        """waiting からの取り下げ。

        mode = 'activate' / 'pause' / 'abort'
        """
        if mode not in RESUME_MODES:
            raise ValueError(f"invalid resume mode: {mode}")

        if mode == RESUME_MODE_ACTIVATE:
            # 先に waiting → unstarted-like な許可状態に直してから activate を呼ぶ
            # （activate は ACTIVATABLE_STATUSES に waiting を含めているのでそのままで OK）
            current = self.get(track_id)
            if current.status != STATUS_WAITING:
                raise InvalidTrackStateError(
                    f"resume_from_wait requires waiting status, got {current.status}"
                )
            return self.activate(track_id)

        if mode == RESUME_MODE_PAUSE:
            return self._set_status(
                track_id,
                new_status=STATUS_PENDING,
                allowed_from={STATUS_WAITING},
                log_label="resume_from_wait->pending",
                clear_waiting_fields=True,
            )

        # RESUME_MODE_ABORT
        db = self.SessionLocal()
        try:
            track = self._fetch_or_raise(db, track_id)
            if track.status != STATUS_WAITING:
                raise InvalidTrackStateError(
                    f"resume_from_wait requires waiting status, got {track.status}"
                )
            if track.is_persistent:
                raise PersistentTrackError(
                    f"cannot abort persistent track: {track_id}"
                )
            track.status = STATUS_ABORTED
            track.aborted_at = datetime.now()
            track.waiting_for = None
            track.waiting_timeout_at = None
            db.commit()
            db.refresh(track)
            db.expunge(track)
            logging.info("[track] aborted-from-wait %s", track_id)
            return track
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def complete(self, track_id: str) -> ActionTrack:
        """running -> completed。永続 Track は不可。"""
        db = self.SessionLocal()
        try:
            track = self._fetch_or_raise(db, track_id)
            if track.is_persistent:
                raise PersistentTrackError(
                    f"cannot complete persistent track: {track_id}"
                )
            if track.status != STATUS_RUNNING:
                raise InvalidTrackStateError(
                    f"cannot complete from status {track.status}: {track_id}"
                )
            track.status = STATUS_COMPLETED
            track.completed_at = datetime.now()
            db.commit()
            db.refresh(track)
            db.expunge(track)
            logging.info("[track] completed %s", track_id)
            return track
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def abort(self, track_id: str) -> ActionTrack:
        """任意の非終了状態 -> aborted。永続 Track は不可。"""
        db = self.SessionLocal()
        try:
            track = self._fetch_or_raise(db, track_id)
            if track.is_persistent:
                raise PersistentTrackError(
                    f"cannot abort persistent track: {track_id}"
                )
            if track.status in TERMINAL_STATUSES:
                raise InvalidTrackStateError(
                    f"cannot abort already-terminal track: {track_id}"
                )
            track.status = STATUS_ABORTED
            track.aborted_at = datetime.now()
            track.waiting_for = None
            track.waiting_timeout_at = None
            db.commit()
            db.refresh(track)
            db.expunge(track)
            logging.info("[track] aborted %s", track_id)
            return track
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def set_alert(self, track_id: str) -> ActionTrack:
        """Track を alert 状態にする。

        他者から「すぐ確認してほしい」を伝えるための遷移。
        既に running のものを alert にしても意味が薄いため、running は遷移しない。
        completed/aborted からは不可。
        """
        db = self.SessionLocal()
        try:
            track = self._fetch_or_raise(db, track_id)
            if track.status in TERMINAL_STATUSES:
                raise InvalidTrackStateError(
                    f"cannot alert terminal track: {track_id}"
                )
            if track.status == STATUS_RUNNING:
                # 既にアクティブなのでそのまま (no-op)
                logging.debug("[track] set_alert no-op (running) %s", track_id)
                db.expunge(track)
                return track
            track.status = STATUS_ALERT
            db.commit()
            db.refresh(track)
            db.expunge(track)
            logging.info("[track] alert %s persona=%s", track_id, track.persona_id)
            return track
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    # ------------------------------------------------------------------
    # 忘却
    # ------------------------------------------------------------------

    def forget(self, track_id: str) -> ActionTrack:
        """忘却フラグ ON。状態は変えない。"""
        return self._set_forgotten(track_id, True)

    def recall(self, track_id: str) -> ActionTrack:
        """忘却フラグ OFF。"""
        return self._set_forgotten(track_id, False)

    # ------------------------------------------------------------------
    # 内部ヘルパ
    # ------------------------------------------------------------------

    def _fetch_or_raise(self, db: Session, track_id: str) -> ActionTrack:
        track = db.query(ActionTrack).filter_by(track_id=track_id).first()
        if track is None:
            raise TrackNotFoundError(f"track not found: {track_id}")
        return track

    def _set_status(
        self,
        track_id: str,
        new_status: str,
        allowed_from: Iterable[str],
        log_label: str,
        clear_waiting_fields: bool = False,
    ) -> ActionTrack:
        allowed_set = set(allowed_from)
        db = self.SessionLocal()
        try:
            track = self._fetch_or_raise(db, track_id)
            if track.status not in allowed_set:
                raise InvalidTrackStateError(
                    f"cannot {log_label} from status {track.status}: {track_id}"
                )
            track.status = new_status
            if clear_waiting_fields:
                track.waiting_for = None
                track.waiting_timeout_at = None
            db.commit()
            db.refresh(track)
            db.expunge(track)
            logging.info("[track] %s %s", log_label, track_id)
            return track
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _set_forgotten(self, track_id: str, value: bool) -> ActionTrack:
        db = self.SessionLocal()
        try:
            track = self._fetch_or_raise(db, track_id)
            track.is_forgotten = value
            db.commit()
            db.refresh(track)
            db.expunge(track)
            logging.info(
                "[track] forgotten=%s for %s", value, track_id,
            )
            return track
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
