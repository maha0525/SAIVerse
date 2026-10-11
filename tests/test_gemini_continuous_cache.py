"""Gemini の継続キャッシュ (高速応答モード) のテスト。

docs/intent/cache_lifecycle_control.md §3.5。実 API は呼ばない — genai クライアントを
偽物 (caches.create / caches.delete / models.generate_content_stream を記録するだけ) に
差し替え、境目の位置・一致の判定・裏での作成・古いキャッシュの整理・自動キャッシュ
との切り分けを検証する。
"""
from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from google.genai import types

from llm_clients import gemini_cache
from llm_clients.gemini import GeminiClient
from llm_clients.gemini_cache import GeminiCacheController, prefix_digests
from llm_clients.utils import CACHE_BOUNDARY_METADATA_KEY
from sea.runtime_context import mark_cache_boundary

MODEL = "gemini-2.5-flash"
SYS = "SYSTEM PROMPT " * 200
PULSE = "pulse-now"
CC = {"persona_id": "air", "building_id": "room"}


# ── 偽物の genai クライアント ──

class FakeCaches:
    def __init__(self) -> None:
        self.created: List[Dict[str, Any]] = []
        self.deleted: List[str] = []
        self._n = 0
        self._lock = threading.Lock()
        # 裏での作成 (スレッド名で判別) をこの合図まで止める。None なら止めない。
        self.bg_gate: Optional[threading.Event] = None
        # True なら delete が失敗する (Gemini 側の削除エラー)。
        self.fail_delete = False

    def create(self, model: str, config: Any) -> Any:
        if self.bg_gate is not None and threading.current_thread().name.startswith(
            "gemini-continuous-cache"
        ):
            assert self.bg_gate.wait(5), "bg gate never opened"
        with self._lock:
            self._n += 1
            name = f"cachedContents/c{self._n}"
            self.created.append({
                "name": name,
                "contents": list(config.contents or []),
                "system_instruction": config.system_instruction,
                "ttl": config.ttl,
                "thread": threading.current_thread().name,
            })
        return SimpleNamespace(
            name=name, usage_metadata=SimpleNamespace(total_token_count=1000 * self._n),
        )

    def delete(self, name: str) -> None:
        if self.fail_delete:
            raise RuntimeError("delete failed")
        with self._lock:
            self.deleted.append(name)


def _cache_error(
    code: int = 403,
    status: str = "PERMISSION_DENIED",
    message: str = "CachedContent not found (or permission denied)",
) -> Exception:
    """google-genai がキャッシュを使えなかったときに上げる形の例外 (4xx = ClientError)。"""
    from google.genai import errors as genai_errors
    return genai_errors.ClientError(
        code, {"error": {"code": code, "message": message, "status": status}},
    )


def _response(text: str = "ok") -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[types.Candidate(
            content=types.Content(role="model", parts=[types.Part(text=text)]),
            finish_reason=types.FinishReason.STOP,
        )],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=10, candidates_token_count=2, cached_content_token_count=5,
        ),
    )


class FakeModels:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        # cached_content 付きの要求に、先頭から順に上げる例外 (実 SDK と同じく
        # ストリームは最初の chunk を読むときに上げる)。
        self.errors_when_cached: List[BaseException] = []
        # cached_content 付きのストリームで、text を 1 つ出してから上げる例外。
        self.emit_then_raise: Optional[BaseException] = None
        self.text = "ok"

    def _pop_error(self, config: Any) -> Optional[BaseException]:
        if config.cached_content and self.errors_when_cached:
            return self.errors_when_cached.pop(0)
        return None

    def generate_content_stream(self, model: str, contents: list, config: Any):
        self.calls.append({"contents": list(contents), "config": config})
        err = self._pop_error(config)
        if err is not None:
            def _fail():
                raise err
                yield  # pragma: no cover - generator にするため
            return _fail()
        if self.emit_then_raise is not None and config.cached_content:
            late = self.emit_then_raise

            def _emit_then_fail():
                yield _response("partial")
                raise late
            return _emit_then_fail()
        return iter([_response(self.text)])

    def generate_content(self, model: str, contents: list, config: Any):
        self.calls.append({"contents": list(contents), "config": config})
        err = self._pop_error(config)
        if err is not None:
            raise err
        return _response(self.text)


class FakeGenai:
    def __init__(self) -> None:
        self.caches = FakeCaches()
        self.models = FakeModels()


# ── fixtures ──

@pytest.fixture(autouse=True)
def restore_class_attrs():
    saved = (
        GeminiClient._AUTO_CACHE_ENABLED,
        GeminiClient._AUTO_CACHE_KEEP_SECONDS,
        GeminiClient._CONTINUOUS_CACHE_ENABLED,
        GeminiClient._CONTINUOUS_CACHE_TTL_SECONDS,
    )
    GeminiClient._AUTO_CACHE_ENABLED = True
    GeminiClient._AUTO_CACHE_KEEP_SECONDS = 0
    GeminiClient._CONTINUOUS_CACHE_ENABLED = True
    GeminiClient._CONTINUOUS_CACHE_TTL_SECONDS = 300
    yield
    (
        GeminiClient._AUTO_CACHE_ENABLED,
        GeminiClient._AUTO_CACHE_KEEP_SECONDS,
        GeminiClient._CONTINUOUS_CACHE_ENABLED,
        GeminiClient._CONTINUOUS_CACHE_TTL_SECONDS,
    ) = saved


