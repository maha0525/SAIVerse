"""``building_messages`` が壊れたときに何が起きるかを観測する (FLOW-31)。

何を確かめるものか
------------------
建物の会話履歴の正本は 2026-05-20 (``ec9eba70``) に ``log.json`` から
``building_messages`` テーブルへ移った。同じ変更で旧ファイル用の
「5 状態判定 / 隔離 / 起動時バックアップ」は廃止されている。

このスクリプトは、DB 側が壊れた 4 つの形について
**(a) 検出されるか (b) 検出されたとき何が起きるか (c) 利用者に何が見えるか**
を隔離環境で実際に走らせて確かめる。

  形 1: 列に入っている JSON が壊れる (``heard_by`` / ``ingested_by`` /
        ``metadata_json``)
  形 2: 通し番号 (``seq``) の欠番
  形 3: 通し番号の重複
  形 4: SQLite ファイルそのものの破損

あわせて、毎起動の検算 (``saiverse.legacy_log_import.scan_legacy_log_deficits``)
が **旧 log.json が在る部屋しか見ない**ことを、log.json のある部屋と無い部屋の
両方で確かめる。

どう実行するか
--------------
リポジトリルートから::

    .venv/Scripts/python.exe \
      docs/audits/2026-09-10_normal_behavior_remaining_evidence/repro/b12_building_messages_corruption_detection.py

隔離: ``SAIVERSE_HOME`` を一時ディレクトリへ向け、DB は一時ファイル /
in-memory SQLite、合成の建物・ペルソナのみ。本番データには触れない。
LLM は呼ばない。

何が観測されたか (2026-09-10 実行、HEAD=7d7214be の作業ツリー)
--------------------------------------------------------------
形 1 (列の JSON 破損):
    ``fetch_building_messages`` は例外を出さず 3 件すべてを返した。
    壊れた行は ``heard_by=[]`` ``ingested_by=[]`` になり、``metadata`` は
    落ちた。**WARNING 以上のログは 1 行も出ない** (metadata_json だけが
    DEBUG 1 行、``heard_by`` / ``ingested_by`` は無言)。
    → 「誰が聞いたか」「誰が既に読んだか」が空になった状態が、
      壊れていない行と同じ顔で流れていく。

形 2 (通し番号の欠番):
    真ん中の行を消しても ``fetch_building_messages`` は残り 2 件を
    そのまま返す。欠番を数える処理はどこにも無い。

形 3 (通し番号の重複):
    ``UniqueConstraint('building_id','seq')`` が INSERT を拒否した
    (IntegrityError)。**重複は起こりえない** ので検出の対象外。

形 4 (SQLite ファイルの破損):
    ``PRAGMA integrity_check`` は ``database disk image is malformed`` で
    倒れた (= 破損は実在する)。その状態で
    ``fetch_building_messages`` は **例外を出さず 200 件すべてを返した** —
    潰したページがその問い合わせの経路に無かったため。
    つまり破損はファイルの中に黙って居座り、いつかその頁に触れた
    問い合わせが初めて落ちる。
    唯一その破損に触れた処理である起動時バックアップ
    (``database.backup.run_startup_backup``) は、失敗を
    "Startup backup failed (non-fatal)" のログ 1 行にして飲み込み、
    例外も起動時アラート (UI バナー) も出さない。
    → README:35 が約束する「起動するたびのバックアップ」は、
      DB が壊れた瞬間から黙って作られなくなる。

毎起動の検算の射程:
    log.json が無い部屋 → DB の行を全部消しても 欠け 0 件 (検出されない)。
    log.json がある部屋 → DB の行を全部消すと "not_imported" として
    検出され、その場で取り込み直される。
    → 旧ファイルが残っている部屋だけ、**偶然** 復旧の控えを持っている。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT))

_TMP_HOME = tempfile.mkdtemp(prefix="b12_saiverse_home_")
os.environ["SAIVERSE_HOME"] = _TMP_HOME
os.environ.setdefault("SAIVERSE_USER_DATA_DIR", str(Path(_TMP_HOME) / "user_data"))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from database.building_messages import (  # noqa: E402
    fetch_building_messages,
    insert_building_message,
)
from database.models import AI, Base, Building, City, User  # noqa: E402
from saiverse.legacy_log_import import scan_legacy_log_deficits  # noqa: E402

CITY = "test_city"
BUILDING_OLD = "old_room"   # 移行前からある部屋 (log.json が残っている)
BUILDING_NEW = "new_room"   # 移行後に作られた部屋 (log.json が無い)
INSERTED = 200              # 形 4 で書き込む発言の数


class _LogCatcher(logging.Handler):
    """観測中に出たログレコードを溜める。"""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def summary(self) -> str:
        if not self.records:
            return "(ログ出力なし)"
        return "; ".join(
            f"{r.levelname}:{r.getMessage()[:70]}" for r in self.records
        )


def _make_memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    try:
        db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
        db.flush()
        city = City(USERID=1, CITY_SLUG=CITY, UI_PORT=3001, API_PORT=8001)
        db.add(city)
        db.flush()
        db.add(AI(AIID="alice", HOME_CITYID=city.CITYID, AINAME="Alice"))
        for bid in (BUILDING_OLD, BUILDING_NEW):
            db.add(Building(
                BUILDINGID=bid, CITYID=city.CITYID, BUILDINGNAME=bid,
                CAPACITY=4,
            ))
        db.commit()
    finally:
        db.close()
    return engine, factory


def _seed_messages(factory, building_id: str, count: int = 3) -> None:
    for i in range(count):
        insert_building_message(factory, building_id, {
            "role": "user",
            "content": f"はなし {i + 1}",
            "timestamp": f"2026-09-10T00:0{i}:00+00:00",
            "heard_by": ["alice"],
            "ingested_by": ["alice"],
            "metadata": {"note": f"n{i + 1}"},
        })


def case_1_broken_json_columns() -> None:
    print("=" * 72)
    print("形 1: 列に入っている JSON が壊れる")
    print("=" * 72)
    engine, factory = _make_memory_db()
    _seed_messages(factory, BUILDING_NEW)

    db = factory()
    try:
        db.execute(text(
            "UPDATE building_messages SET heard_by='{壊れた', "
            "ingested_by='これは JSON ではない', metadata_json='{' "
            "WHERE building_id=:bid AND seq=2"
        ), {"bid": BUILDING_NEW})
        db.commit()
    finally:
        db.close()

    catcher = _LogCatcher()
    root = logging.getLogger()
    prev_level = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(catcher)
    try:
        rows = fetch_building_messages(factory, BUILDING_NEW)
    finally:
        root.removeHandler(catcher)
        root.setLevel(prev_level)

    broken = next(r for r in rows if r["seq"] == 2)
    healthy = next(r for r in rows if r["seq"] == 1)
    print(f"  読み出せた件数                : {len(rows)} 件 (例外なし)")
    print(f"  健全な行の heard_by           : {healthy['heard_by']}")
    print(f"  壊れた行の heard_by           : {broken['heard_by']}")
    print(f"  壊れた行の ingested_by        : {broken['ingested_by']}")
    print(f"  壊れた行の metadata           : {broken.get('metadata', '(落ちた)')}")
    print(f"  この読み出しで出たログ        : {catcher.summary()}")
    warnings = [r for r in catcher.records if r.levelno >= logging.WARNING]
    print(f"  WARNING 以上のログ            : {len(warnings)} 件")
    print()
    assert len(rows) == 3
    assert broken["heard_by"] == [] and broken["ingested_by"] == []
    assert not warnings, "WARNING が出た (この観測の前提が変わっている)"
    engine.dispose()


def case_2_seq_gap() -> None:
    print("=" * 72)
    print("形 2: 通し番号の欠番")
    print("=" * 72)
    engine, factory = _make_memory_db()
    _seed_messages(factory, BUILDING_NEW)

    db = factory()
    try:
        db.execute(text(
            "DELETE FROM building_messages WHERE building_id=:bid AND seq=2"
        ), {"bid": BUILDING_NEW})
        db.commit()
    finally:
        db.close()

    rows = fetch_building_messages(factory, BUILDING_NEW)
    print(f"  読み出せた件数                : {len(rows)} 件 (例外なし)")
    print(f"  返ってきた seq                : {[r['seq'] for r in rows]}")
    print("  欠番を数える処理              : 見つからない (grep で 0 件)")
    print()
    assert [r["seq"] for r in rows] == [1, 3]
    engine.dispose()


def case_3_seq_duplicate() -> None:
    print("=" * 72)
    print("形 3: 通し番号の重複")
    print("=" * 72)
    engine, factory = _make_memory_db()
    _seed_messages(factory, BUILDING_NEW, count=1)

    db = factory()
    refused = None
    try:
        db.execute(text(
            "INSERT INTO building_messages "
            "(building_id, seq, role, content, timestamp, heard_by, ingested_by) "
            "VALUES (:bid, 1, 'user', 'ふたつめ', '2026-09-10T00:05:00+00:00', "
            "'[]', '[]')"
        ), {"bid": BUILDING_NEW})
        db.commit()
    except IntegrityError as e:
        db.rollback()
        refused = type(e).__name__
    finally:
        db.close()

    print(f"  同じ seq の追加               : {refused or '通ってしまった'}")
    print("  → UniqueConstraint('building_id','seq') が防いでいる")
    print()
    assert refused == "IntegrityError"
    engine.dispose()


def case_4_file_level_corruption() -> None:
    print("=" * 72)
    print("形 4: SQLite ファイルそのものの破損")
    print("=" * 72)
    from database.backup import run_startup_backup

    work = Path(tempfile.mkdtemp(prefix="b12_dbfile_"))
    db_path = work / "saiverse.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    try:
        db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
        db.flush()
        city = City(USERID=1, CITY_SLUG=CITY, UI_PORT=3001, API_PORT=8001)
        db.add(city)
        db.flush()
        db.add(Building(
            BUILDINGID=BUILDING_NEW, CITYID=city.CITYID,
            BUILDINGNAME=BUILDING_NEW, CAPACITY=4,
        ))
        db.commit()
    finally:
        db.close()
    for i in range(INSERTED):
        insert_building_message(factory, BUILDING_NEW, {
            "role": "user", "content": f"はなし {i}" * 20,
            "timestamp": "2026-09-10T00:00:00+00:00",
            "heard_by": ["alice"], "ingested_by": [],
        })
    engine.dispose()

    # ヘッダを避けてページの中身を潰す (実ファイルの物理破損を模す)
    size = db_path.stat().st_size
    with open(db_path, "r+b") as fh:
        fh.seek(size // 2)
        fh.write(b"\x00" * 4096)

    # 破損後のファイルを開き直す (アプリが次の起動で使う経路と同じ形)
    engine2 = create_engine(f"sqlite:///{db_path}")
    factory2 = sessionmaker(bind=engine2)

    try:
        with sqlite3.connect(db_path) as conn:
            report = conn.execute("PRAGMA integrity_check").fetchall()
        ok = bool(report) and report[0][0] == "ok"
        first = report[0][0][:70] if report else "(報告なし)"
    except sqlite3.DatabaseError as e:
        ok = False
        first = f"{type(e).__name__}: {e}"
    print(f"  PRAGMA integrity_check        : {'ok' if ok else '破損を検出'}")
    print(f"    (先頭の報告)                : {first}")

    read_error = None
    read_count = None
    try:
        read_count = len(fetch_building_messages(factory2, BUILDING_NEW))
    except Exception as e:  # noqa: BLE001 - 素通しの観測が目的
        read_error = f"{type(e).__name__}: {e}"
    if read_error:
        print(f"  会話履歴の読み出し            : {read_error} (素通し)")
    else:
        print(f"  会話履歴の読み出し            : 例外なしで {read_count} 件 "
              f"(書き込んだのは {INSERTED} 件)")

    catcher = _LogCatcher()
    root = logging.getLogger()
    prev_level = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(catcher)
    raised = None
    try:
        run_startup_backup(db_path)
    except Exception as e:  # noqa: BLE001 - 例外が出ないことの観測が目的
        raised = f"{type(e).__name__}: {e}"
    finally:
        root.removeHandler(catcher)
        root.setLevel(prev_level)

    print(f"  run_startup_backup が投げた例外: {raised or 'なし (飲み込んだ)'}")
    print(f"  そのとき出たログ              : {catcher.summary()[:150]}")
    print("  起動時アラート (UI バナー)     : 生成する経路が無い")
    print()
    assert not ok, "破損を作れなかった (この観測の前提が崩れている)"
    assert raised is None, "run_startup_backup が例外を投げた"
    engine2.dispose()


def case_5_startup_check_scope() -> None:
    print("=" * 72)
    print("毎起動の検算の射程: log.json の有無で結果が変わるか")
    print("=" * 72)
    engine, factory = _make_memory_db()
    _seed_messages(factory, BUILDING_OLD)
    _seed_messages(factory, BUILDING_NEW)

    home = Path(_TMP_HOME)
    old_dir = home / "cities" / CITY / "buildings" / BUILDING_OLD
    old_dir.mkdir(parents=True, exist_ok=True)
    (old_dir / "log.json").write_text(
        json.dumps([
            {"role": "user", "content": f"はなし {i + 1}",
             "message_id": f"{BUILDING_OLD}:{i + 1}", "seq": i + 1,
             "timestamp": f"2026-05-01T00:0{i}:00+00:00"}
            for i in range(3)
        ], ensure_ascii=False),
        encoding="utf-8",
    )

    # 両方の部屋から DB の行を全部消す (= 正本を失った状態)
    db = factory()
    try:
        db.execute(text("DELETE FROM building_messages"))
        db.commit()
    finally:
        db.close()

    db = factory()
    try:
        deficits = scan_legacy_log_deficits(
            db, home, CITY, [BUILDING_OLD, BUILDING_NEW],
        )
    finally:
        db.close()

    by_building = {d["building_id"]: d for d in deficits}
    print(f"  log.json のある部屋 ({BUILDING_OLD}) : "
          f"{by_building.get(BUILDING_OLD, {}).get('kind', '欠け無しと判定')}")
    print(f"  log.json の無い部屋 ({BUILDING_NEW}) : "
          f"{by_building.get(BUILDING_NEW, {}).get('kind', '欠け無しと判定')}")
    print("  → 検算はファイルと DB の突き合わせ。ファイルが無い部屋は")
    print("     DB の行が全部消えても『欠け無し』になる。")
    print()
    assert BUILDING_OLD in by_building
    assert BUILDING_NEW not in by_building
    engine.dispose()


def main() -> None:
    case_1_broken_json_columns()
    case_2_seq_gap()
    case_3_seq_duplicate()
    case_4_file_level_corruption()
    case_5_startup_check_scope()
    print("観測完了。")


if __name__ == "__main__":
    main()
