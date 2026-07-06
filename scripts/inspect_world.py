#!/usr/bin/env python
"""SAIVerse 検分 CLI — 読み取り専用で世界・記憶・ログの「今」を見る。

AI エージェント (エア / Claude Code 等) が sqlite / grep をその場で組み立てる
代わりに使う統一入口。設計意図と不変条件は docs/intent/agent_inspection_cli.md。

Usage:
    python scripts/inspect_world.py personas
    python scripts/inspect_world.py memory <persona> --tags conversation --tail 30
    python scripts/inspect_world.py threads <persona>
    python scripts/inspect_world.py tracks <persona> [--all]
    python scripts/inspect_world.py tasks <persona> [--desires] [--status active]
    python scripts/inspect_world.py day-plan <persona> [--date 2026-07-05]
    python scripts/inspect_world.py sessions
    python scripts/inspect_world.py llm-io [--session latest] [--persona <id>] [--node <substr>]
    python scripts/inspect_world.py errors [--session latest]

環境の選択 (既定は本番):
    --env test          → test_data/.saiverse + test_data/user_data/database/saiverse.db
    --home / --db       → 任意パス (個別上書き。--env より優先)

安全性:
    - SQLite はすべて mode=ro URI で開く。対象環境への書き込み・ディレクトリ作成は
      一切しない (SAIMemoryAdapter は起動時バックアップ/migration が走るため経由しない)
    - stdout は UTF-8 固定 (cp932 対策)
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PREVIEW_CHARS = 200
LLM_PREVIEW_CHARS = 300

REQUEST_MARKER = "LLM_REQUEST: "
RESPONSE_MARKER = "LLM_RESPONSE: "


class InspectError(RuntimeError):
    """検分の前提が満たされていない (対象が無い等)。メッセージをそのまま表示する。"""


# ---------------------------------------------------------------------------
# 環境解決
# ---------------------------------------------------------------------------


class EnvPaths:
    """検分対象環境のパス一式。"""

    def __init__(self, home: Path, db: Path):
        self.home = home
        self.db = db
        # user_data は DB パスから導出 (<user_data>/database/saiverse.db レイアウト)
        if db.parent.name == "database":
            self.user_data = db.parent.parent
        else:
            self.user_data = home / "user_data"
        self.logs_dir = self.user_data / "logs"

    def persona_memory_db(self, persona_id: str) -> Path:
        return self.home / "personas" / persona_id / "memory.db"


def resolve_env(env: str, home: Optional[str], db: Optional[str]) -> EnvPaths:
    if env == "test":
        resolved_home = ROOT / "test_data" / ".saiverse"
        resolved_db = ROOT / "test_data" / "user_data" / "database" / "saiverse.db"
    else:
        # 本番パスの解決は既存ヘルパに任せる (import は prod 選択時のみ —
        # test 環境検分に本番側の import 副作用を持ち込まない)
        from database.paths import default_db_path
        from saiverse.data_paths import get_saiverse_home
        resolved_home = get_saiverse_home()
        resolved_db = default_db_path()
    if home:
        resolved_home = Path(home)
    if db:
        resolved_db = Path(db)
    return EnvPaths(resolved_home.resolve(), resolved_db.resolve())


# ---------------------------------------------------------------------------
# 共通ヘルパ
# ---------------------------------------------------------------------------


def _open_ro(db_path: Path) -> sqlite3.Connection:
    """SQLite を読み取り専用で開く (存在しなければ明示エラー)。"""
    if not db_path.is_file():
        raise InspectError(f"DB が見つかりません: {db_path}")
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _fmt_ts(value: Any) -> str:
    """created_at (epoch int / ISO 文字列 / DB datetime 文字列) を表示用に整える。"""
    if value is None:
        return "-"
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        try:
            return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M:%S")
        except (OverflowError, OSError, ValueError):
            return str(value)
    return str(value)[:19]


def _ts_to_epoch(value: Any) -> Optional[int]:
    """created_at を epoch 秒へ (比較用)。解釈できなければ None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value)
    if text.isdigit():
        return int(text)
    try:
        return int(datetime.fromisoformat(text).timestamp())
    except ValueError:
        return None