@pytest.fixture
def controller(monkeypatch):
    ctrl = GeminiCacheController()
    monkeypatch.setattr(gemini_cache, "get_gemini_cache_controller", lambda: ctrl)
    yield ctrl
    ctrl.join_background(5)


@pytest.fixture
def storage_records(monkeypatch):
    records: List[tuple] = []

    def _fake(model_id, cached_tokens, ttl_seconds, *, persona_id, building_id):
        records.append((model_id, cached_tokens, ttl_seconds, persona_id, building_id))

    monkeypatch.setattr(
        GeminiClient, "_record_continuous_cache_storage", staticmethod(_fake),
    )
    return records


@pytest.fixture(autouse=True)
def refund_records(monkeypatch):
    """TTL 前の削除で記録される返金 (UsageTracker.record_cache_storage_refund)。"""
    from saiverse.usage_tracker import UsageTracker

    records: List[tuple] = []

    def _fake(self, model_id, cached_tokens, remaining_seconds, *, persona_id=None, building_id=None):
        records.append((model_id, cached_tokens, remaining_seconds, persona_id, building_id))

    monkeypatch.setattr(UsageTracker, "record_cache_storage_refund", _fake)
    return records


@pytest.fixture
def fake(monkeypatch):
    fake_client = FakeGenai()
    monkeypatch.setattr(
        "llm_clients.gemini.build_gemini_clients",
        lambda prefer_paid=False: (None, fake_client, fake_client),
    )
    monkeypatch.delenv("SAIVERSE_DISABLE_GEMINI_STREAMING", raising=False)
    return fake_client


@pytest.fixture
def gemini(fake):
    client = GeminiClient(MODEL, {})
    client.config_key = MODEL
    return client


# ── 並びの組み立て (SEA の _prepare_context と同じ形) ──

def _history(rounds: int, *, old_reply: str = "old reply " * 150) -> List[Any]:
    msgs: List[Any] = [
        {"role": "user", "content": "old question " * 150},
        {"role": "assistant", "content": old_reply},
    ]
    for i in range(rounds):
        msgs.append({"role": "user", "content": f"u{i}"})
        msgs.append({"role": "assistant", "content": f"a{i}"})
    return msgs


def _turn(
    rounds: int,
    *,
    realtime: str = "",
    recall: Optional[str] = None,
    history: Optional[List[Any]] = None,
) -> List[Any]:
    """rounds 回の往復を終えた後の、次のユーザーの発言への返事の並び。

    head(system) / 履歴 (最後の AI の返事に境目の印) / リアルタイム情報 (最後の
    ユーザー発言の直前) / ユーザーの発言 / 自動想起 (末尾)。
    """
    hist = list(history if history is not None else _history(rounds))
    hist.append({"role": "user", "content": f"u{rounds}"})
    hist = mark_cache_boundary(hist, PULSE)
    msgs: List[Any] = [{"role": "system", "content": SYS}] + hist
    msgs.insert(len(msgs) - 1, {
        "role": "user",
        "content": f"<system>realtime {realtime or rounds}</system>",
        "metadata": {"__realtime_context__": True},
    })
    if recall:
        msgs.append({"role": "user", "content": f"<system>recall {recall}</system>"})
    return msgs


def _run(gemini: GeminiClient, messages: List[Any], cc: Optional[dict] = CC) -> str:
    return "".join(
        c for c in gemini.generate_stream(messages, tools=[], continuous_cache=cc)
        if isinstance(c, str)
    )


def _texts(contents: list) -> List[str]:
    return [c.parts[0].text for c in contents]


def _last_call(fake: FakeGenai) -> Dict[str, Any]:
    return fake.models.calls[-1]


# ── 1. 会話の最初の一回: 同期作成して境目より後ろだけ送る ──

def test_first_reply_creates_boundary_prefix_synchronously(gemini, fake, controller, storage_records):
    _run(gemini, _turn(0))

    assert len(fake.caches.created) == 1
    created = fake.caches.created[0]
    # sys + [古い質問, 古い返事] (境目 = 履歴の最後の AI の返事)
    assert created["system_instruction"] == SYS
    assert _texts(created["contents"]) == ["old question " * 150, "old reply " * 150]
    assert created["ttl"] == "300s"
    assert created["thread"] == threading.current_thread().name  # 同期

    call = _last_call(fake)
    assert call["config"].cached_content == "cachedContents/c1"
    assert call["config"].system_instruction is None
    assert _texts(call["contents"]) == ["<system>realtime 0</system>", "u0"]
    # 同期作成も作った瞬間に、この要求のペルソナ・建物で計上する (usage への相乗りはしない)
    assert storage_records == [(MODEL, 1000, 300, "air", "room")]
    assert gemini._latest_usage.cache_storage_tokens == 0
    assert gemini._pending_cache_storage is None
    # 応答直後に消す自動キャッシュの動きはしない
    assert fake.caches.deleted == []


# ── 8. 内部印が payload に漏れない ──

