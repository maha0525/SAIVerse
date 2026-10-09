#!/usr/bin/env python3
"""台本つきの偽 LLM サーバー (openai 互換の ``/v1/chat/completions``)。

隔離テスト環境 (test_data/) の合成ペルソナ ``test_persona_stub`` の LLM として
立て、応答を台本で決める。実 LLM は一切呼ばない。標準ライブラリだけで動く。

使い方::

    # サーバーを立てる (既定ポート 18097、止めるまで前面で動く)
    .venv\\Scripts\\python.exe test_fixtures\\scenarios\\scripted_llm_server.py serve

    # 別の端末から台本を切り替える (サーバーは立て直さない)
    .venv\\Scripts\\python.exe test_fixtures\\scenarios\\scripted_llm_server.py preset spell_then_429
    .venv\\Scripts\\python.exe test_fixtures\\scenarios\\scripted_llm_server.py status
    .venv\\Scripts\\python.exe test_fixtures\\scenarios\\scripted_llm_server.py requests --tail 10
    .venv\\Scripts\\python.exe test_fixtures\\scenarios\\scripted_llm_server.py load my_script.json

台本 (``POST /_control/script`` の本文、または ``load`` に渡す JSON)::

    {"steps": [
        {"kind": "text", "text": "こんにちは"},
        {"kind": "http_error", "status": 429, "repeat": -1},
        {"kind": "cut", "text": "長い発言……", "cut_after_chars": 20, "mode": "abort"}
    ]}

- 台本は先頭から順に、**台本の対象になるリクエスト**に 1 件ずつ答える。
  対象は既定で「構造化出力 (``response_format``) を求めていないリクエスト」 —
  会話の返事と、スペルのあとの続きの呼び出しがこれに当たる。構造化出力の
  リクエスト (反射判断などの裏方の呼び出し) は台本を消費せず、スキーマから
  組んだ最小の JSON で答える。各ステップの ``match`` で対象を絞れる
  (``{"stream": true}`` / ``{"stream": false}`` / ``{"contains": "文字列"}``)。
- ``repeat``: そのステップが答える回数 (既定 1)。``-1`` は台本を差し替えるまで
  答え続ける — クライアントの再試行 (SDK 2 回 × SAIVerse 側 3 回) を全部
  同じ失敗で受けるためにエラーのステップで使う。
- 台本が尽きた後の対象リクエストには ``default_text`` (既定の一文) で答える。

ステップの種類:

- ``text``: 普通の発言。ストリーミング要求なら ``chunk_chars`` 文字ずつ
  (既定 6)、``delay_ms`` おき (既定 20) に SSE で流し、最後に
  ``finish_reason: "stop"`` と ``[DONE]`` を送る。非ストリーミング要求なら
  一括で返す。
- ``http_error``: HTTP エラーを返す (``status`` 既定 429、``Retry-After: 0``)。
  429 は SAIVerse の openai クライアントで ``RateLimitError`` (error_code
  ``rate_limit``) に写る。
- ``cut``: 発言の途中でストリームを切る。``cut_after_chars`` 文字まで流した後:

  - ``mode: "abort"`` (既定) — chunked 転送の終端を送らずに TCP を閉じる。
    クライアント側では「本文が完結しないまま相手が接続を閉じた」例外になる。
  - ``mode: "eof"`` — ``finish_reason`` も ``[DONE]`` も送らずに、転送だけ
    正常に終える (ストリームが黙って途切れた形)。

  非ストリーミング要求に当たったときは、応答を返さずに接続を閉じる。

制御用エンドポイント (すべて localhost のみで待ち受ける):

- ``GET  /_control/status`` — 残りの台本・答えた件数・プリセット名一覧
- ``POST /_control/script`` — 台本を差し替える
- ``POST /_control/preset/<名前>`` — 同梱のプリセットに差し替える
- ``GET  /_control/requests?tail=N`` — 直近の受信リクエストの要約

受信したリクエストは全文を ``test_data/scripted_llm/requests.jsonl`` に追記する
(``--log`` で変更可。test_data/ の外へは既定では書かない)。
"""
from __future__ import annotations

import argparse
import copy
import json
import socket
import sys
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_PORT = 18097
DEFAULT_LOG = PROJECT_ROOT / "test_data" / "scripted_llm" / "requests.jsonl"
MODEL_NAME = "scripted-llm"
DEFAULT_TEXT = "（台本が空です。偽 LLM の既定の一文です。）"

