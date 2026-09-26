#!/usr/bin/env python3
"""「返事が途中で止まった回の出口」のヘッドレス経路確認 (ブラウザを使わない)。

設計: docs/intent/reply_stop_exit.md。手順書: reply_stop_exit_manual.md (同じフォルダ)。

前提 (手順書の 1〜3):

- 隔離テスト環境 (``test_fixtures/setup_test_env.py``) に合成ペルソナ
  ``test_persona_stub`` と部屋 ``test_stub_room`` がある
- 偽 LLM サーバー (``scripted_llm_server.py serve``、ポート 18097) が動いている
- テストバックエンド (``start_scripted_backend.bat``、ポート 18000) が動いている

やること: 偽 LLM の台本を切り替えながら、チャット API (``/api/chat/send`` と
``/api/chat/continue``) のストリーム (NDJSON) を直接叩き、画面へ届くイベントと、
建物の記録 (テスト DB の ``building_messages``) を確かめる。DB は読み取り専用で
開く。書き込むのはテストバックエンド自身と偽 LLM サーバーだけで、どちらも
test_data/ の中にしか書かない。

使い方::

    .venv\\Scripts\\python.exe test_fixtures\\scenarios\\check_reply_stop_exit.py            # 既定: spell_429 と continue
    .venv\\Scripts\\python.exe test_fixtures\\scenarios\\check_reply_stop_exit.py --scenario all
    .venv\\Scripts\\python.exe test_fixtures\\scenarios\\check_reply_stop_exit.py --scenario cut_abort

場面:

- ``spell_429``: スペルを唱える発言 → 続きの呼び出しが 429。エラー札に
  ``interrupted_message_id`` が載ること、その行に ``_interrupted`` が立つこと、
  同じ部屋に中断の通告 (host 行) が一枚だけ入ることを確かめる。
- ``continue``: 直前の ``spell_429`` の発言に「続きの生成」を打ち、二言目が
  保存されて元の行の印が降りることを確かめる (``spell_429`` の後にだけ走る)。
- ``cut_abort`` / ``cut_eof`` / ``spell_then_cut_abort`` / ``spell_then_cut_eof``:
  ストリームの異常切断。観察した結果 (イベント・行・印・通告) を表示する。
  openai 互換の経路には「サーバーが切った」申告 (``consume_stream_error``、
  いまは Gemini クライアントだけが持つ) が無いので、期待値は固定せず観察だけ
  行う (異常があれば FAIL ではなく NOTE として出す)。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TEST_DATA = PROJECT_ROOT / "test_data"
DEFAULT_DB = TEST_DATA / "user_data" / "database" / "saiverse.db"
BACKEND = "http://127.0.0.1:18000"
FAKE_LLM = "http://127.0.0.1:18097"
ROOM = "test_stub_room"
PERSONA = "test_persona_stub"
USER_MESSAGE = "（ヘッドレス経路確認）手帳に何か書いてあったか見てくれる？"


# ---------------------------------------------------------------------------
# 安全柵 — 本番に触らない
# ---------------------------------------------------------------------------


def _guard(backend: str, fake: str, db_path: Path) -> None:
    for url in (backend, fake):
        parsed = urlparse(url)
        if parsed.hostname not in ("127.0.0.1", "localhost"):
            raise SystemExit(f"loopback 以外には繋がない: {url}")
    if urlparse(backend).port in (8000, 3000, None):
        raise SystemExit(f"本番のバックエンドのポートは叩かない: {backend}")
    resolved = db_path.resolve()
    if TEST_DATA.resolve() not in resolved.parents:
        raise SystemExit(f"test_data/ の外の DB は開かない: {resolved}")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _json_request(base: str, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else None
    except HTTPError as exc:
        return {"http_error": exc.code, "body": exc.read().decode("utf-8", "replace")}
    except URLError as exc:
        raise SystemExit(f"{base} に繋がりません: {exc.reason}")


def _stream(base: str, path: str, body: Dict[str, Any], timeout: float = 180.0) -> List[Dict[str, Any]]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = Request(base + path, data=data, method="POST", headers={"Content-Type": "application/json"})
    events: List[Dict[str, Any]] = []
    with urlopen(req, timeout=timeout) as resp:
        while True:
            line = resp.readline()
            if not line:
                break
            text = line.decode("utf-8").strip()
            if not text:
                continue
            try:
                events.append(json.loads(text))
            except json.JSONDecodeError:
                events.append({"type": "_unparsed", "raw": text[:200]})
    return events


def _preset(fake: str, name: str) -> None:
    result = _json_request(fake, "POST", f"/_control/preset/{name}", {})
    if not isinstance(result, dict) or result.get("preset") != name:
        raise SystemExit(f"偽 LLM の台本を {name} に切り替えられませんでした: {result}")


def _fake_requests(fake: str, tail: int = 50) -> List[Dict[str, Any]]:
    result = _json_request(fake, "GET", f"/_control/requests?tail={tail}")
    return result if isinstance(result, list) else []


# ---------------------------------------------------------------------------
# DB (読み取り専用)
# ---------------------------------------------------------------------------


class ReadOnlyDB:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def max_id(self, building_id: str) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(id), 0) AS m FROM building_messages WHERE building_id = ?",
                (building_id,),
            ).fetchone()
            return int(row["m"])

    def rows_after(self, building_id: str, after_id: int) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, seq, role, persona_id, content, metadata_json, message_id, pulse_id "
                "FROM building_messages WHERE building_id = ? AND id > ? ORDER BY id",
                (building_id, after_id),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["metadata"] = json.loads(d.pop("metadata_json") or "{}") or {}
            except json.JSONDecodeError:
                d["metadata"] = {"_unparsed": True}
            out.append(d)
        return out

    def row_by_message_id(self, message_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            r = conn.execute(
                "SELECT id, building_id, role, persona_id, content, metadata_json, message_id "
                "FROM building_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["metadata"] = json.loads(d.pop("metadata_json") or "{}") or {}
        return d


# ---------------------------------------------------------------------------
# 表示
# ---------------------------------------------------------------------------


class Report:
    def __init__(self) -> None:
        self.failures = 0

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            self.failures += 1
        print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
        return ok

    @staticmethod
    def note(label: str) -> None:
        print(f"  [NOTE] {label}")


def _short(text: Any, n: int = 90) -> str:
    s = str(text or "").replace("\n", "\\n")
    return s if len(s) <= n else s[:n] + "…"


def _print_events(events: List[Dict[str, Any]]) -> None:
    print("  イベント:")
    counts: Dict[str, int] = {}
    for ev in events:
        typ = ev.get("type")
        counts[str(typ)] = counts.get(str(typ), 0) + 1
        if typ in ("status",):
            continue
        if typ in ("say_chunk", "stream_chunk", "chunk", "speak_chunk", "sub_speak"):
            continue
        keys = {k: ev.get(k) for k in (
            "error_code", "message_id", "interrupted_message_id",
            "interrupted_building_id", "interrupted_building_name", "building_id",
        ) if ev.get(k) is not None}
        print(f"    - {typ} {json.dumps(keys, ensure_ascii=False)} {_short(ev.get('content'))}")
    print(f"  種類ごとの件数: {json.dumps(counts, ensure_ascii=False)}")


def _print_rows(rows: List[Dict[str, Any]]) -> None:
    print("  建物の記録 (この場面で増えた行):")
    for r in rows:
        mark = r["metadata"].get("_interrupted")
        print(
            f"    - id={r['id']} role={r['role']} persona={r['persona_id']} "
            f"message_id={r['message_id']} _interrupted={mark!r} {_short(r['content'], 110)}"
        )


def _print_fake(fake: str, since: str) -> None:
    reqs = [r for r in _fake_requests(fake) if r.get("at", "") >= since]
    print(f"  偽 LLM が受けたリクエスト ({len(reqs)} 件):")
    for r in reqs:
        print(
            f"    - {r.get('at')} stream={r.get('stream')} structured={r.get('structured')} "
            f"answered_by={r.get('answered_by')} last={r.get('last_role')}:{_short(r.get('last_text_head'), 60)}"
        )


# ---------------------------------------------------------------------------
# 場面
# ---------------------------------------------------------------------------


def _send(args: argparse.Namespace, db: ReadOnlyDB, preset: str, message: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], str]:
    _preset(args.fake, preset)
    since = time.strftime("%Y-%m-%d %H:%M:%S")
    before = db.max_id(ROOM)
    events = _stream(args.backend, "/api/chat/send", {"message": message, "building_id": ROOM})
    time.sleep(1.0)  # 後始末の書き込みがストリームの閉じと前後しても拾えるように
    rows = db.rows_after(ROOM, before)
    return events, rows, since


def scenario_spell_429(args: argparse.Namespace, db: ReadOnlyDB, rep: Report) -> Optional[str]:
    print("\n=== 場面 spell_429: スペルを唱える発言 → 続きの呼び出しが 429 ===")
    events, rows, since = _send(args, db, "spell_then_429", USER_MESSAGE)
    _print_events(events)
    _print_rows(rows)
    _print_fake(args.fake, since)

    errors = [e for e in events if e.get("type") == "error"]
    rep.check(len(errors) == 1, "エラー札のイベントが 1 件", f"{len(errors)} 件")
    err = errors[-1] if errors else {}
    rep.check(err.get("error_code") == "rate_limit", "error_code が rate_limit", repr(err.get("error_code")))
    mid = err.get("interrupted_message_id")
    rep.check(bool(mid), "error イベントに interrupted_message_id が載る", repr(mid))
    rep.check(
        "interrupted_building_id" not in err,
        "同じ部屋なので interrupted_building_id は載らない",
        repr(err.get("interrupted_building_id")),
    )

    persisted = [e for e in events if e.get("type") == "speak_persisted"]
    if persisted:
        rep.check(
            any(str(e.get("message_id")) == str(mid) for e in persisted),
            "speak_persisted の message_id と interrupted_message_id が一致 (画面の突き合わせ材料)",
            f"persisted={[e.get('message_id') for e in persisted]}",
        )
    else:
        rep.note("speak_persisted イベントが見当たらない (画面の突き合わせ材料が届いていない可能性)")

    target = db.row_by_message_id(str(mid)) if mid else None
    rep.check(target is not None, "interrupted_message_id の行が建物の記録にある")
    if target is not None:
        rep.check(target["role"] == "assistant", "その行はペルソナの発言", target["role"])
        rep.check(target["building_id"] == ROOM, "その行は返事の部屋にある", target["building_id"])
        rep.check(target["metadata"].get("_interrupted") is True, "その行に _interrupted が立つ",
                  repr(target["metadata"].get("_interrupted")))
        rep.check("pocketbook_open" in (target["content"] or ""), "その行はスペルを唱えた発言",
                  _short(target["content"], 60))
    notices = [r for r in rows if r["role"] == "host" and "中断" in (r["content"] or "")]
    rep.check(len(notices) == 1, "中断の通告 (host 行) が一枚だけ入る", f"{len(notices)} 枚")
    if notices and target is not None:
        rep.check(notices[0]["id"] > target["id"], "通告は止まった発言の後ろに並ぶ")
        print(f"  通告の本文: {notices[0]['content']}")
    interrupted_rows = [r for r in rows if r["metadata"].get("_interrupted") is True]
    rep.check(len(interrupted_rows) == 1, "印 (_interrupted) はこの返事で一つだけ", f"{len(interrupted_rows)} 行")
    return str(mid) if mid else None


def scenario_continue(args: argparse.Namespace, db: ReadOnlyDB, rep: Report, message_id: str) -> None:
    print("\n=== 場面 continue: 止まった発言に「続きの生成」 → 二言目 ===")
    _preset(args.fake, "reply_plain")
    since = time.strftime("%Y-%m-%d %H:%M:%S")
    before = db.max_id(ROOM)
    events = _stream(args.backend, "/api/chat/continue", {"message_id": message_id})
    time.sleep(1.0)
    rows = db.rows_after(ROOM, before)
    _print_events(events)
    _print_rows(rows)
    _print_fake(args.fake, since)

    errors = [e for e in events if e.get("type") == "error"]
    rep.check(not errors, "続きの生成でエラーが出ない", json.dumps(errors, ensure_ascii=False)[:200])
    replies = [r for r in rows if r["role"] == "assistant" and r["persona_id"] == PERSONA]
    rep.check(len(replies) == 1, "二言目の発言が 1 行保存される", f"{len(replies)} 行")
    if replies:
        rep.check("続きの発言" in (replies[0]["content"] or ""), "二言目は台本 (c) の本文",
                  _short(replies[0]["content"], 60))
    original = db.row_by_message_id(message_id)
    rep.check(
        original is not None and original["metadata"].get("_interrupted") is False,
        "元の発言の印が降りる (_interrupted=False)",
        repr((original or {}).get("metadata", {}).get("_interrupted")),
    )
    new_notices = [r for r in rows if r["role"] == "host" and "中断" in (r["content"] or "")]
    rep.check(not new_notices, "続きの生成で新しい通告は増えない", f"{len(new_notices)} 枚")


def scenario_observe(args: argparse.Namespace, db: ReadOnlyDB, rep: Report, preset: str) -> None:
    print(f"\n=== 場面 {preset}: ストリームの異常切断 (観察) ===")
    events, rows, since = _send(args, db, preset, USER_MESSAGE)
    _print_events(events)
    _print_rows(rows)
    _print_fake(args.fake, since)
    errors = [e for e in events if e.get("type") == "error"]
    infos = [e for e in events if e.get("type") == "info"]
    marked = [r for r in rows if r["metadata"].get("_interrupted") is True]
    notices = [r for r in rows if r["role"] == "host" and "中断" in (r["content"] or "")]
    guidance = [e for e in errors + infos if e.get("interrupted_message_id")]
    print(
        f"  要約: error={len(errors)} info={len(infos)} 印の付いた行={len(marked)} "
        f"通告={len(notices)} 案内つきイベント={len(guidance)}"
    )
    for n in notices:
        print(f"  通告の本文: {n['content']}")
    if len(marked) != len(notices):
        rep.note("印と通告の数が揃っていない (不変条件 2: 印と通告は必ず対)")
    if len(notices) > 1:
        rep.note("通告が二枚以上ある (要件 5: 一回の中断に一つずつ)")
    if marked and not guidance:
        rep.note("印は立ったが、画面への知らせに interrupted_message_id が載っていない")
    if guidance and marked:
        ok = all(str(g.get("interrupted_message_id")) == str(marked[-1]["message_id"]) for g in guidance)
        if not ok:
            rep.note("知らせの interrupted_message_id が印の付いた行と一致しない")


# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="返事が途中で止まった回の出口のヘッドレス経路確認")
    parser.add_argument("--backend", default=BACKEND)
    parser.add_argument("--fake", default=FAKE_LLM)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument(
        "--scenario", default="spell_429",
        choices=["spell_429", "cut_abort", "cut_eof", "spell_then_cut_abort", "spell_then_cut_eof", "all"],
        help="spell_429 は続けて continue も走らせる。all は全場面",
    )
    args = parser.parse_args(argv)
    db_path = Path(args.db)
    _guard(args.backend, args.fake, db_path)
    db = ReadOnlyDB(db_path)
    rep = Report()

    status = _json_request(args.backend, "GET", "/api/user/status")
    print(f"バックエンド: {args.backend}  ユーザーの現在地: {status.get('current_building_id') if isinstance(status, dict) else status}")
    moved = _json_request(args.backend, "POST", "/api/user/move", {"target_building_id": ROOM})
    print(f"ユーザーを {ROOM} へ移動: {_short(json.dumps(moved, ensure_ascii=False), 160)}")
    time.sleep(1.0)

    if args.scenario in ("spell_429", "all"):
        mid = scenario_spell_429(args, db, rep)
        if mid:
            scenario_continue(args, db, rep, mid)
        else:
            rep.check(False, "continue の場面を走らせられない (interrupted_message_id が無い)")
    if args.scenario == "all":
        for preset in ("cut_abort", "cut_eof", "spell_then_cut_abort", "spell_then_cut_eof"):
            scenario_observe(args, db, rep, preset)
    elif args.scenario != "spell_429":
        scenario_observe(args, db, rep, args.scenario)

    _preset(args.fake, "empty")
    print(f"\n結果: FAIL {rep.failures} 件")
    return 1 if rep.failures else 0


if __name__ == "__main__":
    sys.exit(main())