def test_boundary_marker_never_reaches_the_payload(gemini, fake, controller, storage_records):
    _run(gemini, _turn(0, recall="memory"))
    _run(gemini, _turn(1, recall="memory2"))
    controller.join_background(5)

    blobs = []
    for call in fake.models.calls:
        blobs.extend(c.model_dump_json() for c in call["contents"])
        blobs.append(call["config"].model_dump_json())
    for created in fake.caches.created:
        blobs.extend(c.model_dump_json() for c in created["contents"])
    assert blobs
    assert all(CACHE_BOUNDARY_METADATA_KEY not in b for b in blobs)


# ── 2. 次の往復: 前のキャッシュを使い、新しい境目は裏で作って古いのを消す ──

def test_next_turn_reuses_previous_cache_and_supersedes_it(gemini, fake, controller, storage_records):
    _run(gemini, _turn(0))
    _run(gemini, _turn(1, recall="something"))
    controller.join_background(5)

    call = _last_call(fake)
    assert call["config"].cached_content == "cachedContents/c1"
    # キャッシュの k=2 から後ろ全部: 前の往復 + リアルタイム情報 + 今回の発言 + 自動想起
    assert _texts(call["contents"]) == [
        "u0", "a0", "<system>realtime 1</system>", "u1", "<system>recall something</system>",
    ]
    assert len(fake.caches.created) == 2
    bg = fake.caches.created[1]
    assert bg["thread"].startswith("gemini-continuous-cache")
    assert _texts(bg["contents"])[-2:] == ["u0", "a0"]  # 今回の境目まで
    assert len(bg["contents"]) == 4
    assert fake.caches.deleted == ["cachedContents/c1"]
    assert storage_records == [
        (MODEL, 1000, 300, "air", "room"),   # c1 (同期)
        (MODEL, 2000, 300, "air", "room"),   # c2 (裏)
    ]

    # 3 往復目は裏で作った c2 を使う
    _run(gemini, _turn(2))
    assert _last_call(fake)["config"].cached_content == "cachedContents/c2"
    assert _texts(_last_call(fake)["contents"])[:2] == ["u1", "a1"]


# ── 3. 裏の作成が次の発言に間に合わない: 古いキャッシュを使って待たない ──

def test_slow_background_create_does_not_block_and_old_cache_is_used(gemini, fake, controller, storage_records):
    _run(gemini, _turn(0))
    fake.caches.bg_gate = threading.Event()

    _run(gemini, _turn(1))   # c1 を使い、境目 (a0 まで) の作成は止まったまま
    _run(gemini, _turn(2))   # まだ出来ていない → c1 を使い、さらに多めに送る

    call = _last_call(fake)
    assert call["config"].cached_content == "cachedContents/c1"
    assert _texts(call["contents"])[:4] == ["u0", "a0", "u1", "a1"]
    assert len(fake.caches.created) == 1  # 裏の 2 本はまだ作成中
    assert fake.caches.deleted == []      # 何も出来ていないので何も消さない

    fake.caches.bg_gate.set()
    controller.join_background(5)
    # 出来上がった順に関わらず、最後に生き残るのは一番長い (a1 まで) 1 本だけ
    assert len(fake.caches.created) == 3
    names = {c["name"]: len(c["contents"]) for c in fake.caches.created}
    longest = max(names, key=names.get)
    assert names[longest] == 6
    assert set(fake.caches.deleted) == set(names) - {longest}

    _run(gemini, _turn(3))
    assert _last_call(fake)["config"].cached_content == longest


# ── 4. 同じ Pulse の 2 回目の呼び出し: 同じ境目・同じキャッシュ・二重作成なし ──

def test_second_call_in_same_pulse_uses_same_boundary_without_duplicate_create(
    gemini, fake, controller, storage_records,
):
    _run(gemini, _turn(0))
    fake.caches.bg_gate = threading.Event()

    first = _turn(1)
    _run(gemini, first)
    # スペルの往復 (Pulse の途中の発言と結果) が後ろに付いた 2 回目
    second = first + [
        {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "t1", "function": {"name": "look", "arguments": "{}"}}],
        },
        {"role": "tool", "name": "look", "tool_call_id": "t1", "content": "{\"seen\": 1}"},
        {"role": "user", "content": "<system>continue</system>"},
    ]
    _run(gemini, second)

    call = _last_call(fake)
    assert call["config"].cached_content == "cachedContents/c1"
    assert _texts(call["contents"])[:2] == ["u0", "a0"]  # 境目は Pulse の途中の発言ではない
    assert len(call["contents"]) == 7

    fake.caches.bg_gate.set()
    controller.join_background(5)
    assert len(fake.caches.created) == 2  # 裏の作成は 1 本だけ

    # 出来上がった後の 3 回目: 境目ちょうどのキャッシュを使い、新しい作成はしない
    _run(gemini, second)
    assert _last_call(fake)["config"].cached_content == "cachedContents/c2"
    assert _texts(_last_call(fake)["contents"])[0] == "<system>realtime 1</system>"
    controller.join_background(5)
    assert len(fake.caches.created) == 2


# ── 5. 先頭が変わった: 使い回さず同期で作り直す ──

def test_changed_earlier_message_forces_sync_create(gemini, fake, controller, storage_records):
    _run(gemini, _turn(0))
    changed = _history(1, old_reply="edited reply " * 150)
    _run(gemini, _turn(1, history=changed))

    assert len(fake.caches.created) == 2
    assert fake.caches.created[1]["thread"] == threading.current_thread().name  # 同期
    call = _last_call(fake)
    assert call["config"].cached_content == "cachedContents/c2"
    assert _texts(call["contents"]) == ["<system>realtime 1</system>", "u1"]
    # もう一致しない古いキャッシュは整理する
    assert fake.caches.deleted == ["cachedContents/c1"]