def _preview(text: Any, limit: int = PREVIEW_CHARS) -> str:
    s = "" if text is None else str(text)
    s = s.replace("\r\n", "\n").replace("\n", "⏎")
    if len(s) > limit:
        return s[:limit] + f"… ({len(s)} chars)"
    return s


def _parse_metadata_tags(metadata: Any) -> List[str]:
    if not metadata:
        return []
    try:
        data = json.loads(metadata) if isinstance(metadata, str) else metadata
    except json.JSONDecodeError:
        return []
    tags = data.get("tags") if isinstance(data, dict) else None
    return [str(t) for t in tags] if isinstance(tags, list) else []


# ---------------------------------------------------------------------------
# personas
# ---------------------------------------------------------------------------


def collect_personas(db_path: Path) -> List[Dict[str, Any]]:
    conn = _open_ro(db_path)
    try:
        rows = conn.execute(
            "SELECT AIID, AINAME, ACTIVITY_STATE, DEFAULT_MODEL, LIGHTWEIGHT_MODEL,"
            " IS_DISPATCHED, HOME_CITYID, PRIVATE_ROOM_ID FROM ai ORDER BY AIID"
        ).fetchall()
        occupancy = {
            r["AIID"]: r["BUILDINGID"]
            for r in conn.execute(
                "SELECT AIID, BUILDINGID FROM building_occupancy_log"
                " WHERE EXIT_TIMESTAMP IS NULL AND AIID IS NOT NULL"
            )
        }
        building_names = {
            r["BUILDINGID"]: r["BUILDINGNAME"]
            for r in conn.execute("SELECT BUILDINGID, BUILDINGNAME FROM building")
        }
    finally:
        conn.close()

    result = []
    for r in rows:
        loc = occupancy.get(r["AIID"])
        result.append({
            "persona_id": r["AIID"],
            "name": r["AINAME"],
            "activity_state": r["ACTIVITY_STATE"],
            "default_model": r["DEFAULT_MODEL"],
            "lightweight_model": r["LIGHTWEIGHT_MODEL"],
            "is_dispatched": bool(r["IS_DISPATCHED"]),
            "home_city": r["HOME_CITYID"],
            "private_room": r["PRIVATE_ROOM_ID"],
            "current_building": loc,
            "current_building_name": building_names.get(loc),
        })
    return result


