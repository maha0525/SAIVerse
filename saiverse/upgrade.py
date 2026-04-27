"""SAIVerse バージョン認識基盤 (Phase 1).

City と AI それぞれが ``LAST_KNOWN_VERSION`` を持ち、起動時に現在の SAIVerse
バージョン（``saiverse.__version__``）と比較して、必要なアップデートハンドラを
順次実行する。

設計詳細: ``docs/intent/version_aware_world_and_persona.md``

Phase 1 段階では :data:`HANDLERS` は空リスト。Phase 2 で第1号ハンドラ
（dynamic_state ``captured_at`` リセット）を追加する。

外部 I/F:
    - :func:`run_startup_upgrade`: 起動時に呼ぶエントリポイント
    - :data:`HANDLERS`: ハンドラ登録リスト（Phase 2 以降で追加）
    - :class:`UpgradeHandler`: ハンドラ定義のデータクラス
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, List, Literal, Optional

from packaging.version import InvalidVersion, Version

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

LOGGER = logging.getLogger(__name__)

# 「バージョン記録なし」(LAST_KNOWN_VERSION IS NULL) を扱うために最古を指す値。
# v0.3.0 で導入された LAST_KNOWN_VERSION カラムが NULL のまま残っているデータは
# 必然的に v0.3.0 以前から動いていたものなので、最古バージョン扱いとする。
PRE_VERSION_AWARE = Version("0.0.0")

ENV_SKIP_VERSION_CHECK = "SAIVERSE_SKIP_VERSION_CHECK"


# ---- ハンドラ型 ----

@dataclass
class UpgradeHandler:
    """単一バージョン遷移で実行するアップグレード処理の定義。

    Attributes:
        name: 識別子（``"v0_3_0_dynamic_state_reset"`` など。ログとデバッグで使う）
        scope: ``"city"`` か ``"ai"``。実行スコープ
        from_version: 隣接の前バージョン（このバージョンから来たときに適用）
        to_version: このバージョンで導入された処理（このバージョンに上がるときに適用）
        run: 実体。キーワード引数 ``session=`` と ``city=`` または ``ai=`` を受ける
        description: 任意の説明文
    """
    name: str
    scope: Literal["city", "ai"]
    from_version: str
    to_version: str
    run: Callable[..., None]
    description: str = ""


# Phase 2 以降で追加していく。実体は :mod:`saiverse.upgrade_handlers` 側に
# 定義し、:func:`_load_default_handlers` を経由して登録する。循環参照を避ける
# ため lazy import している。
HANDLERS: List[UpgradeHandler] = []
_handlers_loaded = False


def _load_default_handlers() -> None:
    """``saiverse.upgrade_handlers.HANDLERS`` を :data:`HANDLERS` に取り込む。

    起動シーケンスから1度だけ呼ばれる前提（idempotent）。テストで HANDLERS を
    上書きしたい場合は、``HANDLERS.clear()`` してから手動で append する。
    """
    global _handlers_loaded
    if _handlers_loaded:
        return
    try:
        from saiverse import upgrade_handlers
    except ImportError as exc:
        LOGGER.warning("[upgrade] failed to import upgrade_handlers: %s", exc, exc_info=True)
        _handlers_loaded = True
        return
    registered = list(getattr(upgrade_handlers, "HANDLERS", []))
    HANDLERS.extend(registered)
    _handlers_loaded = True
    LOGGER.debug("[upgrade] loaded %d default handler(s)", len(registered))


# ---- バージョン解析 ----

def parse_version(value: Optional[str]) -> Version:
    """LAST_KNOWN_VERSION 文字列を Version に。NULL/不正値は最古として扱う。"""
    if not value:
        return PRE_VERSION_AWARE
    try:
        return Version(value)
    except InvalidVersion as exc:
        LOGGER.warning(
            "Invalid version string %r found in DB, treating as pre-version-aware: %s",
            value, exc,
        )
        return PRE_VERSION_AWARE


def current_version() -> Version:
    """SAIVerse パッケージの現在バージョン（VERSION ファイル経由）。"""
    from saiverse import __version__
    try:
        return Version(__version__)
    except InvalidVersion as exc:
        LOGGER.error("Invalid current SAIVerse version %r: %s", __version__, exc, exc_info=True)
        raise


# ---- ハンドラ選択 ----

def select_handlers(
    scope: Literal["city", "ai"],
    from_version: Version,
    to_version: Version,
) -> List[UpgradeHandler]:
    """``from_version`` から ``to_version`` への遷移で実行すべきハンドラを順序付きで返す。

    各ハンドラは「``to_version`` で導入された処理」と解釈し、
    ``from_version < handler.to_version <= to_version`` を満たすものが対象。
    結果は ``handler.to_version`` の昇順でソートされる（中間バージョンを順次適用）。
    """
    selected: List[UpgradeHandler] = []
    for handler in HANDLERS:
        if handler.scope != scope:
            continue
        try:
            h_to = Version(handler.to_version)
        except InvalidVersion:
            LOGGER.warning(
                "Skipping handler %r with invalid to_version %r",
                handler.name, handler.to_version,
            )
            continue
        if from_version < h_to <= to_version:
            selected.append(handler)

    selected.sort(key=lambda h: Version(h.to_version))
    return selected


# ---- 単一エンティティのアップグレード ----

def _run_handlers_for_entity(
    session: "Session",
    *,
    scope: Literal["city", "ai"],
    entity: Any,
    entity_id: str,
    target: Version,
) -> bool:
    """1つのエンティティ (city or ai) を target バージョンまで順次アップグレードする。

    Returns:
        全ハンドラ成功で LAST_KNOWN_VERSION を更新できれば True。
        どこかで失敗すれば False（バージョンは未更新のまま残る）。
    """
    current = parse_version(getattr(entity, "LAST_KNOWN_VERSION", None))
    if current >= target:
        LOGGER.debug(
            "[upgrade] %s/%s already at %s (target %s), no-op",
            scope, entity_id, current, target,
        )
        # NULL のまま target と同等の場合（新規作成直後など）も値を入れておく
        if getattr(entity, "LAST_KNOWN_VERSION", None) is None:
            entity.LAST_KNOWN_VERSION = str(target)
            session.commit()
            LOGGER.info(
                "[upgrade] %s/%s LAST_KNOWN_VERSION initialized to %s",
                scope, entity_id, target,
            )
        return True

    handlers = select_handlers(scope, current, target)
    LOGGER.info(
        "[upgrade] %s/%s upgrading from %s to %s (%d handlers)",
        scope, entity_id, current, target, len(handlers),
    )

    for handler in handlers:
        try:
            LOGGER.info(
                "[upgrade] %s/%s: running handler %s (%s -> %s)",
                scope, entity_id, handler.name, handler.from_version, handler.to_version,
            )
            kwargs = {"session": session, scope: entity}
            handler.run(**kwargs)
        except Exception as exc:
            LOGGER.error(
                "[upgrade] %s/%s: handler %s FAILED: %s",
                scope, entity_id, handler.name, exc, exc_info=True,
            )
            session.rollback()
            return False

    entity.LAST_KNOWN_VERSION = str(target)
    try:
        session.commit()
    except Exception as exc:
        LOGGER.error(
            "[upgrade] %s/%s: failed to commit LAST_KNOWN_VERSION update: %s",
            scope, entity_id, exc, exc_info=True,
        )
        session.rollback()
        return False

    LOGGER.info(
        "[upgrade] %s/%s LAST_KNOWN_VERSION updated to %s",
        scope, entity_id, target,
    )
    return True


# ---- エントリポイント ----

def run_startup_upgrade(session: "Session") -> bool:
    """起動時に呼ぶエントリポイント。全 City / AI を target バージョンまで上げる。

    Args:
        session: 呼び出し側が用意した SQLAlchemy セッション

    Returns:
        True ... 全エンティティのアップグレードが成功（または不要）
        False ... どこかで失敗し、起動を中断すべき状態

    環境変数 ``SAIVERSE_SKIP_VERSION_CHECK=1`` でフックをスキップ可能。
    """
    if os.environ.get(ENV_SKIP_VERSION_CHECK) == "1":
        LOGGER.warning(
            "[upgrade] %s=1, skipping version check entirely",
            ENV_SKIP_VERSION_CHECK,
        )
        return True

    _load_default_handlers()

    try:
        target = current_version()
    except InvalidVersion:
        LOGGER.error("[upgrade] cannot determine current version, aborting")
        return False

    LOGGER.info("[upgrade] startup version check: target=%s, %d handlers registered",
                target, len(HANDLERS))

    from database.models import AI, City

    # City 単位 → AI 単位の順
    cities = session.query(City).all()
    for city in cities:
        ok = _run_handlers_for_entity(
            session,
            scope="city",
            entity=city,
            entity_id=str(city.CITYID),
            target=target,
        )
        if not ok:
            LOGGER.error("[upgrade] city upgrade failed, aborting startup")
            return False

    ais = session.query(AI).all()
    for ai in ais:
        ok = _run_handlers_for_entity(
            session,
            scope="ai",
            entity=ai,
            entity_id=ai.AIID,
            target=target,
        )
        if not ok:
            LOGGER.error("[upgrade] ai upgrade failed, aborting startup")
            return False

    LOGGER.info("[upgrade] startup version check completed successfully")
    return True