def _image_content(data: bytes) -> types.Content:
    return types.Content(role="user", parts=[
        types.Part(text="look at this " * 150),
        types.Part.from_bytes(data=data, mime_type="image/png"),
    ])


def test_changed_image_bytes_with_same_length_forces_sync_create(gemini, fake, controller, storage_records):
    hist0 = [_image_content(b"A" * 64), {"role": "assistant", "content": "nice " * 150}]
    _run(gemini, _turn(0, history=hist0))
    hist1 = [_image_content(b"B" * 64), {"role": "assistant", "content": "nice " * 150},
             {"role": "user", "content": "u0"}, {"role": "assistant", "content": "a0"}]
    _run(gemini, _turn(1, history=hist1))

    assert len(fake.caches.created) == 2
    assert _last_call(fake)["config"].cached_content == "cachedContents/c2"
    assert fake.caches.created[1]["thread"] == threading.current_thread().name


def test_signature_covers_bytes_and_function_calls():
    a = [_image_content(b"A" * 64)]
    b = [_image_content(b"B" * 64)]
    assert prefix_digests(SYS, a)[1] != prefix_digests(SYS, b)[1]
    assert prefix_digests(SYS, a)[0] == prefix_digests(SYS, b)[0]

    def fc(args):
        return [types.Content(role="model", parts=[
            types.Part(function_call=types.FunctionCall(name="f", args=args)),
        ])]
    assert prefix_digests(SYS, fc({"x": 1}))[1] != prefix_digests(SYS, fc({"x": 2}))[1]
    assert prefix_digests(SYS, fc({"x": 1}))[1] == prefix_digests(SYS, fc({"x": 1}))[1]
    assert prefix_digests("other", a)[1] != prefix_digests(SYS, a)[1]


# ── 6. 返事以外の呼び出し: 継続 ON でも自動キャッシュそのまま ──

def test_non_reply_call_keeps_plain_auto_cache(gemini, fake, controller, storage_records):
    msgs = _turn(0)
    _run(gemini, msgs, cc=None)

    assert len(fake.caches.created) == 1
    # 自動キャッシュ: 最新の 1 件を除いた全部
    assert len(fake.caches.created[0]["contents"]) == 3
    call = _last_call(fake)
    assert call["config"].cached_content == "cachedContents/c1"
    assert _texts(call["contents"]) == ["u0"]
    # 保持秒数 0 → 応答直後に削除
    assert fake.caches.deleted == ["cachedContents/c1"]
    assert controller._continuous == {}


# ── 7. 継続 ON・自動 OFF: キャッシュを一切使わない (今日の自動 OFF と同じ) ──

def test_continuous_without_auto_cache_does_nothing(gemini, fake, controller, storage_records):
    GeminiClient._AUTO_CACHE_ENABLED = False
    msgs = _turn(0)
    _run(gemini, msgs)

    assert fake.caches.created == []
    call = _last_call(fake)
    assert call["config"].cached_content is None
    assert call["config"].system_instruction == SYS
    assert len(call["contents"]) == 4


# ── 落ちる経路 ──

def test_no_boundary_falls_back_to_auto_cache(gemini, fake, controller, storage_records):
    msgs = [
        {"role": "system", "content": SYS},
        {"role": "user", "content": "first ever message " * 100},
        {"role": "user", "content": "hello"},
    ]
    _run(gemini, msgs)
    assert len(fake.caches.created) == 1
    assert _texts(_last_call(fake)["contents"]) == ["hello"]
    assert fake.caches.deleted == ["cachedContents/c1"]  # 自動キャッシュの動き


def test_free_tier_falls_back(gemini, fake, controller, storage_records):
    gemini.free_client = fake  # いま使っているのが無料枠
    _run(gemini, _turn(0))
    assert fake.caches.created == []
    assert _last_call(fake)["config"].cached_content is None


def test_non_streaming_generate_also_uses_continuous_cache(gemini, fake, controller, storage_records):
    gemini.generate(_turn(0), tools=[], continuous_cache=CC)
    gemini.generate(_turn(1), tools=[], continuous_cache=CC)
    controller.join_background(5)
    assert [c["config"].cached_content for c in fake.models.calls] == [
        "cachedContents/c1", "cachedContents/c1",
    ]
    assert fake.caches.deleted == ["cachedContents/c1"]
    assert controller._continuous["cachedContents/c2"].in_use == 0


# ── 10. キャッシュを使えなかった回: キャッシュを外して 1 回だけ送り直す ──

def _uncached_texts(gemini: GeminiClient, messages: List[Any]) -> List[str]:
    """キャッシュを使わない要求が送る contents (変換後の全部)。"""
    return _texts(gemini._convert_messages(messages)[1])


def _assert_no_leaked_refs(controller: GeminiCacheController) -> None:
    assert all(ent.in_use == 0 for ent in controller._continuous.values())


def _gen_stream(gemini, messages):
    return _run(gemini, messages)


def _gen_generate(gemini, messages):
    return gemini.generate(messages, tools=[], continuous_cache=CC)