def format_personas(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "(ペルソナなし)"
    lines = []
    for r in rows:
        loc = r["current_building"] or "-"
        if r["current_building_name"]:
            loc += f" ({r['current_building_name']})"
        dispatched = " [派遣中]" if r["is_dispatched"] else ""
        lines.append(
            f"{r['persona_id']}  {r['name']}  state={r['activity_state']}{dispatched}\n"
            f"    model={r['default_model']} / lite={r['lightweight_model']}\n"
            f"    city={r['home_city']}  現在地={loc}  私室={r['private_room'] or '-'}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# memory / threads
# ---------------------------------------------------------------------------


def collect_memory(
    memory_db: Path,
    *,
    tags: Optional[List[str]] = None,
    thread: Optional[str] = None,
    role: Optional[str] = None,
    line_role: Optional[str] = None,
    scope: Optional[str] = None,
    track: Optional[str] = None,
    pulse: Optional[str] = None,
    grep: Optional[str] = None,
    since: Optional[str] = None,
    on_date: Optional[str] = None,
    tail: int = 20,
) -> List[Dict[str, Any]]:
    """messages を新しい順に走査し、フィルタ通過分を tail 件まで集めて古い順で返す。"""
    since_epoch: Optional[int] = None
    date_range: Optional[tuple] = None
    if since:
        since_epoch = _ts_to_epoch(since)
        if since_epoch is None:
            raise InspectError(f"--since を解釈できません: {since} (YYYY-MM-DD[THH:MM] 形式)")
    if on_date:
        try:
            day_start = datetime.fromisoformat(on_date)
        except ValueError:
            raise InspectError(f"--date を解釈できません: {on_date} (YYYY-MM-DD 形式)")
        date_range = (int(day_start.timestamp()), int(day_start.timestamp()) + 86400)

    where = []
    params: List[Any] = []
    if thread:
        where.append("thread_id = ?")
        params.append(thread)
    if role:
        where.append("role = ?")
        params.append(role)
    if line_role:
        where.append("line_role = ?")
        params.append(line_role)
    if scope:
        where.append("scope = ?")
        params.append(scope)
    if track:
        where.append("origin_track_id = ?")
        params.append(track)
    if pulse:
        where.append("pulse_id = ?")
        params.append(pulse)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    conn = _open_ro(memory_db)
    try:
        cursor = conn.execute(
            "SELECT id, thread_id, role, content, created_at, metadata,"
            " origin_track_id, line_role, line_id, scope, pulse_id, spell_seq"
            f" FROM messages{where_sql} ORDER BY created_at DESC, rowid DESC",
            params,
        )
        matched: List[Dict[str, Any]] = []
        grep_lower = grep.lower() if grep else None
        for row in cursor:
            row_tags = _parse_metadata_tags(row["metadata"])
            if tags and not all(t in row_tags for t in tags):
                continue
            if grep_lower and grep_lower not in str(row["content"] or "").lower():
                continue
            epoch = _ts_to_epoch(row["created_at"])
            if since_epoch is not None and (epoch is None or epoch < since_epoch):
                continue
            if date_range and (epoch is None or not (date_range[0] <= epoch < date_range[1])):
                continue
            matched.append({
                "id": row["id"],
                "thread_id": row["thread_id"],
                "role": row["role"],
                "content": row["content"],
                "created_at": _fmt_ts(row["created_at"]),
                "tags": row_tags,
                "origin_track_id": row["origin_track_id"],
                "line_role": row["line_role"],
                "line_id": row["line_id"],
                "scope": row["scope"],
                "pulse_id": row["pulse_id"],
                "spell_seq": row["spell_seq"],
            })
            if len(matched) >= tail:
                break
    finally:
        conn.close()
    matched.reverse()  # 古い順で出力 (読解しやすさ優先)
    return matched


def format_memory(rows: List[Dict[str, Any]], *, full: bool = False) -> str:
    if not rows:
        return "(該当メッセージなし)"
    lines = []
    for r in rows:
        tag_str = ",".join(r["tags"]) if r["tags"] else "-"
        parts = [f"[{r['created_at']}] {r['role']}  tags={tag_str}  scope={r['scope'] or '-'}"]
        if r["line_role"]:
            parts.append(f"line={r['line_role']}")
        if r["origin_track_id"]:
            parts.append(f"track={str(r['origin_track_id'])[:8]}")
        if r["pulse_id"]:
            parts.append(f"pulse={str(r['pulse_id'])[:8]}")
        parts.append(f"id={str(r['id'])[:8]}")
        header = "  ".join(parts)
        body = str(r["content"] or "") if full else _preview(r["content"])
        lines.append(f"{header}\n    {body}")
    return "\n".join(lines)


def collect_threads(memory_db: Path) -> List[Dict[str, Any]]:
    conn = _open_ro(memory_db)
    try:
        rows = conn.execute(
            "SELECT t.id, t.resource_id, t.overview,"
            " COUNT(m.id) AS msg_count, MIN(m.created_at) AS first_at, MAX(m.created_at) AS last_at"
            " FROM threads t LEFT JOIN messages m ON m.thread_id = t.id"
            " GROUP BY t.id ORDER BY last_at DESC"
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "thread_id": r["id"],
            "resource_id": r["resource_id"],
            "msg_count": r["msg_count"],
            "first_at": _fmt_ts(r["first_at"]),
            "last_at": _fmt_ts(r["last_at"]),
            "overview": r["overview"],
        }
        for r in rows
    ]


def format_threads(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "(スレッドなし)"
    lines = []
    for r in rows:
        lines.append(
            f"{r['thread_id']}  {r['msg_count']} 件  {r['first_at']} 〜 {r['last_at']}\n"
            f"    {_preview(r['overview'], 120) or '(overview なし)'}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# tracks / tasks / day-plan
# ---------------------------------------------------------------------------


def collect_tracks(db_path: Path, persona_id: str, *, include_all: bool = False) -> List[Dict[str, Any]]:
    conn = _open_ro(db_path)
    try:
        where = "WHERE persona_id = ?"
        if not include_all:
            where += " AND status NOT IN ('completed', 'aborted')"
        rows = conn.execute(
            "SELECT track_id, short_id, title, track_type, status, is_persistent,"
            " is_forgotten, intent, last_active_at, created_at, completed_at, aborted_at"
            f" FROM action_track {where}"
            " ORDER BY last_active_at IS NULL, last_active_at DESC, created_at DESC",
            (persona_id,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "short_id": r["short_id"],
            "track_id": r["track_id"],
            "title": r["title"],
            "track_type": r["track_type"],
            "status": r["status"],
            "is_persistent": bool(r["is_persistent"]),
            "is_forgotten": bool(r["is_forgotten"]),
            "intent": r["intent"],
            "last_active_at": _fmt_ts(r["last_active_at"]),
            "created_at": _fmt_ts(r["created_at"]),
            "completed_at": _fmt_ts(r["completed_at"]),
            "aborted_at": _fmt_ts(r["aborted_at"]),
        }
        for r in rows
    ]


def format_tracks(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "(該当 Track なし)"
    lines = []
    for r in rows:
        flags = []
        if r["is_persistent"]:
            flags.append("persistent")
        if r["is_forgotten"]:
            flags.append("forgotten")
        flag_str = f" [{','.join(flags)}]" if flags else ""
        lines.append(
            f"t:{r['short_id']}  [{r['status']}] {r['track_type']}{flag_str}  {r['title'] or '(無題)'}\n"
            f"    id={str(r['track_id'])[:8]}  last_active={r['last_active_at']}  created={r['created_at']}\n"
            f"    intent: {_preview(r['intent'], 120) or '-'}"
        )
    return "\n".join(lines)


def collect_tasks(
    db_path: Path,
    persona_id: str,
    *,
    desires_only: bool = False,
    status: Optional[str] = None,
    include_all: bool = False,
) -> List[Dict[str, Any]]:
    conn = _open_ro(db_path)
    try:
        where = ["persona_id = ?"]
        params: List[Any] = [persona_id]
        if desires_only:
            where.append("parent_kind = 'note'")
        if status:
            where.append("status = ?")
            params.append(status)
        elif not include_all:
            where.append("status NOT IN ('completed', 'cancelled')")
        rows = conn.execute(
            "SELECT short_id, id, title, goal, status, priority, parent_kind, track_id,"
            " desire_type, desire_state, desire_source, artifact_refs,"
            " created_at, updated_at, completed_at"
            f" FROM persona_task WHERE {' AND '.join(where)}"
            " ORDER BY updated_at DESC",
            params,
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "short_id": r["short_id"],
            "id": r["id"],
            "title": r["title"],
            "goal": r["goal"],
            "status": r["status"],
            "priority": r["priority"],
            "parent_kind": r["parent_kind"],
            "track_id": r["track_id"],
            "desire_type": r["desire_type"],
            "desire_state": r["desire_state"],
            "desire_source": r["desire_source"],
            "artifact_refs": r["artifact_refs"],
            "created_at": _fmt_ts(r["created_at"]),
            "updated_at": _fmt_ts(r["updated_at"]),
            "completed_at": _fmt_ts(r["completed_at"]),
        }
        for r in rows
    ]


def format_tasks(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "(該当タスクなし)"
    lines = []
    for r in rows:
        kind = "desire" if r["parent_kind"] == "note" else (r["parent_kind"] or "standalone")
        head = f"task:{r['short_id']}  [{r['status']}] ({kind})  {r['title']}"
        detail = [f"    goal: {_preview(r['goal'], 120) or '-'}"]
        if r["desire_type"] or r["desire_state"]:
            detail.append(
                f"    desire: type={r['desire_type'] or '-'} state={r['desire_state'] or 'fresh'}"
                f"  source: {_preview(r['desire_source'], 100) or '-'}"
            )
        if r["artifact_refs"]:
            detail.append(f"    artifacts: {r['artifact_refs']}")
        detail.append(f"    updated={r['updated_at']}  created={r['created_at']}")
        lines.append("\n".join([head] + detail))
    return "\n".join(lines)


def collect_day_plan(
    db_path: Path, persona_id: str, *, plan_date: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    conn = _open_ro(db_path)
    try:
        if plan_date:
            row = conn.execute(
                "SELECT plan_date, slots_json, meta_json, created_at, updated_at"
                " FROM persona_day_plan WHERE persona_id = ? AND plan_date = ?",
                (persona_id, plan_date),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT plan_date, slots_json, meta_json, created_at, updated_at"
                " FROM persona_day_plan WHERE persona_id = ? ORDER BY plan_date DESC LIMIT 1",
                (persona_id,),
            ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    try:
        slots = json.loads(row["slots_json"])
    except (TypeError, json.JSONDecodeError):
        slots = []
    try:
        meta = json.loads(row["meta_json"]) if row["meta_json"] else None
    except json.JSONDecodeError:
        meta = row["meta_json"]
    return {
        "plan_date": row["plan_date"],
        "slots": slots,
        "meta": meta,
        "created_at": _fmt_ts(row["created_at"]),
        "updated_at": _fmt_ts(row["updated_at"]),
    }


def format_day_plan(plan: Optional[Dict[str, Any]]) -> str:
    if plan is None:
        return "(時間割なし)"
    lines = [f"plan_date={plan['plan_date']}  created={plan['created_at']}  updated={plan['updated_at']}"]
    for slot in plan["slots"]:
        extras = []
        if slot.get("defer_count"):
            extras.append(f"defer={slot['defer_count']}")
        if slot.get("skip_reason"):
            extras.append(f"skip_reason={slot['skip_reason']}")
        if slot.get("record_level"):
            extras.append(f"record={slot['record_level']}")
        extra_str = ("  " + "  ".join(extras)) if extras else ""
        lines.append(
            f"  {slot.get('start', '--:--')}  [{slot.get('status', '?'):8}] {slot.get('kind', '?')}"
            f"  {slot.get('title', '')}  ref={slot.get('ref', '-')}"
            f"  facility={slot.get('facility', '-')}  budget={slot.get('budget_rounds', 0)}{extra_str}"
        )
        if slot.get("note"):
            lines.append(f"        note: {_preview(slot['note'], 100)}")
    if plan["meta"]:
        lines.append("meta:")
        meta_text = json.dumps(plan["meta"], ensure_ascii=False, indent=2) \
            if isinstance(plan["meta"], (dict, list)) else str(plan["meta"])
        lines.extend("  " + ln for ln in meta_text.splitlines())
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# sessions / llm-io / errors
# ---------------------------------------------------------------------------


def collect_sessions(logs_dir: Path, *, limit: int = 10) -> List[Dict[str, Any]]:
    if not logs_dir.is_dir():
        raise InspectError(f"ログディレクトリが見つかりません: {logs_dir}")
    dirs = sorted((d for d in logs_dir.iterdir() if d.is_dir()), key=lambda d: d.name, reverse=True)
    result = []
    for d in dirs[:limit]:
        files = [
            {"name": f.name, "size_kb": round(f.stat().st_size / 1024, 1)}
            for f in sorted(d.iterdir()) if f.is_file()
        ]
        result.append({"session": d.name, "files": files})
    return result


def format_sessions(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "(セッションログなし)"
    lines = []
    for r in rows:
        file_str = ", ".join(f"{f['name']} ({f['size_kb']}KB)" for f in r["files"]) or "(空)"
        lines.append(f"{r['session']}\n    {file_str}")
    return "\n".join(lines)


def resolve_session_dir(logs_dir: Path, session: str) -> Path:
    """セッション指定 ('latest' / ディレクトリ名) を実パスへ解決する。"""
    if not logs_dir.is_dir():
        raise InspectError(f"ログディレクトリが見つかりません: {logs_dir}")
    if session == "latest":
        dirs = sorted((d for d in logs_dir.iterdir() if d.is_dir()), key=lambda d: d.name)
        if not dirs:
            raise InspectError(f"セッションログがありません: {logs_dir}")
        return dirs[-1]
    target = logs_dir / session
    if not target.is_dir():
        raise InspectError(f"セッションが見つかりません: {target}")
    return target


def collect_llm_io(
    llm_log: Path,
    *,
    persona: Optional[str] = None,
    node: Optional[str] = None,
    entry_type: Optional[str] = None,
    grep: Optional[str] = None,
    tail: int = 20,
) -> List[Dict[str, Any]]:
    """llm_io.log を行走査し、フィルタ通過分の末尾 tail 件を返す (メモリ安全)。"""
    if not llm_log.is_file():
        raise InspectError(f"llm_io.log が見つかりません: {llm_log}")
    matched: deque = deque(maxlen=tail)
    grep_lower = grep.lower() if grep else None
    with llm_log.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            for marker, kind in ((REQUEST_MARKER, "request"), (RESPONSE_MARKER, "response")):
                pos = line.find(marker)
                if pos < 0:
                    continue
                if entry_type and kind != entry_type:
                    break
                try:
                    entry = json.loads(line[pos + len(marker):])
                except json.JSONDecodeError:
                    break
                if persona and persona not in (entry.get("persona_id"), entry.get("persona_name")):
                    break
                if node and node not in str(entry.get("node") or "") \
                        and node not in str(entry.get("source") or ""):
                    break
                if grep_lower:
                    haystack = json.dumps(entry, ensure_ascii=False).lower()
                    if grep_lower not in haystack:
                        break
                entry["_ts"] = line[:19]
                matched.append(entry)
                break
    return list(matched)


def format_llm_io(entries: List[Dict[str, Any]], *, full: bool = False) -> str:
    if not entries:
        return "(該当 LLM I/O なし)"
    if full:
        return "\n".join(json.dumps(e, ensure_ascii=False, indent=2) for e in entries)
    lines = []
    for e in entries:
        who = e.get("persona_name") or e.get("persona_id") or "-"
        loc = f"{e.get('source', '?')}·{e.get('node', '?')}"
        if e.get("type") == "request":
            messages = e.get("messages") or []
            last = messages[-1] if messages else {}
            content = last.get("content")
            if isinstance(content, list):  # マルチモーダル content
                content = " ".join(str(p.get("text", "")) if isinstance(p, dict) else str(p) for p in content)
            lines.append(
                f"[{e['_ts']}] REQ  {loc}  {who}  ({len(messages)} msgs)\n"
                f"    last[{last.get('role', '?')}]: {_preview(content, LLM_PREVIEW_CHARS)}"
            )
        else:
            lines.append(
                f"[{e['_ts']}] RES  {loc}  {who}\n"
                f"    {_preview(e.get('output'), LLM_PREVIEW_CHARS)}"
            )
    return "\n".join(lines)


_LEVEL_TOKENS = (" [WARNING] ", " [ERROR] ", " [CRITICAL] ")


def collect_errors(session_dir: Path, *, tail: int = 30) -> Dict[str, Any]:
    """error.log (+ timeout_diagnostics.log) から WARNING 以上のダイジェストを作る。"""
    error_log = session_dir / "error.log"
    entries: List[Dict[str, str]] = []
    counts: Counter = Counter()
    if error_log.is_file():
        current: Optional[Dict[str, str]] = None
        with error_log.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip("\n")
                token = next((t for t in _LEVEL_TOKENS if t in line), None)
                if token:
                    # "ts [LEVEL] logger: message" 形式
                    rest = line.split(token, 1)[1]
                    logger_name = rest.split(":", 1)[0].strip()
                    counts[f"{token.strip(' []')} {logger_name}"] += 1
                    current = {"line": line}
                    entries.append(current)
                elif current is not None:
                    # 継続行 (traceback 等) は直前エントリへ連結
                    current["line"] += "\n" + line
    timeout_log = session_dir / "timeout_diagnostics.log"
    timeout_tail: List[str] = []
    if timeout_log.is_file() and timeout_log.stat().st_size > 0:
        with timeout_log.open(encoding="utf-8", errors="replace") as f:
            timeout_tail = [ln.rstrip("\n") for ln in deque(f, maxlen=10)]
    return {
        "session": session_dir.name,
        "counts": dict(counts.most_common()),
        "entries": [e["line"] for e in entries[-tail:]],
        "timeout_tail": timeout_tail,
        "error_log_exists": error_log.is_file(),
    }


def format_errors(digest: Dict[str, Any]) -> str:
    lines = [f"session: {digest['session']}"]
    if not digest["error_log_exists"]:
        lines.append("(error.log なし — このセッションは WARNING 以上を記録していない)")
        return "\n".join(lines)
    if not digest["counts"]:
        lines.append("WARNING 以上のログはありません。")
    else:
        lines.append("件数 (レベル+ロガー別):")
        for key, count in digest["counts"].items():
            lines.append(f"  {count:5d}  {key}")
        lines.append("")
        lines.append(f"直近 {len(digest['entries'])} 件:")
        lines.extend(digest["entries"])
    if digest["timeout_tail"]:
        lines.append("")
        lines.append("timeout_diagnostics.log (末尾):")
        lines.extend("  " + ln for ln in digest["timeout_tail"])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI 配線
# ---------------------------------------------------------------------------


def _emit(data: Any, text: str, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(text)


def main(argv: Optional[List[str]] = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--env", choices=["prod", "test"], default="prod",
                        help="対象環境 (既定: prod=本番)")
    common.add_argument("--home", default=None, help="SAIVERSE_HOME の上書き")
    common.add_argument("--db", default=None, help="saiverse.db パスの上書き")
    common.add_argument("--json", action="store_true", help="JSON で出力")

    parser = argparse.ArgumentParser(
        description="SAIVerse 検分 CLI (読み取り専用)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("personas", parents=[common], help="ペルソナ一覧")

    p_mem = sub.add_parser("memory", parents=[common], help="ペルソナの記憶 (messages)")
    p_mem.add_argument("persona")
    p_mem.add_argument("--tags", default=None, help="カンマ区切り (全部含む行のみ)")
    p_mem.add_argument("--thread", default=None)
    p_mem.add_argument("--role", default=None)
    p_mem.add_argument("--line-role", default=None)
    p_mem.add_argument("--scope", default=None)
    p_mem.add_argument("--track", default=None, help="origin_track_id 完全一致")
    p_mem.add_argument("--pulse", default=None, help="pulse_id 完全一致")
    p_mem.add_argument("--grep", default=None, help="content 部分一致 (大文字小文字無視)")
    p_mem.add_argument("--since", default=None, help="この日時以降 (YYYY-MM-DD[THH:MM])")
    p_mem.add_argument("--date", default=None, help="この日付のみ (YYYY-MM-DD)")
    p_mem.add_argument("--tail", type=int, default=20)
    p_mem.add_argument("--full", action="store_true", help="本文を切り詰めない")

    p_thr = sub.add_parser("threads", parents=[common], help="スレッド一覧")
    p_thr.add_argument("persona")

    p_trk = sub.add_parser("tracks", parents=[common], help="Track 一覧")
    p_trk.add_argument("persona")
    p_trk.add_argument("--all", action="store_true", help="completed/aborted も含める")

    p_tsk = sub.add_parser("tasks", parents=[common], help="タスク/欲求一覧")
    p_tsk.add_argument("persona")
    p_tsk.add_argument("--desires", action="store_true", help="欲求 (parent_kind='note') のみ")
    p_tsk.add_argument("--status", default=None)
    p_tsk.add_argument("--all", action="store_true", help="completed/cancelled も含める")

    p_day = sub.add_parser("day-plan", parents=[common], help="時間割")
    p_day.add_argument("persona")
    p_day.add_argument("--date", default=None, help="YYYY-MM-DD (省略時: 最新)")

    p_ses = sub.add_parser("sessions", parents=[common], help="ログセッション一覧")
    p_ses.add_argument("--tail", type=int, default=10)

    p_llm = sub.add_parser("llm-io", parents=[common], help="LLM I/O ログ")
    p_llm.add_argument("--session", default="latest")
    p_llm.add_argument("--persona", default=None, help="persona_id または persona_name")
    p_llm.add_argument("--node", default=None, help="node / source 部分一致")
    p_llm.add_argument("--type", choices=["request", "response"], default=None)
    p_llm.add_argument("--grep", default=None)
    p_llm.add_argument("--tail", type=int, default=20)
    p_llm.add_argument("--full", action="store_true", help="エントリ全文 (JSON)")

    p_err = sub.add_parser("errors", parents=[common], help="WARNING 以上のダイジェスト")
    p_err.add_argument("--session", default="latest")
    p_err.add_argument("--tail", type=int, default=30)

    args = parser.parse_args(argv)

    # Windows コンソール (cp932) 対策
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    try:
        env = resolve_env(args.env, args.home, args.db)

        if args.command == "personas":
            rows = collect_personas(env.db)
            _emit(rows, format_personas(rows), args.json)
        elif args.command == "memory":
            rows = collect_memory(
                env.persona_memory_db(args.persona),
                tags=[t.strip() for t in args.tags.split(",")] if args.tags else None,
                thread=args.thread, role=args.role, line_role=args.line_role,
                scope=args.scope, track=args.track, pulse=args.pulse,
                grep=args.grep, since=args.since, on_date=args.date, tail=args.tail,
            )
            _emit(rows, format_memory(rows, full=args.full), args.json)
        elif args.command == "threads":
            rows = collect_threads(env.persona_memory_db(args.persona))
            _emit(rows, format_threads(rows), args.json)
        elif args.command == "tracks":
            rows = collect_tracks(env.db, args.persona, include_all=args.all)
            _emit(rows, format_tracks(rows), args.json)
        elif args.command == "tasks":
            rows = collect_tasks(
                env.db, args.persona,
                desires_only=args.desires, status=args.status, include_all=args.all,
            )
            _emit(rows, format_tasks(rows), args.json)
        elif args.command == "day-plan":
            plan = collect_day_plan(env.db, args.persona, plan_date=args.date)
            _emit(plan, format_day_plan(plan), args.json)
        elif args.command == "sessions":
            rows = collect_sessions(env.logs_dir, limit=args.tail)
            _emit(rows, format_sessions(rows), args.json)
        elif args.command == "llm-io":
            session_dir = resolve_session_dir(env.logs_dir, args.session)
            entries = collect_llm_io(
                session_dir / "llm_io.log",
                persona=args.persona, node=args.node, entry_type=args.type,
                grep=args.grep, tail=args.tail,
            )
            _emit(entries, format_llm_io(entries, full=args.full), args.json)
        elif args.command == "errors":
            session_dir = resolve_session_dir(env.logs_dir, args.session)
            digest = collect_errors(session_dir, tail=args.tail)
            _emit(digest, format_errors(digest), args.json)
    except InspectError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
