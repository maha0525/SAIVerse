"""Phase C-2 動作確認用デバッグスクリプト: Track の手動操作。

実機環境で別 Track を作成 / activate / pause / list するための CLI。
sqlite3 コマンドラインがない環境でも DB 状態を弄れるよう用意。

使用例:
  # ペルソナの Track 一覧を表示
  python scripts/debug_track.py list --persona air_city_a

  # 自律 Track を作成 + activate (対ユーザー Track が pending に押し出される)
  python scripts/debug_track.py create-autonomous \\
      --persona air_city_a --title "メモ整理" --intent "過去の会話を整理する"

  # 既存 Track を activate
  python scripts/debug_track.py activate --track-id <uuid>

  # 既存 Track を pause
  python scripts/debug_track.py pause --track-id <uuid>

注意:
  - サーバー起動中に DB を変更しても、SAIVerseManager のインメモリ状態とは
    同期しない。サーバー再起動で反映される (Phase C-2 範囲では仕方ない)。
  - 本スクリプトは debug 用、production 運用での使用は想定していない。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Make repo root importable
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from database.session import SessionLocal  # noqa: E402
from saiverse.track_manager import (  # noqa: E402
    TrackManager,
    TrackNotFoundError,
)


def _get_track_manager() -> TrackManager:
    return TrackManager(session_factory=SessionLocal)


def cmd_list(args: argparse.Namespace) -> int:
    tm = _get_track_manager()
    tracks = tm.list_for_persona(args.persona, include_forgotten=args.include_forgotten)
    if not tracks:
        print(f"(no tracks for persona={args.persona})")
        return 0
    print(f"Tracks for persona={args.persona}:")
    for t in tracks:
        title = t.title or "(no title)"
        intent = t.intent or ""
        intent_part = f" intent={intent[:40]!r}" if intent else ""
        forgotten_part = " [forgotten]" if t.is_forgotten else ""
        print(
            f"  - id={t.track_id} status={t.status} type={t.track_type} "
            f"persistent={bool(t.is_persistent)} title={title!r}{intent_part}{forgotten_part}"
        )
    return 0


def cmd_create_autonomous(args: argparse.Namespace) -> int:
    tm = _get_track_manager()
    metadata = {}
    if args.metadata_json:
        metadata = json.loads(args.metadata_json)
    track_id = tm.create(
        persona_id=args.persona,
        track_type="autonomous",
        title=args.title,
        intent=args.intent,
        is_persistent=False,
        output_target="none",
        metadata=json.dumps(metadata, ensure_ascii=False) if metadata else None,
    )
    print(f"Created autonomous track: {track_id}")
    if not args.no_activate:
        tm.activate(track_id)
        print(f"Activated track: {track_id} (any other running tracks were pushed to pending)")
    return 0


def cmd_activate(args: argparse.Namespace) -> int:
    tm = _get_track_manager()
    try:
        track = tm.activate(args.track_id)
    except TrackNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Activated: {track.track_id} (status now: {track.status})")
    return 0


def cmd_pause(args: argparse.Namespace) -> int:
    tm = _get_track_manager()
    try:
        track = tm.pause(args.track_id)
    except TrackNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Paused: {track.track_id} (status now: {track.status})")
    return 0


def cmd_delete_user_conversation(args: argparse.Namespace) -> int:
    """全 (or 指定ペルソナの) 対ユーザー Track を削除する。

    Phase C-2 / C-3 で Track コンテキスト注入仕様が変わったため、既存ペルソナ
    の対ユーザー Track を削除して、次のユーザー発話で新規作成パスを通すのに
    使う。新規作成 → activate (即 running) → Track コンテキスト注入が確実に走る。

    注意: 永続 Track (is_persistent=True) を物理削除するため、関連する
    metadata (last_pulse_at 等) も全て失われる。再作成された Track は新しい
    track_id を持つ。
    """
    from database.models import ActionTrack as ActionTrackModel
    tm = _get_track_manager()

    db = tm.SessionLocal()
    try:
        query = db.query(ActionTrackModel).filter_by(track_type="user_conversation")
        if args.persona:
            query = query.filter_by(persona_id=args.persona)
        targets = query.all()
        if not targets:
            print("(no user_conversation tracks found)")
            return 0
        for t in targets:
            print(
                f"Deleting user_conversation track: persona={t.persona_id} "
                f"track_id={t.track_id} title={t.title!r}"
            )
        count = query.delete(synchronize_session=False)
        db.commit()
        print(f"\nDone: deleted={count}")
        return 0
    except Exception as exc:
        db.rollback()
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


def cmd_pause_all_autonomous(args: argparse.Namespace) -> int:
    """全 (or 指定ペルソナの) running な autonomous Track を一括 pause する。

    SubLineScheduler が動いている状態で「動き出した自律行動を稼働中に止めたい」
    時の緊急停止用。SubLineScheduler は次の tick で running な autonomous Track が
    無いことを検知し、新規 Pulse を起動しなくなる (既に起動済みの Pulse は
    完了するまで止まらない)。

    Phase C-3b 緊急停止手段。SAIVERSE_SUBLINE_SCHEDULER_ENABLED=false で
    起動時に止めるのと違い、サーバー再起動なしで動的に止められる。
    """
    from saiverse.track_manager import STATUS_RUNNING
    tm = _get_track_manager()

    if args.persona:
        persona_ids = [args.persona]
    else:
        # 全ペルソナを巡回するため、AI テーブルから引く
        from database.models import AI as AIModel
        db = tm.SessionLocal()
        try:
            persona_ids = [row.AIID for row in db.query(AIModel).all()]
        finally:
            db.close()

    paused_count = 0
    skipped_count = 0
    for persona_id in persona_ids:
        running_tracks = tm.list_for_persona(
            persona_id, statuses=[STATUS_RUNNING]
        )
        for t in running_tracks:
            if t.track_type != "autonomous":
                continue
            try:
                tm.pause(t.track_id)
                paused_count += 1
                print(
                    f"Paused autonomous track: persona={persona_id} "
                    f"track_id={t.track_id} title={t.title!r}"
                )
            except Exception as exc:
                print(
                    f"Error pausing track {t.track_id}: {exc}", file=sys.stderr
                )
                skipped_count += 1

    if paused_count == 0 and skipped_count == 0:
        print("(no running autonomous tracks found)")
    else:
        print(f"\nDone: paused={paused_count}, errors={skipped_count}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List all tracks for a persona")
    p_list.add_argument("--persona", required=True, help="Persona ID (e.g., air_city_a)")
    p_list.add_argument("--include-forgotten", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_create = sub.add_parser(
        "create-autonomous",
        help="Create a new autonomous track (and activate by default)",
    )
    p_create.add_argument("--persona", required=True)
    p_create.add_argument("--title", required=True)
    p_create.add_argument("--intent", default=None, help="What this track aims to accomplish")
    p_create.add_argument("--metadata-json", default=None, help="Additional metadata as JSON string")
    p_create.add_argument(
        "--no-activate",
        action="store_true",
        help="Don't activate after creation (leave as unstarted)",
    )
    p_create.set_defaults(func=cmd_create_autonomous)

    p_activate = sub.add_parser("activate", help="Activate an existing track by ID")
    p_activate.add_argument("--track-id", required=True)
    p_activate.set_defaults(func=cmd_activate)

    p_pause = sub.add_parser("pause", help="Pause a running track by ID")
    p_pause.add_argument("--track-id", required=True)
    p_pause.set_defaults(func=cmd_pause)

    p_pause_all = sub.add_parser(
        "pause-all-autonomous",
        help=(
            "EMERGENCY STOP: pause ALL running autonomous tracks. "
            "Use this to stop runaway autonomous activity without restarting the server. "
            "SubLineScheduler will skip these tracks on next tick."
        ),
    )
    p_pause_all.add_argument(
        "--persona", default=None,
        help="Limit to specific persona (default: all personas)",
    )
    p_pause_all.set_defaults(func=cmd_pause_all_autonomous)

    p_delete_user_conv = sub.add_parser(
        "delete-user-conversation",
        help=(
            "Delete user_conversation tracks (so next user utterance recreates with "
            "current Track context format). Used after Phase C-2/C-3 spec changes."
        ),
    )
    p_delete_user_conv.add_argument(
        "--persona", default=None,
        help="Limit to specific persona (default: all personas)",
    )
    p_delete_user_conv.set_defaults(func=cmd_delete_user_conversation)

    args = parser.parse_args()

    # SAIVERSE_HOME が設定されていない場合の警告
    if not os.environ.get("SAIVERSE_HOME") and not os.environ.get("SAIVERSE_DB_PATH"):
        print(
            "[debug-track] Note: SAIVERSE_HOME / SAIVERSE_DB_PATH not set. "
            "Default DB path will be used. Make sure this matches the running server's DB.",
            file=sys.stderr,
        )

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
