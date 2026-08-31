"""W6: head の fail-closed 化 (SEA 監査 S6) の回帰テスト。

required Section (人格の同一性を担う common_prompt / persona_self / core_memory
相当) の capture / render / persist が失敗したまま LLM を実行しない —
render_head_messages / prepare_context が HeadNotReadyError を投げて Pulse を
中断し、復旧後は次の呼び出しで自己修復することを固定する。

- capture 失敗: 既存値なし → 欠損 (key 省略 + capture_failures 記帳) → raise。
  既存値あり → stale-but-real で据え置き → raise しない。
- render 失敗: required は raise、optional は skip + degrade。
- persist 失敗: store.save 失敗が残っている間は raise、次の呼び出しの
  ensure_persisted 再試行で復旧。
- None Section 値 (旧実装の失敗痕跡 / 旧永続行) は欠損として再 capture される。
- store.save は commit 失敗 / required Section の serialize 失敗で False を返す。
"""
import json
from types import SimpleNamespace

import pytest

from sea.head_pipeline import (
    HeadNotReadyError,
    HeadPipeline,
    HeadSectionRegistry,
    LineHeadInput,
    NotificationLabel,
    RenderedSection,
)
from sea.head_pipeline.integration import render_head_messages


MODEL = "claude-opus-4-7"


class FlakySection:
    """失敗を注入できる汎用テスト Section。required は生成時に指定。"""

    def __init__(self, name, order, *, required=False):
        self.name = name
        self.order = order
        self.required = required
        self.refresh_on_events = frozenset()
        self.value = f"{name}-v1"
        self.fail_capture = False
        self.fail_render = False
        self.fail_serialize = False

    def capture(self, ctx):
        if self.fail_capture:
            raise RuntimeError(f"{self.name}: capture boom")
        return {"text": self.value}

    def render(self, snapshot):
        if self.fail_render:
            raise RuntimeError(f"{self.name}: render boom")
        if not snapshot or not snapshot.get("text"):
            return None
        return RenderedSection(text=snapshot["text"])

    def diff_to_notifications(self, old, new):
        return []

    def serialize_snapshot(self, snapshot):
        if self.fail_serialize:
            raise RuntimeError(f"{self.name}: serialize boom")
        return json.dumps(snapshot or {})

    def deserialize_snapshot(self, data):
        return json.loads(data)


class FakeStore:
    """save の成否を切り替えられる duck-typed store。"""

    def __init__(self):
        self.fail_save = False
        self.save_calls = 0

    def save(self, snapshot, last_notified_sections):
        self.save_calls += 1
        return not self.fail_save

    def save_last_notified(self, persona_id, model_key, last_notified_sections):
        return True

    def load(self, persona_id, model_key):
        return None

    def delete(self, persona_id, model_key):
        pass


@pytest.fixture
def sections():
    return {
        "persona_self": FlakySection("persona_self", 200, required=True),
        "building": FlakySection("building", 300, required=False),
    }


@pytest.fixture
def registry(sections):
    r = HeadSectionRegistry()
    for s in sections.values():
        r.register(s)
    return r


@pytest.fixture
def pipeline(registry):
    return HeadPipeline(registry=registry)


PERSONA = SimpleNamespace(persona_id="air", model=MODEL)
ENABLED = {"persona_self", "building"}


def _render(pipeline, enabled=None):
    # manager=None: anchor TTL 解決は (None, None) スキップ、SYSTEM_PROMPT 組成は
    # 実 Section 名依存なので composition までは行かなくてよい (raise 挙動が主眼)。
    return render_head_messages(
        PERSONA, None, "b_lobby",
        enabled_sections=ENABLED if enabled is None else enabled,
        pipeline=pipeline,
    )


# ---- capture ----

def test_required_capture_failure_blocks_llm_context(pipeline, sections):
    sections["persona_self"].fail_capture = True
    with pytest.raises(HeadNotReadyError) as ei:
        _render(pipeline)
    assert ei.value.stage == "capture"
    assert "persona_self" in ei.value.sections
    # 失敗 Section は None で保存されず key ごと欠損 + 理由記帳
    snapshot = pipeline.get_snapshot("air", MODEL)
    assert "persona_self" not in snapshot.sections
    assert "persona_self" in snapshot.capture_failures


