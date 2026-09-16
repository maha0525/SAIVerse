"""通話モード (Gemini Live API) のバックエンド。

設計と不変条件: ``docs/intent/voice_call.md``。

この module は「通話の一回分」を持つ。ブラウザの WebSocket と Gemini Live API
のセッションの間で音声を中継し、通話が終わったところで文字起こしを
**SAIMemory と建物履歴の両方へ追記**する (通話の内容が後のテキスト会話でも
ペルソナに通じているように — intent §不変条件 3)。

書き戻しは追記のみで、既存のメッセージには一切触らない。ユーザー行は入力側の
自動文字起こし、ペルソナ行は出力側の自動文字起こし (= モデルが実際に発した音声
の転写であって、こちらが書いた文ではない)。どちらの行にも
``metadata["voice_call"] = True`` を刻み、ユーザー行にはさらに
``metadata["voice_transcript"] = True`` を刻んで「本人がタイプした文ではなく
機械の書き起こしである」ことを記録に残す。

書き込みの順番は **1 件ごとに「SAIMemory へ書く → その成否で建物行の
``ingested_by`` を決める」**。記憶に入らなかった行に「取り込み済み」の印を
先に打つと、建物履歴からペルソナ記憶への自動転記
(``builtin_data/tools/get_building_messages.py``) がその行を永久に飛ばし、
通話の内容がペルソナの記憶から丸ごと落ちる。

声そのものも残す。発話の区切り (turn_complete / interrupted) ごとに、その区切りの
マイク音声とペルソナの音声を wav にして
``<persona_dir>/voice_calls/<通話開始時刻>/NNN_user.wav`` /
``NNN_persona.wav`` へ保存し、対応する行の ``metadata["voice_audio"]`` に
ペルソナフォルダからの相対パスを刻む。文字起こしが取れずに記憶の行にならなかった
区切りの音声も、ファイルとしては保存する (紐だけ付かない) — 声は記憶の素材なので、
文字にならなかった分を捨てない。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import wave
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

LOGGER = logging.getLogger(__name__)

# --- プロトコル / 音声フォーマットの定数 (frontend と共有する契約) ---------
DEFAULT_MODEL = "gemini-3.8-live"
DEFAULT_VOICE = "Kore"
INPUT_SAMPLE_RATE = 16000
OUTPUT_SAMPLE_RATE = 24000
INPUT_MIME_TYPE = f"audio/pcm;rate={INPUT_SAMPLE_RATE}"

#: 保存する音声の形。Live API の両方向とも mono / 16bit PCM (リトルエンディアン)
#: なので、wav のヘッダを被せるだけで再生できる。
AUDIO_CHANNELS = 1
AUDIO_SAMPLE_WIDTH = 2
#: ペルソナフォルダの下で通話の音声を置く場所。
VOICE_AUDIO_DIRNAME = "voice_calls"

#: 通話に使ってよいモデル。UI の選択肢 (frontend/src/components/VoiceCallModal.tsx
#: の MODEL_OPTIONS) と同じ並び。ここに無い名前は start の時点で拒否する
#: — 任意の文字列をそのまま Google へ投げると、課金されるモデルをクライアント
#: 側が自由に選べてしまう。
ALLOWED_MODELS = ("gemini-3.8-live", "gemini-3.8-live-extended-thinking")

#: 考える深さの指定が**必須**のモデルと、通話で使う深さ。extended-thinking は
#: この指定なしに接続すると "Thinking level must be specified for this model."
#: で接続ごと拒否される (2026-09-16 実機で確認)。通話は返事までの間が体感を
#: 決めるので、深さは最小限に寄せる (MINIMAL/LOW/MEDIUM/HIGH のうち LOW)。
MODEL_THINKING_LEVELS: Dict[str, str] = {
    "gemini-3.8-live-extended-thinking": "LOW",
}

#: 声の名前として受け付ける形 (英数と ``.`` ``_`` ``-`` と空白、64 文字まで)。
VOICE_NAME_PATTERN = re.compile(r"[A-Za-z0-9._\- ]{1,64}")
#: 名前の長さの上限 (voice / model 共通)。
MAX_NAME_LENGTH = 64

# 通話開始時に積む建物履歴の件数 (intent の設計判断表: 直近 40 件)。
HISTORY_TURN_LIMIT = 40

# context window compression (sliding window)。
#
# 主語: **この Live セッションが保持できるトークンの総量**。gemini-3.8-live の
# セッション上限は 128,000 トークンで、trigger を超えると古い方から
# target まで畳まれる。system_instruction と先頭の prefix turns は sliding
# window の対象外 (SDK の SlidingWindow docstring)。
#
# 人格プロンプトと Memory Weave だけで数万トークンになるので、128k 上限に対して
# 十分手前で、かつ通話の序盤を巻き込まない位置に置く。
LIVE_SESSION_TOKEN_LIMIT = 128000
COMPRESSION_TRIGGER_TOKENS = 100000
COMPRESSION_TARGET_TOKENS = 64000

#: 文字起こしが 1 件も取れなかった通話に残す一行 (item: 通話の痕跡)。
#: ペルソナへのシステム通告は user ロール + ``<system>`` タグ、が既存の流儀
#: (get_building_messages.py の host 経路と同じ)。
NO_TRANSCRIPT_NOTICE = "(音声通話が行われたが、文字起こしは得られなかった)"

_CALL_MODE_INSTRUCTION = {
    "ja": (
        "## いまの状況\n"
        "あなたはいま、ユーザーと音声で通話している。文字ではなくあなたの声そのもの"
        "が届くので、話し言葉で、短く、間を取りながら応じること。箇条書き・見出し・"
        "記号による装飾は読み上げに乗らないので使わない。身体表現やツールの記法 "
        '({"body_emote": ...} のような JSON) も、声に出すと記号がそのまま読み上げ'
        "られてしまう。通話中はそれらを一切出力せず、言葉だけで話すこと。"
    ),
    "en": (
        "## Right now\n"
        "You are on a live voice call with the user. Your words are heard, not read, "
        "so speak conversationally and keep each turn short. Do not use bullet lists, "
        "headings, or markup — they do not survive being spoken aloud. Do not emit "
        'body-expression or tool syntax (JSON like {"body_emote": ...}) either: on a '
        "call it would be read out loud verbatim. Speak in words only."
    ),
}


class VoiceCallError(RuntimeError):
    """通話を始められない / 続けられないことが確定したときに投げる。

    ``code`` は画面の言語で文言を出すための安定した識別子
    (``docs/intent/voice_call.md`` のエラーコード表)。``str(exc)`` の方は
    ログと開発者向けで、画面にそのまま出す前提の文ではない。
    """

    def __init__(self, message: str, code: str = "call_failed") -> None:
        super().__init__(message)
        self.code = code


#: 同時に通話しているペルソナ。同じペルソナへ 2 本目を張ると、同じ記憶に
#: 二つの声が同時に書き込み、どちらの文脈も相手の発話を知らないまま進む。
_ACTIVE_CALLS: Set[str] = set()

#: 書き戻し専用のワーカー。``asyncio.to_thread`` ではなく自前のプールを使うのは、
#: 「まだ走り出していない仕事だけを取り消す」判定 (concurrent.futures.Future.cancel
#: が True を返す = 一度も実行されていない) が要るため — :meth:`VoiceCallSession.finish`
#: 参照。
_WRITE_BACK_POOL: Optional[ThreadPoolExecutor] = None


def _write_back_pool() -> ThreadPoolExecutor:
    global _WRITE_BACK_POOL
    if _WRITE_BACK_POOL is None:
        _WRITE_BACK_POOL = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="voice-call-writeback",
        )
    return _WRITE_BACK_POOL


# ---------------------------------------------------------------------------
# API キーと Live セッションの口
# ---------------------------------------------------------------------------


def resolve_api_key() -> Optional[str]:
    """通話に使う Gemini の API キーを返す (無ければ None)。

    ``GEMINI_API_KEY`` (有料) を優先し、無ければ ``GEMINI_FREE_API_KEY``。
    ``saiverse/gemini_clients.py`` と同じ二つの環境変数を読むが、通話は
    ユーザーの明示操作でしか始まらない (intent §不変条件 1) ので、
    ``prefer_paid`` の判断をここで固定している。
    """
    return os.getenv("GEMINI_API_KEY") or os.getenv("GEMINI_FREE_API_KEY")


def open_live_session(model: str, config: Dict[str, Any], *, api_key: str):
    """``client.aio.live.connect`` の async context manager を返す。

    テストはこの関数を差し替えて偽の Live セッションを挿す。
    """
    from google import genai

    client = genai.Client(api_key=api_key)
    return client.aio.live.connect(model=model, config=config)


def build_live_config(
    system_instruction: str,
    voice: str,
    *,
    prime_history: bool = False,
    model: str = DEFAULT_MODEL,
) -> Dict[str, Any]:
    """``LiveConnectConfig`` に渡す dict を組む。

    dict のまま渡す (SDK が ``types.LiveConnectConfig(**config)`` で検証する)。
    フィールド名は google-genai 2.21.0 の ``types.LiveConnectConfig`` に一致。

    ``prime_history=True`` のときは ``history_config.initial_history_in_client_content``
    を立てる。SDK の ``HistoryConfig`` docstring は「setup_complete のあと、
    サーバーは ``turn_complete=True`` が来るまで ``client_content`` を初期履歴
    として処理する。この初期履歴はモデルの呼び出しを起こさず、model の発話で
    終わってもよい。``turn_complete=True`` のあとにクライアントは
    ``realtime_input`` で会話を始められる」と明記している。積むだけで返事を
    させたくない履歴は、この形でしか正しく渡せない
    (``turn_complete=False`` のまま音声へ移ると、サーバーは client_content の
    続きを待ち続ける)。
    """
    config: Dict[str, Any] = {
        "response_modalities": ["AUDIO"],
        "system_instruction": system_instruction,
        "speech_config": {
            "voice_config": {"prebuilt_voice_config": {"voice_name": voice}},
        },
        "input_audio_transcription": {},
        "output_audio_transcription": {},
        "context_window_compression": {
            "trigger_tokens": COMPRESSION_TRIGGER_TOKENS,
            "sliding_window": {"target_tokens": COMPRESSION_TARGET_TOKENS},
        },
    }
    thinking_level = MODEL_THINKING_LEVELS.get(model)
    if thinking_level:
        # 指定が必須のモデル (MODEL_THINKING_LEVELS のコメント参照)。指定不要の
        # モデルに付けると逆にエラーになりうるので、必要なモデルにだけ付ける。
        config["thinking_config"] = {"thinking_level": thinking_level}
    if prime_history:
        config["history_config"] = {"initial_history_in_client_content": True}
    return config


# ---------------------------------------------------------------------------
# 文脈の組み立て
# ---------------------------------------------------------------------------


def _persona_dir(persona: Any) -> Optional[str]:
    sai_mem = getattr(persona, "sai_memory", None)
    persona_dir = getattr(sai_mem, "persona_dir", None) if sai_mem is not None else None
    return str(persona_dir) if persona_dir else None


def build_system_instruction(manager: Any, persona: Any, building_id: str) -> str:
    """人格プロンプト + 通話モードの但し書きを返す。

    本体は ``builtin_data/tools/get_system_prompt.py`` の出力そのまま
    (= テキスト会話と同じ人格・同じ建物の説明)。素のモデルで喋らせない
    (intent §不変条件 2)。
    """
    from builtin_data.tools.get_system_prompt import get_system_prompt
    from tools.context import persona_context

    persona_id = getattr(persona, "persona_id", None)
    if not persona_id:
        raise VoiceCallError("persona has no persona_id", code="persona_not_found")

    with persona_context(persona_id, _persona_dir(persona) or "", manager):
        base = get_system_prompt(building_id=building_id)

    language = getattr(persona, "language", None) or "ja"
    suffix = _CALL_MODE_INSTRUCTION.get(language) or _CALL_MODE_INSTRUCTION["ja"]
    return f"{base}\n\n---\n\n{suffix}" if base else suffix


def _speaker_name(manager: Any, persona_id: str) -> str:
    personas = getattr(manager, "all_personas", None) or getattr(manager, "personas", None) or {}
    other = personas.get(persona_id)
    return getattr(other, "persona_name", None) or persona_id


def _building_name(persona: Any, building_id: str) -> str:
    building = (getattr(persona, "buildings", None) or {}).get(building_id)
    return getattr(building, "name", None) or building_id


def _history_text(manager: Any, persona: Any, building_id: str, message: Dict[str, Any]) -> Optional[tuple]:
    """建物メッセージ 1 件を ``(role, text)`` にする (積まないなら None)。

    採否と役割の振り分けは ``builtin_data/tools/get_building_messages.py`` の
    転記規則 (``_ingest_round`` の heard_by 判定 + ``_transcribe_message``) に
    合わせる — ペルソナが自分の記憶を読むときと同じ見え方にするため。

    特に外せない三つ:

    - **heard_by に自分が入っていない行は積まない**。``_ingest_round`` は
      heard_by 外を候補から外す。キー自体が無い古い行も同じ扱い (= 積まない)。
    - **persona_id の無い assistant 行は積まない**。誰の発話か決められない行を
      ``model`` ロールで渡すと、本人が言っていない文が本人の発話に化ける。
    - **host 行の legacy な出入りの通知は落とす** (「オフラインになりました」等)。
      presence は別の経路が扱う概念で、記憶には入れない。
    """
    from saiverse.content_tags import strip_for_other_persona

    persona_id = getattr(persona, "persona_id", None)

    heard_by = message.get("heard_by") or []
    if not isinstance(heard_by, list) or persona_id not in heard_by:
        return None

    role = message.get("role")
    content = message.get("content") or ""
    if not content:
        return None
    speaker_id = message.get("persona_id")

    if role == "assistant":
        if "note-box" in content:
            return None
        if not speaker_id:
            # 誰の発話か決められない行。本人名義には絶対にしない。
            return None
        if speaker_id != persona_id:
            stripped = strip_for_other_persona(content)
            if not stripped:
                return None
            return ("user", f"{_speaker_name(manager, speaker_id)}: {stripped}")
        return ("model", content)

    if role == "user":
        if speaker_id and speaker_id == persona_id:
            return None
        return ("user", content)

    if role == "host":
        metadata = message.get("metadata") or {}
        event = metadata.get("event") or {}
        if event.get("type") == "occupancy" or event.get("entity_type") == "user":
            return None
        if "オフラインになりました" in content or "オンラインになりました" in content:
            return None
        cleaned = re.sub(r"<[^>]+>", "", content).strip()
        if not cleaned:
            return None
        return ("user", f"<system>[{_building_name(persona, building_id)}] {cleaned}</system>")

    return None


def _merge_turns(pairs: Iterable[tuple]) -> List[Dict[str, Any]]:
    """``(role, text)`` の並びを Content dict に畳む (同じ role は 1 ターンに結合)。

    Live API に渡す prefix は user / model が交互になっている方が素直なので、
    連続する同 role は改行で束ねる。
    """
    turns: List[Dict[str, Any]] = []
    for role, text in pairs:
        if turns and turns[-1]["role"] == role:
            turns[-1]["parts"][0]["text"] += "\n" + text
        else:
            turns.append({"role": role, "parts": [{"text": text}]})
    return turns


def build_history_turns(
    manager: Any,
    persona: Any,
    building_id: str,
    *,
    limit: int = HISTORY_TURN_LIMIT,
) -> List[Dict[str, Any]]:
    """通話開始時に積む履歴ターン (Memory Weave + 建物履歴の直近 ``limit`` 件)。"""
    pairs: List[tuple] = []

    # 1. Memory Weave (Chronicle)。読めなくても通話は始める。
    try:
        from builtin_data.tools.get_memory_weave_context import get_memory_weave_context
        from tools.context import persona_context

        persona_id = getattr(persona, "persona_id", None)
        persona_dir = _persona_dir(persona)
        if persona_id and persona_dir:
            with persona_context(persona_id, persona_dir, manager):
                for msg in get_memory_weave_context(persona_id=persona_id, persona_dir=persona_dir):
                    text = (msg or {}).get("content")
                    if text:
                        pairs.append(("user", text))
    except Exception:
        LOGGER.warning("[voice_call] memory weave unavailable — continuing without it", exc_info=True)

    # 2. 建物履歴の直近 limit 件。
    try:
        from database.building_messages import fetch_building_messages

        recent = fetch_building_messages(
            getattr(manager, "SessionLocal", None), building_id, limit=limit,
        )
    except Exception:
        LOGGER.warning("[voice_call] failed to read building history for %s", building_id, exc_info=True)
        recent = []

    for message in recent or []:
        entry = _history_text(manager, persona, building_id, message)
        if entry:
            pairs.append(entry)

    return _merge_turns(pairs)


# ---------------------------------------------------------------------------
# 文字起こしの蓄積
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CallTranscript:
    """入力 (ユーザー) と出力 (ペルソナ) の文字起こしと音声を発話単位に束ねる。

    Live API の文字起こしも音声も細切れで届くので、turn の境界
    (``turn_complete`` / ``interrupted``) で 1 件に確定させる。文字起こしと
    音声の区切りは**同じ**で、一つの区切りが :attr:`audio_segments` の 1 要素
    (``{"index": 連番, "user": bytes, "persona": bytes}``) になる。

    文字起こしが取れなかった区切りは :attr:`entries` を増やさないが、音声が
    あれば区切りとしては残り、連番も消費する — 声は記憶の素材なので、文字に
    ならなかった分も捨てない (intent の設計判断表「音声の保存」)。
    """

    def __init__(self) -> None:
        self.entries: List[Dict[str, Any]] = []
        #: 音声のある区切り。``index`` は 1 から始まる連番で、ファイル名の NNN。
        self.audio_segments: List[Dict[str, Any]] = []
        self._input_parts: List[str] = []
        self._output_parts: List[str] = []
        self._input_started_at: Optional[str] = None
        self._output_started_at: Optional[str] = None
        self._input_audio: List[bytes] = []
        self._output_audio: List[bytes] = []

    def add_input(self, text: str) -> None:
        if not text:
            return
        if not self._input_parts:
            self._input_started_at = _now_iso()
        self._input_parts.append(text)

    def add_output(self, text: str) -> None:
        if not text:
            return
        if not self._output_parts:
            self._output_started_at = _now_iso()
        self._output_parts.append(text)

    def add_input_audio(self, data: bytes) -> None:
        """ユーザーのマイク音声 (16kHz PCM16 LE mono) の 1 フレームを溜める。"""
        if data:
            self._input_audio.append(data)

    def add_output_audio(self, data: bytes) -> None:
        """ペルソナの音声 (24kHz PCM16 LE mono) の 1 フレームを溜める。"""
        if data:
            self._output_audio.append(data)

    def flush_turn(self) -> None:
        """溜まっている断片を発話 1 件ずつに確定する (ユーザー → ペルソナの順)。

        音声が片方でもあれば、この区切りに連番を一つ与えて
        :attr:`audio_segments` に積む。その区切りから起きた行には、対応する
        wav のファイル名を ``entry["audio_file"]`` として持たせる
        (ペルソナフォルダからの相対パスに組み立てるのは
        :meth:`VoiceCallSession._pending_entries` 側)。
        """
        user_text = "".join(self._input_parts).strip()
        assistant_text = "".join(self._output_parts).strip()
        user_audio = b"".join(self._input_audio)
        persona_audio = b"".join(self._output_audio)

        index: Optional[int] = None
        if user_audio or persona_audio:
            index = len(self.audio_segments) + 1
            self.audio_segments.append({
                "index": index,
                "user": user_audio,
                "persona": persona_audio,
            })

        if user_text:
            entry: Dict[str, Any] = {
                "role": "user",
                "content": user_text,
                "timestamp": self._input_started_at or _now_iso(),
            }
            if index is not None and user_audio:
                entry["audio_file"] = audio_file_name(index, "user")
            self.entries.append(entry)
        if assistant_text:
            entry = {
                "role": "assistant",
                "content": assistant_text,
                "timestamp": self._output_started_at or _now_iso(),
            }
            if index is not None and persona_audio:
                entry["audio_file"] = audio_file_name(index, "persona")
            self.entries.append(entry)

        self._input_parts = []
        self._output_parts = []
        self._input_started_at = None
        self._output_started_at = None
        self._input_audio = []
        self._output_audio = []

    def __len__(self) -> int:
        return len(self.entries)


# ---------------------------------------------------------------------------
# 音声の保存
# ---------------------------------------------------------------------------


def audio_file_name(index: int, side: str) -> str:
    """区切りの連番と向き (``user`` / ``persona``) から wav のファイル名を作る。"""
    return f"{index:03d}_{side}.wav"


def _write_wav(path: str, pcm: bytes, sample_rate: int) -> None:
    """生の PCM16 LE mono に wav のヘッダを被せて書き出す。"""
    with wave.open(path, "wb") as wav_file:
        wav_file.setnchannels(AUDIO_CHANNELS)
        wav_file.setsampwidth(AUDIO_SAMPLE_WIDTH)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm)


def save_call_audio(
    persona: Any,
    call_dir_name: str,
    segments: Sequence[Dict[str, Any]],
) -> Set[str]:
    """通話の音声を ``<persona_dir>/voice_calls/<call_dir_name>/`` へ書き出す。

    戻り値は**実際に書けたファイル名の集合**。呼び出し側はこれを使って、
    書けなかったファイルへの紐 (``metadata["voice_audio"]``) を記録に残さない
    ようにする — 存在しないファイルを指す紐は、後から聞き返そうとした人に
    「あるはずのものが無い」と言わせる嘘になる。

    ペルソナフォルダが取れないとき (SAIMemory が無い等) は音声の保存だけ
    諦める。文字起こしの書き戻しはこの関数の外なので、従来どおり進む。
    """
    persona_dir = _persona_dir(persona)
    if not persona_dir:
        LOGGER.warning(
            "[voice_call] no persona directory for persona=%s — keeping the transcript "
            "but dropping %d recorded audio segment(s)",
            getattr(persona, "persona_id", None), len(segments),
        )
        return set()

    target_dir = os.path.join(persona_dir, VOICE_AUDIO_DIRNAME, call_dir_name)
    try:
        os.makedirs(target_dir, exist_ok=True)
    except OSError:
        LOGGER.warning("[voice_call] could not create %s", target_dir, exc_info=True)
        return set()

    saved: Set[str] = set()
    for segment in segments:
        index = segment["index"]
        for side, sample_rate in (
            ("user", INPUT_SAMPLE_RATE),
            ("persona", OUTPUT_SAMPLE_RATE),
        ):
            pcm = segment.get(side)
            if not pcm:
                continue
            name = audio_file_name(index, side)
            try:
                _write_wav(os.path.join(target_dir, name), pcm, sample_rate)
            except (OSError, wave.Error):
                LOGGER.warning(
                    "[voice_call] failed to save %s in %s", name, target_dir, exc_info=True,
                )
                continue
            saved.add(name)

    LOGGER.info(
        "[voice_call] saved %d of %d audio file(s) under %s",
        len(saved), sum(bool(s.get("user")) + bool(s.get("persona")) for s in segments),
        target_dir,
    )
    return saved


# ---------------------------------------------------------------------------
# 書き戻し
# ---------------------------------------------------------------------------


def resolve_thread_suffix(adapter: Any, persona_id: str) -> Optional[str]:
    """実会話 (``SEARuntime._store_memory``) と同じ規則で thread_suffix を決める。

    **通話の開始時に一度だけ**呼ぶこと。書き戻し (通話終了時) に呼ぶと、その
    瞬間に Pulse が Stelis のサブスレッドへ切り替えていた場合、通話の全発話が
    別スレッドへ入り、本線の提示列から丸ごと消える。
    """
    if adapter is None or not adapter.is_ready() or not persona_id:
        return None
    current_thread = adapter.get_current_thread()
    if current_thread is None:
        default_thread = f"{persona_id}:{adapter._PERSONA_THREAD_SUFFIX}"
        adapter.set_active_thread(default_thread)
        current_thread = default_thread
    return current_thread.split(":", 1)[1] if ":" in current_thread else current_thread


def _conversation_partners(persona: Any, manager: Any, building_id: str) -> List[str]:
    """``metadata["with"]`` に入れる相手 (``emit_speak`` と同じ規則)。

    同席しているペルソナ (自分を除く) を並べ、そこへ ``"user"`` を足す。
    ``emit_speak`` は ``user_presence_status`` が online / away のときだけ
    ``"user"`` を足すが、通話が成立している時点でユーザーはその場にいるので
    ここでは常に足す (presence の更新が遅れていても取りこぼさない)。
    **同席しているペルソナを落とさない**のがここの要点 — 落とすと「誰と話して
    いた記憶なのか」が後から引けなくなる。
    """
    persona_id = getattr(persona, "persona_id", None)
    occupants = (getattr(manager, "occupants", None) or {}).get(building_id, []) or []
    partners = [str(oid) for oid in occupants if oid and str(oid) != str(persona_id)]
    if "user" not in partners:
        partners.append("user")
    return partners


def _memory_message(
    entry: Dict[str, Any],
    persona: Any,
    manager: Any,
    building_id: str,
) -> Dict[str, Any]:
    """SAIMemory へ渡す message dict を実会話と同じ形で組む。

    - ペルソナ行 (``assistant``): ``sea/runtime_emitters.py`` の ``emit_speak``
      が組む ``{"role", "content", "persona_id", "metadata": {"tags": [...],
      "with": [...]}}`` と同形。``with`` は同席者 + ユーザー。
    - ユーザー行 (``user``): 建物履歴からの転記 (``get_building_messages.py`` の
      ``_transcribe_message``) が組む ``metadata["with"] = ["user"]`` 付きの
      user 行と同形。
    - 通話の痕跡の一行 (``entry["system"]``): host 経路の転記と同じく
      user ロール + ``<system>`` タグ + ``tags=["internal", "event_message"]``。

    ``line_role`` / ``scope`` は message dict の top-level に明示する
    (``saiverse_memory.adapter._append_message`` がそこから読む)。転記経路の
    ``_stamp_layer0`` と同じ値 — NULL の救済を持たない完全一致 SQL に構造的に
    落とされないため。

    ``entry["voice_audio"]`` (ペルソナフォルダからの相対パス) があれば、その
    まま ``metadata["voice_audio"]`` に刻む。音声の無い発話には付けない。
    """
    role = entry["role"]
    metadata: Dict[str, Any] = {
        "tags": ["conversation"],
        "voice_call": True,
    }
    if entry.get("voice_audio"):
        metadata["voice_audio"] = entry["voice_audio"]
    message: Dict[str, Any] = {
        "role": role,
        "content": entry["content"],
        "timestamp": entry["timestamp"],
        "line_role": "main_line",
        "scope": "committed",
    }
    if entry.get("system"):
        metadata["tags"] = ["internal", "event_message"]
    elif role == "assistant":
        message["persona_id"] = getattr(persona, "persona_id", None)
        metadata["with"] = _conversation_partners(persona, manager, building_id)
    else:
        # ユーザーの声の自動文字起こし。本人がタイプした文ではないことを残す。
        metadata["voice_transcript"] = True
        metadata["with"] = ["user"]
    message["metadata"] = metadata
    return message


def _building_entry(
    entry: Dict[str, Any],
    persona: Any,
    manager: Any,
    building_id: str,
    *,
    ingested: bool,
) -> Dict[str, Any]:
    """建物履歴へ渡す行を、通常の会話と同じ形で組む。

    ``heard_by`` は通常の発話と同じく「その場にいた全員」。

    ``ingested_by`` は **SAIMemory への書き込みが成功した行にだけ**通話相手の
    ペルソナを入れる。成功した行にこの印があると、建物履歴の自動転記
    (``get_building_messages.py``) が同じ発話をもう一度ペルソナの記憶へ書くのを
    防げる。逆に、記憶へ入らなかった行にまで印を打つと、その行は自動転記から
    **永久に**外れ、通話の内容がペルソナの記憶に一度も届かない。
    同席していた他のペルソナは、どちらの場合も通常どおり転記される。
    """
    persona_id = getattr(persona, "persona_id", None)
    occupants = list((getattr(manager, "occupants", None) or {}).get(building_id, []))
    heard = {str(oid) for oid in occupants if oid}
    if persona_id:
        heard.add(str(persona_id))
    user_id = getattr(getattr(manager, "state", None), "user_id", None)
    if user_id is not None:
        heard.add(str(user_id))

    message = _memory_message(entry, persona, manager, building_id)
    row: Dict[str, Any] = {
        "role": message["role"],
        "content": message["content"],
        "timestamp": message["timestamp"],
        "metadata": dict(message["metadata"]),
        "heard_by": sorted(heard),
        "ingested_by": [str(persona_id)] if (ingested and persona_id) else [],
    }
    if message.get("persona_id"):
        row["persona_id"] = message["persona_id"]
    return row


def write_back_transcript(
    manager: Any,
    persona: Any,
    building_id: str,
    entries: Sequence[Dict[str, Any]],
    *,
    thread_suffix: Optional[str] = None,
) -> Dict[str, int]:
    """通話の文字起こしを SAIMemory と建物履歴へ**追記**する。

    既存メッセージの改変・挿入は一切しない (intent §不変条件 3)。
    ``thread_suffix`` は通話開始時に確定したもの (:func:`resolve_thread_suffix`)。
    戻り値は ``{"memory": 書けた件数, "building": 書けた件数}``。
    """
    written = {"memory": 0, "building": 0}
    if not entries:
        return written

    persona_id = getattr(persona, "persona_id", None)
    adapter = getattr(persona, "sai_memory", None)
    if adapter is None or thread_suffix is None:
        LOGGER.warning(
            "[voice_call] SAIMemory unavailable for persona=%s — the call transcript "
            "will only reach the building history (and stays open for the normal "
            "building→memory ingest)", persona_id,
        )

    session_factory = getattr(manager, "SessionLocal", None)
    for entry in entries:
        memory_ok = False
        if adapter is not None and thread_suffix is not None:
            try:
                stored = adapter.append_persona_message(
                    _memory_message(entry, persona, manager, building_id),
                    thread_suffix=thread_suffix,
                )
                if stored:
                    memory_ok = True
                    written["memory"] += 1
                else:
                    LOGGER.warning(
                        "[voice_call] SAIMemory refused the row (role=%s) — leaving it "
                        "open for the building→memory ingest", entry.get("role"),
                    )
            except Exception:
                LOGGER.warning("[voice_call] SAIMemory append failed (role=%s)", entry.get("role"), exc_info=True)
        if session_factory is not None:
            try:
                from database.building_messages import insert_building_message

                saved = insert_building_message(
                    session_factory, building_id,
                    _building_entry(entry, persona, manager, building_id, ingested=memory_ok),
                )
                if saved:
                    written["building"] += 1
            except Exception:
                LOGGER.warning("[voice_call] building history insert failed (role=%s)", entry.get("role"), exc_info=True)

    LOGGER.info(
        "[voice_call] transcript written back persona=%s building=%s memory=%d building_rows=%d of %d utterances",
        persona_id, building_id, written["memory"], written["building"], len(entries),
    )
    return written


def persist_call(
    manager: Any,
    persona: Any,
    building_id: str,
    entries: Sequence[Dict[str, Any]],
    *,
    thread_suffix: Optional[str] = None,
    call_dir_name: str,
    segments: Sequence[Dict[str, Any]] = (),
) -> Dict[str, int]:
    """通話の音声を保存してから、文字起こしを書き戻す (ワーカースレッドの仕事)。

    音声を**先に**書くのは、SAIMemory への書き込みが失敗しても声そのものは
    手元に残るようにするため。逆順にすると、書き戻しが例外で落ちたときに
    その通話の音声ごと消える。

    ``entry["audio_file"]`` は、そのファイルが実際に書けた場合にだけ
    ``entry["voice_audio"]`` (ペルソナフォルダからの相対パス) へ組み替える。
    """
    saved = save_call_audio(persona, call_dir_name, segments) if segments else set()

    rows: List[Dict[str, Any]] = []
    for entry in entries:
        row = dict(entry)
        name = row.pop("audio_file", None)
        if name and name in saved:
            row["voice_audio"] = f"{VOICE_AUDIO_DIRNAME}/{call_dir_name}/{name}"
        rows.append(row)

    return write_back_transcript(
        manager, persona, building_id, rows, thread_suffix=thread_suffix,
    )


# ---------------------------------------------------------------------------
# セッション
# ---------------------------------------------------------------------------


class VoiceCallSession:
    """通話 1 回分。ブラウザの WebSocket と Gemini Live セッションを中継する。

    プロトコルは ``docs/intent/voice_call.md`` と frontend の実装で共有する契約:

    - クライアント → サーバー: 最初に ``{"type":"start", ...}``、以後は
      16kHz PCM16 LE mono のバイナリフレーム、終了は ``{"type":"end"}``
    - サーバー → クライアント: ``{"type":"ready"}`` のあと 24kHz PCM16 LE mono
      のバイナリフレームと ``input_transcript`` / ``output_transcript`` /
      ``interrupted`` / ``turn_complete`` / ``error`` / ``call_ended`` の JSON
    """

    def __init__(
        self,
        manager: Any,
        persona_id: str,
        client_building_id: Optional[str] = None,
        *,
        voice: str = DEFAULT_VOICE,
        model: str = DEFAULT_MODEL,
    ) -> None:
        self.manager = manager
        self.persona_id = persona_id
        self.voice = _validate_voice(voice)
        self.model = _validate_model(model)
        self.transcript = CallTranscript()
        self.started_at = datetime.now()
        #: 音声を置くフォルダ名。通話の**開始時刻**でここで確定させる
        #: (終了時に取り直すと、フォルダ名が通話の終わった時刻になる)。
        #: ログのセッションフォルダと同じくローカル時刻。
        self.call_dir_name = self.started_at.strftime("%Y%m%d_%H%M%S")
        self.usage: Dict[str, int] = {"prompt_tokens": 0, "response_tokens": 0, "total_tokens": 0}
        self._written_back = False
        self._holds_slot = False
        self._thread_suffix: Optional[str] = None
        self._ready = False
        self._relayed_audio_frames = 0

        personas = getattr(manager, "personas", None) or {}
        persona = personas.get(persona_id)
        if persona is None:
            raise VoiceCallError(
                f"ペルソナ {persona_id} が見つかりません", code="persona_not_found",
            )
        self.persona = persona

        # 建物はサーバー側の真実で決める。クライアントの申告は**使わない** —
        # メニューを開いてから通話を始めるまでの間にペルソナが移動していると、
        # 本人のいない部屋に本人名義の行が立つ。
        resolved = getattr(persona, "current_building_id", None)
        if not resolved:
            raise VoiceCallError(
                f"ペルソナ {persona_id} の現在地が分かりません",
                code="persona_location_unknown",
            )
        self.building_id = str(resolved)
        if client_building_id and str(client_building_id) != self.building_id:
            LOGGER.info(
                "[voice_call] client asked for building=%s but persona=%s is in %s — using the persona's room",
                client_building_id, persona_id, self.building_id,
            )

    # -- 中継 --------------------------------------------------------------

    def usage_totals(self) -> Dict[str, int]:
        """通話中に積み上がったトークン使用量 (課金の透明性のため画面へも返す)。"""
        return dict(self.usage)

    async def run(self, websocket: Any) -> None:
        """Live セッションを張り、切断まで中継し、必ず書き戻して終わる。"""
        if self.persona_id in _ACTIVE_CALLS:
            raise VoiceCallError(
                f"ペルソナ {self.persona_id} は既に別の通話中です", code="already_in_call",
            )
        api_key = resolve_api_key()
        if not api_key:
            raise VoiceCallError(
                "Gemini の API キーが設定されていません (GEMINI_API_KEY / GEMINI_FREE_API_KEY)",
                code="no_api_key",
            )

        _ACTIVE_CALLS.add(self.persona_id)
        self._holds_slot = True
        try:
            # 記憶のスレッドは**通話開始のこの時点で**確定させる。書き戻しの
            # 瞬間に読むと、そのとき Pulse がサブスレッドへ切り替えていた場合に
            # 通話全体が別スレッドへ落ちる。
            self._thread_suffix = await asyncio.to_thread(
                resolve_thread_suffix, getattr(self.persona, "sai_memory", None), self.persona_id,
            )

            # 人格プロンプトと履歴の組み立ては DB を読む (= 同期・ブロッキング)。
            # バックエンドは同じループで他のリクエストも捌いているので、通話の
            # 支度でループを止めない。
            system_instruction = await asyncio.to_thread(
                build_system_instruction, self.manager, self.persona, self.building_id,
            )
            turns = await asyncio.to_thread(
                build_history_turns, self.manager, self.persona, self.building_id,
            )
            config = build_live_config(
                system_instruction, self.voice,
                prime_history=bool(turns), model=self.model,
            )

            LOGGER.info(
                "[voice_call] starting persona=%s building=%s model=%s voice=%s history_turns=%d thread=%s",
                self.persona_id, self.building_id, self.model, self.voice, len(turns),
                self._thread_suffix,
            )

            async with open_live_session(self.model, config, api_key=api_key) as live:
                if turns:
                    # history_config.initial_history_in_client_content が立って
                    # いるので、この turn_complete=True は「返事をしろ」ではなく
                    # 「初期履歴はここまで」の合図 (SDK の HistoryConfig
                    # docstring)。これを送って初めて realtime_input が通る。
                    await live.send_client_content(turns=turns, turn_complete=True)
                await websocket.send_json({"type": "ready"})
                self._ready = True

                uplink = asyncio.create_task(self._pump_client_to_live(websocket, live))
                downlink = asyncio.create_task(self._pump_live_to_client(websocket, live))
                finished: set = set()
                results: List[Any] = []
                try:
                    finished, _pending = await asyncio.wait(
                        {uplink, downlink}, return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    for task in (uplink, downlink):
                        if not task.done():
                            task.cancel()
                    results = await asyncio.gather(uplink, downlink, return_exceptions=True)
                self._raise_pump_failure((uplink, downlink), finished, results)
        finally:
            try:
                await self.finish()
            except Exception:
                # 書き戻しの失敗で、通話が切れた本当の理由を上書きしない。
                LOGGER.warning("[voice_call] write-back failed on the way out", exc_info=True)

    @staticmethod
    def _raise_pump_failure(tasks: Tuple[Any, ...], finished: set, results: List[Any]) -> None:
        """中継タスクの失敗を握り潰さず、代表 1 つを投げ直す。

        先に終わった方 (``finished``) の例外が「通話が切れた理由」。取り消された
        側の例外も WARN で残す — 消すと、原因が二段構えのときに片方しか見えない。
        """
        failures = [
            (task, result)
            for task, result in zip(tasks, results)
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError)
        ]
        if not failures:
            return
        for task, result in failures:
            if task not in finished:
                LOGGER.warning(
                    "[voice_call] the cancelled relay also failed: %r", result, exc_info=result,
                )
        primary = next(
            (result for task, result in failures if task in finished), failures[0][1],
        )
        raise primary

    async def _pump_client_to_live(self, websocket: Any, live: Any) -> None:
        """ブラウザ → Gemini。バイナリは音声、テキストは制御 JSON。"""
        while True:
            event = await websocket.receive()
            if event.get("type") == "websocket.disconnect":
                LOGGER.info("[voice_call] client disconnected persona=%s", self.persona_id)
                return
            data = event.get("bytes")
            if data:
                await live.send_realtime_input(
                    audio={"data": data, "mime_type": INPUT_MIME_TYPE},
                )
                # Gemini へ送ったのと同じバイトを手元にも溜める (通話終了時に
                # wav にする)。ここで溜めたものがユーザーの声の原本になる。
                self.transcript.add_input_audio(data)
                self._relayed_audio_frames += 1
                continue
            text = event.get("text")
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                LOGGER.warning("[voice_call] ignoring non-JSON text frame")
                continue
            if isinstance(payload, dict) and payload.get("type") == "end":
                LOGGER.info("[voice_call] client requested end persona=%s", self.persona_id)
                return

    async def _pump_live_to_client(self, websocket: Any, live: Any) -> None:
        """Gemini → ブラウザ。音声はバイナリ、文字起こしと状態は JSON。

        SDK の ``session.receive()`` は**モデルの発話一巡 (turn_complete) で
        終わる** async iterator (live.py の実装が turn_complete で break する)。
        一巡ごとに受信を張り直さないと、ペルソナが一言喋った時点で通話が
        「終わった」ことになる (2026-09-16 の初通話で実際に起きた)。
        接続そのものが閉じたときは、一巡が 0 件で終わるのでそこで抜ける。
        """
        while True:
            received_any = False
            async for message in live.receive():
                received_any = True
                await self._handle_live_message(websocket, message)
            if not received_any:
                LOGGER.info(
                    "[voice_call] live session closed by the server persona=%s",
                    self.persona_id,
                )
                return

    async def _handle_live_message(self, websocket: Any, message: Any) -> None:
        usage = getattr(message, "usage_metadata", None)
        if usage is not None:
            self._accumulate_usage(usage)

        go_away = getattr(message, "go_away", None)
        if go_away is not None:
            time_left = getattr(go_away, "time_left", None)
            LOGGER.warning(
                "[voice_call] server is ending the session persona=%s time_left=%s",
                self.persona_id, time_left,
            )
            await websocket.send_json({
                "type": "error",
                "code": "session_ending",
                "message": f"Gemini Live session is about to end (time_left={time_left})",
            })

        server_content = getattr(message, "server_content", None)
        if server_content is None:
            return

        input_tr = server_content.input_transcription
        if input_tr is not None and input_tr.text:
            self.transcript.add_input(input_tr.text)
            await websocket.send_json({"type": "input_transcript", "text": input_tr.text})

        output_tr = server_content.output_transcription
        if output_tr is not None and output_tr.text:
            self.transcript.add_output(output_tr.text)
            await websocket.send_json({"type": "output_transcript", "text": output_tr.text})

        model_turn = server_content.model_turn
        if model_turn is not None and model_turn.parts:
            for part in model_turn.parts:
                inline = getattr(part, "inline_data", None)
                if inline is not None and inline.data:
                    await websocket.send_bytes(inline.data)
                    # ブラウザで鳴るのと同じバイトを手元にも溜める。
                    self.transcript.add_output_audio(inline.data)

        if server_content.interrupted:
            self.transcript.flush_turn()
            await websocket.send_json({"type": "interrupted"})

        if server_content.turn_complete:
            self.transcript.flush_turn()
            await websocket.send_json({"type": "turn_complete"})

    def _accumulate_usage(self, usage: Any) -> None:
        """Live のサーバーターンごとに届く使用量を通話単位で足し込む。"""
        for key, field in (
            ("prompt_tokens", "prompt_token_count"),
            ("response_tokens", "response_token_count"),
            ("total_tokens", "total_token_count"),
        ):
            value = getattr(usage, field, None)
            if isinstance(value, int):
                self.usage[key] += value

    # -- 後始末 ------------------------------------------------------------

    def _pending_entries(self) -> List[Dict[str, Any]]:
        """書き戻す行を確定する (文字起こしゼロの通話には痕跡の一行を立てる)。

        最後の :meth:`CallTranscript.flush_turn` でもある通り、turn_complete を
        受け取らないまま切れた区切りの文字起こしと音声もここで確定する。
        痕跡の一行には音声への紐を付けない — その一行は特定の発話ではなく
        「通話があった」という事実なので、通話中のどの区切りとも対応しない。
        """
        self.transcript.flush_turn()
        entries = list(self.transcript.entries)
        if entries:
            return entries
        if self._ready and self._relayed_audio_frames > 0:
            # 声は確かに流れたのに一文字も起こせなかった通話。「通話があった」
            # という事実だけは残す — 何も残さないと、ペルソナから見て
            # その時間は存在しなかったことになる。
            return [{
                "role": "user",
                "content": f"<system>{NO_TRANSCRIPT_NOTICE}</system>",
                "timestamp": _now_iso(),
                "system": True,
            }]
        return []

    def _release_slot(self) -> None:
        if self._holds_slot:
            _ACTIVE_CALLS.discard(self.persona_id)
            self._holds_slot = False

    async def finish(self) -> Dict[str, int]:
        """書き戻しをちょうど一度だけ行う (正常終了・切断・例外すべての経路から)。"""
        if self._written_back:
            return {"memory": 0, "building": 0}
        self._written_back = True
        LOGGER.info(
            "[voice_call] token usage persona=%s prompt=%d response=%d total=%d",
            self.persona_id, self.usage["prompt_tokens"],
            self.usage["response_tokens"], self.usage["total_tokens"],
        )
        entries = self._pending_entries()
        segments = self.transcript.audio_segments
        if not entries and not segments:
            LOGGER.info(
                "[voice_call] nothing to write back persona=%s building=%s",
                self.persona_id, self.building_id,
            )
            self._release_slot()
            return {"memory": 0, "building": 0}

        # 書き戻す行が 1 件も無くても、音声の区切りがあれば保存はする
        # (文字起こしが一度も取れなかった通話の声を捨てない)。
        args = (self.manager, self.persona, self.building_id, entries)
        kwargs = {
            "thread_suffix": self._thread_suffix,
            "call_dir_name": self.call_dir_name,
            "segments": segments,
        }
        try:
            try:
                future = _write_back_pool().submit(persist_call, *args, **kwargs)
            except RuntimeError:
                # インタプリタ終了中などでワーカーを起こせない場合でも、
                # 文字起こしと音声を落とすよりはこのスレッドで書き切る。
                LOGGER.warning("[voice_call] falling back to a synchronous write-back", exc_info=True)
                return persist_call(*args, **kwargs)
            try:
                return await asyncio.shield(asyncio.wrap_future(future))
            except asyncio.CancelledError:
                # 待っている側が取り消されても、書き戻しは落とさない。
                # cancel() が True = ワーカーはまだ一度も走っていない
                # (= 二重書き込みにならない) ので、ここで同期に書き切る。
                if future.cancel():
                    LOGGER.warning(
                        "[voice_call] write-back was cancelled before it started — "
                        "writing synchronously instead",
                    )
                    persist_call(*args, **kwargs)
                else:
                    LOGGER.warning(
                        "[voice_call] write-back is already running in the worker — "
                        "letting it finish",
                    )
                raise
        finally:
            self._release_slot()


def _validate_voice(voice: Optional[str]) -> str:
    """声の名前を検証する (長さ・使える文字)。"""
    value = (voice or DEFAULT_VOICE).strip()
    if not value:
        return DEFAULT_VOICE
    if len(value) > MAX_NAME_LENGTH or not VOICE_NAME_PATTERN.fullmatch(value):
        raise VoiceCallError(f"声の名前が不正です: {value!r}", code="invalid_voice")
    return value


def _validate_model(model: Optional[str]) -> str:
    """モデル名を許可リストで検証する。"""
    value = (model or DEFAULT_MODEL).strip()
    if not value:
        return DEFAULT_MODEL
    if len(value) > MAX_NAME_LENGTH or value not in ALLOWED_MODELS:
        raise VoiceCallError(f"通話に使えないモデルです: {value!r}", code="invalid_model")
    return value


__all__ = [
    "ALLOWED_MODELS",
    "AUDIO_CHANNELS",
    "AUDIO_SAMPLE_WIDTH",
    "CallTranscript",
    "DEFAULT_MODEL",
    "DEFAULT_VOICE",
    "HISTORY_TURN_LIMIT",
    "INPUT_MIME_TYPE",
    "INPUT_SAMPLE_RATE",
    "LIVE_SESSION_TOKEN_LIMIT",
    "MODEL_THINKING_LEVELS",
    "NO_TRANSCRIPT_NOTICE",
    "OUTPUT_SAMPLE_RATE",
    "VOICE_AUDIO_DIRNAME",
    "VoiceCallError",
    "VoiceCallSession",
    "audio_file_name",
    "build_history_turns",
    "build_live_config",
    "build_system_instruction",
    "open_live_session",
    "persist_call",
    "resolve_api_key",
    "resolve_thread_suffix",
    "save_call_audio",
    "write_back_transcript",
]
