"""EventScheduler — 全イベントの時刻指定ディスパッチャ (Phase 4-e)。

Intent A v0.10 / Intent B v0.7 の「メタレイヤーの定期実行入口」に対する
最終形の実装。固定間隔ポーリングを全廃し、min-heap + ``Condition.wait_until``
で **次に発火すべきイベントの時刻まで sleep する push 駆動** を実現する。

集約対象 (Phase 4-e で統合):
- メタ判断 Pulse の TTL 接近前倒し (anchor touch から push)
- メタ判断 Pulse の interval 経過 (judgment 完了から push)
- AUTONOMY_ENABLED=True 化時の即時発火
- ペルソナスケジュール (PersonaSchedule の発火時刻)
- 内部 alert (Track 状態の遅延通知)
- inter-city DB poll (VisitingAI / ThinkingRequest)
- SDS heartbeat

addon 由来のポーリング (X 監視等) は本 Phase ではスコープ外。
詳細: ``docs/issues/addon_event_scheduler_integration.md``。

スレッドの形 (v0.4 自律運転 §8.2 決定 A、2026-10-12):
- dispatch スレッドは「時刻が来たら手渡す」だけをする。callback は発火ごとに
  起こす専用ワーカースレッド (daemon) で実行する。callback がロック待ち・DB
  書き込み・LLM 呼び出しで数分ブロックしても、他の予約 (全ペルソナのアラーム・
  keep-alive・会話の沈黙タイマー・回復の見回り) の発火を遅らせない。
- 同時刻の別 key の callback の実行順序は保証しない (別々のワーカーで並走しうる)。
  ペルソナ内の直列性は席 (PulseController) と Beat ロックが、同じ発火の二重実行は
  実行台帳の claim が持つ — 発火装置の単一スレッドに直列性を頼らないこと。
- 仮想クロックの同期駆動 (:meth:`EventScheduler.run_due`) だけは従来どおり
  呼び出しスレッド上で同期実行する (シミュレーションの決定性が目的の API)。

責務外:
- リトライポリシー (callback 側のドメイン責務)
- callback の例外による再試行 (WARN ログ + 該当予約消去のみ)
  (旧「Alert 観察ルート」の記述は、alert 状態機械の退役で対象消滅した)

仮想クロック対応 (自律行動 v2 §12):
- 時刻の読み出しは ``saiverse.clock.now()`` に一元化 (実モードでは挙動不変)
- 仮想モード中は dispatch スレッドが発火せず、同期駆動 API
  (``next_fire_time`` / ``run_due``) のみが実行経路になる。
  旧 DES ドライバ (``saiverse.day_simulator``) は v0.4 段 1-4 で撤去 (ティック用の
  早回しは段 3 で作り直す)。
"""
from __future__ import annotations

import heapq
import itertools
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Dict, Optional

from saiverse import clock

LOGGER = logging.getLogger(__name__)


@dataclass(order=True)
class _Entry:
    """min-heap に積むエントリ。

    fire_at_ts (epoch float) で大小比較する。同時刻は seq で安定化。
    callback と key は比較対象から除外 (dataclass の order=True 用に compare=False)。
    """
    fire_at_ts: float
    seq: int
    key: str = field(compare=False)
    callback: Callable[[], None] = field(compare=False)
    cancelled: bool = field(default=False, compare=False)


