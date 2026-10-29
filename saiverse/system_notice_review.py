"""確認待ちの行 (``SYSTEM_NOTICE_REVIEW_KEY`` を持つ記憶の行) を所有者が画面で確定する。

docs/intent/system_notice_delivery.md「追補 (2026-10-29)」の「確認待ちの行の確定
(利用者の画面)」。起動時の移行 (database/system_notice_migration.py) が、
システム通知か発話かを決められずに確認待ちの印を付けた行を、全ペルソナの記憶 DB
から集めて型ごとに束ね、所有者が選んだ操作で確定する。

- 操作は 4 つ (:data:`ACTIONS`)。行ごとに、その行の作りで嘘にならない操作だけを
  「選べる操作」として返し (:func:`allowed_actions`)、選べない操作は書き換えずに
  拒否する。ユーザーやペルソナの行が添付 (``media`` / ``images`` / ``documents``)
  を持つときは「正式に変換」を出さない — 行ごと role=system にすると、発話者の
  添付が機構の通知の持ち物になる。
- 一覧は各行の全文・添付・ハッシュ (``version`` — :func:`row_version`) を返す。
  確定の操作は行ごとに画面が見たハッシュを受け取り、書き込みの取引の中で今の行から
  作ったハッシュと比べて、違えば
  書き換えずに ``changed`` で拒否する (画面が見た内容と違う行に所有者の選択を
  当てない)。
- 確定した行は ``SYSTEM_NOTICE_REVIEW_KEY`` を消し、``SYSTEM_NOTICE_REVIEWED_KEY``
  に選んだ操作を書く (削除は行ごと消えるので書かない)。移行はこの鍵を持つ行を
  飛ばす。「通常のユーザー発話に変換」は、実会話 (storage.py の
  ``_conversation_exclusion``) から外す機構のタグと line_role もその行から外し、
  外したものを :data:`REVIEW_CLEARED_KEY` に残す。
- 削除は、建物の履歴から転記された行 (``building_msg_ref`` を持つ行) なら、消す前に
  建物の側の取り込み済みの印 (``ingested_by``) を確実にする — 印の書き込みに失敗して
  宙に浮いた転記は、次の転記で記憶の側の ``building_msg_ref`` を照会して重複を
  避けるので、照会先の行を消すと同じ発話が取り込み直されて復活する。
- 書き込みは記憶ブラウザの編集 API (api/routes/people/memory.py) と同じ
  ``get_adapter`` 経由のアダプタの書き込みの取引に乗せる
  (``SAIMemoryAdapter.rewrite_message``)。
- 入口は状態に応じて出す: 確認待ちの行が 1 件以上あるか、記憶 DB を読めなかった
  ペルソナがある間は ``manager.startup_alerts`` に :data:`REVIEW_ALERT_ID` の警告を
  置き、確定の操作のたびに数え直して、どちらも無くなったら外す
  (:func:`refresh_review_alert`)。

本文が ``<system>`` の包みで始まるか・剥がせるかの判定は、移行と同じ関数
(``starts_with_system_tag`` / ``strip_system_tags``) を使う。対象は移行が
確認待ちにした行だけで、常設の読み出し・提示の経路はこの判定を使わない。
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, ContextManager, Dict, Iterable, List, Optional, Sequence, Tuple

from database.system_notice_migration import (
    SYSTEM_CLOSE,
    SYSTEM_OPEN,
    starts_with_system_tag,
    strip_system_tags,
)
from sai_memory.memory.storage import (
    CHRONICLE_EXCLUDED_LINE_ROLES,
    MECHANISM_TAGS,
    SYSTEM_NOTICE_KEY,
    SYSTEM_NOTICE_REVIEW_KEY,
    SYSTEM_NOTICE_REVIEWED_KEY,
    SYSTEM_NOTICE_REVIEWED_KEEP,
)

LOGGER = logging.getLogger(__name__)

ACTION_KEEP = SYSTEM_NOTICE_REVIEWED_KEEP  # "keep" — 行を変えず、確定の記録だけ書く
ACTION_SYSTEM = "system"  # role=system + 通知の印 (剥がせるならタグも剥がす)
ACTION_USER = "user"  # role=user のまま普通の発話に戻す (包みが本文全体なら剥がす)
ACTION_DELETE = "delete"  # 行を消す
ACTIONS: Tuple[str, ...] = (ACTION_KEEP, ACTION_SYSTEM, ACTION_USER, ACTION_DELETE)

#: 「正式に変換」した行の ``SYSTEM_NOTICE_KEY`` の値 (系統名)。どの系統が作った
#: 通知かは機械が決められなかった行なので、「所有者が通知だと確定した」ことだけを
#: 表す固定の一つにする。
OWNER_CONFIRMED_KIND = "owner_confirmed"

#: 「通常のユーザー発話に変換」で行から外した印の記録 (metadata の鍵)。値は
#: ``{"tags": [外した機構のタグ], "line_role": 元の line_role}`` のうち外したものだけ。
#: ``__`` で始まるので送り手からは書けない (api/routes/chat.py の ``_client_metadata``)。
REVIEW_CLEARED_KEY = "__system_notice_review_cleared__"

#: 発話へ戻した行の line_role (建物の履歴から転記した発話と同じ値 —
#: builtin_data/tools/get_building_messages.py の ``_stamp_layer0``)。
USER_LINE_ROLE = "main_line"

#: 確認待ちの行がある間、``manager.startup_alerts`` に置く警告の id。
REVIEW_ALERT_ID = "system_notice_review_pending"

#: 束ねる鍵に使う本文の書き出しの長さ。
HEAD_CHARS = 40

#: 発話者が付けた添付の置き場 (api/routes/chat.py の /chat/send が書く 3 つ)。
ATTACHMENT_FIELDS: Tuple[str, ...] = ("media", "images", "documents")
ATTACHMENT_KINDS: Tuple[str, ...] = ("image", "document", "audio", "video", "other")


class ActionNotAllowed(Exception):
    """その行では選べない操作が指定された (書き換えずに拒否する)。"""


class RowChanged(Exception):
    """画面が見た行のハッシュと今の行のハッシュが違う (書き換えずに拒否する)。"""


class BuildingRefUnresolved(Exception):
    """転記された行を消す前に、建物の側の取り込み済みの印を確実にできない。"""


# ---------------------------------------------------------------------------
# 行ごとの判定と書き換え
# ---------------------------------------------------------------------------


def _is_pending(metadata: Optional[Dict[str, Any]]) -> bool:
    return (
        isinstance(metadata, dict)
        and metadata.get(SYSTEM_NOTICE_REVIEW_KEY) is not None
    )


def _stripped_body(content: Any) -> Optional[str]:
    """``<system>`` の包みを剥がした本文 (中身の前後の空白は移行と同じく落とす)。

    包みで始まらない・剥がせない行は None。「正式に変換」で使う — 移行が通知へ
    変換した行と同じ形にそろえる。
    """
    if not starts_with_system_tag(content):
        return None
    return strip_system_tags(content)


def unwrap_system_tags_exactly(content: Any) -> Optional[str]:
    """包みの開き ``<system>`` と閉じ ``</system>`` だけを取り除いた本文。

    包みが本文全体を囲んでいる行 (:func:`_stripped_body` が None でない行) だけが
    対象で、それ以外は None。包みの内側の本文は前後の空白・改行も含めて一字も
    変えない (「通常のユーザー発話に変換」用 — 本人の発話として残す本文なので、
    移行の ``strip_system_tags`` のように内側を整えない)。閉じタグの後ろの空白も
    そのまま残す。
    """
    if _stripped_body(content) is None:
        return None
    text = str(content)
    # 剥がせる行は「開きで始まり、対応する閉じの後ろは空白だけ」なので、対応する
    # 閉じは最後の閉じタグ。
    close = text.rfind(SYSTEM_CLOSE)
    return text[len(SYSTEM_OPEN):close] + text[close + len(SYSTEM_CLOSE):]


def attachment_items(metadata: Any) -> List[Dict[str, str]]:
    """行が持つ添付の項目 (``kind`` と、ファイルを指す ``ref``)。

    ``kind`` は ``image`` / ``document`` / ``audio`` / ``video`` / ``other``。
    ``media`` の項目は ``type`` か ``mime_type`` の頭で決める
    (saiverse/media_utils.py の数え方と同じ)。ファイルを指す欄が無い項目も
    数える — 行ごと role を変えれば、その項目も一緒に持ち主が変わるため。
    """
    items: List[Dict[str, str]] = []
    if not isinstance(metadata, dict):
        return items
    for field_name in ATTACHMENT_FIELDS:
        values = metadata.get(field_name)
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            if field_name == "images":
                kind = "image"
            elif field_name == "documents":
                kind = "document"
            else:
                kind = str(item.get("type") or "").lower()
                if not kind:
                    mime = str(item.get("mime_type") or "").lower()
                    kind = mime.split("/", 1)[0] if "/" in mime else ""
            if kind not in ATTACHMENT_KINDS:
                kind = "other"
            ref = item.get("uri") or item.get("path") or item.get("name") or ""
            items.append({"kind": kind, "ref": str(ref)})
    return items


def attachment_counts(items: Iterable[Dict[str, str]]) -> Dict[str, int]:
    """:func:`attachment_items` を種類ごとに数えたもの (0 件の種類は入れない)。"""
    counts: Dict[str, int] = {}
    for item in items:
        counts[item["kind"]] = counts.get(item["kind"], 0) + 1
    return counts


def row_version(role: Any, content: Any, metadata: Optional[Dict[str, Any]]) -> str:
    """行の role・本文・metadata から作るハッシュ (一覧の ``version``)。

    一覧と確定の操作が同じ関数で作る — metadata は記憶 DB の JSON を辞書として
    読んだもの (読めない行は None) を、鍵の順をそろえて直列化する。
    """
    payload = json.dumps(
        {"role": role, "content": content, "metadata": metadata},
        ensure_ascii=False, sort_keys=True, default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def allowed_actions(role: Any, content: Any, metadata: Optional[Dict[str, Any]]) -> List[str]:
    """その行で選べる操作。確認待ちの印が無い行は空。

    「正式に変換」は、role=system の行か、添付を持たない行にだけ出す。role=user /
    role=assistant の行の添付は発話者のもので、行ごと role=system にすると機構の
    通知の添付になる (docs/intent/system_notice_delivery.md 不変条件 7)。
    「通常のユーザー発話に変換」は role=user の行にだけ出す — role=system や
    role=assistant の行に出すと、機構やペルソナの文がユーザーの名義になる。
    包みが本文全体を囲んでいない role=user の行 (ユーザー本人が本文の途中に
    ``<system>`` を打った発言など) にも出す。そのときは本文を一字も変えずに
    普通の発話へ戻す — 本人が打ったとおりの本文が、本人の発話の正しい記録だから。
    """
    if not _is_pending(metadata):
        return []
    role_s = role.lower() if isinstance(role, str) else ""
    actions = [ACTION_KEEP]
    if role_s == "system" or not attachment_items(metadata):
        actions.append(ACTION_SYSTEM)
    if role_s == "user":
        actions.append(ACTION_USER)
    actions.append(ACTION_DELETE)
    return actions


def rewrite_for_action(
    action: str,
    row: Dict[str, Any],
    *,
    version: Optional[str] = None,
    release_building_ref: Optional[Callable[[str], None]] = None,
) -> Any:
    """``action`` を当てた後の行 (``SAIMemoryAdapter.rewrite_message`` の ``rewrite``)。

    ``row`` は ``rewrite_message`` が渡す辞書 (role / content / metadata /
    line_role)。``version`` を渡したときは、今の行のハッシュ (:func:`row_version`) と
    違えば :class:`RowChanged` を投げる。選べない操作なら :class:`ActionNotAllowed`
    を投げる (どちらも書き換えない)。

    削除は ``SAIMemoryAdapter.MESSAGE_DELETE`` を返す。行が建物の履歴からの転記
    (``building_msg_ref``) なら、先に ``release_building_ref(ref)`` で建物の側の
    取り込み済みの印を確実にする。渡されていない・失敗したら例外 (書き換えない)。
    """
    from saiverse_memory.adapter import SAIMemoryAdapter

    role = row.get("role")
    content = row.get("content")
    metadata = row.get("metadata")
    if version is not None and version != row_version(role, content, metadata):
        raise RowChanged(action)
    if action not in allowed_actions(role, content, metadata):
        raise ActionNotAllowed(action)
    assert metadata is not None  # allowed_actions が確認待ちの印を確かめている
    if action == ACTION_DELETE:
        ref = metadata.get(SAIMemoryAdapter.BUILDING_MSG_REF_META_KEY)
        if ref:
            if release_building_ref is None:
                raise BuildingRefUnresolved(str(ref))
            release_building_ref(str(ref))
        return SAIMemoryAdapter.MESSAGE_DELETE
    new_meta = dict(metadata)
    del new_meta[SYSTEM_NOTICE_REVIEW_KEY]
    new_meta[SYSTEM_NOTICE_REVIEWED_KEY] = action
    if action == ACTION_KEEP:
        return {"role": role, "content": content, "metadata": new_meta}
    if action == ACTION_SYSTEM:
        body = _stripped_body(content)
        new_meta[SYSTEM_NOTICE_KEY] = OWNER_CONFIRMED_KIND
        return {
            "role": "system",
            "content": body if body is not None else content,
            "metadata": new_meta,
        }
    # ACTION_USER: allowed_actions が role=user を確かめている。包みが本文全体を
    # 囲んでいれば開きと閉じだけを外し、そうでなければ本人が打ったとおりの本文を
    # 残す。実会話から外す印 (機構のタグ・line_role) も外し、外したものを残す。
    unwrapped = unwrap_system_tags_exactly(content)
    result: Dict[str, Any] = {
        "role": role,
        "content": unwrapped if unwrapped is not None else content,
        "metadata": new_meta,
    }
    cleared: Dict[str, Any] = {}
    tags = new_meta.get("tags")
    if isinstance(tags, list):
        removed = [t for t in tags if t in MECHANISM_TAGS]
        if removed:
            new_meta["tags"] = [t for t in tags if t not in MECHANISM_TAGS]
            cleared["tags"] = removed
    line_role = row.get("line_role")
    if line_role in CHRONICLE_EXCLUDED_LINE_ROLES:
        result["line_role"] = USER_LINE_ROLE
        cleared["line_role"] = line_role
    if cleared:
        new_meta[REVIEW_CLEARED_KEY] = cleared
    return result


# ---------------------------------------------------------------------------
# 一覧
# ---------------------------------------------------------------------------


@dataclass
class ReviewRow:
    persona_id: str
    persona_name: str
    message_id: str
    role: str
    tags: List[str]
    #: 保存された本文の全文 (剥がしも切り詰めもしない)
    content: str
    reason: str
    created_at: Optional[int]
    actions: List[str]
    #: 確定の操作に渡す行のハッシュ (:func:`row_version`)
    version: str = ""
    attachments: List[Dict[str, str]] = field(default_factory=list)
    #: 束ねる鍵に使う本文の書き出し
    head: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "persona_id": self.persona_id,
            "persona_name": self.persona_name,
            "message_id": self.message_id,
            "role": self.role,
            "tags": self.tags,
            "content": self.content,
            "reason": self.reason,
            "created_at": self.created_at,
            "actions": self.actions,
            "version": self.version,
            "attachments": self.attachments,
            "attachment_counts": attachment_counts(self.attachments),
        }


@dataclass
class ReviewGroup:
    group_id: str
    role: str
    head: str
    tags: List[str]
    reason: str
    actions: List[str]
    rows: List[ReviewRow] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        personas: Dict[str, Dict[str, Any]] = {}
        for row in self.rows:
            entry = personas.setdefault(row.persona_id, {
                "persona_id": row.persona_id,
                "persona_name": row.persona_name,
                "count": 0,
            })
            entry["count"] += 1
        return {
            "group_id": self.group_id,
            "role": self.role,
            "head": self.head,
            "tags": self.tags,
            "reason": self.reason,
            "actions": self.actions,
            "count": len(self.rows),
            "personas": list(personas.values()),
            "rows": [r.to_dict() for r in self.rows],
        }


def _grouping_tags(tags: Sequence[str]) -> List[str]:
    """束ねる鍵に使うタグ (Pulse ごとに値が変わる ``pulse:`` を除く)。"""
    return sorted({t for t in tags if not t.startswith("pulse:")})


def _head(content: Any) -> str:
    """束ねる鍵に使う本文の書き出し (剥がせる行は剥がした本文の最初の行)。"""
    text = content if isinstance(content, str) else ""
    body = _stripped_body(text)
    if body is not None:
        text = body
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:HEAD_CHARS]
    return ""


def _read_pending_rows(db_path: Path) -> List[Tuple[Any, Any, Any, Any, Dict[str, Any]]]:
    """記憶 DB を読み取り専用で開き、確認待ちの印を持つ行を返す。

    ``metadata`` の列が無い古いスキーマには印が付いていない (移行が付けられない)
    ので空。
    """
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
        if not {"id", "role", "content", "metadata"} <= cols:
            return []
        order = "created_at, rowid" if "created_at" in cols else "rowid"
        created = "created_at" if "created_at" in cols else "NULL"
        raw_rows = conn.execute(
            f"SELECT id, role, content, {created}, metadata FROM messages "
            f"WHERE instr(metadata, ?) > 0 ORDER BY {order}",
            (SYSTEM_NOTICE_REVIEW_KEY,),
        ).fetchall()
    rows = []
    for row_id, role, content, created_at, raw_meta in raw_rows:
        try:
            metadata = json.loads(raw_meta)
        except (TypeError, ValueError):
            continue
        if _is_pending(metadata):
            rows.append((row_id, role, content, created_at, metadata))
    return rows


def collect_review_rows(
    persona_dbs: Iterable[Tuple[str, str, Path]],
) -> Tuple[List[ReviewRow], List[Dict[str, str]]]:
    """``(persona_id, persona_name, memory.db のパス)`` の各 DB から確認待ちの行を集める。

    読めなかった DB は ``errors`` (``persona_id`` / ``persona_name`` / ``error``) に
    入れて先へ進む。
    """
    rows: List[ReviewRow] = []
    errors: List[Dict[str, str]] = []
    for persona_id, persona_name, db_path in persona_dbs:
        try:
            pending = _read_pending_rows(db_path)
        except Exception as exc:  # noqa: BLE001 — 一つの DB の失敗で一覧全体を止めない
            LOGGER.warning(
                "[system-notice-review] failed to read %s: %s", db_path, exc, exc_info=True,
            )
            errors.append({
                "persona_id": persona_id, "persona_name": persona_name, "error": str(exc),
            })
            continue
        for row_id, role, content, created_at, metadata in pending:
            text = content if isinstance(content, str) else ""
            raw_tags = metadata.get("tags")
            tags = sorted(t for t in raw_tags if isinstance(t, str)) if isinstance(raw_tags, list) else []
            rows.append(ReviewRow(
                persona_id=persona_id,
                persona_name=persona_name,
                message_id=str(row_id),
                role=str(role or ""),
                tags=tags,
                content=text,
                reason=str(metadata.get(SYSTEM_NOTICE_REVIEW_KEY) or ""),
                created_at=int(created_at) if isinstance(created_at, (int, float)) else None,
                actions=allowed_actions(role, content, metadata),
                version=row_version(role, content, metadata),
                attachments=attachment_items(metadata),
                head=_head(text),
            ))
    return rows, errors


def group_review_rows(rows: Iterable[ReviewRow]) -> List[ReviewGroup]:
    """同じ型 (role・本文の書き出し・タグの組・残した理由・選べる操作) の行を束ねる。

    選べる操作を鍵に含めるので、束の中の行は全部同じ操作を選べる (束ごとの一括
    適用が、一部の行だけ拒否される形にならない)。添付の有無で「正式に変換」を
    選べるかが変わるので、添付のある行と無い行も別の束になる。件数の多い束から
    並べる。
    """
    groups: Dict[Tuple[Any, ...], ReviewGroup] = {}
    for row in rows:
        gtags = _grouping_tags(row.tags)
        key = (row.role.lower(), row.head, tuple(gtags), row.reason, tuple(row.actions))
        group = groups.get(key)
        if group is None:
            digest = hashlib.sha1(repr(key).encode("utf-8")).hexdigest()[:16]
            group = ReviewGroup(
                group_id=digest, role=row.role, head=row.head, tags=gtags,
                reason=row.reason, actions=list(row.actions),
            )
            groups[key] = group
        group.rows.append(row)
    return sorted(groups.values(), key=lambda g: (-len(g.rows), g.head))


def build_review_listing(persona_dbs: Iterable[Tuple[str, str, Path]]) -> Dict[str, Any]:
    """画面に返す一覧 (束と、読めなかった DB)。"""
    rows, errors = collect_review_rows(persona_dbs)
    groups = group_review_rows(rows)
    return {
        "total": len(rows),
        "groups": [g.to_dict() for g in groups],
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# 対象のペルソナ
# ---------------------------------------------------------------------------


def known_persona_dbs(manager: Any, personas_dir: Optional[Path] = None) -> List[Tuple[str, str, Path]]:
    """確定の操作が届くペルソナ (読み込み済みか本体 DB に登録がある) の記憶 DB。

    書き込みは ``get_adapter`` を通し、``get_adapter`` は読み込み済みでも本体 DB に
    登録も無いペルソナを拒む (記憶 DB だけが残った孤児)。確定できない行を数えると
    入口が消えなくなるので、一覧と件数も同じ範囲に揃える。
    """
    if personas_dir is None:
        from saiverse.data_paths import get_personas_dir

        personas_dir = get_personas_dir()
    if not personas_dir.is_dir():
        return []
    names: Dict[str, str] = {}
    for pid, persona in (getattr(manager, "personas", None) or {}).items():
        names[pid] = str(getattr(persona, "persona_name", "") or pid)
    session_factory = getattr(manager, "SessionLocal", None)
    if session_factory is not None:
        from database.models import AI

        db = session_factory()
        try:
            for aiid, ainame in db.query(AI.AIID, AI.AINAME).all():
                names.setdefault(aiid, str(ainame or aiid))
        finally:
            db.close()
    found: List[Tuple[str, str, Path]] = []
    for child in sorted(personas_dir.iterdir()):
        db_path = child / "memory.db"
        if child.is_dir() and child.name in names and db_path.is_file():
            found.append((child.name, names[child.name], db_path))
    return found


# ---------------------------------------------------------------------------
# 確定の適用
# ---------------------------------------------------------------------------


def mark_building_ref_ingested(session_factory: Any, ref: str, persona_id: str) -> None:
    """転記の参照 (``"<building_id>:<message_id>"``) の建物の行に取り込み済みの印を付ける。

    building_id と message_id はどちらも ``:`` を含みうる (message_id は
    ``"<building_id>:<seq>"``) ので、区切り位置の候補を全部試す。該当しない候補は
    ``mark_ingested`` が行の無いものとして何もしない。建物の行が既に無ければ、
    取り込み直す元も無いので何もしなくてよい。本体 DB に届かない・書けないときは
    例外 (呼び出し側は削除を取りやめる)。
    """
    if session_factory is None:
        raise BuildingRefUnresolved(ref)
    from database.building_messages import mark_ingested

    for i, ch in enumerate(ref):
        if ch != ":":
            continue
        building_id, message_id = ref[:i], ref[i + 1:]
        if building_id and message_id:
            mark_ingested(session_factory, building_id, message_id, persona_id)


def apply_review_action(
    action: str,
    items: Sequence[Tuple[str, str, str]],
    open_adapter: Callable[[str], ContextManager[Any]],
    release_building_ref: Optional[Callable[[str, str], None]] = None,
) -> Dict[str, Any]:
    """``items`` (``(persona_id, message_id, version)`` の列) に ``action`` を当てる。

    ``open_adapter(persona_id)`` は記憶ブラウザの編集 API と同じ ``get_adapter``
    (api/routes/people/utils.py)。``version`` は一覧が返した行のハッシュで、今の行と
    違えば書き換えずに ``changed`` で拒否する。選べない操作を指定された行は
    ``not_allowed``、見つからない行は ``not_found`` で拒否する。
    ``release_building_ref(persona_id, ref)`` は削除の前に建物の側の取り込み済みの
    印を確実にする (:func:`mark_building_ref_ingested`)。

    Raises:
        ValueError: ``action`` が 4 つの操作のどれでもない。
    """
    if action not in ACTIONS:
        raise ValueError(f"unknown action: {action!r}")
    by_persona: Dict[str, Dict[str, str]] = {}
    for persona_id, message_id, version in items:
        by_persona.setdefault(str(persona_id), {}).setdefault(str(message_id), str(version))
    applied: List[Dict[str, str]] = []
    rejected: List[Dict[str, str]] = []

    def _reject(persona_id: str, message_id: str, reason: str) -> None:
        rejected.append({"persona_id": persona_id, "message_id": message_id, "reason": reason})

    for persona_id, versions in by_persona.items():
        done: set = set()
        release = (
            (lambda ref, _pid=persona_id: release_building_ref(_pid, ref))
            if release_building_ref is not None else None
        )
        try:
            with open_adapter(persona_id) as adapter:
                for message_id, version in versions.items():
                    done.add(message_id)
                    try:
                        status = adapter.rewrite_message(
                            message_id,
                            lambda row, _v=version: rewrite_for_action(
                                action, row, version=_v, release_building_ref=release,
                            ),
                        )
                    except RowChanged:
                        _reject(persona_id, message_id, "changed")
                        continue
                    except ActionNotAllowed:
                        _reject(persona_id, message_id, "not_allowed")
                        continue
                    except Exception as exc:  # noqa: BLE001 — 一行の失敗で残りを止めない
                        LOGGER.warning(
                            "[system-notice-review] failed to apply %s to %s/%s: %s",
                            action, persona_id, message_id, exc, exc_info=True,
                        )
                        _reject(persona_id, message_id, "error")
                        continue
                    if status in ("updated", "deleted"):
                        applied.append({
                            "persona_id": persona_id, "message_id": message_id,
                            "result": status,
                        })
                    else:
                        _reject(
                            persona_id, message_id,
                            "not_found" if status == "missing" else status,
                        )
        except Exception as exc:  # noqa: BLE001 — 開けないペルソナは、その行だけ拒否する
            LOGGER.warning(
                "[system-notice-review] cannot open memory of %s: %s", persona_id, exc,
            )
            for mid in versions:
                if mid not in done:
                    _reject(persona_id, mid, "unavailable")
    if applied:
        LOGGER.info(
            "[system-notice-review] owner applied %s to %d row(s)", action, len(applied),
        )
    return {"applied": applied, "rejected": rejected}


# ---------------------------------------------------------------------------
# 入口の警告
# ---------------------------------------------------------------------------


def count_review_rows(
    persona_dbs: Iterable[Tuple[str, str, Path]],
) -> Tuple[int, List[Dict[str, str]]]:
    """確認待ちの行の件数と、読めなかった記憶 DB (``collect_review_rows`` の errors)。"""
    rows, errors = collect_review_rows(persona_dbs)
    return len(rows), errors


def review_alert(count: int, unreadable: Sequence[str] = ()) -> Dict[str, Any]:
    """確認待ちの行があることを知らせる警告 (画面の文言は frontend の文言表にある)。

    ``unreadable`` は記憶 DB を読めなかったペルソナの名前。読めなかった DB に
    確認待ちの行が残っているかは分からないので、件数が 0 でも警告を残して
    そのことを書く。
    """
    from api.ui_messages import ui_message

    if count > 0:
        title = ui_message("api.systemNoticeReview.alertTitle", count=count)
    else:
        title = ui_message("api.systemNoticeReview.alertUnreadableTitle")
    if unreadable:
        message = ui_message(
            "api.systemNoticeReview.alertMessageUnreadable", names=", ".join(unreadable),
        )
    else:
        message = ui_message("api.systemNoticeReview.alertMessage")
    return {
        "id": REVIEW_ALERT_ID,
        "level": "warning",
        "title": title,
        "message": message,
        "details": {},
    }


def refresh_review_alert(manager: Any, persona_dbs: Optional[Sequence[Tuple[str, str, Path]]] = None) -> int:
    """確認待ちの行を数え直し、``manager.startup_alerts`` の警告を置き直す。

    確認待ちの行が 1 件以上あるか、記憶 DB を読めなかったペルソナがあれば警告を
    置き (既にあれば同じ位置で差し替え)、どちらも無ければ外す。数えられなかった
    ときは警告をそのままにして -1 を返す。返り値は確認待ちの行の件数。
    """
    try:
        if persona_dbs is None:
            persona_dbs = known_persona_dbs(manager)
        count, errors = count_review_rows(persona_dbs)
    except Exception:  # noqa: BLE001 — 起動と確定の操作を止めない
        LOGGER.warning("[system-notice-review] failed to count pending rows", exc_info=True)
        return -1
    unreadable = [e.get("persona_name") or e.get("persona_id") or "" for e in errors]
    alerts = list(getattr(manager, "startup_alerts", None) or [])
    index = next((i for i, a in enumerate(alerts) if a.get("id") == REVIEW_ALERT_ID), None)
    if count > 0 or unreadable:
        alert = review_alert(count, unreadable)
        if index is None:
            alerts.append(alert)
        else:
            alerts[index] = alert
    elif index is not None:
        del alerts[index]
    manager.startup_alerts = alerts
    return count
