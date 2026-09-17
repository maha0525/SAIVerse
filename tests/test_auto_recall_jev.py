"""自動想起の Jev 選別層 (sea/auto_recall.py) のユニットテスト。

設計: docs/intent/auto_recall_jev_rerank.md

実 API は絶対に呼ばない。``saiverse.typesafe_client.evaluate_nouls`` を差し替えて、
採否の分岐と質問の組み立てだけを検証する (unified_recall と DB は
tests/test_auto_recall.py と同じ流儀でフェイクにする)。

固定する不変条件:
- OFF (既定) では evaluate_nouls が一度も呼ばれず、従来のしきい値判定のまま。
- ON では採否が Noul 確率だけで決まる (cosine しきい値 0.86 と message ソースの
  底上げ +0.02 はどちらも使われない)。
- floor 未満の候補は API に渡らない。
- API が使えなかったターン (応答の欠落・モジュール読み込み失敗を含む) は従来の
  しきい値判定へ静かに戻る。
- Jev は入場の門であって退場の門ではない (台帳に入った記憶の退場は粘着仕様が握る)。
"""

import logging
import re
import sqlite3
import sys
from unittest.mock import patch

import pytest

from sai_memory.memory.storage import add_message, init_db
from sai_memory.unified_recall import RecallHit
from sea import auto_recall
from saiverse.typesafe_client import TypeSafeUnavailable

PERSONA = "jev_test_persona"
THREAD = "jev_test_persona:__persona__"

# env をまっさらにして既定値で走らせる対象 (test_auto_recall.py と同じ流儀)。
_ENV_KEYS = [
    "SAIVERSE_AUTO_RECALL_THRESHOLD",
    "SAIVERSE_AUTO_RECALL_STICKY_TURNS",
    "SAIVERSE_AUTO_RECALL_QUERY_MESSAGES",
    "SAIVERSE_AUTO_RECALL_TOPK",
    "SAIVERSE_AUTO_RECALL_MSG_THRESHOLD_OFFSET",
    "SAIVERSE_AUTO_RECALL_ENTITY_AMBIENT_COUNT",
    "SAIVERSE_AUTO_RECALL_JEV",
    "SAIVERSE_AUTO_RECALL_JEV_FLOOR",
    "SAIVERSE_AUTO_RECALL_JEV_THRESHOLD",
    "SAIVERSE_AUTO_RECALL_JEV_TIMEOUT",
    "SAIVERSE_AUTO_RECALL_JEV_CONTEXT_MESSAGES",
    "SAIVERSE_MEDIA_RECALL_ENABLED",
    "TYPESAFE_API_KEY",
]


def _hit(source_type, source_id, *, embed_score, title="タイトル", content="内容テキスト"):
    return RecallHit(
        source_type=source_type,
        source_id=source_id,
        title=title,
        content=content,
        score=0.01,
        uri=f"saiverse://self/{source_type}/{source_id}",
        embed_score=embed_score,
        chronicle_entry_id=None,
    )


def _msgs(*pairs):
    out = []
    for p in pairs:
        m = {"role": p[0], "content": p[1]}
        if len(p) >= 3:
            m["id"] = p[2]
        out.append(m)
    return out


class _FakeJev:
    """evaluate_nouls の差し替え。呼び出しを記録し、タイトルから Noul を引いて返す。

    本物のクライアントと同じ契約を守る: ``noul_by_title`` に無いタイトル (= 応答に
    answer が無かった候補) があれば部分回答なので ``TypeSafeUnavailable`` を投げる。
    """

    def __init__(self, noul_by_title=None, *, raises=None):
        self.noul_by_title = noul_by_title or {}
        self.raises = raises
        self.calls = []

    def __call__(self, state, questions, *, timeout, model="jev-latest"):
        self.calls.append({"state": state, "questions": questions, "timeout": timeout, "model": model})
        if self.raises is not None:
            raise self.raises
        memories = state["memories"]
        nouls = {}
        for qid in questions:
            title = memories[qid]["title"]
            if title not in self.noul_by_title:
                raise TypeSafeUnavailable(f"no answer for question {qid!r}")
            nouls[qid] = self.noul_by_title[title]
        return nouls, {"input_tokens": 100, "output_tokens": 0}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    auto_recall.reset_ledger(PERSONA)
    yield
    auto_recall.reset_ledger(PERSONA)


@pytest.fixture
def jev_on(monkeypatch):
    monkeypatch.setenv("SAIVERSE_AUTO_RECALL_JEV", "1")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-not-real")