@pytest.mark.parametrize("runner", [_gen_generate, _gen_stream], ids=["generate", "stream"])
def test_unusable_continuous_cache_retries_once_without_cache(
    gemini, fake, controller, storage_records, refund_records, runner,
):
    _run(gemini, _turn(0))                         # c1 (同期)
    fake.caches.bg_gate = threading.Event()        # c2 (裏) は送り直しの後に出来上がる
    fake.models.errors_when_cached = [_cache_error()]
    n_before = len(fake.models.calls)

    msgs = _turn(1)
    assert runner(gemini, msgs) == "ok"

    calls = fake.models.calls[n_before:]
    assert len(calls) == 2
    assert calls[0]["config"].cached_content == "cachedContents/c1"
    retry = calls[1]
    assert retry["config"].cached_content is None
    assert retry["config"].system_instruction == SYS
    assert _texts(retry["contents"]) == _uncached_texts(gemini, msgs)
    # 使えなかった c1 は台帳から外れ、削除されて残りを返金する
    assert "cachedContents/c1" not in controller._continuous
    assert fake.caches.deleted == ["cachedContents/c1"]
    assert len(refund_records) == 1
    model_id, tokens, remaining, persona, building = refund_records[0]
    assert (model_id, tokens, persona, building) == (MODEL, 1000, "air", "room")
    assert 250 < remaining <= 300

    fake.caches.bg_gate.set()
    controller.join_background(5)
    _assert_no_leaked_refs(controller)

    # 次の往復では c1 を二度と選ばない
    _run(gemini, _turn(2))
    later = [c["config"].cached_content for c in fake.models.calls[n_before + 2:]]
    assert "cachedContents/c1" not in later
    assert later[-1] == "cachedContents/c2"
    controller.join_background(5)
    _assert_no_leaked_refs(controller)


def test_unusable_cache_in_structured_call_retries_without_cache(gemini, fake, controller, storage_records):
    """generate_content (非ストリーム) を使う経路でも同じ。"""
    _run(gemini, _turn(0))
    fake.models.text = '{"a": "x"}'
    fake.models.errors_when_cached = [_cache_error(404, "NOT_FOUND", "cachedContents/c1 not found")]
    n_before = len(fake.models.calls)

    msgs = _turn(1)
    result = gemini.generate(
        msgs, tools=[], continuous_cache=CC,
        response_schema={"type": "object", "properties": {"a": {"type": "string"}}},
    )
    assert result == {"a": "x"}
    calls = fake.models.calls[n_before:]
    assert [c["config"].cached_content for c in calls] == ["cachedContents/c1", None]
    assert calls[1]["config"].system_instruction == SYS
    assert _texts(calls[1]["contents"]) == _uncached_texts(gemini, msgs)
    controller.join_background(5)
    _assert_no_leaked_refs(controller)


def test_stream_cache_error_after_output_is_raised(gemini, fake, controller, storage_records):
    from llm_clients.exceptions import LLMError

    _run(gemini, _turn(0))
    fake.models.emit_then_raise = _cache_error()
    fake.caches.bg_gate = threading.Event()   # 裏の c2 が c1 を置き換える前に見る
    n_before = len(fake.models.calls)

    got: List[str] = []
    with pytest.raises(LLMError):
        for chunk in gemini.generate_stream(_turn(1), tools=[], continuous_cache=CC):
            if isinstance(chunk, str):
                got.append(chunk)
    assert got == ["partial"]
    assert len(fake.models.calls) == n_before + 1  # 送り直さない
    # 送り直しはしないが、使えないキャッシュは台帳から外す
    assert "cachedContents/c1" not in controller._continuous
    assert fake.caches.deleted == ["cachedContents/c1"]
    fake.caches.bg_gate.set()
    controller.join_background(5)
    _assert_no_leaked_refs(controller)


@pytest.mark.parametrize("runner", ["generate", "stream"])
def test_unusable_auto_cache_retries_once_without_cache(gemini, fake, controller, storage_records, runner):
    msgs = _turn(0)
    fake.models.errors_when_cached = [_cache_error()]
    if runner == "stream":
        assert _run(gemini, msgs, cc=None) == "ok"
    else:
        assert gemini.generate(msgs, tools=[]) == "ok"

    calls = fake.models.calls
    assert [c["config"].cached_content for c in calls] == ["cachedContents/c1", None]
    assert calls[1]["config"].system_instruction == SYS
    assert _texts(calls[1]["contents"]) == _uncached_texts(gemini, msgs)
    # 再利用の台帳から外れ、応答後に消す予約はその場で済ませる (二重に消さない)
    assert controller._entries == {}
    assert fake.caches.deleted == ["cachedContents/c1"]
    assert gemini._auto_cache_pending_cleanup is None


def test_unusable_explicit_cache_retries_once_without_cache(gemini, fake, controller, monkeypatch):
    GeminiClient._AUTO_CACHE_ENABLED = False
    monkeypatch.setenv("SAIVERSE_GEMINI_EXPLICIT_CACHE", "1")
    gemini._cache_config = {"type": "gemini_explicit", "min_tokens": 1024}
    msgs = _turn(0)
    fake.models.errors_when_cached = [_cache_error(400, "INVALID_ARGUMENT", "Invalid CachedContent name")]

    assert gemini.generate(msgs, tools=[], enable_cache=True, cache_ttl="5m") == "ok"
    calls = fake.models.calls
    assert [c["config"].cached_content for c in calls] == ["cachedContents/c1", None]
    assert calls[1]["config"].system_instruction == SYS
    assert controller._entries == {}


