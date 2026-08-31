"""Initialization helpers extracted from SAIVerseManager.__init__."""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from saiverse.buildings import Building
from saiverse.regions import Region
from database.models import City as CityModel

if TYPE_CHECKING:
    pass

LOGGER = logging.getLogger(__name__)


class InitializationMixin:
    """Initialization helper methods for SAIVerseManager."""

    @staticmethod
    def _set_sqlite_pragmas(dbapi_connection, connection_record):
        """Enable WAL mode and busy_timeout for concurrent read/write safety."""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    def _init_database(self, db_path: str) -> None:
        """Step 0: Database and Configuration Setup."""
        self.db_path = db_path
        self.city_model = CityModel
        self.city_host_avatar_path: Optional[str] = None
        DATABASE_URL = f"sqlite:///{db_path}"
        engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
        event.listen(engine, "connect", self._set_sqlite_pragmas)
        self._ensure_city_timezone_column(engine)
        self._ensure_user_avatar_column(engine)
        self._ensure_city_host_avatar_column(engine)
        self._ensure_item_tables(engine)
        self._ensure_phenomenon_tables(engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        # Configure UsageTracker to use the same database
        from saiverse.usage_tracker import get_usage_tracker
        get_usage_tracker().configure(self.SessionLocal)

    def _init_city_config(self, city_name: str) -> None:
        """Step 1: Load City Configuration from DB."""
        db = self.SessionLocal()
        try:
            my_city_config = db.query(CityModel).filter(CityModel.CITY_SLUG == city_name).first()
            if not my_city_config:
                # Fallback: find by CITYID=1 and auto-repair CITY_SLUG.
                # 旧チュートリアルが内部の識別子を表示名 (非 ASCII を含む) で
                # 上書きしてしまった世界の救済。供給源は塞いだ — 表示名は
                # CITYNAME が持ち、識別子は City 作成後に変更できない
                # (docs/intent/city_identity.md §4 不変条件 2) — が、既に壊れた
                # DB を起動できるようにするためこの経路は残す。
                #
                # 関所 (2026-07-31 席競合案件・十巡目): 同じ DB を所有する別の
                # 稼働中プロセスがいる間は修復しない。runtime marker は City 名
                # でしか二重起動を弾かないため、稼働中の City を別名で起動する
                # と、この修復が CITYID=1 を改名して**同じペルソナ群を 2 プロセス
                # が同時運転する**状態を作ってしまう (判断・ライフ境界・台帳の
                # 直列化前提が全て破れる)。修復はあくまで単一 City ホームの
                # 改名事故の救済であって、稼働中の実体の乗っ取りではない。
                from saiverse.runtime_marker import another_running_process_owns_db

                owned, owner = another_running_process_owns_db(self.db_path)
                if owned:
                    raise ValueError(
                        f"City '{city_name}' not found in the database, and a "
                        f"running SAIVerse process already owns this database "
                        f"({owner}). Refusing CITY_SLUG auto-repair — starting "
                        f"would run the same personas in two processes. Stop "
                        f"the running process first, or start it with its "
                        f"registered city name."
                    )
                # 修復は「City が 1 行だけの DB」に限る (2026-07-31 十一巡目)。
                # 複数 City の DB で未知名を渡された場合、それは改名事故ではなく
                # 呼び出しの誤りなので、CITYID=1 の所属を黙って書き換えない。
                city_count = db.query(CityModel).count()
                my_city_config = (
                    db.query(CityModel).filter(CityModel.CITYID == 1).first()
                    if city_count == 1 else None
                )
                if my_city_config:
                    old_name = my_city_config.CITY_SLUG
                    LOGGER.warning(
                        "City '%s' not found but CITYID=1 exists with CITY_SLUG='%s'. "
                        "Auto-repairing CITY_SLUG to '%s'.",
                        city_name, old_name, city_name,
                    )
                    my_city_config.CITY_SLUG = city_name
                    db.commit()
                else:
                    raise ValueError(
                        f"City '{city_name}' not found in the database. "
                        "Please run 'python database/seed.py' first."
                        + (
                            f" ({city_count} cities exist; CITY_SLUG auto-repair "
                            "only applies to a single-city database)"
                            if city_count > 1 else ""
                        )
                    )
            
            self.city_id = my_city_config.CITYID
            self.city_name = my_city_config.CITY_SLUG
            self.user_room_id = f"user_room_{self.city_name}"
            self.ui_port = my_city_config.UI_PORT
            self.api_port = my_city_config.API_PORT
            self.start_in_online_mode = my_city_config.START_IN_ONLINE_MODE
            self._update_timezone_cache(getattr(my_city_config, "TIMEZONE", "UTC"))
            self.city_host_avatar_path = getattr(my_city_config, "HOST_AVATAR_IMAGE", None)
            
            # Load other cities' configs for inter-city communication
            other_cities = db.query(CityModel).filter(CityModel.CITYID != self.city_id).all()
            self.cities_config = {
                city.CITY_SLUG: {
                    "city_id": city.CITYID,
                    "api_base_url": f"http://127.0.0.1:{city.API_PORT}",
                    "timezone": getattr(city, "TIMEZONE", "UTC") or "UTC",
                } for city in other_cities
            }
            LOGGER.info(
                "Loaded config for '%s' (ID: %s). Found %d other cities.",
                self.city_name, self.city_id, len(self.cities_config)
            )
        finally:
            db.close()

    def _init_buildings(self) -> None:
        """Step 1b: Load Static Assets from DB."""
        self.regions: Dict[str, Region] = self._load_regions_from_db()
        self.buildings: List[Building] = self._load_and_create_buildings_from_db()
        self.building_map: Dict[str, Building] = {b.building_id: b for b in self.buildings}
        self.capacities: Dict[str, int] = {b.building_id: b.capacity for b in self.buildings}
        
        # Item containers (populated later by ItemService)
        self.items: Dict[str, Dict[str, Any]] = {}
        self.item_locations: Dict[str, Dict[str, str]] = {}
        self.items_by_building: Dict[str, List[str]] = defaultdict(list)
        self.items_by_persona: Dict[str, List[str]] = defaultdict(list)
        self.world_items: List[str] = []
        
        # Persona events
        self.persona_pending_events: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self._load_persona_event_logs()

    def _init_file_paths(self) -> None:
        """Step 2: Setup File Paths and Default Avatars."""
        from saiverse.data_paths import get_saiverse_home
        self.saiverse_home = get_saiverse_home()
        self.backup_dir = self.saiverse_home / "backups"
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.building_memory_paths: Dict[str, Path] = {
            b.building_id: self.saiverse_home / "cities" / self.city_name / "buildings" / b.building_id / "log.json"
            for b in self.buildings
        }

    def _init_avatars(self) -> None:
        """Step 2b: Load default avatars with graceful fallback."""
        avatar_fallback_paths = [
            Path("builtin_data/icons/blank.png"),
            Path("builtin_data/icons/user.png"),
            Path("builtin_data/icons/host.png"),
            Path("assets/icons/host.png"),  # Legacy fallback
        ]
        default_avatar_data = ""
        for avatar_path in avatar_fallback_paths:
            data_url = self._load_avatar_data(avatar_path)
            if data_url:
                default_avatar_data = data_url
                break
        self.default_avatar = default_avatar_data

        host_avatar_data = self._load_avatar_data(Path("builtin_data/icons/host.png"))
        self.host_avatar = host_avatar_data or self.default_avatar
        if getattr(self, "city_host_avatar_path", None):
            host_override = self._load_avatar_data(Path(self.city_host_avatar_path))
            if host_override:
                self.host_avatar = host_override
        self.user_avatar_data = self.default_avatar

    def _init_building_histories(self) -> None:
        """Step 3: Building 履歴の起動時初期化。

        Phase 2+3 以降は DB が source of truth。 旧 log.json 5 状態判定 / quarantine
        起動時バックアップは廃止し、 in-memory ``building_histories`` dict は legacy
        caller 互換のため空 dict で初期化するのみ (= 実体は使われない)。
        See docs/intent/building_memory_unified.md
        """
        self.building_histories: Dict[str, List[Dict[str, str]]] = {}
        self._check_legacy_building_log_import()
        self._capture_startup_seq_watermark()

    def _capture_startup_seq_watermark(self) -> None:
        """部屋ごとの「世界が動き出す前の末尾」を記録する。

        「どこまで読んだか」の記録が無いペルソナは、この値までを既読として始める
        (`builtin_data/tools/get_building_messages.py` の `_ingest_round`)。

        **なぜ Pulse のときに数えないのか**: 記録が無いペルソナが最初に喋るのは、
        起動してからしばらく後になる。その間にユーザーが送ったメッセージは、
        Pulse 時点の末尾に含まれてしまう。そこを境界にすると、**ユーザーが送った
        本物のメッセージが読まれないまま既読になる**。境界は誰も書き込めない
        時点で取らなければならない。

        過去ログの取り込み (`_check_legacy_building_log_import`) の後に取る —
        取り込んだ行の seq は 0 未満なので水位には影響しないが、順序を固定して
        おく方が読み違えにくい。
        """
        from database.models import BuildingMessage
        from sqlalchemy import func as sa_func

        # **まず登録済みの全部屋を 0 で埋める。** SELECT の結果だけで作ると、
        # 起動時に空だった部屋が水位に載らない。そこへ起動後の最初のメッセージが
        # 届くと、水位が無いので「現在の末尾」= そのメッセージ自身が境界になり、
        # ユーザーが送った 1 通目が誰にも読まれない (2026-08-16 Codex 指摘)。
        self.startup_seq_watermark: Dict[str, int] = {
            b.building_id: 0 for b in self.buildings
        }
        try:
            db = self.SessionLocal()
            try:
                rows = (
                    db.query(
                        BuildingMessage.building_id,
                        sa_func.max(BuildingMessage.seq),
                    )
                    .group_by(BuildingMessage.building_id)
                    .all()
                )
                for b_id, max_seq in rows:
                    self.startup_seq_watermark[b_id] = max(int(max_seq or 0), 0)
            finally:
                db.close()
        except Exception:
            # 水位を測れなかった。全部屋 0 のまま進む = 記録の無いペルソナは
            # その部屋の履歴を全部読む。読み過ぎは高くつくが、ユーザーが送った
            # メッセージを黙って捨てるよりはよい。気づけるようアラートに出す。
            LOGGER.warning("[legacy-log] 起動時の会話の末尾を測れませんでした", exc_info=True)
            self.startup_alerts.append({
                "id": "startup_seq_watermark_failed",
                "level": "warning",
                "title": "会話の読み進み位置の基準を作れませんでした",
                "message": (
                    "起動時に各部屋の会話の末尾を数えられませんでした。"
                    "読み進み位置の記録が無いペルソナが、その部屋の会話を"
                    "最初から読み直すことがあります（費用がかかります）。"
                ),
                "details": {},
            })
        LOGGER.info(
            "[legacy-log] 起動時の会話の末尾を %d 部屋分ひかえました",
            len(self.startup_seq_watermark),
        )

    def _check_legacy_building_log_import(self) -> None:
        """旧 log.json の取り込み漏れを毎起動で見つけ、その場で取り込む。

        **見つけた本人が直す。** 見つけられるのに放置してバナーだけ出すと、
        ユーザーは「取り込まれていません」と言われるだけで何もできない
        (2026-08-16 まはー裁定)。取り込みは冪等 — 既に DB に居る発言は入れない —
        なので毎起動走らせて構わない。

        取り込んだ過去ログは 0 未満の seq を持ち、通常の発言 (1 以上) より必ず
        前に並ぶ。既存の行は 1 つも動かないので、行を指している他の記録
        (ペルソナ個人の記憶に残る転記元の目印、AddonMessageMetadata) もずれない。

        直せなかったものだけ startup_alerts (UI バナー) に載せる。2026-08-16 の
        テスタロッサの部屋 (隔離マーカー残置で移行がスキップされ、2 ヶ月半誰も
        気づかなかった) の再発防止。
        """
        from saiverse.legacy_log_import import scan_legacy_log_deficits

        # 直せる見込みのある種類。壊れて読めないファイルと、検算自体が倒れた部屋は
        # 取り込みを試しても同じ場所で失敗するので触らない。
        repairable = {"not_imported", "live_rows_only", "partial"}
        try:
            db = self.SessionLocal()
            try:
                deficits = scan_legacy_log_deficits(
                    db, self.saiverse_home, self.city_name,
                    [b.building_id for b in self.buildings],
                )
                targets = [d["building_id"] for d in deficits if d["kind"] in repairable]
                if targets:
                    self._repair_legacy_building_logs(db, targets)
                    # 直った証拠は自分の帳簿でなく DB から取り直す
                    deficits = scan_legacy_log_deficits(
                        db, self.saiverse_home, self.city_name,
                        [b.building_id for b in self.buildings],
                    )
            finally:
                db.close()
        except Exception as e:
            # 検算そのものが倒れたことを黙って飲まない。ここで return するだけだと
            # 「漏れが無かった」と見分けがつかず、沈黙が正常の顔をしてしまう。
            LOGGER.warning("legacy building log check failed", exc_info=True)
            self.startup_alerts.append({
                "id": "legacy_log_check_failed",
                "level": "warning",
                "title": "過去ログの取り込み状況を確認できませんでした",
                "message": (
                    "旧形式の履歴ファイルとデータベースの突き合わせ（毎起動の確認）"
                    f"に失敗しました（{type(e).__name__}: {e}）。取り込み漏れが"
                    "あっても検出できていない可能性があります。"
                ),
                "details": {"error": f"{type(e).__name__}: {e}"},
            })
            return

        for d in deficits:
            b_id = d["building_id"]
            b = self.building_map.get(b_id) if hasattr(self, "building_map") else None
            display = getattr(b, "name", None) or b_id
            if d["kind"] == "check_failed":
                level = "warning"
                body = (
                    f"部屋「{display}」について、過去の会話履歴が新しい保存先"
                    "（データベース）に取り込まれているかを確認できませんでした"
                    f"（{d['reason']}）。この部屋だけ確認できていないため、"
                    "取り込み漏れがあっても気づけません。"
                )
            elif d["kind"] == "unreadable":
                level = "warning"
                body = (
                    f"部屋「{display}」の古い会話が入ったファイルが壊れていて読めません"
                    f"（{d['reason']}）。この部屋の古い会話は自動では戻せません。"
                    "「ファイルを脇へ移す」を押すと、そのファイルの名前を変えて"
                    "脇によけ、この警告は出なくなります。ファイル自体は消えないので、"
                    "後から中身を救う手が見つかれば使えます。"
                )
            else:
                # 自動の取り込みを試したうえで、まだ入っていないもの。
                # 種類 (not_imported / live_rows_only / partial) の違いは
                # 直せなかった時点でユーザーには関係ないので、1 つの文にまとめる。
                level = "critical" if d["kind"] == "not_imported" else "warning"
                body = (
                    f"部屋「{display}」の古い会話 {d['missing']} 件を新しい保存先"
                    "（データベース）へ移そうとしましたが、移せませんでした。"
                    "この分は画面に表示されません。"
                    "会話そのものは古いファイルに残っているので、失われてはいません。"
                )
            self.startup_alerts.append({
                "id": f"legacy_log_deficit_{b_id}",
                "level": level,
                "title": f"過去ログが未取込: {display}",
                "message": body,
                "details": d,
            })
        if deficits:
            LOGGER.warning(
                "[legacy-log] %d 部屋で古い会話を移しきれませんでした: %s",
                len(deficits), ", ".join(d["building_id"] for d in deficits),
            )

    def _repair_legacy_building_logs(self, db, building_ids: List[str]) -> None:
        """取り込み漏れの見つかった部屋を、その場で取り込む。

        部屋ごとに呼んで部屋ごとに確定させる — 後半で失敗しても、先に取り込めた
        部屋の会話は残す。1 部屋の中の失敗の隔離は import_building_logs 側が持つ。
        """
        from saiverse.legacy_log_import import import_building_logs

        LOGGER.info(
            "[legacy-log] %d 部屋に古い会話が残っているので、いま移します: %s",
            len(building_ids), ", ".join(building_ids),
        )
        for b_id in building_ids:
            try:
                stats = import_building_logs(
                    db, self.saiverse_home,
                    city_filter=self.city_name, building_filter=b_id,
                    commit_per_building=True,
                )
            except Exception:
                LOGGER.error(
                    "[legacy-log] %s: 古い会話を移せませんでした", b_id, exc_info=True,
                )
                continue
            LOGGER.info(
                "[legacy-log] %s: 古い会話 %d 件を移しました", b_id, stats.messages_inserted,
            )

    def _quarantine_building(
        self,
        b_id: str,
        path: "Path",
        timestamp: str,
        *,
        reason: str,
        title_suffix: str,
        message_extra: str,
    ) -> None:
        """Move corrupted/invalid file to a backup name and mark building as quarantined.

        Quarantined buildings:
          - have NO key in self.building_histories (so save skips them)
          - are listed in self.quarantined_buildings (with restore options)
          - block movement (handled in OccupancyManager)
          - are surfaced via self.startup_alerts (banner)
        """
        from manager.history import list_log_backups

        backup_path = path.parent / f"{path.name}.corrupted_{timestamp}"
        rescue_error: Optional[str] = None
        rescued = False
        try:
            path.rename(backup_path)
            rescued = True
            LOGGER.error(
                "Building history for %s is %s; rescued to %s",
                b_id, reason, backup_path,
            )
        except OSError as rename_exc:
            rescue_error = str(rename_exc)
            LOGGER.error(
                "Failed to rescue %s log for %s: %s",
                reason, b_id, rename_exc,
            )

        available_backups = [str(p) for p in list_log_backups(path)]

        # 隔離レコード — UI から復旧操作する時のソースオブトゥルース
        self.quarantined_buildings[b_id] = {
            "building_id": b_id,
            "reason": reason,
            "original_path": str(path),
            "corrupted_path": str(backup_path) if rescued else None,
            "rescue_error": rescue_error,
            "available_backups": available_backups,
            "detected_at": timestamp,
        }

        # アラート
        if rescued:
            alert = {
                "id": f"quarantine_{b_id}_{timestamp}",
                "level": "critical",
                "title": f"会話履歴ファイルが{title_suffix}: {b_id}",
                "message": (
                    f"ビルディング「{b_id}」のチャット履歴ファイルが異常状態でした。"
                    f"{message_extra} 破損ファイルを安全な場所に退避し、このビルディングは"
                    "**隔離状態**にしました。新規会話・入室は制限されています。"
                    f"利用可能なバックアップが{len(available_backups)}個あります。"
                    "アラート横の「対応する」ボタンから復元・リセット等を選択してください。"
                ),
                "details": {
                    "building_id": b_id,
                    "reason": reason,
                    "original_path": str(path),
                    "corrupted_path": str(backup_path),
                    "available_backups": available_backups,
                    "recovery_instructions": (
                        "1) アラート横の「対応する」ボタンから復元方法を選ぶ、"
                        "2) または手動で退避ファイルを log.json にリネームして再起動する"
                    ),
                },
            }
        else:
            alert = {
                "id": f"quarantine_rescue_failed_{b_id}_{timestamp}",
                "level": "critical",
                "title": f"会話履歴ファイル{title_suffix} + 退避失敗: {b_id}",
                "message": (
                    "ビルディングのチャット履歴ファイルが異常で、さらに退避にも失敗しました。"
                    "**システムは自動上書きを停止しました**ので、安全な場所にファイルをコピーして"
                    "から手動で対応してください。"
                ),
                "details": {
                    "building_id": b_id,
                    "reason": reason,
                    "original_path": str(path),
                    "rescue_error": rescue_error,
                    "available_backups": available_backups,
                },
            }
        self.startup_alerts.append(alert)

    def _init_model_config(self, model: Optional[str]) -> None:
        """Step 4a: Initialize model configuration."""
        from saiverse.model_configs import get_context_length, get_model_provider
        import os

        def _get_default_model() -> str:
            from saiverse.model_defaults import BUILTIN_DEFAULT_LITE_MODEL
            return os.getenv("SAIVERSE_DEFAULT_MODEL", BUILTIN_DEFAULT_LITE_MODEL)

        base_model = model or _get_default_model()
        self.model = None  # No global override by default
        self.startup_warnings: List[Dict[str, str]] = []
        try:
            self.context_length = get_context_length(base_model)
            self.provider = get_model_provider(base_model)
        except ValueError:
            from saiverse.model_defaults import BUILTIN_DEFAULT_LITE_MODEL
            fallback = BUILTIN_DEFAULT_LITE_MODEL
            city = getattr(self, "city_name", "unknown")
            msg = (
                f"City '{city}' のデフォルトモデル '{base_model}' の設定ファイルが見つかりません。"
                f"デフォルトモデル '{fallback}' にフォールバックしました。"
            )
            LOGGER.warning(
                "Model config '%s' not found. Falling back to '%s'. "
                "Check that the model JSON file exists in builtin_data/models/ or user_data/models/.",
                base_model, fallback,
            )
            self.startup_warnings.append({
                "source": "model_config",
                "message": msg,
            })
            base_model = fallback
            self.context_length = get_context_length(base_model)
            self.provider = get_model_provider(base_model)
        self._base_model = base_model
        self.model_parameter_overrides: Dict[str, Any] = {}
        # Metabolism は常時 ON (2026-07-30 OFF トグル撤去)。水位は model 定義
        # 一本で解決する (sea/session_lifecycle.py get_metabolism_watermarks)。
        self.max_image_embeds_override: Optional[int] = None

    def _update_timezone_cache(self, tz_name: Optional[str]) -> None:
        """Update cached timezone information for this manager.

        Updates the manager's own attributes AND the CoreState object
        (if it exists) so that AdminService / PersonaMixin always see
        the latest timezone when creating or loading personas.
        """
        name = (tz_name or "UTC").strip() or "UTC"
        try:
            tz = ZoneInfo(name)
        except Exception:
            LOGGER.warning("Invalid timezone '%s'. Falling back to UTC.", name)
            name = "UTC"
            tz = ZoneInfo("UTC")
        self.timezone_name = name
        self.timezone_info = tz
        # Propagate to CoreState so AdminService (which reads from state)
        # also picks up the change.
        state = getattr(self, "state", None)
        if state is not None:
            state.timezone_name = name
            state.timezone_info = tz


__all__ = ["InitializationMixin"]
