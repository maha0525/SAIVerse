"""提示コンテキストを変更する経路のキャッシュ状態更新 (docs/intent/cache_lifecycle_control.md §5.2)。

SessionAnchor 行の (TTL_SECONDS, UPDATED_AT) は「現在の提示コンテキストに対して、
有効なプロンプトキャッシュがいつまで残っているか」の記録で、チャット UI の
残り時間表示・head の再取得判定・§14-2 のアンカー自動前進の条件がすべてこれを
参照する。ここで固定するのは、**提示内容 (アンカー位置・圧縮区間) を書き換える
経路が、同じ書き込みでこの 2 値も必ず更新する**こと。

固定する不変条件:

- 返答直前の履歴復元 (``_write_refill``) は UPDATED_AT と TTL_SECONDS を**一緒に**
  更新する。期限切れ行 (古い UPDATED_AT + 長い TTL) に UPDATED_AT=now だけ書くと、
  変更前の窓の TTL が新しい窓のものとして復活してしまう (2026-09-12 実機:
  TTL=3600 / UPDATED_AT=3 ヶ月半前の行に履歴復元が now だけを上書きし、実際は
  5 分キャッシュなのに UI が「残り 58 分」と表示し続けた。直後に来た正しい 300 秒の
  書き込みは TTL 延長規則に弾かれて反映されなかった)。
- キャッシュ有効期限内の行への履歴復元でも、TTL_SECONDS は**現行設定**に更新される。
  「有効な長い TTL を短い書き込みで縮めない」規則は同じ窓を再利用するときの
  規則であって、窓ごと作り変える履歴復元には適用されない。
- 直後に LLM 応答が走らないアンカー巻き戻し (``_write_refill(..., invalidate_cache=True)`` /
  ``run_tail_rewind``) はキャッシュ無効化を書き込み、LLM 呼び出し無しでキャッシュ有効
  と誤表示しない。
- 古い会話の圧縮・欠損圧縮区間の掃除・Chronicle 削除時の連動削除の後は、行が
  キャッシュ期限切れになる。要約表示への再圧縮 (``_refold_raw_view_folds``) は
  TTL_SECONDS のみクリアし、UPDATED_AT は維持する (理由は各テストの docstring)。
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
    """(UPDATED_AT の経過秒, TTL_SECONDS) を指定して行を作成する。既定は「今作成した 1 時間キャッシュ」。"""
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
# 1. 履歴復元・巻き戻しの書き込み — UPDATED_AT と TTL_SECONDS を一緒に更新する
# ---------------------------------------------------------------------------


class TestRefillWritesLifetimeWithTemperature:
    def test_expired_row_does_not_resurrect_its_fossil_lifetime(self, session_factory):
        """2026-09-12 実機不具合の再現 — 期限切れ行への履歴復元で古い TTL を復活させない。

        古い TTL 設定の残り (TTL=3600 / UPDATED_AT=3 ヶ月半前) に UPDATED_AT=now だけ
        書くと、「1 時間キャッシュが今作られた」という誤った状態になる。直後に作成される
        のは現行設定のキャッシュなので、TTL_SECONDS も現行設定で上書きする。
        """
        lc = _make_lifecycle(session_factory)
        _row(lc, age_seconds=int(timedelta(days=105).total_seconds()), ttl_seconds=3600)
        assert not _is_hot(lc)  # 前提: 既存キャッシュは期限切れ

        assert lc._write_refill(
            PERSONA_ID, MODEL, "m0", "b0",
            [FoldedRange(message_ids=["b0"], presented_raw=True)],
        ) is True

        entry = _entry(lc)
        assert entry["anchor_id"] == "b0"
        current = lc.get_anchor_validity_seconds(MODEL, PERSONA_ID)
        assert entry["ttl_seconds"] == current
        assert entry["ttl_seconds"] != 3600      # 古い 1 時間設定が復活しない
        # UPDATED_AT は now (§15-2 — 復元した窓は直後の返答でそのままキャッシュされる)。
        assert _is_hot(lc)
        assert lc.anchor_entry_ttl_seconds(entry, MODEL, PERSONA_ID) == current

    def test_live_row_gets_the_current_setting(self, session_factory):
        """キャッシュ有効期限内の行への履歴復元でも TTL_SECONDS は現行設定にする。

        「有効な長い TTL を短い書き込みで縮めない」延長規則は、同じ窓を再利用するときの
        規則。履歴復元は窓の内容を作り変えるため、変更前の 1 時間 TTL は引き継がない。
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
        """CAS 不一致でスキップされた履歴復元はキャッシュ状態も書き換えない。"""
        lc = _make_lifecycle(session_factory)
        _row(lc, age_seconds=int(timedelta(days=105).total_seconds()), ttl_seconds=3600)
        before = _entry(lc)

        assert lc._write_refill(
            PERSONA_ID, MODEL, "wrong-anchor", "b0", [],
        ) is False

        assert _entry(lc) == before

    def test_invalidate_cache_flag_marks_row_expired(self, session_factory):
        """直後に LLM 応答が走らないアンカー巻き戻し (invalidate_cache=True) は失効扱いにする。

        期限切れ行を誤って有効扱いにせず、有効期限内の行もプロンプト変更に合わせて
        失効扱いへ更新する。
        """
        lc = _make_lifecycle(session_factory)
        # (1) 期限切れ行 → そのまま期限切れ (TTL_SECONDS クリア)
        _row(lc, age_seconds=int(timedelta(days=3).total_seconds()), ttl_seconds=3600)
        assert not _is_hot(lc)
        assert lc._write_refill(
            PERSONA_ID, MODEL, "m0", "b0", [], invalidate_cache=True,
        ) is True
        entry = _entry(lc)
        assert entry["anchor_id"] == "b0"
        assert "ttl_seconds" not in entry
        assert not _is_hot(lc)

        # (2) 有効期限内の行 → プロンプト内容が変わるため失効扱いへ更新
        _row(lc, anchor_id="b0", ttl_seconds=3600)
        assert _is_hot(lc)
        assert lc._write_refill(
            PERSONA_ID, MODEL, "b0", "b1", [], invalidate_cache=True,
        ) is True
        entry = _entry(lc)
        assert entry["anchor_id"] == "b1"
        assert "ttl_seconds" not in entry
        assert not _is_hot(lc)


