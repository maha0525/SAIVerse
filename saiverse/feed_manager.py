"""Feed Manager — RSS/Atom フィード購読の定期取得・保存・ペルソナ配送を管理する。

docs/intent/rss_feed_intake.md の配置層 + 提示層。

- 購読 (feed_subscription) は Building 内の Fixture (TYPE="feed_stand") が持つ
- 定期取得は EventScheduler 経由 (起動時にまず 1 回 → 以後 interval 周期)。
  取得本体は worker スレッドに逃がし、dispatch スレッドを HTTP で塞がない
- 記事 (feed_item) は転載のみ保存。取得失敗は CONSECUTIVE_FAILURES / LAST_ERROR
  に正直に記録する (取得できなかったものを取得できたことにしない — 不変条件 2)
- 配送は「その Building に現在いるペルソナ」の知覚バッファへ、ペルソナごとの
  既読カーソル (feed_read_cursor) より新しいものを最大 N 件 (不変条件 3)
- Fixture.STATE_JSON にはペルソナに見せてよい表示情報 (タイトル類) だけを書く。
  購読 URL / ETag / カーソル等の内部帳簿は載せない — STATE_JSON は
  get_visual_context 経由でそのままペルソナのプロンプトに載るため
"""
from __future__ import annotations

import bisect
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING
from urllib.parse import urlparse, urlunparse

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.models import (
    Building,
    FeedItem,
    FeedReadCursor,
    FeedSubscription,
    Fixture,
)
from saiverse.feed_fetch import FeedFetchError, _normalize_url, fetch_feed
from saiverse.observer_manager import update_fixture_state_keys

if TYPE_CHECKING:
    from saiverse.saiverse_manager import SAIVerseManager

LOGGER = logging.getLogger(__name__)

DEFAULT_FETCH_INTERVAL_SEC = 1800
# 取得サイクル全体 (全購読の逐次取得) の壁時計予算 (秒)。応答の遅い購読が
# 並ぶとサイクルが取得間隔を跨いで伸び続けるため、超過したら残りの購読の
# 取得を打ち切る (取得済みぶんの表示更新・配送・剪定は行う)。0 以下は
# 「打ち切らない」の明示指定。
DEFAULT_FEED_CYCLE_BUDGET_SEC = 300
DEFAULT_MAX_ITEMS_PER_PUSH = 3
# 知覚バッファに未消費の feed 知覚がこの件数以上あるペルソナへは投入を見送る
# (カーソルも前進させない — 次の機会に新しいものから届く)。
DEFAULT_MAX_PENDING_FEED = 10
# 購読ごとに保持する記事 (feed_item) の上限。取得サイクルの最後に古い行を
# 剪定する — 記事は再取得で再生する消耗データで、無制限に積むと DB が
# 際限なく育つ。
DEFAULT_FEED_ITEM_KEEP = 200
# 1 施設 (Fixture) あたりの購読数上限の既定。購読は取得・配送・表示の
# すべてを線形に重くするため、入口 (購読の新規作成) で止める。
DEFAULT_MAX_SUBSCRIPTIONS_PER_FIXTURE = 10

# フィード施設の Fixture.TYPE
FEED_FIXTURE_TYPE = "feed_stand"

# EventScheduler に登録する key (グローバルに 1 個)
_SCHEDULER_KEY = "feeds:fetch"

# 知覚バッファ metadata に刻む冪等キー名。値は "feed_item:{subscription_id}:{guid}"
FEED_DEDUPE_META_KEY = "feed_dedupe_key"

# 配送 content に載せる SUMMARY の上限 (入口は概要まで、深掘りはリンク先 — intent §4-3)
_SUMMARY_MAX_CHARS = 300

# 購読タイトル (FeedSubscription.TITLE) の入口上限。フィード宣言タイトルも
# ユーザー指定タイトルも外部/自由入力なので、保存前にここで切り詰める —
# STATE_JSON 経由でペルソナのプロンプトへ、API 経由で UI へそのまま流れるため。
_SUB_TITLE_MAX_CHARS = 200

# FEED_URL / SITE_URL の保存前上限。DB 宣言 (FeedSubscription.FEED_URL /
# SITE_URL = String(512)) と一致させる。SQLite は VARCHAR 長を強制しないため
# ここで守らないと宣言超過の行がそのまま入る。URL は切り詰めると別資源を指す
# 壊れ値になるので、切り詰めではなく拒否 (ユーザー入力) / 破棄 (外部由来の
# site_url) で扱う。
FEED_URL_MAX_CHARS = 512


class FeedSubscriptionLimitError(ValueError):
    """施設の購読数が上限 (SAIVERSE_FEED_MAX_SUBSCRIPTIONS_PER_FIXTURE) に
    達している。ValueError の派生にしてあるのは既存の呼び出し側 (プリセット
    経路の except ValueError → 422) をそのまま通すため。API 層の購読追加
    経路は「見つかりません」系の ValueError (404) と区別して 422 に写像する。
    """


def feed_url_too_long(feed_url: str) -> bool:
    """正規化後の URL が DB 宣言長 (FEED_URL_MAX_CHARS) を超えるか。

    保存されるのは _normalize_feed_url 後の形なので、判定もその形で行う
    (scheme 補完で入力より伸びる場合がある)。urlparse 不能な入力は生の長さで
    判定する (そうした URL は保存前に別経路で拒否される)。API 層 (入口の
    事前拒否) と保存側の検査が同じ判定を共有するための公開ヘルパ。
    """
    try:
        normalized = FeedManager._normalize_feed_url(feed_url)
    except ValueError:
        normalized = feed_url.strip()
    return len(normalized) > FEED_URL_MAX_CHARS