@pytest.mark.parametrize(
    "error, expected",
    [
        (_cache_error(429, "RESOURCE_EXHAUSTED", "Resource has been exhausted (e.g. check quota)."),
         "RateLimitError"),
        (_cache_error(404, "NOT_FOUND", "models/gemini-x is not found for API version v1beta"),
         "ModelNotFoundError"),
    ],
    ids=["429", "404-model"],
)
@pytest.mark.parametrize("runner", ["generate", "stream"])
def test_unrelated_error_with_cache_attached_is_not_a_cache_failure(
    gemini, fake, controller, storage_records, error, expected, runner,
):
    _run(gemini, _turn(0))
    fake.models.errors_when_cached = [error]
    fake.caches.bg_gate = threading.Event()   # 裏の c2 が c1 を置き換える前に見る
    n_before = len(fake.models.calls)

    with pytest.raises(Exception) as excinfo:
        if runner == "stream":
            _run(gemini, _turn(1))
        else:
            gemini.generate(_turn(1), tools=[], continuous_cache=CC)
    assert type(excinfo.value).__name__ == expected
    calls = fake.models.calls[n_before:]
    assert len(calls) == 1 and calls[0]["config"].cached_content == "cachedContents/c1"
    c1 = controller._continuous["cachedContents/c1"]   # 外さない
    assert not c1.retired and c1.in_use == 0
    assert fake.caches.deleted == []
    fake.caches.bg_gate.set()
    controller.join_background(5)
    _assert_no_leaked_refs(controller)


def test_cache_use_error_reason_is_conservative():
    reason = GeminiClient._cache_use_error_reason
    assert reason(_cache_error()) is not None
    assert reason(_cache_error(404, "NOT_FOUND", "cachedContents/abc not found"))
    assert reason(_cache_error(400, "INVALID_ARGUMENT", "Cached content is too old"))
    # キャッシュに触れていない / 対象外の状態コード / SDK の例外でない
    assert reason(_cache_error(403, "PERMISSION_DENIED", "API key not valid")) is None
    assert reason(_cache_error(429, "RESOURCE_EXHAUSTED", "cached content quota")) is None
    assert reason(RuntimeError("cachedContent not found")) is None


# ── 11. 同期作成の保存料金: 作った瞬間に 1 回だけ ──

def test_sync_create_storage_is_recorded_at_creation_even_if_generation_fails(
    gemini, fake, controller, storage_records,
):
    from llm_clients.exceptions import LLMError

    fake.models.errors_when_cached = [RuntimeError("boom")]
    with pytest.raises(LLMError):
        _run(gemini, _turn(0))
    assert storage_records == [(MODEL, 1000, 300, "air", "room")]
    assert gemini._pending_cache_storage is None
    _assert_no_leaked_refs(controller)

    # 同じ範囲を次に使う回は作らないので、計上も増えない
    _run(gemini, _turn(0))
    assert len(fake.caches.created) == 1
    assert storage_records == [(MODEL, 1000, 300, "air", "room")]


# ── 12. TTL 前の削除: 残りの秒数分を返金する ──

def test_superseded_cache_is_refunded_for_the_remainder(gemini, fake, controller, storage_records, refund_records):
    _run(gemini, _turn(0))
    _run(gemini, _turn(1))
    controller.join_background(5)

    assert fake.caches.deleted == ["cachedContents/c1"]
    assert len(refund_records) == 1
    model_id, tokens, remaining, persona, building = refund_records[0]
    assert (model_id, tokens, persona, building) == (MODEL, 1000, "air", "room")
    assert 250 < remaining <= 300


def _contents(n: int) -> List[Any]:
    return [
        types.Content(role="user" if i % 2 == 0 else "model", parts=[types.Part(text=f"m{i} " * 50)])
        for i in range(n)
    ]


def _account():
    return {"account_model": MODEL, "persona_id": "air", "building_id": "room"}


def test_born_superseded_cache_is_refunded(fake, controller, refund_records):
    longer = _contents(4)
    digests = prefix_digests(SYS, longer)
    long_res = controller.continuous_create_sync(
        fake, MODEL, "air", SYS, longer, digests, 300, **_account(),
    )
    controller.continuous_release(long_res.name)

    shorter = longer[:2]
    short_digests = prefix_digests(SYS, shorter)
    reservation = threading.Event()
    short_res = controller.continuous_create_reserved(
        fake, MODEL, "air", SYS, shorter, short_digests, 300, reservation, **_account(),
    )
    assert short_res.created
    assert fake.caches.deleted == [short_res.name]          # 誰にも使われないので即削除
    assert short_res.name not in controller._continuous
    assert len(refund_records) == 1
    model_id, tokens, remaining, persona, building = refund_records[0]
    assert (model_id, tokens, persona, building) == (MODEL, short_res.cached_tokens, "air", "room")
    assert 250 < remaining <= 300