def test_required_capture_recovers_on_next_call(pipeline, sections):
    sections["persona_self"].fail_capture = True
    with pytest.raises(HeadNotReadyError):
        _render(pipeline)
    # 復旧 → 次の呼び出しの ensure_snapshot 再 capture で自己修復
    sections["persona_self"].fail_capture = False
    messages = _render(pipeline)
    snapshot = pipeline.get_snapshot("air", MODEL)
    assert snapshot.sections["persona_self"] == {"text": "persona_self-v1"}
    assert snapshot.capture_failures == {}
    assert messages is not None


def test_optional_capture_failure_degrades_and_recovers(pipeline, sections):
    sections["building"].fail_capture = True
    _render(pipeline)  # raise しない (optional は degrade)
    snapshot = pipeline.get_snapshot("air", MODEL)
    assert "building" not in snapshot.sections
    assert "building" in snapshot.capture_failures
    # 次 Pulse: 欠損 → 再 capture で自動回復
    sections["building"].fail_capture = False
    _render(pipeline)
    snapshot = pipeline.get_snapshot("air", MODEL)
    assert snapshot.sections["building"] == {"text": "building-v1"}


def test_required_capture_failure_with_existing_value_keeps_stale(pipeline, sections):
    ctx = LineHeadInput(persona_id="air", model_key=MODEL, current_building_id="b_lobby")
    pipeline.capture_all(ctx)  # v1 が入る
    sections["persona_self"].value = "persona_self-v2"
    sections["persona_self"].fail_capture = True
    pipeline.capture_all(ctx)  # 再 capture 失敗 → 既存値据え置き
    snapshot = pipeline.get_snapshot("air", MODEL)
    assert snapshot.sections["persona_self"] == {"text": "persona_self-v1"}
    assert "persona_self" not in snapshot.capture_failures
    # stale-but-real なので LLM 実行は止めない
    messages = _render(pipeline)
    assert messages is not None


def test_stale_reuse_does_not_roll_back_last_notified(registry):
    """capture 失敗で A を据え置いた Section は、B (既読基準) も据え置く。

    2026-07-30 Codex 指摘 high1。当時の capture_all は B を新 A で丸ごと初期化
    しており、capture 失敗で**古い A を再利用した** Section では、diff 通知で
    live state まで進んでいた B が古い A へ巻き戻り、復旧後に「もう届けた変化」
    を再通知してしまう — という穴だった。2026-08-17 に初期化自体を撤去し、
    現在は既存 B を持つ全 Section が無条件で据え置き (B は配送だけが進める)。
    本テストはその据え置きが stale 再利用でも成り立つことの回帰として残す。
    """
    class DiffingSection(FlakySection):
        def diff_to_notifications(self, old, new):
            if old == new:
                return []
            return [NotificationLabel(kind="changed", label=f"{self.name} changed")]

    section = DiffingSection("building", 300)
    registry.unregister("building")
    registry.register(section)
    pipeline = HeadPipeline(registry=registry)
    ctx = LineHeadInput(persona_id="air", model_key=MODEL, current_building_id="b_lobby")

    pipeline.capture_all(ctx)                      # A = B = v1
    section.value = "building-v2"
    assert pipeline.flush_diffs(ctx, all_sections=True)  # 通知済み: B = v2

    section.fail_capture = True
    pipeline.capture_all(ctx)                      # A は v1 に据え置き
    assert pipeline.get_snapshot("air", MODEL).sections["building"] == {
        "text": "building-v1",
    }

    # 復旧。live state は v2 のまま = 既に届けた変化なので再通知しない
    section.fail_capture = False
    assert pipeline.flush_diffs(ctx, all_sections=True) == []


def test_none_section_value_is_treated_as_missing(pipeline, sections):
    ctx = LineHeadInput(persona_id="air", model_key=MODEL, current_building_id="b_lobby")
    pipeline.capture_all(ctx)
    # 旧実装の失敗痕跡 (value=None) を再現 → 欠損として再 capture されること
    snapshot = pipeline.get_snapshot("air", MODEL)
    snapshot.sections["persona_self"] = None
    _render(pipeline)
    snapshot = pipeline.get_snapshot("air", MODEL)
    assert snapshot.sections["persona_self"] == {"text": "persona_self-v1"}


