"""Chronicle 診断レポート (GET /{persona_id}/arasuji/diagnosis) の契約。

2026-09-01 に「文字数」と「帯の実寸」を足した。実ユーザーの帯が予算 20,000 字を
超えて膨らむ疑いがあり、その容疑 — (1) 一件あたりの本文が長い / (2) 重複
source_ids が走査の重なり管理を壊して件数が膨らむ — を切り分ける材料が、それまでの
レポートに無かったため。

ここで固定するのは「材料が出ること」と「数が互いに整合すること」だけ。診断は
読むだけの機構なので、値の良し悪しは判断しない。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from api.routes.people.arasuji import get_chronicle_diagnosis

PERSONA_ID = "tester"
BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


class DummyEmbedder:
    def __init__(self, model=None, **kwargs) -> None:
        self.model_name = model

    def embed(self, texts, **kwargs):
        return [[0.0] * 3 for _ in texts]


@pytest.fixture
def persona_home(tmp_path, monkeypatch):
    """診断が読む ``<home>/personas/<id>/memory.db`` を合成データで用意する。

    診断は ``get_persona_memory_db`` でパスを引くので、SAIVERSE_HOME を隔離すれば
    本番のルート関数をそのまま呼べる。
    """
    monkeypatch.setenv("SAIMEMORY_MEMORY", "1")
    monkeypatch.setenv("SAIVERSE_HOME", str(tmp_path))
    with patch("saiverse_memory.adapter.Embedder", DummyEmbedder):
        from saiverse_memory import SAIMemoryAdapter
        persona_path = tmp_path / "personas" / PERSONA_ID
        persona_path.mkdir(parents=True)
        adapter = SAIMemoryAdapter(
            PERSONA_ID, persona_dir=persona_path, resource_id=PERSONA_ID,
        )
        try:
            yield adapter
        finally:
            try:
                adapter.close()
            except Exception:
                pass


def _add_messages(adapter, count, *, chars=200):
    ids = []
    for i in range(count):
        mid = adapter.append_persona_message({
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"会話 {i} " + "あ" * chars,
            "timestamp": (BASE_TIME + timedelta(minutes=i)).isoformat(),
        })
        assert mid is not None
        ids.append(mid)
    return ids


def _create_entry(conn, source_ids, content):
    from sai_memory.arasuji.storage import create_entry, init_arasuji_tables
    init_arasuji_tables(conn)
    rows = conn.execute(
        "SELECT COALESCE(created_at, 0) FROM messages WHERE id IN ({})".format(
            ",".join("?" for _ in source_ids)
        ),
        [str(s) for s in source_ids],
    ).fetchall()
    times = sorted(int(r[0]) for r in rows) or [0]
    return create_entry(
        conn, level=1, content=content,
        source_ids=[str(s) for s in source_ids],
        start_time=times[0], end_time=times[-1],
        source_count=len(source_ids), message_count=len(source_ids),
    )


def _manager():
    """診断が触るのは帯予算の解決 (列の読み出し) だけ。DB を持たない形で渡す。

    ``SessionLocal`` が無いので列は読めず、予算は env → 既定へ落ちる。診断が
    その経路でも落ちないことを併せて見る。
    """
    return SimpleNamespace(personas={})


@pytest.fixture
def diagnosis(persona_home, monkeypatch):
    monkeypatch.delenv("SAIVERSE_CHRONICLE_CHAR_BUDGET", raising=False)
    ids = _add_messages(persona_home, 6)
    _create_entry(persona_home.conn, ids[0:3], "むかしの話。" * 10)
    _create_entry(persona_home.conn, ids[3:6], "そのあとの話。" * 20)
    persona_home.conn.commit()
    return get_chronicle_diagnosis(PERSONA_ID, manager=_manager())


def test_reports_content_chars_per_level(diagnosis):
    """レベル別の本文文字数が、件数と合計・平均・最大最小で整合する。"""
    stats = diagnosis["content_chars_by_level"]
    assert 1 in stats
    lv1 = stats[1]
    assert lv1["entries"] == 2
    assert lv1["total_chars"] == len("むかしの話。" * 10) + len("そのあとの話。" * 20)
    assert lv1["max_chars"] == len("そのあとの話。" * 20)
    assert lv1["min_chars"] == len("むかしの話。" * 10)
    assert lv1["min_chars"] <= lv1["avg_chars"] <= lv1["max_chars"]


def test_band_simulation_reports_the_visible_entries(diagnosis):
    """帯シミュレーションが、いま載るエントリと文字数を返す。"""
    band = diagnosis["band_simulation"]
    assert band["error"] is None
    # 予算は列を読めない構成なので既定へ落ちる
    assert band["budget"] == 20_000
    assert band["budget_source"] == "builtin_default"
    assert band["total_entries"] == len(band["visible_entries"])
    assert band["total_entries"] > 0
    # 本文合計 = 可視エントリの文字数の総和
    assert band["content_chars"] == sum(
        e["content_chars"] for e in band["visible_entries"]
    )
    # レベル別の内訳も同じ総和に閉じる
    assert sum(v["entries"] for v in band["by_level"].values()) == band["total_entries"]
    assert sum(v["content_chars"] for v in band["by_level"].values()) == (
        band["content_chars"]
    )
    # 整形は飾りが乗るぶん本文より長い
    assert band["formatted_chars"] >= band["content_chars"]
    assert band["over_budget"] is False


def test_band_simulation_states_that_it_excludes_nothing(diagnosis):
    """除外を渡していないことを応答自身が言う。

    本番の weave は提示ウィンドウ内で digest 表示中のエントリを帯から外すが、
    診断はサーバーの実行時状態に依存させないため除外なしで流す。読み手が
    「実際の帯はここから減る」と分かる印が要る。
    """
    assert diagnosis["band_simulation"]["excludes_presented_digests"] is False


def test_band_simulation_uses_the_env_budget_when_set(persona_home, monkeypatch):
    """env の予算がシミュレーションに効き、出どころも申告される。"""
    monkeypatch.setenv("SAIVERSE_CHRONICLE_CHAR_BUDGET", "30000")
    _add_messages(persona_home, 2)
    result = get_chronicle_diagnosis(PERSONA_ID, manager=_manager())
    band = result["band_simulation"]
    assert band["budget"] == 30_000
    assert band["budget_source"] == "env"


@pytest.mark.parametrize(
    "env_value,expected_budget,expected_source,expected_mode",
    [
        # 書式が違っても実効値が同じなら出どころは env — 文字列比較で判定して
        # いた頃はここが builtin_default に化けていた (Codex 指摘 2026-09-01)。
        ("30000", 30_000, "env", "char_budget"),
        ("030000", 30_000, "env", "char_budget"),
        ("+30000", 30_000, "env", "char_budget"),
        ("  30000  ", 30_000, "env", "char_budget"),
        # 0 / 負値は「文字数予算を切る」= 件数モード。予算内/超過の話ではない。
        ("0", 0, "env_budget_disabled", "count_based"),
        ("-5", -5, "env_budget_disabled", "count_based"),
        # int() が通らない値は解決側も既定へ落ちる
        ("たくさん", 20_000, "builtin_default", "char_budget"),
        ("", 20_000, "builtin_default", "char_budget"),
    ],
)
def test_band_budget_source_matches_the_actual_resolution(
    persona_home, monkeypatch, env_value, expected_budget, expected_source,
    expected_mode,
):
    """出どころのラベルは、実際の解決と同じ規則で決まる。"""
    monkeypatch.setenv("SAIVERSE_CHRONICLE_CHAR_BUDGET", env_value)
    _add_messages(persona_home, 2)
    band = get_chronicle_diagnosis(PERSONA_ID, manager=_manager())["band_simulation"]
    assert band["error"] is None
    assert band["budget"] == expected_budget
    assert band["budget_source"] == expected_source
    assert band["budget_mode"] == expected_mode
    # 件数モードでは予算超過という概念が無い
    if expected_mode == "count_based":
        assert band["over_budget"] is False


def test_count_based_mode_still_reports_the_band(persona_home, monkeypatch):
    """予算制を切った構成でも帯は測れる (本番も同じ件数モードで組む)。"""
    monkeypatch.setenv("SAIVERSE_CHRONICLE_CHAR_BUDGET", "0")
    ids = _add_messages(persona_home, 4)
    _create_entry(persona_home.conn, ids[0:2], "件数モードの話。" * 5)
    persona_home.conn.commit()

    band = get_chronicle_diagnosis(PERSONA_ID, manager=_manager())["band_simulation"]
    assert band["budget_mode"] == "count_based"
    assert band["total_entries"] > 0
    assert band["content_chars"] == sum(
        e["content_chars"] for e in band["visible_entries"]
    )


def test_band_simulation_failure_does_not_break_the_report(persona_home):
    """帯の測定が落ちても診断全体は 200 で返る (stelis_stats_error と同じ流儀)。"""
    _add_messages(persona_home, 2)
    with patch(
        "sai_memory.arasuji.context.get_episode_context",
        side_effect=RuntimeError("boom"),
    ):
        result = get_chronicle_diagnosis(PERSONA_ID, manager=_manager())
    assert result["band_simulation"]["error"] == "boom"
    # 他の節は通常どおり揃っている
    assert result["persona_id"] == PERSONA_ID
    assert "content_chars_by_level" in result
