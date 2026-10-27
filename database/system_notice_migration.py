"""システム通知の旧形式データを新形式へ正規化する一回限りの移行。

docs/intent/system_notice_delivery.md「設計 > 移行」。供給側 (第 1 束) と提示
(第 3 束) は、システム通知を role=system・タグ無し本文 + 識別用の metadata
``__system_notice__`` (``sai_memory.memory.storage.SYSTEM_NOTICE_KEY``) で扱う。
それより前に保存された行には二つの旧形式が残っている:

1. 本文に ``<system>`` タグを埋め込んだ role=user (一部 role=assistant) の行。
   **本文が ``<system>`` で始まる** かつ **その系統の識別用 metadata を持つ**
   行だけを、タグを剥がして role=system + ``__system_notice__`` にする
   (AND 判定、不変条件 2・4)。識別用 metadata の無いタグ始まりの行は触らない
   (外部由来の偽物は role=user のまま残れば、合言葉の規則のもとで「ただの
   文章」として読まれる)。
2. 旧世界で role=system・タグ無しで保存された行 (スペルの結果・TOOL の結果・
   Handy Tool・権限拒否など)。``__system_notice__`` が無いので
   ``is_system_notice`` が偽になり、自動想起の種などから外れる。tags から系統を
   推定できるものに印を付ける。推定できない行は触らない。

ほかに、同じ本文の形を持つ列・表を最終形へ揃える: messages の
``paired_action_text`` 列、``perception_batches`` (kind=``realtime_and_recall``)
の ``rendered_text``、本体 DB の ``building_messages`` (音声通話の痕跡が
role=user + タグで載った世代)、``execution_ledger`` / ``execution_outbox`` に
凍結された本文 (まだ送り直し・記録し直しがありうる行だけ)。

「本文が ``<system>`` で始まるか」を意味の判定に使うのはこの移行の一回だけ
(不変条件 2 の例外)。常設のコードからこのモジュールを呼ばない。

起動時の入口は :func:`run_startup_system_notice_migration` (main.py が
ペルソナの読み込みより前に呼ぶ)。ペルソナごとの記憶 DB は ``embed_metadata``
の :data:`MIGRATION_MARKER_KEY` で一回限りにし、書き換える前にその DB の
写しを ``<SAIVERSE_HOME>/backups/system_notice_migration/`` に取る (通常の
起動時バックアップは数が上限を超えると古いものから消えるので、この写しは
別の置き場に置く)。移行の結果は同じ置き場にレポートとして書き出す — 本番へ
当てたあとに人の目で確かめるためのもの。

コピーで試すときは CLI を使う::

    python -m database.system_notice_migration --home <SAIVERSE_HOME の写し> --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

LOGGER = logging.getLogger(__name__)

SYSTEM_OPEN = "<system>"
SYSTEM_CLOSE = "</system>"

#: ペルソナの記憶 DB の ``embed_metadata`` に書く「この移行は済んだ」の印。
#: 値は完了時刻 (ISO)。置き場と判定 (キーがあれば済み) は saiverse/
#: v3_shape_migration.py の ``MARKER_KEY`` と同じ流儀 — 書き込み先と同じ DB に
#: 置くので、memory.db を差し替え・復元しても「移行したか」と中身がずれない。
MIGRATION_MARKER_KEY = "system_notice_migration_done"

#: 写しとレポートの置き場 (``<SAIVERSE_HOME>/backups/`` の下)。
OUTPUT_DIR_NAME = "system_notice_migration"

#: レポートに載せる系統ごとのサンプル数・本文の冒頭の長さ。
SAMPLES_PER_KIND = 3
HEAD_CHARS = 160
#: 推定できなかった role=system 行をレポートに並べる上限 (件数は全数を出す)。
UNKNOWN_SYSTEM_LIST_LIMIT = 50

#: 作業セッションのダイジェストのタグ (sea/work_session.py の ``DIGEST_TAG``)。
#: 旧データの判定なので、保存された当時の値をここに固定する。
_DIGEST_TAG = "session_digest"

#: 子ラインの結果報告の旧本文の書き出し (sea/runtime_nodes.py の
#: ``lg_subplay_node`` が組んでいた ``サブ Playbook '{name}' の実行結果:``)。
_SUBLINE_REPORT_PREFIX = "サブ Playbook '"


# ---------------------------------------------------------------------------
# 本文のタグを剥がす
# ---------------------------------------------------------------------------


def starts_with_system_tag(text: Any) -> bool:
    return isinstance(text, str) and text.startswith(SYSTEM_OPEN)


def strip_system_tags(text: str) -> str:
    """一番外側の ``<system>…</system>`` の包みを外した本文。

    本文を一番外側の塊 (包みの中身と、包みの外の地の文) に分け、各塊の前後の
    空白を落として空行 (``\\n\\n``) で繋ぐ。``perception_batches`` の
    ``rendered_text`` (``<system>`` の塊二つを ``\\n`` で繋いだ旧形式) は、
    第 3 束の提示の最終形 (sea/runtime_context.
    compose_turn_realtime_and_recall_content — 二つの本文を ``\\n\\n`` で繋ぐ) と
    同じ形になる。包みの中に入れ子で現れたタグ (中断の再開文が元の発火文を
    引用している世代など) は本文の一部としてそのまま残す — 送るときに
    クライアント層が無害化する (llm_clients/system_notice.py)。
    """
    segments: List[str] = []
    current: List[str] = []
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        if text.startswith(SYSTEM_OPEN, i):
            if depth == 0:
                segments.append("".join(current))
                current = []
            else:
                current.append(SYSTEM_OPEN)
            depth += 1
            i += len(SYSTEM_OPEN)
            continue
        if text.startswith(SYSTEM_CLOSE, i):
            if depth == 1:
                segments.append("".join(current))
                current = []
                depth = 0
            else:
                # depth > 1: 入れ子の閉じタグ / depth == 0: 対の無い閉じタグ。
                # どちらも本文の一部として残す。
                current.append(SYSTEM_CLOSE)
                if depth > 1:
                    depth -= 1
            i += len(SYSTEM_CLOSE)
            continue
        current.append(text[i])
        i += 1
    segments.append("".join(current))
    return "\n\n".join(s.strip() for s in segments if s.strip())


# ---------------------------------------------------------------------------
# 系統の判定
# ---------------------------------------------------------------------------


def _tags_of(metadata: Dict[str, Any]) -> set:
    raw = metadata.get("tags")
    if not isinstance(raw, list):
        return set()
    return {t for t in raw if isinstance(t, str)}


def _semantic_tags(tags: set) -> set:
    """判定に使うタグ (全行に付く ``conversation`` と ``pulse:`` / ``playbook:`` を除く)。"""
    return {
        t for t in tags
        if t != "conversation" and not t.startswith("pulse:") and not t.startswith("playbook:")
    }


def _has_playbook_tag(tags: set) -> bool:
    return any(t.startswith("playbook:") for t in tags)


def classify_tagged_message(
    role: Any, content: Any, metadata: Dict[str, Any],
) -> Optional[str]:
    """タグ入り本文の行の系統名 (``__system_notice__`` の値)。対象外は None。

    判定は「本文が ``<system>`` で始まる」かつ「その系統の識別用 metadata」の
    AND (docs/handoff/2026-10-27_system_notice_survey.md「別枠: 13 系統」の表と、
    git 履歴に残る旧世代の供給側)。上から順に最初に当たった規則で決める。
    """
    if not starts_with_system_tag(content):
        return None
    role_s = role.lower() if isinstance(role, str) else ""
    tags = _tags_of(metadata)
    semantic = _semantic_tags(tags)

    if role_s in ("assistant", "model"):
        # 中断の記録 (2026-08 に撤去された機構) は role=assistant で
        # ``<system>(中断: …)</system>`` を書いていた。機構の文を本人の発言として
        # 残さない (発話の尊厳)。
        if "interrupted" in tags:
            return "interrupted"
        return None
    if role_s != "user":
        return None

    if "event_message" in tags:
        if "sluice" in tags:
            return "sluice_capture"
        if "day_plan" in tags:
            return "day_plan"
        if "curation" in tags:
            return "curation"
        if "perception" in tags:
            # W14 (知覚レンダリング) より前の知覚の行。今の知覚のまとめと同じ系統名。
            return "perception"
        if "version_upgrade" in tags:
            return "version_upgrade"
        if metadata.get("voice_call") is True:
            return "voice_call"
        if semantic == {"internal", "event_message"}:
            return "building_event"
        # event_message を持つが上のどれでもない世代。系統名は印のタグそのもの。
        return "event_message"
    if "sluice" in tags and _DIGEST_TAG in tags:
        return "sluice_digest"
    if "track_context" in tags:
        # Track 切替通知 (Track ランタイムの退役 2026-08 より前。event_message を
        # 持たない世代)。
        return "track_context"
    if "track_chronicle_insert" in tags:
        return "track_chronicle_insert"
    if "situation_change" in tags:
        return "situation_change"
    if metadata.get("with") == ["system"]:
        return "schedule"
    if _has_playbook_tag(tags):
        if strip_system_tags(content).startswith(_SUBLINE_REPORT_PREFIX):
            return "subline_report"
        return "playbook_memorize"
    return None


def classify_unmarked_system_message(metadata: Dict[str, Any]) -> Optional[str]:
    """印 (``__system_notice__``) の無い role=system 行の系統名。推定できなければ None。"""
    tags = _tags_of(metadata)
    if "spell" in tags or "pre_spell" in tags:
        return "spell_result"
    if "handy_tool" in tags:
        return "handy_tool"
    if {"permission", "denied"} <= tags:
        return "playbook_permission"
    if {"error", "exec"} <= tags:
        return "playbook_exec_error"
    if "tool" in tags:
        return "tool_result"
    if _has_playbook_tag(tags):
        return "playbook_memorize"
    return None


# ---------------------------------------------------------------------------
# 結果の器
# ---------------------------------------------------------------------------


@dataclass
class Change:
    """一行の変換 (レポートのサンプル用)。"""

    table: str
    row_id: str
    kind: str
    before: str
    after: str


@dataclass
class Skipped:
    """変換しなかったタグ始まりの行 (レポートに全件出す)。"""

    table: str
    row_id: str
    role: str
    tags: List[str]
    head: str


@dataclass
class StoreResult:
    """一つの DB の移行結果。"""

    label: str
    path: str
    #: ペルソナの記憶 DB なら True (本体 DB は False)。
    persona: bool = False
    backup_path: Optional[str] = None
    changes: List[Change] = field(default_factory=list)
    skipped: List[Skipped] = field(default_factory=list)
    unknown_system_count: int = 0
    unknown_system_samples: List[Skipped] = field(default_factory=list)
    #: 当てる UPDATE 文 (SQL, 引数)。走査は読むだけで、書くのは :func:`_apply`。
    updates: List[Tuple[str, tuple]] = field(default_factory=list)
    already_done: bool = False
    error: Optional[str] = None

    def counts(self) -> Dict[Tuple[str, str], int]:
        out: Dict[Tuple[str, str], int] = {}
        for c in self.changes:
            out[(c.table, c.kind)] = out.get((c.table, c.kind), 0) + 1
        return out

    @property
    def has_findings(self) -> bool:
        return bool(
            self.changes or self.skipped or self.unknown_system_count or self.error
        )


def _head(text: Any) -> str:
    s = str(text or "").replace("\r", "")
    s = s.replace("\n", "\\n")
    return s if len(s) <= HEAD_CHARS else s[:HEAD_CHARS] + "…"


def _load_json(raw: Any) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


# ---------------------------------------------------------------------------
# ペルソナの記憶 DB
# ---------------------------------------------------------------------------


def scan_persona_conn(conn: sqlite3.Connection, result: StoreResult) -> None:
    """ペルソナの記憶 DB を読んで、変換を ``result`` に積む (書かない)。

    - messages: タグ入り本文の行 (判定は :func:`classify_tagged_message`) と、
      印の無い role=system 行 (:func:`classify_unmarked_system_message`)。
    - messages.paired_action_text: 列そのものが機構だけが書く指示文の控えなので、
      タグで始まる値は剥がす。
    - perception_batches (kind=realtime_and_recall).rendered_text。
    """
    from sai_memory.memory.storage import SYSTEM_NOTICE_KEY

    if not _table_exists(conn, "messages"):
        return
    cols = _columns(conn, "messages")
    rows = conn.execute(
        "SELECT id, role, content, metadata FROM messages "
        "WHERE substr(content, 1, ?) = ? OR role = 'system' "
        "ORDER BY created_at, rowid",
        (len(SYSTEM_OPEN), SYSTEM_OPEN),
    ).fetchall()
    for row_id, role, content, raw_meta in rows:
        metadata = _load_json(raw_meta)
        role_s = role.lower() if isinstance(role, str) else ""
        if starts_with_system_tag(content):
            kind = classify_tagged_message(role, content, metadata)
            if kind is None:
                result.skipped.append(Skipped(
                    "messages", str(row_id), str(role),
                    sorted(_tags_of(metadata)), _head(content),
                ))
                continue
            body = strip_system_tags(content)
            new_meta = dict(metadata)
            new_meta[SYSTEM_NOTICE_KEY] = kind
            result.updates.append((
                "UPDATE messages SET role = 'system', content = ?, metadata = ? "
                "WHERE id = ?",
                (body, _dump_json(new_meta), row_id),
            ))
            result.changes.append(Change("messages", str(row_id), kind, content, body))
            continue
        if role_s != "system" or metadata.get(SYSTEM_NOTICE_KEY):
            continue
        kind = classify_unmarked_system_message(metadata)
        if kind is None:
            result.unknown_system_count += 1
            if len(result.unknown_system_samples) < UNKNOWN_SYSTEM_LIST_LIMIT:
                result.unknown_system_samples.append(Skipped(
                    "messages", str(row_id), str(role),
                    sorted(_tags_of(metadata)), _head(content),
                ))
            continue
        new_meta = dict(metadata)
        new_meta[SYSTEM_NOTICE_KEY] = kind
        result.updates.append((
            "UPDATE messages SET metadata = ? WHERE id = ?",
            (_dump_json(new_meta), row_id),
        ))
        result.changes.append(Change(
            "messages(印の付与)", str(row_id), kind, str(content or ""), str(content or ""),
        ))

    if "paired_action_text" in cols:
        for row_id, text in conn.execute(
            "SELECT id, paired_action_text FROM messages "
            "WHERE substr(paired_action_text, 1, ?) = ?",
            (len(SYSTEM_OPEN), SYSTEM_OPEN),
        ).fetchall():
            body = strip_system_tags(text)
            result.updates.append((
                "UPDATE messages SET paired_action_text = ? WHERE id = ?",
                (body, row_id),
            ))
            result.changes.append(Change(
                "messages.paired_action_text", str(row_id), "paired_action", text, body,
            ))

    if _table_exists(conn, "perception_batches"):
        batch_cols = _columns(conn, "perception_batches")
        kind_expr = "kind" if "kind" in batch_cols else "'perception'"
        for batch_id, batch_kind, text in conn.execute(
            f"SELECT id, {kind_expr}, rendered_text FROM perception_batches "
            "WHERE substr(rendered_text, 1, ?) = ?",
            (len(SYSTEM_OPEN), SYSTEM_OPEN),
        ).fetchall():
            if batch_kind != "realtime_and_recall":
                result.skipped.append(Skipped(
                    "perception_batches", str(batch_id), str(batch_kind), [], _head(text),
                ))
                continue
            body = strip_system_tags(text)
            result.updates.append((
                "UPDATE perception_batches SET rendered_text = ? WHERE id = ?",
                (body, batch_id),
            ))
            result.changes.append(Change(
                "perception_batches", str(batch_id), "realtime_and_recall", text, body,
            ))


def _persona_marker_set(conn: sqlite3.Connection) -> bool:
    from sai_memory.memory.storage import get_embed_metadata

    return get_embed_metadata(conn, MIGRATION_MARKER_KEY) is not None


def _apply(conn: sqlite3.Connection, updates: Sequence[Tuple[str, tuple]]) -> None:
    for sql, params in updates:
        conn.execute(sql, params)


def _snapshot(src: Path, dest: Path) -> None:
    """SQLite のバックアップ API で ``src`` の写しを ``dest`` に作る。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(str(src))) as s, closing(sqlite3.connect(str(dest))) as d:
        s.backup(d)