def _run(hits, messages, fake_jev):
    with patch("sai_memory.unified_recall.unified_recall", return_value=hits), \
         patch("sea.auto_recall._fetch_memopedia_titles", return_value=[]), \
         patch("saiverse.typesafe_client.evaluate_nouls", fake_jev):
        return auto_recall.run_auto_recall(
            conn=object(), embedder=object(), messages=messages,
            persona_id=PERSONA, thread_id=THREAD,
        )


# ---------------------------------------------------------------------------
# OFF (既定)
# ---------------------------------------------------------------------------

def test_off_never_calls_jev_and_keeps_threshold_behavior():
    fake = _FakeJev({"低スコア記憶": 0.99})
    res = _run(
        [_hit("fragment", "f1", embed_score=0.80, title="低スコア記憶")],
        _msgs(("user", "話題")),
        fake,
    )
    assert fake.calls == []
    # 0.80 < 0.86 (既定しきい値) なので従来どおり落ちる。
    assert res.injected is False


def test_off_keeps_accepting_above_threshold():
    fake = _FakeJev({"高スコア記憶": 0.0})
    res = _run(
        [_hit("fragment", "f1", embed_score=0.90, title="高スコア記憶")],
        _msgs(("user", "話題")),
        fake,
    )
    assert fake.calls == []
    assert res.injected is True
    assert "高スコア記憶" in res.block


def test_toggle_without_api_key_stays_off(monkeypatch):
    monkeypatch.setenv("SAIVERSE_AUTO_RECALL_JEV", "1")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert auto_recall.is_jev_rerank_enabled() is False

    fake = _FakeJev({"記憶": 0.99})
    res = _run([_hit("fragment", "f1", embed_score=0.80, title="記憶")], _msgs(("user", "話題")), fake)
    assert fake.calls == []
    assert res.injected is False


def test_enabled_when_toggle_and_key_present(jev_on):
    assert auto_recall.is_jev_rerank_enabled() is True


# ---------------------------------------------------------------------------
# ON: 採否は Noul 確率で決まる
# ---------------------------------------------------------------------------

def test_low_embed_high_noul_is_accepted(jev_on):
    # 0.80 は旧しきい値 0.86 未満だが、Jev が「浮かぶ」と判断したので採用される。
    fake = _FakeJev({"拾い直された記憶": 0.9})
    res = _run(
        [_hit("fragment", "f1", embed_score=0.80, title="拾い直された記憶")],
        _msgs(("user", "話題")),
        fake,
    )
    assert len(fake.calls) == 1
    assert res.injected is True
    assert "拾い直された記憶" in res.block
    assert res.accepted_count == 1


def test_high_embed_low_noul_is_rejected(jev_on):
    # 0.90 は旧しきい値を超えるが、Jev が「無関係」と判断したので落ちる。
    fake = _FakeJev({"ノイズ記憶": 0.1})
    res = _run(
        [_hit("fragment", "f1", embed_score=0.90, title="ノイズ記憶")],
        _msgs(("user", "話題")),
        fake,
    )
    assert len(fake.calls) == 1
    assert res.injected is False
    assert res.accepted_count == 0


def test_noul_threshold_env_is_honored(jev_on, monkeypatch):
    monkeypatch.setenv("SAIVERSE_AUTO_RECALL_JEV_THRESHOLD", "0.8")
    fake = _FakeJev({"境界の記憶": 0.6})
    res = _run(
        [_hit("fragment", "f1", embed_score=0.90, title="境界の記憶")],
        _msgs(("user", "話題")),
        fake,
    )
    assert res.injected is False


@pytest.mark.parametrize("bad_value", ["1.5", "-0.1"], ids=["above_one", "negative"])
def test_out_of_range_noul_threshold_falls_back_to_default(jev_on, monkeypatch, caplog, bad_value):
    """0〜1 の外の指定はクランプせず既定 0.5 に戻し、WARNING を残す。

    そのまま使うと 1 超で全候補却下・0 未満で全候補採用となり、設定の打ち間違いが
    「Jev が効いていない」状態へ静かに化ける。
    """
    monkeypatch.setenv("SAIVERSE_AUTO_RECALL_JEV_THRESHOLD", bad_value)
    caplog.set_level(logging.WARNING, logger="saiverse.auto_recall")

    fake = _FakeJev({"採用される記憶": 0.6, "落とされる記憶": 0.3})
    res = _run(
        [
            _hit("fragment", "f1", embed_score=0.87, title="採用される記憶"),
            _hit("fragment", "f2", embed_score=0.87, title="落とされる記憶"),
        ],
        _msgs(("user", "話題")),
        fake,
    )

    # 既定 0.5 で動いている (1.5 のままなら両方落ち、-0.1 のままなら両方通る)。
    assert res.injected is True
    assert "採用される記憶" in res.block
    assert "落とされる記憶" not in res.block

    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "JEV_THRESHOLD" in r.getMessage()
    ]
    assert len(warnings) == 1


