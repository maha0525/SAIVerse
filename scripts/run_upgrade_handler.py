"""個別アップグレードハンドラを単独実行する管理スクリプト（テスト・デバッグ用）。

ユースケース:
    - ハンドラ実装の動作テスト
    - 冪等性の確認（同じハンドラを2回実行して状態が変わらないか）
    - --dry-run でハンドラの効果をログ確認しつつロールバック

設計詳細: docs/intent/version_aware_world_and_persona.md
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# プロジェクトルートを sys.path に追加
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from database.models import AI, City  # noqa: E402
from database.paths import default_db_path  # noqa: E402
from saiverse.upgrade import HANDLERS, _load_default_handlers  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

LOGGER = logging.getLogger("saiverse.run_upgrade_handler")


def _make_session(db_path: Path):
    engine = create_engine(f"sqlite:///{db_path}")
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return SessionLocal(), engine


def _list_handlers() -> int:
    _load_default_handlers()
    if not HANDLERS:
        print("(no handlers registered)")
        return 0
    print(f"{'Name':<35} {'Scope':<6} {'From -> To':<25} Description")
    print("-" * 100)
    for h in HANDLERS:
        transition = f"{h.from_version} -> {h.to_version}"
        desc = h.description or ""
        if len(desc) > 50:
            desc = desc[:47] + "..."
        print(f"{h.name:<35} {h.scope:<6} {transition:<25} {desc}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a single upgrade handler against a specific entity. "
                    "For testing / debugging only. Use --dry-run to roll back changes "
                    "after observing the effects via logs. "
                    "NOTE: --dry-run only rolls back the main saiverse.db session. "
                    "Side effects on other stores (SAIMemory persona DB, file system, etc.) "
                    "are NOT rolled back. Take a snapshot first if needed.",
    )
    parser.add_argument("name", nargs="?",
                        help="Handler name (e.g. v0_3_0_dynamic_state_reset). "
                             "Omit when --list is used.")
    target_group = parser.add_mutually_exclusive_group()
    target_group.add_argument("--persona", help="AI ID for ai-scope handlers")
    target_group.add_argument("--city", help="City ID for city-scope handlers")
    parser.add_argument("--dry-run", action="store_true",
                        help="Roll back saiverse.db changes after running. "
                             "Side effects on SAIMemory and other stores are NOT rolled back.")
    parser.add_argument("--db-file", help="Override DB path")
    parser.add_argument("--list", action="store_true", help="List all registered handlers")
    parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    if args.list:
        return _list_handlers()

    if not args.name:
        print("ERROR: handler name is required (or use --list).", file=sys.stderr)
        return 2

    _load_default_handlers()
    handler = next((h for h in HANDLERS if h.name == args.name), None)
    if handler is None:
        print(f"ERROR: handler {args.name!r} not found. Use --list to see registered handlers.",
              file=sys.stderr)
        return 1

    if handler.scope == "ai" and not args.persona:
        print(f"ERROR: handler {args.name!r} is ai-scope, --persona is required.", file=sys.stderr)
        return 2
    if handler.scope == "city" and not args.city:
        print(f"ERROR: handler {args.name!r} is city-scope, --city is required.", file=sys.stderr)
        return 2

    db_path = Path(args.db_file).resolve() if args.db_file else default_db_path()
    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        return 1

    print(f"Using DB: {db_path}")
    print(f"Handler: {handler.name} ({handler.from_version} -> {handler.to_version}, scope={handler.scope})")
    if args.dry_run:
        print("Mode: DRY-RUN (saiverse.db changes will be rolled back)")
        print("      WARNING: side effects on SAIMemory and other stores are NOT rolled back.")
    else:
        print("Mode: COMMIT")
    print()

    session, engine = _make_session(db_path)
    try:
        if handler.scope == "ai":
            entity = session.query(AI).filter_by(AIID=args.persona).first()
            if entity is None:
                print(f"ERROR: AI {args.persona!r} not found.", file=sys.stderr)
                return 1
            kwargs = {"session": session, "ai": entity}
        else:
            try:
                city_id: str | int = int(args.city)
            except ValueError:
                city_id = args.city
            entity = session.query(City).filter_by(CITYID=city_id).first()
            if entity is None:
                print(f"ERROR: City {args.city!r} not found.", file=sys.stderr)
                return 1
            kwargs = {"session": session, "city": entity}

        try:
            handler.run(**kwargs)
        except Exception as exc:
            LOGGER.error("Handler %s raised: %s", handler.name, exc, exc_info=True)
            session.rollback()
            print(f"ERROR: handler raised: {exc}", file=sys.stderr)
            return 1

        if args.dry_run:
            session.rollback()
            print("OK: handler ran successfully. Changes ROLLED BACK (dry-run).")
        else:
            session.commit()
            print("OK: handler ran successfully. Changes committed.")
        return 0
    finally:
        session.close()
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
