"""提示列を書き換える経路のキャッシュ申告 (docs/intent/cache_lifecycle_control.md §5.2)。

session_anchor 行の (TTL_SECONDS, UPDATED_AT) は「いま提示している窓に対して、
生きたキャッシュがどれだけ残っているか」の記録で、チャット UI の残り時間表示・
head の再 capture 判定・§14-2 の前進の解禁条件が全部これを読む。ここで固定するのは、
**提示列 (起点・圧縮区間) を書き換える経路が、同じ書き込みでこの 2 値にも必ず
何かを言う**こと。

固定する不変条件:

- 読み戻し (``_write_refill``) は温度と寿命を**一緒に**書く。切れた行 (古い温度 +
  長い寿命) に温度だけ書くと、前の窓のために焼いたキャッシュの寿命が新しい窓の
  ものとして蘇る (2026-09-12 実機: TTL=3600 / UPDATED_AT=3 ヶ月半前の行に読み戻しが
  now だけ書き、実際に送ったのは 5 分キャッシュなのに UI が「残り 58 分」と表示
  し続けた。その後に来た正しい 300 秒の書き込みは延命規則に弾かれて届かなかった)。
- 生きている行への読み戻しも寿命は**現行設定**になる。延命規則 (生存中の長い TTL は
  短い書き込みで縮めない) は「同じ窓を焼き直す書き込み」の規則であって、窓ごと
  作り変える読み戻しには掛からない。
- 畳み (退場)・死んだ区間の掃除・あらすじ削除の道連れの後は、行が「切れている」と
  読める。印戻しは寿命の主張だけ取り下げ、温度は据え置く (理由はクラスの docstring)。
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import Base
from sea.eviction_plan import Watermarks
from sea.session_lifecycle import SessionLifecycle, remove_folds_referencing_entry
from sea.session_window import FoldedRange, SessionWindow

PERSONA_ID = "alice"
MODEL = "model-a"


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    yield Session
    engine.dispose()


def _make_lifecycle(session_factory):
    manager = SimpleNamespace(
        SessionLocal=session_factory,
        event_scheduler=None,
        personas={},
    )
    return SessionLifecycle(SimpleNamespace(), manager)


def _msg(mid, at, chars=1_000):
    return {"id": mid, "content": "x" * chars, "created_at": at}


def _persona(messages=()):
    return SimpleNamespace(
        persona_id=PERSONA_ID, persona_name="エア", model=MODEL, sai_memory=None,
        history_manager=SimpleNamespace(
            get_history_from_anchor=(
                lambda anchor, required_line_roles=None, required_scopes=None,
                pulse_id=None: list(messages)
            ),
        ),
    )


def _window(messages, *, anchor_id=None, folds=None):
    return SessionWindow(
        anchor_id=anchor_id or (messages[0]["id"] if messages else None),
        raw=list(messages),
        presented=list(messages),
        folds=list(folds or []),
    )


def _row(lc, *, anchor_id="m0", age_seconds=0, ttl_seconds=3600):
    """(温度, 寿命) を指定して行を立てる。既定は「たったいま焼いた 1 時間」。"""
    lc.upsert_anchor_entry(PERSONA_ID, MODEL, {
        "anchor_id": anchor_id,
        "updated_at": (
            datetime.now() - timedelta(seconds=age_seconds)
        ).replace(microsecond=0).isoformat(),
        "ttl_seconds": ttl_seconds,
    })


def _entry(lc):
    return lc.load_anchor_entry(PERSONA_ID, MODEL)


def _is_hot(lc):
    return lc._anchor_entry_is_hot(_entry(lc), MODEL, PERSONA_ID)


# ---------------------------------------------------------------------------
# 1. 読み戻しの書き込み — 温度と寿命は一緒に書く
# ---------------------------------------------------------------------------


class TestRefillWritesLifetimeWithTemperature:
    def test_expired_row_does_not_resurrect_its_fossil_lifetime(self, session_factory):
        """2026-09-12 実機事故の再現 — 切れた行への読み戻しは寿命を道連れにする。

        化石 (TTL=3600 / UPDATED_AT=3 ヶ月半前) の行へ読み戻しが温度だけ書くと、
        「1 時間キャッシュがたった今できた」という嘘の状態が生まれる。実際に焼く
        のはこの窓を現行設定で焼いたキャッシュなので、寿命も現行設定になる。
        """
        lc = _make_lifecycle(session_factory)
        _row(lc, age_seconds=int(timedelta(days=105).total_seconds()), ttl_seconds=3600)
        assert not _is_hot(lc)  # 前提: 化石は切れている

        assert lc._write_refill(
            PERSONA_ID, MODEL, "m0", "b0",
            [FoldedRange(message_ids=["b0"], presented_raw=True)],
        ) is True

        entry = _entry(lc)
        assert entry["anchor_id"] == "b0"
        current = lc.get_anchor_validity_seconds(MODEL, PERSONA_ID)
        assert entry["ttl_seconds"] == current
        assert entry["ttl_seconds"] != 3600      # 化石が蘇らない
        # 温度は now (§15-2 — 開いた窓は直後の応答でそのまま焼かれる)。
        assert _is_hot(lc)
        assert lc.anchor_entry_ttl_seconds(entry, MODEL, PERSONA_ID) == current

    def test_live_row_gets_the_current_setting(self, session_factory):
        """生存中の行への読み戻しも寿命は現行設定 — 延命規則には掛からない。

        延命規則 (生存中の長い TTL を短い書き込みで縮めない) は「同じ窓を焼き直す
        書き込み」のためのもの。読み戻しは窓そのものを作り変えるので、前の窓の
        1 時間はこの窓について何も約束していない。
        """
        lc = _make_lifecycle(session_factory)
        _row(lc, ttl_seconds=3600)
        assert _is_hot(lc)

        assert lc._write_refill(
            PERSONA_ID, MODEL, "m0", "b0",
            [FoldedRange(message_ids=["b0"], presented_raw=True)],
        ) is True

        entry = _entry(lc)
        assert entry["ttl_seconds"] == lc.get_anchor_validity_seconds(MODEL, PERSONA_ID)
        assert entry["ttl_seconds"] != 3600

    def test_rejected_write_changes_nothing(self, session_factory):
        """CAS 不一致の読み戻しは申告も書かない (書けなかった窓の主張をしない)。"""
        lc = _make_lifecycle(session_factory)
        _row(lc, age_seconds=int(timedelta(days=105).total_seconds()), ttl_seconds=3600)
        before = _entry(lc)

        assert lc._write_refill(
            PERSONA_ID, MODEL, "wrong-anchor", "b0", [],
        ) is False

        assert _entry(lc) == before


# ---------------------------------------------------------------------------
# 2. キャッシュを壊す操作の申告
# ---------------------------------------------------------------------------


class TestBreakingTheWindowIsDeclared:
    def test_eviction_leaves_the_row_expired(self, session_factory):
        """畳み (退場) の後、行は「切れている」と読める。

        退場は起点を進めて圧縮区間を書く = 提示列が変わるので、直前まで焼けて
        いたキャッシュはもう当たらない。旧実装は起点の前進で UPDATED_AT を now に
        しており、畳んだ直後の行が「たったいま焼いた」と読めていた。
        """
        lc = _make_lifecycle(session_factory)
        lc.is_chronicle_enabled_for_persona = lambda p: True
        lc.generate_chronicle = lambda p, cb=None, **kw: "ok"
        lc.ensure_recall_embeddings = lambda p: None

        def _attach(_persona, folds):
            for i, fold in enumerate(folds):
                fold.chronicle_entry_ids = [f"entry-{i}"]
                fold.chronicle_short_ids = [i + 1]

        lc._attach_chronicle_refs = _attach
        msgs = [_msg(f"m{i}", 100 + i) for i in range(6)]
        _row(lc, anchor_id="m0", ttl_seconds=3600)
        assert _is_hot(lc)

        with patch.dict(os.environ, {"SAIVERSE_SLUICE_ENABLED": "0"}), \
                patch.dict(os.environ, {"SAIVERSE_CHRONICLE_BAND_BUDGET": "2000"}), \
                patch("saiverse.dynamic_state.DynamicStateManager.on_metabolism",
                      lambda *a, **k: None):
            lc.run_metabolism(
                _persona(msgs), "b", _window(msgs),
                Watermarks(target=2_000, high=5_000), None, model_key=MODEL,
            )

        entry = _entry(lc)
        assert entry["anchor_id"] != "m0"        # 前提: 実際に畳んで起点が動いた
        assert "ttl_seconds" not in entry        # 寿命を主張しない
        assert not _is_hot(lc)

    def test_dropping_dead_folds_leaves_the_row_expired(self, session_factory):
        """あらすじを失った区間の掃除も提示を変える (生ログが戻る) → 申告する。"""
        lc = _make_lifecycle(session_factory)
        _row(lc, ttl_seconds=3600)
        persona = _persona()
        dead = FoldedRange(message_ids=["m0"], chronicle_entry_ids=["gone"])
        lc.save_folded_ranges(PERSONA_ID, MODEL, [dead])
        _row(lc, ttl_seconds=3600)  # 温度を立て直して「会話中の窓」から始める
        assert _is_hot(lc)

        window = SessionWindow(
            anchor_id="m0", raw=[_msg("m0", 100)], presented=[_msg("m0", 100)],
            folds=[dead],
        )
        with patch.object(lc, "_resolve_fold_digest_status",
                          return_value=(None, True)), \
                patch.object(lc, "_present_with_folds",
                             side_effect=lambda p, msgs, folds: list(msgs)):
            lc._drop_dead_folds(persona, MODEL, window)

        assert not _is_hot(lc)

    def test_entry_delete_sweep_leaves_the_row_expired(self, session_factory):
        """あらすじエントリ削除の道連れ (記録を外す = 生ログが戻る) も申告する。"""
        lc = _make_lifecycle(session_factory)
        _row(lc, ttl_seconds=3600)
        lc.save_folded_ranges(PERSONA_ID, MODEL, [
            FoldedRange(message_ids=["m0"], chronicle_entry_ids=["e1"]),
        ])
        _row(lc, ttl_seconds=3600)  # 温度を立て直して「会話中の窓」から始める
        assert _is_hot(lc)

        with patch("sea.beat_gate.hold_beat"):
            removed = remove_folds_referencing_entry(lc.manager, PERSONA_ID, "e1")

        assert removed == 1
        assert not _is_hot(lc)

    def test_refold_drops_the_lifetime_claim_and_keeps_a_cold_row_cold(
        self, session_factory,
    ):
        """§15-3 印戻しは寿命の主張だけ取り下げ、温度は据え置く。

        温度を据え置くのは、印戻しが応答の直前 (非常畳み) にしか走らず、畳み直した
        窓がその応答でそのまま焼かれるため — ここで冷やすと直後の組成の resolve が
        §14-2 の前進を掛け、読み戻しが開いた窓を同じ Pulse の中で飲み込む
        (arasuji_levels.md §15-2)。保守経路 (先回り畳み / 手動整理) の行は元から
        冷えているので、据え置いても温かく偽装されない。
        """
        lc = _make_lifecycle(session_factory)
        persona = _persona()
        _row(lc, age_seconds=int(timedelta(days=105).total_seconds()), ttl_seconds=3600)
        before = _entry(lc)["updated_at"]

        raw = [_msg(f"m{i}", 100 + i, 5_000) for i in range(4)]
        folds = [
            FoldedRange(message_ids=["m0"], chronicle_entry_ids=["e1"],
                        presented_raw=True),
            FoldedRange(message_ids=["m1"], chronicle_entry_ids=["e2"],
                        presented_raw=True),
        ]
        window = SessionWindow(
            anchor_id="m0", raw=raw, presented=raw, folds=folds,
        )
        from sea.session_window import apply_folds
        with patch.object(lc, "_present_with_folds",
                          side_effect=lambda p, msgs, fs: apply_folds(
                              msgs, fs, lambda f: "d" * 1_200)):
            assert lc._refold_raw_view_folds(
                persona, MODEL, window, Watermarks(target=17_000, high=19_000),
            ) is not None

        entry = _entry(lc)
        assert "ttl_seconds" not in entry       # 寿命は主張しない
        assert entry["updated_at"] == before    # 温度は据え置き
        assert not _is_hot(lc)                  # 冷えた行は冷えたまま

    def test_refold_keeps_the_temperature_of_a_live_window(self, session_factory):
        """同じ Pulse の読み戻しが立てた「これから焼く」を、印戻しは消さない。"""
        lc = _make_lifecycle(session_factory)
        persona = _persona()
        _row(lc, ttl_seconds=3600)
        before = _entry(lc)["updated_at"]

        raw = [_msg(f"m{i}", 100 + i, 5_000) for i in range(4)]
        folds = [
            FoldedRange(message_ids=["m0"], chronicle_entry_ids=["e1"],
                        presented_raw=True),
        ]
        window = SessionWindow(anchor_id="m0", raw=raw, presented=raw, folds=folds)
        from sea.session_window import apply_folds
        with patch.object(lc, "_present_with_folds",
                          side_effect=lambda p, msgs, fs: apply_folds(
                              msgs, fs, lambda f: "d" * 1_200)):
            lc._refold_raw_view_folds(
                persona, MODEL, window, Watermarks(target=17_000, high=19_000),
            )

        entry = _entry(lc)
        assert entry["updated_at"] == before
        assert _is_hot(lc)


# ---------------------------------------------------------------------------
# 3. 申告で冷やした行と §14-2 (冷えた起点の前進) の噛み合わせ
# ---------------------------------------------------------------------------


def _memory_conn(tmp_path):
    """messages + arasuji_entries を持つ per-persona 記憶 DB (実スキーマ)。"""
    from sai_memory.arasuji import init_arasuji_tables
    from sai_memory.memopedia import init_memopedia_tables
    from sai_memory.memory.storage import init_db

    conn = init_db(str(tmp_path / "memory.db"), check_same_thread=False)
    init_memopedia_tables(conn)
    init_arasuji_tables(conn)
    return conn


def _add_message(conn, msg_id, created_at):
    conn.execute(
        "INSERT INTO messages (id, thread_id, role, content, created_at, "
        "metadata, line_role) VALUES (?, 't-main', 'user', 'hello', ?, "
        "NULL, 'main_line')",
        (msg_id, created_at),
    )
    conn.commit()


def _add_l1_entry(conn, source_ids):
    from sai_memory.arasuji.storage import create_entry
    return create_entry(
        conn, level=1, content="digest", source_ids=list(source_ids),
        source_count=len(source_ids), message_count=len(source_ids),
    )


def _persona_with_memory(conn, last_seen):
    import threading

    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model=MODEL,
        sai_memory=SimpleNamespace(
            conn=conn, is_ready=lambda: True, _db_lock=threading.RLock(),
        ),
    )
    from sea.sluice import _save_pan_marker
    _save_pan_marker(persona, last_seen)  # スルースは全部見た状態にしておく
    return persona


class TestColdDeclarationVersusFrontierAdvance:
    """「壊れた」申告で冷やすと §14-2 の前進が解禁される — その行き先を確かめる。

    温度は残り寿命の記録であると同時に「冷え切った窓は整えてよい」の解禁条件
    でもある (arasuji_levels.md §14-1)。畳みの申告で冷やす以上、直後の resolve が
    何をするかは挙動の一部なので、推測ではなく固定しておく。
    """

    def test_advance_is_a_no_op_right_after_an_eviction(
        self, session_factory, tmp_path,
    ):
        """畳んだ直後の窓では前進は空振りする — 新しい起点 = 最前線だから。

        退場は「編纂済みの先頭連続域を起点が飲み込む」操作なので、畳み終わった
        起点はそのまま「最初の未編纂メッセージ」= 最前線になる。冷やしても前進の
        条件 (最前線が自行より先) が成立せず、窓は畳んだ形のまま残る。
        """
        lc = _make_lifecycle(session_factory)
        conn = _memory_conn(tmp_path)
        for i in range(1, 7):
            _add_message(conn, f"m{i}", 1000 + i)
        _add_l1_entry(conn, ["m1", "m2", "m3"])  # 退場した範囲は編纂済み
        persona = _persona_with_memory(conn, "m6")
        # 畳みの申告が残す形: 起点 = m4、寿命の主張なし、温度は十分に過去
        _row(lc, anchor_id="m4", ttl_seconds=3600)
        lc.save_folded_ranges(PERSONA_ID, MODEL, [])
        assert not _is_hot(lc)

        assert lc.resolve_metabolism_anchor(persona) == ("m4", "self")
        assert _entry(lc)["anchor_id"] == "m4"
        conn.close()

    def test_advance_is_a_no_op_while_the_window_starts_on_raw_log(
        self, session_factory, tmp_path,
    ):
        """窓の途中だけ畳んだ形 (穴が残る形) でも前進は空振りする。

        最前線は「正典順で**最初の**未編纂メッセージ」なので、窓の頭が生ログの
        うちは必ず起点以前にある = 前進の条件 (最前線が自行より先) が成立しない。
        畳みの申告で冷やしても、穴の残った窓がその場で作り替えられることはない。
        """
        from sea.session_window import deserialize_folds

        lc = _make_lifecycle(session_factory)
        conn = _memory_conn(tmp_path)
        for i in range(1, 7):
            _add_message(conn, f"m{i}", 1000 + i)
        _add_l1_entry(conn, ["m2", "m3"])  # 窓の途中だけ編纂済み (m1 は生)
        persona = _persona_with_memory(conn, "m6")
        _row(lc, anchor_id="m1", ttl_seconds=3600)
        lc.save_folded_ranges(PERSONA_ID, MODEL, [
            FoldedRange(message_ids=["m2", "m3"], chronicle_entry_ids=["e1"]),
        ])
        assert not _is_hot(lc)

        assert lc.resolve_metabolism_anchor(persona) == ("m1", "self")
        entry = _entry(lc)
        assert entry["anchor_id"] == "m1"
        assert len(deserialize_folds(entry.get("folded_ranges"))) == 1  # 穴も無傷
        conn.close()

    def test_a_cooled_refilled_window_would_be_swallowed_so_refold_keeps_it_warm(
        self, session_factory, tmp_path,
    ):
        """前進が実際に発火する唯一の形 = 起点が編纂済み領域の中にある窓。

        それは読み戻しが過去を開いた直後の窓そのもの。もし印戻し (同じ Pulse の
        非常畳み) がそこで温度を冷やすと、直後の組成の resolve が最前線まで前進し、
        開いた窓が同じ Pulse の中で閉じる (arasuji_levels.md §15-2 の禁止形)。
        前半でその危険が実在することを見せ、後半で実装 (印戻しは温度を据え置く) が
        それを起こさないことを固定する。
        """
        from sea.session_window import apply_folds, deserialize_folds

        lc = _make_lifecycle(session_factory)
        conn = _memory_conn(tmp_path)
        for i in range(1, 7):
            _add_message(conn, f"m{i}", 1000 + i)
        _add_l1_entry(conn, ["m1", "m2", "m3", "m4", "m5"])  # 最前線 = m6
        persona = _persona_with_memory(conn, "m6")

        # (1) 読み戻しが m1 まで開いた窓を、冷えた状態で resolve に掛けると…
        _row(lc, anchor_id="m1", ttl_seconds=3600)
        lc.save_folded_ranges(PERSONA_ID, MODEL, [
            FoldedRange(message_ids=["m1", "m2", "m3", "m4", "m5"],
                        chronicle_entry_ids=["e1"], presented_raw=True),
        ])
        assert not _is_hot(lc)
        assert lc.resolve_metabolism_anchor(persona) == ("m6", "frontier")
        assert deserialize_folds(
            _entry(lc).get("folded_ranges"),
        ) == []  # 開いた範囲ごと飲み込まれる = 窓が m6 の 1 件に縮む

        # (2) 実装どおりの順序 (読み戻しが「これから焼く」を立てる → 印戻し) では
        #     温度が残り、同じ Pulse の resolve は窓をそのまま返す。
        _row(lc, anchor_id="m1", ttl_seconds=3600)
        fold = FoldedRange(message_ids=["m1", "m2", "m3", "m4", "m5"],
                           chronicle_entry_ids=["e1"], presented_raw=True)
        lc.save_folded_ranges(PERSONA_ID, MODEL, [fold])
        _row(lc, anchor_id="m1", ttl_seconds=3600)  # = _write_refill の「これから焼く」
        raw = [_msg(f"m{i}", 1000 + i, 5_000) for i in range(1, 7)]
        window = SessionWindow(anchor_id="m1", raw=raw, presented=raw, folds=[fold])
        with patch.object(lc, "_present_with_folds",
                          side_effect=lambda p, msgs, fs: apply_folds(
                              msgs, fs, lambda f: "d" * 1_200)):
            assert lc._refold_raw_view_folds(
                persona, MODEL, window, Watermarks(target=17_000, high=19_000),
            ) is not None

        assert _is_hot(lc)
        assert lc.resolve_metabolism_anchor(persona) == ("m1", "self")
        assert len(deserialize_folds(_entry(lc).get("folded_ranges"))) == 1
        conn.close()


# ---------------------------------------------------------------------------
# 4. 被覆補修の印 — 提示を変えないので申告しない (触る行はもともと切れている)
# ---------------------------------------------------------------------------


class TestCoverageMarksDoNotClaimAnything:
    def test_marks_keep_the_row_expired_and_never_touch_a_live_one(
        self, session_factory,
    ):
        """印は ``presented_raw`` (生ログのまま見せる) なので提示列が変わらない。

        キャッシュは壊れないので申告も要らない。加えて書き込みの呼び出し元
        (mark_covered_cold_windows) は温かい行を除くため、印が載る行はもともと
        切れている — 補修の後も「切れている」と読める。
        """
        lc = _make_lifecycle(session_factory)
        _row(lc, age_seconds=int(timedelta(days=3).total_seconds()), ttl_seconds=300)
        assert not _is_hot(lc)
        before = _entry(lc)["updated_at"]

        assert lc.write_folds_if_anchor_unchanged(
            PERSONA_ID, MODEL, "m0",
            [FoldedRange(message_ids=["m0"], chronicle_entry_ids=["e1"],
                         presented_raw=True)],
        ) is True

        entry = _entry(lc)
        assert entry["updated_at"] == before    # 冷えた行を温かく偽装しない
        assert not _is_hot(lc)
