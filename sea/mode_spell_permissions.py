"""モード (aspect) 別スペル権限のゲート。

``docs/intent/persona_cognition/mode_spell_permissions.md`` (確定 v1.0) の実装。

権限マトリクス (§5):

| モード (aspect)        | Track 操作 | Task 操作 |
|------------------------|-----------|-----------|
| メインモード (CONVERSATION) | ✅        | ✅        |
| 自律制御モード (META)       | ✅        | ❌        |
| 自律作業モード (AUTONOMOUS) | ❌        | ✅        |
| 分身モード (WORKER)         | ❌        | ❌        |

- 読み取り系 (``track_list`` / ``get_task_summary`` 等) と汎用スペル
  (recall / note / memopedia / image / web 等) は全モード無制限 (= ここに載せない)。
- 生産手段の ``document_*`` スペル (create / read / edit / search) も汎用スペル
  扱いで全モード無制限。特に分身モード (WORKER) の
  作業セッションがアーティファクト生成に使う前提なので、ここに載せて
  制限しないこと (autonomous_behavior_v2.md §2.2 / §11)。
- 旧 ``task_*`` 系は廃止予定でゲート対象外。
- 判定は ``aspect`` をキーに行い、ゲット文の表示のみ ``mode_display_name`` を使う。
"""
from __future__ import annotations

from typing import Optional

from sea.pulse_context import Aspect

# Track ライフサイクル操作 (mutating)。読み取りの track_list 等は含めない。
TRACK_CONTROL_SPELLS = frozenset({
    "track_create",
    "track_activate",
    "track_complete",
    "track_abort",
    "track_pause",
    "track_parameter_set",
})

# Task 操作スペル (mutating)。タスク一本化 (unified_task_model.md) 後の統合スペル群。
# task は task:N 参照で指す (所属 track/note/なしを横断)。旧 standalone スペル
# (task_change_active / task_close / task_request_creation) は撤去された。
# P2c-4a: 旧 task_* / desire_add スペル自体が撤去された (concept_consolidation.md
# P2c-2 削除表) ので、ここのゲート対象も purpose_* のみに揃える。
TASK_CONTROL_SPELLS = frozenset({
    "purpose_seed",       # 候補を生む (旧 desire_add 後継)
    "purpose_adopt",      # 候補を木に接ぐ / 枝に小目標を作る (旧 task_add 後継)
    "purpose_decompose",  # 目的ノードをステップに分解 (旧 task_decompose 後継)
    "purpose_step",       # 目的ノードのステップ更新 (旧 task_update_step 後継)
    "purpose_close",      # 完了・中止・休眠 (旧 task_done 後継)
})

# 自己定義スペル (生きる目的の設定)。AUTONOMOUS の軽量モデルが勝手に生きる目的を
# 書き換えるのを防ぐため、Track 操作と同じく META / CONVERSATION のみ許可する
# (autonomous_desire.md §4)。
SELF_DEFINITION_SPELLS = frozenset({
    "life_purpose_set",   # 生きる目的 / 趣味 / 仕事 の保存
})

# 各カテゴリを使える aspect (§4 能力レイヤーから導出 / §5 マトリクス)。
_TRACK_ALLOWED_ASPECTS = frozenset({Aspect.CONVERSATION, Aspect.META})
_TASK_ALLOWED_ASPECTS = frozenset({Aspect.CONVERSATION, Aspect.AUTONOMOUS})
# 自己定義は Track 操作と同じ許可 (META / CONVERSATION)。
_SELF_DEFINITION_ALLOWED_ASPECTS = _TRACK_ALLOWED_ASPECTS


def check_spell_permission(spell_name: str, aspect: Optional[Aspect]) -> Optional[str]:
    """スペルが現在の ``aspect`` で実行可能か判定する。

    Args:
        spell_name: 実行しようとしているスペル名。
        aspect: アクティブなラインのアスペクト。``None`` (legacy frame /
            アスペクト不明) のときは制限しない。

    Returns:
        ``None``: 許可 (ゲート対象外のスペル、または許可された aspect)。
        ``str``: 不許可。ペルソナに返すゲット文 (§6.3)。
    """
    if aspect is None:
        return None
    if spell_name in TRACK_CONTROL_SPELLS and aspect not in _TRACK_ALLOWED_ASPECTS:
        return _build_block_message(spell_name, aspect)
    if spell_name in TASK_CONTROL_SPELLS and aspect not in _TASK_ALLOWED_ASPECTS:
        return _build_block_message(spell_name, aspect)
    if spell_name in SELF_DEFINITION_SPELLS and aspect not in _SELF_DEFINITION_ALLOWED_ASPECTS:
        return _build_block_message(spell_name, aspect)
    return None


def _build_block_message(spell_name: str, aspect: Aspect) -> str:
    """ゲット文 (§6.3 確定文面)。"""
    return (
        f"{spell_name}スペルは現在のモード（{aspect.mode_display_name}）では実行できません。"
    )
