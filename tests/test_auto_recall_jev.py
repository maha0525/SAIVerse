"""自動想起の「強化」経路 (sea/auto_recall.py) のユニットテスト。

設計: docs/intent/auto_recall_jev_rerank.md (選別の中身) と
docs/intent/reflex_judgment.md (答える側の決め方)。

実 API は絶対に呼ばない。``saiverse.reflex_judgment.evaluate`` を差し替えて、
採否の分岐と質問の組み立てだけを検証する (unified_recall と DB は
tests/test_auto_recall.py と同じ流儀でフェイクにする)。答える側の解決は本物を
通すので、役割に割り当てる偽モデル設定を MODEL_CONFIGS に差し込む。

固定する不変条件:
- ペルソナのスイッチ (AUTO_RECALL_ENHANCED) が OFF なら判定は一度も呼ばれない。
- スイッチが ON でも、モデルの役割「反射判断」にモデルが割り当てられていなければ
  従来のしきい値判定のまま (黙って費用が発生する経路を作らない)。
- 効いているときは採否が Noul 確率だけで決まる (cosine しきい値 0.86 と message
  ソースの底上げ +0.02 はどちらも使われない)。
- floor 未満の候補は判断に渡らない。
- 判断が使えなかったターン (応答の欠落・モジュール読み込み失敗を含む) は従来の
  しきい値判定へ静かに戻る。
- 反射判断は入場の門であって退場の門ではない (台帳に入った記憶の退場は粘着仕様が握る)。
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
from sea.eviction_plan import CONSUMED_PERCEPTION_KEY
from saiverse.reflex_judgment import ReflexJudgmentUnavailable

PERSONA = "jev_test_persona"
THREAD = "jev_test_persona:__persona__"

#: 役割に割り当てる偽モデルの設定キーと、その宛先のキーの env 名。env 名はこのモデル
#: 自身の名前空間のもの (saiverse/provider_security.py の ``model_credential_env``)。
#: 答える側の解決はキーと宛先の組を通常の会話クライアントと同じ照合へ通すので、
#: 偽の設定もその照合に通る正常形にしておく。
REFLEX_MODEL_KEY = "test-reflex-jev"
REFLEX_KEY_ENV = "SAIVERSE_MODEL_TEST_REFLEX_JEV_API_KEY"
#: ペルソナ個別の上書き (DB の ``AI.REFLEX_JUDGMENT_MODEL``) に使う、世界の既定とは
#: 別のモデル設定。キーの env 名もこのモデル自身の名前空間のものにする。
PERSONA_MODEL_KEY = "test-reflex-persona"
PERSONA_KEY_ENV = "SAIVERSE_MODEL_TEST_REFLEX_PERSONA_API_KEY"
#: 偽の宛先。照合は名前解決まで行うので、実在しないホスト名ではなくループバック。
REFLEX_BASE_URL = "http://127.0.0.1:8088"

# env をまっさらにして既定値で走らせる対象 (test_auto_recall.py と同じ流儀)。
_ENV_KEYS = [
    "SAIVERSE_AUTO_RECALL_THRESHOLD",
    "SAIVERSE_AUTO_RECALL_STICKY_TURNS",
    "SAIVERSE_AUTO_RECALL_QUERY_MESSAGES",
    "SAIVERSE_AUTO_RECALL_TOPK",
    "SAIVERSE_AUTO_RECALL_MSG_THRESHOLD_OFFSET",
    "SAIVERSE_AUTO_RECALL_ENTITY_AMBIENT_COUNT",
    "SAIVERSE_MEDIA_RECALL_ENABLED",
    "SAIVERSE_REFLEX_JUDGMENT_MODEL",
    "SAIVERSE_REFLEX_TIMEOUT_SECONDS",
    REFLEX_KEY_ENV,
    PERSONA_KEY_ENV,
]


def _reflex_model_config(**overrides):
    """同梱の TypeSafe 公式と同じ形の偽モデル定義 (provider の欄を畳み込んだ後の姿)。"""
    config = {
        "model": "jev-latest",
        "protocol": "jev_compat",
        "provider": "jev_compat",
        "base_url": REFLEX_BASE_URL,
        "api_key_env": REFLEX_KEY_ENV,
        "reflex_judgment": {
            "path": "/v1/systemone",
            "answers_key": "answers",
            "usage_key": "usage",
            "answer_fields": {"noul": "noul", "choice": "choice", "score": "score"},
            "usage_fields": {"input_tokens": "input_tokens", "output_tokens": "output_tokens"},
            "supported_types": ["noul", "choice", "score"],
        },
    }
    config.update(overrides)
    return config


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


def _perception(text):
    """送信直前に差し込まれる知覚ブロック (部屋の様子・通知) を 1 枚作る。

    実物 (sea/runtime_context.py::list_presented_perception_blocks) と同じ形 —
    role="user" / content は ``<system>`` 包み / metadata に
    ``CONSUMED_PERCEPTION_KEY``。
    """
    return {
        "role": "user",
        "content": f"<system>{text}</system>",
        "metadata": {
            "tags": ["internal", "event_message", "perception"],
            CONSUMED_PERCEPTION_KEY: True,
        },
    }


class _FakeJev:
    """reflex_judgment.evaluate の差し替え。呼び出しを記録し、タイトルから Noul を引く。

    本物の判断層と同じ契約を守る: ``noul_by_title`` に無いタイトル (= 応答に
    answer が無かった候補) があれば部分回答なので ``ReflexJudgmentUnavailable``。
    """

    def __init__(self, noul_by_title=None, *, raises=None):
        self.noul_by_title = noul_by_title or {}
        self.raises = raises
        self.calls = []

    def __call__(self, state, questions, *, timeout, backend=None, persona_id=None, transport=None):
        self.calls.append({
            "state": state, "questions": questions, "timeout": timeout,
            "backend": backend, "persona_id": persona_id,
        })
        if self.raises is not None:
            raise self.raises
        memories = state["memories"]
        nouls = {}
        for qid in questions:
            title = memories[qid]["title"]
            if title not in self.noul_by_title:
                raise ReflexJudgmentUnavailable(f"no answer for question {qid!r}")
            nouls[qid] = self.noul_by_title[title]
        return nouls, {"input_tokens": 100, "output_tokens": 0}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    from saiverse.reflex_judgment import reset_recent_outcomes

    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    auto_recall.reset_ledger(PERSONA)
    # 直近の呼び出しの記録はプロセス内に残る (設定画面の警告が読む)。テスト間で
    # 持ち越すと、他のファイルの「警告は空のはず」を壊す。
    reset_recent_outcomes()
    yield
    auto_recall.reset_ledger(PERSONA)
    reset_recent_outcomes()


@pytest.fixture
def reflex_on(monkeypatch):
    """モデルの役割「反射判断」に jev 互換の偽モデルを割り当てる。

    これがあって初めて、ペルソナのスイッチ ON が実際の判定に化ける。
    """
    from saiverse import model_configs

    monkeypatch.setenv("SAIVERSE_REFLEX_JUDGMENT_MODEL", REFLEX_MODEL_KEY)
    monkeypatch.setenv(REFLEX_KEY_ENV, "test-key-not-real")
    monkeypatch.setattr(
        model_configs, "MODEL_CONFIGS", {REFLEX_MODEL_KEY: _reflex_model_config()},
    )


@pytest.fixture
def reflex_on_with_persona_model(monkeypatch):
    """世界の既定に加えて、ペルソナ個別の上書きに使える別のモデル設定も用意する。"""
    from saiverse import model_configs

    monkeypatch.setenv("SAIVERSE_REFLEX_JUDGMENT_MODEL", REFLEX_MODEL_KEY)
    monkeypatch.setenv(REFLEX_KEY_ENV, "test-key-not-real")
    monkeypatch.setenv(PERSONA_KEY_ENV, "test-key-not-real")
    monkeypatch.setattr(model_configs, "MODEL_CONFIGS", {
        REFLEX_MODEL_KEY: _reflex_model_config(),
        PERSONA_MODEL_KEY: _reflex_model_config(api_key_env=PERSONA_KEY_ENV),
    })


@pytest.fixture
def reflex_role_without_key(monkeypatch):
    """役割と設定は揃っているが、その宛先のキーの env が空のまま。"""
    from saiverse import model_configs

    monkeypatch.setenv("SAIVERSE_REFLEX_JUDGMENT_MODEL", REFLEX_MODEL_KEY)
    monkeypatch.delenv(REFLEX_KEY_ENV, raising=False)
    monkeypatch.setattr(
        model_configs, "MODEL_CONFIGS", {REFLEX_MODEL_KEY: _reflex_model_config()},
    )


def _run(hits, messages, fake_jev, *, enhanced=True, reflex_model_key=None):
    """1 ターン回す。

    ``enhanced`` はペルソナのスイッチ (呼び出し側 sea/runtime_context.py が DB から
    読んで渡す旗)。既定を True にしてあるのは、このファイルの大半が「スイッチは
    入っている」前提で、役割の割り当ての有無 (``reflex_on`` fixture) だけを切り替えて
    確かめるため。スイッチ自体の OFF は専用のテストで確かめる。

    ``reflex_model_key`` はペルソナ個別のモデル上書き (同じく呼び出し側が DB から
    読んで渡す)。None なら世界の既定 (役割の env) に落ちる。
    """
    with patch("sai_memory.unified_recall.unified_recall", return_value=hits), \
         patch("sea.auto_recall._fetch_memopedia_titles", return_value=[]), \
         patch("saiverse.reflex_judgment.evaluate", fake_jev):
        return auto_recall.run_auto_recall(
            conn=object(), embedder=object(), messages=messages,
            persona_id=PERSONA, thread_id=THREAD, enhanced=enhanced,
            reflex_model_key=reflex_model_key,
        )


# ---------------------------------------------------------------------------
# ON/OFF の条件 (スイッチ × 役割の割り当て)
# ---------------------------------------------------------------------------

def test_switch_off_never_calls_the_judgment(reflex_on):
    """役割にモデルが割り当たっていても、ペルソナのスイッチが OFF なら呼ばない。"""
    fake = _FakeJev({"低スコア記憶": 0.99})
    res = _run(
        [_hit("fragment", "f1", embed_score=0.80, title="低スコア記憶")],
        _msgs(("user", "話題")),
        fake,
        enhanced=False,
    )
    assert fake.calls == []
    # 0.80 < 0.86 (既定しきい値) なので従来どおり落ちる。
    assert res.injected is False


def test_switch_off_keeps_accepting_above_threshold(reflex_on):
    fake = _FakeJev({"高スコア記憶": 0.0})
    res = _run(
        [_hit("fragment", "f1", embed_score=0.90, title="高スコア記憶")],
        _msgs(("user", "話題")),
        fake,
        enhanced=False,
    )
    assert fake.calls == []
    assert res.injected is True
    assert "高スコア記憶" in res.block


def test_switch_on_without_a_role_model_stays_off():
    """スイッチを入れただけでは走らない (役割への割り当てという明示の行為が要る)。"""
    assert auto_recall.is_enhanced_recall_available() is False

    fake = _FakeJev({"記憶": 0.99})
    res = _run([_hit("fragment", "f1", embed_score=0.80, title="記憶")], _msgs(("user", "話題")), fake)
    assert fake.calls == []
    assert res.injected is False


def test_switch_on_with_an_ordinary_llm_role_model_works(monkeypatch):
    """通常の LLM を割り当てても選別は走る (判断層が質問をプロンプトへ変換する)。

    第 2 段で合法になった経路。自動想起の側は答える側の種別を知らない。
    """
    from saiverse import model_configs

    monkeypatch.setenv("SAIVERSE_REFLEX_JUDGMENT_MODEL", "some-llm")
    monkeypatch.setattr(model_configs, "MODEL_CONFIGS", {
        "some-llm": {"model": "gemini-x", "protocol": "gemini_native", "provider": "gemini"},
    })

    assert auto_recall.is_enhanced_recall_available() is True

    fake = _FakeJev({"記憶": 0.99})
    res = _run([_hit("fragment", "f1", embed_score=0.80, title="記憶")], _msgs(("user", "話題")), fake)
    assert len(fake.calls) == 1
    assert fake.calls[0]["backend"].model_key == "some-llm"
    # 0.80 は cosine しきい値 0.86 未満だが、判断が「浮かぶ」と答えたので採用される。
    assert res.injected is True


def test_available_when_a_jev_model_is_assigned(reflex_on):
    assert auto_recall.is_enhanced_recall_available() is True


# ---------------------------------------------------------------------------
# ペルソナ個別のモデル上書き (DB の AI.REFLEX_JUDGMENT_MODEL)
# ---------------------------------------------------------------------------

def test_the_persona_override_decides_the_answering_side(reflex_on_with_persona_model):
    """上書きが渡っていれば、実際に答えるのはそちらのモデル設定。"""
    fake = _FakeJev({"記憶": 0.9})
    _run(
        [_hit("fragment", "f1", embed_score=0.90, title="記憶")],
        _msgs(("user", "話題")),
        fake,
        reflex_model_key=PERSONA_MODEL_KEY,
    )
    assert fake.calls[0]["backend"].model_key == PERSONA_MODEL_KEY


def test_without_an_override_the_world_default_answers(reflex_on_with_persona_model):
    fake = _FakeJev({"記憶": 0.9})
    _run(
        [_hit("fragment", "f1", embed_score=0.90, title="記憶")],
        _msgs(("user", "話題")),
        fake,
    )
    assert fake.calls[0]["backend"].model_key == REFLEX_MODEL_KEY


def test_the_override_works_without_a_world_default(monkeypatch):
    """役割の env が未設定でも、ペルソナ個別の指定だけで判定は走る。"""
    from saiverse import model_configs

    monkeypatch.setenv(PERSONA_KEY_ENV, "test-key-not-real")
    monkeypatch.setattr(model_configs, "MODEL_CONFIGS", {
        PERSONA_MODEL_KEY: _reflex_model_config(api_key_env=PERSONA_KEY_ENV),
    })

    assert auto_recall.is_enhanced_recall_available() is False
    assert auto_recall.is_enhanced_recall_available(PERSONA_MODEL_KEY) is True

    fake = _FakeJev({"拾い直された記憶": 0.9})
    res = _run(
        [_hit("fragment", "f1", embed_score=0.80, title="拾い直された記憶")],
        _msgs(("user", "話題")),
        fake,
        reflex_model_key=PERSONA_MODEL_KEY,
    )
    assert len(fake.calls) == 1
    assert fake.calls[0]["backend"].model_key == PERSONA_MODEL_KEY
    assert res.injected is True


def test_a_broken_override_does_not_fall_back_to_the_world_default(reflex_on_with_persona_model):
    """上書きが解決できないときに、黙って世界の既定で答えさせない。

    落ちるのは「このペルソナの指定した宛先」であって、代わりに別のモデルへ課金する
    経路を作らない。そのターンは従来のしきい値判定へ戻る。
    """
    fake = _FakeJev({"記憶": 0.99})
    res = _run(
        [_hit("fragment", "f1", embed_score=0.80, title="記憶")],
        _msgs(("user", "話題")),
        fake,
        reflex_model_key="gone-model",
    )
    assert fake.calls == []
    # 0.80 < 0.86 (既定しきい値) なので従来どおり落ちる。
    assert res.injected is False


# ---------------------------------------------------------------------------
# ON: 採否は Noul 確率で決まる
# ---------------------------------------------------------------------------

def test_low_embed_high_noul_is_accepted(reflex_on):
    # 0.80 は旧しきい値 0.86 未満だが、判断が「浮かぶ」と答えたので採用される。
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


def test_high_embed_low_noul_is_rejected(reflex_on):
    # 0.90 は旧しきい値を超えるが、判断が「無関係」と答えたので落ちる。
    fake = _FakeJev({"ノイズ記憶": 0.1})
    res = _run(
        [_hit("fragment", "f1", embed_score=0.90, title="ノイズ記憶")],
        _msgs(("user", "話題")),
        fake,
    )
    assert len(fake.calls) == 1
    assert res.injected is False
    assert res.accepted_count == 0


def test_acceptance_threshold_is_the_module_constant(reflex_on, monkeypatch):
    """採用に要る Noul 確率は実験で決めた定数 (既定 0.5)。env の口は持たない。"""
    assert auto_recall._REFLEX_THRESHOLD == 0.5
    monkeypatch.setattr(auto_recall, "_REFLEX_THRESHOLD", 0.8)
    fake = _FakeJev({"境界の記憶": 0.6})
    res = _run(
        [_hit("fragment", "f1", embed_score=0.90, title="境界の記憶")],
        _msgs(("user", "話題")),
        fake,
    )
    assert res.injected is False


def test_message_offset_not_applied_in_the_judged_path(reflex_on):
    # message ソースの底上げ (実効 0.88) は判断の経路では使われない。
    fake = _FakeJev({"過去の会話": 0.9})
    res = _run(
        [_hit("message", "m999", embed_score=0.87, title="過去の会話")],
        _msgs(("user", "こんにちは", "m1")),
        fake,
    )
    assert res.injected is True
    assert len(fake.calls) == 1


def test_partial_answer_falls_back_to_cosine_threshold(reflex_on):
    # 一部の候補にしか answer が返らない応答は判断層が ReflexJudgmentUnavailable に
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
# floor (判断に渡す候補の下限)
# ---------------------------------------------------------------------------

def test_below_floor_candidate_is_not_judged(reflex_on):
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


def test_no_candidate_above_floor_skips_the_call(reflex_on):
    fake = _FakeJev({"床の下": 0.9})
    res = _run(
        [_hit("fragment", "f1", embed_score=0.70, title="床の下")],
        _msgs(("user", "話題")),
        fake,
    )
    assert fake.calls == []
    assert res.injected is False


def test_floor_is_the_module_constant(reflex_on, monkeypatch):
    assert auto_recall._REFLEX_FLOOR == 0.78
    monkeypatch.setattr(auto_recall, "_REFLEX_FLOOR", 0.60)
    fake = _FakeJev({"低いが床の上": 0.9})
    res = _run(
        [_hit("fragment", "f1", embed_score=0.65, title="低いが床の上")],
        _msgs(("user", "話題")),
        fake,
    )
    assert len(fake.calls) == 1
    assert res.injected is True


def test_keyword_only_hit_is_never_judged(reflex_on):
    # embed_score なし (キーワードのみ) は判断の経路でも採用しない。
    fake = _FakeJev({"キーワードのみ": 0.99})
    res = _run(
        [_hit("fragment", "f1", embed_score=None, title="キーワードのみ")],
        _msgs(("user", "話題")),
        fake,
    )
    assert fake.calls == []
    assert res.injected is False


def test_message_already_in_context_is_never_judged(reflex_on):
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

def test_unavailable_falls_back_to_cosine_threshold(reflex_on):
    fake = _FakeJev(raises=ReflexJudgmentUnavailable("HTTP 500"))
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


def test_unexpected_exception_also_falls_back(reflex_on):
    fake = _FakeJev(raises=RuntimeError("boom"))
    res = _run(
        [_hit("fragment", "f1", embed_score=0.90, title="しきい値超え")],
        _msgs(("user", "話題")),
        fake,
    )
    assert res.injected is True
    assert "しきい値超え" in res.block


def test_judgment_module_import_failure_falls_back(reflex_on, caplog):
    """反射判断の読み込み自体が失敗しても、会話の同期経路は落ちない。"""
    caplog.set_level(logging.WARNING, logger="saiverse.auto_recall")
    hits = [
        _hit("fragment", "f1", embed_score=0.87, title="しきい値超え"),
        _hit("fragment", "f2", embed_score=0.80, title="しきい値未満"),
    ]
    with patch("sai_memory.unified_recall.unified_recall", return_value=hits), \
         patch("sea.auto_recall._fetch_memopedia_titles", return_value=[]), \
         patch.dict(sys.modules, {"saiverse.reflex_judgment": None}):
        res = auto_recall.run_auto_recall(
            conn=object(), embedder=object(), messages=_msgs(("user", "話題")),
            persona_id=PERSONA, thread_id=THREAD, enhanced=True,
        )

    # import が本当に失敗した経路を通っていること (通らなければ実 API を叩いてしまう)。
    assert [r for r in caplog.records if "could not prepare the request" in r.getMessage()
            or "could not load the reflex judgment layer" in r.getMessage()]

    # 従来の 0.86 判定に戻る。
    assert res.injected is True
    assert "しきい値超え" in res.block
    assert "しきい値未満" not in res.block


# ---------------------------------------------------------------------------
# 「時間内に答えなかった」ターンの印 (画面の注記の材料)
#
# 画面に注記を出すのは「待ち時間を延ばすか速いモデルに替えれば直る」ターンだけ。
# 他の失敗で出すと、直し方の違う問題へ誤った案内をすることになる。
# ---------------------------------------------------------------------------

def _deadline_error():
    """判断層が締切超過のときに投げる例外そのままの形 (種別の印つき)。"""
    from saiverse.reflex_judgment import UNAVAILABLE_DEADLINE

    return ReflexJudgmentUnavailable("deadline exceeded after 5.5s", kind=UNAVAILABLE_DEADLINE)


def test_deadline_sets_the_fallback_flag(reflex_on):
    res = _run(
        [_hit("fragment", "f1", embed_score=0.90, title="しきい値超え")],
        _msgs(("user", "話題")),
        _FakeJev(raises=_deadline_error()),
    )
    assert res.reflex_deadline_fallback is True
    # 注入そのものは従来どおり (旗は表示のためだけで、採否には効かない)。
    assert res.injected is True
    assert "しきい値超え" in res.block


def test_deadline_on_a_turn_without_injection_still_sets_the_flag(reflex_on):
    """記憶が一つも浮かばなかったターンでも、判定が時間切れになった事実は持ち帰る。"""
    res = _run(
        [_hit("fragment", "f1", embed_score=0.80, title="しきい値未満")],
        _msgs(("user", "話題")),
        _FakeJev(raises=_deadline_error()),
    )
    assert res.injected is False
    assert res.reflex_deadline_fallback is True


def test_other_failures_do_not_set_the_fallback_flag(reflex_on):
    res = _run(
        [_hit("fragment", "f1", embed_score=0.90, title="しきい値超え")],
        _msgs(("user", "話題")),
        _FakeJev(raises=ReflexJudgmentUnavailable("HTTP 500")),
    )
    assert res.reflex_deadline_fallback is False


def test_an_unexpected_exception_does_not_set_the_fallback_flag(reflex_on):
    res = _run(
        [_hit("fragment", "f1", embed_score=0.90, title="しきい値超え")],
        _msgs(("user", "話題")),
        _FakeJev(raises=RuntimeError("boom")),
    )
    assert res.reflex_deadline_fallback is False


def test_a_successful_judgment_does_not_set_the_fallback_flag(reflex_on):
    res = _run(
        [_hit("fragment", "f1", embed_score=0.80, title="判定で採用")],
        _msgs(("user", "話題")),
        _FakeJev({"判定で採用": 0.9}),
    )
    assert res.injected is True
    assert res.reflex_deadline_fallback is False


def test_the_switch_off_turn_never_sets_the_fallback_flag(reflex_on):
    """スイッチ OFF のペルソナの挙動は 1 ビットも変わらない (旗も立たない)。"""
    res = _run(
        [_hit("fragment", "f1", embed_score=0.90, title="しきい値超え")],
        _msgs(("user", "話題")),
        _FakeJev(raises=_deadline_error()),
        enhanced=False,
    )
    assert res.reflex_deadline_fallback is False


# ---------------------------------------------------------------------------
# 反射判断を何秒まで待つか (グローバル設定、既定 5 秒)
# ---------------------------------------------------------------------------

def test_the_wait_time_defaults_to_five_seconds():
    assert auto_recall.get_reflex_timeout() == 5.0
    assert auto_recall.REFLEX_TIMEOUT_DEFAULT == 5.0


def test_a_configured_wait_time_is_used(monkeypatch):
    monkeypatch.setenv(auto_recall.REFLEX_TIMEOUT_ENV, "12.5")
    assert auto_recall.get_reflex_timeout() == 12.5


@pytest.mark.parametrize("raw", ["", "   ", "abc", "0", "-3", "nan"])
def test_a_broken_wait_time_falls_back_to_five_seconds(monkeypatch, caplog, raw):
    """設定が壊れていても会話は止めない (既定で動き続ける)。空欄だけは平常なので黙る。"""
    caplog.set_level(logging.WARNING, logger="saiverse.auto_recall")
    monkeypatch.setenv(auto_recall.REFLEX_TIMEOUT_ENV, raw)

    assert auto_recall.get_reflex_timeout() == 5.0

    warned = [r for r in caplog.records if auto_recall.REFLEX_TIMEOUT_ENV in r.getMessage()]
    if raw.strip():
        assert warned, "壊れた値は黙って既定に倒さない"
    else:
        assert not warned, "未設定は平常なので警告しない"


def test_the_judgment_is_called_with_the_configured_wait_time(reflex_on, monkeypatch):
    """設定は毎ターン読む (保存した次のターンから効く — 再起動は要らない)。"""
    monkeypatch.setenv(auto_recall.REFLEX_TIMEOUT_ENV, "9")
    fake = _FakeJev({"判定で採用": 0.9})
    _run(
        [_hit("fragment", "f1", embed_score=0.80, title="判定で採用")],
        _msgs(("user", "話題")),
        fake,
    )
    # 設定 9 秒 − 判断層の内部の余裕 0.5 秒。見切りの時刻 (timeout + 余裕) が
    # 設定値そのものになる (2026-09-21 の敵対レビュー)。
    assert fake.calls[0]["timeout"] == pytest.approx(8.5)


# ---------------------------------------------------------------------------
# 設定ミスの警告
# ---------------------------------------------------------------------------

def test_floor_above_acceptance_threshold_warns(reflex_on, monkeypatch, caplog):
    """floor が採用しきい値より高いと、従来なら浮かぶ記憶が判断に渡らず落ちる。

    警告が出るのは候補検索にヒットがあったターン (= 実際に記憶が落ちうるターン)。
    """
    monkeypatch.setattr(auto_recall, "_REFLEX_FLOOR", 0.95)
    caplog.set_level(logging.WARNING, logger="saiverse.auto_recall")

    fake = _FakeJev({"記憶": 0.9})
    _run([_hit("fragment", "f1", embed_score=0.90, title="記憶")], _msgs(("user", "話題")), fake)

    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "candidate floor" in r.getMessage()
    ]
    assert len(warnings) == 1


def test_no_warning_when_floor_below_threshold(reflex_on, caplog):
    caplog.set_level(logging.WARNING, logger="saiverse.auto_recall")
    fake = _FakeJev({"記憶": 0.9})
    _run([_hit("fragment", "f1", embed_score=0.90, title="記憶")], _msgs(("user", "話題")), fake)
    assert not [r for r in caplog.records if "candidate floor" in r.getMessage()]


def test_floor_warning_is_silent_on_turns_without_hits(reflex_on, monkeypatch, caplog):
    """ヒットが 1 件も無いターンでは設定ミスの警告を出さない。

    警告の意味は「この設定で記憶が落ちている」なので、落ちる記憶が存在しえない
    ターンにも出すと、実際に起きたことと警告がずれて毎ターンのノイズになる。
    """
    monkeypatch.setattr(auto_recall, "_REFLEX_FLOOR", 0.95)
    caplog.set_level(logging.WARNING, logger="saiverse.auto_recall")

    fake = _FakeJev({})
    res = _run([], _msgs(("user", "話題")), fake)

    assert fake.calls == []
    assert res.injected is False
    assert not [r for r in caplog.records if "candidate floor" in r.getMessage()]


# ---------------------------------------------------------------------------
# 粘着台帳との関係 (判断は入場の門であって退場の門ではない)
# ---------------------------------------------------------------------------

def test_rejection_does_not_evict_sticky_item(reflex_on, monkeypatch):
    """一度台帳に入った記憶は、判断が拒否しても sticky_turns の間は注入され続ける。

    急に消えるのではなく数ターンかけて薄れるのが §4.3 の設計意図 (従来方式で
    cosine しきい値を割ったときとまったく同じ扱い)。
    """
    monkeypatch.setenv("SAIVERSE_AUTO_RECALL_STICKY_TURNS", "2")
    hits = [_hit("fragment", "f1", embed_score=0.90, title="粘着する記憶", content="記憶の本文")]

    # ターン1: 判断が採用 → 台帳に入る (stale=0)。
    fake = _FakeJev({"粘着する記憶": 0.9})
    r1 = _run(hits, _msgs(("user", "その話")), fake)
    assert r1.injected is True
    assert "粘着する記憶" in r1.block

    # ターン2以降: 同じ候補を判断が拒否 (noul 低) しても、粘着ウィンドウの間は残る。
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

def test_question_construction(reflex_on):
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

    # 各質問は型を名乗り、自分の qid を参照し、true/false の基準を持つ。
    assert questions["m0"]["type"] == "noul"
    assert "`memories.m0`" in questions["m0"]["instructions"]
    assert "`memories.m1`" in questions["m1"]["instructions"]
    assert set(questions["m0"]["criteria"]) == {"true", "false"}

    # 設定の既定 5.0 秒から、判断層の内部の余裕 (DEADLINE_MARGIN=0.5) を引いた値。
    # 利用者の設定値は「この秒数を過ぎたら見切る」の約束なので、見切りの時刻
    # (timeout + 余裕) が設定値そのものになるように渡す (2026-09-21 の敵対レビュー)。
    assert call["timeout"] == pytest.approx(4.5)
    # どのモデル設定が答えたかを判定ログに載せられるよう、答える側を解決して渡す。
    assert call["backend"].model_key == REFLEX_MODEL_KEY
    assert call["persona_id"] == PERSONA


def test_state_carries_recent_conversation(reflex_on):
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


def test_conversation_is_limited_to_recent_messages(reflex_on, monkeypatch):
    assert auto_recall._REFLEX_CONTEXT_MESSAGES == 6
    monkeypatch.setattr(auto_recall, "_REFLEX_CONTEXT_MESSAGES", 2)
    fake = _FakeJev({"記憶": 0.9})
    _run(
        [_hit("fragment", "f1", embed_score=0.90, title="記憶")],
        _msgs(("user", "古い発話"), ("assistant", "中くらいの応答"), ("user", "新しい発話")),
        fake,
    )
    conversation = fake.calls[0]["state"]["conversation"]
    assert [c["text"] for c in conversation] == ["中くらいの応答", "新しい発話"]


def test_long_conversation_message_is_clipped(reflex_on):
    """会話本文は 1 件あたり 500 字で切る (超過分は省略記号 1 字)。

    上限が無いと 1 ターンのペイロードが発話の長さに引きずられ、費用の見積もりが
    崩れるうえ、長話のターンほど絶対締切に掛かって判定が効かなくなる。
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
    assert texts[1] == "あ" * auto_recall._REFLEX_CONVERSATION_TEXT_LIMIT + "…"


