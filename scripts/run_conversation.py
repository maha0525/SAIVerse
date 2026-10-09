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
    - **実 LLM を呼ぶ (実コスト発生)**。API キーは .env から読む

安全性:
    - DB / SAIVERSE_HOME / SAIVERSE_USER_DATA_DIR のどれかが本番 (~/.saiverse 配下) を
      指す場合は起動を拒否する (記憶汚染防止)。本番の場所は SAIVERSE_HOME の値に依らない。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# saiverse を import しない純粋なパス判定なので、env を確定する前に読み込んでよい
from scripts._shared.production_guard import (  # noqa: E402
    effective_home,
    effective_user_data_dir,
    refuse_production_paths,
)

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
# 実チャット経路のドライバと同期ディスパッチャ
#
# どちらも元は一日シム (saiverse/day_scenario.py) にあった。一日シムが時間割
# ごと撤去された (autonomous_behavior_v04_plan.md 段 1-4) ので、唯一残った
# 使い手のこのランナーへ移した。
# ---------------------------------------------------------------------------


class RealConversationUserEventDriver:
    """ユーザー発話を本物の会話経路へ注入するドライバ。

    実チャット経路 (``manager/runtime.py`` ``handle_user_input_stream`` の
    backend_worker) と同じ順序で正規経路を叩く:

    1. ユーザー発話を building_messages へ記録 (heard_by = ペルソナ + ユーザー)
    2. 会話が開いていなければ ``saiverse.user_conversation.start_conversation``
       — 会話状態を立て、main_line Pulse (``manager.run_sea_user``) を起動し、
       沈黙タイマーを張る。Pulse 冒頭の auto_ingest が (1) の発話をペルソナ記憶
       (memory.db) へ取り込む。Pulse は :class:`SyncJudgmentDispatcher` の
       ``submit_user`` 経由で呼び出しスレッド上で同期実行される
    3. 会話中の追加メッセージは実経路の「会話が開いている → 直接メインライン
       起動」と同型に ``manager.run_sea_user`` を直接呼ぶ
    4. Pulse 後にペルソナ応答が building_messages に実在するかを検査し、応答ゼロ
       なら WARNING に残す (観察のみ)

    前提: manager は実 SAIVerseManager (persona に history_manager がある)。
    """

    def begin_conversation(self, manager: Any, persona_id: str, text: str) -> None:
        from saiverse.user_conversation import (
            get_open_conversation,
            start_conversation,
        )

        persona = (getattr(manager, "personas", None) or {}).get(persona_id)
        if persona is None:
            raise RuntimeError(f"persona '{persona_id}' not found on manager")
        building_id = getattr(persona, "current_building_id", None)
        if not building_id:
            raise RuntimeError(f"persona '{persona_id}' has no current building")

        # (1) ユーザー発話を building_messages へ記録 (実チャット経路の pre-add)
        seq_before = self._record_user_message(manager, persona, building_id, text)

        if get_open_conversation(manager, persona_id) is not None:
            # (3) 会話継続: 実経路の「会話が開いている → 直接メインライン起動」と同型
            LOGGER.info(
                "user message in ongoing conversation (persona=%s); invoking "
                "main line directly", persona_id,
            )
            manager.run_sea_user(persona, building_id, text)
        else:
            # (2) 会話開始: 実経路と同じ入口 (会話状態 + main_line + タイマー)
            start_conversation(manager, persona_id, str(getattr(manager, "user_id", "")))
            LOGGER.info(
                "conversation started via real path: persona=%s text=%r",
                persona_id, text[:60],
            )

        # (4) 応答の実在検査 (building_messages の追記で確認 — 接地)
        replied = self._persona_replied_after(manager, persona, building_id, seq_before)
        if not replied:
            LOGGER.warning(
                "persona did not reply to user message (persona=%s text=%r) — "
                "this conversation has no exchange yet", persona_id, text[:60],
            )

    def end_conversation(self, manager: Any, persona_id: str) -> bool:
        """leave: 開いている会話状態を落とす (本番の沈黙タイマー経路に相当)。

        Returns:
            会話が実際に終了した (= 会話中だった) なら True。
        """
        from saiverse.autonomy_wiring import handle_conversation_end
        from saiverse.user_conversation import get_open_conversation

        if get_open_conversation(manager, persona_id) is None:
            LOGGER.warning(
                "leave but no conversation is open (persona=%s); ignoring",
                persona_id,
            )
            return False
        try:
            handle_conversation_end(manager, persona_id)
        except Exception:
            LOGGER.warning(
                "failed to close the conversation state (persona=%s)",
                persona_id, exc_info=True,
            )
        LOGGER.info("conversation ended: persona=%s", persona_id)
        return True

    @staticmethod
    def _canonical_building_id(manager: Any, building_id: str) -> str:
        """実 manager の building_id 正規化 (無ければ素通し)。"""
        runtime = getattr(manager, "runtime", None)
        fn = getattr(runtime, "_canonical_building_id", None)
        if callable(fn):
            try:
                return fn(building_id)
            except Exception:
                LOGGER.warning(
                    "_canonical_building_id failed for %r; using it as-is",
                    building_id, exc_info=True,
                )
        return building_id

    def _record_user_message(
        self, manager: Any, persona: Any, building_id: str, text: str
    ) -> int:
        """ユーザー発話を building_messages へ記録し、その seq を返す。

        実チャット経路 (backend_worker) と同じ ``add_to_building_only`` +
        heard_by。auto_ingest は heard_by にペルソナが居るメッセージだけを
        取り込むため、heard_by は必須。
        """
        history_manager = getattr(persona, "history_manager", None)
        if history_manager is None:
            raise RuntimeError(
                f"persona '{persona.persona_id}' has no history_manager — "
                "RealConversationUserEventDriver は実 SAIVerseManager 専用です"
            )
        canonical_bid = self._canonical_building_id(manager, building_id)
        heard = [persona.persona_id]
        user_id = getattr(manager, "user_id", None)
        if user_id is not None:
            heard.append(str(user_id))
        saved = history_manager.add_to_building_only(
            canonical_bid, {"role": "user", "content": text}, heard_by=heard,
        )
        try:
            return int((saved or {}).get("seq") or 0)
        except (TypeError, ValueError):
            return 0

    def _persona_replied_after(
        self, manager: Any, persona: Any, building_id: str, seq_before: int
    ) -> bool:
        """seq_before より後にペルソナの assistant 発言が実在するか (接地検査)。"""
        canonical_bid = self._canonical_building_id(manager, building_id)
        try:
            hist = persona.history_manager.get_building_history(canonical_bid) or []
        except Exception:
            LOGGER.warning(
                "failed to read building history for reply check "
                "(persona=%s building=%s)",
                persona.persona_id, canonical_bid, exc_info=True,
            )
            return False
        for msg in hist:
            try:
                seq = int(msg.get("seq") or 0)
            except (TypeError, ValueError):
                seq = 0
            if seq <= seq_before:
                continue
            if msg.get("role") == "assistant" and msg.get("persona_id") == persona.persona_id:
                return True
        return False


