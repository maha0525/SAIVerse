"""消した建物の ID を空ける: 残る会話などを、二度と使われない特殊な ID へ付け替える。

設計: docs/issues/building_delete_leaves_contents.md の「3. ID の使い回し」と
「5. 既にある残骸」(2026-09-29 まはーの裁定)。

建物を消しても、その部屋で交わされた会話 (``building_messages``) は FLOW-15 の裁定
どおり残る。ほかにも、ペルソナごとの「その部屋の控え」「どこまで読んだか」、
部屋専用の Playbook、予定表や通知の控えの中の部屋 ID、ペルソナの記憶のファイルの
中の機械が読む印、部屋のフォルダが、消した部屋の ID を指したまま残る。建物の ID は
使い回される (日本語名は空いている最小の番号、英語名は名前から決まる) ので、
そのままだと新しい無関係な部屋に、消した部屋の会話と印が戻ってくる。

ここでは、消した部屋を指して残るものを全部、特殊な ID
``deleted_<旧ID>_<YYYYMMDDHHMMSS>`` (ローカル時刻。埋まっていれば ``_2``、``_3`` …) へ
付け替える。元の ID は完全に空き、次に作る建物が同じ ID を使っても何も戻らず、
番号の歯抜けも出ない。``deleted_`` で始まる ID は建物の作成の口
(manager/admin.py::create_building) が受け付けないので、特殊な ID が部屋として
生き返ることは無い。

付け替えの部品は 2026-09-11 の部屋 ID の付け替え (saiverse/building_id_repair.py) の
ものを使う: 部屋を指す DB の欄の全部と JSON の欄 (値の完全一致だけ)・会話の
メッセージ ID・アドオンのメタデータ、ペルソナの記憶のファイルの印 (ペルソナごとに
複製を取ってから。記憶の本文・知覚の文面・添付は変えない — まはー 2026-09-29
「OK、書き換えてほしい！」)、部屋のフォルダ。記録も同じファイル
(``cities/<city>/building_id_renames.json``) に ``"kind": "retire"`` の要素として書く。

**古い会話のファイルとの照合の検査はしない。** 部屋 ID の付け替えでは、古い
``log.json`` の取り込み (起動時の確認処理) が新しい ID の部屋で照合を続けるので、
照合の欠けが増える書き換えを止めていた。取り込みは今ある建物にしか走らず、特殊な
ID は建物にならないので、特殊な ID の下で照合が使われることは無い
(``legacy_message_id`` の空の行に元のメッセージ ID を写す動きは同じ関数のまま残る)。

順番 (建物を消すとき、manager/admin.py::AdminService.delete_building の中):

1. 特殊な ID を決め、記録に「予定」を書く (:func:`plan_retirement`。書けなければ
   削除そのものを止める — 記録が無いと、途中で止まったときに再開できない)
2. 建物の削除と同じトランザクションで、残る参照を特殊な ID へ書き換える
   (:meth:`Retirement.rewrite_references`)。削除が巻き戻ったら記録を閉じる
   (:meth:`Retirement.cancel`)
3. commit の後、全ペルソナの記憶のファイルの印を書き換える
4. 部屋のフォルダを特殊な ID の名前へ移す
5. 記録を「完了」にする (3・4 は :meth:`Retirement.finish`)

3・4 が済まなくても削除は済んでいる (DB は commit 済み)。記録は「予定」のまま
残り、次の起動 (:func:`retire_deleted_buildings`) が 3 から続ける。「予定」が残って
いる間は、元の ID を建物の作成の口が使わない (:func:`pending_retirement_ids`)。
済めば空く。

同じ DB を別の SAIVerse が使っている間は、3・4 を削除の場では行わず、次の起動へ
回す — 相手のプロセスの記憶のファイルの書き手とは、こちらの錠前で順番を付けられない。

起動時 (:func:`retire_deleted_buildings`、部屋 ID の付け替えと消えた建物の残骸の
片付けの後):

- 「予定」のまま残った要素を続ける。旧 ID の参照が DB に残っていれば 2 から、
  無ければ 3 から。旧 ID の建物がある場合は触らずに閉じる (削除が巻き戻っていた、
  または済む前に同じ ID の建物が作られた)。
- 今までに消した建物の残骸 (建物の行が無いのに、その ID の会話などが残っている)
  を、同じやり方で付け替える。
- 同じ DB を別の SAIVerse が使っている間は見送る。
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from sqlalchemy import text

from manager.ids import is_safe_path_component
# 付け替えの部品は部屋 ID の付け替えと同じものを使う (同じ規則を二つにしない)
from saiverse.building_id_repair import (
    DIRECT_REFERENCE_COLUMNS,
    ENTRY_KIND_RETIRE,
    RENAMES_FILENAME,
    STATUS_DONE,
    STATUS_PLANNED,
    PersonaMemoryRewriter,
    _alert,
    _collect_taken_ids,
    _deepest_first,
    _move_room_folders,
    _now,
    _path_exists,
    _Plan,
    _quote,
    _read_schema,
    _resolve_columns,
    _rewrite_database,
    _save_record,
    is_retire_entry,
    legacy_folder_parts,
    load_rename_record,
    rename_record_path,
    repaired_building_id,
)

LOGGER = logging.getLogger(__name__)

_LOG_PREFIX = "[building-retirement]"

#: 特殊な ID の頭。作成の口はこの頭の ID を受け付けない (大文字小文字を問わない)。
TOMBSTONE_PREFIX = "deleted_"
#: 記憶のファイルの複製の置き場 (``<ホーム>/backups/<これ>/<日時>/``)
BACKUP_KIND = "building_retirement"
#: 記録の要素の出どころ
SOURCE_DELETE = "delete"
SOURCE_LEFTOVER = "leftover"
#: 記録を閉じた理由
NOTE_BUILDING_KEPT = "building_kept"  # 削除が commit されなかった
NOTE_ID_REUSED = "id_reused"  # 済む前に同じ ID の建物が作られた

_MAX_SUFFIX = 10_000

#: 起動時に「消した建物の残骸」を見つけるときに見る欄。部屋ごとの記録で、新しい
#: 部屋に同じ ID が付くとその部屋の過去や控えとして読まれるもの。
#:
#: - アイテムの置き場所・設置物・建物のリアルタイムスペルは、この前に走る片付け
#:   (saiverse/building_leftover_cleanup.py) が消しているので見ない。
#: - 入退室の記録は昔の削除でも消していたので残らない。
#: - 利用者の現在地・ペルソナの私室・Region の入口は「どこを指すか」の欄で、部屋の
#:   中身ではない。それだけを根拠に付け替えると、利用者やペルソナの居場所を推測で
#:   動かすことになるので、見つける根拠にはしない (付け替えると決まった ID では、
#:   この欄も一緒に動かす)。
#: - episodes / llm_usage_log は記録の控えで、建物以外の値を持つ書き手が無いことを
#:   確かめていないので、見つける根拠にはしない (同じく、付け替えるときは動かす)。
LEFTOVER_DETECTION_COLUMNS = (
    ("building_messages", "building_id", None),
    ("persona_pulse_cursor", "BUILDING_ID", None),
    ("persona_building_state", "BUILDING_ID", None),
    ("playbooks", "building_id", None),
    ("building_tool_link", "BUILDINGID", None),
)

#: 記録ファイルの読み書き (読んで一行足して書く) を、同じプロセスの中で一人ずつにする。
#: 建物の削除は API のスレッドから同時に来うる。
_RECORD_LOCK = threading.RLock()


# ---------------------------------------------------------------------------
# 小さな部品
# ---------------------------------------------------------------------------

def is_tombstone_id(building_id: Any) -> bool:
    """特殊な ID の形か (``deleted_`` で始まるか。大文字小文字を問わない)。"""
    return isinstance(building_id, str) and building_id.lower().startswith(TOMBSTONE_PREFIX)


def choose_tombstone_id(
    old_id: str, *, now: datetime, taken: Set[str], folder_roots: Sequence[Path],
) -> str:
    """特殊な ID を決める: ``deleted_<旧ID>_<YYYYMMDDHHMMSS>``。

    旧 ID のうちフォルダ名や URL を壊す文字は、部屋 ID の付け替えと同じ規則
    (:func:`repaired_building_id`) で ``_`` に置き換える (v0.3.0 より前の ID の残骸)。
    ``taken`` (小文字にした使用済みの ID) に含まれるか、``folder_roots`` のどれかの
    下に同じ名前のフォルダがあれば、末尾に ``_2``、``_3`` … を足す。
    """
    base = f"{TOMBSTONE_PREFIX}{repaired_building_id(old_id)}_{now.strftime('%Y%m%d%H%M%S')}"
    for n in range(1, _MAX_SUFFIX + 1):
        candidate = base if n == 1 else f"{base}_{n}"
        if candidate.lower() in taken:
            continue
        if any(_path_exists(root / candidate) for root in folder_roots):
            continue
        if not is_safe_path_component(candidate):
            raise RuntimeError(f"特殊な ID がフォルダ名として使えない形になりました: {candidate!r}")
        return candidate
    raise RuntimeError(f"部屋 {old_id!r} の特殊な ID の候補を使い切りました")


def _recorded_new_ids(record_path: Path) -> Set[str]:
    """記録に書かれている付け替え先の ID (小文字)。読めなければ空。"""
    record, _error = load_rename_record(record_path)
    if record is None:
        return set()
    return {
        entry["new_id"].lower()
        for entry in record["renames"]
        if isinstance(entry, dict) and isinstance(entry.get("new_id"), str)
    }


def _append_entries(record_path: Path, entries: Sequence[dict]) -> None:
    """記録に要素を足す。読めない・書けないときは例外 (呼び出し側が止まる)。"""
    with _RECORD_LOCK:
        record, error = load_rename_record(record_path)
        if record is None:
            raise RuntimeError(f"部屋の付け替えの記録が読めません ({record_path}): {error}")
        record["renames"].extend(entries)
        _save_record(record_path, record)


def _update_entry(record_path: Path, new_id: str, **fields: Any) -> bool:
    """記録の、この特殊な ID の要素に ``fields`` を書く。失敗はログだけ (False)。"""
    with _RECORD_LOCK:
        try:
            record, error = load_rename_record(record_path)
            if record is None:
                LOGGER.error(
                    "%s 記録が読めないので、%r の状態を書けませんでした: %s (%s)",
                    _LOG_PREFIX, new_id, record_path, error,
                )
                return False
            for entry in record["renames"]:
                if is_retire_entry(entry) and entry.get("new_id") == new_id:
                    entry.update(fields)
                    _save_record(record_path, record)
                    return True
            LOGGER.error(
                "%s 記録に %r の要素が見つからないので、状態を書けませんでした: %s",
                _LOG_PREFIX, new_id, record_path,
            )
            return False
        except OSError:
            LOGGER.error(
                "%s 記録に %r の状態を書けませんでした: %s",
                _LOG_PREFIX, new_id, record_path, exc_info=True,
            )
            return False


def pending_retirement_ids(saiverse_home: Path) -> Set[str]:
    """付け替えが済んでいない (「予定」の) 消した部屋の元の ID (小文字)。

    建物の作成の口がこの ID を使わないために読む。全 City の記録を見る (建物 ID は
    City をまたいで一意)。読めない記録は飛ばす (警告のログだけ) — 読めない記録
    一つで建物を一つも作れなくなるのを避ける。
    """
    out: Set[str] = set()
    cities_root = Path(saiverse_home) / "cities"
    try:
        paths = sorted(cities_root.glob(f"*/{RENAMES_FILENAME}"))
    except OSError:
        LOGGER.warning("%s 記録の一覧を読めませんでした: %s", _LOG_PREFIX, cities_root, exc_info=True)
        return out
    for path in paths:
        record, error = load_rename_record(path)
        if record is None:
            LOGGER.warning(
                "%s 記録が読めないので、使用中の ID の確認から外します: %s (%s)",
                _LOG_PREFIX, path, error,
            )
            continue
        for entry in record["renames"]:
            if (
                is_retire_entry(entry)
                and entry.get("status") == STATUS_PLANNED
                and isinstance(entry.get("old_id"), str)
            ):
                out.add(entry["old_id"].lower())
    return out


def _folder_roots(saiverse_home: Path) -> List[Path]:
    """部屋のフォルダの置き場: 全 City の ``cities/<slug>/buildings`` と、もっと古い ``buildings``。

    全 City を見るのは、建物 ID が City をまたいで一意で、同じ ID の会話の行は City を
    問わず全部付け替わるから。別の City の置き場に同じ名前のフォルダ (昔消した部屋の
    古い会話のファイル) が残っていると、後でその City に同じ ID の建物ができたとき、
    起動時の過去ログの取り込みがそれを新しい部屋へ入れてしまう。
    """
    home = Path(saiverse_home)
    roots: List[Path] = []
    try:
        children = sorted((home / "cities").iterdir())
    except OSError:
        children = []
    for child in children:
        if is_safe_path_component(child.name) and child.is_dir():
            roots.append(child / "buildings")
    roots.append(home / "buildings")
    return roots


def _move_folders(folder_roots: Sequence[Path], old_id: str, new_id: str) -> Tuple[bool, Optional[str]]:
    """部屋のフォルダを特殊な ID の名前へ移す。戻り値は (済んだか, 済まなかった理由)。

    旧 ID から古いフォルダの場所を安全に決められない形 (``a//b`` など — もう作れない
    ID) は、移すものが無いとみなして済ませる (付け替えを永遠に「予定」に残さない)。
    """
    if legacy_folder_parts(old_id) is None:
        LOGGER.warning(
            "%s 部屋 %r は古いフォルダの場所を安全に決められないので、フォルダは動かしません",
            _LOG_PREFIX, old_id,
        )
        return True, None
    ok, notes, problem = _move_room_folders(folder_roots, old_id, new_id)
    for note in notes:
        LOGGER.info("%s %s", _LOG_PREFIX, note)
    return ok, problem


def _finish_files(
    pairs: Sequence[Tuple[str, str]],
    *,
    session_factory,
    saiverse_home: Path,
    folder_roots: Sequence[Path],
    record_path: Path,
) -> Tuple[Dict[str, bool], List[dict], Dict[str, str]]:
    """手順 3・4・5: 記憶のファイルの印を書き換え、フォルダを移し、済んだ要素を「完了」にする。

    戻り値は ({特殊な ID: 完了にしたか}, 記憶のファイルの警告, {特殊な ID: フォルダを
    移せなかった理由})。記憶のファイルの書き換えは全ペルソナを一度に回る (渡された
    付け替えを全部まとめて)。一人でも済まなければ、どの要素も「完了」にしない
    (次の起動でもう一度 3 から — 完全一致の置き換えなので、済んだペルソナは変わらない)。
    """
    memory_alerts: List[dict] = []
    rewriter = PersonaMemoryRewriter(
        session_factory=session_factory,
        saiverse_home=saiverse_home,
        backup_kind=BACKUP_KIND,
        alerts=memory_alerts,
    )
    memories_done = rewriter.rewrite({old_id: new_id for old_id, new_id in pairs})
    results: Dict[str, bool] = {}
    folder_problems: Dict[str, str] = {}
    plans = _deepest_first([_Plan(old_id, old_id, new_id=new_id) for old_id, new_id in pairs])
    for plan in plans:
        assert plan.new_id is not None
        folder_ok, problem = _move_folders(folder_roots, plan.old_id, plan.new_id)
        if not folder_ok:
            LOGGER.error(
                "%s 部屋 %r -> %r のフォルダを移せませんでした: %s",
                _LOG_PREFIX, plan.old_id, plan.new_id, problem,
            )
            folder_problems[plan.new_id] = problem or ""
        if folder_ok and memories_done:
            results[plan.new_id] = _update_entry(
                record_path, plan.new_id, status=STATUS_DONE, done_at=_now(),
            )
            LOGGER.info(
                "%s 消した部屋 %r の付け替えが済みました (%r)", _LOG_PREFIX, plan.old_id, plan.new_id,
            )
        else:
            results[plan.new_id] = False
            if not memories_done:
                LOGGER.warning(
                    "%s 部屋 %r -> %r: 記憶のファイルの書き換えが済んでいないペルソナがいるので、"
                    "記録は「予定」のまま残します (次の起動で続きを行います)",
                    _LOG_PREFIX, plan.old_id, plan.new_id,
                )
    return results, memory_alerts, folder_problems


def _references_remain(db, direct_columns, old_id: str) -> bool:
    """旧 ID を指す行が、部屋を指す欄のどれかに残っているか。"""
    for column in direct_columns:
        row = db.execute(
            text(
                f"SELECT 1 FROM {_quote(column.table)} WHERE {_quote(column.column)} = :old_id"
                f"{column.where_sql()} LIMIT 1"
            ),
            column.params(old_id=old_id),
        ).first()
        if row is not None:
            return True
    return False


# ---------------------------------------------------------------------------
# 建物を消すとき
# ---------------------------------------------------------------------------

@dataclass
class Retirement:
    """1 つの建物の削除に付く、ID の付け替え。"""

    saiverse_home: Path
    city_slug: str
    old_id: str
    new_id: str
    building_name: Optional[str]

    @property
    def record_path(self) -> Path:
        return rename_record_path(self.saiverse_home, self.city_slug)

    @property
    def folder_roots(self) -> List[Path]:
        return _folder_roots(self.saiverse_home)

    def rewrite_references(self, db) -> None:
        """手順 2: 残る参照を特殊な ID へ書き換える (建物の削除と同じ session。commit しない)。"""
        schema = _read_schema(db)
        per_plan, json_counts = _rewrite_database(
            db, schema, [_Plan(self.old_id, self.building_name or self.old_id, new_id=self.new_id)],
            legacy_buildings_root=None, rename_building_rows=False,
        )
        LOGGER.info(
            "%s 消す部屋 %r の残る参照を %r へ書き換えました。欄ごとの行数: %s / JSON の欄: %s",
            _LOG_PREFIX, self.old_id, self.new_id, per_plan.get(self.old_id), json_counts,
        )

    def cancel(self) -> None:
        """削除が巻き戻ったとき、記録の「予定」を閉じる (元の ID を使用中のままにしない)。"""
        _update_entry(
            self.record_path, self.new_id,
            status=STATUS_DONE, done_at=_now(), note=NOTE_BUILDING_KEPT,
        )

    def finish(self, *, session_factory, db_path) -> bool:
        """手順 3・4・5 (commit の後)。済んだら True。済まなければ記録は「予定」のまま。"""
        from saiverse.runtime_marker import another_running_process_owns_db

        _update_entry(self.record_path, self.new_id, db_renamed_at=_now())
        owned, owner = another_running_process_owns_db(db_path)
        if owned:
            LOGGER.warning(
                "%s 同じ DB を使う別の SAIVerse が動いているので、消した部屋 %r の記憶の印と"
                "フォルダの付け替えは次の起動に回します: %s",
                _LOG_PREFIX, self.old_id, owner,
            )
            return False
        results, memory_alerts, folder_problems = _finish_files(
            [(self.old_id, self.new_id)],
            session_factory=session_factory,
            saiverse_home=self.saiverse_home,
            folder_roots=self.folder_roots,
            record_path=self.record_path,
        )
        done = bool(results.get(self.new_id))
        if not done:
            LOGGER.warning(
                "%s 消した部屋 %r の付け替えの続き (記憶の印・フォルダ) が済まなかったので、"
                "次の起動で続けます。済むまで元の ID は新しい建物に使いません。"
                "記憶の警告: %s / フォルダ: %s",
                _LOG_PREFIX, self.old_id,
                [a.get("details") for a in memory_alerts], folder_problems,
            )
        return done


def plan_retirement(
    db,
    *,
    saiverse_home: Path,
    city_slug: str,
    building_id: str,
    building_name: Optional[str],
    now: Optional[datetime] = None,
) -> Retirement:
    """手順 1: 特殊な ID を決め、記録に「予定」を書く (建物の削除の session の中、commit の前)。

    記録を書けなければ例外 — 呼び出し側は削除ごと巻き戻す。
    """
    if not is_safe_path_component(city_slug):
        raise RuntimeError(
            f"City の識別子 {city_slug!r} がフォルダ名として使えないので、付け替えの記録を書けません"
        )
    home = Path(saiverse_home)
    schema = _read_schema(db)
    direct_columns = _resolve_columns(schema, DIRECT_REFERENCE_COLUMNS)
    building_ids = [
        str(row[0])
        for row in db.execute(text('SELECT "BUILDINGID" FROM "building"')).fetchall()
        if row[0] is not None
    ]
    record_path = rename_record_path(home, city_slug)
    with _RECORD_LOCK:
        taken = _collect_taken_ids(db, direct_columns, building_ids)
        taken.add(building_id.lower())
        taken |= _recorded_new_ids(record_path)
        new_id = choose_tombstone_id(
            building_id, now=now or datetime.now(), taken=taken,
            folder_roots=_folder_roots(home),
        )
        _append_entries(record_path, [{
            "kind": ENTRY_KIND_RETIRE,
            "source": SOURCE_DELETE,
            "old_id": building_id,
            "new_id": new_id,
            "building_name": building_name,
            "city_slug": city_slug,
            "status": STATUS_PLANNED,
            "planned_at": _now(),
        }])
    LOGGER.info(
        "%s 消す部屋 %r (表示名 %r) の残るものを %r へ付け替える予定を書きました",
        _LOG_PREFIX, building_id, building_name, new_id,
    )
    return Retirement(home, city_slug, building_id, new_id, building_name)


# ---------------------------------------------------------------------------
# 起動時
# ---------------------------------------------------------------------------

def _unfinished_alert(pairs: Sequence[Tuple[str, str]], details: dict) -> dict:
    ids = "、".join(old_id for old_id, _ in pairs)
    return _alert(
        "building_retirement_unfinished",
        "消した Building の記録の片付けが済んでいません",
        f"消した Building（ID: {ids}）に残っている会話などを別の名前へ移す作業のうち、"
        "ペルソナの記憶の中の目印の書き換えか、フォルダの移動が済んでいません。"
        "済むまで、この ID は新しい Building に使えません。次の起動でもう一度試します。"
        "この警告が起動のたびに出る場合は、この警告の内容を添えて開発者に知らせてください。",
        {"reason": "retirement_unfinished", "buildings": [
            {"building_id": o, "new_building_id": n} for o, n in pairs
        ], **details},
    )


def _id_reused_alert(old_id: str, new_id: str) -> dict:
    return _alert(
        f"building_retirement_id_reused_{old_id}",
        "消した Building の記録の片付けを止めました",
        f"消した Building（ID: {old_id}）の記録を別の名前へ移す作業が済む前に、同じ ID の"
        "新しい Building が作られていました。新しい Building のものを取り違えて動かさない"
        "ように、残りの作業（ペルソナの記憶の中の目印の書き換えとフォルダの移動）を"
        "止めました。消した Building の会話は別の名前へ移してあります。"
        "この警告の内容を添えて開発者に知らせてください。",
        {"reason": "id_reused", "building_id": old_id, "new_building_id": new_id},
    )


def _database_failed_alert(old_ids: Sequence[str], exc: BaseException) -> dict:
    return _alert(
        "building_retirement_failed",
        "消した Building の記録の片付けに失敗しました",
        f"消した Building（ID: {'、'.join(old_ids)}）に残っている会話などを別の名前へ"
        "移せませんでした。データベースは移す前の状態のままです。済むまで、この ID は"
        "新しい Building に使えません。次の起動でもう一度試します。",
        {"error": f"{type(exc).__name__}: {exc}", "building_ids": list(old_ids)},
    )


def _detect_leftovers(db, schema, building_ids: Sequence[str], exclude_lower: Set[str]) -> List[str]:
    """建物の行が無いのに、部屋ごとの記録が残っている ID (特殊な ID は除く)。"""
    existing = set(building_ids)
    existing_lower = {b.lower() for b in building_ids}
    found: Set[str] = set()
    for column in _resolve_columns(schema, LEFTOVER_DETECTION_COLUMNS):
        rows = db.execute(
            text(
                f"SELECT DISTINCT {_quote(column.column)} FROM {_quote(column.table)} "
                f"WHERE {_quote(column.column)} IS NOT NULL{column.where_sql()}"
            ),
            column.params(),
        ).fetchall()
        for (value,) in rows:
            if not isinstance(value, str) or not value:
                continue
            if value in existing or is_tombstone_id(value) or value.lower() in exclude_lower:
                continue
            if value.lower() in existing_lower:
                # 大文字小文字だけ違う建物がある。フォルダは Windows では同じ場所なので、
                # 動かすと今ある建物のフォルダを持っていく。触らずに知らせる。
                LOGGER.warning(
                    "%s 建物の行の無い ID %r は、大文字小文字だけ違う建物があるので付け替えません",
                    _LOG_PREFIX, value,
                )
                continue
            found.add(value)
    return sorted(found)


def retire_deleted_buildings(
    *,
    session_factory,
    db_path,
    saiverse_home: Path,
    city_slug: str,
    now: Optional[datetime] = None,
) -> List[dict]:
    """起動時: 済んでいない付け替えを続け、昔消した建物の残骸を付け替える。

    部屋 ID の付け替え (building_id_repair) と消えた建物の残骸の片付け
    (building_leftover_cleanup) の後、建物を読み込む前に呼ぶ (manager/initialization.py)。

    Returns:
        startup_alerts に載せる警告の一覧。済んだだけなら空。
    """
    from saiverse.runtime_marker import another_running_process_owns_db

    if not is_safe_path_component(city_slug):
        LOGGER.warning(
            "%s City の識別子 %r がフォルダ名として使えないので、消した部屋の付け替えを確かめません",
            _LOG_PREFIX, city_slug,
        )
        return []
    home = Path(saiverse_home)
    record_path = rename_record_path(home, city_slug)
    record, error = load_rename_record(record_path)
    if record is None:
        # 読めない記録の警告は部屋 ID の付け替え (同じファイル) が先に出している
        LOGGER.error(
            "%s 付け替えの記録が読めないので、消した部屋の付け替えを見送ります: %s (%s)",
            _LOG_PREFIX, record_path, error,
        )
        return []

    owned, owner = another_running_process_owns_db(db_path)
    if owned:
        # 相手のプロセスが建物を作っている最中の行を、建物の無い残骸と取り違えないため
        LOGGER.warning(
            "%s 同じ DB を使う別の SAIVerse が動いているので、消した部屋の付け替えを見送ります: %s",
            _LOG_PREFIX, owner,
        )
        return []

    alerts: List[dict] = []
    db = session_factory()
    try:
        schema = _read_schema(db)
        direct_columns = _resolve_columns(schema, DIRECT_REFERENCE_COLUMNS)
        building_ids = [
            str(row[0])
            for row in db.execute(text('SELECT "BUILDINGID" FROM "building"')).fetchall()
            if row[0] is not None
        ]
        existing_lower = {b.lower() for b in building_ids}

        # -- 「予定」のまま残った要素 ------------------------------------------
        db_plans: List[_Plan] = []
        file_pairs: List[Tuple[str, str]] = []
        unstamped: Set[str] = set()
        seen: Set[str] = set()
        for entry in record["renames"]:
            if not is_retire_entry(entry) or entry.get("status") != STATUS_PLANNED:
                continue
            old_id, new_id = entry.get("old_id"), entry.get("new_id")
            if (
                not isinstance(old_id, str)
                or not isinstance(new_id, str)
                or not is_tombstone_id(new_id)
                or old_id.lower() in seen
            ):
                LOGGER.warning(
                    "%s 記録の予定を読み飛ばします (形が想定と違うか、同じ部屋の予定が重複): %r",
                    _LOG_PREFIX, entry,
                )
                continue
            seen.add(old_id.lower())
            if old_id.lower() in existing_lower:
                if entry.get("db_renamed_at"):
                    LOGGER.error(
                        "%s 消した部屋 %r の付け替えが済む前に、同じ ID の建物が作られていました。"
                        "新しい建物のものを動かさないよう、続きを止めて記録を閉じます",
                        _LOG_PREFIX, old_id,
                    )
                    _update_entry(
                        record_path, new_id, status=STATUS_DONE, done_at=_now(), note=NOTE_ID_REUSED,
                    )
                    alerts.append(_id_reused_alert(old_id, new_id))
                else:
                    LOGGER.info(
                        "%s 部屋 %r の削除は確定していなかった (建物が残っている) ので、予定を閉じます",
                        _LOG_PREFIX, old_id,
                    )
                    _update_entry(
                        record_path, new_id,
                        status=STATUS_DONE, done_at=_now(), note=NOTE_BUILDING_KEPT,
                    )
                continue
            if _references_remain(db, direct_columns, old_id):
                db_plans.append(_Plan(old_id, entry.get("building_name") or old_id, new_id=new_id))
            elif not entry.get("db_renamed_at"):
                # DB は書き換え済み (commit の直後に止まった)。時刻だけ残す
                unstamped.add(new_id)
            file_pairs.append((old_id, new_id))

        # -- 昔消した建物の残骸 --------------------------------------------------
        leftovers = _detect_leftovers(db, schema, building_ids, seen)
        new_entries: List[dict] = []
        if leftovers:
            folder_roots = _folder_roots(home)
            taken = _collect_taken_ids(db, direct_columns, building_ids)
            taken |= _recorded_new_ids(record_path)
            stamp = now or datetime.now()
            for old_id in leftovers:
                new_id = choose_tombstone_id(old_id, now=stamp, taken=taken, folder_roots=folder_roots)
                taken.add(new_id.lower())
                new_entries.append({
                    "kind": ENTRY_KIND_RETIRE,
                    "source": SOURCE_LEFTOVER,
                    "old_id": old_id,
                    "new_id": new_id,
                    "building_name": None,
                    "status": STATUS_PLANNED,
                    "planned_at": _now(),
                })
    finally:
        db.close()

    if new_entries:
        try:
            _append_entries(record_path, new_entries)
        except Exception as exc:
            LOGGER.error(
                "%s 付け替えの記録に書き込めないので、昔消した部屋の残骸の付け替えを見送ります",
                _LOG_PREFIX, exc_info=True,
            )
            alerts.append(_database_failed_alert([e["old_id"] for e in new_entries], exc))
            new_entries = []
        else:
            LOGGER.info(
                "%s 昔消した部屋の残骸を付け替えます: %s",
                _LOG_PREFIX, [(e["old_id"], e["new_id"]) for e in new_entries],
            )
        for entry in new_entries:
            db_plans.append(_Plan(entry["old_id"], entry["old_id"], new_id=entry["new_id"]))
            file_pairs.append((entry["old_id"], entry["new_id"]))

    if not file_pairs:
        return alerts

    # -- 手順 2: DB (予定の続きと残骸をまとめて一つのトランザクション) --------------
    if db_plans:
        # 起動時にまとめて書き換える前に、部屋 ID の付け替えと同じく控えを取る
        try:
            from database.backup import backup_saiverse_db

            backup_path = backup_saiverse_db(Path(db_path), kind=BACKUP_KIND)
            if backup_path is None:
                raise RuntimeError(f"控えを作る元のデータベースが見つかりません: {db_path}")
        except Exception as exc:
            LOGGER.error(
                "%s データベースの控えを作れなかったので、消した部屋の参照の付け替えを見送ります",
                _LOG_PREFIX, exc_info=True,
            )
            alerts.append(_database_failed_alert([plan.old_id for plan in db_plans], exc))
            failed = {plan.old_id for plan in db_plans}
            file_pairs = [pair for pair in file_pairs if pair[0] not in failed]
            db_plans = []
            if not file_pairs:
                return alerts
        else:
            LOGGER.info("%s 付け替えの前にデータベースの控えを作りました: %s", _LOG_PREFIX, backup_path)
    stamped: Set[str] = set(unstamped)
    if db_plans:
        db = session_factory()
        try:
            per_plan, json_counts = _rewrite_database(
                db, _read_schema(db), db_plans,
                legacy_buildings_root=None, rename_building_rows=False,
            )
            db.commit()
        except Exception as exc:
            try:
                db.rollback()
            except Exception:
                LOGGER.debug("%s rollback にも失敗しました", _LOG_PREFIX, exc_info=True)
            LOGGER.error(
                "%s 消した部屋の参照の付け替えに失敗したので、データベースを元に戻しました",
                _LOG_PREFIX, exc_info=True,
            )
            alerts.append(_database_failed_alert([plan.old_id for plan in db_plans], exc))
            failed = {plan.old_id for plan in db_plans}
            file_pairs = [pair for pair in file_pairs if pair[0] not in failed]
        else:
            LOGGER.info(
                "%s 消した部屋の残る参照を付け替えました。部屋ごとの行数: %s / JSON の欄: %s",
                _LOG_PREFIX, per_plan, json_counts,
            )
            stamped.update(plan.new_id for plan in db_plans if plan.new_id)
        finally:
            db.close()
    if not file_pairs:
        return alerts
    renamed_at = _now()
    for new_id in sorted(stamped):
        _update_entry(record_path, new_id, db_renamed_at=renamed_at)

    # -- 手順 3・4・5 ------------------------------------------------------------
    results, memory_alerts, folder_problems = _finish_files(
        file_pairs,
        session_factory=session_factory,
        saiverse_home=home,
        folder_roots=_folder_roots(home),
        record_path=record_path,
    )
    unfinished = [pair for pair in file_pairs if not results.get(pair[1])]
    if unfinished:
        alerts.append(_unfinished_alert(unfinished, {
            "memory": [a.get("details") for a in memory_alerts],
            "folders": folder_problems,
        }))
    return alerts


__all__ = [
    "BACKUP_KIND",
    "LEFTOVER_DETECTION_COLUMNS",
    "Retirement",
    "TOMBSTONE_PREFIX",
    "choose_tombstone_id",
    "is_tombstone_id",
    "pending_retirement_ids",
    "plan_retirement",
    "retire_deleted_buildings",
]