# ---- render ----

def test_required_render_failure_raises(pipeline, sections):
    sections["persona_self"].fail_render = True
    with pytest.raises(HeadNotReadyError) as ei:
        _render(pipeline)
    assert ei.value.stage == "render"
    assert "persona_self" in ei.value.sections


def test_optional_render_failure_is_skipped(pipeline, sections):
    sections["building"].fail_render = True
    messages = _render(pipeline)
    assert messages is not None


def test_required_render_failure_recovers(pipeline, sections):
    sections["persona_self"].fail_render = True
    with pytest.raises(HeadNotReadyError):
        _render(pipeline)
    sections["persona_self"].fail_render = False
    assert _render(pipeline) is not None


def test_empty_required_render_is_not_failure(pipeline, sections):
    # 空 (render → None) は「空の本人」の正規挙動で、readiness を満たす
    sections["persona_self"].value = ""
    assert _render(pipeline) is not None


# ---- persist ----

def test_persist_failure_blocks_and_recovers(registry, sections):
    store = FakeStore()
    store.fail_save = True
    pipeline = HeadPipeline(registry=registry, store=store)
    with pytest.raises(HeadNotReadyError) as ei:
        _render(pipeline)
    assert ei.value.stage == "persist"
    # store 復旧 → 次の呼び出しの ensure_persisted 再試行で通る
    store.fail_save = False
    assert _render(pipeline) is not None
    assert store.save_calls >= 2


def test_no_store_means_no_persist_blocking(pipeline):
    # store 未設定 (テスト / startup 前) は永続化なしが構成上の正
    assert _render(pipeline) is not None


def test_stale_persist_success_does_not_mark_new_version(registry, sections):
    # Codex P1: 並行 capture で「旧版の保存成功」が「新版の保存失敗」を
    # 上書きして fail-closed を迂回しないこと (persisted_version は保存した
    # 版にしか結び付かない)
    store = FakeStore()
    pipeline = HeadPipeline(registry=registry, store=store)
    ctx = LineHeadInput(persona_id="air", model_key=MODEL, current_building_id="b")
    snap_v1 = pipeline.capture_all(ctx)          # v1 保存成功
    store.fail_save = True
    pipeline.capture_all(ctx)                     # v2 公開、保存失敗
    # 旧版 v1 の保存が遅れて成功した (並行 save の完了) と見立てる
    store.fail_save = False
    assert pipeline._persist_snapshot(snap_v1, {}) is True
    # v2 はまだ未確認のまま → 保存できない状況では readiness は通らない
    store.fail_save = True
    assert pipeline.ensure_persisted("air", MODEL) is False
    with pytest.raises(HeadNotReadyError):
        _render(pipeline)
    # store 復旧 → ensure_persisted が現行版 v2 を保存して自己修復
    store.fail_save = False
    assert pipeline.ensure_persisted("air", MODEL) is True
    assert _render(pipeline) is not None


# ---- 欠損の自己修復は capture_all でなく欠損限定 (Codex P2) ----

def test_persistent_optional_failure_does_not_rebuild_whole_head(pipeline, sections):
    sections["building"].fail_capture = True
    _render(pipeline)  # 初回 capture_all — building は欠損
    snap1 = pipeline.get_snapshot("air", MODEL)
    ps1 = snap1.sections["persona_self"]
    v1 = snap1.snapshot_version
    # 2 回目以降: 欠損分だけ再試行し、他 Section の snapshot (= cache) と
    # B は無傷 — 版も進まない (全滅時は据え置き)
    _render(pipeline)
    _render(pipeline)
    snap2 = pipeline.get_snapshot("air", MODEL)
    assert snap2.snapshot_version == v1
    assert snap2.sections["persona_self"] is ps1
    assert "building" in snap2.capture_failures


def test_recapture_missing_fills_only_missing_on_recovery(pipeline, sections):
    sections["building"].fail_capture = True
    _render(pipeline)
    snap1 = pipeline.get_snapshot("air", MODEL)
    ps1 = snap1.sections["persona_self"]
    sections["building"].fail_capture = False
    _render(pipeline)
    snap2 = pipeline.get_snapshot("air", MODEL)
    assert snap2.sections["building"] == {"text": "building-v1"}
    assert snap2.sections["persona_self"] is ps1  # 復旧が他 Section を作り直さない
    assert snap2.snapshot_version == snap1.snapshot_version + 1
    assert "building" not in snap2.capture_failures


