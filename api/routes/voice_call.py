"""通話モード (Gemini Live API) の WebSocket エンドポイント。

パス: ``/api/voice/call``。設計: ``docs/intent/voice_call.md``。

プロトコル (frontend と共有する契約):

1. クライアントが接続し、最初に ``{"type":"start","persona_id":...,
   "building_id":...,"voice":...,"model":...}`` を JSON テキストで送る
   (``building_id`` は互換のために受けるが**使わない** — 通話の舞台は
   サーバー側が持つペルソナの現在地で決める。``voice`` / ``model`` は省略可)
2. サーバーは文脈の積み込みと Gemini Live への接続が済んだら ``{"type":"ready"}``
3. 以後クライアントは 16kHz PCM16 LE mono のバイナリフレームを流し、サーバーは
   24kHz PCM16 LE mono のバイナリと ``input_transcript`` / ``output_transcript``
   / ``interrupted`` / ``turn_complete`` の JSON を返す
4. クライアントの ``{"type":"end"}`` または無言切断で終了。サーバーは書き戻しを
   済ませてから ``{"type":"call_ended","usage":{...}}`` を送って閉じる

エラーは必ず ``{"type":"error","code":"<識別子>","message":"<ログ用の文>"}``。
``code`` は安定した識別子で、画面の文言はフロントが自分の言語で出す
(``components.VoiceCallModal.error_<code>``)。``message`` は開発者向けで、
内部例外の文字列をそのまま載せない。

認証と Origin: WebSocket は ``BaseHTTPMiddleware`` を素通りする
(``scope["type"] == "websocket"``) ので、HTTP 側の ``OwnerAuthMiddleware`` は
この経路に効かない。同じ検証をルート側で明示的に呼ぶ (:func:`authorize_connection`)。
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from saiverse.voice_call import (
    DEFAULT_MODEL,
    DEFAULT_VOICE,
    VoiceCallError,
    VoiceCallSession,
)

LOGGER = logging.getLogger(__name__)

router = APIRouter()


def _resolve_manager():
    """起動済みの SAIVerseManager を返す。

    ``api.deps.get_manager`` は ``Request`` を取るので WebSocket ルートの
    ``Depends`` では解決されない (FastAPI は websocket 接続に ``Request`` を
    渡さない)。同じ ``saiverse.app_state`` を直接読む。
    """
    from saiverse import app_state

    if app_state.manager is None:
        raise VoiceCallError(
            "SAIVerse のマネージャーが初期化されていません", code="manager_not_ready",
        )
    return app_state.manager


def authorize_connection(websocket: WebSocket) -> Optional[str]:
    """接続を受けてよいか判定する。拒否するならエラーコード、通すなら None。

    - **認証**: HTTP 側で ``OwnerAuthMiddleware`` が働いている構成 (= LAN 公開)
      のときだけ、同じ二つの運搬手段 (Bearer ヘッダ / セッション cookie) を
      見る。localhost だけで動かしているときは HTTP 側も素通しなので、ここも
      素通しにして挙動を揃える。
    - **Origin**: ブラウザが送ってくる ``Origin`` を、HTTP の CORS が許す集合
      (``api.owner_auth.allowed_browser_origins``) と突き合わせる。``Origin``
      が無い接続 (ブラウザ以外) は、認証が通っていれば許す — ブラウザだけが
      付けるヘッダなので、無いことを拒否の理由にすると CLI や検証用の
      クライアントが繋げなくなる。

    テストはこの関数を差し替えて、認証の成否だけを組み替える。
    """
    from api.owner_auth import (
        allowed_browser_origins,
        owner_auth_active,
        websocket_owner_authorized,
    )

    app = websocket.scope.get("app")
    if owner_auth_active(app) and not websocket_owner_authorized(websocket):
        return "unauthorized"

    origin = websocket.headers.get("origin")
    if origin and origin.rstrip("/") not in allowed_browser_origins():
        return "bad_origin"
    return None


async def _send_error(websocket: WebSocket, code: str, message: str) -> None:
    try:
        await websocket.send_json({"type": "error", "code": code, "message": message})
    except Exception:
        LOGGER.debug("[voice_call] could not deliver the error to the client", exc_info=True)


async def _refuse(websocket: WebSocket, code: str, message: str) -> None:
    await _send_error(websocket, code, message)
    await _close_quietly(websocket)


@router.websocket("/call")
async def voice_call(websocket: WebSocket) -> None:
    """ペルソナとの音声通話 (Gemini Live API への双方向中継)。実験的機能。"""
    # 認証と Origin は accept の前に判定する。拒否は accept 後に code つきの
    # error フレームで返す — 握手ごと落とすとブラウザには「繋がらなかった」と
    # しか見えず、鍵の問題なのか通話の問題なのかが画面から分からない。
    refusal = authorize_connection(websocket)
    await websocket.accept()
    if refusal is not None:
        LOGGER.warning(
            "[voice_call] refusing the connection (%s) origin=%s",
            refusal, websocket.headers.get("origin"),
        )
        await _refuse(
            websocket, refusal,
            "voice call handshake rejected: " + refusal,
        )
        return

    # -- start ハンドシェイク ------------------------------------------------
    # 最初のフレームはテキストの JSON。バイナリで始まる客も黙って落とさず、
    # 同じ error フレームで返す (receive_text は bytes フレームで KeyError)。
    try:
        event = await websocket.receive()
    except WebSocketDisconnect:
        return
    if event.get("type") == "websocket.disconnect":
        return
    raw = event.get("text")
    try:
        payload = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        payload = None
    if payload is None or not isinstance(payload, dict) or payload.get("type") != "start":
        await _refuse(
            websocket, "bad_start",
            'the first frame must be a JSON {"type":"start", ...} message',
        )
        return

    persona_id = payload.get("persona_id")
    if not persona_id:
        await _refuse(websocket, "missing_persona_id", "start frame has no persona_id")
        return

    session: Optional[VoiceCallSession] = None
    try:
        session = VoiceCallSession(
            _resolve_manager(),
            str(persona_id),
            payload.get("building_id") or None,
            voice=payload.get("voice") or DEFAULT_VOICE,
            model=payload.get("model") or DEFAULT_MODEL,
        )
        await session.run(websocket)
    except WebSocketDisconnect:
        # 無言切断。書き戻しは session.run の finally が済ませている。
        LOGGER.info("[voice_call] call closed by disconnect persona=%s", persona_id)
        return
    except VoiceCallError as exc:
        LOGGER.warning("[voice_call] refusing call persona=%s: %s", persona_id, exc)
        await _refuse(websocket, exc.code, str(exc))
        return
    except Exception:
        # 内部例外の文字列はクライアントへ返さない (どこで何が落ちたかは
        # サーバーのログに全部残る)。
        LOGGER.exception("[voice_call] call failed persona=%s", persona_id)
        await _refuse(websocket, "call_failed", "the call ended with an internal error")
        return

    try:
        await websocket.send_json({"type": "call_ended", "usage": session.usage_totals()})
    except Exception:
        LOGGER.debug("[voice_call] client was already gone at call_ended", exc_info=True)
    await _close_quietly(websocket)


async def _close_quietly(websocket: WebSocket) -> None:
    try:
        await websocket.close()
    except Exception:
        LOGGER.debug("[voice_call] socket was already closed", exc_info=True)
