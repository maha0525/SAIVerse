"""終了 (アプリを閉じる・落ちる) のとき、どの段階の内容がどこに残るかを実測する。

FLOW-26 (終了して、次に開いたとき失われていない) / 元議題 3。
前段 (docs/audits/2026-09-09_product_normal_behavior/flows/E_background.md §5) が
「実行して確かめていないので影響の大きさは断定しない」と残した部分のうち、
**永続層で決まる部分だけ**を実際に走らせて確定させる。

何を確かめるものか
------------------
利用者の入力・ペルソナの発話・まだ生成されていない要求の三つは、終了の瞬間に
それぞれ別の場所に居る。ここで測るのは前二つの「保存先の側」で、次の四点。

1. 利用者の送った文は、ペルソナが一言も喋る前に建物の記録へ確定しているか
   (`manager/runtime.py:507-557` の `_persist_user_utterance` と同じ呼び出し)。
2. ペルソナの発話は、確定 (`emit_speak_finalize` 相当) を通った回と
   通らなかった回で、建物の記録にどう残るか。
3. その二つを、画面 (履歴 API) と取り込み (ペルソナ記憶へ写す側) が
   それぞれどう見るか。両方が content 空の行を外す規則なので、
   「確定しなかった行 = 誰にも見えない」が本当に成り立つかを数える。
4. 通信が切れた利用者のための問い合わせ口
   (`lookup_client_message_outcome`) が、確定しなかった発話を
   「応答あり」と誤って数えないか。

どう実行するか
--------------
    .venv/Scripts/python.exe docs/audits/2026-09-10_normal_behavior_remaining_evidence/repro/a3_shutdown_utterance_survival.py

本番には一切触らない。一時ディレクトリに新しい SQLite を作り、
`database/models.py` のスキーマをそのまま起こして製品の永続層関数
(`database/building_messages.py`) を直接呼ぶ。LLM は呼ばない。
`SAIVERSE_HOME` も一時ディレクトリへ向ける。

何が観測されたか (2026-09-10 実行)
----------------------------------
- 利用者の入力: `insert_building_message_with_location_guard` は呼んだ時点で
  commit まで済み、以後どの経路で落ちても行は残る。画面にも取り込みにも出る。
- 確定を通った発話 (途中まで + 中断の印): 本文が入り、画面にも取り込みにも出る。
  `_interrupted` は metadata に残るので、次に開いたときも「続きの生成」の材料が残る。
- 確定を通らなかった発話 (締切に間に合わなかった回): `content=""` のまま残り、
  **画面の履歴も取り込みも 0 件で外す**。本文はどこにも無い。
- 問い合わせ口: 確定しなかった行は `has_reply=False` を返す
  (`_has_assistant_reply_after` が `content != ""` で絞るため)。
  つまり「応答が付いた」と嘘をつく側へは倒れない。
- 中断の通告 (host 行) は heard_by に載った相手の取り込み対象になる。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

# Windows の既定コンソール (cp932) だと本文の全角ダッシュで落ちる。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BUILDING_ID = "synthetic_room"
PERSONA_ID = "synthetic_persona"
USER_ID = "1"
CLIENT_MESSAGE_ID = "11111111-2222-3333-4444-555555555555"


def _make_session_factory(db_path: Path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from database.models import Base

    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _history_api_view(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """画面の履歴が残す行 (api/routes/chat.py:296-301 と同じ規則)。"""
    return [row for row in rows if row.get("content")]


def _ingest_view(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """取り込みが記憶へ写す候補 (builtin_data/tools/get_building_messages.py:203-207)。

    同関数は content が空の行を ``("consumed", None, False)`` で飛ばす
    (= 記憶へ書かずマークもせず次へ進む)。ここではその規則だけを写す。
    """
    return [row for row in rows if row.get("content")]


def main() -> int:
    tmp_root = Path(tempfile.mkdtemp(prefix="a3_shutdown_"))
    os.environ["SAIVERSE_HOME"] = str(tmp_root / "home")
    os.environ["SAIVERSE_USER_DATA_DIR"] = str(tmp_root / "home" / "user_data")

    from database.building_messages import (
        fetch_building_messages,
        insert_building_message,
        insert_building_message_with_location_guard,
        lookup_client_message_outcome,
        update_building_message_in_db,
    )

    session_factory = _make_session_factory(tmp_root / "world.db")

    print("=" * 72)
    print("段階 1: 利用者の入力 — 認知が始まる前に確定するか")
    print("=" * 72)
    saved_user = insert_building_message_with_location_guard(
        session_factory,
        BUILDING_ID,
        {
            "role": "user",
            "content": "今日はここまでにするね",
            "timestamp": "2026-09-10T03:00:00+09:00",
            "heard_by": [PERSONA_ID, USER_ID],
            "ingested_by": [],
            "client_message_id": CLIENT_MESSAGE_ID,
        },
        user_id=USER_ID,
        # 合成環境には user 行が無いので、現在地照合は同関数の fail-open 分岐
        # (docstring「user 行が引けない環境は検証をスキップ」) を通る。
        expected_building_id=BUILDING_ID,
    )
    print(f"  返り値: message_id={saved_user and saved_user.get('message_id')} "
          f"_was_inserted={saved_user and saved_user.get('_was_inserted')}")
    after_insert = fetch_building_messages(session_factory, BUILDING_ID)
    print(f"  この時点で建物の記録にある行数: {len(after_insert)}")
    print("  → 認知 (Pulse) を一度も起こしていないのに、既に永続している。"
          "ここから先のどの段階で落ちても利用者の文は残る。")

    print()
    print("=" * 72)
    print("段階 2: ペルソナの発話 — 下書き行を置く (emit_speak_start 相当)")
    print("=" * 72)
    placeholder_settled = insert_building_message(
        session_factory, BUILDING_ID,
        {
            "role": "assistant",
            "content": "",
            "persona_id": PERSONA_ID,
            "timestamp": "2026-09-10T03:00:01+09:00",
            "heard_by": [PERSONA_ID, USER_ID],
            "ingested_by": [],
            "metadata": {"tags": ["conversation"], "_streaming_placeholder": True},
        },
    )
    placeholder_lost = insert_building_message(
        session_factory, BUILDING_ID,
        {
            "role": "assistant",
            "content": "",
            "persona_id": PERSONA_ID,
            "timestamp": "2026-09-10T03:00:02+09:00",
            "heard_by": [PERSONA_ID, USER_ID],
            "ingested_by": [],
            "metadata": {"tags": ["conversation"], "_streaming_placeholder": True},
        },
    )
    settled_id = str(placeholder_settled["message_id"])
    lost_id = str(placeholder_lost["message_id"])
    print(f"  下書き行を 2 本置いた: settled={settled_id} / lost={lost_id}")
    print("  どちらも content='' で、まだ本文の器でしかない。")

    print()
    print("=" * 72)
    print("段階 3-A: 締切に間に合った回 (_settle_interrupted_utterance を通る)")
    print("=" * 72)
    update_building_message_in_db(
        session_factory, BUILDING_ID, settled_id,
        content="うん、また明日ね。今日は",
        metadata={
            "tags": ["conversation"],
            "_streaming_placeholder": False,
            "_interrupted": True,
        },
    )
    insert_building_message(
        session_factory, BUILDING_ID,
        {
            "role": "host",
            "content": "(ここで発言が中断されました)",
            "timestamp": "2026-09-10T03:00:03+09:00",
            "heard_by": [PERSONA_ID, USER_ID],
            "ingested_by": [],
        },
    )
    print("  本文を確定 + '言い切っていない' 印 + 中断の通告 (host 行) を置いた。")

    print()
    print("=" * 72)
    print("段階 3-B: 締切に間に合わなかった回 (確定が走らないままプロセスが死ぬ)")
    print("=" * 72)
    print("  何もしない。下書き行は content='' のまま残る。")

    print()
    print("=" * 72)
    print("段階 4: 次に開いたとき、それぞれの受け手に何が見えるか")
    print("=" * 72)
    rows = fetch_building_messages(session_factory, BUILDING_ID)
    print(f"  building_messages にある行数 (生): {len(rows)}")
    for row in rows:
        meta = row.get("metadata") or {}
        print(
            f"    - seq={row.get('seq')} role={row.get('role'):9s} "
            f"content={json.dumps(row.get('content'), ensure_ascii=False)} "
            f"placeholder={meta.get('_streaming_placeholder')} "
            f"interrupted={meta.get('_interrupted')}"
        )

    visible = _history_api_view(rows)
    ingestible = _ingest_view(rows)
    print()
    print(f"  画面 (履歴 API) が描く行数: {len(visible)}")
    print(f"  取り込み (ペルソナ記憶へ写す) の候補行数: {len(ingestible)}")
    lost_visible = any(str(r.get("message_id")) == lost_id for r in visible)
    lost_ingestible = any(str(r.get("message_id")) == lost_id for r in ingestible)
    print(f"  締切に間に合わなかった行は画面に出るか: {lost_visible}")
    print(f"  締切に間に合わなかった行は記憶へ入るか: {lost_ingestible}")
    print("  → どちらも False なら、その発話は本文も痕跡も利用者・ペルソナの"
          "どちらにも届かない。")

    print()
    print("=" * 72)
    print("段階 5: 通信が切れた利用者の問い合わせ口が何を返すか")
    print("=" * 72)
    outcome = lookup_client_message_outcome(session_factory, CLIENT_MESSAGE_ID)
    print(f"  lookup_client_message_outcome -> {outcome}")
    print("  has_reply の判定は content 空の行を数えない "
          "(database/building_messages.py:718-745)。")

    print()
    print("=" * 72)
    print("段階 6: 確定を通った発話しか無い場合の has_reply")
    print("=" * 72)
    # 段階 3-A の行を消して、空の下書き行だけが残る状態を作り直す。
    from database.models import BuildingMessage
    db = session_factory()
    try:
        db.query(BuildingMessage).filter(
            BuildingMessage.building_id == BUILDING_ID,
            BuildingMessage.message_id == settled_id,
        ).delete()
        db.query(BuildingMessage).filter(
            BuildingMessage.building_id == BUILDING_ID,
            BuildingMessage.role == "host",
        ).delete()
        db.commit()
    finally:
        db.close()
    outcome_only_lost = lookup_client_message_outcome(session_factory, CLIENT_MESSAGE_ID)
    print("  確定した発話と通告を取り除き、空の下書き行だけを残した状態:")
    print(f"  lookup_client_message_outcome -> {outcome_only_lost}")
    print("  → status=found かつ has_reply=False なら、利用者の文は残っていて"
          "「返事はまだ無い」と正しく答えている。")

    print()
    print(f"一時ディレクトリ: {tmp_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
