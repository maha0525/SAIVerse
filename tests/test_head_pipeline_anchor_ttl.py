"""Tests for ensure_snapshot anchor-TTL based refresh.

prompt cache の真の起点である anchor.updated_at を ctx に積み、 TTL 超過時に
snapshot を全 capture し直す経路の挙動を検証する。 詳細:
docs/intent/cached_head_architecture.md (anchor-TTL 連携) /
2026-05-21 セッション (= 夜放置 → 朝挨拶で snapshot が古いまま render される問題)。
"""
import time
from typing import Any
from unittest.mock import patch

import pytest

from sea.head_pipeline import (
    EventType,
    HeadPipeline,
    HeadSectionRegistry,
    LineHeadInput,
    NotificationLabel,
    RenderedSection,
    ensure_snapshot,
)


class _SpellListLike:
    """spell_list 相当の section。 capture でカウンタ増やして refresh を観測する。"""
    name = "spell_list"
    order = 600
    refresh_on_events = frozenset({EventType.ADDON_LOADED, EventType.ADDON_UNLOADED})

    def __init__(self):
        self.capture_count = 0
        self.live_spells = ("spell_a",)

    def capture(self, ctx):
        self.capture_count += 1
        return tuple(self.live_spells)

    def render(self, snapshot):
        if not snapshot:
            return None
        return RenderedSection(text=", ".join(snapshot))

    def diff_to_notifications(self, old, new):
        return []

    def serialize_snapshot(self, snapshot):
        import json
        return json.dumps(list(snapshot))

    def deserialize_snapshot(self, data):
        import json
        return tuple(json.loads(data))


@pytest.fixture
def pipeline():
    registry = HeadSectionRegistry()
    section = _SpellListLike()
    registry.register(section)
    pipeline = HeadPipeline(registry=registry)
    return pipeline, section


def _make_ctx(*, anchor_updated_at=None, cache_ttl_seconds=None):
    return LineHeadInput(
        persona_id="air",
        model_key="claude-opus-4-7",
        current_building_id="b_lobby",
        anchor_updated_at=anchor_updated_at,
        cache_ttl_seconds=cache_ttl_seconds,
    )


def test_ensure_snapshot_first_capture_when_no_snapshot(pipeline):
    pipeline_obj, section = pipeline
    ctx = _make_ctx()
    ensure_snapshot(pipeline_obj, ctx)
    assert section.capture_count == 1


def test_ensure_snapshot_reuses_when_ttl_not_set(pipeline):
    """anchor_updated_at / cache_ttl_seconds が None なら判定スキップ = 既存 snapshot を使う。"""
    pipeline_obj, section = pipeline
    ctx = _make_ctx()
    ensure_snapshot(pipeline_obj, ctx)
    assert section.capture_count == 1
    ensure_snapshot(pipeline_obj, ctx)
    assert section.capture_count == 1  # 再 capture されない


def test_ensure_snapshot_reuses_when_ttl_within(pipeline):
    """TTL 内 (anchor_updated_at + ttl > now) なら既存 snapshot を使う = head 不変。"""
    pipeline_obj, section = pipeline
    now = time.time()
    ctx = _make_ctx(anchor_updated_at=now - 60, cache_ttl_seconds=300)  # 60s ago, 5min TTL
    ensure_snapshot(pipeline_obj, ctx)
    assert section.capture_count == 1
    ensure_snapshot(pipeline_obj, ctx)
    assert section.capture_count == 1  # cache hit 想定で snapshot 不変


def test_ensure_snapshot_recaptures_when_ttl_expired(pipeline):
    """TTL 超過 (anchor_updated_at + ttl < now) なら全 Section 再 capture。

    夜放置 → 朝挨拶のシナリオ: 昨夜の anchor (updated_at) と現在の差が TTL を超えていれば
    cache hit はもう成立しないので、 snapshot も最新化する方が情報量で勝る。
    """
    pipeline_obj, section = pipeline
    now = time.time()
    ctx_fresh = _make_ctx(anchor_updated_at=now - 60, cache_ttl_seconds=300)
    ensure_snapshot(pipeline_obj, ctx_fresh)
    assert section.capture_count == 1

    # TTL 超過 (1 時間前、 TTL=5min)
    ctx_expired = _make_ctx(anchor_updated_at=now - 3600, cache_ttl_seconds=300)
    ensure_snapshot(pipeline_obj, ctx_expired)
    assert section.capture_count == 2  # 再 capture された


def test_ensure_snapshot_recaptures_on_long_ttl_expiry(pipeline):
    """Gemini 等 implicit cache の 1200s validity を想定したケース。"""
    pipeline_obj, section = pipeline
    now = time.time()
    ctx_fresh = _make_ctx(anchor_updated_at=now - 600, cache_ttl_seconds=1200)  # 10min ago
    ensure_snapshot(pipeline_obj, ctx_fresh)
    assert section.capture_count == 1

    # TTL 超過 (30 分前、 TTL=1200s=20min)
    ctx_expired = _make_ctx(anchor_updated_at=now - 1800, cache_ttl_seconds=1200)
    ensure_snapshot(pipeline_obj, ctx_expired)
    assert section.capture_count == 2


