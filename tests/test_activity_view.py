"""ライフビュー (saiverse/activity_view.py) の純粋ロジックのテスト。

- 自律 Pulse 間隔の実効値解決 (Track metadata > ペルソナ設定 > Handler デフォルト)
- pulse_logs.tool_calls (OpenAI 形式 JSON) のパース
- ダイジェストラベル組み立て (1 Pulse = 1 行、生活の言葉)

詳細: docs/intent/persona_activity_view.md §5, §7
"""
import json

from unittest.mock import MagicMock

from saiverse.activity_view import (
    AUTONOMOUS_PULSE_INTERVAL_KEY,
    build_activity_label,
    build_digest_label,
    format_tool_phrase,
    parse_tool_calls_json,
    resolve_autonomous_pulse_interval,
)


# ---------------------------------------------------------------------------
# resolve_autonomous_pulse_interval: 優先順の検証
# ---------------------------------------------------------------------------

def test_interval_track_metadata_wins():
    interval = resolve_autonomous_pulse_interval(
        {"pulse_interval_seconds": 120},
        {AUTONOMOUS_PULSE_INTERVAL_KEY: 60},
        handler_default=30,
    )
    assert interval == 120


def test_interval_persona_config_over_default():
    interval = resolve_autonomous_pulse_interval(
        {}, {AUTONOMOUS_PULSE_INTERVAL_KEY: 60}, handler_default=30,
    )
    assert interval == 60


def test_interval_handler_default_fallback():
    assert resolve_autonomous_pulse_interval({}, {}, handler_default=30) == 30
    assert resolve_autonomous_pulse_interval(None, None, handler_default=45) == 45


def test_interval_invalid_values_fall_through():
    # Track metadata が不正値 → ペルソナ設定へ、それも不正 → デフォルト
    interval = resolve_autonomous_pulse_interval(
        {"pulse_interval_seconds": "abc"},
        {AUTONOMOUS_PULSE_INTERVAL_KEY: None},
        handler_default=30,
    )
    assert interval == 30


def test_interval_zero_means_continuous():
    # 0 は「毎 poll 連続実行」の有効値 (既存スケジューラ仕様)。潰してはならない。
    assert resolve_autonomous_pulse_interval({"pulse_interval_seconds": 0}, {}) == 0


def test_interval_negative_clamped_to_zero():
    assert resolve_autonomous_pulse_interval({"pulse_interval_seconds": -5}, {}) == 0


# ---------------------------------------------------------------------------
# parse_tool_calls_json: OpenAI 形式 tool_calls の展開
# ---------------------------------------------------------------------------

def _tool_calls_json(name: str, args: dict) -> str:
    return json.dumps([
        {
            "id": "tc_1",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
        }
    ], ensure_ascii=False)


def test_parse_tool_calls_basic():
    raw = _tool_calls_json("searxng_search", {"query": "短歌 季語"})
    parsed = parse_tool_calls_json(raw)
    assert parsed == [("searxng_search", {"query": "短歌 季語"})]


def test_parse_tool_calls_malformed_returns_empty():
    assert parse_tool_calls_json(None) == []
    assert parse_tool_calls_json("") == []
    assert parse_tool_calls_json("not json") == []
    assert parse_tool_calls_json('{"not": "a list"}') == []


def test_parse_tool_calls_bad_arguments_yields_empty_args():
    raw = json.dumps([
        {"id": "x", "type": "function",
         "function": {"name": "calculator", "arguments": "{{broken"}}
    ])
    parsed = parse_tool_calls_json(raw)
    assert parsed == [("calculator", {})]


# ---------------------------------------------------------------------------
# format_tool_phrase: 生活の言葉への変換
# ---------------------------------------------------------------------------

def test_phrase_known_tool_with_arg():
    phrase = format_tool_phrase("searxng_search", {"query": "短歌 季語"})
    assert phrase == "Web で「短歌 季語」を調べた"


def test_phrase_known_tool_without_arg():
    phrase = format_tool_phrase("searxng_search", {})
    assert phrase == "Web で調べものをした"


def test_phrase_unknown_tool_generic():
    # アドオン / MCP ツール等はそのまま名前を見せる
    phrase = format_tool_phrase("switchbot_get_temperature", {})
    assert phrase == "「switchbot_get_temperature」を使った"


def test_phrase_hidden_tool_returns_none():
    # 内部ブックキーピング系はダイジェストに出さない
    assert format_tool_phrase("track_list", {}) is None
    assert format_tool_phrase("meta_judgment_finalize", {}) is None
    assert format_tool_phrase("get_building_messages", {}) is None


def test_phrase_long_arg_truncated():
    phrase = format_tool_phrase("searxng_search", {"query": "あ" * 100})
    assert phrase is not None
    assert "…" in phrase
    assert len(phrase) < 70


# ---------------------------------------------------------------------------
# build_digest_label: 1 Pulse = 1 行
# ---------------------------------------------------------------------------

def test_digest_tool_phrases_take_priority():
    label = build_digest_label(
        track_title="プロット推敲",
        track_type="autonomous",
        line_roles=["sub_line"],
        tool_calls=[("searxng_search", {"query": "短歌 季語"})],
    )
    assert label == "Web で「短歌 季語」を調べた"


def test_digest_multiple_tools_joined_max_two():
    label = build_digest_label(
        track_title=None,
        track_type="autonomous",
        line_roles=["sub_line"],
        tool_calls=[
            ("searxng_search", {"query": "a"}),
            ("calculator", {}),
            ("image_generator", {}),  # 3 件目は捨てられる
        ],
    )
    assert label == "Web で「a」を調べた、計算をした"


