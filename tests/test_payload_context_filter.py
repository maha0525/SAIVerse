"""Phase 3 段階 4-A: `_payload_passes_context_filter` の網羅単体テスト。

`saiverse_memory/adapter.py` で定義されるフィルタヘルパは、context 構築経路
(history 系 4 関数: recent_persona_messages / _by_count / _balanced /
persona_messages_from_anchor) で共通利用される。仕様:

- Pulse-scoped overrides (line/scope より優先):
  - exclude_pulse_id 一致 → 強制除外
  - pulse_id 一致 → 強制包含 (line/scope/tag フィルタを無視)
- Line-role / scope フィルタ (preferred):
  - required_line_roles 指定時、payload の line_role がリストに含まれること
  - required_scopes 指定時、payload の scope がリストに含まれること
  - legacy 互換: line_role IS NULL → 'main_line' 扱い、scope IS NULL → 'committed' 扱い
- Tag フィルタ (search/recall 互換、4-D まで残置):
  - required_tags 指定時、payload tags に少なくとも 1 つ含まれること
  - legacy 行 (tags 空) は required_tags に "conversation" を含まない場合のみ包含

旧 `tags=["conversation"]` ハードコードを line ベースに置換した移行 (4-A) の
要となるロジックなので、legacy 互換 + override 優先順序を網羅的に検証する。
"""
from saiverse_memory.adapter import _payload_passes_context_filter


# ---------------------------------------------------------------------------
# 0. フィルタなし
# ---------------------------------------------------------------------------

def test_no_filters_passes_any_payload():
    payload = {"content": "x", "line_role": "main_line", "scope": "committed"}
    assert _payload_passes_context_filter(payload) is True


def test_no_filters_passes_legacy_payload_with_no_line_metadata():
    payload = {"content": "legacy", "metadata": {"tags": ["conversation"]}}
    assert _payload_passes_context_filter(payload) is True


# ---------------------------------------------------------------------------
# 1. line_role フィルタ
# ---------------------------------------------------------------------------

def test_line_role_main_line_passes():
    payload = {"line_role": "main_line", "scope": "committed"}
    assert _payload_passes_context_filter(
        payload, required_line_roles=["main_line"]
    ) is True


def test_line_role_sub_line_rejected_by_main_line_filter():
    payload = {"line_role": "sub_line", "scope": "committed"}
    assert _payload_passes_context_filter(
        payload, required_line_roles=["main_line"]
    ) is False


def test_line_role_meta_judgment_rejected_by_main_line_filter():
    payload = {"line_role": "meta_judgment", "scope": "discardable"}
    assert _payload_passes_context_filter(
        payload, required_line_roles=["main_line"]
    ) is False


def test_line_role_null_treated_as_main_line_for_legacy_compat():
    """legacy 行 (line_role IS NULL) は 'main_line' 扱い。Phase 1 以前のデータが
    context に載るためのフォールバック。"""
    payload = {"content": "legacy", "scope": "committed"}
    assert _payload_passes_context_filter(
        payload, required_line_roles=["main_line"]
    ) is True


def test_line_role_null_rejected_when_main_line_not_required():
    """legacy 互換は ['main_line'] が required にあるときだけ。
    ['sub_line'] のみ要求時には legacy NULL は通らない (sub_line ではないため)。"""
    payload = {"content": "legacy", "scope": "committed"}
    assert _payload_passes_context_filter(
        payload, required_line_roles=["sub_line"]
    ) is False


def test_line_role_multi_value_filter():
    """include_internal=True 暫定挙動: ['main_line', 'sub_line'] で sub_line も通す。"""
    payload = {"line_role": "sub_line", "scope": "committed"}
    assert _payload_passes_context_filter(
        payload, required_line_roles=["main_line", "sub_line"]
    ) is True


# ---------------------------------------------------------------------------
# 2. scope フィルタ
# ---------------------------------------------------------------------------

def test_scope_committed_passes():
    payload = {"line_role": "main_line", "scope": "committed"}
    assert _payload_passes_context_filter(
        payload, required_scopes=["committed"]
    ) is True


def test_scope_discardable_rejected_by_committed_filter():
    payload = {"line_role": "meta_judgment", "scope": "discardable"}
    assert _payload_passes_context_filter(
        payload, required_scopes=["committed"]
    ) is False


def test_scope_volatile_rejected_by_committed_filter():
    payload = {"line_role": "sub_line", "scope": "volatile"}
    assert _payload_passes_context_filter(
        payload, required_scopes=["committed"]
    ) is False


def test_scope_null_treated_as_committed_for_legacy_compat():
    """legacy 行 (scope IS NULL) は 'committed' 扱い。schema レベルの
    NOT NULL DEFAULT 'committed' により実 DB では NULL は出ないが、
    payload 構築時に scope キーを含めない経路への保険。"""
    payload = {"line_role": "main_line"}
    assert _payload_passes_context_filter(
        payload, required_scopes=["committed"]
    ) is True


# ---------------------------------------------------------------------------
# 3. line_role + scope の組み合わせ
# ---------------------------------------------------------------------------

def test_line_role_and_scope_both_required():
    payload = {"line_role": "main_line", "scope": "committed"}
    assert _payload_passes_context_filter(
        payload,
        required_line_roles=["main_line"],
        required_scopes=["committed"],
    ) is True


def test_line_role_pass_but_scope_fail_rejected():
    payload = {"line_role": "main_line", "scope": "discardable"}
    assert _payload_passes_context_filter(
        payload,
        required_line_roles=["main_line"],
        required_scopes=["committed"],
    ) is False


# ---------------------------------------------------------------------------
# 4. pulse_id override (= 強制包含)
# ---------------------------------------------------------------------------