# ---------------------------------------------------------------------------
# プリセット — 返事が途中で止まった回の出口 (docs/intent/reply_stop_exit.md) の
# 検証場面。スペル行の書式は sea/runtime_llm.py の _SPELL_PATTERN
# (``^/spell name='X' args={...}$``、行単位) に合わせてある。pocketbook_open は
# 読み取り専用・LLM を呼ばない・引数なしで唱えられるスペル
# (builtin_data/tools/pocketbook_open.py)。
# ---------------------------------------------------------------------------

SPELL_UTTERANCE = (
    "ちょっと待ってね、手帳を開いて確かめてみる。\n"
    "/spell name='pocketbook_open' args={}"
)
PLAIN_REPLY = (
    "お待たせ。手帳を見てきたよ — まだ何も書いていなかった。"
    "これが続きの発言です。"
)
LONG_UTTERANCE = (
    "今日はね、朝から少し変わったことがあって、その話をしようと思っていたんだ。"
    "最初に気づいたのは窓の外の光の色で、いつもより少しだけ青みがかっていて、"
    "それがなんだか季節の変わり目みたいに見えて、思わず手を止めて眺めてしまった。"
)

PRESETS: Dict[str, Dict[str, Any]] = {
    # (a)→(b): スペルを 1 つ唱える発言 → スペルのあとの続きの呼び出しが 429。
    # 429 は台本を差し替えるまで返し続ける (クライアントの再試行を全部受ける)。
    "spell_then_429": {
        "description": "スペルを唱える発言 → 続きの呼び出しが 429 (差し替えまで 429 のまま)",
        "steps": [
            {"kind": "text", "text": SPELL_UTTERANCE, "label": "a:spell"},
            {"kind": "http_error", "status": 429, "repeat": -1, "label": "b:429"},
        ],
    },
    # (c): 普通の発言 1 回 (「続きの生成」の二言目)。
    "reply_plain": {
        "description": "普通の発言を 1 回返す (続きの生成の二言目の確認用)",
        "steps": [
            {"kind": "text", "text": PLAIN_REPLY, "label": "c:plain"},
        ],
    },
    # (b) 単独: スペルなしで最初の呼び出しから 429 (一文字も生まれない回)。
    "http_429": {
        "description": "最初の呼び出しから 429 (差し替えまで 429 のまま)",
        "steps": [
            {"kind": "http_error", "status": 429, "repeat": -1, "label": "b:429"},
        ],
    },
    # (d): 発言の途中で TCP を閉じる (chunked の終端なし)。
    "cut_abort": {
        "description": "発言の途中で接続を切る (chunked の終端を送らずに閉じる)",
        "steps": [
            {"kind": "cut", "text": LONG_UTTERANCE, "cut_after_chars": 40,
             "mode": "abort", "label": "d:cut_abort"},
        ],
    },
    # (d) 変種: finish_reason も [DONE] も無いまま、転送だけ正常に終える。
    "cut_eof": {
        "description": "発言の途中でストリームを黙って終える (finish_reason / [DONE] なし)",
        "steps": [
            {"kind": "cut", "text": LONG_UTTERANCE, "cut_after_chars": 40,
             "mode": "eof", "label": "d:cut_eof"},
        ],
    },
    # スペルの後の最終周の途中で接続を切る。
    "spell_then_cut_abort": {
        "description": "スペルを唱える発言 → 続きの発言の途中で接続を切る",
        "steps": [
            {"kind": "text", "text": SPELL_UTTERANCE, "label": "a:spell"},
            {"kind": "cut", "text": LONG_UTTERANCE, "cut_after_chars": 40,
             "mode": "abort", "label": "d:cut_abort"},
        ],
    },
    "spell_then_cut_eof": {
        "description": "スペルを唱える発言 → 続きの発言を黙って終える",
        "steps": [
            {"kind": "text", "text": SPELL_UTTERANCE, "label": "a:spell"},
            {"kind": "cut", "text": LONG_UTTERANCE, "cut_after_chars": 40,
             "mode": "eof", "label": "d:cut_eof"},
        ],
    },
    "empty": {
        "description": "台本なし (すべて既定の一文で答える)",
        "steps": [],
    },
}


# ---------------------------------------------------------------------------
# 台本の状態
# ---------------------------------------------------------------------------