def test_failed_delete_is_not_refunded(fake, controller, refund_records):
    first = _contents(2)
    res1 = controller.continuous_create_sync(
        fake, MODEL, "air", SYS, first, prefix_digests(SYS, first), 300, **_account(),
    )
    controller.continuous_release(res1.name)
    fake.caches.fail_delete = True
    second = _contents(4)
    controller.continuous_create_sync(
        fake, MODEL, "air", SYS, second, prefix_digests(SYS, second), 300, **_account(),
    )
    assert res1.name not in controller._continuous
    assert fake.caches.deleted == []
    assert refund_records == []


def test_expired_cache_is_not_refunded(fake, controller, refund_records):
    first = _contents(2)
    digests = prefix_digests(SYS, first)
    res = controller.continuous_create_sync(
        fake, MODEL, "air", SYS, first, digests, 300, **_account(),
    )
    controller.continuous_release(res.name)
    controller._continuous[res.name].expire_at = 0.0     # TTL が過ぎた
    assert controller.continuous_acquire(MODEL, "air", digests) is None
    assert controller._continuous == {}
    assert fake.caches.deleted == []
    assert refund_records == []


# ── 境目の位置 (変換で system が抜け、中身の無い tool_calls が落ちる) ──

def test_boundary_index_survives_dropped_messages(gemini):
    msgs = [
        {"role": "system", "content": "a"},
        {"role": "user", "content": "q"},
        {"role": "system", "content": "b"},
        {"role": "assistant", "content": "", "tool_calls": []},  # 何も作らない
        {"role": "assistant", "content": "r", "metadata": {CACHE_BOUNDARY_METADATA_KEY: True}},
        {"role": "user", "content": "next"},
    ]
    sys_msg, contents, boundary = gemini._convert_messages_with_boundary(msgs)
    assert sys_msg == "a\nb"
    assert _texts(contents) == ["q", "r", "next"]
    assert boundary == 1
    # 従来の 2 値の口はそのまま
    assert gemini._convert_messages(msgs) == (sys_msg, contents)


def test_no_marker_means_no_boundary(gemini):
    _, _, boundary = gemini._convert_messages_with_boundary([
        {"role": "user", "content": "q"}, {"role": "assistant", "content": "r"},
    ])
    assert boundary is None


# ── SEA 側: 境目の印と、返事の呼び出しだけに付く kwarg ──

def test_mark_cache_boundary_skips_this_pulse_and_does_not_mutate():
    reply = {"role": "assistant", "content": "prev", "metadata": {"x": 1}}
    in_pulse = {"role": "assistant", "content": "mid", "pulse_id": PULSE}
    hist = [{"role": "user", "content": "q"}, reply, {"role": "user", "content": "u"}, in_pulse]
    marked = mark_cache_boundary(hist, PULSE)

    assert marked[1]["metadata"] == {"x": 1, CACHE_BOUNDARY_METADATA_KEY: True}
    assert CACHE_BOUNDARY_METADATA_KEY not in (marked[3].get("metadata") or {})
    assert reply["metadata"] == {"x": 1}  # 元の dict は書き換えない
    assert hist[1] is reply
    assert mark_cache_boundary([{"role": "user", "content": "q"}], PULSE) == [
        {"role": "user", "content": "q"},
    ]


def test_continuous_cache_kwargs_only_for_user_reply_conversation_line():
    from sea.pulse_context import Aspect, PulseContext
    from sea.runtime_llm import _continuous_cache_kwargs

    persona = SimpleNamespace(persona_id="air")

    ctx = PulseContext(pulse_id="p")
    ctx.push_line(aspect=Aspect.CONVERSATION)
    user_state = {"_pulse_type": "user", "_pulse_context": ctx}
    assert _continuous_cache_kwargs(user_state, persona, "room") == {
        "continuous_cache": {"persona_id": "air", "building_id": "room"},
    }
    # Pulse 開始前 (ライン未 push) も user なら対象
    assert _continuous_cache_kwargs({"_pulse_type": "user"}, persona, "room")

    for pulse_type in ("schedule", "auto", "meta_judgment", "work_session", None):
        assert _continuous_cache_kwargs({"_pulse_type": pulse_type}, persona, "room") == {}

    worker = PulseContext(pulse_id="p")
    worker.push_line(aspect=Aspect.CONVERSATION)
    worker.push_line(aspect=Aspect.WORKER)
    assert _continuous_cache_kwargs(
        {"_pulse_type": "user", "_pulse_context": worker}, persona, "room",
    ) == {}
    assert _continuous_cache_kwargs(
        {"_pulse_type": "user", "_force_lightweight_model": True}, persona, "room",
    ) == {}


def test_other_clients_ignore_the_new_kwarg():
    """Gemini 以外のクライアントは未知の kwarg を ``**_`` で受け流す。"""
    import inspect

    from llm_clients.anthropic import AnthropicClient
    from llm_clients.ollama import OllamaClient
    from llm_clients.openai import OpenAIClient

    for cls in (AnthropicClient, OllamaClient, OpenAIClient):
        for meth in ("generate", "generate_stream"):
            fn = getattr(cls, meth)
            params = inspect.signature(fn).parameters.values()
            assert any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params), (cls, meth)


# ── 9. 設定 API ──