# ---------------------------------------------------------------------------
# 2. プロンプト内容を変更する操作によるキャッシュ無効化
# ---------------------------------------------------------------------------


class TestBreakingTheWindowIsDeclared:
    def test_eviction_leaves_the_row_expired(self, session_factory):
        """古い会話の圧縮 (eviction) の後、行はキャッシュ期限切れになる。

        圧縮ではアンカーを進めて圧縮区間を保存する (= 提示内容が変わる) ため、
        直前までのキャッシュはもうヒットしない。
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
        assert entry["anchor_id"] != "m0"        # 前提: 実際に圧縮してアンカーが動いた
        assert "ttl_seconds" not in entry        # TTL_SECONDS がクリアされている
        assert not _is_hot(lc)

    def test_dropping_dead_folds_leaves_the_row_expired(self, session_factory):
        """参照先 Chronicle を失った圧縮区間の掃除も提示内容を変える (生ログに戻る) ため失効扱いにする。"""
        lc = _make_lifecycle(session_factory)
        _row(lc, ttl_seconds=3600)
        persona = _persona()
        dead = FoldedRange(message_ids=["m0"], chronicle_entry_ids=["gone"])
        lc.save_folded_ranges(PERSONA_ID, MODEL, [dead])
        _row(lc, ttl_seconds=3600)  # 有効なキャッシュがある状態から開始
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
        """Chronicle エントリ削除時の連動削除 (圧縮区間を外して生ログに戻す) も失効扱いにする。"""
        lc = _make_lifecycle(session_factory)
        _row(lc, ttl_seconds=3600)
        lc.save_folded_ranges(PERSONA_ID, MODEL, [
            FoldedRange(message_ids=["m0"], chronicle_entry_ids=["e1"]),
        ])
        _row(lc, ttl_seconds=3600)  # 有効なキャッシュがある状態から開始
        assert _is_hot(lc)

        with patch("sea.beat_gate.hold_beat"):
            removed = remove_folds_referencing_entry(lc.manager, PERSONA_ID, "e1")

        assert removed == 1
        assert not _is_hot(lc)

    def test_refold_drops_the_lifetime_claim_and_keeps_a_cold_row_cold(
        self, session_factory,
    ):
        """§15-3 の要約表示への再圧縮 (_refold_raw_view_folds) は TTL_SECONDS のみクリアし、UPDATED_AT は維持する。

        UPDATED_AT を維持するのは、再圧縮が返答直前の緊急圧縮で走り、圧縮し直した窓が
        そのまま返答でキャッシュされるため。ここで過去時刻に戻すと直後の resolve で
        §14-2 のアンカー自動前進が走り、直前の履歴復元で開いた窓を同じ Pulse 内で閉じてしまう
        (arasuji_levels.md §15-2)。期限切れ行は UPDATED_AT を据え置いても期限切れのままなので、
        バックグラウンド保守経路のキャッシュ状態を誤って有効化しない。
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
        assert "ttl_seconds" not in entry       # TTL_SECONDS はクリアされる
        assert entry["updated_at"] == before    # UPDATED_AT は維持
        assert not _is_hot(lc)                  # 期限切れ行は期限切れのまま

    def test_refold_keeps_the_temperature_of_a_live_window(self, session_factory):
        """同じ Pulse の履歴復元が設定した有効状態 (UPDATED_AT) を、再圧縮が消さない。"""
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
# 3. キャッシュ無効化と §14-2 (期限切れ時のアンカー自動前進) の噛み合わせ
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
    _save_pan_marker(persona, last_seen)  # Sluice 処理済み位置マーカーを最新まで進める
    return persona


