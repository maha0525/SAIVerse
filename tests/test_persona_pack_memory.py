"""ペルソナまるごとパックの書き出し・取り込みのピークメモリの退行テスト。

正典: docs/intent/persona_pack.md §3 原則 6。書き出しは一行ずつ、取り込みは逐次読みと
作業用 SQLite で動き、全行の保持を持たない (既知の残余は intent に書いてある)。
このテストは丸読み・全行リストの復活を機械で止める。

測り方: 同じ形でメッセージ件数だけが SMALL / LARGE の二つの世界を tmp_path に作り、
それぞれ export → new import → merge import を回して、工程ごとに tracemalloc の
ピークを取る。合格条件は「大きい世界のピーク <= 小さい世界のピーク + 余裕」(下の
_assert_flat)。絶対値のしきい値はマシンと Python の版で割れるので、同じプロセスで
測った小さい世界のピークを基準にする。

tracemalloc が見るのは Python の確保だけで、SQLite の C 側のページキャッシュや
ソートの領域は見えない。逐次化で消した保持 (行の dict のリスト・JSON の丸読みの
文字列とオブジェクト) はすべて Python 側なので、これで足りる。
"""
from __future__ import annotations

import gc
import json
import sqlite3
import tracemalloc
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from database.models import AI, Base, Building
from sai_memory.arasuji.storage import init_arasuji_tables
from sai_memory.clips import init_clips_tables
from sai_memory.core_memory import init_core_memory_table
from sai_memory.desk import init_desk_tables
from sai_memory.memopedia.storage import init_memopedia_tables
from sai_memory.memory.storage import init_db
from sai_memory.perception_buffer import init_perception_buffer_table
from sai_memory.purpose_tags import init_purpose_tags_tables
from saiverse_memory.persona_pack import export_persona_pack, import_persona_pack

PID = "tester_city_a"
T0 = 1_760_000_000
# 1 スレッドのメッセージ件数 (両方の世界で同じ。スレッド数を件数に比例させる)。
# 書き出しの chatgpt_compat/conversations.json は 1 スレッドぶんの会話をメモリで組み立てる
# (export._iter_conversations) ので、1 スレッドの件数を世界の大きさと一緒に増やすと、
# その保持がピークに出る。これは逐次化の残余として intent §3 原則 6 に書いてある既知の
# 保持で、このテストが止めたい後戻り (全行の保持) とは別。ここでは 1 スレッドの件数を
# 固定して、その保持を両方の世界で同じ大きさにする。
MESSAGES_PER_THREAD = 500
# メッセージ件数。1 件の content は 200 文字前後の日本語で、LARGE の messages.json は約 30 MB。
# SMALL は、件数に比例しない一定の保持が上限まで育ちきる大きさにする。そうしないと
# その差が「比例して育った」ように見える。育ちきる保持は二つ:
# - 逐次 JSON 読み (json_stream) の読み足しの窓 (_READ_CHARS = 1 Mi 文字) と、その下の
#   TextIOWrapper・zipfile の読み取りの領域。messages.json が窓より小さいと窓が埋まらない
#   (3,000 件の messages.json は約 1 Mi 文字で、窓をちょうど埋めきらない)。
# - 作業用 SQLite に積む行の束 (importer._STAGING_BATCH = 1,000 行)。
SMALL = 5_000
LARGE = 40_000
MIB = 1024 * 1024
# 小さい世界のピークに足す余裕。LARGE - SMALL = 35,000 件ぶんの行を丸ごと持つと、
# 1 件あたり (本文 200 文字の str と行の dict) 少なくとも 1 KiB 強なので 35 MiB を超える。
# 件数に比例して残るのは id だけの保持 (intent §3 原則 6 の既知の残余) で、実測では
# 1 件あたり 100 バイト前後 (35,000 件で 3〜4 MiB) に収まる。余裕 12 MiB はその間に置く。
MARGIN_BYTES = 12 * MIB


