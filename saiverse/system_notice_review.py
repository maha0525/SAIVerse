"""確認待ちの行 (``SYSTEM_NOTICE_REVIEW_KEY`` を持つ記憶の行) を所有者が画面で確定する。

docs/intent/system_notice_delivery.md「追補 (2026-10-29)」の「確認待ちの行の確定
(利用者の画面)」。起動時の移行 (database/system_notice_migration.py) が、
システム通知か発話かを決められずに確認待ちの印を付けた行を、全ペルソナの記憶 DB
から集めて型ごとに束ね、所有者が選んだ操作で確定する。

- 操作は 4 つ (:data:`ACTIONS`)。行ごとに、その行の作りで嘘にならない操作だけを
  「選べる操作」として返し (:func:`allowed_actions`)、選べない操作は書き換えずに
  拒否する。
- 確定した行は ``SYSTEM_NOTICE_REVIEW_KEY`` を消し、``SYSTEM_NOTICE_REVIEWED_KEY``
  に選んだ操作を書く (削除は行ごと消えるので書かない)。移行はこの鍵を持つ行を
  飛ばす。
- 書き込みは記憶ブラウザの編集 API (api/routes/people/memory.py) と同じ
  ``get_adapter`` 経由のアダプタの書き込みの取引に乗せる
  (``SAIMemoryAdapter.rewrite_message``)。
- 入口は状態に応じて出す: 確認待ちの行が 1 件以上ある間は
  ``manager.startup_alerts`` に :data:`REVIEW_ALERT_ID` の警告を置き、確定の
  操作のたびに数え直して、0 件になったら外す (:func:`refresh_review_alert`)。

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

from database.system_notice_migration import starts_with_system_tag, strip_system_tags
from sai_memory.memory.storage import (
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

#: 確認待ちの行がある間、``manager.startup_alerts`` に置く警告の id。
REVIEW_ALERT_ID = "system_notice_review_pending"

#: 一覧に載せる本文の長さ (表示用。保存された本文は変えない)。
PREVIEW_CHARS = 400
#: 束ねる鍵に使う本文の書き出しの長さ。
HEAD_CHARS = 40


class ActionNotAllowed(Exception):
    """その行では選べない操作が指定された (書き換えずに拒否する)。"""


# ---------------------------------------------------------------------------
# 行ごとの判定と書き換え
# ---------------------------------------------------------------------------


def _is_pending(metadata: Optional[Dict[str, Any]]) -> bool:
    return (
        isinstance(metadata, dict)
        and metadata.get(SYSTEM_NOTICE_REVIEW_KEY) is not None
    )


def _stripped_body(content: Any) -> Optional[str]:
    """``<system>`` の包みを剥がした本文。包みで始まらない・剥がせない行は None。"""
    if not starts_with_system_tag(content):
        return None
    return strip_system_tags(content)


def allowed_actions(role: Any, content: Any, metadata: Optional[Dict[str, Any]]) -> List[str]:
    """その行で選べる操作。確認待ちの印が無い行は空。

    「通常のユーザー発話に変換」は role=user の行にだけ出す — role=system や
    role=assistant の行に出すと、機構やペルソナの文がユーザーの名義になる。
    包みが本文全体を囲んでいない role=user の行 (ユーザー本人が本文の途中に
    ``<system>`` を打った発言など) にも出す。そのときは本文を一字も変えずに
    普通の発話へ戻す — 本人が打ったとおりの本文が、本人の発話の正しい記録だから。
    """
    if not _is_pending(metadata):
        return []
    actions = [ACTION_KEEP, ACTION_SYSTEM]
    role_s = role.lower() if isinstance(role, str) else ""
    if role_s == "user":
        actions.append(ACTION_USER)
    actions.append(ACTION_DELETE)
    return actions


def rewrite_for_action(
    action: str, role: Any, content: Any, metadata: Optional[Dict[str, Any]],
) -> Any:
    """``action`` を当てた後の行 (``SAIMemoryAdapter.rewrite_message`` の ``rewrite``)。

    選べない操作なら :class:`ActionNotAllowed` を投げる (書き換えない)。削除は
    ``SAIMemoryAdapter.MESSAGE_DELETE`` を返す。
    """
    from saiverse_memory.adapter import SAIMemoryAdapter

    if action not in allowed_actions(role, content, metadata):
        raise ActionNotAllowed(action)
    if action == ACTION_DELETE:
        return SAIMemoryAdapter.MESSAGE_DELETE
    assert metadata is not None  # allowed_actions が確認待ちの印を確かめている
    new_meta = dict(metadata)
    del new_meta[SYSTEM_NOTICE_REVIEW_KEY]
    new_meta[SYSTEM_NOTICE_REVIEWED_KEY] = action
    if action == ACTION_KEEP:
        return {"role": role, "content": content, "metadata": new_meta}
    body = _stripped_body(content)
    if action == ACTION_SYSTEM:
        new_meta[SYSTEM_NOTICE_KEY] = OWNER_CONFIRMED_KIND
        return {
            "role": "system",
            "content": body if body is not None else content,
            "metadata": new_meta,
        }
    # ACTION_USER: allowed_actions が role=user を確かめている。包みが本文全体を
    # 囲んでいれば剥がし、そうでなければ本人が打ったとおりの本文を残す。
    return {"role": role, "content": body if body is not None else content, "metadata": new_meta}


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
    content: str
    content_truncated: bool
    reason: str
    created_at: Optional[int]
    actions: List[str]
    #: 束ねる鍵に使う本文の書き出し (切り詰める前の全文から作る)
    head: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "persona_id": self.persona_id,
            "persona_name": self.persona_name,
            "message_id": self.message_id,
            "role": self.role,
            "tags": self.tags,
            "content": self.content,
            "content_truncated": self.content_truncated,
            "reason": self.reason,
            "created_at": self.created_at,
            "actions": self.actions,
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

    読めなかった DB は ``errors`` に入れて先へ進む。
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
            errors.append({"persona_id": persona_id, "error": str(exc)})
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
                content=text[:PREVIEW_CHARS],
                content_truncated=len(text) > PREVIEW_CHARS,
                reason=str(metadata.get(SYSTEM_NOTICE_REVIEW_KEY) or ""),
                created_at=int(created_at) if isinstance(created_at, (int, float)) else None,
                actions=allowed_actions(role, content, metadata),
                head=_head(text),
            ))
    return rows, errors


def group_review_rows(rows: Iterable[ReviewRow]) -> List[ReviewGroup]:
    """同じ型 (role・本文の書き出し・タグの組・残した理由・選べる操作) の行を束ねる。

    選べる操作を鍵に含めるので、束の中の行は全部同じ操作を選べる (束ごとの一括
    適用が、一部の行だけ拒否される形にならない)。件数の多い束から並べる。
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


