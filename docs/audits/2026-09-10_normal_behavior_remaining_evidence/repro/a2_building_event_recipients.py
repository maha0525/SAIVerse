"""建物の出来事は誰に届くのか — add_building_event の受け手を実測する。

何を確かめるものか
------------------
``manager/history.py`` の :meth:`HistoryMixin.add_building_event` は受け手を
``heard_by`` 引数で受け取る。呼び出し元には渡す側 (World Event / Blueprint /
City Transfer / game lifecycle / OccupancyManager) と渡さない側
(``_append_building_history_note`` = アイテム系すべて、
``ObserverManager._notify_building``) が同居している。

渡さないと DB の ``heard_by`` 列が ``[]`` になり、建物 → ペルソナ記憶への
転記 (``builtin_data/tools/get_building_messages.py`` の auto_ingest) が
「heard_by に自分が居ない」で読み飛ばす、という読みが正しいかを実測する。

観測は 3 種類の行で行う:
  (a) World Event 相当 — heard_by=在室者 を渡す
  (b) アイテムの記録相当 — heard_by を渡さない (_append_building_history_note)
  (c) Observer の閾値通知相当 — heard_by を渡さない (_notify_building)

どう実行するか
--------------
    .venv/Scripts/python.exe \
      docs/audits/2026-09-10_normal_behavior_remaining_evidence/repro/a2_building_event_recipients.py

本番データには触らない (SQLite in-memory + 合成ペルソナ)。LLM は呼ばない。

何が観測されたか (2026-09-10 実行)
----------------------------------
    [1] DB に書かれた heard_by
      seq=1 world_event      heard_by=['persona_a']
      seq=2 item_note        heard_by=[]
      seq=3 observer_alert   heard_by=[]

    [2] persona_a の auto_ingest の結果 (転記件数=1)
      転記された : <system>[部屋] 🌐 World Event:停電が起きました</system>
      届かなかった: item_note (Item Pickup ...)
      届かなかった: observer_alert (室温が 32 ...)

    [3] cursor = 3 (前進済み = 届かなかった行は後追いされない)

結論: heard_by を渡さない 2 経路の行は、その部屋に居るペルソナの記憶に
一度も入らない。カーソルは前進するので後追いもされない。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

# Windows の既定コンソール (cp932) では絵文字入りの本文が印字できない。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 本番の ~/.saiverse へ触れないよう隔離してから import する。
_TMP_HOME = tempfile.mkdtemp(prefix="saiverse_a2_")
os.environ["SAIVERSE_HOME"] = _TMP_HOME
os.environ["SAIVERSE_USER_DATA_DIR"] = str(Path(_TMP_HOME) / "user_data")

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from builtin_data.tools.get_building_messages import (  # noqa: E402
    auto_ingest_building_messages,
)
from database.models import Base, BuildingMessage  # noqa: E402
from manager.history import HistoryMixin  # noqa: E402
from persona.history_manager import HistoryManager  # noqa: E402

BID = "room_a"
PERSONA = "persona_a"


class FakeMemoryAdapter:
    """memory.db の代役。転記された本文だけを控える。"""

    def __init__(self) -> None:
        self.appended: list = []
        self._mid = 0

    def is_ready(self) -> bool:
        return True

    def append_persona_message(self, message, **_kw):
        self._mid += 1
        self.appended.append(message)
        return str(self._mid)

    def find_message_by_building_ref(self, _ref):
        return None


def main() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_local = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    # --- 世界側 (SAIVerseManager の代役) -------------------------------
    world = SimpleNamespace(
        quarantined_buildings={},
        SessionLocal=session_local,
        occupants={BID: [PERSONA]},
    )
    add_building_event = HistoryMixin.add_building_event.__get__(world)

    # (a) World Event — manager/admin.py:1552 と同じ形 (heard_by=在室者)
    add_building_event(
        BID,
        {"role": "host",
         "content": '<div class="note-box">🌐 World Event:<br>'
                    '<b>停電が起きました</b></div>'},
        heard_by=list(world.occupants.get(BID, [])),
    )
    # (b) アイテムの記録 — saiverse/saiverse_manager.py:1018 と同じ形 (heard_by なし)
    add_building_event(
        BID,
        {"role": "host",
         "content": '<div class="note-box">📦 Item Pickup:<br>'
                    '<b>ミラが「赤い鍵」を拾いました（部屋）。</b></div>'},
    )
    # (c) Observer の閾値通知 — saiverse/observer_manager.py:715 と同じ形
    add_building_event(
        BID,
        {"role": "host",
         "content": "室温が 32 に上昇 (閾値: 30)",
         "event_type": "observer_alert",
         "metadata": {"observer_id": "obs1", "fixture_id": "fx1"}},
    )

    print("[1] DB に書かれた heard_by")
    db = session_local()
    try:
        rows = db.query(BuildingMessage).order_by(BuildingMessage.seq).all()
        labels = ["world_event", "item_note", "observer_alert"]
        for label, row in zip(labels, rows):
            print(f"  seq={row.seq} {label:16s} heard_by={json.loads(row.heard_by)}")
    finally:
        db.close()

    # --- ペルソナ側 -----------------------------------------------------
    adapter = FakeMemoryAdapter()
    hm = HistoryManager(
        persona_id=PERSONA,
        persona_log_path=Path(_TMP_HOME) / "personas" / PERSONA / "log.json",
        building_memory_paths={},
        initial_persona_history=[],
        db_session_factory=session_local,
        memory_adapter=adapter,
    )
    persona = SimpleNamespace(
        persona_id=PERSONA,
        current_building_id=BID,
        history_manager=hm,
        # 「この部屋の記録はあるが、まだ 1 件も読んでいない」= 0
        pulse_cursors={BID: 0},
        entry_markers={},
        buildings={BID: SimpleNamespace(name="部屋")},
        sai_memory=adapter,
        id_to_name_map={},
    )
    manager = SimpleNamespace(
        SessionLocal=session_local,
        occupants={BID: [PERSONA]},
        personas={PERSONA: persona},
        all_personas={PERSONA: persona},
        id_to_name_map={},
        startup_seq_watermark={},
    )

    count = auto_ingest_building_messages(persona, manager)
    print(f"\n[2] {PERSONA} の auto_ingest の結果 (転記件数={count})")
    for message in adapter.appended:
        print(f"  転記された : {message.get('content')}")
    delivered = " ".join(m.get("content", "") for m in adapter.appended)
    for needle, label in (("Item Pickup", "item_note"),
                          ("室温が 32", "observer_alert")):
        if needle not in delivered:
            print(f"  届かなかった: {label} ({needle} ...)")

    print(f"\n[3] cursor = {persona.pulse_cursors[BID]} "
          "(前進済み = 届かなかった行は後追いされない)")


if __name__ == "__main__":
    main()
