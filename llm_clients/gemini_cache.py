"""Gemini explicit cache controller (Phase 3 / M1)。

Anthropic の cache_control (ステートレス) と違い、Gemini の explicit cache は
``client.caches.create()`` でリソース (cache.name) を作り、``generate_content`` 時に
``cached_content=name`` で参照、不要になったら ``caches.delete()`` する **ステートフル**
な機構。本コントローラはそのライフサイクル (作成 / 再利用 / 失効判定) を担う。

M1 スコープ: 「Gemini で実際に cache hit する最小配線」。
- キャッシュ対象は **system_instruction (= head)**。head_pipeline が安定を保証するので
  内容ハッシュで同一性を判定し、生きていれば再利用する。
- key = ``(model, sha256(system_instruction))``。同一 head は同一 cache に集約。
- 最小トークン (Flash: 1024) 未満は create が失敗するので silent fallback (None 返し)。

未実装 (後続 M):
- M2: orphan cleanup (起動時 list → delete)、head 変更 (metabolism) での明示 invalidate
- M3: persona/line 紐付け (timer 表示用)、expire_time の正確な反映
- M4: 標準モードの pulse 終了 delete

継続キャッシュ (高速応答モード、§3.5) もここで管理する: キャッシュごとに「先頭から
何件を入れたか」と、その範囲の中身全体の署名 (``prefix_digests``) を覚え、今回の並びの
先頭と一致する最長のキャッシュを探す (``continuous_*`` メソッド群)。

設計: docs/intent/cache_lifecycle_control.md §3.5 / §4.5 / §7 Phase 3
"""
from __future__ import annotations

import enum
import hashlib
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

LOGGER = logging.getLogger(__name__)

# SAIVerse 由来の cache を識別する displayName prefix (orphan cleanup 用)。
DISPLAY_NAME_PREFIX = "saiverse:"
# create 失敗を避けるための最小トークン (Gemini Flash 系の下限)。
DEFAULT_MIN_TOKENS = 1024
# expire 直前の再利用を避けるための安全マージン (秒)。
_EXPIRY_SAFETY_MARGIN = 10.0
# 継続キャッシュの安全マージン (秒)。継続キャッシュは返事の生成 (音声会話の実測で
# 13〜25 秒) のあいだ参照され続けるので、自動キャッシュの 10 秒より広く取る。
_CONTINUOUS_EXPIRY_SAFETY_MARGIN = 30.0
# 同じ範囲を別の呼び出しが作成中のとき、同期作成が待つ上限 (秒)。
_CONTINUOUS_INFLIGHT_WAIT_SECONDS = 120.0

_TTL_RE = re.compile(r"^\s*(\d+)\s*([smh])\s*$", re.IGNORECASE)


def parse_ttl_seconds(ttl: Any, default: int = 300) -> int:
    """"5m" / "15m" / "30m" / "1h" / "300s" 等を秒に変換する。

    数値ならそのまま秒として扱う。解釈不能なら ``default``。
    """
    if isinstance(ttl, (int, float)):
        return int(ttl)
    if not isinstance(ttl, str):
        return default
    m = _TTL_RE.match(ttl)
    if not m:
        return default
    value = int(m.group(1))
    unit = m.group(2).lower()
    return value * {"s": 1, "m": 60, "h": 3600}[unit]


@dataclass
class CacheEnsureResult:
    """ensure() の戻り値。呼び出し側が storage 計上に必要な情報を持つ。"""
    name: Optional[str]       # cache resource name (None = 作成失敗/スキップ)
    created: bool = False     # このコールで新規作成したか (reuse 時 False)
    cached_tokens: int = 0    # create 時の total_token_count (storage 計上用)
    ttl_seconds: int = 0      # 設定した TTL 秒数


@dataclass
class _CacheEntry:
    name: str           # Gemini cache resource name (cached_content に渡す値)
    model: str
    expire_at: float    # epoch seconds (再利用可否判定用)
    ttl_seconds: int


# ── 継続キャッシュ (高速応答モード) の一致判定 ──
# docs/intent/cache_lifecycle_control.md §3.5「一致の判定」。キャッシュに入れた範囲の
# 中身全体と、今回の並びの先頭の同じ範囲を比べる。画像・音声・動画はバイト列そのもの
# (長さではなく中身)、ツール呼び出しの往復、思考署名も含め、Content が持つ全ての
# フィールドを署名に入れる。合わないキャッシュを使うことが無いように、未知の part 種別も
# model_dump で丸ごと入れる。


