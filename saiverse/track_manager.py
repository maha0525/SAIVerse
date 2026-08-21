"""TrackManager: 行動 Track のライフサイクル管理。

Intent A v0.9 / Intent B v0.6 に準拠した、ペルソナの「行動 Track」の
CRUD + 状態遷移を扱う純粋ロジックレイヤー。

責務:
- Track の作成 / 取得 / 一覧
- 状態遷移メソッド (activate, pause, complete, abort)
- 忘却フラグ (forget, recall)
- 不変条件の維持: 同時 running は 1 本、永続 Track の complete/abort 拒否

責務外:
- Track 作成の自動トリガー (ペルソナ作成 hook 等は別レイヤー)
- メタレイヤーの判断ロジック (AutonomyManager / 後継のメインライン)
- LLM ツールへの登録 (tools/ 配下で別途行う)
- Note (旧概念。P3c① でテーマノードページへ物理統合済み) との連携

NOTE: 「待ち (waiting) Track」機構は v0.31 (2026-05-09) で廃止された。
Phase 5 の時間差ツール基盤が同等機能 (Pulse 中断 → 完了通知で再開) を
提供する。詳細: docs/intent/persona_cognition/handoff_waiting_track_removal.md

NOTE (2026-08-21, track_retirement.md §2 住人 2): **会話は Track を経由しない**。
ユーザーとの会話の器 (出来事の open / main_line 起動 / 沈黙タイマー) は
``saiverse/user_conversation.py`` へ移り、それに伴い本クラスから wait_response
タイマー機構・状態遷移 observer・``on_track_activated`` hook・
``get_entry_line_role`` が撤去された。残る読み手は時間割の track:N コマ
(day_plan)・想起の歩き (recall_walk)・経験の台帳・一部 API で、テーブルごとの
退役は撤去順序④以降。

詳細: docs/intent/persona_action_tracks.md
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime
from typing import Callable, Iterable, List, Optional

from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from database.models import ActionTrack

_SHORT_REF_RE = re.compile(r"^track:(\d+)$", re.IGNORECASE)

# --- 状態定数 ---
STATUS_RUNNING = "running"
STATUS_ALERT = "alert"
STATUS_PENDING = "pending"
STATUS_UNSTARTED = "unstarted"
STATUS_COMPLETED = "completed"
STATUS_ABORTED = "aborted"

ALL_STATUSES = frozenset({
    STATUS_RUNNING, STATUS_ALERT, STATUS_PENDING,
    STATUS_UNSTARTED, STATUS_COMPLETED, STATUS_ABORTED,
})
TERMINAL_STATUSES = frozenset({STATUS_COMPLETED, STATUS_ABORTED})
LIVE_STATUSES = ALL_STATUSES - TERMINAL_STATUSES
ACTIVATABLE_STATUSES = frozenset({
    STATUS_UNSTARTED, STATUS_PENDING, STATUS_ALERT,
})


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
        # NOTE: 旧 alert observer 機構 (set_alert + _alert_observers) は撤去済み
        # (track_retirement.md §7.4)。最後の発火元だったユーザー発話の仲裁は
        # on_event 判断点への直結 (autonomy_wiring.handle_user_utterance_conflict)
        # が後継。STATUS_ALERT 定数は既存 DB 行の互換のため残る (書き手なし)。
        # NOTE (2026-08-21): 状態遷移 observer (_status_change_observers) と
        # activate observer (on_track_activated hook) も購読者ごと撤去した。
        # 前者の購読者はメタ判断ターンの scope 昇格と PulseController の Pulse
        # cancel で、どちらも発火元 (v1 メタ判断の Track 操作) が既に退役していた。
        # 後者は会話の器そのもので、saiverse/user_conversation.py が直接呼ぶ形に
        # 置き換わった。

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
        initial_status: str = STATUS_UNSTARTED,
    ) -> str:
        """新規 Track を作成する。

        ``initial_status`` で初期状態を指定できる。``STATUS_RUNNING`` を
        渡すと、同一トランザクション内で既存 running Track の displacement
        (→ pending 押し出し) も行い、作成と同時に running 状態で確定する。
        これにより create → activate の 2 段階で生じていた UNSTARTED 窓を
        排除し、メタ判断の誤発火を防ぐ。

        Returns:
            track_id (UUID 文字列)
        """
        if not persona_id:
            raise ValueError("persona_id is required")
        if not track_type:
            raise ValueError("track_type is required")
        if initial_status not in (STATUS_UNSTARTED, STATUS_RUNNING):
            raise ValueError(
                f"initial_status must be unstarted or running, got: {initial_status}"
            )

        track_id = str(uuid.uuid4())
        displaced_track_ids: List[str] = []
        db = self.SessionLocal()
        try:
            short_id = self._next_short_id(db, persona_id)

            # initial_status=running の場合、既存 running を pending に押し出す
            if initial_status == STATUS_RUNNING:
                running_q = (
                    db.query(ActionTrack)
                    .filter(
                        ActionTrack.persona_id == persona_id,
                        ActionTrack.status == STATUS_RUNNING,
                    )
                )
                for existing in running_q.all():
                    existing.status = STATUS_PENDING
                    displaced_track_ids.append(existing.track_id)
                    logging.info(
                        "[track] auto-pause %s (was running) for creation of %s",
                        existing.track_id, track_id,
                    )

            track = ActionTrack(
                track_id=track_id,
                persona_id=persona_id,
                short_id=short_id,
                title=title,
                track_type=track_type,
                is_persistent=bool(is_persistent),
                output_target=output_target,
                status=initial_status,
                is_forgotten=False,
                intent=intent,
                track_metadata=metadata,
                last_active_at=datetime.now() if initial_status == STATUS_RUNNING else None,
            )
            db.add(track)
            db.commit()
            db.refresh(track)
            db.expunge(track)
            logging.info(
                "[track] created %s (t:%d) persona=%s type=%s persistent=%s status=%s",
                track_id, short_id, persona_id, track_type, is_persistent, initial_status,
            )
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        return track_id

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

    def set_title(self, track_id: str, title: str) -> None:
        """Track のタイトルを貼り替える。存在しなければ何もしない。"""
        db = self.SessionLocal()
        try:
            track = db.query(ActionTrack).filter_by(track_id=track_id).first()
            if track is None:
                return
            track.title = title
            db.commit()
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
            displaced_track_ids: List[str] = []
            for existing in running_q.all():
                existing.status = STATUS_PENDING
                displaced_track_ids.append(existing.track_id)
                logging.info(
                    "[track] auto-pause %s (was running) for activation of %s",
                    existing.track_id, track_id,
                )

            track.status = STATUS_RUNNING
            track.last_active_at = datetime.now()

            # NOTE (v0.32, 2026-05-09): cache_built_at リセット処理は削除。
            # これは dead だった prepare_pulse_root_context の first_pulse 判定用
            # メタデータで、Track Chronicle 機構移行に伴い不要になった。
            # 詳細: docs/intent/persona_cognition/track_chronicle.md §8

            db.commit()
            db.refresh(track)
            db.expunge(track)
            logging.info("[track] activated %s persona=%s", track_id, track.persona_id)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        return track

    def pause(self, track_id: str) -> ActionTrack:
        """running -> pending。

        alert からの pause は **禁止** (Intent A v0.9 の 03_data_model.md §
        "メタレイヤーのトラック管理ツール群" で `running → pending` と定義
        されているため、alert は含めない)。alert は「即応すべき状況」を表す
        中間状態で、pause で pending に戻せてしまうと「目覚ましアラームを
        止めて寝続ける」現象が起きる: メタ判断レイヤーが alert に対して何も
        対応せず無視できてしまう。alert の解消は実際に対応 (activate /
        complete / abort) を取った時のみ許される。
        """
        return self._set_status(
            track_id,
            new_status=STATUS_PENDING,
            allowed_from={STATUS_RUNNING},
            log_label="paused",
        )

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
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        return track

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
            db.commit()
            db.refresh(track)
            db.expunge(track)
            logging.info("[track] aborted %s", track_id)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        return track

    # NOTE: 旧 set_alert (alert 状態への遷移 + observer 通知) は撤去済み
    # (track_retirement.md §7.4)。STATUS_ALERT の行はもう新規に生まれない。

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
    # Phase C-2: Track パラメータ機構 (intent B v0.7 §"Track パラメータ機構の実装方針")
    # ------------------------------------------------------------------

    def set_parameter(
        self, track_id: str, parameter_name: str, value: float
    ) -> ActionTrack:
        """``action_tracks.metadata.parameters[name] = value`` を更新する。

        Track パラメータは連続値 (0.0〜1.0 推奨)。intent B v0.7 §"Track
        パラメータ機構"。旧読み手 (v1 メタ判断プロンプト・内部 alert ポーラ) は
        いずれも退役済みで、現在は保存のみ。
        """
        if not parameter_name:
            raise ValueError("parameter_name is required")
        try:
            float_value = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"value must be numeric, got {value!r}") from exc

        db = self.SessionLocal()
        try:
            track = self._fetch_or_raise(db, track_id)
            try:
                metadata = json.loads(track.track_metadata) if track.track_metadata else {}
            except (TypeError, ValueError):
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            params = metadata.get("parameters")
            if not isinstance(params, dict):
                params = {}
            params[parameter_name] = float_value
            metadata["parameters"] = params
            track.track_metadata = json.dumps(metadata, ensure_ascii=False)
            db.commit()
            db.refresh(track)
            db.expunge(track)
            logging.info(
                "[track] parameter set: %s.parameters[%s]=%s",
                track_id, parameter_name, float_value,
            )
            return track
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    # ------------------------------------------------------------------
    # 内部ヘルパ
    # ------------------------------------------------------------------

    @staticmethod
    def _next_short_id(db: Session, persona_id: str) -> int:
        """ペルソナ内で次に使う short_id を返す (MAX + 1, 初回は 1)。"""
        current_max = (
            db.query(sa_func.max(ActionTrack.short_id))
            .filter(ActionTrack.persona_id == persona_id)
            .scalar()
        )
        return (current_max or 0) + 1

    def resolve_track_ref(self, persona_id: str, ref: str) -> str:
        """短縮参照 (track:N) または UUID を track_id に解決する。

        - ``track:1`` → persona_id のTrack で short_id=1 の track_id を返す
        - UUID 形式 (36文字, 4ハイフン) → そのまま返す
        - それ以外 → TrackNotFoundError

        Returns:
            解決された track_id (UUID 文字列)
        """
        if not ref:
            raise TrackNotFoundError("empty track reference")

        m = _SHORT_REF_RE.match(ref.strip())
        if m:
            short_id = int(m.group(1))
            db = self.SessionLocal()
            try:
                track = (
                    db.query(ActionTrack)
                    .filter_by(persona_id=persona_id, short_id=short_id)
                    .first()
                )
                if track is None:
                    raise TrackNotFoundError(
                        f"track not found: track:{short_id} (persona={persona_id})"
                    )
                return track.track_id
            finally:
                db.close()

        ref_stripped = ref.strip()
        if len(ref_stripped) == 36 and ref_stripped.count("-") == 4:
            return ref_stripped

        raise TrackNotFoundError(
            f"invalid track reference: {ref!r} "
            f"(expected 'track:N' or UUID format)"
        )

    # NOTE: 旧 Track タスクリスト API (get_tasks / add_task / complete_task /
    # format_task_list) は 2026-08-21 に撤去した。委譲先の
    # ``PersonaTaskManager`` の track_task 互換層 (get_track_tasks ほか) は
    # 束 6 第二便で既に退役しており、**呼べば AttributeError になる委譲**が
    # 呼び手ゼロのまま残っていた (track_retirement.md §7.2 ④群の取りこぼし)。

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
            db.commit()
            db.refresh(track)
            db.expunge(track)
            logging.info("[track] %s %s", log_label, track_id)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        return track

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
