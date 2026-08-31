"""Observer Manager — Fixture/Observer の登録・定期実行・push 受信・通知を管理する。

Observer は Building に固定設置された Fixture の一種で、定期的にツール/Playbook を
実行 (pull 型) するか、外部アプリから HTTP push でデータを受信 (push 型) する。
観測値は observer_metrics テーブルに時系列蓄積し、最新値を fixture.STATE_JSON に
キャッシュする。閾値超過 / 大変動を検知して Building 内に通知する。

詳細: docs/intent/observer.md
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from sqlalchemy import case, desc, func
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from database.models import Fixture, ObserverConfig, ObserverMetric

if TYPE_CHECKING:
    from saiverse.saiverse_manager import SAIVerseManager

LOGGER = logging.getLogger(__name__)

# Fixture.STATE_JSON は複数の書き手のキーがトップレベルで共存する JSON 列
# (record_metrics の metric 名キー / feed_manager.update_fixture_display の
# feed_stand キー)。更新は SQLite JSON1 の json_set による**単文 UPDATE**
# (update_fixture_state_keys) に統一する — 自分のキーだけを DB 側で原子的に
# 差し替え、他のキーは UPDATE 文自身が現在値から引き継ぐため、Python 側に
# read-modify-write の窓が存在しない。プロセス内のスレッド並走も、同一 DB を
# 共有する別プロセス (multi-city 構成) の並走も、同じ一文が守る (旧方式の
# threading.Lock + 楽観 CAS リトライは 2026-08-03 に機構ごと撤去)。
# 置き場所がここなのは、STATE_JSON キャッシュの本家が ObserverManager のため。
# 将来 STATE_JSON の書き手が増えたら、必ず update_fixture_state_keys を通す。

# STATE_JSON のトップレベルキーとして許す形 (英数字・アンダースコア・
# ハイフンのみ)。キーは json_set の JSON パス ('$."<key>"') に埋め込まれる
# ため、引用符・バックスラッシュ・ドット等を含む名前はパス注入や解釈ズレの
# 口になる — 埋め込む前にこの形へ検証して構造的に塞ぐ。
STATE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

# STATE_JSON のトップレベルで observer metric 以外の書き手が使う予約キー。
# record_metrics は metric 名としてこれらを拒否する (WARNING + skip)。
# 既存レイアウト (metric 名がトップレベルに並ぶ) の移行はしない — 本番の
# STATE_JSON は metric 名がトップレベルにある既存契約で、名前空間を分ける
# 変更は表示系全体の migration になるため、予約ガードで衝突だけを塞ぐ。
RESERVED_STATE_KEYS = frozenset({"feed_stand"})


#: 一時的なロック/スナップショット競合と判定する SQLite エラーメッセージ片。
#: これ**以外**の OperationalError (JSON1 不在・スキーマ不整合・ディスク障害
#: 等の恒久障害) は再試行せず伝播する — 無差別再試行は根因を「一時的競合」と
#: 誤記して隠すため (Codex 十七巡目)。
_TRANSIENT_SQLITE_MARKERS = (
    "database is locked",
    "database table is locked",
    "snapshot",  # SQLITE_BUSY_SNAPSHOT
)


def _is_transient_sqlite_error(exc: OperationalError) -> bool:
    text = str(getattr(exc, "orig", None) or exc).lower()
    return any(marker in text for marker in _TRANSIENT_SQLITE_MARKERS)


class SnapshotRetryExhausted(Exception):
    """一時的競合の再試行が枯渇した (二連敗)。

    呼び出し側が「見送ってよい失敗」として捕捉するための専用型。恒久障害
    (JSON1 不在・スキーマ不整合等) はこの型にならず OperationalError の
    まま伝播する — 外側で OperationalError を一括捕捉すると分類が無意味に
    なる (Codex 十八巡目)。"""


def run_with_snapshot_retry(operation, *, context: str):
    """STATE_JSON 書き込み transaction を一時的競合から一回だけ救う。

    WAL では「読み取りで始まった transaction が書き込みへ昇格する」とき、
    スナップショット取得後に別 writer が commit していると OperationalError
    (SQLITE_BUSY_SNAPSHOT / database is locked) になり、busy_timeout では
    解決しない (古いスナップショットは待っても新しくならない)。operation は
    **呼ばれるたびに自前で新しい session を開いて読み直す**契約 — 新しい
    session なら新しいスナップショットで昇格できる。

    再試行するのは一時的競合 (_TRANSIENT_SQLITE_MARKERS) だけ。恒久障害は
    そのまま伝播する。また commit 結果が不明のまま再実行される可能性が
    あるため、operation は**再実行しても二重書きにならない冪等な作り**に
    すること (record_metrics は (observer, recorded_at) の同キー行を
    書き直す形で満たす)。
    """
    try:
        return operation()
    except OperationalError as exc:
        if not _is_transient_sqlite_error(exc):
            raise
        LOGGER.warning(
            "[fixture-state] write transaction hit a transient lock/snapshot "
            "conflict; retrying once with a fresh session (%s)", context,
        )
        try:
            return operation()
        except OperationalError as exc2:
            if not _is_transient_sqlite_error(exc2):
                raise
            raise SnapshotRetryExhausted(context) from exc2


def update_fixture_state_keys(
    db: Session, criteria, updates: Dict[str, Any], *, context: str,
) -> bool:
    """Fixture.STATE_JSON の指定キーだけを DB 側で原子的に更新する。

    SQLite JSON1 の json_set による単文 UPDATE:
    ``STATE_JSON = json_set(<現在値 or '{}'>, '$."key1"', json(?), ...)``。
    他のキーは UPDATE 文自身が現在値から引き継ぐため、プロセス内外のどの
    並走書き込みとも「古いスナップショットで他キーを消す」事故が構造的に
    起きない。既存値が NULL / 壊れた JSON / オブジェクト以外なら '{}' を
    土台に作り直す (壊れ値の自己修復)。

    Args:
        db: 呼び出し側の session。**commit はしない** — 同一 transaction に
            他の書き込み (メトリクス行等) を相乗りさせられるように、commit は
            呼び出し側の責任。
        criteria: 対象行を特定する filter 条件の並び (FIXTURE_ID 一致 +
            必要なら City 境界)。
        updates: ``{キー名: JSON 化可能な値}``。キー名は STATE_KEY_PATTERN を
            満たすこと — 満たさなければ ValueError (json_set パス注入の関所。
            外部由来の名前は呼び出し側が先に検証して落とす)。
        context: ValueError に載せる呼び出し元の説明。

    Returns:
        UPDATE が行に当たったら True。対象行が無ければ False (静かに skip)。
    """
    if not updates:
        return False
    args: List[Any] = []
    for key, value in updates.items():
        if not STATE_KEY_PATTERN.fullmatch(key):
            raise ValueError(
                f"invalid STATE_JSON key for json_set path ({context}): {key!r}"
            )
        args.append(f'$."{key}"')
        args.append(func.json(json.dumps(value, ensure_ascii=False)))
    # 土台の選択: json_set は壊れた JSON を渡すと文ごとエラーになるため、
    # 「有効な JSON かつオブジェクト」のときだけ現在値を使い、それ以外
    # (NULL / 壊れ値 / 配列などの非オブジェクト) は '{}' から作り直す。
    # json_type も壊れた JSON でエラーになるので、json_valid が真のときに
    # 限って評価する入れ子 CASE にする (AND の短絡評価には頼らない)。
    base = case(
        (
            func.json_valid(Fixture.STATE_JSON) == 1,
            case(
                (func.json_type(Fixture.STATE_JSON) == "object",
                 Fixture.STATE_JSON),
                else_="{}",
            ),
        ),
        else_="{}",
    )
    updated = (
        db.query(Fixture)
        .filter(*criteria)
        .update(
            {Fixture.STATE_JSON: func.json_set(base, *args)},
            synchronize_session=False,
        )
    )
    return bool(updated)


class ObserverManager:
    """Observer のライフサイクルと実行を管理する。"""

    def __init__(self, manager: "SAIVerseManager") -> None:
        self.manager = manager

    # ------------------------------------------------------------------
    # Fixture CRUD
    # ------------------------------------------------------------------

    def create_fixture(
        self,
        fixture_id: str,
        building_id: str,
        name: str,
        *,
        fixture_type: str = "object",
        description: str = "",
        state_json: Optional[str] = None,
        file_path: Optional[str] = None,
        creator_id: Optional[str] = None,
        source_context: Optional[str] = None,
    ) -> Fixture:
        """Fixture を upsert する (同一 ID なら既存行を更新)。

        ``state_json`` は「省略 (None) = 既存の STATE_JSON を変更しない」の
        upsert 意味論。STATE_JSON には複数の書き手のキーが共存しており
        (record_metrics の metrics キー / feed_manager.
        update_fixture_display の feed_stand キー)、None のまま丸ごと書くと
        既存 fixture への upsert で他の書き手のキーが消える。明示的に渡した
        場合のみ全置換する。

        保持経路 (state_json=None) は STATE_JSON 列に**触れない** UPDATE で
        実現する — 旧実装の「既存値を読んで書き戻す」だと read-modify-write
        になり、読みと書きの間の他者の更新 (キー単位の json_set — 冒頭の
        コメント参照) を古い値で潰す。書かないものは並走と衝突しない。
        """
        db: Session = self.manager.SessionLocal()
        try:
            existing = (
                db.query(Fixture)
                .filter(Fixture.FIXTURE_ID == fixture_id)
                .first()
            )
            if existing is not None:
                existing.BUILDING_ID = building_id
                existing.NAME = name
                existing.TYPE = fixture_type
                existing.DESCRIPTION = description
                existing.FILE_PATH = file_path
                existing.CREATOR_ID = creator_id
                existing.SOURCE_CONTEXT = source_context
                if state_json is not None:
                    existing.STATE_JSON = state_json  # 明示指定のみ全置換
                else:
                    # 返り値用の読み出しのみ (列への書き戻しはしない)
                    state_json = existing.STATE_JSON
            else:
                db.add(Fixture(
                    FIXTURE_ID=fixture_id,
                    BUILDING_ID=building_id,
                    NAME=name,
                    TYPE=fixture_type,
                    DESCRIPTION=description,
                    STATE_JSON=state_json,
                    FILE_PATH=file_path,
                    CREATOR_ID=creator_id,
                    SOURCE_CONTEXT=source_context,
                ))
            db.commit()
            LOGGER.info(
                "[observer] fixture created: %s (%s) in %s",
                name, fixture_id, building_id,
            )
            # 従来 (db.merge) の返り値と同型の detached スナップショット
            return Fixture(
                FIXTURE_ID=fixture_id,
                BUILDING_ID=building_id,
                NAME=name,
                TYPE=fixture_type,
                DESCRIPTION=description,
                STATE_JSON=state_json,
                FILE_PATH=file_path,
                CREATOR_ID=creator_id,
                SOURCE_CONTEXT=source_context,
            )
        finally:
            db.close()

    def get_fixture(self, fixture_id: str) -> Optional[Fixture]:
        db: Session = self.manager.SessionLocal()
        try:
            return db.query(Fixture).filter(Fixture.FIXTURE_ID == fixture_id).first()
        finally:
            db.close()

    def get_building_fixtures(self, building_id: str) -> List[Fixture]:
        db: Session = self.manager.SessionLocal()
        try:
            return db.query(Fixture).filter(Fixture.BUILDING_ID == building_id).all()
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Observer CRUD
    # ------------------------------------------------------------------

    def create_observer(
        self,
        observer_id: str,
        fixture_id: str,
        exec_kind: str,
        *,
        exec_target: Optional[str] = None,
        exec_args_json: Optional[str] = None,
        interval_sec: Optional[int] = None,
        metric_keys_json: Optional[str] = None,
        notify_rules_json: Optional[str] = None,
        enabled: bool = True,
    ) -> ObserverConfig:
        db: Session = self.manager.SessionLocal()
        try:
            config = ObserverConfig(
                OBSERVER_ID=observer_id,
                FIXTURE_ID=fixture_id,
                ENABLED=enabled,
                EXEC_KIND=exec_kind,
                EXEC_TARGET=exec_target,
                EXEC_ARGS_JSON=exec_args_json,
                INTERVAL_SEC=interval_sec,
                METRIC_KEYS_JSON=metric_keys_json,
                NOTIFY_RULES_JSON=notify_rules_json,
            )
            db.merge(config)
            db.commit()
            LOGGER.info("[observer] config created: %s (kind=%s)", observer_id, exec_kind)
            return config
        finally:
            db.close()

    def get_observer(self, observer_id: str) -> Optional[ObserverConfig]:
        db: Session = self.manager.SessionLocal()
        try:
            return db.query(ObserverConfig).filter(
                ObserverConfig.OBSERVER_ID == observer_id
            ).first()
        finally:
            db.close()

    def get_fixture_observers(self, fixture_id: str) -> List[ObserverConfig]:
        db: Session = self.manager.SessionLocal()
        try:
            return db.query(ObserverConfig).filter(
                ObserverConfig.FIXTURE_ID == fixture_id
            ).all()
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Metrics — Push / Record
    # ------------------------------------------------------------------

    def record_metrics(
        self,
        observer_id: str,
        metrics: Dict[str, Dict[str, Any]],
        recorded_at: Optional[datetime] = None,
    ) -> List[ObserverMetric]:
        """observer_metrics にデータを記録し、STATE_JSON を更新し、閾値判定を行う。

        Args:
            observer_id: Observer の ID
            metrics: {metric_name: {"value_num": float|None, "value_text": str|None}}
            recorded_at: 記録時刻 (省略時は UTC now)

        Returns:
            記録された ObserverMetric のリスト
        """
        if recorded_at is None:
            recorded_at = datetime.now(timezone.utc)

        # metric 名の関所: metric 名は STATE_JSON のトップレベルキー = json_set
        # の JSON パスになるため、(1) 予約キー (feed_stand 等 — 他の書き手の
        # 領域。RESERVED_STATE_KEYS のコメント参照) と (2) パスに安全に埋め
        # 込めない形 (英数字・_・- 以外を含む) は WARNING + skip で拒否する。
        # 拒否した metric は履歴 (observer_metrics) にも書かない — キャッシュ
        # だけ欠けた「記録されたのに表示されない」不整合を作らないため。
        accepted: Dict[str, Dict[str, Any]] = {}
        for metric_name, values in metrics.items():
            if metric_name in RESERVED_STATE_KEYS:
                LOGGER.warning(
                    "[observer] metric name %r is reserved for another "
                    "STATE_JSON writer; metric rejected (observer=%s)",
                    metric_name, observer_id,
                )
                continue
            if not STATE_KEY_PATTERN.fullmatch(metric_name):
                LOGGER.warning(
                    "[observer] metric name %r is not a valid STATE_JSON key "
                    "(allowed: A-Z a-z 0-9 _ -); metric rejected (observer=%s)",
                    metric_name, observer_id,
                )
                continue
            accepted[metric_name] = values
        if not accepted:
            return []

        # 設定確認 → メトリクス追加 → STATE_JSON 更新 → commit を単一
        # transaction で行う。STATE_JSON の更新は json_set の単文 UPDATE
        # (update_fixture_state_keys) — 自分の metric キーだけを DB 側で
        # 原子的に差し替えるため、並走する他の書き手 (feed_manager.
        # update_fixture_display / 別プロセスの record_metrics) のキーを
        # 消さない。lock も CAS リトライも要らない (冒頭のコメント参照)。
        def _write():
            """一回ぶんの書き込み transaction (毎回新しい session で読み直す)。

            run_with_snapshot_retry の契約: 再試行時は新しいスナップショット
            で読み直すため、設定確認からやり直す。
            """
            recorded_batch: List[ObserverMetric] = []
            db: Session = self.manager.SessionLocal()
            try:
                config = db.query(ObserverConfig).filter(
                    ObserverConfig.OBSERVER_ID == observer_id
                ).first()
                if not config:
                    LOGGER.warning(
                        "[observer] record_metrics: unknown observer %s",
                        observer_id,
                    )
                    return None, []
                if not config.ENABLED:
                    LOGGER.debug(
                        "[observer] record_metrics: observer %s is disabled",
                        observer_id,
                    )
                    return None, []
                fixture_id = config.FIXTURE_ID
                # commit で属性が expire する前に session から切り離し、閾値
                # 判定 (_evaluate_notify_rules) へ読み出し値を保持したまま渡す
                db.expunge(config)

                # 冪等化: 一回目の commit 結果が不明のまま再実行された場合の
                # 履歴重複を防ぐ — 同じ (observer, recorded_at) の行を書き直す
                db.query(ObserverMetric).filter(
                    ObserverMetric.OBSERVER_ID == observer_id,
                    ObserverMetric.RECORDED_AT == recorded_at,
                ).delete(synchronize_session=False)

                for metric_name, values in accepted.items():
                    metric = ObserverMetric(
                        OBSERVER_ID=observer_id,
                        METRIC_NAME=metric_name,
                        VALUE_NUM=values.get("value_num"),
                        VALUE_TEXT=values.get("value_text"),
                        RECORDED_AT=recorded_at,
                    )
                    db.add(metric)
                    recorded_batch.append(metric)

                update_fixture_state_keys(
                    db, (Fixture.FIXTURE_ID == fixture_id,),
                    {
                        metric_name: {
                            "value_num": values.get("value_num"),
                            "value_text": values.get("value_text"),
                            "recorded_at": recorded_at.isoformat(),
                        }
                        for metric_name, values in accepted.items()
                    },
                    context=f"record_metrics observer={observer_id}",
                )

                db.commit()
                return config, recorded_batch
            finally:
                db.close()

        try:
            config, recorded = run_with_snapshot_retry(
                _write, context=f"record_metrics observer={observer_id}",
            )
        except SnapshotRetryExhausted:
            # 一時的競合の二連敗だけを見送る — この tick のメトリクスは
            # 履歴・キャッシュとも書かれていない (まとめて rollback 済み)。
            # 次の観測 tick が新しい値で書き直す。恒久障害 (JSON1 不在・
            # スキーマ不整合等) はここに来ず OperationalError のまま伝播し、
            # API は非成功応答になる (障害を空成功で隠さない)。
            LOGGER.warning(
                "[observer] record_metrics gave up after snapshot-conflict "
                "retry; this tick's metrics were skipped (observer=%s)",
                observer_id, exc_info=True,
            )
            return []
        if config is None:
            return []
        LOGGER.debug(
            "[observer] recorded %d metrics for %s", len(recorded), observer_id
        )

        # 閾値判定 (commit 後に実行。config は commit 前に expunge した
        # detached 状態で、読み出し値は保持されている)
        self._evaluate_notify_rules(config, accepted, recorded_at)

        return recorded

    # ------------------------------------------------------------------
    # Metrics — Query
    # ------------------------------------------------------------------

    def get_latest_metrics(self, observer_id: str) -> Dict[str, Dict[str, Any]]:
        """Observer の最新メトリクスを取得する (STATE_JSON キャッシュから)。"""
        db: Session = self.manager.SessionLocal()
        try:
            config = db.query(ObserverConfig).filter(
                ObserverConfig.OBSERVER_ID == observer_id
            ).first()
            if not config:
                return {}
            fixture = db.query(Fixture).filter(
                Fixture.FIXTURE_ID == config.FIXTURE_ID
            ).first()
            if not fixture or not fixture.STATE_JSON:
                return {}
            return json.loads(fixture.STATE_JSON)
        finally:
            db.close()

    def get_metric_history(
        self,
        observer_id: str,
        metric_name: str,
        *,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """observer_metrics から指定メトリクスの履歴を取得する。"""
        db: Session = self.manager.SessionLocal()
        try:
            rows = (
                db.query(ObserverMetric)
                .filter(
                    ObserverMetric.OBSERVER_ID == observer_id,
                    ObserverMetric.METRIC_NAME == metric_name,
                )
                .order_by(desc(ObserverMetric.RECORDED_AT))
                .limit(limit)
                .all()
            )
            return [
                {
                    "value_num": r.VALUE_NUM,
                    "value_text": r.VALUE_TEXT,
                    "recorded_at": r.RECORDED_AT.isoformat() if r.RECORDED_AT else None,
                }
                for r in rows
            ]
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Pull 型 — EventScheduler 連携
    # ------------------------------------------------------------------

    def start_pull_observers(self) -> None:
        """起動時に ENABLED な pull 型 Observer を EventScheduler に登録する。"""
        db: Session = self.manager.SessionLocal()
        try:
            configs = db.query(ObserverConfig).filter(
                ObserverConfig.ENABLED == True,  # noqa: E712
                ObserverConfig.EXEC_KIND.in_(["tool", "playbook"]),
            ).all()
            for config in configs:
                self._schedule_pull(config)
            if configs:
                LOGGER.info("[observer] started %d pull observers", len(configs))
        finally:
            db.close()

    def _schedule_pull(self, config: ObserverConfig) -> None:
        """単一の pull 型 Observer を EventScheduler に登録する。"""
        if not config.INTERVAL_SEC or config.INTERVAL_SEC <= 0:
            LOGGER.warning(
                "[observer] skip pull observer %s: invalid interval", config.OBSERVER_ID
            )
            return

        scheduler = self.manager.event_scheduler
        key = f"observer:{config.OBSERVER_ID}"

        def callback() -> None:
            self._execute_pull(config.OBSERVER_ID)

        scheduler.schedule_periodic(
            interval_seconds=config.INTERVAL_SEC,
            callback=callback,
            key=key,
            first_fire_immediate=False,
        )
        LOGGER.debug(
            "[observer] scheduled pull: %s every %ds", config.OBSERVER_ID, config.INTERVAL_SEC
        )

    def _execute_pull(self, observer_id: str) -> None:
        """pull 型 Observer のツール/Playbook を実行し、結果を record_metrics に流す。"""
        db: Session = self.manager.SessionLocal()
        try:
            config = db.query(ObserverConfig).filter(
                ObserverConfig.OBSERVER_ID == observer_id
            ).first()
            if not config or not config.ENABLED:
                return

            if config.EXEC_KIND == "tool":
                self._execute_tool(config)
            elif config.EXEC_KIND == "playbook":
                LOGGER.debug("[observer] playbook execution not yet implemented for %s", observer_id)
        finally:
            db.close()

    def _execute_tool(self, config: ObserverConfig) -> None:
        """TOOL_REGISTRY から指定ツールを実行し、結果を metrics に記録する。"""
        from tools import TOOL_REGISTRY

        tool_name = config.EXEC_TARGET
        if not tool_name or tool_name not in TOOL_REGISTRY:
            LOGGER.warning("[observer] tool not found: %s", tool_name)
            return

        tool_entry = TOOL_REGISTRY[tool_name]
        tool_func = tool_entry.get("function")
        if not tool_func:
            LOGGER.warning("[observer] tool has no function: %s", tool_name)
            return

        args = json.loads(config.EXEC_ARGS_JSON) if config.EXEC_ARGS_JSON else {}
        try:
            result = tool_func(**args)
        except Exception:
            LOGGER.exception("[observer] tool execution failed: %s", tool_name)
            return

        metrics = self._parse_tool_result(config, result)
        if metrics:
            self.record_metrics(config.OBSERVER_ID, metrics)

    def _parse_tool_result(
        self, config: ObserverConfig, result: Any
    ) -> Dict[str, Dict[str, Any]]:
        """ツールの戻り値を metric_keys_json に従って metrics dict に展開する。"""
        if not config.METRIC_KEYS_JSON:
            if isinstance(result, (int, float)):
                return {"value": {"value_num": float(result)}}
            if isinstance(result, str):
                return {"value": {"value_text": result}}
            return {}

        keys_config = json.loads(config.METRIC_KEYS_JSON)
        metrics: Dict[str, Dict[str, Any]] = {}

        if isinstance(result, dict):
            for metric_name, source_key in keys_config.items():
                val = result.get(source_key)
                if val is None:
                    continue
                if isinstance(val, (int, float)):
                    metrics[metric_name] = {"value_num": float(val)}
                else:
                    metrics[metric_name] = {"value_text": str(val)}
        elif isinstance(result, str):
            try:
                result_dict = json.loads(result)
                return self._parse_tool_result(config, result_dict)
            except (json.JSONDecodeError, TypeError):
                pass

        return metrics

    # ------------------------------------------------------------------
    # Notify Rules — 閾値判定
    # ------------------------------------------------------------------

    def _evaluate_notify_rules(
        self,
        config: ObserverConfig,
        metrics: Dict[str, Dict[str, Any]],
        recorded_at: datetime,
    ) -> None:
        """NOTIFY_RULES_JSON を評価し、条件ヒットで Building に通知する。"""
        if not config.NOTIFY_RULES_JSON:
            return

        try:
            rules = json.loads(config.NOTIFY_RULES_JSON)
        except (json.JSONDecodeError, TypeError):
            return

        db: Session = self.manager.SessionLocal()
        try:
            fixture = db.query(Fixture).filter(
                Fixture.FIXTURE_ID == config.FIXTURE_ID
            ).first()
            if not fixture:
                return
            building_id = fixture.BUILDING_ID
        finally:
            db.close()

        for rule in rules:
            metric_name = rule.get("metric")
            if metric_name not in metrics:
                continue

            value = metrics[metric_name].get("value_num")
            if value is None:
                continue

            triggered = False
            message = ""

            threshold_above = rule.get("above")
            if threshold_above is not None and value > threshold_above:
                triggered = True
                message = rule.get("message_above", f"{metric_name} が {value} に上昇 (閾値: {threshold_above})")

            threshold_below = rule.get("below")
            if threshold_below is not None and value < threshold_below:
                triggered = True
                message = rule.get("message_below", f"{metric_name} が {value} に低下 (閾値: {threshold_below})")

            if triggered:
                self._notify_building(building_id, config, message, recorded_at)

    def _notify_building(
        self,
        building_id: str,
        config: ObserverConfig,
        message: str,
        recorded_at: datetime,
    ) -> None:
        """Building event として通知を注入する。"""
        self.manager.add_building_event(
            building_id,
            {
                "role": "host",
                "content": message,
                "event_type": "observer_alert",
                "metadata": {
                    "observer_id": config.OBSERVER_ID,
                    "fixture_id": config.FIXTURE_ID,
                    "recorded_at": recorded_at.isoformat(),
                },
            },
        )
        LOGGER.info("[observer] notification sent to %s: %s", building_id, message)