class ScriptState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._steps: List[Dict[str, Any]] = []
        self._preset: Optional[str] = None
        self._served = 0
        self._default_text = DEFAULT_TEXT
        self._recent: Deque[Dict[str, Any]] = deque(maxlen=200)

    def load(self, script: Dict[str, Any], preset: Optional[str] = None) -> None:
        steps = script.get("steps")
        if not isinstance(steps, list):
            raise ValueError("script.steps must be a list")
        normalized = []
        for i, step in enumerate(steps):
            if not isinstance(step, dict):
                raise ValueError(f"step {i} is not an object")
            kind = step.get("kind")
            if kind not in ("text", "http_error", "cut"):
                raise ValueError(f"step {i}: unknown kind {kind!r}")
            s = copy.deepcopy(step)
            s.setdefault("repeat", 1)
            s["_remaining"] = s["repeat"]
            normalized.append(s)
        with self._lock:
            self._steps = normalized
            self._preset = preset
            self._default_text = str(script.get("default_text") or DEFAULT_TEXT)

    def take(self, request_info: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """このリクエストに答えるステップを取り出す (台本の対象外なら None)。"""
        with self._lock:
            if not self._steps:
                return None
            head = self._steps[0]
            if not _matches(head.get("match"), request_info):
                return None
            step = copy.deepcopy(head)
            remaining = head["_remaining"]
            if isinstance(remaining, int) and remaining > 0:
                head["_remaining"] = remaining - 1
                if head["_remaining"] == 0:
                    self._steps.pop(0)
            self._served += 1
            return step

    def default_text(self) -> str:
        with self._lock:
            return self._default_text

    def record(self, summary: Dict[str, Any]) -> None:
        with self._lock:
            self._recent.append(summary)

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "preset": self._preset,
                "served_by_script": self._served,
                "remaining_steps": [
                    {k: v for k, v in s.items() if k not in ("text",)}
                    | {"text_head": (s.get("text") or "")[:40]}
                    for s in self._steps
                ],
                "default_text": self._default_text,
                "presets": {k: v["description"] for k, v in PRESETS.items()},
            }

    def recent(self, tail: int) -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self._recent)
        return items[-tail:] if tail > 0 else items


def _is_structured(body: Dict[str, Any]) -> bool:
    return bool(body.get("response_format"))


def _matches(match: Optional[Dict[str, Any]], info: Dict[str, Any]) -> bool:
    if info["structured"]:
        # 構造化出力は、ステップが明示的に求めたときだけ台本の対象にする。
        if not (match and match.get("structured")):
            return False
    if not match:
        return True
    if "stream" in match and bool(match["stream"]) != info["stream"]:
        return False
    if "contains" in match and str(match["contains"]) not in info["all_text"]:
        return False
    return True


# ---------------------------------------------------------------------------
# スキーマから最小の JSON を組む (構造化出力の既定応答)
# ---------------------------------------------------------------------------


def _resolve_ref(ref: str, root: Dict[str, Any]) -> Dict[str, Any]:
    node: Any = root
    for part in ref.lstrip("#/").split("/"):
        if isinstance(node, dict):
            node = node.get(part, {})
    return node if isinstance(node, dict) else {}


def example_from_schema(schema: Any, root: Optional[Dict[str, Any]] = None, depth: int = 0) -> Any:
    if not isinstance(schema, dict) or depth > 12:
        return None
    root = root if root is not None else schema
    if "$ref" in schema:
        return example_from_schema(_resolve_ref(schema["$ref"], root), root, depth + 1)
    if "const" in schema:
        return schema["const"]
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        return schema["enum"][0]
    for key in ("anyOf", "oneOf", "allOf"):
        if isinstance(schema.get(key), list) and schema[key]:
            return example_from_schema(schema[key][0], root, depth + 1)
    typ = schema.get("type")
    if isinstance(typ, list):
        typ = next((t for t in typ if t != "null"), typ[0] if typ else None)
    if typ == "object" or (typ is None and "properties" in schema):
        props = schema.get("properties") or {}
        return {
            name: example_from_schema(sub, root, depth + 1)
            for name, sub in props.items()
        }
    if typ == "array":
        return []
    if typ == "string":
        return ""
    if typ in ("integer", "number"):
        minimum = schema.get("minimum")
        return minimum if isinstance(minimum, (int, float)) else 0
    if typ == "boolean":
        return False
    if typ == "null":
        return None
    return None


def structured_default(body: Dict[str, Any]) -> str:
    fmt = body.get("response_format") or {}
    if fmt.get("type") == "json_schema":
        schema = (fmt.get("json_schema") or {}).get("schema") or {}
        return json.dumps(example_from_schema(schema), ensure_ascii=False)
    return "{}"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _message_text(msg: Dict[str, Any]) -> str:
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "\n".join(parts)
    return ""


