"""City / AI の LAST_KNOWN_VERSION を直接書き換える管理スクリプト。

テスト・デバッグ専用。本番環境では使わない。

ユースケース:
    - アップグレードハンドラの動作テスト（"v0.2.5 から来たことにする"）
    - 冪等性確認（同じハンドラを再実行する前にバージョンを戻す）
    - 起動時バージョン比較フックの動作確認

設計詳細: docs/intent/version_aware_world_and_persona.md
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# プロジェクトルートを sys.path に追加（database / saiverse パッケージを import するため）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from database.models import AI, City  # noqa: E402
from database.paths import default_db_path  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

LOGGER = logging.getLogger("saiverse.set_version")

NULL_TOKEN = "null"


def _make_session(db_path: Path):
    """与えられた DB パスから SQLAlchemy セッションを作る。"""
    engine = create_engine(f"sqlite:///{db_path}")
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return SessionLocal(), engine


def _resolve_target(value: str) -> str | None:
    """``--to`` の引数を DB に書く値に変換。``"null"`` は ``None`` (NULL)。"""
    if value.strip().lower() == NULL_TOKEN:
        return None
    return value.strip()


def _set_city_version(session, city_id: str | int, target: str | None) -> bool:
    city = session.query(City).filter_by(CITYID=city_id).first()
    if city is None:
        print(f"ERROR: City {city_id!r} not found.", file=sys.stderr)
        return False
    old = city.LAST_KNOWN_VERSION
    city.LAST_KNOWN_VERSION = target
    print(f"city/{city.CITYID}: {old!r} -> {target!r}")
    return True


def _set_ai_version(session, ai_id: str, target: str | None) -> bool:
    ai = session.query(AI).filter_by(AIID=ai_id).first()
    if ai is None:
        print(f"ERROR: AI {ai_id!r} not found.", file=sys.stderr)
        return False
    old = ai.LAST_KNOWN_VERSION
    ai.LAST_KNOWN_VERSION = target
    print(f"ai/{ai.AIID}: {old!r} -> {target!r}")
    return True


def _set_all_versions(session, target: str | None) -> bool:
    cities = session.query(City).all()
    ais = session.query(AI).all()
    for c in cities:
        old = c.LAST_KNOWN_VERSION
        c.LAST_KNOWN_VERSION = target
        print(f"city/{c.CITYID}: {old!r} -> {target!r}")
    for a in ais:
        old = a.LAST_KNOWN_VERSION
        a.LAST_KNOWN_VERSION = target
        print(f"ai/{a.AIID}: {old!r} -> {target!r}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Set LAST_KNOWN_VERSION on City / AI rows for testing the "
                    "version-aware upgrade system. Pass 'null' as --to to clear "
                    "the column (simulating a pre-version-aware row).",
    )
    target_group = parser.add_mutually_exclusive_group(required=True)
    target_group.add_argument("--city", help="City ID (CITYID) to update")
    target_group.add_argument("--ai", help="AI ID (AIID / persona_id) to update")
    target_group.add_argument("--all", action="store_true",
                              help="Update every City and AI row")

    parser.add_argument("--to", required=True,
                        help="Target version string (e.g. '0.2.5'), or 'null' to clear")
    parser.add_argument("--db-file", help="Override DB path (default: ~/.saiverse/user_data/database/saiverse.db)")
    parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    target = _resolve_target(args.to)

    db_path = Path(args.db_file).resolve() if args.db_file else default_db_path()
    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        return 1

    print(f"Using DB: {db_path}")
    print(f"Target version: {target!r} (None means NULL)")

    session, engine = _make_session(db_path)
    try:
        if args.all:
            ok = _set_all_versions(session, target)
        elif args.city is not None:
            # CITYID は数値カラム。文字列のまま渡しても SQLAlchemy が変換するが、念のため
            try:
                city_id: str | int = int(args.city)
            except ValueError:
                city_id = args.city
            ok = _set_city_version(session, city_id, target)
        elif args.ai is not None:
            ok = _set_ai_version(session, args.ai, target)
        else:
            print("ERROR: must specify --city, --ai, or --all", file=sys.stderr)
            return 2

        if ok:
            session.commit()
            print("OK: changes committed.")
            return 0
        session.rollback()
        return 1
    except Exception as exc:
        LOGGER.error("set_version failed: %s", exc, exc_info=True)
        session.rollback()
        return 1
    finally:
        session.close()
        engine.dispose()


if __name__ == "__main__":
    # main.py の data_paths 初期化に倣い、SAIVERSE_HOME 等の環境変数は親プロセスから継承される前提
    sys.exit(main())