def test_required_sections_not_enabled_skips_readiness(pipeline, sections):
    # required Section を head に載せない呼び出し (optional のみ) は
    # required の failure に巻き込まれない
    sections["persona_self"].fail_capture = True
    messages = _render(pipeline, enabled={"building"})
    assert messages is not None


# ---- store の成否契約 ----

def _make_db_store(registry, session_factory):
    from sea.head_pipeline import LineHeadSnapshotStore
    return LineHeadSnapshotStore(session_factory=session_factory, registry=registry)


def test_store_save_returns_false_on_commit_failure(registry, sections):
    # DB 障害 (query/commit 例外) を session mock で再現する。session factory
    # 自体が死ぬケースは pipeline._persist_snapshot の except が False 扱いにする
    class BrokenSession:
        def query(self, *a, **k):
            raise RuntimeError("db down")

        def rollback(self):
            pass

        def close(self):
            pass

    store = _make_db_store(registry, lambda: BrokenSession())
    ctx = LineHeadInput(persona_id="air", model_key=MODEL, current_building_id="b")
    pipeline = HeadPipeline(registry=registry)
    snapshot_obj = pipeline.capture_all(ctx)
    assert store.save(snapshot_obj, {}) is False


def test_store_save_returns_false_on_required_serialize_failure(
    registry, sections, monkeypatch,
):
    committed = {"count": 0}

    class OkSession:
        def query(self, *a, **k):
            class _Q:
                def filter(self, *a, **k):
                    return self

                def filter_by(self, **kw):
                    return self

                def update(self, values, synchronize_session=False):
                    return 1  # 既存行を条件付き UPDATE で更新できた

                def first(self):
                    return None

            return _Q()

        def add(self, row):
            pass

        def commit(self):
            committed["count"] += 1

        def rollback(self):
            pass

        def close(self):
            pass

    store = _make_db_store(registry, lambda: OkSession())
    pipeline = HeadPipeline(registry=registry)
    ctx = LineHeadInput(persona_id="air", model_key=MODEL, current_building_id="b")
    snapshot_obj = pipeline.capture_all(ctx)

    sections["persona_self"].fail_serialize = True
    assert store.save(snapshot_obj, {}) is False
    # required の serialize 失敗は DB に一切書き込まない (既存の正常な永続
    # snapshot を不完全な行で上書きしない — Codex 二巡 P2)
    assert committed["count"] == 0

    # optional の serialize 失敗は degrade (省いて保存し True)
    sections["persona_self"].fail_serialize = False
    sections["building"].fail_serialize = True
    assert store.save(snapshot_obj, {}) is True
    assert committed["count"] == 1


def test_store_save_refuses_stale_version(registry, sections):
    # 並行 capture の遅延保存 (旧版 commit が新版 commit の後に届く) が DB を
    # 旧 head に巻き戻さない (Codex 二巡 P1)
    committed = {"count": 0}

    class SessionWithNewerRow:
        def query(self, *a, **k):
            class _Q:
                def filter(self, *a, **k):
                    return self

                def filter_by(self, **kw):
                    return self

                def update(self, values, synchronize_session=False):
                    return 0  # 条件付き UPDATE が「より新しい版が居る」で空振り

                def first(self):
                    return (5,)  # 行は存在する (SNAPSHOT_VERSION=5)

            return _Q()

        def commit(self):
            committed["count"] += 1

        def rollback(self):
            pass

        def close(self):
            pass

    store = _make_db_store(registry, lambda: SessionWithNewerRow())
    pipeline = HeadPipeline(registry=registry)
    ctx = LineHeadInput(persona_id="air", model_key=MODEL, current_building_id="b")
    snapshot_v1 = pipeline.capture_all(ctx)  # snapshot_version=1 < stored 5
    assert store.save(snapshot_v1, {}) is False
    assert committed["count"] == 0


