"""Lite から帰るときの合流の、取り込み先へ書かない部分。

正典: docs/intent/persona_pack.md §5「Lite から帰るときの合流」。形式の仕様は
docs/reference/persona-pack-format.md。

ここに置くもの:

- ``extensions/lite/changes.json`` の読み取りと検査 (read_changes / check_against_pack)。
- 一覧の計算 (build_review)。取り込み先の DB は読むだけ。
- 一覧の保存と読み出し (write_review / read_review_*)。件数が多くても一度に全部を
  画面へ返さないよう、一覧は見比べのフォルダの SQLite に置き、画面は 1 ページずつ読む。
- 選択の検査 (resolve_choices) と、計算し直した一覧との照合 (check_selection / check_profile)。
- memory.db の複製 (backup_memory_db) と active_state.json の書き換え (write_active_thread)。

書き込みの本体は importer.py の ``_ImportJob`` にあり、§5 の合流と同じ経路を通る。
importer.py がこのモジュールを読むので、ここから importer.py を読まない。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .format import decode_value, iso_to_epoch

LOGGER = logging.getLogger(__name__)

CHANGES_FOLDER = "extensions/lite"
CHANGES_NAME = "extensions/lite/changes.json"
CHANGES_FORMAT = "saiverse-lite-changes"
CHANGES_VERSION = 1

ACTIVE_STATE_FILENAME = "active_state.json"  # saiverse_memory/adapter.py の _ACTIVE_STATE_FILENAME
ACTIVE_STATE_ENTRY = "extensions/saiverse/persona_dir/" + ACTIVE_STATE_FILENAME
FRAGMENTS_TABLE = "memopedia_fragments"
# 選ばれたページの上書きで書く memopedia_pages の列。それ以外の列は本体の値を残す
PAGE_COLUMNS: Tuple[str, ...] = ("title", "summary", "content", "updated_at")
# saiverse_memory/adapter.py の _PERSONA_THREAD_SUFFIX (active_state.json が無いときの続き先)
DEFAULT_THREAD_SUFFIX = "__persona__"

CHOICE_LITE = "lite"
CHOICE_BODY = "body"
CHOICES = (CHOICE_LITE, CHOICE_BODY)

KIND_MESSAGE = "message"
KIND_PAGE = "page"
KIND_PROFILE = "profile"
KIND_ACTIVE_THREAD = "active_thread"
KINDS = (KIND_PROFILE, KIND_ACTIVE_THREAD, KIND_PAGE, KIND_MESSAGE)

CHANGE_EDITED = "edited"
CHANGE_DELETED = "deleted"

# Lite で変えられる ai の列。changes.json の profile.edited はこの列名か、右の短い名前で書く
PROFILE_COLUMNS: Tuple[str, ...] = ("AINAME", "SYSTEMPROMPT", "DESCRIPTION")
_PROFILE_ALIASES = {"name": "AINAME", "system_prompt": "SYSTEMPROMPT", "description": "DESCRIPTION"}

# 一覧の 1 ページに載せる本文の長さ。これより長い本文は full=true の読み直しで返す
PREVIEW_CHARS = 1200

BACKUP_KEEP = 3
_BACKUP_DIR = ("backups", "persona_pack_merge")

_REVIEW_DDL = (
    "CREATE TABLE IF NOT EXISTS items ("
    "idx INTEGER PRIMARY KEY, item_key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, "
    "change TEXT NOT NULL, fingerprint TEXT NOT NULL, item TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT NOT NULL)",
)


class ReturnMergeRequired(ValueError):
    """changes.json を持つパックを、選択なしで合流しようとした。"""


class ReturnMergeStale(Exception):
    """一覧に出した項目の本体の値が、見比べた時点から変わった。"""

    def __init__(self, keys: Sequence[str]) -> None:
        self.keys = list(keys)
        super().__init__(f"{len(self.keys)} compared item(s) changed since the comparison")


class ReturnMergeBackupError(RuntimeError):
    """適用の前の memory.db の複製ができなかった。"""


class ReturnMergeWorldFailed(RuntimeError):
    """memory.db の確定の後に saiverse.db の確定が失敗した (見比べ直せば残りを適用できる)。"""


# ---------------------------------------------------------------------------
# changes.json
# ---------------------------------------------------------------------------

@dataclass
class ReturnChanges:
    """changes.json の中身。各辞書は {id: Lite で変えた時刻 (epoch か None)}。"""

    messages_created: Dict[str, Any] = field(default_factory=dict)
    messages_edited: Dict[str, Any] = field(default_factory=dict)
    messages_deleted: Dict[str, Any] = field(default_factory=dict)
    pages_created: Dict[str, Any] = field(default_factory=dict)
    pages_edited: Dict[str, Any] = field(default_factory=dict)
    pages_deleted: Dict[str, Any] = field(default_factory=dict)
    fragments_created: Dict[str, Any] = field(default_factory=dict)
    # {ai の列名: Lite で変えた時刻}
    profile_edited: Dict[str, Any] = field(default_factory=dict)


def _bad(detail: str) -> ValueError:
    return ValueError(f"persona pack: {CHANGES_NAME}: {detail}")


def _epoch(value: Any, where: str) -> Any:
    """changed_at の値を epoch にする (数値か ISO 8601。読めないものは ValueError)。"""
    if value is None:
        return None
    converted = iso_to_epoch(value)
    if isinstance(converted, bool) or not isinstance(converted, (int, float)):
        raise _bad(f"{where}: changed_at must be a number or an ISO 8601 text, got {value!r}")
    return converted


def _entries(section: Mapping[str, Any], name: str, where: str) -> Dict[str, Any]:
    """id の list を {id: changed_at} にする。要素は id の文字列か ``{"id", "changed_at"?}``。"""
    values = section.get(name)
    if not isinstance(values, list):
        raise _bad(f"{where}.{name} must be a list")
    out: Dict[str, Any] = {}
    for i, entry in enumerate(values):
        changed_at = None
        if isinstance(entry, dict):
            changed_at = _epoch(entry.get("changed_at"), f"{where}.{name}[{i}]")
            entry = entry.get("id")
        if not isinstance(entry, str) or not entry:
            raise _bad(f"{where}.{name}[{i}] must be an id")
        out[entry] = changed_at
    return out


def _section(doc: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    section = doc.get(name)
    if not isinstance(section, dict):
        raise _bad(f"{name} must be an object")
    return section


def _refuse_overlap(where: str, **lists: Mapping[str, Any]) -> None:
    names = sorted(lists)
    for i, first in enumerate(names):
        for second in names[i + 1:]:
            both = set(lists[first]) & set(lists[second])
            if both:
                raise _bad(f"{where}: {sorted(both)[0]!r} is in both {first} and {second}")


def parse_changes(doc: Any) -> ReturnChanges:
    """changes.json の中身を検査して ReturnChanges にする (形の不正は ValueError)。"""
    if not isinstance(doc, dict):
        raise _bad("must be an object")
    if doc.get("format") != CHANGES_FORMAT:
        raise _bad(f"unsupported format {doc.get('format')!r}")
    version = doc.get("version")
    if isinstance(version, bool) or version != CHANGES_VERSION:
        raise _bad(f"unsupported version {version!r}")
    messages = _section(doc, "messages")
    pages = _section(doc, "pages")
    fragments = _section(doc, "fragments")
    profile = _section(doc, "profile")
    changes = ReturnChanges(
        messages_created=_entries(messages, "created", "messages"),
        messages_edited=_entries(messages, "edited", "messages"),
        messages_deleted=_entries(messages, "deleted", "messages"),
        pages_created=_entries(pages, "created", "pages"),
        pages_edited=_entries(pages, "edited", "pages"),
        pages_deleted=_entries(pages, "deleted", "pages"),
        fragments_created=_entries(fragments, "created", "fragments"),
    )
    for name, changed_at in _entries(profile, "edited", "profile").items():
        column = _PROFILE_ALIASES.get(name, name)
        if column not in PROFILE_COLUMNS:
            raise _bad(f"profile.edited: unknown field {name!r}")
        changes.profile_edited[column] = changed_at
    _refuse_overlap(
        "messages", created=changes.messages_created, edited=changes.messages_edited,
        deleted=changes.messages_deleted,
    )
    _refuse_overlap(
        "pages", created=changes.pages_created, edited=changes.pages_edited,
        deleted=changes.pages_deleted,
    )
    return changes


def has_changes_file(names: Iterable[str]) -> bool:
    """ZIP のエントリ名に changes.json があるか。"""
    return CHANGES_NAME in names


def read_changes(reader: Any, contents: Set[str]) -> Optional[ReturnChanges]:
    """パックの changes.json を読む。無ければ None (§5 の規則のまま動く本体のパック)。

    manifest の contents と食い違うパックは ValueError — Lite で消した記録を、
    食い違いのせいで黙って落とさないため。
    """
    present = has_changes_file(reader.names)
    listed = CHANGES_FOLDER in contents
    if not present and not listed:
        return None
    if not listed:
        raise _bad(f"the file is in the pack, but manifest.contents has no {CHANGES_FOLDER!r}")
    if not present:
        raise _bad(f"manifest.contents has {CHANGES_FOLDER!r}, but the file is missing")
    return parse_changes(reader.load_json(CHANGES_NAME))


def check_against_pack(
    changes: ReturnChanges,
    *,
    message_rows: Iterable[Mapping[str, Any]],
    page_rows: Iterable[Mapping[str, Any]],
    fragment_rows: Iterable[Mapping[str, Any]],
    profile_ai: Optional[Mapping[str, Any]],
) -> None:
    """changes.json の id がパックの行と合っているかを検査する (書き込み前)。

    Lite の値を読む編集と、足す行 (created) の id は ZIP に無ければ未知の id として断る。
    削除の id は ZIP に無いのが普通なので検査しない。行は流し読みで、覚えるのは
    changes.json の id だけ。
    """
    def row_id(row: Mapping[str, Any]) -> Optional[str]:
        value = row.get("id")
        return value if isinstance(value, str) else None

    missing_messages = set(changes.messages_created) | set(changes.messages_edited)
    for row in message_rows:
        missing_messages.discard(row_id(row))
    if missing_messages:
        raise _bad(f"message {sorted(missing_messages)[0]!r} is not in chatlog/messages.json")

    missing_pages = set(changes.pages_created) | set(changes.pages_edited)
    for row in page_rows:
        page_id = row_id(row)
        missing_pages.discard(page_id)
        if page_id in changes.pages_edited and decode_value(row.get("is_deleted")):
            raise _bad(f"page {page_id!r} is in pages.edited, but the pack has it as deleted")
    if missing_pages:
        raise _bad(f"page {sorted(missing_pages)[0]!r} is not in knowledge/pages.json")

    missing_fragments = set(changes.fragments_created)
    for row in fragment_rows:
        missing_fragments.discard(row_id(row))
    if missing_fragments:
        raise _bad(f"fragment {sorted(missing_fragments)[0]!r} is not in {FRAGMENTS_TABLE}.json")

    if changes.profile_edited:
        columns = set(profile_ai or ())
        for column in changes.profile_edited:
            if column not in columns:
                raise _bad(f"profile.edited has {column!r}, but profile/profile.json has no value for it")


# ---------------------------------------------------------------------------
# いま続いているスレッド
# ---------------------------------------------------------------------------

def active_suffix(state: Any, persona_ids: Iterable[str]) -> Optional[str]:
    """active_state.json の中身からスレッドの後半を取り出す。

    読む鍵の順は saiverse_memory/adapter.py の _active_persona_suffix と同じ。ファイルが
    持つのは後半だけ (set_active_thread) だが、古いデータは全体の ID を持つことがあるので、
    ペルソナ ID の前半が付いていれば外す。
    """
    candidate: Optional[str] = None
    if isinstance(state, dict):
        for key in ("active_thread_id", "thread_id", "active_thread"):
            value = state.get(key)
            if isinstance(value, str) and value.strip():
                candidate = value.strip()
                break
    elif isinstance(state, str) and state.strip():
        candidate = state.strip()
    if candidate is None:
        return None
    for persona_id in persona_ids:
        prefix = f"{persona_id}:"
        if candidate.startswith(prefix) and len(candidate) > len(prefix):
            return candidate[len(prefix):]
    return candidate


def read_body_active(persona_dir: Path, persona_id: str) -> str:
    """取り込み先のいま続いているスレッドの後半 (無ければ DEFAULT_THREAD_SUFFIX)。"""
    path = Path(persona_dir) / ACTIVE_STATE_FILENAME
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return DEFAULT_THREAD_SUFFIX
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        LOGGER.warning("[persona_pack] could not read %s: %s", path, exc)
        return DEFAULT_THREAD_SUFFIX
    return active_suffix(state, (persona_id,)) or DEFAULT_THREAD_SUFFIX


def write_active_thread(persona_dir: Path, suffix: str) -> None:
    """active_state.json の active_thread_id だけを書き換える。

    adapter.set_active_thread はファイルを丸ごと書き直し、他の鍵 (pulse_scoped_parent) を
    落とすので使わない。adapter は読むたびにこのファイルを開くので、読み込み済みの
    ペルソナにも次の読みから届く。
    """
    path = Path(persona_dir) / ACTIVE_STATE_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        LOGGER.warning("[persona_pack] %s was broken; writing a new one", path)
        data = {}
    if not isinstance(data, dict):
        data = {}
    data["active_thread_id"] = suffix
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# 本体の値と指紋
# ---------------------------------------------------------------------------

def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _digest(*values: Any) -> str:
    text = json.dumps(values, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


_ABSENT = "absent"


def _body_message(conn: sqlite3.Connection, message_id: str) -> Optional[Tuple[Any, ...]]:
    return conn.execute(
        "SELECT content, created_at, role, thread_id FROM messages WHERE id = ?", (message_id,)
    ).fetchone()


def _body_page(conn: sqlite3.Connection, page_id: str) -> Optional[Tuple[Any, ...]]:
    if not _has_table(conn, "memopedia_pages"):
        return None
    return conn.execute(
        "SELECT title, summary, content, updated_at, is_deleted, category, is_trunk "
        "FROM memopedia_pages WHERE id = ?",
        (page_id,),
    ).fetchone()


def _message_fingerprint(row: Optional[Tuple[Any, ...]]) -> str:
    """_body_message の行全部 (本文・作成日時・role・thread_id)。"""
    return _ABSENT if row is None else _digest(*row)


def _page_fingerprint(row: Optional[Tuple[Any, ...]]) -> str:
    """題名・概要・本文・更新日時・削除状態。画面に出す本体側の値。"""
    return _ABSENT if row is None else _digest(row[0], row[1], row[2], row[3], bool(row[4]))


def _value_fingerprint(value: Any) -> str:
    return _digest(value)


# ---------------------------------------------------------------------------
# 一覧の計算
# ---------------------------------------------------------------------------

@dataclass
class ReviewItem:
    """一覧の 1 件。``payload`` は画面へ返す JSON (本文の全文を含む)。"""

    key: str
    kind: str
    change: str
    fingerprint: str
    payload: Dict[str, Any]


@dataclass
class ReturnReview:
    """見比べの結果。

    ``warnings`` は一覧に出さなかった項目の知らせ。``resume`` は、未完了の新規取り込みの
    再開になるので一覧が空であること (importer._ImportJob.resuming)。
    """

    items: List[ReviewItem]
    warnings: List[str]
    resume: bool = False


def review_side(
    *,
    exists: bool = True,
    deleted: bool = False,
    title: Any = None,
    summary: Any = None,
    content: Any = None,
    updated_at: Any = None,
) -> Dict[str, Any]:
    """本体か Lite の片側の値 (画面の 1 列)。"""
    return {
        "exists": exists, "deleted": deleted, "title": title, "summary": summary,
        "content": content, "updated_at": updated_at,
    }


def _is_listed_page(row: Mapping[str, Any]) -> bool:
    """一覧に出せるページか。Chronicle は §5 の規則のまま、根のページは Lite の変更の対象外。"""
    if row.get("is_trunk") or str(row.get("id") or "").startswith("root_"):
        return False
    return row.get("category") != "chronicle"


def _review_message(
    message_id: str,
    change: str,
    changed_at: Any,
    zip_row: Optional[Mapping[str, Any]],
    body: Optional[Tuple[Any, ...]],
) -> Optional[ReviewItem]:
    if change == CHANGE_DELETED:
        if body is None:
            return None
        lite = review_side(exists=False, updated_at=changed_at)
    else:
        if zip_row is None:
            raise _bad(f"message {message_id!r} is not in chatlog/messages.json")
        content = decode_value(zip_row.get("content"))
        if body is not None and body[0] == content:
            return None
        created = iso_to_epoch(zip_row.get("created_at"))
        lite = review_side(content=content, updated_at=changed_at if changed_at is not None else created)
    if body is None:
        body_side = review_side(exists=False)
        role, thread_id = (decode_value((zip_row or {}).get("role")), (zip_row or {}).get("thread_id"))
        created_at = iso_to_epoch((zip_row or {}).get("created_at"))
    else:
        body_side = review_side(content=body[0], updated_at=body[1])
        created_at, role, thread_id = body[1], body[2], body[3]
    key = f"{KIND_MESSAGE}:{message_id}"
    return ReviewItem(
        key=key, kind=KIND_MESSAGE, change=change, fingerprint=_message_fingerprint(body),
        payload={
            "key": key, "kind": KIND_MESSAGE, "change": change, "role": role,
            "thread_id": thread_id, "created_at": created_at,
            "body": body_side, "lite": lite,
        },
    )


def _review_page(
    page_id: str,
    change: str,
    changed_at: Any,
    zip_row: Optional[Mapping[str, Any]],
    body: Optional[Tuple[Any, ...]],
) -> Optional[ReviewItem]:
    if change == CHANGE_DELETED:
        if body is None or body[4]:
            return None
        lite = review_side(exists=False, updated_at=changed_at)
        title, category = body[0], body[5]
    else:
        if zip_row is None:
            raise _bad(f"page {page_id!r} is not in knowledge/pages.json")
        values = (
            decode_value(zip_row.get("title")), decode_value(zip_row.get("summary")),
            decode_value(zip_row.get("content")),
        )
        if body is not None and not body[4] and tuple(body[:3]) == values:
            return None
        updated = iso_to_epoch(zip_row.get("updated_at"))
        lite = review_side(
            title=values[0], summary=values[1], content=values[2],
            updated_at=changed_at if changed_at is not None else updated,
        )
        title, category = values[0], zip_row.get("category")
    if body is None:
        body_side = review_side(exists=False)
    else:
        body_side = review_side(
            deleted=bool(body[4]), title=body[0], summary=body[1], content=body[2], updated_at=body[3],
        )
    key = f"{KIND_PAGE}:{page_id}"
    return ReviewItem(
        key=key, kind=KIND_PAGE, change=change, fingerprint=_page_fingerprint(body),
        payload={
            "key": key, "kind": KIND_PAGE, "change": change, "title": title, "category": category,
            "body": body_side, "lite": lite,
        },
    )


def review_profile(
    changes: ReturnChanges,
    zip_profile: Optional[Mapping[str, Any]],
    body_profile: Mapping[str, Any],
) -> List[ReviewItem]:
    """一覧のプロフィールの項目。"""
    items: List[ReviewItem] = []
    for column, changed_at in changes.profile_edited.items():
        lite_value = decode_value((zip_profile or {}).get(column))
        body_value = body_profile.get(column)
        if lite_value == body_value:
            continue
        key = f"{KIND_PROFILE}:{column}"
        items.append(ReviewItem(
            key=key, kind=KIND_PROFILE, change=CHANGE_EDITED,
            fingerprint=_value_fingerprint(body_value),
            payload={
                "key": key, "kind": KIND_PROFILE, "change": CHANGE_EDITED, "field": column,
                "body": review_side(content=body_value),
                "lite": review_side(content=lite_value, updated_at=changed_at),
            },
        ))
    return items


def build_review(
    changes: ReturnChanges,
    *,
    memory: sqlite3.Connection,
    zip_message: Callable[[str], Optional[Dict[str, Any]]],
    zip_page: Callable[[str], Optional[Dict[str, Any]]],
    zip_profile: Optional[Mapping[str, Any]],
    body_profile: Mapping[str, Any],
    zip_active: Optional[str],
    body_active: str,
    thread_tail: Callable[[str, str], Tuple[Dict[str, Any], Dict[str, Any]]],
) -> ReturnReview:
    """一覧を計算する。本体の値は読むだけで、どこにも書かない。

    ``zip_message`` / ``zip_page`` は id からパックの行を返す。``thread_tail`` は
    (本体の値, Lite の値) のスレッドの最後の発言と時刻を片側の値にして返す。

    並びはプロフィール・スレッド・ページ・発言の順で、発言は時刻の順。
    """
    warnings: List[str] = []
    items = review_profile(changes, zip_profile, body_profile)

    if zip_active is not None and zip_active != body_active:
        body_side, lite_side = thread_tail(body_active, zip_active)
        items.append(ReviewItem(
            key=KIND_ACTIVE_THREAD, kind=KIND_ACTIVE_THREAD, change=CHANGE_EDITED,
            fingerprint=_value_fingerprint(body_active),
            payload={
                "key": KIND_ACTIVE_THREAD, "kind": KIND_ACTIVE_THREAD, "change": CHANGE_EDITED,
                "body": body_side, "lite": lite_side,
            },
        ))

    pages: List[ReviewItem] = []
    for ids, change in ((changes.pages_edited, CHANGE_EDITED), (changes.pages_deleted, CHANGE_DELETED)):
        for page_id, changed_at in ids.items():
            zip_row = zip_page(page_id) if change == CHANGE_EDITED else None
            body = _body_page(memory, page_id)
            shape = dict(zip_row) if zip_row is not None else None
            if shape is None and body is not None:
                shape = {"id": page_id, "category": body[5], "is_trunk": body[6]}
            if shape is not None and not _is_listed_page(shape):
                warnings.append(f"page {page_id}: a Chronicle or root page is not compared; {change} ignored")
                continue
            item = _review_page(page_id, change, changed_at, zip_row, body)
            if item is not None:
                pages.append(item)
    items.extend(sorted(pages, key=lambda i: str(i.payload.get("title") or "")))

    messages: List[ReviewItem] = []
    for ids, change in (
        (changes.messages_edited, CHANGE_EDITED), (changes.messages_deleted, CHANGE_DELETED),
    ):
        for message_id, changed_at in ids.items():
            zip_row = zip_message(message_id) if change == CHANGE_EDITED else None
            item = _review_message(message_id, change, changed_at, zip_row, _body_message(memory, message_id))
            if item is not None:
                messages.append(item)
    items.extend(sorted(messages, key=lambda i: _sort_time(i.payload.get("created_at"))))
    return ReturnReview(items, warnings)


def _sort_time(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


# ---------------------------------------------------------------------------
# 一覧の保存と読み出し
# ---------------------------------------------------------------------------

def write_review(path: Path, review: ReturnReview) -> None:
    """一覧を見比べのフォルダの SQLite へ書く。"""
    conn = sqlite3.connect(str(path))
    try:
        with conn:
            for statement in _REVIEW_DDL:
                conn.execute(statement)
            conn.executemany(
                "INSERT INTO items(idx, item_key, kind, change, fingerprint, item) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    (i, item.key, item.kind, item.change, item.fingerprint,
                     json.dumps(item.payload, ensure_ascii=False))
                    for i, item in enumerate(review.items)
                ),
            )
            conn.executemany(
                "INSERT OR REPLACE INTO meta(name, value) VALUES (?, ?)",
                (
                    ("warnings", json.dumps(review.warnings, ensure_ascii=False)),
                    ("resume", json.dumps(review.resume)),
                ),
            )
    finally:
        conn.close()


def read_review_summary(path: Path) -> Dict[str, Any]:
    """一覧の件数の内訳 (本文を読まない)。"""
    conn = sqlite3.connect(str(path))
    try:
        counts = {kind: 0 for kind in KINDS}
        message_deletions = 0
        for kind, change, n in conn.execute(
            "SELECT kind, change, COUNT(*) FROM items GROUP BY kind, change"
        ):
            counts[kind] = counts.get(kind, 0) + n
            if kind == KIND_MESSAGE and change == CHANGE_DELETED:
                message_deletions += n
        meta = dict(conn.execute("SELECT name, value FROM meta").fetchall())
        return {
            "total": sum(counts.values()),
            "counts": counts,
            "message_deletions": message_deletions,
            "warnings": json.loads(meta.get("warnings", "[]")),
            "resume": json.loads(meta.get("resume", "false")),
        }
    finally:
        conn.close()


def _shorten(side: Dict[str, Any]) -> Dict[str, Any]:
    content = side.get("content")
    if not isinstance(content, str) or len(content) <= PREVIEW_CHARS:
        return {**side, "truncated": False}
    return {**side, "content": content[:PREVIEW_CHARS], "truncated": True}


def read_review_items(path: Path, offset: int, limit: int, *, full: bool) -> List[Dict[str, Any]]:
    """一覧の offset 件目から limit 件を返す。full でなければ長い本文は PREVIEW_CHARS で切る。"""
    conn = sqlite3.connect(str(path))
    try:
        rows = conn.execute(
            "SELECT item FROM items WHERE idx >= ? ORDER BY idx LIMIT ?", (offset, limit)
        ).fetchall()
    finally:
        conn.close()
    items: List[Dict[str, Any]] = []
    for (text,) in rows:
        item = json.loads(text)
        if full:
            item["body"]["truncated"] = False
            item["lite"]["truncated"] = False
        else:
            item["body"] = _shorten(item["body"])
            item["lite"] = _shorten(item["lite"])
        items.append(item)
    return items


def read_review_fingerprints(path: Path) -> Dict[str, str]:
    """{項目のキー: 見比べた時点の指紋}。"""
    conn = sqlite3.connect(str(path))
    try:
        return dict(conn.execute("SELECT item_key, fingerprint FROM items ORDER BY idx").fetchall())
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 選択
# ---------------------------------------------------------------------------

@dataclass
class ReturnSelection:
    """適用に渡す選択。``expected`` は見比べた時点の指紋、``decisions`` は項目ごとの選択。"""

    expected: Dict[str, str]
    decisions: Dict[str, str]


def resolve_choices(
    keys: Iterable[str], bulk: Optional[str], choices: Mapping[str, str],
) -> Dict[str, str]:
    """一括の選択と一件ごとの選択を、全項目の選択にする。

    一件ごとの選択が一括に勝つ。一覧に無いキー・選べない値・選択の無い項目があれば
    ValueError — 既定の選択を置かない約束なので、足りない分を片方に倒さない。
    """
    keys = list(keys)
    if bulk is not None and bulk not in CHOICES:
        raise ValueError(f"bulk must be one of {CHOICES}, got {bulk!r}")
    known = set(keys)
    decided: Dict[str, str] = {}
    for key, choice in choices.items():
        if key not in known:
            raise ValueError(f"{key!r} is not in the compared list")
        if choice not in CHOICES:
            raise ValueError(f"{key!r}: the choice must be one of {CHOICES}, got {choice!r}")
        decided[key] = choice
    if bulk is not None:
        for key in keys:
            decided.setdefault(key, bulk)
    undecided = len(known - set(decided))
    if undecided:
        raise ValueError(f"{undecided} item(s) have no choice yet; every item needs one")
    return decided


def _stale_keys(current: Mapping[str, str], expected: Mapping[str, str]) -> List[str]:
    """片方にしか無いキーと、指紋の違うキー。"""
    return sorted(
        key for key in current.keys() | expected.keys() if current.get(key) != expected.get(key)
    )


def check_selection(items: Sequence[ReviewItem], selection: ReturnSelection) -> None:
    """計算し直した一覧 ``items`` と、渡された選択を照らす。

    一覧の全キーに選択があり、余分なキーが無く、指紋が一致するときだけ戻る。空の選択が
    通るのは ``items`` が空のときだけ。``items`` に無い行の変化では止まらない (intent)。

    Raises:
        ValueError: decisions のキーが expected と違う、または選べない値がある。
        ReturnMergeStale: expected が今の一覧と違う。
    """
    unmatched = selection.expected.keys() ^ selection.decisions.keys()
    if unmatched:
        raise ValueError(f"persona pack: {len(unmatched)} compared item(s) have no matching choice")
    for key, choice in selection.decisions.items():
        if choice not in CHOICES:
            raise ValueError(f"persona pack: {key!r}: the choice must be one of {CHOICES}, got {choice!r}")
    stale = _stale_keys({item.key: item.fingerprint for item in items}, selection.expected)
    if stale:
        raise ReturnMergeStale(stale)


def check_profile(items: Sequence[ReviewItem], selection: ReturnSelection) -> None:
    """check_selection のプロフィールの項目だけ。``items`` は review_profile の結果。

    memory.db へ書いた後で呼ぶ。そこから先は発言とページの一覧を計算し直せない。

    Raises:
        ReturnMergeStale: プロフィールの項目が expected と違う。
    """
    prefix = f"{KIND_PROFILE}:"
    expected = {key: value for key, value in selection.expected.items() if key.startswith(prefix)}
    stale = _stale_keys({item.key: item.fingerprint for item in items}, expected)
    if stale:
        raise ReturnMergeStale(stale)


class ReturnPlan:
    """changes.json と選択から、_ImportJob が行ごとに引く判定。"""

    def __init__(self, changes: ReturnChanges, selection: ReturnSelection) -> None:
        self.changes = changes
        self.selection = selection
        # 選ばれたら active_state.json へ書くスレッドの後半 (_ImportJob が ZIP から読む)
        self.active_suffix: Optional[str] = None
        self.fragments_added = 0
        self.fragments_without_page = 0
        self.messages_deleted = 0
        self.pages_deleted = 0
        self.backup_path: Optional[str] = None

    def choice(self, kind: str, ident: str) -> Optional[str]:
        return self.selection.decisions.get(f"{kind}:{ident}")

    def _takes_lite(self, kind: str, ident: str) -> bool:
        return self.choice(kind, ident) == CHOICE_LITE

    def accepts_new_message(self, message_id: str) -> bool:
        """本体に無い発言を足すか。created か、編集の一覧で戻すと選ばれたものだけ。"""
        changes = self.changes
        if message_id in changes.messages_created:
            return True
        return message_id in changes.messages_edited and self._takes_lite(KIND_MESSAGE, message_id)

    def accepts_new_page(self, page_id: str) -> bool:
        changes = self.changes
        if page_id in changes.pages_created:
            return True
        return page_id in changes.pages_edited and self._takes_lite(KIND_PAGE, page_id)

    def page_takes_lite(self, page_id: str) -> bool:
        return page_id in self.changes.pages_edited and self._takes_lite(KIND_PAGE, page_id)

    def edited_messages(self) -> List[str]:
        return [m for m in self.changes.messages_edited if self._takes_lite(KIND_MESSAGE, m)]

    def deleted_messages(self) -> List[str]:
        return [m for m in self.changes.messages_deleted if self._takes_lite(KIND_MESSAGE, m)]

    def deleted_pages(self) -> List[str]:
        return [p for p in self.changes.pages_deleted if self._takes_lite(KIND_PAGE, p)]

    @property
    def profile_columns(self) -> Set[str]:
        return {c for c in self.changes.profile_edited if self._takes_lite(KIND_PROFILE, c)}

    @property
    def takes_lite_thread(self) -> bool:
        return self.selection.decisions.get(KIND_ACTIVE_THREAD) == CHOICE_LITE

    def summary(self) -> Dict[str, Any]:
        """結果の画面へ返す選択の内訳。"""
        chosen: Dict[str, Dict[str, int]] = {}
        for key, choice in self.selection.decisions.items():
            kind = key.partition(":")[0]
            chosen.setdefault(kind, {CHOICE_LITE: 0, CHOICE_BODY: 0})[choice] += 1
        return {
            "choices": chosen,
            "messages_deleted": self.messages_deleted,
            "pages_deleted": self.pages_deleted,
            "fragments_added": self.fragments_added,
            "fragments_without_page": self.fragments_without_page,
            "backup_path": self.backup_path,
        }


# ---------------------------------------------------------------------------
# memory.db の複製
# ---------------------------------------------------------------------------

def open_read_only(path: Path) -> sqlite3.Connection:
    """DB を読み取り専用で開く。"""
    return sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=30)


def backup_memory_db(memory_db_path: Path, home: Path, persona_id: str) -> Path:
    """memory.db を ``<home>/backups/persona_pack_merge/<persona_id>/`` へ複製する。

    sqlite の backup を読み取り専用の接続で行う。適用は memory.db を書き込みで開く前に
    呼ぶので、複製は適用前の値になる。一時ファイルに書き切ってから置くので、途中で
    失敗した複製は名前を持たない。ペルソナごとに新しい BACKUP_KEEP 個を残す。

    Raises:
        ReturnMergeBackupError: 複製できなかった (適用しない)。
    """
    folder = Path(home).joinpath(*_BACKUP_DIR, persona_id)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    dest = folder / f"memory_{stamp}.db"
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        folder.mkdir(parents=True, exist_ok=True)
        source = open_read_only(memory_db_path)
        try:
            target = sqlite3.connect(str(tmp))
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
        os.replace(tmp, dest)
    except (OSError, sqlite3.Error) as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            LOGGER.warning("[persona_pack] could not remove %s", tmp, exc_info=True)
        raise ReturnMergeBackupError(f"the memory could not be backed up: {exc}") from exc
    for old in sorted(folder.glob("memory_*.db"), reverse=True)[BACKUP_KEEP:]:
        try:
            old.unlink()
        except OSError:
            LOGGER.warning("[persona_pack] could not remove the old backup %s", old, exc_info=True)
    return dest