def test_valid_noul_threshold_does_not_warn(jev_on, monkeypatch, caplog):
    monkeypatch.setenv("SAIVERSE_AUTO_RECALL_JEV_THRESHOLD", "0.8")
    caplog.set_level(logging.WARNING, logger="saiverse.auto_recall")
    fake = _FakeJev({"記憶": 0.9})
    _run([_hit("fragment", "f1", embed_score=0.90, title="記憶")], _msgs(("user", "話題")), fake)
    assert not [r for r in caplog.records if "JEV_THRESHOLD" in r.getMessage()]


@pytest.mark.parametrize("bad_value", ["nan", "-0.1", "1.5"], ids=["nan", "negative", "above_one"])
def test_out_of_range_floor_falls_back_to_default(monkeypatch, caplog, bad_value):
    """0〜1 の外・非有限の floor は既定 0.78 に戻し、WARNING を残す。

    nan や負値をそのまま使うと下限比較が常に不成立になり、floor 未満のはずの候補まで
    全部外部 API へ送られる (プライバシー記述と費用見積もりの前提が警告なしに外れる)。
    """
    monkeypatch.setenv("SAIVERSE_AUTO_RECALL_JEV_FLOOR", bad_value)
    caplog.set_level(logging.WARNING, logger="saiverse.auto_recall")
    assert auto_recall.get_jev_floor() == 0.78
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "JEV_FLOOR" in r.getMessage()
    ]
    assert len(warnings) == 1


@pytest.mark.parametrize("bad_value", ["0", "-2", "nan"], ids=["zero", "negative", "nan"])
def test_non_positive_timeout_falls_back_to_default(monkeypatch, caplog, bad_value):
    """0 以下・非有限のタイムアウトは既定 2.5 秒に戻し、WARNING を残す。

    そのまま使うと全呼び出しが即座に締切超過になり、フォールバックで会話は続くが、
    置き去りワーカーが毎ターン生まれて「Jev が効かないのに課金だけ発生する」状態へ
    静かに化ける (しきい値の範囲ガードと同じ型の設定ミス)。
    """
    monkeypatch.setenv("SAIVERSE_AUTO_RECALL_JEV_TIMEOUT", bad_value)
    caplog.set_level(logging.WARNING, logger="saiverse.auto_recall")
    assert auto_recall.get_jev_timeout() == 2.5
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "JEV_TIMEOUT" in r.getMessage()
    ]
    assert len(warnings) == 1


@pytest.mark.parametrize("bad_value", ["0", "-3"], ids=["zero", "negative"])
def test_context_messages_below_one_falls_back_to_default(monkeypatch, caplog, bad_value):
    """1 未満の会話件数は既定 6 に戻し、WARNING を残す。

    クエリ側の「0 以下 = 全件」の慣習をここで踏襲すると、会話履歴の全件が外部 API へ
    送られ、「会話本文は 1 件 500 字まで」という有界性の根拠が件数側から崩れる。
    """
    monkeypatch.setenv("SAIVERSE_AUTO_RECALL_JEV_CONTEXT_MESSAGES", bad_value)
    caplog.set_level(logging.WARNING, logger="saiverse.auto_recall")
    assert auto_recall.get_jev_context_messages() == 6
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "JEV_CONTEXT_MESSAGES" in r.getMessage()
    ]
    assert len(warnings) == 1


def test_message_offset_not_applied_in_jev_path(jev_on):
    # message ソースの底上げ (実効 0.88) は Jev 経路では使われない。
    fake = _FakeJev({"過去の会話": 0.9})
    res = _run(
        [_hit("message", "m999", embed_score=0.87, title="過去の会話")],
        _msgs(("user", "こんにちは", "m1")),
        fake,
    )
    assert res.injected is True
    assert len(fake.calls) == 1