class EventScheduler:
    """汎用時刻ディスパッチャ。

    使い方:
        scheduler = EventScheduler()
        scheduler.start()
        scheduler.schedule(
            fire_at=clock.now() + timedelta(seconds=30),
            callback=lambda: print("fired"),
            key="my_event",
        )
        # ... 30 秒後に "fired" が表示される
        scheduler.stop()

    同じ key で再 schedule すると古い予約は cancel される (上書き)。
    ``fire_at`` は ``saiverse.clock.now()`` 基準で計算すること (期限の比較が
    clock.now() で行われるため。``datetime.now()`` で刻むと仮想クロックの
    シミュレーション中に期限がシム終了後へ飛ぶ)。

    callback の実行スレッドの契約 (実モード = dispatch スレッド経由):
    - callback は発火ごとの専用ワーカースレッド (daemon) で実行される。
      ブロックしてよく、他の予約の発火を遅らせない。
    - 同時刻の別 key の callback の実行順序は保証しない。
    - 例外は WARN ログで吸収され、他の予約に影響しない。
    - :meth:`stop` は走行中のワーカーを join しない (daemon のまま放置)。
    仮想クロックの同期駆動 (:meth:`run_due`) では呼び出しスレッド上で同期実行される。
    """

    def __init__(self) -> None:
        self._heap: list[_Entry] = []
        # key → 最新 _Entry の参照 (古い同 key エントリを cancel するため)。
        self._entries_by_key: Dict[str, _Entry] = {}
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # heap 内エントリの順序を安定化させるための単調増加カウンタ
        self._seq_counter = itertools.count()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """dispatch スレッドを起動する。既に起動済みなら何もしない。"""
        if self._thread and self._thread.is_alive():
            LOGGER.warning("[event_scheduler] already running")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._dispatch_loop,
            name="EventScheduler",
            daemon=True,
        )
        self._thread.start()
        LOGGER.info("[event_scheduler] started")

    def stop(self) -> None:
        """dispatch スレッドを停止する。pending callback は破棄される。

        既にワーカースレッドへ手渡された (走行中の) callback は join しない —
        daemon スレッドとして走り切るか、プロセス終了とともに消える。
        """
        self._stop_event.set()
        with self._cond:
            self._cond.notify_all()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        LOGGER.info("[event_scheduler] stopped")

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    def schedule_if_absent(
        self,
        fire_at: datetime,
        callback: Callable[[], None],
        key: str,
    ) -> bool:
        """有効な予約が **無いときだけ** 登録する (既にあれば何もしない)。

        :meth:`schedule` との違いは上書きしないことだけで、判定と登録を同じロック
        区間で行う点が肝 (2026-07-29 追加)。``has_key`` で確認してから ``schedule``
        を呼ぶ形 (check-then-act) では、その隙間に別スレッドが入れた予約を上書き
        してしまう。用途は「失われた予約を復旧する」操作 — 復旧は穴を埋める仕事で
        あって、現に生きている予約を置き換える仕事ではない。

        Returns:
            登録したら True、既存の有効な予約があって何もしなかったら False。
        """
        fire_ts = fire_at.timestamp()
        with self._cond:
            existing = self._entries_by_key.get(key)
            if existing is not None and not existing.cancelled:
                LOGGER.debug(
                    "[event_scheduler] schedule_if_absent key=%s skipped "
                    "(already armed)", key,
                )
                return False

            entry = _Entry(
                fire_at_ts=fire_ts,
                seq=next(self._seq_counter),
                key=key,
                callback=callback,
            )
            self._entries_by_key[key] = entry
            heapq.heappush(self._heap, entry)
            LOGGER.debug(
                "[event_scheduler] schedule_if_absent key=%s fire_at=%s (in %.1fs)",
                key, fire_at.isoformat(timespec="seconds"),
                fire_ts - clock.now().timestamp(),
            )
            self._cond.notify()
            return True

    def schedule_if_present(
        self,
        fire_at: datetime,
        callback: Callable[[], None],
        key: str,
    ) -> bool:
        """有効な予約が **あるときだけ** 時刻を置き換える (無ければ何もしない)。

        :meth:`schedule_if_absent` の対。判定と置き換えを同じロック区間で行う —
        ``has_key`` で確かめてから ``schedule`` を呼ぶ形 (check-then-act) では、
        その隙間に別スレッドが消した予約を生き返らせてしまう。用途は「生きて
        いる予約だけを後ろへずらす」操作 (例: ティックの予約を最後の標準呼び出し
        から T 分へ押し直す — 予約が無い = ライフ外では生やさない)。

        Returns:
            置き換えたら True、有効な予約が無くて何もしなかったら False。
        """
        fire_ts = fire_at.timestamp()
        with self._cond:
            existing = self._entries_by_key.get(key)
            if existing is None or existing.cancelled:
                return False
            existing.cancelled = True
            entry = _Entry(
                fire_at_ts=fire_ts,
                seq=next(self._seq_counter),
                key=key,
                callback=callback,
            )
            self._entries_by_key[key] = entry
            heapq.heappush(self._heap, entry)
            LOGGER.debug(
                "[event_scheduler] schedule_if_present key=%s fire_at=%s (in %.1fs)",
                key, fire_at.isoformat(timespec="seconds"),
                fire_ts - clock.now().timestamp(),
            )
            self._cond.notify()
            return True

    def fire_time_of(self, key: str) -> Optional[datetime]:
        """指定 key の有効な予約の発火時刻 (無ければ None)。"""
        with self._cond:
            entry = self._entries_by_key.get(key)
            if entry is None or entry.cancelled:
                return None
            return datetime.fromtimestamp(entry.fire_at_ts)

    def schedule(
        self,
        fire_at: datetime,
        callback: Callable[[], None],
        key: str,
    ) -> None:
        """指定時刻に callback を発火する予約を入れる。

        Args:
            fire_at: 発火時刻 (naive datetime, ローカルタイム想定)。
                過去時刻を渡すと即座に発火対象になる。``saiverse.clock.now()``
                基準で計算すること (``datetime.now()`` は仮想クロックとずれる)。
            callback: 発火時に呼ばれる関数。引数なし、戻り値無視。
                実モードでは発火ごとの専用ワーカースレッドで実行される
                (ブロックしてよい。他の予約を遅らせない)。
                例外を投げてもよい (WARN ログ + 該当予約消去で吸収)。
            key: 予約識別子。同じ key で再 schedule すると古い予約は cancel される。
                慣習: ``"<source>:<id>"`` (例 ``"ttl:air_city_a"``)。
                **既存を残したい場合は** :meth:`schedule_if_absent` を使う。
        """
        fire_ts = fire_at.timestamp()
        with self._cond:
            # 既存エントリがあれば cancel フラグを立てる (heap からの物理削除はせず、
            # dispatch ループで cancelled エントリをスキップする lazy deletion 方式)。
            old = self._entries_by_key.get(key)
            if old is not None:
                old.cancelled = True

            entry = _Entry(
                fire_at_ts=fire_ts,
                seq=next(self._seq_counter),
                key=key,
                callback=callback,
            )
            self._entries_by_key[key] = entry
            heapq.heappush(self._heap, entry)

            LOGGER.debug(
                "[event_scheduler] schedule key=%s fire_at=%s (in %.1fs)",
                key, fire_at.isoformat(timespec="seconds"),
                fire_ts - clock.now().timestamp(),
            )

            # heap の先頭が変わった可能性があるので dispatch ループを起こす。
            self._cond.notify()

    def cancel(self, key: str) -> bool:
        """予約をキャンセルする。

        Returns:
            該当する key の予約があってキャンセルした場合 True、なかった場合 False。
        """
        with self._cond:
            entry = self._entries_by_key.pop(key, None)
            if entry is None:
                return False
            entry.cancelled = True
            LOGGER.debug("[event_scheduler] cancel key=%s", key)
            # cancel 後、heap 先頭が長く待たされていれば、不要な wake は不要。
            # ただし heap 先頭が cancelled になった場合、dispatch ループは
            # 起きてからスキップして次のエントリを見るだけなので問題なし。
            return True

    def cancel_prefix(self, prefix: str) -> list[str]:
        """key が prefix で始まる有効な予約を全てキャンセルし、key のリストを返す。

        (persona, model) 単位の予約 key (例 ``ttl:{persona_id}:{model_key}``) を
        「その persona の全 model 分」まとめて止めるための入口。呼び出し側が
        model を列挙できない (どの model の Session が見張り中か知らない) 場面で
        使う — 例: ライフ終端の keep-alive 一括 cancel
        (``saiverse.day_plan._cancel_keepalive_reservation``)。
        """
        with self._cond:
            keys = [
                key for key, entry in self._entries_by_key.items()
                if key.startswith(prefix) and not entry.cancelled
            ]
            for key in keys:
                entry = self._entries_by_key.pop(key)
                entry.cancelled = True
            if keys:
                LOGGER.debug(
                    "[event_scheduler] cancel_prefix=%s: %s",
                    prefix, ", ".join(sorted(keys)),
                )
            return keys

    def cancel_all(self) -> list[str]:
        """有効な予約を全てキャンセルし、キャンセルした key のリストを返す。

        一日シミュレータ (DES) がシナリオ実行前に、manager 構築時に積まれた
        実時刻由来の予約 (db_polling / SDS heartbeat 等の定期イベント) を
        一括除去するための入口 (自律行動 v2 §12)。仮想クロック下では実時刻で
        シードされた 3 秒周期イベントが数千ステップの空回りになるため、
        シナリオ由来のイベントだけを回す前提を「シム側が」成立させる。
        本番経路 (dispatch スレッド運転) からは呼ばれない。
        """
        with self._cond:
            keys = [
                key for key, entry in self._entries_by_key.items()
                if not entry.cancelled
            ]
            for entry in self._entries_by_key.values():
                entry.cancelled = True
            self._entries_by_key.clear()
            if keys:
                LOGGER.info(
                    "[event_scheduler] cancel_all: %d reservations cancelled (%s)",
                    len(keys), ", ".join(sorted(keys)),
                )
            return keys

    def schedule_periodic(
        self,
        interval_seconds: float,
        callback: Callable[[], None],
        key: str,
        *,
        first_fire_immediate: bool = False,
    ) -> None:
        """周期的に callback を呼ぶラッパー。

        callback 完了後、自動的に次回を ``interval_seconds`` 後に再 schedule する。
        callback が例外を投げた場合は **周期処理を停止する** (再 schedule しない)。
        無限ログ汚染防止のため。再開したければ呼び出し側で再度 schedule_periodic を呼ぶ。

        再 schedule は callback の完了後に行うので、同じ key の周期 callback が
        ワーカースレッド上で重なって走ることはない (前回が終わるまで次回は積まれない)。

        Args:
            interval_seconds: 呼び出し間隔 (秒)。callback の実行時間は含まない。
            callback: 周期的に呼ぶ関数。
            key: 予約識別子。同じ key で上書き登録される。
            first_fire_immediate: True なら最初の発火を即時行う (interval_seconds を待たない)。
        """
        def wrapper() -> None:
            try:
                callback()
            except Exception:
                LOGGER.exception(
                    "[event_scheduler] periodic callback raised; "
                    "stopping periodic schedule for key=%s", key,
                )
                return
            # 成功時のみ次回を schedule
            self.schedule(
                fire_at=clock.now() + timedelta(seconds=interval_seconds),
                callback=wrapper,
                key=key,
            )

        first_fire_at = (
            clock.now()
            if first_fire_immediate
            else clock.now() + timedelta(seconds=interval_seconds)
        )
        self.schedule(fire_at=first_fire_at, callback=wrapper, key=key)

    # ------------------------------------------------------------------
    # Dispatch loop
    # ------------------------------------------------------------------

    def _dispatch_loop(self) -> None:
        """heap 先頭の fire_at まで wait し、時刻到来で callback を発火するメインループ。"""
        LOGGER.info("[event_scheduler] dispatch loop started")
        while not self._stop_event.is_set():
            with self._cond:
                if clock.is_virtual():
                    # 仮想モード中: 背景スレッドは発火しない。
                    # 実行経路は同期駆動 API (next_fire_time / run_due) のみ。
                    # 短い timeout で再確認し、disable_virtual 後に自然復帰する。
                    self._cond.wait(timeout=0.5)
                    continue

                # キャンセル済みのエントリを heap 先頭からスキップ
                while self._heap and self._heap[0].cancelled:
                    heapq.heappop(self._heap)

                if not self._heap:
                    # 何も予約がない: 新規予約か stop シグナルまで待つ
                    self._cond.wait()
                    continue

                next_entry = self._heap[0]
                now_ts = clock.now().timestamp()
                wait_seconds = next_entry.fire_at_ts - now_ts

                if wait_seconds > 0:
                    # まだ時刻未到来。新規予約 (より早い fire_at) で起こされるか
                    # stop シグナルまで wait。
                    self._cond.wait(timeout=wait_seconds)
                    continue

                # 時刻到来: heap から取り出して発火準備
                heapq.heappop(self._heap)
                # _entries_by_key から自分自身を削除 (cancel/再 schedule の race 回避)
                if self._entries_by_key.get(next_entry.key) is next_entry:
                    self._entries_by_key.pop(next_entry.key, None)

            # Lock を抜けた状態で、callback をワーカースレッドへ手渡す。
            # dispatch スレッド上で callback を実行すると、callback が LLM 呼び出し
            # 等でブロックしている間、全ペルソナの全予約が発火できない (§8.1 の欠陥)。
            self._hand_off(next_entry)

        LOGGER.info("[event_scheduler] dispatch loop ended")

    def _hand_off(self, entry: _Entry) -> None:
        """期限の来た entry を発火ごとの専用ワーカースレッドへ手渡す (実モード経路)。

        ワーカーは daemon で、``stop()`` は join しない。例外の吸収は ``_fire`` が
        ワーカー上で行う。
        """
        if entry.cancelled:
            # 取り出した直後にキャンセルされたケース。スレッドを起こすまでもない。
            return
        worker = threading.Thread(
            target=self._fire,
            args=(entry,),
            name=f"evt-fire:{entry.key}",
            daemon=True,
        )
        try:
            worker.start()
        except RuntimeError:
            # スレッドを起こせない (資源枯渇)。予約を黙って失うより、従来の
            # 同期実行で発火させる方を取る — この発火のあいだ dispatch は止まるが、
            # 周期予約 (schedule_periodic) の連鎖が切れるよりは被害が小さい。
            LOGGER.exception(
                "[event_scheduler] failed to start worker thread; firing key=%s "
                "synchronously on the dispatch thread", entry.key,
            )
            self._fire(entry)

    def _fire(self, entry: _Entry) -> None:
        """callback を呼び、例外を WARN ログで吸収する。

        実モードではワーカースレッド上で、``run_due`` では呼び出しスレッド上で走る。
        """
        if entry.cancelled:
            # 取り出した直後にキャンセルされたケース。callback は呼ばない。
            return
        try:
            entry.callback()
        except Exception:
            LOGGER.exception(
                "[event_scheduler] callback raised; dropping key=%s",
                entry.key,
            )

    # ------------------------------------------------------------------
    # Synchronous drive (virtual-clock simulation; 自律行動 v2 §12)
    # ------------------------------------------------------------------

    def next_fire_time(self) -> Optional[datetime]:
        """キューの次イベント (有効な予約のうち最も早いもの) の発火時刻を返す。

        予約がなければ None。DES ドライバが「次にどこまで時計をワープするか」
        を決めるために使う。
        """
        with self._cond:
            while self._heap and self._heap[0].cancelled:
                heapq.heappop(self._heap)
            if not self._heap:
                return None
            return datetime.fromtimestamp(self._heap[0].fire_at_ts)

    def run_due(self, now: datetime) -> int:
        """``now`` 以前が期限のイベントを期限順に同期実行し、実行数を返す。

        シムモードの実行経路。呼び出しスレッド上で callback を直接実行する
        (dispatch スレッドは経由しない)。callback 内で新たに schedule された
        イベントも、期限が ``now`` 以前なら同じ呼び出しで消化する。

        注意: callback が「期限 <= now」の予約を無限に積み続けると本メソッドは
        返らない (例: ``schedule_periodic(interval_seconds=0)``)。DES では
        派生イベントは必ず未来時刻に積むこと。

        Args:
            now: 期限判定の基準時刻 (通常は ``clock.now()`` の仮想時刻)。

        Returns:
            実行した callback の数。
        """
        now_ts = now.timestamp()
        executed = 0
        while True:
            with self._cond:
                while self._heap and self._heap[0].cancelled:
                    heapq.heappop(self._heap)
                if not self._heap or self._heap[0].fire_at_ts > now_ts:
                    break
                entry = heapq.heappop(self._heap)
                # _entries_by_key から自分自身を削除 (cancel/再 schedule の race 回避)
                if self._entries_by_key.get(entry.key) is entry:
                    self._entries_by_key.pop(entry.key, None)

            LOGGER.debug(
                "[event_scheduler] run_due fire key=%s (due=%s, now=%s)",
                entry.key,
                datetime.fromtimestamp(entry.fire_at_ts).isoformat(timespec="seconds"),
                now.isoformat(timespec="seconds"),
            )
            # Lock を抜けた状態で callback を実行 (callback 内で再 schedule 可能にする)
            self._fire(entry)
            executed += 1
        return executed

    # ------------------------------------------------------------------
    # Introspection (debug / tests)
    # ------------------------------------------------------------------

    def pending_count(self) -> int:
        """現在の有効な予約数 (cancelled でないもの)。"""
        with self._cond:
            return sum(1 for e in self._heap if not e.cancelled)

    def has_key(self, key: str) -> bool:
        """指定 key の有効な予約があるか。"""
        with self._cond:
            entry = self._entries_by_key.get(key)
            return entry is not None and not entry.cancelled
