"""ペルソナまるごとパック (persona-pack) の取り込み — 新規作成と合流。

正典: docs/intent/persona_pack.md §5。export.py が書いた ZIP を、取り込み先の
既存ペルソナ (ai 行と memory.db が既にあるもの) へ取り込む。ペルソナ自体の作成
(ai 行・私室・フォルダ) は呼び出し側 (API 層) が既存の作成経路で行う。

mode:

- ``"new"``: 空のペルソナへの合流 + ``items/`` と ``extensions/saiverse/`` の取り込み。
  取り込み先の memory.db に messages が 1 行でもあれば書き込む前に拒否する
  (``extensions/`` の機械的な行の挿入は、空の取り込み先でしか意味が定義できない)。
- ``"merge"``: ``chatlog/`` ``knowledge/`` ``journal/`` ``attachments/`` だけを §5 の
  規則で混ぜる。``items/`` と ``extensions/`` は取り込まず、スキップ件数として
  内訳に載せる (intent §5「合流で混ぜる範囲」)。

ID の扱いは native_export.py の「移植」(2026-07-16 まはー裁定) を継承する:

- ZIP 内のスレッド ID の前半 (``<persona>:`` の部分) を取り込み先のペルソナ ID に
  置き換える (threads / messages / stelis_threads の親 / Chronicle ページの
  metadata.thread_id)。``resource_id`` が元のペルソナ ID そのものなら取り込み先へ。
- 取り込んだ各 message の ``metadata.transplanted_from`` に元の
  ``{"persona_id", "thread_id"}`` を残す (元と取り込み先が同じ ID なら残さない —
  native_export の移植と同じ)。

照合と採否 (intent §5):

- 追記型 (messages / threads / stelis_threads / 手帳 / clips / purpose_tags /
  task_book) は ID (UUID・uuid 列・TASK_ID) で照合し、無いものだけ挿入する。
  既にあるものは変更しない。
- memopedia_pages は id で照合し、本文 (content) が違い、かつ ZIP 側の
  updated_at が新しいときだけ上書きする。上書き前の本文は
  memopedia_page_edit_history に before_* として記録する (rollback_page で戻せる)。
- 合流時は Chronicle (chronicle 分類の trunk でないページ) を取り込まない —
  docs/intent/arasuji_levels.md §7 の木 (parent_id / source_ids / 予算の勘定) は
  一つの環境の中でしか整合しないため。新規取り込みでは全部入れる。

短縮番号 (memopedia_pages.short_id / clips.short_id / コア記憶の metadata.core_id /
Chronicle の metadata.short_id) は DB ごとの連番 (docs/intent/reference_addressing.md
§2.1)。挿入する行の元の番号が取り込み先の取り込み開始時の MAX より大きければ
その番号のまま入れる (取り込み先で一度も使われていない番号なので一意性も
再利用禁止も破らず、本文に埋まった ``m:N`` も指し先が変わらない)。そうでない行は
MAX+1 から振り直し、元の番号を行の metadata に ``original_short_id`` /
``original_core_id`` / ``original_chronicle_short_id`` として残す
(clips は metadata 列を持たないため元の番号を残せない)。

書き込みは memory.db と saiverse.db それぞれ単一トランザクションで行い、全段が
成功したときだけ commit する。途中で失敗したら両方 rollback し、この取り込みが
作ったファイルを消す。ZIP の JSON の解釈と形の検査は書き込みより前に全部済ませる。
二つの DB の commit は別々なので、memory.db の commit 後に saiverse.db の commit が
失敗した場合だけは片側が残る (例外で知らせる。合流で再実行すれば UUID 照合で
足りない分だけが入る)。

embeddings は運ばれてこない。取り込んだ message は同じトランザクションで空の
ベクトル行 (``storage.DEFERRED_VECTOR``) を持って入る — 起動時の検査と
``SAIMemoryAdapter.reembed_stale_messages`` が拾う (import_chatlog.py と同じ形)。
Memopedia ページ・Chronicle・Fragment は「埋め込みの無い行」として
``SessionLifecycle.ensure_recall_embeddings`` の対象に自然に入る。上書きした
ページは memopedia_embeddings の行を消して、同じ経路で作り直させる。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .export import (
    _ATTACHMENT_KEYS,
    _DAY_REPORTS_DIR,
    _MEDIA_DIR_NAMES,
    _MEDIA_URI_DIRS,
    _MEMORY_DB_FILES,
    _SUMMARY_SUFFIX,
    EPOCH_COLUMNS,
    JSON_TEXT_COLUMNS,
    _attachment_key,
)
from .format import FORMAT_NAME, MANIFEST_NAME, RAW_TEXT_MARKER, decode_value, iso_to_epoch

LOGGER = logging.getLogger(__name__)

MODES = ("new", "merge")

# profile/ の上書きで書き換えない ai の列 (識別子・世界の中の位置・取り込み先の
# 運用状態)。AVATAR_IMAGE / APPEARANCE_IMAGE_PATH は値ではなくファイルを復元して
# 取り込み先のパスを書く (_PROFILE_FILE_COLUMNS)。
PROFILE_PROTECTED_COLUMNS: Tuple[str, ...] = (
    "AIID",                    # 識別子
    "HOME_CITYID",             # 取り込み先の街との結び付き
    "PRIVATE_ROOM_ID",         # 取り込み先の私室 (building 行)
    "PERSONA_ROLE",            # 'ruler' は Region との結び付き (世界の中の位置)
    "IS_DISPATCHED",           # 街間移動の運用状態 (凍結済み)
    "METABOLISM_ANCHORS",      # 旧 anchor の控え (運ばない session_anchor の旧形)
    "LAST_KNOWN_VERSION",      # 取り込み先で動いた版 (アップグレード処理の判定)
    "AUTO_COUNT",              # 自律発話の回数 (運用状態)
    "LAST_AUTO_PROMPT_TIMES",  # 自律発話の時刻 (運用状態)
)
_PROFILE_FILE_COLUMNS = ("AVATAR_IMAGE", "APPEARANCE_IMAGE_PATH")

# items/ の置き場 (ペルソナフォルダ配下)。世界への登録 (building / item 行) は
# 呼び出し側の課題。
IMPORTED_ITEMS_DIR = "imported_items"

_EDIT_SOURCE = "persona_pack_import"
_CATEGORY_CORE = "core"           # sai_memory/core_memory.py の CATEGORY_CORE
_CATEGORY_CHRONICLE = "chronicle"  # sai_memory/arasuji/storage.py の CATEGORY_CHRONICLE
_CHUNK = 500


@dataclass
class ImportCounts:
    """1 カテゴリの内訳。"""

    added: int = 0
    updated: int = 0
    skipped: int = 0


@dataclass
class PersonaPackImportResult:
    """取り込みの結果 (intent §5「結果の内訳」)。

    ``counts`` のキー: ``threads`` ``stelis_threads`` ``messages`` ``attachments``
    ``pages`` ``chronicle_pages`` ``activities`` ``memos`` ``clips``
    ``purpose_tags`` ``task_book`` ``day_reports`` ``profile`` ``items`` と、
    ``extensions/memory_db/<table>`` ``extensions/saiverse_db/<table>``
    ``extensions/persona_dir``。ZIP に無いカテゴリは載らない。
    """

    mode: str
    source_persona_id: str
    target_persona_id: str
    counts: Dict[str, ImportCounts] = field(default_factory=dict)
    # 振り直した短縮番号の件数: "pages.short_id" / "pages.core_id" /
    # "pages.chronicle_short_id" / "clips.short_id"
    renumbered: Dict[str, int] = field(default_factory=dict)
    # 空のベクトル行を付けて入れた message の件数 (再生成待ち)
    embeddings_deferred: int = 0
    # profile の上書きで書き換えた ai の列
    profile_columns_overwritten: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def count(self, key: str) -> ImportCounts:
        return self.counts.setdefault(key, ImportCounts())


# ---------------------------------------------------------------------------
# ZIP の読み取りと値の戻し方
# ---------------------------------------------------------------------------

def _safe_relpath(rel: str, where: str) -> PurePosixPath:
    """ZIP 由来の相対パスを検査する (絶対パス・``..``・ドライブ指定を拒否)。"""
    p = PurePosixPath(rel)
    if (
        not rel
        or "\\" in rel
        or ":" in rel
        or p.is_absolute()
        or any(part in ("", ".", "..") for part in p.parts)
    ):
        raise ValueError(f"persona pack: unsafe path in {where}: {rel!r}")
    return p


class _PackReader:
    def __init__(self, zf: zipfile.ZipFile) -> None:
        self.zf = zf
        self.names: Set[str] = {n for n in zf.namelist() if not n.endswith("/")}

    def read(self, name: str) -> bytes:
        return self.zf.read(name)

    def load_json(self, name: str) -> Any:
        try:
            return json.loads(self.zf.read(name).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"persona pack: {name} is not valid JSON: {exc}") from exc

    def rows(self, name: str, required: Sequence[str] = ()) -> List[Dict[str, Any]]:
        """JSON 配列 (要素は dict) を読む。無ければ空。必須キーの欠けは ValueError。"""
        if name not in self.names:
            return []
        data = self.load_json(name)
        if not isinstance(data, list):
            raise ValueError(f"persona pack: {name} must be a JSON array")
        for i, row in enumerate(data):
            if not isinstance(row, dict):
                raise ValueError(f"persona pack: {name}[{i}] must be an object")
            for key in required:
                if row.get(key) is None or row.get(key) == "":
                    raise ValueError(f"persona pack: {name}[{i}] missing {key!r}")
        return data

    def files_under(self, prefix: str) -> List[str]:
        return sorted(n for n in self.names if n.startswith(prefix))


def _decode_json_text(value: Any) -> Tuple[bool, Any]:
    """export の encode_json_text の逆の前半。(元が解釈できない文字列だったか, 値)。"""
    if isinstance(value, dict) and len(value) == 1 and isinstance(value.get(RAW_TEXT_MARKER), str):
        return True, value[RAW_TEXT_MARKER]
    return False, decode_value(value)


def _encode_json_text(is_raw: bool, value: Any) -> Any:
    if is_raw:
        return value
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _decode_row(row: Dict[str, Any], table: Optional[str] = None) -> Dict[str, Any]:
    """export の _encode_row の逆。epoch 列・JSON 文字列列・bytes を元の値へ戻す。"""
    epoch_cols = EPOCH_COLUMNS.get(table or "", ())
    json_cols = JSON_TEXT_COLUMNS.get(table or "", ())
    out: Dict[str, Any] = {}
    for key, value in row.items():
        if key in epoch_cols:
            out[key] = iso_to_epoch(value)
        elif key in json_cols:
            out[key] = _encode_json_text(*_decode_json_text(value))
        else:
            out[key] = decode_value(value)
    return out


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _epoch_key(value: Any) -> Tuple[int, float]:
    """created_at の並びの鍵。NULL は SQLite と同じく先頭側。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return (0, 0.0)
    return (1, float(value))