def test_partial_answer_falls_back_to_cosine_threshold(jev_on):
    # 一部の候補にしか answer が返らない応答はクライアントが TypeSafeUnavailable に
    # するので、そのターン全体が従来のしきい値判定へ戻る (答えの無い候補だけを
    # 静かに不採用にはしない)。
    fake = _FakeJev({})
    res = _run(
        [
            _hit("fragment", "f1", embed_score=0.90, title="しきい値超え"),
            _hit("fragment", "f2", embed_score=0.80, title="しきい値未満"),
        ],
        _msgs(("user", "話題")),
        fake,
    )
    assert len(fake.calls) == 1
    assert res.injected is True
    assert "しきい値超え" in res.block
    assert "しきい値未満" not in res.block


# ---------------------------------------------------------------------------
# floor (Jev に渡す候補の下限)
# ---------------------------------------------------------------------------

def test_below_floor_candidate_is_not_sent_to_jev(jev_on):
    fake = _FakeJev({"床の上": 0.9, "床の下": 0.9})
    res = _run(
        [
            _hit("fragment", "f1", embed_score=0.85, title="床の上"),
            _hit("fragment", "f2", embed_score=0.70, title="床の下"),
        ],
        _msgs(("user", "話題")),
        fake,
    )
    assert len(fake.calls) == 1
    sent_titles = {m["title"] for m in fake.calls[0]["state"]["memories"].values()}
    assert sent_titles == {"床の上"}
    assert res.injected is True
    assert "床の上" in res.block
    assert "床の下" not in res.block


def test_no_candidate_above_floor_skips_api_call(jev_on):
    fake = _FakeJev({"床の下": 0.9})
    res = _run(
        [_hit("fragment", "f1", embed_score=0.70, title="床の下")],
        _msgs(("user", "話題")),
        fake,
    )
    assert fake.calls == []
    assert res.injected is False


def test_floor_env_is_honored(jev_on, monkeypatch):
    monkeypatch.setenv("SAIVERSE_AUTO_RECALL_JEV_FLOOR", "0.60")
    fake = _FakeJev({"低いが床の上": 0.9})
    res = _run(
        [_hit("fragment", "f1", embed_score=0.65, title="低いが床の上")],
        _msgs(("user", "話題")),
        fake,
    )
    assert len(fake.calls) == 1
    assert res.injected is True


def test_keyword_only_hit_never_reaches_jev(jev_on):
    # embed_score なし (キーワードのみ) は Jev 経路でも採用しない。
    fake = _FakeJev({"キーワードのみ": 0.99})
    res = _run(
        [_hit("fragment", "f1", embed_score=None, title="キーワードのみ")],
        _msgs(("user", "話題")),
        fake,
    )
    assert fake.calls == []
    assert res.injected is False


def test_message_already_in_context_never_reaches_jev(jev_on):
    fake = _FakeJev({"コンテキスト内": 0.99})
    res = _run(
        [_hit("message", "m123", embed_score=0.95, title="コンテキスト内")],
        _msgs(("user", "こんにちは", "m123")),
        fake,
    )
    assert fake.calls == []
    assert res.injected is False


# ---------------------------------------------------------------------------
# 失敗時のフォールバック
# ---------------------------------------------------------------------------

def test_unavailable_falls_back_to_cosine_threshold(jev_on):
    fake = _FakeJev(raises=TypeSafeUnavailable("HTTP 500"))
    res = _run(
        [
            _hit("fragment", "f1", embed_score=0.90, title="しきい値超え"),
            _hit("fragment", "f2", embed_score=0.80, title="しきい値未満"),
        ],
        _msgs(("user", "話題")),
        fake,
    )
    assert len(fake.calls) == 1
    assert res.injected is True
    # 従来の 0.86 判定に戻る。
    assert "しきい値超え" in res.block
    assert "しきい値未満" not in res.block


def test_unexpected_exception_also_falls_back(jev_on):
    fake = _FakeJev(raises=RuntimeError("boom"))
    res = _run(
        [_hit("fragment", "f1", embed_score=0.90, title="しきい値超え")],
        _msgs(("user", "話題")),
        fake,
    )
    assert res.injected is True
    assert "しきい値超え" in res.block


def test_client_module_import_failure_falls_back(jev_on, caplog):
    """typesafe_client の読み込み自体が失敗しても、会話の同期経路は落ちない。"""
    caplog.set_level(logging.WARNING, logger="saiverse.auto_recall")
    hits = [
        _hit("fragment", "f1", embed_score=0.87, title="しきい値超え"),
        _hit("fragment", "f2", embed_score=0.80, title="しきい値未満"),
    ]
    with patch("sai_memory.unified_recall.unified_recall", return_value=hits), \
         patch("sea.auto_recall._fetch_memopedia_titles", return_value=[]), \
         patch.dict(sys.modules, {"saiverse.typesafe_client": None}):
        res = auto_recall.run_auto_recall(
            conn=object(), embedder=object(), messages=_msgs(("user", "話題")),
            persona_id=PERSONA, thread_id=THREAD,
        )

    # import が本当に失敗した経路を通っていること (通らなければ実 API を叩いてしまう)。
    assert [r for r in caplog.records if "could not prepare the request" in r.getMessage()]

    # 従来の 0.86 判定に戻る。
    assert res.injected is True
    assert "しきい値超え" in res.block
    assert "しきい値未満" not in res.block