def _chunks(text: str, size: int) -> List[str]:
    size = max(1, int(size))
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ScriptedLLM/1.0"

    # ThreadingHTTPServer にぶら下げる共有物 (serve() が設定する)
    state: ScriptState
    log_path: Optional[Path]
    log_lock = threading.Lock()

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401
        sys.stderr.write("[scripted-llm] " + (fmt % args) + "\n")

    # -- helpers ----------------------------------------------------------

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _send_json(self, status: int, obj: Any, extra_headers: Optional[Dict[str, str]] = None) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _write_chunk(self, data: bytes) -> None:
        self.wfile.write(f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n")
        self.wfile.flush()

    def _sse(self, obj: Any) -> None:
        payload = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
        self._write_chunk(f"data: {payload}\n\n".encode("utf-8"))

    def _abort_connection(self) -> None:
        self.close_connection = True
        try:
            self.wfile.flush()
        except Exception:
            pass
        try:
            # RST で閉じる (SO_LINGER 0) — chunked の終端を送らない異常切断。
            self.connection.setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00",
            )
        except Exception:
            pass
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self.connection.close()
        except Exception:
            pass

    # -- routes -------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in ("/v1/models", "/models"):
            self._send_json(200, {"object": "list", "data": [
                {"id": MODEL_NAME, "object": "model", "owned_by": "scripted"},
            ]})
            return
        if parsed.path == "/_control/status":
            self._send_json(200, self.state.status())
            return
        if parsed.path == "/_control/requests":
            qs = parse_qs(parsed.query)
            tail = int((qs.get("tail") or ["20"])[0])
            self._send_json(200, self.state.recent(tail))
            return
        self._send_json(404, {"error": {"message": f"not found: {parsed.path}"}})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            body = self._read_json()
        except Exception as exc:  # noqa: BLE001
            self._send_json(400, {"error": {"message": f"bad json: {exc}"}})
            return
        if parsed.path == "/_control/script":
            try:
                self.state.load(body, preset=body.get("name"))
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, self.state.status())
            return
        if parsed.path.startswith("/_control/preset/"):
            name = parsed.path.rsplit("/", 1)[-1]
            preset = PRESETS.get(name)
            if preset is None:
                self._send_json(404, {"error": f"unknown preset {name!r}", "presets": sorted(PRESETS)})
                return
            self.state.load(preset, preset=name)
            self._send_json(200, self.state.status())
            return
        if parsed.path in ("/v1/chat/completions", "/chat/completions"):
            self._chat(body)
            return
        self._send_json(404, {"error": {"message": f"not found: {parsed.path}"}})

    # -- chat ---------------------------------------------------------------

    def _chat(self, body: Dict[str, Any]) -> None:
        messages = body.get("messages") or []
        stream = bool(body.get("stream"))
        structured = _is_structured(body)
        all_text = "\n".join(_message_text(m) for m in messages if isinstance(m, dict))
        info = {"stream": stream, "structured": structured, "all_text": all_text}
        step = self.state.take(info)
        last = messages[-1] if messages and isinstance(messages[-1], dict) else {}
        summary = {
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "stream": stream,
            "structured": structured,
            "n_messages": len(messages),
            "last_role": last.get("role"),
            "last_text_head": _message_text(last)[:160],
            "answered_by": (step.get("label") or step.get("kind")) if step else (
                "default:structured" if structured else "default:text"
            ),
        }
        self.state.record(summary)
        self._append_log({**summary, "request": body})

        if step is None:
            text = structured_default(body) if structured else self.state.default_text()
            self._reply_text(text, stream=stream, body=body)
            return
        kind = step["kind"]
        if kind == "http_error":
            status = int(step.get("status") or 429)
            message = step.get("message") or {
                429: "Rate limit reached (scripted)",
                500: "Internal server error (scripted)",
                503: "Service unavailable (scripted)",
            }.get(status, f"HTTP {status} (scripted)")
            self._send_json(
                status,
                {"error": {"message": message, "type": "scripted_error", "code": str(status)}},
                extra_headers={"Retry-After": "0", "x-should-retry": "true"},
            )
            return
        if kind == "text":
            self._reply_text(
                str(step.get("text") or ""), stream=stream, body=body,
                chunk_chars=step.get("chunk_chars", 6), delay_ms=step.get("delay_ms", 20),
            )
            return
        if kind == "cut":
            self._reply_cut(step, stream=stream)
            return

    def _completion_id(self) -> str:
        return "chatcmpl-scripted-" + uuid.uuid4().hex[:12]

    def _reply_text(
        self, text: str, *, stream: bool, body: Dict[str, Any],
        chunk_chars: int = 6, delay_ms: int = 20,
    ) -> None:
        cid = self._completion_id()
        created = int(time.time())
        usage = {"prompt_tokens": 10, "completion_tokens": max(1, len(text)), "total_tokens": 10 + max(1, len(text))}
        if not stream:
            self._send_json(200, {
                "id": cid, "object": "chat.completion", "created": created, "model": MODEL_NAME,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                             "finish_reason": "stop"}],
                "usage": usage,
            })
            return
        self._start_stream()
        try:
            self._stream_pieces(cid, created, text, chunk_chars, delay_ms)
            self._sse({"id": cid, "object": "chat.completion.chunk", "created": created, "model": MODEL_NAME,
                       "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            if (body.get("stream_options") or {}).get("include_usage"):
                self._sse({"id": cid, "object": "chat.completion.chunk", "created": created,
                           "model": MODEL_NAME, "choices": [], "usage": usage})
            self._sse("[DONE]")
            self.wfile.write(b"0\r\n\r\n")  # chunked の終端
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # クライアントが途中で読むのをやめた (スペル行を見つけて打ち切った等)。
            self.close_connection = True

    def _start_stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _stream_pieces(self, cid: str, created: int, text: str, chunk_chars: int, delay_ms: int) -> None:
        first = True
        for piece in _chunks(text, chunk_chars):
            delta: Dict[str, Any] = {"content": piece}
            if first:
                delta["role"] = "assistant"
                first = False
            self._sse({"id": cid, "object": "chat.completion.chunk", "created": created, "model": MODEL_NAME,
                       "choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
            if delay_ms:
                time.sleep(max(0, int(delay_ms)) / 1000.0)

    def _reply_cut(self, step: Dict[str, Any], *, stream: bool) -> None:
        text = str(step.get("text") or "")
        upto = int(step.get("cut_after_chars") or max(1, len(text) // 2))
        mode = step.get("mode") or "abort"
        if not stream:
            self._abort_connection()
            return
        cid = self._completion_id()
        created = int(time.time())
        self._start_stream()
        try:
            self._stream_pieces(cid, created, text[:upto], step.get("chunk_chars", 6), step.get("delay_ms", 20))
            if mode == "eof":
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
                self.close_connection = True
                return
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
            return
        self._abort_connection()

    def _append_log(self, record: Dict[str, Any]) -> None:
        if not self.log_path:
            return
        try:
            with self.log_lock:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.log_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[scripted-llm] could not append the request log: {exc}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def serve(host: str, port: int, log_path: Optional[Path], preset: Optional[str]) -> None:
    state = ScriptState()
    if preset:
        state.load(PRESETS[preset], preset=preset)
    handler = type("Handler", (_Handler,), {"state": state, "log_path": log_path})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    print(f"[scripted-llm] listening on http://{host}:{port}/v1  (log: {log_path})", flush=True)
    print(f"[scripted-llm] presets: {', '.join(sorted(PRESETS))}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def _control(base: str, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = Request(base + path, data=data, method=method,
                  headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        return {"http_error": exc.code, "body": exc.read().decode("utf-8", "replace")}
    except URLError as exc:
        raise SystemExit(f"偽 LLM サーバーに繋がりません ({base}): {exc.reason}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="台本つきの偽 LLM サーバー (openai 互換)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="サーバーを立てる")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--log", default=str(DEFAULT_LOG),
                         help="受信リクエストの追記先 (空文字で記録しない)")
    p_serve.add_argument("--preset", choices=sorted(PRESETS), default=None,
                         help="起動時に読み込むプリセット")

    p_preset = sub.add_parser("preset", help="プリセットに差し替える")
    p_preset.add_argument("name", choices=sorted(PRESETS))

    p_load = sub.add_parser("load", help="台本 JSON ファイルに差し替える")
    p_load.add_argument("path")

    sub.add_parser("status", help="いまの台本を見る")

    p_req = sub.add_parser("requests", help="直近の受信リクエストの要約")
    p_req.add_argument("--tail", type=int, default=20)

    args = parser.parse_args(argv)
    base = f"http://127.0.0.1:{args.port}"

    if args.command == "serve":
        if args.host not in ("127.0.0.1", "localhost", "::1"):
            raise SystemExit("この偽サーバーは localhost でだけ待ち受ける")
        log_path = Path(args.log) if args.log else None
        serve(args.host, args.port, log_path, args.preset)
        return 0
    if args.command == "preset":
        result = _control(base, "POST", f"/_control/preset/{args.name}", {})
    elif args.command == "load":
        script = json.loads(Path(args.path).read_text(encoding="utf-8"))
        result = _control(base, "POST", "/_control/script", script)
    elif args.command == "status":
        result = _control(base, "GET", "/_control/status")
    else:
        result = _control(base, "GET", f"/_control/requests?tail={args.tail}")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
