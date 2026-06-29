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
- Note との連携 (NoteManager で扱う、Phase C)

NOTE: 「待ち (waiting) Track」機構は v0.31 (2026-05-09) で廃止された。
Phase 5 の時間差ツール基盤が同等機能 (Pulse 中断 → 完了通知で再開) を
提供する。詳細: docs/intent/persona_cognition/handoff_waiting_track_removal.md

詳細: docs/intent/persona_action_tracks.md
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from database.models import ActionTrack
from saiverse.persona_task_manager import PersonaTaskManager

_SHORT_REF_RE = re.compile(r"^[Tt]:(\d+)$")

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

# 候補補充 Track (autonomous_desire.md §11) の識別子 (track_metadata.role)。
DESIRE_REFILL_ROLE = "desire_refill"


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

    def __init__(
        self,
        session_factory: Callable[[], Session],
        event_scheduler: Optional[Any] = None,
        wait_response_timeout_provider: Optional[
            Callable[[Any], Optional[Tuple[int, Optional[datetime]]]]
        ] = None,
        wait_response_timeout_callback: Optional[
            Callable[[str, str], None]
        ] = None,
    ):
        self.SessionLocal = session_factory
        # Phase 4-e: wait_response timeout 等の予約 push に使う。None の場合
        # (tools 等の独立インスタンス) はタイマー機構が無効化される。
        self.event_scheduler = event_scheduler
        # 2026-05-09: post_complete_behavior='wait_response' な Track が活性化したまま
        # 長期 idle になり、メタ判断が抑止されて自律稼働が止まる症状の脱出経路
        # (docs/intent/persona_cognition/handoff_2026-05-09.md §4)。
        # provider: ``track -> (timeout_minutes, base_time) or None``
        #   - None を返したら Track はタイムアウト対象外 (= wait_response でない、
        #     ペルソナがロードされていない、等)。
        #   - base_time は最終メッセージの created_at 想定。NULL ならスケジュール側で
        #     ``datetime.now()`` にフォールバックする (= ペルソナが activate した
        #     直後の Track が即タイムアウトする事故を回避)。
        # callback: タイムアウト発火 + pause 完了後に呼ばれる。
        #   - 用途: メタ判断 Pulse の起動 (MetaLayer.on_periodic_tick) と
        #     AutonomyManager の interval 再開要求。
        # どちらも None の場合はタイマー機構が完全に無効化される (テスト容易性)。
        self.wait_response_timeout_provider = wait_response_timeout_provider
        self.wait_response_timeout_callback = wait_response_timeout_callback
        # Track 内タスク (旧 track_task) は統合 persona_task テーブルに一本化された。
        # 旧 ActionTrack.tasks_json チェックリスト API は PersonaTaskManager の
        # track_task 互換層へ委譲する (unified_task_model.md §5 step 4)。
        self._task_manager = PersonaTaskManager(session_factory)
        # alert 状態への遷移を購読する observer 群。
        # signature: (persona_id: str, track_id: str, context: dict) -> None
        # MetaLayer はここに登録される。TrackManager は観察対象を増やす責務を持たないため、
        # 「どんな種別の Track の alert に反応するか」のフィルタは observer 側で判断する。
        self._alert_observers: List[Callable[[str, str, dict], None]] = []
        # 状態遷移 (alert 以外) を購読する observer 群。
        # signature: (persona_id: str, track_id: str, pulse_id: Optional[str]) -> None
        # 用途:
        # - メタ判断ターン (line_role='meta_judgment', scope='discardable') を
        #   Track 切替起点で 'committed' に昇格する hook (Intent A v0.14
        #   「[B] 移動: 分岐ターンをそのまま残す」)
        # - PulseController が current pulse の origin_track_id と一致する状態
        #   変化を観測したら cancellation_token.cancel() を発動 (pulse_dispatch.md §6.2)
        # set_alert は alert 観察ルートが別にあるためこの hook では発火しない。
        self._status_change_observers: List[Callable[[str, str, Optional[str]], None]] = []
        # Track activate (= running 遷移) を購読する observer 群。
        # signature: (persona_id: str, track: ActionTrack, pulse_id: Optional[str],
        #             suppress_pulse: bool) -> None
        # 用途: pulse_dispatch.md §5 で定義した on_track_activated hook。
        # Handler 側で _inject_track_context や Pulse 起動を担う統一経路。
        # `_status_change_observers` とは別軸で、activate 時のみ発火する
        # (pause / complete / abort 等では発火しない)。
        # suppress_pulse=True はライフビューの停止パッケージ (activity/stop) 用:
        # Handler は Track 切替通知の注入のみ行い、Pulse 起動をスキップする
        # (docs/intent/persona_activity_view.md §6.3)。
        self._track_activated_observers: List[Callable[..., None]] = []

    # ------------------------------------------------------------------
    # Observer
    # ------------------------------------------------------------------

    def add_alert_observer(
        self, callback: Callable[[str, str, dict], None]
    ) -> None:
        """alert 状態への遷移時に呼ばれる callback を登録する。

        callback signature: (persona_id, track_id, context) -> None
        context は遷移を起こした側が任意で添えるメタ情報 (空 dict 可)。

        observer の例外は外に伝播させない (1 つの observer の障害で他の
        observer や呼び出し元の状態遷移処理を巻き込まないため)。
        """
        if callback in self._alert_observers:
            return
        self._alert_observers.append(callback)

    def remove_alert_observer(
        self, callback: Callable[[str, str, dict], None]
    ) -> None:
        """登録済みの alert observer を解除する。未登録なら何もしない。"""
        try:
            self._alert_observers.remove(callback)
        except ValueError:
            pass

    def _notify_alert(
        self, persona_id: str, track_id: str, context: Optional[dict] = None
    ) -> None:
        """alert 遷移を全 observer に通知する。各 observer の例外は握り潰さず WARN ログ。"""
        ctx = context or {}
        for cb in list(self._alert_observers):
            try:
                cb(persona_id, track_id, ctx)
            except Exception:
                logging.exception(
                    "[track] alert observer raised: cb=%r persona=%s track=%s",
                    cb, persona_id, track_id,
                )

    def add_status_change_observer(
        self, callback: Callable[[str, str, Optional[str]], None]
    ) -> None:
        """alert 以外の状態遷移 (activate/pause/complete/abort) を購読する。

        callback signature: (persona_id, track_id, pulse_id) -> None
        pulse_id は当該遷移を起こした Pulse の ID。Pulse 外で呼ばれた場合 (CLI /
        テスト) は None。observer 側は None ならスキップして良い。

        SAIVerseManager 起動時に以下を登録する:
        - `_promote_meta_judgment_in_pulse` — Intent A v0.14 [B] 移動の hook
        - `PulseController._on_track_status_change` — pulse_dispatch.md §6.2 の
          current pulse cancel 経路

        observer の例外は握り潰さず WARN ログ。
        """
        if callback in self._status_change_observers:
            return
        self._status_change_observers.append(callback)

    def remove_status_change_observer(
        self, callback: Callable[[str, str, Optional[str]], None]
    ) -> None:
        """登録済みの status_change observer を解除する。未登録なら何もしない。"""
        try:
            self._status_change_observers.remove(callback)
        except ValueError:
            pass

    def _notify_status_change(
        self, persona_id: str, track_id: str, pulse_id: Optional[str]
    ) -> None:
        """状態遷移 (alert 以外) を全 observer に通知する。"""
        for cb in list(self._status_change_observers):
            try:
                cb(persona_id, track_id, pulse_id)
            except Exception:
                logging.exception(
                    "[track] status_change observer raised: cb=%r persona=%s track=%s pulse=%s",
                    cb, persona_id, track_id, pulse_id,
                )

    def add_track_activated_observer(
        self, callback: Callable[..., None]
    ) -> None:
        """Track の activate (= running 遷移) を購読する callback を登録する。

        callback signature: (persona_id, track, pulse_id, suppress_pulse) -> None
        ``track`` は遷移後の ``ActionTrack`` (detached, 読み取り専用想定)。
        ``pulse_id`` は呼び出し元の Pulse ID (Pulse 外なら None)。
        ``suppress_pulse`` が True のとき、Handler は activate に伴う Pulse 起動を
        スキップする (Track 切替通知の注入は行う)。

        pulse_dispatch.md §5 で定義した on_track_activated hook の登録口。
        Handler 側で track_type をフィルタして自分の責務範囲を判定する
        (TrackManager は種別判定の責務を持たない)。

        observer の例外は外に伝播させない (1 つの observer の障害で他の
        observer や状態遷移処理を巻き込まないため)。
        """
        if callback in self._track_activated_observers:
            return
        self._track_activated_observers.append(callback)

    def remove_track_activated_observer(
        self, callback: Callable[..., None]
    ) -> None:
        """登録済みの track_activated observer を解除する。未登録なら何もしない。"""
        try:
            self._track_activated_observers.remove(callback)
        except ValueError:
            pass

    def _notify_track_activated(
        self,
        persona_id: str,
        track: Any,
        pulse_id: Optional[str],
        suppress_pulse: bool = False,
    ) -> None:
        """Track の activate を全 observer に通知する。各 observer の例外は握り潰さず WARN ログ。"""
        for cb in list(self._track_activated_observers):
            try:
                cb(persona_id, track, pulse_id, suppress_pulse)
            except Exception:
                logging.exception(
                    "[track] track_activated observer raised: cb=%r persona=%s track=%s",
                    cb, persona_id, getattr(track, "track_id", None),
                )

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

        # running 作成時は activate 相当の後処理を実行
        if initial_status == STATUS_RUNNING:
            for displaced_id in displaced_track_ids:
                self._cancel_wait_response_timeout(displaced_id)
            self._schedule_wait_response_timeout(track)
            for displaced_id in displaced_track_ids:
                self._notify_status_change(persona_id, displaced_id, None)
            self._notify_status_change(persona_id, track_id, None)
            self._notify_track_activated(persona_id, track, None)

        return track_id

    def ensure_desire_refill_track(self, persona_id: str) -> str:
        """ペルソナの「やりたいことを探す」永続 Track を get-or-create する。

        候補補充 Track (autonomous_desire.md §11): 候補生成の動線 + idle 即帰宅の
        歯止め。``track_metadata.role == 'desire_refill'`` で識別する singleton。
        全ペルソナ共通・永続 (complete/abort 不可)・autonomous (sub_line/軽量)。
        自律 ON 時に ensure する。既に在ればその track_id を返す。

        Returns:
            track_id (UUID 文字列)
        """
        if not persona_id:
            raise ValueError("persona_id is required")
        db = self.SessionLocal()
        try:
            rows = db.query(ActionTrack).filter_by(persona_id=persona_id).all()
            for t in rows:
                if not t.track_metadata:
                    continue
                try:
                    md = json.loads(t.track_metadata)
                except (TypeError, ValueError):
                    continue
                if isinstance(md, dict) and md.get("role") == DESIRE_REFILL_ROLE:
                    return t.track_id
        finally:
            db.close()

        metadata = json.dumps(
            {"role": DESIRE_REFILL_ROLE, "entry_line_role": "sub_line"},
            ensure_ascii=False,
        )
        return self.create(
            persona_id=persona_id,
            track_type="autonomous",
            title="やりたいことを探す",
            intent=(
                "自分の生きる目的・趣味・興味・これまでの記憶をもとに、"
                "これからやってみたいことを見つけて、desire_add スペルで候補として"
                "書き留める。やることが尽きたときに立ち寄る、自分のための探索。"
            ),
            is_persistent=True,
            metadata=metadata,
            initial_status=STATUS_UNSTARTED,
        )

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

    def get_entry_line_role(self, track_id: str) -> str:
        """Read the Track's entry_line_role from its metadata JSON.

        The entry_line_role determines which model/cache type drives the Track's
        Pulse: 'main_line' (heavyweight, used for other-talk Tracks) or
        'sub_line' (lightweight, used for autonomous work). Set at creation
        time by track_create and immutable thereafter (Intent A v0.14, Intent B
        v0.11). Defaults to 'main_line' for safety when metadata is missing or
        malformed — matches Intent A invariant 9 (other-talk is heavyweight).
        """
        try:
            track = self.get(track_id)
        except TrackNotFoundError:
            return "main_line"
        if not track.track_metadata:
            return "main_line"
        try:
            meta = json.loads(track.track_metadata)
            if isinstance(meta, dict):
                role = meta.get("entry_line_role")
                if role in ("main_line", "sub_line"):
                    return role
        except (TypeError, ValueError):
            pass
        return "main_line"

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

    def activate(
        self,
        track_id: str,
        *,
        pulse_id: Optional[str] = None,
        suppress_pulse: bool = False,
    ) -> ActionTrack:
        """Track をアクティブ化する。

        - 同一ペルソナの既存 running が居れば pending に押し出す
        - 自身が completed/aborted なら InvalidTrackStateError

        ``pulse_id`` は呼び出し元の Pulse 識別子。Pulse 完了時の deferred apply
        ルートから渡される。観察者 (status_change observer) が pulse_id ベースで
        メタ判断ターンを昇格する用途。Pulse 外 (CLI/テスト) では None で OK。

        ``suppress_pulse=True`` はサイレント activate: track_activated observer に
        フラグを伝搬し、Handler 側で activate に伴う Pulse 起動をスキップさせる。
        ライフビューの停止パッケージが「ユーザー待ちに戻す」ために使う —
        Track 切替通知の SAIMemory 注入は通常通り行われる
        (docs/intent/persona_activity_view.md §6.3)。
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
        # 押し出された Track の wait_response タイマーを解除する。activate 経由の
        # auto-pause は _set_status を通らないので、ここで明示的に解除しないと
        # 旧 running 用のタイマーが残り続けて誤発火する。
        for displaced_id in displaced_track_ids:
            self._cancel_wait_response_timeout(displaced_id)
        # 新たに running になった Track が wait_response 型なら自動 pause タイマーを予約。
        self._schedule_wait_response_timeout(track)
        # 押し出された Track にも status_change を通知 (pulse_dispatch.md §6.2:
        # 進行中 Pulse の cancel 経路。押し出された Track の Pulse は意味を失うので
        # PulseController の observer が cancel する)。
        for displaced_id in displaced_track_ids:
            self._notify_status_change(track.persona_id, displaced_id, pulse_id)
        # Notify status change observers for the activated track itself
        # (commit/close 完了後、別 session 不要なため)
        self._notify_status_change(track.persona_id, track_id, pulse_id)
        # Notify track_activated observers (pulse_dispatch.md §5)
        # Handler 側が _inject_track_context や Pulse 起動を担う統一経路。
        self._notify_track_activated(track.persona_id, track, pulse_id, suppress_pulse)
        return track

    def pause(self, track_id: str, *, pulse_id: Optional[str] = None) -> ActionTrack:
        """running -> pending。

        alert からの pause は **禁止** (Intent A v0.9 の 03_data_model.md §
        "メタレイヤーのトラック管理ツール群" で `running → pending` と定義
        されているため、alert は含めない)。alert は「即応すべき状況」を表す
        中間状態で、pause で pending に戻せてしまうと「目覚ましアラームを
        止めて寝続ける」現象が起きる: メタ判断レイヤーが alert に対して何も
        対応せず無視できてしまう。alert の解消は実際に対応 (activate /
        complete / abort) を取った時のみ許される。
        """
        result = self._set_status(
            track_id,
            new_status=STATUS_PENDING,
            allowed_from={STATUS_RUNNING},
            log_label="paused",
            pulse_id=pulse_id,
        )
        self._cancel_wait_response_timeout(track_id)
        return result

    # ------------------------------------------------------------------
    # Phase 4-e: wait_response timeout の EventScheduler 連携
    # ------------------------------------------------------------------

    def ensure_wait_response_timeout(self, persona_id: str) -> None:
        """ペルソナの現在 running な Track に wait_response 自動 pause タイマーを
        張り直す (冪等)。

        wait_response タイマーは Track が running に遷移する瞬間 (activate /
        create(initial_status=running)) にしか予約されず、EventScheduler は
        インメモリのため**再起動で失われる**。起動時に DB からロードした
        running Track にはタイマーが無い状態になる。本メソッドはそれを補う。

        対象外判定 (wait_response 以外 / ACTIVITY_STATE != Active /
        ペルソナ unloaded 等) はすべて ``wait_response_timeout_provider`` に
        委ねる (= activate 時と同じ単一ゲート)。同 key で再 schedule しても
        EventScheduler が上書きするので冪等。
        """
        running = self.get_running(persona_id)
        if running is not None:
            self._schedule_wait_response_timeout(running)

    @staticmethod
    def _wait_response_timeout_key(track_id: str) -> str:
        return f"wait_response_timeout:{track_id}"

    def _schedule_wait_response_timeout(self, track: Any) -> None:
        """running になった Track が wait_response 型なら自動 pause タイマーを予約する。

        ``wait_response_timeout_provider`` で「対象か / タイマー基準は」を SAIVerseManager
        に委ねる。本メソッドはスケジューリングのみを行う。

        基準時刻 (base_time) は最終メッセージの created_at を想定する。これにより:
        - 直近メッセージがあった Track: 直近 + N 分後に発火
        - メッセージが無い Track: base_time=None → ``now`` にフォールバック
        - **base_time が現在時刻より過去**: activate 時点が事実上の「ユーザー宛て呼びかけ」
          開始なので、``now`` を基準にしてフレッシュな N 分の猶予を与える
          (2026-05-10 修正)。これがないと、長期 idle Track をペルソナが
          自律 activate するたびに「即タイムアウト → 即 pause → メタ判断 → 再 activate」
          のタイトループが起きる (メタ判断 v2 で構造化出力が Track 操作を強制する
          ようになったことで顕在化した既存設計の欠陥)。

        基底時刻が条件未達の場合の race は、_handle_wait_response_timeout 側で
        provider を再呼び出しして idle_for を見て再評価する。
        """
        if (
            self.event_scheduler is None
            or self.wait_response_timeout_provider is None
            or track is None
        ):
            return
        try:
            result = self.wait_response_timeout_provider(track)
        except Exception:
            logging.exception(
                "[track] wait_response_timeout_provider raised for %s",
                getattr(track, "track_id", "?"),
            )
            return
        if result is None:
            return
        minutes, base_time = result
        if not minutes or minutes <= 0:
            return
        now = datetime.now()
        # base_time が None / 過去のときは activate 時刻 (now) を基準にする。
        # 過去の最終メッセージ時刻をそのまま使うと、長期 idle 状態の Track を
        # 自律 activate した瞬間に「過去 + 30分 = まだ過去」で EventScheduler が
        # 即発火してしまい、メタ判断ループに陥る (上記 docstring 参照)。
        if base_time is None or base_time < now:
            base = now
        else:
            base = base_time
        fire_at = base + timedelta(minutes=int(minutes))
        track_id = track.track_id
        persona_id = track.persona_id

        def _on_timeout(tid: str = track_id, pid: str = persona_id) -> None:
            self._handle_wait_response_timeout(tid, pid)

        self.event_scheduler.schedule(
            fire_at=fire_at,
            callback=_on_timeout,
            key=self._wait_response_timeout_key(track_id),
        )
        logging.info(
            "[track] scheduled wait_response timeout: track=%s persona=%s "
            "base=%s timeout_min=%s fire_at=%s",
            track_id, persona_id,
            base.isoformat(timespec="seconds"),
            minutes,
            fire_at.isoformat(timespec="seconds"),
        )

    def _cancel_wait_response_timeout(self, track_id: str) -> None:
        """wait_response タイマーをキャンセル (pause/abort/complete/alert/活性置換 等で呼ぶ)。"""
        if self.event_scheduler is None:
            return
        self.event_scheduler.cancel(self._wait_response_timeout_key(track_id))

    def _handle_wait_response_timeout(self, track_id: str, persona_id: str) -> None:
        """EventScheduler から呼ばれる wait_response timeout callback。

        race 回避のため発火時にもう一度 provider を呼んで状態を再評価する:
        - Track が既に running でなければ: 何もしない (ユーザー発話等で抜けた)
        - provider が None を返す (= もう wait_response 対象でない): 何もしない
        - 最終メッセージから N 分未満しか経過していない: 再スケジュール
        - 上記をクリアしたら: pause → callback (メタ判断発火 / autonomy 再開)
        """
        try:
            current = self.get(track_id)
        except TrackNotFoundError:
            logging.debug(
                "[track] wait_response timeout: track %s not found (deleted?)",
                track_id,
            )
            return

        if current.status != STATUS_RUNNING:
            logging.debug(
                "[track] wait_response timeout fired but status=%s (no longer running): track=%s",
                current.status, track_id,
            )
            return

        if self.wait_response_timeout_provider is None:
            return
        try:
            result = self.wait_response_timeout_provider(current)
        except Exception:
            logging.exception(
                "[track] wait_response_timeout_provider raised on fire for %s",
                track_id,
            )
            return
        if result is None:
            # ペルソナ unloaded 等で対象外になった
            logging.debug(
                "[track] wait_response timeout fired but provider returned None: track=%s",
                track_id,
            )
            return
        minutes, base_time = result
        if not minutes or minutes <= 0:
            return

        now = datetime.now()
        if base_time is not None:
            idle_for = now - base_time
            if idle_for < timedelta(minutes=int(minutes)):
                # まだ閾値に達していない → 残り時間で再予約
                remaining = timedelta(minutes=int(minutes)) - idle_for
                fire_at = now + remaining
                track_id_local = track_id
                persona_id_local = persona_id

                def _on_retry(tid: str = track_id_local, pid: str = persona_id_local) -> None:
                    self._handle_wait_response_timeout(tid, pid)

                self.event_scheduler.schedule(
                    fire_at=fire_at,
                    callback=_on_retry,
                    key=self._wait_response_timeout_key(track_id),
                )
                logging.debug(
                    "[track] wait_response timeout re-scheduled (idle=%ss < threshold=%dmin): track=%s",
                    int(idle_for.total_seconds()), minutes, track_id,
                )
                return

        # 条件成立 → pause + callback
        logging.info(
            "[track] wait_response timeout reached: track=%s persona=%s timeout_min=%s",
            track_id, persona_id, minutes,
        )
        try:
            self.pause(track_id)
        except (InvalidTrackStateError, TrackNotFoundError) as exc:
            logging.warning(
                "[track] wait_response timeout pause failed: track=%s err=%s",
                track_id, exc,
            )
            return

        if self.wait_response_timeout_callback is not None:
            try:
                self.wait_response_timeout_callback(persona_id, track_id)
            except Exception:
                logging.exception(
                    "[track] wait_response_timeout_callback raised: persona=%s track=%s",
                    persona_id, track_id,
                )

    def complete(self, track_id: str, *, pulse_id: Optional[str] = None) -> ActionTrack:
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
        self._cancel_wait_response_timeout(track_id)
        self._notify_status_change(track.persona_id, track_id, pulse_id)
        return track

    def abort(self, track_id: str, *, pulse_id: Optional[str] = None) -> ActionTrack:
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
        self._cancel_wait_response_timeout(track_id)
        self._notify_status_change(track.persona_id, track_id, pulse_id)
        return track

    def set_alert(
        self, track_id: str, context: Optional[dict] = None
    ) -> ActionTrack:
        """Track を alert 状態にする。

        他者から「すぐ確認してほしい」を伝えるための遷移。
        既に running のものを alert にしても意味が薄いため、running は遷移しない。
        completed/aborted からは不可。

        Observer 通知の挙動 (Phase 2.6, 2026-05-01):

        - **状態遷移あり** (pending/unstarted → alert): 通常の alert として
          observer に通知。context は呼び出し元が渡したものをそのまま転送。
        - **既 running** (state は no-op): observer に **`target_already_running=True`**
          を付けて通知する。これがないとペルソナの自律先制と外部 alert イベント
          が衝突した時に、メタ判断者が起動理由を認識できない (intent docs
          §"自律先制と外部 alert のレース"参照)。
        - **既 alert** (state は no-op): 重複通知を避けるため observer 通知しない。

        Args:
            track_id: 対象 Track ID。
            context: observer に渡す任意のメタ情報 (発火源の種別、トリガとなった
                発話内容のメッセージ ID 等を入れる想定)。
        """
        notify = False
        notify_already_running = False  # Phase 2.6: 自律先制と外部 alert の衝突
        persona_id_for_notify: Optional[str] = None
        track_title_for_notify: Optional[str] = None
        track_type_for_notify: Optional[str] = None
        db = self.SessionLocal()
        try:
            track = self._fetch_or_raise(db, track_id)
            if track.status in TERMINAL_STATUSES:
                raise InvalidTrackStateError(
                    f"cannot alert terminal track: {track_id}"
                )
            if track.status == STATUS_RUNNING:
                # 既にアクティブ (no-op だが observer には通知する)。
                # ペルソナが直前に自律判断で running 化していたケース等で、
                # ユーザー発話のような外部イベントが「埋もれない」よう観察者へ届ける。
                logging.debug("[track] set_alert no-op (running) %s", track_id)
                persona_id_for_notify = track.persona_id
                track_title_for_notify = track.title
                track_type_for_notify = track.track_type
                notify_already_running = True
                db.expunge(track)
                return track
            if track.status == STATUS_ALERT:
                # 既に alert (重複通知を避けるため observer 通知しない)
                logging.debug("[track] set_alert no-op (already alert) %s", track_id)
                db.expunge(track)
                return track
            track.status = STATUS_ALERT
            persona_id_for_notify = track.persona_id
            track_title_for_notify = track.title
            track_type_for_notify = track.track_type
            notify = True
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
            if (notify or notify_already_running) and persona_id_for_notify is not None:
                # observer 通知は DB トランザクション完了後に行う
                # (observer 内で別 DB アクセスがあっても整合する)
                # Phase 2.6: context を拡張 (target_already_running フラグ + Track 識別情報)
                enriched_context = dict(context or {})
                if notify_already_running:
                    enriched_context["target_already_running"] = True
                if track_title_for_notify:
                    enriched_context.setdefault("target_track_title", track_title_for_notify)
                if track_type_for_notify:
                    enriched_context.setdefault("target_track_type", track_type_for_notify)
                self._notify_alert(persona_id_for_notify, track_id, enriched_context)

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

        Track パラメータは連続値 (0.0〜1.0 推奨) で、メタレイヤー判断時に
        プロンプトに含められる + 内部 alert ポーラの閾値判定に使われる。
        intent B v0.7 §"Track パラメータ機構".
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
        """短縮参照 (t:N) または UUID を track_id に解決する。

        - ``t:1`` / ``T:1`` → persona_id のTrack で short_id=1 の track_id を返す
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
                        f"track not found: t:{short_id} (persona={persona_id})"
                    )
                return track.track_id
            finally:
                db.close()

        ref_stripped = ref.strip()
        if len(ref_stripped) == 36 and ref_stripped.count("-") == 4:
            return ref_stripped

        raise TrackNotFoundError(
            f"invalid track reference: {ref!r} "
            f"(expected 't:N' or UUID format)"
        )

    # ------------------------------------------------------------------
    # Track タスクリスト
    # ------------------------------------------------------------------

    def get_tasks(self, track_id: str) -> List[Dict[str, Any]]:
        """Track のタスクリストを ``[{id, title, done}]`` で返す。

        統合 persona_task テーブル (track_id バインド) から取得する。旧 tasks_json
        互換の ``{title, done}`` に加え ``id`` を含む (呼び出し側は title/done のみ参照)。
        """
        return self._task_manager.get_track_tasks(track_id)

    def add_task(self, track_id: str, title: str) -> List[Dict[str, Any]]:
        """Track にタスクを追加して更新後のリストを返す。

        persona_id は Track 行から導出する (統合テーブルは persona スコープ)。
        """
        persona_id = self._task_persona_id(track_id)
        return self._task_manager.add_track_task(track_id, title, persona_id=persona_id)

    def complete_task(self, track_id: str, index: int) -> List[Dict[str, Any]]:
        """Track のタスクを完了にして更新後のリストを返す。"""
        persona_id = self._task_persona_id(track_id)
        return self._task_manager.complete_track_task(track_id, index, persona_id=persona_id)

    def format_task_list(self, track_id: str) -> str:
        """Track のタスクリストを Markdown チェックリスト形式で返す。"""
        return self._task_manager.format_track_task_list(track_id)

    def _task_persona_id(self, track_id: str) -> str:
        """Track の persona_id を引く (タスク書き込みに必要)。存在しなければ raise。"""
        db = self.SessionLocal()
        try:
            track = self._fetch_or_raise(db, track_id)
            return track.persona_id
        finally:
            db.close()

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
        *,
        pulse_id: Optional[str] = None,
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
        self._notify_status_change(track.persona_id, track_id, pulse_id)
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