def _hash_value(h: Any, value: Any) -> None:
    """``value`` を型の印つきで ``h`` (hashlib) へ流し込む (決定的な直列化)。"""
    if value is None:
        h.update(b"N;")
        return
    if isinstance(value, enum.Enum):
        _hash_value(h, value.value)
        return
    if isinstance(value, bool):
        h.update(b"B1;" if value else b"B0;")
        return
    if isinstance(value, (bytes, bytearray, memoryview)):
        data = bytes(value)
        h.update(b"Y" + str(len(data)).encode("ascii") + b":")
        h.update(data)
        return
    if isinstance(value, str):
        data = value.encode("utf-8")
        h.update(b"S" + str(len(data)).encode("ascii") + b":")
        h.update(data)
        return
    if isinstance(value, (int, float)):
        h.update(b"I" + repr(value).encode("ascii") + b";")
        return
    if isinstance(value, dict):
        h.update(b"D" + str(len(value)).encode("ascii") + b"{")
        for key in sorted(value.keys(), key=str):
            _hash_value(h, str(key))
            _hash_value(h, value[key])
        h.update(b"}")
        return
    if isinstance(value, (list, tuple)):
        h.update(b"L" + str(len(value)).encode("ascii") + b"[")
        for item in value:
            _hash_value(h, item)
        h.update(b"]")
        return
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        # pydantic (google.genai.types.*)。python モードなので bytes は bytes のまま。
        h.update(b"M" + type(value).__name__.encode("utf-8") + b":")
        _hash_value(h, dump(exclude_none=True))
        return
    h.update(b"R")
    _hash_value(h, repr(value))


def content_digest(content: Any) -> bytes:
    """Content 1 件の中身全体の digest。"""
    h = hashlib.sha256()
    _hash_value(h, content)
    return h.digest()


def prefix_digests(system_instruction: str, contents: Optional[list]) -> List[str]:
    """``out[k]`` = 「system_instruction + contents[:k]」の署名 (k = 0..len(contents))。

    連鎖ハッシュなので、先頭 k 件だけの署名を全ての k について一度に得られる。
    継続キャッシュは「先頭から何件を入れたか (k) と、その範囲の署名」を覚えておき、
    今回の並びの ``out[k]`` と比べて一致を判定する。
    """
    h = hashlib.sha256()
    h.update(b"saiverse-continuous-cache-v1\x00")
    _hash_value(h, system_instruction or "")
    current = h.hexdigest()
    out = [current]
    for content in contents or []:
        step = hashlib.sha256(current.encode("ascii"))
        step.update(content_digest(content))
        current = step.hexdigest()
        out.append(current)
    return out


@dataclass
class _ContinuousEntry:
    """継続キャッシュ 1 本。"""
    name: str
    model: str
    chain: str              # 誰の会話のキャッシュか (persona_id)。古いものの整理の単位
    prefix_count: int       # contents の先頭から何件を入れたか (k)
    prefix_sig: str         # prefix_digests(...)[k]
    expire_at: float        # epoch seconds (作成開始時刻 + TTL。安全側)
    ttl_seconds: int
    seq: int                # 登録順 (新しいほど大きい)
    digests: List[str] = field(default_factory=list, repr=False)  # prefix_digests(...)[:k+1]
    client: Any = field(default=None, repr=False)  # 削除に使う genai クライアント
    in_use: int = 0         # いまこのキャッシュを参照している要求の数
    retired: bool = False   # 新しいキャッシュに置き換えられた / 使えないと分かった (参照が 0 になったら消す)
    # 保存料金の返金 (TTL 前に消したときの残り) に使う、作成時の計上と同じ値。
    # account_model が空なら作成時の計上もしていない扱いで、返金もしない。
    cached_tokens: int = 0
    account_model: str = ""
    persona_id: Optional[str] = None
    building_id: Optional[str] = None


