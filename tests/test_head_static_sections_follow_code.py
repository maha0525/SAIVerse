"""静的セクションの文言は、保存済み snapshot ではなくコードが正本 (2026-09-01)。

head snapshot は DB 永続 (`session_head_snapshot`) なので、deserialize が保存値を
そのまま復元すると、コード側の文言を直しても既存ユーザーの head には次の再構築
(Metabolism / anchor TTL 切れ) まで旧文言が残る。放置されたペルソナでは恒久的に
残りうる — 文言修正が「いつ届くか分からない配布」になってしまう。

対処は 2 通りで、snapshot が何を持っているかで分かれる:

- `autonomy_modes` / `self_image` ... snapshot の中身が定数そのもの。deserialize は
  保存値を捨てて現在の定数を返す (比較も版番号も要らない — 定数が版)。
- `common_prompt` ... snapshot は persona/Building を焼き込んだ**展開結果**で、
  これは状態でもある。deserialize に ctx が渡らないので展開し直せない。そこで
  テンプレートの指紋だけを比べ、違えば SnapshotStaleError で再 capture させる。
"""
from __future__ import annotations

import json

import pytest

from sea.head_pipeline.sections import common_prompt as common_prompt_mod
from sea.head_pipeline.sections.autonomy_modes import (
    _AUTONOMY_MODES_TEXT,
    AutonomyModesSection,
)
from sea.head_pipeline.sections.common_prompt import (
    CommonPromptSection,
    CommonPromptSnapshot,
)
from sea.head_pipeline.sections.self_image import (
    DRIVE_TEXT,
    MARK_NOTATION_TEXT,
    SelfImageSection,
)
from sea.head_pipeline.types import SnapshotStaleError

# v0.3.1 で削った文言 (自律行動 / Track)。実在ユーザーの保存済み行に入っている形。
_OLD_AUTONOMY_TEXT = (
    "## 自律行動\nユーザーのUI操作によって自律行動が開始されます。\n\n"
    "## Track\nTrackはあなたが長期的に取り組む大目的の単位です。\n\n"
    "## モード\n以下の4モードに分けられます。"
)


# ---------------------------------------------------------------------------
# 定数返しの 2 セクション
# ---------------------------------------------------------------------------


def test_autonomy_modes_restores_the_current_constant_not_the_stored_text():
    section = AutonomyModesSection()
    stored = json.dumps({"text": _OLD_AUTONOMY_TEXT}, ensure_ascii=False)

    snapshot = section.deserialize_snapshot(stored)
    rendered = section.render(snapshot)

    assert snapshot.text == _AUTONOMY_MODES_TEXT
    assert rendered is not None
    # 削った節が復活しない
    assert "## 自律行動" not in rendered.text
    assert "## Track" not in rendered.text
    assert "自律制御モード" not in rendered.text
    assert "自律作業モード" not in rendered.text
    # 残した節は出る
    assert "### メインモード" in rendered.text
    assert "### 分身モード" in rendered.text


def test_self_image_restores_the_current_constants_not_the_stored_text():
    section = SelfImageSection()
    stored = json.dumps(
        # 旧フィールド (purpose_text) 入りの行も想定に含める
        {"drive_text": "## 内発的な動機\n旧い駆動文。", "purpose_text": "旧い目的"},
        ensure_ascii=False,
    )

    snapshot = section.deserialize_snapshot(stored)
    rendered = section.render(snapshot)

    assert snapshot.drive_text == DRIVE_TEXT
    assert rendered is not None
    assert "旧い駆動文" not in rendered.text
    assert DRIVE_TEXT in rendered.text
    # render 時に読む定数 (mark 記法) も現在値
    assert MARK_NOTATION_TEXT in rendered.text
    assert "あとで思い出したい" not in rendered.text


def test_static_sections_round_trip_their_own_serialization():
    """serialize の形は変えていない (保存済み行との互換)。"""
    autonomy = AutonomyModesSection()
    restored = autonomy.deserialize_snapshot(
        autonomy.serialize_snapshot(autonomy.capture(None))
    )
    assert restored.text == _AUTONOMY_MODES_TEXT

    self_image = SelfImageSection()
    restored_si = self_image.deserialize_snapshot(
        self_image.serialize_snapshot(self_image.capture(None))
    )
    assert restored_si.drive_text == DRIVE_TEXT


# ---------------------------------------------------------------------------
# common_prompt (展開結果 = 状態なので、指紋で再 capture を要求する)
# ---------------------------------------------------------------------------


@pytest.fixture
def template(tmp_path, monkeypatch):
    """`find_file` が返すテンプレートを差し替えられるようにする。"""
    path = tmp_path / "common.txt"
    path.write_text("## パルスシステム\nきっかけは2種類。", encoding="utf-8")
    monkeypatch.setattr(
        "saiverse.data_paths.find_file",
        lambda subdir, filename: path if filename == "common.txt" else None,
    )
    return path


def _stored(text, fingerprint):
    payload = {"text": text}
    if fingerprint is not None:
        payload["template_fingerprint"] = fingerprint
    return json.dumps(payload, ensure_ascii=False)


