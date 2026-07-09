#!/usr/bin/env python
"""会話シナリオランナー — 台本をサンドボックスのペルソナへ実チャット経路で流す。

「応答がおかしい」系の再現・前後比較を、チャット UI に張り付かずに回すための
軽量ランナー。設計意図と不変条件は docs/intent/conversation_runner.md。

Usage:
    # 台本ファイル (形式は intent doc §3)
    python scripts/run_conversation.py --script test_fixtures/conversations/greeting.json

    # 台本なしの単発
    python scripts/run_conversation.py --persona quon_city_a \\
        --message "おはよう" --message "昨日は何をしてたの？"

前提:
    - テスト環境 (clone_world_to_test_env.py 推奨) が test_data/ にあること。
      SAIVERSE_HOME / SAIVERSE_USER_DATA_DIR 未設定時は自動で test_data/ を指す
    - 対象ペルソナが ACTIVITY_STATE != 'Stop' であること (world clone の --persona
      で指定した写し身)
    - **実 LLM を呼ぶ (実コスト発生)**。API キーは .env から読む

安全性:
    - DB が本番 (~/.saiverse 配下) を指す場合は起動を拒否する (記憶汚染防止)。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LOGGER = logging.getLogger("scripts.run_conversation")

DEFAULT_HOME = ROOT / "test_data" / ".saiverse"
DEFAULT_USER_DATA = ROOT / "test_data" / "user_data"
DEFAULT_DB = DEFAULT_USER_DATA / "database" / "saiverse.db"


class ConversationError(RuntimeError):
    """前提未達 (本番 DB / ペルソナ不在 / 台本不正)。メッセージをそのまま表示する。"""


# ---------------------------------------------------------------------------
# 台本
# ---------------------------------------------------------------------------


def load_conversation_script(path: Path) -> Dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return normalize_script(data)


def normalize_script(data: Dict[str, Any]) -> Dict[str, Any]:
    persona_id = data.get("persona_id")
    messages = data.get("messages")
    if not persona_id or not isinstance(persona_id, str):
        raise ConversationError("台本に persona_id (文字列) が必要です")
    if not messages or not isinstance(messages, list) \
            or not all(isinstance(m, str) and m.strip() for m in messages):
        raise ConversationError("台本に messages (空でない文字列の配列) が必要です")
    return {
        "persona_id": persona_id,
        "title": str(data.get("title") or "会話シナリオ"),
        "messages": [m.strip() for m in messages],
        "leave": bool(data.get("leave", True)),
    }


# ---------------------------------------------------------------------------
# 実行
# ---------------------------------------------------------------------------


def _collect_new_building_messages(manager: Any, building_id: str, since_id: int):
    """building_messages の増分 (id > since_id) を返す。"""
    from database.models import BuildingMessage
    db = manager.SessionLocal()
    try:
        return (
            db.query(BuildingMessage)
            .filter(BuildingMessage.building_id == building_id)
            .filter(BuildingMessage.id > since_id)
            .order_by(BuildingMessage.id)
            .all()
        )
    finally:
        db.close()


def _max_building_message_id(manager: Any) -> int:
    from sqlalchemy import func as sqla_func
    from database.models import BuildingMessage
    db = manager.SessionLocal()
    try:
        return db.query(sqla_func.coalesce(sqla_func.max(BuildingMessage.id), 0)).scalar()
    finally:
        db.close()


def run_conversation(manager: Any, script: Dict[str, Any], *, driver: Any = None) -> List[Dict[str, Any]]:
    """台本を 1 本流し、transcript エントリの列を返す。

    各エントリ: {"user": 発話, "replies": [building_messages 増分行の dict]}
    manager.pulse_controller は同期ディスパッチャ済みであること (呼び出し側の責務)。
    """
    if driver is None:
        from saiverse.day_scenario import RealConversationUserEventDriver
        driver = RealConversationUserEventDriver()

    persona_id = script["persona_id"]
    persona = (getattr(manager, "personas", None) or {}).get(persona_id)
    if persona is None:
        available = sorted((getattr(manager, "personas", None) or {}).keys())
        raise ConversationError(
            f"ペルソナ '{persona_id}' が manager にいません。存在するのは: {available}"
        )

    transcript: List[Dict[str, Any]] = []
    for text in script["messages"]:
        building_id = getattr(persona, "current_building_id", None)
        since_id = _max_building_message_id(manager)
        LOGGER.info("user → %s: %r", persona_id, text[:80])
        driver.begin_conversation(manager, persona_id, text)
        rows = _collect_new_building_messages(manager, building_id, since_id)
        replies = [
            {
                "role": r.role,
                "persona_id": r.persona_id,
                "content": r.content,
                "timestamp": r.timestamp,
                "event_type": r.event_type,
            }
            for r in rows
            # ユーザー発話自身は driver が記録した行なので transcript では区別する
            if not (r.role == "user" and r.content == text)
        ]
        transcript.append({"user": text, "replies": replies})
        if not any(r["role"] == "assistant" for r in replies):
            LOGGER.warning("ペルソナ応答なし (persona=%s text=%r)", persona_id, text[:60])

    if script["leave"]:
        ended = driver.end_conversation(manager, persona_id)
        LOGGER.info("leave: conversation track ended=%s", ended)
    return transcript


def format_transcript(script: Dict[str, Any], transcript: List[Dict[str, Any]]) -> str:
    lines = [f"# 会話 transcript — {script['persona_id']}: {script['title']}"]
    lines.append("")
    lines.append(f"- 実行日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- ターン数: {len(transcript)} / leave: {script['leave']}")
    lines.append("")
    for i, entry in enumerate(transcript, start=1):
        lines.append(f"## ターン {i}")
        lines.append("")
        lines.append(f"**user**: {entry['user']}")
        lines.append("")
        assistant_seen = False
        for r in entry["replies"]:
            if r["role"] == "assistant":
                assistant_seen = True
                lines.append(f"**{r['persona_id']}** [{r['timestamp']}]:")
                lines.append("")
                lines.append(str(r["content"]))
            else:
                label = r["event_type"] or r["role"]
                lines.append(f"> ({label}) {r['content']}")
            lines.append("")
        if not assistant_seen:
            lines.append("(応答なし)")
            lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _guard_not_production(db_path: Path) -> None:
    """本番 DB (~/.saiverse 配下) への実行をハード拒否する (intent doc §2-1)。"""
    production_root = Path(os.getenv("SAIVERSE_HOME") or Path.home() / ".saiverse")
    try:
        db_path.resolve().relative_to(production_root.resolve())
    except ValueError:
        return  # 本番の外 — OK
    raise ConversationError(
        f"DB が本番 ({production_root}) を指しています。会話テストは偽の記憶を"
        "committed するため、本番には実行できません。"
        " clone_world_to_test_env.py でテスト環境を作ってください。"
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="台本会話をサンドボックスのペルソナへ実チャット経路で流す",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--script", type=Path, default=None, help="台本 JSON ファイル")
    parser.add_argument("--persona", default=None, help="台本なし実行時のペルソナ ID")
    parser.add_argument("--message", action="append", default=[],
                        help="台本なし実行時のユーザー発話 (複数指定可)")
    parser.add_argument("--no-leave", action="store_true",
                        help="最後に退室しない (Track を running のまま残す)")
    parser.add_argument("--city", default="city_a", help="City 名 (default: city_a)")
    parser.add_argument("--db-file", type=Path, default=DEFAULT_DB,
                        help=f"SQLite DB パス (default: {DEFAULT_DB})")
    parser.add_argument("--out", type=Path, default=None,
                        help="transcript の出力先 (省略時: test_data/conversations/<persona>_<時刻>.md)")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    # saiverse モジュールの import 前に環境をテスト側へ倒す (intent doc §2-4)。
    # 呼び出し側が明示設定していればそれを尊重する。
    os.environ.setdefault("SAIVERSE_HOME", str(DEFAULT_HOME))
    os.environ.setdefault("SAIVERSE_USER_DATA_DIR", str(DEFAULT_USER_DATA))

    try:
        if args.script:
            script = load_conversation_script(args.script)
        else:
            if not args.persona or not args.message:
                raise ConversationError(
                    "--script か、--persona + --message (1 回以上) のどちらかが必要です")
            script = normalize_script({
                "persona_id": args.persona,
                "messages": args.message,
                "leave": not args.no_leave,
            })
        if args.no_leave:
            script["leave"] = False

        db_path = args.db_file.resolve()
        if not db_path.is_file():
            raise ConversationError(
                f"DB が見つかりません: {db_path}。"
                " 先に python scripts/clone_world_to_test_env.py を実行してください。")
        _guard_not_production(db_path)

        # ここから saiverse を import (env 確定後)
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")

        from saiverse.day_scenario import SyncJudgmentDispatcher
        from saiverse.saiverse_manager import SAIVerseManager

        manager = SAIVerseManager(
            city_name=args.city, db_path=str(db_path),
            sds_url="http://127.0.0.1:8080",
        )
        original_controller = manager.pulse_controller
        manager.pulse_controller = SyncJudgmentDispatcher(manager)
        try:
            transcript = run_conversation(manager, script)
        finally:
            manager.pulse_controller = original_controller

        text = format_transcript(script, transcript)
        print(text)
        out_path = args.out
        if out_path is None:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_path = ROOT / "test_data" / "conversations" / f"{script['persona_id']}_{stamp}.md"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        LOGGER.info("transcript saved: %s", out_path)
    except ConversationError as exc:
        LOGGER.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