# ---------------------------------------------------------------------------
# 設定ミスの警告
# ---------------------------------------------------------------------------

def test_floor_above_acceptance_threshold_warns(jev_on, monkeypatch, caplog):
    """floor が採用しきい値より高いと、従来なら浮かぶ記憶が Jev に渡らず落ちる。

    警告が出るのは候補検索にヒットがあったターン (= 実際に記憶が落ちうるターン)。
    """
    monkeypatch.setenv("SAIVERSE_AUTO_RECALL_JEV_FLOOR", "0.95")
    caplog.set_level(logging.WARNING, logger="saiverse.auto_recall")

    fake = _FakeJev({"記憶": 0.9})
    _run([_hit("fragment", "f1", embed_score=0.90, title="記憶")], _msgs(("user", "話題")), fake)

    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "JEV_FLOOR" in r.getMessage()
    ]
    assert len(warnings) == 1


def test_no_warning_when_floor_below_threshold(jev_on, caplog):
    caplog.set_level(logging.WARNING, logger="saiverse.auto_recall")
    fake = _FakeJev({"記憶": 0.9})
    _run([_hit("fragment", "f1", embed_score=0.90, title="記憶")], _msgs(("user", "話題")), fake)
    assert not [r for r in caplog.records if "JEV_FLOOR" in r.getMessage()]


def test_floor_warning_is_silent_on_turns_without_hits(jev_on, monkeypatch, caplog):
    """ヒットが 1 件も無いターンでは設定ミスの警告を出さない。

    警告の意味は「この設定で記憶が落ちている」なので、落ちる記憶が存在しえない
    ターンにも出すと、実際に起きたことと警告がずれて毎ターンのノイズになる。
    """
    monkeypatch.setenv("SAIVERSE_AUTO_RECALL_JEV_FLOOR", "0.95")
    caplog.set_level(logging.WARNING, logger="saiverse.auto_recall")

    fake = _FakeJev({})
    res = _run([], _msgs(("user", "話題")), fake)

    assert fake.calls == []
    assert res.injected is False
    assert not [r for r in caplog.records if "JEV_FLOOR" in r.getMessage()]


# ---------------------------------------------------------------------------
# 粘着台帳との関係 (Jev は入場の門であって退場の門ではない)
# ---------------------------------------------------------------------------

def test_jev_rejection_does_not_evict_sticky_item(jev_on, monkeypatch):
    """一度台帳に入った記憶は、Jev が拒否しても sticky_turns の間は注入され続ける。

    急に消えるのではなく数ターンかけて薄れるのが §4.3 の設計意図 (従来方式で
    cosine しきい値を割ったときとまったく同じ扱い)。
    """
    monkeypatch.setenv("SAIVERSE_AUTO_RECALL_STICKY_TURNS", "2")
    hits = [_hit("fragment", "f1", embed_score=0.90, title="粘着する記憶", content="記憶の本文")]

    # ターン1: Jev が採用 → 台帳に入る (stale=0)。
    fake = _FakeJev({"粘着する記憶": 0.9})
    r1 = _run(hits, _msgs(("user", "その話")), fake)
    assert r1.injected is True
    assert "粘着する記憶" in r1.block

    # ターン2以降: 同じ候補を Jev が拒否 (noul 低) しても、粘着ウィンドウの間は残る。
    reject = _FakeJev({"粘着する記憶": 0.1})
    r2 = _run(hits, _msgs(("user", "別の話")), reject)          # stale=1
    assert r2.injected is True
    assert "粘着する記憶" in r2.block
    assert r2.accepted_count == 0

    r3 = _run(hits, _msgs(("user", "さらに別の話")), reject)      # stale=2
    assert r3.injected is True
    assert "粘着する記憶" in r3.block

    # sticky_turns=2 を超えたターンで初めて台帳から消える。
    r4 = _run(hits, _msgs(("user", "まだ別の話")), reject)        # stale=3 > 2
    assert r4.injected is False