def test_matching_fingerprint_restores_the_stored_expansion(template):
    """テンプレートが変わっていなければ保存値をそのまま使う。

    展開結果は persona/Building 依存なので、変わっていない限り捨てる理由が無い。
    """
    section = CommonPromptSection()
    current = common_prompt_mod._template_fingerprint()
    snapshot = section.deserialize_snapshot(_stored("エアのための展開結果", current))
    assert snapshot.text == "エアのための展開結果"
    assert snapshot.template_fingerprint == current


def test_changed_template_forces_a_recapture(template):
    """テンプレートを直したら失効 — これが無いと旧文言が head に残り続ける。"""
    section = CommonPromptSection()
    stale = _stored("旧テンプレートの展開結果", "0" * 64)
    with pytest.raises(SnapshotStaleError):
        section.deserialize_snapshot(stale)


def test_rows_without_a_fingerprint_recapture_once(template):
    """指紋を持たない旧行 (v0.3.0 以前) は一度だけ再 capture に落ちる。"""
    section = CommonPromptSection()
    with pytest.raises(SnapshotStaleError):
        section.deserialize_snapshot(_stored("指紋なしの旧行", None))


def test_unreadable_template_falls_back_to_the_stored_snapshot(monkeypatch):
    """テンプレートを読めないときは保存値を返す (stale-but-real)。

    読めない状況で毎回失効させると head を組めない側へ倒れる。required Section
    なので、そこで fail-closed に落ちると会話そのものが止まる。
    """
    section = CommonPromptSection()

    def _boom(subdir, filename):
        raise OSError("prompts directory is gone")

    monkeypatch.setattr("saiverse.data_paths.find_file", _boom)
    assert common_prompt_mod._template_fingerprint() is None

    snapshot = section.deserialize_snapshot(_stored("読めないときの保存値", "abc"))
    assert snapshot.text == "読めないときの保存値"


def test_capture_records_the_current_fingerprint(template):
    """capture が指紋を焼く — 次回の deserialize がこれと比べる。"""
    from types import SimpleNamespace

    section = CommonPromptSection()
    persona = SimpleNamespace(
        common_prompt="こんにちは {current_persona_name}",
        persona_name="エア",
        persona_id="air",
        current_city_id="city_a",
        persona_system_instruction="",
        linked_user_name="まはー",
        buildings={},
    )
    ctx = SimpleNamespace(persona=persona, current_building_id="room")

    snapshot = section.capture(ctx)
    assert snapshot.text == "こんにちは エア"
    assert snapshot.template_fingerprint == common_prompt_mod._template_fingerprint()

    # 焼いた指紋のまま round trip すれば失効しない
    restored = section.deserialize_snapshot(section.serialize_snapshot(snapshot))
    assert restored == snapshot


def test_snapshot_defaults_keep_old_rows_constructible():
    """指紋フィールドは既定つき — 旧行の dict をそのまま渡しても壊れない。"""
    assert CommonPromptSnapshot(text="x").template_fingerprint == ""


# ---------------------------------------------------------------------------
# 失効の着地点 (required Section を欠いたまま LLM へ行かないこと)
# ---------------------------------------------------------------------------


def test_stale_common_prompt_is_recaptured_not_fail_closed(template, monkeypatch):
    """失効した common_prompt は、LLM 前の自己修復で埋め直される。

    common_prompt は required Section なので、欠けたまま render まで行くと
    HeadNotReadyError で会話が止まる。既存ユーザー全員が上げた直後にそれを
    踏むと実害が大きいので、``ensure_snapshot`` の欠損補填
    (recapture_missing) に乗ることを実際の pipeline で確かめる。
    """
    from types import SimpleNamespace

    from sea.head_pipeline.integration import ensure_snapshot
    from sea.head_pipeline.pipeline import HeadPipeline
    from sea.head_pipeline.registry import HeadSectionRegistry
    from sea.head_pipeline.types import LineHeadInput

    registry = HeadSectionRegistry()
    registry.register(CommonPromptSection())
    pipeline = HeadPipeline(registry=registry)

    persona = SimpleNamespace(
        common_prompt=template.read_text(encoding="utf-8"),
        persona_name="エア", persona_id="air", current_city_id="city_a",
        persona_system_instruction="", linked_user_name="まはー", buildings={},
    )
    ctx = LineHeadInput(
        persona_id="air", model_key="m", current_building_id="room",
        persona=persona, manager=None,
    )
    pipeline.capture_all(ctx)

    # テンプレートを書き換える = 保存済み snapshot の指紋が古くなる状況
    template.write_text("## パルスシステム\nきっかけは1種類。", encoding="utf-8")
    persona.common_prompt = template.read_text(encoding="utf-8")

    # 保存 → 復元の往復を、失効を挟んで再現する
    section = CommonPromptSection()
    stored = section.serialize_snapshot(
        pipeline.get_snapshot("air", "m").sections["common_prompt"]
    )
    with pytest.raises(SnapshotStaleError):
        section.deserialize_snapshot(stored)

    # 復元で欠けた state を作り、自己修復に通す
    pipeline.discard_session(ctx.persona_id, ctx.model_key)
    ensure_snapshot(pipeline, ctx)

    restored = pipeline.get_snapshot("air", "m").sections["common_prompt"]
    assert "きっかけは1種類" in restored.text
    assert restored.template_fingerprint == common_prompt_mod._template_fingerprint()