def test_digest_hidden_tools_fall_back_to_track_label():
    label = build_digest_label(
        track_title="メモ整理",
        track_type="autonomous",
        line_roles=["sub_line"],
        tool_calls=[("track_list", {})],
    )
    assert label == "「メモ整理」を進めた"


def test_digest_autonomous_without_title():
    label = build_digest_label(
        track_title=None,
        track_type="autonomous",
        line_roles=["sub_line"],
        tool_calls=[],
    )
    assert label == "自分の作業を進めた"


# ---------------------------------------------------------------------------
# メタ判断結果の詳細化 (spells_emitted → 遷移先表示)
# ---------------------------------------------------------------------------

def test_digest_meta_judgment_no_spells():
    """spell なし = 現状を続けることにした"""
    label = build_digest_label(
        track_title=None, track_type=None,
        line_roles=["meta_judgment"], tool_calls=[],
        spells_emitted=None,
    )
    assert "現状を続ける" in label


def test_digest_meta_judgment_activate_autonomous():
    """track_activate で autonomous Track に切り替え → タイトル表示"""
    mock_track = MagicMock()
    mock_track.track_type = "autonomous"
    mock_track.title = "メモ整理"
    label = build_digest_label(
        track_title=None, track_type=None,
        line_roles=["meta_judgment"], tool_calls=[],
        spells_emitted=[{"name": "track_activate", "args": {"track_id": "t:3"}}],
        track_resolver={"t:3": mock_track},
    )
    assert "メモ整理" in label
    assert "すること" in label


def test_digest_meta_judgment_activate_user_conversation():
    """track_activate で user_conversation Track → 「あなたと話す」"""
    mock_track = MagicMock()
    mock_track.track_type = "user_conversation"
    mock_track.title = "対 user1 会話"
    label = build_digest_label(
        track_title=None, track_type=None,
        line_roles=["meta_judgment"], tool_calls=[],
        spells_emitted=[{"name": "track_activate", "args": {"track_id": "t:1"}}],
        track_resolver={"t:1": mock_track},
    )
    assert "あなたと話す" in label


def test_digest_meta_judgment_activate_social():
    """track_activate で social Track → 「他のペルソナと話す」"""
    mock_track = MagicMock()
    mock_track.track_type = "social"
    mock_track.title = "対 bob 会話"
    label = build_digest_label(
        track_title=None, track_type=None,
        line_roles=["meta_judgment"], tool_calls=[],
        spells_emitted=[{"name": "track_activate", "args": {"track_id": "t:2"}}],
        track_resolver={"t:2": mock_track},
    )
    assert "他のペルソナ" in label


def test_digest_meta_judgment_track_create():
    """track_create → 「○○を始めることにした」"""
    label = build_digest_label(
        track_title=None, track_type=None,
        line_roles=["meta_judgment"], tool_calls=[],
        spells_emitted=[{"name": "track_create", "args": {"title": "Web調査"}}],
    )
    assert "Web調査" in label
    assert "始める" in label


def test_digest_meta_judgment_track_pause():
    label = build_digest_label(
        track_title=None, track_type=None,
        line_roles=["meta_judgment"], tool_calls=[],
        spells_emitted=[{"name": "track_pause", "args": {"track_id": "t:3"}}],
    )
    assert "中断" in label


# ---------------------------------------------------------------------------
# Playbook 名ベースの行動種別表示
# ---------------------------------------------------------------------------

def test_digest_playbook_names_with_display_names():
    """ツール実行なし + playbook 名あり → DB の display_name を使って表示"""
    label = build_digest_label(
        track_title="テスト",
        track_type="autonomous",
        line_roles=["sub_line"],
        tool_calls=[],
        playbook_names=["web_research", "track_autonomous"],
        playbook_display_names={"web_research": "Webリサーチ"},
    )
    assert "Webリサーチ" in label
    assert "を行った" in label


def test_digest_playbook_without_display_name_uses_raw():
    """display_name が DB に無い playbook は name をそのまま使う"""
    label = build_digest_label(
        track_title="テスト",
        track_type="autonomous",
        line_roles=["sub_line"],
        tool_calls=[],
        playbook_names=["custom_playbook"],
        playbook_display_names={},
    )
    assert "custom playbook" in label
    assert "を行った" in label


def test_digest_playbook_track_autonomous_skipped():
    """track_autonomous は自明なので playbook リストから除外"""
    label = build_digest_label(
        track_title="テスト",
        track_type="autonomous",
        line_roles=["sub_line"],
        tool_calls=[],
        playbook_names=["track_autonomous"],
    )
    # track_autonomous しかないので playbook ベースは空、Track タイトルに退避
    assert "テスト" in label
    assert "進めた" in label


# ---------------------------------------------------------------------------
# build_activity_label: 常在インジケータの短縮ラベル
# ---------------------------------------------------------------------------

def test_activity_label_from_title():
    assert build_activity_label("プロット推敲", None) == "プロット推敲"


def test_activity_label_falls_back_to_intent():
    assert build_activity_label(None, "記憶を整理する") == "記憶を整理する"


def test_activity_label_truncated():
    label = build_activity_label("とても長いタイトルのトラックです", None)
    assert len(label) <= 12
    assert label.endswith("…")


def test_activity_label_empty_fallback():
    assert build_activity_label(None, None) == "活動中"