def _open_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def migrate_persona_db(
    db_path: Path,
    *,
    persona_id: str,
    output_dir: Path,
    dry_run: bool = False,
    stamp: str,
) -> StoreResult:
    """一つのペルソナの記憶 DB を移行する。

    印 (:data:`MIGRATION_MARKER_KEY`) が立っていれば何もしない。変換する行が
    あれば、書き換える前に写しを ``output_dir/<persona_id>/`` へ取る (写しが
    取れなければ書き換えない)。``dry_run`` は読み取り専用で開き、何も書かない。
    """
    result = StoreResult(label=f"ペルソナ {persona_id}", path=str(db_path), persona=True)
    if dry_run:
        with closing(_open_readonly(db_path)) as conn:
            if _persona_marker_set(conn):
                result.already_done = True
                return result
            scan_persona_conn(conn, result)
        return result

    from sai_memory.memory.storage import init_db, set_embed_metadata

    # init_db はペルソナの読み込み (SAIMemoryAdapter) と同じ開き方で、列と
    # embed_metadata を用意する。
    with closing(init_db(str(db_path))) as conn:
        if _persona_marker_set(conn):
            result.already_done = True
            return result
        probe = StoreResult(label=result.label, path=result.path)
        scan_persona_conn(conn, probe)
        if probe.updates:
            backup = output_dir / persona_id / f"memory.db.pre_migration_{stamp}.bak"
            try:
                _snapshot(db_path, backup)
            except Exception as exc:  # noqa: BLE001 — 写しが取れなければ書き換えない
                LOGGER.error(
                    "[system-notice-migration] could not back up %s; skipping its migration",
                    db_path, exc_info=True,
                )
                result.error = f"移行前の写しを取れなかったので、この DB は移行していません: {exc}"
                return result
            result.backup_path = str(backup)
        conn.execute("BEGIN IMMEDIATE")
        try:
            scan_persona_conn(conn, result)
            _apply(conn, result.updates)
            set_embed_metadata(
                conn, MIGRATION_MARKER_KEY,
                datetime.now(timezone.utc).isoformat(), commit=False,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return result


# ---------------------------------------------------------------------------
# 本体 DB (saiverse.db)
# ---------------------------------------------------------------------------

#: 回収で応対が送り直される判断 (on_event) の台帳の kind (saiverse/
#: autonomy_wiring.py の ``f"judgment.{kind}"``、判断点の on_event)。
_ON_EVENT_LEDGER_KIND = "judgment.on_event"
#: スルースの台帳の kind (sea/sluice.py の ``_LEDGER_KIND``)。記録済みの結果の
#: ``prompt`` は再適用のときに記録の paired_action_text になる。
_SLUICE_LEDGER_KIND = "sluice.pan"
#: 記憶への追記を運ぶ送信トレイの宛先 (saiverse/execution_ledger_wiring.py)。
_SAIMEMORY_APPEND_TARGETS = ("saimemory.append", "saimemory.append_digest")


def scan_main_conn(conn: sqlite3.Connection, result: StoreResult) -> None:
    """本体 DB を読んで、変換を ``result`` に積む (書かない)。

    凍結された本文は「まだ送り直し・記録し直しがありうる行」だけを直す — 送り
    終えた行の凍結は崩さない (送信トレイの不変条件 6 の例外を最小にする):

    - building_messages: 音声通話の痕跡 (role=user + タグ + event_message +
      ``voice_call``) を、今の形 (role=host・タグ無し、saiverse/voice_call.py の
      ``_building_entry``) にする。
    - execution_ledger: on_event の prepared 行の ``dispatch_envelope.user_input``
      (回収で応対の入力として送り直される) と、スルースの applied / completed
      行の結果の ``prompt`` (再適用で記録の paired_action_text になる)。
    - execution_outbox: 未配送 (pending / dead) の記憶への追記の ``message``。
    """
    from sai_memory.memory.storage import SYSTEM_NOTICE_KEY

    if _table_exists(conn, "building_messages"):
        for row_id, role, content, raw_meta in conn.execute(
            "SELECT id, role, content, metadata_json FROM building_messages "
            "WHERE substr(content, 1, ?) = ? ORDER BY id",
            (len(SYSTEM_OPEN), SYSTEM_OPEN),
        ).fetchall():
            metadata = _load_json(raw_meta)
            tags = _tags_of(metadata)
            if (
                role == "user"
                and "event_message" in tags
                and metadata.get("voice_call") is True
            ):
                body = strip_system_tags(content)
                result.updates.append((
                    "UPDATE building_messages SET role = 'host', content = ? WHERE id = ?",
                    (body, row_id),
                ))
                result.changes.append(Change(
                    "building_messages", str(row_id), "voice_call", content, body,
                ))
                continue
            result.skipped.append(Skipped(
                "building_messages", str(row_id), str(role), sorted(tags), _head(content),
            ))

    if _table_exists(conn, "execution_ledger"):
        for exec_id, raw_payload in conn.execute(
            "SELECT EXECUTION_ID, PAYLOAD_JSON FROM execution_ledger "
            "WHERE KIND = ? AND STATUS = 'prepared'",
            (_ON_EVENT_LEDGER_KIND,),
        ).fetchall():
            payload = _load_json(raw_payload)
            envelope = payload.get("dispatch_envelope")
            if not isinstance(envelope, dict):
                continue
            text = envelope.get("user_input")
            if not starts_with_system_tag(text):
                continue
            body = strip_system_tags(text)
            envelope = dict(envelope, user_input=body)
            payload = dict(payload, dispatch_envelope=envelope)
            result.updates.append((
                "UPDATE execution_ledger SET PAYLOAD_JSON = ? WHERE EXECUTION_ID = ?",
                (_dump_json(payload), exec_id),
            ))
            result.changes.append(Change(
                "execution_ledger(on_event)", str(exec_id), "schedule", text, body,
            ))
        for exec_id, raw_result in conn.execute(
            "SELECT EXECUTION_ID, RESULT_JSON FROM execution_ledger "
            "WHERE KIND = ? AND STATUS IN ('applied', 'completed')",
            (_SLUICE_LEDGER_KIND,),
        ).fetchall():
            recorded = _load_json(raw_result)
            text = recorded.get("prompt")
            if not starts_with_system_tag(text):
                continue
            body = strip_system_tags(text)
            recorded = dict(recorded, prompt=body)
            result.updates.append((
                "UPDATE execution_ledger SET RESULT_JSON = ? WHERE EXECUTION_ID = ?",
                (_dump_json(recorded), exec_id),
            ))
            result.changes.append(Change(
                "execution_ledger(sluice)", str(exec_id), "paired_action", text, body,
            ))

    if _table_exists(conn, "execution_outbox"):
        placeholders = ",".join("?" for _ in _SAIMEMORY_APPEND_TARGETS)
        for outbox_id, raw_payload in conn.execute(
            "SELECT OUTBOX_ID, PAYLOAD_JSON FROM execution_outbox "
            f"WHERE TARGET IN ({placeholders}) AND STATUS IN ('pending', 'dead')",
            _SAIMEMORY_APPEND_TARGETS,
        ).fetchall():
            payload = _load_json(raw_payload)
            message = payload.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not starts_with_system_tag(content):
                continue
            metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
            kind = classify_tagged_message(message.get("role"), content, metadata)
            if kind is None:
                result.skipped.append(Skipped(
                    "execution_outbox", str(outbox_id), str(message.get("role")),
                    sorted(_tags_of(metadata)), _head(content),
                ))
                continue
            body = strip_system_tags(content)
            new_meta = dict(metadata)
            new_meta[SYSTEM_NOTICE_KEY] = kind
            message = dict(message, role="system", content=body, metadata=new_meta)
            payload = dict(payload, message=message)
            result.updates.append((
                "UPDATE execution_outbox SET PAYLOAD_JSON = ? WHERE OUTBOX_ID = ?",
                (_dump_json(payload), outbox_id),
            ))
            result.changes.append(Change(
                "execution_outbox", str(outbox_id), kind, content, body,
            ))


def migrate_main_db(
    db_path: Path, *, output_dir: Path, dry_run: bool = False, stamp: str,
) -> StoreResult:
    """本体 DB を移行する。

    印は持たない — 変換の条件 (タグで始まる・未配送・prepared) を満たす行は
    変換すると条件から外れるので、毎起動で呼んでも二度目以降は何も書かない。
    書き換える行があるときは、書き換える前に写しを ``output_dir`` に取る
    (起動時の pre_upgrade バックアップは数が上限を超えると消えるので別に取る)。
    """
    result = StoreResult(label="本体 DB", path=str(db_path))
    if not db_path.exists():
        return result
    with closing(_open_readonly(db_path)) as conn:
        scan_main_conn(conn, result)
    if dry_run or not result.updates:
        return result

    backup = output_dir / f"saiverse.db.pre_migration_{stamp}.bak"
    try:
        _snapshot(db_path, backup)
    except Exception as exc:  # noqa: BLE001 — 写しが取れなければ書き換えない
        LOGGER.error(
            "[system-notice-migration] could not back up %s; skipping its migration",
            db_path, exc_info=True,
        )
        result.error = f"移行前の写しを取れなかったので、本体 DB は移行していません: {exc}"
        result.changes = []
        result.updates = []
        return result

    applied = StoreResult(label=result.label, path=result.path, backup_path=str(backup))
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            scan_main_conn(conn, applied)
            _apply(conn, applied.updates)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return applied


# ---------------------------------------------------------------------------
# レポート
# ---------------------------------------------------------------------------


def render_report(results: Sequence[StoreResult], *, dry_run: bool, stamp: str) -> str:
    lines: List[str] = [
        "# システム通知の移行レポート",
        "",
        f"- 実行: {stamp}" + (" (dry-run: 何も書き換えていない)" if dry_run else ""),
        "- 設計: docs/intent/system_notice_delivery.md「移行」",
        "",
    ]
    for res in results:
        if not res.has_findings and not res.backup_path:
            continue
        lines.append(f"## {res.label}")
        lines.append(f"- DB: {res.path}")
        if res.backup_path:
            lines.append(f"- 移行前の写し: {res.backup_path}")
        if res.error:
            lines.append(f"- エラー: {res.error}")
        counts = res.counts()
        if counts:
            lines.append("")
            lines.append("### 変換した件数 (表 / 系統)")
            for (table, kind), count in sorted(counts.items()):
                lines.append(f"- {table} / {kind}: {count}")
            lines.append("")
            lines.append(f"### サンプル (系統ごとに最大 {SAMPLES_PER_KIND} 件)")
            seen: Dict[Tuple[str, str], int] = {}
            for c in res.changes:
                key = (c.table, c.kind)
                if seen.get(key, 0) >= SAMPLES_PER_KIND:
                    continue
                seen[key] = seen.get(key, 0) + 1
                lines.append(f"- [{c.table} / {c.kind}] id={c.row_id}")
                lines.append(f"  - 前: {_head(c.before)}")
                if c.before != c.after:
                    lines.append(f"  - 後: {_head(c.after)}")
        if res.skipped:
            lines.append("")
            lines.append(
                f"### 変換しなかった <system> 始まりの行 (全 {len(res.skipped)} 件)"
            )
            for s in res.skipped:
                lines.append(
                    f"- [{s.table}] id={s.row_id} role={s.role} tags={s.tags}: {s.head}"
                )
        if res.unknown_system_count:
            lines.append("")
            lines.append(
                f"### 系統を推定できなかった role=system の行 (全 {res.unknown_system_count} 件、"
                f"先頭 {len(res.unknown_system_samples)} 件を表示。印は付けていない)"
            )
            for s in res.unknown_system_samples:
                lines.append(f"- id={s.row_id} tags={s.tags}: {s.head}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _iter_persona_dbs(saiverse_home: Path) -> Iterable[Tuple[str, Path]]:
    personas_dir = saiverse_home / "personas"
    if not personas_dir.is_dir():
        return
    for child in sorted(personas_dir.iterdir()):
        db = child / "memory.db"
        if child.is_dir() and db.is_file():
            yield child.name, db


def _report_needed(results: Sequence[StoreResult]) -> bool:
    """レポートを書くか。

    変換・エラーがあれば書く。変換しなかった行・推定できなかった行だけなら、
    ペルソナの記憶 DB を初めて見た回 (印がまだ無かった回) だけ書く — 本体 DB は
    印を持たないので、残した行だけを理由に毎起動レポートを書かないようにする。
    """
    for r in results:
        if r.changes or r.error:
            return True
        if r.persona and not r.already_done and (
            r.skipped or r.unknown_system_count
        ):
            return True
    return False


def run_migration(
    *,
    db_path: Optional[Path],
    saiverse_home: Path,
    dry_run: bool = False,
    report_path: Optional[Path] = None,
) -> Tuple[List[StoreResult], Optional[Path]]:
    """本体 DB と全ペルソナの記憶 DB を移行し、見るべきものがあればレポートを書く。

    返り値は (DB ごとの結果, レポートのパス)。レポートは、変換した行・変換
    しなかったタグ始まりの行・推定できなかった role=system 行・エラーの
    どれかがあるときだけ書く (印の立った後の起動では書かない)。
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = saiverse_home / "backups" / OUTPUT_DIR_NAME
    results: List[StoreResult] = []

    if db_path is not None:
        try:
            results.append(migrate_main_db(
                db_path, output_dir=output_dir, dry_run=dry_run, stamp=stamp,
            ))
        except Exception as exc:  # noqa: BLE001 — 一つの DB の失敗で他を止めない
            LOGGER.error("[system-notice-migration] main DB migration failed", exc_info=True)
            results.append(StoreResult(
                label="本体 DB", path=str(db_path), error=f"移行に失敗しました (変更は巻き戻し済み): {exc}",
            ))

    for persona_id, persona_db in _iter_persona_dbs(saiverse_home):
        try:
            results.append(migrate_persona_db(
                persona_db, persona_id=persona_id, output_dir=output_dir,
                dry_run=dry_run, stamp=stamp,
            ))
        except Exception as exc:  # noqa: BLE001
            LOGGER.error(
                "[system-notice-migration] persona %s migration failed", persona_id,
                exc_info=True,
            )
            results.append(StoreResult(
                label=f"ペルソナ {persona_id}", path=str(persona_db), persona=True,
                error=f"移行に失敗しました (変更は巻き戻し済み): {exc}",
            ))

    written: Optional[Path] = None
    if _report_needed(results):
        written = report_path or (
            output_dir / f"report_{stamp}{'_dry_run' if dry_run else ''}.md"
        )
        written.parent.mkdir(parents=True, exist_ok=True)
        written.write_text(render_report(results, dry_run=dry_run, stamp=stamp), encoding="utf-8")
    for res in results:
        if res.changes or res.error:
            LOGGER.info(
                "[system-notice-migration] %s: converted=%d skipped=%d unknown_system=%d error=%s",
                res.label, len(res.changes), len(res.skipped),
                res.unknown_system_count, res.error,
            )
    if written is not None:
        LOGGER.info("[system-notice-migration] report written: %s", written)
    return results, written


def run_startup_system_notice_migration(db_path: str) -> List[Dict[str, Any]]:
    """起動時の入口 (main.py がペルソナの読み込みより前に呼ぶ)。

    返り値は画面のバナーへ積む警告・知らせ (``manager.startup_alerts`` の形)。
    移行の失敗はここで握って起動は止めない — 失敗した DB は印が立たないので
    次の起動で再試行される。
    """
    from saiverse.data_paths import get_saiverse_home

    try:
        results, report = run_migration(
            db_path=Path(db_path), saiverse_home=get_saiverse_home(),
        )
    except Exception:  # noqa: BLE001
        LOGGER.error("[system-notice-migration] migration aborted", exc_info=True)
        return [{
            "id": "system_notice_migration_failed",
            "level": "warning",
            "title": "システム通知の記録の移行に失敗しました",
            "message": "起動時の移行が途中で止まりました。次の起動で再試行します。ログを確認してください。",
            "details": {},
        }]
    alerts: List[Dict[str, Any]] = []
    failed = [r for r in results if r.error]
    if failed:
        alerts.append({
            "id": "system_notice_migration_failed",
            "level": "warning",
            "title": "システム通知の記録の移行が一部の記録でできませんでした",
            "message": (
                "移行できなかった記録は元のまま残っていて、次の起動で再試行します。"
                + (f" 詳細: {report}" if report else "")
            ),
            "details": {"failed": [r.label for r in failed]},
        })
    elif report is not None and any(r.changes for r in results):
        alerts.append({
            "id": "system_notice_migration_done",
            "level": "info",
            "title": "システム通知の記録を新しい形式へ移行しました",
            "message": f"移行前の写しと結果のレポートを保存しました: {report}",
            "details": {},
        })
    return alerts


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="システム通知の旧形式データを新形式へ正規化する (コピーで試すこと)",
    )
    parser.add_argument("--home", required=True, help="SAIVERSE_HOME (personas/ を含むフォルダ)")
    parser.add_argument(
        "--db", default=None,
        help="本体 DB のパス (省略時は <home>/user_data/database/saiverse.db、無ければ本体 DB は飛ばす)",
    )
    parser.add_argument("--dry-run", action="store_true", help="読み取り専用で開き、レポートだけ書く")
    parser.add_argument("--report", default=None, help="レポートの書き出し先")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)
    home = Path(args.home)
    db = Path(args.db) if args.db else home / "user_data" / "database" / "saiverse.db"
    results, report = run_migration(
        db_path=db if db.exists() else None,
        saiverse_home=home,
        dry_run=args.dry_run,
        report_path=Path(args.report) if args.report else None,
    )
    for res in results:
        print(f"{res.label}: converted={len(res.changes)} skipped={len(res.skipped)} "
              f"unknown_system={res.unknown_system_count} done_before={res.already_done} "
              f"error={res.error}")
    print(f"report: {report}")
    return 0 if not any(r.error for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