def test_ensure_snapshot_partial_ttl_only_one_field(pipeline):
    """anchor_updated_at だけ / cache_ttl_seconds だけ ある時は判定スキップ。"""
    pipeline_obj, section = pipeline
    now = time.time()
    ctx_no_ttl = _make_ctx(anchor_updated_at=now - 3600, cache_ttl_seconds=None)
    ensure_snapshot(pipeline_obj, ctx_no_ttl)
    assert section.capture_count == 1
    ensure_snapshot(pipeline_obj, ctx_no_ttl)
    assert section.capture_count == 1  # 再 capture されない (= None 安全側)

    ctx_no_anchor = _make_ctx(anchor_updated_at=None, cache_ttl_seconds=300)
    ensure_snapshot(pipeline_obj, ctx_no_anchor)
    assert section.capture_count == 1


class _DiffingSpellSection(_SpellListLike):
    """diff 通知を実際に出す版 (入退室検知と同じ「集合の増減」型)。"""

    def diff_to_notifications(self, old, new):
        if old is None or new is None:
            return []
        return [
            NotificationLabel(kind="spell_removed", label=f"{s} が使えなくなりました")
            for s in sorted(set(old) - set(new))
        ]


def test_ttl_recapture_preserves_diff_baseline():
    """TTL 超過の再 capture は B (通知の既読基準) を据え置く。

    回帰 (2026-08-17 実運用): エリスの入退室通知が 1 ヶ月間全滅していた実バグ。
    休止中に起きた変化 (ユーザー退室等) が、TTL 切れ Pulse 頭の capture_all で
    「既読」に上書きされ、直後の flush_diffs が差分なしになっていた。
    再 capture 後の flush で休止中の差分が届くことを検証する。
    """
    registry = HeadSectionRegistry()
    section = _DiffingSpellSection()
    section.live_spells = ("spell_a", "spell_b")
    registry.register(section)
    pipeline_obj = HeadPipeline(registry=registry)
    now = time.time()

    ensure_snapshot(
        pipeline_obj, _make_ctx(anchor_updated_at=now - 60, cache_ttl_seconds=300),
    )  # 初回 capture: A = B = (spell_a, spell_b)

    section.live_spells = ("spell_a",)  # 休止中に spell_b が消えた

    ctx_expired = _make_ctx(anchor_updated_at=now - 3600, cache_ttl_seconds=300)
    ensure_snapshot(pipeline_obj, ctx_expired)  # TTL 超過 → 再 capture

    labels = pipeline_obj.flush_diffs(ctx_expired, all_sections=True)
    assert [label.kind for label in labels] == ["spell_removed"]


# ---- build_line_head_input integration ----


class _FakeLifecycle:
    """SessionLifecycle の最小フェイク (anchor 状態解決に必要な公開 API だけ)。"""

    def __init__(self, anchors_dict, validity_map):
        self._anchors = anchors_dict
        self._validity = validity_map

    def load_anchors(self, persona):
        return self._anchors

    def get_anchor_validity_seconds(self, model_key, persona_id=None):
        return self._validity.get(model_key, 1200)


class _FakeRuntime:
    def __init__(self, anchors_dict, validity_map=None):
        self.session_lifecycle = _FakeLifecycle(anchors_dict, validity_map or {})


class _FakeManager:
    def __init__(self, runtime):
        self.sea_runtime = runtime


class _FakePersona:
    def __init__(self, persona_id="air", model="claude-opus-4-7"):
        self.persona_id = persona_id
        self.default_model = model


def test_build_line_head_input_resolves_anchor_ttl():
    """build_line_head_input が manager 経由で anchor 状態を解決して ctx に積む。"""
    from sea.head_pipeline.integration import build_line_head_input
    from datetime import datetime

    iso_time = "2026-05-21T03:00:00"
    expected_epoch = datetime.fromisoformat(iso_time).timestamp()
    runtime = _FakeRuntime(
        {"claude-opus-4-7": {"anchor_id": "msg_abc", "updated_at": iso_time}},
        validity_map={"claude-opus-4-7": 300},
    )
    manager = _FakeManager(runtime)
    persona = _FakePersona()

    ctx = build_line_head_input(persona, manager, "b_lobby")
    assert ctx.anchor_updated_at == pytest.approx(expected_epoch)
    assert ctx.cache_ttl_seconds == 300


def test_build_line_head_input_no_anchor_returns_none():
    """anchor 未登録 (= 初回 / minimal load 後) なら ctx の TTL field は None。"""
    from sea.head_pipeline.integration import build_line_head_input

    runtime = _FakeRuntime({})  # 空 anchors
    manager = _FakeManager(runtime)
    persona = _FakePersona()

    ctx = build_line_head_input(persona, manager, "b_lobby")
    assert ctx.anchor_updated_at is None
    assert ctx.cache_ttl_seconds is None


def test_build_line_head_input_no_runtime_returns_none():
    """manager に sea_runtime が無い (= テスト fixture / 初期化前) なら None。"""
    from sea.head_pipeline.integration import build_line_head_input

    class _BareManager:
        pass

    persona = _FakePersona()
    ctx = build_line_head_input(persona, _BareManager(), "b_lobby")
    assert ctx.anchor_updated_at is None
    assert ctx.cache_ttl_seconds is None


def test_build_line_head_input_malformed_anchor_safe():
    """anchor entry が壊れてる (updated_at 欠落 / 不正 ISO 等) でも None で安全に返す。"""
    from sea.head_pipeline.integration import build_line_head_input

    runtime = _FakeRuntime(
        {"claude-opus-4-7": {"anchor_id": "msg_x"}},  # updated_at 欠落
    )
    manager = _FakeManager(runtime)
    persona = _FakePersona()

    ctx = build_line_head_input(persona, manager, "b_lobby")
    assert ctx.anchor_updated_at is None
    assert ctx.cache_ttl_seconds is None