def test_config_api_round_trip_for_continuous_fields(monkeypatch):
    from api.routes import admin, config as config_routes

    written: Dict[str, str] = {}
    monkeypatch.setattr(admin, "write_env_updates", lambda updates: written.update(updates))
    GeminiClient._AUTO_CACHE_ENABLED = False
    GeminiClient._CONTINUOUS_CACHE_ENABLED = False

    result = config_routes.set_gemini_auto_cache(config_routes.GeminiAutoCacheRequest(
        enabled=True, keep_seconds=0, continuous=True, continuous_ttl_seconds=600,
    ))
    assert result["continuous"] is True
    assert result["continuous_ttl_seconds"] == 600
    assert GeminiClient._CONTINUOUS_CACHE_ENABLED is True
    assert GeminiClient.continuous_cache_active() is True
    assert written["SAIVERSE_GEMINI_CONTINUOUS_CACHE"] == "1"
    assert written["SAIVERSE_GEMINI_CONTINUOUS_CACHE_TTL_SECONDS"] == "600"

    got = config_routes.get_gemini_auto_cache()
    assert got["continuous"] is True
    assert got["continuous_ttl_seconds"] == 600
    assert (got["continuous_ttl_seconds_min"], got["continuous_ttl_seconds_max"]) == (120, 1200)

    # TTL は 120〜1200 秒に丸める
    config_routes.set_gemini_auto_cache(config_routes.GeminiAutoCacheRequest(
        enabled=True, continuous=True, continuous_ttl_seconds=99999,
    ))
    assert GeminiClient.continuous_cache_ttl_seconds() == 1200
    config_routes.set_gemini_auto_cache(config_routes.GeminiAutoCacheRequest(
        enabled=True, continuous=True, continuous_ttl_seconds=5,
    ))
    assert GeminiClient.continuous_cache_ttl_seconds() == 120


def test_config_api_old_client_does_not_turn_continuous_off(monkeypatch):
    from api.routes import admin, config as config_routes

    written: Dict[str, str] = {}
    monkeypatch.setattr(admin, "write_env_updates", lambda updates: written.update(updates))
    GeminiClient._CONTINUOUS_CACHE_ENABLED = True
    GeminiClient._CONTINUOUS_CACHE_TTL_SECONDS = 900

    result = config_routes.set_gemini_auto_cache(
        config_routes.GeminiAutoCacheRequest(enabled=True, keep_seconds=30)
    )
    assert result["continuous"] is True
    assert result["continuous_ttl_seconds"] == 900
    assert GeminiClient._CONTINUOUS_CACHE_ENABLED is True
    assert "SAIVERSE_GEMINI_CONTINUOUS_CACHE" not in written
    assert "SAIVERSE_GEMINI_CONTINUOUS_CACHE_TTL_SECONDS" not in written

    # 三択の「オフ」は両方 0 を書く
    config_routes.set_gemini_auto_cache(
        config_routes.GeminiAutoCacheRequest(enabled=False, continuous=False)
    )
    assert written["SAIVERSE_GEMINI_AUTO_CACHE"] == "0"
    assert written["SAIVERSE_GEMINI_CONTINUOUS_CACHE"] == "0"
    assert GeminiClient.continuous_cache_active() is False


# ── SEA の文脈組み立てを通した境目の印 ──

def _prepare(monkeypatch, pulse_type, *, preview_only=False):
    from sea.playbook_models import ContextRequirements
    from sea.runtime_context import prepare_context

    monkeypatch.setattr(
        "sea.head_pipeline.render_head_messages",
        lambda *a, **k: [{"role": "system", "content": "head"}],
    )
    hist = [
        {"role": "user", "content": "q", "id": "m1"},
        {"role": "assistant", "content": "r", "id": "m2", "pulse_id": "old"},
        {"role": "user", "content": "u", "id": "m3"},
        {"role": "assistant", "content": "mid", "id": "m4", "pulse_id": PULSE},
    ]
    persona = SimpleNamespace(
        persona_id="air",
        model="gemini-2.5-flash",
        history_manager=SimpleNamespace(
            get_recent_history_by_count=lambda limit, **k: [dict(m) for m in hist],
        ),
    )
    runtime = SimpleNamespace(
        manager=SimpleNamespace(),
        session_lifecycle=None,
        _enrich_history_with_attachments=lambda recent: recent,
    )
    return prepare_context(
        runtime, persona, "room", "u",
        ContextRequirements(history_depth="10messages", realtime_context=False),
        pulse_id=PULSE, pulse_type=pulse_type, preview_only=preview_only,
    )


def _marked_ids(messages):
    return [
        m.get("id") for m in messages
        if CACHE_BOUNDARY_METADATA_KEY in (m.get("metadata") or {})
    ]


def test_prepare_context_marks_last_prior_reply_in_user_pulse(monkeypatch):
    msgs = _prepare(monkeypatch, "user")
    assert [m.get("id") for m in msgs if m.get("id")] == ["m1", "m2", "m3", "m4"]
    # この Pulse 自身の発言 (m4) ではなく、Pulse 前の最後の AI の返事 (m2)
    assert _marked_ids(msgs) == ["m2"]


@pytest.mark.parametrize("pulse_type", ["schedule", "auto", None])
def test_prepare_context_does_not_mark_other_pulses(monkeypatch, pulse_type):
    assert _marked_ids(_prepare(monkeypatch, pulse_type)) == []


def test_prepare_context_does_not_mark_preview(monkeypatch):
    assert _marked_ids(_prepare(monkeypatch, "user", preview_only=True)) == []