def test_store_save_refuses_snapshot_missing_required_section(registry, sections):
    # capture 失敗で required の key ごと省かれた snapshot は serialize を通らない
    # ため、欠損自体を commit 前に拒否する (Codex 三巡 P2 — 再起動直後の
    # capture_all が完全な既存 DB 行を欠損行で上書きしない)
    committed = {"count": 0}

    class CountingSession:
        def query(self, *a, **k):
            class _Q:
                def filter(self, *a, **k):
                    return self

                def filter_by(self, **kw):
                    return self

                def update(self, values, synchronize_session=False):
                    return 1

                def first(self):
                    return None

            return _Q()

        def add(self, row):
            pass

        def commit(self):
            committed["count"] += 1

        def rollback(self):
            pass

        def close(self):
            pass

    store = _make_db_store(registry, lambda: CountingSession())
    pipeline = HeadPipeline(registry=registry)
    ctx = LineHeadInput(persona_id="air", model_key=MODEL, current_building_id="b")
    sections["persona_self"].fail_capture = True
    snapshot = pipeline.capture_all(ctx)  # persona_self 欠損
    assert store.save(snapshot, {}) is False
    assert committed["count"] == 0


def test_persist_marking_requires_snapshot_identity(registry, sections):
    # 同版番号の別オブジェクト (並行 capture_all の採番衝突相当) の保存成功が
    # 現行 snapshot を保存済みに見せかけない (Codex 三巡 P1)
    from dataclasses import replace

    store = FakeStore()
    pipeline = HeadPipeline(registry=registry, store=store)
    ctx = LineHeadInput(persona_id="air", model_key=MODEL, current_building_id="b")
    pipeline.capture_all(ctx)              # v1 保存済み
    store.fail_save = True
    current = pipeline.capture_all(ctx)    # v2 公開、保存失敗
    clone = replace(current)               # 同版・別オブジェクト
    store.fail_save = False
    assert pipeline._persist_snapshot(clone, {}) is True
    # 現行 (別オブジェクト) は未確認のまま → 保存できない状況では通らない
    store.fail_save = True
    assert pipeline.ensure_persisted("air", MODEL) is False


def test_persist_rebases_after_stale_rejection(registry, sections):
    # capture_all 時の load_version 一時失敗で低い版 (v1) から採番してしまった
    # state が、DB 復旧後の readiness 検証で再採番 (DB 版 +1) して自己回復する
    # (Codex 四巡 P1 — 恒久 stale 拒否で fail-closed が解けなくなる毒の封鎖)
    store = FakeStore()
    store.stored_version = 5
    calls = {"n": 0}

    def load_version(persona_id, model_key):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient db read failure")
        return store.stored_version

    def save(snapshot, last_notified_sections):
        if snapshot.snapshot_version < store.stored_version:
            return False  # store.save の stale 版ガード相当
        store.stored_version = snapshot.snapshot_version
        return True

    store.load_version = load_version
    store.save = save
    pipeline = HeadPipeline(registry=registry, store=store)
    ctx = LineHeadInput(persona_id="air", model_key=MODEL, current_building_id="b")
    snap = pipeline.capture_all(ctx)  # 読取失敗 → v1 採番、保存は stale 拒否
    assert snap.snapshot_version == 1
    # readiness 再試行: stale 拒否 → DB 版を読み直し v6 に再採番 → 保存成功
    assert pipeline.ensure_persisted("air", MODEL) is True
    assert pipeline.get_snapshot("air", MODEL).snapshot_version == 6
    assert _render(pipeline) is not None


def test_next_version_continues_from_store_row(registry, sections):
    # 再起動後に load を経ず capture_all へ来た場合、DB の版から採番を継続する
    # (1 から振り直すと stale 版ガードに永久拒否され fail-closed が解けない)
    store = FakeStore()
    store.stored_version = 7

    def load_version(persona_id, model_key):
        return store.stored_version

    store.load_version = load_version
    pipeline = HeadPipeline(registry=registry, store=store)
    ctx = LineHeadInput(persona_id="air", model_key=MODEL, current_building_id="b")
    snapshot = pipeline.capture_all(ctx)
    assert snapshot.snapshot_version == 8


# ---- 版の pin (検証・永続化・描画の一致、Codex 二巡 P1) ----