def _make_env(root: Path) -> Dict[str, Any]:
    """空のペルソナが一体いる隔離環境 (ai 行・私室・初期化済みの memory.db)。"""
    home = root / "home"
    for sub in ("image", "documents", "user_data/icons", "user_data/database"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    persona_dir = home / "personas" / PID
    persona_dir.mkdir(parents=True)
    world_db = home / "user_data" / "database" / "saiverse.db"
    engine = create_engine(f"sqlite:///{world_db.as_posix()}")
    Base.metadata.create_all(engine)
    room_id = f"{PID}_room"
    with Session(engine) as s:
        s.add(AI(AIID=PID, HOME_CITYID=1, AINAME="テスト子", SYSTEMPROMPT="あなたはテスト子です。",
                 DESCRIPTION="説明", PRIVATE_ROOM_ID=room_id, AUTO_COUNT=0))
        s.add(Building(CITYID=1, BUILDINGID=room_id, BUILDINGNAME="テスト子の部屋"))
        s.commit()
    engine.dispose()
    memory_db = persona_dir / "memory.db"
    conn = init_db(str(memory_db))
    for init in (init_memopedia_tables, init_clips_tables, init_purpose_tags_tables, init_desk_tables,
                 init_perception_buffer_table, init_arasuji_tables, init_core_memory_table):
        init(conn)
    conn.commit()
    conn.close()
    return {"home": home, "world_db": world_db, "memory_db": memory_db}


def _populate(env: Dict[str, Any], messages: int) -> None:
    """メッセージ (寿命に比例する本体) と、件数に比例するページ・クリップを書く。"""
    threads = messages // MESSAGES_PER_THREAD
    conn = sqlite3.connect(env["memory_db"])
    try:
        conn.executemany(
            "INSERT INTO threads(id, resource_id) VALUES (?, ?)",
            [(f"{PID}:room_{t}", PID) for t in range(threads)],
        )
        meta = json.dumps({"tags": ["conversation"]})
        conn.executemany(
            "INSERT INTO messages(id, thread_id, role, content, resource_id, created_at, metadata) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                (f"m{i:07d}", f"{PID}:room_{i % threads}", "user" if i % 2 else "assistant",
                 f"{i} 番目の発言。" + "今日はいい天気で、散歩に出かけた。" * 12, PID, T0 + i, meta)
                for i in range(messages)
            ),
        )
        conn.executemany(
            "INSERT INTO memopedia_pages(id, parent_id, title, summary, content, category, "
            "created_at, updated_at, keywords, metadata) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                (f"page-{i}", "root_terms", f"用語 {i}", "", "説明の本文。" * 30, "terms",
                 T0 + i, T0 + i, "[]", "{}")
                for i in range(messages // 50)
            ),
        )
        conn.executemany(
            "INSERT INTO clips(clip_id, message_id, quote, created_at) VALUES (?,?,?,?)",
            ((f"clip-{i}", f"m{i * 20:07d}", "いい天気", T0 + i) for i in range(messages // 20)),
        )
        conn.commit()
    finally:
        conn.close()


def _peak(step: Callable[[], Any]) -> int:
    """step を回した間の Python の確保のピーク (バイト)。直前の確保量からの増分で返す。"""
    gc.collect()
    tracemalloc.reset_peak()
    base = tracemalloc.get_traced_memory()[0]
    step()
    return tracemalloc.get_traced_memory()[1] - base


def _measure(root: Path, messages: int) -> Dict[str, int]:
    """一つの世界で export → new import → merge import を回し、工程ごとのピークを返す。"""
    src = _make_env(root / "src")
    _populate(src, messages)
    dst = _make_env(root / "dst")
    zip_path = root / "out" / "pack.zip"
    zip_path.parent.mkdir()

    def export() -> None:
        export_persona_pack(PID, saiverse_db_path=src["world_db"], saiverse_home=src["home"],
                            output_path=zip_path)

    def import_into_dst(mode: str) -> Callable[[], None]:
        def run() -> None:
            result = import_persona_pack(zip_path, mode=mode, target_persona_id=PID,
                                         saiverse_db_path=dst["world_db"], saiverse_home=dst["home"])
            counts = result.counts["messages"]
            # new は全件を足し、合流 (取り込み先に同じ id が全部ある) は全件を飛ばす
            assert (counts.added if mode == "new" else counts.skipped) == messages
        return run

    tracemalloc.start()
    try:
        peaks = {
            "export": _peak(export),
            "new import": _peak(import_into_dst("new")),
            "merge import": _peak(import_into_dst("merge")),
        }
    finally:
        tracemalloc.stop()
    return peaks


def _assert_flat(small: Dict[str, int], large: Dict[str, int]) -> None:
    rows: List[Tuple[str, int, int]] = [(k, small[k], large[k]) for k in small]
    report = ", ".join(
        f"{k}: {s / MIB:.1f} MiB -> {lg / MIB:.1f} MiB" for k, s, lg in rows
    )
    for step, s, lg in rows:
        assert lg <= s + MARGIN_BYTES, (
            f"{step} の Python のピークがメッセージ件数に比例して育っている "
            f"({SMALL} 件 -> {LARGE} 件で {report})"
        )


def test_peak_memory_does_not_grow_with_message_count(tmp_path: Path) -> None:
    small = _measure(tmp_path / "small", SMALL)
    large = _measure(tmp_path / "large", LARGE)
    _assert_flat(small, large)