class TestColdDeclarationVersusFrontierAdvance:
    """キャッシュ無効化で期限切れ扱いになると §14-2 のアンカー自動前進が許可される — その挙動を固定する。

    UPDATED_AT はキャッシュ残り時間の記録であると同時に「キャッシュ期限切れなら
    アンカーを自動前進させてよい」という判定条件でもある (arasuji_levels.md §14-1)。
    圧縮後の無効化で期限切れ扱いにした直後の resolve が意図どおり動くことを検証する。
    """

    def test_advance_is_a_no_op_right_after_an_eviction(
        self, session_factory, tmp_path,
    ):
        """古い会話の圧縮直後は自動前進が no-op になる — 新アンカー = 未要約の先頭位置 (frontier) のため。"""
        lc = _make_lifecycle(session_factory)
        conn = _memory_conn(tmp_path)
        for i in range(1, 7):
            _add_message(conn, f"m{i}", 1000 + i)
        _add_l1_entry(conn, ["m1", "m2", "m3"])  # 圧縮済み範囲は Chronicle 生成済み
        persona = _persona_with_memory(conn, "m6")
        # 圧縮後の状態: アンカー = m4、TTL_SECONDS なし、UPDATED_AT は過去時刻
        _row(lc, anchor_id="m4", ttl_seconds=3600)
        lc.save_folded_ranges(PERSONA_ID, MODEL, [])
        assert not _is_hot(lc)

        assert lc.resolve_metabolism_anchor(persona) == ("m4", "self")
        assert _entry(lc)["anchor_id"] == "m4"
        conn.close()

    def test_advance_is_a_no_op_while_the_window_starts_on_raw_log(
        self, session_factory, tmp_path,
    ):
        """窓の途中だけ圧縮した状態 (途中圧縮区間あり) でも自動前進は no-op になる。

        未要約の先頭位置 (frontier) は時系列で最初の未要約メッセージなので、窓の先頭が
        生ログである限り常に現在アンカー以前にあり、自動前進の条件を満たさない。
        """
        from sea.session_window import deserialize_folds

        lc = _make_lifecycle(session_factory)
        conn = _memory_conn(tmp_path)
        for i in range(1, 7):
            _add_message(conn, f"m{i}", 1000 + i)
        _add_l1_entry(conn, ["m2", "m3"])  # 窓の途中だけ要約済み (m1 は生ログ)
        persona = _persona_with_memory(conn, "m6")
        _row(lc, anchor_id="m1", ttl_seconds=3600)
        lc.save_folded_ranges(PERSONA_ID, MODEL, [
            FoldedRange(message_ids=["m2", "m3"], chronicle_entry_ids=["e1"]),
        ])
        assert not _is_hot(lc)

        assert lc.resolve_metabolism_anchor(persona) == ("m1", "self")
        entry = _entry(lc)
        assert entry["anchor_id"] == "m1"
        assert len(deserialize_folds(entry.get("folded_ranges"))) == 1  # 途中圧縮区間も無傷
        conn.close()

    def test_a_cooled_refilled_window_would_be_swallowed_so_refold_keeps_it_warm(
        self, session_factory, tmp_path,
    ):
        """自動前進が発火する唯一のケース = アンカーが要約済み領域の中にある窓。

        履歴復元で過去を開いた直後がこれに当たる。もし同じ Pulse 内の再圧縮
        (_refold_raw_view_folds) で UPDATED_AT を過去時刻に戻すと、直後の resolve が
        未要約の先頭位置まで自動前進してしまい、復元した窓が同じ Pulse 内で閉じる
        (arasuji_levels.md §15-2)。前半でその危険を確認し、後半で実装
        (keep_updated_at=True) がそれを防ぐことを固定する。
        """
        from sea.session_window import apply_folds, deserialize_folds

        lc = _make_lifecycle(session_factory)
        conn = _memory_conn(tmp_path)
        for i in range(1, 7):
            _add_message(conn, f"m{i}", 1000 + i)
        _add_l1_entry(conn, ["m1", "m2", "m3", "m4", "m5"])  # 未要約の先頭位置 = m6
        persona = _persona_with_memory(conn, "m6")

        # (1) 履歴復元で m1 まで開いた窓を期限切れ状態にして resolve に掛けると自動前進する
        _row(lc, anchor_id="m1", ttl_seconds=3600)
        lc.save_folded_ranges(PERSONA_ID, MODEL, [
            FoldedRange(message_ids=["m1", "m2", "m3", "m4", "m5"],
                        chronicle_entry_ids=["e1"], presented_raw=True),
        ])
        assert not _is_hot(lc)
        assert lc.resolve_metabolism_anchor(persona) == ("m6", "frontier")
        assert deserialize_folds(
            _entry(lc).get("folded_ranges"),
        ) == []  # 復元した範囲ごと閉じられ、窓が m6 の 1 件に縮む

        # (2) 実装どおりの順序 (履歴復元で UPDATED_AT=now → 再圧縮で UPDATED_AT 維持) なら
        #     同じ Pulse の resolve は窓をそのまま返す。
        _row(lc, anchor_id="m1", ttl_seconds=3600)
        fold = FoldedRange(message_ids=["m1", "m2", "m3", "m4", "m5"],
                           chronicle_entry_ids=["e1"], presented_raw=True)
        lc.save_folded_ranges(PERSONA_ID, MODEL, [fold])
        _row(lc, anchor_id="m1", ttl_seconds=3600)  # _write_refill による新規設定相当
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
# 4. 要約漏れ修復 (coverage_repair) — 除外マークとアンカー巻き戻しのキャッシュ扱い
# ---------------------------------------------------------------------------