def test_render_head_renders_pinned_snapshot(pipeline, sections):
    ctx = LineHeadInput(persona_id="air", model_key=MODEL, current_building_id="b")
    pinned = pipeline.capture_all(ctx)
    # 別スレッドの新版公開を模擬
    sections["persona_self"].value = "persona_self-v2"
    pipeline.capture_all(ctx)
    rendered = dict(pipeline.render_head("air", MODEL, snapshot=pinned))
    assert rendered["persona_self"].text == "persona_self-v1"


def test_recapture_missing_does_not_overwrite_concurrent_fill(pipeline, sections):
    ctx = LineHeadInput(persona_id="air", model_key=MODEL, current_building_id="b")
    sections["building"].fail_capture = True
    pipeline.capture_all(ctx)  # building 欠損
    snap1 = pipeline.get_snapshot("air", MODEL)
    v1 = snap1.snapshot_version

    # 再 capture のロック外区間で、並行 capture が同 Section を新値で埋めた
    # 状況を再現する: capture の副作用で state 側を直接埋めてから古い値を返す
    def concurrent_fill_capture(ctx_arg):
        snap = pipeline.get_snapshot("air", MODEL)
        snap.sections["building"] = {"text": "fresh-from-concurrent"}
        return {"text": "stale-from-recapture"}

    sections["building"].fail_capture = False
    sections["building"].capture = concurrent_fill_capture
    result = pipeline.recapture_missing(ctx, {"building"})
    assert result.sections["building"] == {"text": "fresh-from-concurrent"}
    assert result.snapshot_version == v1  # 何も適用しなかったので版は据え置き


# ---- prepare_context の伝播 ----

def _stub_runtime():
    return SimpleNamespace(manager=None, session_lifecycle=None)


def _reqs(**overrides):
    from sea.playbook_models import ContextRequirements
    # head の章立ては呼び出し側から選べない (PERSONA_HEAD_SECTIONS 固定) ので、
    # ここで指定できるのは履歴と実時間情報だけ。
    base = dict(history_depth=0, realtime_context=False)
    base.update(overrides)
    return ContextRequirements(**base)


def test_prepare_context_propagates_head_not_ready(monkeypatch):
    import sea.head_pipeline as hp
    from sea.runtime_context import prepare_context

    def _raise(*a, **k):
        raise HeadNotReadyError("air", MODEL, "capture", {"persona_self": "boom"})

    monkeypatch.setattr(hp, "render_head_messages", _raise)
    with pytest.raises(HeadNotReadyError):
        prepare_context(
            _stub_runtime(), PERSONA, "b_lobby", None,
            requirements=_reqs(), preview_only=True,
        )


def test_prepare_context_escalates_generic_head_failure_when_identity_requested(
    monkeypatch,
):
    import sea.head_pipeline as hp
    from sea.runtime_context import prepare_context

    def _raise(*a, **k):
        raise RuntimeError("pipeline exploded")

    monkeypatch.setattr(hp, "render_head_messages", _raise)
    with pytest.raises(HeadNotReadyError) as ei:
        prepare_context(
            _stub_runtime(), PERSONA, "b_lobby", None,
            requirements=_reqs(), preview_only=True,
        )
    assert ei.value.stage == "pipeline"


def test_prepare_context_has_no_degrade_path(monkeypatch):
    """head 構築の失敗に「人格なしで続行する」逃げ道が無いこと。

    2026-07-23 以前は ``system_prompt=False`` (= 人格を要求しない head) の
    呼び出しに限り、pipeline 失敗を握り潰して空の head で続行していた。head の
    章立てを固定 (PERSONA_HEAD_SECTIONS) して呼び出し側から選べなくしたので、
    「人格を要求しない呼び出し」自体が存在しなくなり、degrade 経路も消えた。

    どんな呼び出しでも head が組めなければ落ちる = 人格に属さない発話を
    本人履歴へ確定させない (W6 / SEA 監査 S6 の fail-closed)。
    """
    import sea.head_pipeline as hp
    from sea.runtime_context import prepare_context

    def _raise(*a, **k):
        raise RuntimeError("pipeline exploded")

    monkeypatch.setattr(hp, "render_head_messages", _raise)
    with pytest.raises(HeadNotReadyError) as ei:
        prepare_context(
            _stub_runtime(), PERSONA, "b_lobby", None,
            requirements=_reqs(), preview_only=True,
        )
    assert ei.value.stage == "pipeline"