def _utcnow_naive() -> datetime:
    """DB の DateTime 列に入れる naive UTC now。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def city_feed_fixture_ids(db: Session, city_id: int):
    """City 所有権境界の共通述語: 現 City の Building に属するフィード施設
    (TYPE="feed_stand") の FIXTURE_ID を返す subquery。

    multi-city (inter-city travel) は凍結中 (2026-07-16) だが、過去データで
    複数 City を持つ DB は実在しうる。この述語なしだと、City A のプロセスが
    City B の購読を列挙・取得・配送・表示更新できてしまう — 別プロセスが
    所有するペルソナ向けデータをこちらが動かす境界破りになる (十二巡目 Q2)。
    API 層 (api/routes/feeds.py) と FeedManager の全読み書き経路が、この
    述語で自 City の施設に閉じる。使い方は
    ``.filter(<FIXTURE_ID 列>.in_(city_feed_fixture_ids(db, city_id)))``。
    """
    return (
        db.query(Fixture.FIXTURE_ID)
        .join(Building, Fixture.BUILDING_ID == Building.BUILDINGID)
        .filter(
            Fixture.TYPE == FEED_FIXTURE_TYPE,
            Building.CITYID == city_id,
        )
    )


class FeedManager:
    """フィード購読のライフサイクルと定期取得・配送を管理する。"""

    def __init__(self, manager: "SAIVerseManager") -> None:
        # ⚠️ 構築のみ。背景処理 (EventScheduler 登録・スレッド起動) は start() で行う
        # (saiverse_manager.py の構築/起動分離の不変条件)。
        self.manager = manager
        self.interval_seconds = self._read_interval_env()
        # ライフサイクル状態は "new" → "started" → "stopped" の一方向。
        # FeedManager は SAIVerseManager と同寿命で、stop 後に再 start する
        # 正当な用途は無い — stopped からの start() は no-op (WARNING のみ)。
        # 再 start を許すと、stop() の join 待ち中に割り込んだ start() が
        # _stop_event を clear し、中断要求を失った旧 worker が続行する。
        self._state = "new"
        # ライフサイクル状態 (start/stop/worker 起動) の直列化点。stop() の
        # 中断要求確認と worker の登録が非原子だと、stop 直後の tick が
        # 追跡外の worker を起動しうる。
        self._lifecycle_lock = threading.Lock()
        # 取得サイクルの多重起動ガード (worker スレッド側で non-blocking acquire)
        self._fetch_lock = threading.Lock()
        # stop() からの中断要求。worker は購読ごとの境界でこれを確認する
        self._stop_event = threading.Event()
        # 起動した worker の追跡簿 (生存確認しながら剪定)。stop() が全員を join する
        self._workers: List[threading.Thread] = []
        # 公平性 (二十三巡目 AA3): 前回サイクルの先頭購読 ID。次サイクルは
        # その次の購読から取得を始める — 予算打ち切りは列挙順の後方を削る
        # ため、開始位置が固定だと後方の購読が慢性的に取得されない。
        # プロセス内メモリで持つ (再起動でリセットは許容 — 目的は「同じ購読
        # だけが削られ続けない」ことで、次サイクルからまた満たされるため
        # 永続化に値する状態ではない)。書き込みは取得サイクル worker のみ
        # (同時 1 本 — _start_worker が保証) なので lock 不要。
        self._cycle_first_sub_id: Optional[str] = None

    @staticmethod
    def _read_interval_env() -> int:
        env_val = os.environ.get("SAIVERSE_FEED_FETCH_INTERVAL_SEC")
        if env_val:
            try:
                return max(60, int(env_val))
            except ValueError:
                LOGGER.warning(
                    "Invalid SAIVERSE_FEED_FETCH_INTERVAL_SEC=%r; using default",
                    env_val,
                )
        return DEFAULT_FETCH_INTERVAL_SEC

    @staticmethod
    def _read_cycle_budget_env() -> int:
        """取得サイクル全体の壁時計予算 (秒)。0 以下は「打ち切らない」の
        明示指定 (黙って既定へ繰り上げない)。"""
        env_val = os.environ.get("SAIVERSE_FEED_CYCLE_BUDGET_SEC")
        if env_val:
            try:
                return int(env_val)
            except ValueError:
                LOGGER.warning(
                    "Invalid SAIVERSE_FEED_CYCLE_BUDGET_SEC=%r; using default",
                    env_val,
                )
        return DEFAULT_FEED_CYCLE_BUDGET_SEC

    @staticmethod
    def _read_max_items_env() -> int:
        """1 購読 × 1 ペルソナあたりの配送上限 N。

        0 以下は「配送無効」— deliver_new_items は知覚投入もカーソル前進も
        一切しない (取得・保存・STATE_JSON 更新は従来どおり動く)。
        黙って 1 に繰り上げない: 0 は「配送を止めたい」という明示的な指定。
        """
        env_val = os.environ.get("SAIVERSE_FEED_MAX_ITEMS_PER_PUSH")
        if env_val:
            try:
                return int(env_val)
            except ValueError:
                LOGGER.warning(
                    "Invalid SAIVERSE_FEED_MAX_ITEMS_PER_PUSH=%r; using default",
                    env_val,
                )
        return DEFAULT_MAX_ITEMS_PER_PUSH

    @staticmethod
    def _read_max_pending_env() -> int:
        env_val = os.environ.get("SAIVERSE_FEED_MAX_PENDING")
        if env_val:
            try:
                return max(1, int(env_val))
            except ValueError:
                LOGGER.warning(
                    "Invalid SAIVERSE_FEED_MAX_PENDING=%r; using default", env_val,
                )
        return DEFAULT_MAX_PENDING_FEED

    @staticmethod
    def _read_item_keep_env() -> int:
        """購読ごとの記事保持上限。0 以下は「剪定しない」の明示指定。"""
        env_val = os.environ.get("SAIVERSE_FEED_ITEM_KEEP")
        if env_val:
            try:
                return int(env_val)
            except ValueError:
                LOGGER.warning(
                    "Invalid SAIVERSE_FEED_ITEM_KEEP=%r; using default", env_val,
                )
        return DEFAULT_FEED_ITEM_KEEP

    @staticmethod
    def _read_max_subscriptions_env() -> int:
        """1 施設あたりの購読数上限。0 以下は不正値として既定へ (0 で
        「購読を止める」意味は持たせない — 止めたければ購読を削除する)。"""
        env_val = os.environ.get("SAIVERSE_FEED_MAX_SUBSCRIPTIONS_PER_FIXTURE")
        if env_val:
            try:
                value = int(env_val)
            except ValueError:
                LOGGER.warning(
                    "Invalid SAIVERSE_FEED_MAX_SUBSCRIPTIONS_PER_FIXTURE=%r; "
                    "using default", env_val,
                )
            else:
                if value > 0:
                    return value
                LOGGER.warning(
                    "Invalid SAIVERSE_FEED_MAX_SUBSCRIPTIONS_PER_FIXTURE=%r "
                    "(must be >= 1); using default", env_val,
                )
        return DEFAULT_MAX_SUBSCRIPTIONS_PER_FIXTURE

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """定期取得を EventScheduler に登録する。

        first_fire_immediate=True で起動直後にまず 1 回取得する — サーバーを
        常時起動しないユーザーでは「起動時にまず取得」が実質の既定になる
        (intent §10-6)。

        stop() 済みの FeedManager は再 start できない (no-op)。_stop_event は
        clear しない — clear すると stop() の join 待ち中の旧 worker が中断
        要求を失って続行する (__init__ の _state コメント参照)。
        """
        with self._lifecycle_lock:
            if self._state == "stopped":
                LOGGER.warning(
                    "[feed] start() after stop() is ignored (FeedManager is "
                    "single-use; create a new instance to restart)",
                )
                return
            if self._state == "started":
                LOGGER.debug("[feed] Already started")
                return
            scheduler = getattr(self.manager, "event_scheduler", None)
            if scheduler is None:
                LOGGER.warning("[feed] event_scheduler not available; cannot start")
                return
            scheduler.schedule_periodic(
                interval_seconds=self.interval_seconds,
                callback=self._safe_tick,
                key=_SCHEDULER_KEY,
                first_fire_immediate=True,
            )
            self._state = "started"
        LOGGER.info(
            "[feed] Started (interval=%d sec, first fetch immediate)",
            self.interval_seconds,
        )

    def stop(self) -> None:
        """定期取得を止め、実行中の worker には中断を要求して join を試みる。

        中断要求 (_stop_event) を立てた後は、_safe_tick / fetch_now /
        _start_worker のいずれも新しい worker を起動しない (入口で確認)。
        実行中の worker は購読ごとの境界と配送前の停止確認で中断する。
        join は全 worker 合計でタイムアウト付き (10 秒) — daemon スレッド
        なので残ってもプロセス終了は妨げないが、残留は WARNING で表明する。
        タイムアウト + WARNING 続行は意図的な設計: 完全なバリア (無期限 join)
        は shutdown を最悪 HTTP タイムアウト分 (60 秒級) 塞ぐ方が害が大きい。
        残留 worker の書き込みは commit 前の停止確認 (_fetch_one /
        _fetch_cycle_worker) によりデータ表へ到達しない (2026-08-02 裁定)。
        """
        with self._lifecycle_lock:
            self._stop_event.set()
            if self._state == "started":
                scheduler = getattr(self.manager, "event_scheduler", None)
                if scheduler is not None:
                    scheduler.cancel(_SCHEDULER_KEY)
            self._state = "stopped"
            workers = [w for w in self._workers if w.is_alive()]
        # join は lock の外で行う (握ったまま最長 10 秒待たない)
        deadline = time.monotonic() + 10.0
        for worker in workers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            worker.join(timeout=remaining)
        leftover = [w for w in workers if w.is_alive()]
        if leftover:
            LOGGER.warning(
                "[feed] %d fetch worker(s) did not stop within 10s", len(leftover),
            )
        with self._lifecycle_lock:
            self._workers = [w for w in self._workers if w.is_alive()]
        LOGGER.info("[feed] Stopped")

    # ------------------------------------------------------------------
    # 定期取得 (EventScheduler → worker スレッド)
    # ------------------------------------------------------------------

    def _safe_tick(self) -> None:
        """schedule_periodic の callback。

        EventScheduler の dispatch スレッドを HTTP で塞がないよう、取得本体は
        worker スレッド (daemon) に逃がす。例外はすべて握って WARNING —
        callback が例外を漏らすと周期処理ごと止まる (event_scheduler.py の仕様)。
        """
        try:
            if self._stop_event.is_set():
                # stop() 後にも周期 callback は届きうる: cancel は「実行中の
                # callback が完了後に行う再予約」を止められない
                # (schedule_periodic の wrapper は callback 成功後に無条件で
                # 次回を積む)。event_scheduler の API で再予約自体を止めるには
                # callback から例外を投げる手 (wrapper は例外時に再予約しない)
                # があるが、正常な停止経路で ERROR ログ + traceback を出す
                # ことになるため採らない。no-op 化で足りる: 残った周期予約は
                # start() の同一 key 上書きかプロセス終了で消える。
                LOGGER.debug("[feed] tick ignored (stopping)")
                return
            if self._start_worker() is None:
                LOGGER.debug("[feed] previous fetch cycle still running; tick skipped")
        except Exception:
            LOGGER.exception("[feed] tick failed to start worker")

    def _start_worker(self) -> Optional[threading.Thread]:
        """取得 worker を 1 本起動する。既に実行中か停止中なら起動せず None。

        _lifecycle_lock を握ったまま「停止確認 → 生存 worker の剪定・確認 →
        登録 → start」まで行う (stop() の中断要求と非原子だと、stop 直後に
        追跡外の worker が走り出す)。_fetch_lock の non-blocking acquire は
        worker 側の最終防衛線として残す。
        """
        with self._lifecycle_lock:
            if self._stop_event.is_set():
                return None
            self._workers = [w for w in self._workers if w.is_alive()]
            if self._workers or self._fetch_lock.locked():
                return None
            worker = threading.Thread(
                target=self._fetch_cycle_worker, name="FeedFetchWorker", daemon=True,
            )
            self._workers.append(worker)
            worker.start()
            return worker

    def _fetch_cycle_worker(self) -> None:
        """worker スレッド本体: 全購読の取得 → 表示更新 → 配送。"""
        if not self._fetch_lock.acquire(blocking=False):
            LOGGER.debug("[feed] previous fetch cycle still running; skipping")
            return
        try:
            self._fetch_all()
            # 取得と表示更新・配送の間の停止確認: stop() 後に DB (STATE_JSON /
            # カーソル) や知覚バッファへ書き込まない。取得済み記事は保存済み
            # なので、次回サイクルで表示・配送される (欠落しない)。
            if self._stop_event.is_set():
                LOGGER.info(
                    "[feed] fetch cycle interrupted by stop(); "
                    "display update and delivery skipped",
                )
                return
            self._update_all_fixture_displays()
            self.deliver_new_items()
            self._prune_old_items()
        except Exception:
            LOGGER.exception("[feed] fetch cycle failed")
        finally:
            self._fetch_lock.release()

    def fetch_now(self) -> Optional[threading.Thread]:
        """手動の全取得。定期取得の worker と同じ経路を即時に 1 回走らせる。

        Returns: 起動した worker スレッド (呼び出し側は join で完了を待てる)。
        既に取得サイクルが実行中、または stop() による停止中は起動せず None
        (API 層は 409 を返す)。
        """
        return self._start_worker()

    # ------------------------------------------------------------------
    # 取得・保存
    # ------------------------------------------------------------------

    def _fetch_all(self) -> None:
        """ENABLED な全購読を逐次取得する (同時 1 本)。

        列挙は現 City のフィード施設 (city_feed_fixture_ids — City 境界 +
        TYPE="feed_stand") に属する購読に限る — 配送クエリ
        (deliver_new_items) と同じ形。Fixture の TYPE が後から書き換わって
        孤児化した購読や別 City の購読は列挙から外れ、取得もネットワークも
        走らない (不活性で無害)。孤児購読のデータ (行・記事・カーソル) は
        残るが、これは受容する — 削除は UI からの明示操作に委ねる。

        サイクル全体には壁時計予算 (SAIVERSE_FEED_CYCLE_BUDGET_SEC) が
        かかる (二十三巡目 AA3) — 購読ごとのタイムアウトはあっても本数分は
        積み上がるため、遅い購読が並ぶとサイクルが取得間隔を跨いで伸びる。
        超過したら残りの購読の取得を打ち切る (WARNING で残数表明)。打ち切りは
        取得だけで、取得済みぶんの表示更新・配送・剪定は呼び出し元
        (_fetch_cycle_worker) が続けて実行する。打ち切りで削られるのが常に
        列挙順の後方に偏らないよう、処理開始位置はサイクルごとに回転する
        (_rotate_subscription_order)。
        """
        db: Session = self.manager.SessionLocal()
        try:
            sub_ids = [
                row[0]
                for row in db.query(FeedSubscription.SUBSCRIPTION_ID)
                .filter(
                    FeedSubscription.ENABLED == True,  # noqa: E712
                    FeedSubscription.FIXTURE_ID.in_(
                        city_feed_fixture_ids(db, self.manager.city_id)
                    ),
                )
                # ローテーションの前提となる決定的な列挙順 (順序の値自体に
                # 意味はない — 安定していればよい)
                .order_by(FeedSubscription.SUBSCRIPTION_ID)
                .all()
            ]
        finally:
            db.close()
        sub_ids = self._rotate_subscription_order(sub_ids)
        budget = self._read_cycle_budget_env()
        cycle_started = time.monotonic()
        for index, subscription_id in enumerate(sub_ids):
            if self._stop_event.is_set():
                LOGGER.info("[feed] fetch cycle interrupted by stop()")
                break
            if budget > 0 and time.monotonic() - cycle_started >= budget:
                # 黙って止めない: 何本を取得せず残したか表明する
                LOGGER.warning(
                    "[feed] fetch cycle budget exceeded (%ds); "
                    "%d of %d subscriptions left unfetched this cycle",
                    budget, len(sub_ids) - index, len(sub_ids),
                )
                break
            try:
                self._fetch_one(subscription_id)
            except Exception:
                # 階層防御: 既知の取得失敗は fetch_feed が FeedFetchError に
                # 写像し _fetch_one が記録して返す。ここは想定外の例外の関所 —
                # 悪性フィード 1 本 (解析クラッシュ等) がサイクル全体を殺し、
                # 残りの購読の取得まで止めることを防ぐ。失敗は購読行に正直に
                # 記録し (不変条件 2)、次の購読へ続行する。
                LOGGER.exception(
                    "[feed] unexpected fetch failure isolated: %s", subscription_id,
                )
                if not self._stop_event.is_set():
                    try:
                        self._record_fetch_failure(
                            subscription_id, "予期しないエラーで取得に失敗しました",
                        )
                    except Exception:
                        LOGGER.exception(
                            "[feed] failed to record fetch failure: %s",
                            subscription_id,
                        )

    def _rotate_subscription_order(self, sub_ids: List[str]) -> List[str]:
        """整列済みの購読 ID 列を「前回サイクルの先頭の次」から始まる並びへ
        回転し、今回の先頭を記録する (二十三巡目 AA3 の公平性)。

        入力は SUBSCRIPTION_ID 昇順で整列済みであること (_fetch_all の
        order_by)。前回の先頭が削除済みでも、bisect で「整列順でその次」の
        位置に落ちるため回転は継続する。
        """
        if not sub_ids:
            return sub_ids
        start = 0
        if self._cycle_first_sub_id is not None:
            start = bisect.bisect_right(
                sub_ids, self._cycle_first_sub_id
            ) % len(sub_ids)
        rotated = sub_ids[start:] + sub_ids[:start]
        self._cycle_first_sub_id = rotated[0]
        return rotated

    def _fetch_one(self, subscription_id: str) -> int:
        """購読 1 本を取得して新規記事を保存する。

        DB session を外部 GET の間は握らない: 購読情報の読み出し → session close
        → ネットワーク → 新 session で書き込み、の 3 段に分ける。

        Returns: 新規保存した記事数。失敗時は 0 (失敗は購読行に正直に記録する)。
        """
        # --- 1. 購読情報の読み出し (すぐ閉じる) ---
        db: Session = self.manager.SessionLocal()
        try:
            sub = (
                db.query(FeedSubscription)
                .filter(FeedSubscription.SUBSCRIPTION_ID == subscription_id)
                .first()
            )
            if sub is None or not sub.ENABLED:
                return 0
            feed_url = sub.FEED_URL
            etag = sub.ETAG
            last_modified = sub.LAST_MODIFIED
            display_name = sub.TITLE or sub.FEED_URL
        finally:
            db.close()

        # --- 2. ネットワーク (session なし) ---
        try:
            result = fetch_feed(feed_url, etag=etag, last_modified=last_modified)
        except FeedFetchError as exc:
            if self._stop_event.is_set():
                # 停止中の残留 worker はデータ表に書かない (stop() の裁定
                # コメント参照)。失敗記録は次回サイクルの取得で付き直る。
                LOGGER.info(
                    "[feed] fetch failure for %s not recorded (stopping)",
                    display_name,
                )
                return 0
            # 取得失敗を成功と偽装しない (不変条件 2): 失敗として記録する
            failures = self._record_fetch_failure(subscription_id, str(exc))
            LOGGER.warning(
                "[feed] fetch failed: %s (%s) failures=%d: %s",
                display_name, subscription_id, failures, exc,
            )
            return 0

        # --- 3. 結果の書き込み (新 session) ---
        # commit 前の停止確認: stop() 後の残留 worker は、取得が済んでいても
        # 購読状態・記事をデータ表に書かない (stop() の裁定コメント参照)。
        # 捨てた分は次回サイクルで再取得される (欠落しない)。
        if self._stop_event.is_set():
            LOGGER.info(
                "[feed] fetch result for %s discarded (stopping)", display_name,
            )
            return 0
        db = self.manager.SessionLocal()
        try:
            # 所有権条件は保存 transaction 自身が運ぶ: ネットワーク中に購読が
            # 消えた / Fixture の Building 差し替え・ID 再利用で別 City 所有に
            # なった購読へは書かない (列挙時点の確認 = 事前確認を認可根拠に
            # しない — city_feed_fixture_ids の docstring 参照)。
            sub = (
                db.query(FeedSubscription)
                .filter(
                    FeedSubscription.SUBSCRIPTION_ID == subscription_id,
                    FeedSubscription.FIXTURE_ID.in_(
                        city_feed_fixture_ids(db, self.manager.city_id)
                    ),
                )
                .first()
            )
            if sub is None:
                LOGGER.warning(
                    "[feed] fetch result for %s discarded: subscription was "
                    "deleted or no longer belongs to a feed fixture of this "
                    "city", display_name,
                )
                return 0

            sub.LAST_OK_AT = _utcnow_naive()
            sub.CONSECUTIVE_FAILURES = 0
            sub.LAST_ERROR = None

            if result.not_modified:
                db.commit()
                LOGGER.debug("[feed] not modified: %s", sub.FEED_URL)
                return 0

            sub.ETAG = result.etag
            sub.LAST_MODIFIED = result.last_modified
            # タイトル未設定の購読はフィード自身の宣言タイトルで埋める (転載)
            if not sub.TITLE and result.title:
                sub.TITLE = result.title[:_SUB_TITLE_MAX_CHARS]

            guids = [e.guid for e in result.entries]
            existing = set()
            if guids:
                existing = {
                    row[0]
                    for row in db.query(FeedItem.GUID)
                    .filter(
                        FeedItem.SUBSCRIPTION_ID == subscription_id,
                        FeedItem.GUID.in_(guids),
                    )
                    .all()
                }
            new_count = 0
            for entry in result.entries:
                if entry.guid in existing:
                    continue
                existing.add(entry.guid)  # 同一レスポンス内の重複 guid も弾く
                published_at = (
                    entry.published.astimezone(timezone.utc).replace(tzinfo=None)
                    if entry.published
                    else None
                )
                db.add(
                    FeedItem(
                        SUBSCRIPTION_ID=subscription_id,
                        GUID=entry.guid,
                        TITLE=entry.title,
                        SUMMARY=entry.summary,
                        LINK=entry.link,
                        PUBLISHED_AT=published_at,
                    )
                )
                new_count += 1
            db.commit()
            if new_count:
                LOGGER.info(
                    "[feed] fetched %s: %d new item(s)",
                    sub.TITLE or sub.FEED_URL, new_count,
                )
            return new_count
        finally:
            db.close()

    def _record_fetch_failure(self, subscription_id: str, message: str) -> int:
        """購読 1 本の取得失敗を購読行に記録する (CONSECUTIVE_FAILURES++ と
        LAST_ERROR)。_fetch_one の FeedFetchError 経路と、_fetch_all の
        想定外例外の隔離経路が共用する。

        Returns: 記録後の連続失敗回数 (購読が消えているか現 City の所有で
        なくなっていれば 0)。
        """
        db: Session = self.manager.SessionLocal()
        try:
            # 保存経路 (_fetch_one) と同じ所有権条件: 失敗記録も「現 City の
            # フィード施設に属する購読」へしか書かない (取得中の削除・越境に
            # 耐えるのは書き込み transaction 自身の条件だけ)。
            sub = (
                db.query(FeedSubscription)
                .filter(
                    FeedSubscription.SUBSCRIPTION_ID == subscription_id,
                    FeedSubscription.FIXTURE_ID.in_(
                        city_feed_fixture_ids(db, self.manager.city_id)
                    ),
                )
                .first()
            )
            if sub is None:
                LOGGER.warning(
                    "[feed] fetch failure for %s not recorded: subscription "
                    "was deleted or no longer belongs to a feed fixture of "
                    "this city", subscription_id,
                )
                return 0
            sub.CONSECUTIVE_FAILURES = (sub.CONSECUTIVE_FAILURES or 0) + 1
            sub.LAST_ERROR = message[:512]
            db.commit()
            return sub.CONSECUTIVE_FAILURES
        finally:
            db.close()

    def _prune_old_items(self) -> int:
        """購読ごとに保持上限 (SAIVERSE_FEED_ITEM_KEEP 件) を超えた古い記事を
        削除する。取得サイクルの最後 (配送後) に呼ばれる。

        「新しい」の定義は**配送の選択順位と同一** (PUBLISHED_AT desc [NULL は
        末尾], id desc)。id 降順で残すと、newest-first フィードの初回取り込み
        (若い id ほど新しい記事) で「配送がこれから選ぶ最新記事」を剪定が
        削る順位ズレが起きる (十九巡目で検出 — F1 のカーソル順位ズレと同族)。
        既読カーソル (feed_read_cursor) は id を数値として参照するだけなので、
        行の剪定でカーソルの意味は変わらない (配送済み判定が巻き戻ることは
        ない)。UI の記事一覧 API は保存されている範囲を返すだけで、剪定に
        伴う変更は不要。

        「剪定が未配送記事を不可逆に消すのでは」という懸念は設計意味論で
        却下済み (十一巡目 P2): 配送は常に「カーソルより新しい候補のうち
        最新 N 件 (SAIVERSE_FEED_MAX_ITEMS_PER_PUSH) を届け、カーソルは候補
        全体の末尾へ飛ぶ」(タイムライン意味論、intent §13)。つまり次に配送
        されうるのは各購読の最新 N 件だけで、KEEP >= N である限りその N 件は
        剪定を必ず生き残る。剪定で消える古い記事は、剪定が無くても配送されず
        スキップされる運命の行 — 消しても配送結果は変わらない。

        Returns: 削除した記事数。上限が 0 以下 (剪定無効) なら 0。
        """
        keep = self._read_item_keep_env()
        if keep <= 0:
            return 0
        # 上の却下理由が成立するのは KEEP >= 配送 N 件のときだけ。KEEP < N の
        # 端な設定では「配送されるはずの最新 N 件」の一部が剪定されうるため、
        # 実効 keep を N まで引き上げて意味論を守る (N <= 0 = 配送無効時は
        # max() が keep をそのまま返す)。
        keep = max(keep, self._read_max_items_env())
        db: Session = self.manager.SessionLocal()
        try:
            # 剪定対象は現 City のフィード施設に属する購読に限る — 全 DB を
            # 対象にすると City A のサイクルが City B の記事を削除してしまう
            # (city_feed_fixture_ids の docstring 参照)。列挙から外れた孤児
            # 購読 (TYPE 書き換え等) の記事は剪定もされず残るが、_fetch_all
            # と同じく受容する (削除は UI からの明示操作に委ねる)。
            sub_ids = [
                row[0]
                for row in db.query(FeedSubscription.SUBSCRIPTION_ID)
                .filter(
                    FeedSubscription.FIXTURE_ID.in_(
                        city_feed_fixture_ids(db, self.manager.city_id)
                    ),
                )
                .all()
            ]
            deleted_total = 0
            for sub_id in sub_ids:
                # 配送と同一順位で「残す集合」を選ぶ (配送がこれから選び
                # うる行は必ず生き残る)。published 順は id 順と一致しない
                # (newest-first 初回取り込み) ため、境界 id 方式は使えない
                rows = (
                    db.query(FeedItem.id)
                    .filter(FeedItem.SUBSCRIPTION_ID == sub_id)
                    .order_by(FeedItem.PUBLISHED_AT.desc(), FeedItem.id.desc())
                    .limit(keep + 1)
                    .all()
                )
                if len(rows) <= keep:
                    continue  # 保存件数が keep 件以下
                keep_ids = [row[0] for row in
                            db.query(FeedItem.id)
                            .filter(FeedItem.SUBSCRIPTION_ID == sub_id)
                            .order_by(
                                FeedItem.PUBLISHED_AT.desc(),
                                FeedItem.id.desc(),
                            )
                            .limit(keep)
                            .all()]
                # 選択と DELETE の間に取り込まれた行 (id が選択時点の最大より
                # 大きい) は消さない — NOT IN の巻き添え防止
                max_seen_id = (
                    db.query(func.max(FeedItem.id))
                    .filter(FeedItem.SUBSCRIPTION_ID == sub_id)
                    .scalar()
                )
                deleted_total += (
                    db.query(FeedItem)
                    .filter(
                        FeedItem.SUBSCRIPTION_ID == sub_id,
                        ~FeedItem.id.in_(keep_ids),
                        FeedItem.id <= max_seen_id,
                        # City 条件は DELETE 文自身が運ぶ: 列挙・選択と削除の
                        # 間に施設の所有が別 City へ移った購読の行は消さない
                        # (事前確認を認可根拠にしない — 十四巡目 S2)
                        FeedItem.SUBSCRIPTION_ID.in_(
                            db.query(FeedSubscription.SUBSCRIPTION_ID).filter(
                                FeedSubscription.FIXTURE_ID.in_(
                                    city_feed_fixture_ids(
                                        db, self.manager.city_id
                                    )
                                ),
                            )
                        ),
                    )
                    .delete(synchronize_session=False)
                )
            if deleted_total:
                db.commit()
                LOGGER.info(
                    "[feed] pruned %d old item(s) (keep=%d per subscription)",
                    deleted_total, keep,
                )
            return deleted_total
        finally:
            db.close()

    # ------------------------------------------------------------------
    # 配送 (知覚バッファへの投入)
    # ------------------------------------------------------------------

    def deliver_new_items(self) -> int:
        """フィード施設のある Building に現在いるペルソナへ未読記事を配送する。

        ペルソナごとの既読カーソルより新しい記事のうち**最も新しい N 件**を
        知覚バッファ (kind="feed") に積み、カーソルは候補全体の末尾へ前進させる
        (古い候補は正直にスキップ — タイムラインは流れるもの、intent §13)。

        Returns: 投入した知覚の総数。
        """
        max_items = self._read_max_items_env()
        if max_items <= 0:
            # N=0 (以下) は配送無効: 知覚投入もカーソル前進も一切しない。
            # 取得・保存・STATE_JSON 更新は呼び出し元の取得サイクルが従来どおり
            # 行う (_read_max_items_env の docstring 参照)。
            LOGGER.debug(
                "[feed] delivery disabled (SAIVERSE_FEED_MAX_ITEMS_PER_PUSH<=0)",
            )
            return 0
        max_pending = self._read_max_pending_env()

        # 購読 → Building の対応を先に取り切る (session を長持ちさせない)
        db: Session = self.manager.SessionLocal()
        try:
            rows = (
                db.query(FeedSubscription, Fixture)
                .join(Fixture, FeedSubscription.FIXTURE_ID == Fixture.FIXTURE_ID)
                .filter(
                    FeedSubscription.ENABLED == True,  # noqa: E712
                    # City 境界 + TYPE="feed_stand" (city_feed_fixture_ids)
                    Fixture.FIXTURE_ID.in_(
                        city_feed_fixture_ids(db, self.manager.city_id)
                    ),
                )
                .all()
            )
            subs_by_building: Dict[str, List[Dict[str, str]]] = {}
            for sub, fixture in rows:
                subs_by_building.setdefault(fixture.BUILDING_ID, []).append(
                    {
                        "subscription_id": sub.SUBSCRIPTION_ID,
                        "title": sub.TITLE or "",
                    }
                )
        finally:
            db.close()

        if not subs_by_building:
            return 0

        occupants: Dict[str, List[str]] = getattr(self.manager, "occupants", {}) or {}
        personas: Dict[str, Any] = getattr(self.manager, "personas", {}) or {}

        delivered_total = 0
        for building_id, subs in subs_by_building.items():
            for occupant_id in list(occupants.get(building_id, [])):
                persona = personas.get(occupant_id)
                if persona is None:
                    continue  # ユーザーやローカルに実体の無い occupant は対象外
                # 退室者への誤配送の再確認は購読ごと・カーソル commit 直前に
                # 行う (_deliver_subscription_to_persona 内)。
                adapter = getattr(persona, "sai_memory", None)
                if adapter is None or not adapter.is_ready():
                    # 黙って欠落させない: 投入もカーソル前進もしないことを明示する
                    LOGGER.warning(
                        "[feed] SAIMemory adapter not ready for %s; "
                        "delivery skipped (cursor not advanced)", occupant_id,
                    )
                    continue
                # 知覚バッファの膨張ガード: 未消費の feed 知覚が上限以上なら
                # このペルソナへの投入を見送る (カーソルも前進させない —
                # 次の機会に新しいものから届く)。
                pending = adapter.count_pending_perceptions("feed")
                if pending is None:
                    LOGGER.warning(
                        "[feed] pending count unavailable for %s; "
                        "delivery skipped (cursor not advanced)", occupant_id,
                    )
                    continue
                # 残り枠はペルソナ単位の共有予算: 未消費 (pending) + この
                # サイクルでの投入合計が max_pending を超えないよう、購読を
                # またいで共有する (購読ごとに pending を読み直す方式だと
                # 購読数 × N で上限を突き抜ける)。
                remaining = max_pending - pending
                if remaining <= 0:
                    LOGGER.info(
                        "[feed] %s has %d pending feed perception(s) (>= %d); "
                        "delivery deferred", occupant_id, pending, max_pending,
                    )
                    continue
                for sub in subs:
                    if remaining <= 0:
                        # 予算を使い切った: 以降の購読は見送り (カーソルも
                        # 据え置き — 次の機会に新しいものから届く)
                        LOGGER.debug(
                            "[feed] pending budget exhausted for %s; "
                            "remaining subscriptions deferred", occupant_id,
                        )
                        break
                    try:
                        pushed = self._deliver_subscription_to_persona(
                            persona=persona,
                            building_id=building_id,
                            persona_id=occupant_id,
                            adapter=adapter,
                            subscription_id=sub["subscription_id"],
                            subscription_title=sub["title"],
                            max_items=min(max_items, remaining),
                        )
                    except Exception:
                        # 購読単位の失敗隔離 (_fetch_all の N2 と同じ形):
                        # スナップショット競合等の DB 例外 1 件で cycle 全体
                        # (他の購読・他のペルソナへの配送) を殺さない。例外が
                        # 逃げられるのはカーソル commit までの DB 区間だけ
                        # (知覚投入は commit 後で、記事単位に握られている) —
                        # つまりこの購読は書き込み全スキップで、カーソルも
                        # 動いていない。次の機会に新しいものから届く。
                        LOGGER.warning(
                            "[feed] delivery failed for subscription %s to %s; "
                            "skipped (no cursor advance, no perception push)",
                            sub["subscription_id"], occupant_id, exc_info=True,
                        )
                        pushed = 0
                    delivered_total += pushed
                    remaining -= pushed
        return delivered_total

    def _deliver_subscription_to_persona(
        self,
        *,
        persona: Any,
        building_id: str,
        persona_id: str,
        adapter: Any,
        subscription_id: str,
        subscription_title: str,
        max_items: int,
    ) -> int:
        """購読 1 本 × ペルソナ 1 人の配送。投入した知覚の数を返す。

        退室者への誤配送の緩和: 現在地 (current_building_id) の再確認は
        カーソル commit の直前で行い、不一致なら commit も知覚投入もせず
        skip する (occupants 取得時点の確認だけでは、そこから配送までの間の
        移動を取りこぼす)。残余: この確認から commit までの間の移動は
        なお取りこぼしうる — 完全な同期 (位置の世代管理) までは求めない
        (F10 裁定の延長, 2026-08-02)。

        購読削除との並走: 購読の生存 (存在 + ENABLED + 現 City 所有) も
        カーソル commit 直前に再確認し、消えていれば commit も知覚投入もせず
        0 を返す
        (道連れ削除済みカーソルの復活 INSERT や、削除済み購読名義の知覚
        投入を防ぐ)。残余: この確認から commit・知覚投入までの間に削除が
        入る窓は受容する — 削除 API と配送 worker のロック結合は過剰と裁定
        (2026-08-03)。

        選択: 候補 = カーソルより新しい**全件**のうち、最も新しい N 件
        (PUBLISHED_AT desc nulls last, id desc の先頭 N)。新着順フィードの
        初回取り込み (id 昇順 = 新しい → 古い) でも最新記事が漏れない。
        提示はその N 件を「日付あり昇順 → 日付なしは末尾」で投入する
        (いつのものか不明な記事を時系列の中に混ぜず、最後に置く)。

        カーソルは**候補全体**の max(id) へ前進する — 配送しなかった古い候補は
        正直にスキップ (タイムラインは流れるもの、intent §13)。

        順序: カーソル前進 (main DB commit) を先に耐久化してから知覚を投入する。
        投入失敗・クラッシュ時は当該 N 件が欠落する — 重複より欠落に倒す
        (intent §13 整合)。
        """
        db: Session = self.manager.SessionLocal()
        try:
            cursor = (
                db.query(FeedReadCursor)
                .filter(
                    FeedReadCursor.PERSONA_ID == persona_id,
                    FeedReadCursor.SUBSCRIPTION_ID == subscription_id,
                )
                .first()
            )
            last_item_id = cursor.LAST_ITEM_ID if cursor else 0

            candidate_filter = (
                FeedItem.SUBSCRIPTION_ID == subscription_id,
                FeedItem.id > last_item_id,
            )
            # 最も新しい N 件 (SQLite は desc で NULL が最後に並ぶ)
            newest = (
                db.query(FeedItem)
                .filter(*candidate_filter)
                .order_by(FeedItem.PUBLISHED_AT.desc(), FeedItem.id.desc())
                .limit(max_items)
                .all()
            )
            if not newest:
                return 0
            new_last = db.query(func.max(FeedItem.id)).filter(
                *candidate_filter
            ).scalar()

            # 提示順を明示: (日付なしか, 公開日時, id) の昇順 = 日付あり記事を
            # 時系列昇順で先に、日付なし (PUBLISHED_AT が NULL) 記事は末尾に置く。
            # 選択クエリの reversed() に頼ると NULL が先頭に来てしまう。
            presented = sorted(
                newest,
                key=lambda it: (
                    it.PUBLISHED_AT is None,
                    it.PUBLISHED_AT or datetime.min,
                    it.id,
                ),
            )
            # commit で ORM 属性が expire するため、先に素の値へ写し取る
            items = [
                {
                    "id": item.id,
                    "guid": item.GUID,
                    "title": item.TITLE,
                    "summary": item.SUMMARY,
                    "link": item.LINK,
                    "published_at": item.PUBLISHED_AT,
                }
                for item in presented
            ]

            # 現在地の再確認 (カーソル commit 直前)。不一致なら commit も
            # 知覚投入もしない — カーソルは動かず、次の機会に届く。
            if getattr(persona, "current_building_id", None) != building_id:
                LOGGER.debug(
                    "[feed] %s moved out of %s; delivery skipped",
                    persona_id, building_id,
                )
                return 0

            # 購読の生存再確認 (カーソル commit 直前)。配送と削除 API は並走
            # しうる — 消えて/無効化されていれば commit も知覚投入もしない
            # (docstring「購読削除との並走」参照)。所有権 (現 City のフィード
            # 施設に属すること) も同じ再確認に含める — 候補列挙後に施設が
            # 別 City の Building へ付け替わった購読へは配送しない (書き込み
            # 直前の条件だけが越境に耐える — city_feed_fixture_ids 参照)。
            live = (
                db.query(FeedSubscription.SUBSCRIPTION_ID)
                .filter(
                    FeedSubscription.SUBSCRIPTION_ID == subscription_id,
                    FeedSubscription.ENABLED == True,  # noqa: E712
                    FeedSubscription.FIXTURE_ID.in_(
                        city_feed_fixture_ids(db, self.manager.city_id)
                    ),
                )
                .first()
            )
            if live is None:
                LOGGER.debug(
                    "[feed] subscription %s removed, disabled, or no longer "
                    "owned by this city during delivery; skipped",
                    subscription_id,
                )
                return 0

            # カーソル前進を先に耐久化 (クラッシュ時は重複より欠落に倒す)。
            # push 失敗分は知覚への自動配送の機会だけを失う。記事データは
            # feed_item に残り、UI と施設の見出しからは引き続き到達可能
            # (outbox を建てない理由。タイムライン意味論 = intent §13)。
            if cursor is None:
                db.add(
                    FeedReadCursor(
                        PERSONA_ID=persona_id,
                        SUBSCRIPTION_ID=subscription_id,
                        LAST_ITEM_ID=new_last,
                    )
                )
            else:
                cursor.LAST_ITEM_ID = new_last
            db.commit()
        finally:
            db.close()

        # --- 知覚投入 (main DB session は閉じた後) ---
        pushed = 0
        for item in items:
            # 冪等 marker: sha256(subscription_id + ":" + guid) の hex。
            # 生 GUID を使うと JSON エスケープ (ダブルクォート/バックスラッシュ)
            # で LIKE 照合をすり抜けるため、hex に固定して構造的に排除する。
            dedupe_value = hashlib.sha256(
                f"{subscription_id}:{item['guid']}".encode("utf-8")
            ).hexdigest()
            try:
                if adapter.has_pending_perception_marker(
                    FEED_DEDUPE_META_KEY, dedupe_value,
                ):
                    LOGGER.debug(
                        "[feed] duplicate perception suppressed: %s", dedupe_value,
                    )
                    continue
                adapter.push_perception(
                    kind="feed",
                    content=self._format_item_content(subscription_title, item),
                    metadata=json.dumps(
                        {
                            FEED_DEDUPE_META_KEY: dedupe_value,
                            "subscription_id": subscription_id,
                            "link": item["link"] or "",
                            "published_at": (
                                item["published_at"].isoformat()
                                if item["published_at"]
                                else None
                            ),
                        },
                        ensure_ascii=False,
                    ),
                )
            except Exception:
                # カーソルは既に前進済み: 失敗分は欠落として諦める (重複させない)
                LOGGER.warning(
                    "[feed] perception push failed for %s (item %s); "
                    "item dropped (cursor already advanced)",
                    persona_id, item["id"], exc_info=True,
                )
                continue
            pushed += 1

        if pushed:
            LOGGER.info(
                "[feed] delivered %d item(s) of %s to %s (cursor -> %d)",
                pushed, subscription_title or subscription_id,
                persona_id, new_last,
            )
        return pushed

    @staticmethod
    def _format_item_content(subscription_title: str, item: Dict[str, Any]) -> str:
        """配送する知覚の本文。実在記事の転載のみで、生成・言い換えはしない
        (intent 不変条件 1)。冒頭の一行で「外部サイトの転載」であることを
        枠づけする (内容の出所をペルソナに明示する — 生成はしない)。"""
        title = subscription_title or "フィード"
        summary = (item["summary"] or "")[:_SUMMARY_MAX_CHARS]
        lines = [
            "(外部サイトの記事の転載)",
            f"『{title}』の新着記事: {item['title']}",
        ]
        if summary:
            lines.append(summary)
        if item["link"]:
            lines.append(f"(リンク: {item['link']})")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Fixture 表示 (STATE_JSON)
    # ------------------------------------------------------------------

    def update_fixture_display(self, fixture_id: str) -> None:
        """Fixture.STATE_JSON にペルソナに見せてよい表示情報だけを書く。

        STATE_JSON は get_visual_context 経由でそのままペルソナのプロンプトに
        載るため、購読 URL・ETag・カーソル等の内部帳簿は絶対に入れない。
        書くのは購読タイトル一覧 + 直近記事タイトル + 更新時刻のみ。

        STATE_JSON は複数の書き手のキーがトップレベルで共存する列
        (ObserverManager.record_metrics の metric キー / ここの feed_stand
        キー)。書き込みは json_set の単文 UPDATE (update_fixture_state_keys)
        で feed_stand キーだけを DB 側で原子的に差し替える — プロセス内外の
        どの並走書き込みとも他キーを消さない (observer_manager.py 冒頭の
        コメント参照)。
        """
        from saiverse.observer_manager import run_with_snapshot_retry

        def _write() -> None:
            self._update_fixture_display_once(fixture_id)

        # WAL の snapshot 昇格衝突は新しい session の一回再試行で解く
        # (run_with_snapshot_retry の docstring 参照)
        run_with_snapshot_retry(
            _write, context=f"update_fixture_display fixture={fixture_id}",
        )

    def _update_fixture_display_once(self, fixture_id: str) -> None:
        """update_fixture_display の一回ぶんの transaction (再試行の単位)。"""
        db: Session = self.manager.SessionLocal()
        try:
            criteria = (
                Fixture.FIXTURE_ID == fixture_id,
                # 別 City の施設の STATE_JSON には書かない (City 境界 —
                # city_feed_fixture_ids の docstring 参照)
                Fixture.FIXTURE_ID.in_(
                    city_feed_fixture_ids(db, self.manager.city_id)
                ),
            )
            if db.query(Fixture.FIXTURE_ID).filter(*criteria).first() is None:
                return
            subs = (
                db.query(FeedSubscription)
                .filter(
                    FeedSubscription.FIXTURE_ID == fixture_id,
                    FeedSubscription.ENABLED == True,  # noqa: E712
                )
                .all()
            )
            sub_ids = [s.SUBSCRIPTION_ID for s in subs]
            latest_titles: List[str] = []
            if sub_ids:
                latest_items = (
                    db.query(FeedItem)
                    .filter(FeedItem.SUBSCRIPTION_ID.in_(sub_ids))
                    .order_by(FeedItem.PUBLISHED_AT.desc(), FeedItem.id.desc())
                    .limit(5)
                    .all()
                )
                # ペルソナのプロンプトに載る表示: 件数 5 件・各 100 文字まで。
                # 並びは配送の選択・items API と同一 (published desc [SQLite は
                # desc で NULL が末尾], id desc = 最新が先頭)。id desc 選択だと
                # newest-first フィードの初回取り込み (若い id ほど新しい) で
                # 実際の最新記事が 5 件から漏れる (二十巡目 V2)
                latest_titles = [
                    (it.TITLE or "")[:100] for it in latest_items
                ]

            display = {
                "subscriptions": [s.TITLE or "(無題のフィード)" for s in subs],
                "latest": latest_titles,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

            # "feed_stand" は STATE_JSON トップレベルの予約キー
            # (observer_manager.RESERVED_STATE_KEYS — metric 名としては拒否
            # される)。ここが唯一の書き手。
            if update_fixture_state_keys(
                db, criteria, {"feed_stand": display},
                context=f"update_fixture_display fixture={fixture_id}",
            ):
                db.commit()
            else:
                db.rollback()
        finally:
            db.close()

    def update_fixture_display_best_effort(self, fixture_id: str) -> None:
        """変更系操作の成功後に呼ぶ表示更新 (失敗は WARNING のみ)。

        本体 (Fixture・購読の commit) が成功した後に表示更新で例外が出ても、
        成功した変更を呼び出し側 (API 層) の 500 に見せない。STATE_JSON の
        表示は購読データからの派生物で、次の取得サイクル
        (_update_all_fixture_displays) が同じ内容を書き直して自己修復する。
        """
        try:
            self.update_fixture_display(fixture_id)
        except Exception:
            LOGGER.warning(
                "[feed] fixture display update failed after successful change: "
                "%s (display self-heals on the next fetch cycle)",
                fixture_id, exc_info=True,
            )

    def _update_all_fixture_displays(self) -> None:
        for fixture in self.list_feed_fixtures():
            try:
                self.update_fixture_display(fixture.FIXTURE_ID)
            except Exception:
                LOGGER.exception(
                    "[feed] failed to update fixture display: %s", fixture.FIXTURE_ID,
                )

    # ------------------------------------------------------------------
    # 購読 / Fixture CRUD (後続の API 層が呼ぶ)
    # ------------------------------------------------------------------

    def _require_city_building(self, db: Session, building_id: str) -> None:
        """INSERT と同一 transaction 内で「BUILDING_ID が現 City の実在
        Building」であることを検証する (City 所有権境界)。

        API 層のキャッシュ (manager.buildings) や別 transaction の事前確認は
        認可根拠にしない — 確認と INSERT の間に Building が消えたり所属が
        変わったりする変化に耐えるのは、実操作と同じ transaction の条件だけ。
        不在 (存在しない / 別 City) は LookupError (API 層は 404 に写像)。
        """
        exists = (
            db.query(Building.BUILDINGID)
            .filter(
                Building.BUILDINGID == building_id,
                Building.CITYID == self.manager.city_id,
            )
            .first()
        )
        if exists is None:
            raise LookupError(f"Building が見つかりません: {building_id}")

    def create_feed_fixture(
        self, building_id: str, name: str, description: str = "",
    ) -> Fixture:
        """フィード施設の Fixture (TYPE="feed_stand") を作成する。

        Building の実在検証 (現 City 所有 — _require_city_building) と INSERT
        は単一 transaction。新規 uuid の INSERT なので STATE_JSON の並走
        書き込みは存在しない (他の書き手はまだこの id を知らない)。
        """
        fixture_id = str(uuid.uuid4())
        db: Session = self.manager.SessionLocal()
        try:
            self._require_city_building(db, building_id)
            fixture = Fixture(
                FIXTURE_ID=fixture_id,
                BUILDING_ID=building_id,
                NAME=name,
                TYPE=FEED_FIXTURE_TYPE,
                DESCRIPTION=description,
                SOURCE_CONTEXT="feed_manager",
            )
            db.add(fixture)
            db.commit()
            db.refresh(fixture)
            db.expunge(fixture)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        LOGGER.info(
            "[feed] fixture created: %s (%s) in %s", name, fixture_id, building_id,
        )
        return fixture

    def create_fixture_from_preset(self, building_id: str, preset_id: str) -> Fixture:
        """プリセット (購読束 + 施設定義) からフィード施設を丸ごと作る。

        全か無か: プリセット内の全フィード URL を **施設を作る前に** 入口検査
        (feed_fetch._normalize_url — scheme 検査のみ、ネットワークは叩かない)
        に通し、1 本でも不正ならプリセット全体を ValueError で拒否する
        (API 層は 422)。Fixture 作成と全購読の作成は単一 transaction —
        途中で失敗しても「施設だけある」「購読が半分だけある」部分状態を
        残さない。

        新規 uuid の INSERT なので STATE_JSON の並走書き込みは存在しない
        (他の書き手はまだこの id を知らない)。
        """
        from saiverse import feed_presets

        preset = feed_presets.get_preset(preset_id)
        if preset is None:
            raise ValueError(f"フィードプリセットが見つかりません: {preset_id}")

        feeds: List[Dict[str, Optional[str]]] = []
        seen_urls: set = set()
        for feed in preset.get("feeds", []):
            url = (feed.get("url") or "").strip()
            if not url:
                continue
            try:
                _normalize_url(url)
            except FeedFetchError as exc:
                raise ValueError(
                    f"フィードプリセット {preset_id} に不正な URL が含まれて"
                    f"います ({url}): {exc}"
                ) from exc
            # 正規化後 URL の重複はここで排除する (最初の 1 本を採用)。
            # session は autoflush=False のため、同一 transaction 内で add
            # 済み・未 flush の購読は _add_subscription_in_session の
            # get-or-create 検索に見えず、重複はそのまま INSERT → 一意制約
            # 違反 (= プリセット全体が 500) になる。事前排除により重複が
            # transaction へ届く経路は到達不能になる。
            normalized = self._normalize_feed_url(url)
            if len(normalized) > FEED_URL_MAX_CHARS:
                # 保存側 (_add_subscription_in_session) と同じ上限を事前検査
                # にも掛ける — 全か無かの拒否を施設を作る前に判定するため
                raise ValueError(
                    f"フィードプリセット {preset_id} に長すぎる URL が含まれて"
                    f"います (正規化後 {len(normalized)} 文字 / "
                    f"上限 {FEED_URL_MAX_CHARS} 文字)"
                )
            if normalized in seen_urls:
                LOGGER.warning(
                    "[feed] preset %s contains duplicate feed URL "
                    "(normalized: %s); duplicate skipped", preset_id, normalized,
                )
                continue
            seen_urls.add(normalized)
            feeds.append({"url": url, "title": feed.get("title")})

        # 購読数上限の事前検証 (全か無かの拒否を施設を作る前に判定する)。
        # 一括作成の transaction 内では _add_subscription_in_session の
        # COUNT に add 済み・未 flush の購読が見えない (autoflush=False)
        # ため、上限はここでプリセットの本数に対して強制する。
        limit = self._read_max_subscriptions_env()
        if len(feeds) > limit:
            raise FeedSubscriptionLimitError(
                f"この施設の購読数が上限に達しています (フィードプリセット "
                f"{preset_id} は {len(feeds)} 件 / 上限 {limit} 件)"
            )

        fixture_id = str(uuid.uuid4())
        db: Session = self.manager.SessionLocal()
        try:
            # Building の実在検証 (現 City 所有) は INSERT と同一 transaction
            # (_require_city_building の docstring 参照)
            self._require_city_building(db, building_id)
            fixture = Fixture(
                FIXTURE_ID=fixture_id,
                BUILDING_ID=building_id,
                NAME=preset.get("fixture_name") or preset.get("name") or preset_id,
                TYPE=FEED_FIXTURE_TYPE,
                DESCRIPTION=(
                    preset.get("fixture_description")
                    or preset.get("description")
                    or ""
                ),
                SOURCE_CONTEXT="feed_manager",
            )
            db.add(fixture)
            db.flush()  # 購読の Fixture 存在確認が同一 session 内で通るように
            for feed in feeds:
                self._add_subscription_in_session(
                    db, fixture_id, feed["url"], title=feed["title"],
                )
            db.commit()
            db.refresh(fixture)
            db.expunge(fixture)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        LOGGER.info(
            "[feed] fixture created from preset %s: %s (%d subscription(s))",
            preset_id, fixture_id, len(feeds),
        )
        self.update_fixture_display_best_effort(fixture_id)
        return fixture

    def add_subscription(
        self,
        fixture_id: str,
        feed_url: str,
        *,
        site_url: Optional[str] = None,
        title: Optional[str] = None,
    ) -> FeedSubscription:
        """購読を 1 本追加する。

        購読の起点はプリセットかユーザーの明示操作のみ (不変条件 5) —
        本メソッドの呼び出し元がその境界を守る (記事の中身から呼ばない)。

        同一 Fixture に同一 URL (正規化後) の購読が既にあれば、新規作成せず
        既存の購読を返す (get-or-create)。UniqueConstraint(FIXTURE_ID, FEED_URL)
        が DB 側の最終防衛線。

        本メソッドは単発 API 用の自己完結 transaction。プリセット一括作成の
        ように呼び出し側の transaction に参加させたい場合は
        _add_subscription_in_session を使う。
        """
        db: Session = self.manager.SessionLocal()
        try:
            sub, created = self._add_subscription_in_session(
                db, fixture_id, feed_url, site_url=site_url, title=title,
            )
            if not created:
                db.expunge(sub)
                LOGGER.info(
                    "[feed] subscription already exists: %s (%s) on fixture %s",
                    sub.TITLE or sub.FEED_URL, sub.SUBSCRIPTION_ID, fixture_id,
                )
                return sub
            try:
                db.commit()
            except IntegrityError:
                # 同時 POST の収束: 事前検索の後に別リクエストが同じ
                # (FIXTURE_ID, FEED_URL) を先に INSERT すると、一意 index
                # (uq_feed_sub_fixture_url) がここで弾く。rollback して
                # 既存行を返す (get-or-create の DB 側最終防衛線と協調し、
                # API 層を 500 にしない)。
                db.rollback()
                existing = self._find_existing_subscription(
                    db, fixture_id, self._normalize_feed_url(feed_url),
                )
                if existing is None:
                    # 一意 index 以外の整合性違反 (FK 等) はそのまま表明する
                    raise
                db.expunge(existing)
                LOGGER.info(
                    "[feed] concurrent subscription insert converged: %s (%s) "
                    "on fixture %s",
                    existing.TITLE or existing.FEED_URL,
                    existing.SUBSCRIPTION_ID, fixture_id,
                )
                return existing
            # commit で属性が expire するため、session を閉じた後も呼び出し側が
            # 属性を読めるよう再ロードしてから切り離す
            db.refresh(sub)
            db.expunge(sub)
            LOGGER.info(
                "[feed] subscription added: %s (%s) on fixture %s",
                sub.TITLE or sub.FEED_URL, sub.SUBSCRIPTION_ID, fixture_id,
            )
            return sub
        finally:
            db.close()

    def _add_subscription_in_session(
        self,
        db: Session,
        fixture_id: str,
        feed_url: str,
        *,
        site_url: Optional[str] = None,
        title: Optional[str] = None,
    ) -> Tuple[FeedSubscription, bool]:
        """購読追加の中核。session は呼び出し側から渡され、commit しない
        (呼び出し側の transaction に参加する — プリセット一括作成の全か無か)。

        Returns: (購読, 新規作成したか)。同一 Fixture × 同一 URL (正規化後)
        の既存購読があれば (既存行, False) を返す (get-or-create)。注意:
        session は autoflush=False のため、同一 transaction 内で add 済み・
        未 flush の行はこの検索に**見えない** — プリセット内の重複 URL は
        create_fixture_from_preset 側の事前重複排除で先に落ちる (ここには
        届かない)。
        """
        if not feed_url or not feed_url.strip():
            raise ValueError("フィード URL が指定されていません。")
        normalized_url = self._normalize_feed_url(feed_url)
        if len(normalized_url) > FEED_URL_MAX_CHARS:
            raise ValueError(
                f"URLが長すぎます (最大{FEED_URL_MAX_CHARS}文字)。"
            )
        # Fixture の存在確認は「現 City の Building に属すること」まで含めて、
        # INSERT と同一 transaction 内で検証する (City 所有権境界)。API 層の
        # _fixture_or_404 は別 transaction の事前確認にすぎず、認可根拠は
        # ここ。別 City の Fixture は存在ごと伏せて「見つかりません」。
        fixture = (
            db.query(Fixture)
            .join(Building, Fixture.BUILDING_ID == Building.BUILDINGID)
            .filter(
                Fixture.FIXTURE_ID == fixture_id,
                Building.CITYID == self.manager.city_id,
            )
            .first()
        )
        if fixture is None:
            raise ValueError(f"Fixture が見つかりません: {fixture_id}")
        if fixture.TYPE != FEED_FIXTURE_TYPE:
            raise ValueError(
                f"フィード施設 (TYPE={FEED_FIXTURE_TYPE}) ではありません: "
                f"{fixture_id}"
            )
        existing = self._find_existing_subscription(db, fixture_id, normalized_url)
        if existing is not None:
            return existing, False
        # 購読数の上限 (SAIVERSE_FEED_MAX_SUBSCRIPTIONS_PER_FIXTURE) は
        # 新規作成時のみ数える — 既存 URL の再追加 (上の get-or-create) は
        # 上限到達後でも既存行を返して成功する。COUNT と INSERT は同一
        # transaction。並走 POST が COUNT と INSERT の間に滑り込むと僅かに
        # 超過しうるが受容する — 上限は資源保護の目安で、越境やデータ破壊を
        # 防ぐ境界ではない。
        limit = self._read_max_subscriptions_env()
        current = (
            db.query(func.count(FeedSubscription.SUBSCRIPTION_ID))
            .filter(FeedSubscription.FIXTURE_ID == fixture_id)
            .scalar()
        ) or 0
        if current >= limit:
            raise FeedSubscriptionLimitError(
                f"この施設の購読数が上限に達しています (上限 {limit} 件)"
            )
        site = (site_url or "").strip()
        if len(site) > FEED_URL_MAX_CHARS:
            # site_url は外部由来 (フィードの channel link) の表示用メタデータ。
            # 宣言長超過はエラーにせず安全に破棄する — 無くても購読は成立し、
            # 外部の値 1 つで購読追加全体を失敗させない。切り詰めは別資源を
            # 指す壊れリンクになるのでしない。
            LOGGER.warning(
                "[feed] site_url exceeds %d chars; dropped (fixture=%s)",
                FEED_URL_MAX_CHARS, fixture_id,
            )
            site = ""
        sub = FeedSubscription(
            SUBSCRIPTION_ID=str(uuid.uuid4()),
            FIXTURE_ID=fixture_id,
            FEED_URL=normalized_url,
            SITE_URL=site or None,
            TITLE=(title or "").strip()[:_SUB_TITLE_MAX_CHARS],
        )
        db.add(sub)
        return sub, True

    @staticmethod
    def _find_existing_subscription(
        db: Session, fixture_id: str, normalized_url: str,
    ) -> Optional[FeedSubscription]:
        """同一 Fixture × 同一 URL (正規化後) の既存購読を検索する。"""
        return (
            db.query(FeedSubscription)
            .filter(
                FeedSubscription.FIXTURE_ID == fixture_id,
                FeedSubscription.FEED_URL == normalized_url,
            )
            .first()
        )

    @staticmethod
    def _normalize_feed_url(feed_url: str) -> str:
        """購読 URL の同一性判定のための正規化 (軽量)。

        scheme/ホストの小文字化、既定ポート (http の :80 / https の :443) の
        除去、path 末尾スラッシュの除去のみ。query はフィードの識別に意味を
        持ちうるので保持し (並び替えもしない — パラメータ順が意味を持つ場合が
        あるため)、fragment は落とす。
        """
        url = feed_url.strip()
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        parts = urlparse(url)
        scheme = parts.scheme.lower()
        netloc = parts.netloc.lower()
        default_port = {"http": ":80", "https": ":443"}.get(scheme)
        if default_port and netloc.endswith(default_port):
            netloc = netloc[: -len(default_port)]
        return urlunparse((
            scheme,
            netloc,
            parts.path.rstrip("/"),
            parts.params,
            parts.query,
            "",
        ))

    def remove_subscription(self, subscription_id: str) -> bool:
        """購読を削除する (記事・既読カーソルも道連れ)。

        City 所有権境界 (「現 City のフィード施設に属する購読」) の条件は
        購読本体の DELETE 文自身が運ぶ — 「選別 SELECT → 別クエリで削除」の
        二段だと、選別と削除の間に施設の所有が別 City へ移った購読を消して
        しまう (事前確認を認可根拠にしない — 十四巡目 S2)。道連れ (記事・
        カーソル) は本体 DELETE が行を消せた場合のみ、同一 transaction 内で
        削除する (先に子を消すと、本体が 0 行だったとき所有していない購読の
        子だけが消える)。別 City の購読は「該当なし」として False。

        Returns: 削除したら True、該当が無ければ False。
        """
        db: Session = self.manager.SessionLocal()
        try:
            deleted = (
                db.query(FeedSubscription)
                .filter(
                    FeedSubscription.SUBSCRIPTION_ID == subscription_id,
                    FeedSubscription.FIXTURE_ID.in_(
                        city_feed_fixture_ids(db, self.manager.city_id)
                    ),
                )
                .delete(synchronize_session=False)
            )
            if not deleted:
                db.rollback()
                return False
            db.query(FeedItem).filter(
                FeedItem.SUBSCRIPTION_ID == subscription_id
            ).delete(synchronize_session=False)
            db.query(FeedReadCursor).filter(
                FeedReadCursor.SUBSCRIPTION_ID == subscription_id
            ).delete(synchronize_session=False)
            db.commit()
            LOGGER.info("[feed] subscription removed: %s", subscription_id)
            return True
        finally:
            db.close()

    def list_feed_fixtures(self) -> List[Fixture]:
        """現 City のフィード施設 (TYPE="feed_stand") の Fixture 一覧。"""
        db: Session = self.manager.SessionLocal()
        try:
            return (
                db.query(Fixture)
                .filter(
                    Fixture.FIXTURE_ID.in_(
                        city_feed_fixture_ids(db, self.manager.city_id)
                    ),
                )
                .all()
            )
        finally:
            db.close()

    def list_subscriptions(self, fixture_id: str) -> List[FeedSubscription]:
        """Fixture 1 つの購読一覧 (ENABLED 含む全件)。

        別 City の Fixture を渡されたら空を返す (City 境界 —
        city_feed_fixture_ids の docstring 参照)。
        """
        db: Session = self.manager.SessionLocal()
        try:
            return (
                db.query(FeedSubscription)
                .filter(
                    FeedSubscription.FIXTURE_ID == fixture_id,
                    FeedSubscription.FIXTURE_ID.in_(
                        city_feed_fixture_ids(db, self.manager.city_id)
                    ),
                )
                .all()
            )
        finally:
            db.close()
