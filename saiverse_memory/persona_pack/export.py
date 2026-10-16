"""ペルソナまるごとパック (persona-pack) の書き出し。

正典: docs/intent/persona_pack.md。ペルソナ一体の全データを一つの ZIP に書き出す。

- **読み取り専用**: saiverse.db / memory.db は SQLite の ``mode=ro`` で開き、
  DB ごとに一つの読み取りトランザクションの中で読む。同じ DB の中の読みは
  そのトランザクションを始めた時点の一枚に揃うが、二つの DB は別の接続なので
  読み取り開始の時点が完全には揃わない (書き出し中の本体の書き込みが、片方の
  DB にだけ入って見えることがある)。ペルソナの状態は一切変えない。
- **SAIVERSE_HOME の外のファイルは入れない**: DB に保存されたパス (添付の
  path・building.IMAGE_PATH・fixture / item の FILE_PATH・アイコン) は、解決後の
  パス (symlink を辿った先) が SAIVERSE_HOME の内側にあるものだけを ZIP に入れる。
  共有メディアフォルダ (image / documents / audio / video) は常に SAIVERSE_HOME の
  直下にある (saiverse/media_utils.py)。外を指すものは ``missing_files`` に
  ``"<where> (outside SAIVERSE_HOME)"`` として記録する。
- **本体サーバーに依存しない**: 入力は ペルソナ ID・saiverse.db のパス・
  SAIVERSE_HOME 相当のルート・出力先パスだけ。``saiverse.data_paths`` の
  環境依存の解決は使わない (テストと SAIVerse Lite で同じ関数を使うため)。
- **途中で倒れても壊れた ZIP を残さない**: 出力先の隣の一時ファイルに書き、
  全部書けたら置き換える。

ZIP の中身 (intent §4 の表):

``manifest.json``
    形式名・版・生成元・作成日時・ペルソナ名・ペルソナ ID・``contents``。
``profile/``
    ``profile.json`` (ai 行の全列。認証情報は ai 行に無い) とアイコン・外見画像。
``chatlog/``
    ``threads.json`` / ``stelis_threads.json`` / ``messages.json``。messages は
    全列と metadata を丸ごと運び、元 DB の rowid を ``seq`` として付ける
    (同じ秒の中の並び順の鍵。native_export.py の ``seq`` と同じ意味)。
    並びは正典順 (created_at, rowid)。
``knowledge/``
    ``pages.json`` (memopedia_pages の全ページ。core / chronicle 分類も含む)。
``attachments/``
    メッセージ metadata の ``images`` / ``documents`` / ``media`` が指す共有
    メディアファイルの実体と ``index.json`` (参照 → ZIP 内パス)。
``items/``
    私室 (building 行・fixture 行) と所持品 (item_location / item 行) と、
    それらが指すファイル。
``journal/``
    ``day_reports/`` (一日新聞)・activities / memos (手帳)・task_book (約束帳)・
    clips (切り抜き)・purpose_tags (目的タグ)。
``extensions/saiverse/``
    共通フォルダに写していない残り。``memory_db/<table>.json`` (memory.db の
    残りのテーブルを機械的に)、``saiverse_db/<table>.json`` (saiverse.db の
    このペルソナの行)、``persona_dir/`` (ペルソナフォルダのファイルそのまま)。
``chatgpt_compat/``
    ``conversations.json`` (会話のやりとりだけの ChatGPT エクスポート互換コピー)。

値の書き方は format.py (bytes の包み・JSON 文字列列の展開・epoch 秒の ISO 化)。
saiverse.db の DateTime 列はタイムゾーンを持たない文字列で保存されているため、
元の値のまま書く (変換に必要なタイムゾーンを DB が持っていない)。例外は
task_book の ``DUE_AT`` / ``CREATED_AT`` / ``CLOSED_AT`` で、これらは DateTime 列では
なく epoch 秒の整数列なので、memory.db の epoch 列と同じく ISO 8601 (UTC) に
変換して書く (EPOCH_COLUMNS)。
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import sqlite3
import urllib.parse
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

from .chatgpt_compat import build_conversation, is_conversation_message
from .format import (
    FORMAT_NAME,
    FORMAT_VERSION,
    MANIFEST_NAME,
    encode_json_text,
    encode_value,
    epoch_to_iso,
)

LOGGER = logging.getLogger(__name__)

# manifest.json の contents に載せる順 (intent §4 の表の順)。
FOLDER_ORDER: Tuple[str, ...] = (
    "profile",
    "chatlog",
    "knowledge",
    "attachments",
    "avatar",
    "voice",
    "items",
    "journal",
    "extensions/saiverse",
    "chatgpt_compat",
)

# --- memory.db のテーブルの割り当て -----------------------------------------
# 共通フォルダへ写すテーブル。ここに無いテーブルは、NOT_CARRIED_MEMORY_TABLES
# を除いて全部 extensions/saiverse/memory_db/ へ機械的に書き出す (未知の
# テーブルも黙って落とさない)。
CHATLOG_TABLES = ("threads", "stelis_threads", "messages")
KNOWLEDGE_TABLES = ("memopedia_pages",)
JOURNAL_MEMORY_TABLES = ("activities", "memos", "clips", "purpose_tags")

# 運ばないテーブル (intent §4「運ばないもの」)。
# - embeddings 系: 意味検索用の数値データ。取り込み後に再生成する。
# perception_* (知覚台帳) は当初ここに入れていたが、sai_memory/
# perception_buffer.py の宣言どおり「消費済み行も削除しない永続台帳 = 知覚の
# 証跡」であり再生成できないため、extensions/saiverse/memory_db/ で運ぶ
# (2026-10-17、intent §4 と同時に訂正)。
NOT_CARRIED_MEMORY_TABLES = frozenset({
    "embeddings",
    "message_embeddings",
    "memopedia_embeddings",
    "memopedia_fragment_embeddings",
    "arasuji_embeddings",
    "embed_metadata",
})

# epoch 秒の列 (ISO 8601 に変換して書く) と JSON 文字列の列 (展開して書く)。
# 共通フォルダへ写すテーブルにだけ適用する。extensions/ の機械的な書き出しは
# 元の値のまま。
EPOCH_COLUMNS: Dict[str, Tuple[str, ...]] = {
    "threads": ("overview_updated_at",),
    "stelis_threads": ("created_at", "completed_at"),
    "messages": ("created_at",),
    "memopedia_pages": ("created_at", "updated_at", "last_referenced_at"),
    "activities": ("born_at", "closed_at"),
    "clips": ("created_at",),
    "purpose_tags": ("created_at",),
    "task_book": ("DUE_AT", "CREATED_AT", "CLOSED_AT"),  # saiverse.db
}
JSON_TEXT_COLUMNS: Dict[str, Tuple[str, ...]] = {
    "messages": ("metadata",),
    "memopedia_pages": ("metadata", "keywords"),
}

# --- saiverse.db のこのペルソナの行 (extensions/saiverse/saiverse_db/) ---------
# (テーブル名, WHERE 句)。WHERE 句の ? にはペルソナ ID が入る。
SAIVERSE_DB_EXTENSION_TABLES: Tuple[Tuple[str, str], ...] = (
    ("persona_task", "persona_id = ?"),
    ("persona_task_step",
     "task_id IN (SELECT id FROM persona_task WHERE persona_id = ?)"),
    ("persona_task_history",
     "task_id IN (SELECT id FROM persona_task WHERE persona_id = ?)"),
    ("persona_schedule", "PERSONA_ID = ?"),
    ("persona_day_plan", "persona_id = ?"),
    ("persona_timetable_template", "PERSONA_ID = ?"),
    ("persona_event_log", "PERSONA_ID = ?"),
    ("feed_read_cursor", "PERSONA_ID = ?"),
    ("building_occupancy_log", "AIID = ?"),
    ("episodes", "PERSONA_ID = ?"),
    ("episode_inheritance", "PERSONA_ID = ?"),
    ("action_track", "persona_id = ?"),
    ("track_local_log",
     "track_id IN (SELECT track_id FROM action_track WHERE persona_id = ?)"),
    ("meta_judgment_log", "persona_id = ?"),
)

# ペルソナフォルダのうち、別の形で運ぶ (memory.db) ためファイルとしては写さないもの。
_MEMORY_DB_FILES = frozenset({
    "memory.db", "memory.db-wal", "memory.db-shm", "memory.db-journal",
})
_DAY_REPORTS_DIR = "day_reports"

# メッセージ metadata の添付の置き場 (api/routes/chat.py の /chat/send が書く 3 つ)。
_ATTACHMENT_KEYS = ("images", "documents", "media")
# saiverse://<種類>/<ファイル名> → 共有メディアフォルダ名 (saiverse/media_utils.py の
# resolve_media_uri と同じ対応)。
_MEDIA_URI_DIRS: Tuple[Tuple[str, str], ...] = (
    ("saiverse://image/", "image"),
    ("saiverse://document/", "documents"),
    ("saiverse://audio/", "audio"),
    ("saiverse://video/", "video"),
)
_MEDIA_DIR_NAMES = frozenset(d for _, d in _MEDIA_URI_DIRS)
_SUMMARY_SUFFIX = ".summary.txt"  # saiverse/media_utils.py の SUMMARY_SUFFIX
# missing_files の where に添える印 (解決後のパスが SAIVERSE_HOME の外を指したもの)
_OUTSIDE_HOME = "outside SAIVERSE_HOME"
_SYMLINK_SKIPPED = "symlink skipped"
# 読み取り接続の busy_timeout (ミリ秒)
_BUSY_TIMEOUT_MS = 5000


@dataclass
class PersonaPackExportResult:
    """書き出しの結果。"""

    output_path: Path
    contents: List[str]
    counts: Dict[str, int] = field(default_factory=dict)
    # ZIP に入れられなかったもの: {"ref", "where"}。参照はあるがファイルが無いもの、
    # SAIVERSE_HOME の外を指すもの、symlink、item 行の無い所持品、辿れない置き場所の行
    missing_files: List[Dict[str, str]] = field(default_factory=list)
    # 運ばなかった memory.db のテーブル名
    not_carried_tables: List[str] = field(default_factory=list)
    # 読む予定だったが DB に無かったテーブル (古い DB など): "<db>:<table>"
    absent_tables: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# SQLite 読み取り
# ---------------------------------------------------------------------------

def _open_read_only(db_path: Path) -> sqlite3.Connection:
    """DB を読み取り専用 (mode=ro) で開き、読み取りトランザクションを始める。

    本体が書き込み中でも待てるよう busy_timeout を設定する (_BUSY_TIMEOUT_MS)。
    """
    uri = "file:" + urllib.parse.quote(db_path.resolve().as_posix(), safe="/:") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        conn.execute("BEGIN")
    except BaseException:
        conn.close()
        raise
    return conn


def _table_names(conn: sqlite3.Connection) -> List[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is not None


def _iter_rows(
    conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()
) -> Iterator[Dict[str, Any]]:
    cur = conn.execute(sql, params)
    names = [d[0] for d in cur.description]
    for row in cur:
        yield dict(zip(names, row))


def _iter_table_rows(
    conn: sqlite3.Connection, table: str, where: str = "", params: Sequence[Any] = ()
) -> Iterator[Dict[str, Any]]:
    """テーブルの全列を rowid 順に返す (rowid の無いテーブルは並び指定なし)。"""
    quoted = '"' + table.replace('"', '""') + '"'
    clause = f" WHERE {where}" if where else ""
    try:
        conn.execute(f"SELECT rowid FROM {quoted} LIMIT 0")
        order = " ORDER BY rowid"
    except sqlite3.OperationalError:
        order = ""
    return _iter_rows(conn, f"SELECT * FROM {quoted}{clause}{order}", params)


def _encode_row(row: Dict[str, Any], table: Optional[str] = None) -> Dict[str, Any]:
    """1 行を JSON に書ける形にする。table を渡すと epoch / JSON 文字列列を展開する。"""
    epoch_cols = EPOCH_COLUMNS.get(table or "", ())
    json_cols = JSON_TEXT_COLUMNS.get(table or "", ())
    out: Dict[str, Any] = {}
    for key, value in row.items():
        if key in epoch_cols:
            out[key] = epoch_to_iso(value)
        elif key in json_cols:
            out[key] = encode_json_text(value)
        else:
            out[key] = encode_value(value)
    return out


# ---------------------------------------------------------------------------
# ZIP 書き込み
# ---------------------------------------------------------------------------

def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name) or "_"


class _PackWriter:
    def __init__(self, zf: zipfile.ZipFile) -> None:
        self.zf = zf
        self.folders: Set[str] = set()
        self.names: Set[str] = set()
        # (フォルダ, ディスク上のファイル) → ZIP 内パス。同じフォルダに同じファイルを
        # 二度入れない。フォルダが違えば別に入れる (フォルダ単位で取り込めるように)。
        self._copied: Dict[Tuple[str, Path], str] = {}

    def _register(self, folder: str, arcname: str) -> None:
        if arcname in self.names:
            raise ValueError(f"duplicate entry in persona pack: {arcname}")
        self.names.add(arcname)
        self.folders.add(folder)

    def write_json(self, folder: str, arcname: str, obj: Any) -> None:
        self._register(folder, arcname)
        self.zf.writestr(arcname, json.dumps(obj, ensure_ascii=False, indent=2))

    def write_json_array(self, folder: str, arcname: str, items: Iterable[Any]) -> int:
        """JSON 配列を一要素ずつ書く (大きな表を全部メモリに載せないため)。"""
        self._register(folder, arcname)
        count = 0
        with self.zf.open(arcname, "w", force_zip64=True) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8")
            text.write("[")
            for item in items:
                text.write("\n" if count == 0 else ",\n")
                text.write(json.dumps(item, ensure_ascii=False))
                count += 1
            text.write("\n]\n" if count else "]\n")
            text.flush()
            text.detach()
        return count

    def copy_file(self, folder: str, src: Path, arcname: str) -> str:
        """ファイルを ZIP に入れて ZIP 内パスを返す。名前が衝突したら番号を足す。"""
        key = (folder, src.resolve())
        if key in self._copied:
            return self._copied[key]
        candidate = arcname
        stem, dot, ext = arcname.rpartition(".")
        if not dot or "/" in ext:
            stem, ext = arcname, ""
        n = 1
        while candidate in self.names:
            candidate = f"{stem}_{n}.{ext}" if ext else f"{stem}_{n}"
            n += 1
        self._register(folder, candidate)
        self.zf.write(src, candidate)
        self._copied[key] = candidate
        return candidate


# ---------------------------------------------------------------------------
# ファイル参照の解決
# ---------------------------------------------------------------------------

def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _stored_candidate(value: Any, home: Path) -> Optional[Path]:
    """DB に保存されたファイルパス (SAIVERSE_HOME からの相対 or 絶対) を、存在の検査なしで組む。"""
    if not isinstance(value, str) or not value.strip():
        return None
    p = Path(value.strip())
    return p if p.is_absolute() else home / p


def _resolve_stored_path(value: Any, home: Path) -> Optional[Path]:
    """DB に保存されたファイルパスを解決する。

    ファイルが無いときと、解決後のパスが SAIVERSE_HOME の外にあるときは None
    (モジュール docstring)。呼び出し側は :func:`_missing_entry` で記録する。
    """
    candidate = _stored_candidate(value, home)
    if candidate is None or not candidate.is_file() or not _is_within(candidate, home):
        return None
    return candidate


def _missing_entry(value: Any, where: str, home: Path) -> Dict[str, str]:
    """解決できなかった保存パスの missing_files の 1 件。外を指していたら where に印を添える。"""
    candidate = _stored_candidate(value, home)
    if candidate is not None and candidate.is_file() and not _is_within(candidate, home):
        where = f"{where} ({_OUTSIDE_HOME})"
    return {"ref": str(value), "where": where}


def _resolve_icon_path(value: Any, home: Path) -> Optional[Path]:
    """ai.AVATAR_IMAGE / APPEARANCE_IMAGE_PATH を解決する。

    保存形は api/deps.py の avatar_path_to_url が受ける形 (絶対パス、
    ``user_data/icons/<名前>`` を含むパス、``/api/static/user_icons/<名前>``)。
    SAIVERSE_HOME の外のファイルは返さない。
    """
    if not isinstance(value, str) or not value.strip():
        return None
    direct = _resolve_stored_path(value, home)
    if direct is not None:
        return direct
    normalized = value.strip().replace("\\", "/")
    for marker in ("user_data/icons/", "/api/static/user_icons/"):
        if marker in normalized:
            name = normalized.split(marker)[-1]
            if name and Path(name).name == name:
                candidate = home / "user_data" / "icons" / name
                if candidate.is_file() and _is_within(candidate, home):
                    return candidate
    return None


def _resolve_media_item(
    item: Dict[str, Any], home: Path
) -> Tuple[Optional[Path], str, bool]:
    """添付の項目 (uri / path) を共有メディアフォルダのファイルへ解決する。

    返り値は (ファイル, ZIP 内の種類フォルダ名, SAIVERSE_HOME の外を指したので
    入れなかったか)。saiverse/media_utils.py の iter_image_media と同じく uri を
    先に、見つからなければ path を見る。解決後のパスが SAIVERSE_HOME の内側に
    あるものだけを認める (任意の場所のファイルを ZIP に入れないため)。
    """
    uri = item.get("uri")
    kind = "other"
    outside = False
    if isinstance(uri, str):
        for prefix, dirname in _MEDIA_URI_DIRS:
            if uri.startswith(prefix):
                kind = dirname
                name = uri[len(prefix):].strip()
                if name and Path(name).name == name:
                    candidate = home / dirname / name
                    if candidate.is_file():
                        if _is_within(candidate, home):
                            return candidate, kind, False
                        outside = True
                break
    raw_path = item.get("path")
    if isinstance(raw_path, str) and raw_path.strip():
        p = Path(raw_path.strip())
        if not p.is_absolute():
            p = home / p
        if p.is_file():
            if not _is_within(p, home):
                return None, kind, True
            if kind == "other" and p.parent.name in _MEDIA_DIR_NAMES:
                kind = p.parent.name
            return p, kind, False
    return None, kind, outside


def _attachment_refs(metadata: Any) -> Iterator[Dict[str, Any]]:
    if not isinstance(metadata, dict):
        return
    for key in _ATTACHMENT_KEYS:
        entries = metadata.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            uri = entry.get("uri")
            path = entry.get("path")
            if (isinstance(uri, str) and uri.strip()) or (isinstance(path, str) and path.strip()):
                yield entry


def _attachment_key(entry: Dict[str, Any]) -> str:
    """添付の項目を同一視する鍵 (uri があれば uri、無ければ path)。"""
    uri = entry.get("uri")
    if isinstance(uri, str) and uri.strip():
        return uri.strip()
    return str(entry.get("path")).strip()


# ---------------------------------------------------------------------------
# 本体
# ---------------------------------------------------------------------------

def export_persona_pack(
    persona_id: str,
    *,
    saiverse_db_path: Path,
    saiverse_home: Path,
    output_path: Path,
    generator_version: Optional[str] = None,
    now: Optional[datetime] = None,
) -> PersonaPackExportResult:
    """ペルソナ一体をペルソナまるごとパック (ZIP) に書き出す。

    Args:
        persona_id: 書き出すペルソナの ID (ai.AIID)。
        saiverse_db_path: 本体 DB (saiverse.db) のパス。
        saiverse_home: SAIVERSE_HOME 相当のルート。ペルソナフォルダは
            ``<root>/personas/<persona_id>/``、共有メディアは ``<root>/image/`` 等。
        output_path: 書き出す ZIP のパス。既にあれば置き換える。
        generator_version: manifest の generator.version。省略時は
            ``saiverse.__version__``。
        now: manifest の created_at (タイムゾーン付き)。省略時は現在時刻 (UTC)。

    Raises:
        FileNotFoundError: saiverse.db が無い。
        ValueError: ai テーブルにペルソナの行が無い。
    """
    saiverse_db_path = Path(saiverse_db_path)
    home = Path(saiverse_home)
    output_path = Path(output_path)
    if not saiverse_db_path.is_file():
        raise FileNotFoundError(f"saiverse.db not found: {saiverse_db_path}")

    if generator_version is None:
        import saiverse

        generator_version = saiverse.__version__
    created = now or datetime.now(timezone.utc)
    if created.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    persona_dir = home / "personas" / persona_id
    memory_db_path = persona_dir / "memory.db"

    result = PersonaPackExportResult(output_path=output_path, contents=[])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    partial = output_path.with_name(output_path.name + ".partial")
    world = _open_read_only(saiverse_db_path)
    memory: Optional[sqlite3.Connection] = None
    try:
        if memory_db_path.is_file():
            memory = _open_read_only(memory_db_path)
        ai_rows = list(_iter_rows(world, "SELECT * FROM ai WHERE AIID = ?", (persona_id,)))
        if not ai_rows:
            raise ValueError(f"persona not found in saiverse.db: {persona_id}")
        ai_row = ai_rows[0]

        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            writer = _PackWriter(zf)
            _write_profile(writer, ai_row, home, result)
            message_ids: Set[str] = set()
            if memory is not None:
                _write_memory_db(writer, memory, persona_id, home, result, message_ids)
            _write_items(writer, world, persona_id, ai_row, home, result)
            _write_journal_files(writer, world, persona_id, persona_dir, result)
            _write_world_extensions(writer, world, persona_id, message_ids, result)
            _write_persona_dir_files(writer, persona_dir, result)

            contents = [f for f in FOLDER_ORDER if f in writer.folders]
            unknown = writer.folders - set(FOLDER_ORDER)
            if unknown:
                raise RuntimeError(f"unlisted folders in persona pack: {sorted(unknown)}")
            manifest = {
                "format": FORMAT_NAME,
                "version": FORMAT_VERSION,
                "generator": {"name": "SAIVerse", "version": generator_version},
                "created_at": created.isoformat(),
                "persona_name": ai_row.get("AINAME"),
                "persona_id": persona_id,
                "contents": contents,
            }
            zf.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))
            result.contents = contents
        os.replace(partial, output_path)
    except BaseException:
        try:
            partial.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        world.rollback()
        world.close()
        if memory is not None:
            memory.rollback()
            memory.close()

    LOGGER.info(
        "[persona_pack] exported persona=%s to %s contents=%s counts=%s missing=%d",
        persona_id, output_path, result.contents, result.counts, len(result.missing_files),
    )
    return result


# --- profile/ ---------------------------------------------------------------

def _write_profile(
    writer: _PackWriter, ai_row: Dict[str, Any], home: Path, result: PersonaPackExportResult
) -> None:
    files: Dict[str, Optional[str]] = {}
    for column, subdir in (("AVATAR_IMAGE", "icon"), ("APPEARANCE_IMAGE_PATH", "appearance")):
        value = ai_row.get(column)
        if not value:
            continue
        src = _resolve_icon_path(value, home)
        if src is None:
            files[column] = None
            result.missing_files.append(_missing_entry(value, f"ai.{column}", home))
            continue
        files[column] = writer.copy_file("profile", src, f"profile/{subdir}/{src.name}")
    writer.write_json("profile", "profile/profile.json", {
        "ai": _encode_row(ai_row),
        "files": files,
    })


# --- memory.db: chatlog/ knowledge/ attachments/ journal/ extensions/ chatgpt_compat/

def _write_memory_db(
    writer: _PackWriter,
    memory: sqlite3.Connection,
    persona_id: str,
    home: Path,
    result: PersonaPackExportResult,
    message_ids: Set[str],
) -> None:
    tables = _table_names(memory)
    present = set(tables)
    for table in (*CHATLOG_TABLES, *KNOWLEDGE_TABLES, *JOURNAL_MEMORY_TABLES):
        if table not in present:
            result.absent_tables.append(f"memory.db:{table}")

    # chatlog/
    thread_titles: Dict[str, Optional[str]] = {}
    if "threads" in present:
        thread_rows = list(_iter_table_rows(memory, "threads"))
        for row in thread_rows:
            thread_titles[row["id"]] = row.get("title")
        result.counts["threads"] = writer.write_json_array(
            "chatlog", "chatlog/threads.json",
            (_encode_row(r, "threads") for r in thread_rows),
        )
    if "stelis_threads" in present:
        result.counts["stelis_threads"] = writer.write_json_array(
            "chatlog", "chatlog/stelis_threads.json",
            (_encode_row(r, "stelis_threads") for r in _iter_table_rows(memory, "stelis_threads")),
        )

    attachment_refs: Dict[str, Dict[str, Any]] = {}
    conversations: Dict[str, List[Dict[str, Any]]] = {}
    if "messages" in present:
        def _messages() -> Iterator[Dict[str, Any]]:
            rows = _iter_rows(
                memory,
                "SELECT rowid AS __pack_rowid, * FROM messages "
                "ORDER BY created_at ASC, rowid ASC",
            )
            for row in rows:
                seq = row.pop("__pack_rowid")
                message_id = row.get("id")
                message_ids.add(message_id)
                encoded = _encode_row(row, "messages")
                encoded["seq"] = seq
                metadata = encoded.get("metadata")
                for ref in _attachment_refs(metadata):
                    entry = attachment_refs.setdefault(
                        _attachment_key(ref), {"item": ref, "message_ids": []}
                    )
                    entry["message_ids"].append(message_id)
                thread_id = row.get("thread_id")
                if isinstance(thread_id, str) and is_conversation_message(
                    row.get("role"), row.get("scope"), metadata, row.get("content")
                ):
                    conversations.setdefault(thread_id, []).append({
                        "id": message_id,
                        "role": row.get("role"),
                        "content": row.get("content"),
                        "created_at": row.get("created_at"),
                    })
                yield encoded

        result.counts["messages"] = writer.write_json_array(
            "chatlog", "chatlog/messages.json", _messages()
        )

    # knowledge/
    if "memopedia_pages" in present:
        result.counts["pages"] = writer.write_json_array(
            "knowledge", "knowledge/pages.json",
            (_encode_row(r, "memopedia_pages") for r in _iter_table_rows(memory, "memopedia_pages")),
        )

    # attachments/
    if attachment_refs:
        index: List[Dict[str, Any]] = []
        for ref_key, entry in attachment_refs.items():
            item = entry["item"]
            src, kind, outside = _resolve_media_item(item, home)
            zip_path: Optional[str] = None
            summary_path: Optional[str] = None
            if src is None:
                where = f"messages.metadata ({_OUTSIDE_HOME})" if outside else "messages.metadata"
                result.missing_files.append({"ref": str(ref_key), "where": where})
            else:
                zip_path = writer.copy_file("attachments", src, f"attachments/{kind}/{src.name}")
                summary_src = src.with_name(src.name + _SUMMARY_SUFFIX)
                if summary_src.is_file() and _is_within(summary_src, home):
                    summary_path = writer.copy_file(
                        "attachments", summary_src, zip_path + _SUMMARY_SUFFIX
                    )
            index.append({
                "ref": ref_key,
                "uri": item.get("uri"),
                "original_path": item.get("path"),
                "mime_type": item.get("mime_type"),
                "path": zip_path,
                "summary_path": summary_path,
                "message_ids": entry["message_ids"],
            })
        writer.write_json("attachments", "attachments/index.json", index)
        result.counts["attachments"] = sum(1 for i in index if i["path"])

    # journal/ (memory.db 側)
    for table in JOURNAL_MEMORY_TABLES:
        if table in present:
            result.counts[table] = writer.write_json_array(
                "journal", f"journal/{table}.json",
                (_encode_row(r, table) for r in _iter_table_rows(memory, table)),
            )

    # extensions/saiverse/memory_db/ (残り全部)
    assigned = set(CHATLOG_TABLES) | set(KNOWLEDGE_TABLES) | set(JOURNAL_MEMORY_TABLES)
    for table in tables:
        if table in assigned:
            continue
        if table in NOT_CARRIED_MEMORY_TABLES or table.startswith("sqlite_"):
            result.not_carried_tables.append(table)
            continue
        writer.write_json_array(
            "extensions/saiverse",
            f"extensions/saiverse/memory_db/{_safe_name(table)}.json",
            (_encode_row(r) for r in _iter_table_rows(memory, table)),
        )

    # chatgpt_compat/
    if conversations:
        built = []
        for thread_id, items in conversations.items():
            conv = build_conversation(thread_id, thread_titles.get(thread_id), items)
            if conv is not None:
                built.append(conv)
        writer.write_json("chatgpt_compat", "chatgpt_compat/conversations.json", built)
        result.counts["chatgpt_conversations"] = len(built)


# --- items/ -----------------------------------------------------------------

def _write_items(
    writer: _PackWriter,
    world: sqlite3.Connection,
    persona_id: str,
    ai_row: Dict[str, Any],
    home: Path,
    result: PersonaPackExportResult,
) -> None:
    room_id = ai_row.get("PRIVATE_ROOM_ID")

    # 私室: building 行と、その部屋の fixture 行
    if room_id and _table_exists(world, "building"):
        rooms = list(_iter_rows(world, "SELECT * FROM building WHERE BUILDINGID = ?", (room_id,)))
        if rooms:
            room = rooms[0]
            files: Dict[str, Any] = {"IMAGE_PATH": None, "fixtures": {}}
            image = _resolve_stored_path(room.get("IMAGE_PATH"), home)
            if image is not None:
                files["IMAGE_PATH"] = writer.copy_file(
                    "items", image, f"items/private_room/{image.name}"
                )
            elif room.get("IMAGE_PATH"):
                result.missing_files.append(
                    _missing_entry(room.get("IMAGE_PATH"), "building.IMAGE_PATH", home)
                )
            fixtures: List[Dict[str, Any]] = []
            if _table_exists(world, "fixture"):
                fixtures = list(_iter_table_rows(world, "fixture", "BUILDING_ID = ?", (room_id,)))
                for fx in fixtures:
                    src = _resolve_stored_path(fx.get("FILE_PATH"), home)
                    if src is not None:
                        files["fixtures"][fx["FIXTURE_ID"]] = writer.copy_file(
                            "items", src, f"items/fixtures/{_safe_name(fx['FIXTURE_ID'])}/{src.name}"
                        )
                    elif fx.get("FILE_PATH"):
                        result.missing_files.append(
                            _missing_entry(fx.get("FILE_PATH"), "fixture.FILE_PATH", home)
                        )
            else:
                result.absent_tables.append("saiverse.db:fixture")
            writer.write_json("items", "items/private_room.json", {
                "building": _encode_row(room),
                "fixtures": [_encode_row(f) for f in fixtures],
                "files": files,
            })

    # 所持品: ペルソナが持つもの・私室に置かれたもの・それらを入れ物にしているもの。
    # item_location.OWNER_KIND は building / persona / bag の三つ (database/models.py。
    # 'world' = どこにも置かれていない は行を持たない)。入れ物の中身は TYPE を問わず
    # OWNER_KIND='bag' で辿る (manager/admin.py の _collect_nested_item_ids と同じ —
    # TYPE を bag から変えられた入れ物の中身も拾う)。
    if not (_table_exists(world, "item_location") and _table_exists(world, "item")):
        result.absent_tables.append("saiverse.db:item_location/item")
        return
    owners: List[Tuple[str, str]] = [("persona", persona_id)]
    if room_id:
        owners.append(("building", room_id))
    locations: List[Dict[str, Any]] = []
    items: Dict[str, Dict[str, Any]] = {}
    missing_items: Set[str] = set()
    seen_owners: Set[Tuple[str, str]] = set()
    while owners:
        owner = owners.pop(0)
        if owner in seen_owners:
            continue
        seen_owners.add(owner)
        for loc in _iter_table_rows(
            world, "item_location", "OWNER_KIND = ? AND OWNER_ID = ?", owner
        ):
            locations.append(loc)
            item_id = loc.get("ITEM_ID")
            if item_id in items or item_id in missing_items:
                continue
            found = list(_iter_rows(world, "SELECT * FROM item WHERE ITEM_ID = ?", (item_id,)))
            if not found:
                missing_items.add(item_id)
                result.missing_files.append(
                    {"ref": str(item_id), "where": "item (no row for item_location.ITEM_ID)"}
                )
                continue
            items[item_id] = found[0]
            owners.append(("bag", item_id))
    # 走査した持ち主を指しているのに、OWNER_KIND が三つのどれでもない行 (辿れない)
    owner_ids = sorted({owner_id for _, owner_id in seen_owners})
    for i in range(0, len(owner_ids), 500):
        part = owner_ids[i:i + 500]
        for loc in _iter_table_rows(
            world, "item_location",
            f"OWNER_ID IN ({', '.join('?' for _ in part)}) "
            "AND OWNER_KIND NOT IN ('persona', 'building', 'bag')",
            part,
        ):
            result.missing_files.append({
                "ref": str(loc.get("ITEM_ID")),
                "where": f"item_location.OWNER_KIND={loc.get('OWNER_KIND')!r} not traversed",
            })
    if not locations:
        return
    item_files: Dict[str, str] = {}
    for item_id, item in items.items():
        src = _resolve_stored_path(item.get("FILE_PATH"), home)
        if src is not None:
            arc = writer.copy_file("items", src, f"items/files/{_safe_name(item_id)}/{src.name}")
            item_files[item_id] = arc
            summary_src = src.with_name(src.name + _SUMMARY_SUFFIX)
            if summary_src.is_file() and _is_within(summary_src, home):
                writer.copy_file("items", summary_src, arc + _SUMMARY_SUFFIX)
        elif item.get("FILE_PATH"):
            result.missing_files.append(_missing_entry(item.get("FILE_PATH"), "item.FILE_PATH", home))
    writer.write_json("items", "items/items.json", {
        "locations": [_encode_row(loc) for loc in locations],
        "items": [_encode_row(i) for i in items.values()],
        "files": item_files,
    })
    result.counts["items"] = len(items)


# --- journal/ (saiverse.db 側とペルソナフォルダ側) ---------------------------

def _copyable_persona_file(path: Path, persona_dir: Path, result: PersonaPackExportResult) -> bool:
    """ペルソナフォルダを走査して見つけた path を ZIP に写してよいか。

    symlink は写さず missing_files に記録する。symlink でなくても解決後のパスが
    ペルソナフォルダの外にあるもの (Windows のジャンクション配下など) は写さず記録する。
    ディレクトリは黙って飛ばす。
    """
    if path.is_symlink():
        result.missing_files.append({"ref": str(path), "where": _SYMLINK_SKIPPED})
        return False
    if not path.is_file():
        return False
    if not _is_within(path, persona_dir):
        result.missing_files.append(
            {"ref": str(path), "where": "persona folder (resolves outside the persona folder)"}
        )
        return False
    return True


def _write_journal_files(
    writer: _PackWriter,
    world: sqlite3.Connection,
    persona_id: str,
    persona_dir: Path,
    result: PersonaPackExportResult,
) -> None:
    if _table_exists(world, "task_book"):
        result.counts["task_book"] = writer.write_json_array(
            "journal", "journal/task_book.json",
            (
                _encode_row(r, "task_book")
                for r in _iter_table_rows(world, "task_book", "PERSONA_ID = ?", (persona_id,))
            ),
        )
    else:
        result.absent_tables.append("saiverse.db:task_book")

    reports_dir = persona_dir / _DAY_REPORTS_DIR
    if reports_dir.is_dir():
        count = 0
        for path in sorted(reports_dir.rglob("*")):
            if not _copyable_persona_file(path, persona_dir, result):
                continue
            rel = path.relative_to(reports_dir).as_posix()
            writer.copy_file("journal", path, f"journal/day_reports/{rel}")
            count += 1
        result.counts["day_reports"] = count


# --- extensions/saiverse/saiverse_db/ ----------------------------------------

def _write_world_extensions(
    writer: _PackWriter,
    world: sqlite3.Connection,
    persona_id: str,
    message_ids: Set[str],
    result: PersonaPackExportResult,
) -> None:
    for table, where in SAIVERSE_DB_EXTENSION_TABLES:
        if not _table_exists(world, table):
            result.absent_tables.append(f"saiverse.db:{table}")
            continue
        writer.write_json_array(
            "extensions/saiverse",
            f"extensions/saiverse/saiverse_db/{table}.json",
            (_encode_row(r) for r in _iter_table_rows(world, table, where, (persona_id,))),
        )
    # アドオンがメッセージに付けた値 (このペルソナの memory.db のメッセージ宛てだけ)
    if _table_exists(world, "addon_message_metadata"):
        writer.write_json_array(
            "extensions/saiverse",
            "extensions/saiverse/saiverse_db/addon_message_metadata.json",
            (
                _encode_row(r) for r in _iter_table_rows(world, "addon_message_metadata")
                if r.get("message_id") in message_ids
            ),
        )
    else:
        result.absent_tables.append("saiverse.db:addon_message_metadata")


# --- extensions/saiverse/persona_dir/ ----------------------------------------

def _write_persona_dir_files(
    writer: _PackWriter, persona_dir: Path, result: PersonaPackExportResult
) -> None:
    """ペルソナフォルダのファイルをそのまま写す (memory.db 一式と day_reports/ を除く)。"""
    if not persona_dir.is_dir():
        return
    count = 0
    for path in sorted(persona_dir.rglob("*")):
        rel = path.relative_to(persona_dir)
        if len(rel.parts) == 1 and rel.name in _MEMORY_DB_FILES:
            continue
        if rel.parts[0] == _DAY_REPORTS_DIR:
            continue  # _write_journal_files が扱う (symlink の記録も向こうで行う)
        if not _copyable_persona_file(path, persona_dir, result):
            continue
        writer.copy_file(
            "extensions/saiverse", path, f"extensions/saiverse/persona_dir/{rel.as_posix()}"
        )
        count += 1
    result.counts["persona_dir_files"] = count