def test_attachment_summaries_are_judged_when_media_recall_is_on(reflex_on, monkeypatch):
    """検索クエリに入る添付の概要は、判断の材料にも入る。

    写真をきっかけに検索が拾ってきた候補を、写真を知らない判断が落としてしまう
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


def test_attachment_only_message_is_still_judged(reflex_on, monkeypatch):
    """本文が空で添付だけのメッセージでも、概要は判断に渡る。"""
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


def test_media_recall_off_keeps_state_shape_unchanged(reflex_on):
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
# 拾い上げの拡張 (効いているときだけ) — キーワード抽出
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
# 拾い上げの拡張 (効いているときだけ) — unified_recall へ渡す引数
# ---------------------------------------------------------------------------

def _capture_recall_call(conn, messages, fake_jev, hits=None, *, enhanced=True):
    """run_auto_recall を 1 ターン回し、unified_recall に渡った kwargs を返す。"""
    captured = {}
    hits = hits if hits is not None else [_hit("fragment", "f1", embed_score=0.90, title="記憶")]

    def _fake_recall(_conn, _embedder, _query, **kwargs):
        captured.update(kwargs)
        return hits

    with patch("sai_memory.unified_recall.unified_recall", _fake_recall), \
         patch("sea.auto_recall._fetch_memopedia_titles", return_value=[]), \
         patch("saiverse.reflex_judgment.evaluate", fake_jev):
        auto_recall.run_auto_recall(
            conn=conn, embedder=object(), messages=messages,
            persona_id=PERSONA, thread_id=THREAD, enhanced=enhanced,
        )
    return captured


def test_off_does_not_pass_the_sweep_arguments(memory_conn):
    """効いていないときは拾い上げの引数を 1 つも渡さない (挙動が 1 ビットも変わらない)。"""
    _fill(memory_conn, "十条の商店街を歩いた")
    captured = _capture_recall_call(
        memory_conn, _msgs(("user", "十条まで歩いた日のこと", "m1")), _FakeJev({"記憶": 0.9}),
        enhanced=False,
    )
    assert set(captured) == {"topk", "search_chronicle", "search_memopedia",
                             "search_fragments", "search_messages"}


def test_on_passes_keywords_exclusions_and_allocations(reflex_on, memory_conn):
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


def test_missing_api_key_does_not_widen_the_sweep(reflex_role_without_key, memory_conn):
    """キーが無い宛先では、候補の集め方も従来のままにする。

    拾い上げだけ広げて判定だけ落ちると、「従来方式へ戻った」というログと実際の挙動
    (候補の母集団が違う) が食い違う。
    """
    assert auto_recall.is_enhanced_recall_available() is False

    _fill(memory_conn, "十条の商店街を歩いた", times=3)
    captured = _capture_recall_call(
        memory_conn, _msgs(("user", "十条まで歩いた日のこと", "m1")), _FakeJev({"記憶": 0.9}),
    )
    assert set(captured) == {"topk", "search_chronicle", "search_memopedia",
                             "search_fragments", "search_messages"}


def test_on_with_no_extractable_keyword_falls_back_to_default_split(reflex_on, memory_conn):
    """語が 1 つも残らないターンは ``keywords=None`` (= 従来の split 挙動)。"""
    captured = _capture_recall_call(
        memory_conn, _msgs(("user", "うん", "m1")), _FakeJev({"記憶": 0.9}),
    )
    assert captured["keywords"] is None
    # 残り 2 つは語が無くても渡る (発話の自席占領の除外と message 枠の拡張)。
    assert captured["exclude_message_ids"] == {"m1"}
    assert captured["source_allocations"]["message"] == 3


# ---------------------------------------------------------------------------
# 判断が使えなかったターンは、候補集めからやり直して従来の形へ戻す
# ---------------------------------------------------------------------------

#: 従来の集め方で返るヒット (cosine 0.86 を超えるので従来経路でも採用される)。
_CONVENTIONAL_HIT = _hit("fragment", "f1", embed_score=0.90, title="しきい値超え")
#: 広げた集め方でだけ増えるヒット。message 枠の拡張と発話の除外で初めて浮上する
#: もので、従来の集め方では母集団に入らない (cosine では通ってしまう 0.95)。
_WIDENED_HIT = _hit("message", "m9", embed_score=0.95, title="広げて拾った過去の会話")

_CONVENTIONAL_KWARGS = {"topk", "search_chronicle", "search_memopedia",
                        "search_fragments", "search_messages"}


def _recall_calls(messages, fake_jev, *, enhanced=True):
    """1 ターン回し、unified_recall の各呼び出しの kwargs と結果を返す。

    広げた引数で呼ばれたときだけ母集団が増える偽の候補集め — 「広げた母集団のまま
    cosine で選別した」のか「集め直してから選別した」のかを、注入結果で見分けられる。
    """
    calls = []

    def _fake_recall(_conn, _embedder, _query, **kwargs):
        calls.append(kwargs)
        if "source_allocations" in kwargs:
            return [_CONVENTIONAL_HIT, _WIDENED_HIT]
        return [_CONVENTIONAL_HIT]

    auto_recall.reset_ledger(PERSONA)
    with patch("sai_memory.unified_recall.unified_recall", _fake_recall), \
         patch("sea.auto_recall._fetch_memopedia_titles", return_value=[]), \
         patch("saiverse.reflex_judgment.evaluate", fake_jev):
        result = auto_recall.run_auto_recall(
            conn=object(), embedder=object(), messages=messages,
            persona_id=PERSONA, thread_id=THREAD, enhanced=enhanced,
        )
    return calls, result


def test_failed_judgment_re_collects_candidates_the_conventional_way(reflex_on, caplog):
    """判定が落ちたターンは、広げた母集団を捨てて集め直す。

    広げたまま cosine で選別すると、従来経路なら母集団にすら入らない記憶が
    注入されてしまう — 「従来方式へ戻った」というログと実際の挙動が食い違う。
    """
    caplog.set_level(logging.INFO, logger="saiverse.auto_recall")

    calls, res = _recall_calls(
        _msgs(("user", "話題", "m1")), _FakeJev(raises=ReflexJudgmentUnavailable("HTTP 500")),
    )

    assert len(calls) == 2
    assert "source_allocations" in calls[0]          # 1 回目は広げた集め方
    assert set(calls[1]) == _CONVENTIONAL_KWARGS      # 2 回目は従来の引数だけ
    assert [r for r in caplog.records if "re-collecting candidates" in r.getMessage()]

    assert res.injected is True
    assert "しきい値超え" in res.block
    assert "広げて拾った過去の会話" not in res.block


def test_the_failed_turn_injects_exactly_what_the_switch_off_turn_would(reflex_on):
    """判定が落ちたターンの注入は、最初からスイッチ OFF だった場合と一致する。"""
    _calls, failed = _recall_calls(
        _msgs(("user", "話題", "m1")), _FakeJev(raises=ReflexJudgmentUnavailable("HTTP 500")),
    )
    off_calls, off = _recall_calls(
        _msgs(("user", "話題", "m1")), _FakeJev({}), enhanced=False,
    )

    assert len(off_calls) == 1
    assert failed.block == off.block
    assert failed.accepted_count == off.accepted_count
    assert failed.hit_count == off.hit_count


def test_a_failed_re_collection_keeps_the_enhanced_candidates(reflex_on, caplog):
    """集め直し自体が失敗した回は、成功済みの広い候補を空で上書きしない。

    障害の空を「候補の無かったターン」として扱うと、粘着台帳が古びて、障害が
    続いただけで粘着記憶が消える。このターンだけは広げた候補のまま cosine 選別
    (集め直し導入前の受容済みの形) に戻る。
    """
    caplog.set_level(logging.WARNING, logger="saiverse.auto_recall")
    calls = []

    def _fake_recall(_conn, _embedder, _query, **kwargs):
        calls.append(kwargs)
        if "source_allocations" in kwargs:
            return [_CONVENTIONAL_HIT, _WIDENED_HIT]
        raise RuntimeError("database is locked")

    auto_recall.reset_ledger(PERSONA)
    with patch("sai_memory.unified_recall.unified_recall", _fake_recall), \
         patch("sea.auto_recall._fetch_memopedia_titles", return_value=[]), \
         patch("saiverse.reflex_judgment.evaluate",
               _FakeJev(raises=ReflexJudgmentUnavailable("HTTP 500"))):
        res = auto_recall.run_auto_recall(
            conn=object(), embedder=object(), messages=_msgs(("user", "話題", "m1")),
            persona_id=PERSONA, thread_id=THREAD, enhanced=True,
        )

    assert len(calls) == 2
    assert res.injected is True
    assert "しきい値超え" in res.block
    assert "広げて拾った過去の会話" in res.block
    assert [r for r in caplog.records if "re-collection failed" in r.getMessage()]


def test_a_successful_judgment_collects_candidates_only_once(reflex_on):
    """判定が成立したターンの経路は変えない (集め直しは起きない)。"""
    calls, res = _recall_calls(
        _msgs(("user", "話題", "m1")),
        _FakeJev({"しきい値超え": 0.9, "広げて拾った過去の会話": 0.9}),
    )

    assert len(calls) == 1
    assert "source_allocations" in calls[0]
    assert res.injected is True
    # 広げて拾った候補も判定を通って注入される (強化が効いているターンの姿)。
    assert "広げて拾った過去の会話" in res.block


def test_the_switch_off_turn_still_collects_candidates_only_once():
    """強化が効いていないターンの経路も変えない。"""
    calls, res = _recall_calls(_msgs(("user", "話題", "m1")), _FakeJev({}), enhanced=False)

    assert len(calls) == 1
    assert set(calls[0]) == _CONVENTIONAL_KWARGS
    assert res.injected is True


def test_observations_are_judged_when_the_user_spoke(reflex_on):
    """発言の後ろに挟まった「いま見えたもの」は、別枠で判断の材料に入る。

    クエリの種はユーザーの発言のままなので、見えたものを知らない判断が、
    見えたものに紐づく候補を落とす非対称が残ってしまう。
    """
    fake = _FakeJev({"記憶": 0.9})
    messages = [
        {"role": "user", "content": "エリスは来てる？"},
        _perception("部屋の様子: エリスが窓際の椅子に座っている"),
    ]
    _run([_hit("fragment", "f1", embed_score=0.90, title="記憶")], messages, fake)

    state = fake.calls[0]["state"]
    assert state["observations"] == ["部屋の様子: エリスが窓際の椅子に座っている"]
    # 会話本文はユーザーの発言だけ (知覚ブロックは conversation に入らない)。
    assert [c["text"] for c in state["conversation"]] == ["エリスは来てる？"]


def test_observations_key_is_absent_without_perception_blocks(reflex_on):
    fake = _FakeJev({"記憶": 0.9})
    _run(
        [_hit("fragment", "f1", embed_score=0.90, title="記憶")],
        _msgs(("user", "話題")),
        fake,
    )
    assert "observations" not in fake.calls[0]["state"]


def test_observations_are_clipped_and_capped(reflex_on):
    """1 件 500 字で切り、最新側から最大 3 件。

    目に入ったものの量で 1 ターンのペイロードが青天井にならないようにする
    (会話本文と同じ有界化)。
    """
    fake = _FakeJev({"記憶": 0.9})
    messages = [{"role": "user", "content": "何が見える？"}]
    messages += [_perception(f"{i}番目の記録") for i in range(4)]
    messages.append(_perception("あ" * 2000))
    _run([_hit("fragment", "f1", embed_score=0.90, title="記憶")], messages, fake)

    observations = fake.calls[0]["state"]["observations"]
    assert len(observations) == 3
    # 最新側の 3 件 (古い「0番目の記録」「1番目の記録」は落ちる)。
    assert observations[0] == "2番目の記録"
    assert observations[1] == "3番目の記録"
    assert observations[2] == "あ" * auto_recall._REFLEX_CONVERSATION_TEXT_LIMIT + "…"


def test_observations_are_not_collected_on_an_autonomous_turn(reflex_on):
    """発言が無いターンは知覚ブロック自体がクエリの種なので、脇道では渡さない。"""
    fake = _FakeJev({"記憶": 0.9})
    messages = [
        {"role": "user", "content": "おはよう"},
        {"role": "assistant", "content": "おはよう、まはー"},
        _perception("部屋の様子: 窓の外で雨が降り始めた"),
    ]
    _run([_hit("fragment", "f1", embed_score=0.90, title="記憶")], messages, fake)
    assert "observations" not in fake.calls[0]["state"]


def test_rare_word_in_a_fresh_observation_becomes_a_keyword(reflex_on, memory_conn):
    """発言の後ろの知覚ブロックの珍しい語も、字面検索の脇道から参加する。"""
    _fill(memory_conn, "十条の商店街を歩いた", times=3)
    messages = [
        {"role": "user", "content": "何が見える？"},
        _perception("部屋の様子: 十条の写真が壁に飾られている"),
    ]
    captured = _capture_recall_call(memory_conn, messages, _FakeJev({"記憶": 0.9}))
    assert captured["keywords"] == ["十条"]


def test_observation_keywords_share_the_cap_with_the_query(reflex_on, memory_conn, monkeypatch):
    """上限 4 個の枠はクエリ本文と共通で、同数ならクエリ本文の語が先に並ぶ。"""
    monkeypatch.setattr(auto_recall, "_KEYWORD_MAX_COUNT", 1)
    _fill(memory_conn, "十条の話", times=3)
    _fill(memory_conn, "赤羽の話", times=3)
    messages = [
        {"role": "user", "content": "十条の話をしていたよね"},
        _perception("部屋の様子: 赤羽の写真が壁に飾られている"),
    ]
    captured = _capture_recall_call(memory_conn, messages, _FakeJev({"記憶": 0.9}))
    assert captured["keywords"] == ["十条"]


def test_off_ignores_observations_entirely(memory_conn):
    """効いていないときは観察テキストの経路に一切入らない。"""
    _fill(memory_conn, "十条の商店街を歩いた", times=3)
    messages = [
        {"role": "user", "content": "何が見える？", "id": "m1"},
        _perception("部屋の様子: 十条の写真が壁に飾られている"),
    ]
    captured = _capture_recall_call(
        memory_conn, messages, _FakeJev({"記憶": 0.9}), enhanced=False,
    )
    assert set(captured) == {"topk", "search_chronicle", "search_memopedia",
                             "search_fragments", "search_messages"}


def test_non_conversational_messages_excluded_from_state(reflex_on):
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