# ---------------------------------------------------------------------------
# 質問と state の組み立て
# ---------------------------------------------------------------------------

def test_question_construction(jev_on):
    fake = _FakeJev({"記憶ひとつめ": 0.9, "記憶ふたつめ": 0.9})
    _run(
        [
            _hit("fragment", "f1", embed_score=0.90, title="記憶ひとつめ", content="本文1"),
            _hit("memopedia", "p1", embed_score=0.88, title="記憶ふたつめ", content="本文2"),
        ],
        _msgs(("user", "アイフィの話をしていた")),
        fake,
    )
    call = fake.calls[0]
    questions = call["questions"]
    memories = call["state"]["memories"]

    # qid は m0, m1, ... と振られ、state の memories と対応する。
    assert list(questions) == ["m0", "m1"]
    assert memories["m0"] == {"title": "記憶ひとつめ", "content": "本文1"}
    assert memories["m1"] == {"title": "記憶ふたつめ", "content": "本文2"}

    # 各質問は自分の qid を参照し、true/false の基準を持つ。
    assert "`memories.m0`" in questions["m0"]["instructions"]
    assert "`memories.m1`" in questions["m1"]["instructions"]
    assert set(questions["m0"]["criteria"]) == {"true", "false"}

    assert call["timeout"] == pytest.approx(2.5)


def test_state_carries_recent_conversation(jev_on):
    fake = _FakeJev({"記憶": 0.9})
    _run(
        [_hit("fragment", "f1", embed_score=0.90, title="記憶")],
        _msgs(("user", "ひとつめの発話"), ("assistant", "ふたつめの応答"), ("user", "みっつめの発話")),
        fake,
    )
    conversation = fake.calls[0]["state"]["conversation"]
    assert conversation == [
        {"role": "user", "text": "ひとつめの発話"},
        {"role": "assistant", "text": "ふたつめの応答"},
        {"role": "user", "text": "みっつめの発話"},
    ]


def test_conversation_is_limited_to_recent_messages(jev_on, monkeypatch):
    monkeypatch.setenv("SAIVERSE_AUTO_RECALL_JEV_CONTEXT_MESSAGES", "2")
    fake = _FakeJev({"記憶": 0.9})
    _run(
        [_hit("fragment", "f1", embed_score=0.90, title="記憶")],
        _msgs(("user", "古い発話"), ("assistant", "中くらいの応答"), ("user", "新しい発話")),
        fake,
    )
    conversation = fake.calls[0]["state"]["conversation"]
    assert [c["text"] for c in conversation] == ["中くらいの応答", "新しい発話"]


def test_long_conversation_message_is_clipped(jev_on):
    """会話本文は 1 件あたり 500 字で切る (超過分は省略記号 1 字)。

    上限が無いと 1 ターンのペイロードが発話の長さに引きずられ、費用の見積もりが
    崩れるうえ、長話のターンほど絶対締切に掛かって Jev が効かなくなる。
    """
    fake = _FakeJev({"記憶": 0.9})
    _run(
        [_hit("fragment", "f1", embed_score=0.90, title="記憶")],
        _msgs(("user", "短い発話"), ("assistant", "あ" * 2000)),
        fake,
    )

    texts = [c["text"] for c in fake.calls[0]["state"]["conversation"]]
    assert all(len(t) <= 501 for t in texts)
    # 上限以下の発話はそのまま、超過分だけが切られる。
    assert texts[0] == "短い発話"
    assert texts[1] == "あ" * auto_recall._JEV_CONVERSATION_TEXT_LIMIT + "…"


def test_attachment_summaries_reach_jev_when_media_recall_is_on(jev_on, monkeypatch):
    """検索クエリに入る添付の概要は、Jev の判断材料にも入る。

    写真をきっかけに検索が拾ってきた候補を、写真を知らない Jev が落としてしまう
    非対称を避けるため (docs/intent/auto_recall_jev_rerank.md 判定の流れ 2)。
    """
    monkeypatch.setenv("SAIVERSE_MEDIA_RECALL_ENABLED", "1")
    fake = _FakeJev({"記憶": 0.9})
    messages = [
        {"role": "user", "content": "これ見て",
         "metadata": {"images": [{"summary": "机の上に置かれた黒猫のぬいぐるみの写真"}]}},
    ]
    _run([_hit("fragment", "f1", embed_score=0.90, title="記憶")], messages, fake)

    state = fake.calls[0]["state"]
    assert state["attachments"] == ["机の上に置かれた黒猫のぬいぐるみの写真"]
    # 会話本文はこれまでどおり入る。
    assert [c["text"] for c in state["conversation"]] == ["これ見て"]