def _plan_numbers(originals: Sequence[Optional[int]], start_max: int) -> List[Optional[int]]:
    """挿入する行の短縮番号を決める (モジュール docstring の規則)。

    元の番号が ``start_max`` より大きく、挿入する行の中で重複していなければ
    そのまま使う。それ以外は MAX+1 から振る (MAX は取り込み開始時の値と、
    そのまま使う番号の大きい方)。元が None の行は None のまま。
    """
    kept = {o for o in originals if o is not None and o > start_max}
    top = max([start_max, *kept])
    result: List[Optional[int]] = list(originals)
    used: Set[int] = set()
    order = sorted(
        range(len(originals)),
        key=lambda i: (originals[i] is None, originals[i] or 0, i),
    )
    for i in order:
        o = originals[i]
        if o is None:
            continue
        if o > start_max and o not in used:
            used.add(o)
            result[i] = o
            continue
        top += 1
        result[i] = top
    return result


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _norm(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


# ---------------------------------------------------------------------------
# 読み込んだ ZIP の中身 (書き込み前に全部検査する)
# ---------------------------------------------------------------------------

@dataclass
class _PackData:
    source_id: str
    contents: Set[str]
    threads: List[Dict[str, Any]] = field(default_factory=list)
    stelis_threads: List[Dict[str, Any]] = field(default_factory=list)
    messages: List[Dict[str, Any]] = field(default_factory=list)
    pages: List[Dict[str, Any]] = field(default_factory=list)
    activities: List[Dict[str, Any]] = field(default_factory=list)
    memos: List[Dict[str, Any]] = field(default_factory=list)
    clips: List[Dict[str, Any]] = field(default_factory=list)
    purpose_tags: List[Dict[str, Any]] = field(default_factory=list)
    task_book: List[Dict[str, Any]] = field(default_factory=list)
    day_reports: List[Tuple[str, PurePosixPath]] = field(default_factory=list)
    attachment_index: List[Dict[str, Any]] = field(default_factory=list)
    profile: Optional[Dict[str, Any]] = None
    item_files: List[Tuple[str, PurePosixPath]] = field(default_factory=list)
    ext_memory_tables: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    ext_saiverse_tables: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    ext_persona_files: List[Tuple[str, PurePosixPath]] = field(default_factory=list)


_EXT = "extensions/saiverse/"
_EXT_MEMORY = _EXT + "memory_db/"
_EXT_SAIVERSE = _EXT + "saiverse_db/"
_EXT_PERSONA = _EXT + "persona_dir/"
_TABLE_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _load_pack(reader: _PackReader, mode: str) -> _PackData:
    """ZIP を読み、取り込みに使う全 JSON を解釈して形を検査する (書き込みはしない)。"""
    if MANIFEST_NAME not in reader.names:
        raise ValueError("persona pack: manifest.json is missing")
    manifest = reader.load_json(MANIFEST_NAME)
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT_NAME:
        raise ValueError(f"persona pack: unsupported format {manifest.get('format') if isinstance(manifest, dict) else None!r}")
    version = str(manifest.get("version") or "")
    if version.split(".", 1)[0] != "1":
        raise ValueError(f"persona pack: unsupported version {version!r}")
    source_id = manifest.get("persona_id")
    if not isinstance(source_id, str) or not source_id:
        raise ValueError("persona pack: manifest.persona_id is missing")
    contents = manifest.get("contents")
    if not isinstance(contents, list) or not all(isinstance(c, str) for c in contents):
        raise ValueError("persona pack: manifest.contents must be a list of folder names")

    pack = _PackData(source_id=source_id, contents=set(contents))
    has = pack.contents.__contains__

    if has("chatlog"):
        pack.threads = reader.rows("chatlog/threads.json", ("id",))
        pack.stelis_threads = reader.rows("chatlog/stelis_threads.json", ("thread_id",))
        pack.messages = reader.rows("chatlog/messages.json", ("id", "thread_id"))
    if has("knowledge"):
        pack.pages = reader.rows("knowledge/pages.json", ("id", "category"))
    if has("journal"):
        pack.activities = reader.rows("journal/activities.json", ("id",))
        pack.memos = reader.rows("journal/memos.json", ("activity_id",))
        pack.clips = reader.rows("journal/clips.json", ("clip_id",))
        pack.purpose_tags = reader.rows("journal/purpose_tags.json", ("tag_id",))
        pack.task_book = reader.rows("journal/task_book.json", ("TASK_ID",))
        prefix = f"journal/{_DAY_REPORTS_DIR}/"
        pack.day_reports = [
            (name, _safe_relpath(name[len(prefix):], name)) for name in reader.files_under(prefix)
        ]
    if has("attachments") and "attachments/index.json" in reader.names:
        index = reader.load_json("attachments/index.json")
        if not isinstance(index, list) or not all(isinstance(e, dict) for e in index):
            raise ValueError("persona pack: attachments/index.json must be a list of objects")
        for i, entry in enumerate(index):
            path = entry.get("path")
            if path is not None and path not in reader.names:
                raise ValueError(f"persona pack: attachments/index.json[{i}] points to a missing file {path!r}")
        pack.attachment_index = index
    if has("profile") and "profile/profile.json" in reader.names:
        profile = reader.load_json("profile/profile.json")
        if not isinstance(profile, dict) or not isinstance(profile.get("ai"), dict):
            raise ValueError("persona pack: profile/profile.json must have an 'ai' object")
        pack.profile = profile
    if has("items"):
        pack.item_files = [
            (name, _safe_relpath(name[len("items/"):], name)) for name in reader.files_under("items/")
        ]
    if has("extensions/saiverse"):
        pack.ext_persona_files = [
            (name, _safe_relpath(name[len(_EXT_PERSONA):], name))
            for name in reader.files_under(_EXT_PERSONA)
        ]
        for prefix, target in ((_EXT_MEMORY, pack.ext_memory_tables),
                               (_EXT_SAIVERSE, pack.ext_saiverse_tables)):
            for name in reader.files_under(prefix):
                table = name[len(prefix):]
                if not table.endswith(".json") or not _TABLE_NAME_RE.match(table[:-5]):
                    raise ValueError(f"persona pack: unexpected extensions file {name!r}")
                # 合流では取り込まないので、件数を数えるだけ (解釈できなくても止めない)
                if mode == "new":
                    target[table[:-5]] = reader.rows(name)
                else:
                    try:
                        target[table[:-5]] = reader.rows(name)
                    except ValueError:
                        target[table[:-5]] = [{}]
    return pack


# ---------------------------------------------------------------------------
# 取り込み本体
# ---------------------------------------------------------------------------

@dataclass
class _Relocation:
    """添付 1 件の取り込み先。"""

    dest: Path
    renamed: bool


@dataclass
class _FilePlan:
    zip_name: str
    dest: Path


def import_persona_pack(
    zip_path: Path,
    *,
    mode: str,
    target_persona_id: str,
    saiverse_db_path: Path,
    saiverse_home: Path,
    overwrite_profile: bool = False,
) -> PersonaPackImportResult:
    """ペルソナまるごとパックを既存のペルソナへ取り込む。

    Args:
        zip_path: export_persona_pack が書いた ZIP。
        mode: ``"new"`` (空のペルソナへ全部) か ``"merge"`` (既存のペルソナへ合流)。
        target_persona_id: 取り込み先のペルソナ ID。ai 行と
            ``<saiverse_home>/personas/<id>/memory.db`` が既にあること。
        saiverse_db_path: 取り込み先の本体 DB (saiverse.db)。
        saiverse_home: 取り込み先の SAIVERSE_HOME 相当のルート。
        overwrite_profile: True のときだけ ai 行を profile/profile.json の値で
            上書きする (PROFILE_PROTECTED_COLUMNS を除く)。

    Raises:
        FileNotFoundError: saiverse.db / memory.db が無い。
        ValueError: mode が不正・ZIP の形が不正・ai 行が無い・mode="new" で
            取り込み先に messages がある。いずれも書き込み前に投げる。
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    zip_path = Path(zip_path)
    saiverse_db_path = Path(saiverse_db_path)
    home = Path(saiverse_home)
    persona_dir = home / "personas" / target_persona_id
    memory_db_path = persona_dir / "memory.db"
    if not saiverse_db_path.is_file():
        raise FileNotFoundError(f"saiverse.db not found: {saiverse_db_path}")
    if not memory_db_path.is_file():
        raise FileNotFoundError(f"memory.db not found for persona {target_persona_id}: {memory_db_path}")

    with zipfile.ZipFile(zip_path) as zf:
        reader = _PackReader(zf)
        pack = _load_pack(reader, mode)
        result = PersonaPackImportResult(
            mode=mode, source_persona_id=pack.source_id, target_persona_id=target_persona_id,
        )
        job = _ImportJob(
            reader=reader, pack=pack, result=result, mode=mode,
            target=target_persona_id, home=home, persona_dir=persona_dir,
            overwrite_profile=overwrite_profile,
        )
        job.run(memory_db_path, saiverse_db_path)

    LOGGER.info(
        "[persona_pack] imported %s (source=%s) into %s mode=%s counts=%s renumbered=%s "
        "deferred_embeddings=%d warnings=%d",
        zip_path, result.source_persona_id, target_persona_id, mode,
        {k: (v.added, v.updated, v.skipped) for k, v in result.counts.items()},
        result.renumbered, result.embeddings_deferred, len(result.warnings),
    )
    return result


def _open_memory_db(path: Path) -> sqlite3.Connection:
    """取り込み先の memory.db を開き、各モジュールの init でスキーマを揃える。

    init はそれぞれ冪等で、本体の SAIMemoryAdapter が起動時に通すものと同じ。
    取り込みの書き込みはこの後の単一トランザクションで行う。
    """
    from sai_memory.clips import init_clips_tables
    from sai_memory.memopedia.storage import init_memopedia_tables
    from sai_memory.memory.storage import init_db
    from sai_memory.purpose_tags import init_purpose_tags_tables

    conn = init_db(str(path))
    try:
        init_memopedia_tables(conn)
        init_clips_tables(conn)
        init_purpose_tags_tables(conn)
        if conn.in_transaction:
            conn.commit()
        conn.isolation_level = None
        conn.execute("PRAGMA busy_timeout = 30000")
    except BaseException:
        conn.close()
        raise
    return conn


class _ImportJob:
    def __init__(
        self,
        *,
        reader: _PackReader,
        pack: _PackData,
        result: PersonaPackImportResult,
        mode: str,
        target: str,
        home: Path,
        persona_dir: Path,
        overwrite_profile: bool,
    ) -> None:
        self.reader = reader
        self.pack = pack
        self.result = result
        self.mode = mode
        self.target = target
        self.source = pack.source_id
        self.home = home
        self.persona_dir = persona_dir
        self.overwrite_profile = overwrite_profile
        self.thread_map: Dict[str, str] = {}
        self.relocations: Dict[str, _Relocation] = {}
        self.file_plans: List[_FilePlan] = []
        self.reserved: Set[str] = set()
        self.created_files: List[Path] = []
        self.created_dirs: List[Path] = []
        self._columns_cache: Dict[Tuple[int, str], List[str]] = {}
        self._dropped_warned: Set[Tuple[str, str]] = set()
        self._unmapped_threads: Set[str] = set()

    # --- 共通 ---------------------------------------------------------------

    def warn(self, message: str) -> None:
        self.result.warnings.append(message)
        LOGGER.warning("[persona_pack] %s", message)

    def columns(self, conn: sqlite3.Connection, table: str) -> List[str]:
        key = (id(conn), table)
        if key not in self._columns_cache:
            rows = conn.execute(f"PRAGMA table_info({_quote(table)})").fetchall()
            self._columns_cache[key] = [r[1] for r in rows]
        return self._columns_cache[key]

    def table_exists(self, conn: sqlite3.Connection, table: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone() is not None

    def insert(
        self,
        conn: sqlite3.Connection,
        table: str,
        row: Dict[str, Any],
        *,
        rowid: Optional[int] = None,
        or_ignore: bool = False,
    ) -> sqlite3.Cursor:
        """取り込み先の表にある列だけで 1 行挿入する (無い列は警告して落とす)。"""
        cols = set(self.columns(conn, table))
        names: List[str] = []
        values: List[Any] = []
        for key, value in row.items():
            if key not in cols:
                if (table, key) not in self._dropped_warned:
                    self._dropped_warned.add((table, key))
                    self.warn(f"{table}.{key} does not exist in the target database; values dropped")
                continue
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            names.append(key)
            values.append(value)
        if rowid is not None:
            names.insert(0, "rowid")
            values.insert(0, rowid)
        verb = "INSERT OR IGNORE" if or_ignore else "INSERT"
        sql = (
            f"{verb} INTO {_quote(table)} ({', '.join(_quote(n) for n in names)}) "
            f"VALUES ({', '.join('?' for _ in names)})"
        )
        return conn.execute(sql, values)

    def existing_keys(
        self, conn: sqlite3.Connection, table: str, column: str, keys: Iterable[Any]
    ) -> Set[Any]:
        keys = [k for k in dict.fromkeys(keys) if k is not None]
        found: Set[Any] = set()
        for i in range(0, len(keys), _CHUNK):
            part = keys[i:i + _CHUNK]
            rows = conn.execute(
                f"SELECT {_quote(column)} FROM {_quote(table)} "
                f"WHERE {_quote(column)} IN ({', '.join('?' for _ in part)})",
                part,
            ).fetchall()
            found.update(r[0] for r in rows)
        return found

    def remap_thread_id(self, thread_id: Any) -> Any:
        """スレッド ID の前半 (ペルソナ ID の部分) を取り込み先に置き換える。"""
        if not isinstance(thread_id, str):
            return thread_id
        if thread_id in self.thread_map:
            return self.thread_map[thread_id]
        if ":" not in thread_id:
            if thread_id not in self._unmapped_threads:
                self._unmapped_threads.add(thread_id)
                self.warn(f"thread id {thread_id!r} has no persona prefix; kept as is")
            return thread_id
        mapped = f"{self.target}:{thread_id.split(':', 1)[1]}"
        self.thread_map[thread_id] = mapped
        return mapped

    def remap_resource(self, value: Any) -> Any:
        return self.target if value == self.source else value

    def remap_exact(self, value: Any) -> Any:
        """extensions/ の値のうち、元のペルソナ ID・既知のスレッド ID と完全一致するものだけ置き換える。"""
        if not isinstance(value, str):
            return value
        if value == self.source:
            return self.target
        if value in self.thread_map:
            return self.thread_map[value]
        return value

    def plan_file(self, zip_name: str, dest: Path) -> None:
        self.reserved.add(_norm(dest))
        self.file_plans.append(_FilePlan(zip_name, dest))

    def is_reserved(self, path: Path) -> bool:
        return _norm(path) in self.reserved

    # --- 実行 ---------------------------------------------------------------

    def run(self, memory_db_path: Path, saiverse_db_path: Path) -> None:
        world = sqlite3.connect(str(saiverse_db_path), timeout=30, isolation_level=None)
        memory: Optional[sqlite3.Connection] = None
        try:
            if world.execute("SELECT 1 FROM ai WHERE AIID = ?", (self.target,)).fetchone() is None:
                raise ValueError(f"persona not found in saiverse.db: {self.target}")
            memory = _open_memory_db(memory_db_path)
            if self.mode == "new":
                (n_messages,) = memory.execute("SELECT COUNT(*) FROM messages").fetchone()
                if n_messages:
                    raise ValueError(
                        f"mode='new' requires a persona without messages; "
                        f"{self.target} has {n_messages}. Use mode='merge'."
                    )
            memory_committed = False
            try:
                memory.execute("BEGIN IMMEDIATE")
                self.stage_chatlog(memory)
                self.stage_knowledge(memory)
                self.stage_journal_memory(memory)
                if self.mode == "new":
                    self.stage_ext_memory(memory)
                world.execute("BEGIN IMMEDIATE")
                self.stage_task_book(world)
                self.stage_profile(world)
                if self.mode == "new":
                    self.stage_ext_saiverse(world)
                self.stage_day_reports()
                if self.mode == "new":
                    self.stage_persona_dir_files()
                    self.stage_items()
                else:
                    self.count_merge_skips()
                self.write_files()
                memory.execute("COMMIT")
                memory_committed = True
                world.execute("COMMIT")
            except BaseException:
                for conn in (world, memory) if not memory_committed else (world,):
                    try:
                        if conn.in_transaction:
                            conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        LOGGER.warning("[persona_pack] rollback failed", exc_info=True)
                if not memory_committed:
                    self.remove_created_files()
                else:
                    LOGGER.error(
                        "[persona_pack] memory.db was committed but saiverse.db was not "
                        "(persona=%s); re-run the import with mode='merge' to fill the rest",
                        self.target,
                    )
                raise
        finally:
            world.close()
            if memory is not None:
                memory.close()

    def write_files(self) -> None:
        """計画したファイルを書く。既存のファイルは決して上書きしない ("xb")。"""
        for plan in self.file_plans:
            missing: List[Path] = []
            directory = plan.dest.parent
            while not directory.exists():
                missing.append(directory)
                directory = directory.parent
            plan.dest.parent.mkdir(parents=True, exist_ok=True)
            self.created_dirs.extend(reversed(missing))
            with plan.dest.open("xb") as f:
                self.created_files.append(plan.dest)
                f.write(self.reader.read(plan.zip_name))

    def remove_created_files(self) -> None:
        for path in reversed(self.created_files):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                LOGGER.warning("[persona_pack] could not remove %s", path, exc_info=True)
        for directory in reversed(self.created_dirs):
            try:
                directory.rmdir()
            except OSError:
                pass

    # --- chatlog/ + attachments/ ------------------------------------------------

    def stage_chatlog(self, conn: sqlite3.Connection) -> None:
        pack = self.pack
        # スレッド ID の写像を先に全部決める (stelis の親・Chronicle の
        # metadata.thread_id・extensions/ の完全一致置き換えが参照する)
        for row in pack.threads:
            self.remap_thread_id(row["id"])
        for row in pack.stelis_threads:
            self.remap_thread_id(row["thread_id"])
            self.remap_thread_id(row.get("parent_thread_id"))
        for row in pack.messages:
            self.remap_thread_id(row["thread_id"])

        # threads
        counts = self.result.count("threads") if pack.threads else None
        existing = self.existing_keys(conn, "threads", "id", self.thread_map.values())
        for row in pack.threads:
            new_id = self.remap_thread_id(row["id"])
            if new_id in existing:
                counts.skipped += 1
                continue
            decoded = _decode_row(row, "threads")
            decoded["id"] = new_id
            decoded["resource_id"] = self.remap_resource(decoded.get("resource_id"))
            self.insert(conn, "threads", decoded)
            existing.add(new_id)
            counts.added += 1

        # stelis_threads
        if pack.stelis_threads:
            counts = self.result.count("stelis_threads")
            present = self.existing_keys(
                conn, "stelis_threads", "thread_id",
                (self.remap_thread_id(r["thread_id"]) for r in pack.stelis_threads),
            )
            for row in pack.stelis_threads:
                new_id = self.remap_thread_id(row["thread_id"])
                if new_id in present:
                    counts.skipped += 1
                    continue
                decoded = _decode_row(row, "stelis_threads")
                decoded["thread_id"] = new_id
                decoded["parent_thread_id"] = self.remap_thread_id(decoded.get("parent_thread_id"))
                self.insert(conn, "stelis_threads", decoded)
                present.add(new_id)
                counts.added += 1

        if not pack.messages:
            if pack.attachment_index:
                self.result.count("attachments").skipped += len(pack.attachment_index)
            return

        # messages: id で照合し、無いものだけ入れる
        counts = self.result.count("messages")
        present = self.existing_keys(conn, "messages", "id", (m["id"] for m in pack.messages))
        new_messages: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        for msg in pack.messages:
            if msg["id"] in present or msg["id"] in seen:
                counts.skipped += 1
                continue
            seen.add(msg["id"])
            new_messages.append(msg)

        self.plan_attachments(new_messages)

        # メッセージの行が指すスレッドが無ければ器だけ作る
        thread_counts = self.result.count("threads")
        known_threads = self.existing_keys(
            conn, "threads", "id", (self.remap_thread_id(m["thread_id"]) for m in new_messages)
        )
        for msg in new_messages:
            tid = self.remap_thread_id(msg["thread_id"])
            if tid not in known_threads:
                self.insert(conn, "threads", {"id": tid, "resource_id": self.target})
                known_threads.add(tid)
                thread_counts.added += 1

        # 正典順 (created_at, seq) で挿入する (native_export の _canonical_import_order と同じ鍵)
        def _order(item: Tuple[int, Dict[str, Any]]) -> Tuple[Any, ...]:
            pos, msg = item
            seq = _as_int(msg.get("seq"))
            return (_epoch_key(iso_to_epoch(msg.get("created_at"))),
                    (0, seq) if seq is not None else (1, pos))

        ordered = [m for _, m in sorted(enumerate(new_messages), key=_order)]
        rowids = self.seq_rowids_if_free(conn, ordered)

        from sai_memory.memory.storage import write_deferred_message_embedding

        for idx, msg in enumerate(ordered):
            row = self.decode_message(msg)
            self.insert(conn, "messages", row, rowid=rowids[idx] if rowids else None)
            content = row.get("content")
            if isinstance(content, str) and content.strip():
                write_deferred_message_embedding(conn, row["id"], commit=False)
                self.result.embeddings_deferred += 1
            counts.added += 1

    def seq_rowids_if_free(
        self, conn: sqlite3.Connection, ordered: List[Dict[str, Any]]
    ) -> Optional[List[int]]:
        """seq (元の rowid) を取り込み先の rowid に使えるか (native_export の _seq_rowids_if_free と同じ規則)。"""
        seqs: List[int] = []
        for msg in ordered:
            seq = msg.get("seq")
            if not isinstance(seq, int) or isinstance(seq, bool) or seq <= 0:
                return None
            seqs.append(seq)
        if len(set(seqs)) != len(seqs):
            return None
        for i in range(0, len(seqs), _CHUNK):
            part = seqs[i:i + _CHUNK]
            (occupied,) = conn.execute(
                f"SELECT COUNT(*) FROM messages WHERE rowid IN ({', '.join('?' for _ in part)})",
                part,
            ).fetchone()
            if occupied:
                return None
        return seqs

    def decode_message(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        row: Dict[str, Any] = {}
        for key, value in msg.items():
            if key == "seq":
                continue
            if key == "created_at":
                row[key] = iso_to_epoch(value)
            elif key == "metadata":
                continue
            else:
                row[key] = decode_value(value)
        original_thread = msg["thread_id"]
        row["thread_id"] = self.remap_thread_id(original_thread)
        row["resource_id"] = self.remap_resource(row.get("resource_id"))

        is_raw, metadata = _decode_json_text(msg.get("metadata"))
        if not is_raw:
            metadata = self.relocate_metadata(metadata)
            if self.source != self.target:
                if metadata is None:
                    metadata = {}
                if isinstance(metadata, dict):
                    metadata = dict(metadata)
                    metadata["transplanted_from"] = {
                        "persona_id": self.source,
                        "thread_id": original_thread,
                    }
                else:
                    self.warn(f"message {msg['id']}: metadata is not an object; transplanted_from not recorded")
        elif self.source != self.target:
            self.warn(f"message {msg['id']}: metadata is not JSON; transplanted_from not recorded")
        row["metadata"] = _encode_json_text(is_raw, metadata)
        return row

    def plan_attachments(self, new_messages: List[Dict[str, Any]]) -> None:
        """取り込む message が参照する添付を共有メディアフォルダへ置く計画を立てる。"""
        if not self.pack.attachment_index:
            return
        counts = self.result.count("attachments")
        needed: Set[str] = set()
        for msg in new_messages:
            is_raw, metadata = _decode_json_text(msg.get("metadata"))
            if is_raw or not isinstance(metadata, dict):
                continue
            for key in _ATTACHMENT_KEYS:
                entries = metadata.get(key)
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if isinstance(entry, dict) and (entry.get("uri") or entry.get("path")):
                        needed.add(_attachment_key(entry))
        for entry in self.pack.attachment_index:
            ref = entry.get("ref")
            zip_path = entry.get("path")
            if ref not in needed or not zip_path:
                counts.skipped += 1
                continue
            parts = PurePosixPath(zip_path).parts
            if len(parts) != 3 or parts[0] != "attachments":
                raise ValueError(f"persona pack: unexpected attachment path {zip_path!r}")
            kind, name = parts[1], parts[2]
            _safe_relpath(name, "attachments/index.json")
            dest_dir = self.home / (kind if kind in _MEDIA_DIR_NAMES else "documents")
            data_hash = _sha256_bytes(self.reader.read(zip_path))
            summary_zip = entry.get("summary_path")
            if summary_zip is not None and summary_zip not in self.reader.names:
                summary_zip = None
            dest, reused = self.choose_media_dest(dest_dir, name, data_hash, summary_zip is not None)
            self.relocations[ref] = _Relocation(dest=dest, renamed=dest.name != name)
            if reused:
                counts.skipped += 1
            else:
                self.plan_file(zip_path, dest)
                counts.added += 1
            if summary_zip is not None:
                summary_dest = dest.with_name(dest.name + _SUMMARY_SUFFIX)
                if not summary_dest.exists() and not self.is_reserved(summary_dest):
                    self.plan_file(summary_zip, summary_dest)

    def choose_media_dest(
        self, directory: Path, name: str, data_hash: str, with_summary: bool
    ) -> Tuple[Path, bool]:
        """同名で中身が同じファイルがあれば再利用、違えば ``<stem>_<n><ext>`` へ避ける。"""
        stem, ext = Path(name).stem, Path(name).suffix
        n = 0
        while True:
            candidate = directory / (name if n == 0 else f"{stem}_{n}{ext}")
            n += 1
            if self.is_reserved(candidate):
                continue
            if candidate.exists():
                if candidate.is_file() and _sha256_file(candidate) == data_hash:
                    return candidate, True
                continue
            if with_summary and candidate.with_name(candidate.name + _SUMMARY_SUFFIX).exists():
                continue
            return candidate, False

    def relocate_metadata(self, metadata: Any) -> Any:
        """取り込んだ添付の置き場に合わせて metadata の uri / path を書き換える。"""
        if not isinstance(metadata, dict) or not self.relocations:
            return metadata
        out = dict(metadata)
        for key in _ATTACHMENT_KEYS:
            entries = out.get(key)
            if not isinstance(entries, list):
                continue
            rewritten = []
            for entry in entries:
                if isinstance(entry, dict) and (entry.get("uri") or entry.get("path")):
                    reloc = self.relocations.get(_attachment_key(entry))
                    if reloc is not None:
                        entry = dict(entry)
                        uri = entry.get("uri")
                        if reloc.renamed and isinstance(uri, str):
                            for prefix, _dirname in _MEDIA_URI_DIRS:
                                if uri.startswith(prefix):
                                    entry["uri"] = prefix + reloc.dest.name
                                    break
                        path = entry.get("path")
                        if isinstance(path, str) and path.strip():
                            entry["path"] = str(reloc.dest)
                rewritten.append(entry)
            out[key] = rewritten
        return out

    # --- knowledge/ -------------------------------------------------------------

    def stage_knowledge(self, conn: sqlite3.Connection) -> None:
        pages = self.pack.pages
        if not pages:
            return
        from sai_memory.memopedia.storage import generate_diff, record_page_edit

        target_rows: Dict[str, Tuple[Any, ...]] = {}
        ids = [p["id"] for p in pages]
        for i in range(0, len(ids), _CHUNK):
            part = ids[i:i + _CHUNK]
            for row in conn.execute(
                "SELECT id, title, summary, content, updated_at, metadata, category "
                f"FROM memopedia_pages WHERE id IN ({', '.join('?' for _ in part)})",
                part,
            ):
                target_rows[row[0]] = row

        to_insert: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        for page in pages:
            is_chronicle = page.get("category") == _CATEGORY_CHRONICLE and not page.get("is_trunk")
            counts = self.result.count("chronicle_pages" if is_chronicle else "pages")
            if page["id"] in seen:
                counts.skipped += 1
                continue
            seen.add(page["id"])
            if is_chronicle and self.mode == "merge":
                # arasuji_levels.md §7: Chronicle の木は一つの環境の中でしか整合しない
                counts.skipped += 1
                continue
            existing = target_rows.get(page["id"])
            if existing is None:
                to_insert.append(page)
                continue
            decoded = _decode_row(page, "memopedia_pages")
            zip_updated = decoded.get("updated_at")
            target_updated = existing[4]
            if (
                decoded.get("content") == existing[3]
                or not isinstance(zip_updated, (int, float))
                or not isinstance(target_updated, (int, float))
                or zip_updated <= target_updated
            ):
                counts.skipped += 1
                continue
            self.update_page(conn, page, decoded, existing)
            record_page_edit(
                conn,
                page_id=page["id"],
                diff_text=generate_diff(existing[3] or "", decoded.get("content") or ""),
                edit_type="update",
                edit_source=_EDIT_SOURCE,
                before_title=existing[1],
                before_summary=existing[2],
                before_content=existing[3],
                commit=False,
            )
            conn.execute("DELETE FROM memopedia_embeddings WHERE page_id = ?", (page["id"],))
            counts.updated += 1

        if not to_insert:
            return
        decoded_pages = [_decode_row(p, "memopedia_pages") for p in to_insert]
        # [元が解釈できない文字列だったか, metadata の値] (番号の記録で書き換えるので list)
        metas: List[List[Any]] = [list(_decode_json_text(p.get("metadata"))) for p in to_insert]

        # 列の short_id
        (col_max,) = conn.execute("SELECT COALESCE(MAX(short_id), 0) FROM memopedia_pages").fetchone()
        originals = [_as_int(d.get("short_id")) for d in decoded_pages]
        planned = _plan_numbers(originals, int(col_max))
        for d, meta, orig, new in zip(decoded_pages, metas, originals, planned):
            if new != orig:
                d["short_id"] = new
                self.record_original(meta, "original_short_id", orig, d["id"])
                self.result.renumbered["pages.short_id"] = self.result.renumbered.get("pages.short_id", 0) + 1

        # metadata の番号 (コア記憶の core_id / Chronicle の short_id)
        for category, meta_key, original_key, counter, title_fmt in (
            (_CATEGORY_CORE, "core_id", "original_core_id", "pages.core_id", "コア記憶 c:{}"),
            (_CATEGORY_CHRONICLE, "short_id", "original_chronicle_short_id",
             "pages.chronicle_short_id", "chronicle:{}"),
        ):
            idx = [
                i for i, (d, (raw, meta)) in enumerate(zip(decoded_pages, metas))
                if d.get("category") == category and not raw and isinstance(meta, dict)
                and meta_key in meta
            ]
            if not idx:
                continue
            (meta_max,) = conn.execute(
                f"SELECT COALESCE(MAX(CAST(json_extract(metadata, '$.{meta_key}') AS INTEGER)), 0) "
                "FROM memopedia_pages WHERE category = ?",
                (category,),
            ).fetchone()
            originals = [_as_int(metas[i][1].get(meta_key)) for i in idx]
            planned = _plan_numbers(originals, int(meta_max or 0))
            for i, orig, new in zip(idx, originals, planned):
                if new == orig or new is None:
                    continue
                meta = metas[i][1]
                meta[meta_key] = new
                meta[original_key] = orig
                # 機構が書いた表題 (core_memory.add_core_memory / arasuji create_entry)
                # だけを新しい番号に合わせる
                if decoded_pages[i].get("title") == title_fmt.format(orig):
                    decoded_pages[i]["title"] = title_fmt.format(new)
                self.result.renumbered[counter] = self.result.renumbered.get(counter, 0) + 1

        for d, (is_raw, meta) in zip(decoded_pages, metas):
            if not is_raw and d.get("category") == _CATEGORY_CHRONICLE and isinstance(meta, dict):
                if isinstance(meta.get("thread_id"), str):
                    meta["thread_id"] = self.remap_thread_id(meta["thread_id"])
            d["metadata"] = _encode_json_text(is_raw, meta)
            self.insert(conn, "memopedia_pages", d)
            key = "chronicle_pages" if (
                d.get("category") == _CATEGORY_CHRONICLE and not d.get("is_trunk")
            ) else "pages"
            self.result.count(key).added += 1

    def record_original(self, meta_cell: List[Any], key: str, original: Any, page_id: str) -> None:
        """振り直した行の元の番号を metadata に残す。meta_cell は [is_raw, 値]。"""
        if meta_cell[0]:
            self.warn(f"memopedia page {page_id}: metadata is not JSON; {key} not recorded")
            return
        if meta_cell[1] is None:
            meta_cell[1] = {}
        if not isinstance(meta_cell[1], dict):
            self.warn(f"memopedia page {page_id}: metadata is not an object; {key} not recorded")
            return
        meta_cell[1][key] = original

    def update_page(
        self,
        conn: sqlite3.Connection,
        page: Dict[str, Any],
        decoded: Dict[str, Any],
        existing: Tuple[Any, ...],
    ) -> None:
        """ZIP 側が新しいページで取り込み先の行を上書きする。

        番号 (列の short_id、metadata の core_id / Chronicle の short_id と
        original_*) は取り込み先の値を保つ — 取り込み先で既に参照されている番号だから。
        """
        category = existing[6]
        try:
            target_meta = json.loads(existing[5]) if existing[5] else None
        except (TypeError, ValueError):
            target_meta = None
        is_raw, zip_meta = _decode_json_text(page.get("metadata"))
        keep_keys = ["original_short_id"]
        if category == _CATEGORY_CORE:
            keep_keys += ["core_id", "original_core_id"]
        if category == _CATEGORY_CHRONICLE:
            keep_keys += ["short_id", "original_chronicle_short_id"]
        decoded = dict(decoded)
        if not is_raw and (zip_meta is None or isinstance(zip_meta, dict)):
            merged = dict(zip_meta or {})
            for key in keep_keys:
                if isinstance(target_meta, dict) and key in target_meta:
                    merged[key] = target_meta[key]
                else:
                    merged.pop(key, None)
            keep_null = zip_meta is None and not merged
            decoded["metadata"] = None if keep_null else json.dumps(merged, ensure_ascii=False)
        cols = set(self.columns(conn, "memopedia_pages"))
        assignments = [
            (k, v) for k, v in decoded.items()
            if k in cols and k not in ("id", "short_id", "created_at")
        ]
        conn.execute(
            f"UPDATE memopedia_pages SET {', '.join(f'{_quote(k)} = ?' for k, _ in assignments)} "
            "WHERE id = ?",
            [v for _, v in assignments] + [decoded["id"]],
        )

    # --- journal/ (memory.db 側) ------------------------------------------------

    def stage_journal_memory(self, conn: sqlite3.Connection) -> None:
        self.stage_activities_and_memos(conn)
        self.stage_clips(conn)
        self.stage_purpose_tags(conn)

    def stage_activities_and_memos(self, conn: sqlite3.Connection) -> None:
        pack = self.pack
        id_map: Dict[int, int] = {}
        if pack.activities:
            counts = self.result.count("activities")
            by_uuid = dict(conn.execute(
                "SELECT uuid, id FROM activities WHERE uuid IS NOT NULL"
            ).fetchall())
            open_by_name = dict(conn.execute(
                "SELECT name, id FROM activities WHERE status = 'open'"
            ).fetchall())
            for row in sorted(pack.activities, key=lambda r: _as_int(r.get("id")) or 0):
                zip_id = _as_int(row.get("id"))
                u = row.get("uuid")
                if u and u in by_uuid:
                    id_map[zip_id] = by_uuid[u]
                    counts.skipped += 1
                    continue
                if row.get("status") == "open" and row.get("name") in open_by_name:
                    # open な同名は一つだけ (idx_activities_open_name)。同じアクティビティとして扱う
                    id_map[zip_id] = open_by_name[row["name"]]
                    counts.skipped += 1
                    self.warn(
                        f"activity {row.get('name')!r} (uuid {u}) joined the open activity of the "
                        "same name in the target"
                    )
                    continue
                decoded = _decode_row(row, "activities")
                decoded.pop("id", None)
                decoded["uuid"] = u or str(uuid.uuid4())
                cur = self.insert(conn, "activities", decoded)
                id_map[zip_id] = cur.lastrowid
                by_uuid[decoded["uuid"]] = cur.lastrowid
                if decoded.get("status") == "open":
                    open_by_name[decoded.get("name")] = cur.lastrowid
                counts.added += 1
        if pack.memos:
            counts = self.result.count("memos")
            present_uuids = self.existing_keys(conn, "memos", "uuid", (m.get("uuid") for m in pack.memos))
            present_idem = self.existing_keys(conn, "memos", "idem_key", (m.get("idem_key") for m in pack.memos))
            for row in sorted(pack.memos, key=lambda r: _as_int(r.get("id")) or 0):
                u = row.get("uuid")
                idem = row.get("idem_key")
                if (u and u in present_uuids) or (idem and idem in present_idem):
                    counts.skipped += 1
                    continue
                activity_id = id_map.get(_as_int(row.get("activity_id")))
                if activity_id is None:
                    counts.skipped += 1
                    self.warn(f"memo {u}: its activity is not in the pack; skipped")
                    continue
                decoded = _decode_row(row, "memos")
                decoded.pop("id", None)
                decoded["activity_id"] = activity_id
                decoded["uuid"] = u or str(uuid.uuid4())
                self.insert(conn, "memos", decoded)
                present_uuids.add(decoded["uuid"])
                if idem:
                    present_idem.add(idem)
                counts.added += 1

    def stage_clips(self, conn: sqlite3.Connection) -> None:
        clips = self.pack.clips
        if not clips:
            return
        counts = self.result.count("clips")
        present = self.existing_keys(conn, "clips", "clip_id", (c["clip_id"] for c in clips))
        to_insert = []
        seen: Set[str] = set()
        for row in clips:
            if row["clip_id"] in present or row["clip_id"] in seen:
                counts.skipped += 1
                continue
            seen.add(row["clip_id"])
            to_insert.append(_decode_row(row, "clips"))
        if not to_insert:
            return
        (col_max,) = conn.execute("SELECT COALESCE(MAX(short_id), 0) FROM clips").fetchone()
        originals = [_as_int(d.get("short_id")) for d in to_insert]
        planned = _plan_numbers(originals, int(col_max))
        for d, orig, new in zip(to_insert, originals, planned):
            if new != orig:
                d["short_id"] = new
                # clips は metadata 列を持たないので元の番号は残せない
                self.result.renumbered["clips.short_id"] = self.result.renumbered.get("clips.short_id", 0) + 1
            self.insert(conn, "clips", d)
            counts.added += 1

    def stage_purpose_tags(self, conn: sqlite3.Connection) -> None:
        tags = self.pack.purpose_tags
        if not tags:
            return
        counts = self.result.count("purpose_tags")
        present = self.existing_keys(conn, "purpose_tags", "tag_id", (t["tag_id"] for t in tags))
        pairs = set(conn.execute("SELECT target_ref, purpose_ref FROM purpose_tags").fetchall())
        for row in tags:
            # tag_id の一致、または (target_ref, purpose_ref) の一致 (UNIQUE) は同じ付与として扱う
            pair = (row.get("target_ref"), row.get("purpose_ref"))
            if row["tag_id"] in present or pair in pairs:
                counts.skipped += 1
                continue
            self.insert(conn, "purpose_tags", _decode_row(row, "purpose_tags"))
            present.add(row["tag_id"])
            pairs.add(pair)
            counts.added += 1

    # --- journal/ (saiverse.db 側とファイル) -----------------------------------

    def stage_task_book(self, world: sqlite3.Connection) -> None:
        rows = self.pack.task_book
        if not rows:
            return
        counts = self.result.count("task_book")
        if not self.table_exists(world, "task_book"):
            counts.skipped += len(rows)
            self.warn("saiverse.db has no task_book table; task_book skipped")
            return
        present = self.existing_keys(world, "task_book", "TASK_ID", (r["TASK_ID"] for r in rows))
        present_idem = self.existing_keys(world, "task_book", "IDEM_KEY", (r.get("IDEM_KEY") for r in rows))
        for row in rows:
            idem = row.get("IDEM_KEY")
            if row["TASK_ID"] in present or (idem and idem in present_idem):
                counts.skipped += 1
                continue
            decoded = _decode_row(row, "task_book")
            decoded["PERSONA_ID"] = self.target
            self.insert(world, "task_book", decoded)
            present.add(row["TASK_ID"])
            if idem:
                present_idem.add(idem)
            counts.added += 1

    def stage_day_reports(self) -> None:
        reports = self.pack.day_reports
        if not reports:
            return
        counts = self.result.count("day_reports")
        base = self.persona_dir / _DAY_REPORTS_DIR
        label = re.sub(r"[^A-Za-z0-9_.-]", "_", self.source)
        for zip_name, rel in reports:
            data_hash = _sha256_bytes(self.reader.read(zip_name))
            dest = base.joinpath(*rel.parts)
            if not dest.exists() and not self.is_reserved(dest):
                self.plan_file(zip_name, dest)
                counts.added += 1
                continue
            if dest.is_file() and _sha256_file(dest) == data_hash:
                counts.skipped += 1
                continue
            # 同じ日付で内容が違う: 別々の環境で生きたどちらも本物の記録なので両方残す
            stem, ext = dest.stem, dest.suffix
            n = 0
            while True:
                tag = f".from_{label}" if n == 0 else f".from_{label}_{n}"
                candidate = dest.with_name(f"{stem}{tag}{ext}")
                n += 1
                if self.is_reserved(candidate):
                    continue
                if candidate.exists():
                    if candidate.is_file() and _sha256_file(candidate) == data_hash:
                        counts.skipped += 1
                        break
                    continue
                self.plan_file(zip_name, candidate)
                counts.added += 1
                break

    # --- profile/ ---------------------------------------------------------------

    def stage_profile(self, world: sqlite3.Connection) -> None:
        profile = self.pack.profile
        if profile is None:
            return
        counts = self.result.count("profile")
        if not self.overwrite_profile:
            counts.skipped += 1
            return
        ai = _decode_row(profile["ai"])
        files = profile.get("files") if isinstance(profile.get("files"), dict) else {}
        cols = self.columns(world, "ai")
        updates: Dict[str, Any] = {}
        for col in cols:
            if col in PROFILE_PROTECTED_COLUMNS or col in _PROFILE_FILE_COLUMNS or col not in ai:
                continue
            updates[col] = ai[col]
        for col in _PROFILE_FILE_COLUMNS:
            if col not in cols or col not in ai:
                continue
            if not ai[col]:
                updates[col] = None
                continue
            zip_name = files.get(col)
            if not isinstance(zip_name, str) or zip_name not in self.reader.names:
                self.warn(f"profile: the file for ai.{col} is not in the pack; column left unchanged")
                continue
            name = PurePosixPath(zip_name).name
            _safe_relpath(name, "profile/profile.json")
            data_hash = _sha256_bytes(self.reader.read(zip_name))
            dest, reused = self.choose_media_dest(self.home / "user_data" / "icons", name, data_hash, False)
            if not reused:
                self.plan_file(zip_name, dest)
            updates[col] = str(dest)
        for col in ai:
            if col not in cols and (("ai", col) not in self._dropped_warned):
                self._dropped_warned.add(("ai", col))
                self.warn(f"ai.{col} does not exist in the target database; value dropped")
        if updates:
            world.execute(
                f"UPDATE ai SET {', '.join(f'{_quote(c)} = ?' for c in updates)} WHERE AIID = ?",
                [*updates.values(), self.target],
            )
        self.result.profile_columns_overwritten = sorted(updates)
        counts.updated += 1

    # --- extensions/ と items/ (mode="new" のみ) ------------------------------------

    def stage_ext_memory(self, conn: sqlite3.Connection) -> None:
        for table, rows in sorted(self.pack.ext_memory_tables.items()):
            counts = self.result.count(f"extensions/memory_db/{table}")
            if not rows:
                continue
            if not self.table_exists(conn, table):
                counts.skipped += len(rows)
                self.warn(f"memory.db has no table {table!r}; {len(rows)} row(s) skipped")
                continue
            if table == "desk_items":
                (n,) = conn.execute("SELECT COUNT(*) FROM desk_items").fetchone()
                if n:
                    counts.skipped += len(rows)
                    continue
            for row in rows:
                decoded = {k: self.remap_exact(decode_value(v)) for k, v in row.items()}
                cur = self.insert(conn, table, decoded, or_ignore=True)
                if cur.rowcount:
                    counts.added += 1
                else:
                    counts.skipped += 1

    def stage_ext_saiverse(self, world: sqlite3.Connection) -> None:
        for table, rows in sorted(self.pack.ext_saiverse_tables.items()):
            counts = self.result.count(f"extensions/saiverse_db/{table}")
            if not rows:
                continue
            if not self.table_exists(world, table):
                counts.skipped += len(rows)
                self.warn(f"saiverse.db has no table {table!r}; {len(rows)} row(s) skipped")
                continue
            # 整数の主キー (rowid の別名) は世界全体の連番なので、取り込み先で振り直させる
            info = world.execute(f"PRAGMA table_info({_quote(table)})").fetchall()
            pk_cols = [r for r in info if r[5]]
            drop_col = pk_cols[0][1] if (
                len(pk_cols) == 1 and str(pk_cols[0][2]).upper() == "INTEGER"
            ) else None
            for row in rows:
                decoded = {
                    k: self.remap_exact(decode_value(v)) for k, v in row.items() if k != drop_col
                }
                cur = self.insert(world, table, decoded, or_ignore=True)
                if cur.rowcount:
                    counts.added += 1
                else:
                    counts.skipped += 1

    def stage_persona_dir_files(self) -> None:
        files = self.pack.ext_persona_files
        if not files:
            return
        counts = self.result.count("extensions/persona_dir")
        for zip_name, rel in files:
            if (len(rel.parts) == 1 and rel.name in _MEMORY_DB_FILES) or rel.parts[0] == _DAY_REPORTS_DIR:
                counts.skipped += 1
                self.warn(f"{zip_name}: not restored (reserved name in the persona folder)")
                continue
            self.plan_keep_existing(zip_name, self.persona_dir.joinpath(*rel.parts), counts)

    def stage_items(self) -> None:
        files = self.pack.item_files
        if not files:
            return
        counts = self.result.count("items")
        base = self.persona_dir / IMPORTED_ITEMS_DIR
        for zip_name, rel in files:
            self.plan_keep_existing(zip_name, base.joinpath(*rel.parts), counts)

    def plan_keep_existing(self, zip_name: str, dest: Path, counts: ImportCounts) -> None:
        """既存のファイルは上書きしない (同じ中身ならスキップ、違えば警告してスキップ)。"""
        if self.is_reserved(dest):
            counts.skipped += 1
            return
        if dest.exists():
            counts.skipped += 1
            if not (dest.is_file() and _sha256_file(dest) == _sha256_bytes(self.reader.read(zip_name))):
                self.warn(f"{zip_name}: {dest} already exists with different content; not overwritten")
            return
        self.plan_file(zip_name, dest)
        counts.added += 1

    def count_merge_skips(self) -> None:
        """合流では items/ と extensions/ を取り込まない (intent §5)。件数だけ載せる。"""
        pack = self.pack
        if pack.item_files:
            self.result.count("items").skipped += len(pack.item_files)
        for table, rows in pack.ext_memory_tables.items():
            self.result.count(f"extensions/memory_db/{table}").skipped += len(rows)
        for table, rows in pack.ext_saiverse_tables.items():
            self.result.count(f"extensions/saiverse_db/{table}").skipped += len(rows)
        if pack.ext_persona_files:
            self.result.count("extensions/persona_dir").skipped += len(pack.ext_persona_files)