def test_pulse_id_match_overrides_line_role_filter():
    """pulse_id 一致は line_role/scope を無視して強制包含。
    Pulse 内のメッセージは何であろうと自分の Pulse プロンプトに載る。"""
    payload = {
        "line_role": "sub_line",
        "scope": "volatile",
        "pulse_id": "p1",
    }
    assert _payload_passes_context_filter(
        payload,
        required_line_roles=["main_line"],
        required_scopes=["committed"],
        pulse_id="p1",
    ) is True


def test_pulse_id_match_overrides_scope_discardable():
    """meta_judgment の discardable も同 Pulse 内なら包含 (Phase 2 挙動維持)。"""
    payload = {
        "line_role": "meta_judgment",
        "scope": "discardable",
        "pulse_id": "p1",
    }
    assert _payload_passes_context_filter(
        payload,
        required_line_roles=["main_line"],
        required_scopes=["committed"],
        pulse_id="p1",
    ) is True


def test_pulse_id_match_via_legacy_pulse_tag():
    """payload に pulse_id カラム値が無くても、metadata.tags の "pulse:{uuid}"
    タグ経由で一致を判定 (Phase 2.5 バックフィル前のデータ向けフォールバック)。"""
    payload = {
        "line_role": "sub_line",
        "scope": "volatile",
        "metadata": {"tags": ["pulse:legacy-uuid"]},
    }
    assert _payload_passes_context_filter(
        payload,
        required_line_roles=["main_line"],
        pulse_id="legacy-uuid",
    ) is True


# ---------------------------------------------------------------------------
# 5. exclude_pulse_id (= 強制除外、最強)
# ---------------------------------------------------------------------------

def test_exclude_pulse_id_match_overrides_everything():
    payload = {
        "line_role": "main_line",
        "scope": "committed",
        "pulse_id": "p_other",
    }
    assert _payload_passes_context_filter(
        payload,
        required_line_roles=["main_line"],
        required_scopes=["committed"],
        exclude_pulse_id="p_other",
    ) is False


def test_exclude_pulse_id_wins_over_include_pulse_id():
    """同じ pulse_id を pulse_id と exclude_pulse_id 両方に渡された時、
    exclude が勝つ (除外)。実用上は起きないが、優先順位の保険。"""
    payload = {"line_role": "main_line", "scope": "committed", "pulse_id": "p1"}
    assert _payload_passes_context_filter(
        payload,
        pulse_id="p1",
        exclude_pulse_id="p1",
    ) is False


def test_exclude_pulse_id_via_legacy_tag():
    payload = {
        "line_role": "main_line",
        "scope": "committed",
        "metadata": {"tags": ["pulse:legacy-pp"]},
    }
    assert _payload_passes_context_filter(
        payload,
        exclude_pulse_id="legacy-pp",
    ) is False


# ---------------------------------------------------------------------------
# 6. required_tags (search/recall 互換、4-D まで残置)
# ---------------------------------------------------------------------------

def test_required_tags_match():
    payload = {"line_role": "main_line", "metadata": {"tags": ["conversation", "audit"]}}
    assert _payload_passes_context_filter(
        payload, required_tags=["audit"]
    ) is True


def test_required_tags_no_match():
    payload = {"line_role": "main_line", "metadata": {"tags": ["conversation"]}}
    assert _payload_passes_context_filter(
        payload, required_tags=["audit"]
    ) is False


def test_required_tags_legacy_no_tags_included_when_conversation_not_required():
    """legacy 行 (tags 空) は required_tags に 'conversation' が無いなら通す。
    既存の adapter 旧仕様の挙動を保持 (4-D で完全廃止予定)。"""
    payload = {"line_role": "main_line", "scope": "committed"}
    assert _payload_passes_context_filter(
        payload, required_tags=["audit"]
    ) is True


def test_required_tags_legacy_no_tags_excluded_when_conversation_required():
    payload = {"line_role": "main_line", "scope": "committed"}
    assert _payload_passes_context_filter(
        payload, required_tags=["conversation"]
    ) is False


# ---------------------------------------------------------------------------
# 7. line + tag 同時指定 (移行期間中の併用想定)
# ---------------------------------------------------------------------------

def test_line_filter_pass_but_tag_filter_fail_rejected():
    payload = {
        "line_role": "main_line",
        "scope": "committed",
        "metadata": {"tags": ["audit"]},
    }
    assert _payload_passes_context_filter(
        payload,
        required_line_roles=["main_line"],
        required_tags=["other_tag"],
    ) is False


def test_line_filter_and_tag_filter_both_pass():
    payload = {
        "line_role": "main_line",
        "scope": "committed",
        "metadata": {"tags": ["audit"]},
    }
    assert _payload_passes_context_filter(
        payload,
        required_line_roles=["main_line"],
        required_scopes=["committed"],
        required_tags=["audit"],
    ) is True


# ---------------------------------------------------------------------------
# 8. metadata 形式の壊れた payload (防御的)
# ---------------------------------------------------------------------------

def test_payload_with_non_dict_metadata_does_not_crash():
    payload = {"line_role": "main_line", "scope": "committed", "metadata": "broken"}
    # metadata が dict でなくても落ちない (filter は通す)
    assert _payload_passes_context_filter(
        payload, required_line_roles=["main_line"]
    ) is True


def test_payload_with_non_list_tags_does_not_crash():
    payload = {
        "line_role": "main_line",
        "scope": "committed",
        "metadata": {"tags": "broken"},
    }
    # tags が list でなくても落ちない (空 tags 扱い)
    assert _payload_passes_context_filter(
        payload, required_line_roles=["main_line"]
    ) is True