class GeminiCacheController:
    """(model, head ハッシュ) 単位で Gemini explicit cache を管理する。

    in-memory のみ (非永続)。プロセス再起動で消えるため、実体の掃除は起動時の
    orphan cleanup (M2) が担う。
    """

    def __init__(self) -> None:
        self._entries: Dict[Tuple[str, str], _CacheEntry] = {}
        self._unsupported_models: set = set()
        self._lock = threading.Lock()
        # 継続キャッシュ (§3.5): name → entry、作成中の (model, 署名) → 完了の合図。
        self._continuous: Dict[str, _ContinuousEntry] = {}
        self._continuous_inflight: Dict[Tuple[str, str], threading.Event] = {}
        self._continuous_seq = 0
        self._background_threads: List[threading.Thread] = []

    @staticmethod
    def _content_signature(contents: Optional[list]) -> str:
        """contents (types.Content の列) を決定的な文字列に直列化する (ハッシュ用)。

        各メッセージの role とテキスト part を連結。非テキスト part (画像等) は
        mime/サイズの marker で表現して内容変化を検出する。
        """
        if not contents:
            return ""
        lines = []
        for c in contents:
            role = getattr(c, "role", "") or ""
            parts = getattr(c, "parts", None) or []
            chunks = []
            for p in parts:
                text = getattr(p, "text", None)
                if text:
                    chunks.append(text)
                    continue
                inline = getattr(p, "inline_data", None)
                if inline is not None:
                    data = getattr(inline, "data", b"") or b""
                    mime = getattr(inline, "mime_type", "") or ""
                    chunks.append(f"<{mime}:{len(data)}>")
            lines.append(role + "\x1f" + "\x1e".join(chunks))
        return "\x1d".join(lines)

    def _key(self, model: str, system_instruction: str, contents: Optional[list]) -> Tuple[str, str]:
        raw = system_instruction + "\x1c" + self._content_signature(contents)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return (model, digest)

    @staticmethod
    def _approx_chars(system_instruction: str, contents: Optional[list]) -> int:
        """sys + contents のおおよその文字数 (最小トークンガード用)。

        1 token >= 1 char なので「文字数 < min_tokens」なら確実にトークン不足
        (= create 失敗確定)。これで API を叩かずに弾ける (日本語でも誤って弾かない)。
        """
        total = len(system_instruction or "")
        for c in (contents or []):
            for p in (getattr(c, "parts", None) or []):
                total += len(getattr(p, "text", "") or "")
        return total

    def ensure(
        self,
        client: Any,
        model: str,
        system_instruction: str,
        ttl_seconds: int,
        *,
        contents: Optional[list] = None,
        min_tokens: int = DEFAULT_MIN_TOKENS,
    ) -> CacheEnsureResult:
        """``system_instruction`` + ``contents`` (キャッシュ対象の prefix) に対応する
        生きた cache の name を返す。無ければ作成。

        ``contents`` が None/空なら system_instruction のみをキャッシュする。
        作成不能 (トークン不足 / API エラー) なら name=None の結果を返し、
        呼び出し側は explicit cache 無しの通常コールにフォールバックする。
        """
        _none = CacheEnsureResult(name=None)
        if not client or not model or (not system_instruction and not contents):
            return _none
        if model in self._unsupported_models:
            return _none
        key = self._key(model, system_instruction, contents)
        now = time.time()

        with self._lock:
            ent = self._entries.get(key)
            if ent and ent.expire_at > now + _EXPIRY_SAFETY_MARGIN:
                return CacheEnsureResult(name=ent.name, created=False)

        # 最小トークンガード (文字数で安全側に判定、API コールなし)。
        if self._approx_chars(system_instruction, contents) < min_tokens:
            LOGGER.debug("[gemini_cache] skip create (prefix below min tokens) model=%s", model)
            return _none

        try:
            from google.genai import types
            cfg_kwargs: Dict[str, Any] = {
                "display_name": f"{DISPLAY_NAME_PREFIX}{key[1][:24]}",
                "ttl": f"{int(ttl_seconds)}s",
            }
            if system_instruction:
                cfg_kwargs["system_instruction"] = system_instruction
            if contents:
                cfg_kwargs["contents"] = contents
            cache = client.caches.create(
                model=model,
                config=types.CreateCachedContentConfig(**cfg_kwargs),
            )
        except Exception as exc:
            status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
            if status == 404:
                self._unsupported_models.add(model)
                LOGGER.info(
                    "[gemini_cache] model=%s does not support createCachedContent; skipping future attempts",
                    model,
                )
            else:
                LOGGER.warning("[gemini_cache] caches.create failed model=%s", model, exc_info=True)
            return _none

        name = getattr(cache, "name", None)
        if not name:
            return _none

        # create 時の cached token 数を取得 (storage 計上用)
        um = getattr(cache, "usage_metadata", None)
        cached_tokens = getattr(um, "total_token_count", 0) or 0 if um else 0

        entry = _CacheEntry(
            name=name,
            model=model,
            expire_at=now + int(ttl_seconds),
            ttl_seconds=int(ttl_seconds),
        )
        with self._lock:
            self._entries[key] = entry
        LOGGER.info(
            "[gemini_cache] created cache name=%s model=%s ttl=%ds cached_msgs=%d cached_tokens=%d",
            name, model, int(ttl_seconds), len(contents or []), cached_tokens,
        )
        return CacheEnsureResult(
            name=name, created=True,
            cached_tokens=cached_tokens, ttl_seconds=int(ttl_seconds),
        )

    def delete(self, client: Any, cache_name: str) -> bool:
        """cache を明示削除し、内部エントリも除去する。"""
        try:
            client.caches.delete(name=cache_name)
        except Exception:
            LOGGER.warning("[gemini_cache] caches.delete failed name=%s", cache_name, exc_info=True)
            return False
        self.forget(cache_name)
        LOGGER.info("[gemini_cache] deleted cache name=%s", cache_name)
        return True

    def forget(self, cache_name: str) -> None:
        """内部エントリだけを除去する (API は叩かない)。

        使おうとしたら Gemini に拒まれた (消えている・失効した等) キャッシュを、
        :meth:`ensure` が二度と再利用しないようにするため。
        """
        with self._lock:
            to_remove = [k for k, v in self._entries.items() if v.name == cache_name]
            for k in to_remove:
                del self._entries[k]

    # ── 継続キャッシュ (高速応答モード) ──
    # docs/intent/cache_lifecycle_control.md §3.5。キャッシュごとに「先頭から何件を
    # 入れたか (k)」と、その範囲の中身全体の署名を覚え、今回の並びの先頭と一致する
    # 生きたキャッシュを探す。一致が無い回だけ同期で作り、それ以外は次の往復のための
    # キャッシュを裏で作る。新しいキャッシュが出来たら、それが延長した古いキャッシュを
    # 消す。いま要求が参照しているキャッシュは消さず、参照が 0 になった時点で消す。

    def _purge_expired_continuous_locked(self, now: float) -> None:
        """失効済みの継続キャッシュを台帳から外す (実体は Gemini 側で消えている)。"""
        expired = [n for n, e in self._continuous.items() if e.expire_at <= now]
        for name in expired:
            del self._continuous[name]

    def _live_continuous_locked(self, model: str, chain: str, now: float):
        for ent in self._continuous.values():
            if ent.retired or ent.model != model or ent.chain != chain:
                continue
            if ent.expire_at <= now + _CONTINUOUS_EXPIRY_SAFETY_MARGIN:
                continue
            yield ent

    def continuous_acquire(
        self, model: str, chain: str, digests: List[str],
    ) -> Optional[Tuple[str, int]]:
        """今回の並びの先頭と一致する生きたキャッシュのうち、最も長いものを確保する。

        ``digests`` は ``prefix_digests(sys, contents[:B+1])`` (長さ B+2)。
        一致の条件は「キャッシュの k が B+1 以下で、``digests[k]`` が記録した署名と
        等しい」こと。見つかったら参照を 1 つ確保して ``(name, k)`` を返す
        (使い終わったら :meth:`continuous_release`)。無ければ None。
        """
        max_k = len(digests) - 1
        now = time.time()
        with self._lock:
            self._purge_expired_continuous_locked(now)
            best: Optional[_ContinuousEntry] = None
            for ent in self._live_continuous_locked(model, chain, now):
                k = ent.prefix_count
                if k < 1 or k > max_k:
                    continue
                if digests[k] != ent.prefix_sig:
                    continue
                if best is None or k > best.prefix_count:
                    best = ent
            if best is None:
                return None
            best.in_use += 1
            return best.name, best.prefix_count

    def continuous_release(self, name: str) -> None:
        """:meth:`continuous_acquire` / 同期作成で確保した参照を返す。

        置き換え済み (retired) で参照が 0 になったら、ここで削除する。
        """
        doomed: Optional[_ContinuousEntry] = None
        with self._lock:
            ent = self._continuous.get(name)
            if ent is None:
                return
            ent.in_use = max(0, ent.in_use - 1)
            if ent.retired and ent.in_use == 0:
                del self._continuous[name]
                doomed = ent
        if doomed is not None:
            self._delete_continuous(doomed, reason="superseded, released after use")

    def continuous_discard(self, name: str, *, reason: str) -> None:
        """使おうとしたら Gemini に拒まれたキャッシュを、二度と選ばれないようにする。

        呼び出し元が確保していた参照を 1 つ返す (:meth:`continuous_release` の
        代わりに呼ぶ — 両方呼ぶと参照を二重に返す)。retired にするので、以後の
        一致の検索・予約・同期作成の対象から外れる。他の要求がまだ参照していれば
        その参照が全部返った時点で、いなければここで台帳から外して削除を試みる
        (削除が成功したときだけ TTL の残りを返金する、:meth:`_delete_continuous`)。
        """
        doomed: Optional[_ContinuousEntry] = None
        with self._lock:
            ent = self._continuous.get(name)
            if ent is None:
                return
            ent.retired = True
            ent.in_use = max(0, ent.in_use - 1)
            if ent.in_use == 0:
                del self._continuous[name]
                doomed = ent
        LOGGER.info(
            "[gemini][continuous_cache] discarded unusable name=%s (%s)%s",
            name, reason, "" if doomed is not None else " — deleted when the other users release it",
        )
        if doomed is not None:
            self._delete_continuous(doomed, reason=f"unusable: {reason}")

    def continuous_chain_size(self, model: str, chain: str) -> int:
        """その会話 (chain) の生きた継続キャッシュの本数 (ログの理由づけ用)。"""
        now = time.time()
        with self._lock:
            return sum(1 for _ in self._live_continuous_locked(model, chain, now))

    def continuous_reserve(
        self, model: str, chain: str, sig: str,
    ) -> Optional[threading.Event]:
        """裏での作成の予約。同じ範囲が生きているか作成中なら None (作らない)。

        予約は呼び出し元のスレッドで同期に取る — 同じ Pulse の 2 回目の呼び出しが
        スレッドの起動より先に来ても、二重に作り始めないため。
        """
        now = time.time()
        with self._lock:
            if (model, sig) in self._continuous_inflight:
                return None
            for ent in self._live_continuous_locked(model, chain, now):
                if ent.prefix_sig == sig:
                    return None
            reservation = threading.Event()
            self._continuous_inflight[(model, sig)] = reservation
            return reservation

    def continuous_create_sync(
        self,
        client: Any,
        model: str,
        chain: str,
        system_instruction: str,
        contents: list,
        digests: List[str],
        ttl_seconds: int,
        *,
        min_tokens: int = DEFAULT_MIN_TOKENS,
        account_model: str = "",
        persona_id: Optional[str] = None,
        building_id: Optional[str] = None,
    ) -> CacheEnsureResult:
        """一致するキャッシュが無い回の同期作成。

        同じ範囲を別の呼び出しが作成中ならその完了を待つ (二重に作らない)。
        戻り値の name は参照を 1 つ確保済み (使い終わったら :meth:`continuous_release`)。
        この会話の古い継続キャッシュは、もう一致しないので出来た時点で整理する。

        ``account_model`` / ``persona_id`` / ``building_id`` は、呼び出し元が作成時に
        計上する保存料金と同じ値。TTL 前に消したときの返金に使う。
        """
        k = len(contents)
        sig = digests[k]
        deadline = time.monotonic() + _CONTINUOUS_INFLIGHT_WAIT_SECONDS
        while True:
            now = time.time()
            with self._lock:
                for ent in self._live_continuous_locked(model, chain, now):
                    if ent.prefix_count == k and ent.prefix_sig == sig:
                        ent.in_use += 1
                        return CacheEnsureResult(
                            name=ent.name, created=False, ttl_seconds=ent.ttl_seconds,
                        )
                waiting = self._continuous_inflight.get((model, sig))
                if waiting is None:
                    reservation = threading.Event()
                    self._continuous_inflight[(model, sig)] = reservation
                    break
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not waiting.wait(timeout=remaining):
                LOGGER.warning(
                    "[gemini][continuous_cache] timed out waiting for an in-flight "
                    "create of the same prefix (model=%s prefix=%d msgs)", model, k,
                )
                return CacheEnsureResult(name=None)
        return self._create_reserved(
            client, model, chain, system_instruction, contents, digests,
            ttl_seconds, reservation,
            mode="sync", acquire=True, retire_all_older=True, min_tokens=min_tokens,
            account_model=account_model, persona_id=persona_id, building_id=building_id,
        )

    def continuous_create_reserved(
        self,
        client: Any,
        model: str,
        chain: str,
        system_instruction: str,
        contents: list,
        digests: List[str],
        ttl_seconds: int,
        reservation: threading.Event,
        *,
        min_tokens: int = DEFAULT_MIN_TOKENS,
        account_model: str = "",
        persona_id: Optional[str] = None,
        building_id: Optional[str] = None,
    ) -> CacheEnsureResult:
        """:meth:`continuous_reserve` で予約した範囲を作る (裏での作成用)。

        参照は確保しない。出来たら、それが延長した古いキャッシュを整理する。
        計上用の引数は :meth:`continuous_create_sync` と同じ。
        """
        return self._create_reserved(
            client, model, chain, system_instruction, contents, digests,
            ttl_seconds, reservation,
            mode="bg", acquire=False, retire_all_older=False, min_tokens=min_tokens,
            account_model=account_model, persona_id=persona_id, building_id=building_id,
        )

    def _create_reserved(
        self,
        client: Any,
        model: str,
        chain: str,
        system_instruction: str,
        contents: list,
        digests: List[str],
        ttl_seconds: int,
        reservation: threading.Event,
        *,
        mode: str,
        acquire: bool,
        retire_all_older: bool,
        min_tokens: int,
        account_model: str = "",
        persona_id: Optional[str] = None,
        building_id: Optional[str] = None,
    ) -> CacheEnsureResult:
        k = len(contents)
        sig = digests[k]
        _none = CacheEnsureResult(name=None)
        try:
            if not client or not model or k < 1:
                return _none
            if model in self._unsupported_models:
                return _none
            if self._approx_chars(system_instruction, contents) < min_tokens:
                LOGGER.debug(
                    "[gemini][continuous_cache] skip create(%s) (prefix below min tokens) model=%s",
                    mode, model,
                )
                return _none
            # 失効時刻は作成の開始時刻から数える (完了時刻から数えるより早めに見切る、安全側)。
            started = time.time()
            t0 = time.monotonic()
            try:
                from google.genai import types
                cfg_kwargs: Dict[str, Any] = {
                    "display_name": f"{DISPLAY_NAME_PREFIX}cc:{sig[:24]}",
                    "ttl": f"{int(ttl_seconds)}s",
                    "contents": contents,
                }
                if system_instruction:
                    cfg_kwargs["system_instruction"] = system_instruction
                cache = client.caches.create(
                    model=model,
                    config=types.CreateCachedContentConfig(**cfg_kwargs),
                )
            except Exception as exc:
                status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
                if status == 404:
                    self._unsupported_models.add(model)
                    LOGGER.info(
                        "[gemini_cache] model=%s does not support createCachedContent; skipping future attempts",
                        model,
                    )
                else:
                    LOGGER.warning(
                        "[gemini][continuous_cache] create(%s) failed model=%s prefix=%d msgs",
                        mode, model, k, exc_info=True,
                    )
                return _none
            secs = time.monotonic() - t0

            name = getattr(cache, "name", None)
            if not name:
                return _none
            um = getattr(cache, "usage_metadata", None)
            cached_tokens = (getattr(um, "total_token_count", 0) or 0) if um else 0

            doomed: List[_ContinuousEntry] = []
            with self._lock:
                self._continuous_seq += 1
                entry = _ContinuousEntry(
                    name=name,
                    model=model,
                    chain=chain,
                    prefix_count=k,
                    prefix_sig=sig,
                    expire_at=started + int(ttl_seconds),
                    ttl_seconds=int(ttl_seconds),
                    seq=self._continuous_seq,
                    digests=list(digests[: k + 1]),
                    client=client,
                    in_use=1 if acquire else 0,
                    cached_tokens=int(cached_tokens),
                    account_model=account_model or "",
                    persona_id=persona_id,
                    building_id=building_id,
                )
                born_superseded = False
                for other in list(self._continuous.values()):
                    if other.retired or other.model != model or other.chain != chain:
                        continue
                    if (
                        not acquire
                        and other.prefix_count > k
                        and len(other.digests) > k
                        and other.digests[k] == sig
                    ):
                        # 裏での作成が遅れて、もっと長い (この範囲を延長した)
                        # キャッシュが先に出来ていた。これは誰にも使われない。
                        born_superseded = True
                        continue
                    extended = (
                        other.prefix_count < k
                        and digests[other.prefix_count] == other.prefix_sig
                    )
                    if not (extended or retire_all_older):
                        continue
                    other.retired = True
                    if other.in_use == 0:
                        del self._continuous[other.name]
                        doomed.append(other)
                if born_superseded:
                    entry.retired = True
                    doomed.append(entry)
                else:
                    self._continuous[name] = entry
            LOGGER.info(
                "[gemini][continuous_cache] create(%s) name=%s model=%s prefix=%d msgs "
                "ttl=%ds secs=%.1f cached_tokens=%d",
                mode, name, model, k, int(ttl_seconds), secs, cached_tokens,
            )
            for other in doomed:
                self._delete_continuous(other, reason=f"superseded by {name}")
            return CacheEnsureResult(
                name=name, created=True,
                cached_tokens=cached_tokens, ttl_seconds=int(ttl_seconds),
            )
        finally:
            with self._lock:
                if self._continuous_inflight.get((model, sig)) is reservation:
                    del self._continuous_inflight[(model, sig)]
            reservation.set()

    def _delete_continuous(self, entry: _ContinuousEntry, *, reason: str) -> None:
        """継続キャッシュを消す。継続キャッシュを消す経路はすべてここを通る。

        保存料金は作成時に TTL の全額を計上している (予約席の約束、
        ``UsageTracker.record_cache_storage``) ので、削除が成功したらここで残りの
        秒数分を返金する。削除が失敗したキャッシュは TTL まで残るので返金しない。
        失効して台帳から外れるだけのもの (:meth:`_purge_expired_continuous_locked`) は
        ここを通らず、返金も無い。
        """
        client = entry.client
        if client is None:
            return
        try:
            client.caches.delete(name=entry.name)
        except Exception:
            LOGGER.warning(
                "[gemini][continuous_cache] delete failed name=%s (%s)",
                entry.name, reason, exc_info=True,
            )
            return
        remaining = max(0.0, min(float(entry.ttl_seconds), entry.expire_at - time.time()))
        LOGGER.info(
            "[gemini][continuous_cache] deleted name=%s prefix=%d msgs remaining=%.0fs (%s)",
            entry.name, entry.prefix_count, remaining, reason,
        )
        self._refund_continuous_storage(entry, remaining)

    @staticmethod
    def _refund_continuous_storage(entry: _ContinuousEntry, remaining_seconds: float) -> None:
        if remaining_seconds <= 0 or not entry.cached_tokens or not entry.account_model:
            return
        try:
            from saiverse.usage_tracker import get_usage_tracker
            get_usage_tracker().record_cache_storage_refund(
                model_id=entry.account_model,
                cached_tokens=entry.cached_tokens,
                remaining_seconds=remaining_seconds,
                persona_id=entry.persona_id,
                building_id=entry.building_id,
            )
        except Exception:
            LOGGER.warning(
                "[gemini][continuous_cache] storage refund failed name=%s", entry.name, exc_info=True,
            )

    def spawn_background(
        self, target: Callable[[], None], *, name: str = "gemini-continuous-cache",
    ) -> threading.Thread:
        """裏での作成スレッドを起動する (daemon。返事の生成を待たせない)。"""
        thread = threading.Thread(target=target, name=name, daemon=True)
        with self._lock:
            self._background_threads = [t for t in self._background_threads if t.is_alive()]
            self._background_threads.append(thread)
        thread.start()
        return thread

    def join_background(self, timeout: float = 10.0) -> None:
        """裏での作成の完了を待つ (テストとシャットダウン用)。"""
        with self._lock:
            threads = list(self._background_threads)
        for thread in threads:
            thread.join(timeout)


_DEFAULT_CONTROLLER: Optional[GeminiCacheController] = None


def get_gemini_cache_controller() -> GeminiCacheController:
    """プロセス共有の単一コントローラを返す。"""
    global _DEFAULT_CONTROLLER
    if _DEFAULT_CONTROLLER is None:
        _DEFAULT_CONTROLLER = GeminiCacheController()
    return _DEFAULT_CONTROLLER