def test_attachment_only_message_still_reaches_jev(jev_on, monkeypatch):
    """本文が空で添付だけのメッセージでも、概要は Jev に渡る。"""
    monkeypatch.setenv("SAIVERSE_MEDIA_RECALL_ENABLED", "1")
    fake = _FakeJev({"記憶": 0.9})
    messages = [
        {"role": "user", "content": "",
         "metadata": {"media": [{"summary": "雨音が続く 10 秒の録音"}]}},
    ]
    _run([_hit("fragment", "f1", embed_score=0.90, title="記憶")], messages, fake)

    state = fake.calls[0]["state"]
    assert state["attachments"] == ["雨音が続く 10 秒の録音"]
    assert state["conversation"] == []


def test_media_recall_off_keeps_state_shape_unchanged(jev_on):
    """メディア想起 OFF (既定) では attachments キー自体を入れない。"""
    fake = _FakeJev({"記憶": 0.9})
    messages = [
        {"role": "user", "content": "これ見て",
         "metadata": {"images": [{"summary": "机の上に置かれた黒猫のぬいぐるみの写真"}]}},
    ]
    _run([_hit("fragment", "f1", embed_score=0.90, title="記憶")], messages, fake)

    state = fake.calls[0]["state"]
    assert set(state) == {"conversation", "memories"}


# ---------------------------------------------------------------------------
# 拾い上げの拡張 (ON のときだけ) — キーワード抽出
# ---------------------------------------------------------------------------

@pytest.fixture
def memory_conn():
    """messages テーブルだけを持つ空の memory.db (キーワード件数の検算用)。"""
    conn = init_db(":memory:")
    yield conn
    conn.close()


def _fill(conn, content, times=1):
    for _ in range(times):
        add_message(conn, thread_id="t1", role="user", content=content)


def test_rare_word_is_kept_and_common_word_is_dropped(memory_conn):
    """ありふれた語は捨て、珍しい語だけ残す。

    unified_recall ではキーワード一致数が第一ソートキーなので、常連の語を残すと
    毎ターン同じ大量のヒットが上位を占めて枠を食い潰す。
    """
    _fill(memory_conn, "エリスとの何気ない会話", times=60)   # ありふれた語
    _fill(memory_conn, "十条の商店街を歩いた", times=3)       # 珍しい語

    words = auto_recall._extract_recall_keywords(
        memory_conn, "ねーエリス、十条まで歩いてみた日のこと覚えてる？",
    )
    assert words == ["十条"]


def test_words_are_ordered_by_rarity_and_capped(memory_conn, monkeypatch):
    monkeypatch.setattr(auto_recall, "_KEYWORD_MAX_COUNT", 2)
    _fill(memory_conn, "アイフィの話", times=9)
    _fill(memory_conn, "十条の話", times=5)
    _fill(memory_conn, "散歩の話", times=1)

    words = auto_recall._extract_recall_keywords(memory_conn, "アイフィと十条を散歩した")
    assert words == ["散歩", "十条"]


def test_katakana_kanji_and_alnum_runs_are_extracted(memory_conn):
    _fill(memory_conn, "エリスと PostgreSQL の設定を直した")
    _fill(memory_conn, "十条へ行った")

    words = auto_recall._extract_recall_keywords(
        memory_conn, "エリス、PostgreSQL の話と十条の話をしたよね",
    )
    assert set(words) == {"エリス", "PostgreSQL", "十条"}


def test_word_absent_from_messages_is_dropped(memory_conn):
    # 0 件の語はキーワードに入れない (LIKE を 1 本無駄に撃つだけになる)。
    _fill(memory_conn, "十条へ行った")
    words = auto_recall._extract_recall_keywords(memory_conn, "十条と赤羽へ行った")
    assert words == ["十条"]


def test_like_metacharacters_in_a_keyword_are_escaped(memory_conn, monkeypatch):
    """件数の検算に使う LIKE で ``_`` がワイルドカードにならない。

    既定の抽出正規表現は英数・カタカナ・漢字しか拾わないのでメタ文字は出ないが、
    ここを緩めたときに「ありふれた語の判定が黙って壊れる」ことがないよう、
    エスケープ側を固定する (``a_b`` が ``axb`` まで数えると 61 件になり、
    珍しい語のはずが ``_KEYWORD_COMMON_LIMIT`` 超過で捨てられてしまう)。
    """
    monkeypatch.setattr(auto_recall, "_KEYWORD_PATTERN", re.compile(r"[A-Za-z0-9_]{2,}"))
    _fill(memory_conn, "識別子 axb を使った", times=60)
    _fill(memory_conn, "識別子 a_b を使った", times=1)

    words = auto_recall._extract_recall_keywords(memory_conn, "a_b の話")
    assert words == ["a_b"]