class TestCoverageMarksDoNotClaimAnything:
    def test_marks_keep_the_row_expired_and_never_touch_a_live_one(
        self, session_factory,
    ):
        """除外マークは ``presented_raw=True`` (生ログ提示のまま) なので提示内容が変わらない。

        キャッシュは無効化されないため更新不要。加えて呼び出し元
        (mark_covered_cold_windows) はキャッシュ有効期限内の行を対象外にするため、
        修復後も期限切れのままである。
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
        assert entry["updated_at"] == before    # 期限切れ行を誤って有効化しない
        assert not _is_hot(lc)

    def test_tail_rewind_does_not_mark_row_as_freshly_cached(
        self, session_factory,
    ):
        """run_tail_rewind は直後に LLM 応答が走らないため、キャッシュ失効扱いで書き込む。

        巻き戻し後に圧縮が不要 ("rewound") でも、期限切れ行を誤って有効表示にせず、
        有効だった行もプロンプト変更に合わせて失効扱いにする。
        """
        from sea.coverage_repair import TailRewindPlan, run_tail_rewind

        lc = _make_lifecycle(session_factory)
        persona = SimpleNamespace(
            persona_id=PERSONA_ID,
            sai_memory=SimpleNamespace(is_ready=lambda: True, conn=object()),
        )

        # (1) 期限切れ行 → 巻き戻し後も期限切れのまま
        _row(lc, anchor_id="m1", age_seconds=int(timedelta(days=5).total_seconds()), ttl_seconds=3600)
        assert not _is_hot(lc)
        plan = TailRewindPlan(
            model_key=MODEL, expected_anchor_id="m1", new_anchor_id="gap",
            window_chars_after=500, fold_needed=False,
        )
        with patch("sea.coverage_repair._resolve_uncovered_tail", return_value="gap"), \
                patch("sea.coverage_repair.plan_tail_rewind", return_value=plan):
            assert run_tail_rewind(lc, persona) == "rewound"

        entry = _entry(lc)
        assert entry["anchor_id"] == "gap"
        assert "ttl_seconds" not in entry
        assert not _is_hot(lc)

        # (2) 有効期限内の行 → アンカーが変わってキャッシュが効かなくなるので失効扱い
        _row(lc, anchor_id="m2", ttl_seconds=3600)
        assert _is_hot(lc)
        plan2 = TailRewindPlan(
            model_key=MODEL, expected_anchor_id="m2", new_anchor_id="gap2",
            window_chars_after=500, fold_needed=False,
        )
        with patch("sea.coverage_repair._resolve_uncovered_tail", return_value="gap2"), \
                patch("sea.coverage_repair.plan_tail_rewind", return_value=plan2):
            assert run_tail_rewind(lc, persona) == "rewound"

        entry2 = _entry(lc)
        assert entry2["anchor_id"] == "gap2"
        assert "ttl_seconds" not in entry2
        assert not _is_hot(lc)