class SyncJudgmentDispatcher:
    """同期 Pulse ディスパッチャ (``manager.pulse_controller`` 互換)。

    実 ``PulseController`` はレーン管理 (優先度・並列メタ判断レーン・キュー) を
    持つ。本ディスパッチャは ``manager.sea_runtime.run_meta_user`` を呼び出し
    スレッドでそのまま実行する (Playbook・finalize・SAIMemory 書き込みはすべて
    正規経路)。ランナーの実行中だけ ``manager.pulse_controller`` を差し替える。

    叩かれる入口は 2 つ:

    - ``submit_user``: ユーザー会話 Pulse (``saiverse.user_conversation`` →
      ``manager.run_sea_user``)。実 ``PulseController.submit_user`` と同シグネチャ
    - ``submit_meta_judgment``: 判断点 (``run_judgment_point``) の起動経路
    """

    def __init__(self, manager: Any) -> None:
        self.manager = manager

    def _require_persona(self, persona_id: str) -> Any:
        persona = (getattr(self.manager, "personas", None) or {}).get(persona_id)
        if persona is None:
            raise RuntimeError(f"persona '{persona_id}' not found on manager")
        return persona

    def submit_meta_judgment(
        self,
        persona_id: str,
        building_id: str,
        meta_playbook: str,
        args: Optional[Dict[str, Any]] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Optional[List[str]]:
        persona = self._require_persona(persona_id)
        return self.manager.sea_runtime.run_meta_user(
            persona,
            user_input=None,
            building_id=building_id,
            meta_playbook=meta_playbook,
            args=args,
            event_callback=event_callback,
            pulse_type="meta_judgment",
        )

    def submit_user(
        self,
        persona_id: str,
        building_id: str,
        user_input: str,
        metadata: Optional[Dict[str, Any]] = None,
        meta_playbook: Optional[str] = None,
        args: Optional[Dict[str, Any]] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        pre_spells: Optional[List[str]] = None,
        pre_generation_check: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
    ) -> Optional[List[str]]:
        """ユーザー会話 Pulse を呼び出しスレッドで同期実行する。

        実 ``PulseController.submit_user`` → ``_do_execute`` と同じく
        ``run_meta_user(pulse_type="user")`` (CONVERSATION アスペクト) を叩く。
        """
        persona = self._require_persona(persona_id)
        return self.manager.sea_runtime.run_meta_user(
            persona,
            user_input=user_input,
            building_id=building_id,
            metadata=metadata,
            meta_playbook=meta_playbook,
            args=args,
            event_callback=event_callback,
            pre_spells=pre_spells,
            pulse_type="user",
            pre_generation_check=pre_generation_check,
        )


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


def _guard_not_production(
    db_path: Path,
    home: Path,
    user_data_dir: Path,
    out_path: Optional[Path] = None,
) -> None:
    """この実行が書く場所のどれかが本番 (~/.saiverse 配下) ならハード拒否する (intent doc §2-1)。

    DB だけでなく、ペルソナの memory.db や建物ログが書かれる SAIVERSE_HOME、
    SAIVERSE_USER_DATA_DIR、transcript の出力先も検査する。
    本番の場所は SAIVERSE_HOME の値に依らない。
    """
    refuse_production_paths(
        {
            "--db-file": db_path,
            "SAIVERSE_HOME": home,
            "SAIVERSE_USER_DATA_DIR": user_data_dir,
            "--out": out_path,
        },
        reason=(
            "会話テストは偽の記憶を committed するため、本番には実行できません。"
            " clone_world_to_test_env.py でテスト環境を作ってください。"
        ),
        error_cls=ConversationError,
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
        # env を直接読まず data_paths と同じ導出で解決する (空文字の env は「未設定 = ~/.saiverse」扱い)
        _guard_not_production(db_path, effective_home(), effective_user_data_dir(), args.out)
        if not db_path.is_file():
            raise ConversationError(
                f"DB が見つかりません: {db_path}。"
                " 先に python scripts/clone_world_to_test_env.py を実行してください。")

        # ここから saiverse を import (env 確定後)
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")

        # テストの世界を本番の Discord ゲートウェイにつながせない (.env の本番設定を、ゲートウェイを止める値で上書き)
        from scripts._shared.gateway_isolation import force_discord_gateway_off
        force_discord_gateway_off()

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