def test_extraction_failure_returns_empty_list(caplog):
    """DB 障害でも会話は止めない (呼び出し側は従来の split 経路へ戻る)。"""
    caplog.set_level(logging.WARNING, logger="saiverse.auto_recall")

    class _BrokenConn:
        def execute(self, *args, **kwargs):
            raise sqlite3.OperationalError("no such table: messages")

    assert auto_recall._extract_recall_keywords(_BrokenConn(), "十条まで歩いた") == []
    assert [r for r in caplog.records if "keyword extraction failed" in r.getMessage()]


def test_extraction_on_empty_query_returns_empty_list(memory_conn):
    assert auto_recall._extract_recall_keywords(memory_conn, "") == []
    assert auto_recall._extract_recall_keywords(memory_conn, "   ") == []


# ---------------------------------------------------------------------------
# 拾い上げの拡張 (ON のときだけ) — unified_recall へ渡す引数
# ---------------------------------------------------------------------------

def _capture_recall_call(conn, messages, fake_jev, hits=None):
    """run_auto_recall を 1 ターン回し、unified_recall に渡った kwargs を返す。"""
    captured = {}
    hits = hits if hits is not None else [_hit("fragment", "f1", embed_score=0.90, title="記憶")]

    def _fake_recall(_conn, _embedder, _query, **kwargs):
        captured.update(kwargs)
        return hits

    with patch("sai_memory.unified_recall.unified_recall", _fake_recall), \
         patch("sea.auto_recall._fetch_memopedia_titles", return_value=[]), \
         patch("saiverse.typesafe_client.evaluate_nouls", fake_jev):
        auto_recall.run_auto_recall(
            conn=conn, embedder=object(), messages=messages,
            persona_id=PERSONA, thread_id=THREAD,
        )
    return captured


def test_off_does_not_pass_the_sweep_arguments(memory_conn):
    """OFF (既定) では拾い上げの引数を 1 つも渡さない (挙動が 1 ビットも変わらない)。"""
    _fill(memory_conn, "十条の商店街を歩いた")
    captured = _capture_recall_call(
        memory_conn, _msgs(("user", "十条まで歩いた日のこと", "m1")), _FakeJev({"記憶": 0.9}),
    )
    assert set(captured) == {"topk", "search_chronicle", "search_memopedia",
                             "search_fragments", "search_messages"}


def test_on_passes_keywords_exclusions_and_allocations(jev_on, memory_conn):
    _fill(memory_conn, "十条の商店街を歩いた", times=3)
    captured = _capture_recall_call(
        memory_conn, _msgs(("user", "十条まで歩いた日のこと", "m1")), _FakeJev({"記憶": 0.9}),
    )
    assert captured["keywords"] == ["十条"]
    assert captured["exclude_message_ids"] == {"m1"}
    assert captured["source_allocations"] == {"fragment": 5, "memopedia": 1, "message": 3}
    # 既存の引数はそのまま。
    assert captured["topk"] == 8
    assert captured["search_chronicle"] is False


def test_on_with_no_extractable_keyword_falls_back_to_default_split(jev_on, memory_conn):
    """語が 1 つも残らないターンは ``keywords=None`` (= 従来の split 挙動)。"""
    captured = _capture_recall_call(
        memory_conn, _msgs(("user", "うん", "m1")), _FakeJev({"記憶": 0.9}),
    )
    assert captured["keywords"] is None
    # 残り 2 つは語が無くても渡る (発話の自席占領の除外と message 枠の拡張)。
    assert captured["exclude_message_ids"] == {"m1"}
    assert captured["source_allocations"]["message"] == 3


def test_non_conversational_messages_excluded_from_state(jev_on):
    fake = _FakeJev({"記憶": 0.9})
    messages = [
        {"role": "user", "content": "head 由来の合成メッセージ",
         "metadata": {"__memory_weave_context__": True}},
        {"role": "system", "content": "システム行"},
        {"role": "user", "content": "本物の発話"},
    ]
    _run([_hit("fragment", "f1", embed_score=0.90, title="記憶")], messages, fake)
    conversation = fake.calls[0]["state"]["conversation"]
    assert [c["text"] for c in conversation] == ["本物の発話"]