def apply_review_action(
    action: str,
    items: Sequence[Tuple[str, str]],
    open_adapter: Callable[[str], ContextManager[Any]],
) -> Dict[str, Any]:
    """``items`` (``(persona_id, message_id)`` の列) に ``action`` を当てる。

    ``open_adapter(persona_id)`` は記憶ブラウザの編集 API と同じ ``get_adapter``
    (api/routes/people/utils.py)。選べない操作を指定された行・見つからない行は
    書き換えずに ``rejected`` へ入れる。

    Raises:
        ValueError: ``action`` が 4 つの操作のどれでもない。
    """
    if action not in ACTIONS:
        raise ValueError(f"unknown action: {action!r}")
    by_persona: Dict[str, List[str]] = {}
    for persona_id, message_id in items:
        ids = by_persona.setdefault(str(persona_id), [])
        if str(message_id) not in ids:
            ids.append(str(message_id))
    applied: List[Dict[str, str]] = []
    rejected: List[Dict[str, str]] = []
    for persona_id, message_ids in by_persona.items():
        done: set = set()
        try:
            with open_adapter(persona_id) as adapter:
                for message_id in message_ids:
                    done.add(message_id)
                    try:
                        status = adapter.rewrite_message(
                            message_id,
                            lambda role, content, metadata: rewrite_for_action(
                                action, role, content, metadata,
                            ),
                        )
                    except ActionNotAllowed:
                        rejected.append({
                            "persona_id": persona_id, "message_id": message_id,
                            "reason": "not_allowed",
                        })
                        continue
                    except Exception as exc:  # noqa: BLE001 — 一行の失敗で残りを止めない
                        LOGGER.warning(
                            "[system-notice-review] failed to apply %s to %s/%s: %s",
                            action, persona_id, message_id, exc, exc_info=True,
                        )
                        rejected.append({
                            "persona_id": persona_id, "message_id": message_id,
                            "reason": "error",
                        })
                        continue
                    if status in ("updated", "deleted"):
                        applied.append({
                            "persona_id": persona_id, "message_id": message_id,
                            "result": status,
                        })
                    else:
                        rejected.append({
                            "persona_id": persona_id, "message_id": message_id,
                            "reason": "not_found" if status == "missing" else status,
                        })
        except Exception as exc:  # noqa: BLE001 — 開けないペルソナは、その行だけ拒否する
            LOGGER.warning(
                "[system-notice-review] cannot open memory of %s: %s", persona_id, exc,
            )
            rejected.extend(
                {"persona_id": persona_id, "message_id": mid, "reason": "unavailable"}
                for mid in message_ids if mid not in done
            )
    if applied:
        LOGGER.info(
            "[system-notice-review] owner applied %s to %d row(s)", action, len(applied),
        )
    return {"applied": applied, "rejected": rejected}


# ---------------------------------------------------------------------------
# 入口の警告
# ---------------------------------------------------------------------------


def count_review_rows(persona_dbs: Iterable[Tuple[str, str, Path]]) -> int:
    rows, _errors = collect_review_rows(persona_dbs)
    return len(rows)


def review_alert(count: int) -> Dict[str, Any]:
    """確認待ちの行があることを知らせる警告 (画面の文言は frontend の文言表にある)。"""
    from api.ui_messages import ui_message

    return {
        "id": REVIEW_ALERT_ID,
        "level": "warning",
        "title": ui_message("api.systemNoticeReview.alertTitle", count=count),
        "message": ui_message("api.systemNoticeReview.alertMessage"),
        "details": {},
    }


def refresh_review_alert(manager: Any, persona_dbs: Optional[Sequence[Tuple[str, str, Path]]] = None) -> int:
    """確認待ちの行を数え直し、``manager.startup_alerts`` の警告を置き直す。

    1 件以上なら警告を置き (既にあれば同じ位置で件数を差し替え)、0 件なら外す。
    数えられなかったときは警告をそのままにして -1 を返す。
    """
    try:
        if persona_dbs is None:
            persona_dbs = known_persona_dbs(manager)
        count = count_review_rows(persona_dbs)
    except Exception:  # noqa: BLE001 — 起動と確定の操作を止めない
        LOGGER.warning("[system-notice-review] failed to count pending rows", exc_info=True)
        return -1
    alerts = list(getattr(manager, "startup_alerts", None) or [])
    index = next((i for i, a in enumerate(alerts) if a.get("id") == REVIEW_ALERT_ID), None)
    if count > 0:
        if index is None:
            alerts.append(review_alert(count))
        else:
            alerts[index] = review_alert(count)
    elif index is not None:
        del alerts[index]
    manager.startup_alerts = alerts
    return count
